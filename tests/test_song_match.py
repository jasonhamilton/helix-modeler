"""T217: songs.py resolution, scripts/song_match.py plan and resume (processes faked, no Native, no real audio), finetune_knobs job resolution."""
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
sys.path.insert(0, str(ROOT / "src"))
from tonematch import songs as SG  # noqa: E402

import song_match as SM  # noqa: E402

SR = 48_000


def make_stems(root: Path, hash_: str, source: str, seconds: float = 3.0) -> Path:
    d = root / hash_ / "htdemucs_6s"
    d.mkdir(parents=True)
    rng = np.random.default_rng(0)
    sf.write(d / "guitar.wav", (0.1 * rng.standard_normal(int(seconds * SR))).astype("float32"), SR)
    (d / "manifest.json").write_text(json.dumps({"source": source, "song_hash": hash_}))
    return d


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    stems = tmp_path / "stems"
    a = make_stems(stems, "aaaaaaaaaaaaaaaa", "data/songs/Nirvana - Smells Like Teen Spirit (Official Music Video).mp3")
    b = make_stems(stems, "bbbbbbbbbbbbbbbb", "data/songs/Mother_0-68s.wav")
    c = make_stems(stems, "cccccccccccccccc", "data/songs/Mother Nature.mp3")
    monkeypatch.setattr(SG, "STEMS_ROOT", stems)
    monkeypatch.setattr(SG, "SONGS_ROOT", tmp_path / "songs")
    monkeypatch.setattr(SG, "MATCHES_ROOT", tmp_path / "matches")
    return SimpleNamespace(root=tmp_path, a=a, b=b, c=c)


def test_resolve_by_id_hash_and_slug(lib):
    assert SG.resolve("teen-spirit").stems_dir == lib.a
    assert SG.resolve("bbbbbb").stems_dir == lib.b                     # hash prefix
    assert SG.resolve("mother-0-68s").stems_dir == lib.b               # the slug find_stems_all uses
    r = SG.resolve("aaaaaaaaaaaaaaaa")
    assert r.id == "nirvana-smells-like-teen-spirit-official-music-video"


def test_resolve_ambiguous_and_missing(lib):
    with pytest.raises(ValueError, match="matches 2"):
        SG.resolve("mother")
    with pytest.raises(SG.SongNotFound):
        SG.resolve("nothing-like-it")


def test_resolve_file_separates_when_needed(lib, tmp_path):
    f = tmp_path / "My Song.mp3"
    f.write_bytes(b"x")
    with pytest.raises(SG.SongNotFound):
        SG.resolve(str(f))
    new = make_stems(SG.STEMS_ROOT, "dddddddddddddddd", "data/songs/other.mp3")
    got = SG.resolve(str(f), separate=lambda p: new)
    assert got.id == "my-song" and got.stems_dir == new
    known = tmp_path / "Mother_0-68s.wav"
    known.write_bytes(b"x")
    assert SG.resolve(str(known)).stems_dir == lib.b                    # found by its manifest source name, no separation


def test_layout_names_match_the_old_runs(lib):
    assert SG.match_dir("mother", "-prs7").name == "mother-dryrebuild-prs7"
    assert SG.finetune_dir("mother", "-prs7").name == "mother-finetune-prs7"
    assert SG.playlevel_dir("mother", "-prs7").parts[-2:] == ("mother-rebuilt", "playlevel-prs7")
    assert SG.parse_window("28.3:38.3") == (28.3, 38.3)
    with pytest.raises(ValueError):
        SG.parse_window("5:2")


def test_scale_to_level_hits_the_level_and_caps_the_peak():
    from tonematch.dry_rebuild import loud_level_db

    x = (0.01 * np.random.default_rng(1).standard_normal(SR)).astype("float32")
    y, g, got = SM.scale_to_level(x, -28.08)
    assert abs(loud_level_db(y) - (-28.08)) < 0.1
    hot, *_ = SM.scale_to_level(x, -3.0)
    assert np.abs(hot).max() <= 10 ** (-1 / 20) + 1e-6


class FakeGuard:
    def wait(self, *a, **k):
        return 0.0


