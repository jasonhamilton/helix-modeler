"""Offline tests for the play-along user-DI capture (reamp-capture --user-di --play). Fake audio
device and fake control helper only; no pedal, no song audio (synthetic songs in tmp_path)."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
import soundfile as sf

from tonematch import reamp, user_di
from tonematch.engines import stomp_bridge

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reamp_capture", ROOT / "scripts" / "reamp_capture.py")
RC = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RC)

SR = reamp.SR
LAT = 1206
SPIKE_SONG_S = 12.0  # song time of the marker in the synthetic song


# --------------------------------------------------------------------------- helpers

def _song(tmp_path: Path, seconds: float = 16.0) -> Path:
    """A silent 'song' with one spike at SPIKE_SONG_S (the fake player plays a note there)."""
    x = np.zeros(int(seconds * SR), np.float32)
    x[int(SPIKE_SONG_S * SR)] = 0.9
    p = tmp_path / "Song.wav"
    sf.write(p, x, SR, subtype="FLOAT")
    return p


def _cal(tmp_path: Path, firmware: str = "3.50") -> Path:
    cal = reamp.Calibration(latency_samples=LAT, measurements=[LAT] * 3, level_gain_db=0.0, tone_in_dbfs=-18,
                            tone_out_dbfs=-18, firmware=firmware, pa_latency=reamp.PA_LATENCY)
    return cal.save(tmp_path / "latency.json")


def _sections(tmp_path: Path) -> Path:
    beats = list(np.arange(0.25, 30, 0.5))  # 120 bpm, phase 0.25 s
    p = tmp_path / "sections.json"
    p.write_text(json.dumps({"tempo_bpm": 120.0, "beat_times": beats, "bar_starts": beats[::4]}))
    return p


NOTE = (0.3 * np.sin(2 * np.pi * 220 * np.arange(4800) / SR) * np.exp(-np.arange(4800) / 1200)).astype(np.float32)


class FakeDevice:
    """Stands in for reamp.duplex. The 'player' hears the song spike and plays NOTE on it; the dry
    Main L arrives on USB in 5 after the round trip, the wet on USB in 1/2 three samples later."""

    def __init__(self, mode: str = "ok"):
        self.mode, self.calls = mode, []

    def __call__(self, play, out_channels, device=None):
        self.calls.append((play.shape, tuple(out_channels), device))
        rec = np.zeros((len(play), 8), np.float32)
        i = int(np.argmax(np.abs(play[:, 0])))  # the spike (louder than the clicks)
        if self.mode != "unplugged":
            rec[i + LAT:i + LAT + len(NOTE), 4] = NOTE
            if self.mode == "clip":
                rec[i + LAT:i + LAT + 50, 4] = 1.0
            if self.mode not in ("no_wet", "bleed"):
                rec[i + LAT + 3:i + LAT + 3 + len(NOTE), 0] = 0.5 * NOTE
                rec[i + LAT + 3:i + LAT + 3 + len(NOTE), 1] = 0.5 * NOTE
        if self.mode == "bleed":
            rec[LAT:, 0] += play[:-LAT, 0]
            rec[LAT:, 1] += play[:-LAT, 1]
        return rec


def _run(monkeypatch, tmp_path, *extra, device=None, bridge=False):
    dev = device or FakeDevice()
    monkeypatch.setattr(reamp, "find_device", lambda *a, **k: 7)
    monkeypatch.setattr(reamp, "duplex", dev)
    monkeypatch.setattr(RC.time, "sleep", lambda s: None)
    argv = ["reamp_capture.py", "--user-di", "--play", str(_song(tmp_path)), "--section", "8:14",
            "--target", "10:14", "--countin", "4", "--calibration", str(_cal(tmp_path)),
            "--sections", str(_sections(tmp_path)), "--out", str(tmp_path / "out"), "--no-prompt", *([] if bridge else ["--no-bridge"]), *extra]
    monkeypatch.setattr(sys, "argv", argv)
    return RC.main(), dev


# --------------------------------------------------------------------------- pure maths

def test_parse_span():
    assert user_di.parse_span("36.45:46.45") == (36.45, 46.45)
    assert user_di.parse_span(None) is None
    with pytest.raises(ValueError):
        user_di.parse_span("5:4")
    with pytest.raises(ValueError):
        user_di.parse_span("abc")


def test_beat_grid_continues_the_song_grid():
    sec = {"beat_times": [0.25 + 0.5 * k for k in range(40)], "bar_starts": [0.25 + 2 * k for k in range(10)]}
    g = user_di.beat_grid(8.0, sections=sec)
    assert g.beat_s == pytest.approx(0.5) and g.first_beat_s == pytest.approx(8.25) and g.source == "sections.json"
    assert user_di.beat_grid(8.0, bpm=90).beat_s == pytest.approx(60 / 90)
    assert user_di.beat_grid(8.0).source.startswith("default")
    past = user_di.beat_grid(100.0, sections=sec)  # past the last detected beat: extrapolated
    assert (past.first_beat_s - 0.25) / 0.5 == pytest.approx(round((past.first_beat_s - 0.25) / 0.5))
    assert past.first_beat_s >= 100.0


def test_playback_layout_clicks_on_grid_then_section():
    sec = user_di.Section(np.ones((SR, 2), np.float32) * 0.1, 8.0, "x")
    grid = user_di.Grid(0.5, 8.25, "t", bar_starts=(6.25,))
    pb = user_di.build_playback(sec, 4, grid, tail_s=2.0, click_dbfs=-12.0)
    clicks_song = [8.0 + (i - pb.countin_n) / SR for i in pb.click_at]
    assert clicks_song == pytest.approx([6.25, 6.75, 7.25, 7.75], abs=1 / SR)
    assert pb.countin_n == round((user_di.LEAD_S + 1.75) * SR)
    assert np.all(pb.play[pb.countin_n:pb.countin_n + SR] == pytest.approx(0.1))
    assert pb.tail_n == 2 * SR and len(pb.play) == pb.countin_n + SR + 2 * SR
    first, second = (pb.play[i:i + 1200, 0] for i in pb.click_at[:2])
    zc = [int(np.sum(np.diff(np.sign(c)) != 0)) for c in (first, second)]
    assert zc[0] > 1.8 * zc[1]  # the downbeat at 6.25 is accented (an octave up)
    assert np.max(np.abs(first)) == pytest.approx(10 ** (-12 / 20), rel=1e-3)


def test_split_take_compensates_the_round_trip():
    pb = user_di.Playback(np.zeros((1000, 2), np.float32), countin_n=100, click_at=[], n_section=500, tail_n=100)
    rec = np.zeros((1000, 8), np.float32)
    rec[100 + 30 + 7, 4] = 1.0
    t = user_di.split_take(rec, pb, latency=30)
    assert len(t.dry) == 600 and int(np.argmax(t.dry)) == 7 and t.offset_samples == 130
    assert user_di.split_take(rec, pb, 30, extra_latency=2).dry[5] == 1.0
    short = user_di.split_take(rec[:400], pb, 30)  # stream came back short: zero padded
    assert len(short.dry) == 600


def test_next_take_index_never_reuses(tmp_path):
    assert user_di.next_take_index(tmp_path) == 1
    (tmp_path / "take1_dry.wav").write_bytes(b"")
    (tmp_path / "take3_dry.wav").write_bytes(b"")
    assert user_di.next_take_index(tmp_path) == 4


# --------------------------------------------------------------------------- script, fake device

def test_play_along_takes_are_aligned_to_the_song(monkeypatch, tmp_path):
    rc, dev = _run(monkeypatch, tmp_path, "--takes", "2")
    assert rc == 0 and len(dev.calls) == 2
    shape, outs, device = dev.calls[0]
    assert outs == user_di.SONG_TO and device == 7 and shape[1] == 2
    out = tmp_path / "out"
    for k in (1, 2):
        dry, sr = sf.read(out / f"take{k}_dry.wav", dtype="float32")
        wet, _ = sf.read(out / f"take{k}_wet.wav", dtype="float32")
        meta = json.loads((out / f"take{k}.json").read_text())
        assert sr == SR and dry.ndim == 1 and wet.shape[1] == 2
        # the note the player played on the song's spike sits at the spike's song time
        assert int(np.argmax(np.abs(dry))) == round((SPIKE_SONG_S - 8.0) * SR) + int(np.argmax(np.abs(NOTE)))
        al = meta["alignment"]
        assert al["take_sample0_song_s"] == 8.0 and al["song_offset_samples"] == 8 * SR
        assert al["latency_samples"] == LAT and al["recording_offset_samples"] == al["countin_samples"] + LAT
        assert meta["levels"]["dry_to_wet"]["lag_samples"] == 3
        assert meta["levels"]["target_window_samples"] == [2 * SR, 6 * SR]
        assert meta["warnings"] == [] and meta["levels"]["dry"]["clipped_samples"] == 0
        assert meta["countin"]["click_song_s"] == pytest.approx([6.25, 6.75, 7.25, 7.75], abs=1e-4)
        assert meta["source_sha16"] and meta["pedal"]["read_only"] is True
        assert len(dry) == meta["section_samples"] + meta["tail_samples"] == 6 * SR + 2 * SR


def test_unplugged_guitar_warns_and_exits_3(monkeypatch, tmp_path, capsys):
    rc, _ = _run(monkeypatch, tmp_path, device=FakeDevice("unplugged"))
    meta = json.loads((tmp_path / "out" / "take1.json").read_text())
    assert rc == 3 and meta["levels"]["dry"]["digital_zero"]
    assert any("not plugged in" in w for w in meta["warnings"])
    assert "not plugged in" in capsys.readouterr().err


def test_clipping_missing_wet_and_song_bleed_are_flagged(monkeypatch, tmp_path):
    for mode, words in (("clip", "clipped"), ("no_wet", "Input block on USB 5/6"), ("bleed", "song itself")):
        d = tmp_path / mode
        d.mkdir()
        rc, _ = _run(monkeypatch, d, device=FakeDevice(mode))
        meta = json.loads((d / "out" / "take1.json").read_text())
        assert rc == 0 and any(words in w for w in meta["warnings"]), (mode, meta["warnings"])


def test_takes_inside_the_repo_must_be_under_cache(monkeypatch, tmp_path):
    rc, dev = _run(monkeypatch, tmp_path, "--out", str(ROOT / "docs" / "nope"))
    assert rc == 1 and dev.calls == []
    assert not (ROOT / "docs" / "nope").exists()


def test_firmware_mismatch_refuses_before_playing(monkeypatch, tmp_path):
    class Bridge(FakeBridge):
        firmware = "3.80"

    monkeypatch.setattr(stomp_bridge, "helper_path", lambda: Path("/fake/stompd"))
    monkeypatch.setattr(stomp_bridge, "StompBridge", Bridge)
    rc, dev = _run(monkeypatch, tmp_path, bridge=True)
    assert rc == 1 and dev.calls == []


class FakeBridge:
    """Control helper stand-in. Records every command; --user-di may only read."""

    firmware = "3.50"
    instances: ClassVar[list[FakeBridge]] = []

    def __init__(self, *a, **k):
        self.calls, self.closed = [], False
        FakeBridge.instances.append(self)

    def call(self, cmd, **args):
        self.calls.append(cmd)
        if cmd == "info":
            return {"firmware": self.firmware, "label": "04C", "name": "Smoke Known"}
        if cmd == "chain":
            return {"setlist": 0, "index": 11, "name": "Smoke Known", "assignments": [],
                    "slots": [{"pos": 0, "kind": "Input", "enabled": True, "routing": 6, "params": []},
                              {"pos": 1, "kind": "Block", "model": "Amp", "enabled": True, "params": []}]}
        raise AssertionError(f"--user-di sent a non-read command: {cmd}")

    def close(self):
        self.closed = True


def test_bridge_is_read_only_warns_on_usb_input_and_confirms_unchanged(monkeypatch, tmp_path, capsys):
    FakeBridge.instances.clear()
    monkeypatch.setattr(stomp_bridge, "helper_path", lambda: Path("/fake/stompd"))
    monkeypatch.setattr(stomp_bridge, "StompBridge", FakeBridge)
    rc, _ = _run(monkeypatch, tmp_path, bridge=True)
    b = FakeBridge.instances[-1]
    assert rc == 0 and set(b.calls) == {"info", "chain"} and b.closed
    meta = json.loads((tmp_path / "out" / "take1.json").read_text())
    assert meta["pedal"]["unchanged_after_session"] is True and meta["pedal"]["input_routing"] == 6
    assert meta["pedal"]["firmware"] == "3.50" and meta["pedal"]["preset"] == "04C Smoke Known"
    assert "Input block is on USB" in capsys.readouterr().err


def test_dry_run_touches_no_device(monkeypatch, tmp_path):
    rc, dev = _run(monkeypatch, tmp_path, "--dry-run")
    assert rc == 0 and dev.calls == [] and not (tmp_path / "out").exists()


# --------------------------------------------------------------------------- reamp.duplex, fake sounddevice

def test_duplex_maps_columns_and_stops_on_interrupt(monkeypatch):
    seen = {}

    def playrec(buf, **k):
        seen["buf"], seen["k"] = buf, k
        if seen.get("raise"):
            raise KeyboardInterrupt
        return np.zeros((len(buf), 8), np.float32)

    fake = types.SimpleNamespace(query_devices=lambda d: {"max_output_channels": 8, "max_input_channels": 8},
                                 playrec=playrec, stop=lambda: seen.setdefault("stopped", True))
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    play = np.stack([np.full(10, 0.1, np.float32), np.full(10, 0.2, np.float32)], axis=1)
    rec = reamp.duplex(play, (1, 2), device=3)
    assert rec.shape == (10, 8)
    assert np.all(seen["buf"][:, 0] == np.float32(0.1)) and np.all(seen["buf"][:, 1] == np.float32(0.2))
    assert not np.any(seen["buf"][:, 2:])
    assert seen["k"]["latency"] == reamp.PA_LATENCY and seen["k"]["samplerate"] == SR
    with pytest.raises(reamp.ReampError):
        reamp.duplex(play, (1, 9), device=3)
    seen["raise"] = True
    with pytest.raises(KeyboardInterrupt):
        reamp.duplex(play, (1, 2), device=3)
    assert seen["stopped"]
