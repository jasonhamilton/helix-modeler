#!/usr/bin/env python3
"""HX Stomp verification suite (T108, stomp-engine; human top priority 2026-10-17: "make sure the
sampling and automation is robust"). The Stomp side of native-verify-v1. Ledger dataset
``stomp-verify-v1`` (kinds smoke and parity, engine stomp).

The pass/fail rules were fixed in docs/DECISIONS.md (2026-10-17, stomp-engine, T108) BEFORE the
first run and are not changed after it; the constants below implement them.

  gen      offline: draw the 40 (model, settings) items (seed 105), build one .hlx per item on K.hlx's
           frame, compile each onto an 04C backup and decode it (stompd-compile, no USB); drop the
           parameters the device does not store, redraw items that do not build. cache/stomp/verify/
  run      the pedal, ONE batched session: HX Edit must be closed, device check and shared lock
           (tonematch.pedal_lock.session), scratch edit buffer 04C only, the sentinel on. Checks 1 to 4
           in that order, then the original preset is restored and judged by restore_ok. Writes every
           render and readback under cache/stomp/hw/<stamp>-verify/ (analysis is offline).
  analyse  offline: the rules applied to a run directory -> verdict.json
  ledger   one ledger row per check from verdict.json

  TONEMATCH_AGENT=stomp-engine TONEMATCH_STOMPD=cache/stomp/bin/stompd \\
  TONEMATCH_STOMPD_COMPILE=cache/stomp/bin/stompd-compile .venv/bin/python scripts/stomp_verify.py run
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

SR = 48_000
STOMP = ROOT / "cache" / "stomp"
OUT = STOMP / "verify"
K_HLX = STOMP / "inject" / "K.hlx"
COMPILER = STOMP / "bin" / "stompd-compile"
DI = ROOT / "fixtures" / "smoke" / "di_2s_real.wav"
DATASET = "stomp-verify-v1"
SCRATCH_NAME = "Smoke Known"
SCRATCH = (0, 11)                     # setlist, index of 04C
HX_EDIT = "HX Edit.app/Contents/MacOS/HX Edit"
FW_MAX = 0x03800000
USB56 = 6

# --- population (rule "Population") -------------------------------------------------------------
SEED = 105
COUNTS = {"drive": 6, "comp": 5, "eq": 5, "delay": 6, "reverb": 6, "amp": 6, "mod": 4, "other": 2}
SCORED = ("drive", "comp", "eq", "delay", "reverb", "amp")
ENDPOINT_P = 0.2
FIXED_MIN = re.compile(r"^(Hum|Ripple|EarlyReflections|ModDepth|ModRate|ModSpeed|Depth|Mod|Modulation|"
                       r"WowFlutter\d*|Wow Flt|RandDepth|RandSpeed|SweepDepth|Seq Drift)$")
TEMPO_SYNC = re.compile(r"^TempoSync\d*$")
FEEDBACK = re.compile(r"(?i)(feedback|fdbk)")
LEVELS = ("Level", "ChVol", "Master", "FX Level")
LEVEL_U = (0.4, 0.95)
DELAY_TIME_MAX_S = 0.75
FEEDBACK_MAX_U = 0.5
OTHER_EXCLUDE = ("AppDSPFlow", "Looper", "ImpulseResponse")

# --- render rules ---------------------------------------------------------------------------------
TIME_VARIANT_DB = -30.0      # back-to-back null above this: time-variant, not scored
MARGIN_DB = 3.0              # a render passes at <= category floor + 3 dB
LAG_SEARCH = 480             # rule 2c: integer lag search, +-samples
FRAC_LAG_MAX = 0.1           # rule 2d
C2_SEED = 1052
C2_ITEMS, C2_REPEATS, C2_DISTRACTORS = 10, 3, 10
# --- check 3 ---------------------------------------------------------------------------------------
SETTLE_MAX_S = 0.125
SETTLE_WIN = 240             # 5 ms
SETTLE_MARGIN_DB = 6.0
SETTLE_SILENT_DB = -60.0     # windows of the settled capture quieter than this have no null: skipped
STIM_S, CHANGE_AT_S, STREAM_PRE_S, STREAM_TAIL_S = 5.0, 2.0, 0.25, 0.1
C3_TRIALS = 3
# --- check 4 ---------------------------------------------------------------------------------------
SENTINEL_EVERY = 20
FORCED_LATENCY_ERROR = 5


# ============================================================================== pure helpers


def f32(x) -> np.float32:
    return np.float32(float(x))


def same32(a, b) -> bool:
    """Bit-exact equality after float32 storage (what the device keeps)."""
    if a is None or b is None:
        return False
    return bool(f32(a) == f32(b))


def expected_value(kind: str | None, v) -> float:
    """What the device should hold for a sent value v: switches the bool, menus the integer,
    knobs float32(v)."""
    if kind == "Switch":
        return 1.0 if float(v) >= 0.5 else 0.0
    if kind == "Enum":
        return float(round(float(v)))
    return float(f32(v))


def unit_type(name: str, p: dict) -> str:
    """Quantisation report group of a catalog parameter: Hz, dB, time, knob01, menu, switch, other."""
    kind, disp = p.get("kind"), (p.get("display") or "")
    if kind == "bool":
        return "switch"
    if kind == "stepped" or name == "@mic":
        return "menu"
    d = disp.lower()
    if "cut" in d or "freq" in d or d == "frequency":
        return "Hz"
    if d in ("volume", "volume_eq") or name in ("Level", "Threshold", "LowGain", "MidGain", "HighGain"):
        return "dB"
    if d.startswith("time") or "_ms" in d or d.endswith("time_ms") or d in ("comp_attack", "comp_release"):
        return "time"
    lo, hi = p.get("min"), p.get("max")
    if lo == 0 and hi == 1:
        return "knob01"
    return "other"


def model_pool(cat: dict, category: str) -> list[str]:
    out = []
    for mid, m in sorted(cat["models"].items()):
        if not (m.get("stomp") and m.get("mono")):
            continue
        fw = m.get("stomp_min_firmware")
        if fw and int(fw, 16) > FW_MAX:
            continue
        c = m.get("category")
        if category == "amp":
            ok = c == "amp" and mid.startswith("HD2_Amp")
        elif category == "other":
            ok = c == "other" and not any(x in mid for x in OTHER_EXCLUDE)
        elif category == "cab":
            ok = c == "cab"
        else:
            ok = c == category
        if ok:
            out.append(mid)
    return out


def draw_value(name: str, p: dict, rng: random.Random, category: str) -> tuple[object, float]:
    """(native value, normalised u) for one parameter, by the population rule."""
    kind = p.get("kind")
    lo, hi = p.get("min"), p.get("max")
    if kind == "bool":
        if FIXED_MIN.match(name) or TEMPO_SYNC.match(name):
            return False, 0.0
        b = rng.random() < 0.5
        return b, float(b)
    lo, hi = float(lo), float(hi)
    span = hi - lo
    if kind == "stepped":
        if FIXED_MIN.match(name):
            return int(lo), 0.0
        k = rng.randint(round(lo), round(hi))
        return k, (k - lo) / span if span else 0.0
    if FIXED_MIN.match(name):
        return lo, 0.0
    if name in LEVELS:
        u = rng.uniform(*LEVEL_U)
    else:
        r = rng.random()
        u = 0.0 if r < ENDPOINT_P / 2 else 1.0 if r < ENDPOINT_P else rng.random()
    if category == "delay" and name == "Time" and span > 0:
        u = min(u, max(0.0, (DELAY_TIME_MAX_S - lo) / span))
    if FEEDBACK.search(name):
        u = min(u, FEEDBACK_MAX_U)
    return lo + u * span, u


def draw_params(cat: dict, model: str, rng: random.Random, category: str) -> tuple[dict, dict]:
    vals, us = {}, {}
    for name, p in cat["models"][model]["params"].items():
        if name.startswith("@") and name != "@mic":
            continue
        if p.get("stereo_only") or p.get("min") is None:
            continue
        vals[name], us[name] = draw_value(name, p, rng, category)
    return vals, us


def draw_item(cat: dict, category: str, rng: random.Random, idx: int, exclude: set) -> dict:
    pool = [m for m in model_pool(cat, category) if m not in exclude] or model_pool(cat, category)
    model = rng.choice(pool)
    vals, us = draw_params(cat, model, rng, category)
    it = {"id": f"i{idx:02d}", "category": category, "model": model, "scored": category in SCORED,
          "params": vals, "u": us, "cab": None, "cab_params": {}, "cab_u": {}, "dropped": [], "redraws": []}
    if category == "amp":
        cab = rng.choice(model_pool(cat, "cab"))
        cv, cu = draw_params(cat, cab, rng, "cab")
        it.update(cab=cab, cab_params=cv, cab_u=cu)
    return it


def build_hlx(it: dict, values: dict | None = None, cab_values: dict | None = None, enabled: bool = True,
              name: str | None = None) -> dict:
    """K.hlx's frame with one block (or Amp+Cab) at Path A position 0, input USB 5/6, no controller
    or footswitch assignments, every snapshot with the block on."""
    from stomp_fw380 import FW_380

    d = json.loads(K_HLX.read_text())
    data, tone = d["data"], d["data"]["tone"]
    dsp = tone["dsp0"]
    for k in [k for k in dsp if k.startswith(("block", "cab"))]:
        del dsp[k]
    for sec in ("controller", "footswitch"):
        tone.pop(sec, None)
    node = {"@model": it["model"], "@enabled": bool(enabled), "@path": 0, "@position": 0,
            "@no_snapshot_bypass": False, **(it["params"] if values is None else values)}
    if it.get("cab"):
        node.update({"@type": 3, "@cab": "cab0"})
        dsp["cab0"] = {"@model": it["cab"], "@enabled": True,
                       **(it["cab_params"] if cab_values is None else cab_values)}
    dsp["block0"] = node
    for n in range(8):
        sn = tone.get(f"snapshot{n}")
        if isinstance(sn, dict):
            sn["blocks"] = {"dsp0": {"block0": bool(enabled)}}
            sn.pop("controllers", None)
    dsp["inputA"]["@input"] = USB56
    dsp["inputB"]["@input"] = 0
    tone["global"]["@topology0"] = "A"
    tone["global"]["@current_snapshot"] = 0
    data["device_version"] = FW_380
    data["meta"]["name"] = (name or f"verify {it['id']}")[:16]
    return d


MISSING_RE = re.compile(r"^(block0|cab0)\.(.+): in the file, no such parameter on the device$")


def missing_params(diff_missing: list[str]) -> list[tuple[str, str]]:
    out = []
    for m in diff_missing:
        g = MISSING_RE.match(m)
        if g:
            out.append((g.group(1), g.group(2)))
    return out


def draw_population(cat: dict, seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    items: list[dict] = []
    idx = 0
    for category, n in COUNTS.items():
        used: set = set()
        for _ in range(n):
            it = draw_item(cat, category, rng, idx, used)
            used.add(it["model"])
            items.append(it)
            idx += 1
    return order_items(items, rng)


def order_items(items: list[dict], rng: random.Random) -> list[dict]:
    """Shuffle so consecutive items never share a model (each whole write is a model change)."""
    for _ in range(1000):
        rng.shuffle(items)
        if all(a["model"] != b["model"] for a, b in itertools.pairwise(items)):
            return items
    return items


def check2_plan(items: list[dict], seed: int = C2_SEED) -> tuple[list[str], list[str]]:
    """(chosen item ids, render sequence of item ids): at least one chosen item per scored category,
    C2_REPEATS renders each, shuffled with C2_DISTRACTORS renders of other items."""
    rng = random.Random(seed)
    scored = [it for it in items if it["scored"]]
    chosen: list[str] = []
    for c in SCORED:
        pool = [it["id"] for it in scored if it["category"] == c]
        if pool:
            chosen.append(rng.choice(pool))
    rest = [it["id"] for it in scored if it["id"] not in chosen]
    rng.shuffle(rest)
    chosen += rest[:max(0, C2_ITEMS - len(chosen))]
    others = [it["id"] for it in items if it["id"] not in chosen]
    distract = rng.sample(others, min(C2_DISTRACTORS, len(others)))
    seq = [i for i in chosen for _ in range(C2_REPEATS)] + distract
    rng.shuffle(seq)
    return chosen, seq


def int_lag(ref_ext: np.ndarray, x_ext: np.ndarray, w: int = LAG_SEARCH) -> int:
    """Integer lag of x against ref (positive: x is later). Both are the capture from w samples
    before the expected clip start to w after its end; ref's centre is matched within +-w."""
    ref = np.asarray(ref_ext, np.float64)[w:len(ref_ext) - w]
    x = np.asarray(x_ext, np.float64)
    n = len(ref)
    if n <= 0 or len(x) < n + 2 * w:
        raise ValueError("captures too short for the lag search")
    m = 1 << int(np.ceil(np.log2(len(x) + n)))
    c = np.fft.irfft(np.fft.rfft(x, m) * np.conj(np.fft.rfft(ref, m)), m)[:2 * w + 1]
    return int(np.argmax(c)) - w


