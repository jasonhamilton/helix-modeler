"""T168: the template capture leaves out its op-21 put-back when the whole-preset load that follows
rebuilds or empties the capture slot anyway (approved by the human, 2026-10-18). Fake bridge."""
from __future__ import annotations

import pytest

from test_stomp import Preset, Slot, make_preset
from test_stomp_robust import template_engine
from tonematch.engines import stomp


def restores(b) -> int:
    return [c for c, _ in b.calls].count("restore")


def test_capture_slot_is_slot_2_in_the_fixture(monkeypatch, tmp_path):
    eng, _b = template_engine(monkeypatch, tmp_path)
    assert eng._capture_pos() == 2
    assert stomp.device_pos(0, 1) == 2


def test_put_back_skipped_when_the_load_rebuilds_the_capture_slot(monkeypatch, tmp_path):
    eng, b = template_engine(monkeypatch, tmp_path)
    before = stomp.chain_signature(eng.chain)
    p = make_preset(model_id="AmpB")                       # slot 2 gets another amp
    assert eng.put_back_needed(p, 2) is False
    got = eng.capture_templates([("AmpB", "CabA")], then_load=p)
    assert list(got) == ["AmpB|CabA"]
    assert restores(b) == 0 and eng.putbacks_skipped == 1
    # the engine's view is still the buffer as it was, which the whole load compares with
    assert stomp.chain_signature(eng.chain) == before


def test_put_back_skipped_when_the_load_empties_the_capture_slot(monkeypatch, tmp_path):
    eng, b = template_engine(monkeypatch, tmp_path)
    p = Preset(name="t", slots=[Slot("delay", "DelayB", {"Mix": 0.2}, True, key="block5", position=2,
                                     extra={"@path": 0})])
    assert eng.put_back_needed(p, 2) is False
    eng.capture_templates([("DelayB", None)], then_load=p)
    assert restores(b) == 0 and eng.putbacks_skipped == 1


def test_put_back_kept_when_the_load_keeps_the_capture_slots_model(monkeypatch, tmp_path):
    eng, b = template_engine(monkeypatch, tmp_path)
    p = make_preset()                                      # AmpA + CabA stay at slot 2
    p.slots[2].model_id = "DelayB"                         # only the delay at slot 3 is new
    assert eng.put_back_needed(p, 2) is True
    before = stomp.chain_signature(b.chain)
    eng.capture_templates([("DelayB", None)], then_load=p)
    assert restores(b) == 1 and eng.putbacks_skipped == 0
    assert stomp.chain_signature(b.chain) == before


def test_put_back_kept_without_a_following_load(monkeypatch, tmp_path):
    eng, b = template_engine(monkeypatch, tmp_path)
    before = stomp.chain_signature(b.chain)
    eng.capture_templates([("AmpB", "CabA")])
    assert restores(b) == 1 and stomp.chain_signature(b.chain) == before


def test_put_back_kept_when_the_device_refuses_a_model(monkeypatch, tmp_path):
    eng, b = template_engine(monkeypatch, tmp_path, refuse={("AmpM", "CabW")})
    p = make_preset(model_id="AmpM", cab_id="CabW")
    with pytest.raises(stomp.ModelRefused):
        eng.capture_templates([("AmpM", "CabW")], then_load=p)
    assert restores(b) == 1 and eng.putbacks_skipped == 0


def test_put_back_kept_when_the_capture_fails(monkeypatch, tmp_path):
    eng, b = template_engine(monkeypatch, tmp_path)
    real = b.call

    def failing(cmd, timeout_s=30.0, **a):
        if cmd == "chain" and any(c == "model" for c, _ in b.calls):
            raise stomp.BridgeError("chain: timed out")
        return real(cmd, timeout_s, **a)

    b.call = failing
    with pytest.raises(stomp.BridgeError):
        eng.capture_templates([("AmpB", "CabA")], then_load=make_preset(model_id="AmpB"))
    assert restores(b) == 1 and eng.putbacks_skipped == 0
