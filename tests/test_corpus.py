"""Corpus statistics and conditional prior on synthetic presets (no Line 6 data)."""
import base64
import json
import math
import zlib

import pytest

from tonematch import chain_prior as CP
from tonematch import corpus as C

M = {"drive": "HD2_DistScream808", "amp": "HD2_AmpUSDeluxeNrm", "delay": "HD2_DelayTransistorTape",
     "reverb": "HD2_ReverbRoom", "mod": "HD2_ChorusTrinityChorus", "comp": "HD2_CompressorDeluxeComp"}


def hlx(cats, device=2162694, version=0x03110000, name="p", author="a", drive=0.5):
    d = {"inputA": {"@model": "HelixStomp_AppDSPFlowInput"}}
    for i, c in enumerate(cats):
        d[f"block{i}"] = {"@model": M[c], "@position": i, "@path": 0, "@enabled": True}
        if c == "amp":
            d[f"block{i}"]["Drive"] = drive
    return {"version": 6, "data": {"device": device, "device_version": version, "meta": {"name": name, "author": author},
                                   "tone": {"dsp0": d, "dsp1": {}}}}


def test_fw_string():
    assert C.fw_string(0x03110000) == "3.11" and C.fw_string(43122688) == "2.92" and C.fw_string(None) is None


def test_collapse_and_positions():
    assert C.collapse(["drive", "drive", "amp", "cab", "cab"]) == ["drive", "amp", "cab"]
    assert C.collapse(["amp", "cab", "preamp"], C.AMP_UNIT) == ["amp_unit"]
    ps = C.position_stats([["drive", "amp", "reverb"], ["comp", "amp", "delay", "reverb"]])
    assert ps["from_start"]["0"]["share"] == {"drive": 0.5, "comp": 0.5}
    assert ps["from_end"]["0"]["share"] == {"reverb": 1.0}
    assert ps["from_end"]["1"]["share"] == {"amp": 0.5, "delay": 0.5}


def test_run_lengths_and_pairs():
    rl = C.run_lengths([["drive", "drive", "amp"], ["drive", "amp"]])
    assert rl["drive"] == {"groups": 2, "size1": 1, "size2": 1, "size3plus": 0}
    seqs = [["drive", "amp", "delay", "reverb"]] * 20 + [["drive", "amp", "reverb", "delay"]] * 5
    pr = C.pair_stats(seqs, ["drive", "amp", "delay", "reverb"])
    p = pr["delay|reverb"]
    assert p["n"] == 25 and p["x_first"] == pytest.approx(0.8) and p["swap_index"] == pytest.approx(0.4)
    assert pr["drive|amp"]["swap_index"] == 0 and pr["drive|amp"]["adjacent"] == 1.0


def test_contiguity_lift():
    seqs = [["comp", "delay", "reverb", "drive", "amp"]] * 30
    c = C.contiguity(seqs, {"delay", "reverb"})
    assert c["contiguous"] == 1.0 and c["lift"] > 1.5
    c2 = C.contiguity([["delay", "amp", "reverb"]] * 10, {"delay", "reverb"})
    assert c2["contiguous"] == 0.0


def test_artist_crosswalk():
    x = C.artist_crosswalk(["AC DC", "Guns N Roses", "Metalica"], ["AC/DC", "Guns N' Roses", "Metallica", "Queen"])
    assert x["exact"]["AC DC"] == "AC/DC" and x["exact"]["Guns N Roses"] == "Guns N' Roses"
    assert x["fuzzy"]["Metalica"] == "Metallica" and x["list_not_in_folders"] == ["Queen"]


def test_mi_vs_shuffle_detects_dependence_and_independence():
    ctx = ["a"] * 100 + ["b"] * 100
    dep = ["x"] * 100 + ["y"] * 100
    assert C.mi_vs_shuffle(dep, ctx, perms=20)["frac_of_H"] > 0.9
    import random
    r = random.Random(1)
    ind = [r.choice("xy") for _ in range(200)]
    assert abs(C.mi_vs_shuffle(ind, ctx, perms=40)["frac_of_H"]) < 0.05
    cv = C.cv_lookup(dep, ctx)
    assert cv["acc_ctx"] > 0.95 and cv["gain_bits"] > 0.5


