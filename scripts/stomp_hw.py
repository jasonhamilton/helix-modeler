#!/usr/bin/env python3
"""HX Stomp hardware checks, 2026-10-15 priorities (stomp-engine). Edit buffer only, never flash.

  restore04c      select 04C (reloads it from flash) and byte-diff the edit buffer against
                  cache/stomp/live/base04C.bin; with --render also renders di_2s once (audio check)
  delay-readback  item 2: write the old rank2_H_live.bin and the fixed H_check.bin, read the delay back
                  at once and after 1/3/6 s, then with MIDI traffic (CC 110 = the delay's controller and
                  footswitch, CC 69 = snapshot select), each value logged
  settle          item 3: N cycles of whole-write K / A, renders r0..r3 straight after each write,
                  per-render null against the cycle's settled render and a 100 ms residual profile
  dual-path       item 8: one chain on Path A, another on Path B, record USB 1/2 and 3/4 in one pass;
                  null each path against its single-path render; time both ways
  release-check   T059: N sessions of open + info (+ read-only chain with --chain) + close, each
                  printing the helper's session-close report; no write, no select. The human then
                  checks the pedal's blocks can be edited. USB reset off unless --usb-reset says so

Every subcommand starts and ends on 04C and byte-diffs the buffer against base04C.bin. A pedal that
does not answer stops the run with the power-cycle message (StompBridge health check). Results under
cache/stomp/hw/<stamp>-<cmd>/ (summary.json, WAVs, documents).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch import reamp
from tonematch.engines.stomp_bridge import PowerCycleNeeded, StompBridge

LIVE = ROOT / "cache" / "stomp" / "live"
BASE04C = LIVE / "base04C.bin"
INJECT = ROOT / "cache" / "stomp" / "inject"
OUT = ROOT / "cache" / "stomp" / "hw"
DI = ROOT / "fixtures" / "smoke" / "di_2s.wav"
SETLIST, INDEX_04C, NAME_04C = 0, 11, "Smoke Known"
DELAY_POS = 3
MIDI_PY = os.environ.get("TONEMATCH_MIDI_PYTHON")   # a python with python-rtmidi (not in .venv)


def stamp_dir(cmd: str) -> Path:
    d = OUT / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{cmd}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def to_04c(b: StompBridge, run_dir: Path, tag: str) -> dict:
    """Select 04C and byte-diff the edit buffer against the 04C backup."""
    b.call("select", setlist=SETLIST, index=INDEX_04C, timeout_s=20)
    time.sleep(0.5)
    info = b.call("info")
    p = run_dir / f"04C_{tag}.bin"
    b.call("backup", path=str(p))
    same = p.read_bytes() == BASE04C.read_bytes()
    out = {"tag": tag, "preset": {k: info.get(k) for k in ("setlist", "index", "label", "name")},
           "bytes_equal_base04C": same}
    print("04C", json.dumps(out), flush=True)
    if info.get("name") != NAME_04C:
        raise SystemExit(f"after select the pedal shows {info.get('name')!r}, not {NAME_04C!r}: stopping")
    return out


def delay_values(chain: dict, pos: int = DELAY_POS) -> dict:
    s = next((x for x in chain["slots"] if x["pos"] == pos), {})
    vals = {p.get("sym") or p.get("id") or f"x{p.get('extra')}": round(float(p["value"]), 4)
            for p in s.get("params", [])}
    return {"model": s.get("model_id"), "enabled": s.get("enabled"), **vals}


def midi(cc: int, value: int, channel: int = 1) -> None:
    if not MIDI_PY:
        raise SystemExit("set TONEMATCH_MIDI_PYTHON to a python with python-rtmidi for the MIDI steps")
    code = ("import rtmidi,sys;o=rtmidi.MidiOut();p=o.get_ports();"
            "i=next(i for i,n in enumerate(p) if 'hx stomp' in n.lower());o.open_port(i);"
            f"o.send_message([0xB0|{channel - 1},{cc},{value}]);import time;time.sleep(0.05)")
    subprocess.run([MIDI_PY, "-c", code], check=True, timeout=20)


# ----------------------------------------------------------------------------- restore04c


def cmd_restore04c(a) -> int:
    run_dir = stamp_dir("restore04c")
    with StompBridge() as b:
        res = {"before": {k: b.call("info").get(k) for k in ("setlist", "index", "label", "name")}}
        res["after"] = to_04c(b, run_dir, "after")
    if a.render:
        import soundfile as sf

        from tonematch.engines.stomp import StompEngine

        dry, _ = sf.read(DI, dtype="float32")
        with StompEngine(session_dir=run_dir) as eng:
            eng._refresh()
            eng._route_input()
            y = eng.render(dry)
            res["render_rms_db"] = reamp.rms_db(y)
        with StompBridge() as b:
            res["final"] = to_04c(b, run_dir, "final")
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res))
    return 0


# ----------------------------------------------------------------------------- item 2


def cmd_delay_readback(a) -> int:
    run_dir = stamp_dir("delay-readback")
    res: dict = {"docs": {}, "midi": []}
    with StompBridge(allow_document_write=True) as b:
        res["start"] = to_04c(b, run_dir, "start")
        for name in ("rank2_H_live.bin", "H_check.bin"):
            doc = LIVE / name
            steps = []
            to_04c(b, run_dir, f"before_{name}")
            t0 = time.time()
            b.call("restore", path=str(doc), timeout_s=30)
            for wait in (0, 1, 3, 6):
                time.sleep(max(0.0, t0 + wait - time.time()))
                steps.append({"t_s": round(time.time() - t0, 2), **delay_values(b.call("chain"))})
                print(name, json.dumps(steps[-1]), flush=True)
            rb = run_dir / f"readback_{name}"
            b.call("backup", path=str(rb))
            res["docs"][name] = {"steps": steps, "readback": str(rb)}
        # With the fixed document loaded: MIDI traffic our sessions could send or a controller could.
        if MIDI_PY:
            for cc, val, what in ((110, 0, "CC110=0 (delay controller + footswitch)"),
                                  (110, 127, "CC110=127"),
                                  (69, 1, "CC69=1 snapshot 2"), (69, 0, "CC69=0 snapshot 1")):
                midi(cc, val)
                time.sleep(0.5)
                v = {"what": what, **delay_values(b.call("chain"))}
                res["midi"].append(v)
                print("midi", json.dumps(v), flush=True)
        else:
            res["midi"] = "skipped: TONEMATCH_MIDI_PYTHON not set"
        res["end"] = to_04c(b, run_dir, "end")
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    print(f"summary: {run_dir / 'summary.json'}")
    return 0


def cmd_verify(a) -> int:
    """Read the pedal's CURRENT edit buffer (no write, no select) and diff every parameter against an
    .hlx: run it when a live session starts and when a listening judgement is recorded."""
    from tonematch.engines.stomp_readback import readback_diff

    hlx = json.loads(Path(a.hlx).read_text())
    with StompBridge() as b:
        chain = b.call("chain")
    d = readback_diff(chain, hlx, check_input=False)
    print(json.dumps({"preset": {k: chain.get(k) for k in ("label", "name")}, **d.as_dict()}, indent=1))
    return 0 if d.ok else 1


def cmd_release_check(a) -> int:
    """T059 phase 3: does the helper's session-close leave the pedal editable? Read-only sessions."""
    out = []
    for k in range(a.sessions):
        if k:
            time.sleep(a.gap_s)
        t0 = time.perf_counter()
        with StompBridge(usb_reset=a.usb_reset) as b:
            rec: dict = {"session": k + 1, "ready": b.ready, "info": b.call("info")}
            if a.chain:
                ch = b.call("chain")
                rec["endpoints"] = [{x: s.get(x) for x in ("pos", "kind", "routing", "model", "name")}
                                    for s in ch["slots"] if s["kind"] in ("Input", "Output", "Split", "Join")]
        rec["release"] = b.release_report
        rec["usb_reset"] = b.reset_report
        rec["wall_s"] = round(time.perf_counter() - t0, 3)
        out.append(rec)
        print(json.dumps(rec), flush=True)
    print("Now check on the pedal: can a block be selected and edited? (report to the lead)", file=sys.stderr)
    return 0 if all((r["release"] or {}).get("release_sent") for r in out) else 1


