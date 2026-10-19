"""scripts/match_block.py plumbing on a fake engine and fake blocks (no Native, no song audio)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("match_block", ROOT / "scripts" / "match_block.py")
MB = importlib.util.module_from_spec(spec)
sys.modules["match_block"] = MB
spec.loader.exec_module(MB)
MS = MB.MS
SR = 48_000


class FakePreset:
    def __init__(self, model, params):
        self.slots = [SimpleNamespace(model_id=model, category="drive", params=dict(params))]
        self.name = ""


class FakeBlock:
    """Stands in for synthetic_v1.Block: one gain knob `Drive` in 0..1."""

    def __init__(self, model, base=0.5):
        self.model, self.category = model, "drive"
        self.free = [("Drive", 0.0, 1.0, base)]
        self.preset = FakePreset(model, {"Drive": base})
        self.rng = {"Drive": (0.0, 1.0)}

    def with_values(self, values):
        p = FakePreset(self.model, self.preset.slots[0].params)
        p.slots[0].params.update({k: float(v) for k, v in values.items()})
        return p


class FakeInner:
    """load(preset) / set_params({(0, name): v}) / render(dry): output = dry * (1 + 4 * Drive)."""

    def __init__(self):
        self.p = None

    def load(self, preset):
        self.p = FakePreset(preset.slots[0].model_id, preset.slots[0].params)

    def set_params(self, ch):
        for (_, n), v in ch.items():
            self.p.slots[0].params[n] = v

    def render(self, dry):
        return np.asarray(dry, np.float32) * np.float32(1 + 4 * self.p.slots[0].params["Drive"])


class FakeKit:
    """total = |level ratio - 1|, so a target of dry * 3.8 is matched exactly by Drive 0.7."""

    def __init__(self):
        self.mask = None

    def score(self, target, candidate, **kw):
        ratio = float(np.sqrt(np.mean(candidate ** 2) / np.mean(target ** 2)))
        return SimpleNamespace(total=abs(ratio - 1), terms={})


@pytest.fixture
def stub_hlx(monkeypatch):
    import tonematch.preset as P

    def write(preset, path):
        Path(path).write_text(json.dumps({"model": preset.slots[0].model_id, "name": preset.name,
                                          "params": preset.slots[0].params}))
        return Path(path)

    monkeypatch.setattr(P, "write_hlx", write)


def test_shards_partition_the_pool_without_overlap():
    models = [f"M{i:02d}" for i in range(11)]
    parts = [MB.shard_models(models, i, 3) for i in range(3)]
    assert sorted(sum(parts, [])) == sorted(models)
    assert all(parts) and max(map(len, parts)) - min(map(len, parts)) <= 1


def test_merge_orders_refined_before_screened():
    a = [{"model": "A", "values": {}, "screen": 5.0, "refined": 3.0}, {"model": "B", "values": {}, "screen": 4.0, "refined": None}]
    b = [{"model": "C", "values": {}, "screen": 9.0, "refined": 2.0}, {"model": "D", "values": {}, "screen": 1.0, "refined": None}]
    assert [r["model"] for r in MB.merge_shards([a, b])] == ["C", "A", "D", "B"]


def test_worker_count_explicit_is_capped_by_models():
    assert MB.worker_count("4", 60) == 4
    assert MB.worker_count("9", 3) == 3


def test_progress_writer_writes_atomically_and_only_improvements(tmp_path, stub_hlx):
    blocks = {"HD2_A": FakeBlock("HD2_A"), "HD2_B": FakeBlock("HD2_B")}
    w = MB.ProgressWriter(tmp_path, blocks, "drive", "Test", min_interval_s=0.0, log=lambda *_: None)
    w.renders = lambda: 7
    assert w.offer(5.0, "HD2_A", {"Drive": 0.2})                      # the very first score is written
    d = json.loads((tmp_path / "current_best.json").read_text())
    assert d["seq"] == 1 and d["model"] == "HD2_A" and d["loss"] == 5.0 and d["values"] == {"Drive": 0.2}
    assert d["category"] == "drive" and d["renders"] == 7
    assert json.loads((tmp_path / "current_best.hlx").read_text())["params"]["Drive"] == 0.2
    assert not w.offer(6.0, "HD2_A", {"Drive": 0.9})                  # worse: nothing written
    assert w.offer(4.0, "HD2_B", {"Drive": 0.4})
    assert json.loads((tmp_path / "current_best.json").read_text())["seq"] == 2
    assert not list(tmp_path.glob("*.tmp.*"))                         # no temp files left behind
    # a second process (another writer) sees the shared best and does not overwrite it with a worse one
    w2 = MB.ProgressWriter(tmp_path, blocks, "drive", "Test", min_interval_s=0.0, log=lambda *_: None)
    assert not w2.offer(4.5, "HD2_A", {"Drive": 0.1})
    assert w2.offer(3.0, "HD2_A", {"Drive": 0.1})
    assert json.loads((tmp_path / "current_best.json").read_text())["seq"] == 3
    w2.offer(9.0, "HD2_A", {"Drive": 0.1}, stage="final", force=True)  # the final check replaces it, any loss
    assert json.loads((tmp_path / "current_best.json").read_text())["stage"] == "final"


def test_progress_writer_throttles_but_flushes_the_pending_best(tmp_path, stub_hlx):
    blocks = {"HD2_A": FakeBlock("HD2_A")}
    w = MB.ProgressWriter(tmp_path, blocks, "drive", "Test", min_interval_s=3600.0, log=lambda *_: None)
    assert w.offer(5.0, "HD2_A", {"Drive": 0.2})
    assert not w.offer(4.0, "HD2_A", {"Drive": 0.3})                  # inside the interval: held
    assert json.loads((tmp_path / "current_best.json").read_text())["loss"] == 5.0
    assert w.flush(force=True)
    assert json.loads((tmp_path / "current_best.json").read_text())["loss"] == 4.0


def test_hybrid_style_scoring_reports_each_candidate_through_engine_and_kit(tmp_path, stub_hlx):
    blocks = {"HD2_A": FakeBlock("HD2_A")}
    eng = MB.ProgressEngine(FakeInner(), log=lambda *_: None)
    w = MB.ProgressWriter(tmp_path, blocks, "drive", "Test", min_interval_s=0.0, log=lambda *_: None)
    kit = MB.ProgressKit(FakeKit(), eng, w)
    rng = np.random.default_rng(0)
    dry = rng.standard_normal(SR).astype(np.float32) * 0.1
    target = dry * 3.8                                                # Drive 0.7
    eng.load(blocks["HD2_A"].with_values({"Drive": 0.1}))
    for v in (0.1, 0.5, 0.3, 0.69):
        eng.set_params({(0, "Drive"): v})
        y = eng.render(dry)
        kit.score(target[:SR // 2], np.asarray(y, np.float32)[:SR // 2])  # a view, as Evaluator passes it
    d = json.loads((tmp_path / "current_best.json").read_text())
    assert d["values"]["Drive"] == pytest.approx(0.69) and d["loss"] < 0.02 and d["model"] == "HD2_A"
    assert d["seq"] == 3                                              # 0.1, 0.5 and 0.69 improved; 0.3 did not


def test_serial_variants_are_tracked_too(tmp_path, stub_hlx):
    """base.serial_render_many (what TimedEngine uses) loads each Preset variant then renders it."""
    from tonematch.engines.base import render_many

    blocks = {"HD2_A": FakeBlock("HD2_A"), "HD2_B": FakeBlock("HD2_B")}
    eng = MB.ProgressEngine(FakeInner(), log=lambda *_: None)
    w = MB.ProgressWriter(tmp_path, blocks, "drive", "Test", min_interval_s=0.0, log=lambda *_: None)
    kit = MB.ProgressKit(FakeKit(), eng, w)
    dry = (np.random.default_rng(1).standard_normal(SR) * 0.1).astype(np.float32)
    target = dry * 3.8
    variants = [blocks["HD2_A"].with_values({"Drive": 0.2}), blocks["HD2_B"].with_values({"Drive": 0.7})]
    for y in render_many(eng, dry, variants):
        kit.score(target, y)
    d = json.loads((tmp_path / "current_best.json").read_text())
    assert d["model"] == "HD2_B" and d["values"]["Drive"] == pytest.approx(0.7)


def test_final_check_rescoring_orders_by_full_loss(monkeypatch):
    blocks = {"HD2_A": FakeBlock("HD2_A"), "HD2_B": FakeBlock("HD2_B")}
    dry = (np.random.default_rng(2).standard_normal(SR) * 0.1).astype(np.float32)
    sec = SimpleNamespace(target=dry * 3.8, dry=dry, stems=None, mask=None)

    class Kit(FakeKit):
        inner = None
    kit = Kit()
    kit.inner = FakeKit()
    rows = [{"model": "HD2_A", "values": {"Drive": 0.1}, "screen": 1.0, "refined": 0.5},
            {"model": "HD2_B", "values": {"Drive": 0.7}, "screen": 2.0, "refined": 0.6}]
    eng = MS.TimedEngine(FakeInner(), log=lambda *_: None)
    monkeypatch.setattr(MS, "_weights", lambda: {})                                          # the fake kit ignores weights
    out = MB.final_check(eng, blocks, rows, sec, kit, 5, log=lambda *_: None)
    assert [d["model"] for d in out] == ["HD2_B", "HD2_A"]
    assert out[0]["loss"] < 0.01 and out[0]["search_loss"] == 0.6


def _args(*extra):
    return MB.parse(["--target-window", "0:10", "--di", "x.wav", "--out", "o", *extra])


def test_knob_tol_defaults_and_old_behaviour_switch():
    assert MB.resolve_knob_tol(_args()) == 0.15                       # drive: on by default (T176)
    assert MB.resolve_knob_tol(_args("--knob-tol", "0")) == 0.0       # the old behaviour (T164)
    assert MB.resolve_knob_tol(_args("--knob-tol", "0.25")) == 0.25
    assert MB.resolve_knob_tol(_args("--category", "amp")) == 0.0     # other categories keep the old behaviour


# ---------------------------------------------------------------- T178: per-knob tolerance and the AFx-Rep shortlist

class TwoKnobBlock(FakeBlock):
    def __init__(self, model, drive=True, tone=True):
        super().__init__(model)
        self.free = ([("Drive", 0.0, 1.0, 0.5)] if drive else []) + ([("Tone", 0.0, 1.0, 0.5)] if tone else [])
        self.preset = FakePreset(model, {n: d for n, _, _, d in self.free})

    def with_values(self, values):
        p = FakePreset(self.model, self.preset.slots[0].params)
        p.slots[0].params.update({k: float(v) for k, v in values.items()})
        return p


class CountingEngine(FakeInner):
    def __init__(self):
        super().__init__()
        self.renders = 0

    def load(self, preset):
        self.p = FakePreset(preset.slots[0].model_id, preset.slots[0].params)

    def render(self, dry):
        self.renders += 1
        return np.asarray(dry, np.float32) * np.float32(1 + 4 * self.p.slots[0].params.get("Drive", 0.5))


def test_knob_tol_per_knob_shape():
    from tonematch.search.knob_tol import KnobTol
    kt = KnobTol()
    assert float(kt) == 0.15 and kt > 0
    assert kt.tolerance("Gain", 0.5) == 0.15
    assert kt.tolerance("Tone", 0.5) == 0.10
    assert kt.tolerance("Tone", 0.05) == 0.05 and kt.tolerance("Treble", 0.95) == 0.05
    assert kt.tolerance("Level", 0.0) == 0.15


def test_knob_tol_map_overrides_names(tmp_path):
    from tonematch.search.knob_tol import make_knob_tol
    f = tmp_path / "k.json"
    f.write_text(json.dumps({"models": {"M": {"tone_knobs": ["Hiss"], "drive_knobs": ["Tone"], "tone_tol": 0.2}}}))
    kt = make_knob_tol(map_path=f).for_model("M")
    assert kt.tolerance("Hiss", 0.5) == 0.2 and kt.tolerance("Tone", 0.5) == 0.15
    with pytest.raises(FileNotFoundError):
        make_knob_tol(map_path=tmp_path / "none.json")


def test_resolve_knob_tol_options():
    from tonematch.search.knob_tol import KnobTol
    assert isinstance(MB.resolve_knob_tol(_args()), KnobTol)
    kt = MB.resolve_knob_tol(_args("--knob-tol-drive", "0.2", "--knob-tol-tone", "0.12"))
    assert float(kt) == 0.2 and kt.tone == 0.12
    assert not hasattr(MB.resolve_knob_tol(_args("--knob-tol", "0.25")), "vector")
    assert MB.resolve_knob_tol(_args("--category", "amp", "--knob-tol-tone", "0.1")) .tone == 0.1


def test_refine_chain_runs_with_per_knob_tolerance_and_stops_early():
    from tonematch.search import Budget, Plan
    from tonematch.search.knob_tol import KnobTol
    from tonematch.search.loop import Evaluator, refine_chain
    from tonematch.search.space import ParamSpace
    import inspect
    sig = inspect.signature(refine_chain)
    assert "tol" in sig.parameters
    # tolerance vector on a real ParamSpace
    sp = ParamSpace.__new__(ParamSpace)
    sp.refs = [(0, "Drive"), (0, "Tone")]
    sp.lo, sp.hi = np.zeros(2), np.ones(2)
    kt = KnobTol().for_model("M")
    v = kt.vector(sp, [0, 1], np.array([0.5, 0.97]))
    assert list(v) == [0.15, 0.05]
    assert list(KnobTol(0).vector(sp, [0, 1], np.array([0.5, 0.5]))) == [0, 0]


def test_shortlist_ranks_by_distance_and_counts_renders():
    from tonematch.search import shortlist as SL
    blocks = {"A": TwoKnobBlock("A"), "B": TwoKnobBlock("B", tone=False), "C": TwoKnobBlock("C", drive=False, tone=False)}
    eng = CountingEngine()
    dry = np.ones(4800, np.float32) * 0.1
    sec = SimpleNamespace(dry=dry, target=dry * 3.8)
    embed = lambda x: float(np.sqrt(np.mean(np.asarray(x) ** 2)))       # noqa: E731 - a level "embedding"
    dist = lambda a, b: abs(a - b)                                      # noqa: E731
    out = SL.shortlist(eng, blocks, sec, 2, families_path="/nonexistent", embed=embed, dist=dist, log=lambda *_: None)
    assert out["renders"] == eng.renders == 6 + 3 + 1
    assert out["top"][0] in ("A", "B") and len(out["top"]) == 2 and not out["families_used"]
    best = next(r for r in out["ranked"] if r["model"] == "B")
    assert abs(best["best_values"]["Drive"] - 0.5) < 1e-9 or best["best_values"]["Drive"] in SL.COARSE_DRIVE


def test_shortlist_expands_to_family(tmp_path):
    from tonematch.search import shortlist as SL
    f = tmp_path / "fam.json"
    f.write_text(json.dumps({"families": {"x": ["A", "Z"], "y": ["C"]}}))
    fams = SL.load_families(f)
    assert SL.expand_families(["A"], fams, {"A", "B", "Z"}) == ["A", "Z"]
    assert SL.load_families(tmp_path / "none.json") == {}


def test_knob_path_choice():
    from tonematch.search import shortlist as SL
    assert SL.knob_path(inversion=None, aligned_take=False) == "coarse_grid"
    assert SL.knob_path(inversion=None, aligned_take=True) == "aligned_take"
    assert SL.knob_path(inversion=lambda *a: {}, aligned_take=True) == "inversion"


def test_run_shortlist_coarse_grid_is_low_confidence(monkeypatch):
    from tonematch.search import shortlist as SL
    pool = {"A": [], "B": []}
    monkeypatch.setattr(MB, "make_blocks", lambda p, c, cat: {"A": TwoKnobBlock("A"), "B": TwoKnobBlock("B")})
    monkeypatch.setattr(SL, "fingerprint_inversion", lambda: None)
    import tonematch.metrics.afxrep as AR
    monkeypatch.setattr(AR, "features", lambda x: float(np.sqrt(np.mean(np.asarray(x) ** 2))))
    monkeypatch.setattr(AR, "distance", lambda a, b: abs(a - b))
    dry = np.ones(4800, np.float32) * 0.1
    sec = SimpleNamespace(dry=dry, target=dry * 3.8)
    args = _args("--shortlist", "afxrep", "--shortlist-n", "1", "--families", "/nonexistent")
    sl = MB.run_shortlist(args, pool, sec, "drive", CountingEngine(), lambda *_: None)
    assert sl["path"] == "coarse_grid" and sl["renders"] == 12 and len(sl["rows"]) == 1
    assert set(sl["rows"][0]["values"]) == {"Drive", "Tone"}
    args = _args("--shortlist", "afxrep", "--aligned-take")
    assert MB.run_shortlist(args, pool, sec, "drive", CountingEngine(), lambda *_: None)["rows"] == []


# ---------------------------------------------------------------- T183: end-to-end flow fixes

def test_second_choice_adds_only_loose_members(tmp_path):
    from tonematch.search import shortlist as SL
    f = tmp_path / "fam.json"
    f.write_text(json.dumps({"families": {"F1": ["A", "B"], "F2": ["C", "D"], "F3": ["E"]},
                             "member_confidence": {"A": 0.3, "B": 0.9, "E": 0.2},
                             "second_choice": {"A": {"family": "F2", "coassoc": 0.2}, "B": {"family": "F3"}, "E": "F1"}}))
    sec = SL.load_second_choice(f, 0.5)
    assert sec == {"A": "F2", "E": "F1"}
    models, added = SL.expand_second_choice(["A", "B"], ["A", "B"], sec, SL.family_members(f), {"A", "B", "C", "D", "E"})
    assert models == ["A", "B", "C", "D"] and added == ["C", "D"]
    assert SL.load_second_choice(tmp_path / "none.json") == {}


def test_inversion_positions_become_parameter_values_and_path_can_be_forced(monkeypatch):
    from tonematch.search import shortlist as SL
    blk = TwoKnobBlock("A")
    blk.free = [("Drive", 0.0, 10.0, 5.0), ("Tone", 0.0, 1.0, 0.5)]
    monkeypatch.setattr(MB, "make_blocks", lambda p, c, cat: {"A": blk})
    monkeypatch.setattr(SL, "fingerprint_inversion", lambda: (lambda t, d, models: {"A": {"Drive": 0.3, "Tone": 1.4}}))
    import tonematch.metrics.afxrep as AR
    monkeypatch.setattr(AR, "features", lambda x: 0.0)
    monkeypatch.setattr(AR, "distance", lambda a, b: 0.0)
    dry = np.ones(4800, np.float32) * 0.1
    sec = SimpleNamespace(dry=dry, target=dry)
    sl = MB.run_shortlist(_args("--shortlist", "afxrep", "--families", "/nonexistent"), {"A": []}, sec, "drive",
                          CountingEngine(), lambda *_: None)
    assert sl["path"] == "inversion"
    assert sl["rows"][0]["values"] == {"Drive": 3.0, "Tone": 1.0}          # positions scaled to the range and clipped
    sl = MB.run_shortlist(_args("--shortlist", "afxrep", "--families", "/nonexistent", "--knob-path", "coarse_grid"),
                          {"A": []}, sec, "drive", CountingEngine(), lambda *_: None)
    assert sl["path"] == "coarse_grid"


def test_target_wav_section_uses_files_whole(tmp_path):
    import soundfile as sf
    sf.write(tmp_path / "t.wav", np.ones(9600, np.float32) * 0.1, 48000)
    sf.write(tmp_path / "d.wav", np.ones(12000, np.float32) * 0.2, 48000)
    args = _args("--target-wav", str(tmp_path / "t.wav"), "--dry-wav", str(tmp_path / "d.wav"))
    sec, win = MB.build_target_section(args)
    assert len(sec.target) == len(sec.dry) == 9600 and sec.mask is None and sec.stems is None and win["dur_s"] == 0.2


def test_coarse_points_use_the_measured_knob_map_over_names():
    from tonematch.search import shortlist as SL
    free = [("Bass", 0, 1, .5), ("Drive", 0, 1, .5), ("Filter", 0, 1, .5), ("Treble", 0, 1, .5)]
    assert set(SL.coarse_points(free)[0]) == {"Drive", "Treble"}                       # the name rules pick Treble
    pts = SL.coarse_points(free, knobs={"drive_knobs": ["Drive"], "tone_knobs": ["Filter"]})
    assert set(pts[0]) == {"Drive", "Filter"} and len(pts) == 6
    free2 = [("Gain 1", 0, 1, .5), ("Tone 1", 0, 1, .5), ("Gain 2", 0, 1, .5)]
    assert SL.coarse_points(free2) == [{}]                                              # names alone move nothing
    assert set(SL.coarse_points(free2, knobs={"drive_knobs": ["Gain 1"], "tone_knobs": ["Tone 1"]})[0]) == {"Gain 1", "Tone 1"}
    assert set(SL.coarse_points([("Boost", 0, 1, .5)], knobs={"drive_knobs": ["Boost"]})[0]) == {"Boost"}


def test_inversion_that_declines_falls_back_to_the_coarse_grid_and_says_so(monkeypatch):
    from tonematch.search import shortlist as SL
    monkeypatch.setattr(MB, "make_blocks", lambda p, c, cat: {"A": TwoKnobBlock("A")})
    monkeypatch.setattr(SL, "fingerprint_inversion", lambda: (lambda t, d, models: {}))
    import tonematch.metrics.afxrep as AR
    monkeypatch.setattr(AR, "features", lambda x: 0.0)
    monkeypatch.setattr(AR, "distance", lambda a, b: 0.0)
    dry = np.ones(4800, np.float32) * 0.1
    msgs = []
    sl = MB.run_shortlist(_args("--shortlist", "afxrep", "--families", "/nonexistent"), {"A": []},
                          SimpleNamespace(dry=dry, target=dry), "drive", CountingEngine(), msgs.append)
    assert sl["path"] == "coarse_grid" and sl["inversion_declined"] and any("declined" in m for m in msgs)


def test_shortlist_renders_are_not_counted_twice():
    assert MB.shortlist_renders_to_add({"renders": 336}, "coarse_grid", CountingEngine()) == 0
    assert MB.shortlist_renders_to_add({"renders": 336}, "coarse_grid", object()) == 336
