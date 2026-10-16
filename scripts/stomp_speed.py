#!/usr/bin/env python3
"""HX Stomp speed measurements, before and after (stomp-engine, T061). Edit buffer only, never flash.

  sessions  item 1: cost of opening and closing per test (engine open, restore + session close, and
            the separate restore-check session scripts used) against one session per batch with the
            restore byte-check done in the same session (StompEngine.close(verify_bytes=True))
  clips     item 2: render wall time against clip length (2, 3, 4, 10 s excerpts of the 10 s DI) on K
  tails     item 3: delay/reverb chains rendered with the safe upper-bound wait and with
            decay-sized waits (TONEMATCH_STOMP_TAIL=measured); wait per render and the null of a
            measured-wait render against a bound-wait render, next to the bound repeat null
  params    item 5: parameter-only in-place loads with a full readback every load against every Nth
  switch    item 6: stored-preset switching by `select` and by MIDI Program Change (read-only:
            no save, no flash write), against a whole edit-buffer write; one render check per preset

Every subcommand starts on 04C "Smoke Known", ends on it, and byte-checks the edit buffer against
the backup taken at open. Results: cache/stomp/hw/<stamp>-speed-<cmd>/summary.json. Ledger rows with
--ledger (kind render-cost, engine stomp, dataset stomp-speed, notes carry fw=3.80).
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
sys.path.insert(0, str(ROOT / "scripts"))

from tonematch import catalog as C
from tonematch import preset as P
from tonematch import reamp

OUT = ROOT / "cache" / "stomp" / "hw"
FW380 = ROOT / "cache" / "stomp" / "fw380"
DI2 = ROOT / "fixtures" / "smoke" / "di_2s.wav"
DI10 = ROOT / "cache" / "parity" / "di_10s_tail.wav"
ORIGINAL = "Smoke Known"
MIDI_PY = os.environ.get("TONEMATCH_MIDI_PYTHON")


def stamp_dir(cmd: str) -> Path:
    d = OUT / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-speed-{cmd}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_wav(p: Path) -> np.ndarray:
    import soundfile as sf

    x, _ = sf.read(p, dtype="float32")
    return x if x.ndim == 1 else x[:, 0]


def ledger(a, metric: str, value: float, notes: str, renders: int | None = None) -> None:
    if not a.ledger:
        print("(no ledger)", metric, value, notes)
        return
    env = dict(os.environ)
    env.setdefault("TONEMATCH_AGENT", "stomp-engine")
    args = ["--kind", "render-cost", "--engine", "stomp", "--dataset", "stomp-speed", "--metric", metric,
            "--value", f"{value:.4f}", "--notes", f"fw=3.80; {notes}"]
    if renders is not None:
        args += ["--renders", str(renders)]
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), *args], check=True, env=env)


def engine(run_dir: Path, **kw):
    from tonematch.engines.stomp import StompEngine

    eng = StompEngine(session_dir=run_dir, **kw)
    if eng.original.get("name") != ORIGINAL:
        eng.close()
        raise SystemExit(f"the pedal is on {eng.original}, not 04C '{ORIGINAL}': nothing written")
    return eng


def finish(eng, res: dict, run_dir: Path) -> bool:
    eng.close(verify_bytes=True)
    res["restore_report"] = eng.restore_report
    res["session_close"] = eng.bridge.release_report
    res["usb_reset"] = eng.bridge.reset_report
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    ok = bool(eng.restore_report.get("bytes_equal_backup"))
    print(json.dumps({"restore_match": eng.restore_report.get("match"), "bytes_equal_backup": ok,
                      "release_sent": (eng.bridge.release_report or {}).get("release_sent")}))
    print("summary:", run_dir / "summary.json")
    return ok


def aligned_null(ref: np.ndarray, x: np.ndarray) -> float:
    import parity_null as pn

    lag, _c = pn.estimate_lag(ref, x, 480)
    lag = pn.refine_lag(ref, x, lag)
    xs = pn.shift_fractional(np.asarray(x, np.float64), lag)
    r = np.asarray(ref, np.float64)
    m = slice(2400, len(r) - 2400)
    g = float(np.dot(r[m], xs[m]) / max(np.dot(xs[m], xs[m]), 1e-30))
    return round(float(10 * np.log10(np.sum((g * xs[m] - r[m]) ** 2) / np.sum(r[m] ** 2))), 1)


# ----------------------------------------------------------------------------- item 1


def cmd_sessions(a) -> int:
    """Old: per test, engine open + load + render + close + a separate restore-check session.
    New: one engine for all tests, restore byte-check in the same session."""
    from tonematch.engines.stomp_bridge import StompBridge

    cat = C.load_catalog("current")
    run_dir = stamp_dir("sessions")
    dry = load_wav(DI2)
    k = P.read_hlx(FW380 / "K.hlx", cat)
    res: dict = {"old": [], "new": {}}
    for _ in range(a.tests):
        t0 = time.perf_counter()
        eng = engine(run_dir, whole_preset="fallback")
        t1 = time.perf_counter()
        eng.load(k)
        eng.render(dry)
        t2 = time.perf_counter()
        eng.close()
        t3 = time.perf_counter()
        with StompBridge() as b:          # the restore check scripts ran in a second session
            b.call("info")
            after = run_dir / "old_after.bin"
            b.call("backup", path=str(after))
        t4 = time.perf_counter()
        eq = after.read_bytes() == Path(eng.backup_path).read_bytes()
        res["old"].append({"open_s": t1 - t0, "work_s": t2 - t1, "close_s": t3 - t2, "verify_session_s": t4 - t3,
                           "bytes_equal": eq})
        print("old", res["old"][-1], flush=True)
    t0 = time.perf_counter()
    eng = engine(run_dir, whole_preset="fallback")
    t1 = time.perf_counter()
    work = []
    for _ in range(a.tests):
        s = time.perf_counter()
        eng.load(k)
        eng.render(dry)
        work.append(time.perf_counter() - s)
    t2 = time.perf_counter()
    ok = finish(eng, res, run_dir)
    t3 = time.perf_counter()
    res["new"] = {"open_s": t1 - t0, "work_s": work, "close_verify_s": t3 - t2}
    old_over = float(np.median([r["open_s"] + r["close_s"] + r["verify_session_s"] for r in res["old"]]))
    new_over = (res["new"]["open_s"] + res["new"]["close_verify_s"]) / a.tests
    res["per_test_overhead_s"] = {"old": old_over, "new": new_over}
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res["per_test_overhead_s"]))
    ledger(a, "session_overhead_per_test_old_s", old_over, f"engine open + restore/close + separate restore-check "
           f"session, median of {a.tests}; {run_dir.name}")
    ledger(a, "session_overhead_per_test_new_s", new_over, f"one engine for {a.tests} tests, restore byte-check in "
           f"the same session (close(verify_bytes=True)); {run_dir.name}")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- item 2


def cmd_clips(a) -> int:
    cat = C.load_catalog("current")
    run_dir = stamp_dir("clips")
    di = load_wav(DI10)[: 10 * reamp.SR]
    eng = engine(run_dir, whole_preset="fallback")
    res: dict = {"clips": {}}
    try:
        eng.load(P.read_hlx(FW380 / "K.hlx", cat))
        for sec in a.seconds:
            x, start = reamp.excerpt(di, sec)
            times = []
            for _ in range(a.repeats):
                t0 = time.perf_counter()
                eng.render(x)
                times.append(time.perf_counter() - t0)
            res["clips"][str(sec)] = {"start_s": start / reamp.SR, "render_s": times, "median_s": float(np.median(times))}
            print(sec, res["clips"][str(sec)], flush=True)
    finally:
        ok = finish(eng, res, run_dir)
    for sec, r in res["clips"].items():
        ledger(a, f"seconds_per_render_clip{float(sec):g}s", r["median_s"],
               f"K (delay off) {sec} s loudest excerpt of di_10s_tail; median of {a.repeats}; {run_dir.name}",
               renders=a.repeats)
    return 0 if ok else 1


# ----------------------------------------------------------------------------- item 3


def cmd_tails(a) -> int:
    cat = C.load_catalog("current")
    run_dir = stamp_dir("tails")
    dry = load_wav(DI2)
    eng = engine(run_dir, whole_preset="always")
    res: dict = {"chains": {}}
    try:
        for name in a.presets:
            p = P.read_hlx(FW380 / f"{name}.hlx", cat)
            r: dict = {}
            eng.tail_mode = "bound"
            eng.load(p)
            eng._flush_after = (0.0, 0.0)
            bound = []
            for _ in range(a.renders):
                t0 = time.perf_counter()
                y = eng.render(dry)
                bound.append({"wall_s": time.perf_counter() - t0, "wait_s": eng.last_wait_s, "y": y})
            eng.tail_mode = "measured"
            eng.decay_s.clear()
            eng.DECAY_REL_DB = a.rel_db
            meas = []
            for _ in range(a.renders + 1):
                t0 = time.perf_counter()
                y = eng.render(dry)
                meas.append({"wall_s": time.perf_counter() - t0, "wait_s": eng.last_wait_s, "y": y})
            key = eng._tail_key()
            r["bound_s"] = eng.pre_roll_s()
            r["measured_decay_s"] = eng.decay_s.get(key)
            r["bound_wall_s"] = [b["wall_s"] for b in bound]
            r["bound_wait_s"] = [b["wait_s"] for b in bound]
            r["measured_wall_s"] = [m["wall_s"] for m in meas]
            r["measured_wait_s"] = [m["wait_s"] for m in meas]
            # All pairs: bound-wait repeats (skipping the first render after the load), measured-wait
            # repeats (after the probe and its first follower), and measured against bound.
            import itertools

            import soundfile as sf

            bs, ms = [b["y"] for b in bound[1:]], [m["y"] for m in meas[2:]]
            for i, y in enumerate(bs):
                sf.write(run_dir / f"{name}_bound{i}.wav", y, reamp.SR, subtype="FLOAT")
            for i, y in enumerate(ms):
                sf.write(run_dir / f"{name}_meas{i}.wav", y, reamp.SR, subtype="FLOAT")
            r["rel_db"] = a.rel_db
            r["null_bound_repeat_db"] = [aligned_null(x, y) for x, y in itertools.combinations(bs, 2)]
            r["null_measured_repeat_db"] = [aligned_null(x, y) for x, y in itertools.combinations(ms, 2)]
            r["null_measured_vs_bound_db"] = [aligned_null(x, y) for x in bs for y in ms]
            res["chains"][name] = r
            print(name, json.dumps(r), flush=True)
    finally:
        eng.tail_mode = "bound"
        ok = finish(eng, res, run_dir)
    for name, r in res["chains"].items():
        ledger(a, f"tail_wait_bound_s_{name}", float(np.median(r["bound_wait_s"][1:])),
               f"{name}: upper-bound wait per render; {run_dir.name}")
        ledger(a, f"tail_wait_measured_s_{name}", float(np.median(r["measured_wait_s"][2:])),
               f"{name}: decay-sized wait per render after one probe (decay {r['measured_decay_s']:.2f} s at "
               f"{r['rel_db']} dB); median null measured-vs-bound {np.median(r['null_measured_vs_bound_db']):.1f} dB; "
               f"bound repeats {np.median(r['null_bound_repeat_db']):.1f}; measured repeats "
               f"{np.median(r['null_measured_repeat_db']):.1f}; {run_dir.name}")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- item 5


def cmd_params(a) -> int:
    import copy
    import random

    cat = C.load_catalog("current")
    run_dir = stamp_dir("params")
    eng = engine(run_dir, whole_preset="fallback")
    rng = random.Random(61)
    base = P.read_hlx(FW380 / "K.hlx", cat)
    amp = next(i for i, s in enumerate(base.slots) if s.category == "amp")
    trials = []
    for _ in range(a.trials):
        p = copy.deepcopy(base)
        for k in ("Drive", "Bass", "Mid", "Treble", "ChVol", "Master"):
            if k in p.slots[amp].params:
                p.slots[amp].params[k] = round(rng.uniform(0.1, 0.9), 3)
        trials.append(p)
    res: dict = {"every": {}}
    try:
        eng.load(base)
        for every in a.every:
            eng.readback_every = every
            eng._loads_since_readback = 0
            times = []
            for p in trials:
                t0 = time.perf_counter()
                eng.load(p)
                times.append(time.perf_counter() - t0)
            # A full readback at the end of each series proves the trusted loads landed.
            eng.readback_every = 1
            eng.load(trials[-1])
            res["every"][str(every)] = {"load_s": times, "median_s": float(np.median(times)),
                                        "final_readback_ok": eng.last_readback.get("ok"),
                                        "skipped": eng.readbacks_skipped}
            print(every, {k: v for k, v in res["every"][str(every)].items() if k != "load_s"}, flush=True)
    finally:
        ok = finish(eng, res, run_dir)
    for every, r in res["every"].items():
        ledger(a, f"param_load_s_readback_every{every}", r["median_s"],
               f"K amp 6 random params per in-place load; full readback every {every} load(s); final readback "
               f"ok={r['final_readback_ok']}; {a.trials} loads; {run_dir.name}")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- item 6


class Midi:
    """A persistent python-rtmidi process (the project venv has no rtmidi): `pc N` -> sends."""

    def __init__(self) -> None:
        code = ("import rtmidi,sys,time\no=rtmidi.MidiOut();p=o.get_ports();o.open_port(next(i for i,n in enumerate(p) "
                "if 'HX Stomp' in n))\nprint('ready',flush=True)\nfor l in sys.stdin:\n n=int(l.split()[1]);"
                "o.send_message([0xC0,n]);print('sent',time.time(),flush=True)\n")
        self.p = subprocess.Popen([MIDI_PY, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert self.p.stdout.readline().startswith("ready")

    def pc(self, n: int) -> float:
        self.p.stdin.write(f"pc {n}\n")
        self.p.stdin.flush()
        return float(self.p.stdout.readline().split()[1])

    def close(self) -> None:
        self.p.stdin.close()
        self.p.wait(timeout=5)


def cmd_switch(a) -> int:
    cat = C.load_catalog("current")
    run_dir = stamp_dir("switch")
    dry = load_wav(DI2)
    eng = engine(run_dir, whole_preset="always")
    b = eng.bridge
    res: dict = {"select": [], "pc": [], "whole": []}
    midi = Midi() if MIDI_PY else None
    try:
        o = eng.original
        for idx in a.indexes:
            t0 = time.perf_counter()
            r = b.call("select", setlist=o["setlist"], index=idx, timeout_s=20)
            ts = time.perf_counter() - t0
            # audio check: route the input to USB 5/6 (edit buffer only), render now and after 2 s
            b.call("route_key", pos=0, key=51, to=eng.cal.input_usb56)
            eng._refresh()
            y0 = eng.render(dry)
            time.sleep(2.0)
            y1 = eng.render(dry)
            res["select"].append({"index": idx, "label": r.get("label"), "name": r.get("name"), "select_s": ts,
                                  "null_now_vs_2s_db": aligned_null(y1, y0), "rms_dbfs": reamp.rms_db(y1)})
            print("select", res["select"][-1], flush=True)
        if midi:
            for idx in [*a.indexes, o["index"]]:
                sent = midi.pc(idx)
                while True:
                    info = b.call("info")
                    if info.get("index") == idx:
                        break
                    if time.time() - sent > 10:
                        raise RuntimeError(f"PC {idx}: the pedal did not switch within 10 s")
                    time.sleep(0.01)
                res["pc"].append({"index": idx, "pc_to_current_s": time.time() - sent})
                print("pc", res["pc"][-1], flush=True)
        b.call("select", setlist=o["setlist"], index=o["index"], timeout_s=20)
        eng._refresh()
        for name in ("K", "A"):
            t0 = time.perf_counter()
            eng.load(P.read_hlx(FW380 / f"{name}.hlx", cat))
            res["whole"].append({"preset": name, "load_s": time.perf_counter() - t0,
                                 "write_s": eng.whole_writes[-1]["write_s"]})
            print("whole", res["whole"][-1], flush=True)
    finally:
        if midi:
            midi.close()
        ok = finish(eng, res, run_dir)
    ledger(a, "preset_select_s", float(np.median([r["select_s"] for r in res["select"]])),
           f"select of stored presets {a.indexes} (read-only; device reports current); {run_dir.name}")
    if res["pc"]:
        ledger(a, "preset_pc_to_current_s", float(np.median([r["pc_to_current_s"] for r in res["pc"]])),
               f"MIDI Program Change sent -> info shows the preset (10 ms polling); {run_dir.name}")
    ledger(a, "whole_load_s_switch_run", float(np.median([r["load_s"] for r in res["whole"]])),
           f"whole edit-buffer load (backup+compile+write+readback) K and A in the same session; {run_dir.name}")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- end to end


def _trials(cat, name: str, n: int, seed: int = 61) -> list:
    import copy
    import random

    rng = random.Random(seed)
    base = P.read_hlx(FW380 / f"{name}.hlx", cat)
    amp = next(i for i, s in enumerate(base.slots) if s.category == "amp")
    out = []
    for _ in range(n):
        p = copy.deepcopy(base)
        for k in ("Drive", "Bass", "Mid", "Treble", "ChVol", "Master"):
            if k in p.slots[amp].params:
                p.slots[amp].params[k] = round(rng.uniform(0.1, 0.9), 3)
        out.append(p)
    return out


def cmd_batch(a) -> int:
    """A search batch end to end: `--trials` amp-parameter trials on `--chain`, each load + render.
    Before: full readback every load, upper-bound waits, `--old-clip` s. After: readback every 10th
    load, decay-sized waits, `--new-clip` s. One session each (session cost reported separately)."""
    cat = C.load_catalog("current")
    run_dir = stamp_dir("batch")
    di = load_wav(DI10)[: 10 * reamp.SR]
    trials = _trials(cat, a.chain, a.trials)
    res: dict = {}
    for label, every, mode, clip_s in (("before", 1, "bound", a.old_clip), ("after", 10, "measured", a.new_clip)):
        x, _ = reamp.excerpt(di, clip_s)
        t0 = time.perf_counter()
        eng = engine(run_dir, whole_preset="fallback")
        t_open = time.perf_counter() - t0
        eng.readback_every, eng.tail_mode = every, mode
        try:
            eng.load(trials[0])
            eng._loads_since_readback = 0
            t1 = time.perf_counter()
            for p in trials:
                eng.load(p)
                eng.render(x)
            work = time.perf_counter() - t1
            eng.readback_every = 1
            eng.load(trials[-1])
            final_ok = eng.last_readback.get("ok")
        finally:
            t2 = time.perf_counter()
            r: dict = {}
            ok = finish(eng, r, run_dir)
            t_close = time.perf_counter() - t2
        res[label] = {"restore_report": r.get("restore_report"),
                      "per_trial_s": work / len(trials), "work_s": work, "open_s": t_open, "close_s": t_close,
                      "clip_s": clip_s, "readback_every": every, "tail_mode": mode, "final_readback_ok": final_ok,
                      "restore_ok": ok}
        print(label, res[label], flush=True)
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    for label, r in res.items():
        ledger(a, f"search_batch_per_trial_s_{label}", r["per_trial_s"],
               f"{a.trials} amp-param trials on {a.chain}; clip {r['clip_s']} s; readback every {r['readback_every']}; "
               f"tails {r['tail_mode']}; final readback ok={r['final_readback_ok']}; {run_dir.name}",
               renders=a.trials)
    return 0 if all(r["restore_ok"] for r in res.values()) else 1


def cmd_verify(a) -> int:
    """A verification batch end to end: each finalist whole-written and rendered `--renders` times on
    the 10 s clip. Before: a session per finalist plus a restore-check session, upper-bound waits.
    After: one session, decay-sized waits, restore check in the same session."""
    from tonematch.engines.stomp_bridge import StompBridge

    cat = C.load_catalog("current")
    run_dir = stamp_dir("verify")
    x = load_wav(DI10)[: 10 * reamp.SR]
    presets = [P.read_hlx(FW380 / f"{n}.hlx", cat) for n in a.presets]
    res: dict = {}
    t0 = time.perf_counter()
    for p in presets:
        eng = engine(run_dir, whole_preset="always")
        eng.tail_mode = "bound"
        try:
            eng.load(p)
            for _ in range(a.renders):
                eng.render(x)
        finally:
            eng.close()
        with StompBridge() as b:
            b.call("info")
            b.call("backup", path=str(run_dir / "old_after.bin"))
    res["before"] = {"per_finalist_s": (time.perf_counter() - t0) / len(presets)}
    print("before", res["before"], flush=True)
    t0 = time.perf_counter()
    eng = engine(run_dir, whole_preset="always")
    eng.tail_mode = "measured"
    try:
        for p in presets:
            eng.load(p)
            for _ in range(a.renders):
                eng.render(x)
    finally:
        r: dict = {}
        ok = finish(eng, r, run_dir)
    res["after"] = {"per_finalist_s": (time.perf_counter() - t0) / len(presets), "restore_ok": ok}
    print("after", res["after"], flush=True)
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    for label, r in res.items():
        ledger(a, f"verify_batch_per_finalist_s_{label}", r["per_finalist_s"],
               f"{len(presets)} finalists {a.presets} whole-written, {a.renders} x 10 s renders each; "
               f"{'session per finalist + restore-check session, bound waits' if label == 'before' else 'one session, measured waits'}; "
               f"{run_dir.name}", renders=len(presets) * a.renders)
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sessions")
    s.add_argument("--tests", type=int, default=3)
    c = sub.add_parser("clips")
    c.add_argument("--seconds", type=float, nargs="+", default=[2, 3, 4, 10])
    c.add_argument("--repeats", type=int, default=3)
    t = sub.add_parser("tails")
    t.add_argument("--presets", nargs="+", default=["Cdly", "Crev"])
    t.add_argument("--renders", type=int, default=4)
    t.add_argument("--rel-db", type=float, default=-60.0, help="decay threshold below the clip peak")
    pa = sub.add_parser("params")
    pa.add_argument("--trials", type=int, default=20)
    pa.add_argument("--every", type=int, nargs="+", default=[1, 10])
    sw = sub.add_parser("switch")
    sw.add_argument("--indexes", type=int, nargs="+", default=[12, 13, 10])
    ba = sub.add_parser("batch")
    ba.add_argument("--chain", default="Cdly0")
    ba.add_argument("--trials", type=int, default=10)
    ba.add_argument("--old-clip", type=float, default=10.0)
    ba.add_argument("--new-clip", type=float, default=3.0)
    ve = sub.add_parser("verify")
    ve.add_argument("--presets", nargs="+", default=["K", "Cdly0", "Crev0", "N1", "Cmod"])
    ve.add_argument("--renders", type=int, default=2)
    a = ap.parse_args(argv)
    if subprocess.run(["pgrep", "-f", "HX Edit.app/Contents/MacOS/HX Edit"], capture_output=True, check=False).stdout.strip():
        sys.exit("HX Edit is running: quit it first (it holds the control interface)")
    for var in ("TONEMATCH_STOMPD", "TONEMATCH_STOMPD_COMPILE"):
        if not os.environ.get(var):
            sys.exit(f"set {var}")
    return {"sessions": cmd_sessions, "clips": cmd_clips, "tails": cmd_tails, "params": cmd_params,
            "switch": cmd_switch, "batch": cmd_batch, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