def cmd_snapshot_recall(a) -> int:
    """Audible check: the old and the fixed rank2_H documents differ only in snapshot controller
    values (0.52/0.23 vs 0.40/0.25). Render both (input routed to USB 5/6) and null them."""
    import soundfile as sf

    from tonematch.engines.stomp import StompEngine

    run_dir = stamp_dir("snapshot-recall")
    dry, _ = sf.read(DI, dtype="float32")
    res: dict = {}
    with StompBridge() as b:
        res["start"] = to_04c(b, run_dir, "start")
    eng = StompEngine(whole_preset="always", session_dir=run_dir)
    eng.PRE_S = a.pre_s
    res["pre_s"] = a.pre_s
    try:
        r = {}
        for name in ("rank2_H_live.bin", "H_check.bin", "rank2_H_live.bin"):
            eng.bridge.call("restore", path=str(LIVE / name), timeout_s=30)
            eng._refresh()
            eng._route_input(force=True)
            eng._refs.clear()
            ys = [eng.render(dry) for _ in range(2)]
            r.setdefault(name, []).append(ys)
            res.setdefault("delay_read", []).append({"doc": name, **delay_values(eng.bridge.call("chain"))})
        old1, fixed, old2 = r["rank2_H_live.bin"][0], r["H_check.bin"][0], r["rank2_H_live.bin"][1]
        res["null_old_vs_fixed_db"] = round(null_db(old1[1], fixed[1]), 1)
        res["null_old_vs_old_again_db"] = round(null_db(old1[1], old2[1]), 1)
        res["null_repeat_old_db"] = round(null_db(old1[0], old1[1]), 1)
        res["null_repeat_fixed_db"] = round(null_db(fixed[0], fixed[1]), 1)
    finally:
        eng.close()
        res["restore_report"] = eng.restore_report
    with StompBridge() as b:
        res["end"] = to_04c(b, run_dir, "end")
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k.startswith("null") or k == "delay_read"}, indent=1))
    return 0


