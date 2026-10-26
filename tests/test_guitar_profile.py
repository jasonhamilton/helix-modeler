"""guitar_profile on fake plucked-string audio (synthetic test data only)."""
from __future__ import annotations

import numpy as np
import soundfile as sf

from tonematch import dry_rebuild as D
from tonematch import guitar_profile as G
from tonematch.dry_rebuild import SR, BankNote
from tonematch.midi_read import MidiNote


def pluck(pitch, dur=1.5, amp=0.3, bright=1.0, seed=0):
    f = 440 * 2 ** ((pitch - 69) / 12)
    t = np.arange(int(dur * SR)) / SR
    y = sum(bright ** (k - 1) / k * np.sin(2 * np.pi * f * k * t) for k in range(1, 9) if f * k < 12000)
    y = y * np.exp(-t * 2.5)
    y[:int(0.002 * SR)] *= np.linspace(0, 1, int(0.002 * SR))
    nf = int(0.05 * SR)
    y[-nf:] *= np.linspace(1, 0, nf)
    return (amp * y / np.max(np.abs(y))).astype(np.float32)


def track(pitches, gap=1.6, amp=0.3, bright=1.0, lead=0.3):
    x = np.zeros(int((lead + gap * len(pitches) + 0.5) * SR), np.float32)
    ons = []
    for i, p in enumerate(pitches):
        a = int((lead + i * gap) * SR)
        n = pluck(p, gap, amp, bright)
        x[a:a + len(n)] += n
        ons.append(lead + i * gap)
    return x, ons


def test_segmentation_open_and_run():
    x, ons = track([40] * 3)
    seg = {"id": "open_s6", "group": "a", "meta": {"expect": "open", "string": 6}}
    notes = G.segment_notes(x, seg, "f.wav")
    assert len(notes) == 3 and all(n.pitch == 40 and n.string == 6 and n.fret == 0 for n in notes)
    assert all(abs(n.onset - o) < 0.03 for n, o in zip(notes, ons))
    x, _ = track(list(range(40, 53)), gap=0.5)
    seg = {"id": "run_s6", "group": "b", "meta": {"expect": "run", "string": 6}}
    notes = G.segment_notes(x, seg, "f.wav")
    assert [n.fret for n in notes] == list(range(13))


def test_chord_skipped_and_dynamics_labels():
    x, _ = track([45], amp=0.3)
    assert G.segment_notes(x, {"id": "chord_A", "group": "c", "meta": {"expect": "chord"}}, "f") == []
    mk = lambda lv, d: BankNote("f", "OWN", "x", 50, 0, 0, 1, None, None, lv, 0, {"dynamic": d, "technique": "normal"})
    ns = [mk(-40, "soft"), mk(-30, "medium"), mk(-20, "hard"), mk(-22, None), mk(-38, None)]
    lv = G.label_dynamics(ns)
    assert lv["soft"] == -40 and ns[3].extra["dynamic"] == "hard" and ns[4].extra["dynamic"] == "soft"


def test_level_block():
    x = np.random.default_rng(0).standard_normal(SR * 3).astype(np.float32) * 0.05
    lv = G.level_block(x, {"soft": -40.0, "hard": -22.0})
    assert abs(lv["normal_loud_dbfs"] - D.loud_level_db(x)) < 0.01 and lv["dynamic_range_db"] == 18.0
    assert lv["crest_db"] > 5


def test_min_phase_fir_matches_curve():
    g = np.array([0, 0, 2, 4, 6, 3, 0, -3, -6, -8, -10], float)
    h = G.min_phase_fir(g)
    H = 20 * np.log10(np.abs(np.fft.rfft(h, G.NFFT)) + 1e-12)
    f = np.fft.rfftfreq(G.NFFT, 1 / SR)
    want = G.eq_curve_db(g, f)
    sel = (f > 100) & (f < 9000)
    assert np.max(np.abs(H[sel] - want[sel])) < 1.0
    assert np.argmax(np.abs(h)) < 200          # minimum phase: energy at the front


def test_tone_eq_recovers_known_tilt(tmp_path):
    pitches = [45, 52, 57, 64, 69, 76]
    bx, bons = track(pitches * 2, bright=1.0)
    own_target = np.array([0, 0, 0, 0, 0, 0, 2, 4, 6, 8, 10.0])      # a brighter guitar
    h = G.min_phase_fir(own_target)
    ox = G.apply_fir(bx, h)
    own_p, _ = G.ltas_power(ox, bons)
    bank_p, _ = G.ltas_power(bx, bons)
    g = G.fit_tone_eq(own_p, bank_p)
    want = own_target - own_target[[i for i, c in enumerate(G.BANDS_HZ) if 125 <= c <= 5000]].mean()
    # compare where the synthetic note has energy (fundamentals/harmonics up to 12 kHz are sparse): bands 125-3150
    for i, c in enumerate(G.BANDS_HZ):
        if 200 <= c <= 3150:
            assert abs(g[i] - want[i]) < 4.0, (c, g[i], want[i])
    assert np.all(np.abs(g) <= G.EQ_CAP_DB)


def test_build_and_profiled_rebuild(tmp_path):
    # fake bank: a note per pitch, dull; the "guitar": brighter, louder, with own notes only for some pitches
    pitches = list(range(45, 70, 2))
    bx, bons = track(pitches, bright=0.6)
    bwav = tmp_path / "bank.wav"
    sf.write(bwav, bx, SR)
    bank = [BankNote(str(bwav), "P1", "singlenotes", p, o, o, 1.4, None, None, G.note_level_db(bx, o), 0.0)
            for p, o in zip(pitches, bons)]
    ox, _ = track([45, 47, 49, 51] * 4, amp=0.2, bright=1.0)
    free = tmp_path / "free.wav"
    sf.write(free, ox, SR)
    prof = G.build_profile("fake", {"free": free}, bank=bank, use_basic_pitch=False)
    assert prof.data["tone"]["bank_notes_used"] > 0 and len(prof.bank) >= 12
    assert prof.data["coverage"]["own_pitches"] == [45, 47, 49, 51]
    assert 53 in prof.data["coverage"]["bank_fallback_pitches"]
    prof.save(tmp_path / "fake")
    back = G.GuitarProfile.load("fake", tmp_path)
    assert len(back.bank) == len(prof.bank) and back.data["level"] == prof.data["level"]

    notes = [MidiNote(p, 0.3 + 0.6 * i, 0.3 + 0.6 * i + 0.5, 90, 0, 0) for i, p in enumerate([45, 49, 53, 57, 61])]
    y, log, summ = G.render_profiled(notes, bank, back, total_s=4.0)
    assert summ["own_notes"] == 2 and summ["bank_eq_notes"] == 3 and summ["missing"] == 0
    assert abs(summ["loud_dbfs"] - back.data["level"]["normal_loud_dbfs"]) < 0.1 or summ["capped"]
    assert D.peak_db(y) <= -0.99
    # notes past total_s are skipped, not a crash (T211: full-song MIDI rendered with a shorter total_s)
    late = notes + [MidiNote(49, 7.0, 7.5, 90, 0, 0)]
    y2, _, _ = G.render_profiled(late, bank, back, total_s=4.0)
    assert len(y2) == len(y)
    # velocity maps to level: with soft/hard unknown, louder velocity -> louder target
    assert G.velocity_to_level_db(120, back) > G.velocity_to_level_db(40, back)
