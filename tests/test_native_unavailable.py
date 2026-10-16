"""Models Helix Native refuses are not available on Native (lead 2026-10-16): catalog, validate, match_song."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_preset import CAT, synth

from tonematch import catalog as C
from tonematch import preset as P
from tonematch.engines import native as N

ROOT = Path(__file__).resolve().parents[1]


def _ms():
    if "match_song" in sys.modules:
        return sys.modules["match_song"]
    spec = importlib.util.spec_from_file_location("match_song", ROOT / "scripts" / "match_song.py")
    ms = importlib.util.module_from_spec(spec)
    sys.modules["match_song"] = ms
    spec.loader.exec_module(ms)
    return ms


@pytest.fixture
def refused(monkeypatch, tmp_path):
    """A rejected-drops log naming HD2_DelayFake as an invalid model (plus non-model refusals)."""
    log = {"a.hlx": {"models": ["HD2_DelayFake"], "message": ["HelixCore: Invalid model. [code -1000302]"]},
           "b.hlx": {"models": ["HD2_CabFakeWithPan"], "message": ["HelixCore: Object not found."]},
           "c.hlx": {"models": ["HD2_AmpFake", "HD2_CabFake"], "message": ["Invalid model"]}}
    p = tmp_path / "rejected_drops.json"
    p.write_text(json.dumps(log))
    monkeypatch.setattr(N, "rejected_path", lambda: p)
    return p


def test_native_unavailable_reads_only_single_model_invalid_refusals(refused):
    assert C.native_unavailable() == {"HD2_DelayFake"}
    assert not C.available_on("HD2_DelayFake", "native")
    assert C.available_on("HD2_AmpFake", "native") and C.available_on("HD2_DelayFake", "stomp", CAT)
    assert C.available_on("HD2_DelayFake", "native", unavailable=set())
    with pytest.raises(ValueError):
        C.available_on("HD2_AmpFake", "pod")


def test_validate_fails_refused_model_only_for_native(refused):
    pre = P.from_dict(synth(), CAT)
    assert not [v for v in P.validate(pre, CAT) if v.severity == "fail"]
    fails = [v for v in P.validate(pre, CAT, engine="native") if v.severity == "fail"]
    assert [v.check for v in fails] == ["model"] and "HD2_DelayFake" in fails[0].message


def test_build_candidates_skips_a_set_with_a_refused_model(refused, tmp_path, monkeypatch):
    ms = _ms()
    P.write_hlx(P.from_dict(synth(), CAT), tmp_path / "x.hlx")
    (tmp_path / "t").mkdir()
    monkeypatch.setattr(ms, "TEMPLATE_SETS", {"X": ("x.hlx", "t")})
    cands, notes = ms.build_candidates(tmp_path, CAT, None, ms.NATIVE_FROZEN)
    assert cands == [] and len(notes) == 1 and "not available on Helix Native" in notes[0]


def test_freeze_writes_policy_values_in_place():
    ms = _ms()
    pre = P.from_dict(synth(), CAT)
    out = ms.freeze(pre, {("amp", "Drive"): 0.0, ("delay", "Absent"): 1.0})
    assert out is pre and pre.slots[0].params["Drive"] == 0.0 and "Absent" not in pre.slots[2].params


def test_build_candidates_freezes_before_picking_a_template(tmp_path, monkeypatch):
    """A template baked at the old value must be judged against the frozen preset, so it reads as stale."""
    ms = _ms()
    monkeypatch.setattr(C, "native_unavailable", lambda: set())
    P.write_hlx(P.from_dict(synth(), CAT), tmp_path / "x.hlx")
    (tmp_path / "t").mkdir()
    monkeypatch.setattr(ms, "TEMPLATE_SETS", {"X": ("x.hlx", "t")})
    seen = []
    monkeypatch.setattr(ms, "pick_template", lambda variants, preset: seen.append(preset.slots[0].params["Drive"]))
    cands, notes = ms.build_candidates(tmp_path, CAT, None, {("amp", "Drive"): 0.0})
    assert seen == [0.0] and cands == [] and "frozen values" in notes[0]


def test_policy_freezes_hum_and_ripple_at_zero_on_both_engines():
    from tonematch import param_policy
    for eng in ("stomp", "native"):
        frozen = param_policy.frozen_for(eng)
        assert frozen[("amp", "Hum")] == 0.0 and frozen[("amp", "Ripple")] == 0.0
