"""T205 chord-list rebuild: names -> voicings, Viterbi/strike decoding on synthetic posteriors (no Basic Pitch call)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from tonematch import chord_rebuild as CR

ROOT = Path(__file__).resolve().parents[1]
FPS = 86.1328125


def test_six_chord_voicings_match_the_prototype():
    got = CR.parse_chords("B5,G5,A5,F#5,D5,C#5")
    assert got == {"B5": [47, 54], "G5": [43, 50, 55], "A5": [45, 52], "F#5": [42, 49], "D5": [50, 57], "C#5": [49, 56]}


def test_maj_min_flats_and_errors():
    assert CR.voicing("G") == [43, 47, 50]                 # the prototype's G major
    assert CR.voicing("Em") == [40, 43, 47]
    assert CR.voicing("Bb5") == CR.voicing("A#5") == [46, 53]
    assert CR.parse_chords(["A5", "A5", " E5 "]) == {"A5": [45, 52], "E5": [40, 47]}
    for bad in ("H5", "B7", "", "Bsus4", "b5x"):
        with pytest.raises(ValueError):
            CR.voicing(bad)
    with pytest.raises(ValueError):
        CR.parse_chords(" , ")


def _posteriors(plan, T, chords):
    """plan: [(chord or None, t0_s, t1_s, [onset times])] -> note, onset matrices."""
    note = np.zeros((T, 88), np.float32)
    onset = np.zeros((T, 88), np.float32)
    for c, a, b, ons in plan:
        if c is None:
            continue
        for p in chords[c]:
            note[int(a * FPS): int(b * FPS), p - 21] = 0.8
        for o in ons:
            for p in chords[c]:
                onset[int(o * FPS), p - 21] = 0.9
    return note, onset


def test_decode_segments_silence_and_strikes():
    ch = CR.parse_chords("B5,G5,A5")
    plan = [("B5", 0.5, 2.0, [0.5, 1.3]), (None, 2.0, 2.5, []), ("G5", 2.5, 4.0, [2.5]), ("A5", 4.0, 5.0, [4.0, 4.2])]
    note, onset = _posteriors(plan, int(5.5 * FPS), ch)
    notes, tl = CR.decode(note, onset, FPS, ch)
    assert [t[0] for t in tl] == ["B5", "G5", "A5"]
    assert abs(tl[0][1] - 0.5) < 0.05 and abs(tl[0][2] - 2.0) < 0.05
    assert [t[3] for t in tl] == [2, 1, 1]                  # B5 re-strike at 1.3; A5 onset 0.2 s after the change is too close
    assert len(notes) == 2 * 2 + 3 * 1 + 2 * 1              # strikes x chord pitches
    assert all(n.velocity == 100 for n in notes)
    b5 = sorted(n.onset for n in notes if n.pitch == 47)
    assert abs(b5[1] - 1.3) < 0.03 and abs(notes[0].offset - b5[1]) < 1e-9   # first strike lasts until the re-strike


def test_weak_onset_and_short_blip_ignored_and_single_chord_works():
    ch = CR.parse_chords("B5")
    note, onset = _posteriors([("B5", 0.2, 3.0, [])], int(3.5 * FPS), ch)
    onset[int(1.0 * FPS), 47 - 21] = 0.4                    # below the 0.6 strike height
    note[int(3.2 * FPS): int(3.2 * FPS) + 3, 47 - 21] = 0.9  # 35 ms blip: shorter than the minimum segment
    notes, tl = CR.decode(note, onset, FPS, ch)
    assert len(tl) == 1 and tl[0][3] == 1


def test_all_silence_gives_no_notes():
    ch = CR.parse_chords("B5,G5")
    notes, tl = CR.decode(np.zeros((300, 88), np.float32), np.zeros((300, 88), np.float32), FPS, ch)
    assert notes == [] and tl == []


def test_rebuild_chords_end_to_end_with_fake_bank(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("rebuild_dry", ROOT / "scripts" / "rebuild_dry.py")
    RD = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(RD)
    ch = CR.parse_chords("B5,G5")
    note, onset = _posteriors([("B5", 0.5, 2.0, [0.5]), ("G5", 2.0, 3.5, [2.0])], int(4 * FPS), ch)
    seen = {}

    def fake_rebuild_song(bank, midi, stem, out, **kw):
        seen.update(kw)
        out.mkdir(parents=True, exist_ok=True)
        return {"ok": True, **kw["extra"]}

    monkeypatch.setattr(RD, "rebuild_song", fake_rebuild_song)
    rep = RD.rebuild_chords([], "B5,G5", "mother", tmp_path / "o", 4.0, tmp_path / "stem.wav",
                            posteriors={"note": note, "onset": onset, "fps": FPS})
    assert seen["prefer_position"] == "low" and len(seen["notes_override"]) == 5
    assert rep["chord_rebuild"]["segments"] == 2 and rep["chord_rebuild"]["strikes"] == 2
    import json
    j = json.loads((tmp_path / "o" / "chords.json").read_text())
    assert j["switch_penalty"] == 0.6 and [s[0] for s in j["segments"]] == ["B5", "G5"]


def _rd():
    spec = importlib.util.spec_from_file_location("rebuild_dry", ROOT / "scripts" / "rebuild_dry.py")
    RD = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(RD)
    return RD


def test_profile_flag_reaches_the_profile_rebuild(monkeypatch, tmp_path):
    RD = _rd()
    prof = RD.GP.GuitarProfile({"guitar_id": "g1", "level": {}, "tone": {"gains_db": [0.0] * 8}}, ["own"], tmp_path)
    loaded = []
    monkeypatch.setattr(RD.GP.GuitarProfile, "load", classmethod(lambda cls, gid, *a: loaded.append(gid) or prof))
    monkeypatch.setattr(RD.D, "index_bank", lambda root: [])
    monkeypatch.setattr(RD.D, "check_bank_alignment", lambda bank, n: {})
    calls = []
    monkeypatch.setattr(RD, "rebuild_song", lambda *a, **kw: calls.append(kw) or {"alignment_vs_stem_full": {"rebuilt": {"score": 0}}})
    mid = tmp_path / "x.mid"
    mid.write_bytes(b"")
    base = ["--midi", str(mid), "--out", str(tmp_path / "o")]
    assert RD.main(["--profile", "g1", "--profile-tone-only", *base]) == 0
    assert loaded == ["g1"] and calls[0]["profile"] is prof and calls[0]["profile_tone_only"] is True
    calls.clear()
    assert RD.main(base) == 0                                      # default: no profile
    assert calls[0]["profile"] is None and calls[0]["profile_tone_only"] is False
    with pytest.raises(SystemExit):
        RD.main(["--profile-tone-only", *base])


def test_rebuild_song_uses_render_profiled_and_tone_only_drops_own_notes(monkeypatch, tmp_path):
    import soundfile as sf
    RD = _rd()
    seen = {}

    def fake(notes, bank, prof, total_s=None, player=None):
        seen["own"] = len(prof.bank)
        return np.zeros(int(total_s * 48000), np.float32) + 0.001, [], {"loud_dbfs": -28.0, "capped": False}

    monkeypatch.setattr(RD.GP, "render_profiled", fake)
    monkeypatch.setattr(RD, "_align_mod", lambda: type("A", (), {
        "onset_env": staticmethod(lambda x: np.zeros(10)),
        "align": staticmethod(lambda a, b: {k: 0 for k in ("score", "median_corr", "lag_iqr_ms", "global_lag_s", "global_corr")} | {"windows": []})}))
    stem = tmp_path / "s.wav"
    sf.write(stem, np.zeros(48000 * 2, np.float32), 48000)
    prof = RD.GP.GuitarProfile({"guitar_id": "g1"}, ["a", "b"], tmp_path)
    notes = [RD.D.MidiNote(50, 0.1, 0.5, 100)] if hasattr(RD.D, "MidiNote") else [MidiNoteT(50, 0.1, 0.5, 100)]
    for tone_only, n_own, fname in ((False, 2, "rebuilt_dry_profiled.wav"), (True, 0, "rebuilt_dry_profiled_toneonly.wav")):
        out = tmp_path / str(tone_only)
        rep = RD.rebuild_song([], Path("x"), stem, out, seconds=2.0, notes_override=notes, profile=prof, profile_tone_only=tone_only)
        assert seen["own"] == n_own and (out / fname).exists() and rep["profile"]["tone_only"] is tone_only


from tonematch.midi_read import MidiNote as MidiNoteT  # noqa: E402


def _fake_rd(monkeypatch):
    RD = _rd()
    seen = {}

    def fake(notes, bank, total_s=None, player=None, prefer_position=None):
        seen["total_s"] = total_s
        return np.zeros(int(total_s * 48000), np.float32) + 0.001, []

    monkeypatch.setattr(RD.D, "render", fake)
    monkeypatch.setattr(RD, "_align_mod", lambda: type("A", (), {
        "onset_env": staticmethod(lambda x: np.zeros(10)),
        "align": staticmethod(lambda a, b: {k: 0 for k in ("score", "median_corr", "lag_iqr_ms", "global_lag_s", "global_corr")} | {"windows": []})}))
    return RD, seen


def test_default_length_is_the_full_stem_not_68_s(monkeypatch, tmp_path, capsys):
    import soundfile as sf
    RD, seen = _fake_rd(monkeypatch)
    stem = tmp_path / "s.wav"
    sf.write(stem, np.zeros(48000 * 100, np.float32), 48000)
    notes = [MidiNoteT(50, 0.1, 0.5, 100)]
    rep = RD.rebuild_song([], Path("x"), stem, tmp_path / "a", notes_override=notes)
    assert seen["total_s"] == 100.0 and rep["seconds"] == 100.0 and rep["seconds_source"] == "stem"
    assert "rebuild length: 100.00 s" in capsys.readouterr().out
    rep = RD.rebuild_song([], Path("x"), stem, tmp_path / "b", seconds=30.0, notes_override=notes)   # explicit override
    assert seen["total_s"] == 30.0 and rep["seconds_source"] == "--seconds"


def test_no_stem_length_is_last_note_plus_tail(monkeypatch, tmp_path):
    RD, seen = _fake_rd(monkeypatch)
    rep = RD.rebuild_song([], Path("x"), None, tmp_path / "c", notes_override=[MidiNoteT(50, 1.0, 9.0, 100)])
    assert seen["total_s"] == 9.0 + RD.TAIL_S and "alignment_vs_stem_full" not in rep


def test_cli_needs_a_song_or_midi():
    with pytest.raises(SystemExit):
        _rd().main([])


def test_envelope_from_stem_flag_passes_rel_db_and_default_is_off(monkeypatch, tmp_path):
    import soundfile as sf
    RD = _rd()
    seen = []

    def fake(notes, bank, prof, total_s=None, player=None, **kw):
        seen.append(kw)
        return np.zeros(int(total_s * 48000), np.float32) + 0.001, [], {"loud_dbfs": -28.0, "capped": False}

    monkeypatch.setattr(RD.GP, "render_profiled", fake)
    monkeypatch.setattr(RD, "_align_mod", lambda: type("A", (), {
        "onset_env": staticmethod(lambda x: np.zeros(10)),
        "align": staticmethod(lambda a, b: {k: 0 for k in ("score", "median_corr", "lag_iqr_ms", "global_lag_s", "global_corr")} | {"windows": []})}))
    stem = tmp_path / "s.wav"
    x = np.zeros(48000 * 2, np.float32)
    x[4800:14400] = 0.1
    sf.write(stem, x, 48000)
    prof = RD.GP.GuitarProfile({"guitar_id": "g1"}, [], tmp_path)
    notes = [MidiNoteT(50, 0.1, 0.5, 100), MidiNoteT(55, 1.0, 1.4, 100)]
    RD.rebuild_song([], Path("x"), stem, tmp_path / "off", seconds=2.0, notes_override=notes, profile=prof)
    rep = RD.rebuild_song([], Path("x"), stem, tmp_path / "on", seconds=2.0, notes_override=notes, profile=prof, envelope_from_stem=True)
    assert seen[0] == {} and len(seen[1]["rel_db"]) == 2 and seen[1]["rel_db"][0] > seen[1]["rel_db"][1]
    assert rep["envelope_from_stem"]["notes"] == 2
    with pytest.raises(SystemExit):
        RD.rebuild_song([], Path("x"), None, tmp_path / "nostem", seconds=2.0, notes_override=notes, profile=prof, envelope_from_stem=True)
    with pytest.raises(SystemExit):                                          # the parser accepts the flag; no input -> usage error
        RD.main(["--envelope-from-stem"])