def test_scan_tree_setlist_encrypted_and_dedupe(tmp_path):
    (tmp_path / "Artist Patches" / "Band").mkdir(parents=True)
    (tmp_path / "Amps & Styles" / "80s").mkdir(parents=True)
    (tmp_path / "Miscellaneous").mkdir()
    chain = ["drive", "amp", "delay", "reverb"]
    (tmp_path / "Artist Patches" / "Band" / "one.hlx").write_text(json.dumps(hlx(chain, name="one")))
    (tmp_path / "Amps & Styles" / "80s" / "two.hlx").write_text(json.dumps(hlx(chain, name="dupe")))
    (tmp_path / "Amps & Styles" / "80s" / "three.hlx").write_text(json.dumps(hlx(["amp", "reverb"], drive=0.9, name="three")))
    (tmp_path / "Miscellaneous" / "bad.hlx").write_text("{not json")
    (tmp_path / "Miscellaneous" / "enc.hlx").write_text(json.dumps({"encryption": {"type": "x"}, "encoded_data": "zz"}))
    body = json.dumps({"meta": {}, "presets": [hlx(["amp"])["data"] | {"tone": hlx(["amp"])["data"]["tone"]}, {"meta": {}}]})
    raw = body.encode()
    (tmp_path / "Miscellaneous" / "set.hlx").write_text(json.dumps({
        "schema": "L6Setlist", "encoding": "Base64", "compression": {"type": "zlib", "crc32": zlib.crc32(raw),
                                                                       "decompressed_size": len(raw)},
        "encoded_data": base64.b64encode(zlib.compress(raw)).decode()}))
    recs = C.scan(tmp_path)
    kinds = sorted(r.kind for r in recs)
    assert kinds.count("hlx") == 4 and "setlist_member" in kinds and "encrypted" in kinds and "empty_slot" in kinds
    ctx = {r.name: r.ctx for r in recs if r.ok and r.kind == "hlx"}
    assert ctx["one"] == {"group": "artist", "artist": "Band"} and ctx["three"]["decade"] == "80s"
    kept, st = C.dedupe([r for r in recs if r.kind != "empty_slot"])
    assert st["exact_duplicates_removed"] == 1
    three = next(r for r in kept if r.name == "three")
    assert three.amp_drive == 0.9 and C.features(three)["gain"] == "high" and three.fits_stomp
    assert not next(r for r in recs if r.kind == "encrypted").ok


def _chains(n_a, n_b):
    a = [CP.extract_chain(hlx(["drive", "amp", "delay", "reverb"])) for _ in range(n_a)]
    b = [CP.extract_chain(hlx(["comp", "amp", "mod", "reverb"])) for _ in range(n_b)]
    return a, b


def test_conditional_prior_and_backoff():
    a, b = _chains(40, 40)
    ctx = [{"style": "metal"}] * 40 + [{"style": "clean"}] * 40
    pr = CP.build_prior(a + b, contexts=ctx, context_keys=("style",))
    assert pr["conditional"]["style"]["metal"]["n"] == 40
    seq = ["drive", "amp", "delay", "reverb"]
    g = CP.sequence_logprob(seq, pr)
    m = CP.sequence_logprob(seq, pr, {"style": "Metal"})            # case-insensitive
    c = CP.sequence_logprob(seq, pr, {"style": "clean"})
    assert m > g > c
    assert CP.sequence_logprob(seq, pr, {"style": "unseen"}) == pytest.approx(g)   # backoff
    assert CP.sequence_logprob(seq, pr, {"style": "metal", "decade": "80s"}) == pytest.approx(m)
    top = CP.top_structures({"drive", "amp", "delay", "reverb"}, k=1, prior=pr, context={"style": "metal"})
    assert top[0][0] == ("drive", "amp", "delay", "reverb")


def test_sparse_context_backs_off():
    a, b = _chains(40, 5)
    ctx = [{"style": "metal"}] * 40 + [{"style": "rare"}] * 5
    pr = CP.build_prior(a + b, contexts=ctx, context_keys=("style",))
    assert "rare" in pr["conditional"]["style"]          # stored (>= 5) ...
    seq = ["comp", "amp", "mod", "reverb"]
    assert CP.sequence_logprob(seq, pr, {"style": "rare"}) == pytest.approx(CP.sequence_logprob(seq, pr))  # ... not used (< 15)


def test_prior_without_conditional_ignores_context():
    a, _ = _chains(5, 0)
    pr = CP.build_prior(a)
    assert "conditional" not in pr
    assert CP.sequence_logprob(["amp"], pr, {"style": "metal"}) == pytest.approx(CP.sequence_logprob(["amp"], pr))
    assert math.isfinite(CP.sequence_logprob(["amp"], pr))
