"""Helix Native render engine (plan Step 4, Native mode). Implements contracts/engine-interface.md.

LICENCE BOUNDARY. The plugin host (pedalboard, GPLv3: it bundles JUCE and the VST3 SDK under
their GPL options; DawDreamer, GPLv3, as the fallback host) is never imported in this process.
It lives in ``tonematch.engines.native_host``, spawned as a child process and driven over a
pipe. This module is plain Python + numpy, so the project's licence choice stays open. The
boundary also contains plugin crashes (a bad state blob can kill the host process).

Helix Native itself is the user's own licensed install. It is located at runtime, never copied,
never redistributed, never committed. Only the original Helix Native 3.8x is supported; Helix
Stadium Native is refused.

How a whole preset gets in (task T002, the spike decides; see docs/notes/native-spike.md):

- ``route="state"``: Native's saved state embeds the preset document and the spike proved a
  round trip. ``load()`` writes the whole preset, model changes included. Enabled only when
  ``cache/native/state_codec.json`` (written by ``scripts/native_spike.py`` after a proven round
  trip) exists.
- ``route="automation"`` (default until then): one state blob per chain structure, captured
  from Native's GUI with up to 16 knobs and 16 switches assigned, plus a map from
  ``(slot, param)`` to the automation parameter. ``load()`` switches between captured chains and
  raises ``UnsupportedModelChange`` for a chain with no template; ``search`` groups trials by
  chain (``chain_key``).

``render()`` returns 48 kHz float32 mono, same length as the input, trimmed by the latency
measured with an impulse (once per chain), and level-matched by a fixed calibration gain:
Native's digital output *is* the reference level (``calibration_db`` defaults to 0), so a
silent input stays silent and per-preset loudness differences survive for the loss to judge.
Each render resets the plugin (deterministic) and pre-rolls ``PREROLL`` samples of silence over
Native's post-reset fade-in. The reset is most of the cost on 3.82 (~1.6 s fixed on the empty
preset, ~1.8 s on the known.hlx amp+cab+delay chain, whose DSP adds ~0.045 s per second of
audio); ``set_params`` only moves automation knobs (~0.5 ms, no state is re-sent). The opt-in
``reset_mode="flush"`` skips the reset once the spike shows it matches on the chains in use.

``cost()`` is measured on the loaded chain (or, before any load, on a captured template) and
cached on disk by chain structure, not by raw state: knob moves change Native's raw state, and
a state-keyed cache re-ran the ~150 s measurement on every new knob value.

State findings (T002, Native 3.82, VST3 and AU): the saved state wraps a ``CMXH`` chunk
container whose preset records are opaque and high-entropy, and Native rejects replayed library
records, so there is no readable preset document to write. ``route="state"`` stays disabled.

``NativeRenderPool`` is the batch render service for ``benchmark`` and ``learning``: N worker
processes, jobs grouped by chain so state loads are minimised.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import importlib.util
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .base import (
    SAMPLE_RATE,
    Audio,
    Capabilities,
    Cost,
    EngineError,
    ParamRef,
    UnsupportedModelChange,
)

ROOT = Path(__file__).resolve().parents[3]
SRC = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------- locating Native

_MAC = [  # VST3 first (plan Step 4); the AU is the fallback and also the only Mac build with MIDI PC recall
    "/Library/Audio/Plug-Ins/VST3/Helix Native.vst3",
    "~/Library/Audio/Plug-Ins/VST3/Helix Native.vst3",
    "/Library/Audio/Plug-Ins/VST3/Line 6/Helix Native.vst3",   # where the 3.82 installer puts it
    "/Library/Audio/Plug-Ins/Components/Helix Native.component",
    "~/Library/Audio/Plug-Ins/Components/Helix Native.component",
]
_WIN = [
    "C:/Program Files/Common Files/VST3/Helix Native.vst3",
    "C:/Program Files/Common Files/VST3/Line 6/Helix Native.vst3",
]
_LINUX = [  # Wine + yabridge, best effort
    "~/.vst3/yabridge/Helix Native.vst3",
    "~/.vst3/Helix Native.vst3",
    "~/.wine/drive_c/Program Files/Common Files/VST3/Helix Native.vst3",
]


def cache_dir() -> Path:
    return Path(os.environ.get("TONEMATCH_CACHE", ROOT / "cache")) / "native"


def candidate_paths(plat: str | None = None) -> list[Path]:
    plat = plat or sys.platform
    pats = _MAC if plat == "darwin" else _WIN if plat.startswith("win") else _LINUX
    out = [Path(p).expanduser() for p in pats]
    env = os.environ.get("TONEMATCH_NATIVE_PLUGIN")
    if env and not env.startswith("builtin:"):
        out.insert(0, Path(env).expanduser())
    return out


def bundle_info(path: str | Path) -> dict:
    """Selected Info.plist fields of a macOS plugin bundle ({} elsewhere or if unreadable)."""
    import plistlib

    f = Path(path) / "Contents" / "Info.plist"
    try:
        d = plistlib.loads(f.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}
    keys = ("CFBundleName", "CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion",
            "CFBundleGetInfoString")
    out = {k: d[k] for k in keys if k in d}
    comps = d.get("AudioComponents") or []
    if comps:
        out["AudioComponentName"] = comps[0].get("name", "")
    return out


def is_stadium(path: str | Path) -> bool:
    """Helix Stadium Native, judged by the path and (on macOS) the bundle's Info.plist."""
    if "stadium" in str(path).lower():
        return True
    return any("stadium" in str(v).lower() for v in bundle_info(path).values())