def settle_index(y: np.ndarray, yb: np.ndarray, win: int = SETTLE_WIN, tail_s: float = 1.0,
                 margin_db: float = SETTLE_MARGIN_DB, silent_db: float = SETTLE_SILENT_DB,
                 max_shift: int = 4) -> dict:
    """Where a capture with a mid-stream change has settled onto the settled capture yb of the new
    state: the first window from which every later window nulls within (median of the last tail_s)
    + margin_db. yb is first aligned to y (integer, +-max_shift, on the last 2 s)."""
    y = np.asarray(y, np.float64)
    yb = np.asarray(yb, np.float64)
    n = min(len(y), len(yb))
    y, yb = y[:n], yb[:n]
    seg = slice(max(0, n - 2 * SR), n - max_shift)
    best, shift = -np.inf, 0
    for s in range(-max_shift, max_shift + 1):
        sc = float(np.dot(y[seg], np.roll(yb, s)[seg]))
        if sc > best:
            best, shift = sc, s
    yb = np.roll(yb, shift)
    k = n // win
    d = (y[:k * win] - yb[:k * win]).reshape(k, win)
    b = yb[:k * win].reshape(k, win)
    eb = np.sum(b ** 2, axis=1)
    loud = 10 * np.log10(np.maximum(eb / win, 1e-30)) > silent_db
    e = np.full(k, np.nan)
    e[loud] = 10 * np.log10(np.maximum(np.sum(d ** 2, axis=1)[loud], 1e-30) / eb[loud])
    tail = e[max(0, k - int(tail_s * SR / win)):]
    ref = float(np.nanmedian(tail)) if np.any(np.isfinite(tail)) else float("nan")
    ok = ~(e > ref + margin_db)          # nan (silent windows) counts as ok
    bad = np.nonzero(~ok)[0]
    w0 = int(bad[-1] + 1) if len(bad) else 0
    return {"index": w0 * win, "ref_db": round(ref, 2), "shift": shift, "profile_db": [
        None if not np.isfinite(v) else round(float(v), 1) for v in e]}


def category_floors(reps: dict[str, float], cats: dict[str, str]) -> tuple[dict, list[str]]:
    """Per category: the worst back-to-back null among the non-time-variant items; and the list of
    time-variant item ids (null above TIME_VARIANT_DB)."""
    tv = sorted(i for i, v in reps.items() if v > TIME_VARIANT_DB)
    floors: dict[str, float] = {}
    for i, v in reps.items():
        if i in tv:
            continue
        floors[cats[i]] = max(floors.get(cats[i], -np.inf), v)
    return floors, tv


