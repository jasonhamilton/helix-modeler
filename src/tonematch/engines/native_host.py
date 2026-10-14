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


class Host:
    def __init__(self) -> None:
        self.plugin = None
        self.sr = 48_000
        self.block = 512
        self.channels = 1

    # --- commands ---------------------------------------------------------------------------

    def open(self, path: str, plugin_name: str | None = None, sample_rate: int = 48_000,
             block_size: int = 512, **_) -> dict:
        import pedalboard as pb

        self.sr, self.block = int(sample_rate), int(block_size)
        t0 = time.perf_counter()
        if path.startswith("builtin:"):
            name, _, q = path[len("builtin:"):].partition("?")
            kwargs = {k: float(v) for k, v in parse_qsl(q)}
            self.plugin = getattr(pb, name)(**kwargs)
        else:
            self.plugin = pb.load_plugin(path, plugin_name=plugin_name)
        load_s = time.perf_counter() - t0
        self.channels = self._probe_channels()
        return {"load_s": load_s, **self.info()}

    def _probe_channels(self) -> int:
        """Mono if the plugin accepts it, else stereo (dry duplicated, output folded to mono)."""
        x = np.zeros((1, self.block), dtype=np.float32)
        try:
            self.plugin.process(x, self.sr, buffer_size=self.block, reset=True)
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
        out = {"params": params, "channels": self.channels, "has_state": hasattr(p, "raw_state")}
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
        else:
            self.plugin.raw_state = payload
        return {"set_s": time.perf_counter() - t0}

    def get_params(self, **_) -> dict:
        return {"values": {n: float(p.raw_value) for n, p in self.plugin.parameters.items()}}

    def set_params(self, values: dict, **_) -> dict:
        prm = self.plugin.parameters
        missing = [n for n in values if n not in prm]
        if missing:
            raise KeyError(f"unknown plugin parameters: {missing[:5]}")
        for n, v in values.items():
            prm[n].raw_value = float(v)
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
        y = self.plugin.process(inp, self.sr, buffer_size=self.block, reset=bool(reset))
        dt = time.perf_counter() - t0
        y = np.asarray(y, dtype=np.float32)
        if y.ndim == 2:
            y = y.mean(axis=0) if y.shape[0] > 1 else y[0]
        if len(y) < len(x):  # plugin still buffering: pad, the parent trims by measured latency
            y = np.concatenate([np.zeros(len(x) - len(y), np.float32), y])
        return {"render_s": dt, "out_channels": int(inp.shape[0])}, y[: len(x)].astype("<f4").tobytes()

    def editor(self, **_) -> dict:
        self.plugin.show_editor()  # blocks until the human closes the window
        return {}

    def ping(self, **_) -> dict:
        return {"pid": os.getpid()}


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
            if isinstance(res, tuple):
                write_msg(proto_out, {"ok": True, **res[0]}, res[1])
            else:
                write_msg(proto_out, {"ok": True, **(res or {})})
        except Exception as e:  # noqa: BLE001
            write_msg(proto_out, {"ok": False, "error": f"{type(e).__name__}: {e}",
                                  "trace": traceback.format_exc(limit=4)})


if __name__ == "__main__":
    sys.exit(main())
