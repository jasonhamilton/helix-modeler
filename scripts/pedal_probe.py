"""Measured-pedal mode (T187): capture a real drive pedal's dfp-v1 fingerprint through the HX Stomp's FX loop.

PREPARED, NOT APPROVED. The human has not approved measured-pedal mode; the hardware steps
(`loopback`, `capture`) run only with `--human`, i.e. when the human runs them. Nothing here has
run on the pedal yet. Design, checklist and simulation: docs/notes/measured-pedal-mode.md.

Path (edit buffer only; nothing is saved on the pedal):
    host USB out 5/6 -> Input (USB 5/6) -> FX Loop 1 (Send/Return 0 dB, Mix 100 %) -> Send L
    -> pedal -> Return L -> Path A -> host USB in 1/2

Subcommands
    checklist                    print the human's steps
    template [--compile]         build the routing preset offline (and try `stompd compile`, offline)
    selftest                     the whole pipeline on a fake loop and a fake pedal (no hardware)
    loopback --human [--l2]      L1: cable Send L -> Return L (latency, gain, response, noise, linearity);
                                 --l2: then cable Send L -> guitar input (the input scale)
    capture --human --pedal NAME --setting TEXT [--knob drive=2:00 ...] [--repeats N] [--noise-take]
    identify FINGERPRINT.npz [--noise NOISE.npz]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch import pedal_probe as PP

BASE_DOC = ROOT / "cache" / "stomp" / "live" / "base04C.bin"
COMPILER = ROOT / "cache" / "stomp" / "bin" / "stompd-compile"
PRE_S, TAIL_S = 0.5, 0.5
OUT_PAIR = (5, 6)
PATH_A = (0, 1)              # host USB in 1/2 (0-based columns)
DRY_MAIN = (4, 5)            # host USB in 5/6: dry guitar input (L2)
DRY_RETURN = (6, 7)          # host USB in 7/8: dry returns (QA)

CHECKLIST = """\
MEASURED-PEDAL MODE: the human's checklist (T187; the mode is NOT approved yet; nothing is saved on the pedal)

Before you start
 1. Quit HX Edit. Select the scratch preset 04C "Smoke Known" on the HX Stomp. The tool only changes
    the EDIT BUFFER (it turns 04C's delay slot into an FX Loop 1 block, bypasses the amp, routes the
    Input to USB 5/6) and puts the buffer back at the end. It never saves; do not press Save yourself.
 2. Turn the HX Stomp's big VOLUME knob fully down and take off headphones: the probe's sweeps reach
    0 dBFS and also come out of the main outputs. USB recording is not affected by the knob.
 3. Global Settings > Ins/Outs: if Send/Return offers Instrument or Line, choose Instrument. Guitar In
    pad off. Note what you chose; it is part of the calibration.
 4. Use short instrument cables. Power the pedal from its usual supply (note battery or supply).

Step L1, the loopback (once per session, about 2 minutes)
 5. Cable from SEND L straight to RETURN L. Nothing else in the loop.
 6. Run:  .venv/bin/python scripts/pedal_probe.py loopback --human [--l2]
    It checks: latency agreement, loop gain near 0 dB, flat response, no distortion, no clipping.
Step L2, the input scale (optional, recommended; 2 more minutes)
 7. With --l2 the tool then asks you to move the cable: SEND L -> the Stomp's GUITAR INPUT (Main In L).
    It measures what the guitar input sees, so the pedal gets the probe at the level a Helix model gets it.

Each pedal setting (about 2 minutes each, plus about 2 more with --noise-take)
 8. Cable SEND L -> pedal INPUT; pedal OUTPUT -> RETURN L. Pedal switched ON (effect engaged).
 9. Write down every knob as a clock position (7:00 = fully left, 12:00 = noon, 5:00 = fully right),
    plus any switches, and the pedal's name and version.
10. Run:  .venv/bin/python scripts/pedal_probe.py capture --human --pedal "NAME" --setting "D 2:00 T 12:00 L 10:00" \\
             --knob drive=2:00 --knob tone=12:00 --knob level=10:00 --noise-take
    If it reports clipping, turn the pedal's Level down and repeat. If it reports a noisy capture, add --repeats 2.
11. Optional: one take with the pedal OFF (bypassed): it should look like the loopback.

After
12. The tool restores the edit buffer and the selected preset and says whether the restore matched.
    Unplug the loop cables. Nothing on the pedal was saved or changed.
