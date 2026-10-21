"""T196 predictor-first drive matcher: fake head, fake blocks, fake engine (no Native, no AFx-Rep weights, no audio)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from tonematch.search import predictor_first as PF

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class Preset:
    def __init__(self, model, params):
        self.slots = [SimpleNamespace(model_id=model, params=dict(params))]


class Block:
    def __init__(self, model, tone=True):
        self.model = model
        self.free = [("Drive", 0.0, 10.0, 5.0)] + ([("Tone", 0.0, 10.0, 5.0)] if tone else []) + [("Level", 0.0, 10.0, 5.0)]
        self.preset = Preset(model, {n: d for n, _, _, d in self.free})

    def with_values(self, v):
        p = Preset(self.model, self.preset.slots[0].params)
        p.slots[0].params.update(v)
        return p


class Engine:
    """Output level = 1 + Drive, so the 'embedding' (RMS) tells the drive; Tone does nothing."""

    def __init__(self):
        self.renders, self.p = 0, None

    def load(self, p):
        self.p = p

    def render(self, dry):
        self.renders += 1
        return np.asarray(dry, np.float32) * np.float32(1 + self.p.slots[0].params["Drive"])


class FakeHead:
    """Three models: A p=.6, B p=.3, C p=.1; knob means and sigmas set per model (C has no Tone knob)."""
    models = ["A", "B", "C", "D"]

    def predict(self, crops, allowed=None):
        p = np.array([0.6, 0.3, 0.1, 0.0])
        if allowed is not None:
            p = p * np.array([m in set(allowed) for m in self.models])
            p = p / p.sum()
        mu = np.array([[0.4, 0.5], [0.7, 0.2], [0.3, np.nan], [np.nan, np.nan]])
        sig = np.array([[0.1, 0.1], [0.02, 0.2], [0.1, np.nan], [np.nan, np.nan]])
        return PF.Prediction(self.models, p, mu, sig, 1.0)


KNOBS = {m: {"drive_knobs": ["Drive"], "tone_knobs": ["Tone"]} for m in "ABCD"}


def test_select_models_covers_probability_and_caps():
    pred = FakeHead().predict(None)
    assert PF.select_models(pred, cum=0.5) == ["A"]
    assert PF.select_models(pred, cum=0.95) == ["A", "B", "C"]
    assert PF.select_models(pred, cum=0.99, max_models=2) == ["A", "B"]
    assert PF.select_models(pred, allowed={"B", "C"}, cum=0.95) == ["B", "C"]


def test_knob_points_two_knobs_drive_only_clipping_floor_and_dedupe():
    pts = PF.knob_points([0.4, 0.5], [0.1, 0.2])
    assert [p["label"] for p in pts] == ["mean", "d-1s", "d+1s", "t-1s", "t+1s"]
    assert abs(pts[1]["drive"] - 0.3) < 1e-9 and pts[1]["tone"] == 0.5
    assert abs(pts[4]["tone"] - 0.7) < 1e-9
    one = PF.knob_points([0.3, np.nan], [0.1, np.nan])
    assert len(one) == 5 and all(p["tone"] is None for p in one)
    edge = PF.knob_points([0.0, 1.0], [0.1, 0.1])
    assert len(edge) == 3 and min(p["drive"] for p in edge) == 0.0 and max(p["tone"] for p in edge) == 1.0
    tiny = PF.knob_points([0.5, 0.5], [0.0, 0.0])
    assert abs(tiny[2]["drive"] - 0.55) < 1e-9                       # sigma floor 0.05
    assert PF.knob_points([np.nan, np.nan], [np.nan, np.nan]) == [{"label": "default", "drive": None, "tone": None}]


def test_values_for_maps_positions_to_ranges_and_leaves_level_alone():
    b = Block("A")
    v = PF.values_for(b, KNOBS["A"], 0.25, 0.5)
    assert v == {"Drive": 2.5, "Tone": 5.0, "Level": 5.0}
    assert PF.values_for(Block("C", tone=False), KNOBS["C"], 0.5, 0.9)["Drive"] == 5.0   # no Tone knob: ignored


def test_run_predictor_first_renders_few_and_picks_by_distance():
    blocks = {m: Block(m, tone=(m != "C")) for m in "ABCD"}
    eng = Engine()
    dry = np.ones(4800, np.float32) * 0.1
    # target made with model B at Drive 0.7 -> level 1 + 7 = 8
    sec = SimpleNamespace(dry=dry, target=dry * 8.0)
    embed = lambda x: float(np.sqrt(np.mean(np.asarray(x) ** 2)))     # noqa: E731
    dist = lambda a, b: abs(a - b)                                    # noqa: E731
    r = PF.run_predictor_first(eng, blocks, sec, FakeHead(), KNOBS, embed_crops=lambda x: np.zeros((3, 4)),
                               embed=embed, dist=dist, log=lambda *_: None)
    assert r["selected"] == ["A", "B", "C"]
    assert r["renders"] == eng.renders == 5 + 5 + 5
    assert r["top"][0] == "B"                                          # AFx-style distance overrules the predictor's order
    assert r["predictor_own"]["model"] == "A" and r["predictor_own"]["values"]["Drive"] == 4.0
    assert r["rows"][0]["model"] == "B" and abs(r["rows"][0]["values"]["Drive"] - 7.0) < 1e-9
    assert [c["label"] for c in r["candidates"] if c["model"] == "C"][:2] == ["mean", "d-1s"]
    assert set(r["p_selected"]) == {"A", "B", "C"}


def test_allowed_models_mask_excluded_models():
    blocks = {m: Block(m) for m in "ABCD"}
    sec = SimpleNamespace(dry=np.ones(100, np.float32), target=np.ones(100, np.float32))
    r = PF.run_predictor_first(Engine(), blocks, sec, FakeHead(), KNOBS, embed_crops=lambda x: np.zeros((3, 4)),
                               embed=lambda x: 1.0, dist=lambda a, b: 0.0, allowed=["B", "C"], log=lambda *_: None)
    assert "A" not in r["selected"] and r["predictor_own"]["model"] == "B"


def test_target_crops_shapes():
    x = np.arange(480000, dtype=np.float32)
    cs = PF.target_crops(x)
    assert len(cs) == 3 and all(len(c) == 160000 for c in cs) and cs[1][0] == 160000
    assert all(len(c) == 160000 for c in PF.target_crops(x[:100000]))
    assert all(len(c) == 160000 for c in PF.target_crops(np.zeros(900000, np.float32)))


def test_match_block_predictor_option(monkeypatch):
    spec = importlib.util.spec_from_file_location("match_block", ROOT / "scripts" / "match_block.py")
    MB = importlib.util.module_from_spec(spec)
    sys.modules["match_block"] = MB
    spec.loader.exec_module(MB)
    a = MB.parse(["--target-wav", "t.wav", "--dry-wav", "d.wav", "--out", "o", "--shortlist", "predictor"])
    assert a.shortlist == "predictor" and a.predictor_cum == 0.95 and a.predictor_max_models == 6
    assert MB.parse(["--target-wav", "t.wav", "--dry-wav", "d.wav", "--out", "o"]).shortlist == "none"   # not the default
    blocks = {m: Block(m, tone=(m != "C")) for m in "ABCD"}
    monkeypatch.setattr(MB, "make_blocks", lambda p, c, cat: blocks)
    monkeypatch.setattr(PF.Head, "load", classmethod(lambda cls, path=None: FakeHead()))
    monkeypatch.setattr(PF, "embed_crops_afxrep", lambda x: np.zeros((3, 4)))
    import tonematch.metrics.afxrep as AR
    monkeypatch.setattr(AR, "features", lambda x: float(np.sqrt(np.mean(np.asarray(x) ** 2))))
    monkeypatch.setattr(AR, "distance", lambda a, b: abs(a - b))
    import tonematch.search.knob_tol as KT
    monkeypatch.setattr(KT, "load_sensitivity", lambda p=None: (KNOBS, "fake"))
    dry = np.ones(4800, np.float32) * 0.1
    sec = SimpleNamespace(dry=dry, target=dry * 8.0)
    eng = Engine()
    sl = MB.run_predictor_shortlist(a, {m: [] for m in "ABCD"}, sec, "drive", eng, lambda *_: None)
    assert sl["path"] == "predictor" and sl["renders"] == 15 and sl["top"][0] == "B" and len(sl["rows"]) == 3
    assert sl["ranked"][0]["model"] == "B"
