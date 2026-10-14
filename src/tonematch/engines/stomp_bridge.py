"""Client for the long-lived HX Stomp control helper (`stompd`), JSON lines over stdin/stdout.

The helper is a small original Rust wrapper over tonepush's `hx-usb` crates (MIT), built outside this
repo (its source is not published here; protocol and build notes: docs/notes/stompd.md):
it holds one USB session on interface 0, sends keepalives while idle, and exposes edit-buffer
operations only (no save, rename, slot write, settings write or IR command). Its path comes from
`TONEMATCH_STOMPD`. Protocol interop only; no Line 6 code or data is involved here.

One session per process: opening and closing a session per command is the main wedge risk
(docs/notes/T003-stomp-control-spike.md), so callers keep one `StompBridge` for a whole run.
"""
from __future__ import annotations

import collections
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Self

from .base import EngineError

ENV_HELPER = "TONEMATCH_STOMPD"
VENDOR_ID = 0x0E41
PRODUCT_ID_HX_STOMP = 0x4246


class BridgeError(EngineError):
    """The helper reported an error, timed out or exited."""


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
WEDGE_HINT = ("If the pedal no longer answers (here or on its own screen), it may be wedged: pull the "
              "HX Stomp's 9V adapter for a few seconds and plug it back in. A USB replug does not "
              "clear it. Do not retry in a loop.")


class StompBridge:
    """One `stompd` process and one device session.

    Never retries an open. After a timeout or a lost session the helper is killed, so a late
    reply can never be read as the answer to a later request; the bridge is then dead and every
    further call raises."""

    def __init__(self, binary: Path | None = None, open_timeout_s: float = 20.0) -> None:
        exe = binary or helper_path()
        if exe is None:
            raise BridgeError(f"stompd helper not found; it is built outside this repo (see docs/notes/stompd.md), "
                              f"then set {ENV_HELPER} to its path")
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
        except BridgeError:
            self._kill()
            raise
        if not ready.get("ok") or ready.get("event") != "ready":
            self._kill()
            raise BridgeError(f"stompd did not open the device: {ready} {self._stderr_tail()} "
                              "(is HX Edit running? it holds the control interface)")
        self.ready: dict[str, Any] = ready

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
            return msg

    def _stderr_tail(self) -> str:
        return "".join(self._stderr)[-2000:]

    def call(self, cmd: str, timeout_s: float = 30.0, **args: Any) -> dict[str, Any]:
        if cmd not in ALLOWED_COMMANDS:
            raise BridgeError(f"command {cmd!r} is not allowed (edit-buffer operations only)")
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
        return msg

    def close(self) -> None:
        if self._dead is None and self._proc.poll() is None:
            try:
                self.call("quit", timeout_s=10)
            except BridgeError:
                pass
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._kill()
        self._dead = self._dead or "closed"

    def _kill(self) -> None:
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
