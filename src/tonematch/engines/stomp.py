"""HX Stomp render engine (plan Step 4, Stomp mode). Implements contracts/engine-interface.md.

Control: USB interface 0 through the long-lived ``stompd`` helper (tonepush's ``hx-usb`` crate,
built outside the repo, see docs/notes/stompd.md; ``tonematch.engines.stomp_bridge``). Audio: USB reamping
(``tonematch.reamp``): host USB 5/6 -> Input block on USB 5/6 -> Path A -> USB 1/2.

Edit buffer only. ``load()`` and ``set_params()`` never save; nothing writes a flash slot. On open
the engine records the pedal's current preset and backs the edit buffer up to
``cache/stomp/session/``; ``close()`` re-selects the original preset, reads the chain back,
compares it to the one recorded at open, and only if they differ (the buffer had unsaved edits)
writes the backup into the edit buffer and compares again. ``restore_report`` says what happened.

v0 limits (measured on firmware 3.50, docs/notes/T003-stomp-control-spike.md):
- ``load()`` changes models, parameters and bypass on the blocks the pedal's current preset
  already has. It does not add, remove or move blocks: a preset whose block layout differs from
  the pedal's raises ``UnsupportedModelChange``. Load a base preset with the right layout first
  (by hand, or with a template the human imports once).
- Amp+Cab blocks are changed with ``set_model_pair``: ``set_model`` alone drops the cab.
- The Input block's "USB 5/6" value is 6 on the Stomp (the Floor menu value 15 is silent).
- One render at a time (``parallelism=1``); Path A/B parallel renders are untested.

Whole-preset load (T018s, behind a flag: ``StompEngine(whole_preset=...)`` or
``TONEMATCH_STOMP_WHOLE_PRESET=fallback|always``): when the in-place plan raises
``UnsupportedModelChange`` (``fallback``), or for every load (``always``), the preset is written as
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
import os
import subprocess
import tempfile
import time
import weakref
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
from .stomp_readback import ReadbackMismatch, check, snapshot_conflicts, sync_snapshot_controllers

__all__ = ["ReadbackMismatch", "StompEngine", "compile_document", "decode_document", "reamp_hlx_dict"]

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
WHOLE_MODES = ("off", "fallback", "always")


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


def compile_document(hlx: Path, base: Path, out: Path, compiler: Path | None = None) -> dict:
    """Compile a .hlx onto a base device document with ``stompd compile`` (offline, no USB).

    Returns the compiler's report. A refusal raises UnsupportedModelChange (what the compiler
    cannot build is a layout this engine cannot load)."""
    exe = _offline_exe("compile", compiler)
    r = subprocess.run([str(exe), "compile", str(hlx), str(base), str(out)], capture_output=True, text=True,
                       timeout=60, check=False)
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


def reamp_hlx_dict(preset, input_value: int, sync_snapshots: bool = True) -> dict:
    """The preset as .hlx JSON with Path A's input on the reamp source (USB 5/6).

    With ``sync_snapshots`` (the engine's default) every snapshot controller value is set to the
    block's own value: the pedal recalls those over the block values on load (T051), and the
    engine renders the slot values it was given."""
    d = preset.to_hlx_dict(sync_snapshots=sync_snapshots)
    d["data"]["tone"].setdefault("dsp0", {}).setdefault("inputA", {})["@input"] = int(input_value)
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
    return "delay" if ("delay" in low or "dl4" in low) else "reverb" if "reverb" in low else None


def _block_slots(chain: dict) -> dict[int, dict]:
    return {s["pos"]: s for s in chain["slots"] if s["kind"] == "Block"}


def device_pos(hlx_path: int, position: int) -> int:
    """Device slot index of an .hlx block: Path A row is slots 1..8 (0 is the Input)."""
    if hlx_path not in (0, 1):
        raise UnsupportedModelChange(f"@path {hlx_path} is not on the Stomp's single DSP")
    return 1 + int(position) + 10 * int(hlx_path)


def plan_load(preset, chain: dict) -> dict:
    """Map a Preset onto the pedal's current chain. Pure: no device I/O (unit-tested).

    Returns {"targets": {pos: {"model", "cab", "enabled", "params", "cab_params", "slot", "cab_slot"}},
             "bypass": [pos, ...]}; raises UnsupportedModelChange when the layout differs.
    """
    by_key = {s.key: (i, s) for i, s in enumerate(preset.slots) if s.key}
    targets: dict[int, dict] = {}
    for i, s in enumerate(preset.slots):
        if s.model_id is None or s.category == "cab" and (s.key or "").startswith("cab"):
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
    PRE_S = 0.25          # render pre-roll; the stream start is not sample-stable before this
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
                 compiler=None, decoder=None):
        mode = whole_preset if whole_preset is not None else os.environ.get(ENV_WHOLE_PRESET, "off")
        if mode not in WHOLE_MODES:
            raise EngineError(f"whole_preset must be one of {WHOLE_MODES}, got {mode!r}")
        self.whole_preset = mode
        self._compiler = compiler or compile_document
        # The compiled document is decoded and checked before it is written (T051). With the real
        # compiler the decoder is the same stompd-compile build; a custom compiler (tests) brings
        # its own decoder or skips this pre-write check (the post-write readback still runs).
        self._decoder = decoder or (decode_document if compiler is None else None)
        self.cal = calibration or reamp.Calibration.load()
        self.bridge = bridge or StompBridge(allow_document_write=mode != "off")
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
        fast = self.readback_every > 1 and self._chain_trusted and self.whole_preset != "always"
        if not fast:
            self._refresh()
        if self.whole_preset == "always":
            return self._load_whole(preset, t_start)
        try:
            plan = plan_load(preset, self.chain)
        except UnsupportedModelChange:
            if self.whole_preset == "fallback":
                return self._load_whole(preset, t_start)
            raise
        dev = _block_slots(self.chain)
        if self.whole_preset == "fallback" and any(
                dev[pos].get("model_id") != t["model"] or (t["cab"] and dev[pos].get("paired_id") != t["cab"])
                for pos, t in plan["targets"].items()):
            # A `model` command resets the block to its catalog defaults before the values are set
            # (2026-10-15: the live delay read back exactly the DL4 defaults). In fallback mode a
            # model change is one whole write instead (0.46 s vs 0.66 s in place, T018s).
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
            report = self._compiler(hlx, base, out)
            t2 = time.perf_counter()
            if self._decoder is not None:
                # Offline, before anything is written: every block value and every snapshot
                # controller value of the compiled document against the .hlx.
                checks["document"] = check(self._decoder(out), hlx_dict, report, "the compiled document").as_dict()
            self.bridge.call("restore", path=str(out), timeout_s=30)
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
                raise EngineError(f"whole-preset write: slot {pos} holds {cur and cur.get('model_id')}, "
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
                        raise EngineError(f"whole-preset write: slot {pos} cab is {cur.get('paired_id')}, "
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
            raise
        self.last_readback = checks["readback"]
        self.preset = preset

    def set_params(self, changes: dict[ParamRef, float]) -> None:
        if not changes:
            return
        if self.preset is None:
            raise EngineError("set_params before load()")
        dev = _block_slots(self.chain)
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
        x = np.asarray(dry, dtype=np.float32)
        if x.ndim != 1:
            raise EngineError(f"render() takes mono (n,) float32, got shape {x.shape}")
        if len(x) == 0:
            return x.copy()
        tail = self.cal.latency_samples / SAMPLE_RATE + self.TAIL_PAD_S
        key = self._tail_key()
        bound = self._bound_s()
        probe = self.tail_mode == "measured" and bound > self.PRE_S and key not in self.decay_s
        if probe:
            tail += bound   # record this chain's own tail once and measure where it dies
        wait = self._wait_s()
        self.last_wait_s = wait
        rec, pre = reamp.playrec(x, self.device, pre_s=wait, tail_s=tail)
        ended = time.monotonic()
        if probe:
            self.decay_s[key] = measure_decay_s(reamp.to_mono(rec, self.cal.mono), pre + self.cal.latency_samples,
                                                len(x), self.DECAY_REL_DB, self.DECAY_FLOOR_DB)
            self._flush_after = (0.0, ended)   # the recorded tail already played out
        else:
            need = self.decay_s.get(key, bound) + self.DECAY_MARGIN_S if self.tail_mode == "measured" else bound
            self._flush_after = (need if bound > self.PRE_S else 0.0, ended)
        j = reamp.MAX_JITTER
        y_ext = reamp.trim(reamp.to_mono(rec, self.cal.mono), pre, self.cal.latency_samples - j, len(x) + 2 * j, 0)
        # Cancel the 0..1 sample stream-start jitter against this chain's first render of this clip.
        key = (self._chain_key(), hash(x.tobytes()))
        ref = self._refs.get(key)
        if ref is None:
            y, self.last_offset = y_ext[j:j + len(x)].copy(), 0
            self._refs[key] = y
        else:
            y, self.last_offset = reamp.align(y_ext, ref, len(x), j)
        y = (y * np.float32(10 ** (self.cal.level_gain_db / 20))).astype(np.float32)
        if not np.all(np.isfinite(y)):
            raise EngineError("the Stomp capture contains NaN or inf")
        if reamp.rms_db(x) > self.SILENT_IN_DB and reamp.rms_db(y) < self.SILENT_OUT_DB:
            raise EngineError("the Stomp returned silence for a non-silent input (Input block not on USB 5/6?)")
        return y

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
        return float(max(self.PRE_S, min(left, self.MAX_FLUSH_S)))

    def pre_roll_s(self) -> float:
        """Silence before the clip, long enough for the previous render's delay or reverb tail to
        die away (hardware 2026-10-15: a DL4 Ping Pong chain, Time 0.375 s, Feedback 0.40, rendered
        with a 0.25 s pre-roll gave renders of the same document 8.5 dB apart across loads and
        repeats at -28 dB; with 3 s, -32..-36 dB, the chain's repeat floor). Delays: time to -60 dB
        from Time and Feedback (Time 2 s when tempo-synced: worst case); reverbs: REVERB_FLUSH_S."""
        need = self.PRE_S
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
                    "latency_samples": self.cal.latency_samples, "level_gain_db": self.cal.level_gain_db,
                    "input_usb56": self.cal.input_usb56, "original_preset": self.original,
                    "helper": str(helper_path())})

    def close(self, verify_bytes: bool = False) -> None:
        """Restore the original preset and end the session. With ``verify_bytes`` the edit buffer
        is backed up again in the SAME session and byte-compared with the backup taken at open
        (``restore_report["bytes_equal_backup"]``), so no second session is needed (T061 item 1)."""
        if self._closed:
            return
        self._closed = True
        try:
            self.restore_report = self._restore()
            if verify_bytes:
                try:
                    after = Path(self.backup_path).with_name(Path(self.backup_path).stem + "_after_restore.bin")
                    self.bridge.call("backup", path=str(after))
                    self.restore_report["bytes_equal_backup"] = after.read_bytes() == Path(self.backup_path).read_bytes()
                    self.restore_report["after_backup"] = str(after)
                except (BridgeError, OSError) as e:
                    self.restore_report["bytes_equal_backup"] = False
                    self.restore_report["verify_error"] = str(e)
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
