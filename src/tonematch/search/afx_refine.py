"""Local knob fine-tuning of ONE fixed model against the AFx-Rep distance (T209).

Starts at the given knob values (e.g. T207's coarse-grid point), searches a small box around them with CMA-ES
(`search.cmaes`, unit coordinates of each knob's range), and stops early when generations stop improving or the
render budget is spent. The start point is always scored first and is never lost: the result is never worse
than the start. Pure logic: `engine`, `embed` and `dist` are injected, so tests need no Native host.
"""
from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np

from .cmaes import CMAES


def refine_block(engine, block, dry, target_feat, start: dict[str, float], *, embed: Callable, dist: Callable,
                 budget: int = 96, sigma0: float = 0.12, popsize: int | None = None, patience: int = 3,
                 min_gain: float = 5e-4, seed: int = 0, wait: Callable[[], object] | None = None,
                 log: Callable = print) -> dict:
    """Minimise dist(target_feat, embed(render)) over the block's free knobs. Returns start/final values,
    distances, renders and the per-generation trace. `wait()` is called before each generation (load guard)."""
    names = [n for n, *_ in block.free]
    lo = np.array([a for _, a, _, _ in block.free], float)
    hi = np.array([b for _, _, b, _ in block.free], float)
    span = np.where(hi > lo, hi - lo, 1.0)
    t0, renders, bad = time.time(), 0, []

    def to_vals(u):
        return {n: round(float(lo[i] + float(np.clip(u[i], 0, 1)) * span[i]), 6) for i, n in enumerate(names)}

    def score(u):
        nonlocal renders
        renders += 1
        try:
            engine.load(block.with_values(to_vals(u)))
            y = engine.render(dry)
        except Exception as e:  # noqa: BLE001 - a setting the plugin renders as silence is a bad setting, not a crash
            bad.append(f"{type(e).__name__}: {e}")
            return 1e9
        d = float(dist(target_feat, embed(y)))
        return d if np.isfinite(d) else 1e9

    sv = {n: float(start.get(n, dflt)) for n, _, _, dflt in block.free}
    u0 = np.clip(np.array([(sv[n] - lo[i]) / span[i] for i, n in enumerate(names)]), 0, 1)
    d0 = score(u0)
    best_u, best_d = u0.copy(), d0
    es = CMAES(u0, sigma0=sigma0, popsize=popsize, seed=seed)
    stale, trace = 0, [{"gen": 0, "best": d0, "renders": renders}]
    while renders + es.lam <= budget and stale < patience:
        if wait:
            wait()
        xs = es.ask()
        f = np.array([score(es.clip(x)) for x in xs])
        es.tell(xs, f)
        i = int(np.argmin(f))
        if f[i] < best_d - min_gain:
            stale = 0
        else:
            stale += 1
        if f[i] < best_d:
            best_u, best_d = es.clip(xs[i]).copy(), float(f[i])
        trace.append({"gen": es.gen, "best": best_d, "renders": renders, "sigma": es.sigma})
        log(f"  gen {es.gen}: best {best_d:.4f} ({renders} renders)")
    final = to_vals(best_u)
    moved = {n: abs(final[n] - sv[n]) / span[i] for i, n in enumerate(names)}
    return {"start_values": {n: round(sv[n], 6) for n in names}, "final_values": final, "dist_before": d0,
            "dist_after": best_d, "renders": renders, "wall_s": time.time() - t0, "trace": trace,
            "moved_frac": moved, "failed_renders": len(bad), "stopped": "patience" if stale >= patience else "budget"}
