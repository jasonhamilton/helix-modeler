"""note_tune helpers, the transcribe_stems sweep (Basic Pitch mocked) and the low-position rebuild option."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

from tonematch import dry_rebuild as D
from tonematch import note_tune as NT
from tonematch.midi_read import MidiNote

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("transcribe_stems", ROOT / "scripts" / "transcribe_stems.py")
TS = importlib.util.module_from_spec(spec)
sys.modules["transcribe_stems"] = TS
spec.loader.exec_module(TS)


def test_f_measure_tolerance_pitch_and_octave():
    ref = [(1.0, 1.5, 50, 0.8), (2.0, 2.5, 55, 0.8), (3.0, 3.5, 60, 0.8)]
    est = [(1.03, 1.4, 50, 0.7), (2.2, 2.5, 55, 0.7), (3.0, 3.4, 72, 0.7), (4.0, 4.2, 40, 0.5)]
    s = NT.f_measure(ref, est, 0.05)
    assert s["tp"] == 1 and s["n_est"] == 4 and s["n_ref"] == 3 and abs(s["precision"] - 0.25) < 1e-9
    o = NT.f_measure(ref, est, 0.05, octave_tolerant=True)
    assert o["tp"] == 2                                   # the C one octave up now counts
    assert NT.f_measure(ref, est, 0.25)["tp"] == 2        # a 250 ms window admits the note that is 200 ms late
    assert NT.f_measure([], [])["f"] == 0.0


def test_f_measure_is_one_to_one():
    ref = [(1.0, 2.0, 50, 1.0)]
    est = [(1.0, 1.2, 50, 1.0), (1.02, 1.3, 50, 1.0)]
    assert NT.f_measure(ref, est)["tp"] == 1


def test_filters():
    notes = [(0.0, 0.5, 35, 0.9), (0.0, 0.5, 52, 0.9), (0.1, 0.6, 60, 0.9), (0.2, 0.7, 64, 0.1), (5.0, 5.5, 90, 0.5)]
    assert [n[2] for n in NT.filter_notes(notes, min_pitch=40, max_pitch=84)] == [52, 60, 64]
    out = NT.filter_notes(notes, rel_amp=0.5)
    assert (0.2, 0.7, 64, 0.1) not in out and (5.0, 5.5, 90, 0.5) in out        # lone note kept: no neighbours
    m = NT.filter_notes([(0, 1, 50, 0.4), (1.05, 2, 50, 0.9), (3, 4, 50, 0.5)], restrike_gap_s=0.1)
    assert m == [(0, 2, 50, 0.9), (3, 4, 50, 0.5)]
    assert NT.shift(notes[:1], 2.0)[0][:3] == (2.0, 2.5, 35)
    assert len(NT.crop(notes, 0.05, 1.0)) == 2


def test_flag_out_of_scale_flags_only():
    notes = [(0, 1, 47, 1.0), (1, 2, 51, 1.0)]           # B (in B minor) and D# (out)
    assert NT.flag_out_of_scale(notes, [11, 1, 2, 4, 6, 7, 9]) == [False, True]
    assert NT.pc_histogram(notes) == {3: 1, 11: 1}


def _fake_decode(output, params):
    """Pretend Basic Pitch: higher onset_threshold keeps fewer of the 'detections' stored in the fake output."""
    return [(t, t + 0.2, p, a) for t, p, a in output["dets"] if a >= params["onset_threshold"]]


def test_sweep_picks_the_setting_that_matches_the_reference():
    ref = [(1.0, 1.2, 50, 1.0), (2.0, 2.2, 52, 1.0)]
    dets = [(1.0, 50, 0.9), (2.0, 52, 0.7), (3.0, 40, 0.35), (4.0, 41, 0.3)]     # two real notes, two strays
    songs = {"a": {"out": {"dets": dets}, "ref": ref, "span": (0.0, 10.0)},
             "b": {"out": {"dets": dets}, "ref": ref, "span": (0.0, 10.0)}}
    grid = {"onset_threshold": [0.2, 0.5], "frame_threshold": [0.3], "minimum_note_length": [58.0],
            "range": [(None, None)], "melodia_trick": [True]}
    res = TS.sweep(songs, grid=grid, fgrid={"min_pitch": [None, 45]}, top_k=2, decode=_fake_decode, log=lambda *_: None)
    best = max(res["core"] + res["filtered"], key=lambda r: r["mean_f"])
    assert best["settings"]["params"]["onset_threshold"] == 0.5 and best["mean_f"] == 1.0
    assert res["default"]["mean_f"] == 1.0 or res["default"]["mean_p"] <= 1.0
    low = [r for r in res["core"] if r["settings"]["params"]["onset_threshold"] == 0.2][0]
    assert low["mean_p"] == 0.5 and low["mean_r"] == 1.0
    assert len(res["filtered"]) == 4


def test_cached_output_runs_the_network_once(tmp_path):
    wav = tmp_path / "x.wav"
    sf.write(str(wav), np.zeros(100, np.float32), 48000)
    calls = []

    def infer(p):
        calls.append(p)
        return {"note": np.ones((3, 2)), "onset": np.zeros((3, 2)), "contour": np.zeros((3, 6))}

    a = TS.cached_output(wav, tmp_path / "c", infer=infer)
    b = TS.cached_output(wav, tmp_path / "c", infer=infer)
    assert len(calls) == 1 and np.array_equal(a["note"], b["note"])


def test_find_stems_maps_sources_via_manifests(tmp_path):
    for h, src in (("aa", "data/songs/Mother_0-68s.wav"), ("bb", "data/songs/The Smashing Pumpkins - Cherub Rock.mp3"),
                   ("cc", "/tmp/synth_mix.wav")):
        d = tmp_path / h / "htdemucs_6s"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"source": src}))
        (d / "guitar.wav").write_bytes(b"")
    found = TS.find_stems(tmp_path)
    assert set(found) == {"mother", "cherub-rock"} and found["mother"].parent.parent.name == "aa"


def test_apply_settings_merges_defaults_and_filters():
    out = {"dets": [(1.0, 35, 0.9), (2.0, 52, 0.9)]}
    notes = TS.apply_settings(out, {"params": {"onset_threshold": 0.5}, "filters": {"min_pitch": 40}}, decode=_fake_decode)
    assert [n[2] for n in notes] == [52]


def test_prefer_position_low_uses_the_lowest_fret(tmp_path):
    t = np.arange(D.SR) / D.SR
    wav = tmp_path / "n.wav"
    sf.write(str(wav), (0.3 * np.sin(2 * np.pi * 110 * t)).astype(np.float32), D.SR, subtype="FLOAT")
    mk = lambda fret, string: D.BankNote(file=str(wav), player="P1", kind="singlenotes", pitch=57, onset=0.0, midi_onset=0.0,
                                         avail=0.9, string=string, fret=fret, level_db=-20.0)
    bank = [mk(12, 3), mk(5, 5), mk(7, 4)]
    _, log = D.render([MidiNote(57, 0.1, 0.4, 100), MidiNote(57, 0.5, 0.8, 100)], bank, total_s=1.0, prefer_position="low")
    assert {x["fret"] for x in log} == {5}
    _, log = D.render([MidiNote(57, 0.1, 0.4, 100), MidiNote(57, 0.5, 0.8, 100), MidiNote(57, 0.9, 1.2, 100)], bank, total_s=1.5)
    assert len({x["fret"] for x in log}) > 1                     # default: round-robin over all positions
