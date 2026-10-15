"""Native-gated: renders are history-independent under the host's state reset (T043/T044).

Runs only where Helix Native, a plugin host and a captured CC template are present (skips
elsewhere, e.g. CI); set TONEMATCH_SKIP_NATIVE_TESTS=1 to skip it on a busy machine. One host,
~25 renders, about 20 s. Synthetic excitation only.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from tonematch.engines import native as nat

N_TRIALS = 20


def _template():
    try:
        variants = nat.ChainTemplate.load_variants()
    except (OSError, ValueError, KeyError):
        return None
    tpls = [t for ts in variants.values() for t in ts if t.map and t.baked_hash]
    return max(tpls, key=lambda t: len(t.map)) if tpls else None


@pytest.fixture
def native_engine(monkeypatch):
    if os.environ.get("TONEMATCH_SKIP_NATIVE_TESTS"):
        pytest.skip("TONEMATCH_SKIP_NATIVE_TESTS set")
    if not nat.NativeEngine.is_available():
        pytest.skip("Helix Native or a plugin host is not installed")
    tpl = _template()  # read from the real cache before the autouse fixtures of other files apply
    if tpl is None:
        pytest.skip("no captured Native CC template in cache/native/templates")
    monkeypatch.setenv("TONEMATCH_NATIVE_HOST_RESET", "state")
    e = nat.NativeEngine(preroll=144000, autocapture=False)
    e._set_state(tpl.state)
    e.current_chain, e.current_template = tpl.chain, tpl
    yield e, tpl
    e.close()


def test_render_is_identical_after_random_cc_trials(native_engine):
    e, tpl = native_engine
    refs = [(int(k.split(":")[0]), k.split(":", 1)[1]) for k in tpl.map]
    lo_hi = {r: tpl.ranges.get(f"{r[0]}:{r[1]}", [0.0, 1.0]) for r in refs}
    base = {r: lo + 0.5 * (hi - lo) for r, (lo, hi) in lo_hi.items()}
    x = nat.test_signal(1.0)
    e.set_params(base)
    first = e.render(x)
    assert e.last_reset_via == "state"
    rng = np.random.default_rng(2026)
    for _ in range(N_TRIALS):
        e.set_params({r: lo + float(u) * (hi - lo) for (r, (lo, hi)), u in
                      zip(lo_hi.items(), rng.uniform(0, 1, len(refs)))})
        try:
            e.render(x)
        except nat.EngineError as err:  # a random setting may be silent (ChVol 0)
            assert "silence" in str(err)
    e.set_params(base)
    again = e.render(x)
    assert nat.null_db(first, again) <= -100.0
