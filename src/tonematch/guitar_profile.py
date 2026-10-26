"""Per-guitar calibration profile (T204): level, tone against the Guitar-TECHS bank, and a labelled own-note bank.

The recording session (scripts/guitar_profile_record.py) saves short prompted segments; this module turns them
into `data/guitars/<id>/profile.json` plus `bank.json` (BankNote rows in the T201 format, player "OWN", so
dry_rebuild can use them). Everything is local; nothing here reads Line 6 files or touches song audio.

Tone method: for onsets that carry a clean single pitch, take a 0.35 s window after the onset, normalise it to
unit RMS, and average the power spectra. The same is done for Guitar-TECHS P1/P2 bank notes drawn with the
same pitch histogram ("matched notes"). The dB difference, averaged in 11 half-octave-wide bands and centred
over 125 Hz to 5 kHz (level is kept separate), capped at +-12 dB, is the correction that maps the bank's tone
onto this guitar. It is realised as a minimum-phase FIR.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import soundfile as sf

from tonematch import dry_rebuild as D
from tonematch.dry_rebuild import SR, BankNote
from tonematch.midi_read import MidiNote

ROOT = Path(__file__).resolve().parents[2]
GUITARS = ROOT / "data" / "guitars"
BANDS_HZ = (80, 125, 200, 315, 500, 800, 1250, 2000, 3150, 5000, 8000)
EQ_CAP_DB = 12.0
NFFT = 16384
NOTE_WIN_S = 0.35
STD_OPEN = {6: 40, 5: 45, 4: 50, 3: 55, 2: 59, 1: 64}   # string -> open pitch (6 = low E)
SCHEMA = "guitar-profile/v0"


# --------------------------------------------------------------------------- the session script

def _seg(id_, group, title, text, seconds, **meta):
    return {"id": id_, "group": group, "title": title, "text": text, "seconds": seconds, "meta": meta}


_SNAME = {6: "low E (6th, thickest)", 5: "A (5th)", 4: "D (4th)", 3: "G (3rd)", 2: "B (2nd)", 1: "high E (1st, thinnest)"}


def session_segments() -> list[dict]:
    """The prompted segments in order. Each: id, group (a..g), title, text, seconds, meta (string/technique/dynamic)."""
    s: list[dict] = []
    for st in (6, 5, 4, 3, 2, 1):
        s.append(_seg(f"open_s{st}", "a", f"Open string {st}: {_SNAME[st]}",
                      "Pick the open string 3 times, one pick about every 2 seconds, and let each ring.", 9,
                      string=st, expect="open", picks=3))
    for st in (6, 5, 4, 3, 2, 1):
        s.append(_seg(f"run_s{st}", "b", f"Chromatic run on string {st}: {_SNAME[st]}",
                      "Play every fret from open up to the 12th fret, one note per pick, steady and not rushed "
                      "(13 notes). Or play a scale up this string.", 22, string=st, expect="run"))
    for cid, name in (("E", "E major (open)"), ("A", "A major (open)"), ("D", "D major (open)"), ("G", "G major (open)"),
                      ("C", "C major (open)"), ("E5", "E5 power chord"), ("A5", "A5 power chord"), ("D5", "D5 power chord")):
        s.append(_seg(f"chord_{cid}", "c", f"Chord {name}",
                      "Strum it twice, letting it ring about 2 seconds each time.", 7, expect="chord", chord=cid))
    for dyn, txt in (("soft", "SOFT: pick as gently as you can while every note still sounds."),
                     ("medium", "MEDIUM: your normal picking."),
                     ("hard", "HARD: dig in, as hard as you would in a loud part.")):
        s.append(_seg(f"dyn_{dyn}", "d", f"Picking strength: {dyn}",
                      f"Play 4 single notes on any string, ringing 1 second each. {txt}", 9, expect="notes", dynamic=dyn))
    s.append(_seg("palm_notes", "e", "Palm-muted single notes",
                  "Rest the picking-hand palm on the strings by the bridge. Play 6 single notes on the low strings.",
                  10, expect="notes", technique="palm_mute"))
    s.append(_seg("palm_riff", "e", "Palm-muted riff",
                  "Play a short chugging palm-muted riff of your own, repeating it.", 10, expect="notes", technique="palm_mute"))
    s.append(_seg("extra_bend", "f", "Optional: bends", "Play 3 string bends, held for a second each. Skip if you do not bend.",
                  10, expect="notes", technique="bend", optional=True))
    s.append(_seg("extra_slide", "f", "Optional: slides", "Play 3 slides up or down the neck, one string.", 10,
                  expect="notes", technique="slide", optional=True))
    s.append(_seg("extra_harm", "f", "Optional: harmonics", "Play 3 natural harmonics (12th, 7th or 5th fret, touch lightly).", 10,
                  expect="notes", technique="harmonic", optional=True))
    s.append(_seg("free", "g", "Free playing", "Play 30 seconds in your normal style, as if playing the song.", 30,
                  expect="free"))
    return s


def select_segments(spec: str | None, include_optional: bool = False) -> list[dict]:
    """spec: None (all but group f), or comma list of group letters (a,b) and/or segment ids."""
    allseg = session_segments()
    if not spec:
        return [x for x in allseg if include_optional or x["group"] != "f"]
    want = {t.strip() for t in spec.split(",") if t.strip()}
    out = [x for x in allseg if x["group"] in want or x["id"] in want]
    if not out:
        raise ValueError(f"no segment matches {spec!r}; groups a-g or ids like open_s6, chord_E, dyn_soft, free")
    return out


# --------------------------------------------------------------------------- audio helpers

def read_mono(path: str | Path) -> np.ndarray:
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != SR:
        from math import gcd

        from scipy.signal import resample_poly
        g = gcd(SR, sr)
        x = resample_poly(x, SR // g, sr // g).astype(np.float32)
    return x


def note_level_db(x: np.ndarray, onset_s: float, dur: float = 0.3) -> float:
    seg = x[int(onset_s * SR): int((onset_s + dur) * SR)]
    return float(20 * np.log10(np.sqrt(np.mean(seg.astype(np.float64) ** 2)) + 1e-9)) if len(seg) else -120.0


def detect_onsets(x: np.ndarray, min_gap_s: float = 0.12, floor_db: float = 35.0) -> list[float]:
    """Onset times (s) of plucks: librosa onset detection, kept when the 100 ms after the onset is within
    `floor_db` of the loudest onset window (drops noise and string squeaks); at least min_gap_s apart."""
    import librosa
    if len(x) < SR // 2 or not np.any(x):
        return []
    hop = 256
    env = librosa.onset.onset_strength(y=x.astype(np.float32), sr=SR, hop_length=hop)
    fr = librosa.onset.onset_detect(onset_envelope=env, sr=SR, hop_length=hop, backtrack=False, delta=0.15, wait=int(min_gap_s * SR / hop))
    ons = librosa.frames_to_time(fr, sr=SR, hop_length=hop)
    if len(ons) == 0:
        return []
    # the onset strength peaks a little after the true attack: step back to the steepest rise in the 40 ms before
    ref = [D.refine_onset(x, float(t), back=0.04, fwd=0.01) for t in ons]
    lv = np.array([note_level_db(x, t, 0.1) for t in ref])
    keep = lv > lv.max() - floor_db
    out: list[float] = []
    for t, k in zip(ref, keep):
        if k and (not out or t - out[-1] >= min_gap_s):
            out.append(float(t))
    return out


def onset_pitch(x: np.ndarray, t: float, nxt: float | None = None) -> tuple[float | None, float]:
    """(MIDI pitch estimate, steadiness 0..1) of the note starting at t (YIN over 80-380 ms after the onset, cut
    at the next onset). Steadiness = share of YIN frames within 0.5 semitone of the median: low for chords/noise."""
    import librosa
    a = int((t + 0.08) * SR)
    b = int(min(t + 0.38, (nxt - 0.01) if nxt else 1e9) * SR)
    seg = x[a:b]
    if len(seg) < 4096 + 1024:
        return None, 0.0
    f0 = librosa.yin(seg.astype(np.float32), fmin=70.0, fmax=1500.0, sr=SR, frame_length=4096)
    m = 69 + 12 * np.log2(np.maximum(f0, 1.0) / 440.0)
    med = float(np.median(m))
    return med, float(np.mean(np.abs(m - med) < 0.5))


def basic_pitch_available() -> str | None:
    """Path of the python in cache/venvs/basic-pitch if basic_pitch imports there (T203), else None."""
    import subprocess
    py = ROOT / "cache" / "venvs" / "basic-pitch" / "bin" / "python"
    if not py.exists():
        return None
    r = subprocess.run([str(py), "-c", "import basic_pitch"], capture_output=True)
    return str(py) if r.returncode == 0 else None


def basic_pitch_notes(wav: str | Path, py: str) -> list[tuple[float, float, int]]:
    """(onset s, offset s, pitch) from Basic Pitch with its default settings, run in the T203 venv."""
    import subprocess
    code = ("import sys,json;from basic_pitch.inference import predict;"
            "_,_,ev=predict(sys.argv[1]);print(json.dumps([[float(e[0]),float(e[1]),int(e[2])] for e in ev]))")
    r = subprocess.run([py, "-c", code, str(wav)], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return []
    return [(a, b, p) for a, b, p in json.loads(r.stdout.strip().splitlines()[-1])]


# --------------------------------------------------------------------------- segmenting into labelled notes

def segment_notes(x: np.ndarray, seg: dict, file: str, bp: list[tuple[float, float, int]] | None = None) -> list[BankNote]:
    """Labelled single notes from one segment's audio. Chords yield nothing. Pitch: the prompted pitch for open
    strings; fret order for a 13-note run; else YIN (or Basic Pitch when `bp` is given and agrees within 2
    semitones). Notes whose pitch is unsteady (chords, noise) are skipped for free playing."""
    meta = seg["meta"]
    if meta.get("expect") == "chord":
        return []
    ons = detect_onsets(x)
    out: list[BankNote] = []
    string = meta.get("string")
    run_order = meta.get("expect") == "run" and len(ons) == 13
    for i, t in enumerate(ons):
        nxt = ons[i + 1] if i + 1 < len(ons) else None
        est, steady = onset_pitch(x, t, nxt)
        if bp:
            near = [p for (a, b, p) in bp if abs(a - t) < 0.06 and (est is None or abs(p - est) <= 2.5)]
            if near:
                est = float(near[0])
                steady = max(steady, 0.9)
        if string is not None and meta.get("expect") == "open":
            pitch = STD_OPEN[string]
            if est is not None and abs(est - pitch) > 1.5:
                continue                       # not the string we asked for (noise or a wrong string)
        elif run_order:
            pitch = STD_OPEN[string] + i
            if est is not None and abs(est - pitch) > 1.5:
                pitch = int(round(est))        # skipped or repeated a fret: trust the audio
        else:
            if est is None or (steady < 0.7 and meta.get("expect") == "free"):
                continue
            pitch = int(round(est))
        if not 36 <= pitch <= 92:
            continue
        end = nxt if nxt else len(x) / SR
        fret = pitch - STD_OPEN[string] if string else None
        if fret is not None and not 0 <= fret <= 24:
            fret = None
        tech = meta.get("technique") or ("open_string" if meta.get("expect") == "open" else "normal")
        out.append(BankNote(file, "OWN", seg["id"], pitch, float(t), float(t), max(0.0, end - t - 0.01), string, fret,
                            note_level_db(x, t), 0.0,
                            {"technique": tech, "dynamic": meta.get("dynamic"), "steady": round(steady, 2)}))
    return out


def label_dynamics(notes: list[BankNote]) -> dict:
    """Soft/medium/hard note levels from the d-segments; label every other plain note by the nearest class."""
    lv = {}
    for d in ("soft", "medium", "hard"):
        v = [n.level_db for n in notes if n.extra.get("dynamic") == d]
        lv[d] = float(np.median(v)) if v else None
    for n in notes:
        if n.extra.get("dynamic"):
            continue
        have = {k: v for k, v in lv.items() if v is not None}
        n.extra["dynamic"] = min(have, key=lambda k: abs(have[k] - n.level_db)) if have else None
    return lv


# --------------------------------------------------------------------------- level

def level_block(normal_audio: np.ndarray, dyn: dict) -> dict:
    loud = D.loud_level_db(normal_audio)
    pk = D.peak_db(normal_audio)
    soft, hard = dyn.get("soft"), dyn.get("hard")
    return {"normal_loud_dbfs": round(loud, 2), "peak_dbfs": round(pk, 2), "crest_db": round(pk - loud, 2),
            "ref_loud_dbfs": D.REF_LOUD_DBFS, "vs_ref_db": round(loud - D.REF_LOUD_DBFS, 2),
            "soft_note_dbfs": None if soft is None else round(soft, 2),
            "medium_note_dbfs": None if dyn.get("medium") is None else round(dyn["medium"], 2),
            "hard_note_dbfs": None if hard is None else round(hard, 2),
            "dynamic_range_db": None if soft is None or hard is None else round(hard - soft, 2)}


# --------------------------------------------------------------------------- tone

def ltas_power(x: np.ndarray, onsets: list[float]) -> tuple[np.ndarray, int]:
    """Mean unit-RMS power spectrum over note windows (NFFT bins), and the window count."""
    w = np.hanning(int(NOTE_WIN_S * SR))
    acc = np.zeros(NFFT // 2 + 1)
    n = 0
    for t in onsets:
        seg = x[int((t + 0.01) * SR): int((t + 0.01) * SR) + len(w)].astype(np.float64)
        if len(seg) < len(w):
            continue
        r = np.sqrt(np.mean(seg ** 2))
        if r < 1e-6:
            continue
        acc += np.abs(np.fft.rfft(seg / r * w, NFFT)) ** 2
        n += 1
    return (acc / n if n else acc), n


def band_db(power: np.ndarray, bands=BANDS_HZ) -> np.ndarray:
    f = np.fft.rfftfreq(NFFT, 1 / SR)
    out = []
    for c in bands:
        m = (f >= c / 2 ** 0.5) & (f < c * 2 ** 0.5)
        out.append(10 * np.log10(np.mean(power[m]) + 1e-20))
    return np.array(out)


def fit_tone_eq(own_power: np.ndarray, bank_power: np.ndarray, cap_db: float = EQ_CAP_DB) -> np.ndarray:
    """Band gains (dB) mapping the bank's tone to this guitar: own - bank, mean over 125 Hz-5 kHz removed, capped."""
    d = band_db(own_power) - band_db(bank_power)
    mid = [i for i, c in enumerate(BANDS_HZ) if 125 <= c <= 5000]
    d = d - d[mid].mean()
    return np.clip(d, -cap_db, cap_db)


