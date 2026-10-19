#!/usr/bin/env python3
"""Live preview of a running search on the HX Stomp (T166). Owner: stomp-engine.

Human request 2026-10-18: "every time there is better fit on the model, put that preset on the
helix so I can hear the current progress". The human plays guitar through the Stomp while a
search (Native) runs; this watches the search's progress directory and puts each new best preset
into the Stomp's EDIT BUFFER.

  - Watches ``<dir>/current_best.json`` (seq, loss, model, category, values, ...) and
    ``<dir>/current_best.hlx``, written atomically by the search (scripts/match_block.py
    ProgressWriter: the .hlx first, then the json). A pair is loaded only when it is consistent:
    the .hlx's preset name ends in ``s<seq>`` (or, failing that, its block model is the json's model).
  - Loads whenever ``seq`` increases, at most one load per ``--throttle-s`` (default 5 s). If several
    improvements arrive in between, only the latest is loaded (the file is read at load time).
  - Edit buffer only: never a flash write, never a save. Loads go through StompEngine (whole-preset
    write into the edit buffer, full readback), inside one ``tonematch.pedal_lock.session`` for the
    whole run, so no other agent renders on the pedal while the human listens.
  - The Input block stays on the guitar input (Main L/R, ``@input`` 1), NOT USB 5/6, so the human
    hears the guitar through the preset. Exactly the searched blocks are loaded: no amp, cab or
    other block is added; no pilot block (slip guard off), no sentinel; nothing is rendered.
  - Refuses to start unless HX Edit is closed and the pedal is on the scratch preset 04C
    "Smoke Known" (checked read-only, before any backup or select).
  - On exit, Ctrl-C, SIGTERM or SIGHUP the pedal's original preset is restored and verified
    (``StompEngine.close(verify_bytes=True)``) unless ``--no-restore-on-exit``.

  python scripts/stomp_live_preview.py --dir cache/matches/Mother-0-68s-drive
  python scripts/stomp_live_preview.py --dir ... --once          # load the current best, hold until Ctrl-C

Needs TONEMATCH_STOMPD and TONEMATCH_STOMPD_COMPILE (default cache/stomp/bin/stompd{,-compile}).
Output: ``cache/stomp/hw/<stamp>-live-preview/`` (preview.log, status.json, summary.json, backups).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SCRATCH = (0, 11, "Smoke Known")   # 04C, the scratch slot the human named
INPUT_MAIN_LR = 1                  # Input block "@input" for Main L/R on the HX Stomp (HX Edit exports)
DEFAULT_THROTTLE_S = 5.0
KEY_KNOBS = ("Drive", "Gain", "Distortion", "Fuzz", "Sustain", "Tone", "Bass", "Mid", "Treble",
             "Level", "Volume", "Output", "Mix")
_SEQ_RE = re.compile(r"\bs(\d+)\s*$")


# --------------------------------------------------------------------------------------- reading


@dataclasses.dataclass
class Best:
    seq: int
    loss: float
    model: str
    category: str | None
    values: dict
    hlx: Path
    doc: dict


def _hlx_seq_and_models(hlx: Path) -> tuple[int | None, list[str]]:
    d = json.loads(hlx.read_text(encoding="utf-8"))
    tone = d.get("data", {}).get("tone", {})
    name = str(d.get("data", {}).get("meta", {}).get("name", ""))
    m = _SEQ_RE.search(name)
    models = [b.get("@model") for dsp in ("dsp0", "dsp1") for k, b in (tone.get(dsp) or {}).items()
              if k.startswith("block") and isinstance(b, dict)]
    return (int(m.group(1)) if m else None), [x for x in models if x]


def read_best(directory: Path) -> Best | None:
    """The current best when current_best.json and current_best.hlx exist and agree, else None
    (missing, half-written, or the .hlx already one step ahead of the json: read again next poll)."""
    j, h = directory / "current_best.json", directory / "current_best.hlx"
    try:
        doc = json.loads(j.read_text(encoding="utf-8"))
        seq = int(doc["seq"])
        hseq, models = _hlx_seq_and_models(h)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if hseq is not None and hseq != seq:
        return None
    if hseq is None and doc.get("model") not in models:
        return None
    return Best(seq=seq, loss=float(doc.get("loss", float("nan"))), model=str(doc.get("model")),
                category=doc.get("category"), values=dict(doc.get("values") or {}), hlx=h, doc=doc)


def knob_text(values: dict, n: int = 6) -> str:
    keys = [k for k in KEY_KNOBS if k in values] + sorted(k for k in values if k not in KEY_KNOBS)
    out = []
    for k in keys[:n]:
        v = values[k]
        out.append(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}")
    more = len(keys) - n
    return " ".join(out) + (f" (+{more})" if more > 0 else "")


# --------------------------------------------------------------------------------------- the loop


class LivePreview:
    """Pure decision logic plus a loader callback (unit-tested with a fake engine).

    ``step()`` reads the directory once and loads the latest best when its seq is new and the
    throttle allows. Because the file is read at load time, a burst of improvements inside one
    throttle window loads only the newest one."""

    def __init__(self, directory: Path, loader: Callable[[Best], dict], throttle_s: float = DEFAULT_THROTTLE_S,
                 clock: Callable[[], float] = time.monotonic, out: Callable[[str], None] = print,
                 reader: Callable[[Path], Best | None] = read_best,
                 on_first_load: Callable[[Best, dict], None] | None = None):
        self.dir, self.loader, self.throttle_s = Path(directory), loader, float(throttle_s)
        self.clock, self.out, self.reader, self.on_first_load = clock, out, reader, on_first_load
        self.last_seq: int | None = None      # last seq attempted (loaded or refused)
        self.last_load_t: float | None = None
        self.loads: list[dict] = []
        self.refused: list[dict] = []
        self.skipped = 0                      # improvements superseded before they could be loaded
        self.waiting_told = False

    def due(self) -> bool:
        return self.last_load_t is None or self.clock() - self.last_load_t >= self.throttle_s

    def step(self) -> Best | None:
        best = self.reader(self.dir)
        if best is None:
            if not self.waiting_told and not self.loads:
                self.out(f"waiting for {self.dir / 'current_best.json'}")
                self.waiting_told = True
            return None
        if self.last_seq is not None and best.seq <= self.last_seq:
            return None
        if not self.due():
            return None
        return self._load(best)

    def _load(self, best: Best) -> Best | None:
        t = self.clock()
        if self.last_seq is not None:
            self.skipped += max(0, best.seq - self.last_seq - 1)   # superseded while throttled
        self.last_load_t, self.last_seq = t, best.seq
        try:
            info = self.loader(best) or {}
        except Exception as e:
            if _fatal(e):
                raise
            rec = {"seq": best.seq, "model": best.model, "error": f"{type(e).__name__}: {e}"}
            self.refused.append(rec)
            self.out(f"seq {best.seq}: NOT loaded ({rec['error'][:200]}); the pedal keeps the previous preset")
            return None
        rec = {"seq": best.seq, "loss": best.loss, "model": best.model, "category": best.category,
               "values": best.values, "t": t, **info}
        self.loads.append(rec)
        extra = f" load {info['load_s']:.2f}s" if isinstance(info.get("load_s"), float) else ""
        if info.get("readback_ok") is False:
            extra += " READBACK MISMATCH"
        self.out(f"seq {best.seq}  loss {best.loss:.4f}  {best.model}  {knob_text(best.values)}{extra}")
        if len(self.loads) == 1 and self.on_first_load is not None:
            try:
                self.on_first_load(best, rec)
            except Exception as e:  # noqa: BLE001 - bookkeeping must not stop the preview
                self.out(f"first-load hook failed: {type(e).__name__}: {e}")
        return best

    def run(self, poll_s: float = 0.5, once: bool = False, sleep: Callable[[float], None] = time.sleep,
            stop: Callable[[], bool] = lambda: False) -> None:
        """Watch until ``stop()`` (or an interrupt). ``once``: load the first best found, then hold."""
        while not stop():
            if not (once and self.loads):
                self.step()
            sleep(poll_s)


def _fatal(e: BaseException) -> bool:
    """Errors after which the session cannot go on (a dead bridge, a wedged pedal)."""
    try:
        from tonematch.engines.stomp_bridge import BridgeError, PowerCycleNeeded
    except Exception:  # noqa: BLE001
        return False
    return isinstance(e, (BridgeError, PowerCycleNeeded))


# --------------------------------------------------------------------------------------- hardware


def hx_edit_running() -> bool:
    return bool(subprocess.run(["pgrep", "-f", "HX Edit.app/Contents/MacOS/HX Edit"], capture_output=True,
                               check=False).stdout.strip())


def pedal_position() -> dict:
    """Which preset the pedal is on, read-only: one `info` call, no backup, no select, no write."""
    from tonematch.engines.stomp_bridge import StompBridge

    b = StompBridge(purpose="stomp_live_preview T166 preflight (read-only info)")
    try:
        i = b.call("info")
        return {k: i.get(k) for k in ("firmware", "setlist", "index", "label", "name")}
    finally:
        b.close()


def open_engine(run_dir: Path):
    """StompEngine for listening: no pilot, no sentinel, Input on Main L/R."""
    from tonematch.engines.stomp import StompEngine

    eng = StompEngine(session_dir=run_dir, purpose="stomp_live_preview T166", sentinel=False, slip_guard="off")
    # Every load writes this value into the Input block and the readback checks it: the guitar input.
    eng.cal = dataclasses.replace(eng.cal, input_usb56=INPUT_MAIN_LR)
    return eng


def stomp_loader(eng, catalog) -> Callable[[Best], dict]:
    from tonematch import preset as P

    def load(best: Best) -> dict:
        p = P.read_hlx(best.hlx, catalog)   # exactly the searched blocks, nothing added
        t0 = time.perf_counter()
        eng.load(p)
        rb = eng.last_readback or {}
        inp = next((s for s in eng.chain["slots"] if s["kind"] == "Input" and s["pos"] == 0), {})
        if inp.get("routing") != INPUT_MAIN_LR:
            raise RuntimeError(f"input routing reads {inp.get('routing')}, wanted Main L/R ({INPUT_MAIN_LR})")
        return {"load_s": round(time.perf_counter() - t0, 3), "readback_ok": rb.get("ok"),
                "whole_writes": len(eng.whole_writes), "policy_warnings": list(eng.policy_warnings),
                "blocks": [(s["pos"], s.get("model_id"), bool(s.get("enabled"))) for s in eng.chain["slots"]
                           if s["kind"] == "Block"]}
    return load


def ledger_first_load(run_dir: Path, directory: Path):
    def hook(best: Best, rec: dict) -> None:
        env = dict(os.environ, TONEMATCH_AGENT=os.environ.get("TONEMATCH_AGENT") or "stomp-engine")
        notes = (f"T166 live preview: first edit-buffer load ok; seq {best.seq} {best.model} loss {best.loss:.4f}; "
                 f"load {rec.get('load_s')} s, readback {rec.get('readback_ok')}, input Main L/R; "
                 f"watching {directory.relative_to(ROOT) if directory.is_relative_to(ROOT) else directory}; "
                 f"run {run_dir.relative_to(ROOT)}")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "smoke",
                        "--engine", "stomp", "--dataset", "live-preview", "--metric", "first_load_ok",
                        "--value", "1", "--renders", "0", "--notes", notes], check=True, env=env, cwd=ROOT)
    return hook


class _Stop(Exception):
    pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="progress directory with current_best.json / .hlx")
    ap.add_argument("--throttle-s", type=float, default=DEFAULT_THROTTLE_S)
    ap.add_argument("--poll-s", type=float, default=0.5)
    ap.add_argument("--once", action="store_true", help="load the current best, then hold until Ctrl-C")
    ap.add_argument("--restore-on-exit", action=argparse.BooleanOptionalAction, default=True,
                    help="restore the original preset on exit (default on)")
    ap.add_argument("--run-dir", help="output directory (default cache/stomp/hw/<stamp>-live-preview)")
    ap.add_argument("--ledger-first-load", action="store_true",
                    help="append the smoke/live-preview first_load_ok ledger row after the first load")
    a = ap.parse_args(argv)
    os.environ.setdefault("TONEMATCH_STOMPD", str(ROOT / "cache" / "stomp" / "bin" / "stompd"))
    os.environ.setdefault("TONEMATCH_STOMPD_COMPILE", str(ROOT / "cache" / "stomp" / "bin" / "stompd-compile"))
    directory = Path(a.dir).resolve()
    run_dir = Path(a.run_dir) if a.run_dir else (
        ROOT / "cache" / "stomp" / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-live-preview")
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    logf = open(run_dir / "preview.log", "a", buffering=1, encoding="utf-8")  # noqa: SIM115

    def out(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")

    def _signal(signum, _frame):
        raise _Stop(signal.Signals(signum).name)
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, _signal)

    res: dict = {"dir": str(directory), "run_dir": str(run_dir), "pid": os.getpid(), "errors": []}
    out(f"stomp_live_preview pid {os.getpid()}; dir {directory}; throttle {a.throttle_s} s; "
        f"restore on exit {a.restore_on_exit}; log {run_dir / 'preview.log'}")
    if hx_edit_running():
        out("HX Edit is running: quit it first (it holds the control interface). Nothing done.")
        return 2
    from tonematch import pedal_lock
    from tonematch.catalog import load_catalog

    catalog = load_catalog()
    eng = None
    try:
        with pedal_lock.session("stomp_live_preview T166 (human listening; no renders)"):
            try:
                rc = _watch(a, directory, run_dir, catalog, res, out)
                if rc:
                    return rc
                eng = res.pop("_eng", None)
            except (KeyboardInterrupt, _Stop) as e:
                out(f"stopping ({type(e).__name__} {e})")
                eng = res.pop("_eng", None)
            except Exception as e:  # noqa: BLE001 - recorded; the restore below still runs
                res["errors"].append(f"{type(e).__name__}: {e}")
                out(f"ERROR {type(e).__name__}: {e}")
                eng = res.pop("_eng", None)
            finally:
                for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                    signal.signal(s, signal.SIG_IGN)   # the restore must not be interrupted
                eng = eng or res.pop("_eng", None)
                if eng is not None:   # inside the lock: nobody else touches the pedal before the restore
                    _finish(eng, a.restore_on_exit, res, out)
    except (KeyboardInterrupt, _Stop) as e:
        out(f"stopped before the session opened ({e})")
    finally:
        res.pop("_eng", None)
        res["lock_released"] = not pedal_lock.held_here()
        (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
        logf.close()
    return 1 if res["errors"] or (eng is not None and a.restore_on_exit and not res.get("restore_ok")) else 0


def _watch(a, directory: Path, run_dir: Path, catalog, res: dict, out) -> int:
    pos = pedal_position()
    res["preflight"] = pos
    if (pos["setlist"], pos["index"], pos["name"]) != SCRATCH:
        out(f"the pedal is on {pos.get('label')} '{pos.get('name')}', not 04C '{SCRATCH[2]}': "
            f"nothing done (presets are not changed by this script)")
        return 3
    eng = res["_eng"] = open_engine(run_dir)
    res["original"] = eng.original
    out(f"session open on {eng.original['label']} '{eng.original['name']}' (fw {eng.info.get('firmware')}); "
        f"backup {eng.backup_path.name}; input Main L/R; edit buffer only")
    lp = LivePreview(directory, stomp_loader(eng, catalog), a.throttle_s, out=out,
                     on_first_load=ledger_first_load(run_dir, directory) if a.ledger_first_load else None)
    n_status = [-1]

    def status_tick() -> bool:
        n = len(lp.loads) + len(lp.refused)
        if n != n_status[0]:
            n_status[0] = n
            (run_dir / "status.json").write_text(json.dumps(
                {"pid": os.getpid(), "last": lp.loads[-1] if lp.loads else None, "loads": len(lp.loads),
                 "refused": lp.refused, "skipped": lp.skipped}, indent=1, default=str))
        return False
    try:
        lp.run(poll_s=a.poll_s, once=a.once, stop=status_tick)
    finally:
        res.update(loads=lp.loads, refused=lp.refused, skipped=lp.skipped)
    return 0


def _finish(eng, restore: bool, res: dict, out) -> None:
    from tonematch.engines.stomp import restore_verdict

    if restore:
        eng.close(verify_bytes=True)
        rr = eng.restore_report
        res.update(restore_report=rr, restore_ok=restore_verdict(rr),
                   bytes_equal_backup=rr.get("bytes_equal_backup"), decoded_equal_backup=rr.get("decoded_equal_backup"))
        out(f"restored {eng.original['label']} '{eng.original['name']}': restore_ok {res['restore_ok']} "
            f"(decoded equal {rr.get('decoded_equal_backup')}, bytes equal {rr.get('bytes_equal_backup')})")
    else:
        eng._closed = True   # the atexit hook must not restore either
        eng.bridge.close()
        out("left the last preview in the edit buffer (--no-restore-on-exit); nothing saved to flash")
    res["session_close"] = eng.bridge.release_report


if __name__ == "__main__":
    raise SystemExit(main())
