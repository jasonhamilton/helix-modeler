"""block-probe offline part (T046): probe set, analysis against KNOWN synthetic blocks, fingerprint
schema and compare, CLI. No engine, no song audio, no Line 6 content.

The synthetic blocks (tonematch.probe.synthetic) are numpy/scipy DSP with known parameters; the
analysis must recover them within the tolerances stated in `synthetic.checks`.
"""
from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tonematch.probe import analysis, fingerprint, signals, synthetic

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def probe():
    return signals.build_probe_set(di=False)


@pytest.fixture(scope="module")
def results(probe):
    rows, fps = synthetic.validate(probe)
    return rows, fps


# ------------------------------------------------------------------------------------ probe set

def test_probe_set_is_versioned_deterministic_and_bounded(probe):
    again = signals.build_probe_set(di=False)
    assert probe.version == signals.PROBE_VERSION + "-nodi"
    assert probe.signal.dtype == np.float32 and probe.sample_rate == 48_000
    assert np.array_equal(probe.signal, again.signal)
    assert probe.layout()["sha256"] == again.layout()["sha256"]
    assert float(np.max(np.abs(probe.signal))) <= 10 ** (-3 / 20) + 1e-6   # loudest is -3 dBFS
    assert 30.0 < probe.duration_s < 45.0


def test_segments_are_ordered_disjoint_and_separated_by_silence(probe):
    segs = probe.segments
    for a, b in itertools.pairwise(segs):
        assert a.start + a.length <= b.start
    kinds = {s.kind for s in segs}
    assert {"impulse", "mod_sine", "ess", "stepped_sine", "two_tone", "level_step", "bursts",
            "gate_stair", "marker"} <= kinds
    assert len(probe.of_kind("ess")) == 4 and len(probe.of_kind("stepped_sine")) == 3
    # every ESS is followed by its silent gap; the sync marker is last
    for s in probe.of_kind("ess"):
        gap = probe.signal[s.start + int(signals.ESS_T * 48_000) + 1:s.start + s.length]
        assert np.max(np.abs(gap)) == 0.0
    assert segs[-1].kind == "marker"
    imp = probe.seg("impulse")
    assert np.count_nonzero(probe.signal[imp.start:imp.start + imp.length]) == 1


