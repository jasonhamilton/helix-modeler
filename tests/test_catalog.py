"""Catalog importer tests. All data here is synthetic; no Line 6 file is used or copied."""
import json
import plistlib

import pytest

from tonematch import catalog as C

DIST = [
    {"symbolicID": "HD2_DistFake", "mono": True, "stereo": True, "name": "Fake OD", "category": 3,
     "load": 4.0, "load_stereo": 8.0,
     "params": [
         {"symbolicID": "Gain", "name": "Gain", "valueType": 1, "min": 0.0, "max": 1.0, "default": 0.5},
         {"symbolicID": "Mode", "name": "Mode", "valueType": 0, "min": 0, "max": 3, "default": 1},
         {"symbolicID": "@enabled", "name": "Enabled", "valueType": 2, "min": False, "max": True,
          "default": True},
         {"symbolicID": "Scale", "name": "Scale", "valueType": 1, "min": 0.0, "max": 1.0,
          "default": 0.2, "stereo-only": True}]},
    {"symbolicID": "HD2_DistOnlyBig", "name": "Big", "category": 3, "load": 9.0,
     "devices": [{"id": 1}], "params": []},
]
AMP = [{"symbolicID": "HD2_AmpFake", "name": "Fake Amp", "category": 1, "load": 20.0,
        "cablink": "HD2_CabFake", "devices": [{"id": C.STOMP_DEVICE_ID, "version": "0x03190100"}],
        "params": [{"symbolicID": "Drive", "name": "Drive", "valueType": 1, "min": 0.0, "max": 1.0,
                    "default": 0.4}]}]
GATE = [{"symbolicID": "HD2_GateFake", "name": "G", "category": 4, "load": 1.0, "params": []}]
FIXED = [{"symbolicID": "@global_params", "params": [
    {"symbolicID": "@tempo", "name": "Tempo", "valueType": 1, "min": 20.0, "max": 240.0,
     "default": 120.0}]}]


def make_install(tmp_path, version="9.9", name="Fake Edit.app", mac=True):
    root = tmp_path / name
    res = root / "Contents" / "Resources" if mac else root / "res"
    res.mkdir(parents=True)
    for fn, data in (("distortion", DIST), ("amp", AMP), ("gate", GATE), ("fixed", FIXED)):
        (res / f"{fn}.models").write_text(json.dumps(data))
    if mac:
        (root / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleShortVersionString": version}))
    return root


def test_locate_and_version(tmp_path):
    loc = C.locate(make_install(tmp_path))
    assert loc["version"] == "9.9" and loc["resource_dir"].name == "Resources"


def test_locate_picks_newest(tmp_path):
    old = make_install(tmp_path / "a", "3.9")
    new = make_install(tmp_path / "b", "3.51")
    versions = [C.read_version(r, C.find_resource_dir(r)) for r in (old, new)]
    assert max(versions, key=C._vkey) == "3.51"  # numeric, not lexical


def test_version_from_folder_name_when_no_plist(tmp_path):
    root = make_install(tmp_path, mac=False, name="HX Edit 3.70")
    assert C.locate(root)["version"] == "3.70"


def test_missing_install_raises_with_guidance(tmp_path):
    with pytest.raises(C.CatalogError, match="--hxedit-path"):
        C.locate(tmp_path / "nope")


def test_build_catalog(tmp_path):
    loc = C.locate(make_install(tmp_path))
    cat = C.build_catalog(loc["resource_dir"], loc["version"])
    m = cat["models"]
    assert set(m) == {"HD2_DistFake", "HD2_DistOnlyBig", "HD2_AmpFake", "HD2_GateFake"}
    d = m["HD2_DistFake"]
    assert d["category"] == "drive" and d["load"] == 4.0 and d["load_stereo"] == 8.0
    assert d["params"]["Gain"]["kind"] == "continuous"
    assert d["params"]["Mode"]["kind"] == "stepped"
    assert d["params"]["@enabled"]["kind"] == "bool"
    assert d["params"]["Scale"]["stereo_only"] is True
    assert m["HD2_AmpFake"]["category"] == "amp" and m["HD2_AmpFake"]["cablink"] == "HD2_CabFake"
    assert m["HD2_GateFake"]["category"] == "other"  # gate is not the template's comp slot
    assert d["stomp"] is True and m["HD2_DistOnlyBig"]["stomp"] is False
    assert m["HD2_AmpFake"]["stomp_min_firmware"] == "0x03190100"
    assert "@tempo" in cat["globals"]
    assert cat["_source"]["hxedit_version"] == "9.9"
    s = C.summarize(cat)
    assert s["models"] == 4 and s["stomp_models"] == 3 and s["per_category"]["drive"] == 2


def test_cache_roundtrip_and_log(tmp_path):
    loc = C.locate(make_install(tmp_path))
    cat = C.build_catalog(loc["resource_dir"], loc["version"])
    out = tmp_path / "cache" / "catalog"
    paths = C.write_cache(cat, out)
    assert {p.name for p in paths} == {"9.9.json", "current.json"}
    assert C.load_catalog(cache_dir=out) == cat
    assert "version=9.9" in (out / "IMPORTS.log").read_text()


def test_load_missing_cache(tmp_path):
    with pytest.raises(C.CatalogError, match="catalog-import"):
        C.load_catalog(cache_dir=tmp_path)


def test_crosscheck(tmp_path):
    loc = C.locate(make_install(tmp_path))
    cat = C.build_catalog(loc["resource_dir"], loc["version"])
    def hlx(blocks):
        p = tmp_path / "t.hlx"
        p.write_text(json.dumps({"data": {"tone": {"dsp0": blocks}}}))
        return p
    ok = hlx({"inputA": {"@model": "HD2_AppDSPFlow1Input", "decay": 0.5},
              "block0": {"@model": "HD2_DistFake", "@enabled": True, "Gain": 0.3}})
    assert C.crosscheck_hlx(cat, [ok]) == []
    bad = hlx({"block0": {"@model": "HD2_Nope"},
               "block1": {"@model": "HD2_DistFake", "Bogus": 1}})
    msgs = C.crosscheck_hlx(cat, [bad])
    assert len(msgs) == 2 and "unknown model HD2_Nope" in msgs[0] and "Bogus" in msgs[1]