def render_pass(null: float, floor: float | None) -> bool:
    return floor is not None and np.isfinite(floor) and null <= floor + MARGIN_DB


def compare_param(kind: str | None, v, sent, reply, r, r2, w) -> dict:
    """Rule 1 per parameter. ``sent``/``reply`` are None when no `set` carried this value."""
    exp = expected_value(kind, v)
    out = {"v": v, "sent": sent, "reply": reply, "r": r, "r2": r2, "w": w, "kind": kind,
           "exact": same32(r, exp), "resend_same": same32(r2, r), "whole_same": same32(w, r),
           "reply_same": None if reply is None else same32(reply, r),
           "sent_is_v": None if sent is None else bool(np.isclose(float(sent), float(v), rtol=1e-12, atol=1e-12))}
    out["dev"] = None if r is None else float(abs(float(r) - float(v)))
    out["ok"] = bool(out["exact"] and out["resend_same"] and out["whole_same"] and out["reply_same"] is not False
                     and out["sent_is_v"] is not False)
    return out


# ============================================================================== gen (offline)


def cmd_gen(a) -> int:
    from stomp_twopath import base_backup

    from tonematch import catalog as C
    from tonematch import preset as P
    from tonematch.engines.base import EngineError
    from tonematch.engines.stomp import compile_document, decode_document
    from tonematch.engines.stomp_readback import readback_diff

    cat = C.load_catalog("current")
    out = OUT / str(a.seed)
    hd = out / "hlx"
    hd.mkdir(parents=True, exist_ok=True)
    base = base_backup()
    items = draw_population(cat, a.seed)
    rng = random.Random(a.seed + 1)
    final = []
    for it in items:
        for attempt in range(6):
            d = build_hlx(it)
            h = hd / f"{it['id']}.hlx"
            h.write_text(P.dumps_hlx(d), encoding="utf-8")
            try:
                rep = compile_document(h, base, hd / f"{it['id']}.bin", compiler=COMPILER)
                ch = decode_document(hd / f"{it['id']}.bin", compiler=COMPILER)
                diff = readback_diff(ch, d, rep)
            except EngineError as e:
                why = f"compile: {e}"
            else:
                drop = missing_params(diff.missing)
                if drop and attempt < 5:
                    for key, name in drop:
                        (it["params"] if key == "block0" else it["cab_params"]).pop(name, None)
                        (it["u"] if key == "block0" else it["cab_u"]).pop(name, None)
                        it["dropped"].append(f"{key}.{name}")
                    continue
                if diff.ok:
                    break
                why = "decode: " + diff.summary(4)
            old = it["model"]
            new = draw_item(cat, it["category"], rng, int(it["id"][1:]), {x["model"] for x in items})
            new["redraws"] = it["redraws"] + [f"{old}: {why}"]
            new["id"] = it["id"]
            it.clear()
            it.update(new)
        else:
            raise SystemExit(f"{it['id']}: no buildable model after 6 attempts: {it['redraws']}")
        final.append(it)
        print(it["id"], it["category"], it["model"], it.get("cab") or "", "dropped", it["dropped"],
              "redraws", len(it["redraws"]), flush=True)
    final = order_items(final, random.Random(a.seed))
    chosen, seq = check2_plan(final)
    (out / "items.json").write_text(json.dumps({"seed": a.seed, "base": str(base), "items": final,
                                                "check2": {"chosen": chosen, "sequence": seq}}, indent=1, default=str))
    print("items:", out / "items.json")
    return 0


# ============================================================================== run (pedal)


def hx_edit_running() -> bool:
    r = subprocess.run(["pgrep", "-f", HX_EDIT], capture_output=True, text=True, check=False)
    mine = {str(os.getpid()), str(os.getppid())}
    return bool([p for p in r.stdout.split() if p not in mine])


class RawTap:
    """Wraps reamp.playrec: keeps Path A's raw capture around the expected clip (before the
    engine's alignment), for the latency rule 2c."""

    def __init__(self, latency: int):
        from tonematch import reamp

        self.reamp, self.lat, self.caps, self.status = reamp, latency, [], []
        self.real = reamp.playrec

        def tapped(mono, device=None, pre_s=reamp.PRE_S, tail_s=reamp.TAIL_S, out_pair=reamp.PLAY_TO,
                   in_pair=reamp.RECORD_FROM):
            rec, pre = self.real(mono, device, pre_s=pre_s, tail_s=tail_s, out_pair=out_pair, in_pair=in_pair)
            try:
                import sounddevice as sd

                self.status.append(str(sd.get_status()))   # PortAudio xrun flags of this stream
            except Exception:  # noqa: BLE001 - diagnostic only
                self.status.append("?")
            m = reamp.to_mono(np.asarray(rec)[:, 0:2])
            s, n = pre + self.lat - LAG_SEARCH, len(mono) + 2 * LAG_SEARCH
            w = np.zeros(n, np.float32)
            seg = m[max(0, s):s + n]
            w[max(0, -s):max(0, -s) + len(seg)] = seg
            self.caps.append(w)
            return rec, pre

        reamp.playrec = tapped

    def uninstall(self) -> None:
        self.reamp.playrec = self.real


class CallTap:
    """Wraps the bridge's call: logs every edit command, its reply and (during check 3a) the
    stream frame at its return."""

    LOGGED = ("set", "restore", "model", "enable", "route_key", "select")

    def __init__(self, bridge):
        self.real = bridge.call
        self.log: list[dict] = []
        self.probe = None
        bridge.call = self

    def __call__(self, cmd, timeout_s=30.0, **a):
        r = self.real(cmd, timeout_s=timeout_s, **a)
        if cmd in self.LOGGED:
            self.log.append({"cmd": cmd, "args": {k: v for k, v in a.items() if k != "path"},
                             "reply": {k: r.get(k) for k in ("native", "text", "status") if k in r},
                             "t": time.monotonic(), "frame": self.probe() if self.probe else None})
        return r


def stream_capture(stim: np.ndarray, device: int, latency: int, at_s: float | None = None, action=None,
                   tap: CallTap | None = None) -> dict:
    """One full-duplex stream (48 kHz, reamp.PA_LATENCY, the stimulus on USB 5/6), with ``action``
    run from this thread once the stream has handed ``at_s`` s of the stimulus to the output.
    Returns Path A mono trimmed by ``latency`` (stimulus index), the frames handed out when each
    logged command returned, and the largest callback block."""
    import sounddevice as sd

    from tonematch import reamp

    info = sd.query_devices(device)
    n_out, n_in = int(info["max_output_channels"]), int(info["max_input_channels"])
    pre, tail = int(STREAM_PRE_S * SR), int(STREAM_TAIL_S * SR) + latency
    buf = np.zeros((pre + len(stim) + tail, n_out), np.float32)
    for ch in reamp.PLAY_TO:
        buf[pre:pre + len(stim), ch - 1] = stim
    rec = np.zeros((len(buf), n_in), np.float32)
    st = {"pos": 0, "maxblock": 0, "status": []}

    def cb(indata, outdata, frames, _time, status):
        if status:
            st["status"].append(str(status))
        p = st["pos"]
        m = max(0, min(frames, len(buf) - p))
        outdata[:m] = buf[p:p + m]
        outdata[m:] = 0
        rec[p:p + m] = indata[:m]
        st["pos"] = p + m
        st["maxblock"] = max(st["maxblock"], frames)
        if p + m >= len(buf):
            raise sd.CallbackStop

    events: list[dict] = []
    deadline = time.monotonic() + len(buf) / SR + reamp.STREAM_GRACE_S + 5.0
    with reamp._streaming(), sd.Stream(device=(device, device), samplerate=SR, channels=(n_in, n_out),
                                       dtype="float32", latency=reamp.PA_LATENCY, callback=cb) as s:
        if action is not None:
            while st["pos"] < pre + at_s * SR:
                if time.monotonic() > deadline:
                    raise reamp.ReampError("check 3 stream did not advance")
                time.sleep(0.001)
            mark = len(tap.log) if tap else 0
            if tap:
                tap.probe = lambda: st["pos"]
            f0 = st["pos"]
            try:
                action()
            finally:
                if tap:
                    tap.probe = None
            events = [{"cmd": e["cmd"], "frame": e["frame"]} for e in (tap.log[mark:] if tap else [])]
            events.insert(0, {"cmd": "action-start", "frame": f0})
        while s.active:
            if time.monotonic() > deadline:
                raise reamp.ReampError("check 3 stream did not finish")
            time.sleep(0.01)
    y = reamp.to_mono(rec[pre + latency:pre + latency + len(stim), 0:2])
    return {"y": y, "events": events, "maxblock": st["maxblock"], "pre": pre, "status": st["status"]}


