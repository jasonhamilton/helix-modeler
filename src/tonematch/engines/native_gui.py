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


def _texts(view) -> list[str]:
    """Strings shown by text fields under ``view`` (an NSAlert's message and details)."""
    out: list[str] = []
    if view is None:
        return out
    if hasattr(view, "stringValue") and view.className() in ("NSTextField", "NSTextView") or \
            str(view.className()).endswith("TextField"):
        v = str(view.stringValue() or "").strip()
        if v:
            out.append(v)
    for sub in view.subviews() or []:
        out += _texts(sub)
    return out


def _png(view, path: str) -> None:
    import AppKit

    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
    data.writeToFile_atomically_(path, True)


_CLASSES = None


def _classes():
    """The ObjC helper classes, defined once per process (PyObjC refuses a second definition)."""
    global _CLASSES
    if _CLASSES is None:
        _CLASSES = _define_classes()
    return _CLASSES


def _define_classes():
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

        def _editor(self):
            """The editor window: the first window holding an XGView that is not a dialog
            (remembered once found, so a dialog drawn with an XGView is never mistaken for it)."""
            ed = self.job.get("_editor_win")
            if ed is not None:
                return ed, _find(ed.contentView(), "XGView")
            for w in AppKit.NSApp.windows():
                v = _find(w.contentView(), "XGView") if w.contentView() else None
                if v is not None and w.className() != "L6DialogWindow":
                    self.job["_editor_win"] = w
                    return w, v
            return None, None

        def _others(self):
            ed, _ = self._editor()
            return [w for w in AppKit.NSApp.windows() if w.isVisible() and w is not ed]

        def probe_(self, _):
            self.job["view_ready"] = self._editor()[1] is not None

        def drop_(self, _):
            job = self.job
            try:
                win, view = self._editor()
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
            finally:
                job["drop_done"] = True

        def after_(self, _):
            """Count every visible window other than the editor (Native's HC-mode question,
            a "failed to load" message, any modal), describe and optionally snapshot them."""
            job = self.job
            others = self._others()
            job["dialogs"] = len(others)
            job["windows"] = [{"cls": str(w.className()), "title": str(w.title() or ""),
                               "text": _texts(w.contentView())} for w in others]
            job["modal"] = AppKit.NSApp.modalWindow() is not None
            snap = job.get("snap_prefix") or job.get("dialog_snap_prefix")
            if snap and others:
                for i, w in enumerate(others):
                    if w.contentView() is not None:
                        _png(w.contentView(), snap + f"_dialog{i}.png")
            if job.get("snap_prefix"):
                _, v = self._editor()
                if v is not None:
                    _png(v, job["snap_prefix"] + "_after.png")

        def dismiss_(self, _):
            """Close every window other than the editor and leave any modal loop, so no dialog
            is ever left on screen (the host is closed after the session anyway)."""
            if AppKit.NSApp.modalWindow() is not None:
                AppKit.NSApp.abortModal()
            for w in self._others():
                w.orderOut_(None)
                w.close()
            self.job["dismissed"] = self.job.get("dismissed", 0) + 1

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


def drop_files(plugin, paths: list[str], point=DEFAULT_DROP_POINT, open_timeout_s: float = 20.0,
               settle_s: float = 0.3, load_timeout_s: float = 8.0, poll_s: float = 0.1,
               stable_polls: int = 2, drop_timeout_s: float = 10.0,
               dialog_snap_prefix: str | None = None) -> dict:
    """Open the editor once and drop each of ``paths`` on it in turn (phase 3 capture speed-up).

    Waits for the editor view by polling (not a fixed sleep), then for each file drops it and
    polls ``plugin.raw_state`` until it differs from the state before the drop and has stayed
    the same for ``stable_polls`` polls. Returns ``{"results": [{"path", "drop", "dialogs",
    "windows", "state" (bytes) or "error", "load_s"}...], "editor_s", "open_s"}``.

    A rejected load is detected without a human: any visible window besides the editor
    (Native's "failed to load" message, the HC-mode question), a drop that does not return
    within ``drop_timeout_s`` (Native running a modal loop), or a state that did not change.
    The window is snapshotted (``dialog_snap_prefix``), closed, and the session stops at that
    file; the caller retries the files after it in a new host. Must run on the main thread.
    """
    _, TMDriver = _classes()
    job: dict = {"point": tuple(point), "snap_prefix": None, "results": [],
                 "dialog_snap_prefix": dialog_snap_prefix}
    drv = TMDriver.alloc().initWithJob_(job)
    close = threading.Event()

    def on_main(sel: bytes, wait: bool = True) -> None:
        drv.performSelectorOnMainThread_withObject_waitUntilDone_(sel, None, wait)

    def fail(res: dict, why: str) -> None:
        res["error"] = why
        on_main(b"dismiss:", wait=False)  # runs inside a modal loop too (common modes)
        t = time.perf_counter()
        while time.perf_counter() - t < 2.0 and job.get("dismissed", 0) < 1:
            time.sleep(0.05)
        job["results"].append(res)

    def worker():
        try:
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < open_timeout_s:
                on_main(b"probe:")
                if job.get("view_ready"):
                    break
                time.sleep(poll_s)
            job["open_s"] = round(time.perf_counter() - t0, 2)
            if not job.get("view_ready"):
                job["error"] = "Native editor view (XGView) did not appear"
                return
            time.sleep(settle_s)
            for k, path in enumerate(paths):
                if k and str(path) == str(paths[k - 1]) and "state" in job["results"][-1]:
                    job["results"].append({**job["results"][-1], "load_s": 0.0})  # same file again
                    continue
                before = bytes(plugin.raw_state)
                job["path"] = str(path)
                for key in ("drop", "error", "drop_done", "dialog_snap_prefix"):
                    job.pop(key, None)
                if dialog_snap_prefix:
                    job["dialog_snap_prefix"] = f"{dialog_snap_prefix}_{k:03d}"
                t1 = time.perf_counter()
                on_main(b"drop:", wait=False)
                while not job.get("drop_done") and time.perf_counter() - t1 < drop_timeout_s:
                    time.sleep(0.02)
                res = {"path": str(path), "drop": job.get("drop")}
                if not job.get("drop_done"):
                    on_main(b"after:", wait=False)
                    time.sleep(0.2)
                    res.update(dialogs=job.get("dialogs", 0), windows=job.get("windows", []))
                    fail(res, f"drop did not return in {drop_timeout_s:.0f} s (modal dialog)")
                    return
                if job.get("error"):
                    fail(res, job["error"])
                    return
                last, same = None, 0
                while time.perf_counter() - t1 < load_timeout_s:
                    time.sleep(poll_s)
                    st = bytes(plugin.raw_state)
                    if st != before and st == last:
                        same += 1
                        if same >= stable_polls:
                            break
                    else:
                        same = 0
                    last = st
                on_main(b"after:")
                res["dialogs"] = job.get("dialogs", 0)
                res["windows"] = job.get("windows", [])
                res["load_s"] = round(time.perf_counter() - t1, 2)
                if res["dialogs"] or last is None or last == before:
                    why = (f"dialog shown: {res['windows']}" if res["dialogs"] else "state did not change")
                    fail(res, why)
                    return
                res["state"] = last
                job["results"].append(res)
        finally:
            close.set()

    threading.Thread(target=worker, daemon=True).start()
    t0 = time.perf_counter()
    plugin.show_editor(close)
    job["editor_s"] = round(time.perf_counter() - t0, 2)
    return {k: job[k] for k in ("results", "editor_s", "open_s", "error") if k in job}
