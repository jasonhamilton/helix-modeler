"""Client for the long-lived HX Stomp control helper (`stompd`), JSON lines over stdin/stdout.

The helper is a small original Rust wrapper over tonepush's `hx-usb` crates (MIT), built outside this
repo (its source is not published here; protocol and build notes: docs/notes/stompd.md):
it holds one USB session on interface 0, sends keepalives while idle, and exposes edit-buffer
operations only (no save, rename, slot write, settings write or IR command). Its path comes from
`TONEMATCH_STOMPD`. Protocol interop only; no Line 6 code or data is involved here.

One session per process: opening and closing a session per command is the main wedge risk
(docs/notes/T003-stomp-control-spike.md), so callers keep one `StompBridge` for a whole run.

Session end (T059). A session that ends without HX Edit's session-close leaves the pedal's block
editing locked until USB is unplugged. The helper (build of 2026-10-15 on) sends that close on
exit and reports it as a `closed` event. As a fallback, `close()` can then re-enumerate the
pedal's USB connection (`stompd reset`), the software version of an unplug. Mode from
`TONEMATCH_STOMP_USB_RESET` or `StompBridge(usb_reset=...)`:
- `auto` (default): reset only when a session was open and the helper could not confirm the
  release (older helper, helper killed, or no close frame sent);
- `always`: reset after every session;
- `off`: never.
Never while audio is streaming through `tonematch.reamp`, never on a wedged pedal (a reset does
not clear a wedge; only the 9V does). tonepush's notes warn a reset once took an HX Stomp off the
bus until a physical replug, so a reset that does not bring the pedal back tells the user to
replug the USB cable.
"""
from __future__ import annotations

import collections
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

from .base import EngineError

ENV_HELPER = "TONEMATCH_STOMPD"
ENV_USB_RESET = "TONEMATCH_STOMP_USB_RESET"
USB_RESET_MODES = ("auto", "always", "off")
DEFAULT_USB_RESET = "auto"
REPLUG_MESSAGE = ("The HX Stomp did not come back after the software USB reset. Unplug its USB cable "
                  "and plug it back in (the 9V adapter can stay in), then check that its blocks can be "
                  "edited on the pedal.")
VENDOR_ID = 0x0E41
PRODUCT_ID_HX_STOMP = 0x4246


class BridgeError(EngineError):
    """The helper reported an error, timed out or exited."""


POWER_CYCLE_MESSAGE = (
    "The HX Stomp is not answering on USB. Switch the pedal off and on (pull its 9V adapter for a "
    "few seconds and plug it back in), wait until it has finished booting and shows a preset, then "
    "run this again. Nothing was written to the pedal. (A USB replug does not clear this.)")


class PowerCycleNeeded(BridgeError):
    """Pre-run health check failed: the pedal does not answer. The message is for the user."""

    def __init__(self, detail: str = ""):
        super().__init__(POWER_CYCLE_MESSAGE)
        self.detail = detail


def usb_reset_mode(explicit: str | None = None) -> str:
    """The session-end USB reset mode: the argument, else TONEMATCH_STOMP_USB_RESET, else auto."""
    mode = (explicit or os.environ.get(ENV_USB_RESET) or DEFAULT_USB_RESET).strip().lower()
    if mode in ("0", "no", "false", "never"):
        mode = "off"
    if mode in ("1", "yes", "true", "on"):
        mode = "always"
    if mode not in USB_RESET_MODES:
        raise BridgeError(f"{ENV_USB_RESET} must be one of {USB_RESET_MODES}, got {mode!r}")
    return mode


def add_usb_reset_arg(parser: Any) -> None:
    """`--usb-reset {auto,always,off}` for scripts that end Stomp sessions (sets the env default)."""
    parser.add_argument("--usb-reset", choices=USB_RESET_MODES, default=None,
                        help=f"software USB reset after each pedal session (default: ${ENV_USB_RESET} or "
                             f"{DEFAULT_USB_RESET}: only when the helper could not send HX Edit's session-close)")