class _Done(Exception):
    """The selected checks are done."""


class Session:
    """State of one pedal session (records go to run_dir; renders as .npy)."""

    def __init__(self, eng, run_dir: Path, dry: np.ndarray):
        self.eng, self.dir, self.dry = eng, run_dir, dry
        (run_dir / "renders").mkdir(parents=True, exist_ok=True)
        (run_dir / "raw").mkdir(parents=True, exist_ok=True)
        self.raw = RawTap(eng.cal.latency_samples)
        self.calls = CallTap(eng.bridge)
        self.renders: list[dict] = []
        self.res: dict = {"errors": [], "check1": [], "check2": [], "check3": {}, "check4": {}, "anchors": {}}

    def save(self) -> None:
        self.res["renders"] = self.renders
        self.res["sentinel_report"] = self.eng.sentinel_report
        (self.dir / "session.json").write_text(json.dumps(self.res, indent=1, default=str))

    def render(self, label: str, group: str | None, dry: np.ndarray | None = None) -> np.ndarray:
        eng = self.eng
        n0, s0 = len(self.raw.caps), len(self.raw.status)
        nw = len(eng.whole_writes)
        t0 = time.perf_counter()
        try:
            y = eng.render(self.dry if dry is None else dry)
        finally:
            caps, status = self.raw.caps[n0:], self.raw.status[s0:]
            del self.raw.caps[n0:]
            del self.raw.status[s0:]
        dt = time.perf_counter() - t0
        np.save(self.dir / "renders" / f"{label}.npy", y)
        if caps:
            np.save(self.dir / "raw" / f"{label}.npy", caps[0])
        from tonematch import reamp

        self.renders.append({"label": label, "group": group, "s": round(dt, 3), "wait_s": round(eng.last_wait_s, 3),
                             "offset": eng.last_offset, "rms_db": round(reamp.rms_db(y), 2),
                             "stream_status": status[0] if status else None,
                             "sentinel_ran": len(caps) > 1 or len(eng.whole_writes) > nw})
        return y

    def values(self, it: dict) -> dict:
        """The pedal's values of the item's block (device slot 1) by catalog name."""
        from tonematch.engines.stomp import _block_slots, _param_meta

        self.eng._refresh()
        s = _block_slots(self.eng.chain).get(1)
        if s is None:
            return {}
        out = {}
        for tag, cab in (("main", False), ("cab", True)):
            meta = _param_meta(s.get("paired_params" if cab else "params") or [], cab=cab)
            names = it["cab_params"] if cab else it["params"]
            for n in names:
                m = meta.get(n)
                if m is not None:
                    out[f"{tag}:{n}"] = {"value": float(m["value"]), "kind": m.get("kind"), "i": m.get("i"),
                                         "min": m.get("min"), "max": m.get("max")}
        return out


def item_preset(cat: dict, it: dict, values=None, cab_values=None, enabled=True, name=None):
    from tonematch import preset as P

    return P.from_dict(build_hlx(it, values, cab_values, enabled, name), cat)


def check1_item(S: Session, cat: dict, it: dict) -> dict:
    eng = S.eng
    rec: dict = {"id": it["id"], "category": it["category"], "model": it["model"], "cab": it.get("cab"),
                 "scored": it["scored"], "errors": []}
    pv = item_preset(cat, it)
    eng.whole_preset = "always"
    try:
        eng.load(pv)                                     # (a) whole write, from the previous item's state
    finally:
        eng.whole_preset = "fallback"
    rec["whole_readback"] = {k: eng.last_readback.get(k) for k in ("ok", "checked", "mismatches", "missing")}
    w = S.values(it)
    if it["scored"]:
        S.render(f"c1_{it['id']}_r1", it["id"])
        S.render(f"c1_{it['id']}_r2", it["id"])
    # (b) the same model at catalog defaults, in place; then set_params with every addressable value
    addressable = {k for k, m in w.items() if m.get("min") is not None and m.get("max") is not None}
    models = cat["models"]

    def defaults(names: dict, model: str, tag: str) -> dict:
        out = dict(names)
        for n in names:
            if f"{tag}:{n}" in addressable:
                out[n] = models[model]["params"][n]["default"]
        return out

    pdef = item_preset(cat, it, defaults(it["params"], it["model"], "main"),
                       defaults(it["cab_params"], it["cab"], "cab") if it.get("cab") else None)
    nw = len(eng.whole_writes)
    eng.load(pdef)
    rec["defaults_route"] = "whole" if len(eng.whole_writes) > nw else "in place"
    changes = {}
    for k in addressable:
        tag, n = k.split(":", 1)
        slot = 1 if tag == "cab" else 0
        changes[(slot, n)] = (it["cab_u"] if tag == "cab" else it["u"])[n]
    mark = len(S.calls.log)
    eng.set_params(changes)
    sets = [e for e in S.calls.log[mark:] if e["cmd"] == "set"]
    r = S.values(it)
    if it["scored"]:
        S.render(f"c1_{it['id']}_in", it["id"])
    # (c) re-send every value at the read-back value's own normalised position
    resend = {}
    for k in addressable:
        tag, n = k.split(":", 1)
        m = r[k]
        lo, hi = float(m["min"]), float(m["max"])
        if hi > lo:
            resend[(1 if tag == "cab" else 0, n)] = (m["value"] - lo) / (hi - lo)
    eng.set_params(resend)
    r2 = S.values(it)
    by_i = {(bool(e["args"].get("paired")), e["args"].get("param")): e for e in sets}
    params = {}
    for k in sorted(set(w) | set(r)):
        tag, n = k.split(":", 1)
        v = (it["cab_params"] if tag == "cab" else it["params"]).get(n)
        if v is None:
            continue
        m = r.get(k) or w.get(k)
        e = by_i.get((tag == "cab", m.get("i"))) if k in addressable else None
        cp = (models[it["cab"] if tag == "cab" else it["model"]]["params"]).get(n, {})
        params[k] = compare_param(m.get("kind"), v, e["args"]["value"] if e else None,
                                  e["reply"].get("native") if e else None, r.get(k, {}).get("value"),
                                  r2.get(k, {}).get("value"), w.get(k, {}).get("value"))
        params[k].update(type=unit_type(n, cp), addressable=k in addressable,
                         dev_range=[m.get("min"), m.get("max")], cat_range=[cp.get("min"), cp.get("max")])
    named = {f"main:{n}" for n in it["params"]} | {f"cab:{n}" for n in it["cab_params"]}
    rec["not_on_device"] = sorted(named - set(params))
    rec["params"] = params
    rec["readback_ok"] = bool(params) and all(p["ok"] for p in params.values()) and not rec["not_on_device"] \
        and bool(rec["whole_readback"].get("ok"))
    return rec


