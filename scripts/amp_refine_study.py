"""Amp refine study (T120): the TRUE model is given; refinement methods compared at an equal render budget.

    python scripts/amp_refine_study.py synthetic-devfam i003,i019 out.json {base|bo|split} 40 150

  base   40 space-filling draws, then CMA-ES from the best draw (110 renders)
  bo     40 space-filling draws, then Gaussian-process expected improvement (110 renders)
  split  gain knobs first (CMA-ES), then tone-stack knobs, then all together
Scores with the level-guarded known-DI loss (admitted mrstft_v2); reports the guarded loss, the held-out loss on the
item's second passage and the parameter error. One Native host; devfam amp items only (dev set, not gate evidence).
The result rows in the ledger (`*_refine_study_*`) came from the first version of this script, run from a scratch path."""

import json
import math
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import match_song as MS
import soundfile as sf
import synthetic_v1 as S
from scipy.stats import norm
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel

from tonematch.catalog import load_catalog
from tonematch.engines import native_library as NL
from tonematch.engines.base import EngineError
from tonematch.metrics import LossKit
from tonematch.search.cmaes import CMAES
from tonematch.search.hybrid import latin_hypercube

setname = sys.argv[1]
ids = sys.argv[2].split(",")
out = Path(sys.argv[3])
method = sys.argv[4]
N0 = int(sys.argv[5])
TOTAL = int(sys.argv[6])
setdir = ROOT / "cache" / setname
man = json.loads((setdir / "manifest.json").read_text())
labels = {l["id"]: l for l in json.loads((setdir / "labels.json").read_text())}
cat = load_catalog()
blocks = {
    m: S.Block(m, "amp", S.free_params(m, NL.template_for(m), cat, "amp"), cat)
    for m in man["pool"]["amp"]
}
kit = LossKit()
W = {"mrstft_v2": 1.0}
guard = MS.LevelGuardKit(kit, known_di=True)
eng = S.make_engine()
TONE = re.compile(r"^(Bass|Mid|Treble|High|Low|Tone|Presence)$")
U_LO, U_HI = 0.05, 0.95


class Obj:
    def __init__(s, b, dry, tgt):
        s.b, s.dry, s.tgt, s.n, s.X, s.F = b, dry, tgt, 0, [], []

    def f(s, u):
        u = np.clip(u, 0, 1)
        v = s.b.space.from_unit(u)
        p = s.b.with_values({r[1]: float(x) for r, x in zip(s.b.space.refs, v)})
        s.n += 1
        try:
            eng.load(p)
            y = np.asarray(eng.render(s.dry), np.float32)
            val = float(guard.score(s.tgt, y, weights=W).total)
        except EngineError:
            val = 9.0
        if not math.isfinite(val):
            val = 9.0
        s.X.append(np.array(u, float))
        s.F.append(val)
        return val

    def best(s):
        j = int(np.argmin(s.F))
        return s.X[j], s.F[j]


def cma(o, u0, idx, budget, sigma, seed):
    n = len(idx)
    es = CMAES(u0[idx], sigma0=sigma, popsize=4 + int(3 * math.log(max(n, 2))), seed=seed)
    end = o.n + budget
    base = u0.copy()
    while o.n < end:
        xs = es.ask()
        fs = []
        for x in xs:
            u = base.copy()
            u[idx] = es.clip(x)
            fs.append(o.f(u))
        es.tell(xs, np.array(fs))
        b = o.best()[0]
        base = b.copy()  # follow the global best for the other coords


def bo(o, budget, rng, d):
    end = o.n + budget
    while o.n < end:
        X = np.array(o.X)
        y = np.log(np.array(o.F) + 0.02)
        k = ConstantKernel(1.0, (1e-2, 1e2)) * Matern(
            length_scale=np.full(d, 0.4), length_scale_bounds=(0.05, 5), nu=2.5
        ) + WhiteKernel(1e-3, (1e-6, 1e-1))
        gp = GaussianProcessRegressor(
            k, normalize_y=True, n_restarts_optimizer=0, random_state=0
        ).fit(X, y)
        top = X[np.argsort(y)[:5]]
        cand = np.vstack(
            [U_LO + (U_HI - U_LO) * rng.random((2500, d))]
            + [np.clip(t + 0.08 * rng.standard_normal((300, d)), U_LO, U_HI) for t in top]
        )
        mu, sd = gp.predict(cand, return_std=True)
        sd = np.maximum(sd, 1e-9)
        fb = y.min()
        z = (fb - mu) / sd
        ei = (fb - mu) * norm.cdf(z) + sd * norm.pdf(z)
        o.f(cand[int(np.argmax(ei))])


res = []
for i in ids:
    it = labels[i]
    b = blocks[it["model"]]
    dry = sf.read(str(ROOT / it["di"]["A"]["file"]), dtype="float32")[0][:480000]
    dryC = sf.read(str(ROOT / it["di"]["C"]["file"]), dtype="float32")[0][:480000]
    true_p = b.with_values(it["values"])
    eng.load(true_p)
    tgt = np.asarray(eng.render(dry), np.float32)
    eng.load(true_p)
    tgtC = np.asarray(eng.render(dryC), np.float32)
    sp = b.space
    d = len(sp.refs)
    names = [r[1] for r in sp.refs]
    rng = np.random.default_rng([3, int(i[1:])])
    o = Obj(b, dry, tgt)
    tone = [j for j, n in enumerate(names) if TONE.match(n)]
    gain = [j for j in range(d) if j not in tone]
    if method == "split":
        mid = np.full(d, 0.5)
        u = latin_hypercube(N0, len(gain), rng)
        u = U_LO + (U_HI - U_LO) * u
        for row in u:
            x = mid.copy()
            x[gain] = row
            o.f(x)
        rest = TOTAL - o.n
        ng = int(rest * 0.35)
        nt = int(rest * 0.35)
        cma(o, o.best()[0].copy(), gain, ng, 0.2, 1)
        cma(o, o.best()[0].copy(), tone, nt, 0.25, 2)
        cma(o, o.best()[0].copy(), list(range(d)), TOTAL - o.n, 0.1, 3)
    else:
        u = U_LO + (U_HI - U_LO) * latin_hypercube(N0, d, rng)
        for row in u:
            o.f(row)
        if method == "bo":
            bo(o, TOTAL - o.n, rng, d)
        else:
            cma(o, o.best()[0].copy(), list(range(d)), TOTAL - o.n, 0.3, 1)
    ub, fb = o.best()
    v = sp.from_unit(ub)
    vals = {r[1]: float(x) for r, x in zip(sp.refs, v)}
    eng.load(b.with_values(vals))
    yc = np.asarray(eng.render(dryC), np.float32)
    held = float(kit.score(tgtC, yc, weights=W).total)
    tn = b.norm(it["values"])
    pn = b.norm(vals)
    mae = float(np.mean([abs(tn[n] - pn[n]) for n in tn]))
    row = {
        "id": i,
        "model": it["model"],
        "method": method,
        "fit": round(fb, 4),
        "held": round(held, 4),
        "mae": round(mae, 3),
        "spent": o.n,
    }
    print(json.dumps(row), flush=True)
    res.append(row)
eng.close()
out.write_text(json.dumps(res))
