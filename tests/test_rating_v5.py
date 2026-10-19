"""rating_v5 (T188): build with a fake renderer, check the design, the hidden key and the analysis."""
import json
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import rating_v5 as R5

pytestmark = pytest.mark.skipif(not R5.PRED.exists(), reason="T185 predictions not present")


def fake_render(model, params, dry):
    g = 1 + (zlib.crc32((model + json.dumps(params, sort_keys=True)).encode()) % 1000) / 100
    return np.tanh(np.asarray(dry) * g).astype(np.float32)


fake_render.defaults = lambda m: {"Drive": 0.5}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("r5")
    f, k = R5.build_session("t5", seed=3, render=fake_render, out_root=d / "pages", key_root=d / "key",
                            usable=lambda m: True, log=lambda *a: None)
    return f, json.loads(k.read_text())


def test_design(built):
    f, key = built
    tr = key["trials"]
    kinds = [t["kind"] for t in tr]
    assert kinds.count("regular") == 24 and kinds.count("repeat") == 2 and len([k for k in kinds if k.startswith("sanity")]) == 2
    regs = [t for t in tr if t["kind"] == "regular"]
    assert sum(t["condition"] == "blind" for t in regs) == 18 and len({t["w"] for t in regs}) == 24
    for t in regs:
        assert {t["A"]["cand_kind"], t["B"]["cand_kind"]} in ({"afx", "prov"}, {"afx", "alt"})
        assert t["A"]["model"] != t["B"]["model"] or t["A"]["params"] != t["B"]["params"]
    assert len(list(f.glob("*.wav"))) == 3 * len(tr)
    assert "afx" not in (f / "index.html").read_text().lower().replace("afx-", "")
    assert not list(f.glob("*.json"))[1:]  # only trials.json: the key is not in the page folder


def test_audio_sources():
    with pytest.raises(ValueError):
        R5.source_of("data/songs/x/stems/guitar.wav")
    assert R5.source_of("data/di/Guitar-TECHS/P1_chords/audio/directinput/a.wav") == "techs"


def test_analyse(built):
    _, key = built
    rng = np.random.default_rng(0)
    byid, sc = {}, {}
    for t in key["trials"]:
        if t["kind"] in ("regular", "repeat"):
            sa = {"A": float(rng.random()), "B": float(rng.random())}
            sc[t["id"]] = {"A": {"mrstft_v2": sa["A"]}, "B": {"mrstft_v2": sa["B"]}}
        byid[t["id"]] = {"choice": "A", "rating_a": 4, "rating_b": 2}
    out = R5.analyse(key, byid, sc)
    assert out["closer"]["all"]["n"] == 26 and "cond:blind" in out["closer"]
    assert out["agree"]["all"]["mrstft_v2"]["n"] == 26
    assert "Mean 1-5" in R5.fmt_extras(out)
