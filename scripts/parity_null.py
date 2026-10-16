#!/usr/bin/env python3
"""parity-null: the same .hlx and the same DI through the HX Stomp and Helix Native, aligned,
level-matched and nulled. Owner: benchmark. Skill: .claude/skills/parity-null.

  gen   offline: write the two extra presets (C clean, H high gain) next to the four T018s variants
        in cache/stomp/inject/, validate all six for firmware 3.50, and cut the DI
        (cache/parity/di_10s_tail.wav: 10 s of Guitar-TECHS + 2 s of silence for delay tails)
  check offline: parse and validate the presets, print the plan; touches no engine. Warns when a file's
        snapshot controller values differ from its blocks; --base <04C backup> also compiles each preset
        and checks every parameter of the result (stompd decode, T051)
  run   hardware: for each preset write it whole into the Stomp's edit buffer and render, restore the
        pedal (04C) and byte-diff it, then load the SAME file into Helix Native and render; analyse

Per preset the outputs are <name>_stereo.wav (LEFT = HX Stomp, RIGHT = Helix Native, both aligned and
level-matched to Native), <name>_residual.wav (Stomp minus Native, mono), <name>.json, plus
summary.json, and ledger rows (kind=parity, engine=both, dataset=parity-v0).

Level match: a = <native, stomp'> / <stomp', stomp'> over the whole clip, stomp'' = a * stomp'.
gain_db = 20 log10(a) is what the Stomp render must be scaled by to equal Native's level. The
Stomp render already includes reamp calibration (cal_level_gain_db in the JSON). The residual is
relative to the Native render: null_residual_db = 10 log10(sum (stomp'' - native)^2 / sum native^2).

Run from the human's Terminal (the Stomp reamp needs that app's Microphone permission; Native's
window opens briefly for each new chain). HX Edit must be quit.

Known non-identities, stated in every summary: pedal firmware 3.50 vs Native 3.82 (a model's
algorithm can differ between releases); the Stomp path still includes its output stage, USB
converter and the reamp input path; tempo-synced delays follow the preset's tempo on the pedal
but Native's host tempo; both render at 48 kHz.
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
sys.path.insert(0, str(ROOT / "scripts"))

SR = 48_000
DATASET = "parity-v0"
INJECT = ROOT / "cache" / "stomp" / "inject"
PARITY = ROOT / "cache" / "parity"
DI_SOURCE = ROOT / "data" / "di" / "Guitar-TECHS" / "P3_music" / "audio" / "directinput" / "directinput_03.wav"
DI_DEFAULT = PARITY / "di_10s_tail.wav"
DI_SHORT = ROOT / "fixtures" / "smoke" / "di_2s_real.wav"
DEFAULT_PRESETS = ("K", "A", "B0", "B", "C", "H")
ORIGINAL = ("Smoke Known",)  # name of the pedal's preset (04C) the variants were compiled against
MAX_LAG = 2400                # +-50 ms search for the Stomp-vs-Native offset
BAND_EDGES = [44.19 * 2.0 ** k for k in range(10)] + [SR / 2]  # octave bands 63 Hz .. 16 kHz (last to Nyquist)
BAND_CENTRES = [62.5 * 2 ** k for k in range(9)] + [23300.0]  # last: 22.6 kHz to Nyquist
NON_IDENTITIES = [
    "Pedal firmware 3.50 vs Helix Native 3.82: models present in both may differ slightly between releases.",
    "The Stomp path still includes its output stage and USB converter (and the reamp input path); Native is all digital.",
    "Tempo-synced delays follow the preset's tempo on the pedal and the host tempo in Native (preset B uses TempoSync).",
    "Both render at 48 kHz; the Stomp render is trimmed by its measured latency and scaled by its reamp calibration gain.",
]


# ----------------------------------------------------------------------------- DSP


def rms_db(x: np.ndarray) -> float:
    x = np.asarray(x, np.float64)
    return float(10 * np.log10(max(float(np.mean(x ** 2)), 1e-30))) if x.size else -300.0


def _db(num: float, den: float) -> float:
    return float(10 * np.log10(max(num, 1e-30) / max(den, 1e-30)))


def shift_fractional(x: np.ndarray, d: float, pad: int = SR) -> np.ndarray:
    """y[n] = x[n + d] for a real, possibly fractional d (positive d advances the signal), by a
    frequency-domain phase ramp on a zero-padded copy; same length as x."""
    n = len(x)
    nfft = 1 << int(np.ceil(np.log2(n + 2 * pad)))
    buf = np.zeros(nfft)
    buf[:n] = x
    f = np.fft.rfftfreq(nfft)
    y = np.fft.irfft(np.fft.rfft(buf) * np.exp(2j * np.pi * f * d), nfft)
    return y[:n].astype(np.float64)


def estimate_lag(ref: np.ndarray, x: np.ndarray, max_lag: int = MAX_LAG) -> tuple[float, float]:
    """How many samples x lags ref (positive: x is later), to a fraction of a sample, and the
    normalised correlation at that lag. Cross-correlation by FFT, parabolic peak interpolation."""
    n = min(len(ref), len(x))
    r, s = np.asarray(ref[:n], np.float64), np.asarray(x[:n], np.float64)
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    c = np.fft.irfft(np.conj(np.fft.rfft(r, nfft)) * np.fft.rfft(s, nfft), nfft)  # c[k] = sum r[i] s[i+k]
    lags = np.arange(-max_lag, max_lag + 1)
    vals = c[lags % nfft]
    k = int(np.argmax(vals))
    frac = 0.0
    if 0 < k < len(vals) - 1:
        a, b, g = vals[k - 1], vals[k], vals[k + 1]
        den = a - 2 * b + g
        if den != 0:
            frac = float(np.clip(0.5 * (a - g) / den, -0.5, 0.5))
    norm = float(np.sqrt(np.sum(r ** 2) * np.sum(s ** 2))) or 1.0
    return float(lags[k] + frac), float(vals[k] / norm)


def refine_lag(ref: np.ndarray, x: np.ndarray, lag: float, half_width: float = 0.75, iters: int = 24) -> float:
    """Golden-section search around lag for the fractional shift that maximises the correlation of
    x (moved by -lag) with ref; the parabolic estimate from the sampled correlation is biased for
    band-limited signals."""
    r = np.asarray(ref, np.float64)

    def score(d: float) -> float:
        y = shift_fractional(x, d)
        return float(np.dot(r, y) / max(np.sqrt(np.dot(y, y)), 1e-30))

    g = (np.sqrt(5) - 1) / 2
    a, b = lag - half_width, lag + half_width
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = score(c), score(d)
    for _ in range(iters):
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = score(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = score(d)
    return float((a + b) / 2)


def band_energies(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, np.float64)
    # One-sided power spectrum scaled so the bands sum to the time-domain energy sum(x**2) (Parseval):
    # band dB values are then re the signal's total energy, not offset by 10*log10(N).
    spec = np.abs(np.fft.rfft(x)) ** 2 / len(x)
    spec[1:] *= 2.0
    if len(x) % 2 == 0:
        spec[-1] /= 2.0
    f = np.fft.rfftfreq(len(x), 1 / SR)
    out = np.zeros(len(BAND_CENTRES))
    for i in range(len(BAND_CENTRES)):
        lo, hi = (0.0 if i == 0 else BAND_EDGES[i]), BAND_EDGES[i + 1]
        out[i] = spec[(f >= lo) & (f < hi)].sum()
    return out


def analyse(stomp: np.ndarray, native: np.ndarray, tail_s: float = 2.0, max_lag: int = MAX_LAG) -> dict:
    """Align (Stomp moved onto Native), level-match, null. Returns metrics plus the processed arrays
    under 'stomp_matched', 'native' and 'residual' (float64, equal length)."""
    n = min(len(stomp), len(native))
    s, nat = np.asarray(stomp[:n], np.float64), np.asarray(native[:n], np.float64)
    if rms_db(s) < -100 or rms_db(nat) < -100:
        raise ValueError("one of the renders is silent")
    lag, corr = estimate_lag(nat, s, max_lag)
    lag = refine_lag(nat, s, lag)
    unaligned_a = float(np.dot(nat, s) / max(np.dot(s, s), 1e-30))
    null_unaligned = _db(float(np.sum((unaligned_a * s - nat) ** 2)), float(np.sum(nat ** 2)))
    s_al = shift_fractional(s, lag)                      # undo the lag: y[n] = s[n + lag]
    a = float(np.dot(nat, s_al) / max(np.dot(s_al, s_al), 1e-30))
    sm = a * s_al
    res = sm - nat
    e_n, e_r = float(np.sum(nat ** 2)), float(np.sum(res ** 2))
    bn, br, bs = band_energies(nat), band_energies(res), band_energies(sm)
    bands = []
    for i, c in enumerate(BAND_CENTRES):
        bands.append({"centre_hz": c, "native_db": _db(bn[i], e_n), "residual_db_re_native_band": _db(br[i], bn[i]),
                      "stomp_minus_native_db": _db(bs[i], bn[i])})
    use = [b for b in bands if b["native_db"] > -60.0]
    tilt = (float(np.polyfit(np.log2([b["centre_hz"] for b in use]), [b["stomp_minus_native_db"] for b in use], 1)[0])
            if len(use) >= 3 else None)
    out = {
        "samples": n, "lag_samples": lag, "lag_ms": lag / SR * 1000, "xcorr_peak": corr,
        "gain_db": float(20 * np.log10(abs(a))) if a else None, "gain_linear": a,
        "null_residual_db": _db(e_r, e_n), "null_residual_db_unaligned_gain_only": null_unaligned,
        "native_rms_db": rms_db(nat), "stomp_rms_db_raw": rms_db(s), "residual_rms_db": rms_db(res),
        "bands": bands, "spectral_tilt_db_per_octave": tilt,
        "stomp_matched": sm, "native": nat, "residual": res,
    }
    k = int(tail_s * SR)
    if n > 2 * k:
        out["tail_s"] = tail_s
        out["tail_null_residual_db"] = _db(float(np.sum(res[-k:] ** 2)), float(np.sum(nat[-k:] ** 2)))
        out["tail_native_rms_db"], out["tail_stomp_rms_db"] = rms_db(nat[-k:]), rms_db(sm[-k:])
        out["body_null_residual_db"] = _db(float(np.sum(res[:-k] ** 2)), float(np.sum(nat[:-k] ** 2)))
    return out


# ----------------------------------------------------------------------------- files


def write_wav(path: Path, x: np.ndarray) -> None:
    import soundfile as sf

    sf.write(str(path), np.asarray(x, np.float32), SR, subtype="FLOAT")


def make_di(out: Path = DI_DEFAULT, source: Path = DI_SOURCE, start_s: float = 5.5, dur_s: float = 10.0,
            tail_s: float = 2.0, peak_dbfs: float = -12.5) -> Path:
    """10 s excerpt (dual mono mixdown, peak normalised, 10 ms fades) + silence for delay tails."""
    import soundfile as sf

    x, sr = sf.read(str(source), dtype="float64", always_2d=True)
    assert sr == SR, f"{source} is {sr} Hz"
    seg = x[int(start_s * SR): int((start_s + dur_s) * SR)].mean(axis=1)
    seg *= 10 ** (peak_dbfs / 20) / max(float(np.max(np.abs(seg))), 1e-9)
    f = int(0.01 * SR)
    seg[:f] *= np.linspace(0, 1, f)
    seg[-f:] *= np.linspace(1, 0, f)
    y = np.concatenate([seg, np.zeros(int(tail_s * SR))]).astype(np.float32)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_wav(out, y)
    return out


def load_di(path: Path) -> np.ndarray:
    import soundfile as sf

    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if sr != SR:
        raise SystemExit(f"{path} is {sr} Hz, need {SR}")
    return x.mean(axis=1).astype(np.float32)


def extra_variants(cat: dict) -> dict:
    """C (clean) and H (high gain + untied delay), built like the T018s variants from known.hlx so the
    block layout (amp, cab, delay slot) is the one the Stomp compile accepts."""
    import stomp_inject as SI

    base = SI.make_variants(cat)["K"]
    amp = next(i for i, s in enumerate(base.slots) if s.category == "amp")
    cab = next(i for i, s in enumerate(base.slots) if s.category == "cab")
    dly = next(i for i, s in enumerate(base.slots) if s.category == "delay")

    def set_amp_cab(p, amp_id, cab_id, **amp_over):
        p.slots[amp].model_id, p.slots[amp].params = amp_id, SI._defaults(cat, amp_id)
        p.slots[amp].params.update(amp_over)
        p.slots[cab].model_id, p.slots[cab].params = cab_id, SI._defaults(cat, cab_id)
        p.slots[cab].extra["@mic"] = cat["models"][cab_id]["params"]["@mic"]["default"]

    c = copy.deepcopy(base)
    c.name = "Parity C clean"
    set_amp_cab(c, "HD2_AmpUSDeluxeNrm", "HD2_Cab1x12USDeluxe", Drive=0.3, ChVol=0.8, Master=1.0)
    h = copy.deepcopy(base)
    h.name = "Parity H hi-gain"
    set_amp_cab(h, "HD2_AmpPlacaterDirty", "HD2_Cab4x12UberV30", Drive=0.85, Master=0.6, ChVol=0.7)
    h.slots[dly].enabled = True
    h.slots[dly].params.update(TempoSync1=False, Time=0.375, Feedback=0.4, Mix=0.25)   # fixed seconds, no tempo
    for sn in h.snapshots:
        sn.enabled[h.slots[dly].key] = True
        # known.hlx assigns Mix and Feedback to MIDI CC 110, so every snapshot stores their values
        # and the pedal recalls those over the block's (T051). Keep them equal to the block.
        for pn in sn.params.get(h.slots[dly].key, {}):
            if pn in h.slots[dly].params:
                sn.params[h.slots[dly].key][pn] = h.slots[dly].params[pn]
    out = {"C": c, "H": h}
    for p in out.values():
        for s in p.blocks():
            assert SI._fw_ok(cat["models"][s.model_id]), f"{s.model_id} is newer than 3.50"
            assert cat["models"][s.model_id].get("native", True) is not False
    return out


def cmd_gen(_a) -> int:
    import stomp_inject as SI

    from tonematch import catalog as C

    cat = C.load_catalog("current")
    SI.OUT.mkdir(parents=True, exist_ok=True)
    rc = 0
    allv = dict(SI.make_variants(cat))
    allv.update(extra_variants(cat))
    for name in DEFAULT_PRESETS:
        f = INJECT / f"{name}.hlx"
        if name in ("C", "H") or not f.exists():
            f.write_text(SI.hlx_text(allv[name]), encoding="utf-8")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "hlx_validate.py"), str(f), "--firmware",
                            hex(SI.FW_350)], capture_output=True, text=True, check=False)
        print(f"{name}: {f} validate exit {r.returncode}")
        rc = max(rc, r.returncode)
    if not DI_DEFAULT.exists():
        make_di()
    print("DI:", DI_DEFAULT)
    return rc


def preset_paths(names: list[str]) -> dict[str, Path]:
    out = {}
    for n in names:
        p = Path(n)
        out[p.stem if p.suffix == ".hlx" else n] = p if p.suffix == ".hlx" else INJECT / f"{n}.hlx"
    return out


# ----------------------------------------------------------------------------- run


def ledger(args: list[str], enabled: bool) -> None:
    if not enabled:
        print("(no ledger) would append:", " ".join(args))
        return
    env = dict(os.environ)
    env.setdefault("TONEMATCH_AGENT", "benchmark")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), *args], check=True, env=env)


def default_stomp_factory():
    from tonematch.engines.stomp import StompEngine

    return StompEngine(whole_preset="always")


PREROLL = 144_000       # 3 s: delay lines survive Native's reset (file B alternates between renders with 0.1 s)
TEMPLATES = ROOT / "cache" / "native" / "templates_files"
HOSTVENV = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"


def default_native_factory(name: str):
    """One NativeEngine per preset on its own captured template dir (scripts/native_capture_files.py).
    Autocapture is forced OFF: a missing template is an error for that preset, nothing new is captured."""
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and HOSTVENV.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(HOSTVENV)
    from tonematch.engines.native import NativeEngine

    return NativeEngine(templates_dir=TEMPLATES / name, preroll=PREROLL)


def default_verify_restore(eng) -> dict:
    """The pedal's edit buffer is byte-equal to the backup taken at open. Since T061 the engine checks
    this in its own session (close(verify_bytes=True)); a fresh helper session only for engines
    that did not."""
    rr = getattr(eng, "restore_report", {}) or {}
    if "bytes_equal_backup" in rr:
        o = rr.get("original") or {}
        return {"after": {k: o.get(k) for k in ("setlist", "index", "label", "name")},
                "bytes_equal_backup": bool(rr["bytes_equal_backup"]), "same_session": True}
    from tonematch.engines.stomp_bridge import StompBridge

    with StompBridge() as b:
        info = b.call("info")
        after = Path(eng.backup_path).with_name("parity_after_restore.bin")
        b.call("backup", path=str(after))
    return {"after": {k: info.get(k) for k in ("setlist", "index", "label", "name")},
            "bytes_equal_backup": after.read_bytes() == Path(eng.backup_path).read_bytes()}


def render_all(factory, presets: dict[str, Path], dry: np.ndarray, raw_dir: Path, engine: str, reuse: bool,
               catalog, summary: dict, extra_check=None, verify=None,
               gap_s: float = 0.0) -> dict[str, np.ndarray]:
    """Load each preset into one engine and render the DI; raw renders are saved so a run can resume."""
    from tonematch import preset as P

    renders: dict[str, np.ndarray] = {}
    todo = []
    for name in presets:
        f = raw_dir / f"{name}_{engine}.wav"
        if reuse and f.exists():
            renders[name] = load_di(f)
            summary["engines"][engine].setdefault("reused", []).append(name)
        else:
            todo.append(name)
    if not todo:
        return renders
    info = summary["engines"][engine]
    # Stomp: one session for all presets. Native: one engine per preset (each preset has its own template dir).
    groups = [todo] if engine == "stomp" else [[n] for n in todo]
    for names in groups:
        eng = factory() if engine == "stomp" else factory(names[0])
        try:
            caps = eng.capabilities()
            info.update({"firmware_version": caps.firmware_version,
                         "extras": {k: str(v) for k, v in caps.extras.items()}})
            if extra_check:
                extra_check(eng, info)
            for name in names:
                t0 = time.perf_counter()
                try:
                    try:
                        eng.load(P.read_hlx(presets[name], catalog))
                    finally:
                        # Full-parameter readback (T051): a Stomp load that did not land as the .hlx
                        # says raises ReadbackMismatch, so this preset is never rendered or scored.
                        if engine == "stomp" and getattr(eng, "whole_writes", None):
                            info.setdefault("readback", {})[name] = eng.whole_writes[-1].get("checks", {})
                    t1 = time.perf_counter()
                    y = np.asarray(eng.render(dry), np.float32)
                    info.setdefault("timing_s", {})[name] = {"load": t1 - t0, "render": time.perf_counter() - t1}
                    write_wav(raw_dir / f"{name}_{engine}.wav", y)
                    renders[name] = y
                    print(f"{engine} {name}: ok load {t1 - t0:.1f}s rms {rms_db(y):.1f} dB", flush=True)
                except Exception as e:  # one preset failing must not stop the others
                    summary["errors"].append(f"{engine} {name}: {type(e).__name__}: {e}")
                    if type(e).__name__ == "ReadbackMismatch":
                        print(f"\n*** {engine} {name}: THE PEDAL DOES NOT HOLD THIS PRESET; not rendered ***", flush=True)
                    print(f"{engine} {name}: ERROR {e}", flush=True)
                    if type(e).__name__ == "BridgeError":
                        raise
                if engine == "stomp" and gap_s:
                    time.sleep(gap_s)   # let the previous preset's delay tail die before the next write+render
        finally:
            if engine == "stomp":
                try:
                    eng.close(verify_bytes=True)   # restore check in the same session (T061 item 1)
                except TypeError:
                    eng.close()
            else:
                eng.close()
            if engine == "stomp":
                info["restore_report"] = getattr(eng, "restore_report", {})
                if verify is not None:
                    try:
                        info["restore_check"] = verify(eng)
                    except Exception as e:  # noqa: BLE001
                        info["restore_check"] = {"error": f"{type(e).__name__}: {e}"}
                        summary["errors"].append(f"restore check: {e}")
    return renders


def run(presets: dict[str, Path], di_path: Path, out_dir: Path, *, engines=("stomp", "native"), reuse=False,
        use_ledger=True, stomp_factory=None, native_factory=None, verify_restore=None, catalog=None,
        stomp_gap_s: float = 0.0) -> dict:
    from tonematch import catalog as C

    catalog = catalog or C.load_catalog("current")
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    dry = load_di(di_path)
    summary: dict = {"dataset": DATASET, "di": str(di_path), "di_seconds": len(dry) / SR, "presets": list(presets),
                     "engines": {"stomp": {}, "native": {}}, "errors": [], "results": {},
                     "non_identities": NON_IDENTITIES, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    renders: dict[str, dict[str, np.ndarray]] = {}

    def stomp_check(eng, info):
        orig = getattr(eng, "original", {})
        info["original_preset"] = orig
        if orig and orig.get("name") not in ORIGINAL:
            raise SystemExit(f"the pedal is on {orig}, not 04C 'Smoke Known': stopping before any write")
        cal = getattr(eng, "cal", None)
        if cal is not None:
            info["cal_level_gain_db"] = float(getattr(cal, "level_gain_db", float("nan")))

    try:
        if "stomp" in engines:
            renders["stomp"] = render_all(stomp_factory or default_stomp_factory, presets, dry, raw_dir, "stomp", reuse,
                                          catalog, summary, stomp_check, verify_restore or default_verify_restore,
                                          gap_s=stomp_gap_s)
        if "native" in engines:
            renders["native"] = render_all(native_factory or default_native_factory, presets, dry, raw_dir, "native",
                                           reuse, catalog, summary)
    except SystemExit as e:
        summary["errors"].append(str(e))
    renders.setdefault("stomp", {})
    renders.setdefault("native", {})
    # Reuse earlier raw renders of an engine that was not run now.
    for eng_name in ("stomp", "native"):
        for name in presets:
            f = raw_dir / f"{name}_{eng_name}.wav"
            if name not in renders[eng_name] and f.exists():
                renders[eng_name][name] = load_di(f)
    for name in presets:
        s, n = renders["stomp"].get(name), renders["native"].get(name)
        if s is None or n is None:
            summary["results"][name] = {"skipped": f"missing render: stomp={s is not None} native={n is not None}"}
            continue
        r = analyse(s, n)
        stereo = np.stack([r["stomp_matched"], r["native"]], axis=1)
        write_wav(out_dir / f"{name}_stereo.wav", stereo)
        write_wav(out_dir / f"{name}_residual.wav", r["residual"])
        res = {k: v for k, v in r.items() if k not in ("stomp_matched", "native", "residual")}
        res["cal_level_gain_db"] = summary["engines"]["stomp"].get("cal_level_gain_db")
        res["files"] = {"stereo": f"{name}_stereo.wav", "residual": f"{name}_residual.wav"}
        summary["results"][name] = res
        (out_dir / f"{name}.json").write_text(json.dumps(res, indent=1))
        print(f"{name}: null {res['null_residual_db']:.1f} dB, gain {res['gain_db']:+.2f} dB, "
              f"lag {res['lag_samples']:+.2f} samples, xcorr {res['xcorr_peak']:.3f}", flush=True)
    fw = summary["engines"]["stomp"].get("firmware_version", "?")
    nv = summary["engines"]["native"].get("firmware_version", "?")
    for name, res in summary["results"].items():
        if "null_residual_db" not in res:
            continue
        notes = (f"preset={name};fw={fw};native={nv};lag_samples={res['lag_samples']:.2f};xcorr={res['xcorr_peak']:.3f};"
                 f"tilt_db_oct={res['spectral_tilt_db_per_octave']};di={Path(summary['di']).name};stereo L=Stomp R=Native")
        ledger(["--kind", "parity", "--engine", "both", "--dataset", DATASET, "--metric", "null_residual_db",
                "--value", f"{res['null_residual_db']:.2f}", "--notes", notes], use_ledger)
        ledger(["--kind", "parity", "--engine", "both", "--dataset", DATASET, "--metric", "stomp_to_native_gain_db",
                "--value", f"{res['gain_db']:.2f}", "--notes",
                (f"preset={name};level-match gain applied to the Stomp render "
                 f"(includes cal gain {res.get('cal_level_gain_db')} dB);fw={fw};native={nv}")], use_ledger)
    rc = summary["engines"]["stomp"].get("restore_check", {})
    if "bytes_equal_backup" in rc:
        ledger(["--kind", "parity", "--engine", "stomp", "--dataset", DATASET, "--metric", "restore_bytes_equal",
                "--value", str(int(bool(rc["bytes_equal_backup"]))), "--notes",
                f"edit buffer byte-diff against the backup after the parity run; pedal now {rc.get('after')}"], use_ledger)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print("summary:", out_dir / "summary.json")
    return summary


def cmd_check(a) -> int:
    import stomp_inject as SI

    from tonematch import catalog as C
    from tonematch import preset as P

    cat = C.load_catalog("current")
    rc = 0
    for name, f in preset_paths(a.presets).items():
        if not f.exists():
            print(f"{name}: MISSING {f} (run `parity_null.py gen`)")
            rc = 1
            continue
        p = P.read_hlx(f, cat)
        ok = all(SI._fw_ok(cat["models"][s.model_id]) for s in p.blocks())
        print(f"{name}: {f.name} fw {p.device_version:#x} blocks {[s.model_id for s in p.blocks()]} stomp-3.50-ok={ok}")
        rc = max(rc, 0 if ok else 1)
        from tonematch.engines.stomp_readback import snapshot_conflicts

        conflicts = snapshot_conflicts(json.loads(f.read_text()))
        if conflicts:
            # The Stomp engine renders the block values (it syncs the snapshots); a host that
            # honours snapshot recall would render the snapshot values: the two would differ.
            print(f"   WARNING the file's snapshot controller values differ from its blocks: {conflicts}")
        if a.base:
            doc = SI.document_check(f, Path(a.base))
            print(f"   offline document check on {Path(a.base).name}: "
                  f"{'OK' if doc['ok'] else 'MISMATCH'} ({doc['checked']} checks) {doc['mismatches'] + doc['missing']}"
                  f"{' warnings ' + str(doc['warnings']) if doc.get('warnings') else ''}")
            rc = max(rc, 0 if doc["ok"] else 1)
    print("DI:", a.di, "exists" if Path(a.di).exists() else "MISSING (run gen)")
    return rc


def cmd_run(a) -> int:
    if subprocess.run(["pgrep", "-f", "HX Edit.app/Contents/MacOS/HX Edit"], capture_output=True, check=False).stdout.strip():
        sys.exit("HX Edit is running: quit it first (it holds the control interface)")
    for var in ("TONEMATCH_STOMPD", "TONEMATCH_STOMPD_COMPILE"):
        if "stomp" in a.engines and not os.environ.get(var):
            sys.exit(f"set {var} (see the run command in scripts/README.md)")
    if not Path(a.di).exists():
        make_di() if Path(a.di) == DI_DEFAULT else sys.exit(f"{a.di} missing")
    out_dir = Path(a.out_dir) if a.out_dir else PARITY / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    s = run(preset_paths(a.presets), Path(a.di), out_dir, engines=tuple(a.engines), reuse=a.reuse,
            use_ledger=not a.no_ledger, verify_restore=None, stomp_gap_s=a.stomp_gap_s)
    return 1 if s["errors"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen")
    for nm in ("check", "run"):
        p = sub.add_parser(nm)
        p.add_argument("--presets", nargs="+", default=list(DEFAULT_PRESETS),
                       help="names in cache/stomp/inject/ or paths to .hlx (the same file goes to both engines)")
        p.add_argument("--di", default=str(DI_DEFAULT), help=f"default {DI_DEFAULT}; short option {DI_SHORT}")
        if nm == "check":
            p.add_argument("--base", help="a device document (an edit-buffer backup of 04C): compile each preset onto "
                                          "it offline and check every parameter of the result (needs "
                                          "TONEMATCH_STOMPD_COMPILE; no USB)")
        if nm == "run":
            p.add_argument("--out-dir", help="default cache/parity/<stamp>; reuse a dir with --reuse to resume")
            p.add_argument("--engines", nargs="+", choices=["stomp", "native"], default=["stomp", "native"])
            p.add_argument("--reuse", action="store_true", help="use raw renders already in <out-dir>/raw")
            p.add_argument("--stomp-gap-s", type=float, default=3.0,
                           help="wait after each Stomp render so the delay tail dies (default 3 s)")
            p.add_argument("--no-ledger", action="store_true")
            from tonematch.engines.stomp_bridge import add_usb_reset_arg
            add_usb_reset_arg(p)
    a = ap.parse_args(argv)
    from tonematch.engines.stomp_bridge import apply_usb_reset_arg
    apply_usb_reset_arg(a)
    return {"gen": cmd_gen, "check": cmd_check, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
