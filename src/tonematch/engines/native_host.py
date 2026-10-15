"""Plugin-host worker process for Helix Native. Run as ``python -m tonematch.engines.native_host``.

LICENCE BOUNDARY. This is the only module in the project that imports pedalboard (GPLv3; it
bundles JUCE and the VST3 SDK under their GPL options) or DawDreamer (GPLv3). It runs in its
own process and talks to ``tonematch.engines.native`` over a pipe with a tiny framed protocol,
so the rest of the project never links GPL code and the project's licence choice stays open.
Keep it that way: do not import this module from anywhere else; spawn it.

It also contains plugin crashes: pedalboard warns that an invalid ``raw_state`` can take the
whole process down. The parent notices the dead pipe, raises EngineError and respawns.

Protocol (both directions): uint32_le header length, UTF-8 JSON header, then ``header["nbytes"]``
bytes of payload if present. Requests have ``cmd``; replies have ``ok`` and either results or
``error``. The real stdout is reserved for the protocol; fd 1 is pointed at stderr so plugin
chatter cannot corrupt the stream.

Plugin paths: a ``.vst3``/``.component`` path, or ``builtin:<PedalboardClass>`` (test stand-in,
e.g. ``builtin:Delay?delay_seconds=0.01&mix=0.5``) so the harness is testable without Native.

MEMORY (measured 2026-10-15, Native 3.82, VST3 and AU, docs/DECISIONS.md). Native never returns
the memory of a destroyed plugin instance: every instance ever created costs about 355 MB of
physical footprint for the life of the process. pedalboard classifies Native as
``PersistsAudioOnReset``, so ``process(reset=True)`` destroys and re-creates the instance on
every call: a 3 s pre-roll render grew the host by ~355 MB each time (7 GB after ~15 renders,
then SIGKILL under memory pressure), and ``load_plugin`` (``open``/``reload_with_state``) by
~1.3-1.6 GB. The fix is ``reset_strategy="state"`` (default): a reset render pads the instance
to a whole 16-sample Native frame (``FRAME``), re-applies the last loaded state, re-sends every
parameter set since (forced: JUCE skips a value equal to its cache, and a state load does not
refresh the cache) and processes with ``reset=False``. Renders are bit-identical to each
preset's own drop capture (-317 dB) and history-independent (-313 dB after 40 random trials),
at about 0.28 s instead of 2.0 s, with no growth. Without the frame padding a render differs by
about -50 dB depending on the previous render's length. ``"reinstantiate"``
(``TONEMATCH_NATIVE_HOST_RESET``) restores pedalboard's behaviour. Plugins with no saved state
(builtins) or a non-zero reported latency (pedalboard trims latency only after a real reset)
always use pedalboard's reset. Every reply carries ``mem_mb``, the host's physical footprint, so
the parent can recycle a host that still grows (``reload_with_state`` cannot avoid new instances).
"""
from __future__ import annotations

import json
import os
import struct
import sys
import time
import traceback
from urllib.parse import parse_qsl

import numpy as np


def _read_exact(f, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return bytes(buf)


def read_msg(f) -> tuple[dict, bytes]:
    n = struct.unpack("<I", _read_exact(f, 4))[0]
    hdr = json.loads(_read_exact(f, n))
    payload = _read_exact(f, hdr["nbytes"]) if hdr.get("nbytes") else b""
    return hdr, payload


def write_msg(f, hdr: dict, payload: bytes = b"") -> None:
    hdr = dict(hdr)
    if payload:
        hdr["nbytes"] = len(payload)
    h = json.dumps(hdr).encode()
    f.write(struct.pack("<I", len(h)) + h + payload)
    f.flush()


def mem_mb() -> float | None:
    """This process's physical footprint in MB (what macOS counts against memory), else RSS.

    macOS: ``proc_pid_rusage`` ``ri_phys_footprint`` (includes compressed and swapped pages,
    which ``ps`` RSS hides). Linux: current RSS from /proc. Elsewhere None.
    """
    try:
        if sys.platform == "darwin":
            import ctypes

            class RUsageV2(ctypes.Structure):  # struct rusage_info_v2 (sys/resource.h)
                _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
                    "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins",
                    "wired_size", "resident_size", "phys_footprint", "proc_start_abstime",
                    "proc_exit_abstime", "child_user_time", "child_system_time",
                    "child_pkg_idle_wkups", "child_interrupt_wkups", "child_pageins",
                    "child_elapsed_abstime", "diskio_bytesread", "diskio_byteswritten")]

            lib = ctypes.CDLL("/usr/lib/libproc.dylib")
            ri = RUsageV2()
            if lib.proc_pid_rusage(os.getpid(), 2, ctypes.byref(ri)) == 0:
                return ri.phys_footprint / 2**20
            return None
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except (OSError, ValueError, AttributeError):
        return None
    return None