def plugin_version(path: str | Path) -> str:
    """Marketing version from the bundle (e.g. '3.82'), or '' if unknown."""
    return str(bundle_info(path).get("CFBundleShortVersionString", ""))


def locate_plugin() -> Path | None:
    """The user's Helix Native VST3, or None. Never Stadium Native."""
    for p in candidate_paths():
        if p.exists() and not is_stadium(p):
            return p
    return None


def host_python() -> str:
    return os.environ.get("TONEMATCH_NATIVE_PYTHON", sys.executable)


_HOST_OK: dict[str, bool] = {}


def host_available() -> bool:
    """pedalboard importable by the interpreter that will run the host process."""
    py = host_python()
    if py not in _HOST_OK:
        if py == sys.executable:
            _HOST_OK[py] = importlib.util.find_spec("pedalboard") is not None
        else:
            r = subprocess.run([py, "-c", "import pedalboard"], capture_output=True, check=False)
            _HOST_OK[py] = r.returncode == 0
    return _HOST_OK[py]


# --------------------------------------------------------------------------- worker process


class HostProcess:
    """One plugin-host child process. Thread-safe per instance (one request at a time)."""

    def __init__(self, plugin: str, plugin_name: str | None = None, block_size: int = 512):
        from .native_host import read_msg, write_msg  # protocol helpers only, no pedalboard

        self._read, self._write = read_msg, write_msg
        self._lock = threading.Lock()
        self.plugin, self.plugin_name, self.block_size = plugin, plugin_name, block_size
        self.info: dict = {}
        self._spawn()

    def _spawn(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            [host_python(), "-m", "tonematch.engines.native_host"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, env=env,
        )
        self.info = self.call("open", path=self.plugin, plugin_name=self.plugin_name,
                              sample_rate=SAMPLE_RATE, block_size=self.block_size)[0]

    def call(self, cmd: str, payload: bytes = b"", **kw) -> tuple[dict, bytes]:
        with self._lock:
            if self.proc.poll() is not None:
                raise EngineError(f"Native host process exited (code {self.proc.returncode})")
            try:
                self._write(self.proc.stdin, {"cmd": cmd, **kw}, payload)
                hdr, data = self._read(self.proc.stdout)
            except (EOFError, BrokenPipeError, OSError) as e:
                code = self.proc.poll()
                raise EngineError(f"Native host died during {cmd!r} (code {code}): {e}") from e
        if not hdr.pop("ok", False):
            raise EngineError(f"Native host {cmd!r} failed: {hdr.get('error')}")
        hdr.pop("nbytes", None)
        return hdr, data

    def restart(self) -> None:
        self.close()
        self._spawn()

    def close(self) -> None:
        if getattr(self, "proc", None) and self.proc.poll() is None:
            try:
                self.call("quit")
            except EngineError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --------------------------------------------------------------------------- chain templates


def chain_key(preset) -> tuple[tuple[str, str | None], ...]:
    """The structure of a preset: (category, model_id) per slot. Trials sharing it share a template."""
    slots = getattr(preset, "slots", None)
    if slots is None:
        raise EngineError("load() needs a tonematch.preset.Preset (with .slots)")
    return tuple((s.category, s.model_id) for s in slots)


def chain_id(key) -> str:
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:12]


