"""T061 speed features, offline: decay measurement, clip excerpts, readback cadence, measured
waits and the in-session restore byte-check. Fake bridge; no pedal."""
from __future__ import annotations

import numpy as np
import pytest
from test_stomp import FakeBridge, make_chain, make_engine, make_preset

from tonematch import reamp
from tonematch.engines import stomp

SR = reamp.SR


def test_measure_decay_finds_where_an_exponential_tail_dies():
    rng = np.random.default_rng(0)
    n, tail = SR, 6 * SR
    clip = 0.5 * rng.standard_normal(n)
    t = np.arange(tail) / SR
    dec = 0.5 * rng.standard_normal(tail) * np.exp(-t / 0.2)        # -43.4 dB per second
    floor = 1e-5 * rng.standard_normal(n + tail)
    x = np.concatenate([clip, dec]) + floor
    d = stomp.measure_decay_s(x, 0, n)
    # -60 dB below the clip peak: about 60 / 43.4 = 1.38 s (envelope windows add a little)
    assert 1.2 < d < 1.6


def test_measure_decay_is_zero_for_a_dry_chain():
    rng = np.random.default_rng(1)
    x = np.concatenate([0.5 * rng.standard_normal(SR), 1e-5 * rng.standard_normal(2 * SR)])
    assert stomp.measure_decay_s(x, 0, SR) < 0.05


def test_excerpt_picks_the_loudest_window_and_clip_for_stages():
    x = np.zeros(10 * SR, np.float32)
    x[6 * SR:7 * SR] = 1.0
    y, start = reamp.excerpt(x, 2.0)
    assert len(y) == 2 * SR and start <= 6 * SR and start + 2 * SR >= 7 * SR
    s, _ = reamp.clip_for(x, "search")
    f, _ = reamp.clip_for(x, "finalist")
    assert len(s) == round(reamp.SEARCH_CLIP_S * SR) and len(f) == round(min(reamp.FINALIST_CLIP_S, 10) * SR)
    short, st = reamp.excerpt(x[:SR], 3.0)
    assert len(short) == SR and st == 0
    with pytest.raises(ValueError):
        reamp.clip_for(x, "final")


def test_readback_every_n_skips_chain_reads_but_keeps_values(monkeypatch, tmp_path):
    eng, b = make_engine(monkeypatch, tmp_path)
    eng.whole_preset = "fallback"
    eng.load(make_preset(drive=0.6))
    eng.readback_every = 5
    b.calls.clear()
    for k in range(4):
        eng.load(make_preset(drive=0.1 + 0.1 * k))
    cmds = [c for c, _ in b.calls]
    assert cmds.count("chain") == 0 and eng.readbacks_skipped == 4
    assert cmds.count("set") == 4                              # only the changed Drive each time
    assert eng.last_readback.get("skipped") is True
    eng.load(make_preset(drive=0.9))                           # 5th load: full readback
    assert [c for c, _ in b.calls].count("chain") == 1 and eng.last_readback.get("ok") is True
    drive = next(p for p in b._slot(2)["params"] if p["sym"] == "Drive")["value"]
    assert drive == pytest.approx(0.9)


def test_default_still_reads_back_every_load(monkeypatch, tmp_path):
    eng, b = make_engine(monkeypatch, tmp_path)
    eng.load(make_preset(drive=0.6))
    b.calls.clear()
    eng.load(make_preset(drive=0.7))
    assert [c for c, _ in b.calls].count("chain") == 2         # before and after, as before T061


def test_cached_routing_is_updated_after_route_key(monkeypatch, tmp_path):
    eng, b = make_engine(monkeypatch, tmp_path)
    eng.readback_every = 10
    eng.load(make_preset(drive=0.6))
    b.calls.clear()
    eng.load(make_preset(drive=0.65))
    assert "route_key" not in [c for c, _ in b.calls]


def fake_playrec(log, tail_db=-20.0):
    def playrec(x, device, pre_s, tail_s):
        log.append((pre_s, tail_s))
        pre, n = int(pre_s * SR), len(x)
        rest = int(tail_s * SR)
        y = np.concatenate([np.zeros(pre), np.asarray(x, np.float64), np.zeros(rest)])
        t = np.arange(rest) / SR
        y[pre + n:] += 0.3 * np.sin(2 * np.pi * 440 * t) * np.exp(-t / 0.15)  # a 0.15 s decay
        y = np.concatenate([np.zeros(10), y])[: len(y)] + 1e-6
        return np.stack([y, y], axis=1).astype(np.float32), pre
    return playrec


def test_measured_tail_probes_once_then_waits_less(monkeypatch, tmp_path):
    chain = make_chain(delay_on=True)
    eng, _b = make_engine(monkeypatch, tmp_path, chain)
    log: list = []
    monkeypatch.setattr(reamp, "playrec", fake_playrec(log))
    monkeypatch.setattr(stomp.StompEngine, "pre_roll_s", lambda self: 3.0)   # the upper bound
    x = 0.3 * np.sin(2 * np.pi * 220 * np.arange(SR) / SR).astype(np.float32)
    eng.tail_mode = "bound"
    eng.render(x)
    assert log[-1][0] == 3.0
    eng.tail_mode = "measured"
    eng.render(x)                                   # probe: records the bound as tail
    assert log[-1][1] > 3.0
    d = eng.decay_s[eng._tail_key()]
    assert 0.5 < d < 1.5                            # 0.3 * exp(-t/0.15) to -60 dB re the clip
    eng.render(x)                                   # the probe's tail already played out
    assert log[-1][0] == eng.PRE_S
    eng.render(x)
    assert eng.PRE_S <= log[-1][0] <= d + eng.DECAY_MARGIN_S + 1e-6 < 3.0


def test_close_verify_bytes_uses_the_same_session(monkeypatch, tmp_path):
    eng, b = make_engine(monkeypatch, tmp_path)

    def call(cmd, timeout_s=30.0, **a):
        if cmd == "backup":
            from pathlib import Path
            Path(a["path"]).write_bytes(b"doc")
        return FakeBridge.call(b, cmd, timeout_s, **a)

    b.call = call
    eng.bridge.call("backup", path=str(eng.backup_path))
    eng.load(make_preset(drive=0.9))
    eng.close(verify_bytes=True)
    assert eng.restore_report["match"] is True and eng.restore_report["bytes_equal_backup"] is True
    assert b.closed


def test_trusted_loads_never_touch_the_original_chain(monkeypatch, tmp_path):
    """Regression (hardware 2026-10-16): fast loads mutated original_chain, so close() compared the
    restored preset with the edited one and fell back to writing the backup."""
    import copy

    eng, _b = make_engine(monkeypatch, tmp_path)
    before = copy.deepcopy(eng.original_chain)
    eng.readback_every = 10
    eng.load(make_preset(drive=0.9))
    eng.load(make_preset(drive=0.2))
    assert eng.original_chain == before
    eng.close()
    assert eng.restore_report["match"] is True and eng.restore_report["steps"] == ["select original"]
