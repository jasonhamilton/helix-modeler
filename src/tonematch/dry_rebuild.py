"""Rebuild a dry guitar DI from a MIDI transcription by concatenating REAL Guitar-TECHS DI notes (T201).

No synth. The note bank comes from the P1/P2 single-note and scale takes (isolated notes, each with a
MIDI file giving pitch/onset/offset; the track index is the string). P3 (the held-out player) is refused.
Local only: the result is for listening, and the song audio and transcription never leave the machine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from tonematch.midi_read import MidiNote, read_midi

SR = 48_000
OPEN_MIDI = {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40}      # track index -> open-string pitch (standard tuning)
REF_LOUD_DBFS = -26.7                                        # loud-frame level of the older renders (T194)
ALLOWED_PLAYERS = ("P1", "P2")
PLAIN_KINDS = ("singlenotes", "scales")


# --------------------------------------------------------------------------- levels

def loud_level_db(x: np.ndarray, win: int = 2400) -> float:
    """RMS of 50 ms frames within 30 dB of the loudest, in dBFS (same definition as helix_trust_set)."""
    n = len(x) // win
    if n == 0:
        return -120.0
    fr = np.sqrt(np.mean(x[: n * win].reshape(n, win).astype(np.float64) ** 2, axis=1)) + 1e-12
    db = 20 * np.log10(fr)
    keep = db > db.max() - 30
    return float(20 * np.log10(np.sqrt(np.mean(fr[keep] ** 2))))


def peak_db(x: np.ndarray) -> float:
    return float(20 * np.log10(max(float(np.max(np.abs(x))) if len(x) else 0.0, 1e-9)))


def lufs(x: np.ndarray, sr: int = SR) -> float:
    """Integrated loudness (ITU-R BS.1770 K-weighting, absolute gate -70 and relative gate -10 LU) of mono/stereo."""
    from scipy.signal import lfilter
    # K-weighting biquads, designed for 48 kHz
    assert sr == 48_000
    b1 = [1.53512485958697, -2.69169618940638, 1.19839281085285]
    a1 = [1.0, -1.69065929318241, 0.73248077421585]
    b2, a2 = [1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621]
    x = np.atleast_2d(np.asarray(x, dtype=np.float64).T if np.ndim(x) > 1 else np.asarray(x, dtype=np.float64))
    y = np.stack([lfilter(b2, a2, lfilter(b1, a1, c)) for c in x])
    blk, hop = int(0.4 * sr), int(0.1 * sr)
    if y.shape[1] < blk:
        return -120.0
    z = np.array([np.sum(np.mean(y[:, s:s + blk] ** 2, axis=1)) for s in range(0, y.shape[1] - blk + 1, hop)])
    l_blk = -0.691 + 10 * np.log10(z + 1e-12)
    g = z[l_blk > -70]
    if len(g) == 0:
        return -120.0
    rel = -0.691 + 10 * np.log10(g.mean()) - 10
    g = z[l_blk > max(-70, rel)]
    return float(-0.691 + 10 * np.log10(g.mean() + 1e-12)) if len(g) else -120.0


def level_variants(x: np.ndarray, ref_db: float = REF_LOUD_DBFS, steps=(("ref", 0.0), ("hot", 6.0), ("veryhot", 12.0)),
                   peak_cap_db: float = -1.0) -> dict[str, dict]:
    """Same signal at ref, +6 and +12 dB loud-frame level; a variant that would peak above peak_cap_db is
    turned down to the cap (flagged 'capped', with the gain it actually got)."""
    base = loud_level_db(x)
    out = {}
    for name, add in steps:
        want = ref_db + add
        g = want - base
        y = x * np.float32(10 ** (g / 20))
        capped = False
        pk = peak_db(y)
        if pk > peak_cap_db:
            g -= pk - peak_cap_db
            y = x * np.float32(10 ** (g / 20))
            capped = True
        out[name] = {"audio": y.astype(np.float32), "target_loud_dbfs": want, "loud_dbfs": loud_level_db(y),
                     "peak_dbfs": peak_db(y), "capped": capped, "gain_db": float(g)}
    return out


# --------------------------------------------------------------------------- the note bank

@dataclass
class BankNote:
    file: str                 # path of the DI wav
    player: str               # P1 | P2
    kind: str                 # singlenotes | scales
    pitch: int
    onset: float              # seconds in the file (audio onset, refined from the MIDI onset)
    midi_onset: float
    avail: float              # seconds of clean audio before the next note starts in that file
    string: int | None = None  # 1 = high e ... 6 = low E (MIDI track index)
    fret: int | None = None
    level_db: float = -120.0   # loudness of the first 300 ms (RMS dBFS), for per-note normalisation
    lag_ms: float = 0.0        # audio onset minus MIDI onset
    extra: dict = field(default_factory=dict)


def _env(x: np.ndarray, hop: int = 240) -> np.ndarray:
    n = len(x) // hop
    return np.sqrt(np.mean(x[: n * hop].reshape(n, hop).astype(np.float64) ** 2, axis=1))


def refine_onset(x: np.ndarray, t_midi: float, sr: int = SR, back: float = 0.10, fwd: float = 0.10) -> float:
    """Audio onset near a MIDI onset: the 5 ms frame with the steepest rise of the dB envelope in [-back, +fwd]
    (a ringing previous note does not fool it the way a threshold would); returned at the start of that rise."""
    hop = int(0.005 * sr)
    a, b = max(0, int((t_midi - back) * sr)), min(len(x), int((t_midi + fwd) * sr) + hop * 4)
    e = 20 * np.log10(_env(x[a:b], hop) + 1e-6)
    if len(e) < 4:
        return t_midi
    d = np.diff(e)
    k = int(np.argmax(d))
    return (a + k * hop) / sr


def index_bank(root: str | Path, players=ALLOWED_PLAYERS, kinds=PLAIN_KINDS, refine: bool = True) -> list[BankNote]:
    """Every DI note in `<root>/<player>_<kind>/` (audio/directinput/*.wav + midi/*.mid, matched by name)."""
    root = Path(root)
    bank: list[BankNote] = []
    for pl in players:
        if pl not in ALLOWED_PLAYERS:
            raise ValueError(f"{pl} is not allowed in the bank (P3 is the reserved held-out player)")
        for kind in kinds:
            d = root / f"{pl}_{kind}"
            for mid in sorted((d / "midi").glob("*.mid")):
                wav = d / "audio" / "directinput" / mid.name.replace("midi_", "directinput_").replace(".mid", ".wav")
                if not wav.exists():
                    continue
                bank.extend(_index_file(wav, mid, pl, kind, refine))
    return bank


def _index_file(wav: Path, mid: Path, player: str, kind: str, refine: bool) -> list[BankNote]:
    x, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != SR:
        from math import gcd

        from scipy.signal import resample_poly
        g = gcd(SR, sr)
        x = resample_poly(x, SR // g, sr // g).astype(np.float32)
    notes = [n for n in read_midi(mid) if n.onset < len(x) / SR]
    onsets = sorted(n.onset for n in notes)
    out = []
    for n in notes:
        on = refine_onset(x, n.onset) if refine else n.onset
        nxt = [t for t in onsets if t > n.onset + 0.02]
        end = min(nxt[0] if nxt else len(x) / SR, n.offset + 0.5, len(x) / SR)
        string = n.track if n.track in OPEN_MIDI else None
        fret = n.pitch - OPEN_MIDI[string] if string else None
        if fret is not None and not 0 <= fret <= 24:
            string, fret = None, None
        a = int(on * SR)
        seg = x[a:a + int(0.3 * SR)]
        lvl = 20 * np.log10(np.sqrt(np.mean(seg.astype(np.float64) ** 2)) + 1e-9) if len(seg) else -120.0
        out.append(BankNote(str(wav), player, kind, n.pitch, on, n.onset, max(0.0, end - on - 0.01), string, fret,
                            float(lvl), (on - n.onset) * 1000.0))
    return out


def yin_pitch(seg: np.ndarray, sr: int = SR, fmin: float = 70.0, fmax: float = 1500.0) -> float:
    import librosa
    f0 = librosa.yin(seg.astype(np.float32), fmin=fmin, fmax=fmax, sr=sr, frame_length=4096)
    return float(np.median(f0))


def check_bank_alignment(bank: list[BankNote], n: int = 12, seed: int = 0) -> dict:
    """Pitch estimate (YIN, 100-400 ms after the audio onset) against MIDI for a sample of notes, plus the
    audio-onset minus MIDI-onset lag for all notes."""
    rng = np.random.default_rng(seed)
    pick = [bank[i] for i in rng.choice(len(bank), size=min(n, len(bank)), replace=False)]
    cache: dict[str, np.ndarray] = {}
    rows = []
    for b in pick:
        if b.file not in cache:
            cache.clear()
            cache[b.file] = sf.read(b.file, dtype="float32", always_2d=True)[0].mean(axis=1)
        x = cache[b.file]
        seg = x[int((b.onset + 0.1) * SR): int((b.onset + 0.4) * SR)]
        f = yin_pitch(seg)
        est = 69 + 12 * np.log2(f / 440.0)
        rows.append({"file": Path(b.file).name, "player": b.player, "midi_pitch": b.pitch, "yin_pitch": round(float(est), 2),
                     "cents_off": round(float((est - b.pitch) * 100), 0), "lag_ms": round(b.lag_ms, 1)})
    lags = np.array([b.lag_ms for b in bank])
    return {"rows": rows, "pitch_ok": int(sum(abs(r["cents_off"]) < 80 for r in rows)), "n": len(rows),
            "lag_ms_median": float(np.median(lags)), "lag_ms_p5_p95": [float(np.percentile(lags, 5)), float(np.percentile(lags, 95))]}


# --------------------------------------------------------------------------- cleaning the transcription

def clean_transcription(notes: list[MidiNote], low: int = 40, min_dur: float = 0.04, min_vel: int = 20,
                        max_poly: int = 6, octave_tol: int = 0) -> tuple[list[MidiNote], dict]:
    """Below `low` (E2, the lowest guitar string): if the note an octave up (or the same pitch class an octave up)
    is already sounding, drop it (a sub-octave ghost of a note we do play); else transpose it up an octave
    until it is playable. Then drop notes shorter than min_dur or quieter than min_vel; trim a note
    that is re-struck on the same pitch while it still rings (one string cannot do both); cap polyphony."""
    st = {"input": len(notes), "dropped_low_octave_ghost": 0, "transposed_low": 0, "dropped_short": 0,
          "dropped_quiet": 0, "retrigger_trimmed": 0, "dropped_poly": 0}
    work: list[MidiNote] = []
    for n in notes:
        if n.pitch < low:
            up = n.pitch
            while up < low:
                up += 12
            if any(m is not n and m.pitch % 12 == up % 12 and m.pitch >= low and m.onset < n.offset and m.offset > n.onset
                   for m in notes):
                st["dropped_low_octave_ghost"] += 1
                continue
            n = MidiNote(up, n.onset, n.offset, n.velocity, n.channel, n.track)
            st["transposed_low"] += 1
        work.append(n)
    work.sort(key=lambda n: (n.onset, n.pitch))
    keep = []
    for n in work:
        if n.offset - n.onset < min_dur:
            st["dropped_short"] += 1
        elif n.velocity < min_vel:
            st["dropped_quiet"] += 1
        else:
            keep.append(n)
    last: dict[int, int] = {}
    for i, n in enumerate(keep):
        j = last.get(n.pitch)
        if j is not None and keep[j].offset > n.onset:
            p = keep[j]
            keep[j] = MidiNote(p.pitch, p.onset, max(n.onset, p.onset + 0.02), p.velocity, p.channel, p.track)
            st["retrigger_trimmed"] += 1
        last[n.pitch] = i
    # polyphony cap: when more than max_poly notes ring, drop the quietest of the newcomers
    out: list[MidiNote] = []
    for n in keep:
        active = [m for m in out if m.offset > n.onset]
        if len(active) >= max_poly:
            low_v = min(active + [n], key=lambda m: m.velocity)
            if low_v is n:
                st["dropped_poly"] += 1
                continue
            out.remove(low_v)
            st["dropped_poly"] += 1
        out.append(n)
    out.sort(key=lambda n: (n.onset, n.pitch))
    st["output"] = len(out)
    return out, st


def moment_counts(notes: list[MidiNote], group_s: float = 0.05) -> dict:
    """Per attack event (onsets within group_s merged): how many were single notes and how many chords/dyads."""
    ons = sorted(n.onset for n in notes)
    ev, cur = [], [ons[0]] if ons else []
    for t in ons[1:]:
        if t - cur[-1] <= group_s:
            cur.append(t)
        else:
            ev.append(len(cur))
            cur = [t]
    if cur:
        ev.append(len(cur))
    ev = np.array(ev)
    # time-weighted: how long 0, 1, 2, 3+ notes ring
    pts = sorted([(n.onset, 1) for n in notes] + [(n.offset, -1) for n in notes])
    dur = {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0}
    c, tprev = 0, pts[0][0] if pts else 0.0
    for t, d in pts:
        dur[min(c, 3)] += t - tprev
        c, tprev = c + d, t
    return {"events": int(len(ev)), "single": int((ev == 1).sum()), "dyad": int((ev == 2).sum()),
            "chord3plus": int((ev >= 3).sum()), "ring_time_s": {str(k): round(v, 1) for k, v in dur.items()}}


# --------------------------------------------------------------------------- rendering

def stem_envelope_db(notes: list[MidiNote], stem: np.ndarray, sr: int = SR, window_s: float = 0.30, smooth_s: float = 0.50,
                     group_s: float = 0.05, max_dev_db: float = 12.0) -> list[float]:
    """T224: per-note dynamics taken from the separated stem instead of Basic Pitch velocity. Returns one relative level
    in dB per note (same order as `notes`; 0 dB = the song's median moment).

    Notes whose onsets are within group_s form one attack moment. Each moment's level is the RMS (dB) of the stem over
    the first window_s after the onset (shortened to the next moment's onset, at least 30 ms). Moment levels are smoothed
    with a moving average in dB over +-smooth_s/2 seconds, referenced to their median and limited to +-max_dev_db. Every
    note then carries its moment's level minus 10*log10(notes in the moment), so a chord sums to the stem's level rather
    than n times it. Pitch, timing and note count come from the transcription; only the level comes from the stem."""
    if not notes:
        return []
    order = sorted(range(len(notes)), key=lambda i: notes[i].onset)
    moments: list[list[int]] = []
    for i in order:
        if moments and notes[i].onset - notes[moments[-1][-1]].onset <= group_s:
            moments[-1].append(i)
        else:
            moments.append([i])
    t0 = np.array([min(notes[i].onset for i in m) for m in moments])
    lev = np.empty(len(moments))
    for k, m in enumerate(moments):
        nxt = t0[k + 1] if k + 1 < len(moments) else t0[k] + window_s
        w = float(np.clip(nxt - t0[k], 0.03, window_s))
        a = max(0, int((t0[k] - 0.005) * sr))
        seg = stem[a: a + int(w * sr)].astype(np.float64)
        lev[k] = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-9) if len(seg) else -180.0
    sm = np.array([lev[np.abs(t0 - t) <= smooth_s / 2].mean() for t in t0])
    rel = np.clip(sm - np.median(sm), -max_dev_db, max_dev_db)
    out = [0.0] * len(notes)
    for k, m in enumerate(moments):
        for i in m:
            out[i] = float(rel[k] - 10 * np.log10(len(m)))
    return out


def choose_player(bank: list[BankNote], pitches: list[int]) -> str:
    """The one player covering most of the needed pitches (ties: P1)."""
    cov = {pl: sum(any(b.pitch == p and b.player == pl for b in bank) for p in set(pitches))
           for pl in sorted({b.player for b in bank})}
    return max(cov, key=lambda k: (cov[k], k == "P1"))


def render(notes: list[MidiNote], bank: list[BankNote], total_s: float | None = None, fade_s: float = 0.03,
           player: str | None = None, ref_level_db: float = -30.0, seed: int = 0,
           prefer_position: str | None = None, rel_db: list[float] | None = None) -> tuple[np.ndarray, list[dict]]:
    """Mono float32 at 48 kHz. Each note: a same-pitch bank note (player preference, a source long enough,
    round-robin over the candidates), cut to the transcribed duration with a cosine release, scaled by
    velocity/127 after per-note level normalisation. The sum is returned un-normalised (peak-safe scaling is
    the caller's job). rel_db (T224, stem_envelope_db): per-note dB around the median velocity replaces velocity."""
    if not notes:
        return np.zeros(int((total_s or 1) * SR), np.float32), []
    player = player or choose_player(bank, [n.pitch for n in notes])
    by_pitch: dict[int, list[BankNote]] = {}
    for b in bank:
        by_pitch.setdefault(b.pitch, []).append(b)
    rr: dict[int, int] = {}
    cache: dict[str, np.ndarray] = {}
    n_out = int(((total_s if total_s else max(n.offset for n in notes) + 1.0)) * SR)
    y = np.zeros(n_out + SR, np.float32)
    log = []
    med_vel = float(np.median([n.velocity for n in notes]))
    for ni, n in enumerate(notes):
        cands = by_pitch.get(n.pitch, [])
        if not cands:                                  # nearest pitch, shifted by resampling is NOT used: skip
            near = sorted(by_pitch, key=lambda p: abs(p - n.pitch))
            log.append({"pitch": n.pitch, "onset": n.onset, "missing": True})
            continue
        dur = n.offset - n.onset
        pref = [c for c in cands if c.player == player] or cands
        long_enough = [c for c in pref if c.avail >= dur + fade_s] or sorted(pref, key=lambda c: -c.avail)[:1]
        if prefer_position == "low":                   # lowest fret (open strings first) that has a long-enough source
            frets = sorted({c.fret if c.fret is not None else 99 for c in long_enough})
            long_enough = [c for c in long_enough if (c.fret if c.fret is not None else 99) == frets[0]]
        k = rr.get(n.pitch, 0)
        rr[n.pitch] = k + 1
        c = long_enough[k % len(long_enough)]
        if c.file not in cache:
            cache.clear()
            cache[c.file] = sf.read(c.file, dtype="float32", always_2d=True)[0].mean(axis=1)
        x = cache[c.file]
        pre = 0.003
        a = max(0, int((c.onset - pre) * SR))
        use = min(dur + fade_s * 0.5, c.avail)
        seg = x[a: a + int((use + pre) * SR)].copy()
        # attack de-click, then release over the last fade_s
        na = min(int(0.002 * SR), len(seg))
        seg[:na] *= np.linspace(0, 1, na, dtype=np.float32)
        nf = min(int(fade_s * SR), len(seg))
        seg[len(seg) - nf:] *= (0.5 * (1 + np.cos(np.linspace(0, np.pi, nf)))).astype(np.float32)
        gain = 10 ** ((ref_level_db - c.level_db) / 20) * ((n.velocity / 127.0) if rel_db is None else (med_vel / 127.0) * 10 ** (rel_db[ni] / 20))
        i0 = int(round((n.onset - pre) * SR))
        i0c = max(0, i0)
        seg = seg[i0c - i0:]
        y[i0c: i0c + len(seg)] += seg[: len(y) - i0c] * np.float32(gain)
        log.append({"pitch": n.pitch, "onset": round(n.onset, 3), "dur": round(dur, 3), "player": c.player,
                    "string": c.string, "fret": c.fret, "src": Path(c.file).name, "src_onset": round(c.onset, 3),
                    "avail": round(c.avail, 2), "short_source": bool(c.avail < dur)})
    return y[:n_out], log