def test_layout_round_trip_and_tamper_detection(probe, tmp_path):
    wav, js = signals.write_probe_set(probe, tmp_path)
    x, sr = sf.read(str(wav), dtype="float32")
    assert sr == 48_000 and np.array_equal(x, probe.signal)
    layout = json.loads(js.read_text())
    assert signals.probe_from_layout(js).layout()["sha256"] == layout["sha256"]
    layout["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        signals.probe_from_layout(layout)


def test_di_excerpts_are_referenced_not_copied():
    root = signals.default_di_root()
    if not all((root / c["path"]).exists() for c in signals.DI_CLIPS):
        pytest.skip("local DI library (data/di) not present")
    p = signals.build_probe_set(di=True)
    assert p.version == signals.PROBE_VERSION and len(p.di) == len(signals.DI_CLIPS)
    lay = p.layout()
    for ref in lay["di"]:
        assert not Path(ref["path"]).is_absolute() and ref["licence"] == "CC BY 4.0"
        assert len(ref["excerpt_sha256"]) == 64
    assert len(json.dumps(lay)) < 20_000            # references and hashes only, no audio
    for s in p.of_kind("di"):
        pk = np.max(np.abs(p.signal[s.start:s.start + s.length]))
        assert abs(20 * np.log10(pk) - signals.DI_PEAK_DBFS) < 0.1


# ----------------------------------------------------------------------- accuracy vs known blocks

BLOCK_NAMES = [b.name for b in synthetic.default_blocks()]


@pytest.mark.parametrize("name", BLOCK_NAMES)
def test_known_block_parameters_recovered(results, name):
    rows, _ = results
    mine = [r for r in rows if r["block"] == name]
    assert mine, name
    bad = [f"{r['feature']}: true {r['true']} measured {r['measured']} tol {r['tol']}"
           for r in mine if not r["pass"]]
    assert not bad, bad


def test_eq_frequency_response_is_level_independent(results):
    _, fps = results
    mags = np.array(fps["biquad_eq"]["linear"]["mag_db"])
    assert np.max(np.abs(mags - mags[0])) < 0.05


def test_identity_has_unity_gain_everywhere(results):
    _, fps = results
    fp = fps["identity"]
    for p in fp["harmonics"]["per_freq"]:
        assert np.max(np.abs(p["gain_db"])) < 0.01
    assert fp["dynamics"]["attack_ms"] is None and fp["dynamics"]["gate_threshold_dbfs"] is None
    assert all(r["imd_total_db"] < -100 for r in fp["imd"])


def test_distortion_ranks_by_drive(probe):
    x = probe.signal.astype(np.float64)
    thd = []
    for d in (2.0, 5.0, 20.0):
        fp = analysis.analyse(synthetic.tanh_clip(d)(x), probe)
        h = next(p for p in fp["harmonics"]["per_freq"] if p["freq_hz"] == 440.0)
        thd.append(h["thd_pct"][-1])
        assert h["thd_pct"] == sorted(h["thd_pct"])          # THD rises with level
    assert thd == sorted(thd)
    # odd-order only for a symmetric clipper: H2 far below H3
    assert h["h_db"][-1][0] < h["h_db"][-1][1] - 40


def test_imd_rises_for_a_nonlinear_block(results):
    _, fps = results
    lin = {r["name"]: r["imd_total_db"] for r in fps["biquad_eq"]["imd"]}
    nl = {r["name"]: r["imd_total_db"] for r in fps["tanh_x10"]["imd"]}
    for k, v in lin.items():
        assert nl[k] > v + 60


# ------------------------------------------------------------------- alignment and input forms

def test_given_latency_and_stereo_input_match_marker_mono(probe):
    x = probe.signal.astype(np.float64)
    y = synthetic.compressor(-30.0, 4.0, 10.0, 100.0)(x)
    a = analysis.analyse(y, probe)
    b = analysis.analyse(np.stack([y, y], axis=1), probe, latency=synthetic.LATENCY)
    assert b["alignment"]["source"] == "given"
    assert a["alignment"]["latency_samples"] == synthetic.LATENCY
    for k in ("attack_ms", "release_ms", "level_dependent_gain_db"):
        assert a["dynamics"][k] == pytest.approx(b["dynamics"][k], abs=1e-6)


def test_short_wet_render_is_padded_and_silence_is_reported(probe):
    fp = analysis.analyse(np.zeros(probe.signal.size // 2, np.float32), probe)
    assert fp["alignment"]["source"] == "default-0" and fp["notes"]
    assert fingerprint.validate(fp) == []


# ------------------------------------------------------------------------ fingerprint + compare

def test_fingerprint_schema_valid_strict_json_and_round_trip(results, tmp_path):
    _, fps = results
    for fp in fps.values():
        assert fingerprint.validate(fp) == []
    p = fingerprint.save(fps["tanh_x10"], tmp_path / "fp.json")
    assert fingerprint.load(p) == json.loads(p.read_text())
    bad = dict(fps["tanh_x10"], schema_version=99)
    assert fingerprint.validate(bad)
    with pytest.raises(ValueError):
        fingerprint.save(bad, tmp_path / "bad.json")


def test_compare_identical_is_clean(results):
    _, fps = results
    for fp in fps.values():
        res = fingerprint.compare(fp, fp)
        assert res["n_differs"] == 0 and res["n_compared"] > 30


def test_compare_names_the_features_that_changed(results):
    _, fps = results
    res = fingerprint.compare(fps["identity"], fps["biquad_eq"])
    bad = {r["feature"] for r in res["rows"] if r["status"] == "differs"}
    assert any(f.startswith("linear.mag_db") for f in bad)
    assert not any(f.startswith(("harmonics.thd", "delay.", "modulation.")) for f in bad)
    res = fingerprint.compare(fps["identity"], fps["delay_300ms_fb0.5_m0.6"])
    assert any(r["feature"] == "delay.detected" and r["status"] == "differs" for r in res["rows"])
    res = fingerprint.compare(fps["tremolo_4Hz_d0.5"], fps["vibrato_1.5Hz_1ms"])
    assert any(r["feature"] == "modulation.kind" and r["status"] == "differs"
               for r in res["rows"])
    text = fingerprint.format_compare(res)
    assert "modulation.kind" in text


def test_compare_setting_a_vs_b_within_tolerance_for_tiny_change(probe):
    x = probe.signal.astype(np.float64)
    a = analysis.analyse(synthetic.compressor(-30.0, 4.0, 10.0, 100.0)(x), probe)
    b = analysis.analyse(synthetic.compressor(-30.0, 4.0, 10.5, 100.0)(x), probe)
    c = analysis.analyse(synthetic.compressor(-30.0, 4.0, 30.0, 100.0)(x), probe)
    assert fingerprint.compare(a, b)["n_differs"] == 0
    rows = fingerprint.compare(a, c)["rows"]
    assert any(r["feature"] == "dynamics.attack_ms" and r["status"] == "differs" for r in rows)


# ------------------------------------------------------------------------------------------ CLI

def test_cli_make_analyse_compare(tmp_path, probe):
    script = str(ROOT / "scripts" / "block_probe.py")
    r = subprocess.run([sys.executable, script, "make", "--no-di", "--out", str(tmp_path)],
                       check=False, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    layout = tmp_path / f"{probe.version}.json"
    x, _ = sf.read(str(tmp_path / f"{probe.version}.wav"), dtype="float64")
    for name, blk in (("a", synthetic.identity()), ("b", synthetic.eq_block())):
        sf.write(str(tmp_path / f"{name}.wav"), blk(x), 48_000, subtype="FLOAT")
        r = subprocess.run([sys.executable, script, "analyse", str(tmp_path / f"{name}.wav"),
                            "--layout", str(layout), "--subject", f"block={name}"],
                           check=False, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    fa, fb = (tmp_path / "a.fingerprint.json"), (tmp_path / "b.fingerprint.json")
    assert fingerprint.load(fa)["subject"]["block"] == "a"
    same = subprocess.run([sys.executable, script, "compare", str(fa), str(fa)],
                          check=False, capture_output=True, text=True)
    diff = subprocess.run([sys.executable, script, "compare", str(fa), str(fb)],
                          check=False, capture_output=True, text=True)
    assert same.returncode == 0 and diff.returncode == 1
    assert "linear.mag_db" in diff.stdout
