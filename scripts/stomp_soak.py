#!/usr/bin/env python3
"""T168: reproduction and soak test for the HX Stomp control-channel wedge.

Loads alternating NEW-MODEL single drive blocks into the 04C edit buffer (never flash), idles a
random 1-60 s between loads, optionally with an audio stream running through the pedal the whole
time, and stops at the FIRST sign of trouble: a precursor reported by the helper (a USB write
slower than ``PRECURSOR_WRITE_MS``), any bridge error, or a call far slower than normal. It never
retries into a pedal that stopped answering.

Sequences (``--sequence``):
- ``load`` (default): the product path. ``StompEngine.load`` of a one-drive preset per step, so each
  first use of a model runs the T110 template capture (model command, backups, op-21 put-back) and
  then the whole-preset write: exactly what the T166 live preview did when it wedged the pedal.
- ``capture``: the raw incident sequence through the bridge, no engine: chain, backup, model,
  chain, backup, restore (op 21) of the first backup, chain; compared by chain signature.

Audio (``--audio``): one continuous full-duplex stream on the Stomp's USB audio (48 kHz) for the
whole run, quiet noise (-60 dBFS by default, --noise-dbfs) to USB 5/6, the inputs discarded; status flags are counted.

Hardware rules: HX Edit closed; the shared pedal lock is held by the bridge; the pedal must be on 04C
'Smoke Known'; edit buffer only; the original preset is restored and verified at the end.

Output: ``cache/stomp/hw/<stamp>-soak/`` with ``soak.jsonl`` (one row per step), ``summary.json``
and the transaction trace (``trace.log``: every call and reply with the helper's link stats, and
the helper's per-frame TRACE lines) when ``--trace`` (default on).

Usage:
  scripts/stomp_soak.py --loads 200 --idle-min 1 --idle-max 60
  scripts/stomp_soak.py --loads 60 --idle-min 1 --idle-max 20 --audio
  scripts/stomp_soak.py --sequence capture --loads 20 --helper-env STOMPD_PUMP=0 --helper-env STOMPD_IDLE_DRAIN=0
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SCRATCH = (0, 11, "Smoke Known")      # 04C
# A step slower than this is a precursor too (normal: whole load 0.1-2.1 s, capture step < 3 s).
SLOW_STEP_S = {"load": 8.0, "capture": 8.0}
NOISE_DBFS = -60.0   # quiet: it runs through high-gain drives to the pedal's main outputs


def drive_models(catalog: dict, refused: set[str]) -> list[str]:
    """Mono drive models the Stomp runs (catalog ``stomp``), minus any the device refused."""
    out = []
    for mid, m in sorted(catalog["models"].items()):
        if m.get("category") != "drive" or not m.get("stomp") or not m.get("mono"):
            continue
        if m.get("stomp_min_firmware") or mid in refused:
            continue
        out.append(mid)
    return out


def model_sequence(models: list[str], n: int, rng: random.Random) -> list[str]:
    """n models, each different from the one before (every step is a model change)."""
    seq: list[str] = []
    pool: list[str] = []
    while len(seq) < n:
        if not pool:
            pool = models[:]
            rng.shuffle(pool)
        m = pool.pop()
        if seq and m == seq[-1]:
            pool.insert(0, m)
            if len(set(pool)) == 1 and pool[0] == seq[-1]:
                pool = []
            continue
        seq.append(m)
    return seq


def drive_hlx(base: dict, model_id: str, catalog: dict, rng: random.Random) -> dict:
    """``base`` (a one-drive .hlx) with its block0 replaced by ``model_id`` at random settings."""
    import copy

    h = copy.deepcopy(base)
    dsp = h["data"]["tone"]["dsp0"]
    blk = {"@model": model_id, "@enabled": True, "@position": 0, "@stereo": False, "@type": 0, "@path": 0}
    for name, p in catalog["models"][model_id].get("params", {}).items():
        if p.get("kind") == "continuous" and isinstance(p.get("min"), (int, float)):
            blk[name] = round(rng.uniform(float(p["min"]), float(p["max"])), 4) if name != "Level" else p["default"]
        elif "default" in p:
            blk[name] = p["default"]
    dsp["block0"] = blk
    for k in [k for k in dsp if k.startswith("block") and k != "block0"]:
        del dsp[k]
    return h


class Audio:
    """One continuous duplex stream through the pedal (quiet noise to USB 5/6), in the background."""

    def __init__(self, dbfs: float = NOISE_DBFS) -> None:
        self.dbfs = dbfs
        self.status_flags = 0
        self.callbacks = 0
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import numpy as np
        import sounddevice as sd

        from tonematch import reamp

        dev = reamp.find_device()
        info = sd.query_devices(dev)
        n_out, n_in = int(info["max_output_channels"]), int(info["max_input_channels"])
        amp = 10 ** (self.dbfs / 20)
        rng = np.random.default_rng(0)
        out_cols = [c - 1 for c in reamp.PLAY_TO]

        def cb(indata, outdata, frames, _time, status):
            if status:
                self.status_flags += 1
            self.callbacks += 1
            outdata.fill(0)
            noise = (rng.standard_normal(frames) * amp).astype("float32")
            for c in out_cols:
                outdata[:, c] = noise

        ready = threading.Event()

        def run() -> None:
            try:
                with reamp._streaming(), sd.Stream(device=(dev, dev), samplerate=reamp.SR, dtype="float32",
                                                   channels=(n_in, n_out), latency=reamp.PA_LATENCY, callback=cb):
                    ready.set()
                    while not self._stop.wait(0.2):
                        pass
            except Exception as e:  # noqa: BLE001 - recorded in the summary
                self.error = f"{type(e).__name__}: {e}"
                ready.set()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        ready.wait(10)
        time.sleep(1.0)
        if self.error:
            raise RuntimeError(f"audio stream failed: {self.error}")
        if self.callbacks == 0:
            raise RuntimeError("audio stream opened but its callback never ran (Microphone permission?)")

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(10)
        return {"callbacks": self.callbacks, "status_flags": self.status_flags, "error": self.error}


def foreign_audio() -> bool:
    """macOS: is CoreAudio running the HX Stomp's audio engine (someone is playing or recording
    through it)? Used to stop a soak rather than change presets under a listening session."""
    try:
        out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "AppleUSBAudioEngine:LINE 6:HX Stomp" in out


def chain_sig(ch: dict) -> list:
    return [(s["pos"], s["kind"], s.get("model_id"), s.get("enabled")) for s in ch["slots"]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sequence", choices=("load", "capture"), default="load")
    ap.add_argument("--loads", type=int, default=200)
    ap.add_argument("--idle-min", type=float, default=1.0)
    ap.add_argument("--idle-max", type=float, default=60.0)
    ap.add_argument("--audio", action="store_true", help="run a duplex audio stream through the pedal throughout")
    ap.add_argument("--seed", type=int, default=168)
    ap.add_argument("--helper", help="stompd binary (default: TONEMATCH_STOMPD or cache/stomp/bin/stompd)")
    ap.add_argument("--helper-env", action="append", default=[], metavar="K=V",
                    help="environment for the helper, e.g. STOMPD_PUMP=0 (diagnosis)")
    ap.add_argument("--trace", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--run-dir")
    ap.add_argument("--base-hlx", default=str(ROOT / "cache" / "matches" / "Mother-0-68s-drive" / "current_best.hlx"))
    ap.add_argument("--noise-dbfs", type=float, default=NOISE_DBFS, help="level of the --audio noise")
    ap.add_argument("--fresh-templates", action="store_true",
                    help="capture device templates into the run dir (every model's first use runs the T110 "
                         "capture; the shared cache/stomp/templates is not touched)")
    ap.add_argument("--ledger", action="store_true", help="append the smoke/stomp-soak ledger rows at the end")
    ap.add_argument("--label", default="", help="free text for the ledger notes")
    a = ap.parse_args(argv)

    os.environ.setdefault("TONEMATCH_STOMPD", str(ROOT / "cache" / "stomp" / "bin" / "stompd"))
    os.environ.setdefault("TONEMATCH_STOMPD_COMPILE", str(ROOT / "cache" / "stomp" / "bin" / "stompd-compile"))
    if a.helper:
        os.environ["TONEMATCH_STOMPD"] = str(Path(a.helper).resolve())
    for kv in a.helper_env:
        k, _, v = kv.partition("=")
        os.environ[k] = v
    run_dir = Path(a.run_dir) if a.run_dir else (
        ROOT / "cache" / "stomp" / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-soak")
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    if a.trace:
        os.environ["TONEMATCH_STOMP_TRACE"] = str(run_dir / "trace.log")
    rows_f = open(run_dir / "soak.jsonl", "a", buffering=1, encoding="utf-8")  # noqa: SIM115

    def out(msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    if subprocess.run(["pgrep", "-f", "HX Edit.app/Contents/MacOS/HX Edit"], capture_output=True,
                      check=False).stdout.strip():
        out("HX Edit is running: quit it first. Nothing done.")
        return 2

    from tonematch import pedal_lock
    from tonematch import preset as P
    from tonematch.catalog import load_catalog
    from tonematch.engines.stomp_bridge import BridgeError, LinkDegraded, StompBridge

    catalog = load_catalog()
    rng = random.Random(a.seed)
    summary: dict = {"args": vars(a), "run_dir": str(run_dir), "helper": os.environ["TONEMATCH_STOMPD"],
                     "helper_env": a.helper_env, "steps": 0, "ok_steps": 0, "stopped": None, "errors": []}
    audio = Audio(a.noise_dbfs) if a.audio else None
    eng = None
    bridge = None
    t_run = time.monotonic()
    try:
        with pedal_lock.session(f"stomp_soak T168 ({a.sequence}, {a.loads} loads{', audio' if a.audio else ''})"):
            if a.sequence == "load":
                from tonematch.engines import stomp as stomp_mod
                from tonematch.engines.stomp import TEMPLATE_ROOT, StompEngine

                if a.fresh_templates:
                    stomp_mod.TEMPLATE_ROOT = run_dir / "templates"

                eng = StompEngine(session_dir=run_dir, purpose="stomp_soak T168", sentinel=False, slip_guard="off")
                bridge = eng.bridge
                refused = set(json.loads((TEMPLATE_ROOT / str(eng.info.get("firmware")) / "index.json").read_text())
                              .get("refused", {}).keys()) if (TEMPLATE_ROOT / str(eng.info.get("firmware")) / "index.json").exists() else set()
                refused = {k.split("|")[0] for k in refused}
                orig = eng.original
            else:
                bridge = StompBridge(purpose="stomp_soak T168 capture")
                refused = set()
                orig = bridge.call("info", timeout_s=3)
            summary["original"] = {k: orig.get(k) for k in ("setlist", "index", "label", "name")}
            summary["helper_ready"] = bridge.ready
            if (orig.get("setlist"), orig.get("index"), orig.get("name")) != SCRATCH:
                out(f"the pedal is on {orig.get('label')} '{orig.get('name')}', not 04C '{SCRATCH[2]}': nothing done")
                summary["stopped"] = "not on 04C"
                return 3
            models = drive_models(catalog, refused)
            seq = model_sequence(models, a.loads, rng)
            base = json.loads(Path(a.base_hlx).read_text())
            if foreign_audio():   # someone else's stream, before ours exists
                summary["stopped"] = "foreign audio before start"
                out("the Stomp's audio is in use by someone else: nothing done")
                return 4
            if audio is not None:
                audio.start()
                out(f"audio stream running ({a.noise_dbfs:.0f} dBFS noise to USB 5/6)")
            cap0 = None
            if a.sequence == "capture":
                ch = bridge.call("chain", timeout_s=15)
                cap0 = chain_sig(ch)
                blocks = sorted(s["pos"] for s in ch["slots"] if s["kind"] == "Block" and s["pos"] < 10)
                cap_pos = blocks[0]
            out(f"soak: {a.sequence}, {a.loads} new-model loads over {len(models)} drive models; idle "
                f"{a.idle_min:g}-{a.idle_max:g} s; run {run_dir}")
            for i, model in enumerate(seq):
                idle = rng.uniform(a.idle_min, a.idle_max)
                row: dict = {"i": i, "model": model, "idle_before_s": round(idle, 2)}
                time.sleep(idle)
                if audio is None and foreign_audio():
                    summary["stopped"] = "foreign audio (someone is using the Stomp's audio): stopped, nothing loaded"
                    out(f"STOP: {summary['stopped']}")
                    break
                t0 = time.monotonic()
                try:
                    if a.sequence == "load":
                        n_cap = len(eng.templates_captured)
                        p = P.from_dict(drive_hlx(base, model, catalog, rng), catalog)
                        eng.load(p)
                        rb = eng.last_readback or {}
                        row.update(readback_ok=rb.get("ok"), template_captured=len(eng.templates_captured) > n_cap,
                                   whole_writes=len(eng.whole_writes), putbacks_skipped=eng.putbacks_skipped)
                        if rb.get("ok") is False:
                            raise RuntimeError(f"readback mismatch: {rb}")
                    else:
                        bridge.call("chain", timeout_s=15)
                        keep = run_dir / "capture_keep.bin"
                        bridge.call("backup", path=str(keep), timeout_s=15)
                        r = bridge.call("model", pos=cap_pos, model=model, timeout_s=15)
                        row["settle"] = r.get("settle")
                        bridge.call("chain", timeout_s=15)
                        bridge.call("backup", path=str(run_dir / "capture_tpl.bin"), timeout_s=15)
                        bridge.call("restore", path=str(keep), timeout_s=30)
                        back = chain_sig(bridge.call("chain", timeout_s=15))
                        row["restored"] = back == cap0
                        if back != cap0:
                            raise RuntimeError(f"capture: the buffer did not return to its state: {back}")
                except LinkDegraded as e:
                    row.update(ok=False, error=f"LinkDegraded: {e}")
                    summary["stopped"] = "precursor"
                except (BridgeError, Exception) as e:  # noqa: BLE001 - stop at the first error of any kind
                    row.update(ok=False, error=f"{type(e).__name__}: {e}")
                    summary["stopped"] = "error"
                row["s"] = round(time.monotonic() - t0, 3)
                row.setdefault("ok", True)
                row["link"] = dict(bridge.link_summary)
                row["degraded"] = bridge.degraded
                if audio is not None:
                    row["audio"] = {"callbacks": audio.callbacks, "status_flags": audio.status_flags,
                                    "error": audio.error}
                rows_f.write(json.dumps(row, default=str) + "\n")
                summary["steps"] += 1
                if row["ok"]:
                    summary["ok_steps"] += 1
                if row["ok"] and bridge.degraded:
                    summary["stopped"] = "precursor"
                if row["ok"] and row["s"] > SLOW_STEP_S[a.sequence]:
                    summary["stopped"] = f"slow step ({row['s']} s)"
                out(f"{i:3d} {model:28s} idle {idle:5.1f}s  step {row['s']:.2f}s  ok {row['ok']}"
                    f"  wmax {bridge.link_summary['write_ms_max']:.1f}ms"
                    + (f"  {row.get('error', '')[:160]}" if not row["ok"] else ""))
                if summary["stopped"]:
                    out(f"STOP: {summary['stopped']} at step {i}")
                    if not row["ok"]:
                        summary["errors"].append(row.get("error"))
                    break
    except Exception as e:  # noqa: BLE001 - recorded
        summary["errors"].append(f"{type(e).__name__}: {e}")
        summary["stopped"] = summary["stopped"] or "error"
        out(f"ERROR {type(e).__name__}: {e}")
    finally:
        if audio is not None:
            summary["audio"] = audio.stop()
        if eng is not None:
            try:
                from tonematch.engines.stomp import restore_verdict

                eng.close(verify_bytes=True)
                rr = eng.restore_report
                summary.update(restore_ok=restore_verdict(rr), restore_report=rr)
            except Exception as e:  # noqa: BLE001
                summary["errors"].append(f"close: {type(e).__name__}: {e}")
                summary["restore_ok"] = False
        elif bridge is not None:
            try:
                if not bridge._dead:
                    bridge.call("select", setlist=SCRATCH[0], index=SCRATCH[1], timeout_s=20)
                    info = bridge.call("info", timeout_s=3)
                    summary["restore_ok"] = info.get("name") == SCRATCH[2]
            except Exception as e:  # noqa: BLE001
                summary["errors"].append(f"restore: {type(e).__name__}: {e}")
                summary["restore_ok"] = False
            bridge.close()
        if bridge is not None:
            summary["link"] = bridge.link_summary
            summary["degraded"] = bridge.degraded
            summary["session_close"] = bridge.release_report
        summary["wall_s"] = round(time.monotonic() - t_run, 1)
        summary["lock_released"] = not pedal_lock.held_here()
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
        rows_f.close()
    clean = not summary["stopped"] and summary["ok_steps"] == a.loads and summary.get("restore_ok")
    out(f"done: {summary['ok_steps']}/{a.loads} ok; stopped {summary['stopped']}; restore_ok {summary.get('restore_ok')}; "
        f"write max {summary.get('link', {}).get('write_ms_max')} ms; summary {run_dir / 'summary.json'}")
    if a.ledger:
        _ledger(a, summary, run_dir, bool(clean))
    return 0 if clean else 1


def _ledger(a, summary: dict, run_dir: Path, clean: bool) -> None:
    env = dict(os.environ, TONEMATCH_AGENT=os.environ.get("TONEMATCH_AGENT") or "stomp-engine")
    rel = run_dir.relative_to(ROOT) if run_dir.is_relative_to(ROOT) else run_dir
    link = summary.get("link") or {}
    notes = (f"T168 soak {a.sequence}{' +audio' if a.audio else ''}{' ' + a.label if a.label else ''}: "
             f"{summary['ok_steps']}/{a.loads} new-model loads, idle {a.idle_min:g}-{a.idle_max:g} s, "
             f"stopped {summary['stopped']}, restore_ok {summary.get('restore_ok')}, write max "
             f"{link.get('write_ms_max')} ms, slow writes {link.get('slow_writes')}, helper env {a.helper_env or 'default'}"
             + (f", audio {summary.get('audio')}" if a.audio else "") + f"; run {rel}")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "smoke", "--engine", "stomp",
                    "--dataset", "stomp-soak", "--metric", "no_wedge_loads", "--value", str(summary["ok_steps"] if clean else 0),
                    "--renders", "0", "--wall-clock-s", str(summary.get("wall_s", 0)), "--notes", notes],
                   check=False, env=env, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