def check3_presets(cat: dict) -> dict:
    s808 = {"id": "c3", "model": "HD2_DistScream808", "params": {"Gain": 0.5, "Tone": 0.5, "Level": 0.3},
            "cab": None, "cab_params": {}}
    kb_model = "HD2_DistKinkyBoost"
    kb = {"id": "c3kb", "model": kb_model, "cab": None, "cab_params": {},
          "params": {k: v["default"] for k, v in cat["models"][kb_model]["params"].items()
                     if not k.startswith("@") and not v.get("stereo_only")}}
    return {"A": item_preset(cat, s808, name="verify c3 A"),
            "B_level": item_preset(cat, s808, {**s808["params"], "Level": 0.9}, name="verify c3 B"),
            "B_model": item_preset(cat, kb, name="verify c3 KB"),
            "ref": item_preset(cat, s808, {**s808["params"], "Level": 0.6}, name="verify sentinel"),
            "bypass": item_preset(cat, s808, enabled=False, name="verify bypass")}


def check3(S: Session, pr: dict) -> None:
    from tonematch import reamp

    eng = S.eng
    lat = eng.cal.latency_samples
    reps = int(np.ceil(STIM_S * SR / len(S.dry)))
    stim = np.tile(S.dry, reps)[:int(STIM_S * SR)].astype(np.float32)
    d = S.dir / "c3"
    d.mkdir(exist_ok=True)

    def to_a():
        eng.whole_preset = "fallback"
        eng.load(pr["A"])

    def whole(p):
        def go():
            eng.whole_preset = "always"
            try:
                eng.load(p)
            finally:
                eng.whole_preset = "fallback"
        return go

    cases = {"set_params": (lambda: eng.set_params({(0, "Level"): 0.9}), "set"),
             "whole_level": (whole(pr["B_level"]), "restore"),
             "whole_model": (whole(pr["B_model"]), "restore")}
    out: dict = {"trials": [], "renders": {}}
    for case, (action, cmd) in cases.items():
        for t in range(C3_TRIALS):
            to_a()
            time.sleep(0.5)
            cap = stream_capture(stim, eng.device, lat, CHANGE_AT_S, action, S.calls)
            settled = stream_capture(stim, eng.device, lat)
            np.save(d / f"{case}_{t}_mid.npy", cap["y"])
            np.save(d / f"{case}_{t}_settled.npy", settled["y"])
            ev = [e for e in cap["events"] if e["cmd"] == cmd]
            out["trials"].append({"case": case, "trial": t, "cmd": cmd, "events": cap["events"],
                                  "cmd_frame": ev[-1]["frame"] if ev else None, "maxblock": cap["maxblock"],
                                  "pre": cap["pre"], "status": cap["status"] + settled["status"]})
            S.res["check3"] = out
            S.save()
            print("c3a", case, t, "cmd frame", ev[-1]["frame"] if ev else None, flush=True)
        # (3b) a render straight after the change at the current waits, then two more; then PRE_S 0.05
        to_a()
        S.render(f"c3_{case}_primeA", f"c3_{case}_A")
        action()
        S.render(f"c3_{case}_imm", f"c3_{case}_B")
        S.render(f"c3_{case}_s1", f"c3_{case}_B")
        S.render(f"c3_{case}_s2", f"c3_{case}_B")
        to_a()
        S.render(f"c3_{case}_primeA2", f"c3_{case}_A")
        old = eng.PRE_S
        eng.PRE_S = 0.05
        try:
            action()
            S.render(f"c3_{case}_fast", f"c3_{case}_B")
        finally:
            eng.PRE_S = old
        out["renders"][case] = {"pre_s": old, "fast_pre_s": 0.05}
        S.res["check3"] = out
        S.save()
    _ = reamp


def latency_anchor(S: Session, pr: dict, tag: str) -> None:
    from tonematch import reamp

    S.eng.whole_preset = "always"
    try:
        S.eng.load(pr["bypass"])
    finally:
        S.eng.whole_preset = "fallback"
    n0, s0 = len(S.raw.caps), len(S.raw.status)
    S.res["anchors"][tag] = reamp.measure_latency(S.eng.device, 3)
    del S.raw.caps[n0:]
    del S.raw.status[s0:]
    print("latency anchor", tag, S.res["anchors"][tag], flush=True)


# Carry-over diagnostic (not a rule; added after the 16:54Z session, T108): does a render depend
# on the render just before it on the same block, and how much pre-roll removes that?
DIAG_ITEMS = ("i02", "i11", "i09", "i33", "i12", "i30", "i01")
DIAG_PRE = (0.25, 0.5, 1.0, 2.0, 3.0)
DIAG_IDLE_S = 3.0


def diag(S: Session, cat: dict, items: dict) -> None:
    """Per block: whole write, two renders after DIAG_IDLE_S of idle each ("rested"), then for each
    pre-roll p a render at the engine's pre-roll followed at once by a render at pre-roll p. The
    sentinel is held off (its whole writes would reset the block between the two)."""
    eng = S.eng
    out = S.res.setdefault("diag", {})
    eng._sentinel_busy = True
    try:
        for iid in DIAG_ITEMS:
            it = items[iid]
            eng.whole_preset = "always"
            try:
                eng.load(item_preset(cat, it))
            finally:
                eng.whole_preset = "fallback"
            g = f"d_{iid}"
            for k in (1, 2):
                time.sleep(DIAG_IDLE_S)
                S.render(f"d_{iid}_rest{k}", g)
            old = eng.PRE_S
            for p in DIAG_PRE:
                S.render(f"d_{iid}_prime_{p}", g)
                eng.PRE_S = p
                try:
                    S.render(f"d_{iid}_bb_{p}", g)
                finally:
                    eng.PRE_S = old
            out[iid] = {"model": it["model"], "pre": list(DIAG_PRE)}
            S.save()
            print("diag", iid, it["model"], flush=True)
    finally:
        eng._sentinel_busy = False