def fake_shell(calls, lib, fail=None):
    def sh(cmd, log_file=None, env=None):
        cmd = [str(c) for c in cmd]
        script = Path(cmd[1]).name if len(cmd) > 1 else ""
        calls.append(script)
        if script == fail:
            return 1
        get = lambda flag: Path(cmd[cmd.index(flag) + 1])  # noqa: E731
        if script == "transcribe_stems.py":
            sid = cmd[cmd.index("--songs") + 1]
            p = SG.transcription_mid(sid)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"mid")
        elif script == "rebuild_dry.py":
            out = get("--out")
            out.mkdir(parents=True, exist_ok=True)
            name = "rebuilt_dry_profiled.wav" if "--profile" in cmd else "rebuilt_dry_ref.wav"
            sf.write(out / name, (0.05 * np.random.default_rng(2).standard_normal(SR)).astype("float32"), SR)
        elif script == "match_block.py":
            assert "--target-stems" in cmd and "--shortlist" in cmd and cmd[cmd.index("--shortlist") + 1] == "afxrep"
            out = get("--out")
            (out / "listen").mkdir(parents=True)
            (out / "best.hlx").write_text("{}")
            for f in ("target_stem", "match", "di_dry", "ab_L-target_R-match"):
                sf.write(out / "listen" / f"{f}.wav", np.zeros(100, "float32"), SR)
            (out / "result.json").write_text(json.dumps({
                "window": {"start_s": 1.0, "end_s": 2.0}, "renders": 50, "wall_s": 10.0, "low_confidence": False,
                "shortlist": {"ranked": [{"model": "HD2_DistZeroAmpBassDI", "distance": 0.3}, {"model": "HD2_DistB", "distance": 0.4}]},
                "finals": [{"model": "HD2_DistZeroAmpBassDI", "values": {"Drive": 0.4}}]}))
        elif script == "finetune_knobs.py":
            assert "--match-dir" in cmd and "--stems-dir" in cmd
            out = get("--out")
            (out / "listen").mkdir(parents=True)
            (out / "best.hlx").write_text("{}")
            for f in ("target_stem", "match", "di_dry", "ab_L-target_R-match", "t207_match"):
                sf.write(out / "listen" / f"{f}.wav", np.zeros(100, "float32"), SR)
            (out / "result.json").write_text(json.dumps({
                "model": "HD2_DistZeroAmpBassDI", "final_values": {"Drive": 0.5}, "start_values": {"Drive": 0.4}, "dist_before": 0.3,
                "dist_after": 0.25, "renders": 121, "stopped": "x", "wall_s": 5.0}))
        return 0
    return sh


@pytest.fixture()
def patched(monkeypatch):
    import match_song as MS
    from tonematch.engines import host_budget

    monkeypatch.setattr(host_budget, "LoadGuard", FakeGuard)
    monkeypatch.setattr(MS, "validate_hlx", lambda p: (0, "ok"))


