#!/usr/bin/env python3
"""T213 (learning): one unattended supervisor for the slot-dataset-v1 overnight run (human 2026-10-22: "you have 12
hours on the cpu. run what you need"; T212 options A and B approved). Measurement only: nothing is admitted.

In order, without agents:
  1. wait for the running amp batch (--wait-pid) to exit; resume amp until every planned shard is done;
  2. amp refill (`refill --slot amp`, floor -240 dBFS, out_db stored), render it;
  3. train amp at half and full size with --with-refill (training lane, nice 15, 2 threads, alongside the next render);
  4. cab, delay, mod, reverb, eq in that order: plan with --silence-db -240 (out_db stored), render, refill if any
     non-digital-zero silence failures, train half and full. A slot whose render cannot finish before the deadline
     is skipped (not started), never left half-rendered by choice; a stopped render stays resumable;
  5. after every step: scripts/proc_check.py (report only) and ledger rows (render cost here; training rows are
     appended by `slot_dataset_render.py train`);
  6. docs/notes/slot-datasets-v1-results.md, rewritten after every training and at the end.

    scripts/slot_overnight.sh --wait-pid 78416 --deadline "2026-10-23 09:00"   # launches this in the background
    cat cache/slot-dataset-v1/overnight/status.txt                               # one status line
    tail -f cache/slot-dataset-v1/overnight/overnight.log
    kill <PID>       # stop safely: the render finishes its current shard and closes its hosts, training is stopped

Busy-core guard: the render workers keep drive's gate (pause above 0.95 x cores busy); training runs at nice 15 with
2 threads, so the two lanes never put more than the core count to work (the renders pause instead).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

NEW_SLOTS = ("cab", "delay", "mod", "reverb", "eq")
SIZES = (("half", 0.5), ("full", 1.0))       # human 2026-10-22: half- and full-size training
RENDER_MARGIN = 1.15                          # an estimate must fit with 15 % to spare
PLAN_S = 300.0                                # planning reads every eligible DI file once (a few minutes)


# ------------------------------------------------------------------------------------------ pure helpers

def fits(now: float, est_s: float, deadline: float, margin: float = RENDER_MARGIN, extra_s: float = 0.0) -> bool:
    """True if a job of est_s seconds (times margin, plus extra_s) ends before the deadline."""
    return now + est_s * margin + extra_s <= deadline


def render_estimate_s(clips_remaining: int, rate: float | None) -> float:
    """Seconds to render the remaining clips at the last measured rate (clips/s); inf without a rate."""
    if clips_remaining <= 0:
        return 0.0
    return clips_remaining / rate if rate and rate > 0 else math.inf


def refill_candidates(failures: list[dict], refill_plan: dict | None) -> int:
    """Silence failures that a -240 dBFS refill could keep: not digital zero, not already in the refill plan."""
    have = {c["id"] for c in (refill_plan or {}).get("clips", [])}
    return len({x["id"] for x in failures if not x.get("digital_zero") and x["id"] not in have})


def parse_deadline(s: str) -> float:
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M"))


def hhmm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def dur(s: float) -> str:
    if not math.isfinite(s):
        return "unknown"
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}"


# ------------------------------------------------------------------------------------------ results note

def _r(x) -> str:
    return "" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:.3f}"


def results_note(base: Path, slots: list[str], events: list[dict], trains: list[dict], deadline: float) -> str:
    """The measurement note: per slot clips and failures, training per head at half vs full, wall times, problems."""
    import drive_dataset_render as DD
    import slot_dataset_render as SR
    out = ["# slot-dataset-v1 overnight results (T213)", "",
           "Measurement only. Nothing here is admitted into the matcher. All slot-dataset-v1 defaults as approved on "
           "2026-10-22; T212 options A (amp refill at -240 dBFS, out_db stored, digital-zero clips skipped, training "
           "with --with-refill keeping every clip) and B (new slot plans store the -240 dBFS floor and out_db). "
           "Training: the T189 heads (logreg, MLP), multi-crop, CPU, at half (the first half of the plan's shards, a "
           "seeded random half) and full size. Knob accuracy: within +-0.15 of the true position with the true "
           "model's knob predictions, pooled over the model's knobs (true) and with the head's own top-1 model "
           "(own). Evaluation on the held-out DI files (test) and the T186/T189 test files (v1test).", "",
           f"Written by scripts/slot_overnight.py, {time.strftime('%Y-%m-%d %H:%M')} local; window ends "
           f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline))}.", "",
           "## Clips and failures", "",
           "| Slot | Planned | Rendered ok | Failed | Silence (digital zero) | Refill planned / ok / failed "
           "| Render wall (this supervisor) |", "|---|---|---|---|---|---|---|"]
    for slot in slots:
        pp = base / slot / "plan.json"
        if not pp.is_file():
            out.append(f"| {slot} | no plan | | | | | |")
            continue
        plan = json.loads(pp.read_text())
        st = DD.shard_stats(base / slot, plan)
        fails = SR.failed_silence(base / slot)
        rp = base / f"{slot}-refill" / "plan.json"
        rtxt = "-"
        if rp.is_file():
            rpl = json.loads(rp.read_text())
            rs = DD.shard_stats(base / f"{slot}-refill", rpl)
            rtxt = f"{rs['clips_planned']} / {rs['clips_ok']} / {rs['clips_failed']}"
        wall = sum(e.get("wall_s", 0) for e in events if e.get("kind") == "render" and e.get("slot") in
                   (slot, f"{slot}-refill"))
        out.append(f"| {slot} | {st['clips_planned']:,} | {st['clips_ok']:,} | {st['clips_failed']:,} | "
                   f"{len(fails)} ({sum(x['digital_zero'] for x in fails)}) | {rtxt} | {dur(wall)} |")
    out.append("")
    out += ["## Training (main arm, within player)", "",
            "| Slot | Size | Clips | Head | Eval | Model top-1 | Top-3 | Knobs within +-0.15 true / own | "
            "All knobs within (true) | Fit s |", "|---|---|---|---|---|---|---|---|---|---|"]
    for t in trains:
        res = t.get("result")
        if not res:
            out.append(f"| {t['slot']} | {t['size']} | | | | {t.get('status', '')} | | | | |")
            continue
        for key, r in res["results"].items():
            arm, head, _p, seed = key.split("/")
            if arm != "main":
                continue
            for ev in ("test", "v1test"):
                if ev in r:
                    e = r[ev]
                    out.append(f"| {t['slot']} | {t['size']} | {res['n']:,} | {head} {seed} | {ev} | "
                               f"{_r(e.get('model_top1'))} | {_r(e.get('model_top3'))} | "
                               f"{_r(e.get('knob_within_true'))} / {_r(e.get('knob_within_top1'))} | "
                               f"{_r(e.get('all_within_true'))} | {r.get('fit_s', 0):.0f} |")
    out.append("")
    out += ["## Training (cross player)", "",
            "| Slot | Size | Arm | Head | Eval | Model top-1 | Top-3 | Knobs within true / own |",
            "|---|---|---|---|---|---|---|---|"]
    for t in trains:
        res = t.get("result")
        for key, r in (res or {}).get("results", {}).items():
            arm, head, _p, seed = key.split("/")
            if arm == "main":
                continue
            for ev in ("test", "v1test"):
                if ev in r:
                    e = r[ev]
                    out.append(f"| {t['slot']} | {t['size']} | {arm} | {head} | {ev} | {_r(e.get('model_top1'))} | "
                               f"{_r(e.get('model_top3'))} | {_r(e.get('knob_within_true'))} / "
                               f"{_r(e.get('knob_within_top1'))} |")
    out.append("")
    out += ["## Per knob, within +-0.15 (main MLP, held-out test files, true model): half vs full", ""]
    by_slot: dict[str, dict[str, dict]] = {}
    for t in trains:
        bn = ((t.get("result") or {}).get("results", {}).get("main/mlp/multi/s0", {}).get("test", {})
              .get("by_name"))
        if bn:
            by_slot.setdefault(t["slot"], {})[t["size"]] = bn
    for slot, sizes in by_slot.items():
        names = sorted(set().union(*[set(v) for v in sizes.values()]))
        out += [f"### {slot}", "", "| Knob | Half | Full | n (full) |", "|---|---|---|---|"]
        for nm in names:
            h, f = sizes.get("half", {}).get(nm), sizes.get("full", {}).get(nm)
            out.append(f"| {nm} | {_r(h and h['within_true'])} | {_r(f and f['within_true'])} | "
                       f"{(f or h or {}).get('n', '')} |")
        out.append("")
    out += ["## Wall times", "", "| Step | Slot | Start | End | Wall | Result |", "|---|---|---|---|---|---|"]
    for e in events:
        if "t0" in e:
            out.append(f"| {e['kind']} | {e.get('slot', '')}{(' ' + e['size']) if e.get('size') else ''} | "
                       f"{hhmm(e['t0'])} | {hhmm(e.get('t1', e['t0']))} | {dur(e.get('wall_s', 0))} | "
                       f"{e.get('result', '')} |")
    out.append("")
    probs = [e for e in events if e.get("problem")]
    out += ["## What went wrong", ""]
    out += [f"- {hhmm(e.get('t1', e.get('t0', time.time())))} {e.get('slot', '')}: {e['problem']}" for e in probs] \
        or ["- Nothing recorded by the supervisor."]
    out.append("")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------------------------ supervisor

class Overnight:
    def __init__(self, a):
        import slot_dataset_render as SR
        self.SR = SR
        self.a = a
        self.base = SR.BASE
        # the window's end (human 2026-10-22: 08:00) minus a stop margin: a render told to stop at its --hours
        # still finishes its current shard and closes its hosts, and the final proc_check must be clean by the end
        self.window_end = parse_deadline(a.deadline)
        self.deadline = self.window_end - 60.0 * a.stop_margin_min
        self.dir = self.base / "overnight"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "overnight.log"
        self.status_path = self.dir / "status.txt"
        self.py = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
        self.events: list[dict] = []
        self.trains: list[dict] = []
        self.rate: float | None = None
        self.train_s_per_clip: float | None = None
        self.stop = threading.Event()
        self.children: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()
        self.tq: queue.Queue = queue.Queue()
        self.now_render = "starting"
        self.now_train = "idle"
        self.note_path = Path(a.note) if a.note else ROOT / "docs/notes/slot-datasets-v1-results.md"
        self.slots = ["amp"] + [s for s in a.slots.split(",") if s]
        nice0 = os.nice(0)
        self.render_nice = max(0, 10 - nice0)      # renders at nice 10 (as slot_batch.sh), training at 15
        self.train_nice = max(0, 15 - nice0)

    # ---- plumbing
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        with self.lock, open(self.log_path, "a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def status(self) -> None:
        parts = [time.strftime("%H:%M:%S"), f"render: {self.now_render}", f"train: {self.now_train}",
                 f"queued trainings {self.tq.qsize()}", f"deadline {hhmm(self.deadline)}"]
        for slot in self.slots:
            pp = self.base / slot / "plan.json"
            if pp.is_file():
                try:
                    import drive_dataset_render as DD
                    st = DD.shard_stats(self.base / slot, json.loads(pp.read_text()))
                    parts.append(f"{slot} {st['shards_done']}/{st['shards_total']}")
                except Exception:  # noqa: BLE001  (a status line must never stop the run)
                    pass
        from tonematch.engines.elastic import atomic_write_text
        atomic_write_text(self.status_path, " | ".join(parts) + "\n")
        atomic_write_text(self.dir / "status.json", json.dumps({"events": self.events, "trains": [
            {k: v for k, v in t.items() if k != "result"} for t in self.trains]}, indent=1, default=str))

    def env(self, train: bool = False) -> dict:
        e = dict(os.environ)
        e.setdefault("TONEMATCH_AGENT", "learning")
        e["TONEMATCH_AFXREP_DEVICE"] = "cpu"
        if train:
            e.update({"TONEMATCH_TRAIN_THREADS": "2", "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
                      "MKL_NUM_THREADS": "2", "VECLIB_MAXIMUM_THREADS": "2"})
        return e

    def run(self, name: str, argv: list[str], *, nice: int = 0, train: bool = False, log_file: Path | None = None,
            pidfile: Path | None = None, kill_at_deadline: bool = False) -> int:
        cmd = (["nice", "-n", str(nice)] if nice else []) + argv
        lf = open(log_file or self.log_path, "ab")
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(ROOT),
                             env=self.env(train))
        with self.lock:
            self.children[name] = p
        if pidfile:
            pidfile.write_text(f"{p.pid}\n")
        self.log(f"{name}: started PID {p.pid}: {' '.join(argv[1:])}")
        termed = False
        while p.poll() is None:
            hard = time.time() > self.window_end - 300      # backstop: nothing of ours is alive at the window's end
            if not termed and (self.stop.is_set() or hard or (kill_at_deadline and time.time() > self.deadline)):
                why = "stop requested" if self.stop.is_set() else "window ends in 5 min" if hard else \
                    "deadline reached"
                self.log(f"{name}: {why}; SIGTERM to PID {p.pid}")
                p.terminate()
                termed = True
            time.sleep(5)
        with self.lock:
            self.children.pop(name, None)
        lf.close()
        self.log(f"{name}: PID {p.pid} exited {p.returncode}")
        return p.returncode

    def proc_check(self, after: str) -> None:
        def once():
            return subprocess.run([self.py, str(ROOT / "scripts/proc_check.py")], capture_output=True, text=True,
                                  cwd=str(ROOT))
        r = once()
        if r.returncode != 0:     # a child of this supervisor that just exited is a zombie until its lane reaps it
            time.sleep(10)        # (the lanes poll every 5 s): look again before calling it a problem
            r = once()
        txt = (r.stdout + r.stderr).strip().replace("\n", " / ")
        self.log(f"proc_check after {after}: exit {r.returncode}: {txt[:600]}")
        if r.returncode != 0:
            self.events.append({"kind": "proc_check", "slot": after, "problem": f"proc_check flagged: {txt[:300]}"})

    def ledger(self, **kw) -> None:
        if self.a.no_ledger:
            return
        argv = [self.py, str(ROOT / "scripts/ledger_append.py")]
        for k, v in kw.items():
            argv += [f"--{k.replace('_', '-')}", str(v)]
        r = subprocess.run(argv, capture_output=True, text=True, cwd=str(ROOT), env=self.env())
        self.log(f"ledger {kw.get('dataset')} {kw.get('metric')}={kw.get('value')}: exit {r.returncode}")

    def stats(self, d: Path) -> dict | None:
        import drive_dataset_render as DD
        pp = d / "plan.json"
        if not pp.is_file():
            return None
        return DD.shard_stats(d, json.loads(pp.read_text()))

    def complete(self, d: Path) -> bool:
        st = self.stats(d)
        return bool(st) and st["shards_done"] >= st["shards_total"]

    def render_cost_row(self, name: str, d: Path, note: str) -> dict:
        try:
            pr = json.loads((d / "progress.json").read_text())
        except (OSError, ValueError):
            return {}
        n, el = int(pr.get("clips_ok_this_run", 0)), float(pr.get("elapsed_s", 0))
        rate = n / el if el > 0 else 0.0
        if n > 0:
            self.rate = rate
        self.ledger(kind="render-cost", engine="native", dataset=f"slot-dataset-v1-{name}", metric="clips_per_s_wall",
                    value=f"{rate:.3f}", renders=n, wall_clock_s=f"{el:.0f}",
                    notes=(f"T213 {note}; up to 4 Native hosts; {pr.get('shards_done')}/{pr.get('shards_total')} "
                           f"shards done; total ok {pr.get('clips_ok')} failed {pr.get('clips_failed')}; render "
                           f"{pr.get('render_s_sum')} s embed {pr.get('embed_s_sum')} s summed over hosts; features "
                           "only; measurement only not adopted"))
        return pr

    # ---- render lane
    def render_until_done(self, slot: str, refill: bool = False) -> bool:
        name = f"{slot}-refill" if refill else slot
        d = self.base / name
        for attempt in range(1, 4):
            if self.complete(d):
                return True
            left = self.deadline - time.time()
            if self.stop.is_set() or left < 120:
                return False
            self.now_render = f"{name} (attempt {attempt}, until {hhmm(self.deadline)})"
            self.status()
            t0 = time.time()
            argv = [self.py, str(ROOT / "scripts/slot_dataset_render.py"), "render", "--slot", slot,
                    "--hours", f"{left / 3600:.3f}"] + (["--refill"] if refill else []) \
                + (["--hosts", str(self.a.hosts)] if self.a.hosts else [])
            rc = self.run(f"render-{name}", argv, nice=self.render_nice, kill_at_deadline=False,
                          log_file=d / f"batch-{time.strftime('%m%d-%H%M')}.log", pidfile=d / "job.pid")
            t1 = time.time()
            st = self.stats(d) or {}
            ev = {"kind": "render", "slot": name, "t0": t0, "t1": t1, "wall_s": t1 - t0, "rc": rc,
                  "result": f"{st.get('shards_done')}/{st.get('shards_total')} shards, ok {st.get('clips_ok')}, "
                            f"failed {st.get('clips_failed')}"}
            stop_reason = (d / "STOP_REASON")
            if stop_reason.exists():
                ev["problem"] = f"render stopped: {stop_reason.read_text().strip()[:200]}"
            elif rc not in (0, 143):
                ev["problem"] = f"render exited {rc}"
            self.events.append(ev)
            self.render_cost_row(name, d, f"{name} render by the overnight supervisor (attempt {attempt})")
            self.proc_check(f"render {name}")
            self.status()
        return self.complete(d)

    def refill_step(self, slot: str) -> bool:
        """Refill (T212 A/B) when any silence failure is not digital zero. Returns True if a refill plan exists."""
        fails = self.SR.failed_silence(self.base / slot)
        rp = self.base / f"{slot}-refill" / "plan.json"
        old = json.loads(rp.read_text()) if rp.is_file() else None
        k = refill_candidates(fails, old)
        nz = sum(x["digital_zero"] for x in fails)
        self.log(f"{slot}: {len(fails)} silence failures ({nz} digital zero); {k} new refill candidates")
        if k:
            t0 = time.time()
            rc = self.run(f"refill-plan-{slot}", [self.py, str(ROOT / "scripts/slot_dataset_render.py"), "refill",
                                                  "--slot", slot, "--silence-db", str(self.a.silence_db)])
            self.events.append({"kind": "refill-plan", "slot": slot, "t0": t0, "t1": time.time(),
                                "wall_s": time.time() - t0, "result": f"{k} clips added (rc {rc})",
                                **({"problem": f"refill plan exited {rc}"} if rc else {})})
        if rp.is_file():
            ok = self.render_until_done(slot, refill=True)
            if not ok:
                self.events.append({"kind": "render", "slot": f"{slot}-refill",
                                    "problem": "refill render not complete before the deadline or stop"})
            self.proc_check(f"refill {slot}")
            return True
        return False

    def plan_step(self, slot: str) -> bool:
        if (self.base / slot / "plan.json").is_file():
            return True
        t0 = time.time()
        self.now_render = f"planning {slot}"
        self.status()
        argv = [self.py, str(ROOT / "scripts/slot_dataset_render.py"), "plan", "--slot", slot,
                "--silence-db", str(self.a.silence_db)] + (["--limit", str(self.a.limit)] if self.a.limit else [])
        rc = self.run(f"plan-{slot}", argv, nice=self.render_nice)
        self.events.append({"kind": "plan", "slot": slot, "t0": t0, "t1": time.time(), "wall_s": time.time() - t0,
                            "result": f"rc {rc}", **({"problem": f"plan exited {rc}"} if rc else {})})
        self.proc_check(f"plan {slot}")
        return rc == 0 and (self.base / slot / "plan.json").is_file()

    def enqueue_training(self, slot: str, with_refill: bool) -> None:
        for size, frac in SIZES:
            self.tq.put({"slot": slot, "size": size, "fraction": frac, "with_refill": with_refill})

    def render_lane(self) -> None:
        a = self.a
        if a.wait_pid:
            self.now_render = f"waiting for PID {a.wait_pid} (amp batch)"
            self.status()
            self.log(f"waiting for PID {a.wait_pid} to exit")
            t0 = time.time()
            while not self.stop.is_set():
                try:
                    os.kill(a.wait_pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    pass
                time.sleep(20)
                if int(time.time()) % 300 < 20:
                    self.status()
            self.events.append({"kind": "wait", "slot": "amp", "t0": t0, "t1": time.time(),
                                "wall_s": time.time() - t0, "result": f"PID {a.wait_pid} exited"})
            if self.stop.is_set():
                return
            self.render_cost_row("amp", self.base / "amp", f"amp batch (PID {a.wait_pid}; scripts/slot_batch.sh amp "
                                 "7; started before the supervisor)")
            self.proc_check(f"PID {a.wait_pid}")
        for slot in self.slots:
            if self.stop.is_set():
                break
            d = self.base / slot
            if slot != "amp":
                st = self.stats(d)
                planned = st["clips_planned"] - st["clips_ok"] - st["clips_failed"] if st else \
                    (min(a.limit, self.SR.SD.SLOT_CONFIG[slot]["target"]) if a.limit else
                     self.SR.SD.SLOT_CONFIG[slot]["target"])
                est = render_estimate_s(planned, self.rate)
                if not fits(time.time(), est, self.deadline, extra_s=0.0 if st else PLAN_S):
                    msg = (f"skipped: {planned:,} clips at {self.rate or 0:.2f} clips/s need about {dur(est)} "
                           f"(x{RENDER_MARGIN}); {dur(self.deadline - time.time())} left")
                    self.log(f"{slot}: {msg}")
                    self.events.append({"kind": "skip", "slot": slot, "t0": time.time(), "result": msg,
                                        "problem": msg})
                    continue
                if not self.plan_step(slot):
                    continue
            elif not d.joinpath("plan.json").is_file():
                self.events.append({"kind": "render", "slot": slot, "problem": "no amp plan"})
                continue
            if not self.render_until_done(slot):
                self.events.append({"kind": "render", "slot": slot, "t0": time.time(),
                                    "problem": "render not complete (deadline, stop or failing workers); "
                                               "resumable; not trained"})
                continue
            has_refill = self.refill_step(slot)
            self.enqueue_training(slot, has_refill)
        self.now_render = "done"
        self.status()

    # ---- training lane
    def train_lane(self) -> None:
        while True:
            job = self.tq.get()
            if job is None:
                return
            slot, size = job["slot"], job["size"]
            st = self.stats(self.base / slot) or {}
            n = int(st.get("clips_ok", 0) * job["fraction"])
            est = n * self.train_s_per_clip if self.train_s_per_clip else 0.0
            rec = {"slot": slot, "size": size, "clips_est": n}
            self.trains.append(rec)
            if self.stop.is_set() or not fits(time.time(), est, self.deadline, margin=1.1, extra_s=60):
                rec["status"] = (f"skipped: about {dur(est)} needed, {dur(self.deadline - time.time())} left"
                                 if not self.stop.is_set() else "skipped: stop requested")
                self.events.append({"kind": "train", "slot": slot, "size": size, "t0": time.time(),
                                    "result": rec["status"], "problem": f"{size} training {rec['status']}"})
                self.log(f"train {slot} {size}: {rec['status']}")
                continue
            self.now_train = f"{slot} {size} (est {dur(est) if est else 'unknown'})"
            self.status()
            out = self.base / slot
            before = set(out.glob("summary-*.json"))
            t0 = time.time()
            argv = [self.py, str(ROOT / "scripts/slot_dataset_render.py"), "train", "--slot", slot,
                    "--fraction", str(job["fraction"])] + (["--with-refill"] if job["with_refill"] else []) \
                + (["--no-ledger"] if self.a.no_ledger else [])
            rc = self.run(f"train-{slot}-{size}", argv, nice=self.train_nice, train=True, kill_at_deadline=True,
                          log_file=out / f"train-{size}-{time.strftime('%m%d-%H%M')}.log")
            t1 = time.time()
            new = sorted(set(out.glob("summary-*.json")) - before, key=lambda p: p.stat().st_mtime)
            ev = {"kind": "train", "slot": slot, "size": size, "t0": t0, "t1": t1, "wall_s": t1 - t0, "rc": rc}
            if rc == 0 and new:
                res = json.loads(new[-1].read_text())
                rec.update({"status": "done", "summary": str(new[-1]), "result": res, "wall_s": t1 - t0})
                if res.get("n"):
                    self.train_s_per_clip = (t1 - t0) / res["n"]
                m = res["results"].get("main/mlp/multi/s0", {}).get("test", {})
                ev["result"] = (f"n {res['n']}; main MLP test top-1 {_r(m.get('model_top1'))} top-3 "
                                f"{_r(m.get('model_top3'))} knobs {_r(m.get('knob_within_true'))}")
            else:
                rec["status"] = f"failed (rc {rc})"
                ev["result"] = rec["status"]
                ev["problem"] = f"{size} training exited {rc}; see {out}/train-{size}-*.log"
            self.events.append(ev)
            self.proc_check(f"train {slot} {size}")
            self.now_train = "idle"
            self.write_note()
            self.status()

    def write_note(self) -> None:
        try:
            from tonematch.engines.elastic import atomic_write_text
            atomic_write_text(self.note_path, results_note(self.base, self.slots, self.events, self.trains,
                                                           self.window_end))
            self.log(f"wrote {self.note_path}")
        except Exception as e:  # noqa: BLE001  (a note failure must not stop the run)
            self.log(f"results note failed: {type(e).__name__}: {e}")

    def on_signal(self, signum, frame) -> None:
        self.log(f"signal {signum}: stopping; the render finishes its current shard, training is stopped")
        self.stop.set()

    def main(self) -> int:
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)
        (self.dir / "overnight.pid").write_text(f"{os.getpid()}\n")
        own = os.nice(0)
        self.log(f"T213 overnight supervisor PID {os.getpid()}: slots {','.join(self.slots)}; window ends "
                 f"{self.a.deadline}, nothing runs past {hhmm(self.deadline)} (stop margin {self.a.stop_margin_min:g} "
                 f"min); floor {self.a.silence_db} dBFS for new plans and refills; renders at nice "
                 f"{own + self.render_nice}, training at nice {own + self.train_nice} with 2 threads; base {self.base}")
        self.proc_check("start")
        tl = threading.Thread(target=self.train_lane, name="train-lane", daemon=True)
        tl.start()
        try:
            self.render_lane()
        except Exception as e:  # noqa: BLE001
            self.log(f"render lane crashed: {type(e).__name__}: {e}")
            self.events.append({"kind": "render", "t0": time.time(), "problem": f"render lane crashed: {e}"})
        self.tq.put(None)
        while tl.is_alive():
            tl.join(30)
            self.status()
        self.now_render, self.now_train = "finished", "finished"
        self.proc_check("end")
        self.write_note()
        self.status()
        self.log("supervisor finished")
        return 0


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--deadline", required=True, help='local "YYYY-MM-DD HH:MM"; nothing new starts that cannot end')
    ap.add_argument("--wait-pid", type=int, default=0, help="the running amp batch to wait for")
    ap.add_argument("--slots", default=",".join(NEW_SLOTS), help="after amp, in order")
    ap.add_argument("--silence-db", type=float, default=-240.0)
    ap.add_argument("--stop-margin-min", type=float, default=15.0,
                    help="stop everything this many minutes before --deadline (renders finish their shard)")
    ap.add_argument("--limit", type=int, default=0, help="smoke only: plan this many clips per new slot")
    ap.add_argument("--hosts", type=int, default=0, help="Native hosts per render (default the renderer's 4)")
    ap.add_argument("--note", default="", help="results note path (default docs/notes/slot-datasets-v1-results.md)")
    ap.add_argument("--no-ledger", action="store_true", help="smoke: no ledger rows")
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.exit(Overnight(parse()).main())
