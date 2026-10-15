"""In-process file drop onto Helix Native's editor (macOS), imported only by ``native_host``.

T018n prototype. Native's editor is one custom-drawn ``XGView`` (Line 6's UI framework) with
no accessibility children, so System Events / AppleScript can only click blind coordinates and
needs the Accessibility permission. The view does register ``public.file-url`` as a drag type,
and the Pilot's Guide documents "drag a preset file into the Signal Flow" as a way to *load* a
preset. The editor lives in our own host process, so the host can call the view's public
``NSDraggingDestination`` methods directly with a file-URL pasteboard. That is ordinary AppKit
use inside our own process: no TCC permission, no synthetic OS events, no Line 6 code touched.

Requirements: macOS, a logged-in GUI session (the editor window opens on screen for a few
seconds), PyObjC (``pyobjc-framework-Cocoa``, MIT) in the host interpreter. Not Windows/Wine
(an IDropTarget equivalent would be needed; untested).

Known behaviour (Native 3.82): if the file's device does not match the instance's Hardware
Compatibility mode, Native opens an ``L6DialogWindow`` asking whether to change mode; the load
waits on it. Start from a state already in the file's HC mode, or have the human tick
"Don't ask me again" once (Preferences: ``ChangeHWModeForFile``).
"""
from __future__ import annotations

import threading
import time

# Signal Flow area, XGView coordinates (bottom-left origin) for the 1034 x 656 editor of 3.82.
DEFAULT_DROP_POINT = (650.0, 584.0)


def _find(view, cls: str):
    if view.className() == cls:
        return view
    for s in view.subviews():
        r = _find(s, cls)
        if r is not None:
            return r
    return None


def _png(view, path: str) -> None:
    import AppKit

    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
    data.writeToFile_atomically_(path, True)


def _classes():
    import AppKit
    import objc
    from Foundation import NSObject

    class TMDragInfo(NSObject):
        """Just enough of NSDraggingInfo for a file drop (duck-typed, no protocol declaration)."""

        def initWithPasteboard_window_point_(self, pb, win, pt):
            obj = objc.super(TMDragInfo, self).init()
            obj._pb, obj._win, obj._pt = pb, win, pt
            return obj

        def draggingPasteboard(self):
            return self._pb

        @objc.typedSelector(b"{CGPoint=dd}@:")
        def draggingLocation(self):
            return self._pt

        @objc.typedSelector(b"{CGPoint=dd}@:")
        def draggedImageLocation(self):
            return self._pt

        @objc.typedSelector(b"Q@:")
        def draggingSourceOperationMask(self):
            return AppKit.NSDragOperationCopy | AppKit.NSDragOperationGeneric | AppKit.NSDragOperationLink

        def draggingSource(self):
            return None

        def draggingDestinationWindow(self):
            return self._win

        @objc.typedSelector(b"q@:")
        def draggingSequenceNumber(self):
            return 18018

        def draggedImage(self):
            return None

        @objc.typedSelector(b"q@:")
        def numberOfValidItemsForDrop(self):
            return 1

        @objc.typedSelector(b"v@:q")
        def setNumberOfValidItemsForDrop_(self, n):
            pass

        @objc.typedSelector(b"B@:")
        def animatesToDestination(self):
            return False

        @objc.typedSelector(b"v@:B")
        def setAnimatesToDestination_(self, v):
            pass

        @objc.typedSelector(b"q@:")
        def draggingFormation(self):
            return 0

        @objc.typedSelector(b"v@:q")
        def setDraggingFormation_(self, v):
            pass

        @objc.typedSelector(b"q@:")
        def springLoadingHighlight(self):
            return 0

        def resetSpringLoading(self):
            pass

        @objc.typedSelector(b"v@:{CGPoint=dd}")
        def slideDraggedImageTo_(self, pt):
            pass

    class TMDriver(NSObject):
        def initWithJob_(self, job):
            obj = objc.super(TMDriver, self).init()
            obj.job = job
            return obj

        def drop_(self, _):
            job = self.job
            win, view = None, None
            for w in AppKit.NSApp.windows():
                v = _find(w.contentView(), "XGView") if w.contentView() else None
                if v is not None and w.className() != "L6DialogWindow":
                    win, view = w, v
                    break
            if view is None:
                job["error"] = "Native editor view (XGView) not found"
                return
            if job.get("snap_prefix"):
                _png(view, job["snap_prefix"] + "_before.png")
            pb = AppKit.NSPasteboard.pasteboardWithUniqueName()
            pb.declareTypes_owner_(["public.file-url", "NSFilenamesPboardType"], None)
            from Foundation import NSURL

            pb.setString_forType_(NSURL.fileURLWithPath_(job["path"]).absoluteString(), "public.file-url")
            pb.setPropertyList_forType_([job["path"]], "NSFilenamesPboardType")
            x, y = job["point"]
            pt = view.convertPoint_toView_(AppKit.NSMakePoint(x, y), None)
            info = TMDragInfo.alloc().initWithPasteboard_window_point_(pb, win, pt)
            r = {"entered": int(view.draggingEntered_(info)),
                 "updated": int(view.draggingUpdated_(info))}
            r["prepare"] = bool(view.prepareForDragOperation_(info))
            r["perform"] = bool(view.performDragOperation_(info))
            view.concludeDragOperation_(info)
            job["drop"] = r

        def after_(self, _):
            job = self.job
            dialogs = [w for w in AppKit.NSApp.windows()
                       if w.className() == "L6DialogWindow" and w.isVisible()]
            job["dialogs"] = len(dialogs)
            if job.get("snap_prefix"):
                for i, w in enumerate(dialogs):
                    _png(w.contentView(), job["snap_prefix"] + f"_dialog{i}.png")
                for w in AppKit.NSApp.windows():
                    v = _find(w.contentView(), "XGView") if w.contentView() else None
                    if v is not None and w.className() != "L6DialogWindow":
                        _png(v, job["snap_prefix"] + "_after.png")
                        break

    return TMDragInfo, TMDriver


def drop_file(plugin, path: str, point=DEFAULT_DROP_POINT, open_wait_s: float = 8.0,
              load_wait_s: float = 4.0, snap_prefix: str | None = None,
              auto_close: bool = True) -> dict:
    """Open ``plugin``'s editor, drop ``path`` on it, wait, close. Must run on the main thread.

    Returns ``{"drop": {...}, "dialogs": n, "editor_s": s}``; ``dialogs > 0`` means Native is
    asking a question (usually the HC-mode prompt) and the file was probably not loaded.
    With ``auto_close=False`` the window stays open after the drop until the human closes it
    (used once, so the human can answer Native's HC-mode question).
    """
    _, TMDriver = _classes()
    job = {"path": str(path), "point": tuple(point), "snap_prefix": snap_prefix}
    drv = TMDriver.alloc().initWithJob_(job)
    close = threading.Event()

    def worker():
        try:
            time.sleep(open_wait_s)
            drv.performSelectorOnMainThread_withObject_waitUntilDone_(b"drop:", None, True)
            time.sleep(load_wait_s)
            drv.performSelectorOnMainThread_withObject_waitUntilDone_(b"after:", None, True)
        finally:
            if auto_close:
                close.set()

    threading.Thread(target=worker, daemon=True).start()
    t0 = time.perf_counter()
    plugin.show_editor(close)
    job["editor_s"] = round(time.perf_counter() - t0, 2)
    job.pop("snap_prefix", None)
    return job
