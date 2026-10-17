"""Chain-pattern prior on synthetic presets (no Line 6 data)."""
import importlib.util
import json
import math
from pathlib import Path

import pytest

from tonematch import chain_prior as CP

SPEC = importlib.util.spec_from_file_location(
    "chain_patterns", Path(__file__).resolve().parents[1] / "scripts" / "chain_patterns.py")
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)

M = {"drive": "HD2_DistScream808", "amp": "HD2_AmpUSDeluxeNrm", "cab": "HD2_Cab1x12USDeluxe",
     "delay": "HD2_DelayTransistorTape", "reverb": "HD2_ReverbRoom", "mod": "HD2_ChorusTrinityChorus",
     "comp": "HD2_CompressorDeluxeComp", "eq": "HD2_EQParametric"}


def blk(cat, pos, path=0, en=True):
    return {"@model": M[cat], "@position": pos, "@path": path, "@enabled": en}


def preset(cats, cab0=False, snaps=None):
    d = {"inputA": {"@model": "HelixStomp_AppDSPFlowInput"}}
    for i, c in enumerate(cats):
        d[f"block{i}"] = blk(c, i)
    if cab0:
        d["cab0"] = {"@model": M["cab"], "@enabled": True}
    t = {"dsp0": d, "dsp1": {}}
    for s in range(3):
        t[f"snapshot{s}"] = {"blocks": {"dsp0": (snaps or {}).get(s, {})}}
    return {"data": {"tone": t}}


def two_path(pre, a, b, post):
    d = {}
    pos = 0
    n = 0
    for c in pre:
        d[f"block{n}"] = blk(c, pos); n += 1; pos += 1
    d["split"] = {"@model": "HD2_AppDSPFlowSplitY", "@position": pos}
    spos = pos
    for c in a:
        d[f"block{n}"] = blk(c, pos + 1); n += 1; pos += 1
    for c in b:
        d[f"block{n}"] = blk(c, spos + 1, path=1); n += 1
    pos = max(pos, spos + len(b)) + 1
    d["join"] = {"@model": "HD2_AppDSPFlowJoin", "@position": pos}
    for c in post:
        pos += 1
        d[f"block{n}"] = blk(c, pos); n += 1
    return {"data": {"tone": {"dsp0": d, "dsp1": {}}}}


def test_categorize_heuristic():
    assert CP.categorize("HD2_AmpUSDeluxeNrm") == "amp"
    assert CP.categorize("HD2_PreampJCM800") == "preamp"
    assert CP.categorize("HD2_GateNoiseGate") == "comp"
    assert CP.categorize("HD2_VolPanVol") == "volpan"
    assert CP.categorize("HD2_Unknownthing") == "other"


def test_serial_order_and_cab0():
    ch = CP.extract_chain(preset(["drive", "amp", "delay", "reverb"], cab0=True))
    assert ch.linear == ["drive", "amp", "cab", "delay", "reverb"]
    assert not ch.two_path
    e = ch.edges()
    assert ("<s>", "drive") in e and ("reverb", "</s>") in e and ("amp", "cab") in e


def test_bypassed_dropped_unless_any_snapshot_enables():
    p = preset(["drive", "amp", "reverb"])
    t = p["data"]["tone"]["dsp0"]
    t["block0"]["@enabled"] = False
    t["block2"]["@enabled"] = False
    assert CP.extract_chain(p).linear == ["amp"]  # drive and reverb off
    p["data"]["tone"]["snapshot2"]["blocks"]["dsp0"] = {"block2": True}
    assert CP.extract_chain(p).linear == ["amp", "reverb"]


def test_two_path_y_split():
    ch = CP.extract_chain(two_path(["drive", "amp"], ["delay"], ["mod"], ["reverb"]))
    assert ch.pre == ["drive", "amp"] and ch.a == ["delay"] and ch.b == ["mod"]
    assert ch.post == ["reverb"] and ch.two_path
    e = set(ch.edges())
    assert {("amp", "delay"), ("amp", "mod"), ("delay", "reverb"), ("mod", "reverb")} <= e
    assert ("delay", "mod") not in e
    assert not ch.comparable(2, 3) and ch.comparable(0, 3)


def test_two_path_branches_from_start():
    ch = CP.extract_chain(two_path([], ["drive", "amp"], ["comp"], []))
    assert ch.pre == [] and ch.a == ["drive", "amp"] and ch.b == ["comp"]
    e = set(ch.edges())
    assert ("<s>", "drive") in e and ("<s>", "comp") in e
    assert ("amp", "</s>") in e and ("comp", "</s>") in e


def _prior(seqs):
    return CP.build_prior([CP.extract_chain(preset(s)) for s in seqs])


