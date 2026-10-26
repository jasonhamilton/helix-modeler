"""dry_rebuild and midi_read on a tiny fake bank (sine 'notes' written to a temp folder; test data only)."""
from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from tonematch import dry_rebuild as D
from tonematch.midi_read import MidiNote, read_midi

SR = D.SR


def _vlq(v: int) -> bytes:
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    return bytes(reversed(out))


def write_midi(path, notes, ppq=480, track=0):
    """notes: (pitch, start_s, end_s, vel) at 120 bpm (1 s = 960 ticks)."""
    tk = 960
    ev = []
    for p, a, b, v in notes:
        ev += [(int(a * tk), 1, p, v), (int(b * tk), 0, p, 0)]
    ev.sort()
    body, t = b"", 0
    for tick, on, p, v in ev:
        body += _vlq(tick - t) + bytes([0x90, p, v])
        t = tick
    body += b"\x00\xff\x2f\x00"
    tempo = b"\x00\xff\x51\x03\x07\xa1\x20\x00\xff\x2f\x00"
    mt = lambda b: b"MTrk" + len(b).to_bytes(4, "big") + b
    path.write_bytes(b"MThd" + (6).to_bytes(4, "big") + b"\x00\x01\x00\x02" + ppq.to_bytes(2, "big") + mt(tempo) + mt(body))


def tone(f, dur, decay=1.5):
    t = np.arange(int(dur * SR)) / SR
    return (np.sin(2 * np.pi * f * t) * np.exp(-t * decay)).astype(np.float32)


@pytest.fixture()
def bank_root(tmp_path):
    for pl in ("P1", "P2", "P3"):
        d = tmp_path / f"{pl}_singlenotes"
        (d / "midi").mkdir(parents=True)
        (d / "audio" / "directinput").mkdir(parents=True)
        pitches, y, notes = [45, 52, 57], [], []
        for i, p in enumerate(pitches):
            y.append(np.zeros(int(0.1 * SR), np.float32))   # silence, then a note 2 s long
            y.append(tone(440 * 2 ** ((p - 69) / 12), 1.9))
            notes.append((p, i * 2.0 + 0.1, i * 2.0 + 2.0, 100))
        sf.write(str(d / "audio" / "directinput" / "directinput_allsinglenotes.wav"), np.concatenate(y), SR, subtype="FLOAT")
        write_midi(d / "midi" / "midi_allsinglenotes.mid", notes)
    return tmp_path


def test_midi_roundtrip(tmp_path):
    write_midi(tmp_path / "a.mid", [(60, 0.5, 1.0, 90), (64, 0.5, 1.5, 70)])
    n = read_midi(tmp_path / "a.mid")
    assert [(x.pitch, round(x.onset, 3), round(x.offset, 3), x.velocity) for x in n] == [(60, 0.5, 1.0, 90), (64, 0.5, 1.5, 70)]


def test_bank_skips_p3_and_refines_onset(bank_root):
    bank = D.index_bank(bank_root)
    assert {b.player for b in bank} == {"P1", "P2"} and len(bank) == 6
    assert all(abs(b.lag_ms) < 15 for b in bank)
    with pytest.raises(ValueError):
        D.index_bank(bank_root, players=("P3",))
    chk = D.check_bank_alignment(bank, 6)
    assert chk["pitch_ok"] == chk["n"]


def test_clean_drops_low_ghost_trims_retrigger_caps_poly():
    ns = [MidiNote(35, 1.0, 2.0, 60), MidiNote(47, 1.0, 2.0, 80),          # ghost under a played B2
          MidiNote(36, 5.0, 5.5, 60),                                      # nothing above: moved up an octave
          MidiNote(50, 6.0, 7.0, 60), MidiNote(50, 6.5, 7.0, 60),          # re-strike while ringing
          MidiNote(52, 8.0, 8.02, 80), MidiNote(53, 8.0, 9.0, 5)]          # too short, too quiet
    out, st = D.clean_transcription(ns)
    assert st["dropped_low_octave_ghost"] == 1 and st["transposed_low"] == 1 and st["retrigger_trimmed"] == 1
    assert st["dropped_short"] == 1 and st["dropped_quiet"] == 1
    assert sorted(n.pitch for n in out) == [47, 48, 50, 50]
    assert min(n.pitch for n in out) >= 40
    many = [MidiNote(40 + i, 0.0, 1.0, 50 + i) for i in range(9)]
    out, st = D.clean_transcription(many, max_poly=6)
    assert len(out) == 6 and st["dropped_poly"] == 3


def test_render_places_notes_and_fades(bank_root):
    bank = D.index_bank(bank_root)
    ns = [MidiNote(52, 1.0, 1.5, 127), MidiNote(57, 2.0, 2.3, 64)]
    y, log = D.render(ns, bank, total_s=3.0)
    assert y.dtype == np.float32 and len(y) == 3 * SR
    assert {x["player"] for x in log} == {"P1"} and not any(x.get("missing") for x in log)
    assert np.abs(y[: int(0.99 * SR)]).max() == 0
    assert np.abs(y[int(1.0 * SR): int(1.2 * SR)]).max() > 0
    assert np.abs(y[int(1.55 * SR): int(1.9 * SR)]).max() < 1e-6          # released, nothing leaks past the fade
    assert abs(float(y[int(1.5 * SR) + int(0.02 * SR)])) < abs(float(y[int(1.2 * SR)])) + 1                # no click: smooth
    assert np.abs(np.diff(y)).max() < 0.2 * np.abs(y).max()
    hi = np.abs(y[int(1.0 * SR): int(1.5 * SR)]).max()
    lo = np.abs(y[int(2.0 * SR): int(2.3 * SR)]).max()
    assert 0.4 < lo / hi < 0.6                                              # velocity 64 vs 127


