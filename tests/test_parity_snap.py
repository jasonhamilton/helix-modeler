"""Parity method rule (parity_snap.py): the Stomp is given the values Native's 128-step CC grid plays, with lo and hi
read from the captured template (never the catalog). Offline."""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import parity_snap as S

from tonematch import preset as P
from tonematch.engines import native as N


def _preset(time=0.6, rate=0.2) -> P.Preset:
    return P.Preset(name="t", slots=[
        P.Slot("delay", "HD2_DelaySimpleDelay", {"Time": time, "Mix": 0.5}, True, key="block0", position=0),
        P.Slot("mod", "HD2_PhaserDeluxePhaser", {"Rate": rate, "Depth": 0.3}, True, key="block1", position=1)])


def _template(p, ranges, tmp_path, verified=None):
    mp = {f"{i}:{n}": f"cc_{i}{n}" for i, s in enumerate(p.slots) for n in s.params}
    ver = sorted(mp) if verified is None else verified
    t = N.ChainTemplate(N.chain_key(p), b"state", mp, ranges, "t", ver, {}, N.baked_fingerprint(p, mp, ver)[1])
    t.save(tmp_path)
    return t


def test_snap_value_is_the_cc_grid_and_follows_the_template_range():
    # Time 0-8 s: 62.99 ms steps; the narrowed 0-1.27 s range (T072): 10 ms steps
    assert S.snap_value(0.6, 0.0, 8.0) == pytest.approx(0.6299212, abs=1e-6)
    assert S.snap_value(0.6, 0.0, 1.27) == pytest.approx(0.6, abs=1e-6)
    assert S.snap_value(0.6, 0.0, 8.0) == pytest.approx(N.cc_value(0.0, 8.0, 10))
    assert S.snap_value(-5.0, 0.0, 1.0) == 0.0 and S.snap_value(5.0, 0.0, 1.0) == 1.0     # clamps like the CC


def test_ranges_come_from_the_template_not_the_catalog(tmp_path):
    p = _preset()
    ranges = {"0:Time": [0.0, 1.27], "0:Mix": [0.0, 1.0], "1:Rate": [0.0, 10.0], "1:Depth": [0.0, 1.0]}
    t = _template(p, ranges, tmp_path)
    assert S.template_for(p, tmp_path).ranges == t.ranges
    moves = S.snapped_values(p, t)
    assert "0:Time" not in moves                                  # 0.6 s is on the narrowed grid
    assert moves["1:Rate"][1] == pytest.approx(N.cc_value(0.0, 10.0, 3))          # 0.2 -> 0.2362
    assert moves["1:Rate"][2:] == (0.0, 10.0)
    t2 = _template(p, {**ranges, "0:Time": [0.0, 8.0]}, tmp_path / "wide")
    assert S.snapped_values(p, t2)["0:Time"][1] == pytest.approx(0.6299212, abs=1e-6)


def test_snap_preset_writes_snapped_values_and_is_idempotent(tmp_path):
    p = _preset()
    ranges = {"0:Time": [0.0, 8.0], "0:Mix": [0.0, 1.0], "1:Rate": [0.0, 10.0], "1:Depth": [0.0, 1.0]}
    _template(p, ranges, tmp_path)
    before = copy.deepcopy(p)
    q, moves = S.snap_preset(p, tmp_path)
    assert p.slots[0].params == before.slots[0].params                           # input untouched
    assert q.slots[0].params["Time"] == pytest.approx(0.6299212, abs=1e-6)
    assert set(moves) == {"0:Time", "0:Mix", "1:Rate", "1:Depth"}
    # the snapped preset needs a template of its own to be re-snapped; on-grid values do not move
    _template(q, ranges, tmp_path)
    q2, moves2 = S.snap_preset(q, tmp_path)
    assert moves2 == {} and q2.slots[0].params == q.slots[0].params


def test_unverified_or_unranged_params_are_left_alone(tmp_path):
    p = _preset()
    t = _template(p, {"0:Time": [0.0, 8.0]}, tmp_path, verified=["0:Time", "0:Mix"])
    assert set(S.snapped_values(p, t)) == {"0:Time"}                  # Mix has no range, Rate/Depth are not verified


def test_no_template_is_an_error_not_a_silent_catalog_fallback(tmp_path):
    with pytest.raises(LookupError):
        S.template_for(_preset(), tmp_path)


def test_out_of_range_value_rejects_the_template(tmp_path):
    p = _preset(time=3.0)
    _template(p, {"0:Time": [0.0, 8.0], "1:Rate": [0.0, 10.0]}, tmp_path / "wide")
    narrow = _template(p, {"0:Time": [0.0, 1.27], "1:Rate": [0.0, 10.0]}, tmp_path / "narrow")
    assert N.out_of_range(narrow, p) == ["0:Time"]
    d = tmp_path / "both"
    d.mkdir()
    for sub in ("wide", "narrow"):
        for f in (tmp_path / sub).iterdir():
            (d / f"{sub}_{f.name}").write_bytes(f.read_bytes())
    assert S.template_for(p, d).ranges["0:Time"] == [0.0, 8.0]


def test_write_snapped_hlx_round_trip(tmp_path):
    p = _preset()
    ranges = {"0:Time": [0.0, 8.0], "0:Mix": [0.0, 1.0], "1:Rate": [0.0, 10.0], "1:Depth": [0.0, 1.0]}
    tdir = tmp_path / "t"
    _template(p, ranges, tdir)
    src = P.write_hlx(p, tmp_path / "a.hlx")
    rep = S.write_snapped_hlx(src, tmp_path / "out" / "a.hlx", tdir)
    q = P.read_hlx(tmp_path / "out" / "a.hlx")
    assert q.slots[0].params["Time"] == pytest.approx(0.6299212, abs=1e-6)
    assert rep["moves"]["0:Time"][1] == pytest.approx(q.slots[0].params["Time"])


def test_timefx_flags_renders_made_off_grid(tmp_path, monkeypatch):
    import json

    import parity_timefx as PT

    monkeypatch.setattr(PT, "OUT", tmp_path)
    assert PT.stomp_on_grid(["a", "b"]) == {"a": False, "b": False}          # no marker: the T060 renders
    (tmp_path / "snapped_stomp.json").write_text(json.dumps({"a": True}))
    assert PT.stomp_on_grid(["a", "b"]) == {"a": True, "b": False}