"""


# ------------------------------------------------------------------------------------------- template

def chain_of(base: Path | None, chain_json: Path | None) -> dict:
    if chain_json is not None:
        return json.loads(Path(chain_json).read_text())
    from tonematch.engines.stomp import decode_document

    return decode_document(base or BASE_DOC, compiler=COMPILER if COMPILER.exists() else None)


def cmd_template(a) -> dict:
    from tonematch import reamp
    from tonematch.catalog import load_catalog

    cat = load_catalog()
    chain = chain_of(a.base, a.chain)
    hlx = PP.template_hlx(chain, cat, reamp.STOMP_INPUT_USB56, send_db=a.send_db)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(hlx, indent=1))
    res = {"hlx": str(out), "plan": PP.routing_plan(chain, cat, send_db=a.send_db)}
    if a.compile:
        res["compile"] = compile_check(out, a.base or BASE_DOC)
    res["route"] = "whole" if res.get("compile", {}).get("ok") else "in-place"
    return res


def compile_check(hlx: Path, base: Path, compiler: Path | None = None) -> dict:
    """Offline `stompd compile` of the template onto a backup (never opens USB). The v0 compiler
    refuses Send/Return blocks; the tool then loads the template in place."""
    from tonematch.engines.base import EngineError, UnsupportedModelChange
    from tonematch.engines.stomp import compile_document

    out = Path(hlx).with_suffix(".bin")
    try:
        rep = compile_document(Path(hlx), Path(base), out, compiler=compiler or (COMPILER if COMPILER.exists() else None))
        return {"ok": True, "document": str(out), "report": rep}
    except UnsupportedModelChange as e:
        return {"ok": False, "refused": str(e)}
    except EngineError as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------------------------------------- session

class ProbeSession:
    """One pedal session: the template on the edit buffer, takes through the loop.

    ``engine``: an open ``StompEngine(whole_preset="off")`` (it backs the edit buffer up at open and
    restores it on ``close()``). ``playrec``: ``tonematch.reamp.playrec`` or a stand-in with the same
    signature (tests)."""

    def __init__(self, engine, catalog: dict, playrec=None, send_db: float = 0.0):
        from tonematch import reamp

        self.engine = engine
        self.catalog = catalog
        self.playrec = playrec or reamp.playrec
        self.send_db = float(send_db)
        self.plan = None

    def setup(self, send_db: float | None = None) -> dict:
        if send_db is not None:
            self.send_db = float(send_db)
        self.plan = PP.routing_plan(self.engine.chain, self.catalog, send_db=self.send_db)
        self.engine.load(PP.template_preset(self.engine.chain, self.catalog, send_db=self.send_db))
        if hasattr(self.playrec, "set_send_db"):        # the fake loop of the tests models the Send block
            self.playrec.set_send_db(self.send_db)
        return self.plan

    def take(self, x: np.ndarray) -> tuple[np.ndarray, int]:
        rec, pre = self.playrec(np.asarray(x, np.float32), getattr(self.engine, "device", None), pre_s=PRE_S,
                                tail_s=TAIL_S, out_pair=OUT_PAIR, in_pair=None)
        return np.asarray(rec, np.float64), int(pre)

    @staticmethod
    def mono(rec: np.ndarray, cols=PATH_A) -> np.ndarray:
        rec = np.asarray(rec, float)
        if rec.ndim == 1:
            return rec
        return 0.5 * (rec[:, cols[0]] + rec[:, cols[1]]) if rec.shape[1] > max(cols) else rec[:, 0]

    def loopback(self, firmware: str = "") -> tuple[PP.LoopCal, dict, dict]:
        """L1, with the template's Send at its current value (``setup(PP.LOOP_SEND_DB)`` in the tool)."""
        x, meta = PP.calibration_signal()
        rec, pre = self.take(x)
        cal, arr, sc = PP.loop_calibration(self.mono(rec), dict(meta, pre=pre), firmware=firmware, send_db=self.send_db,
                                           notes={"route": "in-place", "plan": self.plan})
        if rec.ndim == 2 and rec.shape[1] > max(DRY_RETURN):
            dry = PP.trim(self.mono(rec, DRY_RETURN), pre + meta["probe_start"], cal.latency_samples, meta["probe_n"])
            y = PP.trim(self.mono(rec), pre + meta["probe_start"], cal.latency_samples, meta["probe_n"])
            cal.notes["dry_return_vs_path_a_db"] = float(20 * np.log10((np.std(dry) + 1e-30) / (np.std(y) + 1e-30)))
        return cal, arr, sc

    def input_scale(self, cal: PP.LoopCal) -> float:
        """L2 (cable Send L -> guitar input): the guitar input's dBFS for the probe's dBFS at Send 0 dB."""
        x, meta = PP.calibration_signal()
        rec, pre = self.take(x)
        if rec.ndim != 2 or rec.shape[1] <= max(DRY_MAIN):
            raise RuntimeError("the capture has no USB in 5/6 (dry guitar input) channels")
        g = PP.input_scale_db(self.mono(rec, DRY_MAIN), dict(meta, pre=pre), cal.latency_samples) - self.send_db
        cal.input_scale_db = g
        return g

    def capture(self, cal: PP.LoopCal, repeats: int = 1) -> tuple[np.ndarray, list[dict]]:
        sig, lay = PP.probe()
        takes, q = [], []
        for _ in range(max(1, repeats)):
            rec, pre = self.take(sig)
            y = PP.trim(self.mono(rec), pre, cal.latency_samples, lay["n"])
            q.append(PP.capture_quality(y))
            takes.append(y)
        y = PP.average(takes) if len(takes) > 1 else takes[0]
        return y, q

    def noise(self, cal: PP.LoopCal) -> np.ndarray:
        lay = PP.layout()
        rec, pre = self.take(np.zeros(lay["n"], np.float32))
        return PP.trim(self.mono(rec), pre, cal.latency_samples, lay["n"])


