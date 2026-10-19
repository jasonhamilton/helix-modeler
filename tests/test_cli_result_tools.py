"""One cheap smoke test per tool wired to --result-json: run it in a hardware-free mode and read the manifest."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
SR = 48_000
GOLDEN = ROOT / "fixtures" / "golden" / "HXStomp.hlx"


def manifest(p) -> dict:
    d = json.loads(Path(p).read_text())
    assert d["schema"] == "cli-result/v0"
    return d


def test_proc_check(tmp_path, monkeypatch):
    import proc_check as PC

    monkeypatch.setattr(PC, "ps_rows", list)
    out = tmp_path / "r.json"
    assert PC.main(["--result-json", str(out)]) == 0
    d = manifest(out)
    assert d["tool"] == "proc_check" and d["status"] == "ok"
    assert d["metrics"]["orphans"] == 0 and d["metrics"]["zombies"] == 0


def test_proc_check_flags_a_zombie_as_exit_1(tmp_path, monkeypatch):
    import proc_check as PC

    z = {"pid": 9, "ppid": 8, "stat": "Z", "pcpu": 0.0, "etime": "1", "cmd": "python x"}
    monkeypatch.setattr(PC, "ps_rows", lambda: [z])
    out = tmp_path / "r.json"
    assert PC.main(["--result-json", str(out)]) == 1
    d = manifest(out)
    assert d["status"] == "error" and d["exit_code"] == 1 and d["metrics"]["zombie_pids"] == [9]


def test_licence_check(tmp_path):
    import licence_check as LC

    root = tmp_path / "repo"
    (root / "src" / "tonematch" / "engines").mkdir(parents=True)
    (root / "src" / "tonematch" / "engines" / "native.py").write_text("# LICENCE BOUNDARY\n")
    (root / ".gitignore").write_text("fixtures/golden/*.hlx\n")
    out = tmp_path / "r.json"
    rc = LC.main(["--root", str(root), "--no-write", "--date", "2026-01-01", "--result-json", str(out)])
    d = manifest(out)
    assert d["tool"] == "licence_check" and d["exit_code"] == rc
    assert d["status"] == ("ok" if rc == 0 else "error")
    assert d["metrics"]["overall"] in ("PASS", "FAIL")
    assert "Dependency licences" in d["metrics"]["sections"] and d["outputs"] == {}


@pytest.mark.skipif(not GOLDEN.exists(), reason="fixtures/golden is local only")
def test_hlx_validate(tmp_path):
    import hlx_validate as HV

    out = tmp_path / "r.json"
    rc = HV.main([str(GOLDEN), "--no-report", "--cache-dir", str(tmp_path), "--result-json", str(out)])
    d = manifest(out)
    assert d["tool"] == "hlx_validate" and d["exit_code"] == rc
    assert d["inputs"]["preset"].endswith("HXStomp.hlx") and "n_fail" in d["metrics"]
    assert d["status"] == ("ok" if rc == 0 else "error")


def test_hlx_validate_unreadable_input_is_exit_2(tmp_path):
    import hlx_validate as HV

    bad = tmp_path / "bad.hlx"
    bad.write_text("not json")
    out = tmp_path / "r.json"
    assert HV.main([str(bad), "--no-report", "--cache-dir", str(tmp_path), "--result-json", str(out)]) == 2
    assert manifest(out)["exit_code"] == 2 and manifest(out)["status"] == "error"


def test_engine_smoke_no_engine_found(tmp_path, monkeypatch):
    import engine_smoke as ES

    from tonematch.engines import registry

    monkeypatch.setattr(registry, "detect", dict)
    out = tmp_path / "r.json"
    assert ES.main(["--no-ledger", "--result-json", str(out)]) == 2
    d = manifest(out)
    assert d["status"] == "error" and d["exit_code"] == 2 and d["metrics"]["engines_detected"] == []
    assert "No engine found" in d["notes"]


def test_engine_smoke_with_a_fake_engine(tmp_path, monkeypatch):
    import engine_smoke as ES

    from tonematch import preset
    from tonematch.engines import registry

    class Fake:
        def load(self, p): ...
        def close(self): ...
        def render(self, dry):
            x = np.asarray(dry, np.float32)
            return (x / max(float(np.max(np.abs(x))), 1e-9) * 0.3).astype(np.float32)
        def cost(self): return SimpleNamespace(seconds_per_render=0.01)
        def capabilities(self): return SimpleNamespace(firmware_version="3.82", models=["m"])

    known = tmp_path / "known.hlx"
    known.write_text("{}")
    monkeypatch.setattr(ES, "PRESET", known)
    monkeypatch.setattr(preset, "read_hlx", lambda p: object())
    monkeypatch.setattr(registry, "detect", lambda: {"native": Fake})
    out = tmp_path / "r.json"
    assert ES.main(["--engine", "native", "--di", "synthetic", "--no-ledger", "--result-json", str(out)]) == 0
    d = manifest(out)
    assert d["status"] == "ok" and d["metrics"]["engines"]["native"]["ok"] is True
    assert {r["metric"] for r in d["ledger_rows"]} == {"peak_dbfs", "seconds_per_render"}
    assert all(r["written"] is False for r in d["ledger_rows"])          # --no-ledger: would-be rows
    assert d["inputs"]["di"].endswith("di_2s.wav")


def test_separate_with_a_fake_separator(tmp_path, monkeypatch):
    import separate as SEP

    cache = tmp_path / "stems" / "h" / "htdemucs_6s"
    cache.mkdir(parents=True)
    (cache / "manifest.json").write_text("{}")
    st = SimpleNamespace(cache_dir=cache, cached=True, model="htdemucs_6s", backend="cpu", wall_clock_s=1.5,
                         song_hash="abcdef0123", user_supplied=False)
    mask = SimpleNamespace(values=np.ones((3, 3)), energy_weighted_mean=0.7, exposed_fraction=0.25,
                           summary=lambda: {"mean": 0.7})
    secs = SimpleNamespace(tempo_bpm=120, bar_snapshot=[0, 0], n_snapshots=1, exposed_windows=[], overridden=False,
                           save=lambda p: Path(p).write_text("{}"), table=lambda: "table")
    monkeypatch.setattr(SEP, "run_separate", lambda *a, **k: st)
    monkeypatch.setattr(SEP, "confidence_mask", lambda s: mask)
    monkeypatch.setattr(SEP, "segment", lambda s: secs)
    out = tmp_path / "r.json"
    assert SEP.main([str(tmp_path / "song.mp3"), "--no-ledger", "--result-json", str(out)]) == 0
    d = manifest(out)
    assert d["tool"] == "separate" and d["status"] == "ok"
    assert d["metrics"]["model"] == "htdemucs_6s" and d["metrics"]["tempo_bpm"] == 120
    assert d["outputs"]["stems_dir"].endswith("htdemucs_6s") and d["outputs"]["guitar_wav"].endswith("guitar.wav")


def test_separate_unavailable_is_exit_2(tmp_path, monkeypatch):
    import separate as SEP

    def boom(*a, **k):
        raise SEP.SeparatorUnavailable("no torch")
    monkeypatch.setattr(SEP, "run_separate", boom)
    out = tmp_path / "r.json"
    assert SEP.main(["x.mp3", "--result-json", str(out)]) == 2
    d = manifest(out)
    assert d["status"] == "error" and "no torch" in d["notes"]


def _bursts(times, n_s=10.0):
    x = np.zeros(int(n_s * SR), np.float32)
    rng = np.random.default_rng(0)
    for t in times:
        i = int(t * SR)
        x[i:i + 4000] = (rng.standard_normal(4000) * np.hanning(4000) * 0.5).astype(np.float32)
    return x


def test_align_takes(tmp_path):
    import align_takes as AT

    times = [0.5, 1.4, 2.1, 3.3, 4.0, 5.2, 6.1, 7.4, 8.2]
    sf.write(str(tmp_path / "stem.wav"), _bursts(times), SR, subtype="FLOAT")
    take = tmp_path / "t1_dry.wav"
    sf.write(str(take), _bursts([t + 0.3 for t in times]), SR, subtype="FLOAT")
    out = tmp_path / "r.json"
    assert AT.main([str(take), "--stem", str(tmp_path / "stem.wav"), "--result-json", str(out)]) == 0
    d = manifest(out)
    assert d["tool"] == "align_takes" and d["status"] == "ok"
    assert d["metrics"]["best_take"] == "t1_dry.wav" and "t1_dry.wav" in d["metrics"]["takes"]
    assert len(d["outputs"]["align_json"]) == 1 and Path(d["outputs"]["align_json"][0]).is_file()
    assert abs(d["metrics"]["takes"]["t1_dry.wav"]["global_lag_s"]) == pytest.approx(0.3, abs=0.05)


def _fake_audio(monkeypatch, P):
    monkeypatch.setattr(P.reamp, "find_device", lambda: 7)
    monkeypatch.setattr(P.reamp.Calibration, "load", classmethod(
        lambda cls, *a, **k: SimpleNamespace(latency_samples=10, firmware="3.50")))

    def duplex(play, play_ch, dev, *a, **k):
        rec = np.zeros((len(play), 8), np.float32)
        rec[:, 4] = 0.3 * np.sin(np.arange(len(play)) / 20)       # dry on USB in 5
        rec[:, 0] = 0.2 * np.sin(np.arange(len(play)) / 20)       # processed on USB in 1/2
        rec[:, 1] = rec[:, 0]
        return rec
    monkeypatch.setattr(P.reamp, "duplex", duplex)


def test_playalong_record_check_and_takes(tmp_path, monkeypatch):
    import playalong_record as P

    _fake_audio(monkeypatch, P)
    out = tmp_path / "check.json"
    assert P.main(["--check", "--result-json", str(out)]) == 0
    d = manifest(out)
    assert d["metrics"]["mode"] == "check" and "dry_peak_dbfs" in d["metrics"]

    pack = tmp_path / "pack"
    pack.mkdir()
    sf.write(str(pack / "back.wav"), np.zeros((SR, 2), np.float32), SR, subtype="FLOAT")
    out = tmp_path / "take.json"
    assert P.main(["--pack", str(pack), "--backing", "back.wav", "--takes", "1", "--countdown", "0",
                   "--result-json", str(out)]) == 0
    d = manifest(out)
    assert d["metrics"]["mode"] == "record" and d["metrics"]["takes"] == 1
    assert len(d["outputs"]["dry_wavs"]) == 1 and Path(d["outputs"]["dry_wavs"][0]).is_file()
    assert Path(d["outputs"]["session_json"]).is_file()


def test_match_song_dry_run(tmp_path, monkeypatch):
    import match_song as MS
    from test_match_song import _song

    song, mask = _song()
    d = tmp_path / "stems"
    d.mkdir()
    sf.write(str(d / "guitar.wav"), song.guitar, SR, subtype="FLOAT")
    sf.write(str(d / "others.wav"), song.others, SR, subtype="FLOAT")
    np.save(d / "confidence.npy", mask.astype(np.float16))
    (d / "sections.json").write_text(json.dumps(song.sections))
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(MS, "build_candidates", lambda *a, **k: ([], []))
    out = tmp_path / "r.json"
    rc = MS.main(["--stems-dir", str(d), "--song", "fake", "--out", str(tmp_path / "out"), "--dur", "4",
                  "--di-library", str(lib), "--dry-run", "--result-json", str(out)])
    assert rc == 0
    m = manifest(out)
    assert m["tool"] == "match_song" and m["status"] == "ok" and m["metrics"]["dry_run"] is True
    assert m["outputs"]["out_dir"].endswith("out") and m["args"]["dry_run"] is True
    assert m["inputs"]["stems_dir"] == str(d)


def test_match_block_wraps_run(tmp_path, monkeypatch):
    import match_block as MB

    def fake_run(args, engine=None, rows_by_shard=None):
        o = Path(args.out)
        o.mkdir(parents=True, exist_ok=True)
        (o / "best.hlx").write_text("{}")
        (o / "result.json").write_text(json.dumps({
            "label": "unadmitted loss", "renders": 12, "wall_s": 3.0, "workers": 1, "n_models": 4,
            "finals": [{"model": "HD2_DistA", "loss": 1.5}, {"model": "HD2_DistB", "loss": 2.5}]}))
        return 0
    monkeypatch.setattr(MB, "run", fake_run)
    out = tmp_path / "r.json"
    rc = MB.main(["--target-window", "1:5", "--di", "x.wav", "--out", str(tmp_path / "o"), "--result-json", str(out)])
    assert rc == 0
    m = manifest(out)
    assert m["tool"] == "match_block" and m["metrics"]["best_model"] == "HD2_DistA"
    assert m["metrics"]["top"][1] == {"model": "HD2_DistB", "loss": 2.5}
    assert m["outputs"]["best_hlx"].endswith("best.hlx") and m["inputs"]["di"] == "x.wav"


def test_synthetic_v1_summarize(tmp_path, monkeypatch):
    import synthetic_v1 as sv
    from test_synthetic_v1 import item

    sd = tmp_path / "set"
    (sd / "runs" / "t").mkdir(parents=True)
    (sd / "manifest.json").write_text(json.dumps({"pool": {"drive": {"a": 1, "b": 2}}, "categories": ["drive"]}))
    (sd / "labels.json").write_text(json.dumps([{"id": "i000", "category": "drive"}]))
    (sd / "runs" / "t" / "i000.json").write_text(json.dumps(item("drive")))
    old = (sv.DATASET, sv.SET_DIR, sv.CATEGORIES)
    monkeypatch.setattr(sv, "set_paths", lambda name: setattr(sv, "SET_DIR", sd))
    try:
        out = tmp_path / "r.json"
        assert sv.main(["summarize", "--tag", "t", "--result-json", str(out)]) == 0
    finally:
        sv.DATASET, sv.SET_DIR, sv.CATEGORIES = old
    m = manifest(out)
    assert m["tool"] == "synthetic_v1 summarize" and m["status"] == "ok"
    assert m["metrics"]["block_f1"] == 1.0 and m["metrics"]["items"] == 1
    assert m["args"]["tag"] == "t"


def test_synthetic_v1_summarize_error_is_recorded(tmp_path, monkeypatch):
    import synthetic_v1 as sv

    monkeypatch.setattr(sv, "set_paths", lambda name: setattr(sv, "SET_DIR", tmp_path / "missing"))
    old = sv.SET_DIR
    try:
        out = tmp_path / "r.json"
        with pytest.raises(FileNotFoundError):
            sv.main(["summarize", "--result-json", str(out)])
    finally:
        sv.SET_DIR = old
    assert manifest(out)["status"] == "error" and "FileNotFoundError" in manifest(out)["error"]


def test_loss_trust_check_proxy(tmp_path):
    import loss_trust_check as LT

    out = tmp_path / "r.json"
    rep = tmp_path / "rep.json"
    rc = LT.main(["--term", "level_db", "--proxy", "--items", "2", "--distractors", "2", "--seconds", "3",
                  "--jobs", "1", "--out", str(rep), "--result-json", str(out)])
    assert rc == 0
    m = manifest(out)
    assert m["tool"] == "loss_trust_check" and m["status"] == "ok"
    assert m["outputs"]["report_json"] == str(rep) and rep.is_file()
    assert m["metrics"]["proxy"] is True and m["ledger_rows"] == []
    assert "level_db" in json.dumps(m["metrics"]["rank1_rate"])


def test_no_di_invariance_error_path(tmp_path):
    import no_di_invariance as ND

    out = tmp_path / "r.json"
    with pytest.raises(FileNotFoundError):
        ND.main(["report", "--inp", str(tmp_path / "nope.json"), "--result-json", str(out)])
    m = manifest(out)
    assert m["tool"] == "no_di_invariance report" and m["status"] == "error"


def test_gate2_report_build(tmp_path):
    import gate2_report as G
    from test_gate2_report import make_project

    root = make_project(tmp_path / "proj")
    out = tmp_path / "r.json"
    assert G.main(["build", "--root", str(root), "--out", str(tmp_path / "site"), "--no-audio",
                   "--result-json", str(out)]) == 0
    m = manifest(out)
    assert m["tool"] == "gate2_report build" and m["status"] == "ok"
    assert m["outputs"]["index_html"].endswith("index.html") and Path(m["outputs"]["index_html"]).is_file()
    assert m["metrics"]["audio"] is False
