"""Tests for the .hls codec and CC assignment (no Line 6 files needed)."""
import json
import zlib

import pytest

from tonematch.engines import native_setlist as hs


def _hlx(name="T", drive=0.61):
    return {"version": 6, "schema": "L6Preset", "meta": {},
            "data": {"meta": {"name": name}, "device": 2162694, "device_version": 58720256,
                     "tone": {"dsp0": {
                         "block3": {"@model": "HD2_AmpEssexA15", "@position": 1, "@cab": "cab0",
                                    "Drive": drive, "Master": 1, "Hz": 440.0, "On": True},
                         "cab0": {"@model": "HD2_Cab1x12BlueBell", "LowCut": 19.9},
                         "block5": {"@model": "HD2_DL4PingPong", "@position": 2, "Mix": 0.23},
                         "inputA": {"@model": "HelixStomp_AppDSPFlowInput"}}}}}


def test_free_ccs_counts_match_pilots_guide():
    assert len(hs.free_ccs("stomp")) == 90
    assert len(hs.free_ccs("off")) == 125
    assert 69 not in hs.free_ccs("stomp") and 69 not in hs.free_ccs("off")
    assert 4 in hs.free_ccs("stomp") and 50 not in hs.free_ccs("stomp")


def test_roundtrip_and_crc_semantics():
    obj = hs.encode_hls([_hlx("A"), _hlx("B", 0.2)], "Gen")
    assert obj["schema"] == "L6Setlist" and obj["meta"]["device"] == 2162694
    dec = hs.decode_hls(obj)
    assert [p["meta"]["name"] for p in dec["presets"]] == ["A", "B"]
    assert dec["presets"][1]["tone"]["dsp0"]["block3"]["Drive"] == 0.2
    payload = zlib.decompress(__import__("base64").b64decode(obj["encoded_data"]))
    assert obj["compression"]["crc32"] == zlib.crc32(payload)  # CRC of the decompressed JSON
    bad = dict(obj, compression=dict(obj["compression"], crc32=1))
    with pytest.raises(ValueError):
        hs.decode_hls(bad)


def test_stomp_setlist_limit(tmp_path):
    with pytest.raises(ValueError):
        hs.encode_hls([_hlx()] * 127, "too many")
    p = hs.write_hls([_hlx()] * 126, "full", tmp_path / "s.hls")
    assert len(hs.decode_hls(p)["presets"]) == 126
    assert json.loads(p.read_text())["version"] == 2


def test_assign_ccs_writes_controller_and_map():
    src = _hlx()
    out, amap, rng, skipped = hs.assign_ccs(src, [(0, "Drive"), (0, "Master"), (2, "Mix"),
                                                  (0, "Hz"), (0, "On"), (1, "LowCut"), (9, "X")])
    assert "controller" not in src["data"]["tone"]  # input untouched
    c = out["data"]["tone"]["controller"]["dsp0"]
    assert c["block3"]["Drive"] == {"@controller": 8, "@cc": 4, "@min": 0.0, "@max": 1.0,
                                    "@snapshot_disable": False}
    assert amap == {"0:Drive": "cc_4", "0:Master": "cc_5", "2:Mix": "cc_6"}
    assert rng["2:Mix"] == [0.0, 1.0]
    assert set(skipped) == {"0:Hz", "0:On", "1:LowCut", "9:X"}


def test_assign_ccs_explicit_range_and_used_ccs():
    src = _hlx()
    src["data"]["tone"]["controller"] = {"dsp0": {"block5": {"Mix": {"@controller": 8, "@cc": 4}}}}
    out, amap, rng, _ = hs.assign_ccs(src, [(1, "LowCut"), (0, "Drive")],
                                      ranges={"1:LowCut": (19.9, 500.0)})
    assert amap == {"1:LowCut": "cc_5", "0:Drive": "cc_6"}  # cc 4 already used by the file
    assert out["data"]["tone"]["controller"]["dsp0"]["cab0"]["LowCut"]["@max"] == 500.0
    assert rng["1:LowCut"] == [19.9, 500.0]
