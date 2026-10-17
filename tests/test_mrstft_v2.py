"""`mrstft_v2` (T096): V1 reproduces `mrstft`, DEFAULT is the frozen tuned config, level is not scored.

`mrstft_v2`, like `mrstft`, needs an aligned DI and is not content-invariant by design
(contracts/loss-kit.md, Invariances), so there is no note-content test here.
"""
from __future__ import annotations

import numpy as np
import pytest

from tonematch.metrics import mrstft
from tonematch.metrics import mrstft_v2 as MV2

SR = 48_000


def _guitarish(seed: int, seconds: float = 2.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(SR * seconds)) / SR
    x = np.zeros_like(t)
    for k, f in enumerate((110.0, 220.0, 330.0, 440.0, 660.0)):
        x += np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28)) / (k + 1)
    env = np.exp(-3.0 * (t % 0.5))
    return (0.2 * x * env + 1e-3 * rng.standard_normal(t.size)).astype(np.float32)


def _drive(x: np.ndarray, g: float) -> np.ndarray:
    return np.tanh(g * x) / np.tanh(g)


def test_v1_reproduces_mrstft():
    a, b = _guitarish(1), _drive(_guitarish(1), 4.0)
    assert MV2.term(a, b, config=MV2.V1) == pytest.approx(mrstft.term(a, b), rel=1e-6, abs=1e-9)


def test_default_is_frozen_tuned_config():
    assert MV2.DEFAULT != MV2.V1
    assert MV2.DEFAULT == MV2.FROZEN[MV2.VERSION]
    assert MV2.DEFAULT.as_dict() == MV2.FROZEN[MV2.VERSION].as_dict()


@pytest.mark.parametrize("cfg", [MV2.V1, MV2.DEFAULT])
def test_zero_on_identical_and_level_free(cfg):
    a = _guitarish(2)
    b = _drive(a, 3.0)
    assert MV2.term(a, a, config=cfg) == pytest.approx(0.0, abs=1e-9)
    d = MV2.term(a, b, config=cfg)
    assert d > 0
    # the stem's level is arbitrary: a pure gain on either side must not change the distance much
    assert MV2.term(a, 0.25 * b, config=cfg) == pytest.approx(d, rel=1e-3)
    assert MV2.term(4.0 * a, b, config=cfg) == pytest.approx(d, rel=1e-3)


def test_default_orders_drive_amount():
    a = _guitarish(3)
    target = _drive(a, 3.0)
    near, far = _drive(a, 3.6), _drive(a, 8.0)
    assert MV2.term(target, target) < MV2.term(target, near) < MV2.term(target, far)