def _require_human(a) -> None:
    if not a.human:
        raise SystemExit("pedal_probe: the hardware steps run only when the human runs them (add --human). "
                         "Measured-pedal mode is not approved yet (T187); `selftest` runs the same maths on a "
                         "fake device.")


def _open_engine(run_dir: Path):
    from tonematch.engines.stomp import StompEngine

    return StompEngine(session_dir=run_dir, whole_preset="off", purpose="pedal_probe T187", sentinel=False,
                       slip_guard="off")


def _run_dir(kind: str) -> Path:
    d = PP.LOOP_ROOT / "runs" / f"{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}-{kind}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def latest_loopback() -> Path:
    fs = sorted((PP.LOOP_ROOT / "loopback").glob("*.json"))
    if not fs:
        raise SystemExit("no loopback calibration yet: run `pedal_probe.py loopback --human` first")
    return fs[-1]


def cmd_loopback(a) -> dict:
    _require_human(a)
    from tonematch import pedal_lock
    from tonematch.catalog import load_catalog

    run = _run_dir("loopback")
    cat = load_catalog()
    with pedal_lock.session("pedal_probe loopback (T187, human)"):
        eng = _open_engine(run)
        try:
            ps = ProbeSession(eng, cat)
            plan = ps.setup(PP.LOOP_SEND_DB)
            input("Cable SEND L -> RETURN L (nothing else). Volume knob down. Press Enter to play the 2-minute loop test...")
            cal, arr, sc = ps.loopback(firmware=str(eng.info.get("firmware", "")))
            if a.l2:
                input("Now move the cable: SEND L -> the Stomp's GUITAR INPUT. Press Enter...")
                ps.input_scale(cal)
        finally:
            eng.close(verify_bytes=True)
    stamp = run.name.split("-")[0]
    p = cal.save(PP.LOOP_ROOT / "loopback" / f"{stamp}.json")
    (PP.LOOP_ROOT / "loopback" / f"{stamp}.npz").write_bytes(PP.P.pack(arr, sc))
    return {"loopback": str(p), "ok": cal.ok, "problems": cal.problems(), "latency": cal.latency_samples,
            "gain_1k_db": round(cal.gain_1k_db, 3), "flatness_db": round(cal.flatness_db, 3),
            "input_scale_db": cal.input_scale_db, "send_db_for_native_scale": PP.send_db_for(cal), "plan": plan,
            "restore": eng.restore_report}