def cmd_run(a) -> int:
    import soundfile as sf

    from tonematch import catalog as C
    from tonematch import pedal_lock, reamp
    from tonematch.engines.stomp import Sentinel, SentinelDrift, StompEngine, restore_verdict
    from tonematch.engines.stomp_bridge import BridgeError

    if hx_edit_running():
        sys.exit("HX Edit is running: quit it first (nothing was sent to the pedal)")
    if not os.environ.get("TONEMATCH_STOMPD") and not shutil.which("stompd"):
        sys.exit("set TONEMATCH_STOMPD to the USB helper (cache/stomp/bin/stompd)")
    os.environ.setdefault("TONEMATCH_STOMPD_COMPILE", str(COMPILER))
    spec = json.loads((OUT / str(a.seed) / "items.json").read_text())
    items = {it["id"]: it for it in spec["items"]}
    order = [it["id"] for it in spec["items"]][:a.limit or None]
    cat = C.load_catalog("current")
    dry, sr = sf.read(DI, dtype="float32")
    assert sr == SR
    pr = check3_presets(cat)
    clip, _ = reamp.excerpt(dry, 1.0)
    run_dir = STOMP / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-verify"
    run_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.perf_counter()
    res_final: dict = {"run_dir": str(run_dir), "seed": a.seed}
    with pedal_lock.session("stomp_verify run (T108)"):
        eng = StompEngine(whole_preset="fallback", session_dir=run_dir, purpose="stomp_verify run (T108)",
                          sentinel=Sentinel(every=SENTINEL_EVERY, preset=pr["ref"], clip=clip))
        S = Session(eng, run_dir, dry)
        S.res.update(original=eng.original, firmware=eng.info.get("firmware"), seed=a.seed,
                     latency_samples=eng.cal.latency_samples)
        stage = "open"
        try:
            if eng.original.get("name") != SCRATCH_NAME:
                if not a.select_scratch:
                    raise SystemExit(f"the pedal is on {eng.original}, not 04C '{SCRATCH_NAME}': nothing written "
                                     "(re-run with --select-scratch to select 04C without writing)")
                eng.bridge.call("select", setlist=SCRATCH[0], index=SCRATCH[1], timeout_s=20)
                eng._refresh()
                S.res["selected_scratch"] = True
            checks = set(a.checks.split(","))
            S.res["checks"] = sorted(checks)
            stage = "anchor-start"
            latency_anchor(S, pr, "start")
            if "diag" in checks:
                stage = "diag"
                diag(S, cat, items)
            stage = "check1"
            for n, iid in enumerate(order if "1" in checks else []):
                it = items[iid]
                try:
                    r = check1_item(S, cat, it)
                except (SentinelDrift, BridgeError, reamp.ReampError):
                    raise
                except Exception as e:  # noqa: BLE001 - an item failure is recorded, the session goes on
                    r = {"id": iid, "category": it["category"], "model": it["model"], "scored": it["scored"],
                         "errors": [f"{type(e).__name__}: {e}"], "readback_ok": False}
                S.res["check1"].append(r)
                S.save()
                print(f"c1 {n + 1}/{len(order)} {iid} {it['category']} {it['model']} readback_ok={r.get('readback_ok')} "
                      f"errors={r.get('errors')}", flush=True)
            stage = "check2"
            done = set(order)
            seq = [i for i in spec["check2"]["sequence"] if i in done] if "2" in checks else []
            for k, iid in enumerate(seq):
                it = items[iid]
                nw = len(eng.whole_writes)
                ent = {"k": k, "id": iid, "chosen": iid in spec["check2"]["chosen"]}
                try:
                    eng.load(item_preset(cat, it))
                    ent["route"] = "whole" if len(eng.whole_writes) > nw else "in place"
                    S.render(f"c2_{k:02d}_{iid}", iid)
                except (SentinelDrift, BridgeError, reamp.ReampError):
                    raise
                except Exception as e:  # noqa: BLE001
                    ent["error"] = f"{type(e).__name__}: {e}"
                S.res["check2"].append(ent)
                S.save()
                print("c2", k, iid, ent.get("route"), ent.get("error", ""), flush=True)
            stage = "check3"
            if "3" in checks:
                check3(S, pr)
            stage = "anchor-end"
            latency_anchor(S, pr, "end")
            stage = "check4"
            if "4" not in checks:
                raise _Done
            c4: dict = {"unforced_trips": bool(eng.sentinel_report.get("tripped")),
                        "checks_before": eng.sentinel_report.get("checks")}
            eng.cal.latency_samples += FORCED_LATENCY_ERROR     # in memory only; the file is never written
            try:
                eng.sentinel_check_now()
                c4["detected"] = False
            except SentinelDrift as e:
                c4.update(detected=True, reason=str(e))
            finally:
                eng.cal.latency_samples -= FORCED_LATENCY_ERROR
            try:
                eng.render(clip)
                c4["refused_after"] = False
            except SentinelDrift:
                c4["refused_after"] = True
            S.res["check4"] = c4
            stage = "done"
        except _Done:
            stage = "done"
        except SentinelDrift as e:
            S.res["errors"].append(f"{stage}: SentinelDrift (session stopped): {e}")
            print("SENTINEL TRIPPED", e, flush=True)
        except SystemExit as e:
            S.res["errors"].append(f"{stage}: {e}")
        except BaseException as e:  # noqa: BLE001 - always restore
            S.res["errors"].append(f"{stage}: {type(e).__name__}: {e}")
            print("ERROR", stage, type(e).__name__, e, flush=True)
        finally:
            S.raw.uninstall()
            S.res["stage"] = stage
            S.res["engine"] = {"whole_writes": len(eng.whole_writes), "model_load_s": eng.model_load_s,
                               "readbacks_skipped": eng.readbacks_skipped}
            S.res["call_log"] = S.calls.log[-2000:]
            eng.close(verify_bytes=True)
            S.res["restore_report"] = eng.restore_report
            S.res["restore_ok"] = restore_verdict(eng.restore_report)
            S.res["session_close"] = getattr(eng.bridge, "release_report", None)
            S.res["wall_clock_s"] = round(time.perf_counter() - t_start, 1)
            S.save()
    res_final.update(stage=S.res["stage"], errors=S.res["errors"], restore_ok=S.res["restore_ok"],
                     wall_clock_s=S.res["wall_clock_s"])
    print(json.dumps(res_final, indent=1, default=str))
    print("session:", run_dir / "session.json")
    return 0 if S.res["restore_ok"] and not S.res["errors"] else 1


# ============================================================================== analyse (offline)


def _load(run: Path, label: str, raw: bool = False) -> np.ndarray | None:
    p = run / ("raw" if raw else "renders") / f"{label}.npy"
    return np.load(p) if p.exists() else None