def args(lib, **kw):
    base = dict(song="teen-spirit", id=None, guitar_id="g1", window="1:2", level_db=None, chords=None, finetune=True,
                workers="3", out=str(lib.root / "run"), force=False, dry_run=False, load_stomp=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_full_run_then_resume(lib, patched):
    calls = []
    a = args(lib)
    assert SM.run_pipeline(a, sh=fake_shell(calls, lib), log=lambda m: None) == 0
    assert calls.count("transcribe_stems.py") == 1 and calls.count("rebuild_dry.py") == 1
    assert calls.count("match_block.py") == 1 and calls.count("finetune_knobs.py") == 1
    out = lib.root / "run"
    s = json.loads((out / "summary.json").read_text())
    assert s["model"] == "HD2_DistZeroAmpBassDI" and s["fine_tuned"] and s["renders"] == 171 and s["validate_exit"] == 0
    assert (out / "best.hlx").exists() and "index.html" in {p.name for p in out.iterdir()}
    assert "listen/match.wav" in (out / "index.html").read_text()
    calls.clear()
    assert SM.run_pipeline(a, sh=fake_shell(calls, lib), log=lambda m: None) == 0           # resume: only proc_check-free steps
    assert "match_block.py" not in calls and "finetune_knobs.py" not in calls and "rebuild_dry.py" not in calls
    calls.clear()
    assert SM.run_pipeline(args(lib, window="2:3"), sh=fake_shell(calls, lib), log=lambda m: None) == 0   # new window re-searches
    assert "match_block.py" in calls and "finetune_knobs.py" in calls and "rebuild_dry.py" not in calls
    calls.clear()
    assert SM.run_pipeline(args(lib, window="2:3", force=True), sh=fake_shell(calls, lib), log=lambda m: None) == 0
    assert {"transcribe_stems.py", "rebuild_dry.py", "match_block.py", "finetune_knobs.py"} <= set(calls)


def test_finetune_is_opt_in_and_locks_level_knobs(lib, patched):
    calls = []
    assert SM.run_pipeline(args(lib, finetune=False), sh=fake_shell(calls, lib), log=lambda m: None) == 0
    assert "finetune_knobs.py" not in calls
    s = json.loads((lib.root / "run" / "summary.json").read_text())
    assert not s["fine_tuned"] and s["dist_after"] == 0.3
    seen = []

    def sh(cmd, log_file=None, env=None):
        seen.append([str(c) for c in cmd])
        return fake_shell([], lib)(cmd, log_file, env)
    SM.run_pipeline(args(lib, out=str(lib.root / "run2")), sh=sh, log=lambda m: None)
    ft = next(c for c in seen if c[1].endswith("finetune_knobs.py"))
    assert "--lock-level-knobs" in ft
    plain = next(c for c in seen if c[1].endswith("match_block.py"))
    assert "--knob-tol" in plain and "--pack-lufs" in plain and "--category" in plain


def test_default_level_rule(lib, patched):
    import soundfile as sf2

    from tonematch.dry_rebuild import loud_level_db

    SM.run_pipeline(args(lib, guitar_id=None), sh=fake_shell([], lib), log=lambda m: None)
    x, _ = sf2.read(lib.root / "run" / "dry" / "rebuilt_dry_playlevel.wav")
    assert abs(loud_level_db(x) - SM.DEFAULT_LEVEL_DB) < 0.2           # no profile: -28.08
    SM.run_pipeline(args(lib, guitar_id=None, level_db=-20.0, out=str(lib.root / "r3")), sh=fake_shell([], lib), log=lambda m: None)
    x, _ = sf2.read(lib.root / "r3" / "dry" / "rebuilt_dry_playlevel.wav")
    assert abs(loud_level_db(x) + 20.0) < 0.2


def test_failure_stops_and_reports(lib, patched, capsys):
    assert SM.run_pipeline(args(lib), sh=fake_shell([], lib, fail="match_block.py"), log=lambda m: None) == 1
    assert "search failed" in capsys.readouterr().err
    assert not (lib.root / "run" / "summary.json").exists()


def test_dry_run_runs_nothing(lib, capsys):
    calls = []
    assert SM.run_pipeline(args(lib, dry_run=True), sh=fake_shell(calls, lib)) == 0
    out = capsys.readouterr().out
    assert calls == [] and "match_block.py" in out and "rebuild_dry.py" in out and not (lib.root / "run").exists()


def test_unknown_song_exit_2(lib, capsys):
    assert SM.run_pipeline(args(lib, song="no-such-song"), sh=fake_shell([], lib), log=lambda m: None) == 2


def test_load_stomp_needs_the_helper(lib, monkeypatch, tmp_path):
    monkeypatch.setenv("TONEMATCH_STOMPD", str(tmp_path / "missing"))
    msgs = []
    assert SM.load_stomp(tmp_path / "best.hlx", tmp_path, msgs.append) is False and "not present" in msgs[0]


def test_cli_result_manifest(lib, patched, monkeypatch, tmp_path):
    rj = tmp_path / "r.json"
    assert SM.main(["--song", "teen-spirit", "--window", "1:2", "--dry-run", "--result-json", str(rj)]) == 0
    m = json.loads(rj.read_text())
    assert m["tool"] == "song_match" and m["metrics"]["dry_run"] and m["status"] == "ok"


def test_finetune_job_resolution(lib):
    import finetune_knobs as FK

    a = SimpleNamespace(variant="-v", stems_dir=None, match_dir=None, dry_dir=None, out=None)
    j = FK.resolve_job("teen-spirit", a)
    assert j.stems_dir == lib.a and j.match_dir.name == "teen-spirit-dryrebuild-v" and j.out.name == "teen-spirit-finetune-v"
    a = SimpleNamespace(variant="", stems_dir="S", match_dir="M", dry_dir="D", out="O")
    j = FK.resolve_job("x", a)
    assert (j.stems_dir, j.match_dir, j.dry_dir, j.out) == (Path("S"), Path("M"), Path("D"), Path("O"))


def test_locked_view_hides_level_knobs():
    import finetune_knobs as FK

    class B:
        free = [("Drive", 0, 1, 0.5), ("Output", 0, 1, 0.5), ("Master", 0, 1, 0.5)]

        def with_values(self, v):
            return dict(v)
    lk = FK._Locked(B())
    assert [f[0] for f in lk.free] == ["Drive"] and lk.with_values({"Drive": 1, "Output": 2}) == {"Drive": 1}


def test_match_block_needs_stems_or_target_wav():
    import match_block as MB

    with pytest.raises(SystemExit):
        MB.parse(["--target-window", "1:2", "--di", "x.wav", "--out", "o"])
    assert MB.parse(["--target-stems", "s", "--target-window", "1:2", "--di", "x.wav", "--out", "o"]).target_stems == "s"
