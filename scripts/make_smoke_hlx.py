#!/usr/bin/env python3
"""Build fixtures/smoke/known.hlx from the HX Stomp golden: one amp, one cab, one bypassed delay.

The output embeds Line 6 model data taken from the user's own export, so it is gitignored and
stays local (see .gitignore). Usage: python scripts/make_smoke_hlx.py [--golden F] [--out F]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tonematch import preset as P


def build(golden: Path) -> P.Preset:
    pre = P.read_hlx(golden)
    amp = next(s for s in pre.slots if s.category == "amp")
    cab = next(s for s in pre.slots if s.category == "cab" and s.key == amp.extra.get("@cab"))
    delay = next(s for s in pre.slots if s.category == "delay")
    amp.position, delay.position, delay.enabled = 1, 2, False
    pre.slots = [amp, cab, delay]
    pre.name = "Smoke Known"
    for sn in pre.snapshots:
        sn.enabled = {amp.key: True, delay.key: False}
        sn.params = {k: v for k, v in sn.params.items() if k == delay.key}
    return pre


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default=str(ROOT / "fixtures" / "golden" / "HXStomp.hlx"))
    ap.add_argument("--out", default=str(ROOT / "fixtures" / "smoke" / "known.hlx"))
    a = ap.parse_args(argv)
    if not Path(a.golden).is_file():
        print(f"make_smoke_hlx: {a.golden} missing (T010 golden export)", file=sys.stderr)
        return 2
    print(P.write_hlx(build(Path(a.golden)), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
