"""T187 measured-pedal mode: the capture tool on fake audio and a fake bridge (no pedal, no Native).

The pedal is never opened here. The FX Loop template is loaded in place on a fake 04C-like edit buffer,
the loop and a toy pedal are numpy stand-ins (`scripts/pedal_probe.py` FakeLoop), and the calibration
maths are checked against known latency, gain, tilt and input scale."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from tonematch import pedal_probe as PP
from tonematch import reamp
from tonematch.engines import stomp
from tonematch.metrics import distortion_probe as P
from tonematch.metrics import distortion_step4 as S4

ROOT = Path(__file__).resolve().parents[1]
SR = 48000


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


PS = _load("pedal_probe")
SIM = _load("pedal_probe_sim")


def _p(i, name, value, lo=0.0, hi=1.0, kind="Continuous"):
    return {"i": i, "id": name, "sym": name, "name": name, "kind": kind, "min": lo, "max": hi, "value": value}


FX_PARAMS = [_p(0, "Send", 0.0, -60.0, 6.0), _p(1, "Return", 0.0, -60.0, 6.0), _p(2, "Mix", 1.0)]


def chain_04c():
    """The layout of 04C "Smoke Known" (decoded offline): Amp+Cab at slot 2, Ping Pong delay at 3."""
    return {"setlist": 0, "index": 11, "name": "Smoke Known", "assignments": [],
            "slots": [{"pos": 0, "kind": "Input", "enabled": True, "routing": 1},
                      {"pos": 1, "kind": "Empty", "enabled": True},
                      {"pos": 2, "kind": "Block", "enabled": True, "model": 20, "model_id": "HD2_AmpEssexA15",
                       "paired": 45, "paired_id": "HD2_Cab1x12BlueBell", "params": [_p(0, "Drive", 0.61)],
                       "paired_params": [_p(0, "Distance", 4.0, 1.0, 12.0)]},
                      {"pos": 3, "kind": "Block", "enabled": False, "model": 408, "model_id": "HD2_DL4PingPong",
                       "params": [_p(0, "Mix", 0.23), _p(1, "Feedback", 0.52)]},
                      {"pos": 9, "kind": "Output", "enabled": True, "routing": 1}]}


class FakeBridge:
    """Keeps a chain, applies edits, logs calls; `model` gives the slot the new model's parameters."""

    def __init__(self, chain):
        self.flash = copy.deepcopy(chain)
        self.chain = copy.deepcopy(chain)
        self.calls: list[tuple[str, dict]] = []
        self.ready = {"presets": 126}

    def _slot(self, pos):
        return next(s for s in self.chain["slots"] if s["pos"] == pos)

    def call(self, cmd, timeout_s=30.0, **a):
        self.calls.append((cmd, a))
        if cmd == "info":
            return {"firmware": "3.80", "build": "b", "setlist": 0, "index": 11, "label": "04C", "name": "Smoke Known"}
        if cmd == "chain":
            return copy.deepcopy(self.chain)
        if cmd == "backup":
            Path(a["path"]).write_bytes(b"doc")
            return {"bytes": 3}
        if cmd == "model":
            s = self._slot(a["pos"])
            s.update(model_id=a["model"], model=141, params=copy.deepcopy(FX_PARAMS))
            for p in s["params"]:
                p["value"] = -60.0 if p["sym"] != "Mix" else 0.0      # a model change resets values
            return {}
        if cmd == "set":
            s = self._slot(a["pos"])
            next(p for p in s["params"] if p["i"] == a["param"])["value"] = a["value"]
            return {}
        if cmd == "enable":
            self._slot(a["pos"])["enabled"] = a["on"]
            return {}
        if cmd == "route_key":
            self._slot(a["pos"])["routing"] = a["to"]
            return {"status": 0}
        if cmd == "select":
            self.chain = copy.deepcopy(self.flash)
            return {}
        raise AssertionError(f"unexpected command {cmd}")

    def close(self):
        pass


def catalog():
    return {"models": {PP.FX_LOOP_MODEL: {"id": PP.FX_LOOP_MODEL, "category": "other", "mono": True, "stereo": False,
                                          "params": {"Send": {"min": -60.0, "max": 6.0, "default": 0.0},
                                                     "Return": {"min": -60.0, "max": 6.0, "default": 0.0},
                                                     "Mix": {"min": 0.0, "max": 1.0, "default": 1.0},
                                                     "@trails": {"min": False, "max": True, "default": False}}}}}


