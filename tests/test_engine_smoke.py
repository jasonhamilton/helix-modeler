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
