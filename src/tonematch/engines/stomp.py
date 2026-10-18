"""HX Stomp render engine (plan Step 4, Stomp mode). Implements contracts/engine-interface.md.

Control: USB interface 0 through the long-lived ``stompd`` helper (tonepush's ``hx-usb`` crate,
built outside the repo, see docs/notes/stompd.md; ``tonematch.engines.stomp_bridge``). Audio: USB reamping
(``tonematch.reamp``): host USB 5/6 -> Input block on USB 5/6 -> Path A -> USB 1/2.

Edit buffer only. ``load()`` and ``set_params()`` never save; nothing writes a flash slot. On open
the engine records the pedal's current preset and backs the edit buffer up to
``cache/stomp/session/``; ``close()`` re-selects the original preset, reads the chain back,
compares it to the one recorded at open, and only if they differ (the buffer had unsaved edits)
writes the backup into the edit buffer and compares again. ``restore_report`` says what happened.

Load modes (``StompEngine(whole_preset=...)`` or ``TONEMATCH_STOMP_WHOLE_PRESET``):
- ``fallback`` (default; lead 2026-10-15, layout changes T092): parameters and bypass are changed in
  place; a model change, or a preset whose block layout differs (a block where the pedal has none,
  an Amp+Cab against a single block, any Path B difference), is one whole write (below) with the
  full readback. ``UnsupportedModelChange`` is raised only when the compiler or the device refuses
  the layout (``LayoutRefused``: the read-back layout is not the one written), or when no
  ``stompd compile`` build is configured (then model changes stay in place, as in ``off``).
- ``off``: the v0 behaviour, in place only; a layout change raises ``UnsupportedModelChange``.
- ``always``: every load is a whole write (proposed default, docs/DECISIONS.md 2026-10-16 T094).
- Amp+Cab blocks are changed with ``set_model_pair``: ``set_model`` alone drops the cab.
- The Input block's "USB 5/6" value is 6 on the Stomp (the Floor menu value 15 is silent).

``render_many`` (contract amendment 2026-10-16): two single-path variants that fit the DSP are
rendered in one pedal pass, the first on Path A (USB 1/2) and the second on Path B (USB 3/4,
topology SAB: Y split and join at HX Edit's 0.5/0.5, Path B's own output). Anything else is
serial (``tonematch.engines.base.serial_render_many``). See :func:`two_path_fit`.

Whole-preset load (T018s): the preset is written as
an ``.hlx`` with the Input on USB 5/6, compiled offline into a device document on top of a backup
of the current edit buffer (``stompd compile``, which keeps the device's own container: section
table, settings, junctions, assignments, snapshots), and written into the EDIT BUFFER with one
whole-document write (op 21). Never a flash write. The compile refuses what it cannot build (a
model change or emptied slot that carries controller assignments, Send/Return blocks, a block
outside the template's rows); that is raised as ``UnsupportedModelChange``.

Whole loads are checked twice, parameter by parameter (T051, ``stomp_readback``): the compiled
document is decoded offline (``stompd decode``) and compared with the .hlx, including the
snapshot controller values the pedal recalls over the block values, before anything is written;
after the write the pedal's chain is read back and compared again. Either mismatch raises
``ReadbackMismatch``. Snapshot controller values in the written .hlx are set to the slot values
first, so a preset whose snapshot section disagrees with its blocks renders its block values.
"""
from __future__ import annotations

import atexit
import datetime as dt
import importlib.util
import json
import logging
import os
import subprocess
import tempfile
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import numpy as np

from .. import reamp
from .base import (
    SAMPLE_RATE,
    Audio,
    Capabilities,
    Cost,
    EngineError,
    ParamRef,
    UnsupportedModelChange,
)
from .stomp_bridge import BridgeError, StompBridge, helper_path, stomp_on_bus
from .stomp_layout import ensure_sabj
from .stomp_readback import ReadbackMismatch, check, snapshot_conflicts, sync_snapshot_controllers

__all__ = ["LayoutRefused", "ReadbackMismatch", "Sentinel", "SentinelDrift", "StompEngine", "compare_backups",
           "compile_document", "decode_document", "merge_two_path", "reamp_hlx_dict", "resolve_sentinel",
           "restore_verdict", "two_path_fit"]

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[3]
SESSION_DIR = ROOT / "cache" / "stomp" / "session"
EPS = 1e-5

# Engines still open when the interpreter exits (a script died between load() and close()):
# close them so the pedal goes back to its original preset. atexit does not run on SIGKILL or a
# hard crash; then the pedal stays on whatever the edit buffer holds (nothing is ever saved).
_LIVE: weakref.WeakSet = weakref.WeakSet()


def _close_live_engines() -> None:
    for eng in list(_LIVE):
        try:
            eng.close()
        except Exception:  # noqa: BLE001, S110 - best effort at interpreter exit
            pass


atexit.register(_close_live_engines)
ENV_WHOLE_PRESET = "TONEMATCH_STOMP_WHOLE_PRESET"   # off | fallback | always
ENV_COMPILER = "TONEMATCH_STOMPD_COMPILE"           # a stompd build with the offline `compile` mode
ENV_DSP_BUDGET = "TONEMATCH_STOMP_DSP_BUDGET"       # catalog-load limit for a two-path pass
WHOLE_MODES = ("off", "fallback", "always")
DEFAULT_WHOLE_MODE = "fallback"     # lead decision 2026-10-15 (T056); "always" is proposed (T094)
# The largest catalog-estimated load (HX Edit's per-model `load`, not device-measured) known to
# render correctly as one two-path pass on this pedal: K0 on Path A + B00's Amp+Cab on Path B,
# 65.8 (cache/stomp/twopath/dual.hlx, -72.9 / -52.6 dB against single renders, T063). HX Edit's
# own 11C is 57.8. Above it a pair renders serially.
TWO_PATH_DSP_BUDGET = 66.0
MAX_BLOCKS = 8               # the HX Stomp's block limit, both paths together
OUT_B_USB34 = 6              # outputB.@output: Path B's own output to USB 3/4 (stompd notes, T063)
PATH_PAIRS = {"A": (1, 2), "B": (3, 4)}


class LayoutRefused(ReadbackMismatch, UnsupportedModelChange):
    """A whole write landed with another layout than the one written (the device re-derived it:
    a block missing or moved, another model, the topology or a junction changed)."""


class SilentPath(EngineError):
    """A path came back silent for a non-silent input."""


class SentinelDrift(EngineError):
    """The session's sentinel render drifted from its reference (or could not be made, or the
    engine could not return to the session's state afterwards). The engine refuses every later
    render; ``close()`` still restores the pedal. ``report`` is the engine's sentinel_report."""

    def __init__(self, msg: str, report: dict):
        super().__init__(msg)
        self.report = report


ENV_SENTINEL = "TONEMATCH_STOMP_SENTINEL"           # on | off | auto (auto: on for benchmark sessions)
ENV_SENTINEL_EVERY = "TONEMATCH_STOMP_SENTINEL_EVERY"


@dataclass
class Sentinel:
    """A fixed reference re-rendered every ``every`` renders (T108, stomp-verify-v1 rule 4).

    ``preset``: the reference setting (None: a copy of the first preset loaded in the session).
    ``clip``: the reference clip (None: 1 s of seeded noise at -24 dBFS RMS). The first check renders
    the reference twice: the floor is their null, the threshold max(floor + margin_db, strictest_db);
    a floor above max_floor_db (a reference that does not repeat) disables the sentinel."""

    every: int = 25
    preset: object | None = None
    clip: np.ndarray | None = None
    margin_db: float = 6.0
    strictest_db: float = -60.0
    max_floor_db: float = -30.0


def sentinel_clip(seconds: float = 1.0, dbfs_rms: float = -24.0, seed: int = 105) -> np.ndarray:
    """The default sentinel clip: seeded white noise, faded in and out (deterministic)."""
    x = np.random.default_rng(seed).standard_normal(int(seconds * SAMPLE_RATE))
    x *= 10 ** (dbfs_rms / 20) / np.sqrt(np.mean(x ** 2))
    fade = int(0.01 * SAMPLE_RATE)
    ramp = np.linspace(0.0, 1.0, fade)
    x[:fade] *= ramp
    x[-fade:] *= ramp[::-1]
    return x.astype(np.float32)


def resolve_sentinel(sentinel, purpose: str | None) -> Sentinel | None:
    """The engine's sentinel from its argument and ``TONEMATCH_STOMP_SENTINEL``. Explicit
    arguments win (a Sentinel, True, False); otherwise ``on``/``off``, or ``auto`` (default): on
    when the session's purpose names a benchmark."""
    if isinstance(sentinel, Sentinel):
        return sentinel
    every = int(os.environ.get(ENV_SENTINEL_EVERY) or Sentinel.every)
    if sentinel is True:
        return Sentinel(every=every)
    if sentinel is False:
        return None
    mode = (os.environ.get(ENV_SENTINEL) or "auto").strip().lower()
    if mode not in ("on", "off", "auto"):
        raise EngineError(f"{ENV_SENTINEL} must be on, off or auto, got {mode!r}")
    if mode == "on" or (mode == "auto" and purpose and "benchmark" in purpose.lower()):
        return Sentinel(every=every)
    return None


DEVICE_REFUSED = "never showed it back"   # stompd's restore error when the device drops a written document


# --------------------------------------------------------------------------- T112: stream-slip guard
#
# About 1 render in 90 (PRE_S 1.0, T108) has a one-sample slip or a 1-6 ms corrupted burst inside
# the clip, with no PortAudio flag. The guard: a pilot path. The preset is written with one bypassed
# block on Path B in the SAB layout (Path B fed by the split, sent to its own output, USB 3/4), so
# USB 3/4 records the dry clip itself, digitally, through the same stream as Path A. Every render
# compares that loopback with the clip it sent (best integer lag, least-squares gain, residual per
# 2.5 ms window) plus a short marker sent after the clip (it reaches the pedal after the last
# returned sample, so it changes nothing returned; it shows a slip in the clip's trailing silence).
# A render whose loopback does not match is taken again, up to SLIP_RETAKES times, then StreamSlip.
# Rules, thresholds and their evidence: docs/DECISIONS.md (T112).

ENV_SLIP_GUARD = "TONEMATCH_STOMP_SLIP_GUARD"       # on (default) | pairs | off
ENV_SLIP_RETAKES = "TONEMATCH_STOMP_SLIP_RETAKES"
SLIP_MODES = ("on", "pairs", "off")
PILOT_MODEL = "HD2_EQSimple3Band"   # bypassed; landed as a built block in every T108 session
PILOT_POS = 11                      # device slot of Path B cell 0
SLIP_RETAKES = 3
SLIP_WIN = 120                      # 2.5 ms
SLIP_LAG_SEARCH = 16
SLIP_FLOOR_REL_DB = -30.0           # windows quieter than this (re the clip's mean) are judged against it
SLIP_THRESHOLD_DB = -60.0           # registered in docs/DECISIONS.md before the T112 pass run
SLIP_MARKER_S = 0.03
SLIP_MARKER_GAP_S = 0.008


class StreamSlip(EngineError):
    """Every take of a render failed the loopback check (``report``: the last verdicts)."""

    def __init__(self, msg: str, report: dict):
        super().__init__(msg)
        self.report = report


def slip_marker(seconds: float = SLIP_MARKER_S, gap_s: float = SLIP_MARKER_GAP_S, dbfs_rms: float = -30.0,
                seed: int = 112) -> np.ndarray:
    """The post-clip marker: a gap, then a short seeded noise burst (deterministic)."""
    x = np.random.default_rng(seed).standard_normal(int(seconds * SAMPLE_RATE))
    x *= 10 ** (dbfs_rms / 20) / np.sqrt(np.mean(x ** 2))
    return np.concatenate([np.zeros(int(gap_s * SAMPLE_RATE)), x]).astype(np.float32)


