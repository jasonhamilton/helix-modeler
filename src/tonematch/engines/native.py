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
Native's post-reset fade-in. Since 2026-10-15 the host resets by re-applying the loaded state
and re-sending the knobs set since (``native_host`` docstring): bit-identical to a re-created
instance and to dedicated per-preset captures, history-independent, ~0.25 s per render instead
of ~1.9 s, and no memory growth. pedalboard's own reset re-created the Native instance on every
render and leaked ~355 MB each time (the host reached 7-11 GB and was SIGKILLed);
``TONEMATCH_NATIVE_HOST_RESET=reinstantiate`` restores it for comparison. A watchdog recycles
the host above ``mem_limit_mb`` (``TONEMATCH_NATIVE_HOST_MEM_LIMIT_MB``, default 5120) and
restores chain and knobs. ``set_params`` only moves automation knobs (~0.5 ms). The opt-in
``reset_mode="flush"`` skips the reset entirely.

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

from ..preset import (  # noqa: F401  canonical snapshot sync lives in tonematch.preset
    snapshot_mismatches,
    sync_hlx_snapshots,
    sync_snapshots_to_blocks,
)
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


def rss_mb(pid: int) -> float | None:
    """Resident set size of process ``pid`` in MB (``ps``; None if it is gone or ps is missing)."""
    try:
        r = subprocess.run(["ps", "-o", "rss=", "-p", str(int(pid))], capture_output=True, text=True,
                           check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    s = r.stdout.strip()
    return float(s) / 1024 if s else None


class HostProcess:
    """One plugin-host child process. Thread-safe per instance (one request at a time)."""

    def __init__(self, plugin: str, plugin_name: str | None = None, block_size: int = 512):
        from .native_host import read_msg, write_msg  # protocol helpers only, no pedalboard

        self._read, self._write = read_msg, write_msg
        self._lock = threading.Lock()
        self.plugin, self.plugin_name, self.block_size = plugin, plugin_name, block_size
        self.info: dict = {}
        self.mem_mb: float | None = None  # host's physical footprint after the last reply
        self._spawn()

    def _spawn(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            [host_python(), "-m", "tonematch.engines.native_host"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, env=env,
        )
        try:
            self.info = self.call("open", path=self.plugin, plugin_name=self.plugin_name,
                                  sample_rate=SAMPLE_RATE, block_size=self.block_size)[0]
        except EngineError:
            self.proc.kill()  # never leave a half-open host (each holds GBs) behind
            self.proc.wait(timeout=10)
            raise

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
        if hdr.get("mem_mb") is not None:
            self.mem_mb = float(hdr["mem_mb"])
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


def _hc_mode(raw: bytes) -> int | None:
    """Native's Hardware Compatibility mode word from a state (ILCP chunk), None if not Native."""
    try:
        from . import native_state as ns

        pl = ns.chunk_payload(ns.plugin_component(raw)[1], "ILCP")
    except Exception:  # noqa: BLE001 - non-Native (builtin stand-in) or malformed state
        return None
    return int.from_bytes(pl[8:12], "little") if pl and len(pl) >= 12 else None


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
    # T018n stale-template fix: every value the captured state carries that the host cannot set
    # (unmapped params, bypass states, extras, and mapped params whose CC was not verified to
    # move the render) is "baked". A template is reused only for presets whose baked values
    # hash the same. Templates without a hash (older captures) are never reused.
    verified: list[str] = field(default_factory=list)
    baked: dict = field(default_factory=dict)
    baked_hash: str = ""

    @classmethod
    def load_all(cls, d: Path | None = None) -> dict[tuple, ChainTemplate]:
        d = d or cache_dir() / "templates"
        out = {}
        for j in sorted(d.glob("*.json")) if d.is_dir() else []:
            meta = json.loads(j.read_text())
            chain = tuple(tuple(x) for x in meta["chain"])
            state = (j.with_suffix(".state")).read_bytes()
            out[chain] = cls(chain, state, meta.get("map", {}), meta.get("ranges", {}), meta.get("name", j.stem),
                             meta.get("verified", []), meta.get("baked", {}), meta.get("baked_hash", ""))
        return out

    @classmethod
    def load_variants(cls, d: Path | None = None) -> dict[tuple, list[ChainTemplate]]:
        """All templates per chain (several baked-value variants may share a structure)."""
        d = d or cache_dir() / "templates"
        out: dict[tuple, list[ChainTemplate]] = {}
        for j in sorted(d.glob("*.json")) if d.is_dir() else []:
            meta = json.loads(j.read_text())
            if "chain" not in meta or not j.with_suffix(".state").is_file():
                continue
            chain = tuple(tuple(x) for x in meta["chain"])
            out.setdefault(chain, []).append(cls(
                chain, j.with_suffix(".state").read_bytes(), meta.get("map", {}), meta.get("ranges", {}),
                meta.get("name", j.stem), meta.get("verified", []), meta.get("baked", {}),
                meta.get("baked_hash", "")))
        return out

    def save(self, d: Path | None = None) -> Path:
        d = d or cache_dir() / "templates"
        d.mkdir(parents=True, exist_ok=True)
        cid = chain_id([list(x) for x in self.chain])
        stem = f"{cid}_{self.baked_hash[:10]}" if self.baked_hash else cid
        (d / f"{stem}.state").write_bytes(self.state)
        p = d / f"{stem}.json"
        p.write_text(json.dumps({"name": self.name, "chain": [list(x) for x in self.chain],
                                 "map": self.map, "ranges": self.ranges, "verified": self.verified,
                                 "baked_hash": self.baked_hash, "baked": self.baked}, indent=1))
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


def autocapture_enabled() -> bool:
    """``TONEMATCH_NATIVE_AUTOCAPTURE=1`` lets ``load()`` capture unknown chains itself (T018n)."""
    return os.environ.get("TONEMATCH_NATIVE_AUTOCAPTURE", "0").strip().lower() in ("1", "true", "yes")


def start_state_path() -> Path:
    """The human-made HX Stomp start state (scripts/native_hc_once.py), if present."""
    return cache_dir() / "start_stomp.state"


def _scalar(v):
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    return json.dumps(v, sort_keys=True, default=str)


def baked_fingerprint(preset, amap: dict[str, str], verified) -> tuple[dict, str]:
    """Values a template's state fixes for ``preset``: every param, bypass and extra except
    the mapped params whose CC was verified to move the render. Returns (values, sha1)."""
    live = {k for k in verified if k in amap}
    vals: dict = {}
    for i, s in enumerate(preset.slots):
        if s.model_id is None:
            continue
        for name, v in sorted((s.params or {}).items()):
            if f"{i}:{name}" not in live:
                vals[f"{i}:{name}"] = _scalar(v)
        if f"{i}:@enabled" not in live:
            vals[f"{i}:@enabled"] = bool(s.enabled)
        for k, v in sorted((getattr(s, "extra", None) or {}).items()):
            if k not in ("@position", "@cab"):
                vals[f"{i}:{k}"] = _scalar(v)
    h = hashlib.sha1(json.dumps(vals, sort_keys=True).encode()).hexdigest()
    return vals, h


def catalog_ranges(preset, catalog: dict | None = None) -> dict[str, tuple[float, float]] | None:
    """``"<slot>:<param>" -> (min, max)`` for continuous params from the HX catalog, or None."""
    if catalog is None:
        try:
            from tonematch.catalog import load_catalog

            catalog = load_catalog()
        except Exception:  # noqa: BLE001 - no catalog cached: fall back to [0, 1] params
            return None
    models = catalog.get("models", catalog)
    out = {}
    for i, s in enumerate(preset.slots):
        m = models.get(s.model_id or "", {})
        for name in s.params or {}:
            p = (m.get("params") or {}).get(name)
            if p and p.get("kind") == "continuous" and p.get("min") is not None and p.get("max") is not None:
                out[f"{i}:{name}"] = (float(p["min"]), float(p["max"]))
    return out


def capture_chain(preset, plugin: str, *, plugin_name: str | None = None,
                  start_state: bytes | None = None, templates_dir: Path | None = None,
                  host_factory=None, open_wait_s: float = 8.0, load_wait_s: float = 4.0,
                  verify_ccs: bool = True, catalog: dict | None = None) -> ChainTemplate:
    """Capture a template for ``preset``'s chain with no human (T018n, macOS VST3).

    Writes the preset as ``.hlx`` with a free MIDI CC assigned to every [0, 1] parameter
    (``native_setlist.assign_ccs``), opens Native's editor in a fresh host process, drops the
    file on it in-process (``native_gui``), closes the editor and saves the state as a
    ``ChainTemplate`` whose map points at the VST3's ``cc_<n>`` params. Raises
    ``UnsupportedModelChange`` with the reason if it cannot (no GUI session, no PyObjC, Native
    asked a question, or the state did not change). ``host_factory(plugin, plugin_name)`` must
    return an object with ``call``/``close`` like ``HostProcess`` (tests pass a fake).

    CC ranges come from the HX catalog for continuous params (cab Hz, dB levels included);
    without a catalog only params valued in [0, 1] are mapped, as [0, 1]. With ``verify_ccs``
    each mapped CC is checked by render (one render per CC, 3 s pre-roll): only CCs that move
    the render are ``verified``; everything else is baked into the template's fingerprint, so
    the template is reused only for presets with identical baked values.
    """
    from .native_setlist import assign_ccs

    key = chain_key(preset)
    cid = chain_id([list(k) for k in key])
    d = sync_hlx_snapshots(preset.to_hlx_dict())  # block values win (lead, 2026-10-15)
    ranges = catalog_ranges(preset, catalog)
    targets = [(i, n) for i, s in enumerate(preset.slots) for n in (s.params or {})
               if ranges is None or f"{i}:{n}" in ranges]
    d, amap, rng, _skipped = assign_ccs(d, targets, ranges=ranges)
    _, full_hash = baked_fingerprint(preset, {}, [])
    out = cache_dir() / "drop"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{cid}_{full_hash[:10]}.hlx"
    path.write_text(json.dumps(d, indent=1))
    host = (host_factory or HostProcess)(plugin, plugin_name)
    try:
        gui = host.call("gui_available")[0]
        if not gui.get("gui"):
            raise UnsupportedModelChange(f"Native autocapture unavailable: {gui.get('reason')}")
        if start_state:
            host.call("reload_with_state", start_state, path=plugin, plugin_name=plugin_name)
        before = host.call("get_state")[1]
        res = host.call("editor_drop", path=str(path.resolve()), open_wait_s=open_wait_s,
                        load_wait_s=load_wait_s)[0]
        if res.get("error"):
            raise UnsupportedModelChange(f"Native autocapture failed: {res['error']}")
        if res.get("dialogs"):
            raise UnsupportedModelChange(
                "Native asked a question while loading the file (usually the HC-mode prompt): run "
                "scripts/native_hc_once.py once and tick \"Don't ask me again\"")
        state = host.call("get_state")[1]
        if state == before:
            raise UnsupportedModelChange(
                f"Native autocapture: the drop of {path.name} did not change the state")
        verified = _verify_ccs(host, plugin, plugin_name, state, preset, amap, rng) if (
            verify_ccs and amap) else []
    finally:
        host.close()
    baked, h = baked_fingerprint(preset, amap, verified)
    tpl = ChainTemplate(key, state, amap, rng, name=getattr(preset, "name", "") or cid,
                        verified=verified, baked=baked, baked_hash=h)
    tpl.save(templates_dir)
    return tpl


def _verify_ccs(host, plugin, plugin_name, state, preset, amap, rng,
                threshold_db: float = -45.0, preroll_s: float = 3.0) -> list[str]:
    """Mapped params whose CC moves the render (one render each, against a base render)."""
    host.call("reload_with_state", state, path=plugin, plugin_name=plugin_name)
    pre = int(preroll_s * SAMPLE_RATE)
    x = np.concatenate([np.zeros(pre, np.float32), test_signal(1.0)])

    def render() -> np.ndarray:
        _, data = host.call("render", x.astype("<f4").tobytes(), reset=True)
        return np.frombuffer(data, dtype="<f4")[pre:]

    def norm(ref: str) -> float:
        i, name = ref.split(":", 1)
        lo, hi = rng.get(ref, [0.0, 1.0])
        v = float((preset.slots[int(i)].params or {}).get(name, lo))
        return float(np.clip((v - lo) / ((hi - lo) or 1.0), 0.0, 1.0))

    host.call("set_params", values={prm: norm(ref) for ref, prm in amap.items()})
    base = render()
    ok = []
    for ref, prm in amap.items():
        v = norm(ref)
        host.call("set_params", values={prm: 0.25 if v >= 0.5 else 0.75})
        if null_db(render(), base) > threshold_db:
            ok.append(ref)
        host.call("set_params", values={prm: v})
    return ok


class NativeEngine:
    """Helix Native via a child-process plugin host. See the module docstring."""

    SILENT_IN_DB = -70.0    # input RMS above this counts as non-silent
    SILENT_OUT_DB = -100.0  # output RMS below this for a non-silent input is an error
    # Native 3.82 fades its output in after a host reset: about 3,180 samples to reach unity
    # within 0.1% (measured 2026-10-14, VST3 and AU, any block size). Every render is reset for
    # determinism, so each one is prefixed with this much silence, then trimmed off.
    PREROLL = 4800
    # A pedalboard reset (instance re-created) cost Native 3.82 about 1.6-1.9 s and leaked ~355 MB;
    # the host's state reset costs a state load (~0.05 s) plus the DSP (~0.045 s per second of
    # audio on an amp+cab+delay chain), measured 2026-10-15. reset_mode="flush" skips the reset
    # and pre-rolls flush_s of silence instead; it is history-dependent (about -50 dB from a
    # 16-sample frame FIFO and tails), so "reset" stays the default.
    RESET_MODES = ("reset", "flush")
    # The host is recycled (new process, chain and knobs restored) when its physical footprint
    # passes this. Native leaks ~355 MB per plugin instance it ever created (native_host
    # docstring); with the host's state reset a host holds ~2-3 GB steady, and each
    # Hardware-Compatibility-mode reload (reload_with_state) adds ~1.6 GB. 0 disables.
    MEM_LIMIT_MB = 5120.0

    def __init__(self, plugin_path: str | Path | None = None, plugin_name: str | None = None,
                 *, calibration_db: float = 0.0, block_size: int = 512,
                 templates_dir: Path | None = None, route: str | None = None,
                 preroll: int | None = None, reset_mode: str | None = None, flush_s: float = 2.0,
                 autocapture: bool | None = None, mem_limit_mb: float | None = None):
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
        self.autocapture = autocapture_enabled() if autocapture is None else bool(autocapture)
        self.last_capture_s: float | None = None
        self._capture_host_factory = None  # tests inject a fake host here
        self.host = HostProcess(p, plugin_name, block_size)
        self._params = {d["name"]: d for d in self.host.info.get("params", [])}
        self.templates = ChainTemplate.load_all(templates_dir)
        self._variants = ChainTemplate.load_variants(templates_dir)
        self._initial_state = self._get_state()
        self._hc_mode = _hc_mode(self._initial_state) if self._initial_state else None
        self.codec = _load_codec()
        self.route = route or ("state" if self.codec else "automation")
        self.current_chain: tuple | None = None
        self.current_template: ChainTemplate | None = None
        self.last_unmapped: list[str] = []
        self.last_gain_db = self.calibration_db
        self._latency: dict[tuple | None, int] = {}
        self._cost: Cost | None = None
        env_lim = os.environ.get("TONEMATCH_NATIVE_HOST_MEM_LIMIT_MB")
        self.mem_limit_mb = float(mem_limit_mb if mem_limit_mb is not None else
                                  env_lim if env_lim else self.MEM_LIMIT_MB)
        self.recycles = 0
        self.last_reset_via: str | None = None  # how the host reset the last render
        self._loaded_state: bytes | None = None  # last state sent to the host (restored on recycle)
        self._host_params: dict[str, float] = {}  # host params set since that state

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
        tpl, why = self._match_template(preset, key)
        if tpl is None and self.autocapture:
            tpl = self._autocapture(preset)
        if tpl is None:
            raise UnsupportedModelChange(
                f"No usable Native template for chain {chain_id([list(x) for x in key])} "
                f"({[m for _, m in key if m]}): {why}. Capture it hands-off with "
                "TONEMATCH_NATIVE_AUTOCAPTURE=1 (macOS, GUI session, extra native-gui)."
            )
        if key != self.current_chain or tpl is not self.current_template:
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

    def _match_template(self, preset, key) -> tuple[ChainTemplate | None, str]:
        """The template whose baked values (state-fixed, not host-settable) equal the preset's.

        Stale-template fix (DECISIONS 2026-10-15): a structure match alone is not enough, since
        unmapped or unverified params live in the captured state. Templates without a
        fingerprint (captured before the fix) are never reused.
        """
        cands = self._variants.get(key, [])
        if not cands:
            return None, "no template captured for this structure"
        diffs = []
        for t in cands:
            if not t.baked_hash:
                diffs.append(f"{t.name!r} has no baked-value fingerprint (captured before the fix)")
                continue
            vals, h = baked_fingerprint(preset, t.map, t.verified)
            if h == t.baked_hash:
                return t, ""
            d = sorted(k for k in set(vals) | set(t.baked) if vals.get(k) != t.baked.get(k))
            diffs.append(f"{t.name!r} differs in baked {d[:6]}")
        return None, "; ".join(diffs[:4])

    def _autocapture(self, preset) -> ChainTemplate:
        """Capture an unknown chain by in-process drop (T018n); raises UnsupportedModelChange."""
        p = start_state_path()
        t0 = time.perf_counter()
        tpl = capture_chain(preset, self.plugin, plugin_name=self.plugin_name,
                            start_state=p.read_bytes() if p.is_file() else None,
                            templates_dir=self.templates_dir,
                            host_factory=self._capture_host_factory)
        self.last_capture_s = time.perf_counter() - t0
        self.templates[tpl.chain] = tpl
        self._variants.setdefault(tpl.chain, []).append(tpl)
        return tpl

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
        self._host_params.update(values)

    def render(self, dry: Audio) -> Audio:
        x = np.asarray(dry, dtype=np.float32)
        if x.ndim != 1:
            raise EngineError(f"render() takes mono (n,) float32, got shape {x.shape}")
        if len(x) == 0:
            return x.copy()
        self.check_memory()
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
                    "reset_mode": self.reset_mode, "host_reset": info.get("reset_strategy"),
                    "host_mem_mb": self.host.mem_mb, "host_mem_limit_mb": self.mem_limit_mb,
                    "host_recycles": self.recycles,
                    "last_reset_via": getattr(self, "last_reset_via", None)},
        )

    def close(self) -> None:
        self.host.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # --- internals ------------------------------------------------------------------------

    def _raw_render(self, x: np.ndarray, reset: bool = True) -> np.ndarray:
        hdr, data = self.host.call("render", x.astype("<f4").tobytes(), reset=reset)
        self.last_reset_via = hdr.get("reset_via")
        return np.frombuffer(data, dtype="<f4").astype(np.float32)

    def _get_state(self) -> bytes:
        if not self.host.info.get("has_state"):
            return b""  # builtin test stand-ins have no saved state
        return self.host.call("get_state")[1]

    def _set_state(self, blob: bytes) -> None:
        mode = _hc_mode(blob)
        try:
            if mode is not None and mode != self._hc_mode:
                # Native 3.82 ignores a state that switches HC mode once audio has been processed
                # (T018n): give it a fresh instance with the state set before any render.
                self.host.call("reload_with_state", blob, path=self.plugin, plugin_name=self.plugin_name)
            else:
                self.host.call("set_state", blob)
            if mode is not None:
                self._hc_mode = mode
            self._loaded_state, self._host_params = blob, {}
        except EngineError:
            self.host.restart()  # a bad blob may have killed or wedged the host
            self._hc_mode = _hc_mode(self._initial_state) if self._initial_state else None
            self._loaded_state, self._host_params = None, {}
            raise

    def check_memory(self) -> bool:
        """Recycle the host if its footprint passed ``mem_limit_mb``; True if it did.

        The new process gets the loaded state and every knob set since, so renders continue
        unchanged (the state reset makes them history-independent). If a fresh host restored
        to this chain already sits near the limit, the limit is raised to fresh + 2 GB rather
        than recycling on every render.
        """
        mem = self.host.mem_mb
        if not self.mem_limit_mb or mem is None or mem <= self.mem_limit_mb:
            return False
        self.recycle_host()
        fresh = self.host.mem_mb
        if fresh is not None and fresh > 0.8 * self.mem_limit_mb:
            new = max(self.mem_limit_mb, fresh + 2048)  # only ever raise a limit
            print(f"NativeEngine: a fresh host on this chain uses {fresh:.0f} MB; raising the "
                  f"recycle limit from {self.mem_limit_mb:.0f} to {new:.0f} MB", file=sys.stderr)
            self.mem_limit_mb = new
        return True

    def recycle_host(self) -> None:
        """Replace the host process and restore the loaded state and knobs."""
        state, params = self._loaded_state, dict(self._host_params)
        self.host.restart()
        self.recycles += 1
        self._hc_mode = _hc_mode(self._initial_state) if self._initial_state else None
        self._loaded_state, self._host_params = None, {}
        if state is not None:
            self._set_state(state)
        if params:
            self.host.call("set_params", values=params)
            self._host_params = params

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
        host_reset = self.host.info.get("reset_strategy") or "reinstantiate"
        return (f"{self.plugin}|{self.host.info.get('version')}|{platform.node()}|{os.cpu_count()}|{sh}"
                f"|{self.reset_mode}|host:{host_reset}")

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


def max_hosts_by_memory(per_host_gb: float = 3.0, share: float = 0.5) -> int:
    """Native hosts that fit in ``share`` of physical memory (``TONEMATCH_NATIVE_MAX_HOSTS`` overrides)."""
    env = os.environ.get("TONEMATCH_NATIVE_MAX_HOSTS")
    if env:
        return max(1, int(env))
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        return 4
    return max(1, int(total * share / per_host_gb))


def measure_parallelism(plugin: str, plugin_name: str | None, state: bytes, clip: np.ndarray,
                        max_workers: int | None = None, preroll: int = 0,
                        rounds: int = 3, reset: bool = True) -> tuple[int, dict[int, float]]:
    """Renders/s for k host processes rendering at once; picks the k past which gains drop under 15%.

    The scan stops at the first k that gains under 15% (the choice cannot change after it), and
    new host processes are opened concurrently (each Native open is ~5 s), so a cold scan costs
    well under a minute instead of ~2.5 minutes. Without ``max_workers`` the scan is capped by
    memory as well as CPUs (``max_hosts_by_memory``): each Native host holds 2-3 GB.
    """
    cpu = os.cpu_count() or 2
    max_workers = max_workers or max(1, min(cpu - 1, max_hosts_by_memory()))
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
