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
import copy
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


# Host canary (2026-10-15). About 1 Native host process in 10 renders every chain slightly
# differently for its whole life ("mode B": DoubleTank -27 dB, Dig Delay -21, Ganymede -22, an
# EQ -69, a fuzz -84 dB against the usual "mode A"; identical across B hosts; params and saved
# state identical; not CPU load, core type, drops or frame phase). Each B host is consistent with
# itself, so search and parity results silently depended on which host rendered them. Every
# host opened in a start state renders a short canary through it; a host that disagrees with
# the machine's reference is replaced (majority of three decides if the reference is in doubt).
CANARY_THRESHOLD_DB = -150.0
CANARY_RETRIES = 6
_CANARY_LOCK = threading.Lock()


def canary_signal() -> np.ndarray:
    return np.concatenate([np.zeros(4800, np.float32), test_signal(0.5, seed=7)])


def _canary_file(plugin: str, state: bytes) -> Path:
    key = hashlib.sha1(state + plugin_version(plugin).encode() + platform.node().encode()).hexdigest()[:16]
    return cache_dir() / "canary" / f"{key}.npy"


def _canary_votes(f: Path) -> list[dict]:
    """[{"y": canary render, "n": hosts seen}] for one key (``.json`` index + one ``.npy`` each)."""
    try:
        meta = json.loads(f.with_suffix(".json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for m in meta:
        try:
            out.append({"y": np.load(f.parent / m["npy"]), "n": int(m["n"])})
        except (OSError, ValueError, KeyError):
            continue
    return out


def _save_canary_votes(f: Path, votes: list[dict]) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    meta = []
    for i, v in enumerate(votes):
        name = f"{f.stem}_{i}.npy"
        if not (f.parent / name).is_file():
            np.save(f.parent / name, v["y"])
        meta.append({"npy": name, "n": v["n"]})
    f.with_suffix(".json").write_text(json.dumps(meta))


def canary_enabled() -> bool:
    return os.environ.get("TONEMATCH_NATIVE_CANARY", "1").strip().lower() not in ("0", "false", "no")


class HostProcess:
    """One plugin-host child process. Thread-safe per instance (one request at a time).

    Opened in ``initial_state`` (a real plugin), the host is checked against the machine's
    canary reference (see ``CANARY_THRESHOLD_DB``) and replaced until it agrees;
    ``canary_null_db`` and ``canary_respawns`` record what happened."""

    def __init__(self, plugin: str, plugin_name: str | None = None, block_size: int = 512,
                 initial_state: bytes | None = None, canary: bool | None = None):
        from .native_host import read_msg, write_msg  # protocol helpers only, no pedalboard

        self._read, self._write = read_msg, write_msg
        self._lock = threading.Lock()
        self.plugin, self.plugin_name, self.block_size = plugin, plugin_name, block_size
        self.initial_state = initial_state or b""  # set before any audio (HC mode at start)
        self.info: dict = {}
        self.mem_mb: float | None = None  # host's physical footprint after the last reply
        self.canary = (canary_enabled() if canary is None else canary) and bool(self.initial_state) \
            and not str(plugin).startswith("builtin:")
        self.canary_null_db: float | None = None
        self.canary_respawns = 0
        self._spawn()

    def _spawn(self) -> None:
        self._spawn_process()
        if self.canary:
            self._check_canary()

    def _spawn_process(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            [host_python(), "-m", "tonematch.engines.native_host"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, env=env,
        )
        try:
            self.info = self.call("open", self.initial_state, path=self.plugin,
                                  plugin_name=self.plugin_name, sample_rate=SAMPLE_RATE,
                                  block_size=self.block_size)[0]
        except EngineError:
            self.proc.kill()  # never leave a half-open host (each holds GBs) behind
            self.proc.wait(timeout=10)
            raise

    def _canary_render(self) -> np.ndarray:
        x = canary_signal()
        _, y = self.call("render", x.astype("<f4").tobytes(), reset=True)
        return np.frombuffer(y, dtype="<f4")[4800:].copy()

    def _check_canary(self) -> None:
        """Replace this host until its canary is the machine's majority render (module comment).

        Every host's canary is a vote, kept on disk (``cache/native/canary/<key>.json``): renders
        that null to ``CANARY_THRESHOLD_DB`` form one cluster. A host is accepted when its
        cluster has the most votes and at least two (so the first host on a machine waits for a
        second, agreeing one). Odd hosts are about 1 in 10 (up to 2 in 5 when opened together),
        so a pool can never switch the reference to the odd mode by outvoting it in one burst."""
        f = _canary_file(self.plugin, self.initial_state)
        nd = 0.0
        for attempt in range(CANARY_RETRIES + 1):
            y = self._canary_render()
            with _CANARY_LOCK:
                votes = _canary_votes(f)
                idx = next((i for i, v in enumerate(votes) if len(v["y"]) == len(y)
                            and null_db(v["y"], y) <= CANARY_THRESHOLD_DB), None)
                if idx is None:
                    votes.append({"y": y, "n": 0})
                    idx = len(votes) - 1
                votes[idx]["n"] += 1
                _save_canary_votes(f, votes)
                best = max(range(len(votes)), key=lambda i: (votes[i]["n"], -i))
                nd = null_db(votes[best]["y"], y) if idx != best else -300.0
                if idx == best and votes[best]["n"] >= 2:
                    self.canary_null_db = nd
                    return
            if votes[best]["n"] >= 2:
                print(f"NativeHost: host renders differently from the majority ({nd:.1f} dB, "
                      f"votes {[v['n'] for v in votes]}); replacing it", file=sys.stderr)
            if attempt == CANARY_RETRIES:
                break
            self.canary_respawns += 1
            self.close()
            self._spawn_process()
        self.close()
        raise EngineError(f"Native host canary: no agreeing host in {CANARY_RETRIES + 1} tries")

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
        stem = template_stem(self)
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
    the mapped params whose CC was verified to move the render. Returns (values, sha1).

    A block's bypass CC (``"<slot>:@enabled"``) is live whenever it is assigned: bypass by CC
    equals drop-captured truth bit-exactly on every block type tried (phase 2, 2026-10-15), and
    a block that is transparent at capture settings (a flat EQ) would otherwise never verify."""
    live = {k for k in verified if k in amap} | {k for k in amap if k.endswith(":@enabled")}
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


# Narrower CC ranges (T072, lead ruling on T066b, 2026-10-16). A MIDI CC has 128 values over
# its assignment's @min..@max, and Native honours a per-assignment @min/@max written in the
# dropped .hlx (bit-exact, scripts/native_cc_range_probe.py; the range is kept in the captured
# state). Over the catalog range, delay Time 0-8 s moves in 62.99 ms steps and an LFO Speed
# 0-15 Hz in 0.118 Hz steps. These rules give time-like and rate params a narrower range;
# a value outside it keeps the catalog range (``cc_ranges``), so no preset loses its value.
CC_RANGE_RULES_VERSION = "cc-range-v1"
DELAY_TIME_SPAN_S = 1.27            # 10 ms per CC step: half a step (5 ms) is inside the 2 %
#                                     delay-time tolerance from 250 ms up (T066)
DELAY_NOT_ECHO_TIMES = frozenset({"Attack", "Decay", "DynAttack", "DynRel"})
RATE_STEPS_HZ = (0.02, 0.04, 0.08)  # LFO rate ladder; the smallest whose span holds the headroom
RATE_MIN_HI_HZ = 2.5                # every narrowed rate range reaches at least 2.5 Hz
RATE_HEADROOM = 2.0                 # ...and at least twice the model's default rate
DECAY_STEPS_S = (0.05, 0.1)         # reverb Decay in seconds (catalog 0.1-45.1 s)
DECAY_HEADROOM = 2.0


def narrow_range(category: str, name: str, p: dict) -> tuple[float, float] | None:
    """The narrowed CC range for catalog param ``p`` (``name`` in a ``category`` block), or None
    when the catalog range stays. Rules (``CC_RANGE_RULES_VERSION``):

    - delay echo times (display ``time_ms*``; not Attack/Decay/DynAttack/DynRel): [lo, lo + 1.27 s],
      10 ms steps (0-8 s gave 62.99 ms, 0-2 s 15.75 ms);
    - LFO rates in Hz (display ``frequency`` or ``4VoiceChorus_Rate`` with max <= 20 Hz, names
      with Speed or Rate): [lo, lo + 127 s] for the smallest s in ``RATE_STEPS_HZ`` whose top
      reaches max(2.5 Hz, 2 x default); fast-default LFOs (trem, vibrato, rotary) keep the
      catalog range, whose step is already under 2 % of their rate;
    - reverb Decay in seconds (display ``time_sec_DynamicHall``): [lo, lo + 127 s], s in
      ``DECAY_STEPS_S``, top at least 2 x default.

    Pre-delays (0-0.2 s: 1.57 ms steps, half a step 0.79 ms against a 5 ms tolerance) and every
    other param keep the catalog range. A narrowed range is never wider than the catalog's."""
    lo, hi = p.get("min"), p.get("max")
    if p.get("kind") != "continuous" or lo is None or hi is None:
        return None
    lo, hi = float(lo), float(hi)
    disp = str(p.get("display") or "")
    dflt = float(p.get("default") or lo) if isinstance(p.get("default"), (int, float)) else lo
    if category == "delay" and disp.startswith("time_ms") and name not in DELAY_NOT_ECHO_TIMES:
        top = lo + DELAY_TIME_SPAN_S
        return (lo, round(top, 6)) if top < hi - 1e-9 else None
    if (disp in ("frequency", "4VoiceChorus_Rate") and hi <= 20.0
            and ("Speed" in name or "Rate" in name)):
        need = max(RATE_MIN_HI_HZ, RATE_HEADROOM * dflt)
        for s in RATE_STEPS_HZ:
            top = lo + CC_STEPS * s
            if top >= need:
                return (lo, round(top, 6)) if top < hi - 1e-9 else None
        return None
    if category == "reverb" and disp == "time_sec_DynamicHall":
        for s in DECAY_STEPS_S:
            top = lo + CC_STEPS * s
            if top >= DECAY_HEADROOM * dflt:
                return (lo, round(top, 6)) if top < hi - 1e-9 else None
    return None


def cc_ranges(preset, catalog: dict | None = None, narrow: bool = True) -> dict[str, tuple[float, float]] | None:
    """The CC range each continuous param of ``preset`` is assigned over at capture: the catalog
    range (``catalog_ranges``), narrowed by ``narrow_range`` when ``narrow`` and the preset's
    own value lies inside the narrowed range. Templates record the result in ``ranges``."""
    out = catalog_ranges(preset, catalog)
    if out is None or not narrow:
        return out
    if catalog is None:
        from tonematch.catalog import load_catalog

        catalog = load_catalog()
    models = catalog.get("models", catalog)
    for i, s in enumerate(preset.slots):
        m = models.get(s.model_id or "", {})
        for name, v in (s.params or {}).items():
            ref = f"{i}:{name}"
            p = (m.get("params") or {}).get(name)
            if ref not in out or not p or isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            nr = narrow_range(s.category or m.get("category", ""), name, p)
            if nr is not None and nr[0] - 1e-9 <= float(v) <= nr[1] + 1e-9:
                out[ref] = nr
    return out


def out_of_range(tpl: ChainTemplate, preset, tol: float = 1e-6) -> list[str]:
    """CC-driven params of ``preset`` whose value lies outside ``tpl``'s assignment range: the
    CC would clamp them, so ``tpl`` cannot render ``preset`` (narrowed ranges, T072)."""
    bad = []
    live = set(tpl.verified) & set(tpl.map)
    for i, s in enumerate(preset.slots):
        for name, v in (s.params or {}).items():
            ref = f"{i}:{name}"
            if ref not in live or ref not in tpl.ranges or isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            lo, hi = tpl.ranges[ref]
            span = abs(hi - lo) or 1.0
            if not lo - tol * span <= float(v) <= hi + tol * span:
                bad.append(ref)
    return bad


def range_width_key(tpl: ChainTemplate) -> float:
    """Sum of log widths of ``tpl``'s CC ranges: smaller is a finer grid (template choice)."""
    return float(sum(np.log(max(abs(hi - lo), 1e-12)) for lo, hi in tpl.ranges.values()))


def _capture_doc(preset, catalog, bypass_ccs: bool):
    """The drop file for ``preset``: snapshots synced, bypass and param CCs assigned."""
    from .native_setlist import assign_ccs

    key = chain_key(preset)
    cid = chain_id([list(k) for k in key])
    d = sync_hlx_snapshots(preset.to_hlx_dict())  # block values win (lead, 2026-10-15)
    ranges = cc_ranges(preset, catalog)
    # Bypass CCs first (phase 2, 2026-10-15): one capture then serves every bypass pattern of
    # the chain, so they get first claim on the 90 free CCs.
    targets = [(i, "@enabled") for i, s in enumerate(preset.slots) if bypass_ccs and s.model_id]
    targets += [(i, n) for i, s in enumerate(preset.slots) for n in (s.params or {})
                if ranges is None or f"{i}:{n}" in ranges]
    d, amap, rng, _skipped = assign_ccs(d, targets, ranges=ranges)
    _, full_hash = baked_fingerprint(preset, {}, [])
    out = cache_dir() / "drop"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{cid}_{full_hash[:10]}.hlx"
    path.write_text(json.dumps(d, indent=1))
    return key, cid, amap, rng, path


# Blocks whose tails survive the host's state reset (delay lines, 2026-10-15): CC verification
# renders of chains holding one get a 3 s pre-roll; other chains need only 0.5 s.
TAIL_CATEGORIES = frozenset({"delay", "reverb", "other"})
CC_STEPS = 127  # a MIDI CC has 128 values over the assignment's Min..Max
# The truth check moves every mapped param by this many CC steps (1/8 of its range), toward the
# middle (dB levels alternately up and down). Jumping every param to 32/127 or 95/127 put C8's
# six Level params near -43 dB each, the chain output near the float floor, and the renders
# stopped repeating (-54.9 dB); moving every level 8 dB down still left it at -81 dBFS.
MOVE_STEPS = 16


def cc_value(lo: float, hi: float, c: int) -> float:
    """The parameter value Native gives a MIDI CC at ``c`` (0..127) assigned over [lo, hi]:
    linear, lo + c/127 (hi - lo). Measured 2026-10-15 (scripts/native_cc_grid_probe.py): a
    drop capture with this value equals the CC route at -260 to -317 dB on Hz, dB, Q, ms and
    distance params (cab, EQ, comp, chorus, delay, reverb); log and c/128 grids miss by -13 to
    -71 dB. Computed in float32, as the plugin stores it."""
    n = np.float32(c) / np.float32(CC_STEPS)
    return float(np.float32(lo) + n * (np.float32(hi) - np.float32(lo)))


def verify_preroll_s(preset) -> float:
    """Pre-roll for CC verification renders of ``preset``'s chain (see ``TAIL_CATEGORIES``)."""
    cats = {s.category for s in preset.slots if s.model_id}
    return 3.0 if cats & TAIL_CATEGORIES else 0.5


def _pair(preset, ref: str) -> str:
    i, name = ref.split(":", 1)
    return f"{preset.slots[int(i)].model_id}|{name}"


def _norm(preset, rng: dict, ref: str) -> float:
    """``ref``'s value in ``preset`` as the CC's normalized value (bypass: 1.0, every block on)."""
    i, name = ref.split(":", 1)
    if name == "@enabled":  # verify params with every block on, so a bypassed block's
        return 1.0          # params are not judged dead
    lo, hi = rng.get(ref, [0.0, 1.0])
    v = float((preset.slots[int(i)].params or {}).get(name, lo))
    return float(np.clip((v - lo) / ((hi - lo) or 1.0), 0.0, 1.0))


def moved_preset(preset, amap: dict, rng: dict):
    """``preset`` with every CC-mapped param moved ``MOVE_STEPS`` CC steps from its own value
    toward the middle of its range (on the CC grid, ``cc_value``), and every block on.

    Returns ``(moved, values)``: ``values`` are the host CC values that reproduce ``moved`` on
    ``preset``'s template. The truth check renders both ways and nulls them."""
    p = copy.deepcopy(preset)
    p.name = f"{getattr(preset, 'name', '') or 'chain'} moved"
    values: dict[str, float] = {}
    up = False
    for ref, prm in amap.items():
        i, name = ref.split(":", 1)
        if name == "@enabled":
            values[prm] = 1.0
            continue
        lo, hi = rng.get(ref, [0.0, 1.0])
        c0 = round(_norm(preset, rng, ref) * CC_STEPS)
        if lo <= -24.0:  # a dB level: alternate up and down so the chain's gain stays put
            up = not up
            c = min(c0 + MOVE_STEPS, CC_STEPS) if up and c0 < CC_STEPS else max(c0 - MOVE_STEPS, 0)
        else:
            c = c0 - MOVE_STEPS if c0 >= 64 else c0 + MOVE_STEPS
        p.slots[int(i)].params[name] = cc_value(lo, hi, c)
        values[prm] = c / CC_STEPS
    for s in p.slots:
        if s.model_id:
            s.enabled = True
    return p, values


def capture_key(preset, catalog=None, bypass_ccs: bool = True) -> str:
    """Presets with equal keys can share one capture: the chain structure plus every value the
    capture would bake if all of its CC-mapped params verify (``capture_missing`` dedupe)."""
    from .native_setlist import assign_ccs

    ranges = cc_ranges(preset, catalog)
    targets = [(i, "@enabled") for i, s in enumerate(preset.slots) if bypass_ccs and s.model_id]
    targets += [(i, n) for i, s in enumerate(preset.slots) for n in (s.params or {})
                if ranges is None or f"{i}:{n}" in ranges]
    _, amap, rng, _ = assign_ccs(sync_hlx_snapshots(preset.to_hlx_dict()), targets, ranges=ranges)
    _, h = baked_fingerprint(preset, amap, list(amap))
    # Two presets share a capture only if their CC ranges agree too (a value outside a narrowed
    # range keeps the catalog range, T072).
    rh = hashlib.sha1(json.dumps(rng, sort_keys=True).encode()).hexdigest()[:8]
    return chain_id([list(k) for k in chain_key(preset)]) + h + rh


def capture_chain(preset, plugin: str, **kw) -> ChainTemplate:
    """Capture a template for ``preset``'s chain with no human (T018n, macOS VST3).

    Writes the preset as ``.hlx`` with a free MIDI CC assigned to every [0, 1] parameter
    (``native_setlist.assign_ccs``), opens Native's editor in a host process, drops the file
    on it in-process (``native_gui``), closes the editor and saves the state as a
    ``ChainTemplate`` whose map points at the VST3's ``cc_<n>`` params. Raises
    ``UnsupportedModelChange`` with the reason if it cannot (no GUI session, no PyObjC, Native
    asked a question, or the state did not change). Keywords as ``capture_chains``.

    CC ranges come from the HX catalog for continuous params (cab Hz, dB levels included);
    without a catalog only params valued in [0, 1] are mapped, as [0, 1]. With ``verify_ccs``
    the mapped CCs are checked by render (``capture_chains``): only CCs shown to work are
    ``verified``; everything else is baked into the template's fingerprint, so the template is
    reused only for presets with identical baked values.

    With ``bypass_ccs`` every block's bypass (``@enabled``) is also assigned a CC (map key
    ``"<slot>:@enabled"``; >= 0.5 is on). Bypass states are then no longer baked, so one
    capture serves every bypass pattern of the chain.
    """
    out = capture_chains([preset], plugin, **kw)[0]
    if isinstance(out, Exception):
        raise out
    return out


def capture_chains(presets: list, plugin: str, *, plugin_name: str | None = None,
                   start_state: bytes | None = None, templates_dir: Path | None = None,
                   host_factory=None, verify_ccs: bool = True, catalog: dict | None = None,
                   bypass_ccs: bool = True, load_timeout_s: float = 8.0,
                   known_live: bool = True, stats: dict | None = None,
                   verify_mode: str = "auto", truth_min_ccs: int = 8,
                   truth_threshold_db: float = -50.0, replay_every: int = 10,
                   replay_threshold_db: float = -90.0, poll_s: float = 0.1, stable_polls: int = 2,
                   settle_s: float = 0.3, replay_host=None, max_sessions: int = 3,
                   replay_refs: dict | None = None, **_) -> list:
    """Capture several chains in ONE host and ONE editor session (phase 3 speed-up).

    The host opens directly in ``start_state`` (no ``reload_with_state``, which leaks ~1.3 GB),
    the editor opens once and every file is dropped in turn; each load is detected by polling
    the state (about 1 s per file instead of ~31 s per file in its own process, measured
    2026-10-15). CCs are then verified on the same host by ``set_state``:

    - ``verify_mode="per-cc"``: one render per mapped param against a base render (a param
      that moves the render by more than -45 dB is live). Bypass CCs are live by rule.
    - ``"truth"``: the chain is also dropped with every mapped param moved to a CC-grid value
      (``moved_preset``). The template with its CCs moved must null against that drop to
      ``truth_threshold_db``; then every mapped param is live. Two renders and one extra drop
      instead of one render per param. On a miss it falls back to per-CC.
    - ``"auto"`` (default): truth when at least ``truth_min_ccs`` params need checking.

    With ``known_live`` a CC whose (model, param) was verified live in an earlier capture
    (``cache/native/cc_live.json``) is not re-checked. Every ``replay_every``-th template this
    process captures (0: none; counted across calls, so single autocaptures are sampled too) is
    replayed in a FRESH host after the session (``replay_host``, e.g. an engine's own host that
    never opened an editor, else a new one) and must null against its in-session render to
    ``replay_threshold_db``, else it is discarded and reported as failed.
    Returns one ``ChainTemplate`` or ``UnsupportedModelChange`` per preset, in order.
    ``host_factory(plugin, plugin_name)`` must return an object with ``call``/``close`` like
    ``HostProcess`` (tests pass a fake); it is then given the start state by reload.
    ``replay_refs`` (a dict, optional) receives ``template_stem(t) -> (render, host values)`` for
    every replayed template whose fresh-host replay passed: the in-session render of
    ``test_signal(1.0)`` after a 3 s pre-roll, kept so a later fresh host can null against it
    (T048 block library smoke).
    """
    st = stats if stats is not None else {}
    tick = time.perf_counter()

    def lap(name: str) -> None:
        nonlocal tick
        now = time.perf_counter()
        st[name] = round(st.get(name, 0.0) + now - tick, 3)
        tick = now

    def new_host():
        if host_factory is not None:
            h = host_factory(plugin, plugin_name)
            if start_state:
                h.call("reload_with_state", start_state, path=plugin, plugin_name=plugin_name)
            return h
        return HostProcess(plugin, plugin_name, initial_state=start_state) if start_state else \
            HostProcess(plugin, plugin_name)

    live_db = _load_live() if known_live else {}
    bad = invalid_models()
    refused = {i: sorted({s.model_id for s in p.slots if s.model_id in bad}) for i, p in enumerate(presets)}
    refused = {i: m for i, m in refused.items() if m}
    if refused:  # never drop a model Native already refused (its alert would show on screen)
        keep = [i for i in range(len(presets)) if i not in refused]
        sub = capture_chains([presets[i] for i in keep], plugin, **{**_capture_kw(locals()), "stats": stats}) \
            if keep else []
        out = [None] * len(presets)
        for i, t in zip(keep, sub, strict=True):
            out[i] = t
        for i, m in refused.items():
            out[i] = UnsupportedModelChange(f"Native refused {m} before (\"Invalid model\", "
                                            f"{rejected_path().name}); not dropped")
        return out
    docs = [_capture_doc(p, catalog, bypass_ccs) for p in presets]
    modes, truths = [], {}
    for i, (p, d) in enumerate(zip(presets, docs, strict=True)):
        amap, rng = d[2], d[3]
        todo = [r for r in amap if not r.endswith(":@enabled") and not live_db.get(_pair(p, r))]
        m = verify_mode if (verify_ccs and amap) else "none"
        if m == "auto":
            m = "truth" if len(todo) >= truth_min_ccs else "per-cc"
        if m == "truth" and todo:
            mp, mvals = moved_preset(p, amap, rng)
            md = _capture_doc(mp, catalog, bypass_ccs)
            if md[2] == amap:
                truths[i] = (md[4], mvals)
            else:  # the moved file did not get the same CCs (should not happen): check per CC
                m = "per-cc"
        modes.append(m)
    drops: list[tuple[int, str, Path]] = []
    for i, d in enumerate(docs):
        drops.append((i, "base", d[4]))
        if i in truths:
            drops.append((i, "truth", truths[i][0]))
    lap("write_files_s")
    out: list = [None] * len(presets)
    host = new_host() if host_factory is None else host_factory(plugin, plugin_name)
    lap("host_open_s")
    st["chains"], st["rejected"], st["sessions"] = [], [], 0
    replay: list[tuple[int, np.ndarray, dict]] = []
    snaps = cache_dir() / "dialogs"
    snaps.mkdir(parents=True, exist_ok=True)
    try:
        gui = host.call("gui_available")[0]
        if not gui.get("gui"):
            raise UnsupportedModelChange(f"Native autocapture unavailable: {gui.get('reason')}")
        if start_state and host_factory is not None:
            host.call("reload_with_state", start_state, path=plugin, plugin_name=plugin_name)
        lap("gui_check_s")
        got: dict = {}
        pending = list(range(len(drops)))
        while pending:
            # One editor session over the pending files. A rejected load (Native's error or
            # question window, a modal drop, an unchanged state) ends the session at that file
            # (window snapshotted to cache/native/dialogs/ and closed); the files after it are
            # retried in a fresh host, at most max_sessions sessions in all.
            st["sessions"] += 1
            stamp = time.strftime("%Y%m%dT%H%M%S")
            hdr, blob = host.call("editor_drop_many",
                                  paths=[str(drops[j][2].resolve()) for j in pending],
                                  load_timeout_s=load_timeout_s, poll_s=poll_s,
                                  stable_polls=stable_polls, settle_s=settle_s,
                                  dialog_snap_prefix=str(snaps / f"{stamp}_{os.getpid()}"))
            st["editor_open_s"] = hdr.get("open_s")
            st["editor_s"] = round(st.get("editor_s", 0.0) + float(hdr.get("editor_s") or 0.0), 2)
            if hdr.get("error") and not hdr.get("results"):
                raise UnsupportedModelChange(f"Native autocapture failed: {hdr['error']}")
            results, pos = hdr.get("results", []), 0
            for j, res in zip(pending, results, strict=False):
                i, kind, path = drops[j]
                n = int(res.get("state_len", 0))
                state, pos = blob[pos:pos + n], pos + n
                if res.get("error") or not state:
                    why = res.get("error") or "no state"
                    texts = [t for w in res.get("windows") or [] for t in w.get("text") or []]
                    st["rejected"].append({"file": path.name, "preset": getattr(presets[i], "name", ""),
                                           "kind": kind, "error": why, "windows": res.get("windows"),
                                           "models": [s.model_id for s in presets[i].slots if s.model_id]})
                    _record_rejected(presets[i], path.name, texts or [why])
                    hc = any("L6DialogWindow" in str(w.get("cls")) for w in res.get("windows") or [])
                    shown = " (Native asked a question or reported an error)" if res.get("dialogs") else ""
                    got[(i, kind)] = UnsupportedModelChange(
                        f"Native autocapture: Native rejected {path.name}: {why}{shown}" + (
                            " (if this is the HC-mode question, run scripts/native_hc_once.py once "
                            "and tick \"Don't ask me again\")" if hc else ""))
                else:
                    got[(i, kind)] = state
                got[(i, kind, "load_s")] = res.get("load_s")
            rest = pending[len(results):]
            if rest and st["sessions"] < max_sessions:
                host.close()
                host = new_host()
            elif rest:
                for j in rest:
                    i, kind, path = drops[j]
                    got[(i, kind)] = UnsupportedModelChange(
                        f"Native autocapture: {path.name} was not dropped ({max_sessions} sessions "
                        "ended early on rejected files)")
            pending = rest if st["sessions"] < max_sessions else []
        lap("editor_session_s")
        st["drops"] = len(drops)
        for i, (p, (key, cid, amap, rng, _path)) in enumerate(zip(presets, docs, strict=True)):
            state = got[(i, "base")]
            if isinstance(state, Exception):
                out[i] = state
                continue
            vst: dict = {"mode": modes[i]}
            t_v = time.perf_counter()
            pre_s = verify_preroll_s(p)
            verified: list[str] = []
            if modes[i] == "truth" and i in truths:
                tstate = got[(i, "truth")]
                ok, vst["truth_null_db"] = (False, None) if isinstance(tstate, Exception) else \
                    _truth_check(host, plugin, plugin_name, state, tstate, truths[i][1], pre_s,
                                 truth_threshold_db)
                vst["verify_renders"] = 0 if isinstance(tstate, Exception) else 2
                if ok:
                    verified = list(amap)
                    for ref in amap:
                        live_db[_pair(p, ref)] = True
                else:
                    vst["mode"] = "truth->per-cc"
            if modes[i] in ("per-cc", "truth") and not verified:
                pst: dict = {}
                verified = _verify_ccs(host, plugin, plugin_name, state, p, amap, rng,
                                       preroll_s=pre_s, live_db=live_db, stats=pst)
                vst["verify_renders"] = vst.get("verify_renders", 0) + pst.get("verify_renders", 0)
                vst["verify_skipped_known"] = pst.get("verify_skipped_known", 0)
            verify_s = time.perf_counter() - t_v
            baked, h = baked_fingerprint(p, amap, verified)
            tpl = ChainTemplate(key, state, amap, rng, name=getattr(p, "name", "") or cid,
                                verified=verified, baked=baked, baked_hash=h)
            if replay_every and _next_capture_index() % replay_every == 0:
                vals = {prm: _norm(p, rng, ref) if not ref.endswith(":@enabled") else
                        (1.0 if p.slots[int(ref.split(':')[0])].enabled else 0.0)
                        for ref, prm in amap.items()}
                replay.append((i, _render_state(host, state, vals, 3.0), vals))
            tpl.save(templates_dir)
            out[i] = tpl
            st["chains"].append({"name": tpl.name, "blocks": sum(1 for s in p.slots if s.model_id),
                                 "ccs": len(amap), "verified": len(verified),
                                 "load_s": got.get((i, "base", "load_s")),
                                 "verify_s": round(verify_s, 3), **vst})
        lap("verify_and_save_s")
        st["host_mem_mb"] = getattr(host, "mem_mb", None)
    finally:
        host.close()
        lap("close_s")
        if known_live and live_db:
            _save_live(live_db)
    if replay:  # fresh-process replay null for a sample (correctness check, per session)
        h2 = replay_host or new_host()
        try:
            for i, ref_y, vals in replay:
                y = _render_state(h2, out[i].state, vals, 3.0)
                nd = null_db(y, ref_y) if np.any(ref_y) else -300.0
                # A non-finite render (NaN from Native, seen on 5 of 73 chains in one session,
                # 2026-10-16, T072) fails the check: NaN compares False both ways, so such
                # templates were kept with a wrong ``verified`` list and no ref.
                if not np.isfinite(nd) or not (np.all(np.isfinite(y)) and np.all(np.isfinite(ref_y))):
                    nd = 0.0
                rn = st.setdefault("replay_null_db", {})
                rn[out[i].name if out[i].name not in rn else f"{out[i].name} {template_stem(out[i])}"] = round(nd, 1)
                if nd <= replay_threshold_db and replay_refs is not None:
                    replay_refs[template_stem(out[i])] = (ref_y, vals)
                if nd > replay_threshold_db:
                    _discard(out[i], templates_dir)
                    out[i] = UnsupportedModelChange(
                        f"Native autocapture: {out[i].name!r} replayed in a fresh host nulls at "
                        f"{nd:.1f} dB (> {replay_threshold_db} dB); template discarded")
        finally:
            if h2 is not replay_host:
                h2.close()
        lap("replay_check_s")
    return out


def rejected_path() -> Path:
    return cache_dir() / "rejected_drops.json"


def invalid_models() -> set[str]:
    """Models this Native install refused as "Invalid model" when dropped alone (logged by
    earlier captures). The HX Edit 3.82 catalog lists models Native 3.82 cannot load, e.g.
    HD2_CompressorTransientShaper (2026-10-15)."""
    try:
        db = json.loads(rejected_path().read_text())
    except (OSError, ValueError):
        return set()
    return {e["models"][0] for e in db.values() if len(e.get("models") or []) == 1
            and any("Invalid model" in t for t in e.get("message") or [])}


def _record_rejected(preset, fname: str, texts: list[str]) -> None:
    """Log a file Native refused (local cache, for the catalog owners): models and message."""
    with _LIVE_LOCK:
        try:
            db = json.loads(rejected_path().read_text())
        except (OSError, ValueError):
            db = {}
        db[fname] = {"models": [s.model_id for s in preset.slots if s.model_id],
                     "name": getattr(preset, "name", ""), "message": texts,
                     "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        rejected_path().parent.mkdir(parents=True, exist_ok=True)
        rejected_path().write_text(json.dumps(db, indent=1, sort_keys=True))


_CAPTURE_COUNT = [0]
_COUNT_LOCK = threading.Lock()


def _capture_kw(loc: dict) -> dict:
    """capture_chains' keyword arguments from its locals (for the re-call without refused presets)."""
    names = ("plugin_name", "start_state", "templates_dir", "host_factory", "verify_ccs", "catalog",
             "bypass_ccs", "load_timeout_s", "known_live", "verify_mode", "truth_min_ccs",
             "truth_threshold_db", "replay_every", "replay_threshold_db", "poll_s", "stable_polls",
             "settle_s", "replay_host", "max_sessions", "replay_refs")
    return {k: loc[k] for k in names}


def _next_capture_index() -> int:
    """0, 1, 2, ... over every template captured in this process (replay-check sampling)."""
    with _COUNT_LOCK:
        i = _CAPTURE_COUNT[0]
        _CAPTURE_COUNT[0] += 1
        return i


def template_stem(tpl: ChainTemplate) -> str:
    """File stem ``ChainTemplate.save`` uses: ``<chain id>[_<baked hash[:10]>]``."""
    cid = chain_id([list(x) for x in tpl.chain])
    return f"{cid}_{tpl.baked_hash[:10]}" if tpl.baked_hash else cid


def _discard(tpl: ChainTemplate, d: Path | None) -> None:
    d = d or cache_dir() / "templates"
    stem = template_stem(tpl)
    for suf in (".json", ".state"):
        (d / f"{stem}{suf}").unlink(missing_ok=True)


def _render_state(host, state: bytes, values: dict, preroll_s: float, seconds: float = 1.0) -> np.ndarray:
    """Render ``test_signal`` through ``state`` with host ``values`` set (reset render)."""
    host.call("set_state", state)
    if values:
        host.call("set_params", values=values)
    pre = int(preroll_s * SAMPLE_RATE)
    x = np.concatenate([np.zeros(pre, np.float32), test_signal(seconds)])
    _, data = host.call("render", x.astype("<f4").tobytes(), reset=True)
    return np.frombuffer(data, dtype="<f4")[pre:].copy()


def _truth_check(host, plugin, plugin_name, state: bytes, truth_state: bytes, values: dict,
                 preroll_s: float, threshold_db: float) -> tuple[bool, float]:
    """Template + moved CCs against the moved file's own drop capture (same CC values).

    Single blocks null bit-exactly (-258 to -314 dB, 8 C8 blocks, 2026-10-15); the 8-block C8
    chain at -66 dB, its amp's own 1-ulp sensitivity (phase 1). The -50 dB pass mark leaves
    room for that and still catches 49 of C8's 55 params if their CC were ignored (the other
    six move the render by less than -55 dB at these settings)."""
    a = _render_state(host, state, values, preroll_s)
    b = _render_state(host, truth_state, values, preroll_s)
    nd = null_db(b, a) if np.any(b) else (-300.0 if not np.any(a) else 0.0)
    return nd <= threshold_db, round(float(nd), 1)


def capture_chains_parallel(presets: list, plugin: str, sessions: int = 2,
                            stats: dict | None = None, **kw) -> list:
    """``capture_chains`` split over ``sessions`` host processes at once (each its own editor
    session), capped by ``max_hosts_by_memory()``. Results in ``presets`` order."""
    k = max(1, min(int(sessions), len(presets), max_hosts_by_memory()))
    if k == 1:
        return capture_chains(presets, plugin, stats=stats, **kw)
    parts = [list(range(len(presets)))[j::k] for j in range(k)]
    sts: list[dict] = [{} for _ in parts]
    with cf.ThreadPoolExecutor(k) as ex:
        outs = list(ex.map(lambda j: capture_chains([presets[i] for i in parts[j]], plugin,
                                                     stats=sts[j], **kw), range(k)))
    out: list = [None] * len(presets)
    for part, res in zip(parts, outs, strict=True):
        for i, r in zip(part, res, strict=True):
            out[i] = r
    if stats is not None:
        stats["sessions"] = sts
    return out


_LIVE_LOCK = threading.Lock()


def _live_path() -> Path:
    return cache_dir() / "cc_live.json"


def _load_live() -> dict:
    try:
        return json.loads(_live_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_live(db: dict) -> None:
    """Merge ``db``'s live pairs into the file (parallel sessions each save theirs)."""
    with _LIVE_LOCK:
        merged = {**_load_live(), **{k: v for k, v in db.items() if v}}
        _live_path().parent.mkdir(parents=True, exist_ok=True)
        _live_path().write_text(json.dumps(merged, indent=0, sort_keys=True))


def _verify_ccs(host, plugin, plugin_name, state, preset, amap, rng,
                threshold_db: float = -45.0, preroll_s: float = 3.0,
                live_db: dict | None = None, stats: dict | None = None) -> list[str]:
    """Mapped params whose CC moves the render (one render each, against a base render).

    Assigned bypass CCs are live by rule (bit-exact against drop truth, phase 2) and are not
    rendered. ``live_db`` (``"<model>|<param>" -> True``) skips the render for pairs already
    shown live and records new live pairs; a dead result is not recorded (it may depend on
    settings).
    """
    try:
        host.call("set_state", state)  # same HC mode as the host after the drop
    except EngineError:
        host.call("reload_with_state", state, path=plugin, plugin_name=plugin_name)
    pre = int(preroll_s * SAMPLE_RATE)
    x = np.concatenate([np.zeros(pre, np.float32), test_signal(1.0)])

    def render() -> np.ndarray:
        _, data = host.call("render", x.astype("<f4").tobytes(), reset=True)
        return np.frombuffer(data, dtype="<f4")[pre:]

    live_db = {} if live_db is None else live_db
    ok = [ref for ref in amap if ref.endswith(":@enabled") or live_db.get(_pair(preset, ref))]
    todo = [ref for ref in amap if ref not in ok]
    if stats is not None:
        stats["verify_renders"] = len(todo) + 1 if todo else 0
        stats["verify_skipped_known"] = len([r for r in ok if not r.endswith(":@enabled")])
    if not todo:
        return [ref for ref in amap if ref in ok]
    host.call("set_params", values={prm: _norm(preset, rng, ref) for ref, prm in amap.items()})
    base = render()
    for ref in todo:
        prm, v = amap[ref], _norm(preset, rng, ref)
        host.call("set_params", values={prm: 0.25 if v >= 0.5 else 0.75})
        if null_db(render(), base) > threshold_db:
            ok.append(ref)
            live_db[_pair(preset, ref)] = True
        host.call("set_params", values={prm: v})
    return [ref for ref in amap if ref in ok]


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
                 autocapture: bool | None = None, mem_limit_mb: float | None = None,
                 start_state: bytes | None = None):
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
        if start_state is None and not p.startswith("builtin:") and \
                os.environ.get("TONEMATCH_NATIVE_START_STATE", "1") != "0" and start_state_path().is_file():
            # Start in the human-made HX Stomp state (scripts/native_hc_once.py): templates are
            # captured in HX Stomp mode, so loading them needs no HC-mode reload, and the
            # ~1.3 GB a reload_with_state leaks is never spent.
            start_state = start_state_path().read_bytes()
        self.host = (HostProcess(p, plugin_name, block_size, initial_state=start_state) if start_state
                     else HostProcess(p, plugin_name, block_size))
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
        self._latency: dict[tuple | None, int] = {}  # builtin stand-ins: per chain
        self._plugin_latency: int | None = None   # Native: once per engine (latency())
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
        diffs, ok = [], []
        for t in cands:
            if not t.baked_hash:
                diffs.append(f"{t.name!r} has no baked-value fingerprint (captured before the fix)")
                continue
            vals, h = baked_fingerprint(preset, t.map, t.verified)
            if h == t.baked_hash:
                bad = out_of_range(t, preset)
                if not bad:
                    ok.append(t)
                    continue
                diffs.append(f"{t.name!r} cannot reach {bad[:6]} (outside its CC range)")
                continue
            d = sorted(k for k in set(vals) | set(t.baked) if vals.get(k) != t.baked.get(k))
            diffs.append(f"{t.name!r} differs in baked {d[:6]}")
        if ok:  # the finest CC grid wins (narrowed ranges, T072); ties keep the stored order
            return min(ok, key=range_width_key), ""
        return None, "; ".join(diffs[:4])

    CAPTURE_SESSIONS = 2  # parallel editor sessions for capture_missing (measured, phase 3)

    def capture_missing(self, presets: list, sessions: int | None = None, **kw) -> dict[int, Exception]:
        """Capture every preset with no usable template, in one editor session per host.

        Phase 3 (2026-10-15): about 1-1.5 s per chain in one session instead of ~31 s per chain
        in its own process; ``sessions`` host processes capture at once (default
        ``TONEMATCH_NATIVE_CAPTURE_SESSIONS`` or 2, capped by the RAM cap less this engine's
        own host). Presets that would share a template (same structure and same values outside
        the CC map the capture will assign) are captured once. Returns
        ``{index: UnsupportedModelChange}`` for the ones that failed (empty if all succeeded).
        """
        todo: dict[str, int] = {}
        for i, p in enumerate(presets):
            key = chain_key(p)
            if self._match_template(p, key)[0] is None:
                todo.setdefault(capture_key(p, kw.get("catalog"), kw.get("bypass_ccs", True)), i)
        if not todo:
            return {}
        idx = list(todo.values())
        p0 = start_state_path()
        n = sessions or int(os.environ.get("TONEMATCH_NATIVE_CAPTURE_SESSIONS", self.CAPTURE_SESSIONS))
        n = max(1, min(n, len(idx), max_hosts_by_memory() - 1))
        common = {"plugin_name": self.plugin_name,
                  "start_state": p0.read_bytes() if p0.is_file() else None,
                  "templates_dir": self.templates_dir, "host_factory": self._capture_host_factory}
        t0 = time.perf_counter()
        try:
            if n == 1:  # replay-check on this engine's own host (never opened an editor)
                outs = capture_chains([presets[i] for i in idx], self.plugin, replay_host=self.host,
                                      **common, **kw)
            else:
                outs = capture_chains_parallel([presets[i] for i in idx], self.plugin, sessions=n,
                                               **common, **kw)
        finally:
            self._restore_host()
        self.last_capture_s = time.perf_counter() - t0
        errs = {}
        for i, t in zip(idx, outs, strict=True):
            if isinstance(t, Exception):
                errs[i] = t
                continue
            self.templates[t.chain] = t
            self._variants.setdefault(t.chain, []).append(t)
        return errs

    def _restore_host(self) -> None:
        """Put the loaded state and knobs back after the host was borrowed (replay check)."""
        if self._loaded_state is not None:
            self.host.call("set_state", self._loaded_state)
            if self._host_params:
                self.host.call("set_params", values=dict(self._host_params))

    def _autocapture(self, preset) -> ChainTemplate:
        """Capture an unknown chain by in-process drop (T018n); raises UnsupportedModelChange."""
        p = start_state_path()
        t0 = time.perf_counter()
        try:
            tpl = capture_chain(preset, self.plugin, plugin_name=self.plugin_name,
                                start_state=p.read_bytes() if p.is_file() else None,
                                templates_dir=self.templates_dir,
                                host_factory=self._capture_host_factory, replay_host=self.host)
        finally:
            self._restore_host()
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
                    "host_params": len(self._params), "latency_samples": self._plugin_latency if self._plugin_latency is not None
                    else self._latency.get(self.current_chain),
                    "templates": len(self.templates), "calibration_db": self.calibration_db,
                    "reset_mode": self.reset_mode, "host_reset": info.get("reset_strategy"),
                    "host_mem_mb": self.host.mem_mb, "host_mem_limit_mb": self.mem_limit_mb,
                    "host_recycles": self.recycles,
                    "last_reset_via": getattr(self, "last_reset_via", None),
                    # MIDI-CC-mapped params take 128 values over the assignment's Min..Max
                    # (renders equal drop captures bit-exactly only on that grid, 2026-10-15)
                    "param_steps": self._param_steps()},
        )

    def _param_steps(self) -> int | dict | None:
        tpl = self.current_template or next(iter(self.templates.values()), None)
        if tpl is None or not tpl.map:
            return None
        steps = {(int(k.split(":")[0]), k.split(":", 1)[1]): 128 for k, v in tpl.map.items()
                 if str(v).startswith("cc_")}
        if len(steps) == len(tpl.map):
            return 128
        return steps or None

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

    # Native 3.82's own latency: 16 samples, VST3 and AU, every block size, by impulse through
    # an empty chain (2026-10-14). Used when a chain has no bypass CCs to make it a pass-through.
    NATIVE_LATENCY = 16

    def latency(self) -> int:
        """Samples of plugin latency trimmed from every render, measured once with an impulse.

        Native: once per engine, through the loaded chain with every block bypassed by its CC
        (a pass-through), else ``NATIVE_LATENCY``. Until 2026-10-16 it was measured through the
        live chain at whatever settings the first render had: 0 to 30 samples on C8 (the onset
        of amp hum, filter group delay), so two engines trimmed the same render differently
        (-22.8 dB between a pool and a single engine). Builtin stand-ins: once per chain."""
        if not self.plugin.startswith("builtin:"):
            if self._plugin_latency is None:
                self._plugin_latency = self._measure_plugin_latency()
            return self._plugin_latency
        key = self.current_chain
        if key not in self._latency:
            imp = np.zeros(self.preroll + SAMPLE_RATE // 4, np.float32)
            imp[self.preroll] = 0.5  # after the post-reset fade-in, which would swallow a sample-0 impulse
            self._latency[key] = onset_latency(self._raw_render(imp)[self.preroll:])
        return self._latency[key]

    def _measure_plugin_latency(self) -> int:
        tpl = self.current_template
        byp = [prm for ref, prm in (tpl.map if tpl else {}).items() if ref.endswith(":@enabled")]
        if not byp:
            return self.NATIVE_LATENCY
        imp = np.zeros(self.preroll + SAMPLE_RATE // 4, np.float32)
        imp[self.preroll] = 0.5
        self.host.call("set_params", values={prm: 0.0 for prm in byp})
        try:
            lat = onset_latency(self._raw_render(imp)[self.preroll:])
        finally:  # back to the loaded bypass states
            self.host.call("set_params", values={prm: self._host_params.get(prm, 1.0) for prm in byp})
        if not 0 <= lat <= 256:
            print(f"NativeEngine: pass-through latency {lat} looks wrong; using {self.NATIVE_LATENCY}",
                  file=sys.stderr)
            return self.NATIVE_LATENCY
        return lat

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


def max_hosts_by_memory(per_host_gb: float = 2.5, share: float = 0.5) -> int:
    """Native hosts that fit in ``share`` of physical memory (``TONEMATCH_NATIVE_MAX_HOSTS`` overrides).
    2.5 GB per host (measured 1.3-1.75 GB; human 2026-10-16): 6 hosts on 32 GB, the sweep's sweet spot."""
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
        h = HostProcess(plugin, plugin_name, initial_state=state or None)  # HC mode set before audio
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
    """Parallel render service: N NativeEngine hosts rendered from N threads.

    For ``search``: ``render_generation(preset, param_sets, dry)`` renders one CMA-ES generation
    (one chain, many parameter sets) across all hosts, results in ``param_sets`` order;
    ``map_generation(..., fn)`` applies ``fn`` (e.g. the loss) to each render in the worker
    threads and returns only its results. Every host loads the chain once (later loads of the
    same template only move CCs), and the host state reset makes each render independent of
    which host ran it and what it ran before (bit-identical to a single engine, measured
    2026-10-15). For ``benchmark``/``learning``: ``render_batch(jobs)`` takes mixed chains,
    grouped by chain so state loads are minimised. Order of results matches the input.

    Hosts are opened concurrently (~6 s, ~1.4-1.7 GB each in HX Stomp mode), each checked by the
    host canary. ``workers`` defaults to ``DEFAULT_WORKERS`` (``TONEMATCH_NATIVE_POOL``) and is
    always capped by the RAM cap (``max_hosts_by_memory``, ``TONEMATCH_NATIVE_MAX_HOSTS``) and by
    CPUs - 1; each engine keeps its own memory
    watchdog. Engines default to ``autocapture=False``; ``ensure_templates`` captures what is
    missing first (one capture, shared by every host).
    """

    # Pool sweep 2026-10-16 (C8, render + MR-STFT score, 32 GB / 10-core Mac): 2 s clips 3.5 /
    # 6.8 / 10.0 / 12.2 / 13.1 tests/s at 2 / 4 / 6 / 8 / 10 hosts; memory pressure green up to
    # 6, yellow at 8 and 10, swap grew 2-3 GB while 8-10 hosts opened. 6 is the best within safe
    # memory. ``max_hosts_by_memory`` allows 6 on 32 GB (2.5 GB per host, human 2026-10-16).
    DEFAULT_WORKERS = 6

    def __init__(self, workers: int | None = None, **engine_kw):
        cap = max_hosts_by_memory()
        env = os.environ.get("TONEMATCH_NATIVE_POOL")
        n = workers or (int(env) if env else min(self.DEFAULT_WORKERS, max(1, (os.cpu_count() or 2) - 1)))
        n = max(1, min(int(n), cap))
        engine_kw.setdefault("autocapture", False)
        self.last_errors: dict[int, Exception] = {}
        self.last_item_s: list[float] = []  # seconds per item (load + set_params + render [+ fn])
        with cf.ThreadPoolExecutor(n) as ex:
            futs = [ex.submit(NativeEngine, **engine_kw) for _ in range(n)]
            engines, errs = [], []
            for f in futs:
                try:
                    engines.append(f.result())
                except EngineError as e:  # keep the ones that opened; close them if none usable
                    errs.append(e)
        if not engines:
            raise errs[0]
        self.engines: list[NativeEngine] = engines

    @property
    def workers(self) -> int:
        return len(self.engines)

    def mem_mb(self) -> float:
        """Sum of the hosts' physical footprints (MB)."""
        return float(sum(e.host.mem_mb or 0.0 for e in self.engines))

    def stats(self) -> dict:
        """Hosts, footprint, watchdog recycles and canary replacements so far."""
        return {"hosts": self.workers, "mem_mb": round(self.mem_mb()),
                "mem_mb_per_host": [round(e.host.mem_mb or 0.0) for e in self.engines],
                "recycles": sum(e.recycles for e in self.engines),
                "canary_respawns": sum(getattr(e.host, "canary_respawns", 0) for e in self.engines)}

    def use_template(self, template: ChainTemplate) -> None:
        """Make every host use ``template`` for its chain (as search's load_candidate does)."""
        for e in self.engines:
            e.templates[template.chain] = template
            e._variants[template.chain] = [template]
            e.current_chain = e.current_template = None

    def ensure_templates(self, presets: list, **kw) -> dict[int, Exception]:
        """Capture the presets no host can load yet (``NativeEngine.capture_missing`` on the
        first engine) and share the new templates with every host."""
        first = self.engines[0]
        before = {id(t) for v in first._variants.values() for t in v}
        errs = first.capture_missing(presets, **kw)
        new = [t for v in first._variants.values() for t in v if id(t) not in before]
        for e in self.engines[1:]:
            for t in new:
                e.templates[t.chain] = t
                e._variants.setdefault(t.chain, []).append(t)
        return errs

    def _run(self, work: list[list], fn, on_error: str) -> None:
        self.last_errors = {}
        times: list[float] = []

        def worker(eng: NativeEngine, items: list) -> None:
            for i, item in items:
                t0 = time.perf_counter()
                try:
                    fn(eng, i, item)
                except EngineError as e:
                    if on_error == "raise":
                        raise
                    self.last_errors[i] = e
                times.append(time.perf_counter() - t0)

        with cf.ThreadPoolExecutor(len(self.engines)) as ex:
            for f in [ex.submit(worker, e, w) for e, w in zip(self.engines, work, strict=True)]:
                f.result()
        self.last_item_s = times

    def _stripes(self, n: int) -> list[list[int]]:
        return [[i for i in range(n) if i % len(self.engines) == w] for w in range(len(self.engines))]

    def map_generation(self, preset, param_sets: list[dict[ParamRef, float]], dry: Audio, fn,
                       on_error: str = "raise") -> list:
        """``fn(render)`` for ``preset`` with each of ``param_sets`` applied on top, computed in
        the worker threads (e.g. the loss against the target); results in order. Renders are
        not kept, so a large generation needs no more memory than one render per host.

        ``on_error="none"`` returns None for a failed item (e.g. a silent setting) and keeps the
        exception in ``last_errors[index]``; ``"raise"`` re-raises the first failure.
        """
        out: list = [None] * len(param_sets)

        def one(eng: NativeEngine, i: int, ps: dict) -> None:
            eng.load(preset)  # same template as before: CCs only, no state load
            if ps:
                eng.set_params(ps)
            out[i] = fn(eng.render(dry))

        self._run([[(i, param_sets[i]) for i in s] for s in self._stripes(len(param_sets))], one, on_error)
        return out

    def render_generation(self, preset, param_sets: list[dict[ParamRef, float]], dry: Audio,
                          on_error: str = "raise") -> list[Audio | None]:
        """Render ``preset`` with each of ``param_sets`` applied on top; results in order.

        ``on_error`` as ``map_generation``."""
        return self.map_generation(preset, param_sets, dry, lambda y: y, on_error)

    def render_batch(self, jobs: list[RenderJob], on_error: str = "raise") -> list[Audio | None]:
        def key(j):
            try:
                return chain_id([list(x) for x in chain_key(j.preset)]) if j.preset is not None else ""
            except EngineError:
                return ""
        order = sorted(range(len(jobs)), key=lambda i: key(jobs[i]))
        # keep each chain on as few workers as possible: contiguous slices rather than stripes
        size = -(-len(order) // len(self.engines)) if order else 0
        shards = [[(i, jobs[i]) for i in order[w * size:(w + 1) * size]] for w in range(len(self.engines))]
        out: list[Audio | None] = [None] * len(jobs)

        def one(eng: NativeEngine, i: int, j: RenderJob) -> None:
            if j.preset is not None:
                eng.load(j.preset)
            if j.params:
                eng.set_params(j.params)
            out[i] = eng.render(j.dry)

        self._run(shards, one, on_error)
        return out

    def close(self) -> None:
        for e in self.engines:
            e.close()
