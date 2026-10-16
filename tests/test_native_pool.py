"""NativeRenderPool API for search (render_generation / map_generation / ensure_templates).
Fake engines only: no Native, no pedalboard."""
from __future__ import annotations

import threading

import numpy as np
import pytest

from tonematch.engines import native as nat
from tonematch.engines.base import EngineError


class FakeEngine:
    def __init__(self, k: int):
        self.k, self.loads, self.params = k, 0, {}
        self.templates, self._variants = {}, {}
        self.current_chain = self.current_template = None
        self.recycles = 0
        self.host = type("H", (), {"mem_mb": 1500.0 + k, "canary_respawns": 0})()
        self.threads: set[int] = set()

    def load(self, preset):
        self.loads += 1
        self.params = {}

    def set_params(self, ps):
        self.params.update(ps)

    def render(self, dry):
        self.threads.add(threading.get_ident())
        g = self.params.get((0, "Drive"), 0.0)
        if g < 0:
            raise EngineError("Native returned silence for a non-silent input")
        return (np.asarray(dry) * (1.0 + g)).astype(np.float32)

    def capture_missing(self, presets, **kw):
        t = nat.ChainTemplate(("x",), b"S", name="new")
        self._variants.setdefault(t.chain, []).append(t)
        self.templates[t.chain] = t
        return {}


def _pool(n=3):
    pool = nat.NativeRenderPool.__new__(nat.NativeRenderPool)
    pool.engines = [FakeEngine(k) for k in range(n)]
    pool.last_errors, pool.last_item_s = {}, []
    return pool


DRY = np.ones(16, np.float32)


def test_render_generation_keeps_order_and_uses_every_host():
    pool = _pool(3)
    sets = [{(0, "Drive"): i / 10} for i in range(7)]
    out = pool.render_generation(object(), sets, DRY)
    assert [float(y[0]) for y in out] == pytest.approx([1 + i / 10 for i in range(7)])
    assert all(e.loads > 0 for e in pool.engines) and len(pool.last_item_s) == 7


def test_map_generation_applies_fn_in_workers_and_returns_only_its_results():
    pool = _pool(2)
    sets = [{(0, "Drive"): i / 10} for i in range(4)]
    out = pool.map_generation(object(), sets, DRY, lambda y: float(y.sum()))
    assert out == pytest.approx([16 * (1 + i / 10) for i in range(4)])


def test_on_error_none_keeps_going_and_records_the_error():
    pool = _pool(2)
    sets = [{(0, "Drive"): 0.1}, {(0, "Drive"): -1.0}, {(0, "Drive"): 0.2}]
    out = pool.render_generation(object(), sets, DRY, on_error="none")
    assert out[1] is None and out[0] is not None and out[2] is not None
    assert "silence" in str(pool.last_errors[1])
    with pytest.raises(EngineError):
        pool.render_generation(object(), sets, DRY)


def test_stats_and_memory_sum_over_hosts():
    pool = _pool(3)
    st = pool.stats()
    assert st["hosts"] == 3 and st["mem_mb"] == 1500 * 3 + 3 and st["recycles"] == 0


def test_ensure_templates_shares_new_templates_with_every_host():
    pool = _pool(3)
    assert pool.ensure_templates([object()]) == {}
    assert all(("x",) in e.templates and e._variants[("x",)][0].name == "new" for e in pool.engines)


def test_pool_size_env_and_ram_cap(monkeypatch):
    made = []
    monkeypatch.setattr(nat, "NativeEngine", lambda **kw: made.append(kw) or FakeEngine(len(made)))
    monkeypatch.setattr(nat, "max_hosts_by_memory", lambda *a, **k: 3)
    monkeypatch.setenv("TONEMATCH_NATIVE_POOL", "8")
    pool = nat.NativeRenderPool()
    assert pool.workers == 3 and all(kw["autocapture"] is False for kw in made)
