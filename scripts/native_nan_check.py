#!/usr/bin/env python3
"""T132 live check: a NaN-poisoned Native host is caught and cleaned, and the true preset renders.

Native 3.82 renders Preamp EV Panama Red with Low 0 and High about 0.96 numerically unstable: the
output grows about 55 dB per second from ~2 s into a render and overflows to inf/NaN at about 9.5 s,
whatever the input. After that ONE render, every later render of that host process is NaN, on any
chain (state re-load, another chain, the empty-chain flush and silence do not clear it; a re-prepare
or a new process does). T120's devfam items i071/i079/i087/i095 died of it (10 s clips).

Checks, one host:
  1. clean reference: the true preset (i071's Panama Red values) on a test signal;
  2. the unstable setting raises EngineError ("unstable"), the host is left clean, the true preset
     renders bit-identically to (1);
  3. the host is poisoned behind the engine's back: the in-run sentinel reports it (inf, a drift) and
     fixes it; the true preset renders identically to (1);
  4. poisoned again: the next ``render`` replaces the host, re-renders once and returns the clean
     result, identical to (1).

``--ledger`` appends kind=smoke rows, dataset native-nan-v1. Needs Native; at most one host.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MODEL = "HD2_PreampEVPanamaRed"
TRUE = {"Gain": 0.6692913385826772, "High": 0.7952755905511811, "Low": 0.12598425196850394,
        "Master": 0.3464566929133858, "Mid": 0.7795275590551181, "Sag": 0.5275590551181102}  # devfam i071
UNSTABLE = {"Gain": 0.2677165354330709, "High": 0.9606299212598425, "Low": 0.0,
            "Master": 0.9921259842519685, "Mid": 1.0, "Sag": 0.0}  # reached by the i071 search
BAR_DB = -150.0  # CANARY_THRESHOLD_DB: bit-identical renders null to about -290 dB


def preset(values: dict):
    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL

    p = NL.block_preset(MODEL, load_catalog())
    p.slots[0].params.update(values)
    return p


def poison(eng, x: np.ndarray) -> int:
    """Render the unstable setting straight on the host (the engine does not see it); the number of
    non-finite samples it returned."""
    eng.load(preset(UNSTABLE))
    padded = np.concatenate([np.zeros(eng.preroll, np.float32), x, np.zeros(eng.latency(), np.float32)])
    _, data = eng.host.call("render", padded.astype("<f4").tobytes(), reset=True)
    return int((~np.isfinite(np.frombuffer(data, dtype="<f4"))).sum())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=10.0, help="test clip length (the blow-up needs ~9.5 s)")
    ap.add_argument("--ledger", action="store_true")
    a = ap.parse_args(argv)
    hv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and hv.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(hv)
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    from tonematch.engines import native_library as NL
    from tonematch.engines.base import EngineError
    from tonematch.engines.native import NativeEngine, null_db, test_signal

    t0 = time.perf_counter()
    eng = NativeEngine(templates_dir=NL.library_templates())
    x = test_signal(a.seconds, seed=3)
    res: dict[str, object] = {}
    try:
        eng.load(preset(TRUE))
        ref = eng.render(x)

        def true_null() -> float:
            eng.load(preset(TRUE))
            return null_db(ref, eng.render(x))

        # 2. the unstable setting itself
        eng.load(preset(UNSTABLE))
        t = time.perf_counter()
        try:
            eng.render(x)
            res["unstable_raises"] = False
        except EngineError as e:
            res["unstable_raises"] = "unstable" in str(e)
        res["unstable_s"] = round(time.perf_counter() - t, 1)
        res["null_after_unstable_db"] = true_null()
        # 3. poisoned behind the engine's back: the sentinel
        res["poison_nonfinite_3"] = poison(eng, x)
        d0, f0 = eng.sentinel["drifts"], eng.sentinel["fixed"] + eng.sentinel["recycles"]
        nd = eng.sentinel_check()
        res["sentinel_null_db"] = nd
        res["sentinel_caught"] = bool(nd is not None and nd > BAR_DB and eng.sentinel["drifts"] == d0 + 1
                                      and eng.sentinel["fixed"] + eng.sentinel["recycles"] == f0 + 1)
        res["null_after_sentinel_db"] = true_null()
        # 4. poisoned again: the render path
        res["poison_nonfinite_4"] = poison(eng, x)
        eng.load(preset(TRUE))
        rec0 = eng.nonfinite["recovered"]
        t = time.perf_counter()
        y = eng.render(x)
        res["recover_s"] = round(time.perf_counter() - t, 1)
        res["render_recovered"] = eng.nonfinite["recovered"] == rec0 + 1
        res["null_after_recovery_db"] = null_db(ref, y)
        stats = eng.stats()
    finally:
        eng.close()
    wall = time.perf_counter() - t0
    nulls = [res["null_after_unstable_db"], res["null_after_sentinel_db"], res["null_after_recovery_db"]]
    checks = {"unstable setting raises, host clean": bool(res["unstable_raises"]) and nulls[0] <= BAR_DB,
              "sentinel catches a poisoned host": bool(res["sentinel_caught"]) and nulls[1] <= BAR_DB,
              "render recovers a poisoned host": bool(res["render_recovered"]) and nulls[2] <= BAR_DB,
              "poisoning reproduced": res["poison_nonfinite_3"] > 0 and res["poison_nonfinite_4"] > 0}
    for k, v in res.items():
        print(f"  {k}: {v}")
    print("  stats:", {k: v for k, v in stats.items() if k.startswith(("nonfinite", "sentinel_", "recycles"))})
    for k, v in checks.items():
        print(f"{'PASS' if v else 'FAIL'}  {k}")
    passed = sum(checks.values())
    print(f"{passed}/{len(checks)} checks, worst true-preset null {max(nulls):.1f} dB, {wall:.0f} s")
    if a.ledger:
        env = dict(os.environ)
        env.setdefault("TONEMATCH_AGENT", "native-engine")
        notes = (f"T132 scripts/native_nan_check.py; {MODEL} Low 0 High 0.96 unstable on a {a.seconds:.0f} s test "
                 f"signal; checks: " + "; ".join(f"{k} {'ok' if v else 'FAIL'}" for k, v in checks.items())
                 + f"; sentinel null {res['sentinel_null_db']}; recovery {res['recover_s']} s; "
                 f"nonfinite stats events {stats['nonfinite_events']} recovered {stats['nonfinite_recovered']} "
                 f"unstable {stats['nonfinite_unstable']} recycles {stats['nonfinite_recycles']}")
        for metric, value in (("nan_checks_passed", passed / len(checks)),
                              ("true_null_after_poison_db_worst", max(nulls))):
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "smoke",
                            "--engine", "native", "--dataset", "native-nan-v1", "--metric", metric,
                            "--value", f"{value:.6g}", "--renders", str(stats["renders"]),
                            "--wall-clock-s", f"{wall:.1f}", "--notes", notes], check=True, env=env, cwd=ROOT)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
