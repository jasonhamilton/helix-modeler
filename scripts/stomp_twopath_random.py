#!/usr/bin/env python3
"""Two-path parity on the HX Stomp with a random chain (T071, stomp-engine, human request).

One random chain of blocks on a single path (input USB 5/6 -> Path A -> USB 1/2), then the same
blocks duplicated on Path A and Path B (topology SAB: Y split at the start of the row, Path A to
USB 1/2, Path B with its own output to USB 3/4, HX Edit's split/join values 0.5/0.5). Three renders
per pass: single, dual A, dual B; each null-compared with the other two and with its own repeat.

Pool: the fw 3.80 parity-spot models (scripts/parity_spot.SETS) of Distortion, Dynamics, EQ, Delay
and Reverb. Excluded: Modulation (free-running LFO: the pedal's own repeat floor is -1.5 dB, T060)
and Amp/Cab (two amps + two cabs + time effects on one DSP). At most one Delay and one Reverb.
Time-based blocks go last. Settings as parity_spot.build: defaults; EQ continuous values random in
20..80 % of range; delays unsynced, Time 0.375 s, Feedback 0.4, Mix 0.4.

  gen   offline: draw the chain (--seed), write cache/stomp/twopath_random/<seed>/{single,dual}.hlx,
        validate for fw 3.80, compile both onto an 04C backup and decode (junctions, block positions)
  run   pedal, one session: refuses if HX Edit runs; device check + shared pedal lock (tonematch.pedal_lock); scratch slot
        04C as base; whole writes with document readback; --repeats renders each; restores the original
        preset (readback + byte check); always releases the lock. Then the nulls and ledger rows.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import parity_spot as PS
import stomp_fw380 as F

from tonematch import catalog as C
from tonematch import preset as P
from tonematch.engines.stomp import compile_document, decode_document, restore_verdict
from tonematch.engines.stomp_layout import HX_EDIT_JOIN, HX_EDIT_SPLIT_Y
from tonematch.engines.stomp_readback import readback_diff

STOMP = ROOT / "cache" / "stomp"
OUT = STOMP / "twopath_random"
NEW = STOMP / "bin" / "stompd-compile"
DI = ROOT / "fixtures" / "smoke" / "di_2s.wav"
SCRATCH = (0, 11, "Smoke Known")
USB56 = 6
OUT_B_USB34 = 6
PROCESS = "HX Edit.app/Contents/MacOS/HX Edit"
DATASET = "twopath-parity-fw380"
POOL = ("Distortion", "Dynamics", "EQ", "Delay", "Reverb")
N_BLOCKS = 4
ONE_OF = ("Delay", "Reverb")
FREE_RUNNING = ("WowFlutter", "Wow", "Flutter")   # tape modulation free-runs like an LFO: 0 (as Hum/Ripple)


def draw(seed: int, exclude: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    rng = random.Random(seed)
    models = [(c, m) for c in POOL for m in PS.SETS[c] if m not in exclude]
    while True:
        pick = rng.sample(models, N_BLOCKS)
        if all(sum(c == k for c, _ in pick) <= 1 for k in ONE_OF):
            break
    return [cm for cm in pick if cm[0] not in ONE_OF] + [cm for cm in pick if cm[0] in ONE_OF]


def block_params(cat: dict, category: str, model: str) -> dict:
    prm = F._defaults(cat, model)
    name = f"{category[:3]}_{PS.short(model)}"
    if any(name.startswith(x) for x in PS.STRIP_FOR):
        for k in PS.STOMP_LACKS:
            prm.pop(k, None)
    if category == "Delay":
        for k, v in (("TempoSync1", False), ("Feedback", 0.4), ("Mix", 0.4)):
            if k in prm:
                prm[k] = v
        prm["Time"] = min(max(0.375, cat["models"][model]["params"]["Time"]["min"]), 2.0)
    for k in FREE_RUNNING:
        if k in prm:
            prm[k] = 0.0
    if category == "EQ":
        rng = random.Random(f"eq-{model}")
        for k, pr in cat["models"][model]["params"].items():
            if k.startswith("@") or pr["kind"] != "continuous" or k.lower().startswith("level"):
                continue
            prm[k] = round(pr["min"] + (pr["max"] - pr["min"]) * rng.uniform(0.2, 0.8), 3)
    return prm


def build(cat: dict, chain: list[tuple[str, str]], dual: bool, seed: int) -> dict:
    """K.hlx's frame (I/O, globals, snapshots) with every block replaced by `chain` on Path A
    (and a copy on Path B when `dual`); no controller or footswitch assignments."""
    d = json.loads((F.INJECT / "K.hlx").read_text())
    data, tone = d["data"], d["data"]["tone"]
    dsp = tone["dsp0"]
    for k in [k for k in dsp if k.startswith(("block", "cab"))]:
        del dsp[k]
    for sec in ("controller", "footswitch"):
        tone.pop(sec, None)
    keys = []
    for path in ((0, 1) if dual else (0,)):
        for i, (c, m) in enumerate(chain):
            key = f"block{i + 8 * path}"
            dsp[key] = {"@model": m, "@enabled": True, "@path": path, "@position": i, "@no_snapshot_bypass": False,
                        **block_params(cat, c, m)}
            keys.append(key)
    for n in range(8):
        sn = tone.get(f"snapshot{n}")
        if isinstance(sn, dict):
            sn["blocks"] = {"dsp0": {k: True for k in keys}}
            sn.pop("controllers", None)
    dsp["inputA"]["@input"] = USB56
    dsp["inputB"]["@input"] = 0
    if dual:
        dsp["outputB"]["@output"] = OUT_B_USB34
        tone["global"]["@topology0"] = "SAB"
        dsp["split"].update({"@model": "HD2_AppDSPFlowSplitY", "@position": 0, "@enabled": True, **HX_EDIT_SPLIT_Y})
        dsp["join"].update({"@model": "HD2_AppDSPFlowJoin", "@position": 8, "@enabled": True, **HX_EDIT_JOIN})
    else:
        tone["global"]["@topology0"] = "A"
    data["device_version"] = F.FW_380
    data["meta"]["name"] = f"rnd{seed} {'dual' if dual else 'single'}"[:16]
    return d


def write(path: Path, d: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(P.dumps_hlx(d), encoding="utf-8")
    return path


def layout(chain: dict) -> list:
    out = []
    for s in chain["slots"]:
        if s["kind"] in ("Split", "Join"):
            out.append((s["kind"], s.get("attach"), s.get("b_routing")))
        elif s["kind"] == "Block":
            out.append((s["pos"], s.get("model_id"), s.get("enabled")))
    return out


def cmd_gen(a) -> int:
    from stomp_twopath import base_backup

    cat = C.load_catalog("current")
    chain = draw(a.seed, tuple(a.exclude))
    d = OUT / str(a.seed)
    res = {"seed": a.seed, "exclude": a.exclude, "chain": chain}
    base = base_backup()
    for tag, dual in (("single", False), ("dual", True)):
        h = write(d / f"{tag}.hlx", build(cat, chain, dual, a.seed))
        v = subprocess.run([sys.executable, str(ROOT / "scripts" / "hlx_validate.py"), str(h), "--firmware",
                            hex(F.FW_380)], capture_output=True, text=True, check=False)
        rep = compile_document(h, base, d / f"{tag}_offline.bin", compiler=NEW)
        ch = decode_document(d / f"{tag}_offline.bin", compiler=NEW)
        rb = readback_diff(ch, json.loads(h.read_text()), rep)
        res[tag] = {"validate_rc": v.returncode, "validate_tail": v.stdout.strip().splitlines()[-3:],
                    "topology": rep.get("topology"), "warnings": rep.get("warnings"), "layout": layout(ch),
                    "document_check_ok": rb.ok, "mismatches": rb.mismatches[:8]}
    (d / "gen.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    return 0 if all(res[t]["validate_rc"] == 0 and res[t]["document_check_ok"] for t in ("single", "dual")) else 1


def hx_edit_running() -> bool:
    r = subprocess.run(["pgrep", "-f", PROCESS], capture_output=True, text=True, check=False)
    mine = {str(os.getpid()), str(os.getppid())}
    return bool([p for p in r.stdout.split() if p not in mine])


def cmd_run(a) -> int:
    import soundfile as sf
    from stomp_hw import aligned_null

    from tonematch import reamp
    from tonematch.engines.stomp import StompEngine

    if hx_edit_running():
        sys.exit("HX Edit is running: quit it first")
    if not os.environ.get("TONEMATCH_STOMPD") and not shutil.which("stompd"):
        sys.exit("set TONEMATCH_STOMPD to the USB helper (cache/stomp/bin/stompd)")
    src = OUT / str(a.seed)
    gen = json.loads((src / "gen.json").read_text())
    from tonematch import pedal_lock

    pedal_lock.require_device()       # DeviceMissing / LockHeld exit with their own codes and messages
    hold = pedal_lock.PedalLock("stomp_twopath_random run").acquire()
    run_dir = STOMP / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-twopath-random"
    docs = run_dir / "documents"
    res: dict = {"seed": a.seed, "chain": gen["chain"], "errors": [], "renders": 0}
    renders: dict[str, list[np.ndarray]] = {}
    t_start = time.perf_counter()
    eng = None
    try:
        docs.mkdir(parents=True, exist_ok=True)
        dry, _ = sf.read(DI, dtype="float32")
        n = len(dry)
        eng = StompEngine(whole_preset="always", session_dir=run_dir)
        res["original"] = eng.original
        lat = eng.cal.latency_samples
        try:
            if eng.original.get("name") != SCRATCH[2]:
                raise SystemExit(f"the pedal is on {eng.original}, not 04C '{SCRATCH[2]}': nothing written")
            base = docs / "base04C.bin"
            eng.bridge.call("backup", path=str(base))

            def cap(tag: str, k: int) -> None:
                eng._refresh()
                pre_s = max(eng.pre_roll_s(), 3.0)
                for i in range(k):
                    rec, pre = reamp.playrec(dry, eng.device, pre_s=pre_s, tail_s=lat / 48000 + 0.05, in_pair=None)
                    seg = rec[pre + lat: pre + lat + n]
                    for pair, name in ((slice(0, 2), "A"), (slice(2, 4), "B")):
                        y = reamp.to_mono(seg[:, pair], eng.cal.mono).astype(np.float32)
                        renders.setdefault(f"{tag}_{name}", []).append(y)
                        sf.write(run_dir / f"{tag}_{name}_{i}.wav", y, 48000, subtype="FLOAT")
                    res["renders"] += 1
                    print("rendered", tag, i, {nm: round(reamp.rms_db(renders[f"{tag}_{nm}"][-1]), 2) for nm in "AB"},
                          flush=True)

            for tag in ("single", "dual"):
                hlx = json.loads((src / f"{tag}.hlx").read_text())
                out = docs / f"{tag}.bin"
                rep = compile_document(src / f"{tag}.hlx", base, out, compiler=NEW)
                t0 = time.perf_counter()
                eng.bridge.call("restore", path=str(out), timeout_s=30)
                w = time.perf_counter() - t0
                eng._refresh()
                eng._route_input(force=True)
                back = docs / f"{tag}_readback.bin"
                eng.bridge.call("backup", path=str(back))
                ch = decode_document(back, compiler=NEW)
                d = readback_diff(ch, hlx, rep)
                res[tag] = {"topology": rep.get("topology"), "write_s": round(w, 3), "document_readback_ok": d.ok,
                            "mismatches": d.mismatches[:10], "layout": layout(ch)}
                print("wrote", tag, json.dumps(res[tag], default=str), flush=True)
                if not d.ok:
                    raise SystemExit(f"{tag}: readback mismatch, not rendered")
                cap(tag, a.repeats)
        except SystemExit as e:
            res["errors"].append(str(e))
        except Exception as e:  # noqa: BLE001 - always restore
            res["errors"].append(f"{type(e).__name__}: {e}")
            print("ERROR", e, flush=True)
        finally:
            eng.close(verify_bytes=True)
            res["restore_report"] = eng.restore_report
            res["session_close"] = eng.bridge.release_report
    except Exception as e:  # noqa: BLE001 - engine open failed
        res["errors"].append(f"open {type(e).__name__}: {e}")
    finally:
        hold.release()
        res["lock_released"] = not pedal_lock.lock_dir().exists()
        res["wall_clock_s"] = round(time.perf_counter() - t_start, 1)

    def pairs(x: str, y: str) -> list[float]:
        xs, ys = renders.get(x, []), renders.get(y, [])
        if x == y:
            return [aligned_null(p, q)["null_db"] for p, q in itertools.combinations(xs, 2)]
        return [aligned_null(p, q)["null_db"] for p in xs for q in ys]

    nulls = {}
    for name, x, y in (("single_vs_dualA", "single_A", "dual_A"), ("single_vs_dualB", "single_A", "dual_B"),
                       ("dualA_vs_dualB", "dual_A", "dual_B"), ("single_repeat", "single_A", "single_A"),
                       ("dualA_repeat", "dual_A", "dual_A"), ("dualB_repeat", "dual_B", "dual_B")):
        if renders.get(x) and renders.get(y):
            v = [float(z) for z in pairs(x, y)]
            if v:
                nulls[name] = {"median_db": float(np.median(v)), "all_db": v}
    if renders.get("dual_A") and renders.get("dual_B"):
        # same render: Path A vs Path B of one pass (no repeat between them)
        v = [float(aligned_null(p, q)["null_db"]) for p, q in zip(renders["dual_A"], renders["dual_B"])]
        nulls["dualA_vs_dualB_same_pass"] = {"median_db": float(np.median(v)), "all_db": v}
    gains = {}
    for name, x, y in (("single_vs_dualA", "single_A", "dual_A"), ("single_vs_dualB", "single_A", "dual_B")):
        if renders.get(x) and renders.get(y):
            gains[name] = float(aligned_null(renders[x][0], renders[y][0])["gain_db"])
    res["nulls"], res["gain_db"] = nulls, gains
    res["levels_dbfs"] = {k: round(float(np.median([reamp.rms_db(r) for r in v])), 2) for k, v in renders.items()}
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: res.get(k) for k in ("nulls", "gain_db", "levels_dbfs", "errors", "restore_report",
                                              "lock_released", "wall_clock_s")}, indent=1, default=str))
    print("summary:", run_dir / "summary.json")
    rr = res.get("restore_report") or {}
    return 0 if not res["errors"] and rr and restore_verdict(rr) else 1   # decoded equality (T097)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--seed", type=int, required=True)
    g.add_argument("--exclude", nargs="*", default=[],
                   help="models left out of the pool (e.g. HD2_ReverbGanymede: modulated, repeat floor -15 dB)")
    r = sub.add_parser("run")
    r.add_argument("--seed", type=int, required=True)
    r.add_argument("--repeats", type=int, default=2)
    a = ap.parse_args(argv)
    return {"gen": cmd_gen, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