def apply_usb_reset_arg(args: Any) -> None:
    mode = getattr(args, "usb_reset", None)
    if mode:
        os.environ[ENV_USB_RESET] = usb_reset_mode(mode)


def _reamp_audio_active() -> bool:
    try:
        from .. import reamp
    except Exception:  # noqa: BLE001 - no reamp module, no stream
        return False
    return reamp.audio_active()


def helper_path() -> Path | None:
    """The `stompd` binary, from TONEMATCH_STOMPD or PATH."""
    env = os.environ.get(ENV_HELPER)
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() and os.access(p, os.X_OK) else None
    found = shutil.which("stompd")
    return Path(found) if found else None


def stomp_on_bus() -> bool:
    """True if an HX Stomp is enumerated on USB. Claims nothing."""
    try:
        import platform

        if platform.system() == "Darwin":
            out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w0"], check=False, capture_output=True,
                                 text=True, timeout=10).stdout
            return f'"idVendor" = {VENDOR_ID}' in out and f'"idProduct" = {PRODUCT_ID_HX_STOMP}' in out
        if platform.system() == "Linux":
            for dev in Path("/sys/bus/usb/devices").glob("*"):
                try:
                    v = (dev / "idVendor").read_text().strip()
                    p = (dev / "idProduct").read_text().strip()
                except OSError:
                    continue
                if int(v, 16) == VENDOR_ID and int(p, 16) == PRODUCT_ID_HX_STOMP:
                    return True
            return False
    except Exception:  # noqa: BLE001 - detection must never raise
        return False
    return False


# Commands this client will send. Edit-buffer operations and reads only: nothing here saves,
# renames, writes a slot, writes a setting or touches IRs, whatever the helper binary offers.
ALLOWED_COMMANDS = frozenset({"info", "chain", "backup", "restore", "select", "model", "set", "enable",
                              "route_key", "quit"})
# The whole-document write (op 21, `restore` in the helper, EDIT BUFFER only). By default it may
# only write back a backup this same session took (close() putting a dirty buffer back). Writing
# any other document, such as one compiled from a generated .hlx (T018s), needs a bridge opened
# with allow_document_write=True.
DOCUMENT_WRITE = "restore"
WEDGE_HINT = ("If the pedal no longer answers (here or on its own screen), it may be wedged: pull the "
              "HX Stomp's 9V adapter for a few seconds and plug it back in. A USB replug does not "
              "clear it. Do not retry in a loop.")