def test_levels_and_cap():
    rng = np.random.default_rng(0)
    x = (rng.standard_normal(SR * 3) * 0.05).astype(np.float32)
    v = D.level_variants(x)
    assert abs(v["ref"]["loud_dbfs"] + 26.7) < 0.1 and abs(v["hot"]["loud_dbfs"] + 20.7) < 0.1
    assert not v["ref"]["capped"]
    x[1000] = 1.0                                       # a spike: +12 would clip, so it is capped at -1 dBFS
    v = D.level_variants(x)
    assert v["veryhot"]["capped"] and v["veryhot"]["peak_dbfs"] <= -0.99
    assert v["veryhot"]["loud_dbfs"] < v["veryhot"]["target_loud_dbfs"]


def test_lufs_scales_with_gain():
    x = tone(220, 3.0, 0.0) * 0.1
    assert abs((D.lufs(x * 2) - D.lufs(x)) - 6.02) < 0.05


# ---- T224: dynamics from the stem (rebuild_dry --envelope-from-stem)

def _stem_with_hits(hits, total=6.0):
    """Noise bursts (onset_s, amplitude) of 0.4 s on silence."""
    rng = np.random.default_rng(1)
    x = np.zeros(int(total * SR), np.float32)
    for t, a in hits:
        i = int(t * SR)
        x[i: i + int(0.4 * SR)] = (rng.standard_normal(int(0.4 * SR)) * a).astype(np.float32)
    return x


def test_stem_envelope_follows_stem_level_and_ignores_velocity():
    hits = [(0.5, 0.1), (1.5, 0.05), (2.5, 0.2), (3.5, 0.1)]            # 0, -6, +6, 0 dB around the median
    stem = _stem_with_hits(hits)
    ns = [MidiNote(52, t, t + 0.4, v) for (t, _), v in zip(hits, (127, 20, 20, 127))]     # velocities say the opposite
    rel = D.stem_envelope_db(ns, stem, smooth_s=0.05)
    assert len(rel) == 4
    assert rel[2] > rel[0] > rel[1]                                       # quiet note stays quiet whatever the velocity
    assert abs((rel[2] - rel[1]) - 12.0) < 0.5 and abs(rel[0] - rel[3]) < 0.1


def test_stem_envelope_chord_sums_to_the_moment_and_is_limited():
    stem = _stem_with_hits([(0.5, 0.1), (1.5, 0.1), (2.5, 0.1), (3.5, 0.0001)])
    ns = [MidiNote(40 + 7 * i, 0.5, 0.9, 80) for i in range(2)] + [MidiNote(52, 1.5, 1.9, 80), MidiNote(52, 2.5, 2.9, 80),
                                                                    MidiNote(52, 3.5, 3.9, 80)]
    rel = D.stem_envelope_db(ns, stem, smooth_s=0.05, max_dev_db=12.0)
    assert abs((rel[0] - rel[2]) + 3.01) < 0.1 and rel[0] == rel[1]       # two notes in a moment: each 3 dB down
    assert rel[4] >= -12.0 - 1e-9                                         # a silent moment is limited to -12 dB
    assert D.stem_envelope_db([], stem) == []


def test_stem_envelope_smoothing_averages_neighbours():
    hits = [(0.5, 0.1), (0.8, 0.4), (1.1, 0.1), (3.5, 0.1)]
    stem = _stem_with_hits(hits)
    ns = [MidiNote(52, t, t + 0.25, 80) for t, _ in hits]
    raw = D.stem_envelope_db(ns, stem, smooth_s=0.01)
    sm = D.stem_envelope_db(ns, stem, smooth_s=0.8)
    assert raw[1] - raw[0] > 10 and (sm[1] - sm[0]) < (raw[1] - raw[0]) / 2   # the spike is spread over its neighbours


def test_render_rel_db_replaces_velocity(bank_root):
    bank = D.index_bank(bank_root)
    ns = [MidiNote(52, 1.0, 1.5, 127), MidiNote(57, 2.0, 2.3, 127)]
    y0, _ = D.render(ns, bank, total_s=3.0)
    y1, _ = D.render(ns, bank, total_s=3.0, rel_db=[0.0, -6.0])
    hi = np.abs(y1[int(1.0 * SR): int(1.5 * SR)]).max()
    lo = np.abs(y1[int(2.0 * SR): int(2.3 * SR)]).max()
    assert 0.4 < lo / hi < 0.6                                               # -6 dB although both velocities are 127
    hi0 = np.abs(y0[int(1.0 * SR): int(1.5 * SR)]).max()
    assert abs(hi - hi0) / hi0 < 0.01                                        # rel 0 dB = the median velocity's level
