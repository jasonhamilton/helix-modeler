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
"""
from __future__ import annotations

import atexit
import datetime as dt
import importlib.util
import json
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

    def __init__(self, bridge: StompBridge | None = None, calibration: reamp.Calibration | None = None,
                 session_dir: Path | None = None):
        self.cal = calibration or reamp.Calibration.load()
        self.bridge = bridge or StompBridge()
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
        self.chain = self.original_chain
        self.preset = None
        self.slot_map: dict[int, tuple[int, bool]] = {}   # preset slot index -> (device pos, is_cab)
        self.model_change_s: list[float] = []   # model commands alone
        self.model_load_s: list[float] = []     # whole load() calls that changed a model
        self.restore_report: dict = {}
        self.last_offset = 0
        self._refs: dict[tuple, np.ndarray] = {}  # (chain key, clip hash) -> first render, for alignment
        self._cost: Cost | None = None

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
        self._refresh()
        plan = plan_load(preset, self.chain)
        dev = _block_slots(self.chain)
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
            self._apply_values(pos, cur.get("params", []), t["params"], paired=False)
            if t["cab"]:
                self._apply_values(pos, cur.get("paired_params", []), t["cab_params"], paired=True)
            if bool(cur["enabled"]) != t["enabled"]:
                self.bridge.call("enable", pos=pos, on=t["enabled"])
        for pos in plan["bypass"]:
            self.bridge.call("enable", pos=pos, on=False)
        self._route_input()
        time.sleep(self.SETTLE_PARAM_S)
        self._refresh()
        if changed_model:
            self.model_load_s.append(time.perf_counter() - t_start)
        self.preset = preset
        self.slot_map = {}
        for pos, t in plan["targets"].items():
            self.slot_map[t["slot"]] = (pos, False)
            if t["cab_slot"] is not None:
                self.slot_map[t["cab_slot"]] = (pos, True)

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
            self.bridge.call("set", pos=pos, param=meta["i"], value=to_native(meta, v), paired=is_cab)
        time.sleep(self.SETTLE_PARAM_S)

    def render(self, dry: Audio) -> Audio:
        x = np.asarray(dry, dtype=np.float32)
        if x.ndim != 1:
            raise EngineError(f"render() takes mono (n,) float32, got shape {x.shape}")
        if len(x) == 0:
            return x.copy()
        tail = self.cal.latency_samples / SAMPLE_RATE + self.TAIL_PAD_S
        rec, pre = reamp.playrec(x, self.device, pre_s=self.PRE_S, tail_s=tail)
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
                    "latency_samples": self.cal.latency_samples, "level_gain_db": self.cal.level_gain_db,
                    "input_usb56": self.cal.input_usb56, "original_preset": self.original,
                    "helper": str(helper_path())})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.restore_report = self._restore()
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

    def _route_input(self) -> None:
        inp = next((s for s in self.chain["slots"] if s["kind"] == "Input" and s["pos"] == 0), None)
        if inp is None:
            raise EngineError("the pedal's preset has no Path A input")
        if inp.get("routing") != self.cal.input_usb56:
            self.bridge.call("route_key", pos=0, key=51, to=self.cal.input_usb56)

    def _apply_values(self, pos: int, dev_params: list[dict], want: dict, paired: bool) -> None:
        meta = _param_meta(dev_params, cab=paired)
        for pid, v in want.items():
            m = meta.get(pid)
            if m is None:
                continue  # parameter the device does not expose under this id (kept in the .hlx only)
            nv = _wire_value(v)
            if abs(float(m["value"]) - nv) <= EPS * max(1.0, abs(nv)):
                continue
            args = {"pos": pos, "param": m["i"], "value": nv, "paired": paired}
            if "min" not in m:  # no catalog entry: say what the device stores
                args["wire"] = "bool" if isinstance(v, bool) else "int" if isinstance(v, int) else "float"
            self.bridge.call("set", **args)

    def _same_as_original(self) -> tuple[bool, list[str]]:
        now, was = chain_signature(self.bridge.call("chain")), chain_signature(self.original_chain)
        names = ("setlist", "index", "name", "slots", "assignments (any order)")
        diffs = [f"{n} differ" for n, x, y in zip(names, was, now, strict=True) if x != y]
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
            if not ok:
                # Re-selecting the loaded preset may not reload it; step away and back (no flash write).
                other = o["index"] + 1 if o["index"] + 1 < int(self.bridge.ready.get("presets", 126)) else o["index"] - 1
                self.bridge.call("select", setlist=o["setlist"], index=other, timeout_s=20)
                self.bridge.call("select", setlist=o["setlist"], index=o["index"], timeout_s=20)
                report["steps"].append(f"select {other} then original")
                time.sleep(0.5)
                ok, diffs = self._same_as_original()
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