def analyse(run: Path) -> dict:
    import stomp_hw

    from tonematch import reamp

    ses = json.loads((run / "session.json").read_text())
    spec = json.loads((OUT / str(ses.get("seed", SEED)) / "items.json").read_text())
    items = {it["id"]: it for it in spec["items"]}
    v: dict = {"run": str(run), "restore_ok": ses.get("restore_ok"), "errors": ses.get("errors"),
               "stage": ses.get("stage")}
    # --- floors
    reps, cats = {}, {}
    for r in ses["check1"]:
        a, b = _load(run, f"c1_{r['id']}_r1"), _load(run, f"c1_{r['id']}_r2")
        if r.get("scored") and a is not None and b is not None:
            reps[r["id"]] = float(reamp.null_db(a, b))
            cats[r["id"]] = r["category"]
    floors, tv = category_floors(reps, cats)
    v["floors_db"] = {k: round(x, 2) for k, x in floors.items()}
    v["repeat_db"] = {k: round(x, 2) for k, x in reps.items()}
    v["time_variant"] = tv
    # --- check 1
    c1 = {"items": [], "quantisation": {}}
    q: dict = {}
    for r in ses["check1"]:
        e = {"id": r["id"], "category": r["category"], "model": r["model"], "readback_ok": bool(r.get("readback_ok")),
             "errors": r.get("errors"), "bad_params": {k: p for k, p in (r.get("params") or {}).items() if not p["ok"]},
             "not_on_device": r.get("not_on_device"), "whole_readback": r.get("whole_readback")}
        # Diagnostic, not a rule: the parameters that fail only because set_params' value differs from
        # the file's v below float32 precision (the device reports its ranges as float32 copies of the
        # catalog's), i.e. the same float32 on the device.
        e["sent_only_f32_equal"] = sorted(
            k for k, p in e["bad_params"].items()
            if p["sent_is_v"] is False and p["sent"] is not None and same32(p["sent"], p["v"])
            and p["exact"] and p["resend_same"] and p["whole_same"] and p["reply_same"] is not False)
        e["readback_ok_f32_diag"] = bool(e["readback_ok"] or (
            not e["errors"] and not e["not_on_device"] and (e["whole_readback"] or {}).get("ok")
            and set(e["bad_params"]) == set(e["sent_only_f32_equal"])))
        for k, p in (r.get("params") or {}).items():
            g = q.setdefault(p["type"], {"n": 0, "f32_exact": 0, "max_dev": 0.0, "max_dev_param": None})
            g["n"] += 1
            g["f32_exact"] += int(p["exact"])
            if p["dev"] is not None and p["dev"] > g["max_dev"]:
                g["max_dev"], g["max_dev_param"] = p["dev"], f"{r['model']} {k} v={p['v']} r={p['r']}"
        a, b = _load(run, f"c1_{r['id']}_r1"), _load(run, f"c1_{r['id']}_in")
        if r.get("scored") and r["id"] not in tv:
            if a is None or b is None:
                e.update(render_scored=True, render_ok=False, render_null_db=None)
            else:
                nd = float(reamp.null_db(a, b))
                e.update(render_scored=True, render_null_db=round(nd, 2), floor_db=floors.get(r["category"]),
                         render_ok=render_pass(nd, floors.get(r["category"])),
                         profile_db=stomp_hw.profile(b, a) if not render_pass(nd, floors.get(r["category"])) else None)
        else:
            e["render_scored"] = False
        e["ok"] = e["readback_ok"] and e.get("render_ok", True) is not False
        c1["items"].append(e)
    c1["quantisation"] = q
    c1["pass"] = sum(e["ok"] for e in c1["items"])
    c1["total"] = len(c1["items"])
    c1["readback_pass"] = sum(e["readback_ok"] for e in c1["items"])
    c1["readback_pass_f32_diag"] = sum(e["readback_ok_f32_diag"] for e in c1["items"])
    sc = [e for e in c1["items"] if e.get("render_scored")]
    c1["render_pass"], c1["render_total"] = sum(bool(e.get("render_ok")) for e in sc), len(sc)
    c1["worst_render"] = max(sc, key=lambda e: (e.get("render_null_db") or 0) - (e.get("floor_db") or 0), default=None)
    c1["passed"] = c1["pass"] == c1["total"] == COUNTS_TOTAL
    v["check1"] = c1
    # --- check 2
    c2: dict = {"renders": []}
    first: dict = {}
    for ent in ses["check2"]:
        if not ent.get("chosen"):
            continue
        lab = f"c2_{ent['k']:02d}_{ent['id']}"
        y = _load(run, lab)
        it = items[ent["id"]]
        row = {"label": lab, "id": ent["id"], "category": it["category"], "route": ent.get("route"),
               "error": ent.get("error")}
        if ent["id"] in tv:
            row["scored"] = False
        elif y is None:
            row.update(scored=True, ok=False)
        elif ent["id"] not in first:
            first[ent["id"]] = y
            row.update(scored=False, first=True)        # the reference; the later renders are scored
        else:
            nd = float(reamp.null_db(first[ent["id"]], y))
            fl = floors.get(it["category"])
            row.update(scored=True, null_db=round(nd, 2), floor_db=fl, ok=render_pass(nd, fl))
        c2["renders"].append(row)
    scored2 = [r for r in c2["renders"] if r.get("scored")]
    c2["pass"], c2["total"] = sum(bool(r.get("ok")) for r in scored2), len(scored2)
    c2["worst"] = max((r for r in scored2 if "null_db" in r), key=lambda r: r["null_db"] - (r["floor_db"] or 0),
                      default=None)
    # 2c / 2d: every repeated setting in checks 1 and 2 (grouped by item id; sentinel captures not included)
    groups: dict[str, list[str]] = {}
    for rr in ses["renders"]:
        g = rr.get("group")
        if g and (rr["label"].startswith("c1_") or rr["label"].startswith("c2_")):
            groups.setdefault(g, []).append(rr["label"])
    lags, fracs = [], []
    for g, labs in groups.items():
        if len(labs) < 2:
            continue
        ref_raw, ref_y = _load(run, labs[0], raw=True), _load(run, labs[0])
        for lab in labs[1:]:
            xr, y = _load(run, lab, raw=True), _load(run, lab)
            if ref_raw is not None and xr is not None:
                lags.append({"group": g, "label": lab, "lag": int_lag(ref_raw, xr)})
            if ref_y is not None and y is not None and g not in tv:
                fracs.append({"group": g, "label": lab, "lag": float(stomp_hw.aligned_null(ref_y, y)["lag"])})
    c2["latency"] = {"n": len(lags), "nonzero": [x for x in lags if x["lag"] != 0],
                     "lag_values": sorted({x["lag"] for x in lags}), "anchors": ses.get("anchors"),
                     "calibration": ses.get("latency_samples")}
    c2["alignment"] = {"n": len(fracs), "worst": max(fracs, key=lambda x: abs(x["lag"]), default=None),
                       "over": [x for x in fracs if abs(x["lag"]) > FRAC_LAG_MAX]}
    c2["routes"] = sorted({r.get("route") for r in c2["renders"] if r.get("route")})
    c2["passed"] = (c2["pass"] == c2["total"] > 0 and not c2["latency"]["nonzero"] and c2["latency"]["n"] > 0
                    and not c2["alignment"]["over"])
    v["check2"] = c2
    # --- check 3
    c3: dict = {"trials": [], "renders": {}}
    lat = int(ses.get("latency_samples") or 1207)
    for t in (ses.get("check3") or {}).get("trials", []):
        mid = np.load(run / "c3" / f"{t['case']}_{t['trial']}_mid.npy")
        st = np.load(run / "c3" / f"{t['case']}_{t['trial']}_settled.npy")
        si = settle_index(mid, st)
        row = {"case": t["case"], "trial": t["trial"], "ref_db": si["ref_db"], "settled_at": si["index"]}
        if t.get("cmd_frame") is None:
            row.update(settle_s=None, ok=False, why="no command frame")
        else:
            k = si["index"] + t["pre"]
            s_up = (k - t["cmd_frame"] + lat + t["maxblock"]) / SR
            s_lo = (k - t["cmd_frame"]) / SR
            row.update(settle_upper_s=round(s_up, 4), settle_lower_s=round(s_lo, 4), ok=s_up <= SETTLE_MAX_S)
            # Diagnostic, not the rule: settled = every later window (but the last 50 ms) within -60 dB.
            e = np.array([np.nan if x is None else x for x in si["profile_db"]])[:-10]
            bad = np.nonzero(np.nan_to_num(e, nan=-999.0) > -60.0)[0]
            w60 = int(bad[-1] + 1) if len(bad) else 0
            row["settle_60db_upper_s"] = round((w60 * SETTLE_WIN + t["pre"] - t["cmd_frame"] + lat + t["maxblock"]) / SR, 4)
        c3["trials"].append(row)
    for case in ("set_params", "whole_level", "whole_model"):
        imm, s1, s2, fast = (_load(run, f"c3_{case}_{x}") for x in ("imm", "s1", "s2", "fast"))
        if s1 is None or s2 is None or imm is None:
            c3["renders"][case] = {"ok": False, "why": "missing renders"}
            continue
        fl = float(reamp.null_db(s1, s2))
        ni = float(reamp.null_db(s1, imm))
        c3["renders"][case] = {"floor_db": round(fl, 2), "imm_db": round(ni, 2), "ok": ni <= fl + MARGIN_DB,
                               "fast_db": None if fast is None else round(float(reamp.null_db(s1, fast)), 2)}
    c3["worst_settle_60db_s"] = max((r.get("settle_60db_upper_s") for r in c3["trials"]
                                     if r.get("settle_60db_upper_s") is not None), default=None)
    c3["worst_settle"] = max((r for r in c3["trials"] if r.get("settle_upper_s") is not None),
                             key=lambda r: r["settle_upper_s"], default=None)
    allc3 = c3["trials"] + list(c3["renders"].values())
    c3["pass"], c3["total"] = sum(bool(r.get("ok")) for r in allc3), len(allc3)
    c3["passed"] = c3["total"] == 3 * C3_TRIALS + 3 and c3["pass"] == c3["total"]
    v["check3"] = c3
    # --- check 4
    sr = ses.get("sentinel_report") or {}
    c4 = dict(ses.get("check4") or {})
    c4.update(sentinel={k: sr.get(k) for k in ("enabled", "every", "checks", "floor_db", "threshold_db", "tripped",
                                               "reason", "disabled_reason", "seconds")},
              worst_null_db=max(sr.get("nulls_db") or [float("nan")]))
    c4["live_passed"] = bool(sr.get("enabled") is not False and c4.get("unforced_trips") is False
                             and c4.get("detected") and c4.get("refused_after") and ses.get("restore_ok"))
    v["check4"] = c4
    v["diag"] = analyse_diag(run, ses)
    st = [r.get("stream_status") for r in ses.get("renders", []) if r.get("stream_status") is not None]
    v["stream_status"] = {"renders": len(st), "flagged": [s for s in st if s not in ("", "?")]}
    return v


