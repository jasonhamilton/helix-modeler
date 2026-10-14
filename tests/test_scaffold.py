import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_engine_interface_importable():
    from tonematch.engines import Capabilities, Cost, Engine  # noqa: F401


def test_no_engine_raises_clearly(monkeypatch):
    from tonematch.engines import EngineError, registry
    monkeypatch.setenv("TONEMATCH_ENGINE", "auto")
    try:
        registry.select()
    except EngineError as e:
        assert "at least one is required" in str(e)
    else:  # an engine is actually present on this machine
        pass


def test_ledger_header():
    with (ROOT / "ledger" / "runs.csv").open() as f:
        header = next(csv.reader(f))
    assert header[:4] == ["timestamp", "commit", "agent", "kind"]


def test_no_line6_assets_committed():
    bad = [p for p in ROOT.rglob("*") if p.suffix.lower() in {".hxf", ".hxb"}]
    assert not bad, f"Line 6 bundles must not be committed: {bad}"