CORPUS = ([["comp", "drive", "amp", "delay", "reverb"]] * 6 + [["drive", "amp", "mod", "reverb"]] * 4
          + [["drive", "drive", "amp", "delay"]] * 2 + [["amp", "reverb"]])


def test_aggregates():
    pr = _prior(CORPUS)
    assert pr["n_presets"] == 13
    assert pr["bigram_counts"]["<s>>comp"] == 6
    assert pr["bigram_counts"]["drive>drive"] == 2
    assert pr["count_hist"]["drive"]["2"] == 2
    assert pr["cooccurrence"]["delay"]["amp"] == 1.0
    assert pr["order_before"]["drive|amp"] == [12, 12]
    assert pr["top_sequences"][0] == ["comp>drive>amp>delay>reverb", 6]
    assert pr["position"]["reverb"]["median_norm"] == 1.0
    assert json.dumps(pr)  # serializable
    assert not any(m in json.dumps(pr) for m in M.values())


def test_smoothing_gives_unseen_orders_mass_and_normalizes():
    pr = _prior(CORPUS)
    seen = CP.sequence_logprob(["drive", "amp", "delay", "reverb"], pr)
    unseen = CP.sequence_logprob(["reverb", "wah", "delay"], pr)
    assert math.isfinite(unseen) and unseen < seen
    for a in ["<s>", "amp", "reverb"]:
        tot = sum(CP._trans(pr, a, b) for b in [*pr["categories"], "</s>"])
        assert tot == pytest.approx(1.0)
    assert CP.sequence_logprob(["bogus"], pr) == CP.sequence_logprob(["other"], pr)


def test_sanity_good_order_beats_bad_order():
    pr = _prior(CORPUS)
    good = CP.sequence_logprob(["drive", "amp", "cab", "delay", "reverb"], pr)
    bad = CP.sequence_logprob(["reverb", "drive", "cab", "amp"], pr)
    assert good > bad


def test_committed_prior_sanity():
    if not CP.PRIOR_PATH.exists():
        pytest.skip("prior not generated")
    assert CP.sequence_logprob(["drive", "amp", "cab", "delay", "reverb"]) > \
        CP.sequence_logprob(["reverb", "drive", "cab", "amp"])
    assert CP.typical_position("reverb") > CP.typical_position("drive")


def test_top_structures_ordering_and_constraints():
    pr = _prior(CORPUS)
    out = CP.top_structures({"reverb", "drive", "amp", "delay"}, k=5, prior=pr)
    assert out[0][0] == ("drive", "amp", "delay", "reverb")
    ps = [p for _s, p in out]
    assert ps == sorted(ps, reverse=True) and 0 < sum(ps) <= 1.0 + 1e-9
    assert all(set(s) == {"reverb", "drive", "amp", "delay"} for s, _ in out)
    # cab glued right after the amp
    for s, _ in CP.top_structures({"amp", "cab", "delay", "drive"}, k=8, prior=pr):
        assert s[s.index("amp") + 1] == "cab"
    # repeats
    out = CP.top_structures({"drive", "amp"}, k=1, prior=pr, counts={"drive": 2})
    assert out[0][0].count("drive") == 2
    # limits
    assert CP.top_structures({"drive", "amp", "delay"}, prior=pr, max_blocks=2) == []
    pr2 = dict(pr, dsp_cost_median={"drive": 5, "amp": 25, "delay": 5})
    assert CP.top_structures({"drive", "amp"}, prior=pr2, dsp_budget=20) == []
    assert CP.top_structures({"drive", "amp"}, prior=pr2, dsp_budget=40)


def test_top_structures_probabilistic_presence():
    pr = _prior(CORPUS)
    out = CP.top_structures({"drive": 1.0, "amp": 1.0, "reverb": 0.1, "mod": 0.9}, k=3, prior=pr)
    assert "mod" in out[0][0] and "reverb" not in out[0][0]


def test_script_hlx_dir_recursive(tmp_path, monkeypatch):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    (sub / "x.hlx").write_text(json.dumps(preset(["drive", "amp", "reverb"])))
    (tmp_path / "junk.hlx").write_text("not json")
    out = tmp_path / "prior.json"
    monkeypatch.setattr(S, "LOCAL", tmp_path / "local")
    assert S.main(["--no-default", "--no-catalog", "--hlx-dir", str(tmp_path), "--out", str(out)]) == 0
    pr = json.loads(out.read_text())
    assert pr["n_presets"] == 1
    freq = json.loads((tmp_path / "local" / "model_frequencies.json").read_text())
    assert freq["amp"][M["amp"]] == 1 and M["amp"] not in out.read_text()
