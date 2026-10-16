"""scripts/match_song.py plumbing on a fake engine and synthetic stems (no Native, no song audio)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from scipy.signal import stft

ROOT = Path(__file__).resolve().parents[1]
KNOWN = ROOT / "fixtures" / "smoke" / "known.hlx"
# known.hlx is built from the user's own HX Stomp export (Line 6 content), so it is gitignored and
# absent in CI; tests that need it skip there and run locally.
needs_known = pytest.mark.skipif(not KNOWN.exists(), reason="fixtures/smoke/known.hlx is local only (gitignored)")
spec = importlib.util.spec_from_file_location("match_song", ROOT / "scripts" / "match_song.py")
MS = importlib.util.module_from_spec(spec)
sys.modules["match_song"] = MS  # dataclasses look the module up
spec.loader.exec_module(MS)

SR = MS.SR


def _song(seconds=16, seed=0):
    """Guitar bursts everywhere, the band loud in 0-8 s, silent 8-12 s; mask low where the band is loud."""
    rng = np.random.default_rng(seed)
    n = seconds * SR
    env = np.repeat(rng.uniform(0.3, 1.0, n // 4800 + 1), 4800)[:n]
    guitar = (0.1 * rng.standard_normal(n) * env).astype(np.float32)
    others = (0.1 * rng.standard_normal(n)).astype(np.float32)
    others[8 * SR:12 * SR] = 0.0
    frames = stft(guitar, fs=SR, nperseg=MS.N_FFT, noverlap=MS.N_FFT - MS.HOP)[2].shape[1]
    mask = np.full((1025, frames), 0.95, np.float32)
    mask[:, : 8 * SR // MS.HOP] = 0.3
    sections = {"bar_starts": [0.0, 4.0, 8.0, 12.0], "bar_snapshot": [0, 0, 1, 1],
                "exposed_windows": [[8.0, 10.0], [9.0, 11.0], [10.0, 12.0]]}
    return MS.Song(guitar, others, mask, sections, {}), mask


def test_frame_and_sample_confidence_follow_the_mask():
    song, mask = _song()
    fc, fp = MS.frame_confidence(song.mask_tf, song.guitar)
    assert fc.shape == fp.shape == (mask.shape[1],)
    assert fc[: 4 * SR // MS.HOP].mean() == pytest.approx(0.3, abs=0.02)
    assert fc[10 * SR // MS.HOP: 14 * SR // MS.HOP].mean() == pytest.approx(0.95, abs=0.02)
    m = MS.sample_confidence(fc, len(song.guitar))
    assert m.shape == song.guitar.shape and m.min() >= 0 and m.max() <= 1
    assert m[2 * SR] < 0.4 and m[13 * SR] > 0.9


def test_pick_window_prefers_confident_exposed_material_inside_the_snapshot():
    song, _ = _song()
    fc, fp = MS.frame_confidence(song.mask_tf, song.guitar)
    w = MS.pick_window(fc, fp, song.sections, 1, 4.0, len(song.guitar) / SR)
    assert 8.0 <= w["start_s"] and w["end_s"] <= 16.0                 # inside snapshot 1 (bars 3-4)
    assert w["mean_confidence"] > 0.9
    assert w["exposed_overlap_s"] == pytest.approx(4.0)                # union, not the sum of overlaps
    w0 = MS.pick_window(fc, fp, song.sections, 0, 4.0, len(song.guitar) / SR)
    assert w0["end_s"] <= 8.0 + 1e-6 and w0["mean_confidence"] < 0.5
    with pytest.raises(ValueError):
        MS.pick_window(fc, fp, song.sections, 2, 4.0, 16.0)


def test_union_overlap():
    assert MS._union_overlap(0, 10, [(1, 4), (3, 6), (8, 12)]) == pytest.approx(7.0)
    assert MS._union_overlap(0, 1, []) == 0.0


def test_level_match_equal_rms_and_peak_cap():
    rng = np.random.default_rng(1)
    a, b = rng.standard_normal(4800).astype(np.float32), 5 * rng.standard_normal(4800).astype(np.float32)
    out, gains = MS.level_match({"a": a, "b": b}, "a")
    assert MS.rms(out["a"]) == pytest.approx(MS.rms(out["b"]), rel=1e-3)
    assert max(np.abs(v).max() for v in out.values()) <= 10 ** (-1 / 20) + 1e-6
    assert gains["b"] < gains["a"]


def test_masked_kit_injects_and_slices_the_mask():
    seen = {}

    class K:
        def score(self, t, c, *, stems=None, mask=None, weights=None):
            seen["mask"] = mask
            return SimpleNamespace(total=0.0, terms={})

    mk = MS.MaskedKit(K(), np.linspace(0, 1, 100, dtype=np.float32))
    mk.score(np.zeros(60), np.zeros(60))
    assert len(seen["mask"]) == 60
    mk.score(np.zeros(60), np.zeros(60), mask=np.ones(60))
    assert seen["mask"].sum() == 60


@needs_known
def test_derive_free_skips_minutiae_unmapped_and_bypassed_delay():
    from tonematch.preset import read_hlx

    pre = read_hlx(ROOT / "fixtures" / "smoke" / "known.hlx")
    tpl = SimpleNamespace(map={f"0:{k}": "cc" for k in pre.slots[0].params} | {"1:EarlyReflections": "cc",
                                                                                  "2:Mix": "cc"})
    free = MS.derive_free(pre, tpl, None)
    names = {n for _, n in free}
    assert {"Drive", "Master", "Treble"} <= names
    assert not names & MS.FIXED_PARAMS
    assert "Mix" not in names                      # the delay is bypassed in the template
    assert (1, "EarlyReflections") in free
    assert (1, "Distance") not in free             # not mapped
    pre.slots[2].enabled = True
    assert (2, "Mix") in MS.derive_free(pre, tpl, None)


@needs_known
def test_write_best_hlx_applies_values_and_snapshots(tmp_path):
    from tonematch.preset import read_hlx
    from tonematch.search import ParamSpace

    pre = read_hlx(ROOT / "fixtures" / "smoke" / "known.hlx")
    cand = MS.Cand("known", pre, None, [(0, "Drive"), (0, "Treble")])
    space = ParamSpace.from_preset(pre, cand.free)
    path = MS.write_best_hlx(cand, space, np.array([0.123456, 0.9]), tmp_path / "x.hlx", "DS-Sample s1")
    back = read_hlx(path)
    assert back.slots[0].params["Drive"] == pytest.approx(0.123456, abs=1e-6)   # grid values are written unrounded
    assert back.slots[0].params["Treble"] == pytest.approx(0.9)
    assert back.name == "DS-Sample s1" and back.device_version == pre.device_version
    assert pre.slots[0].params["Drive"] != 0.123456                         # the source preset is untouched


def _args(tmp_path, **kw):
    lib = tmp_path / "emptylib"
    lib.mkdir(exist_ok=True)
    base = ["--stems-dir", str(tmp_path / "stems"), "--song", "fake", "--out", str(tmp_path / "out"),
            "--dur", "4", "--wall-min", "0.1", "--reserve-s", "1", "--snap0-renders", "8",
            "--di-library", str(lib)]
    return MS.parse(base + [x for k, v in kw.items() for x in (f"--{k.replace('_', '-')}", str(v))])


def test_end_to_end_with_a_fake_engine(tmp_path, recwarn):
    from tonematch.search.stubs import FakeEngine, fake_preset

    song, mask = _song()
    d = tmp_path / "stems"
    d.mkdir()
    sf.write(str(d / "guitar.wav"), song.guitar, SR, subtype="FLOAT")
    sf.write(str(d / "others.wav"), song.others, SR, subtype="FLOAT")
    np.save(d / "confidence.npy", mask.astype(np.float16))
    (d / "sections.json").write_text(json.dumps(song.sections))
    cands = [MS.Cand("lo", fake_preset(0.3, 0.5, 0.5)), MS.Cand("hi", fake_preset(0.8, 0.4, 0.5))]
    args = _args(tmp_path)
    eng = FakeEngine(engine_name="native")
    assert MS.run(args, engine=eng, cands=cands, max_scores_per_chain=30) == 0
    out = tmp_path / "out"
    for f in ("report.md", "result.json", "best_snapshot1.hlx", "best_snapshot0.hlx", "run.log",
              "listen/target_stem.wav", "listen/match.wav", "listen/start_unmatched.wav",
              "listen/ab_L-target_R-match.wav", "listen/di_dry.wav"):
        assert (out / f).is_file(), f
    ab, sr = sf.read(str(out / "listen" / "ab_L-target_R-match.wav"))
    assert sr == SR and ab.ndim == 2 and ab.shape[1] == 2
    tgt, _ = sf.read(str(out / "listen" / "target_stem.wav"))
    mat, _ = sf.read(str(out / "listen" / "match.wav"))
    assert np.sqrt(np.mean(tgt ** 2)) == pytest.approx(np.sqrt(np.mean(mat ** 2)), rel=0.01)
    res = json.loads((out / "result.json").read_text())
    assert res["label"] == "unadmitted loss" and res["finals"]
    assert res["finals"][0]["loss"] <= res["start_loss"] + 1e-9 or res["finals"][0]["loss"] < res["dry_loss"]
    rep = (out / "report.md").read_text()
    assert "unadmitted loss" in rep and "Top 3 presets" in rep and "Caveats" in rep and "Chains tried" in rep


def test_dry_run_renders_nothing(tmp_path):
    song, mask = _song()
    d = tmp_path / "stems"
    d.mkdir()
    sf.write(str(d / "guitar.wav"), song.guitar, SR, subtype="FLOAT")
    sf.write(str(d / "others.wav"), song.others, SR, subtype="FLOAT")
    np.save(d / "confidence.npy", mask.astype(np.float16))
    (d / "sections.json").write_text(json.dumps(song.sections))

    class Boom:
        def __getattr__(self, k):
            raise AssertionError("engine touched in a dry run")

    args = _args(tmp_path, chains="K")
    args.dry_run = True
    assert MS.run(args, engine=Boom(), cands=[]) == 0
    assert not (tmp_path / "out" / "report.md").exists()


@needs_known
def test_make_space_includes_the_minimum_grid_step():
    from tonematch.preset import read_hlx

    pre = read_hlx(ROOT / "fixtures" / "smoke" / "known.hlx")
    cand = MS.Cand("known", pre, None, [(0, "Cut"), (0, "Drive")])
    sp = MS.make_space(cand)
    assert sp.start[0] == 0.0 and sp.lo.min() == 0.0                  # Cut starts at 0.0: minimum is a grid step
    assert sp.from_unit(np.zeros(2)).min() == 0.0
    assert MS.make_space(MS.Cand("all", pre, None, None)).n >= 9      # free=None means every numeric param


def test_timed_engine_recovers_from_a_dead_host_once():
    from tonematch.engines.base import EngineError
    from tonematch.search.stubs import FakeEngine, fake_dry, fake_preset

    class Dying(FakeEngine):
        def __init__(self):
            super().__init__(engine_name="native")
            self.die_next = False

        def render(self, dry):
            if self.die_next:
                self.die_next = False
                raise EngineError("Native host process exited (code -9)")
            return super().render(dry)

    inner = Dying()
    eng = MS.TimedEngine(inner, log=lambda m: None)
    eng.load(fake_preset(0.3, 0.5, 0.5))
    eng.set_params({(0, "Gain"): 0.9})
    y0 = eng.render(fake_dry(0.2))
    inner.params = {}                    # a restarted host has lost everything
    inner.die_next = True
    y1 = eng.render(fake_dry(0.2))       # dies, is reloaded (load + set_params replayed), retried
    assert eng.restarts == 1 and inner.params[(0, "Gain")] == 0.9
    assert np.allclose(y0, y1)
    inner.die_next = True
    eng.load(fake_preset())              # a load after a death works too
    other = EngineError("Native host 'render' failed: bad")
    assert not eng._dead(other)


def test_level_guard_penalises_a_collapsed_output_only():
    class K:
        def score(self, t, c, **kw):
            return SimpleNamespace(total=1.0, terms={"a": 1.0})

    g = MS.LevelGuardKit(K(), tol_db=6.0, slope=0.5)
    x = np.full(1000, 0.1)
    assert g.score(x, x).total == 1.0 and "level_penalty" not in g.score(x, x).terms   # no reference yet
    g.ref_db = g.level_db(x)
    assert g.score(x, x * 10 ** (-4 / 20)).total == 1.0                  # within +-6 dB is free
    quiet = g.score(x, x * 10 ** (-52 / 20))                             # the level collapse of run 2
    assert quiet.total == pytest.approx(1.0 + 0.5 * 46) and quiet.terms["level_db_vs_ref"] == pytest.approx(-52)
    assert g.score(x, x * 10 ** (20 / 20)).total > 1.0                   # and so is too loud


@needs_known
def test_make_prior_is_off_at_zero_weight_and_pulls_to_the_middle():
    from tonematch.preset import read_hlx

    pre = read_hlx(ROOT / "fixtures" / "smoke" / "known.hlx")
    sp = MS.make_space(MS.Cand("k", pre, None, [(0, "Drive"), (0, "Treble")]))
    assert MS.make_prior(sp, 0.0) is None
    pr = MS.make_prior(sp, 0.3)
    assert pr.penalty({(0, "Drive"): 0.5, (0, "Treble"): 0.5}) == 0.0
    assert pr.penalty({(0, "Drive"): 1.0, (0, "Treble"): 1.0}) == pytest.approx(0.3 * (0.5 / 0.3) ** 2)


def test_timed_engine_restarts_a_bloated_host_before_it_is_killed(monkeypatch):
    from tonematch.search.stubs import FakeEngine, fake_dry, fake_preset

    class Proc:
        pid = 1

        def poll(self):
            return None

    class Host:
        proc = Proc()
        restarts = 0

        def restart(self):
            Host.restarts += 1

    inner = FakeEngine(engine_name="native")
    inner.host = Host()
    eng = MS.TimedEngine(inner, log=lambda m: None, rss_limit_mb=1000, check_every=2)
    monkeypatch.setattr(eng, "host_rss_mb", lambda: 5000.0)
    eng.load(fake_preset(0.4, 0.5, 0.5))
    eng.set_params({(0, "Gain"): 0.8})
    for _ in range(4):
        eng.render(fake_dry(0.1))
    assert eng.proactive >= 1 and Host.restarts == eng.restarts >= 1
    assert inner.params[(0, "Gain")] == 0.8                               # state replayed after each restart


@needs_known
def test_frozen_params_excluded_and_pack_writes_true_level_di(tmp_path):
    from tonematch.preset import read_hlx

    pre = read_hlx(ROOT / "fixtures" / "smoke" / "known.hlx")
    tpl = SimpleNamespace(map={"1:EarlyReflections": "c", "0:Drive": "c"})
    assert (1, "EarlyReflections") in MS.derive_free(pre, tpl, None)
    assert (1, "EarlyReflections") not in MS.derive_free(pre, tpl, None, MS.STOMP_FROZEN)
    rng = np.random.default_rng(0)
    di = (0.2 * rng.standard_normal(4800)).astype(np.float32)
    info = MS.write_pack(tmp_path, di * 0.1, di * 3, di, di)
    back, _ = sf.read(str(tmp_path / "di_search.wav"), dtype="float32")
    assert np.array_equal(back, di) and (tmp_path / "gains.json").is_file() and "gains_db" in info


def test_target_and_user_di_options(tmp_path):
    from tonematch.search.stubs import FakeEngine, fake_preset

    song, mask = _song()
    d = tmp_path / "stems"
    d.mkdir()
    sf.write(str(d / "guitar.wav"), song.guitar, SR, subtype="FLOAT")
    sf.write(str(d / "others.wav"), song.others, SR, subtype="FLOAT")
    np.save(d / "confidence.npy", mask.astype(np.float16))
    (d / "sections.json").write_text(json.dumps(song.sections))
    rng = np.random.default_rng(3)
    for k, (t0, sc) in enumerate([(5.0, 0.2), (6.0, 0.1)]):       # take covers song t0 .. t0+10 s
        sf.write(str(tmp_path / f"take{k}_dry.wav"), (sc * rng.standard_normal(10 * SR)).astype(np.float32),
                 SR, subtype="FLOAT")
        (tmp_path / f"take{k}.json").write_text(json.dumps({"alignment": {"take_sample0_song_s": t0}}))
    assert MS.parse_target("34.5:42.0") == (34.5, 42.0)
    args = _args(tmp_path, target="9:13", user_di=tmp_path / "take0_dry.wav",
                 user_di_json=tmp_path / "take0.json", user_di_extra=tmp_path / "take1_dry.wav")
    cands = [MS.Cand("lo", fake_preset(0.3, 0.5, 0.5)), MS.Cand("hi", fake_preset(0.8, 0.4, 0.5))]
    assert MS.run(args, engine=FakeEngine(engine_name="native"), cands=cands, max_scores_per_chain=20) == 0
    out = tmp_path / "out"
    res = json.loads((out / "result.json").read_text())
    assert (res["window"]["start_s"], res["window"]["end_s"]) == (9.0, 13.0)
    assert len(res["di_sensitivity"]) == 1
    di, _ = sf.read(str(out / "listen" / "di_search.wav"), dtype="float32")
    x, _ = sf.read(str(tmp_path / "take0_dry.wav"), dtype="float32")
    assert np.array_equal(di, x[4 * SR:8 * SR])                   # song 9 s = take sample (9-5)*48000, true level
    assert "user take" in (out / "report.md").read_text()
    with pytest.raises(ValueError):
        MS.user_di_clip(tmp_path / "take0_dry.wav", tmp_path / "take0.json", {"start_s": 20.0, "end_s": 24.0, "dur_s": 4.0})


@needs_known
def test_top3_hlx_written_from_result_json(tmp_path):
    from tonematch.preset import read_hlx

    pre = read_hlx(ROOT / "fixtures" / "smoke" / "known.hlx")
    cand = MS.Cand("known", pre, None, [(0, "Drive"), (0, "Treble")])
    sp = MS.make_space(cand)
    fins = [{"result": SimpleNamespace(cand=cand, space=sp), "values": np.array([0.2 * k, 0.9])}
            for k in (1, 2, 3)]
    out = MS.write_top_hlx(fins, tmp_path, "T")
    assert [p.name for p, _ in out] == ["rank1_known.hlx", "rank2_known.hlx", "rank3_known.hlx"]
    assert read_hlx(tmp_path / "rank2_known.hlx").slots[0].params["Drive"] == pytest.approx(0.4)


@needs_known
def test_space_snaps_to_the_cc_grid_and_hlx_keeps_snapped_values(tmp_path):
    from tonematch.preset import read_hlx

    pre = read_hlx(ROOT / "fixtures" / "smoke" / "known.hlx")
    tpl = SimpleNamespace(ranges={"0:Drive": [0.0, 1.0], "0:Treble": [0.2, 0.8]})
    cand = MS.Cand("known", pre, tpl, [(0, "Drive"), (0, "Treble")])
    sp = MS.make_space(cand)
    rng = np.random.default_rng(0)
    for _ in range(50):
        v = sp.from_unit(rng.uniform(size=2))
        k0, k1 = v[0] * 127, (v[1] - 0.2) / 0.6 * 127
        assert abs(k0 - round(k0)) < 1e-6 and abs(k1 - round(k1)) < 1e-6 and 0 <= round(k0) <= 127
        assert 0.2 - 1e-9 <= v[1] <= 0.8 + 1e-9
    k = sp.from_unit(np.array([0.3, 0.7]))
    path = MS.write_best_hlx(cand, sp, k, tmp_path / "g.hlx", "g")
    back = read_hlx(path).slots[0].params
    assert back["Drive"] * 127 == pytest.approx(round(back["Drive"] * 127), abs=1e-4)
    assert back["Drive"] == pytest.approx(k[0], abs=1e-6) and back["Treble"] == pytest.approx(k[1], abs=1e-6)


# ------------------------------------------------------------------ Stomp speed options (T061)


def test_apply_stomp_speed_defaults_legacy_and_env(monkeypatch):
    monkeypatch.delenv("TONEMATCH_STOMP_READBACK_EVERY", raising=False)
    monkeypatch.delenv("TONEMATCH_STOMP_TAIL", raising=False)
    e = SimpleNamespace(readback_every=1, tail_mode="bound")
    assert MS.apply_stomp_speed(e) == {"readback_every": 10, "tail_mode": "measured"}
    assert MS.apply_stomp_speed(e, legacy=True) == {"readback_every": 1, "tail_mode": "bound"}
    monkeypatch.setenv("TONEMATCH_STOMP_READBACK_EVERY", "5")
    e = SimpleNamespace(readback_every=5, tail_mode="bound")
    assert MS.apply_stomp_speed(e)["readback_every"] == 5          # the user's own setting stays


def test_search_view_cuts_everything_at_the_same_samples():
    from tonematch.search import Section

    n = 8 * SR
    dry = np.zeros(n, np.float32)
    dry[5 * SR:6 * SR] = 1.0                                       # the loud part
    ramp = np.arange(n, dtype=np.float32)
    sec = Section("s", "x", ramp.copy(), dry, stems={"others": ramp * 2}, mask=ramp / 3)
    v = MS.search_view(sec, 3.0)
    assert len(v.dry) == len(v.target) == len(v.mask) == len(v.stems["others"]) == 3 * SR
    i = int(v.target[0])
    assert dry[i:i + 3 * SR].max() == 1.0 and np.array_equal(v.dry, dry[i:i + 3 * SR])
    assert v.stems["others"][0] == pytest.approx(2 * i) and v.mask[0] == pytest.approx(i / 3)
    assert MS.search_view(sec, 10.0) is sec and MS.search_view(sec, 0) is sec


def _stomp_run(tmp_path, *extra):
    from tonematch.search.stubs import FakeEngine, fake_preset

    song, mask = _song()
    d = tmp_path / "stems"
    d.mkdir()
    sf.write(str(d / "guitar.wav"), song.guitar, SR, subtype="FLOAT")
    sf.write(str(d / "others.wav"), song.others, SR, subtype="FLOAT")
    np.save(d / "confidence.npy", mask.astype(np.float16))
    (d / "sections.json").write_text(json.dumps(song.sections))
    cands = [MS.Cand("lo", fake_preset(0.3, 0.5, 0.5)), MS.Cand("hi", fake_preset(0.8, 0.4, 0.5))]
    eng = FakeEngine(engine_name="stomp")
    eng.readback_every, eng.tail_mode, lens = 1, "bound", []
    orig = eng.render
    eng.render = lambda x: (lens.append(len(x)), orig(x))[1]
    args = _args(tmp_path, snap0_renders=0)
    args = MS.parse(["--stems-dir", str(d), "--song", "fake", "--out", str(tmp_path / "out"), "--dur", "5",
                     "--wall-min", "0.1", "--reserve-s", "1", "--snap0-renders", "0", "--di-library",
                     str(tmp_path / "emptylib"), "--engine", "stomp", *extra])
    assert MS.run(args, engine=eng, cands=cands, max_scores_per_chain=30) == 0
    return eng, lens, json.loads((tmp_path / "out" / "result.json").read_text())


def test_stomp_default_uses_short_search_clips_and_speed_options(tmp_path, monkeypatch):
    monkeypatch.delenv("TONEMATCH_STOMP_READBACK_EVERY", raising=False)
    monkeypatch.delenv("TONEMATCH_STOMP_TAIL", raising=False)
    eng, lens, res = _stomp_run(tmp_path)
    assert (eng.readback_every, eng.tail_mode) == (10, "measured")
    assert res["stomp_speed"]["search_clip_s"] == 3.0 and res["stomp_speed"]["final_clip_s"] == 5.0
    assert lens.count(3 * SR) > 10 and lens.count(5 * SR) >= 3     # search short, final check full
    assert "Stomp engine, speed options" in (tmp_path / "out" / "report.md").read_text()


def test_stomp_legacy_flag_restores_the_old_behaviour(tmp_path, monkeypatch):
    monkeypatch.delenv("TONEMATCH_STOMP_READBACK_EVERY", raising=False)
    monkeypatch.delenv("TONEMATCH_STOMP_TAIL", raising=False)
    eng, lens, res = _stomp_run(tmp_path, "--stomp-legacy")
    assert (eng.readback_every, eng.tail_mode) == (1, "bound")
    assert set(lens) == {5 * SR} and res["stomp_speed"]["legacy"] is True
