"""Offline tests for scripts/song_meta_probe.py. No network."""
import importlib.util
from pathlib import Path

_p = Path(__file__).resolve().parents[1] / "scripts" / "song_meta_probe.py"
_s = importlib.util.spec_from_file_location("song_meta_probe", _p)
probe = importlib.util.module_from_spec(_s)
_s.loader.exec_module(probe)


def test_build_url_escapes_quotes():
    url = probe.build_url('A "B"', "x")
    assert "fmt=json" in url and "%5C%22B%5C%22" in url


def test_parse_picks_best_score_and_sorts_tags():
    payload = {"recordings": [
        {"id": "a", "score": 60, "tags": [{"name": "pop", "count": 9}]},
        {"id": "b", "score": 100, "first-release-date": "1991-09-10",
         "tags": [{"name": "rock", "count": 1}, {"name": "metal", "count": 5}]}]}
    out = probe.parse_recordings(payload)
    assert out["mbid"] == "b" and out["first_release_year"] == 1991
    assert out["tags"] == ["metal", "rock"]


def test_parse_empty():
    assert probe.parse_recordings({"recordings": []}) == {"found": False}


def test_dry_run_default_sends_nothing(monkeypatch, capsys):
    monkeypatch.setattr(probe, "fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert probe.main([]) == 0
    assert "dry-run" in capsys.readouterr().out


def test_live_requires_opt_in(capsys):
    assert probe.main(["--live"]) == 2


def test_year_is_the_oldest_release_among_strong_matches():
    """Re-releases must not move the year: the original release dates the tone (human 2026-10-17)."""
    payload = {"recordings": [
        {"id": "remaster", "score": 100, "first-release-date": "2005-03-01"},
        {"id": "original", "score": 98, "first-release-date": "1979-11-30"},
        {"id": "live", "score": 95, "releases": [{"date": "1988"}, {"date": "1995-01-01"}]},
        {"id": "weak-cover", "score": 40, "first-release-date": "1970"},
    ]}
    r = probe.parse_recordings(payload)
    assert r["first_release_year"] == 1979 and r["year_from_mbid"] == "original"
    assert r["mbid"] == "remaster" and r["matches_considered"] == 3