def _knobs(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = v.strip()
    return out


def cmd_capture(a) -> dict:
    _require_human(a)
    from tonematch import pedal_lock
    from tonematch.catalog import load_catalog

    cal = PP.LoopCal.load(Path(a.loopback) if a.loopback else latest_loopback())
    if not cal.ok and not a.force:
        raise SystemExit(f"the loopback calibration has problems {cal.problems()}; redo it or pass --force")
    run = _run_dir("capture")
    cat = load_catalog()
    send = PP.send_db_for(cal) if a.send_db is None else a.send_db
    noise = None
    with pedal_lock.session("pedal_probe capture (T187, human)"):
        eng = _open_engine(run)
        try:
            ps = ProbeSession(eng, cat)
            ps.setup(send)
            input(f"Pedal '{a.pedal}' in the loop (SEND L -> pedal in, pedal out -> RETURN L), switched ON, "
                  f"knobs at '{a.setting}'. Press Enter to play the probe ({a.repeats} x 2 minutes)...")
            y, q = ps.capture(cal, a.repeats)
            if a.noise_take:
                noise = ps.noise(cal)
        finally:
            eng.close(verify_bytes=True)
    yc, corr = PP.correct_capture(y, cal)
    arr, sc = PP.fingerprint(yc)
    meta = {"knobs": _knobs(a.knob), "loopback": cal.date, "corrections": dict(corr, send_db=send, repeats=a.repeats),
            "quality": q, "restore_ok": eng.restore_report.get("restore_ok")}
    ent = PP.write_fingerprint(a.pedal, a.setting, arr, sc, meta)
    res = {"fingerprint": ent["file"], "quality": q, "restore": eng.restore_report}
    nfa = None
    if noise is not None:
        nc, _ = PP.correct_capture(noise, cal)
        nfa, nsc = PP.fingerprint(nc)
        nf = Path(ent["file"]).with_name(Path(ent["file"]).stem + "__noise.npz")
        (ROOT / nf if not nf.is_absolute() else nf).write_bytes(PP.P.pack(nfa, nsc))
        res["noise_take"] = str(nf)
    res["identify"] = identify(arr, sc, nfa)
    return res


# ------------------------------------------------------------------------------------------- identify

def identify(arr: dict, sc: dict, noise_arr: dict | None = None, top: int = 5) -> dict:
    """The capture against the 54-model library (dfp-v1 + tone ends): the T187 measurement's choice
    is plain matching, plus the noise mask when a noise take exists (docs/notes/measured-pedal-mode.md)."""
    lib = PP.Library()
    if noise_arr is not None:
        arr = PP.noise_mask(arr, noise_arr)
    rk = lib.rank(arr, sc)
    fam = PP.families()
    return {"ranking": [{"model": m, "distance": round(d, 4), "family": fam.get(m), "knobs": lib.knobs_named(m, p)}
                        for m, d, p in rk[:top]],
            "noise_mask": noise_arr is not None,
            "note": "measurement-stage tool: the mode is not approved; distances above the in-library 95th percentile "
                    "of the T187 simulation suggest a pedal unlike any library model"}


def cmd_identify(a) -> dict:
    arr, sc = PP.read_fingerprint(Path(a.fingerprint))
    nz = PP.read_fingerprint(Path(a.noise))[0] if a.noise else None
    return identify(arr, sc, nz, a.top)


# ------------------------------------------------------------------------------------------- selftest

class FakeLoop:
    """A stand-in for the Stomp's audio: ``playrec`` with a latency, a loop gain and tilt, noise, an
    optional pedal between Send and Return, and a guitar-input scale for L2. 8 input channels."""

    def __init__(self, latency: int = 1207, gain_db: float = -0.4, tilt: float = 0.0, noise_dbfs: float = -100.0,
                 pedal=None, guitar_in_db: float = 0.0, seed: int = 0, send_db: float = 0.0):
        self.latency, self.gain_db, self.tilt, self.noise_dbfs = latency, gain_db, tilt, noise_dbfs
        self.pedal, self.guitar_in_db, self.seed, self.send_db = pedal, guitar_in_db, seed, send_db
        self.calls = 0

    def set_send_db(self, v: float) -> None:
        self.send_db = float(v)

    def __call__(self, mono, device=None, pre_s=PRE_S, tail_s=TAIL_S, out_pair=OUT_PAIR, in_pair=None):
        from tonematch import reamp

        self.calls += 1
        sr = PP.SR
        pre, tail = int(pre_s * sr), int(tail_s * sr)
        x = np.concatenate([np.zeros(pre), np.asarray(mono, float), np.zeros(tail)])
        send = PP.gain(x, self.send_db)
        y = self.pedal(send) if self.pedal is not None else send
        y = PP.tilt(y, self.tilt) if self.tilt else y
        y = PP.add_noise(PP.gain(y, self.gain_db), self.noise_dbfs, seed=self.seed + self.calls)
        y = np.clip(PP.delay(y, self.latency), -1.0, 1.0)
        g = PP.delay(PP.gain(send, self.guitar_in_db), self.latency)
        rec = np.zeros((x.size, 8))
        rec[:, 0] = rec[:, 1] = y
        rec[:, 4] = rec[:, 5] = g
        rec[:, 6] = rec[:, 7] = y
        assert tuple(out_pair) == reamp.PLAY_TO
        return rec.astype(np.float32), pre


def fake_pedal(drive_db: float = 20.0, hp_hz: float = 700.0, lp_hz: float = 4000.0):
    """A Tube-Screamer-like toy: bass cut, gain, symmetric soft clip, treble cut."""
    from scipy import signal

    bh, ah = signal.butter(1, hp_hz, "high", fs=PP.SR)
    bl, al = signal.butter(1, lp_hz, "low", fs=PP.SR)

    def f(x):
        u = signal.lfilter(bh, ah, x) * 10 ** (drive_db / 20)
        return 0.5 * signal.lfilter(bl, al, np.tanh(u))
    return f


def cmd_selftest(a) -> dict:
    """The pipeline on the fake loop, no hardware: L1, L2, a capture, the fingerprint, identification."""
    cal_loop = FakeLoop(latency=a.latency, gain_db=-0.4, tilt=a.tilt, noise_dbfs=-100.0, guitar_in_db=-1.5,
                        send_db=PP.LOOP_SEND_DB)
    x, meta = PP.calibration_signal()
    rec, pre = cal_loop(x)
    cal, _, _ = PP.loop_calibration(rec[:, 0], dict(meta, pre=pre), send_db=PP.LOOP_SEND_DB)
    rec2, pre2 = cal_loop(x)
    cal.input_scale_db = PP.input_scale_db(rec2[:, 4], dict(meta, pre=pre2), cal.latency_samples) - PP.LOOP_SEND_DB
    ped = FakeLoop(latency=a.latency, gain_db=-0.4, tilt=a.tilt, noise_dbfs=-100.0, pedal=fake_pedal())
    sig, lay = PP.probe()
    r3, p3 = ped(sig)
    y = PP.trim(r3[:, 0], p3, cal.latency_samples, lay["n"])
    yc, corr = PP.correct_capture(y, cal)
    arr, sc = PP.fingerprint(yc)
    ent = PP.write_fingerprint("selftest toy TS", "drive 20 dB", arr, sc,
                               {"knobs": {"drive": "toy"}, "corrections": corr, "loopback": "fake"},
                               root=PP.LOOP_ROOT / "selftest")
    return {"latency": cal.latency_samples, "latency_true": a.latency, "gain_1k_db": round(cal.gain_1k_db, 3),
            "flatness_db": round(cal.flatness_db, 3), "input_scale_db": round(cal.input_scale_db, 3),
            "problems": cal.problems(), "fingerprint": ent["file"], "knee_dbfs": sc.get("knee_1k_dbfs"),
            "identify": identify(arr, sc, top=3) if a.identify else None}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("checklist")
    t = sub.add_parser("template")
    t.add_argument("--base", type=Path, default=None, help="an edit-buffer backup to decode (default 04C's)")
    t.add_argument("--chain", type=Path, default=None, help="a chain JSON instead of a backup")
    t.add_argument("--send-db", type=float, default=0.0)
    t.add_argument("--out", default=str(PP.LOOP_ROOT / "template.hlx"))
    t.add_argument("--compile", action="store_true", help="try the offline compiler (never opens USB)")
    s = sub.add_parser("selftest")
    s.add_argument("--latency", type=int, default=1207)
    s.add_argument("--tilt", type=float, default=0.0)
    s.add_argument("--identify", action="store_true")
    lb = sub.add_parser("loopback")
    lb.add_argument("--human", action="store_true")
    lb.add_argument("--l2", action="store_true")
    c = sub.add_parser("capture")
    c.add_argument("--human", action="store_true")
    c.add_argument("--pedal", required=True)
    c.add_argument("--setting", required=True)
    c.add_argument("--knob", action="append", default=[])
    c.add_argument("--repeats", type=int, default=1)
    c.add_argument("--noise-take", action="store_true")
    c.add_argument("--loopback", default=None)
    c.add_argument("--send-db", type=float, default=None)
    c.add_argument("--force", action="store_true")
    i = sub.add_parser("identify")
    i.add_argument("fingerprint")
    i.add_argument("--noise", default=None)
    i.add_argument("--top", type=int, default=5)
    a = ap.parse_args(argv)
    if a.cmd == "checklist":
        print(CHECKLIST)
        return
    out = {"template": cmd_template, "selftest": cmd_selftest, "loopback": cmd_loopback, "capture": cmd_capture,
           "identify": cmd_identify}[a.cmd](a)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