def residual_extent_ms(ref: np.ndarray, x: np.ndarray, rel_db: float = -60.0, win: int = SETTLE_WIN) -> dict:
    """Where x differs from ref: 5 ms windows whose residual is above rel_db of the window's energy.
    Returns the count, the last such window's end (ms) and whether they form one leading stretch."""
    a, b = np.asarray(ref, np.float64), np.asarray(x, np.float64)
    n = min(len(a), len(b)) // win
    d = (b[:n * win] - a[:n * win]).reshape(n, win)
    e = 10 * np.log10(np.maximum(np.sum(d ** 2, 1), 1e-30) / np.maximum(np.sum(a[:n * win].reshape(n, win) ** 2, 1), 1e-30))
    bad = np.nonzero(e > rel_db)[0]
    return {"windows": len(bad), "last_ms": None if not len(bad) else int((bad[-1] + 1) * win * 1000 // SR),
            "leading": bool(len(bad)) and bool(np.all(np.diff(np.concatenate([[-1], bad])) == 1))}


def analyse_diag(run: Path, ses: dict) -> dict:
    from tonematch import reamp

    out = {}
    for iid, d in (ses.get("diag") or {}).items():
        r1, r2 = _load(run, f"d_{iid}_rest1"), _load(run, f"d_{iid}_rest2")
        if r1 is None or r2 is None:
            continue
        row = {"model": d["model"], "rest_floor_db": round(float(reamp.null_db(r1, r2)), 2),
               "rest_extent": residual_extent_ms(r1, r2), "pre": {}}
        for p in d["pre"]:
            bb, pm = _load(run, f"d_{iid}_bb_{p}"), _load(run, f"d_{iid}_prime_{p}")
            if bb is None:
                continue
            row["pre"][str(p)] = {"bb_vs_rest_db": round(float(reamp.null_db(r1, bb)), 2),
                                  "bb_extent": residual_extent_ms(r1, bb),
                                  "prime_vs_rest_db": None if pm is None else round(float(reamp.null_db(r1, pm)), 2)}
        out[iid] = row
    return out


COUNTS_TOTAL = sum(COUNTS.values())


def cmd_analyse(a) -> int:
    run = Path(a.run)
    v = analyse(run)
    (run / "verdict.json").write_text(json.dumps(v, indent=1, default=str))
    brief = {"restore_ok": v["restore_ok"], "errors": v["errors"], "stage": v["stage"], "floors_db": v["floors_db"],
             "time_variant": v["time_variant"],
             "check1": {k: v["check1"][k] for k in ("passed", "pass", "total", "readback_pass", "render_pass",
                                                     "render_total")},
             "check2": {k: v["check2"][k] for k in ("passed", "pass", "total")} | {
                 "lag_values": v["check2"]["latency"]["lag_values"], "nonzero_lags": len(v["check2"]["latency"]["nonzero"]),
                 "align_over": len(v["check2"]["alignment"]["over"])},
             "check3": {k: v["check3"][k] for k in ("passed", "pass", "total", "worst_settle")},
             "check4": {k: v["check4"].get(k) for k in ("live_passed", "detected", "refused_after", "unforced_trips")}}
    print(json.dumps(brief, indent=1, default=str))
    print("verdict:", run / "verdict.json")
    return 0


def cmd_ledger(a) -> int:
    run = Path(a.run)
    v = json.loads((run / "verdict.json").read_text())
    ses = json.loads((run / "session.json").read_text())
    n_r = len(ses.get("renders", []))
    wall = ses.get("wall_clock_s")
    rows = []
    c1 = v["check1"]
    w1 = c1.get("worst_render") or {}
    rows.append(("smoke", "check1_setting_truth_pass", c1["pass"],
                 (f"{c1['pass']}/{c1['total']} items (readback {c1['readback_pass']}/{c1['total']}; renders "
                 f"{c1['render_pass']}/{c1['render_total']}); worst render {w1.get('model')} "
                 f"{w1.get('render_null_db')} dB vs floor {w1.get('floor_db')}; passed={c1['passed']}")))
    c2 = v["check2"]
    w2 = c2.get("worst") or {}
    rows.append(("parity", "check2_repeat_pass", c2["pass"],
                 (f"{c2['pass']}/{c2['total']} renders; worst {w2.get('id')} {w2.get('null_db')} dB vs floor "
                 f"{w2.get('floor_db')}; raw lags {c2['latency']['lag_values']} ({len(c2['latency']['nonzero'])} "
                 f"nonzero of {c2['latency']['n']}); align over 0.1 = {len(c2['alignment']['over'])}; "
                 f"passed={c2['passed']}")))
    c3 = v["check3"]
    w3 = c3.get("worst_settle") or {}
    rows.append(("smoke", "check3_settle_pass", c3["pass"],
                 (f"{c3['pass']}/{c3['total']}; worst settle {w3.get('case')} {w3.get('settle_upper_s')} s "
                 f"(upper bound; limit {SETTLE_MAX_S}); passed={c3['passed']}")))
    c4 = v["check4"]
    rows.append(("smoke", "check4_sentinel_pass", int(bool(c4.get("live_passed"))),
                 (f"live: unforced trips {c4.get('unforced_trips')} in {c4['sentinel'].get('checks')} checks "
                 f"(worst {c4.get('worst_null_db')} dB, threshold {c4['sentinel'].get('threshold_db')}); forced "
                 f"+{FORCED_LATENCY_ERROR} samples detected={c4.get('detected')} refused_after={c4.get('refused_after')}")))
    rows.append(("smoke", "restore_ok", int(bool(v.get("restore_ok"))), f"session {run.name}; stage {v.get('stage')}"))
    if a.only:
        keep = {"c1": 0, "c2": 1, "c3": 2, "c4": 3, "restore": 4}
        rows = [rows[keep[k]] for k in a.only.split(",")]
    for kind, metric, value, notes in rows:
        notes = f"{a.label}: {notes}" if a.label else notes
        args = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", kind, "--engine", "stomp",
                "--dataset", DATASET, "--metric", metric, "--value", str(value), "--renders", str(n_r),
                "--notes", notes]
        if wall is not None:
            args += ["--wall-clock-s", str(wall)]
        if a.dry_run:
            print(" ".join(args))
            continue
        subprocess.run(args, check=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--seed", type=int, default=SEED)
    r = sub.add_parser("run")
    r.add_argument("--seed", type=int, default=SEED)
    r.add_argument("--select-scratch", action="store_true",
                   help="select 04C (no write) when the pedal is on another preset; close() selects it back")
    r.add_argument("--limit", type=int, default=0, help="check-1 items (0 = all; a shakedown only)")
    r.add_argument("--checks", default="1,2,3,4",
                   help="comma list of 1,2,3,4 and diag (the carry-over diagnostic); default all four checks")
    an = sub.add_parser("analyse")
    an.add_argument("--run", required=True)
    le = sub.add_parser("ledger")
    le.add_argument("--run", required=True)
    le.add_argument("--dry-run", action="store_true")
    le.add_argument("--label", default="", help="prefix for every row's notes (which session, which engine)")
    le.add_argument("--only", default="", help="comma list of rows to write: c1,c2,c3,c4,restore (default all)")
    a = ap.parse_args(argv)
    return {"gen": cmd_gen, "run": cmd_run, "analyse": cmd_analyse, "ledger": cmd_ledger}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
