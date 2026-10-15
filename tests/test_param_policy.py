"""Per-engine parameter policy (offline)."""
from __future__ import annotations

import json

import pytest

from tonematch import param_policy as pp
from tonematch.preset import Preset, Slot


def _preset(er=0.5):
    return Preset(name="t", slots=[
        Slot("amp", "AmpA", {"Drive": 0.5}, True, key="block0", position=0, extra={"@cab": "cab0"}),
        Slot("cab", "CabA", {"EarlyReflections": er, "Distance": 4.0}, True, key="cab0"),
    ])


def test_shipped_policy_freezes_early_reflections_on_the_stomp_only():
    assert pp.frozen_for("stomp")[("cab", "EarlyReflections")] == 0.0
    assert ("cab", "EarlyReflections") not in pp.frozen_for("native")


def test_violations_and_apply():
    assert pp.violations(_preset(0.0), "stomp") == []
    v = pp.violations(_preset(0.5), "stomp")
    assert len(v) == 1 and "EarlyReflections" in v[0]
    fixed = pp.apply(_preset(0.5), "stomp")
    assert fixed.slots[1].params["EarlyReflections"] == 0.0 and pp.violations(fixed, "stomp") == []
    assert pp.violations(_preset(0.5), "native") == []


def test_forbid_and_bad_policy(tmp_path):
    pol = {"rules": [{"category": "cab", "param": "Distance", "engines": {"stomp": {"action": "forbid"}}}]}
    assert pp.forbidden_for("stomp", pol) == {("cab", "Distance")}
    assert "forbidden" in pp.violations(_preset(0.0), "stomp", pol)[0]
    assert "Distance" not in pp.apply(_preset(0.0), "stomp", pol).slots[1].params
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"rules": [{"category": "c", "param": "p", "engines": {"stomp": {"action": "freeze"}}}]}))
    with pytest.raises(ValueError):
        pp.load_policy(str(bad))


def test_match_song_reads_the_policy():
    import importlib.util
    from pathlib import Path

    src = (Path(__file__).parents[1] / "scripts" / "match_song.py").read_text()
    assert 'param_policy.frozen_for("stomp")' in src
    assert importlib.util.find_spec("tonematch.param_policy") is not None
