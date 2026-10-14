#!/usr/bin/env python3
"""SMOKE DEMO, not a benchmark: search Drive on the captured known.hlx chain (Native knob_01).

Target = Native render at Drive=0.75; start at the file's Drive; spectral stub loss; ~16 renders.
Writes no ledger row.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from engine_smoke import DI, PRESET, read_wav_f32

from tonematch.engines.native import NativeEngine
from tonematch.preset import read_hlx
from tonematch.search import Budget, Section, match, plan_budget
from tonematch.search.stubs import SpectralStubLoss

TRUE = 0.75


def main() -> int:
    dry = read_wav_f32(DI)
    pre = read_hlx(PRESET)
    t0 = time.perf_counter()
    with NativeEngine() as eng:
        truth = read_hlx(PRESET)
        truth.slots[0].params["Drive"] = TRUE
        eng.load(truth)
        target = eng.render(dry)
        b = Budget(max_renders=16, top_k_final=1)
        print(plan_budget(eng, b))
        out = match(Section("smoke", "known", target, dry), eng, SpectralStubLoss(), b,
                    candidates=[pre], free={tuple((x.category, x.model_id) for x in pre.slots): [(0, "Drive")]})
    r = out[0]
    print(f"start Drive={pre.slots[0].params.get('Drive')} found={r.values[(0, 'Drive')]:.3f} true={TRUE} "
          f"score={r.score:.4f} renders={r.renders} wall={time.perf_counter() - t0:.0f}s (SMOKE DEMO)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