def eq_curve_db(gains_db, f: np.ndarray) -> np.ndarray:
    """Smooth curve through the band gains, linear in log frequency, held flat outside the end bands."""
    return np.interp(np.log2(np.maximum(f, 1.0)), np.log2(BANDS_HZ), np.asarray(gains_db, float))


def min_phase_fir(gains_db, taps: int = 2048) -> np.ndarray:
    """Minimum-phase FIR whose magnitude follows eq_curve_db (cepstral method)."""
    f = np.fft.rfftfreq(NFFT, 1 / SR)
    logmag = eq_curve_db(gains_db, f) / 20 * np.log(10)
    c = np.fft.irfft(logmag, NFFT)
    fold = np.zeros(NFFT)
    fold[0] = c[0]
    fold[1: NFFT // 2] = 2 * c[1: NFFT // 2]
    fold[NFFT // 2] = c[NFFT // 2]
    h = np.fft.irfft(np.exp(np.fft.rfft(fold)), NFFT)[:taps]
    h[taps // 2:] *= np.linspace(1, 0, taps - taps // 2)
    return h.astype(np.float32)


def apply_fir(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    from scipy.signal import fftconvolve
    return fftconvolve(x, h)[: len(x)].astype(np.float32)


def matched_bank_power(bank: list[BankNote], pitches: list[int], per_pitch: int = 3, seed: int = 0) -> tuple[np.ndarray, int]:
    """Bank LTAS over notes with the same pitch histogram as `pitches` (up to per_pitch bank notes per own note,
    spread over both players; nearest bank pitch when a pitch is absent)."""
    rng = np.random.default_rng(seed)
    by: dict[int, list[BankNote]] = {}
    for b in bank:
        by.setdefault(b.pitch, []).append(b)
    have = sorted(by)
    picks: list[BankNote] = []
    for p in pitches:
        q = p if p in by else min(have, key=lambda v: abs(v - p))
        if abs(q - p) > 2:
            continue
        c = by[q]
        idx = rng.choice(len(c), size=min(per_pitch, len(c)), replace=False)
        picks += [c[i] for i in idx]
    acc, n, cache = np.zeros(NFFT // 2 + 1), 0, {}
    picks.sort(key=lambda b: b.file)
    for b in picks:
        if b.file not in cache:
            cache.clear()
            cache[b.file] = read_mono(b.file)
        p, k = ltas_power(cache[b.file], [b.onset])
        if k:
            acc += p
            n += 1
    return (acc / n if n else acc), n


# --------------------------------------------------------------------------- the profile object

class GuitarProfile:
    def __init__(self, data: dict, bank: list[BankNote], root: Path | None = None):
        self.data, self.bank, self.root = data, bank, root

    @property
    def id(self) -> str:
        return self.data["guitar_id"]

    @property
    def eq_gains_db(self) -> np.ndarray:
        return np.asarray(self.data["tone"]["gains_db"], float)

    def fir(self) -> np.ndarray:
        return min_phase_fir(self.eq_gains_db)

    def save(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "profile.json").write_text(json.dumps(self.data, indent=1))
        (root / "bank.json").write_text(json.dumps([asdict(b) for b in self.bank], indent=0))
        self.root = root

    @classmethod
    def load(cls, guitar_id: str, base: Path = GUITARS) -> GuitarProfile:
        root = Path(base) / guitar_id
        if not (root / "profile.json").exists():
            raise FileNotFoundError(f"no profile {guitar_id!r} in {base}; record a session and build it first")
        data = json.loads((root / "profile.json").read_text())
        rows = json.loads((root / "bank.json").read_text()) if (root / "bank.json").exists() else []
        return cls(data, [BankNote(**r) for r in rows], root)


def build_profile(guitar_id: str, segments: dict[str, Path], bank_root: Path | None = None, bank: list[BankNote] | None = None,
                  use_basic_pitch: bool = True, session: str | None = None, per_pitch: int = 3, seed: int = 0) -> GuitarProfile:
    """segments: segment id -> wav. Unknown ids are treated as free playing."""
    script = {s["id"]: s for s in session_segments()}
    audio, notes, normal = {}, [], []
    bp_py = basic_pitch_available() if use_basic_pitch else None
    for sid, path in segments.items():
        x = read_mono(path)
        audio[sid] = x
        seg = script.get(sid) or {"id": sid, "group": "g", "title": sid, "text": "", "seconds": len(x) / SR, "meta": {"expect": "free"}}
        bp = basic_pitch_notes(path, bp_py) if bp_py and seg["meta"].get("expect") in ("free", "run") else None
        notes += segment_notes(x, seg, str(Path(path).resolve()), bp)
        if seg["meta"].get("expect") in ("free", "run") or seg["group"] == "g":
            normal.append(x)
    if not normal:                                   # no free/run segment: every non-silent segment but the dynamics extremes
        normal = [x for k, x in audio.items() if not k.startswith("dyn_soft") and not k.startswith("extra")]
    dyn = label_dynamics(notes)
    level = level_block(np.concatenate(normal), dyn)

    # tone: own onsets that are plain notes (not palm-muted or extras) against the matched bank notes
    plain = [n for n in notes if n.extra.get("technique") in ("normal", "open_string") and n.extra.get("steady", 1) >= 0.7]
    tone: dict = {"method": "unit-RMS 0.35 s note windows, mean power, matched pitch histogram, 11 half-octave bands",
                  "bands_hz": list(BANDS_HZ), "cap_db": EQ_CAP_DB}
    if bank is None and bank_root is not None:
        bank = D.index_bank(bank_root)
    if bank and plain:
        own_p, acc, n_own = np.zeros(NFFT // 2 + 1), [], 0
        by_file: dict[str, list[BankNote]] = {}
        for n in plain:
            by_file.setdefault(n.file, []).append(n)
        for f, ns in by_file.items():
            x = next(a for k, a in audio.items() if str(Path(segments[k]).resolve()) == f)
            p, k = ltas_power(x, [n.onset for n in ns])
            own_p += p * k
            n_own += k
        own_p /= max(n_own, 1)
        bank_p, n_bank = matched_bank_power(bank, [n.pitch for n in plain], per_pitch, seed)
        gains = fit_tone_eq(own_p, bank_p)
        tone.update({"gains_db": [round(float(g), 2) for g in gains], "own_notes_used": n_own, "bank_notes_used": n_bank,
                     "raw_band_diff_db": [round(float(v), 2) for v in (band_db(own_p) - band_db(bank_p))],
                     "bank": "Guitar-TECHS P1/P2 single notes and scales"})
    else:
        tone.update({"gains_db": [0.0] * len(BANDS_HZ), "own_notes_used": len(plain), "bank_notes_used": 0,
                     "note": "no tone fit (no bank or no plain notes)"})

    own_pitches = sorted({n.pitch for n in notes})
    bank_pitches = sorted({b.pitch for b in bank}) if bank else []
    cov = {"own_pitches": own_pitches, "own_note_count": len(notes),
           "bank_fallback_pitches": [p for p in bank_pitches if p not in own_pitches],
           "bank_pitch_range": [bank_pitches[0], bank_pitches[-1]] if bank_pitches else None,
           "by_dynamic": {d: sum(1 for n in notes if n.extra.get("dynamic") == d) for d in ("soft", "medium", "hard")},
           "by_technique": {t: sum(1 for n in notes if n.extra.get("technique") == t)
                            for t in sorted({n.extra.get("technique") for n in notes})},
           "pitch_source": "basic-pitch+onset" if bp_py else "YIN per onset (Basic Pitch venv not available)"}
    data = {"schema": SCHEMA, "guitar_id": guitar_id, "session": session, "segments": {k: str(Path(v).resolve()) for k, v in segments.items()},
            "level": level, "tone": tone, "coverage": cov}
    return GuitarProfile(data, notes)


# --------------------------------------------------------------------------- the rebuild with a profile

def velocity_to_level_db(vel: int, prof: GuitarProfile) -> float:
    """Target note level (RMS of its first 300 ms, dBFS). With soft and hard measured: linear in dB between them
    over velocity 30..110. Without: the T201 rule (20*log10(vel/127) below -30 dBFS)."""
    lv = prof.data["level"]
    lo, hi = lv.get("soft_note_dbfs"), lv.get("hard_note_dbfs")
    if lo is None or hi is None or hi <= lo:
        return -30.0 + 20 * np.log10(max(vel, 1) / 127.0)
    t = float(np.clip((vel - 30) / 80.0, 0, 1))
    return lo + (hi - lo) * t


def render_profiled(notes: list[MidiNote], bank: list[BankNote], prof: GuitarProfile, total_s: float | None = None,
                    fade_s: float = 0.03, player: str | None = None, peak_cap_db: float = -1.0,
                    rel_db: list[float] | None = None) -> tuple[np.ndarray, list[dict], dict]:
    """Like dry_rebuild.render, but: own notes first (the one whose level is closest to the velocity's target), else a
    bank note passed through the profile's correction EQ; note levels follow velocity -> the profile's soft..hard
    range; the sum is set to the profile's normal-playing loud-frame level (peak-capped, flagged).
    rel_db (T224, dry_rebuild.stem_envelope_db): per-note dB around the median velocity target replaces the velocity mapping."""
    h = prof.fir()
    player = player or D.choose_player(bank, [n.pitch for n in notes])
    own: dict[int, list[BankNote]] = {}
    for b in prof.bank:
        if b.extra.get("technique", "normal") in ("normal", "open_string"):
            own.setdefault(b.pitch, []).append(b)
    bk: dict[int, list[BankNote]] = {}
    for b in bank:
        bk.setdefault(b.pitch, []).append(b)
    n_out = int((total_s if total_s else max(n.offset for n in notes) + 1.0) * SR)
    y = np.zeros(n_out + SR, np.float32)
    raw: dict[str, np.ndarray] = {}
    filt: dict[str, np.ndarray] = {}
    rr: dict[tuple[str, int], int] = {}
    log = []
    med_target = float(np.median([velocity_to_level_db(n.velocity, prof) for n in notes])) if notes else 0.0
    for ni, n in enumerate(notes):
        dur = n.offset - n.onset
        target = velocity_to_level_db(n.velocity, prof) if rel_db is None else med_target + rel_db[ni]
        src, cands = "own", own.get(n.pitch, [])
        if not cands:
            src = "bank+eq"
            cands = [c for c in bk.get(n.pitch, []) if c.player == player] or bk.get(n.pitch, [])
        if not cands:
            log.append({"pitch": n.pitch, "onset": n.onset, "missing": True})
            continue
        if src == "own":
            cands = sorted(cands, key=lambda c: abs(c.level_db - target))[:3]
        long = [c for c in cands if c.avail >= dur + fade_s] or sorted(cands, key=lambda c: -c.avail)[:1]
        k = rr.get((src, n.pitch), 0)
        rr[(src, n.pitch)] = k + 1
        c = long[k % len(long)]
        cache = raw if src == "own" else filt
        if c.file not in cache:
            if len(cache) > 3:
                cache.clear()
            x = read_mono(c.file)
            cache[c.file] = x if src == "own" else apply_fir(x, h)
        x = cache[c.file]
        lvl = c.level_db if src == "own" else note_level_db(x, c.onset)
        pre = 0.003
        a = max(0, int((c.onset - pre) * SR))
        use = min(dur + fade_s * 0.5, c.avail)
        seg = x[a: a + int((use + pre) * SR)].copy()
        na = min(int(0.002 * SR), len(seg))
        seg[:na] *= np.linspace(0, 1, na, dtype=np.float32)
        nf = min(int(fade_s * SR), len(seg))
        seg[len(seg) - nf:] *= (0.5 * (1 + np.cos(np.linspace(0, np.pi, nf)))).astype(np.float32)
        gain_db = float(np.clip(target - lvl, -30, 30))
        i0 = int(round((n.onset - pre) * SR))
        i0c = max(0, i0)
        seg = seg[i0c - i0:]
        if i0c >= len(y):                      # note starts past the render length (total_s shorter than the MIDI)
            continue
        seg = seg[: len(y) - i0c]
        y[i0c: i0c + len(seg)] += seg * np.float32(10 ** (gain_db / 20))
        log.append({"pitch": n.pitch, "onset": round(n.onset, 3), "dur": round(dur, 3), "src": src, "target_db": round(target, 1),
                    "gain_db": round(gain_db, 1), "short_source": bool(c.avail < dur)})
    y = y[:n_out]
    want = prof.data["level"]["normal_loud_dbfs"]
    base = D.loud_level_db(y)
    g = want - base
    out = y * np.float32(10 ** (g / 20))
    capped = False
    if D.peak_db(out) > peak_cap_db:
        g -= D.peak_db(out) - peak_cap_db
        out = y * np.float32(10 ** (g / 20))
        capped = True
    summary = {"target_loud_dbfs": want, "loud_dbfs": round(D.loud_level_db(out), 2), "peak_dbfs": round(D.peak_db(out), 2),
               "capped": capped, "gain_db": round(float(g), 2),
               "own_notes": sum(1 for r in log if r.get("src") == "own"),
               "bank_eq_notes": sum(1 for r in log if r.get("src") == "bank+eq"),
               "missing": sum(1 for r in log if r.get("missing"))}
    return out.astype(np.float32), log, summary
