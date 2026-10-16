#!/usr/bin/env python3
"""Separation stress curve (dataset sep-stress-v1). Owner: separation.

Guitar: Guitar-TECHS P3_music DI excerpts (CC BY 4.0, data/di), passed through a plain numpy
drive+cab stand-in (not Helix; Native was busy). Accompaniment: procedurally synthesised bass,
drums, keys and a second guitar (no third-party audio, so no licence question). Five densities
x guitar-to-accompaniment levels, separated with htdemucs_6s (and any other installed separator),
scored against the known stem. Everything lands under cache/sep-stress-v1/ (gitignored).

  python scripts/sep_stress.py [--clips 02,03,08] [--secs 20] [--models htdemucs_6s] [--no-ledger]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import soundfile as sf
from scipy.signal import butter, lfilter, sosfilt

from tonematch.metrics import ltas, mrstft
from tonematch.separation import confidence_mask, get_separator, separate
from tonematch.separation.audio import write_wav

SR = 48_000
OUT = ROOT / "cache" / "sep-stress-v1"
DI = ROOT / "data/di/Guitar-TECHS/P3_music/audio/directinput"
DENSITIES = ["D1_guitar", "D2_bass", "D3_drums", "D4_keys", "D5_band2gtr"]
LEVELS_DB = {"+6": 6.0, "0": 0.0, "-6": -6.0}   # guitar RMS relative to accompaniment RMS


def rms(x):
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))) + 1e-12)


def amp(x, drive):
    """Stand-in amp: HPF, tanh drive, cab-like 80 Hz-5 kHz band-pass. drive 0 = clean DI + cab."""
    y = x.astype(np.float64)
    y = sosfilt(butter(2, 90, "hp", fs=SR, output="sos"), y, axis=-1)
    if drive > 0:
        y = np.tanh(drive * y / (rms(y) * 3)) / np.tanh(drive / 3)
    y = sosfilt(butter(4, 5000, "lp", fs=SR, output="sos"), y, axis=-1)
    return (y / (np.abs(y).max() + 1e-9) * 0.5).astype(np.float32)


def accompaniment(n, seed, bpm=100.0):
    rng = np.random.default_rng(seed)
    beat = 60 / bpm
    roots = [57, 53, 48, 55]   # A F C G (midi)
    triads = [(0, 3, 7), (0, 4, 7), (0, 4, 7), (0, 4, 7)]
    f = lambda m: 440 * 2 ** ((m - 69) / 12)
    bass, drums, keys = (np.zeros(n) for _ in range(3))
    bar = 4 * beat
    for b in range(int(n / SR / bar) + 1):
        r = roots[b % 4] - 12
        for q in range(8):   # eighth-note bass, saw-ish through LPF
            s = int((b * bar + q * beat / 2) * SR)
            t = np.arange(int(beat * 0.45 * SR)) / SR
            note = np.sign(np.sin(2 * np.pi * f(r) * t)) * 0.5 + np.sin(2 * np.pi * f(r) * t)
            note *= np.exp(-t * 6)
            e = min(n, s + len(t))
            if s < n:
                bass[s:e] += note[:e - s]
        for k in range(4):
            s = int((b * bar + k * beat) * SR)
            if s >= n:
                continue
            t = np.arange(int(0.3 * SR)) / SR
            if k in (0, 2):
                x = np.sin(2 * np.pi * (45 + 90 * np.exp(-t * 35)) * t) * np.exp(-t * 11)
            else:
                x = rng.uniform(-1, 1, t.size) * np.exp(-t * 18) * 0.7 + np.sin(2 * np.pi * 190 * t) * np.exp(-t * 25) * 0.4
            e = min(n, s + len(t)); drums[s:e] += x[:e - s]
            for h in (0, 1):
                hs = s + int(h * beat / 2 * SR)
                if hs < n:
                    hat = np.diff(rng.uniform(-1, 1, int(0.06 * SR) + 1)) * np.exp(-np.arange(int(0.06 * SR)) / 500) * 0.35
                    e = min(n, hs + len(hat)); drums[hs:e] += hat[:e - hs]
        s = int(b * bar * SR)   # keys: sustained chord, EP-like partials
        t = np.arange(min(n - s, int(bar * SR))) / SR if s < n else np.arange(0)
        for iv in triads[b % 4]:
            fr = f(roots[b % 4] + 12 + iv)
            keys[s:s + t.size] += (np.sin(2 * np.pi * fr * t) + 0.4 * np.sin(4 * np.pi * fr * t) * np.exp(-t * 2)) * np.exp(-t * 0.8) * 0.3
    bass = lfilter(*butter(2, 900, "lp", fs=SR), bass)
    keys = lfilter(*butter(2, 3500, "lp", fs=SR), keys)
    st = lambda x, l, r: np.stack([x * l, x * r]).astype(np.float32)
    return {"bass": st(bass, 1, 1), "drums": st(drums, 0.9, 0.9), "keys": st(keys, 0.8, 1.0)}


def build(clip, secs, seed):
    d, _ = sf.read(str(DI / f"directinput_{clip}.wav"), dtype="float32", always_2d=True)
    d2, _ = sf.read(str(DI / f"directinput_{(int(clip) % 12) + 1:02d}.wav"), dtype="float32", always_2d=True)
    n = secs * SR
    def cut(a):
        a = a.T   # [ch, n]
        s = max(0, a.shape[1] // 2 - n // 2)
        return a[:, s:s + n]
    drive = {"02": 8, "03": 0, "08": 5}.get(clip, 6)
    g = amp(cut(d), drive)
    g2 = amp(cut(d2), 6)
    return g, g2, accompaniment(n, seed)


def sdr(ref, est):
    num, den = np.sum(ref ** 2), np.sum((ref - est) ** 2)
    return float(10 * np.log10((num + 1e-12) / (den + 1e-12)))


def si_sdr(ref, est):
    a = np.sum(est * ref) / (np.sum(ref ** 2) + 1e-12)
    t = a * ref
    return float(10 * np.log10((np.sum(t ** 2) + 1e-12) / (np.sum((est - t) ** 2) + 1e-12)))


def sample_mask(m, n):
    frame = m.values.mean(axis=0)
    per = np.repeat(frame, m.hop)[:n]
    per = np.pad(per, (0, n - per.size), mode="edge") if per.size < n else per
    return per.astype(np.float64)


def ledger(dataset, metric, value, notes, wall=None):
    cmd = [sys.executable, str(ROOT / "scripts/ledger_append.py"), "--kind", "stress", "--dataset", dataset,
           "--metric", metric, "--value", f"{value:.6g}", "--notes", notes]
    if wall is not None:
        cmd += ["--wall-clock-s", f"{wall:.3f}"]
    subprocess.run(cmd, check=True, env={**os.environ, "TONEMATCH_AGENT": "separation"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", default="02,03,08")
    ap.add_argument("--secs", type=int, default=20)
    ap.add_argument("--models", default="htdemucs_6s")
    ap.add_argument("--no-ledger", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for model in a.models.split(","):
        try:
            get_separator(model)
        except Exception as e:  # noqa: BLE001
            print(f"skip {model}: {e}")
            continue
        for ci, clip in enumerate(a.clips.split(",")):
            g, g2, acc = build(clip, a.secs, seed=ci)
            for dens in range(5):
                levels = ["0"] if dens == 0 else list(LEVELS_DB)
                a_parts = [acc["bass"]] if dens >= 1 else []
                if dens >= 2: a_parts.append(acc["drums"])
                if dens >= 3: a_parts.append(acc["keys"])
                if dens >= 4: a_parts.append(g2 * 1.0)
                accsum = sum(a_parts) if a_parts else np.zeros_like(g)
                for lv in levels:
                    gain = 1.0 if dens == 0 else (rms(accsum) / rms(g)) * 10 ** (LEVELS_DB[lv] / 20)
                    tg = (g * gain).astype(np.float32)
                    mix = tg + accsum
                    pk = np.abs(mix).max()
                    sc = 0.7 / pk if pk > 0.7 else 1.0
                    mix, tg = mix * sc, tg * sc
                    name = f"{clip}_{DENSITIES[dens]}_{lv}"
                    mp = OUT / "mixes" / f"{name}.wav"
                    mp.parent.mkdir(exist_ok=True)
                    write_wav(mp, mix)
                    write_wav(OUT / "mixes" / f"{name}_truth.wav", tg)
                    t0 = time.perf_counter()
                    st = separate(mp, model=model, cache_root=OUT / "stems", use_cache=False)
                    wall = time.perf_counter() - t0
                    k = np.float32(10 ** (st.gain_db / 20))
                    truth = tg * k
                    est = st.guitar[:, :truth.shape[1]]
                    cm = confidence_mask(st)
                    pm = sample_mask(cm, truth.shape[1])
                    r = dict(model=model, clip=clip, density=dens + 1, dname=DENSITIES[dens], level=lv,  # noqa: C408
                             sdr=sdr(truth, est), si_sdr=si_sdr(truth, est), conf=cm.energy_weighted_mean,
                             exposed=cm.exposed_fraction, wall=wall, backend=st.backend,
                             ltas=ltas.term(truth, est), mr=mrstft.term(truth, est),
                             ltas_m=ltas.distance(ltas.features(est, pm), ltas.features(truth, pm)), mr_m=mrstft.term(est, truth, pm),
                             tilt=ltas.features(est).tilt_db_oct - ltas.features(truth).tilt_db_oct)
                    rows.append(r)
                    print(f"{name:28s} sdr {r['sdr']:6.2f} si {r['si_sdr']:6.2f} conf {r['conf']:.2f} ltas {r['ltas']:.2f} mr {r['mr']:.2f} {wall:.1f}s {st.backend}", flush=True)
    (OUT / "results.json").write_text(json.dumps(rows, indent=1))
    # tone yardstick: distance between two different amp settings of the same playing
    clip = a.clips.split(",")[0]
    gdi = sf.read(str(DI / f"directinput_{clip}.wav"), dtype="float32", always_2d=True)[0].T[:, :a.secs * SR]
    y_a, y_b = amp(gdi, 0), amp(gdi, 8)
    yard = {"ltas": ltas.term(y_a, y_b), "mr": mrstft.term(y_a, y_b)}
    (OUT / "yardstick.json").write_text(json.dumps(yard))
    print("yardstick clean-vs-driven", yard)
    if a.no_ledger or not rows:
        return
    for model in sorted({r["model"] for r in rows}):
        rs = [r for r in rows if r["model"] == model]
        be = rs[0]["backend"]
        ds = "sep-stress-v1"
        for d in range(1, 6):
            q = [r for r in rs if r["density"] == d]
            if not q:
                continue
            nt = f"{model}/{be}; density {DENSITIES[d-1]}; n={len(q)} mixes (clips {a.clips}, levels {','.join(sorted({r['level'] for r in q}))}); {a.secs} s each"
            m = lambda k, q=q: float(np.mean([r[k] for r in q]))
            lo = min(r["sdr"] for r in q)
            ledger(ds, f"guitar_sdr_db_D{d}", m("sdr"), nt + f"; worst mix {lo:.1f} dB; SI-SDR {m('si_sdr'):.2f}; confidence_mean {m('conf'):.3f}; synthetic accompaniment + numpy amp stand-in")
            ledger(ds, f"tone_shift_ltas_D{d}", m("ltas"), nt + f"; ltas_tilt term true-vs-separated {m('ltas'):.3f} (confidence-masked {m('ltas_m'):.3f}); tilt error {m('tilt'):+.2f} dB/oct; yardstick clean-vs-driven {yard['ltas']:.3f}")
            ledger(ds, f"tone_shift_mrstft_D{d}", m("mr"), nt + f"; mrstft term true-vs-separated {m('mr'):.3f} (masked {m('mr_m'):.3f}); yardstick clean-vs-driven {yard['mr']:.3f}")
        ledger(ds, "seconds_per_mix", float(np.mean([r["wall"] for r in rs])), f"{model}/{be}; mean over {len(rs)} mixes of {a.secs} s", wall=float(np.mean([r["wall"] for r in rs])))


if __name__ == "__main__":
    main()
