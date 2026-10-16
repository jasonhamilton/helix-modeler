"""block_coverage_report: category mapping, tested-set derivation from fixture data, SVG present,
no audio paths, writes only under reports/local. A tiny synthetic project in tmp_path."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("block_coverage_report",
                                              REPO / "scripts" / "block_coverage_report.py")
bcr = importlib.util.module_from_spec(spec)
sys.modules["block_coverage_report"] = bcr
spec.loader.exec_module(bcr)

SR = 48_000


def model(mid: str, name: str, src: str, stomp: bool = True, fw: str | None = None,
          params: dict | None = None) -> dict:
    return {"id": mid, "name": name, "category": "x", "source_file": src, "stomp": stomp,
            "stomp_min_firmware": fw, "mono": True, "stereo": False, "load": 5.0,
            "params": params or {}}


KNOB = {"kind": "continuous", "min": 0.0, "max": 1.0, "default": 0.5, "display": "generic_knob"}
PCT = {"kind": "continuous", "min": 0.0, "max": 1.0, "default": 0.0, "display": "percent"}

CATALOG = {
    "_source": {"hxedit_version": "3.82"},
    "models": {m["id"]: m for m in [
        model("HD2_AmpFoo", "Foo Amp", "amp.models", params={"Drive": KNOB}),
        model("HD2_AmpNew", "New Amp", "amp.models", fw="0x03690000"),       # needs fw 3.69
        model("HD2_AmpUntestedSecret", "Secret Amp", "amp.models"),
        model("HD2_PreampFoo", "Foo Preamp", "preamp.models"),
        model("HD2_CabBar", "Bar Cab", "cab.models", params={"EarlyReflections": PCT}),
        model("HD2_CabMicIr_Baz", "Baz IR Cab", "cabmicirs.models"),
        model("HD2_CabMicIr_BazWithPan", "Baz IR Cab pan", "cabmicirswithpan.models"),
        model("HD2_DelayEcho", "Echo", "delay.models", params={"Mix": PCT}),
        model("HD2_DistOD", "Over Drive", "distortion.models"),
        model("HD2_GateNoise", "Noise Gate", "gate.models"),
        model("HD2_CompressorSq", "Squeeze", "compressor.models"),
        model("HD2_ImpulseResponse1024", "IR 1024", "fixed.models"),
        model("HD2_Looper", "Looper", "fixed.models"),
        model("HD2_AppDSPFlow1Input", "Input", "io.models"),
        model("HD2_FilterAuto", "Auto Filter", "filter.models"),
        model("HD2_WahTear", "Tear Wah", "wah.models"),
        model("HD2_VolPanGain", "Gain", "volumepan.models"),
        model("HD2_FXLoopMono3", "FX Loop 3", "sendreturn.models", stomp=False),
        model("HD2_PitchWham", "Wham", "pitch-synth.models"),
        model("HD2_ChorusX", "Chorus X", "modulation.models"),
        model("HD2_ReverbY", "Reverb Y", "reverb.models"),
        model("HD2_EQPara", "Para EQ", "eq.models"),
    ]},
}


def hlx(amp_drive: float = 0.5, delay_on: bool = False, er: float = 0.0, name: str = "t") -> dict:
    return {"data": {"meta": {"name": name}, "tone": {"dsp0": {
        "inputA": {"@model": "HelixStomp_AppDSPFlowInput"},
        "block3": {"@model": "HD2_AmpFoo", "@enabled": True, "@position": 1, "@path": 0,
                   "@cab": "cab0", "Drive": amp_drive},
        "block5": {"@model": "HD2_DelayEcho", "@enabled": delay_on, "@position": 2, "@path": 0,
                   "Mix": 0.23},
        "cab0": {"@model": "HD2_CabBar", "@mic": 6, "@enabled": True, "EarlyReflections": er},
        "outputA": {"@model": "HelixStomp_AppDSPFlowOutputMain"}}}}}


def write(p: Path, obj: object) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def parity_result(null: float, tail: float | None = None) -> dict:
    return {"null_residual_db": null, "lag_ms": 0.2, "xcorr_peak": 0.99, "gain_db": -0.1,
            "native_rms_db": -26.0, "body_null_residual_db": null,
            "tail_null_residual_db": tail if tail is not None else null,
            "bands": [{"centre_hz": c, "native_db": -20.0, "residual_db_re_native_band": null,
                       "stomp_minus_native_db": 0.01} for c in (125.0, 1000.0, 8000.0)],
            "files": {"stereo": "x_stereo.wav", "residual": "x_residual.wav"}}


def make_run(root: Path, run: str, results: dict[str, dict]) -> Path:
    d = root / "cache" / "parity" / run
    rng = np.random.default_rng(1)
    for name, res in results.items():
        x = rng.standard_normal((SR, 2)).astype(np.float32) * 0.05
        res["files"] = {"stereo": f"{name}_stereo.wav", "residual": f"{name}_residual.wav"}
        d.mkdir(parents=True, exist_ok=True)
        sf.write(d / f"{name}_stereo.wav", x, SR, subtype="FLOAT")
        sf.write(d / f"{name}_residual.wav", x[:, 0] * 0.01, SR, subtype="FLOAT")
    write(d / "summary.json", {"dataset": "parity-v0", "di": "cache/parity/di_10s_tail.wav",
                               "presets": list(results), "results": results,
                               "started": "2026-10-15T00:00:00Z"})
    return d


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write(root / "cache" / "catalog" / "current.json", CATALOG)
    write(root / "cache" / "stomp" / "inject" / "K.hlx", hlx(0.61, name="K"))
    write(root / "cache" / "stomp" / "inject" / "A.hlx", hlx(0.95, name="A"))
    write(root / "cache" / "stomp" / "inject" / "E.hlx", hlx(0.61, er=0.5, name="E"))
    write(root / "cache" / "stomp" / "inject" / "D.hlx", hlx(0.61, delay_on=True, name="D"))
    make_run(root, "20261015T000000Z", {"K": parity_result(-31.0), "A": parity_result(-30.0),
                                        "E": parity_result(-9.0),
                                        "D": parity_result(-19.0, tail=4.0)})
    # parity files must be newer than the presets they ran (stale-file guard)
    for f in (root / "cache" / "parity").rglob("summary.json"):
        f.touch()
    (root / "ledger").mkdir()
    (root / "src" / "tonematch").mkdir(parents=True)
    write(root / "src" / "tonematch" / "param_policy.json", {"rules": [{
        "category": "cab", "param": "EarlyReflections",
        "engines": {"stomp": {"action": "freeze", "value": 0.0}}, "evidence": "fixture"}]})
    return root


def tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------

def test_category_mapping_uses_gui_names() -> None:
    m = CATALOG["models"]
    got = {mid: bcr.gui_category(v) for mid, v in m.items()}
    assert got["HD2_AmpFoo"] == "Amp"
    assert got["HD2_PreampFoo"] == "Preamp"
    assert got["HD2_CabBar"] == "Cab" and got["HD2_CabMicIr_Baz"] == "Cab"
    assert got["HD2_GateNoise"] == "Dynamics" and got["HD2_CompressorSq"] == "Dynamics"
    assert got["HD2_ImpulseResponse1024"] == "Impulse Response" and got["HD2_Looper"] == "Looper"
    assert got["HD2_FXLoopMono3"] == "Send/Return" and got["HD2_VolPanGain"] == "Volume/Pan"
    assert got["HD2_PitchWham"] == "Pitch/Synth" and got["HD2_WahTear"] == "Wah"
    assert got["HD2_FilterAuto"] == "Filter" and got["HD2_DistOD"] == "Distortion"
    assert got["HD2_AppDSPFlow1Input"] is None            # routing end, not a block
    assert got["HD2_CabMicIr_BazWithPan"] is None         # pan twin, not a separate GUI entry
    assert set(filter(None, got.values())) <= set(bcr.GUI_CATEGORIES)


def test_stomp_availability_follows_firmware() -> None:
    m = CATALOG["models"]
    assert bcr.stomp_usable(m["HD2_AmpFoo"]) == (True, False)
    assert bcr.stomp_usable(m["HD2_AmpNew"]) == (False, True)        # fw 3.69 > pedal 3.50
    assert bcr.stomp_usable(m["HD2_FXLoopMono3"]) == (False, False)  # not a Stomp model


def test_hlx_blocks_orders_cab_after_its_amp() -> None:
    blocks = bcr.hlx_blocks(hlx())
    assert [b["model"] for b in blocks] == ["HD2_AmpFoo", "HD2_CabBar", "HD2_DelayEcho"]
    assert blocks[1]["params"]["@mic"] == 6


def test_tested_set_is_derived_from_the_fixture(project: Path) -> None:
    res = bcr.generate(project, project / "reports" / "local" / "blocks")
    models = res["model"]["models"]
    assert set(models) == {"HD2_AmpFoo", "HD2_CabBar", "HD2_DelayEcho"}
    amp = models["HD2_AmpFoo"]
    assert amp["stomp_rendered"] and amp["native_rendered"]   # parity renders on both engines
    assert "parity" in amp["stomp_hows"]
    dash = {d["cat"]: d for d in res["model"]["dash"]}
    assert dash["Amp"]["native_avail"] == 3 and dash["Amp"]["stomp_avail"] == 2
    assert dash["Amp"]["stomp_newfw"] == 1
    assert dash["Amp"]["stomp_tested"] == 1 and dash["Amp"]["native_tested"] == 1
    assert dash["Preamp"]["stomp_tested"] == 0
    assert "Cab" in dash and dash["Cab"]["native_avail"] == 2      # BazWithPan not counted


def test_parity_status_and_blame(project: Path) -> None:
    res = bcr.generate(project, project / "reports" / "local" / "blocks")
    m = res["model"]["models"]
    assert m["HD2_AmpFoo"]["status"] == "in parity"            # E and D fail but not because of it
    assert m["HD2_CabBar"]["status"] == "partial"              # ER 0.5 breaks the policy value
    assert any("EarlyReflections" in n for n in m["HD2_CabBar"]["notes"])
    assert m["HD2_DelayEcho"]["status"] == "mismatch"          # only enabled in the failing run
    assert res["model"]["dash"][[d["cat"] for d in res["model"]["dash"]].index("Delay")]["mismatch"] == 1


def test_voided_parity_rows_are_skipped(project: Path) -> None:
    (project / "ledger" / "runs.csv").write_text(
        "timestamp,commit,agent,kind,engine,dataset,metric,value,renders,wall_clock_s,notes\n"
        "2026-10-15T01:00:00+00:00,c,a,parity,both,parity-v0,null_residual_db,-9.0,,,preset=E;x\n",
        encoding="utf-8")
    (project / "ledger" / "void.csv").write_text(
        "timestamp,metric,reason\n2026-10-15T01:00:00+00:00,null_residual_db,test\n", encoding="utf-8")
    res = bcr.generate(project, project / "reports" / "local" / "blocks")
    assert res["model"]["models"]["HD2_CabBar"]["status"] == "in parity"
    assert any("voided" in u for u in res["evidence"].unresolved)


def test_third_octave_bands_find_the_tone() -> None:
    t = np.arange(SR) / SR
    centres, lv = bcr.third_octave_db(np.sin(2 * np.pi * 1000 * t) * 0.5)
    assert abs(centres[int(np.argmax(lv))] - 1000) < 5
    assert lv.max() > lv.min() + 40


def test_html_has_svg_dashboard_and_untested_names_collapsed(project: Path) -> None:
    out = project / "reports" / "local" / "blocks"
    bcr.generate(project, out)
    page = (out / "index.html").read_text(encoding="utf-8")
    assert page.count("<svg") >= 5 and 'role="img"' in page
    assert "Dashboard by GUI category" in page and "id=\"cat-amp\"" in page
    assert "prefers-color-scheme:dark" in page and "viewport" in page
    assert "<script" not in page and "http://" not in page.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in page                                   # no external requests
    m = re.search(r"<details><summary>\d+ untested models in Amp.*?</details>", page, re.DOTALL)
    assert m and "Secret Amp" in m[0]                              # local version names untested models
    md = (out / "README.md").read_text(encoding="utf-8")
    assert "| Amp |" in md and "Secret Amp" in md


def test_no_audio_paths_or_song_material_in_outputs(project: Path) -> None:
    out = project / "reports" / "local" / "blocks"
    bcr.generate(project, out)
    for f in out.iterdir():
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"\.(wav|flac|mp3|aiff?)\b", text), f.name
        assert "target_stem" not in text and "/stems/" not in text and "htdemucs" not in text


def test_writes_nothing_outside_reports_local(project: Path) -> None:
    before = tree(project)
    bcr.generate(project, project / "reports" / "local" / "blocks", json_path=project / "reports" / "local" / "x.json")
    new = tree(project) - before
    assert new and all(p.startswith("reports/local/") for p in new), new
    assert not (project / "reports" / "blocks").exists()           # no public version
    with pytest.raises(SystemExit):
        bcr.generate(project, project / "reports" / "blocks")
    with pytest.raises(SystemExit):
        bcr.generate(project, project / "elsewhere")
    assert not (project / "reports" / "blocks").exists() and not (project / "elsewhere").exists()


def test_reports_local_is_gitignored_and_no_public_folder_in_repo() -> None:
    if not shutil.which("git") or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    r = subprocess.run(["git", "check-ignore", "-q", "reports/local/blocks/index.html"], cwd=REPO,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, "reports/local/ must be gitignored"
    tracked = subprocess.run(["git", "ls-files", "reports/local", "reports/blocks"], cwd=REPO,
                             capture_output=True, text=True, check=False).stdout
    assert tracked.strip() == ""


def test_stale_preset_file_is_not_trusted(project: Path) -> None:
    import os
    import time
    f = project / "cache" / "stomp" / "inject" / "K.hlx"
    future = time.time() + 3600
    os.utime(f, (future, future))
    res = bcr.generate(project, project / "reports" / "local" / "blocks")
    assert any("rewritten" in u for u in res["evidence"].unresolved)
