import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_help_exits_zero_without_touching_ledger():
    ledger = ROOT / "ledger" / "runs.csv"
    before = ledger.read_bytes()
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "engine_smoke.py"), "--help"],
                       check=False, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert "--no-ledger" in r.stdout and "--engine" in r.stdout
    assert ledger.read_bytes() == before


def _smoke():
    sys.path.insert(0, str(ROOT / "scripts"))
    import engine_smoke

    return engine_smoke


def test_real_di_is_the_default_and_synthetic_the_fallback(tmp_path, monkeypatch):
    es = _smoke()
    di, kind = es.pick_di("auto")
    assert (di, kind) == (es.DI_REAL, "real") and es.DATASETS[kind] == "smoke-real-di"
    x = es.read_wav_f32(di)
    assert len(x) == 2 * es.SR and 0.01 < float(abs(x).max()) < 1.0
    assert es.pick_di("synthetic") == (es.DI, "synthetic") and es.DATASETS["synthetic"] == "smoke"
    monkeypatch.setattr(es, "DI_REAL", tmp_path / "missing.wav")
    assert es.pick_di("auto") == (es.DI, "synthetic")


def test_real_di_has_attribution():
    prov = (ROOT / "fixtures" / "smoke" / "PROVENANCE.md").read_text()
    assert "di_2s_real.wav" in prov and "CC BY 4.0" in prov and "zenodo.org/records/14963133" in prov
