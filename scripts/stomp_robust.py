#!/usr/bin/env python3
"""HX Stomp robustness follow-ups to T108 (stomp-engine): T110 device-refused models, T111 in-place
time-parameter glide, T112 stream slips. Ledger datasets ``stomp-refused-v1``, ``stomp-glide-v1``,
``stomp-slip-v1`` (engine stomp).

  probe    the pedal, ONE session (diagnosis, no pass rule): T110 device-built documents of the four
           refused models; the Path B loopback (pilot) feasibility; raw slip captures at a short and
           the production pre-roll; the T111 glide experiments. cache/stomp/hw/<stamp>-robust-probe/
           (Run 2026-10-17T20:48Z; it used a bypassed Gain block as the pilot, which the device refused.)
  session  the pedal, ONE session, rules registered in docs/DECISIONS.md BEFORE it runs: T110 device
           templates and gated loads; T112 pilot check, development phase D (threshold), pass phase P;
           T111 replication of the T108 check-1 sequence and the policy check.
           cache/stomp/hw/<stamp>-robust/
  analyse  offline: a session directory -> verdict.json (the registered rules)
  ledger   ledger rows from verdict.json

Every pedal step holds the shared lock (tonematch.pedal_lock.session), needs HX Edit closed, writes
the 04C scratch edit buffer only (never a flash slot) and ends with the original preset restored and
judged by restore_ok.

  TONEMATCH_AGENT=stomp-engine TONEMATCH_STOMPD=cache/stomp/bin/stompd \\
  TONEMATCH_STOMPD_COMPILE=cache/stomp/bin/stompd-compile .venv/bin/python scripts/stomp_robust.py probe
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import stomp_verify as V

SR = 48_000
STOMP = ROOT / "cache" / "stomp"
DI = ROOT / "fixtures" / "smoke" / "di_2s_real.wav"
ITEMS = STOMP / "verify" / "105" / "items.json"
REFUSED = ("i17", "i18", "i26", "i29")       # Stutter Edit, Inf Sustain, NonLinear, Moon Nrm + WithPan cab
GLIDE = ("i21", "i23")                        # DL4 Echo Platter, Glitz
S808 = {"id": "s808", "model": "HD2_DistScream808", "params": {"Gain": 0.5, "Tone": 0.5, "Level": 0.3},
        "cab": None, "cab_params": {}}
PILOT_MODEL = "HD2_VolPanGain"   # the 20:48Z probe's pilot (refused); the engine's is stomp.PILOT_MODEL


def now_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def items() -> dict:
    return {it["id"]: it for it in json.loads(ITEMS.read_text())["items"]}


def defaults_of(cat: dict, model: str) -> dict:
    return {k: v["default"] for k, v in cat["models"][model]["params"].items()
            if not k.startswith("@") and not v.get("stereo_only")}


def pilot_preset(cat: dict):
    """The Path B pilot alone: one bypassed Gain block (K.hlx's frame)."""
    it = {"id": "pilot", "model": PILOT_MODEL, "params": {"Gain": 0.0}, "cab": None, "cab_params": {}}
    return V.item_preset(cat, it, enabled=False, name="pilot")


def raw_capture(eng, x: np.ndarray, pre_s: float, post: np.ndarray | None = None, tail_s: float = 0.1) -> dict:
    """One stream through what the pedal holds: every input channel, around the clip. Returns Path A
    (USB 1/2 mid) and Path B (USB 3/4 mid) from the clip start - 64 samples to its end + tail."""
    import sounddevice as sd

    from tonematch import reamp

    sig = x if post is None else np.concatenate([x, post]).astype(np.float32)
    rec, pre = reamp.playrec(sig, eng.device, pre_s=pre_s, tail_s=tail_s + eng.cal.latency_samples / SR,
                             in_pair=None)
    try:
        status = str(sd.get_status())
    except Exception:  # noqa: BLE001 - diagnostic
        status = "?"
    lat = eng.cal.latency_samples
    s, n = pre + lat - 64, len(sig) + 128
    a = reamp.to_mono(rec[:, 0:2])[s:s + n]
    b = reamp.to_mono(rec[:, 2:4])[s:s + n] if rec.shape[1] >= 4 else None
    return {"a": a.astype(np.float32), "b": None if b is None else b.astype(np.float32), "status": status,
            "channels": int(rec.shape[1])}


# ============================================================================== probe (session 1)


def probe_refused(eng, cat: dict, its: dict, d: Path, log: dict) -> None:
    """T110: what the device builds itself for each refused model (``model`` command, then the
    item's values one ``set`` at a time), against our compiled documents of the same block."""
    from tonematch.engines.stomp import compile_document
    from tonematch.engines.stomp_bridge import BridgeError
    from tonematch.preset import dumps_hlx

    out = log.setdefault("refused", {})
    frame = V.item_preset(cat, S808, name="robust frame")
    for iid in REFUSED:
        it = its[iid]
        rec: dict = {"model": it["model"], "cab": it.get("cab"), "steps": []}
        out[iid] = rec
        try:
            eng.whole_preset = "always"
            eng.load(frame)
            eng.whole_preset = "fallback"
            base = d / f"{iid}_base.bin"
            eng.bridge.call("backup", path=str(base))
            # Our documents, compiled offline onto this very base.
            for tag, vals, cvals in (("ours_default", defaults_of(cat, it["model"]),
                                      defaults_of(cat, it["cab"]) if it.get("cab") else None),
                                     ("ours_item", None, None)):
                hd = V.build_hlx(it, vals, cvals, name=f"robust {iid}")
                hp = d / f"{iid}_{tag}.hlx"
                hp.write_text(dumps_hlx(hd), encoding="utf-8")
                try:
                    rec[f"{tag}_report"] = compile_document(hp, base, d / f"{iid}_{tag}.bin")
                except Exception as e:  # noqa: BLE001
                    rec[f"{tag}_report"] = {"error": f"{type(e).__name__}: {e}"}
            # The device's own block: the model command (catalog defaults, as the device sets them).
            args = {"pos": 1, "model": it["model"]}
            if it.get("cab"):
                args["cab"] = it["cab"]
            t0 = time.perf_counter()
            r = eng.bridge.call("model", **args)
            rec["steps"].append({"model_cmd": r, "s": round(time.perf_counter() - t0, 3)})
            time.sleep(0.5)
            eng.bridge.call("backup", path=str(d / f"{iid}_device_default.bin"))
            ch = eng.bridge.call("chain")
            (d / f"{iid}_device_default_chain.json").write_text(json.dumps(ch, indent=1))
            # The item's values, in place, one at a time (a refused value shows itself here).
            sets = []
            for cab, vals in ((False, it["params"]), (True, it.get("cab_params") or {})):
                for name, v in vals.items():
                    try:
                        rr = eng.bridge.call("set", pos=1, param=name, value=float(v), paired=cab)
                        sets.append({"param": name, "cab": cab, "sent": v, "native": rr.get("native")})
                    except BridgeError as e:
                        sets.append({"param": name, "cab": cab, "sent": v, "error": str(e)})
            rec["sets"] = sets
            time.sleep(0.5)
            eng.bridge.call("backup", path=str(d / f"{iid}_device_item.bin"))
            (d / f"{iid}_device_item_chain.json").write_text(json.dumps(eng.bridge.call("chain"), indent=1))
            # Our documents written whole: does the device show them, and what does it hold after?
            for tag in ("ours_default", "ours_item"):
                p = d / f"{iid}_{tag}.bin"
                if not p.exists():
                    continue
                t0 = time.perf_counter()
                try:
                    eng.bridge.call("restore", path=str(p), timeout_s=30)
                    rec[f"{tag}_write"] = {"ok": True}
                except BridgeError as e:
                    rec[f"{tag}_write"] = {"ok": False, "error": str(e)}
                rec[f"{tag}_write"]["s"] = round(time.perf_counter() - t0, 3)
                time.sleep(0.5)
                eng.bridge.call("backup", path=str(d / f"{iid}_{tag}_after.bin"))
            eng._refresh()
            eng.preset = None
        except Exception as e:  # noqa: BLE001 - record, go on
            rec["error"] = f"{type(e).__name__}: {e}"
            try:
                eng._refresh()
            except Exception:  # noqa: BLE001, S110
                pass
            eng.preset = None
        print("refused", iid, it["model"], {k: v for k, v in rec.items() if k.endswith("_write")}, rec.get("error", ""),
              flush=True)


def probe_pilot(eng, cat: dict, dry: np.ndarray, d: Path, log: dict, n_fast: int, n_prod: int) -> None:
    """T112: the Path B loopback. Scream 808 single path vs with the bypassed pilot on Path B (SAB,
    USB 3/4); then raw captures at PRE_S 0.25 (many slips, T108) and 1.0 (production)."""
    from tonematch.engines.stomp import merge_two_path

    out = log.setdefault("pilot", {})
    a = V.item_preset(cat, S808, name="robust s808")
    piloted = merge_two_path(a, pilot_preset(cat))
    post = marker()
    caps = d / "caps"
    caps.mkdir(exist_ok=True)

    def take(tag: str, pre_s: float, with_post: bool = True) -> None:
        c = raw_capture(eng, dry, pre_s, post if with_post else None)
        np.save(caps / f"{tag}_a.npy", c["a"])
        if c["b"] is not None:
            np.save(caps / f"{tag}_b.npy", c["b"])
        out.setdefault("takes", []).append({"tag": tag, "pre_s": pre_s, "status": c["status"],
                                            "channels": c["channels"], "t": time.time()})

    eng.whole_preset = "always"
    eng.load(a)
    eng.whole_preset = "fallback"
    for k in range(3):
        take(f"single_{k}", 1.0)
    eng.whole_preset = "always"
    eng.load(piloted)
    eng.whole_preset = "fallback"
    out["piloted_readback"] = eng.last_readback.get("ok") if isinstance(eng.last_readback, dict) else None
    for k in range(3):
        take(f"pilot_{k}", 1.0)
    print("pilot: single and piloted takes done", flush=True)
    for k in range(n_fast):
        take(f"fast_{k:03d}", 0.25)
        if k % 25 == 0:
            print("pilot fast", k, flush=True)
    for k in range(n_prod):
        take(f"prod_{k:03d}", 1.0)
        if k % 25 == 0:
            print("pilot prod", k, flush=True)
    # The transparent chain (Scream 808 bypassed) with the pilot: Path A vs loopback latency.
    byp = merge_two_path(V.item_preset(cat, S808, enabled=False, name="robust byp"), pilot_preset(cat))
    eng.whole_preset = "always"
    eng.load(byp)
    eng.whole_preset = "fallback"
    for k in range(3):
        take(f"bypass_{k}", 1.0)


def marker(seconds: float = 0.03, dbfs_rms: float = -30.0, seed: int = 112) -> np.ndarray:
    """The post-clip marker: a short seeded noise burst after a gap of 8 ms. It reaches the pedal
    after the clip's last sample, so it cannot change any returned sample (causal; the trimmed
    output ends at the clip's last sample plus the alignment window)."""
    gap = np.zeros(int(0.008 * SR), np.float32)
    x = np.random.default_rng(seed).standard_normal(int(seconds * SR))
    x *= 10 ** (dbfs_rms / 20) / np.sqrt(np.mean(x ** 2))
    return np.concatenate([gap, x.astype(np.float32)])


def probe_glide(eng, cat: dict, its: dict, dry: np.ndarray, d: Path, log: dict) -> None:
    """T111: Echo Platter and Glitz. Whole write of the item's setting B (two renders, the floor);
    in place from the defaults (renders at once, after 5 s and after 15 s more); approach from the
    other side; one parameter at a time round trips from B; a whole write of B after an in-place
    change; Echo Platter Time sweep (in place from the default vs whole)."""
    out = log.setdefault("glide", {})
    rd = d / "glide"
    rd.mkdir(exist_ok=True)

    def render(tag: str) -> None:
        y = eng.render(dry)
        np.save(rd / f"{tag}.npy", y)
        out.setdefault("renders", []).append({"tag": tag, "wait_s": round(eng.last_wait_s, 3),
                                              "offset": eng.last_offset, "t": time.time()})

    def whole(it: dict, vals: dict | None = None) -> None:
        eng.whole_preset = "always"
        try:
            eng.load(V.item_preset(cat, it, vals))
        finally:
            eng.whole_preset = "fallback"

    def to_u(it: dict, vals: dict) -> dict:
        ps = cat["models"][it["model"]]["params"]
        res = {}
        for k, v in vals.items():
            p = ps[k]
            lo, hi = float(p["min"]), float(p["max"])
            res[(0, k)] = 0.0 if hi == lo else (float(v) - lo) / (hi - lo)
        return res

    for iid in GLIDE:
        it = its[iid]
        dfl = {k: v for k, v in defaults_of(cat, it["model"]).items() if k in it["params"]}
        tag = iid
        whole(it)
        render(f"{tag}_B_w1")
        render(f"{tag}_B_w2")
        # In place from the defaults (check 1 b of T108), then waits.
        whole(it, dfl)
        render(f"{tag}_D_w")
        eng.set_params(to_u(it, it["params"]))
        render(f"{tag}_inplace_0")
        time.sleep(5)
        render(f"{tag}_inplace_5")
        time.sleep(15)
        render(f"{tag}_inplace_20")
        # A whole write of B afterwards (the fix?).
        whole(it)
        render(f"{tag}_B_after_inplace")
        # One parameter at a time: from B, to its default and back, in place.
        for k in it["params"]:
            if k not in dfl or dfl[k] == it["params"][k]:
                continue
            whole(it)
            eng.set_params(to_u(it, {k: dfl[k]}))
            eng.set_params(to_u(it, {k: it["params"][k]}))
            render(f"{tag}_rt_{k.replace(' ', '_')}")
        # Approach from the other side for the time-like parameters.
        ps = cat["models"][it["model"]]["params"]
        for k in it["params"]:
            disp = (ps[k].get("display") or "").lower()
            if "time" not in disp or k not in dfl:
                continue
            lo, hi = float(ps[k]["min"]), float(ps[k]["max"])
            b = float(it["params"][k])
            # The side opposite the default (which the in-place test above came from).
            other = b + 0.5 * (min(hi, b + 0.5) - b) if dfl[k] < b else lo + 0.25 * (b - lo)
            whole(it, {**it["params"], k: other})
            eng.set_params(to_u(it, {k: it["params"][k]}))
            render(f"{tag}_from_{'above' if other > it['params'][k] else 'below'}_{k.replace(' ', '_')}")
        print("glide", iid, it["model"], "done", flush=True)
    # Echo Platter Time sweep: in place from the default Time vs whole.
    it = its["i21"]
    for t in (0.05, 0.2, 0.5, 1.0):
        vals = {**it["params"], "Time": t}
        whole(it, vals)
        render(f"ep_T{t}_whole")
        whole(it, {**vals, "Time": 0.225})
        eng.set_params(to_u(it, {"Time": t}))
        render(f"ep_T{t}_inplace")
        time.sleep(10)
        render(f"ep_T{t}_inplace_10")
    print("glide sweep done", flush=True)


def cmd_probe(a) -> int:
    import soundfile as sf

    from tonematch import catalog as C
    from tonematch import pedal_lock
    from tonematch.engines.stomp import StompEngine, restore_verdict

    if V.hx_edit_running():
        sys.exit("HX Edit is running: quit it first (nothing was sent to the pedal)")
    if not os.environ.get("TONEMATCH_STOMPD") and not shutil.which("stompd"):
        sys.exit("set TONEMATCH_STOMPD to the USB helper (cache/stomp/bin/stompd)")
    os.environ.setdefault("TONEMATCH_STOMPD_COMPILE", str(V.COMPILER))
    cat = C.load_catalog("current")
    its = items()
    dry, sr = sf.read(DI, dtype="float32")
    assert sr == SR
    d = STOMP / "hw" / f"{now_stamp()}-robust-probe"
    d.mkdir(parents=True, exist_ok=True)
    log: dict = {"errors": [], "parts": a.parts}
    t0 = time.perf_counter()
    parts = set(a.parts.split(","))
    if "refused" in parts and not a.allow_refused_writes:
        sys.exit("the 'refused' part writes documents the device refuses (the 20:48Z probe wedged the pedal "
                 "after 9 of them): pass --allow-refused-writes to run it anyway")
    with pedal_lock.session("stomp_robust probe (T110-T112)"):
        eng = StompEngine(whole_preset="fallback", session_dir=d, purpose="stomp_robust probe (T110-T112)",
                          sentinel=False)
        log.update(original=eng.original, firmware=eng.info.get("firmware"))
        stage = "open"
        try:
            if eng.original.get("name") != V.SCRATCH_NAME:
                raise SystemExit(f"the pedal is on {eng.original}, not 04C '{V.SCRATCH_NAME}': nothing written")
            for part, fn in (("refused", lambda: probe_refused(eng, cat, its, d, log)),
                             ("pilot", lambda: probe_pilot(eng, cat, dry, d, log, a.fast, a.prod)),
                             ("glide", lambda: probe_glide(eng, cat, its, dry, d, log))):
                if part not in parts:
                    continue
                stage = part
                try:
                    fn()
                    if part == "t112" and not (log.get("t112") or {}).get("pilot_on_pedal"):
                        eng.slip_guard = "off"   # no pilot on this pedal: the other parts run unguarded
                except Exception as e:  # noqa: BLE001 - one part failing does not stop the others
                    log["errors"].append(f"{part}: {type(e).__name__}: {e}")
                    print("ERROR", part, type(e).__name__, e, flush=True)
                (d / "probe.json").write_text(json.dumps(log, indent=1, default=str))
            stage = "done"
        except BaseException as e:  # noqa: BLE001 - always restore
            log["errors"].append(f"{stage}: {type(e).__name__}: {e}")
        finally:
            eng.close(verify_bytes=True)
            log["restore_report"] = eng.restore_report
            log["restore_ok"] = restore_verdict(eng.restore_report)
            log["session_close"] = getattr(eng.bridge, "release_report", None)
            log["wall_clock_s"] = round(time.perf_counter() - t0, 1)
            (d / "probe.json").write_text(json.dumps(log, indent=1, default=str))
    print(json.dumps({k: log[k] for k in ("errors", "restore_ok", "wall_clock_s")}, indent=1))
    print("probe:", d)
    return 0 if log["restore_ok"] else 1


# ============================================================================== session (registered rules)

# T112 rules (docs/DECISIONS.md, "T112 ... registered before any run"); constants implement them.
TRUTH_NULL_MARGIN_DB = 20.0
TRUTH_NULL_MAX_DB = -100.0
TRUTH_WINDOW_DB = -80.0
REF_NULL_DB = -100.0
REF_TRIES = 6
D_PRE_S, D_TAKES = 0.25, 150
P_PRE_S, P_RENDERS = 1.0, 300
THETA_MARGIN_DB, THETA_CAP_DB = 20.0, -40.0
FALSE_RETAKE_MAX = 0.02
GLIDE_ITEMS = ("i21", "i23", "i16")           # Echo Platter, Glitz, Auto-Vol Echo
WITHPAN = "HD2_CabMicIr_4x12Greenback30WithPan"
PAIR_TESTS = (("HD2_AmpMoonNrm", WITHPAN), ("HD2_AmpMoonNrm", "HD2_CabMicIr_4x12Greenback30"),
              ("HD2_AmpEssexA15", WITHPAN), ("HD2_AmpEssexA15", "HD2_CabMicIr_4x12Greenback30"))


def load_ok() -> float:
    """The 1-minute load average (the human's machine load rule, docs/DECISIONS.md 2026-10-17)."""
    return os.getloadavg()[0]


def wait_for_load(limit: float = 7.0, poll_s: float = 60.0, log=print) -> None:
    while load_ok() >= limit:
        log(f"load {load_ok():.1f} >= {limit}: waiting {poll_s:.0f} s", flush=True)
        time.sleep(poll_s)


def windowed_rel_db(ref: np.ndarray, y: np.ndarray, win: int = 120, floor_rel_db: float = -30.0) -> float:
    """Worst window residual of y against ref, as slip_check scores the loopback. Pure."""
    a, b = np.asarray(ref, np.float64), np.asarray(y, np.float64)
    n = min(len(a), len(b))
    k = n // win
    er = np.sum(((a[:k * win] - b[:k * win]) ** 2).reshape(k, win), axis=1)
    ex = np.sum((a[:k * win] ** 2).reshape(k, win), axis=1)
    refe = np.maximum(ex, float(np.mean(ex)) * 10 ** (floor_rel_db / 10))
    return float(np.max(10 * np.log10(np.maximum(er, 1e-30) / np.maximum(refe, 1e-30))))


def truth(ref: np.ndarray, y: np.ndarray, floor_db: float) -> dict:
    """The registered truth for one take: slipped when null(R, y) > max(F + 20, -100) dB or a 2.5 ms
    window exceeds -80 dB. Pure."""
    from tonematch import reamp

    nul = float(reamp.null_db(ref, y))
    w = windowed_rel_db(ref, y)
    lim = max(floor_db + TRUTH_NULL_MARGIN_DB, TRUTH_NULL_MAX_DB)
    return {"null_db": round(nul, 2), "window_db": round(w, 2), "slipped": bool(nul > lim or w > TRUTH_WINDOW_DB),
            "null_limit_db": round(lim, 2)}


def theta_from(dev: list[dict]) -> dict:
    """Phase D -> the pass-run threshold: max worst_db over truth-clean takes + 20 dB, capped at -40 dB.
    Pure. Reports how the dev slips sit against it."""
    clean = [t["worst_db"] for t in dev if not t["truth"]["slipped"] and t["worst_db"] is not None]
    slipped = [t["worst_db"] for t in dev if t["truth"]["slipped"] and t["worst_db"] is not None]
    if not clean:
        return {"theta_db": None, "reason": "no clean development take"}
    th = min(max(clean) + THETA_MARGIN_DB, THETA_CAP_DB)
    return {"theta_db": round(th, 1), "max_clean_db": max(clean), "n_clean": len(clean), "n_slipped": len(slipped),
            "min_slipped_db": min(slipped) if slipped else None,
            "dev_missed": sum(1 for s in slipped if s <= th), "dev_false": sum(1 for c in clean if c > th)}


def s808_preset(cat: dict):
    return V.item_preset(cat, S808, name="robust s808")


def save_takes(eng, d: Path, tag: str, start: int) -> list[dict]:
    out = []
    for k, tk in enumerate(eng.takes[start:]):
        name = f"{tag}_{start + k:04d}"
        np.save(d / f"{name}.npy", tk["y"])
        v = dict(tk["verdict"])
        out.append({"file": name, "render": tk["render"], "take": tk["take"], "worst_db": v.get("worst_db"),
                    "lag": v.get("lag"), "gain_db": v.get("gain_db"), "ok": v.get("ok"),
                    "worst_at_s": v.get("worst_at_s"), "offset": tk.get("offset")})
    return out


def phase_t110(eng, cat: dict, its: dict, d: Path, log: dict) -> None:
    """Templates for the three models, the structural gate, then gated whole loads and two renders;
    the Amp+Cab pairing tests use the model command only (no document is written for them)."""
    import soundfile as sf

    from tonematch.engines import stomp as ST

    out = log.setdefault("t110", {"items": {}, "pairs": {}})
    dry, _ = sf.read(DI, dtype="float32")
    for model, cab in PAIR_TESTS:
        k = ST.template_key(model, cab)
        try:
            got = eng.capture_templates([(model, cab)])
            out["pairs"][k] = {"refused": False, "template": got.get(k)}
        except ST.ModelRefused as e:
            out["pairs"][k] = {"refused": True, "error": str(e)}
        print("t110 pair", k, out["pairs"][k], flush=True)
    for iid in ("i17", "i18", "i26"):
        it = its[iid]
        rec: dict = {"model": it["model"]}
        out["items"][iid] = rec
        try:
            pv = V.item_preset(cat, it, name=f"robust {iid}")
            need = eng.needed_templates(pv)
            if need:
                eng.capture_templates(need)
            rec["template"] = eng._template_index()["models"].get(ST.template_key(it["model"], None))
            gate = structural_gate(eng, pv, d / f"{iid}_gate")
            rec["gate"] = gate
            if not gate["ok"]:
                rec["skipped"] = "structural gate failed: not written"
                continue
            eng.whole_preset = "always"
            try:
                eng.load(pv)
            finally:
                eng.whole_preset = "fallback"
            rec["landed"] = True
            rec["readback"] = {k: eng.last_readback.get(k) for k in ("ok", "checked", "mismatches", "missing")}
            rec["pilot"] = eng._pilot_on_pedal
            ys = [eng.render(dry) for _ in range(2)]
            for j, y in enumerate(ys):
                np.save(d / f"{iid}_r{j + 1}.npy", y)
            from tonematch import reamp

            rec["repeat_null_db"] = round(float(reamp.null_db(ys[0], ys[1])), 2)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            if "timed out" in str(e) or "session was lost" in str(e):
                raise
        print("t110", iid, {k: rec.get(k) for k in ("landed", "readback", "repeat_null_db", "error", "skipped")},
              flush=True)
    # Moon Nrm with the same cab without the pan (the item's cab values minus Pan and Delay).
    it = dict(its["i29"])
    it["cab"] = "HD2_CabMicIr_4x12Greenback30"
    it["cab_params"] = {k: v for k, v in it["cab_params"].items() if k not in ("Pan", "Delay")}
    rec = out["items"]["i29_nopan"] = {"model": it["model"], "cab": it["cab"]}
    try:
        pv = V.item_preset(cat, it, name="robust i29np")
        need = eng.needed_templates(pv)
        if need:
            eng.capture_templates(need)
        gate = structural_gate(eng, pv, d / "i29np_gate")
        rec["gate"] = gate
        if gate["ok"]:
            eng.whole_preset = "always"
            try:
                eng.load(pv)
            finally:
                eng.whole_preset = "fallback"
            rec["landed"] = True
            rec["readback"] = {k: eng.last_readback.get(k) for k in ("ok", "checked", "mismatches", "missing")}
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        if "timed out" in str(e) or "session was lost" in str(e):
            raise
    print("t110 i29 without pan", rec, flush=True)


def _slot_struct(dump: str, number: int) -> tuple | None:
    """(class, n, n2) of the first block slot of model ``number`` in a ``stompd dump .. 0 22`` text."""
    import re

    m = re.search(r"\{19: 6, 20: \{24: \{23: (?:true|false), 25: " + str(number) + r", 26: -?\d+\}, 9: (\d+), 10: "
                  r"(?:true|false), 11: \{2: (\d+), 3: (\d+)", dump)
    return tuple(int(g) for g in m.groups()) if m else None


def structural_gate(eng, preset, d: Path) -> dict:
    """Compile ``preset`` onto a backup of the current buffer (with the device templates) and check
    that every built slot has the class and value counts of the device's own slot (its template).
    Nothing is written."""
    import subprocess

    from tonematch.engines import stomp as ST
    from tonematch.preset import dumps_hlx

    d.mkdir(parents=True, exist_ok=True)
    base = d / "base.bin"
    eng.bridge.call("backup", path=str(base))
    target, _ = eng._pilot_target(preset)
    hd = ST.reamp_hlx_dict(target, eng.cal.input_usb56)
    (d / "c.hlx").write_text(dumps_hlx(hd), encoding="utf-8")
    rep = ST.compile_document(d / "c.hlx", base, d / "c.bin", templates=eng._template_dir())
    exe = os.environ.get("TONEMATCH_STOMPD_COMPILE", str(V.COMPILER))
    ours = subprocess.run([exe, "dump", str(d / "c.bin"), "0", "22"], capture_output=True, text=True, check=False).stdout
    res = {"ok": True, "slots": []}
    for s in rep.get("slots", []):
        if s.get("mode") not in ("built", "template"):
            continue
        ent = {"slot": s["slot"], "model": s.get("model"), "mode": s["mode"]}
        if s["mode"] == "built":
            ent["ok"] = False
            ent["why"] = "built from the catalog (no device template)"
        else:
            tpl = subprocess.run([exe, "dump", str(eng._template_dir() / s["template"]), "0", "22"],
                                 capture_output=True, text=True, check=False).stdout
            a, b = _slot_struct(ours, s["number"]), _slot_struct(tpl, s["number"])
            ent.update(ours=a, device=b, ok=a is not None and a == b)
        res["slots"].append(ent)
        res["ok"] = res["ok"] and ent["ok"]
    # The pilot is a known-good catalog build (T108); only the item's own slots must be templates.
    res["ok"] = all(e["ok"] for e in res["slots"] if e["model"] and not e["model"].startswith(ST.PILOT_MODEL))
    return res


def phase_t112(eng, cat: dict, dry: np.ndarray, d: Path, log: dict) -> None:
    """Pilot check, development phase D (threshold), pass phase P. See the registered rules."""
    from tonematch import reamp
    from tonematch.engines import stomp as ST

    out = log.setdefault("t112", {})
    td = d / "takes"
    td.mkdir(exist_ok=True)
    s808 = s808_preset(cat)
    # Single path, guard off, three renders (Path A without the pilot).
    eng.slip_guard = "off"
    eng.whole_preset = "always"
    try:
        eng.load(s808)
    finally:
        eng.whole_preset = "fallback"
    singles = [eng.render(dry) for _ in range(3)]
    for j, y in enumerate(singles):
        np.save(d / f"single_{j}.npy", y)
    eng.slip_guard = "on"
    eng.whole_preset = "always"
    try:
        eng.load(s808)
    finally:
        eng.whole_preset = "fallback"
    out["pilot_on_pedal"] = eng._pilot_on_pedal
    out["pilot_reason"] = eng.pilot_reason
    if not eng._pilot_on_pedal:
        out["skipped"] = "the pilot layout did not land: T112 not scored"
        return
    eng.keep_takes = True
    # Reference R and floor F (PRE_S 1.0).
    eng.slip_threshold_db = 100.0
    eng.slip_retakes = 0
    refs = []
    ref, floor = None, None
    for _ in range(REF_TRIES):
        refs.append(eng.render(dry))
        if len(refs) >= 2:
            f = float(reamp.null_db(refs[-2], refs[-1]))
            if f <= REF_NULL_DB:
                ref, floor = refs[-2], f
                break
    for j, y in enumerate(refs):
        np.save(d / f"ref_try_{j}.npy", y)
    if ref is None:
        out["skipped"] = f"no two opening renders agree within {REF_NULL_DB} dB: T112 not scored"
        return
    np.save(d / "R.npy", ref)
    out["floor_db"] = round(floor, 2)
    out["pilot_vs_single_null_db"] = round(float(reamp.null_db(singles[0], ref)), 2)
    out["single_repeat_null_db"] = round(float(reamp.null_db(singles[0], singles[1])), 2)
    print("t112 floor", out["floor_db"], "pilot vs single", out["pilot_vs_single_null_db"], flush=True)
    # Phase D: PRE_S 0.25, every take scored by the detector with no threshold, no re-takes.
    old_pre = eng.PRE_S
    eng.PRE_S = D_PRE_S
    n0 = len(eng.takes)
    t0 = time.perf_counter()
    try:
        for k in range(D_TAKES):
            eng.render(dry)
            if k % 25 == 0:
                print("t112 D", k, flush=True)
    finally:
        eng.PRE_S = old_pre
    dev = save_takes(eng, td, "D", n0)
    for t in dev:
        t["truth"] = truth(ref, np.load(td / f"{t['file']}.npy"), floor)
    out["D"] = {"takes": dev, "seconds": round(time.perf_counter() - t0, 1)}
    th = theta_from(dev)
    out["theta"] = th
    print("t112 theta", th, flush=True)
    if th["theta_db"] is None:
        out["skipped"] = "no threshold"
        return
    # Phase P: production pre-roll, threshold theta, up to 3 re-takes, 300 returned renders.
    eng.slip_threshold_db = th["theta_db"]
    eng.slip_retakes = ST.SLIP_RETAKES
    n0 = len(eng.takes)
    st0 = dict(eng.slip_stats)
    failed = 0
    returned = []
    t0 = time.perf_counter()
    while len(returned) + failed < P_RENDERS:
        try:
            eng.render(dry)
            returned.append({"index": len(returned) + failed, "take_idx": len(eng.takes) - 1})
        except ST.StreamSlip as e:
            failed += 1
            print("t112 P StreamSlip", e, flush=True)
        if (len(returned) + failed) % 25 == 0:
            print("t112 P", len(returned) + failed, "retakes", eng.slip_stats["retakes"] - st0["retakes"], flush=True)
    secs = time.perf_counter() - t0
    takes = save_takes(eng, td, "P", n0)
    for t in takes:
        t["truth"] = truth(ref, np.load(td / f"{t['file']}.npy"), floor)
    out["P"] = {"takes": takes, "failed": failed, "returned": len(returned), "seconds": round(secs, 1),
                "seconds_per_render": round(secs / max(1, len(returned) + failed), 3),
                "stats": eng.stats()["slip_guard"]}
    eng.keep_takes = False


def phase_t111(eng, cat: dict, its: dict, dry: np.ndarray, d: Path, log: dict) -> None:
    """The T108 check-1 sequence for Echo Platter, Glitz and Auto-Vol Echo, with document backups
    after each step: (a) whole write of B, renders r1 r2; (b) the same model at catalog defaults in
    place, then set_params to B, render y_in; (c) 10 s later y_in10. First with in-place changes
    (the T108 behaviour, guard off, single path), then with the engine policy (guard on)."""
    from tonematch import reamp

    out = log.setdefault("t111", {})
    gd = d / "glide"
    gd.mkdir(exist_ok=True)
    for policy in ("inplace", "policy"):
        eng.ambient_inplace = policy == "inplace"
        eng.slip_guard = "off" if policy == "inplace" else "on"
        for iid in GLIDE_ITEMS:
            it = its[iid]
            tag = f"{policy}_{iid}"
            rec = out.setdefault(tag, {"model": it["model"]})
            try:
                pv = V.item_preset(cat, it)
                eng.whole_preset = "always"
                try:
                    eng.load(pv)
                finally:
                    eng.whole_preset = "fallback"
                eng.bridge.call("backup", path=str(gd / f"{tag}_a.bin"))
                r1, r2 = eng.render(dry), eng.render(dry)
                eng._refresh()
                from tonematch.engines.stomp import _block_slots, _param_meta

                dev = _block_slots(eng.chain).get(1) or {}
                meta = _param_meta(dev.get("params") or [])
                addressable = [n for n in it["params"] if n in meta and meta[n].get("min") is not None]
                models = cat["models"][it["model"]]["params"]
                vals = dict(it["params"])
                for n in addressable:
                    vals[n] = models[n]["default"]
                nw = len(eng.whole_writes)
                eng.load(V.item_preset(cat, it, vals))
                rec["defaults_route"] = "whole" if len(eng.whole_writes) > nw else "in place"
                eng.bridge.call("backup", path=str(gd / f"{tag}_b_defaults.bin"))
                nw = len(eng.whole_writes)
                eng.set_params({(0, n): it["u"][n] for n in addressable})
                rec["set_route"] = "whole" if len(eng.whole_writes) > nw else "in place"
                eng.bridge.call("backup", path=str(gd / f"{tag}_b_set.bin"))
                y_in = eng.render(dry)
                time.sleep(10)
                y_in10 = eng.render(dry)
                for nm, y in (("r1", r1), ("r2", r2), ("in", y_in), ("in10", y_in10)):
                    np.save(gd / f"{tag}_{nm}.npy", y)
                rec.update(floor_db=round(float(reamp.null_db(r1, r2)), 2),
                           in_db=round(float(reamp.null_db(r1, y_in)), 2),
                           in10_db=round(float(reamp.null_db(r1, y_in10)), 2))
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"
                if "timed out" in str(e) or "session was lost" in str(e):
                    raise
            print("t111", tag, rec, flush=True)
    eng.ambient_inplace = False
    eng.slip_guard = "on"


def cmd_session(a) -> int:
    import soundfile as sf

    from tonematch import catalog as C
    from tonematch import pedal_lock
    from tonematch.engines.stomp import StompEngine, restore_verdict

    if V.hx_edit_running():
        sys.exit("HX Edit is running: quit it first (nothing was sent to the pedal)")
    if not os.environ.get("TONEMATCH_STOMPD") and not shutil.which("stompd"):
        sys.exit("set TONEMATCH_STOMPD to the USB helper (cache/stomp/bin/stompd)")
    os.environ.setdefault("TONEMATCH_STOMPD_COMPILE", str(V.COMPILER))
    wait_for_load()
    cat = C.load_catalog("current")
    its = items()
    dry, sr = sf.read(DI, dtype="float32")
    assert sr == SR
    d = STOMP / "hw" / f"{now_stamp()}-robust"
    d.mkdir(parents=True, exist_ok=True)
    log: dict = {"errors": [], "parts": a.parts, "load_at_start": os.getloadavg()}
    parts = a.parts.split(",")
    t0 = time.perf_counter()

    def save():
        (d / "session.json").write_text(json.dumps(log, indent=1, default=str))

    with pedal_lock.session("stomp_robust session (T110-T112)"):
        eng = StompEngine(whole_preset="fallback", session_dir=d, purpose="stomp_robust session (T110-T112)",
                          sentinel=False, slip_guard="on")
        log.update(original=eng.original, firmware=eng.info.get("firmware"), latency=eng.cal.latency_samples)
        try:
            if eng.original.get("name") != V.SCRATCH_NAME:
                raise SystemExit(f"the pedal is on {eng.original}, not 04C '{V.SCRATCH_NAME}': nothing written")
            for part in parts:
                fn = {"t112": lambda: phase_t112(eng, cat, dry, d, log),
                      "t110": lambda: phase_t110(eng, cat, its, d, log),
                      "t111": lambda: phase_t111(eng, cat, its, dry, d, log)}[part]
                try:
                    fn()
                    if part == "t112" and not (log.get("t112") or {}).get("pilot_on_pedal"):
                        eng.slip_guard = "off"   # no pilot on this pedal: the other parts run unguarded
                except Exception as e:  # noqa: BLE001
                    log["errors"].append(f"{part}: {type(e).__name__}: {e}")
                    print("ERROR", part, type(e).__name__, e, flush=True)
                    if "timed out" in str(e) or "session was lost" in str(e):
                        log["errors"].append("the pedal stopped answering: session stopped")
                        break
                save()
        except BaseException as e:  # noqa: BLE001 - always restore
            log["errors"].append(f"{type(e).__name__}: {e}")
        finally:
            log["engine_stats"] = eng.stats()
            eng.close(verify_bytes=True)
            log["restore_report"] = eng.restore_report
            log["restore_ok"] = restore_verdict(eng.restore_report)
            log["session_close"] = getattr(eng.bridge, "release_report", None)
            log["wall_clock_s"] = round(time.perf_counter() - t0, 1)
            save()
    print(json.dumps({k: log.get(k) for k in ("errors", "restore_ok", "wall_clock_s")}, indent=1))
    print("session:", d)
    return 0 if log["restore_ok"] else 1


# ============================================================================== analyse / ledger


def analyse(d: Path) -> dict:
    s = json.loads((d / "session.json").read_text())
    v: dict = {"dir": str(d), "restore_ok": s.get("restore_ok"), "errors": s.get("errors")}
    t = s.get("t112") or {}
    if t.get("P"):
        P, D = t["P"], t["D"]
        renders = P["returned"] + P["failed"]
        kept = [x for x in P["takes"] if x["ok"]]
        discarded = [x for x in P["takes"] if not x["ok"]]
        escapes = sum(1 for x in kept if x["truth"]["slipped"]) + P["failed"]
        false_retakes = sum(1 for x in discarded if not x["truth"]["slipped"])
        allt = D["takes"] + P["takes"]
        th = t["theta"]["theta_db"]
        flagged = [x for x in allt if x["worst_db"] is not None and x["worst_db"] > th]
        fn = sum(1 for x in allt if x["truth"]["slipped"] and not (x["worst_db"] is not None and x["worst_db"] > th))
        fp = sum(1 for x in flagged if not x["truth"]["slipped"])
        n_sl = sum(1 for x in allt if x["truth"]["slipped"])
        v["t112"] = {"renders": renders, "escapes": escapes, "failed": P["failed"], "false_retakes": false_retakes,
                     "false_retake_rate": round(false_retakes / max(1, renders), 4),
                     "retakes": len(discarded), "theta_db": th, "floor_db": t.get("floor_db"),
                     "pilot_vs_single_null_db": t.get("pilot_vs_single_null_db"),
                     "single_repeat_null_db": t.get("single_repeat_null_db"),
                     "seconds_per_render": P.get("seconds_per_render"),
                     "takes_all": len(allt), "truth_slipped_all": n_sl, "fn": fn, "fp": fp,
                     "fn_rate": round(fn / max(1, n_sl), 4), "fp_rate": round(fp / max(1, len(allt) - n_sl), 4),
                     "dev": {k: v_ for k, v_ in t["theta"].items()},
                     "truth_slipped_P_takes": sum(1 for x in P["takes"] if x["truth"]["slipped"]),
                     "truth_slipped_D_takes": sum(1 for x in D["takes"] if x["truth"]["slipped"])}
        v["t112"]["pass"] = renders >= P_RENDERS and escapes == 0 and v["t112"]["false_retake_rate"] < FALSE_RETAKE_MAX
    else:
        v["t112"] = {"pass": False, "skipped": t.get("skipped") or "not run"}
    v["t110"] = s.get("t110")
    t111 = s.get("t111") or {}
    from tonematch.engines import stomp as ST

    for tag, rec in t111.items():
        a, b = d / "glide" / f"{tag}_a.bin", d / "glide" / f"{tag}_b_set.bin"
        if a.exists() and b.exists():
            try:
                rec["doc_difference"] = ST._first_difference(ST.decode_document(a), ST.decode_document(b))
            except Exception as e:  # noqa: BLE001 - diagnostic
                rec["doc_difference"] = f"decode failed: {e}"
        if "in_db" in rec and "floor_db" in rec:
            rec["pass"] = rec["in_db"] <= rec["floor_db"] + 3.0
    v["t111"] = t111
    v["t111_policy_pass"] = bool(t111) and all(r.get("pass") for k, r in t111.items() if k.startswith("policy_"))
    (d / "verdict.json").write_text(json.dumps(v, indent=1, default=str))
    return v


def cmd_analyse(a) -> int:
    v = analyse(Path(a.dir))
    print(json.dumps({k: v[k] for k in ("restore_ok", "errors", "t112")}, indent=1, default=str))
    return 0


def cmd_ledger(a) -> int:
    import subprocess

    d = Path(a.dir)
    v = json.loads((d / "verdict.json").read_text())

    def row(kind, dataset, metric, value, renders=None, notes=""):
        args = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", kind, "--engine", "stomp",
                "--dataset", dataset, "--metric", metric, "--value", str(value), "--notes", notes]
        if renders is not None:
            args += ["--renders", str(renders)]
        subprocess.run(args, check=True)

    t = v.get("t112") or {}
    base = f"{d.name}; rules DECISIONS 2026-10-17 T112; restore_ok {v.get('restore_ok')}"
    if "escapes" in t:
        row("smoke", "stomp-slip-v1", "slip_escapes", t["escapes"], t["renders"],
            f"{base}; returned renders truth-slipped + StreamSlip failures; theta {t['theta_db']} dB; pass {t['pass']}")
        row("smoke", "stomp-slip-v1", "false_retake_rate", t["false_retake_rate"], t["renders"],
            f"{base}; {t['false_retakes']} of {t['retakes']} re-takes truth-clean; bar < 0.02")
        row("smoke", "stomp-slip-v1", "detector_fn_rate", t["fn_rate"], t["takes_all"],
            f"{base}; {t['fn']} of {t['truth_slipped_all']} truth-slipped takes not flagged (phases D+P)")
        row("smoke", "stomp-slip-v1", "detector_fp_rate", t["fp_rate"], t["takes_all"],
            f"{base}; {t['fp']} clean takes flagged (phases D+P)")
        row("parity", "stomp-slip-v1", "pilot_vs_single_null_db", t["pilot_vs_single_null_db"], None,
            f"{base}; Scream 808 Path A with the bypassed pilot on Path B vs single path; single repeat "
            f"{t['single_repeat_null_db']}")
        row("render-cost", "stomp-slip-v1", "seconds_per_render", t["seconds_per_render"], t["renders"],
            f"{base}; 2 s clip; guarded; PRE_S 1.0; incl. re-takes")
    t110 = v.get("t110") or {}
    for iid, rec in (t110.get("items") or {}).items():
        ok = bool(rec.get("landed") and (rec.get("readback") or {}).get("ok"))
        row("smoke", "stomp-refused-v1", f"landed_{iid}", int(ok), 2 if ok else None,
            f"{base}; {rec.get('model')} {rec.get('cab') or ''}; template {bool(rec.get('template'))}; "
            f"repeat null {rec.get('repeat_null_db')}; {rec.get('error') or rec.get('skipped') or ''}")
    for k, rec in (t110.get("pairs") or {}).items():
        row("smoke", "stomp-refused-v1", "model_cmd_refused", int(bool(rec.get("refused"))), None,
            f"{base}; {k.replace('|', ' + ')} (model command only; no document written)")
    for tag, rec in (v.get("t111") or {}).items():
        if "in_db" in rec:
            row("parity", "stomp-glide-v1", f"inplace_null_db_{tag}", rec["in_db"], 4,
                f"{base}; {rec.get('model')}; floor {rec.get('floor_db')}; 10 s later {rec.get('in10_db')}; "
                f"routes {rec.get('defaults_route')}/{rec.get('set_route')}; pass {rec.get('pass')}; "
                f"doc diff {str(rec.get('doc_difference'))[:80]}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--parts", default="refused,pilot,glide")
    p.add_argument("--fast", type=int, default=150, help="raw slip captures at PRE_S 0.25")
    p.add_argument("--prod", type=int, default=60, help="raw slip captures at PRE_S 1.0")
    p.add_argument("--allow-refused-writes", action="store_true")
    p.set_defaults(fn=cmd_probe)
    s = sub.add_parser("session")
    s.add_argument("--parts", default="t112,t110,t111")
    s.set_defaults(fn=cmd_session)
    for name, fn in (("analyse", cmd_analyse), ("ledger", cmd_ledger)):
        q = sub.add_parser(name)
        q.add_argument("dir")
        q.set_defaults(fn=fn)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