def base_preset():
    from tonematch.preset import Preset

    return Preset(name="base", raw={"data": {"tone": {"dsp0": {}, "dsp1": {}, "global": {}}, "meta": {"name": "b"},
                                             "device": 2162694}, "meta": {}, "schema": "L6Preset", "version": 6})


@pytest.fixture
def engine(monkeypatch, tmp_path):
    monkeypatch.setattr(reamp, "find_device", lambda *a, **k: 0)
    from tonematch.engines import native_library as lib

    monkeypatch.setattr(lib, "base_preset", base_preset)
    cal = reamp.Calibration(latency_samples=1207, measurements=[1207] * 3, level_gain_db=0.0, tone_in_dbfs=-18,
                            tone_out_dbfs=-18, firmware="3.80", pa_latency=reamp.PA_LATENCY)
    b = FakeBridge(chain_04c())
    eng = stomp.StompEngine(bridge=b, calibration=cal, session_dir=tmp_path, whole_preset="off", sentinel=False,
                            slip_guard="off")
    return eng, b


# ------------------------------------------------------------------------------------------- template

def test_template_goes_into_the_single_block_slot():
    assert PP.template_slot(chain_04c()) == 3
    only_amp = chain_04c()
    only_amp["slots"] = [s for s in only_amp["slots"] if s["pos"] != 3]
    with pytest.raises(ValueError, match="single"):
        PP.template_slot(only_amp)


def test_template_preset_and_plan_are_edit_buffer_only(monkeypatch):
    from tonematch.engines import native_library as lib

    monkeypatch.setattr(lib, "base_preset", base_preset)
    pr = PP.template_preset(chain_04c(), catalog(), send_db=-1.5)
    (s,) = pr.blocks()
    assert s.model_id == PP.FX_LOOP_MODEL and s.position == 2 and s.extra["@path"] == 0 and s.enabled
    assert s.params == {"Send": -1.5, "Return": 0.0, "Mix": 1.0}
    plan = PP.routing_plan(chain_04c(), catalog())
    assert plan["slot"] == 3 and plan["model_command"] and plan["bypass"] == [2] and plan["writes_flash"] is False
    with pytest.raises(ValueError):
        PP.template_preset(chain_04c(), catalog(), send_db=9.0)
    d = PP.template_hlx(chain_04c(), catalog(), reamp.STOMP_INPUT_USB56)
    assert d["data"]["tone"]["dsp0"]["inputA"]["@input"] == reamp.STOMP_INPUT_USB56


def test_session_loads_in_place_and_restores(engine):
    eng, b = engine
    ps = PS.ProbeSession(eng, catalog(), playrec=PS.FakeLoop(latency=100))
    plan = ps.setup(-2.0)
    cmds = [c for c, _ in b.calls]
    assert ("model", {"pos": 3, "model": PP.FX_LOOP_MODEL}) in b.calls
    fx = next(s for s in b.chain["slots"] if s["pos"] == 3)
    vals = {p["sym"]: p["value"] for p in fx["params"]}
    assert vals == {"Send": -2.0, "Return": 0.0, "Mix": 1.0} and fx["enabled"] is True
    assert next(s for s in b.chain["slots"] if s["pos"] == 2)["enabled"] is False          # amp bypassed
    assert b.chain["slots"][0]["routing"] == reamp.STOMP_INPUT_USB56
    assert not {"save", "rename", "restore", "write"} & set(cmds)                     # no flash, no whole write
    assert plan["bypass"] == [2]
    eng.close()
    assert b.chain == b.flash and eng.restore_report["match"]


def test_compile_refusal_routes_in_place(tmp_path):
    exe = tmp_path / "stompd"
    exe.write_text(f"#!{sys.executable}\n# usage: stompd compile <hlx> <base> <out>\n"
                   "import json\nprint(json.dumps({'ok': False, 'error': 'model 141: Send/Return blocks are not built in v0'}))\n")
    exe.chmod(0o755)
    (tmp_path / "t.hlx").write_text("{}")
    r = PS.compile_check(tmp_path / "t.hlx", tmp_path / "b.bin", compiler=exe)
    assert r["ok"] is False and "Send/Return" in r["refused"]


@pytest.mark.skipif(not (PS.COMPILER.exists() and PS.BASE_DOC.exists() and stomp.supports(PS.COMPILER, "compile")),
                    reason="needs the local stompd-compile and the 04C backup")