def slip_check(loop: np.ndarray, sent: np.ndarray, start: int, lag0: int, search: int = SLIP_LAG_SEARCH,
               win: int = SLIP_WIN, floor_rel_db: float = SLIP_FLOOR_REL_DB,
               threshold_db: float = SLIP_THRESHOLD_DB) -> dict:
    """Does the recorded loopback hold exactly what was sent? Pure; unit-tested.

    ``loop``: the loopback channel of the whole capture; ``sent``: what was played from index
    ``start`` of the stream (clip + marker); ``lag0``: the expected round trip. The best integer lag
    in lag0 +- search and the least-squares gain are fitted over the whole of ``sent``; then the
    residual energy of each ``win``-sample window is compared with that window's own energy, or
    with ``floor_rel_db`` below the mean window energy when the window is quieter. ``ok`` when no
    window exceeds ``threshold_db``. A one-sample slip leaves -15 to -40 dB in every window after it;
    an exact loopback leaves the 24-bit quantisation (about -110 dB)."""
    s = np.asarray(sent, np.float64)
    y = np.asarray(loop, np.float64)
    n = len(s)
    es = float(np.dot(s, s))
    out = {"ok": False, "lag": None, "gain_db": None, "worst_db": None, "worst_at_s": None, "bad_windows": None}
    if n == 0 or es <= 0:
        out.update(ok=True, reason="nothing sent")
        return out
    best, best_l = -np.inf, None
    for lag in range(lag0 - search, lag0 + search + 1):
        a = start + lag
        if a < 0 or a + n > len(y):
            continue
        c = float(np.dot(y[a:a + n], s))
        if c > best:
            best, best_l = c, lag
    if best_l is None:
        out["reason"] = "capture too short for the loopback window"
        return out
    seg = y[start + best_l:start + best_l + n]
    g = float(np.dot(seg, s) / es)
    if g <= 0:
        out.update(lag=best_l, reason="no loopback signal (is the pilot path on USB 3/4?)")
        return out
    r = seg - g * s
    k = -(-n // win)
    pad = k * win - n
    er = np.concatenate([r, np.zeros(pad)]).reshape(k, win)
    ex = np.concatenate([g * s, np.zeros(pad)]).reshape(k, win)
    er, ex = np.sum(er ** 2, axis=1), np.sum(ex ** 2, axis=1)
    ref = np.maximum(ex, float(np.mean(ex)) * 10 ** (floor_rel_db / 10))
    rel = 10 * np.log10(np.maximum(er, 1e-30) / np.maximum(ref, 1e-30))
    w = int(np.argmax(rel))
    worst = float(rel[w])
    bad = int(np.sum(rel > threshold_db))
    out.update(ok=worst <= threshold_db, lag=int(best_l), gain_db=round(20 * np.log10(g), 4),
               worst_db=round(worst, 2), worst_at_s=round(w * win / SAMPLE_RATE, 4), bad_windows=bad)
    if not out["ok"]:
        out["reason"] = f"loopback residual {worst:.1f} dB > {threshold_db:.1f} dB at {w * win / SAMPLE_RATE:.3f} s"
    return out


def _pilot_hlx(preset) -> dict:
    """``preset``'s frame with only the pilot block: one bypassed PILOT_MODEL at cell 0."""
    d = preset.to_hlx_dict()
    tone = d["data"]["tone"]
    dsp = tone.setdefault("dsp0", {})
    for k in [k for k in dsp if k.startswith(("block", "cab"))]:
        del dsp[k]
    for sec in ("controller", "footswitch"):
        tone.pop(sec, None)
    dsp["block0"] = {"@model": PILOT_MODEL, "@enabled": False, "@path": 0, "@position": 0,
                     "@no_snapshot_bypass": False}
    for n in range(8):
        sn = tone.get(f"snapshot{n}")
        if isinstance(sn, dict):
            sn["blocks"] = {"dsp0": {"block0": False}}
            sn.pop("controllers", None)
    return d


def pilot_unfit(preset) -> str | None:
    """Why ``preset`` cannot carry the pilot path, or None. Pure."""
    from .stomp_layout import path_b_blocks, topology

    if not hasattr(preset, "to_hlx_dict"):
        return "not a preset"
    d = preset.to_hlx_dict()
    if path_b_blocks(d) or topology(d) not in ("A", ""):
        return "the preset uses Path B"
    main = _main_blocks(preset)
    if len(main) >= MAX_BLOCKS:
        return f"{len(main)} blocks: no cell left for the pilot"
    if any(s.position is None for s in main):
        return "a block without a row position"
    inp = (d["data"]["tone"].get("dsp0") or {}).get("inputA") or {}
    if inp.get("noiseGate"):
        return "the input noise gate is on (the loopback would be gated)"
    return None


def with_pilot(preset):
    """(piloted preset, None) or (None, reason). The piloted preset is ``preset`` on Path A plus the
    bypassed pilot on Path B (SAB, USB 3/4); ``preset``'s own block and cab keys are unchanged."""
    why = pilot_unfit(preset)
    if why:
        return None, why
    from tonematch import preset as P

    return merge_two_path(preset, P.from_dict(_pilot_hlx(preset))), None


def remap_slots(user, target, slot_map: dict[int, tuple[int, bool]]) -> dict[int, tuple[int, bool]]:
    """A slot map of ``target`` (the piloted preset) by ``user``'s slot indices (matched by key)."""
    idx = {s.key: j for j, s in enumerate(target.slots) if s.key}
    out = {}
    for i, s in enumerate(user.slots):
        j = idx.get(s.key)
        if j is not None and j in slot_map:
            out[i] = slot_map[j]
    return out


# --------------------------------------------------------------------------- T111: ambient in-place policy
#
# T108 (stomp-verify-v1): DL4 Echo Platter, Auto-Vol Echo and Glitz rendered differently after an
# in-place change sequence (B whole -> defaults in place -> B in place) than after a whole write of B
# (-9.5 / -16.4, -32.2, -27.3 dB against floors of -42 to -50 dB). A whole write of B repeats at the
# floor. Policy: a parameter change on an enabled delay or reverb block is a whole write.
# TONEMATCH_STOMP_AMBIENT_INPLACE=1 restores in-place changes. Evidence: docs/DECISIONS.md (T111).
ENV_AMBIENT_INPLACE = "TONEMATCH_STOMP_AMBIENT_INPLACE"
AMBIENT = ("delay", "reverb")


def ambient_changes(plan: dict, dev: dict[int, dict]) -> list[str]:
    """The parameters an in-place plan would change on enabled delay/reverb blocks (same model on
    the pedal). Pure."""
    out = []
    for pos, t in sorted(plan.get("targets", {}).items()):
        cur = dev.get(pos) or {}
        if not t.get("enabled") or _category_of(t.get("model")) not in AMBIENT or cur.get("model_id") != t["model"]:
            continue
        meta = _param_meta(cur.get("params") or [])
        for pid, v in t.get("params", {}).items():
            m = meta.get(pid)
            if m is None:
                continue
            nv = _wire_value(v)
            if abs(float(m["value"]) - nv) > EPS * max(1.0, abs(nv)):
                out.append(f"{pos}:{pid}")
    return out


# --------------------------------------------------------------------------- T110: device templates
#
# stompd-compile builds a new slot from the catalog unless it has a device template: the slot the
# device built itself after a `model` command (STOMPD_TEMPLATES). fw 3.80 refused (op 21, "never
# showed it back") the catalog builds of VIC Stutter Edit, L6SPB Inf Sustain (class 24, no trails)
# and NonLinear (two values and three hidden values more). The engine captures a template the first
# time it builds a model (one `model` command and two backups, on the scratch edit buffer, then the
# buffer is put back), and keeps it per firmware in cache/stomp/templates/<fw>/. A model the device
# refuses on the `model` command itself (Moon Nrm + a "WithPan" IR cab: error -21) is recorded and
# raised as ModelRefused without any document write.
TEMPLATE_ROOT = ROOT / "cache" / "stomp" / "templates"
ENV_TEMPLATES = "TONEMATCH_STOMP_TEMPLATES"         # auto (default) | off


class ModelRefused(UnsupportedModelChange):
    """The device refuses the model (or the Amp+Cab pairing) on its own `model` command."""


def template_key(model_id: str, cab_id: str | None) -> str:
    return f"{model_id}|{cab_id or ''}"

LAYOUT_MARKERS = ("no block at device slot", " model ", " cab ", "the file has no block there", "path topology",
                  "the device has no", "attaches before", "whole-preset write")


def layout_mismatches(diff) -> list[str]:
    """The read-back mismatches that mean the layout itself did not land (not a value)."""
    return [m for m in diff.mismatches if any(k in m for k in LAYOUT_MARKERS)]


def supports(exe: Path, mode: str) -> bool:
    """Whether a stompd build has an offline mode (``compile``, ``decode``), by its usage text.

    Checked without running it: a build without the mode ignores its arguments and opens a USB
    session, which must never happen as a side effect of an offline step."""
    try:
        return f"usage: stompd {mode} ".encode() in Path(exe).read_bytes()
    except OSError:
        return False


def _offline_exe(mode: str, exe: Path | None) -> Path:
    exe = exe or (Path(os.environ[ENV_COMPILER]) if os.environ.get(ENV_COMPILER) else helper_path())
    if exe is None:
        raise EngineError(f"no stompd compiler: set {ENV_COMPILER} to a stompd build with `{mode}`")
    if not supports(exe, mode):
        raise EngineError(f"{exe} has no offline `{mode}` mode (it would open USB instead): set {ENV_COMPILER} "
                          "to the stompd-compile build")
    return Path(exe)


def compile_document(hlx: Path, base: Path, out: Path, compiler: Path | None = None,
                     templates: Path | None = None) -> dict:
    """Compile a .hlx onto a base device document with ``stompd compile`` (offline, no USB).

    ``templates``: a directory of device templates (``STOMPD_TEMPLATES``, T110). Returns the
    compiler's report. A refusal raises UnsupportedModelChange (what the compiler cannot build is
    a layout this engine cannot load)."""
    exe = _offline_exe("compile", compiler)
    env = dict(os.environ)
    if templates is not None and Path(templates).is_dir():
        env["STOMPD_TEMPLATES"] = str(templates)
    r = subprocess.run([str(exe), "compile", str(hlx), str(base), str(out)], capture_output=True, text=True,
                       timeout=60, check=False, env=env)
    try:
        msg = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        raise EngineError(f"stompd compile gave no JSON (exit {r.returncode}): {r.stdout[-500:]} {r.stderr[-500:]}") from e
    if not msg.get("ok"):
        raise UnsupportedModelChange(f"stompd compile refused the preset: {msg.get('error')}")
    return msg["report"]


def decode_document(doc: Path, compiler: Path | None = None) -> dict:
    """A device document as the `chain` command shows it, plus its snapshot controller values
    (``stompd decode``, offline, no USB)."""
    exe = _offline_exe("decode", compiler)
    r = subprocess.run([str(exe), "decode", str(doc)], capture_output=True, text=True, timeout=60, check=False)
    try:
        msg = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        raise EngineError(f"stompd decode gave no JSON (exit {r.returncode}): {r.stdout[-500:]} {r.stderr[-500:]}") from e
    if not msg.get("ok"):
        raise EngineError(f"stompd decode failed: {msg.get('error')}")
    return msg["chain"]


def _first_difference(a, b, path: str = "") -> str | None:
    """Where two decoded documents first differ (dicts compared by key, never by order)."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                return f"{path}.{k}: only in {'after' if k not in a else 'backup'}"
            d = _first_difference(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: {len(a)} vs {len(b)} items"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            d = _first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {a!r} vs {b!r}"


def _unordered_assignments(doc):
    """A decoded document with every ``assignments`` list sorted (they are a set, not a sequence)."""
    if isinstance(doc, dict):
        return {k: (sorted(v, key=str) if k == "assignments" and isinstance(v, list) else _unordered_assignments(v))
                for k, v in doc.items()}
    if isinstance(doc, list):
        return [_unordered_assignments(x) for x in doc]
    return doc


def compare_backups(backup: Path, after: Path, decoder=decode_document) -> dict:
    """Judge a restore: is the edit buffer read after the restore the same document as the backup?

    The device may re-encode a buffer after another preset was selected (2459 vs 2456 bytes in the
    20:27Z push session, field-for-field identical once decoded), so the verdict is decoded
    equality (lead ruling "S1 closed", 2026-10-16); the byte comparison stays as a secondary
    signal. Without a decoder (none given, or no stompd compiler configured) the verdict falls
    back to the bytes and says so in ``restore_compare``.

    Returns ``bytes_equal_backup``, ``decoded_equal_backup`` (None when not decoded),
    ``restore_compare`` ("bytes-identical", "decoded" or "bytes") and, when useful,
    ``decoded_difference`` or ``decode_unavailable``."""
    ba, bb = Path(backup).read_bytes(), Path(after).read_bytes()
    out: dict = {"bytes_equal_backup": ba == bb}
    if ba == bb:   # the same bytes decode to the same document
        out.update(decoded_equal_backup=True, restore_compare="bytes-identical")
        return out
    if decoder is None:
        out.update(decoded_equal_backup=None, restore_compare="bytes",
                   decode_unavailable="no decoder configured: judged by bytes")
        return out
    try:
        da, db = decoder(Path(backup)), decoder(Path(after))
    except (EngineError, OSError, subprocess.SubprocessError) as e:
        out.update(decoded_equal_backup=None, restore_compare="bytes",
                   decode_unavailable=f"decode unavailable ({e}): judged by bytes")
        return out
    # The device may also re-order its controller assignments when it re-encodes a buffer (T108:
    # the 16:52Z verify session read the same three CC 110 assignments back in reverse order).
    # They are a set (chain_signature sorts them too), so they are compared in sorted order.
    diff = _first_difference(_unordered_assignments(da), _unordered_assignments(db))
    out.update(decoded_equal_backup=diff is None, restore_compare="decoded")
    if diff is None:
        log.info("restore: edit buffer re-encoded by the device (%d vs %d bytes), decoded identical",
                 len(ba), len(bb))
    else:
        out["decoded_difference"] = diff
    return out


def restore_verdict(report: dict) -> bool:
    """The headline restore verdict from a restore report: ``restore_ok`` when present, otherwise
    (reports written before T097) chain match and byte equality."""
    if "restore_ok" in report:
        return bool(report["restore_ok"])
    return bool(report.get("match", True)) and bool(report.get("bytes_equal_backup"))


def reamp_hlx_dict(preset, input_value: int, sync_snapshots: bool = True) -> dict:
    """The preset as .hlx JSON with Path A's input on the reamp source (USB 5/6).

    With ``sync_snapshots`` (the engine's default) every snapshot controller value is set to the
    block's own value: the pedal recalls those over the block values on load (T051), and the
    engine renders the slot values it was given."""
    d = preset.to_hlx_dict(sync_snapshots=sync_snapshots)
    d["data"]["tone"].setdefault("dsp0", {}).setdefault("inputA", {})["@input"] = int(input_value)
    # T063: a Path B block on a single-path template gets HX Edit's SABJ layout (Y split and join
    # at 0.5/0.5, Path B fed by the split and merged by the join); HX Edit exports are left as they are.
    ensure_sabj(d)
    return d


# .hlx "@" keys that describe layout, not sound; never sent as parameters.
LAYOUT_KEYS = {"@path", "@position", "@type", "@stereo", "@cab", "@enabled", "@model",
               "@no_snapshot_bypass", "@trails", "@bypassvolume"}


def _sound_params(slot) -> dict:
    """A slot's parameters plus the "@" keys that are device parameters (a cab's "@mic")."""
    out = dict(slot.params)
    out.update({k: v for k, v in slot.extra.items() if k.startswith("@") and k not in LAYOUT_KEYS})
    return out


def chain_signature(chain: dict) -> tuple:
    """What the pedal's sound and identity depend on, ignoring how the helper presents it."""
    def vals(ps):
        return tuple(round(float(p["value"]), 6) for p in ps or [])
    slots = tuple((s["pos"], s["kind"], s.get("model"), s.get("paired"), bool(s["enabled"]),
                   s.get("routing"), vals(s.get("params")), vals(s.get("paired_params")))
                  for s in chain["slots"])
    return (chain.get("setlist"), chain.get("index"), chain.get("name"), slots,
            tuple(sorted(chain.get("assignments") or [])))


_CATEGORY_CACHE: dict[str, str] = {}


def _category_of(model_id: str | None) -> str | None:
    """Catalog category of a model id ("delay", "reverb", ...); None when unknown."""
    if not model_id:
        return None
    if not _CATEGORY_CACHE:
        try:
            from tonematch.catalog import load_catalog

            _CATEGORY_CACHE.update({k: v.get("category") for k, v in load_catalog()["models"].items()})
        except Exception:  # noqa: BLE001 - no catalog: fall back on the id
            _CATEGORY_CACHE["_"] = ""
    cat = _CATEGORY_CACHE.get(model_id)
    if cat:
        return cat
    low = model_id.lower()
    if low.startswith(("hd2_amp", "hd2_preamp")):
        return "amp"
    return "delay" if ("delay" in low or "dl4" in low) else "reverb" if "reverb" in low else None


def _block_slots(chain: dict) -> dict[int, dict]:
    return {s["pos"]: s for s in chain["slots"] if s["kind"] == "Block"}


def device_pos(hlx_path: int, position: int) -> int:
    """Device slot index of an .hlx block: Path A row is slots 1..8 (0 is the Input)."""
    if hlx_path not in (0, 1):
        raise UnsupportedModelChange(f"@path {hlx_path} is not on the Stomp's single DSP")
    return 1 + int(position) + 10 * int(hlx_path)


def plan_load(preset, chain: dict, allow_b: tuple = ()) -> dict:
    """Map a Preset onto the pedal's current chain. Pure: no device I/O (unit-tested).

    Returns {"targets": {pos: {"model", "cab", "enabled", "params", "cab_params", "slot", "cab_slot"}},
             "bypass": [pos, ...]}; raises UnsupportedModelChange when the layout differs.
    ``allow_b``: Path B slots the caller knows to be in the layout it wrote whole (the T112 pilot):
    when both the preset and the pedal have exactly those Path B blocks, with the same models, the
    load stays in place.
    """
    by_key = {s.key: (i, s) for i, s in enumerate(preset.slots) if s.key}
    linked = {s.extra.get("@cab") for s in preset.slots if s.extra.get("@cab")}
    targets: dict[int, dict] = {}
    for i, s in enumerate(preset.slots):
        if s.model_id is None or (s.category == "cab" or s.key in linked) and (s.key or "").startswith("cab"):
            continue
        if s.position is None:
            raise UnsupportedModelChange(f"slot {i} ({s.model_id}) has no @position")
        pos = device_pos(int(s.extra.get("@path", 0)), s.position)
        t = {"model": s.model_id, "enabled": bool(s.enabled), "params": _sound_params(s), "slot": i,
             "cab": None, "cab_params": {}, "cab_slot": None}
        link = s.extra.get("@cab")
        if link and link in by_key:
            ci, cs = by_key[link]
            t.update(cab=cs.model_id, cab_params=_sound_params(cs), cab_slot=ci)
        targets[pos] = t
    dev = _block_slots(chain)
    want_b = sorted(p for p in targets if p > 10)
    have_b = sorted(p for p in dev if p > 10)
    pilot_ok = (bool(allow_b) and want_b == have_b == sorted(allow_b)
                and all(dev[p].get("model_id") == targets[p]["model"] for p in want_b))
    if (want_b or have_b) and not pilot_ok:
        # In place never touches the junctions or Path B's own output, and a bypassed Path B still
        # passes dry signal into the join (SABJ): any Path B on either side is a layout change.
        raise UnsupportedModelChange(f"Path B differs or is in use (preset {want_b}, pedal {have_b}): "
                                     "two-path layouts are written whole")
    missing = sorted(set(targets) - set(dev))
    if missing:
        raise UnsupportedModelChange(
            f"the pedal's preset has no block at slot(s) {missing}; adding blocks is not supported in v0. "
            f"Pedal blocks: {sorted(dev)}; preset blocks: {sorted(targets)}")
    for pos, t in targets.items():
        if t["cab"] is None and dev[pos].get("paired") is not None:
            raise UnsupportedModelChange(f"slot {pos} is an Amp+Cab on the pedal but a single block in the preset")
    bypass = sorted(p for p, s in dev.items() if p not in targets and s["enabled"])
    return {"targets": targets, "bypass": bypass}


# --------------------------------------------------------------------------- two-path passes (S1)


def _dsp_budget() -> float:
    try:
        return float(os.environ.get(ENV_DSP_BUDGET, TWO_PATH_DSP_BUDGET))
    except ValueError:
        return TWO_PATH_DSP_BUDGET


def _main_blocks(preset) -> list:
    """Blocks that take a cell on the row (an Amp+Cab's cab rides with its amp)."""
    return [s for s in preset.slots if s.model_id is not None and not (s.key or "").startswith("cab")]


def two_path_fit(a, b, costs: dict | None = None, budget: float | None = None) -> str | None:
    """Why two presets cannot share one pedal pass (Path A + Path B), or None when they can.

    Both must be single-path (no Path B block, topology A), together at most MAX_BLOCKS blocks,
    every block with a known catalog load, and the catalog-estimated load of both at most the
    budget (``TWO_PATH_DSP_BUDGET``, override ``TONEMATCH_STOMP_DSP_BUDGET``). Pure."""
    from tonematch import preset as P

    from .stomp_layout import path_b_blocks, topology

    for tag, pre in (("first", a), ("second", b)):
        if not hasattr(pre, "to_hlx_dict"):
            return f"the {tag} variant is not a preset"
        d = pre.to_hlx_dict()
        if path_b_blocks(d) or topology(d) not in ("A", ""):
            return f"the {tag} variant is not single-path (topology {topology(d)}, Path B {path_b_blocks(d)})"
        if any(s.position is None or not 0 <= int(s.position) < MAX_BLOCKS for s in _main_blocks(pre)):
            return f"the {tag} variant has a block without a row position"
    if getattr(b, "snapshot_controlled", None):
        return "the second variant has snapshot-varying values (Path B runs block values only)"
    n = len(_main_blocks(a)) + len(_main_blocks(b))
    if n > MAX_BLOCKS:
        return f"{n} blocks on two paths (the HX Stomp runs {MAX_BLOCKS})"
    if costs is None:
        try:
            from tonematch.catalog import load_catalog

            costs = P.build_dsp_costs(load_catalog())
        except Exception as e:  # noqa: BLE001 - no catalog: cannot tell, so do not pair
            return f"no catalog for the DSP estimate ({type(e).__name__})"
    total, unknown = 0.0, []
    for pre in (a, b):
        t, u = P.dsp_total(pre, costs)
        total += t
        unknown += u
    if unknown:
        return f"no catalog load for {sorted(set(unknown))}"
    lim = _dsp_budget() if budget is None else budget
    if total > lim:
        return f"estimated load {total:.1f} > {lim:.1f}"
    return None


def merge_two_path(a, b, catalog: dict | None = None):
    """One Preset with ``a`` on Path A and ``b`` on Path B (SAB): Y split before cell 0 and join
    at cell 8 with HX Edit's values, Path B fed by the split (input 0) and sent to its own
    output, USB 3/4. ``a``'s frame (I/O, globals, snapshots) is kept; ``b``'s blocks, cabs and their
    snapshot states are renamed onto free keys (not its controller assignments). Pure."""
    import copy

    from tonematch import preset as P

    from .stomp_layout import HX_EDIT_JOIN, HX_EDIT_SPLIT_Y, JOIN, SAB, SPLIT_Y

    da, db = a.to_hlx_dict(), b.to_hlx_dict()
    tone, tb = da["data"]["tone"], db["data"]["tone"]
    dsp, bdsp = tone.setdefault("dsp0", {}), tb.get("dsp0", {})
    used = set(dsp)
    ren: dict[str, str] = {}

    def fresh(prefix: str) -> str:
        n = 0
        while f"{prefix}{n}" in used:
            n += 1
        used.add(f"{prefix}{n}")
        return f"{prefix}{n}"

    for k, v in bdsp.items():
        if isinstance(v, dict) and "@model" in v and k.startswith(("block", "cab")):
            ren[k] = fresh("cab" if k.startswith("cab") else "block")
    for k, nk in ren.items():
        node = copy.deepcopy(bdsp[k])
        if nk.startswith("block"):
            node["@path"] = 1
            if node.get("@cab") in ren:
                node["@cab"] = ren[node["@cab"]]
        dsp[nk] = node
    for n in range(8):
        sa, sb = tone.get(f"snapshot{n}"), tb.get(f"snapshot{n}")
        if not isinstance(sa, dict):
            continue
        blocks = sa.setdefault("blocks", {}).setdefault("dsp0", {})
        sbb = ((sb or {}).get("blocks") or {}).get("dsp0") or {}
        for k, nk in ren.items():
            if nk.startswith("block"):
                blocks[nk] = bool(sbb.get(k, bdsp[k].get("@enabled", True)))
        blocks["split"] = True
    # b's controller, footswitch and snapshot-controller entries are not carried over: Path B runs
    # its block values (block values win, as every engine load), and a new assignment on Path B
    # is what the compiler refuses on an assigned base. two_path_fit refuses a b whose values vary
    # by snapshot.
    dsp.setdefault("inputB", {})["@input"] = 0
    ob = dsp.setdefault("outputB", {})
    ob["@output"] = OUT_B_USB34
    ob.setdefault("pan", 0.5)
    ob.setdefault("gain", 0.0)
    dsp.setdefault("split", {}).update({"@model": SPLIT_Y, "@position": 0, "@enabled": True, **HX_EDIT_SPLIT_Y})
    dsp.setdefault("join", {}).update({"@model": JOIN, "@position": 8, "@enabled": True, **HX_EDIT_JOIN})
    tone.setdefault("global", {})["@topology0"] = SAB
    da["data"].setdefault("meta", {})["name"] = "tonematch dual"
    return P.from_dict(da, catalog)


# Values past a model's Helix.sym list, by extra index, as the .hlx names them.
CAB_EXTRAS = {0: "@mic"}


def _param_meta(params: list[dict], cab: bool = False) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in params:
        for k in (p.get("sym"), p.get("id")):
            if k:
                out[k] = p
        if "name" in p:
            out.setdefault(p["name"], p)
        if cab and p.get("extra") in CAB_EXTRAS:
            out[CAB_EXTRAS[p["extra"]]] = p
    return out


def to_native(meta: dict, v: float) -> float:
    """Catalog-normalised 0..1 -> the parameter's own units (what the protocol takes)."""
    lo, hi = float(meta["min"]), float(meta["max"])
    x = lo + float(np.clip(v, 0.0, 1.0)) * (hi - lo)
    if meta.get("kind") in ("Enum", "Switch"):
        x = float(round(x))
    return x


def _wire_value(v) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    return float(v)


def measure_decay_s(mono: np.ndarray, start: int, n: int, rel_db: float = -60.0, floor_db: float = 3.0,
                    win_s: float = 0.02) -> float:
    """Seconds after the clip's last sample until the output stays below max(clip peak + rel_db,
    idle floor + floor_db). The floor is the quietest 200 ms of the recorded tail (its end, where
    the tail is dead under the upper bound). Pure; unit-tested."""
    x = np.asarray(mono, np.float64)
    w = max(1, int(win_s * SAMPLE_RATE))
    clip, tail = x[start:start + n], x[start + n:]
    if len(tail) < 2 * w or len(clip) < w:
        return 0.0
    def env(v):
        k = len(v) // w
        return 10 * np.log10(np.maximum(np.mean(v[:k * w].reshape(k, w) ** 2, axis=1), 1e-30))
    peak = float(env(clip).max())
    te = env(tail)
    m = max(1, int(0.2 / win_s))
    floor = float(np.min(np.convolve(te, np.ones(m) / m, mode="valid"))) if len(te) >= m else float(te.min())
    thr = max(peak + rel_db, floor + floor_db)
    above = np.nonzero(te > thr)[0]
    return 0.0 if len(above) == 0 else float((above[-1] + 1) * w / SAMPLE_RATE)


class StompEngine:
    """The HX Stomp over USB. See the module docstring."""

    SILENT_IN_DB = -70.0
    SILENT_OUT_DB = -100.0
    # Render pre-roll. T108 (stomp-verify-v1, 2026-10-17): the USB stream is not sample-stable until
    # about 0.7 s after it starts. With 0.25 s, 10-23 % of deterministic renders had a one-sample slip
    # or a 1-6 ms burst 0.25-0.67 s after the stream start (inside the clip); none of 12 at >= 0.5 s.
    PRE_S = 1.0
    # T108: amps carry state from the render before (sag, supply): a render 0.3 s after the previous
    # one nulled at -39 dB against a rested render; -56 dB at 1 s, -77 dB at 2 s, -94 dB at 3 s
    # (RevvGen Purple). Chains with an enabled amp wait at least this long.
    AMP_PRE_S = float(os.environ.get("TONEMATCH_STOMP_AMP_PRE_S") or 2.0)
    TAIL_PAD_S = 0.05     # extra after the latency so the trimmed clip is complete
    # Measured 2026-10-14 (fw 3.50): a render started straight after load() with a model change
    # nulls against a settled render as well as two settled renders do (-29..-43 dB vs -31 dB),
    # so no settle time is added; the cost of a model change is load()'s own wall time.
    SETTLE_PARAM_S = 0.0
    SETTLE_MODEL_S = 0.0
    REASSERT_INPUT_AFTER_WHOLE = True
    REVERB_FLUSH_S = 4.0
    MAX_FLUSH_S = 8.0
    # Decay-sized waits (T061 item 3). "bound": wait the safe upper bound (pre_roll_s) before every
    # render. "measured": the first render of a chain + delay/reverb settings records its tail for the
    # upper bound and measures where it dies (DECAY_REL_DB below the clip's peak, or DECAY_FLOOR_DB
    # above the idle floor); later renders wait only that long, minus the time already passed since
    # the previous render ended.
    DECAY_REL_DB = -60.0
    DECAY_FLOOR_DB = 3.0
    DECAY_MARGIN_S = 0.25

    def __init__(self, bridge: StompBridge | None = None, calibration: reamp.Calibration | None = None,
                 session_dir: Path | None = None, whole_preset: str | None = None,
                 compiler=None, decoder=None, purpose: str | None = None, sentinel=None,
                 slip_guard: str | None = None):
        """``purpose`` names the session in the shared pedal lock's owner.json (tonematch.pedal_lock).
        ``sentinel``: a :class:`Sentinel`, True, False, or None (``TONEMATCH_STOMP_SENTINEL``;
        auto = on when ``purpose`` names a benchmark). See :meth:`_sentinel_run`.
        ``slip_guard``: on (default), pairs (two-path passes allowed, unguarded) or off
        (``TONEMATCH_STOMP_SLIP_GUARD``); see :func:`slip_check`."""
        mode = whole_preset if whole_preset is not None else (os.environ.get(ENV_WHOLE_PRESET) or DEFAULT_WHOLE_MODE)
        if mode not in WHOLE_MODES:
            raise EngineError(f"whole_preset must be one of {WHOLE_MODES}, got {mode!r}")
        self.whole_preset = mode
        self._compiler = compiler or compile_document
        self._custom_compiler = compiler is not None
        # The compiled document is decoded and checked before it is written (T051). With the real
        # compiler the decoder is the same stompd-compile build; a custom compiler (tests) brings
        # its own decoder or skips this pre-write check (the post-write readback still runs).
        self._decoder = decoder or (decode_document if compiler is None else None)
        self.cal = calibration or reamp.Calibration.load()
        self.bridge = bridge or StompBridge(allow_document_write=mode != "off", purpose=purpose or "StompEngine")
        if mode != "off" and getattr(self.bridge, "allow_document_write", True) is False:
            self.bridge.close()
            raise EngineError("whole_preset needs a bridge opened with allow_document_write=True")
        self._closed = False
        try:
            self.info = self.bridge.call("info")
            self.cal.check_firmware(self.info.get("firmware"))
            self.original = {k: self.info[k] for k in ("setlist", "index", "label", "name")}
            self.original_chain = self.bridge.call("chain")
            d = session_dir or SESSION_DIR
            d.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
            self.backup_path = d / f"{stamp}_editbuffer.bin"
            self.bridge.call("backup", path=str(self.backup_path))
            (d / f"{stamp}_chain.json").write_text(json.dumps(self.original_chain, indent=1))
            self.device = reamp.find_device()
        except Exception:
            self.bridge.close()
            raise
        _LIVE.add(self)
        # A copy: trusted in-place loads (T061) update self.chain in place, and the original must
        # stay what the pedal held at open (close() compares against it).
        self.chain = json.loads(json.dumps(self.original_chain))
        self.preset = None
        self.slot_map: dict[int, tuple[int, bool]] = {}   # preset slot index -> (device pos, is_cab)
        self.model_change_s: list[float] = []   # model commands alone
        self.model_load_s: list[float] = []     # whole load() calls that changed a model
        self.restore_report: dict = {}
        self.policy_warnings: list[str] = []   # last load(): parameters against the Stomp policy
        self.whole_writes: list[dict] = []      # one entry per whole-document load (timings, report, checks)
        self.last_readback: dict = {}           # the last load()'s full-parameter readback (stomp_readback)
        self.keep_documents: Path | None = None  # if set, each whole load's .hlx/base/document is copied here
        self.session_dir = session_dir or SESSION_DIR
        self.last_offset = 0
        self._refs: dict[tuple, np.ndarray] = {}  # (chain key, clip hash) -> first render, for alignment
        self._cost: Cost | None = None
        # T061 item 5: full readback after every N-th parameter-only in-place load (1 = every load);
        # loads in between trust the engine's own copy of the chain, updated from each `set` reply.
        self.readback_every = max(1, int(os.environ.get("TONEMATCH_STOMP_READBACK_EVERY", "1")))
        self._loads_since_readback = 0
        self._chain_trusted = True              # self.chain was just read from the pedal
        self.readbacks_skipped = 0
        # T061 item 3: decay-sized waits.
        self.tail_mode = os.environ.get("TONEMATCH_STOMP_TAIL", "bound")
        if self.tail_mode not in ("bound", "measured"):
            raise EngineError(f"TONEMATCH_STOMP_TAIL must be bound or measured, got {self.tail_mode!r}")
        self.decay_s: dict[tuple, float] = {}   # tail key -> measured seconds to inaudible
        self._flush_after: tuple[float, float] = (0.0, 0.0)   # (seconds needed, monotonic time render ended)
        self.last_wait_s = 0.0
        # S1: render_many bookkeeping (two-path passes and why a pair went serial).
        self.two_path_passes = 0
        self.serial_renders = 0
        self.two_path_skips: list[str] = []
        self.last_paths: dict[str, Audio] = {}
        # T112: the stream-slip guard (pilot loopback on USB 3/4, re-takes).
        gmode = (slip_guard if slip_guard is not None else os.environ.get(ENV_SLIP_GUARD) or "on").strip().lower()
        if gmode not in SLIP_MODES:
            raise EngineError(f"{ENV_SLIP_GUARD} must be one of {SLIP_MODES}, got {gmode!r}")
        self.slip_guard = gmode
        self.slip_retakes = int(os.environ.get(ENV_SLIP_RETAKES) or SLIP_RETAKES)
        self.slip_threshold_db = SLIP_THRESHOLD_DB
        self._pilot_on_pedal = False     # the pedal holds the piloted layout of the loaded preset
        self._target = None              # what the pedal holds (the piloted preset or the preset itself)
        self.pilot_reason: str | None = None
        self._pilot_disabled: str | None = None   # set when the device refused the pilot layout once
        self.keep_takes = False          # diagnostics: keep every take's output and verdict in self.takes
        self.takes: list[dict] = []
        self.slip_stats = {"renders": 0, "guarded": 0, "takes": 0, "retakes": 0, "slips": 0, "failed": 0,
                           "unguarded": 0, "unguarded_why": {}, "pilot_refused": 0, "last": None}
        # T110: device templates for built slots.
        tmode = (os.environ.get(ENV_TEMPLATES) or "auto").strip().lower()
        if tmode not in ("auto", "off"):
            raise EngineError(f"{ENV_TEMPLATES} must be auto or off, got {tmode!r}")
        self.template_mode = tmode
        self.templates_captured: list[str] = []
        # T111: whole writes for in-place changes on delay/reverb blocks.
        self.ambient_inplace = os.environ.get(ENV_AMBIENT_INPLACE, "") == "1"
        self.ambient_whole = 0
        # T108: the session sentinel (rule 4 of stomp-verify-v1).
        self.sentinel = resolve_sentinel(sentinel, purpose)
        self._first_preset = None
        self._sentinel_ref: np.ndarray | None = None
        self._sentinel_busy = False
        self.sentinel_report: dict = {
            "enabled": self.sentinel is not None, "every": self.sentinel.every if self.sentinel else None,
            "checks": 0, "nulls_db": [], "floor_db": None, "threshold_db": None, "tripped": False,
            "reason": None, "disabled_reason": None, "renders_since": 0, "seconds": 0.0}

    # --- availability ---------------------------------------------------------------------

    @staticmethod
    def is_available() -> bool:
        """True when the stompd helper exists, an HX Stomp is on USB, and sounddevice imports.

        Claims nothing (HX Edit may hold the control interface; opening the engine then fails)."""
        return (helper_path() is not None and stomp_on_bus()
                and importlib.util.find_spec("sounddevice") is not None)

    # --- interface ------------------------------------------------------------------------

    def load(self, preset) -> None:
        t_start = time.perf_counter()
        # Per-engine parameter policy: reported, never silently rewritten (tonematch.param_policy).
        from tonematch import param_policy

        self.policy_warnings = param_policy.violations(preset, "stomp")
        if self._first_preset is None and not self._sentinel_busy:
            import copy

            self._first_preset = copy.deepcopy(preset)   # the sentinel's default reference
        target, why = self._pilot_target(preset)
        self.pilot_reason = why
        if target is not preset:
            try:
                self._load_target(target, t_start, allow_b=(PILOT_POS,) if self._pilot_on_pedal else ())
            except LayoutRefused as e:
                # The pedal would not take the pilot layout: load the preset alone, unguarded.
                log.warning("stomp: the pilot layout was refused (%s); loading without the slip guard", e)
                self.slip_stats["pilot_refused"] += 1
                self._pilot_on_pedal = False
                self.pilot_reason = f"pilot layout refused: {e}"
                self._pilot_disabled = self.pilot_reason   # not tried again in this session
                target = preset
            else:
                self._pilot_on_pedal = True
                self._target = target
                self.slot_map = remap_slots(preset, target, self.slot_map)
                self.preset = preset
                return
        self._pilot_on_pedal = False
        self._load_target(preset, t_start, allow_b=())
        self._target = preset

    def _pilot_target(self, preset):
        """(what to write, why no pilot): the piloted preset when the guard is on and it fits."""
        if self.slip_guard == "off":
            return preset, "slip guard off"
        if self._pilot_disabled:
            return preset, self._pilot_disabled
        if self.whole_preset == "off" or not self._can_compile():
            return preset, "no whole-preset writes (the pilot is a layout change)"
        piloted, why = with_pilot(preset)
        return (piloted, None) if piloted is not None else (preset, why)

    def _load_target(self, preset, t_start: float, allow_b: tuple = ()) -> None:
        fast = self.readback_every > 1 and self._chain_trusted and self.whole_preset != "always"
        if not fast:
            self._refresh()
        if self.whole_preset == "always":
            return self._load_whole(preset, t_start)
        try:
            plan = plan_load(preset, self.chain, allow_b=allow_b)
        except UnsupportedModelChange as e:
            if self.whole_preset == "fallback":
                if not self._can_compile():
                    raise UnsupportedModelChange(f"{e} (a whole write needs {ENV_COMPILER}: a stompd build "
                                                 "with `compile`)") from e
                return self._load_whole(preset, t_start)
            raise
        dev = _block_slots(self.chain)
        if self.whole_preset == "fallback" and self._can_compile() and any(
                dev[pos].get("model_id") != t["model"] or (t["cab"] and dev[pos].get("paired_id") != t["cab"])
                for pos, t in plan["targets"].items()):
            # A `model` command resets the block to its catalog defaults before the values are set
            # (2026-10-15: the live delay read back exactly the DL4 defaults). In fallback mode a
            # model change is one whole write instead (0.46 s vs 0.66 s in place, T018s).
            return self._load_whole(preset, t_start)
        if (self.whole_preset == "fallback" and not self.ambient_inplace and self._can_compile()
                and ambient_changes(plan, dev)):
            # T111: an in-place change on a delay or reverb block is a whole write (see AMBIENT).
            self.ambient_whole += 1
            return self._load_whole(preset, t_start)
        changed_model = False
        for pos, t in sorted(plan["targets"].items()):
            cur = dev[pos]
            want_cab = t["cab"]
            if cur.get("model_id") != t["model"] or (want_cab and cur.get("paired_id") != want_cab):
                t0 = time.perf_counter()
                if want_cab:
                    self.bridge.call("model", pos=pos, model=t["model"], cab=want_cab)
                else:
                    self.bridge.call("model", pos=pos, model=t["model"])
                self.model_change_s.append(time.perf_counter() - t0)
                changed_model = True
        if changed_model:
            time.sleep(self.SETTLE_MODEL_S)
            self._refresh()
            dev = _block_slots(self.chain)
        for pos, t in sorted(plan["targets"].items()):
            cur = dev[pos]
            if cur.get("model_id") != t["model"]:
                raise EngineError(f"slot {pos}: model is {cur.get('model_id')} after load, wanted {t['model']}")
            self._apply_values(pos, cur.get("params", []), t["params"], paired=False, model=cur.get("model"))
            if t["cab"]:
                self._apply_values(pos, cur.get("paired_params", []), t["cab_params"], paired=True,
                                   model=cur.get("paired"))
            if bool(cur["enabled"]) != t["enabled"]:
                self.bridge.call("enable", pos=pos, on=t["enabled"])
                cur["enabled"] = t["enabled"]
        for pos in plan["bypass"]:
            self.bridge.call("enable", pos=pos, on=False)
            dev[pos]["enabled"] = False
        self._route_input()
        time.sleep(self.SETTLE_PARAM_S)
        if changed_model:
            self.model_load_s.append(time.perf_counter() - t_start)
        self._loads_since_readback += 1
        self.preset = None
        if changed_model or self._loads_since_readback >= self.readback_every:
            # Same full-parameter readback as a whole load (T051); the slots follow the Stomp layout.
            self._refresh()
            self.last_readback = check(self.chain, reamp_hlx_dict(preset, self.cal.input_usb56), None,
                                       "the pedal's read-back chain").as_dict()
            self._loads_since_readback = 0
        else:
            # T061 item 5: trusted in-place load; the cached chain carries every value just set.
            self.readbacks_skipped += 1
            self.last_readback = {"ok": None, "skipped": True, "next_in": self.readback_every - self._loads_since_readback}
        self.preset = preset
        self.slot_map = {}
        for pos, t in plan["targets"].items():
            self.slot_map[t["slot"]] = (pos, False)
            if t["cab_slot"] is not None:
                self.slot_map[t["cab_slot"]] = (pos, True)

    def _load_whole(self, preset, t_start: float) -> None:
        """Compile the preset onto a backup of the current edit buffer and write it whole (op 21,
        edit buffer only). See the module docstring."""
        self._ensure_templates(preset)
        before = _block_slots(self.chain)
        hlx_dict = reamp_hlx_dict(preset, self.cal.input_usb56, sync_snapshots=False)
        synced = snapshot_conflicts(hlx_dict)
        sync_snapshot_controllers(hlx_dict)
        checks: dict = {"snapshot_sync": synced}
        with tempfile.TemporaryDirectory(dir=self.session_dir) as td:
            tdp = Path(td)
            hlx, base, out = tdp / "candidate.hlx", tdp / "base.bin", tdp / "candidate.bin"
            from tonematch.preset import dumps_hlx

            hlx.write_text(dumps_hlx(hlx_dict), encoding="utf-8")
            t0 = time.perf_counter()
            self.bridge.call("backup", path=str(base))
            t1 = time.perf_counter()
            if self._custom_compiler:
                report = self._compiler(hlx, base, out)
            else:
                report = self._compiler(hlx, base, out, templates=self._template_dir())
            t2 = time.perf_counter()
            if self._decoder is not None:
                # Offline, before anything is written: every block value and every snapshot
                # controller value of the compiled document against the .hlx.
                checks["document"] = check(self._decoder(out), hlx_dict, report, "the compiled document").as_dict()
            try:
                self.bridge.call("restore", path=str(out), timeout_s=30)
            except BridgeError as e:
                if DEVICE_REFUSED not in str(e):
                    raise
                # T108: the device took the op-21 write and never showed the document (VIC Stutter
                # Edit, 2026-10-17). The model or layout cannot be loaded: a refusal the caller can
                # skip, not a dead session. What the buffer holds now is read back.
                self.preset = None
                try:
                    self._refresh()
                except (BridgeError, EngineError):
                    pass
                raise self._refused(f"the device refused the whole-preset write: {e}") from e
            t3 = time.perf_counter()
            if self.keep_documents is not None:  # evidence for spikes: what was compiled and written
                import shutil

                self.keep_documents.mkdir(parents=True, exist_ok=True)
                n = len(self.whole_writes)
                for f in (hlx, base, out):
                    shutil.copy2(f, self.keep_documents / f"{n:02d}_{f.name}")
        self._refresh()
        dev = _block_slots(self.chain)
        key_to_pos = {s_["key"]: int(s_["slot"]) for s_ in report.get("slots", []) if s_.get("key")}
        self.slot_map = {}
        changed_model = False
        for i, sl in enumerate(preset.slots):
            if sl.model_id is None or sl.key not in key_to_pos:
                continue
            pos = key_to_pos[sl.key]
            cur = dev.get(pos)
            if cur is None or cur.get("model_id") != sl.model_id:
                self.preset = None
                raise self._refused(f"whole-preset write: slot {pos} holds {cur and cur.get('model_id')}, "
                                    f"wanted {sl.model_id}")
            prev = before.get(pos)
            if prev is None or prev.get("model_id") != sl.model_id:
                changed_model = True
            self.slot_map[i] = (pos, False)
            link = sl.extra.get("@cab")
            if link:
                ci = next((j for j, c in enumerate(preset.slots) if c.key == link), None)
                if ci is not None:
                    if cur.get("paired_id") != preset.slots[ci].model_id:
                        self.preset = None
                        raise self._refused(f"whole-preset write: slot {pos} cab is {cur.get('paired_id')}, "
                                            f"wanted {preset.slots[ci].model_id}")
                    if prev is None or prev.get("paired_id") != preset.slots[ci].model_id:
                        changed_model = True
                    self.slot_map[ci] = (pos, True)
        # T018s: re-sending route_key after a whole write is NOT needed for audio. The --diagnose
        # run (2026-10-14T23:44Z) had sound straight after the write without it (k1 = -23.27 dB,
        # same as the control). The earlier "silence" was Terminal lacking Microphone permission.
        # Kept because it is harmless: one op-42 command, and k4 after the re-send was identical.
        self._route_input(force=self.REASSERT_INPUT_AFTER_WHOLE)
        if changed_model or set(before) != set(dev):
            self.model_load_s.append(time.perf_counter() - t_start)
        entry = {"backup_s": t1 - t0, "compile_s": t2 - t1, "write_s": t3 - t2,
                 "load_s": time.perf_counter() - t_start, "report": report, "checks": checks}
        self.whole_writes.append(entry)
        # Full-parameter readback of what the pedal now runs (T051): every parameter of every
        # block, enabled, models and the input routing against the .hlx. The input is checked
        # after _route_input, so the chain is read again first.
        self._refresh()
        try:
            checks["readback"] = check(self.chain, hlx_dict, report, "the pedal's read-back chain").as_dict()
        except ReadbackMismatch as e:
            checks["readback"] = e.diff.as_dict()
            self.preset = None   # never render or tweak a preset that did not land
            lay = layout_mismatches(e.diff)
            if lay:
                raise LayoutRefused(f"the device did not take the layout as written: {'; '.join(lay[:4])}",
                                    e.diff) from e
            raise
        self.last_readback = checks["readback"]
        self.preset = preset

    @staticmethod
    def _refused(msg: str) -> LayoutRefused:
        from .stomp_readback import ReadbackDiff

        return LayoutRefused(msg, ReadbackDiff(mismatches=[msg], checked=1))

    def _can_compile(self) -> bool:
        if self._custom_compiler:
            return True
        try:
            _offline_exe("compile", None)
        except EngineError:
            return False
        return True

    # --- device templates (T110) -------------------------------------------------------------

    def _template_dir(self) -> Path:
        return TEMPLATE_ROOT / str(self.info.get("firmware") or "unknown")

    def _template_index(self) -> dict:
        p = self._template_dir() / "index.json"
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            d = {}
        d.setdefault("models", {})
        d.setdefault("refused", {})
        return d

    def _save_template_index(self, d: dict) -> None:
        p = self._template_dir() / "index.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=1, sort_keys=True))

    def needed_templates(self, preset) -> list[tuple[str, str | None]]:
        """(model, cab) of every mono block a whole write of ``preset`` would build (a model or cab
        other than the pedal's at that slot) that has no device template yet."""
        idx = self._template_index()
        by_key = {s.key: s for s in preset.slots if s.key}
        dev = _block_slots(self.chain)
        out = []
        for s in _main_blocks(preset):
            if s.position is None or s.extra.get("@stereo"):
                continue   # a stereo block resolves to another model number: catalog build as before
            link = s.extra.get("@cab")
            cab = by_key[link].model_id if link in by_key else None
            pos = device_pos(int(s.extra.get("@path", 0)), s.position)
            cur = dev.get(pos) or {}
            if cur.get("model_id") == s.model_id and cur.get("paired_id") == cab:
                continue
            k = template_key(s.model_id, cab)
            if k in idx["refused"]:
                raise ModelRefused(f"the device refuses {s.model_id}" + (f" with {cab}" if cab else "")
                                   + f" on its own model command ({idx['refused'][k]})")
            ent = idx["models"].get(k)
            if ent and (self._template_dir() / ent["file"]).exists():
                continue
            if (s.model_id, cab) not in out:
                out.append((s.model_id, cab))
        return out

    def _ensure_templates(self, preset) -> None:
        if self.template_mode == "off" or self._custom_compiler:
            return
        need = self.needed_templates(preset)
        if need:
            self.capture_templates(need)

    def capture_templates(self, models: list[tuple[str, str | None]]) -> dict:
        """Let the device build each (model, cab) itself: back the edit buffer up, issue the `model`
        command on a Path A block slot, back the buffer up again as the template, and finally write
        the first backup back (the device's own document, same session) and check the chain
        signature. A model the device refuses is recorded and raised as ModelRefused."""
        d = self._template_dir()
        d.mkdir(parents=True, exist_ok=True)
        idx = self._template_index()
        self._refresh()
        before = chain_signature(self.chain)
        blocks = sorted(p for p in _block_slots(self.chain) if p < 10)
        pos = blocks[0] if blocks else 1
        keep = Path(self.session_dir) / f"template_keep_{os.getpid()}.bin"
        self.bridge.call("backup", path=str(keep))
        refused, got = [], {}
        try:
            for model, cab in models:
                k = template_key(model, cab)
                args = {"pos": pos, "model": model}
                if cab:
                    args["cab"] = cab
                try:
                    self.bridge.call("model", **args)
                except BridgeError as e:
                    if "refused" not in str(e):
                        raise
                    idx["refused"][k] = str(e)
                    refused.append(k)
                    continue
                ch = self.bridge.call("chain")
                sl = _block_slots(ch).get(pos) or {}
                num, cnum = sl.get("model"), sl.get("paired")
                if sl.get("model_id") != model or num is None:
                    raise EngineError(f"template capture: slot {pos} holds {sl.get('model_id')} after the "
                                      f"model command for {model}")
                name = f"m{int(num)}" + (f"_c{int(cnum)}" if cab and cnum is not None else "") + ".bin"
                self.bridge.call("backup", path=str(d / name))
                idx["models"][k] = {"file": name, "number": int(num), "cab_number": None if cnum is None else int(cnum),
                                    "captured_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
                got[k] = name
                self.templates_captured.append(k)
        finally:
            self._save_template_index(idx)
            self.bridge.call("restore", path=str(keep), timeout_s=30)
            self._refresh()
            keep.unlink(missing_ok=True)
            if chain_signature(self.chain) != before:
                raise EngineError("template capture: the edit buffer did not return to its state")
        if refused:
            raise ModelRefused(f"the device refuses {refused} on its own model command")
        return got

    def set_params(self, changes: dict[ParamRef, float]) -> None:
        if not changes:
            return
        if self.preset is None:
            raise EngineError("set_params before load()")
        dev = _block_slots(self.chain)
        if not self.ambient_inplace and self.whole_preset == "fallback" and self._can_compile():
            amb = []
            for (slot, _name) in changes:
                pos_cab = self.slot_map.get(slot)
                d = dev.get(pos_cab[0]) if pos_cab else None
                if d and not pos_cab[1] and d.get("enabled") and _category_of(d.get("model_id")) in AMBIENT:
                    amb.append(slot)
            if amb:
                # T111: through load(), which writes a delay/reverb change whole.
                self.load(self._with_changes(self.preset, changes))
                return
        for (slot, name), v in changes.items():
            if slot not in self.slot_map:
                raise EngineError(f"slot {slot} is not on the pedal (not loaded or bypass-only)")
            pos, is_cab = self.slot_map[slot]
            meta = _param_meta(dev[pos].get("paired_params" if is_cab else "params", []), cab=is_cab).get(name)
            if meta is None or "min" not in meta:
                raise EngineError(f"slot {slot}: no parameter {name!r} with a catalog range")
            model = dev[pos].get("paired" if is_cab else "model")
            extra = {"model": int(model)} if model is not None else {}
            r = self.bridge.call("set", pos=pos, param=meta["i"], value=to_native(meta, v), paired=is_cab, **extra)
            if r.get("native") is not None:
                meta["value"] = float(r["native"])   # keep the cached chain current (T061 item 5)
        time.sleep(self.SETTLE_PARAM_S)

    def render(self, dry: Audio) -> Audio:
        self._sentinel_gate()
        y = self._render_paths(dry, ("A",))["A"]
        self._sentinel_tick()
        return y

    # --- sentinel (T108) ------------------------------------------------------------------------

    def _sentinel_gate(self) -> None:
        if self.sentinel_report.get("tripped"):
            raise SentinelDrift(f"the session sentinel tripped earlier ({self.sentinel_report.get('reason')}): "
                                "no further renders in this session", self.sentinel_report)

    def _sentinel_tick(self) -> None:
        """Count a session render; run the sentinel on the first one and then every N."""
        if self.sentinel is None or self._sentinel_busy or not self.sentinel_report["enabled"]:
            return
        rep = self.sentinel_report
        rep["renders_since"] += 1
        if self._sentinel_ref is None or rep["renders_since"] >= self.sentinel.every:
            self._sentinel_run()

    def sentinel_check_now(self) -> float | None:
        """Run a sentinel check at once (scripts; the forced-drift test). Returns its null."""
        if self.sentinel is None or not self.sentinel_report["enabled"]:
            return None
        self._sentinel_gate()
        self._sentinel_run()
        return self.sentinel_report["nulls_db"][-1] if self.sentinel_report["nulls_db"] else None

    def _sentinel_run(self) -> None:
        """Back up the edit buffer, load the reference, render the reference clip, compare it with
        the reference's first render, write the backup back (same session) and check the chain
        signature. The first run renders twice and sets the floor and threshold. Raises
        SentinelDrift on a drift, a broken audio path, or a failed return to the session's state."""
        cfg, rep = self.sentinel, self.sentinel_report
        ref_preset = cfg.preset if cfg.preset is not None else self._first_preset
        if ref_preset is None:
            return   # nothing loaded yet: no reference
        clip = cfg.clip if cfg.clip is not None else sentinel_clip()
        t0 = time.perf_counter()
        self._sentinel_busy = True
        keep = {k: getattr(self, k) for k in ("preset", "slot_map", "last_readback", "last_offset", "last_paths",
                                             "policy_warnings", "_loads_since_readback", "_chain_trusted",
                                             "_pilot_on_pedal", "_target", "pilot_reason")}
        trip = None
        backup = None
        try:
            self._refresh()
            before = chain_signature(self.chain)
            backup = Path(self.session_dir) / f"sentinel_{rep['checks']:03d}_{os.getpid()}.bin"
            self.bridge.call("backup", path=str(backup))
            try:
                self.load(ref_preset)
                ys = [self._render_paths(clip, ("A",))["A"] for _ in range(2 if self._sentinel_ref is None else 1)]
            except (SilentPath, reamp.ReampError, BridgeError) as e:
                trip = f"reference render failed: {type(e).__name__}: {e}"
            except EngineError as e:
                if self._sentinel_ref is None:
                    rep.update(enabled=False, disabled_reason=f"reference not loadable: {type(e).__name__}: {e}")
                else:
                    trip = f"reference load failed: {type(e).__name__}: {e}"
            else:
                if self._sentinel_ref is None:
                    floor = reamp.null_db(ys[0], ys[1])
                    rep["floor_db"] = round(float(floor), 2)
                    if floor > cfg.max_floor_db:
                        rep.update(enabled=False, disabled_reason=f"reference does not repeat: floor {floor:.1f} dB "
                                                                  f"> {cfg.max_floor_db} dB")
                    else:
                        self._sentinel_ref = ys[0]
                        rep["threshold_db"] = round(float(max(floor + cfg.margin_db, cfg.strictest_db)), 2)
                else:
                    null = float(reamp.null_db(self._sentinel_ref, ys[0]))
                    rep["nulls_db"].append(round(null, 2))
                    rep["offsets"] = rep.get("offsets", []) + [int(self.last_offset)]
                    rep["checks"] += 1
                    if null > rep["threshold_db"]:
                        trip = f"sentinel null {null:.1f} dB > threshold {rep['threshold_db']:.1f} dB"
                        # Evidence for the diagnosis: the reference and the render that drifted.
                        ev = Path(self.session_dir) / f"sentinel_trip_{os.getpid()}"
                        try:
                            ev.mkdir(parents=True, exist_ok=True)
                            np.save(ev / "reference.npy", self._sentinel_ref)
                            np.save(ev / "drifted.npy", ys[0])
                            rep["evidence"] = str(ev)
                        except OSError:
                            pass
            # Back to the session's state, whatever happened above.
            self.bridge.call("restore", path=str(backup), timeout_s=30)
            self._refresh()
            if chain_signature(self.chain) != before:
                trip = trip or "the edit buffer did not return to the session's state after the sentinel"
        except (BridgeError, EngineError, OSError) as e:
            trip = trip or f"sentinel failed: {type(e).__name__}: {e}"
        finally:
            for k, v in keep.items():
                setattr(self, k, v)
            self._chain_trusted = trip is None
            self._sentinel_busy = False
            rep["renders_since"] = 0
            rep["seconds"] = round(rep["seconds"] + time.perf_counter() - t0, 3)
            if backup is not None:
                backup.unlink(missing_ok=True)
        if trip:
            rep.update(tripped=True, reason=trip)
            log.error("stomp sentinel: %s", trip)
            raise SentinelDrift(trip, rep)

    def _render_paths(self, dry: Audio, paths: tuple[str, ...]) -> dict[str, Audio]:
        """One pedal pass; Path A from USB 1/2 and, when asked, Path B from USB 3/4. Each path is
        trimmed, aligned against that path's own chain (so a two-path pass and a serial render of
        the same blocks share the jitter reference) and level-matched with the one calibration.

        T112: with the pilot on the pedal (a single-path render), USB 3/4 carries the loopback; a take
        that fails :func:`slip_check` is taken again, up to ``slip_retakes`` times, then StreamSlip."""
        x = np.asarray(dry, dtype=np.float32)
        if x.ndim != 1:
            raise EngineError(f"render() takes mono (n,) float32, got shape {x.shape}")
        if len(x) == 0:
            return {p: x.copy() for p in paths}
        st = self.slip_stats
        st["renders"] += 1
        guarded = paths == ("A",) and self._pilot_on_pedal and self.slip_guard != "off"
        if not guarded:
            why = ("two-path pass (USB 3/4 is Path B)" if paths != ("A",) else
                   self.pilot_reason or "the pedal does not hold the pilot layout")
            st["unguarded"] += 1
            st["unguarded_why"][why] = st["unguarded_why"].get(why, 0) + 1
            out, _, commit = self._render_once(x, paths, guarded=False)
            commit()
            return out
        st["guarded"] += 1
        verdicts = []
        for k in range(self.slip_retakes + 1):
            out, v, commit = self._render_once(x, paths, guarded=True)
            st["takes"] += 1
            v["take"] = k
            verdicts.append(v)
            if self.keep_takes:
                self.takes.append({"render": st["renders"], "take": k, "y": out["A"], "verdict": v,
                                   "offset": self.last_offset})
            st["last"] = v
            if v["ok"]:
                commit()
                return out
            if str(v.get("reason", "")).startswith("no loopback"):
                raise EngineError(f"the slip guard's loopback is silent: {v['reason']}")
            st["slips"] += 1
            if k < self.slip_retakes:
                st["retakes"] += 1
                log.warning("stomp: stream slip (%s); taking the render again (%d/%d)", v.get("reason"), k + 1,
                            self.slip_retakes)
        st["failed"] += 1
        raise StreamSlip(f"{len(verdicts)} takes failed the loopback check (last: {verdicts[-1].get('reason')})",
                         {"verdicts": verdicts})

    def _render_once(self, x: np.ndarray, paths: tuple[str, ...], guarded: bool):
        """One take: (outputs, slip verdict or {}, commit). ``commit()`` stores a first render as the
        path's alignment reference; it is called only for a take that is kept."""
        tail = self.cal.latency_samples / SAMPLE_RATE + self.TAIL_PAD_S
        key = self._tail_key()
        bound = self._bound_s()
        probe = self.tail_mode == "measured" and bound > self._min_wait_s() and key not in self.decay_s
        if probe:
            tail += bound   # record this chain's own tail once and measure where it dies
        wait = self._wait_s()
        self.last_wait_s = wait
        # The marker would sit in a tail recorded to measure its decay: not on probe renders.
        marker = slip_marker() if guarded and not probe else np.zeros(0, np.float32)
        sig = np.concatenate([x, marker]) if len(marker) else x
        if paths == ("A",) and not guarded:
            rec, pre = reamp.playrec(sig, self.device, pre_s=wait, tail_s=tail)
            recs = {"A": rec}
        else:
            rec, pre = reamp.playrec(sig, self.device, pre_s=wait, tail_s=tail, in_pair=None)
            if rec.shape[1] < 4:
                raise EngineError(f"a two-path pass or the slip guard needs USB inputs 1-4; the device gave "
                                  f"{rec.shape[1]}")
            recs = {p: rec[:, PATH_PAIRS[p][0] - 1:PATH_PAIRS[p][1]] for p in paths}
        ended = time.monotonic()
        verdict: dict = {}
        if guarded:
            loop = reamp.to_mono(rec[:, PATH_PAIRS["B"][0] - 1:PATH_PAIRS["B"][1]], self.cal.mono)
            verdict = slip_check(loop, sig, pre, self.cal.latency_samples, threshold_db=self.slip_threshold_db)
        monos = {p: reamp.to_mono(r, self.cal.mono) for p, r in recs.items()}
        if probe:
            self.decay_s[key] = max(measure_decay_s(m, pre + self.cal.latency_samples, len(x), self.DECAY_REL_DB,
                                                    self.DECAY_FLOOR_DB) for m in monos.values())
            self._flush_after = (0.0, ended)   # the recorded tail already played out
        else:
            need = self.decay_s.get(key, bound) + self.DECAY_MARGIN_S if self.tail_mode == "measured" else bound
            self._flush_after = (need if bound > self._min_wait_s() else 0.0, ended)
        j = reamp.MAX_JITTER
        out: dict[str, Audio] = {}
        offsets = {}
        pending: dict = {}
        for p in paths:
            y_ext = reamp.trim(monos[p], pre, self.cal.latency_samples - j, len(x) + 2 * j, 0)
            # Cancel the 0..1 sample stream-start jitter against this path's first render of this clip.
            rk = (self._path_key(p), hash(x.tobytes()))
            ref = self._refs.get(rk)
            if ref is None:
                y, offsets[p] = y_ext[j:j + len(x)].copy(), 0
                pending[rk] = y
            else:
                y, offsets[p] = reamp.align(y_ext, ref, len(x), j)
            y = (y * np.float32(10 ** (self.cal.level_gain_db / 20))).astype(np.float32)
            if not np.all(np.isfinite(y)):
                raise EngineError("the Stomp capture contains NaN or inf")
            if reamp.rms_db(x) > self.SILENT_IN_DB and reamp.rms_db(y) < self.SILENT_OUT_DB:
                raise SilentPath(f"the Stomp returned silence on Path {p} for a non-silent input "
                                 "(Input block not on USB 5/6?)")
            out[p] = y
        self.last_offset = offsets[paths[0]]
        if verdict:
            verdict["offset"] = self.last_offset

        def commit() -> None:
            for rk, y in pending.items():
                self._refs.setdefault(rk, y)

        return out, verdict, commit

    def _path_key(self, path: str) -> tuple:
        """The blocks of one path, at the slots a single-path load puts them (Path B -10)."""
        if path == "A":
            return tuple(k for k in self._chain_key() if k[0] < 10)
        return tuple((k[0] - 10,) + tuple(k[1:]) for k in self._chain_key() if k[0] > 10)

    # --- render_many (contract amendment 2026-10-16) -------------------------------------------

    def render_many(self, dry: Audio, variants: list) -> list[Audio]:
        """Render ``dry`` through every variant, in order. Consecutive variants that
        :func:`two_path_fit` accepts are rendered two per pedal pass (Path A and Path B); the rest
        serially. A dict variant is relative to the variant before it (the loaded preset for the
        first), as the serial fallback has it. A pair the compiler or the device refuses as one
        pass (or that comes back silent on a path) is rendered serially and recorded in
        ``two_path_skips``; an error of a variant on its own is raised. The loaded preset afterwards
        is unspecified (``load`` again)."""
        from .base import serial_render_many

        variants = list(variants)
        if not variants:
            return []
        resolved = self._resolve_variants(variants)
        if not self._dual_possible():
            self.two_path_skips.append("no document write or compiler: serial")
            self.serial_renders += len(variants)
            return serial_render_many(self, dry, resolved)
        if self.slip_guard == "on":
            # T112: a two-path pass has no loopback (USB 3/4 is Path B), so it cannot be checked for
            # stream slips; with the guard on every variant renders on its own, guarded.
            self.two_path_skips.append("slip guard on: serial (a two-path pass has no loopback)")
            self.serial_renders += len(variants)
            return serial_render_many(self, dry, resolved)
        out: list[Audio] = []
        i = 0
        while i < len(resolved):
            why = two_path_fit(resolved[i], resolved[i + 1]) if i + 1 < len(resolved) else "unpaired"
            if why is None:
                try:
                    out.extend(self._render_pair(dry, resolved[i], resolved[i + 1]))
                    i += 2
                    continue
                except (UnsupportedModelChange, ReadbackMismatch, SilentPath) as e:
                    why = f"pair refused as one pass: {type(e).__name__}: {e}"
            if why != "unpaired":
                self.two_path_skips.append(f"variants {i},{i + 1}: {why}")
            self.load(resolved[i])
            out.append(self.render(dry))
            self.serial_renders += 1
            i += 1
        return out

    def _dual_possible(self) -> bool:
        return (self.whole_preset != "off" and getattr(self.bridge, "allow_document_write", True) is not False
                and self._can_compile())

    def _render_pair(self, dry: Audio, a, b) -> list[Audio]:
        self._sentinel_gate()
        merged = merge_two_path(a, b)
        self._pilot_on_pedal = False
        self._target = None
        self._load_whole(merged, time.perf_counter())
        ys = self._render_paths(dry, ("A", "B"))
        self.last_paths = ys
        self.two_path_passes += 1
        self._sentinel_tick()
        return [ys["A"], ys["B"]]

    def _resolve_variants(self, variants: list) -> list:
        """Every variant as a whole Preset: dicts applied (in the catalog's 0..1, as set_params)
        to the preset before them."""
        out = []
        base = self.preset
        for k, v in enumerate(variants):
            if isinstance(v, dict):
                if base is None:
                    raise EngineError(f"variant {k}: parameter changes before any preset (load() first)")
                base = self._with_changes(base, v)
            else:
                base = v
            out.append(base)
        return out

    def _with_changes(self, preset, changes: dict) -> object:
        """A copy of ``preset`` with ``changes`` (ParamRef -> 0..1) in native units: the pedal's
        own range when the slot is loaded with that model, else the catalog's."""
        import copy

        p = copy.deepcopy(preset)
        dev = _block_slots(self.chain)
        cat_models = None
        for (slot, name), v in changes.items():
            if not 0 <= slot < len(p.slots) or p.slots[slot].model_id is None:
                raise EngineError(f"slot {slot} is not in the preset")
            sl = p.slots[slot]
            meta = None
            if preset is self.preset and slot in self.slot_map:
                pos, is_cab = self.slot_map[slot]
                d = dev.get(pos, {})
                if (d.get("paired_id") if is_cab else d.get("model_id")) == sl.model_id:
                    meta = _param_meta(d.get("paired_params" if is_cab else "params", []), cab=is_cab).get(name)
            if meta is None or "min" not in meta:
                if cat_models is None:
                    from tonematch.catalog import load_catalog

                    cat_models = load_catalog()["models"]
                cp = (cat_models.get(sl.model_id) or {}).get("params", {}).get(name)
                if not cp or cp.get("min") is None:
                    raise EngineError(f"slot {slot}: no parameter {name!r} with a catalog range")
                kind = {"bool": "Switch", "stepped": "Enum"}.get(cp.get("kind"), "Continuous")
                meta = {"min": float(cp["min"]), "max": float(cp["max"]), "kind": kind}
            sl.params[name] = to_native(meta, v)
        return p

    def _tail_key(self) -> tuple:
        """The chain plus every enabled delay/reverb block's values: what the tail depends on."""
        vals = []
        for pos, sl in sorted(_block_slots(self.chain).items()):
            if sl.get("enabled") and _category_of(sl.get("model_id")) in ("delay", "reverb"):
                vals.append((pos, tuple(round(float(p["value"]), 4) for p in sl.get("params", []))))
        return (self._chain_key(), tuple(vals))

    def _bound_s(self) -> float:
        return self.pre_roll_s()

    def _wait_s(self) -> float:
        """Silence before the clip. "bound": the upper bound for the current chain (as before).
        "measured": the previous render's measured decay (or its bound), less the time since it
        ended; never below PRE_S."""
        if self.tail_mode == "bound":
            return self.pre_roll_s()
        need, ended = self._flush_after
        left = need - (time.monotonic() - ended) if ended else need
        return float(max(self._min_wait_s(), min(left, self.MAX_FLUSH_S)))

    def _min_wait_s(self) -> float:
        """The shortest pre-roll for the current chain: PRE_S, or AMP_PRE_S with an enabled amp."""
        amp = any(sl.get("enabled") and _category_of(sl.get("model_id")) == "amp"
                  for sl in _block_slots(self.chain).values())
        return float(max(self.PRE_S, self.AMP_PRE_S) if amp else self.PRE_S)

    def pre_roll_s(self) -> float:
        """Silence before the clip, long enough for the previous render's delay or reverb tail to
        die away (hardware 2026-10-15: a DL4 Ping Pong chain, Time 0.375 s, Feedback 0.40, rendered
        with a 0.25 s pre-roll gave renders of the same document 8.5 dB apart across loads and
        repeats at -28 dB; with 3 s, -32..-36 dB, the chain's repeat floor). Delays: time to -60 dB
        from Time and Feedback (Time 2 s when tempo-synced: worst case); reverbs: REVERB_FLUSH_S."""
        need = self._min_wait_s()
        for sl in _block_slots(self.chain).values():
            if not sl.get("enabled"):
                continue
            cat = _category_of(sl.get("model_id"))
            if cat == "reverb":
                need = max(need, self.REVERB_FLUSH_S)
            elif cat == "delay":
                ps = {(p.get("sym") or p.get("id")): float(p["value"]) for p in sl.get("params", [])
                      if p.get("sym") or p.get("id")}
                t = 2.0 if ps.get("TempoSync1", 0.0) >= 0.5 else ps.get("Time", 2.0)
                fb = min(max(ps.get("Feedback", 0.5), 1e-3), 0.95)
                need = max(need, t * (np.log(1e-3) / np.log(fb)) + 0.5)
        return float(min(need, self.MAX_FLUSH_S))

    def cost(self) -> Cost:
        if self._cost is None:
            clip = reamp.tone(2.0, 220.0, -18.0)
            times = []
            for _ in range(2):
                t0 = time.perf_counter()
                self.render(clip)
                times.append(time.perf_counter() - t0)
            if not self.model_load_s and self.preset is not None:
                self.measure_load_penalty()
            penalty = float(np.median(self.model_load_s)) if self.model_load_s else float("nan")
            self._cost = Cost(seconds_per_render=float(np.median(times)), parallelism=1, load_penalty_s=penalty)
        return self._cost

    def measure_load_penalty(self) -> float:
        """Swap the first loaded block to another model of its category, then time load() of the
        current preset (which puts the model back). Edit buffer only; leaves the preset loaded."""
        if self.preset is None or not self.slot_map:
            raise EngineError("measure_load_penalty needs a loaded preset")
        from tonematch.catalog import load_catalog

        pos = min(p for p, cab in self.slot_map.values() if not cab)
        cur = _block_slots(self.chain)[pos]
        models = load_catalog()["models"]
        cat = models.get(cur.get("model_id"), {}).get("category")
        other = next((m["id"] for m in models.values() if m.get("stomp") and m.get("category") == cat
                      and m["id"] != cur.get("model_id") and not m.get("stereo")), None)
        if other is None:
            raise EngineError(f"no other {cat} model to measure a model change with")
        self.bridge.call("model", pos=pos, model=other)
        self.load(self.preset)
        return self.model_load_s[-1]

    def stats(self) -> dict:
        """Session counters: the slip guard (renders, takes, re-takes, slips, unguarded and why),
        device templates captured, ambient whole writes (T111), two-path passes and the sentinel."""
        st = dict(self.slip_stats)
        st["unguarded_why"] = dict(st["unguarded_why"])
        return {"slip_guard": {"mode": self.slip_guard, "retakes_max": self.slip_retakes,
                               "threshold_db": self.slip_threshold_db, **st},
                "templates_captured": list(self.templates_captured), "ambient_whole": self.ambient_whole,
                "two_path_passes": self.two_path_passes, "serial_renders": self.serial_renders,
                "whole_writes": len(self.whole_writes),
                "sentinel": {k: self.sentinel_report.get(k) for k in ("enabled", "checks", "tripped", "reason")}}

    def capabilities(self) -> Capabilities:
        models: tuple[str, ...] = ()
        try:
            from tonematch.catalog import load_catalog

            cat = load_catalog()
            models = tuple(sorted(m["id"] for m in cat["models"].values() if m.get("stomp")))
        except (ImportError, RuntimeError, OSError, ValueError, KeyError):
            models = ()
        return Capabilities(
            engine="stomp", firmware_version=str(self.info.get("firmware") or ""), models=models, snapshots=3,
            extras={"control_route": "usb", "param_steps": None, "build": self.info.get("build"),
                    "whole_preset": self.whole_preset,
                    "render_many": "serial (slip guard on)" if self.slip_guard == "on" else "two-path SAB pairs, else serial",
                    "slip_guard": self.slip_guard, "templates": self.template_mode,
                    "ambient_inplace": self.ambient_inplace,
                    "latency_samples": self.cal.latency_samples, "level_gain_db": self.cal.level_gain_db,
                    "input_usb56": self.cal.input_usb56, "original_preset": self.original,
                    "helper": str(helper_path())})

    def close(self, verify_bytes: bool = False) -> None:
        """Restore the original preset and end the session. With ``verify_bytes`` the edit buffer
        is backed up again in the SAME session and compared with the backup taken at open, so no
        second session is needed (T061 item 1).

        The verdict is ``restore_report["restore_ok"]`` = chain match and ``decoded_equal_backup``
        (both buffers decoded and deep-compared, T097); ``bytes_equal_backup`` is kept as a
        secondary signal, and is the verdict when no decoder is available (``restore_compare``
        says which). Without ``verify_bytes``, ``restore_ok`` is the chain match alone
        (``restore_compare`` "chain-only")."""
        if self._closed:
            return
        self._closed = True
        try:
            self.restore_report = rr = self._restore()
            match = bool(rr.get("match"))
            rr.update(restore_ok=match, restore_compare="chain-only")
            if verify_bytes:
                try:
                    after = Path(self.backup_path).with_name(Path(self.backup_path).stem + "_after_restore.bin")
                    self.bridge.call("backup", path=str(after))
                    rr["after_backup"] = str(after)
                    rr.update(compare_backups(Path(self.backup_path), after, self._decoder))
                    same = (rr["decoded_equal_backup"] if rr["decoded_equal_backup"] is not None
                            else rr["bytes_equal_backup"])
                    rr["restore_ok"] = match and bool(same)
                except (BridgeError, OSError) as e:
                    rr.update(bytes_equal_backup=False, decoded_equal_backup=None, restore_compare="failed",
                              restore_ok=False, verify_error=str(e))
        finally:
            self.bridge.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    # --- internals ------------------------------------------------------------------------

    def _chain_key(self) -> tuple:
        return tuple((s["pos"], s.get("model_id"), s.get("paired_id"))
                     for s in self.chain["slots"] if s["kind"] == "Block")

    def _refresh(self) -> None:
        self.chain = self.bridge.call("chain")
        self._chain_trusted = True

    def _route_input(self, force: bool = False) -> None:
        inp = next((s for s in self.chain["slots"] if s["kind"] == "Input" and s["pos"] == 0), None)
        if inp is None:
            raise EngineError("the pedal's preset has no Path A input")
        if force or inp.get("routing") != self.cal.input_usb56:
            self.bridge.call("route_key", pos=0, key=51, to=self.cal.input_usb56)
            inp["routing"] = self.cal.input_usb56

    def _apply_values(self, pos: int, dev_params: list[dict], want: dict, paired: bool,
                      model: int | None = None) -> None:
        meta = _param_meta(dev_params, cab=paired)
        for pid, v in want.items():
            m = meta.get(pid)
            if m is None:
                continue  # parameter the device does not expose under this id (kept in the .hlx only)
            nv = _wire_value(v)
            if abs(float(m["value"]) - nv) <= EPS * max(1.0, abs(nv)):
                continue
            args = {"pos": pos, "param": m["i"], "value": nv, "paired": paired}
            if model is not None:
                args["model"] = int(model)   # lets the helper skip a preset read per set (T061)
            if "min" not in m:  # no catalog entry: say what the device stores
                args["wire"] = "bool" if isinstance(v, bool) else "int" if isinstance(v, int) else "float"
            r = self.bridge.call("set", **args)
            m["value"] = float(r.get("native", nv)) if isinstance(r, dict) else nv

    def _same_as_original(self) -> tuple[bool, list[str]]:
        now, was = chain_signature(self.bridge.call("chain")), chain_signature(self.original_chain)
        names = ("setlist", "index", "name", "slots", "assignments (any order)")
        diffs = [f"{n} differ" for n, x, y in zip(names, was, now, strict=True) if x != y]
        if was[3] != now[3]:   # say which slots, and how (diagnosis)
            diffs += [f"slot {a[0]}: was {a} now {b}" for a, b in zip(was[3], now[3], strict=False) if a != b][:4]
        return not diffs, diffs

    def _restore(self) -> dict:
        """Put the pedal back: re-select the original preset; if it still differs, select the
        neighbouring preset and come back (visible on the pedal's screen, no flash write); if it
        still differs, the buffer was dirty at open, so write the edit-buffer backup (op 21, the
        whole-document write fretwire associates with every lockup on record) and compare again."""
        report: dict = {"original": self.original, "steps": []}
        try:
            o = self.original
            self.bridge.call("select", setlist=o["setlist"], index=o["index"], timeout_s=20)
            report["steps"].append("select original")
            time.sleep(0.5)
            ok, diffs = self._same_as_original()
            report["first_diffs"] = diffs
            if not ok:
                # Re-selecting the loaded preset may not reload it; step away and back (no flash write).
                other = o["index"] + 1 if o["index"] + 1 < int(self.bridge.ready.get("presets", 126)) else o["index"] - 1
                self.bridge.call("select", setlist=o["setlist"], index=other, timeout_s=20)
                self.bridge.call("select", setlist=o["setlist"], index=o["index"], timeout_s=20)
                report["steps"].append(f"select {other} then original")
                time.sleep(0.5)
                ok, diffs = self._same_as_original()
                report["second_diffs"] = diffs
            if not ok:
                # The buffer had unsaved edits at open: write the backup into the edit buffer.
                self.bridge.call("restore", path=str(self.backup_path), timeout_s=30)
                report["steps"].append("restore edit-buffer backup")
                time.sleep(0.5)
                ok, diffs = self._same_as_original()
            report.update(match=ok, diffs=diffs)
        except (BridgeError, EngineError) as e:
            report.update(match=False, error=str(e))
        return report
