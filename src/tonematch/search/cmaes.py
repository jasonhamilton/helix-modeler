"""Minimal CMA-ES (Hansen 2016 tutorial form), ask/tell, box-bounded on [0,1]^n.

Own implementation, numpy only: the `cma` package (BSD-3) is not installed and adds nothing the
search needs. Only ranks of the objective are used, which lets successive halving feed it mixed
fidelity scores (see loop.py). Bounds: the caller evaluates the clipped point and tells CMA the
unclipped sample.
"""
from __future__ import annotations

import numpy as np


class CMAES:
    def __init__(self, x0, sigma0: float = 0.25, popsize: int | None = None, seed: int = 0,
                 bounds: tuple[float, float] = (0.0, 1.0)):
        self.n = n = len(x0)
        self.rng = np.random.default_rng(seed)
        self.mean = np.asarray(x0, float).copy()
        self.sigma = float(sigma0)
        self.lo, self.hi = bounds
        self.lam = int(popsize or 4 + int(3 * np.log(n)))
        self.mu = self.lam // 2
        w = np.log(self.mu + 0.5) - np.log(np.arange(1, self.mu + 1))
        self.w = w / w.sum()
        self.mueff = 1.0 / np.sum(self.w ** 2)
        self.cc = (4 + self.mueff / n) / (n + 4 + 2 * self.mueff / n)
        self.cs = (self.mueff + 2) / (n + self.mueff + 5)
        self.c1 = 2 / ((n + 1.3) ** 2 + self.mueff)
        self.cmu = min(1 - self.c1, 2 * (self.mueff - 2 + 1 / self.mueff) / ((n + 2) ** 2 + self.mueff))
        self.damps = 1 + 2 * max(0, np.sqrt((self.mueff - 1) / (n + 1)) - 1) + self.cs
        self.chiN = np.sqrt(n) * (1 - 1 / (4 * n) + 1 / (21 * n * n))
        self.pc = np.zeros(n)
        self.ps = np.zeros(n)
        self.C = np.eye(n)
        self.B = np.eye(n)
        self.D = np.ones(n)
        self.gen = 0
        self._z: np.ndarray | None = None

    def ask(self) -> np.ndarray:
        """Return lam samples, shape (lam, n), unclipped. Clip with `clip` before evaluating."""
        z = self.rng.standard_normal((self.lam, self.n))
        self._z = z
        return self.mean + self.sigma * (z * self.D) @ self.B.T

    def clip(self, x):
        return np.clip(x, self.lo, self.hi)

    def tell(self, xs: np.ndarray, ranks_or_f) -> None:
        """`ranks_or_f`: lower is better. Only the order matters."""
        f = np.asarray(ranks_or_f, float)
        order = np.argsort(f, kind="stable")[: self.mu]
        n = self.n
        old = self.mean
        sel = np.asarray(xs)[order]
        self.mean = self.w @ sel
        y = (self.mean - old) / self.sigma
        invsqrtC = (self.B / self.D) @ self.B.T
        self.ps = (1 - self.cs) * self.ps + np.sqrt(self.cs * (2 - self.cs) * self.mueff) * (invsqrtC @ y)
        hsig = (np.linalg.norm(self.ps) / np.sqrt(1 - (1 - self.cs) ** (2 * (self.gen + 1))) / self.chiN
                < 1.4 + 2 / (n + 1))
        self.pc = (1 - self.cc) * self.pc + hsig * np.sqrt(self.cc * (2 - self.cc) * self.mueff) * y
        ys = (sel - old) / self.sigma
        self.C = ((1 - self.c1 - self.cmu) * self.C
                  + self.c1 * (np.outer(self.pc, self.pc) + (1 - hsig) * self.cc * (2 - self.cc) * self.C)
                  + self.cmu * (ys.T * self.w) @ ys)
        self.sigma *= np.exp((self.cs / self.damps) * (np.linalg.norm(self.ps) / self.chiN - 1))
        self.sigma = float(np.clip(self.sigma, 1e-4, 1.0))
        self.C = (self.C + self.C.T) / 2
        d2, self.B = np.linalg.eigh(self.C)
        self.D = np.sqrt(np.maximum(d2, 1e-20))
        self.gen += 1