def test_real_compiler_offline_on_04c(tmp_path):
    from tonematch.catalog import load_catalog

    chain = stomp.decode_document(PS.BASE_DOC, compiler=PS.COMPILER)
    assert PP.template_slot(chain) == 3
    hlx = tmp_path / "t.hlx"
    hlx.write_text(json.dumps(PP.template_hlx(chain, load_catalog(), reamp.STOMP_INPUT_USB56)))
    r = PS.compile_check(hlx, PS.BASE_DOC)
    if r["ok"]:          # a future compiler that builds Send/Return: the document must hold the FX Loop at slot 3
        ch = stomp.decode_document(Path(r["document"]), compiler=PS.COMPILER)
        assert next(s for s in ch["slots"] if s["pos"] == 3)["model_id"] == PP.FX_LOOP_MODEL
    else:
        assert "Send/Return" in r.get("refused", "")


def test_hardware_steps_need_the_human(monkeypatch):
    monkeypatch.setattr(PS, "_open_engine", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened")))
    with pytest.raises(SystemExit, match="human"):
        PS.main(["loopback"])
    with pytest.raises(SystemExit, match="human"):
        PS.main(["capture", "--pedal", "x", "--setting", "y"])


# ------------------------------------------------------------------------------------------- calibration maths

def test_latency_trim_and_lag():
    x, pos = PP.impulse_block()
    y = PP.delay(np.concatenate([x, np.zeros(2000)]) * 0.7, 1207)
    assert PP.impulse_latency(y, pos) == [1207] * PP.IMPULSE_N
    rng = np.random.default_rng(0)
    a = rng.standard_normal(4000)
    assert PP.xcorr_lag(PP.delay(a, 37), a, 100) == 37
    assert PP.xcorr_lag(PP.delay(a, -5), a, 100) == -5
    r = np.arange(10.0)
    assert PP.trim(r, 2, 3, 4).tolist() == [5, 6, 7, 8]
    assert PP.trim(r, 0, -2, 4).tolist() == [0, 0, 0, 1]
    assert PP.trim(r, 8, 0, 4).tolist() == [8, 9, 0, 0]


def _sine_db(y, f):
    from tonematch.metrics import distortion_id as D

    return 20 * np.log10(abs(D.sine_harmonics(np.asarray(y)[4800:-4800], f, SR, 1)[1]))


def test_nuisance_filters():
    t = np.arange(SR) / SR
    for f, want in ((100.0, -2.0), (1000.0, 0.0), (10000.0, 2.0)):
        x = np.sin(2 * np.pi * f * t)
        assert _sine_db(PP.tilt(x, 2.0), f) - _sine_db(x, f) == pytest.approx(want, abs=0.05)
    fc = PP.pickup_corner_hz(10e3)
    assert fc == pytest.approx(1050.4, rel=1e-3)
    x = np.sin(2 * np.pi * fc * t)
    assert _sine_db(PP.pickup_load(x, 10e3), fc) - _sine_db(x, fc) == pytest.approx(-3.0, abs=0.15)
    n = PP.add_noise(np.zeros(SR), -70.0, seed=1)
    assert 20 * np.log10(np.std(n)) == pytest.approx(-70.0, abs=0.1)
    assert np.max(np.abs(PP.gain(np.ones(4), 12.0, clip=True))) == 1.0


def test_loop_calibration_problems():
    cal = PP.LoopCal(latency_samples=10, measurements=[10, 10, 14], gain_1k_db=-2.0, response_db=[0.0] * 49,
                     flatness_db=0.1, noise_dbfs_rms=-100, peak_dbfs=0.0, max_harmonic_db=-40.0, clipped=True, self_lag=9)
    p = " ".join(cal.problems())
    for word in ("disagree", "clipped", "loop gain", "distorts", "lag"):
        assert word in p
    assert not cal.ok


def test_response_correction_inverts_an_output_tilt():
    rng = np.random.default_rng(3)
    x = rng.standard_normal(SR)
    resp = [2.0 * np.log10(f / 1000.0) for f in P.FGRID]
    cal = PP.LoopCal(latency_samples=0, measurements=[0] * 3, gain_1k_db=-3.0, response_db=resp, flatness_db=2.4,
                     noise_dbfs_rms=-100, peak_dbfs=-6, max_harmonic_db=-100, clipped=False, self_lag=0)
    _, done = PP.correct_capture(PP.gain(PP.tilt(x, 2.0), -3.0), cal)
    assert done["response"] and done["gain_db"] == 3.0
    t = np.arange(SR) / SR
    for f in (200.0, 5000.0):
        s = np.sin(2 * np.pi * f * t)
        yc, _ = PP.correct_capture(PP.gain(PP.tilt(s, 2.0), -3.0), cal)
        assert _sine_db(yc, f) == pytest.approx(_sine_db(s, f), abs=0.1)


def test_average_lowers_noise():
    takes = [PP.add_noise(np.zeros(SR), -70.0, seed=i) for i in range(4)]
    assert 20 * np.log10(np.std(PP.average(takes))) == pytest.approx(-76.0, abs=0.3)


# ------------------------------------------------------------------------------------------- fingerprint fixes

def _toy_fp():
    rng = np.random.default_rng(0)
    arr = {"M1": rng.normal(-20, 5, (49, 13)), "Mh": rng.normal(-60, 5, (4, 49, 3)),
           "stair1k_db": rng.normal(-50, 10, (9, 19)), "stair220_db": rng.normal(-50, 10, (9, 19)),
           "imd": rng.normal(-40, 3, (4, 4, 3)), "ss_db": rng.normal(0, 1, 49), "sat_db": rng.normal(0, 1, 49),
           "P_db": np.zeros(49), "Q_db": np.zeros(49), "curve_y": np.linspace(-1, 1, 65)}
    return arr, {"g0_ss_1k_db": 10.0}


def test_shift_level_identity_and_linear_consistency():
    arr, sc = _toy_fp()
    a0, s0 = PP.shift_level(arr, sc, 0.0)
    assert a0 is arr and s0 is sc
    # a linear system: M1 = H(f) + L; a probe heard d dB louder, re-read at -d, gives back the nominal table
    H = np.linspace(-3, 3, 49)
    L = np.asarray(P.SWEEP_LEVELS)
    d = -6.0
    lin = dict(arr, M1=H[:, None] + (L + d)[None])
    a2, s2 = PP.shift_level(lin, {"g0_ss_1k_db": 0.0}, d)
    ok = np.isfinite(a2["M1"])
    assert np.allclose(a2["M1"][ok], (H[:, None] + L[None])[ok])
    i1k = int(np.argmin(np.abs(np.log(P.FGRID / 1000.0))))
    assert s2["g0_ss_1k_db"] == pytest.approx(H[i1k])
    a3, _ = PP.shift_level(arr, sc, 6.0)
    assert np.isnan(a3["M1"][:, 0]).all() and np.isfinite(a3["M1"][:, -1]).all()
    assert np.isnan(a3["stair1k_db"][:, 0]).all()


def test_noise_mask_and_floor():
    arr, _ = _toy_fp()
    nz = {k: np.full_like(np.asarray(v, float), -55.0) for k, v in arr.items()}
    m = PP.noise_mask(arr, nz, margin_db=6.0)
    for k in PP.ABS_KEYS:
        assert np.array_equal(np.isnan(m[k]), ~(arr[k] >= -49.0))
    assert np.array_equal(m["imd"], arr["imd"])
    f = PP.noise_floor_arrays(arr, nz, offset_db=2.0)
    assert np.nanmin(f["stair1k_db"]) >= -53.0 - 1e-9


def test_fingerprint_files_stay_out_of_the_helix_library(tmp_path):
    arr, _ = _toy_fp()
    root = tmp_path / "cache" / "distortion-fp" / "pedals"
    ent = PP.write_fingerprint("My Pedal v2", "D 2:00 T noon", arr, {"g0_ss_1k_db": 10.0},
                               {"knobs": {"drive": "2:00"}, "loopback": "x"}, root=root)
    assert ent["tag"] == "measured-pedal" and ent["model"] == "PEDAL:My Pedal v2" and ent["knobs"] == {"drive": "2:00"}
    a2, s2 = PP.read_fingerprint(Path(ent["file"]))
    assert np.allclose(a2["M1"], arr["M1"].astype(np.float16)) and s2["measured_pedal"]["pedal"] == "My Pedal v2"
    idx = json.loads((root / "My_Pedal_v2" / "index.json").read_text())
    assert idx["version"] == "dfp-v1" and len(idx["settings"]) == 1
    PP.write_fingerprint("My Pedal v2", "D 2:00 T noon", arr, {"g0_ss_1k_db": 10.0}, {}, root=root)
    assert len(json.loads((root / "My_Pedal_v2" / "index.json").read_text())["settings"]) == 1   # replaced
    (tmp_path / "cache" / "distortion-fp" / "My_Pedal_v2").symlink_to(root / "My_Pedal_v2")
    assert S4.load_library(tmp_path, dirs=("My_Pedal_v2",)) == []


# ------------------------------------------------------------------------------------------- end to end (fake device)

@pytest.fixture(scope="module")
def fake_run():
    """L1 + L2 + one toy-pedal capture through FakeLoop: the full 115 s probe, three analyses."""
    loop = PS.FakeLoop(latency=1207, gain_db=-0.4, tilt=1.5, noise_dbfs=-110.0, guitar_in_db=-1.5,
                       send_db=PP.LOOP_SEND_DB)
    x, meta = PP.calibration_signal()
    rec, pre = loop(x)
    cal, _, _ = PP.loop_calibration(rec[:, 0], dict(meta, pre=pre), send_db=PP.LOOP_SEND_DB)
    rec2, pre2 = loop(x)
    cal.input_scale_db = PP.input_scale_db(rec2[:, 4], dict(meta, pre=pre2), cal.latency_samples) - PP.LOOP_SEND_DB
    ped = PS.FakeLoop(latency=1207, gain_db=-0.4, tilt=1.5, noise_dbfs=-110.0, pedal=PS.fake_pedal())
    sig, lay = PP.probe()
    r3, p3 = ped(sig)
    y = PP.trim(r3[:, 0], p3, cal.latency_samples, lay["n"])
    yc, corr = PP.correct_capture(y, cal)
    clean = PS.fake_pedal()(sig.astype(float))
    return {"cal": cal, "corr": corr, "fp": PP.fingerprint(yc), "fp_clean": PP.fingerprint(clean), "y": yc,
            "clean": clean}


def test_fake_loopback_recovers_latency_gain_tilt_and_input_scale(fake_run):
    cal = fake_run["cal"]
    assert cal.latency_samples == 1207 and cal.measurements == [1207] * 3
    assert cal.gain_1k_db == pytest.approx(-0.4, abs=0.05)
    r = dict(zip(P.FGRID, cal.response_db))
    f100 = min(r, key=lambda f: abs(f - 100)), min(r, key=lambda f: abs(f - 10000))
    assert r[f100[0]] == pytest.approx(1.5 * np.log10(f100[0] / 1000), abs=0.15)
    assert r[f100[1]] == pytest.approx(1.5 * np.log10(f100[1] / 1000), abs=0.15)
    assert cal.flatness_db > PP.FLAT_TOL_DB and abs(cal.self_lag) <= 1 and not cal.clipped
    assert cal.max_harmonic_db < -60.0 and cal.ok, cal.problems()
    assert cal.input_scale_db == pytest.approx(-1.5, abs=0.02) and PP.send_db_for(cal) == pytest.approx(1.5, abs=0.02)


def test_fake_capture_matches_the_pedal_without_the_loop(fake_run):
    assert fake_run["corr"]["response"] is True
    a, s = fake_run["fp"]
    a0, s0 = fake_run["fp_clean"]
    assert s["g0_ss_1k_db"] == pytest.approx(s0["g0_ss_1k_db"], abs=0.1)
    assert s["knee_1k_dbfs"] == pytest.approx(s0["knee_1k_dbfs"], abs=0.5)
    m = np.isfinite(a["ss_db"]) & np.isfinite(a0["ss_db"]) & (P.FGRID >= 63) & (P.FGRID <= 16000)
    assert np.max(np.abs(a["ss_db"][m] - a0["ss_db"][m])) < 0.5
    q = PP.capture_quality(fake_run["y"])
    assert not q["clipped"] and abs(q["self_lag"]) <= 1


# ------------------------------------------------------------------------------------------- simulation plumbing

def test_sim_conditions_are_consistent():
    ins = SIM.input_variants()
    outs = SIM.output_variants()
    assert len(ins) == 15 and all(src in ins for src, _ in outs.values())
    assert set(SIM.NOISE_TAKES) <= set(outs)
    assert SIM.level_of("in_lvl_m12") == -12.0 and SIM.level_of("in_lvl_p3") == 3.0 and SIM.level_of("real_l1") == 6.0
    y = np.ones(100)
    assert np.allclose(SIM.apply_ops(y, [("gain", 6.0, False)], 0), 10 ** (6 / 20))
    assert SIM.apply_ops(y, [("delay", 3)], 0)[:3].tolist() == [0, 0, 0]
    assert "mask" in SIM.fixes_for("noise_m70") and "lvl" in SIM.fixes_for("in_lvl_p6") and SIM.fixes_for("lat_2") == ["none"]