RESET_STRATEGIES = ("state", "reinstantiate")
# Native 3.82 runs its DSP in 16-sample frames behind a FIFO that survives a state load: a render
# is history-independent only if the instance has processed a multiple of this many samples when
# the state is re-applied (measured 2026-10-15; see the module docstring).
FRAME = int(os.environ.get("TONEMATCH_NATIVE_FRAME", "16"))


def _away(v: float) -> float:
    """A normalized value clearly different from ``v`` (JUCE skips approximately-equal sets)."""
    return v - 0.25 if v >= 0.5 else v + 0.25


class Host:
    def __init__(self) -> None:
        self.plugin = None
        self.sr = 48_000
        self.block = 512
        self.channels = 1
        self.reset_strategy = os.environ.get("TONEMATCH_NATIVE_HOST_RESET", "state")
        self._base: bytes | None = None    # state a reset render re-applies ("state" strategy)
        self._touched: dict[str, float] = {}  # params set since _base, re-sent after it
        self.instances = 0                 # plugin instances this process created (each leaks)
        self.processed = 0                 # samples the current instance has processed

    def _process(self, x: np.ndarray, reset: bool = False, sr: int | None = None) -> np.ndarray:
        """``plugin.process`` that counts the samples the current instance has seen."""
        y = self.plugin.process(x, sr or self.sr, buffer_size=self.block, reset=bool(reset))
        if reset and hasattr(self.plugin, "raw_state"):
            self.processed = 0  # pedalboard re-created the instance
        self.processed += int(x.shape[-1])
        return y

    # --- commands ---------------------------------------------------------------------------

    def open(self, path: str, plugin_name: str | None = None, sample_rate: int = 48_000,
             block_size: int = 512, reset_strategy: str | None = None, **_) -> dict:
        import pedalboard as pb

        if reset_strategy is not None:
            self.set_reset_strategy(reset_strategy)
        self.sr, self.block = int(sample_rate), int(block_size)
        t0 = time.perf_counter()
        if path.startswith("builtin:"):
            name, _, q = path[len("builtin:"):].partition("?")
            kwargs = {k: float(v) for k, v in parse_qsl(q)}
            self.plugin = getattr(pb, name)(**kwargs)
        else:
            self.plugin = pb.load_plugin(path, plugin_name=plugin_name)
            self.instances += 1
        self.processed = 0
        self._rebase()
        load_s = time.perf_counter() - t0
        self.channels = self._probe_channels()
        return {"load_s": load_s, **self.info()}

    def set_reset_strategy(self, value: str, **_) -> dict:
        if value not in RESET_STRATEGIES:
            raise ValueError(f"reset_strategy must be one of {RESET_STRATEGIES}, got {value!r}")
        self.reset_strategy = value
        return {"reset_strategy": value}

    def reload_with_state(self, payload: bytes, path: str, plugin_name: str | None = None,
                          **_) -> dict:
        """Fresh plugin instance with ``payload`` set before any audio is processed.

        Native 3.82 ignores a state that changes the Hardware Compatibility mode once the
        instance has processed audio (T018n), and ``open`` probes channels with a render.
        """
        import pedalboard as pb

        self.plugin = None
        self.plugin = pb.load_plugin(path, plugin_name=plugin_name)
        self.instances += 1
        self.processed = 0
        t0 = time.perf_counter()
        self.plugin.raw_state = payload
        self._rebase(payload)
        set_s = time.perf_counter() - t0
        self.channels = self._probe_channels()
        return {"set_s": set_s, "channels": self.channels}

    def _probe_channels(self) -> int:
        """Mono if the plugin accepts it, else stereo (dry duplicated, output folded to mono).

        ``reset=False``: a pedalboard reset would re-create (and leak) a Native instance.
        """
        x = np.zeros((1, self.block), dtype=np.float32)
        try:
            self._process(x)
            return 1
        except Exception:  # noqa: BLE001 - plugin channel-layout errors surface as arbitrary C++-mapped types
            return 2

    def info(self) -> dict:
        p = self.plugin
        params = []
        for name, prm in getattr(p, "parameters", {}).items():
            d = {"name": name}
            for a in ("raw_value", "string_value", "label", "units", "is_boolean", "is_discrete",
                      "num_steps", "min_value", "max_value"):
                try:
                    v = getattr(prm, a)
                    json.dumps(v)
                except (AttributeError, TypeError, ValueError, RuntimeError):  # absent or not JSON-able
                    continue
                d[a] = v
            params.append(d)
        out = {"params": params, "channels": self.channels, "has_state": hasattr(p, "raw_state"),
               "reset_strategy": self.reset_strategy}
        for a in ("name", "manufacturer_name", "version", "identifier", "category",
                  "reported_latency_samples", "is_effect", "is_instrument"):
            try:
                v = getattr(p, a)
                json.dumps(v)
            except (AttributeError, TypeError, ValueError, RuntimeError):  # absent or not JSON-able
                continue
            out[a] = v
        return out

    def get_state(self, which: str = "raw", **_) -> tuple[dict, bytes]:
        data = self.plugin.preset_data if which == "preset" else self.plugin.raw_state
        return {}, bytes(data)

    def set_state(self, payload: bytes, which: str = "raw", **_) -> dict:
        t0 = time.perf_counter()
        if which == "preset":
            self.plugin.preset_data = payload
            self._rebase()
        else:
            self.plugin.raw_state = payload
            self._rebase(payload)
        return {"set_s": time.perf_counter() - t0}

    def get_params(self, **_) -> dict:
        return {"values": {n: float(p.raw_value) for n, p in self.plugin.parameters.items()}}

    def set_params(self, values: dict, **_) -> dict:
        prm = self.plugin.parameters
        missing = [n for n in values if n not in prm]
        if missing:
            raise KeyError(f"unknown plugin parameters: {missing[:5]}")
        for n, v in values.items():
            v = float(v)
            # JUCE forwards a parameter only when its cached value changes, and a state load
            # does not update that cache: step away first so the value is always sent.
            prm[n].raw_value = _away(v)
            prm[n].raw_value = v
            self._touched[n] = v
        return {"read_back": {n: float(prm[n].raw_value) for n in values}}

    def set_attrs(self, values: dict, **_) -> dict:
        """Builtin stand-ins only: set Python attributes (e.g. delay_seconds)."""
        for n, v in values.items():
            setattr(self.plugin, n, v)
        return {}

    def render(self, payload: bytes, reset: bool = True, **_) -> tuple[dict, bytes]:
        x = np.frombuffer(payload, dtype="<f4").astype(np.float32)
        inp = np.tile(x, (self.channels, 1))
        t0 = time.perf_counter()
        via = "none"
        if reset:
            reset, via = self._reset_via_state()
        y = self._process(inp, reset=bool(reset))
        dt = time.perf_counter() - t0
        y = np.asarray(y, dtype=np.float32)
        if y.ndim == 2:
            y = y.mean(axis=0) if y.shape[0] > 1 else y[0]
        if len(y) < len(x):  # plugin still buffering: pad, the parent trims by measured latency
            y = np.concatenate([np.zeros(len(x) - len(y), np.float32), y])
        return ({"render_s": dt, "out_channels": int(inp.shape[0]), "reset_via": via},
                y[: len(x)].astype("<f4").tobytes())

    def _rebase(self, state: bytes | None = None) -> None:
        """The state a reset render returns to: ``state``, or the plugin's current one."""
        p = self.plugin
        self._touched = {}
        self._base = bytes(state) if state is not None else (
            bytes(p.raw_state) if hasattr(p, "raw_state") else None)

    def _reset_via_state(self) -> tuple[bool, str]:
        """Prepare a reset render; returns (pass reset=True to pedalboard?, how it was reset).

        "state": pad the instance to a whole Native frame, re-apply the base state (the last
        ``set_state``/``reload_with_state``/``open``), re-send every parameter set since, and
        process without a pedalboard reset. The re-sent values reach the plugin at the start of
        the render's first block, exactly as they reach a re-created instance.
        """
        p = self.plugin
        if (self.reset_strategy == "reinstantiate" or self._base is None
                or int(getattr(p, "reported_latency_samples", 0) or 0) != 0):
            if hasattr(p, "raw_state"):
                self.instances += 1  # pedalboard re-creates external plugins on reset
            return True, "pedalboard"
        pad = (-self.processed) % FRAME
        if pad:
            self._process(np.zeros((self.channels, pad), dtype=np.float32))
        p.raw_state = self._base
        prm = p.parameters
        for n, v in self._touched.items():
            prm[n].raw_value = _away(v)  # see set_params: force JUCE to send it again
            prm[n].raw_value = v
        return False, "state"

    def editor(self, **_) -> dict:
        self.plugin.show_editor()  # blocks until the human closes the window
        return {}

    def reload_type(self, value: str | None = None, **_) -> dict:
        """pedalboard's reset behaviour for this instance (``_reload_type``), optionally set it.

        Values are ``ExternalPluginReloadType`` names: ``ClearsAudioOnReset`` (reset() resets the
        plugin in place) or ``PersistsAudioOnReset``/``Unknown`` (reset() destroys and re-creates
        the plugin instance). Builtins have none: returns ``{"reload_type": None}``.
        """
        import pedalboard as pb

        p = self.plugin
        if not hasattr(p, "_reload_type"):
            return {"reload_type": None}
        before = p._reload_type.name
        if value is not None:
            p._reload_type = getattr(pb.ExternalPluginReloadType, value)
        return {"reload_type": p._reload_type.name, "before": before}

    def editor_cycle(self, open_s: float = 3.0, **_) -> dict:
        """Open the editor for ``open_s`` seconds and close it (memory probe only)."""
        import threading

        ev = threading.Event()
        threading.Timer(float(open_s), ev.set).start()
        self.plugin.show_editor(ev)
        return {}

    def gui_available(self, **_) -> dict:
        """Whether this host can open an editor and drop files on it (macOS + PyObjC + a screen)."""
        if sys.platform != "darwin":
            return {"gui": False, "reason": f"in-process drop is macOS-only (platform {sys.platform})"}
        try:
            import AppKit
        except ImportError as e:
            return {"gui": False, "reason": f"PyObjC missing in the host interpreter ({e}); "
                                            "pip install pyobjc-framework-Cocoa (extra native-gui)"}
        screens = AppKit.NSScreen.screens() or []
        if not screens:
            return {"gui": False, "reason": "no screen: needs a logged-in GUI session"}
        return {"gui": True, "reason": "", "screens": len(screens)}

    def editor_drop(self, path: str, point: list | None = None, open_wait_s: float = 8.0,
                    load_wait_s: float = 4.0, snap_prefix: str | None = None,
                    auto_close: bool = True, **_) -> dict:
        """T018n: open the editor, drop a preset file on it in-process, close (macOS only)."""
        from tonematch.engines.native_gui import DEFAULT_DROP_POINT, drop_file

        return drop_file(self.plugin, path, tuple(point or DEFAULT_DROP_POINT),
                         open_wait_s=open_wait_s, load_wait_s=load_wait_s, snap_prefix=snap_prefix,
                         auto_close=auto_close)

    def ping(self, **_) -> dict:
        return {"pid": os.getpid()}

    def mem(self, **_) -> dict:
        return {"mem_mb": mem_mb(), "instances": self.instances, "reset_strategy": self.reset_strategy}


def main() -> int:
    proto_out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)  # anything printed by plugins goes to stderr
    sys.stdout = sys.stderr
    proto_in = sys.stdin.buffer
    host = Host()
    while True:
        try:
            hdr, payload = read_msg(proto_in)
        except EOFError:
            return 0
        cmd = hdr.pop("cmd", None)
        hdr.pop("nbytes", None)
        if cmd == "quit":
            write_msg(proto_out, {"ok": True})
            return 0
        try:
            fn = getattr(host, cmd)
            if cmd.startswith("_") or not callable(fn):
                raise AttributeError(cmd)
            res = fn(payload=payload, **hdr) if payload else fn(**hdr)
            mem = {"mem_mb": mem_mb()}
            if isinstance(res, tuple):
                write_msg(proto_out, {"ok": True, **mem, **res[0]}, res[1])
            else:
                write_msg(proto_out, {"ok": True, **mem, **(res or {})})
        except Exception as e:  # noqa: BLE001
            write_msg(proto_out, {"ok": False, "error": f"{type(e).__name__}: {e}",
                                  "trace": traceback.format_exc(limit=4)})


if __name__ == "__main__":
    sys.exit(main())
