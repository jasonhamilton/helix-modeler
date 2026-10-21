#!/usr/bin/env python3
"""T197: the DS-Sample reference chain from the human's tone tutorial, on the HX Stomp.

  build    offline: the chain as an .hlx (catalog read at runtime), validated (hlx-validate:
           block limit, ranges, DSP budget) -> cache/matches/DS-Sample-live/reference-chain/
  run      pedal: pre-checks (HX Edit closed, pedal on 04C), shared pedal lock, whole load into the
           04C EDIT BUFFER (never flash), full readback, reamp take2 (song 0:35-0:45) with the Input
           on USB 5/6 and the calibrated latency, listening pack; then the Input goes back to
           Main L/R and the chain STAYS in the edit buffer for live playing. Lock released, no restore.
           The session's backup and chain record go to <run dir>/session.json for `restore`.
  restore  pedal: re-select 04C, read the chain back and compare it to the chain recorded at open;
           step away and back if needed; write the open-time backup into the edit buffer only if it
           still differs; back the buffer up again and decode-compare (StompEngine.close(verify_bytes)).

Song audio stays local (cache/ is gitignored).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "cache" / "matches" / "DS-Sample-live" / "reference-chain"
HLX = OUT / "reference_chain.hlx"
SESSION_RECORD = OUT / "session.json"
TAKE = ROOT / "data" / "songs" / "ds-sample-playalong" / "takes" / "20261020-130031_take2_dry.wav"
ALIGN = TAKE.with_name("20261020-130031_take2_align.json")
STEM = ROOT / "data" / "songs" / "ds-sample-playalong" / "target_guitar_stem.wav"
STEM_SONG_S = 30.0          # the stem excerpt starts at song 30 s
SONG_WINDOW = (30.1, 50.0)  # what the human listens to (extended to the final lick, human 2026-10-20)
TAIL_S = 2.0                # silence after the DI so the reverb tail is recorded (trimmed from the pack)
SCRATCH = (0, 11, "Smoke Known")
INPUT_MAIN_LR = 1
SR = 48_000

# (model, values). Knob values are the catalog's 0..1 range unless noted; the 0-10 tutorial scale is /10.
# Scream 808 Level: "2 o'clock" on a 7-o'clock-to-5-o'clock sweep = 7/10 of the travel = 0.70.
CHAIN = [
    ("HD2_DistScream808", {"Gain": 0.50, "Tone": 0.50, "Level": 0.70}),
    ("HD2_AmpBrit2204", {"Drive": 0.30, "Bass": 0.70, "Mid": 0.50, "Treble": 0.40, "ChVol": 0.60, "Master": 0.70}),
    ("HD2_Cab4x12Greenback25", {}),                                  # catalog default mic settings
    ("HD2_DelaySimpleDelay", {"Time": 0.026, "TempoSync1": False, "Feedback": 0.0, "Mix": 0.50}),
    ("HD2_EQGraphic10Band", {"62p5Hz": 1.25, "125Hz": 3.75, "250Hz": 2.25, "8kHz": -1.55, "16kHz": -3.0}),
]
# Hall Decay is a 0..1 knob with no seconds scale in the catalog. The only measurement on record
# (T060 parity spot, this pedal, fw 3.80): Decay 0.68 -> RT60 ~2.3 s (T30, 250 Hz-2 kHz). Scaled
# proportionally (an assumption): 1.75 s -> 0.68 * 1.75 / 2.3 = 0.52.
HALL = ("HD2_ReverbHall", {"Decay": 0.52, "Mix": 0.40})


def build(with_hall: bool = True):
    from tonematch import preset as P
    from tonematch.catalog import load_catalog
    from tonematch.engines.native_library import base_preset, single_block

    cat = load_catalog()
    base = base_preset()
    blocks = CHAIN + ([HALL] if with_hall else [])
    slots, pos, last_amp = [], 0, None
    for model, vals in blocks:
        s = copy.deepcopy(single_block(model, cat, base).slots[0])
        for k, v in vals.items():
            if k not in s.params:
                raise SystemExit(f"{model}: no parameter {k!r} in the catalog")
            s.params[k] = v
        s.enabled = True
        s.extra["@path"] = 0
        if s.category == "cab":
            s.key, s.position = "cab0", None
            last_amp.extra["@cab"] = "cab0"
        else:
            s.key, s.position = f"block{pos}", pos
            pos += 1
            if s.category == "amp":
                last_amp = s
        slots.append(s)
    p = copy.deepcopy(base)
    p.name, p.slots, p.snapshots = "DS Ref Chain", slots, []
    tone = p.raw["data"]["tone"]
    for k in ("snapshot0", "snapshot1", "snapshot2"):   # every block on in every snapshot (base had K's)
        if isinstance(tone.get(k), dict):
            tone[k]["blocks"] = {"dsp0": {s.key: True for s in slots if s.position is not None}}
    tone["dsp0"]["inputA"]["@input"] = INPUT_MAIN_LR    # the file is a guitar-input preset; loads set it anyway
    return p, cat


def dsp_report(p, cat) -> dict:
    from tonematch import preset as P

    f = ROOT / "cache" / "catalog" / "dsp-costs.json"
    costs = json.loads(f.read_text()) if f.is_file() else P.build_dsp_costs(cat)
    total, unknown = P.dsp_total(p, costs)
    viol = P.validate(p, cat, costs, engine="stomp")
    return {"dsp_total": round(float(total), 2), "budget": costs.get("_budget") if isinstance(costs, dict) else None,
            "unknown": unknown, "fails": [str(v) for v in viol if v.severity == "fail"],
            "warns": [str(v) for v in viol if v.severity != "fail"]}


def cmd_build(a) -> int:
    from tonematch import preset as P

    OUT.mkdir(parents=True, exist_ok=True)
    p, cat = build(with_hall=True)
    rep = dsp_report(p, cat)
    hall = True
    if rep["fails"]:
        print("with Hall:", json.dumps(rep, indent=1))
        p, cat = build(with_hall=False)
        rep, hall = dsp_report(p, cat), False
    P.write_hlx(p, HLX)
    rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "hlx_validate.py"), str(HLX)], cwd=ROOT).returncode
    rep.update(hall_included=hall, hlx=str(HLX.relative_to(ROOT)), hlx_validate_exit=rc)
    (OUT / "build.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))
    return 0 if rc == 0 else 1


# ------------------------------------------------------------------------------- audio helpers


def excerpts():
    import soundfile as sf

    take, sr = sf.read(TAKE, dtype="float32")
    stem, sr2 = sf.read(STEM, dtype="float32", always_2d=True)
    assert sr == sr2 == SR
    t0 = json.loads(ALIGN.read_text())["alignment"]["take_sample0_song_s"]
    a, b = (round((s - t0) * SR) for s in SONG_WINDOW)
    di = take[a:b]
    n = round((SONG_WINDOW[1] - SONG_WINDOW[0]) * SR)
    if len(di) < n:
        di = np.pad(di, (0, n - len(di)))
    sa, sb = (round((s - STEM_SONG_S) * SR) for s in SONG_WINDOW)
    target = stem[sa:sb].mean(axis=1).astype(np.float32)
    return di.astype(np.float32), target, {"take_sample0_song_s": t0, "take_samples": [a, b],
                                           "stem_samples": [sa, sb]}


def write_pack(target: np.ndarray, ref: np.ndarray, di: np.ndarray) -> dict:
    import soundfile as sf

    def rms(x):
        return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)) + 1e-12)
    n = min(len(target), len(ref))
    target, ref = target[:n], ref[:n]
    g = rms(target) / rms(ref)
    refm = (ref * g).astype(np.float32)
    peak = max(float(np.max(np.abs(target))), float(np.max(np.abs(refm))))
    trim = min(1.0, 0.98 / peak)   # same scale on both, so the match holds and nothing clips
    tm, rm = (target * trim).astype(np.float32), (refm * trim).astype(np.float32)
    sf.write(OUT / "target_stem.wav", tm, SR, subtype="PCM_24")
    sf.write(OUT / "reference.wav", rm, SR, subtype="PCM_24")
    sf.write(OUT / "ab_L-target_R-reference.wav", np.stack([tm, rm], axis=1), SR, subtype="PCM_24")
    sf.write(OUT / "di_dry.wav", di[:n], SR, subtype="FLOAT")   # true level, as reamped
    info = {"samples": n, "gains_db": {"target_stem": round(20 * np.log10(trim), 2),
                                       "reference": round(20 * np.log10(g * trim), 2)},
            "match": "RMS to the target (same rule as match_block listen packs)",
            "note": "level-matched listening files; never feed hardware; di_dry.wav is the true-level DI"}
    (OUT / "gains.json").write_text(json.dumps(info, indent=1))
    return info


# ------------------------------------------------------------------------------- pedal


def hx_edit_running() -> bool:
    return bool(subprocess.run(["pgrep", "-f", "HX Edit.app/Contents/MacOS/HX Edit"], capture_output=True,
                               check=False).stdout.strip())


def pedal_position() -> dict:
    from tonematch.engines.stomp_bridge import StompBridge

    b = StompBridge(purpose="T197 preflight (read-only info)")
    try:
        i = b.call("info")
        return {k: i.get(k) for k in ("firmware", "setlist", "index", "label", "name")}
    finally:
        b.close()


def _env() -> None:
    os.environ.setdefault("TONEMATCH_STOMPD", str(ROOT / "cache" / "stomp" / "bin" / "stompd"))
    os.environ.setdefault("TONEMATCH_STOMPD_COMPILE", str(ROOT / "cache" / "stomp" / "bin" / "stompd-compile"))


def _session(kind: str, body) -> int:
    """Shared frame: HX Edit check, the pedal lock, the pedal on slot 04C, a run dir and summary;
    any error stops the run (no retry, T168 rule)."""
    from tonematch import pedal_lock

    _env()
    if hx_edit_running():
        print("HX Edit is running: quit it first. Nothing done.")
        return 2
    run_dir = ROOT / "cache" / "stomp" / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-t197-{kind}"
    run_dir.mkdir(parents=True, exist_ok=True)
    res: dict = {"run_dir": str(run_dir), "steps": []}

    def out(msg):
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
        res["steps"].append(msg)
    try:
        with pedal_lock.session(f"T197 DS-Sample reference chain ({kind})"):
            pos = pedal_position()
            res["preflight"] = pos
            out(f"pedal on {pos.get('label')} '{pos.get('name')}' fw {pos.get('firmware')}")
            if (pos["setlist"], pos["index"]) != SCRATCH[:2]:
                out("not slot 04C: nothing done")
                return 3
            body(run_dir, res, out)
    except Exception as e:  # noqa: BLE001 - reported, never retried
        res["error"] = f"{type(e).__name__}: {e}"
        out(f"ERROR {res['error']} -- stopped, no retry")
    finally:
        res["lock_released"] = not pedal_lock.held_here()
        (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
        (OUT / f"{kind}_summary.json").write_text(json.dumps(
            {k: v for k, v in res.items() if not k.startswith("chain")}, indent=1, default=str))
    return 1 if "error" in res else 0


def _open(run_dir: Path, input_value: int, purpose: str):
    import dataclasses

    from tonematch.engines.stomp import StompEngine

    eng = StompEngine(session_dir=run_dir, purpose=purpose, sentinel=False, slip_guard="off",
                      whole_preset="fallback")
    eng.cal = dataclasses.replace(eng.cal, input_usb56=input_value)   # what every load writes and checks
    return eng


def _leave_loaded(eng, res, out) -> None:
    eng._closed = True            # no restore: the human plays through it; `restore` puts 04C back
    eng.bridge.close()
    res["session_close"] = eng.bridge.release_report
    out("bridge closed; chain left in the 04C edit buffer (nothing saved to flash)")


def _input_routing(eng) -> int | None:
    eng._refresh()
    return next(s for s in eng.chain["slots"] if s["kind"] == "Input" and s["pos"] == 0).get("routing")


def cmd_load(a) -> int:
    from tonematch import preset as P
    from tonematch.catalog import load_catalog

    p = P.read_hlx(HLX, load_catalog())

    def body(run_dir, res, out):
        if SESSION_RECORD.exists() and not a.force:
            raise RuntimeError(f"{SESSION_RECORD} exists (04C not restored since the last load?); "
                               "run `restore` first or pass --force")
        eng = _open(run_dir, INPUT_MAIN_LR, "T197 load reference chain (Main L/R, live play)")
        try:
            record = {"original": eng.original, "original_chain": eng.original_chain,
                      "backup_path": str(eng.backup_path), "firmware": eng.info.get("firmware"),
                      "run_dir": str(run_dir), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            SESSION_RECORD.write_text(json.dumps(record, indent=1))
            out(f"open on {eng.original['label']} '{eng.original['name']}'; backup {eng.backup_path.name}")
            t0 = time.perf_counter()
            eng.load(p)
            rb = eng.last_readback or {}
            res.update(load_s=round(time.perf_counter() - t0, 2), readback=rb,
                       templates_captured=list(eng.templates_captured), policy_warnings=list(eng.policy_warnings),
                       whole_writes=len(eng.whole_writes), chain_loaded=eng.chain)
            routing = _input_routing(eng)
            res["input_routing"] = routing
            out(f"loaded in {res['load_s']} s; readback ok {rb.get('ok')}; input {routing}; "
                f"templates {eng.templates_captured}; policy warnings {eng.policy_warnings}")
            if rb.get("ok") is not True or routing != INPUT_MAIN_LR:
                raise RuntimeError(f"readback not ok or input not Main L/R: {json.dumps(rb)[:800]}")
        except Exception:
            if not eng._closed:
                eng.close(verify_bytes=True)
                res["restore_report"] = eng.restore_report
                SESSION_RECORD.unlink(missing_ok=True)
                out(f"put 04C back after the error: restore_ok {eng.restore_report.get('restore_ok')}")
            raise
        _leave_loaded(eng, res, out)
    return _session("load", body)


def cmd_capture(a) -> int:
    """Reamp take2 through the chain already in the edit buffer: Input to USB 5/6, one render,
    Input back to Main L/R, chain left loaded."""
    import soundfile as sf

    from tonematch import preset as P
    from tonematch.catalog import load_catalog
    from tonematch.engines.stomp import reamp_hlx_dict
    from tonematch.engines.stomp_readback import check

    p = P.read_hlx(HLX, load_catalog())
    di, target, where = excerpts()

    def body(run_dir, res, out):
        res["where"] = where
        eng = _open(run_dir, 6, "T197 reamp take2 through the reference chain")   # USB 5/6 = 6 on the Stomp
        try:
            out(f"latency {eng.cal.latency_samples} samples (cache/stomp/latency.json, fw {eng.cal.firmware})")
            eng.load(p)               # same models and values: in place, only the Input routing changes
            rb = eng.last_readback or {}
            res.update(readback=rb, whole_writes=len(eng.whole_writes))
            routing = _input_routing(eng)
            out(f"input {routing} (USB 5/6 = 6); readback ok {rb.get('ok')}; whole writes {len(eng.whole_writes)}")
            if rb.get("ok") is not True or routing != 6:
                raise RuntimeError(f"readback not ok or input not USB 5/6: {json.dumps(rb)[:800]}")
            x = np.concatenate([di, np.zeros(int(TAIL_S * SR), np.float32)])
            t0 = time.perf_counter()
            y = np.asarray(eng.render(x), np.float32)
            res.update(render_s=round(time.perf_counter() - t0, 2), render_offset=eng.last_offset)
            res["ref_rms_dbfs"] = round(20 * np.log10(np.sqrt(np.mean(y[:len(di)] ** 2)) + 1e-12), 2)
            res["ref_peak_dbfs"] = round(20 * np.log10(np.max(np.abs(y)) + 1e-12), 2)
            sf.write(run_dir / "reference_raw.wav", y, SR, subtype="FLOAT")
            out(f"reamped {len(x) / SR:.1f} s in {res['render_s']} s; rms {res['ref_rms_dbfs']} dBFS, "
                f"peak {res['ref_peak_dbfs']} dBFS")
            res["pack"] = write_pack(target, y[:len(di)], di)
        finally:
            if not eng._closed:
                # Back to the guitar input whatever happened (no other change).
                eng.cal = __import__("dataclasses").replace(eng.cal, input_usb56=INPUT_MAIN_LR)
                eng._refresh()
                eng._route_input(force=True)
                time.sleep(0.3)
                eng._refresh()
                fin = check(eng.chain, reamp_hlx_dict(p, INPUT_MAIN_LR), None, "chain for live play").as_dict()
                res.update(final_input_routing=_input_routing(eng), final_readback_ok=fin.get("ok"))
                out(f"input back to {res['final_input_routing']} (Main L/R = 1); final readback ok {fin.get('ok')}")
                _leave_loaded(eng, res, out)
    return _session("capture", body)


class _RestoreSession:
    """Just enough of a StompEngine to run its own close(verify_bytes=True) against a recorded
    session (original preset, its chain, the open-time backup)."""

    def __new__(cls, bridge, record: dict):
        from tonematch.engines.stomp import StompEngine, decode_document

        eng = StompEngine.__new__(StompEngine)
        eng.bridge = bridge
        eng.original = record["original"]
        eng.original_chain = record["original_chain"]
        eng.backup_path = Path(record["backup_path"])
        eng._decoder = decode_document
        eng._closed = False
        eng.restore_report = {}
        return eng


def restore(record_path: Path = SESSION_RECORD, bridge_factory=None) -> dict:
    from tonematch.engines.stomp import restore_verdict

    record = json.loads(Path(record_path).read_text())
    if bridge_factory is None:
        from tonematch.engines.stomp_bridge import StompBridge

        def bridge_factory():
            return StompBridge(allow_document_write=True, purpose="T197 restore 04C")
    eng = _RestoreSession(bridge_factory(), record)
    eng.close(verify_bytes=True)
    rr = eng.restore_report
    return {"restore_ok": restore_verdict(rr), "report": rr}


def cmd_restore(a) -> int:
    from tonematch import pedal_lock

    _env()
    if hx_edit_running():
        print("HX Edit is running: quit it first. Nothing done.")
        return 2
    with pedal_lock.session("T197 restore 04C after live play"):
        r = restore(Path(a.record))
    (OUT / "restore_summary.json").write_text(json.dumps(r, indent=1, default=str))
    rr = r["report"]
    print(f"04C restore_ok {r['restore_ok']} (chain match {rr.get('match')}, decoded equal "
          f"{rr.get('decoded_equal_backup')}, bytes equal {rr.get('bytes_equal_backup')}; steps {rr.get('steps')})")
    if r["restore_ok"]:
        Path(a.record).rename(Path(a.record).with_name(f"session_restored_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"))
    return 0 if r["restore_ok"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    ld = sub.add_parser("load")
    ld.add_argument("--force", action="store_true", help="load although an unrestored session record exists")
    sub.add_parser("capture")
    r = sub.add_parser("restore")
    r.add_argument("--record", default=str(SESSION_RECORD))
    a = ap.parse_args(argv)
    return {"build": cmd_build, "load": cmd_load, "capture": cmd_capture, "restore": cmd_restore}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
