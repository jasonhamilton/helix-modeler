"""T045 block-serial spike helpers: preset builders and alignment/null analysis (no Native)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("blockserial_spike", ROOT / "scripts" / "blockserial_spike.py")
bs = importlib.util.module_from_spec(spec)
sys.modules["blockserial_spike"] = bs
spec.loader.exec_module(bs)


def test_xcorr_lag_finds_a_shift_and_compare_nulls_it():
    rng = np.random.default_rng(0)
    a = rng.standard_normal(48_000).astype(np.float32)
    b = np.concatenate([np.zeros(16, np.float32), a])[: len(a)]  # b is a delayed by 16
    assert bs.xcorr_lag(a, b) == 16
    pad = np.concatenate([a, np.zeros(64, np.float32)])
    padb = np.concatenate([b, np.zeros(64, np.float32)])
    res = bs.compare(pad, padb * np.float32(1.01), 100, 40_000)
    assert res["lag_samples"] == 16
    assert res["gain_db"] == pytest.approx(-20 * np.log10(1.01), abs=1e-3)
    assert res["null_aligned_gain_db"] < -100


def test_block_slot_types_follow_hx_files():
    amp = bs.block_slot("amp", "HD2_AmpX", {"Drive": 0.5}, {"@cab": "cab0", "@type": 3}, 0, "block0")
    cab = bs.block_slot("cab", "HD2_CabX", {}, {"@mic": 3}, 1, "block1")
    assert amp.extra["@type"] == 1 and "@cab" not in amp.extra  # amp alone has no linked cab
    assert cab.extra["@type"] == 2 and cab.extra["@mic"] == 3 and cab.position == 1
    assert bs.block_slot("delay", "HD2_D", {}, {}, 2, "block2").extra["@type"] == 7
    assert bs.block_slot("eq", "HD2_E", {}, {}, 3, "block3").extra["@type"] == 0


@pytest.mark.skipif(not bs.K_HLX.is_file(), reason="local K.hlx (user preset) not present")
def test_configs_split_k_into_amp_then_cab():
    cat = {"models": {m: {"params": {}, "mono": True, "stereo": False} for _, m in bs.C8_MODELS}}
    cfg = bs.configs(cat)
    k_serial = cfg["K"]["serial"]
    assert [p.slots[0].category for p in k_serial] == ["amp", "cab"]
    assert all(len(p.slots) == 1 and p.slots[0].position == 0 for p in k_serial)
    assert [s.category for s in cfg["C8"]["single"].slots] == [c for c, _ in bs.C8_MODELS]
    assert [s.position for s in cfg["C8"]["single"].slots] == list(range(8))
