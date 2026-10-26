"""afx_refine on a fake engine: no Native host."""
import numpy as np

from tonematch.search.afx_refine import refine_block


class FakeBlock:
    free = [("A", 0.0, 1.0, 0.5), ("B", -10.0, 10.0, 0.0)]

    def with_values(self, v):
        return dict(v)


class FakeEngine:
    def __init__(self):
        self.p = None

    def load(self, p):
        self.p = p

    def render(self, dry):
        return np.array([self.p["A"], self.p["B"]])


def _run(**kw):
    tgt = np.array([0.8, 4.0])
    return refine_block(FakeEngine(), FakeBlock(), None, tgt, {"A": 0.5, "B": 0.0},
                        embed=lambda y: y, dist=lambda a, b: float(np.sum(((a - b) / [1, 10]) ** 2)), log=lambda *_: None, **kw)


def test_improves_and_never_worse():
    r = _run(budget=120)
    assert r["dist_after"] < r["dist_before"] and r["renders"] <= 120


def test_budget_and_bounds():
    r = _run(budget=20)
    assert r["renders"] <= 20
    assert 0 <= r["final_values"]["A"] <= 1 and -10 <= r["final_values"]["B"] <= 10
    assert r["start_values"] == {"A": 0.5, "B": 0.0}


def test_at_optimum_stops_early():
    tgt = np.array([0.5, 0.0])
    r = refine_block(FakeEngine(), FakeBlock(), None, tgt, {"A": 0.5, "B": 0.0}, embed=lambda y: y,
                     dist=lambda a, b: float(np.sum((a - b) ** 2)), budget=400, log=lambda *_: None)
    assert r["dist_after"] <= r["dist_before"] and r["stopped"] == "patience" and r["renders"] < 400


def test_failed_render_is_a_bad_setting():
    class Flaky(FakeEngine):
        def render(self, dry):
            if self.p["A"] > 0.9:
                raise RuntimeError("silence")
            return super().render(dry)
    r = refine_block(Flaky(), FakeBlock(), None, np.array([1.0, 0.0]), {"A": 0.5, "B": 0.0}, embed=lambda y: y,
                     dist=lambda a, b: float(np.sum((a - b) ** 2)), budget=120, sigma0=0.5, log=lambda *_: None)
    assert r["final_values"]["A"] <= 0.9 and r["failed_renders"] > 0