class StompBridge:
    """One `stompd` process and one device session.

    Never retries an open. After a timeout or a lost session the helper is killed, so a late
    reply can never be read as the answer to a later request; the bridge is then dead and every
    further call raises."""

    HEALTH_TIMEOUT_S = 3.0   # a healthy pedal answers `info` in well under 0.1 s
    KILL_GRACE_S = 3.0       # after stdin closes, time for the helper to send the session-close
    CLOSE_TIMEOUT_S = 10.0   # quit -> `closed` report (the close itself takes about 1 s)
    RESET_WAIT_S = 15.0      # how long `stompd reset` waits for the pedal to re-enumerate
    AUDIO_WAIT_S = 30.0      # a reset waits this long for a live stream to end, then is skipped

    def __init__(self, binary: Path | None = None, open_timeout_s: float = 20.0,
                 allow_document_write: bool = False, health_check: bool = True,
                 usb_reset: str | None = None, audio_busy: Callable[[], bool] | None = None) -> None:
        self.allow_document_write = allow_document_write
        self._backups: set[str] = set()
        self.usb_reset = usb_reset_mode(usb_reset)
        self._audio_busy = audio_busy or _reamp_audio_active
        self._opened = False      # a session was brought up (ready received)
        self._wedged = False      # the pedal did not answer: a reset cannot help, only the 9V
        self._ended = False
        self.release_report: dict[str, Any] | None = None   # the helper's `closed` event
        self.reset_report: dict[str, Any] | None = None     # what close() did about a USB reset
        exe = binary or helper_path()
        if exe is None:
            raise BridgeError(f"stompd helper not found; it is built outside this repo (see docs/notes/stompd.md), "
                              f"then set {ENV_HELPER} to its path")
        self._exe = Path(exe)
        self._proc = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, bufsize=1)
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(maxlen=200)
        self._dead: str | None = None
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._lock = threading.Lock()
        try:
            ready = self._next(open_timeout_s)
        except BridgeError as e:
            self._kill()
            if "did not answer" in str(e):   # no ready line at all: the pedal hangs the open
                raise PowerCycleNeeded(str(e)) from None
            raise
        if not ready.get("ok") or ready.get("event") != "ready":
            self._kill()
            raise BridgeError(f"stompd did not open the device: {ready} {self._stderr_tail()} "
                              "(is HX Edit running? it holds the control interface)")
        self.ready: dict[str, Any] = ready
        self._opened = True
        if health_check:
            self.health_check()

    def health_check(self, timeout_s: float | None = None) -> dict[str, Any]:
        """Pre-run check: `info` must answer within a few seconds. On no answer (or a dead
        session) raise PowerCycleNeeded, whose message tells the user to power-cycle the pedal.
        Run before anything is written; never retried in a loop."""
        try:
            return self.call("info", timeout_s=timeout_s or self.HEALTH_TIMEOUT_S)
        except PowerCycleNeeded:
            raise
        except BridgeError as e:
            if "did not answer" in str(e) or "dead" in str(e) or "lost" in str(e) or "exited" in str(e):
                self._wedged = True
                self.close()
                raise PowerCycleNeeded(str(e)) from None
            raise

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _pump_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:  # drained continuously so the helper never blocks on it
            self._stderr.append(line)

    def _fail(self, why: str) -> BridgeError:
        """Kill the helper (no stale replies can follow) and build the error to raise."""
        self._dead = why
        self._kill()
        return BridgeError(f"{why}. {WEDGE_HINT} stderr: {self._stderr_tail()}")

    def _next(self, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise self._fail(f"stompd did not answer within {timeout_s:.0f} s")
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                raise self._fail(f"stompd did not answer within {timeout_s:.0f} s") from None
            if line is None:
                raise self._fail("stompd exited unexpectedly")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("event") == "lost":
                raise self._fail(f"device session lost: {msg.get('error')}")
            if msg.get("event") == "closed":
                self.release_report = msg
                continue
            return msg

    def _stderr_tail(self) -> str:
        return "".join(self._stderr)[-2000:]

    def call(self, cmd: str, timeout_s: float = 30.0, **args: Any) -> dict[str, Any]:
        if cmd not in ALLOWED_COMMANDS:
            raise BridgeError(f"command {cmd!r} is not allowed (edit-buffer operations only)")
        if cmd == DOCUMENT_WRITE and not self.allow_document_write:
            p = str(Path(str(args.get("path", ""))).resolve())
            if p not in self._backups:
                raise BridgeError("writing a document other than this session's own backup needs "
                                  "StompBridge(allow_document_write=True)")
        with self._lock:
            if self._dead is not None:
                raise BridgeError(f"the stompd session is dead ({self._dead}). {WEDGE_HINT}")
            if self._proc.poll() is not None:
                raise self._fail("stompd is not running")
            assert self._proc.stdin is not None
            try:
                self._proc.stdin.write(json.dumps({"cmd": cmd, **args}) + "\n")
                self._proc.stdin.flush()
            except OSError as e:
                raise self._fail(f"cannot write to stompd: {e}") from e
            msg = self._next(timeout_s)
        if not msg.get("ok"):
            raise BridgeError(f"{cmd}: {msg.get('error')}")
        if cmd == "backup" and args.get("path"):
            self._backups.add(str(Path(str(args["path"])).resolve()))
        return msg

    def close(self) -> None:
        """End the session: `quit`, collect the helper's `closed` report (HX Edit's session-close
        sent and answered), wait for it to exit, then apply the USB reset policy. Idempotent."""
        if self._dead is None and self._proc.poll() is None:
            try:
                self.call("quit", timeout_s=10)
            except BridgeError:
                pass
            self._collect_closed(self.CLOSE_TIMEOUT_S)
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._kill()
        self._dead = self._dead or "closed"
        if not self._ended:
            self._ended = True
            self._collect_closed(2.0)   # a killed helper may still have reported (EOF ends this early)
            self._session_end_reset()

    def _collect_closed(self, timeout_s: float) -> None:
        """Read the helper's remaining lines until its `closed` report or EOF."""
        deadline = time.monotonic() + timeout_s
        while self.release_report is None:
            left = deadline - time.monotonic()
            try:
                line = self._lines.get(timeout=max(left, 0.0)) if left > 0 else self._lines.get_nowait()
            except queue.Empty:
                return
            if line is None:
                self._lines.put(None)   # keep EOF visible to anyone else reading
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("event") == "closed":
                self.release_report = msg

    @property
    def released(self) -> bool:
        """The helper reports HX Edit's session-close sent on this session's channels."""
        r = self.release_report or {}
        return bool(r.get("release_sent"))

    def _session_end_reset(self) -> None:
        """The T059 fallback: re-enumerate the pedal's USB after the session, per `usb_reset`."""
        rep: dict[str, Any] = {"mode": self.usb_reset, "released": self.released, "reset": False}
        self.reset_report = rep
        if self.usb_reset == "off":
            rep["why"] = "off"
            return
        if not self._opened:
            rep["why"] = "no session was opened"
            return
        if self._wedged or (self.release_report or {}).get("skipped"):
            rep["why"] = "the pedal did not answer (wedged): only the 9V clears that, a reset cannot"
            return
        if self.usb_reset == "auto" and self.released:
            rep["why"] = "the helper sent the session-close; no reset needed"
            return
        deadline = time.monotonic() + self.AUDIO_WAIT_S
        while self._audio_busy():
            if time.monotonic() >= deadline:
                rep["why"] = "audio streaming active: never reset under a stream"
                print(f"stompd: USB reset skipped, audio was still streaming after {self.AUDIO_WAIT_S:.0f} s. "
                      "If the pedal's blocks cannot be edited, unplug and replug its USB cable.", file=sys.stderr)
                return
            time.sleep(0.1)
        rep["reset"] = True
        try:
            out = subprocess.run([str(self._exe), "reset", f"{self.RESET_WAIT_S:g}"], capture_output=True,
                                 text=True, timeout=self.RESET_WAIT_S + 20, check=False)
            lines = [ln for ln in out.stdout.splitlines() if ln.strip().startswith("{")]
            res = json.loads(lines[-1]) if lines else {"ok": False, "error": (out.stderr or "no output")[-500:]}
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            res = {"ok": False, "error": str(e)}
        rep["result"] = res
        rep["reappeared"] = bool(res.get("ok") and res.get("reappeared"))
        if rep["reappeared"]:
            try:
                from .. import reamp
                rep["audio_rescanned"] = reamp.refresh_audio_devices()
            except Exception as e:  # noqa: BLE001 - a re-scan failure must not hide the reset result
                rep["audio_rescanned"] = f"failed: {e}"
        else:
            print(f"stompd: {REPLUG_MESSAGE} ({res.get('error') or 'not seen again on USB'})", file=sys.stderr)

    def _kill(self) -> None:
        """Stop the helper. Its stdin is closed first so it can still end the session properly
        (send the session-close) once any command in flight returns; terminate after a grace."""
        if self._proc.poll() is None:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=self.KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