@dataclass
class ChainTemplate:
    """A captured Native state for one chain, plus its automation map.

    Files (local only, gitignored): ``cache/native/templates/<id>.json`` and ``<id>.state``.
    ``map`` keys are ``"<slot>:<param_name>"``; values are the host parameter names of the
    automation knobs or switches (e.g. as listed in the spike's params.json). ``ranges`` optionally
    gives the catalog-normalized [lo, hi] the knob covers if the assignment's Min/Max were not
    left at the full range.
    """
    chain: tuple
    state: bytes
    map: dict[str, str] = field(default_factory=dict)
    ranges: dict[str, list[float]] = field(default_factory=dict)
    name: str = ""

    @classmethod
    def load_all(cls, d: Path | None = None) -> dict[tuple, ChainTemplate]:
        d = d or cache_dir() / "templates"
        out = {}
        for j in sorted(d.glob("*.json")) if d.is_dir() else []:
            meta = json.loads(j.read_text())
            chain = tuple(tuple(x) for x in meta["chain"])
            state = (j.with_suffix(".state")).read_bytes()
            out[chain] = cls(chain, state, meta.get("map", {}), meta.get("ranges", {}), meta.get("name", j.stem))
        return out

    def save(self, d: Path | None = None) -> Path:
        d = d or cache_dir() / "templates"
        d.mkdir(parents=True, exist_ok=True)
        cid = chain_id([list(x) for x in self.chain])
        (d / f"{cid}.state").write_bytes(self.state)
        p = d / f"{cid}.json"
        p.write_text(json.dumps({"name": self.name, "chain": [list(x) for x in self.chain],
                                 "map": self.map, "ranges": self.ranges}, indent=1))
        return p


# --------------------------------------------------------------------------- measurements


def onset_latency(impulse_response: np.ndarray, rel_db: float = -40.0) -> int:
    """First sample whose magnitude reaches rel_db below the response's peak."""
    a = np.abs(impulse_response)
    peak = float(a.max()) if len(a) else 0.0
    if peak <= 0:
        raise EngineError("Native returned silence for an impulse; cannot measure latency")
    return int(np.argmax(a >= peak * 10 ** (rel_db / 20)))


def null_db(a: np.ndarray, b: np.ndarray) -> float:
    """Residual of a - b relative to a, in dB (more negative = closer)."""
    num = float(np.sum((a.astype(np.float64) - b) ** 2))
    den = float(np.sum(a.astype(np.float64) ** 2)) or 1e-30
    return 10 * np.log10(max(num, 1e-30) / den)