# ----------------------------------------------------------------------------- item 3


def null_db(a: np.ndarray, b: np.ndarray) -> float:
    return reamp.null_db(np.asarray(a, np.float64), np.asarray(b, np.float64))


def profile(a: np.ndarray, ref: np.ndarray, seg: int = 4800) -> list[float]:
    """Residual energy re the reference, per 100 ms segment (where in time a render differs)."""
    out = []
    for i in range(0, len(ref) - seg + 1, seg):
        r = ref[i:i + seg].astype(np.float64)
        d = a[i:i + seg].astype(np.float64) - r
        out.append(round(10 * np.log10(max(np.sum(d ** 2), 1e-30) / max(np.sum(r ** 2), 1e-30)), 1))
    return out


def cmd_settle(a) -> int:
    import soundfile as sf

    from tonematch import catalog as C
    from tonematch import preset as P
    from tonematch.engines.stomp import StompEngine

    run_dir = stamp_dir("settle")
    dry, _ = sf.read(DI, dtype="float32")
    cat = C.load_catalog("current")
    res: dict = {"cycles": [], "errors": []}
    with StompBridge() as b:
        res["start"] = to_04c(b, run_dir, "start")
    eng = StompEngine(whole_preset="always", session_dir=run_dir)
    try:
        # Control: no write at all, the first renders of a session on 04C with the input routed live.
        eng._refresh()
        eng._route_input()
        ctrl = [eng.render(dry) for _ in range(4)]
        res["control_no_write"] = {"null_vs_last_db": [round(null_db(x, ctrl[-1]), 1) for x in ctrl],
                                   "offsets": None}
        print("control", json.dumps(res["control_no_write"]), flush=True)
        names = (list(a.presets) * a.cycles)[: len(a.presets) * a.cycles]
        for i, name in enumerate(names):
            eng._refs.clear()   # each cycle aligns to its own settled render, not an earlier one
            src = INJECT / f"{name}.hlx"
            if not src.exists():
                src = OUT / "presets" / f"{name}.hlx"
            eng.load(P.read_hlx(src, cat))
            t_w = time.perf_counter()
            rs, offs, ts = [], [], []
            for _k in range(4):
                ts.append(round(time.perf_counter() - t_w, 2))
                rs.append(eng.render(dry))
                offs.append(eng.last_offset)
            ref = rs[-1]
            c = {"i": i, "name": name, "start_s": ts, "offsets": offs,
                 "null_vs_r3_db": [round(null_db(x, ref), 1) for x in rs[:-1]],
                 "null_r1_vs_r2_db": round(null_db(rs[1], rs[2]), 1),
                 "r0_profile_db": profile(rs[0], ref), "r1_profile_db": profile(rs[1], ref)}
            sf.write(run_dir / f"{i:02d}_{name}_r0.wav", rs[0], 48000, subtype="FLOAT")
            sf.write(run_dir / f"{i:02d}_{name}_r3.wav", ref, 48000, subtype="FLOAT")
            res["cycles"].append(c)
            print("cycle", json.dumps({k: c[k] for k in ("i", "name", "offsets", "null_vs_r3_db", "null_r1_vs_r2_db")}),
                  flush=True)
    except PowerCycleNeeded:
        raise
    except Exception as e:  # noqa: BLE001 - record, then restore
        res["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        eng.close()
        res["restore_report"] = eng.restore_report
    with StompBridge() as b:
        res["end"] = to_04c(b, run_dir, "end")
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    print(f"summary: {run_dir / 'summary.json'}")
    return 1 if res["errors"] else 0


# ----------------------------------------------------------------------------- item 8


def make_dual(path_a: Path, path_b: Path, out_b: int, b_enabled: bool = True) -> dict:
    """Path A = path_a's chain; Path B = path_b's amp+cab (its first Amp+Cab block) on row B, input B on
    USB 5/6, output B to `out_b`."""
    import copy

    a, b = json.loads(path_a.read_text()), json.loads(path_b.read_text())
    d = copy.deepcopy(a)
    t, bt = d["data"]["tone"]["dsp0"], b["data"]["tone"]["dsp0"]
    key = next(k for k, v in bt.items() if k.startswith("block") and v.get("@cab"))
    amp = copy.deepcopy(bt[key])
    cab = copy.deepcopy(bt[amp["@cab"]])
    amp.update({"@path": 1, "@position": 0, "@cab": "cab9", "@enabled": bool(b_enabled)})
    t["block19"], t["cab9"] = amp, cab
    t["inputA"]["@input"] = 6
    t["inputB"]["@input"] = 6
    t["outputB"]["@output"] = int(out_b)
    d["data"]["meta"]["name"] = f"dual outB={out_b}"
    return d


def pair_rms(rec: np.ndarray, pre: int, lat: int, n: int) -> tuple[dict, np.ndarray]:
    seg = rec[pre + lat: pre + lat + n]
    return ({f"usb{2 * i + 1}_{2 * i + 2}_db": round(reamp.rms_db(seg[:, 2 * i:2 * i + 2]), 2)
             for i in range(min(4, seg.shape[1] // 2))}, seg)


def aligned_null(ref: np.ndarray, x: np.ndarray) -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    import parity_null as pn

    lag, _c = pn.estimate_lag(ref, x, 480)
    lag = pn.refine_lag(ref, x, lag)
    xs = pn.shift_fractional(np.asarray(x, np.float64), lag)
    r = np.asarray(ref, np.float64)
    m = slice(2400, len(r) - 2400)
    g = float(np.dot(r[m], xs[m]) / max(np.dot(xs[m], xs[m]), 1e-30))
    return {"lag": round(float(lag), 3), "gain_db": round(20 * np.log10(abs(g)), 3),
            "null_db": round(10 * np.log10(np.sum((g * xs[m] - r[m]) ** 2) / np.sum(r[m] ** 2)), 1)}


def cmd_dual_path(a) -> int:
    import soundfile as sf

    from tonematch import catalog as C
    from tonematch import preset as P
    from tonematch.engines.stomp import StompEngine, compile_document

    run_dir = stamp_dir("dual-path")
    dry, _ = sf.read(DI, dtype="float32")
    n = len(dry)
    pa, pb = OUT / "presets" / f"{a.path_a}.hlx", OUT / "presets" / f"{a.path_b}.hlx"
    cat = C.load_catalog("current")
    res: dict = {"path_a": a.path_a, "path_b": a.path_b, "sweep": [], "errors": []}
    with StompBridge() as b:
        res["start"] = to_04c(b, run_dir, "start")
    eng = StompEngine(whole_preset="always", session_dir=run_dir)
    lat = eng.cal.latency_samples

    def write_doc(d: dict, tag: str) -> float:
        hlx, base, out = run_dir / f"{tag}.hlx", run_dir / f"{tag}_base.bin", run_dir / f"{tag}.bin"
        hlx.write_text(json.dumps(d))
        eng.bridge.call("backup", path=str(base))
        rep = compile_document(hlx, base, out)
        t0 = time.perf_counter()
        eng.bridge.call("restore", path=str(out), timeout_s=30)
        dt = time.perf_counter() - t0
        eng._refresh()
        eng._route_input(force=True)
        res.setdefault("compiled", {})[tag] = rep.get("changed")
        return dt

    def raw(x):
        t0 = time.perf_counter()
        rec, pre = reamp.playrec(x, eng.device, pre_s=eng.PRE_S, tail_s=lat / 48000 + 0.05, in_pair=None)
        return rec, pre, time.perf_counter() - t0

    try:
        found = None
        values = [a.out_b] if a.out_b is not None else list(range(13))
        for v in values:
            try:
                write_doc(make_dual(pa, pb, v), f"sweep_outB{v}")
                rec, pre, _ = raw(dry)
                lv, _seg = pair_rms(rec, pre, lat, n)
                row = {"outB": v, **lv}
            except Exception as e:
                row = {"outB": v, "error": f"{type(e).__name__}: {e}"}
                if "dead" in str(e) or "lost" in str(e):
                    raise
                to_04c(eng.bridge, run_dir, f"after_refused_{v}")
            res["sweep"].append(row)
            print("sweep", json.dumps(row), flush=True)
            if row.get("usb3_4_db", -300) > -60:
                found = v
                break
        res["path_b_output_value"] = found
        if found is not None:
            reps = a.repeats
            # Single-path references, each on Path A (USB 1/2).
            singles = {}
            singles_stereo, dual_stereo = [], []
            for name, src in (("A", pa), ("B", pb)):
                eng.load(P.read_hlx(src, cat))
                eng._route_input(force=True)
                ys, ts = [], []
                for _ in range(reps):
                    rec, pre, dt = raw(dry)
                    seg_s = pair_rms(rec, pre, lat, n)[1]
                    ys.append(reamp.to_mono(seg_s[:, 0:2], eng.cal.mono))
                    if name == "A":
                        singles_stereo.append(np.asarray(seg_s[:, 0:2], np.float64))
                    ts.append(dt)
                singles[name] = (ys, ts)
            # Dual: both chains, one pass, USB 1/2 and 3/4.
            res["b_bypassed"] = a.b_bypassed
            res["dual_write_s"] = write_doc(make_dual(pa, pb, found, not a.b_bypassed), "dual")
            da, db, td = [], [], []
            for _ in range(reps):
                rec, pre, dt = raw(dry)
                _lv, seg = pair_rms(rec, pre, lat, n)
                da.append(reamp.to_mono(seg[:, 0:2], eng.cal.mono))
                dual_stereo.append(np.asarray(seg[:, 0:2], np.float64))
                db.append(reamp.to_mono(seg[:, 2:4], eng.cal.mono))
                td.append(dt)
            res["single_s_per_render"] = float(np.median(singles["A"][1] + singles["B"][1]))
            res["dual_s_per_pass"] = float(np.median(td))
            res["throughput_gain"] = 2 * res["single_s_per_render"] / res["dual_s_per_pass"]
            res["null"] = {
                "A_dual_vs_single": aligned_null(singles["A"][0][-1], da[-1]),
                "B_dual_vs_single": aligned_null(singles["B"][0][-1], db[-1]),
                "A_single_repeat": aligned_null(singles["A"][0][-1], singles["A"][0][-2]),
                "B_single_repeat": aligned_null(singles["B"][0][-1], singles["B"][0][-2]),
                "A_dual_repeat": aligned_null(da[-1], da[-2]),
                "B_dual_repeat": aligned_null(db[-1], db[-2]),
                "crosstalk_B_in_A_db": round(reamp.rms_db(da[-1]) - reamp.rms_db(singles["A"][0][-1]), 2),
            }
            for tag, ys in (("A_single", singles["A"][0]), ("B_single", singles["B"][0]), ("A_dual", da),
                            ("B_dual", db)):
                for i, y in enumerate(ys):
                    sf.write(run_dir / f"{tag}_{i}.wav", y, 48000, subtype="FLOAT")
            # Every pair: single-vs-single, dual-vs-dual (repeat noise) and single-vs-dual, per path.
            mat = {}
            for pth, sg, du in (("A", singles["A"][0], da), ("B", singles["B"][0], db)):
                ss = [aligned_null(sg[i], sg[j])["null_db"] for i in range(reps) for j in range(i + 1, reps)]
                dd = [aligned_null(du[i], du[j])["null_db"] for i in range(reps) for j in range(i + 1, reps)]
                sd = [aligned_null(sg[i], du[j])["null_db"] for i in range(reps) for j in range(reps)]
                mat[pth] = {"single_vs_single": ss, "dual_vs_dual": dd, "single_vs_dual": sd,
                            "median_ss": float(np.median(ss)), "median_dd": float(np.median(dd)),
                            "median_sd": float(np.median(sd))}
            res["null_matrix"] = mat
            # Per channel (L, R) of Path A, last single vs last dual: does the dual layout pan or
            # delay one side?
            rec_s = singles_stereo[-1]
            rec_d = dual_stereo[-1]
            res["path_a_channels"] = {
                "single_L_minus_R_db": round(reamp.rms_db(rec_s[:, 0]) - reamp.rms_db(rec_s[:, 1]), 2),
                "dual_L_minus_R_db": round(reamp.rms_db(rec_d[:, 0]) - reamp.rms_db(rec_d[:, 1]), 2),
                "L_single_vs_dual": aligned_null(rec_s[:, 0], rec_d[:, 0]),
                "R_single_vs_dual": aligned_null(rec_s[:, 1], rec_d[:, 1]),
                "single_L_vs_R": aligned_null(rec_s[:, 0], rec_s[:, 1]),
                "dual_L_vs_R": aligned_null(rec_d[:, 0], rec_d[:, 1]),
            }
            print("channels", json.dumps(res["path_a_channels"]), flush=True)
            print("matrix", json.dumps({p_: {k: v for k, v in m.items() if k.startswith("median")}
                                        for p_, m in mat.items()}), flush=True)
            print("dual", json.dumps({k: res[k] for k in ("single_s_per_render", "dual_s_per_pass", "throughput_gain",
                                                          "null")}), flush=True)
    except PowerCycleNeeded:
        raise
    except Exception as e:  # noqa: BLE001
        res["errors"].append(f"{type(e).__name__}: {e}")
        print("ERROR", e, flush=True)
    finally:
        eng.close()
        res["restore_report"] = eng.restore_report
    with StompBridge() as b:
        res["end"] = to_04c(b, run_dir, "end")
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    print(f"summary: {run_dir / 'summary.json'}")
    return 1 if res["errors"] else 0


# ----------------------------------------------------------------------------- dual-path bisection

BISECT_VARIANTS = [   # name, input B, output B, row B content, row B enabled
    ("S_single", 0, 0, None, True),
    ("V1_inB6", 6, 0, None, True),
    ("V2_outB6", 0, 6, None, True),
    ("V3_rowB_amp_bypassed", 0, 0, "amp", False),
    ("V4_rowB_amp_on_noinput", 0, 0, "amp", True),
    ("V5_rowB_gain_in6_out6", 6, 6, "gain", True),
    ("V6_dual_full", 6, 6, "amp", True),
    ("V7_rowB_amp_bypassed_in6_out6", 6, 6, "amp", False),
    ("S_single_end", 0, 0, None, True),
]


def make_variant(path_a: Path, path_b: Path, in_b: int, out_b: int, row_b: str | None, b_on: bool) -> dict:
    import copy

    d = json.loads(path_a.read_text())
    t = d["data"]["tone"]["dsp0"]
    if row_b == "amp":
        bt = json.loads(path_b.read_text())["data"]["tone"]["dsp0"]
        key = next(k for k, v in bt.items() if k.startswith("block") and v.get("@cab"))
        amp, cab = copy.deepcopy(bt[key]), copy.deepcopy(bt[bt[key]["@cab"]])
        amp.update({"@path": 1, "@position": 0, "@cab": "cab9", "@enabled": bool(b_on)})
        t["block19"], t["cab9"] = amp, cab
    elif row_b == "gain":
        t["block19"] = {"@model": "HD2_VolPanGain", "@enabled": bool(b_on), "@path": 1, "@position": 0,
                        "Gain": 0.0}
    t["inputA"]["@input"] = 6
    t["inputB"]["@input"] = int(in_b)
    t["outputB"]["@output"] = int(out_b)
    return d


def cmd_dual_bisect(a) -> int:
    import soundfile as sf

    from tonematch.engines.stomp import StompEngine, compile_document

    run_dir = stamp_dir("dual-bisect")
    dry, _ = sf.read(DI, dtype="float32")
    n = len(dry)
    pa, pb = OUT / "presets" / f"{a.path_a}.hlx", OUT / "presets" / f"{a.path_b}.hlx"
    if not pa.exists():
        pa = INJECT / f"{a.path_a}.hlx"
    res: dict = {"path_a": a.path_a, "path_b": a.path_b, "variants": {}, "errors": []}
    with StompBridge() as b:
        res["start"] = to_04c(b, run_dir, "start")
    eng = StompEngine(whole_preset="always", session_dir=run_dir)
    lat = eng.cal.latency_samples
    renders: dict[str, list] = {}
    try:
        for name, in_b, out_b, row_b, b_on in BISECT_VARIANTS:
            d = make_variant(pa, pb, in_b, out_b, row_b, b_on)
            hlx, base, out = run_dir / f"{name}.hlx", run_dir / f"{name}_base.bin", run_dir / f"{name}.bin"
            hlx.write_text(json.dumps(d))
            eng.bridge.call("backup", path=str(base))
            rep = compile_document(hlx, base, out)
            eng.bridge.call("restore", path=str(out), timeout_s=30)
            eng._refresh()
            eng._route_input(force=True)
            ys, yb = [], []
            for _ in range(a.repeats):
                rec, pre = reamp.playrec(dry, eng.device, pre_s=eng.PRE_S, tail_s=lat / 48000 + 0.05, in_pair=None)
                seg = rec[pre + lat: pre + lat + n]
                ys.append(reamp.to_mono(seg[:, 0:2], eng.cal.mono))
                yb.append(reamp.rms_db(seg[:, 2:4]))
            renders[name] = ys
            sf.write(run_dir / f"{name}_0.wav", ys[0], 48000, subtype="FLOAT")
            res["variants"][name] = {"changed": rep.get("changed"), "usb3_4_db": round(float(np.median(yb)), 1)}
            print("variant", name, json.dumps(res["variants"][name]), flush=True)
        base_r = renders["S_single"]
        for name, ys in renders.items():
            vs = [aligned_null(r, y)["null_db"] for r in base_r for y in ys] if name != "S_single" else []
            rp = [aligned_null(ys[i], ys[j])["null_db"] for i in range(len(ys)) for j in range(i + 1, len(ys))]
            lagv = [aligned_null(r, y)["lag"] for r in base_r[:1] for y in ys[:1]]
            res["variants"][name].update({"null_vs_single_median_db": float(np.median(vs)) if vs else None,
                                          "repeat_median_db": float(np.median(rp)) if rp else None,
                                          "lag_vs_single": lagv[0] if lagv else None})
            print("null", name, json.dumps({k: res["variants"][name][k] for k in
                                            ("null_vs_single_median_db", "repeat_median_db", "lag_vs_single")}),
                  flush=True)
    except PowerCycleNeeded:
        raise
    except Exception as e:  # noqa: BLE001
        res["errors"].append(f"{type(e).__name__}: {e}")
        print("ERROR", e, flush=True)
    finally:
        eng.close()
        res["restore_report"] = eng.restore_report
    with StompBridge() as b:
        res["end"] = to_04c(b, run_dir, "end")
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    print(f"summary: {run_dir / 'summary.json'}")
    return 1 if res["errors"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("restore04c")
    r.add_argument("--render", action="store_true")
    sub.add_parser("delay-readback")
    dp = sub.add_parser("dual-path")
    dp.add_argument("--path-a", default="K0")
    dp.add_argument("--path-b", default="B00")
    dp.add_argument("--out-b", type=int, default=None, help="Path B output value; default: sweep 0..12")
    dp.add_argument("--repeats", type=int, default=3)
    dp.add_argument("--b-bypassed", action="store_true", help="dual layout with Path B's block bypassed (leak test)")
    bi = sub.add_parser("dual-bisect")
    bi.add_argument("--path-a", default="K0")
    bi.add_argument("--path-b", default="B00")
    bi.add_argument("--repeats", type=int, default=3)
    sr = sub.add_parser("snapshot-recall")
    sr.add_argument("--pre-s", type=float, default=0.25, help="silent pre-roll before each render")
    v = sub.add_parser("verify")
    v.add_argument("hlx")
    s = sub.add_parser("settle")
    s.add_argument("--cycles", type=int, default=3)
    s.add_argument("--presets", nargs="+", default=["K", "A"],
                   help="cache/stomp/inject/<name>.hlx or cache/stomp/hw/presets/<name>.hlx")
    rc = sub.add_parser("release-check")
    rc.add_argument("--sessions", type=int, default=1)
    rc.add_argument("--gap-s", type=float, default=2.0, help="pause between sessions")
    rc.add_argument("--chain", action="store_true", help="also read the chain (read-only) and print the "
                                                         "Input/Output/Split/Join routing")
    rc.add_argument("--usb-reset", choices=("auto", "always", "off"), default="off",
                    help="default off here, so the session-close is tested alone")
    a = ap.parse_args(argv)
    if subprocess.run(["pgrep", "-f", "HX Edit.app/Contents/MacOS/HX Edit"], capture_output=True, check=False).stdout.strip():
        sys.exit("HX Edit is running: quit it first (it holds the control interface)")
    try:
        return {"restore04c": cmd_restore04c, "delay-readback": cmd_delay_readback, "settle": cmd_settle,
                "snapshot-recall": cmd_snapshot_recall, "verify": cmd_verify, "dual-path": cmd_dual_path,
                "dual-bisect": cmd_dual_bisect, "release-check": cmd_release_check}[a.cmd](a)
    except PowerCycleNeeded as e:
        print(str(e), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