def test_signal(seconds: float = 2.0, seed: int = 0) -> np.ndarray:
    """Deterministic guitar-ish excitation (decaying noise bursts) at about -12 dBFS peak."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SAMPLE_RATE)
    x = np.zeros(n, np.float32)
    env = np.exp(-np.arange(SAMPLE_RATE // 4) / (0.05 * SAMPLE_RATE)).astype(np.float32)
    for start in range(0, n, SAMPLE_RATE // 4):
        seg = rng.standard_normal(len(env)).astype(np.float32) * env
        x[start:start + len(seg)] += seg[: n - start]
    return (x / np.max(np.abs(x)) * 0.25).astype(np.float32)


def _rms_db(x: np.ndarray) -> float:
    return 10 * np.log10(max(float(np.mean(np.square(x, dtype=np.float64))), 1e-30)) if len(x) else -300.0


# --------------------------------------------------------------------------- the engine


class NativeEngine:
    """Helix Native via a child-process plugin host. See the module docstring."""

    SILENT_IN_DB = -70.0    # input RMS above this counts as non-silent
    SILENT_OUT_DB = -100.0  # output RMS below this for a non-silent input is an error
    # Native 3.82 fades its output in after a host reset: about 3,180 samples to reach unity
    # within 0.1% (measured 2026-10-14, VST3 and AU, any block size). Every render is reset for
    # determinism, so each one is prefixed with this much silence, then trimmed off.
    PREROLL = 4800
    # A host reset costs Native 3.82 about 1.59 s regardless of clip length or block size, while
    # an empty chain's DSP runs near 0.01x real time (measured 2026-10-14). reset_mode="flush"
    # skips the reset and pre-rolls flush_s of silence instead: much faster, but deterministic
    # only if every block's state (delay/reverb tails, LFO phase) is cleared or decays within the
    # flush. The spike's --gui step measures that on a real chain; "reset" stays the default.
    RESET_MODES = ("reset", "flush")

    def __init__(self, plugin_path: str | Path | None = None, plugin_name: str | None = None,
                 *, calibration_db: float = 0.0, block_size: int = 512,
                 templates_dir: Path | None = None, route: str | None = None,
                 preroll: int | None = None, reset_mode: str | None = None, flush_s: float = 2.0):
        p = plugin_path or os.environ.get("TONEMATCH_NATIVE_PLUGIN") or locate_plugin()
        if p is None:
            raise EngineError("Helix Native not found. Install it (your own licence) or set TONEMATCH_NATIVE_PLUGIN.")
        p = str(p)
        if is_stadium(p):
            raise EngineError("Helix Stadium Native is not supported; install original Helix Native 3.8x.")
        self.plugin = p
        self.plugin_name = plugin_name
        self.calibration_db = float(calibration_db)
        self.block_size = int(block_size)
        self.preroll = self.PREROLL if preroll is None else int(preroll)
        self.reset_mode = reset_mode or os.environ.get("TONEMATCH_NATIVE_RESET", "reset")
        if self.reset_mode not in self.RESET_MODES:
            raise EngineError(f"reset_mode must be one of {self.RESET_MODES}, got {self.reset_mode!r}")
        self.flush_s = float(flush_s)
        self.templates_dir = templates_dir
        self.host = HostProcess(p, plugin_name, block_size)
        self._params = {d["name"]: d for d in self.host.info.get("params", [])}
        self.templates = ChainTemplate.load_all(templates_dir)
        self._initial_state = self._get_state()
        self.codec = _load_codec()
        self.route = route or ("state" if self.codec else "automation")
        self.current_chain: tuple | None = None
        self.current_template: ChainTemplate | None = None
        self.last_unmapped: list[str] = []
        self.last_gain_db = self.calibration_db
        self._latency: dict[tuple | None, int] = {}
        self._cost: Cost | None = None

    # --- availability ---------------------------------------------------------------------

    @staticmethod
    def is_available() -> bool:
        """True when a non-Stadium Helix Native (VST3, or AU on macOS) is installed and a plugin host is importable."""
        env = os.environ.get("TONEMATCH_NATIVE_PLUGIN", "")
        if env.startswith("builtin:"):
            return False  # test stand-ins never count as Native
        return locate_plugin() is not None and host_available()

    # --- interface ------------------------------------------------------------------------

    def load(self, preset) -> None:
        if self.route == "state":
            blob = self.codec.encode(preset, self._get_state())
            self._set_state(blob)
            self.current_chain = chain_key(preset)
            self.current_template = None
            return
        key = chain_key(preset)
        tpl = self.templates.get(key)
        if tpl is None:
            raise UnsupportedModelChange(
                f"No captured Native template for chain {chain_id([list(x) for x in key])} "
                f"({[m for _, m in key if m]}). Native is driven through 16+16 automation slots on this "
                "route; capture the chain with scripts/native_spike.py --capture-template."
            )
        if key != self.current_chain:
            self._set_state(tpl.state)
            self.current_chain, self.current_template = key, tpl
        changes, unmapped = {}, []
        for i, s in enumerate(preset.slots):
            for name, v in (s.params or {}).items():
                if f"{i}:{name}" in tpl.map:
                    changes[(i, name)] = v
                else:
                    unmapped.append(f"{i}:{name}")
            if f"{i}:@enabled" in tpl.map:
                changes[(i, "@enabled")] = 1.0 if s.enabled else 0.0
        self.last_unmapped = unmapped  # these stay at the template's captured values
        if changes:
            self.set_params(changes)

    def set_params(self, changes: dict[ParamRef, float]) -> None:
        if not changes:
            return
        if self.route == "state":
            raise EngineError("set_params on the state route needs the codec's param path (pending the spike)")
        tpl = self.current_template
        if tpl is None:
            raise EngineError("set_params before load()")
        values = {}
        for (slot, name), v in changes.items():
            k = f"{slot}:{name}"
            host_name = tpl.map.get(k)
            if host_name is None:
                raise EngineError(f"{k} is not assigned to an automation knob/switch in template {tpl.name!r}")
            lo, hi = tpl.ranges.get(k, [0.0, 1.0])
            values[host_name] = float(np.clip((float(v) - lo) / ((hi - lo) or 1.0), 0.0, 1.0))
        self.host.call("set_params", values=values)

    def render(self, dry: Audio) -> Audio:
        x = np.asarray(dry, dtype=np.float32)
        if x.ndim != 1:
            raise EngineError(f"render() takes mono (n,) float32, got shape {x.shape}")
        if len(x) == 0:
            return x.copy()
        lat, reset = self.latency(), self.reset_mode == "reset"
        pre = self.preroll if reset else int(self.flush_s * SAMPLE_RATE)
        padded = np.concatenate([np.zeros(pre, np.float32), x, np.zeros(lat, np.float32)])
        y = self._raw_render(padded, reset=reset)[pre + lat:pre + lat + len(x)]
        if not np.all(np.isfinite(y)):
            raise EngineError("Native returned NaN or inf")
        if _rms_db(x) > self.SILENT_IN_DB and _rms_db(y) < self.SILENT_OUT_DB:
            raise EngineError("Native returned silence for a non-silent input")
        g = np.float32(10 ** (self.calibration_db / 20))
        self.last_gain_db = self.calibration_db
        return (y * g).astype(np.float32)

    def cost(self) -> Cost:
        if self._cost is None:
            self._cost = self.measure_cost()
        return self._cost

    def capabilities(self) -> Capabilities:
        info = self.host.info
        models: tuple[str, ...] = ()
        try:
            from tonematch.catalog import load_catalog

            cat = load_catalog()
            models = tuple(sorted(m["id"] for m in cat["models"].values() if m.get("stomp")))
        except (ImportError, RuntimeError, OSError, ValueError, KeyError):  # CatalogError is a RuntimeError
            models = ()  # no catalog import yet: models stays empty and engine-smoke says so
        return Capabilities(
            engine="native",
            firmware_version=plugin_version(self.plugin) or str(info.get("version") or ""),
            models=models,
            snapshots=3,
            extras={"route": self.route, "control_route": self.route, "plugin": self.plugin, "host": "pedalboard",
                    "host_params": len(self._params), "latency_samples": self._latency.get(self.current_chain),
                    "templates": len(self.templates), "calibration_db": self.calibration_db,
                    "reset_mode": self.reset_mode},
        )

    def close(self) -> None:
        self.host.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # --- internals ------------------------------------------------------------------------

    def _raw_render(self, x: np.ndarray, reset: bool = True) -> np.ndarray:
        _, data = self.host.call("render", x.astype("<f4").tobytes(), reset=reset)
        return np.frombuffer(data, dtype="<f4").astype(np.float32)

    def _get_state(self) -> bytes:
        if not self.host.info.get("has_state"):
            return b""  # builtin test stand-ins have no saved state
        return self.host.call("get_state")[1]

    def _set_state(self, blob: bytes) -> None:
        try:
            self.host.call("set_state", blob)
        except EngineError:
            self.host.restart()  # a bad blob may have killed or wedged the host
            raise

    def latency(self) -> int:
        """Samples of latency beyond what the host compensates, measured once per chain with an impulse."""
        key = self.current_chain
        if key not in self._latency:
            imp = np.zeros(self.preroll + SAMPLE_RATE // 4, np.float32)
            imp[self.preroll] = 0.5  # after the post-reset fade-in, which would swallow a sample-0 impulse
            self._latency[key] = onset_latency(self._raw_render(imp)[self.preroll:])
        return self._latency[key]

    def cost_key(self) -> str:
        """Disk-cache key for cost(): plugin, machine, reset mode and the chain *structure*.

        Keyed by chain, not by a hash of the raw state: a knob move changes Native's raw state
        (measured 2026-10-14), so a state-hash key missed the cache on every parameter change and
        re-ran the whole ~150 s measurement inside the caller (search's smoke demo spent 156 of
        its 202 s there). Knob values do not change the fixed reset cost, which dominates.
        """
        if self.current_chain is not None:
            sh = "chain:" + chain_id([list(x) for x in self.current_chain])
        else:
            state = self._get_state()
            sh = "state:" + (hashlib.sha1(state).hexdigest()[:12] if state else "nostate")
        return (f"{self.plugin}|{self.host.info.get('version')}|{platform.node()}|{os.cpu_count()}|{sh}"
                f"|{self.reset_mode}")

    def measure_cost(self, max_workers: int | None = None, clip_s: float = 2.0, repeats: int = 3,
                     force: bool = False) -> Cost:
        """Measure seconds per 2 s render, parallel throughput and model-change settling. Cached to disk.

        Measured on a real chain: the loaded one, or (before any load) a captured template, so the
        figure includes the chain's DSP and not only the empty default preset's pass-through.
        The engine's state is restored afterwards. ``force`` re-measures and overwrites the entry.
        """
        switched = None
        if self.current_chain is None and self.templates and self.route == "automation":
            tpl = min(self.templates.values(), key=lambda t: chain_id([list(x) for x in t.chain]))
            switched = self._get_state()
            self._set_state(tpl.state)
            self.current_chain, self.current_template = tpl.chain, tpl
        try:
            return self._measure_cost(max_workers, clip_s, repeats, force)
        finally:
            if switched is not None:
                self._set_state(switched)
                self.current_chain = self.current_template = None

    def _measure_cost(self, max_workers, clip_s, repeats, force) -> Cost:
        key = self.cost_key()
        f = cache_dir() / "cost.json"
        cached = json.loads(f.read_text()) if f.is_file() else {}
        if key in cached and not force:
            return Cost(**cached[key]["cost"])
        load_before = [round(v, 2) for v in os.getloadavg()] if hasattr(os, "getloadavg") else None
        state = self._get_state()
        clip = test_signal(clip_s)
        self.render(clip)  # warm-up
        times = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            self.render(clip)
            times.append(time.perf_counter() - t0)
        spr = statistics.median(times)
        reset = self.reset_mode == "reset"
        pre = self.preroll if reset else int(self.flush_s * SAMPLE_RATE)
        par, scan = measure_parallelism(self.plugin, self.plugin_name, state, clip, max_workers, pre, reset=reset)
        pen, pair = self.measure_load_penalty(clip)
        cost = Cost(seconds_per_render=spr, parallelism=par, load_penalty_s=pen)
        chain = [list(x) for x in self.current_chain] if self.current_chain else None
        cached[key] = {"cost": cost.__dict__, "throughput_by_workers": scan, "render_times": times,
                       "load_penalty_pair": pair, "preroll": self.preroll, "chain": chain,
                       "loadavg_before": load_before,
                       "loadavg_after": [round(v, 2) for v in os.getloadavg()] if load_before else None,
                       "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cached, indent=1))
        return cost

    def measure_load_penalty(self, clip: np.ndarray, tol_db: float = -60.0,
                             timeout_s: float = 10.0) -> tuple[float, str]:
        """Seconds for a chain change: load another state, then poll until two consecutive renders
        null below tol_db (async model/IR loading). Returns (seconds, which pair was measured).

        The pair is from the state the engine opened with (Native's empty default) to the current
        one; with only one distinct state it measures a reload, and says so.
        """
        current = self._get_state()
        source = next((t.state for t in self.templates.values() if t.state != current), self._initial_state)
        pair = "other->current" if source and source != current else "reload(current)"
        if source and source != current:
            self._set_state(source)
            self._raw_render(clip[:SAMPLE_RATE // 10])
        pclip = np.concatenate([np.zeros(self.preroll, np.float32), clip])
        t0 = time.perf_counter()
        if current:
            self._set_state(current)
        pen = time.perf_counter() - t0          # the state load itself
        prev = self._raw_render(pclip)
        while pen < timeout_s:                  # plus every extra poll until renders stop changing
            t1 = time.perf_counter()
            time.sleep(0.05)
            cur = self._raw_render(pclip)
            if null_db(cur, prev) < tol_db:
                break
            pen += time.perf_counter() - t1
            prev = cur
        return float(pen), pair


def measure_parallelism(plugin: str, plugin_name: str | None, state: bytes, clip: np.ndarray,
                        max_workers: int | None = None, preroll: int = 0,
                        rounds: int = 3, reset: bool = True) -> tuple[int, dict[int, float]]:
    """Renders/s for k host processes rendering at once; picks the k past which gains drop under 15%.

    The scan stops at the first k that gains under 15% (the choice cannot change after it), and
    new host processes are opened concurrently (each Native open is ~5 s), so a cold scan costs
    well under a minute instead of ~2.5 minutes.
    """
    cpu = os.cpu_count() or 2
    max_workers = max_workers or max(1, cpu - 1)
    ks = sorted({k for k in (1, 2, 4, 6, 8, 12, 16, 24, 32) if k <= max_workers} | {max_workers})
    hosts: list[HostProcess] = []
    scan: dict[int, float] = {}
    payload = np.concatenate([np.zeros(preroll, np.float32), clip]).astype("<f4").tobytes()

    def open_host(_=None) -> HostProcess:
        h = HostProcess(plugin, plugin_name)
        try:
            if state and h.info.get("has_state"):
                h.call("set_state", state)
            h.call("render", payload, reset=True)  # warm-up
        except EngineError:
            h.close()
            raise
        return h

    best = ks[0]
    try:
        for k in ks:
            need = k - len(hosts)
            if need > 0:
                with cf.ThreadPoolExecutor(need) as ex:
                    futs = [ex.submit(open_host) for _ in range(need)]
                    for fu in futs:  # collect every opened host so finally closes them all
                        try:
                            hosts.append(fu.result())
                        except EngineError:
                            pass
                if len(hosts) < k:
                    raise EngineError(f"could not open {k} Native host processes")
            with cf.ThreadPoolExecutor(k) as ex:
                t0 = time.perf_counter()
                list(ex.map(lambda h: [h.call("render", payload, reset=reset) for _ in range(rounds)], hosts[:k]))
                scan[k] = k * rounds / (time.perf_counter() - t0)
            if k != ks[0]:
                if scan[k] >= scan[best] * 1.15:
                    best = k
                else:
                    break
    finally:
        for h in hosts:
            h.close()
    return best, scan


# --------------------------------------------------------------------------- state codec


class _Codec:
    """Preset -> Native state, from cache/native/state_codec.json (written by the spike on success)."""

    def __init__(self, spec: dict):
        self.spec = spec

    def encode(self, preset, base_state: bytes) -> bytes:
        from . import native_state as ns

        to_doc = getattr(preset, "to_hlx_dict", None)
        if to_doc is None:
            raise EngineError("state route needs Preset.to_hlx_dict() from the preset agent (T006)")
        parts = ns.split_vst3_state(base_state)
        comp = parts["IComponent"]
        cand = ns.best_preset_candidate(ns.scan(comp))
        if cand is None:
            raise EngineError("state codec: preset document not found in current Native state")
        doc = json.loads(cand.data)
        new = to_doc()
        sub = self.spec.get("subtree")  # e.g. ["data"] if Native stores the .hlx 'data' node
        target = doc
        for k in sub or []:
            target = target[k]
        target.clear()
        target.update(new if not sub else _dig(new, sub))
        comp2 = ns.replace(comp, cand.path, json.dumps(doc, separators=(",", ":")).encode())
        return ns.join_vst3_state(base_state, {"IComponent": comp2})


def _dig(d, keys):
    for k in keys:
        d = d[k]
    return d


def _load_codec() -> _Codec | None:
    f = cache_dir() / "state_codec.json"
    if not f.is_file():
        return None
    spec = json.loads(f.read_text())
    return _Codec(spec) if spec.get("proven") else None


# --------------------------------------------------------------------------- render service


@dataclass
class RenderJob:
    dry: Audio
    preset: object | None = None                 # None: keep the worker's current preset
    params: dict[ParamRef, float] = field(default_factory=dict)
    tag: object = None


class NativeRenderPool:
    """Batch render service: N NativeEngine workers, jobs grouped by chain to minimise state loads.

    Used by benchmark (synthetic set) and learning (training data). Order of results matches jobs.
    """

    def __init__(self, workers: int | None = None, **engine_kw):
        first = NativeEngine(**engine_kw)
        n = workers or first.cost().parallelism
        self.engines = [first] + [NativeEngine(**engine_kw) for _ in range(max(0, n - 1))]

    def render_batch(self, jobs: list[RenderJob]) -> list[Audio]:
        def key(j):
            try:
                return chain_id([list(x) for x in chain_key(j.preset)]) if j.preset is not None else ""
            except EngineError:
                return ""
        order = sorted(range(len(jobs)), key=lambda i: key(jobs[i]))
        # keep each chain on as few workers as possible: contiguous slices rather than stripes
        size = -(-len(order) // len(self.engines)) if order else 0
        shards = [order[w * size:(w + 1) * size] for w in range(len(self.engines))]
        out: list[Audio | None] = [None] * len(jobs)

        def run(eng: NativeEngine, idx: list[int]):
            for i in idx:
                j = jobs[i]
                if j.preset is not None:
                    eng.load(j.preset)
                if j.params:
                    eng.set_params(j.params)
                out[i] = eng.render(j.dry)

        with cf.ThreadPoolExecutor(len(self.engines)) as ex:
            list(ex.map(run, self.engines, shards))
        return out  # type: ignore[return-value]

    def close(self) -> None:
        for e in self.engines:
            e.close()
