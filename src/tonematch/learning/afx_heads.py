"""Small trained heads on frozen AFx-Rep embeddings (predictor-pilot-v1, T189, drive only). Measurement only.

Two head types share one output interface so they can be scored and calibrated the same way:
  * `LinearHeads`: family posterior (default: summed from a flat model logistic regression; option: a separate
    family logistic regression, which tested worse), then a logistic regression inside each family (hierarchical
    model posterior), and a per-model ridge regression for Drive and Tone with a per-model residual sigma;
  * `MLPHeads`: one small MLP with a family head, a model head (softmax inside the family), and a Gaussian
    Drive/Tone head conditioned on a learned model embedding.

`predict(X)` takes (n, 512) or (n, crops, 512) embeddings; with crops it averages log-probabilities, means and
variances over the crops (multi-crop pooling). It returns a dict:
  fl (n, F) family log-probs (uncalibrated), lc (n, M) log p(model | its family), mu (n, M, 2) Drive/Tone mean for
  every model, sig (n, M, 2) raw sigma for every model (nan where a model has no such knob).

Calibration (`calibrate`) is fitted on a pool of out-of-fold predictions over many whole DI files, weighted so each
player counts equally and each file equally within its player (the T186 flaw was one calibration file).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

TOL = (0.15, 0.10)          # Drive, Tone tolerance (knob position 0..1)
Z68, Z95 = 1.0, 1.959964


def _log_softmax(a: np.ndarray, axis: int = -1) -> np.ndarray:
    m = a.max(axis=axis, keepdims=True)
    return a - m - np.log(np.exp(a - m).sum(axis=axis, keepdims=True))


def within_family_logsoftmax(ml: np.ndarray, mod_fam: np.ndarray, n_fam: int) -> np.ndarray:
    out = np.empty_like(ml)
    for f in range(n_fam):
        cols = mod_fam == f
        out[:, cols] = _log_softmax(ml[:, cols], 1)
    return out


def joint_logp(fl: np.ndarray, lc: np.ndarray, mod_fam: np.ndarray, T: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """log p(model) = log p(family) + log p(model | family), each tempered by T. Returns (joint, family)."""
    lf = _log_softmax(fl / T, 1)
    lcT = within_family_logsoftmax(lc / T, mod_fam, lf.shape[1])
    return lf[:, mod_fam] + lcT, lf


def _pool(outs: list[dict]) -> dict:
    if len(outs) == 1:
        return outs[0]
    return {"fl": np.mean([o["fl"] for o in outs], 0), "lc": np.mean([o["lc"] for o in outs], 0),
            "mu": np.mean([o["mu"] for o in outs], 0),
            "sig": np.sqrt(np.mean([o["sig"] ** 2 for o in outs], 0))}


def _flatten(X: np.ndarray, *ys):
    """(n, c, d) -> (n*c, d) with labels repeated; (n, d) unchanged."""
    if X.ndim == 2:
        return (X, *ys)
    c = X.shape[1]
    return (X.reshape(-1, X.shape[2]), *[np.repeat(y, c, axis=0) for y in ys])


class _Base:
    def predict(self, X: np.ndarray) -> dict:
        if X.ndim == 2:
            return self._predict(X)
        return _pool([self._predict(X[:, k]) for k in range(X.shape[1])])


@dataclass
class LinearHeads(_Base):
    mod_fam: np.ndarray
    C: float = 10.0
    alpha: float = 0.1
    fam_mode: str = "flat"      # flat: p(family) = sum of a flat model logreg; separate: a family logreg
    fam: object = None
    within: dict = field(default_factory=dict)
    ridge: dict = field(default_factory=dict)
    rsig: np.ndarray | None = None

    def fit(self, X, y_m, K):
        from sklearn.linear_model import LogisticRegression, Ridge
        X, y_m, K = _flatten(X, y_m, K)
        n_fam, M = int(self.mod_fam.max()) + 1, len(self.mod_fam)
        y_f = self.mod_fam[y_m]
        if self.fam_mode == "flat":
            self.fam = LogisticRegression(C=self.C, max_iter=3000).fit(X, y_m)
        else:
            self.fam = LogisticRegression(C=self.C, max_iter=3000).fit(X, y_f)
        self.n_fam, self.M = n_fam, M
        for f in range(n_fam):
            mem = np.where(self.mod_fam == f)[0]
            sel = np.isin(y_m, mem)
            if len(mem) > 1 and len(np.unique(y_m[sel])) > 1:
                self.within[f] = LogisticRegression(C=self.C, max_iter=3000).fit(X[sel], y_m[sel])
        self.rsig = np.full((M, 2), np.nan)
        for m in range(M):
            sel = y_m == m
            for k in range(2):
                s = sel & np.isfinite(K[:, k])
                if s.sum() < 5:
                    continue
                r = Ridge(alpha=self.alpha).fit(X[s], K[s, k])
                self.ridge[(m, k)] = r
                self.rsig[m, k] = max(float(np.std(K[s, k] - r.predict(X[s]))), 0.01)
        return self

    def _predict(self, X):
        n = len(X)
        fl = np.full((n, self.n_fam), -30.0)
        if self.fam_mode == "flat":
            P = np.zeros((n, self.M))
            P[:, self.fam.classes_] = self.fam.predict_proba(X)
            fl = np.log(np.clip(np.stack([P[:, self.mod_fam == f].sum(1) for f in range(self.n_fam)], 1),
                                1e-12, None))
        else:
            fl[:, self.fam.classes_] = np.log(np.clip(self.fam.predict_proba(X), 1e-12, None))
        lc = np.full((n, self.M), -30.0)
        for f in range(self.n_fam):
            mem = np.where(self.mod_fam == f)[0]
            if f in self.within:
                c = self.within[f]
                lc[:, c.classes_] = np.log(np.clip(c.predict_proba(X), 1e-12, None))
            else:
                lc[:, mem] = 0.0 if len(mem) == 1 else -math.log(len(mem))
        mu = np.full((n, self.M, 2), np.nan)
        for (m, k), r in self.ridge.items():
            mu[:, m, k] = np.clip(r.predict(X), 0, 1)
        sig = np.broadcast_to(self.rsig[None], mu.shape).copy()
        return {"fl": fl, "lc": lc, "mu": mu, "sig": sig}


@dataclass
class MLPHeads(_Base):
    mod_fam: np.ndarray
    hidden: int = 256
    epochs: int = 60
    lr: float = 1e-3
    wd: float = 1e-3
    dropout: float = 0.3
    noise: float = 0.01
    seed: int = 0
    device: str = "cpu"
    net: object = None
    has_knob: np.ndarray | None = None

    def _build(self, d_in, n_fam, M):
        import torch
        from torch import nn

        class Net(nn.Module):
            def __init__(s, h=self.hidden, p=self.dropout):
                super().__init__()
                s.trunk = nn.Sequential(nn.Linear(d_in, h), nn.GELU(), nn.Dropout(p), nn.Linear(h, h), nn.GELU(),
                                        nn.Dropout(p))
                s.fam = nn.Linear(h, n_fam)
                s.mod = nn.Linear(h, M)
                s.memb = nn.Embedding(M, 32)
                s.knob = nn.Sequential(nn.Linear(h + 32, 128), nn.GELU(), nn.Linear(128, 4))

            def forward(s, x):
                h = s.trunk(x)
                return h, s.fam(h), s.mod(h)

            def knobs(s, h, m):
                o = s.knob(torch.cat([h, s.memb(m)], 1))
                return torch.sigmoid(o[:, 0::2]), o[:, 1::2].clamp(-6, 2)
        return Net()

    def fit(self, X, y_m, K):
        import torch
        X, y_m, K = _flatten(X, y_m, K)
        torch.manual_seed(self.seed)
        rng = np.random.default_rng(self.seed)
        n_fam, M = int(self.mod_fam.max()) + 1, len(self.mod_fam)
        self.n_fam, self.M = n_fam, M
        self.has_knob = np.zeros((M, 2), bool)
        for m in range(M):
            for k in range(2):
                self.has_knob[m, k] = np.isfinite(K[y_m == m, k]).any()
        dev = self.device
        net = self._build(X.shape[1], n_fam, M).to(dev)
        mf = torch.tensor(self.mod_fam, device=dev)
        Xt = torch.tensor(X, dtype=torch.float32, device=dev)
        ym = torch.tensor(y_m, device=dev)
        Kt = torch.tensor(np.nan_to_num(K, nan=0.5), dtype=torch.float32, device=dev)
        Km = torch.tensor(np.isfinite(K), dtype=torch.float32, device=dev)
        opt = torch.optim.AdamW(net.parameters(), lr=self.lr, weight_decay=self.wd)
        B = 256
        steps = self.epochs * math.ceil(len(X) / B)
        sch = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=self.lr, total_steps=steps)
        net.train()
        for _ in range(self.epochs):
            perm = rng.permutation(len(X))
            for i in range(0, len(X), B):
                b = torch.tensor(perm[i:i + B], device=dev)
                x = Xt[b] + self.noise * torch.randn_like(Xt[b])
                h, fl, ml = net(x)
                lf = torch.log_softmax(fl, 1)
                lse = torch.stack([torch.logsumexp(ml[:, mf == f], 1) for f in range(n_fam)], 1)
                yb = ym[b]
                lj = lf[:, mf][torch.arange(len(b)), yb] + ml[torch.arange(len(b)), yb] - lse[:, mf][
                    torch.arange(len(b)), yb]
                mu, ls = net.knobs(h, yb)
                nll = (0.5 * ((Kt[b] - mu) / ls.exp()) ** 2 + ls) * Km[b]
                loss = -lf[torch.arange(len(b)), mf[yb]].mean() - lj.mean() + nll.sum() / Km[b].sum().clamp_min(1)
                opt.zero_grad()
                loss.backward()
                opt.step()
                sch.step()
        self.net = net.eval()
        return self

    def _predict(self, X):
        import torch
        dev = self.device
        with torch.no_grad():
            h, fl, ml = self.net(torch.tensor(X, dtype=torch.float32, device=dev))
            n, M = len(X), self.M
            hh = h.repeat_interleave(M, 0)
            mm = torch.arange(M, device=dev).repeat(n)
            mu, ls = self.net.knobs(hh, mm)
            mu = mu.reshape(n, M, 2).cpu().numpy().astype(np.float64)
            sig = ls.exp().reshape(n, M, 2).cpu().numpy().astype(np.float64)
        mu[:, ~self.has_knob] = np.nan
        sig[:, ~self.has_knob] = np.nan
        fl = _log_softmax(fl.cpu().numpy().astype(np.float64), 1)
        lc = within_family_logsoftmax(ml.cpu().numpy().astype(np.float64), self.mod_fam, self.n_fam)
        return {"fl": fl, "lc": lc, "mu": mu, "sig": sig}


# ------------------------------------------------------------------ calibration

def balance_weights(files: np.ndarray, players: np.ndarray) -> np.ndarray:
    """Each player weighs the same; inside a player each DI file weighs the same."""
    w = np.zeros(len(files))
    ups = np.unique(players)
    for p in ups:
        fp = np.unique(files[players == p])
        for f in fp:
            s = files == f
            w[s] = 1.0 / (len(ups) * len(fp) * s.sum())
    return w / w.sum()


def wquantile(x: np.ndarray, w: np.ndarray, q: float) -> float:
    o = np.argsort(x)
    c = np.cumsum(w[o]) / w.sum()
    return float(x[o][min(np.searchsorted(c, q), len(x) - 1)])


@dataclass
class Calibration:
    T: float = 1.0
    scale: tuple = (1.0, 1.0)                    # global sigma scale, Drive / Tone
    fam_scale: dict = field(default_factory=dict)  # (family, k) -> scale, only where enough files
    n_files: int = 0
    n_players: int = 0

    def scale_for(self, fam: np.ndarray, k: int, per_family: bool) -> np.ndarray:
        s = np.full(len(fam), self.scale[k])
        if per_family:
            for (f, kk), v in self.fam_scale.items():
                if kk == k:
                    s[fam == f] = v
        return s


def calibrate(out: dict, y_m: np.ndarray, K: np.ndarray, mod_fam: np.ndarray, files: np.ndarray,
              players: np.ndarray, min_files: int = 4, min_clips: int = 100) -> Calibration:
    """Temperature by weighted NLL; one sigma scale per knob so the weighted 68% interval covers 68% (true model
    given); per-family scales where the family has at least `min_files` files and `min_clips` clips in the pool."""
    w = balance_weights(files, players)
    best = (np.inf, 1.0)
    for T in np.exp(np.linspace(np.log(0.25), np.log(6.0), 60)):
        lj, _ = joint_logp(out["fl"], out["lc"], mod_fam, T)
        nll = -(w * lj[np.arange(len(y_m)), y_m]).sum()
        best = min(best, (nll, float(T)))
    cal = Calibration(T=best[1], n_files=len(np.unique(files)), n_players=len(np.unique(players)))
    idx = np.arange(len(y_m))
    sc = []
    fam = mod_fam[y_m]
    for k in range(2):
        mu, sg = out["mu"][idx, y_m, k], out["sig"][idx, y_m, k]
        ok = np.isfinite(K[:, k]) & np.isfinite(mu) & np.isfinite(sg)
        z = np.abs(K[ok, k] - mu[ok]) / sg[ok]
        sc.append(wquantile(z, w[ok], 0.6827) / Z68)
        for f in np.unique(fam[ok]):
            s = fam[ok] == f
            if len(np.unique(files[ok][s])) >= min_files and s.sum() >= min_clips:
                cal.fam_scale[(int(f), k)] = wquantile(z[s], w[ok][s], 0.6827) / Z68
    cal.scale = tuple(sc)
    return cal


# ------------------------------------------------------------------ scoring

def ece(conf: np.ndarray, hit: np.ndarray, bins: int = 15) -> float:
    e, edges = 0.0, np.linspace(0, 1, bins + 1)
    for a, b in zip(edges[:-1], edges[1:]):
        s = (conf > a) & (conf <= b)
        if s.any():
            e += s.mean() * abs(conf[s].mean() - hit[s].mean())
    return float(e)


def score(out: dict, y_m: np.ndarray, K: np.ndarray, mod_fam: np.ndarray, cal: Calibration | None) -> dict:
    n = len(y_m)
    T = cal.T if cal else 1.0
    lj, lf = joint_logp(out["fl"], out["lc"], mod_fam, T)
    lj1, _ = joint_logp(out["fl"], out["lc"], mod_fam, 1.0)
    rank = np.argsort(-lj, 1)
    top1 = rank[:, 0]
    y_f = mod_fam[y_m]
    r = {"n": int(n), "model_top1": float((top1 == y_m).mean()),
         "model_top5": float((rank[:, :5] == y_m[:, None]).any(1).mean()),
         "family_top1": float((lf.argmax(1) == y_f).mean()),
         "family_top3": float((np.argsort(-lf, 1)[:, :3] == y_f[:, None]).any(1).mean()),
         "ece_raw": ece(np.exp(lj1.max(1)), lj1.argmax(1) == y_m),
         "ece_cal": ece(np.exp(lj.max(1)), top1 == y_m), "temperature": T}
    idx = np.arange(n)
    for k, name in ((0, "drive"), (1, "tone")):
        tol = TOL[k]
        ok = np.isfinite(K[:, k])
        for who, mm in (("true", y_m), ("top1", top1)):
            mu, sg = out["mu"][idx, mm, k], out["sig"][idx, mm, k]
            err = np.abs(K[:, k] - mu)
            hit = np.where(np.isfinite(err), err <= tol, False)
            r[f"{name}_within_{who}"] = float(hit[ok].mean())
            if who == "true":
                r[f"{name}_mae_true"] = float(np.nanmean(err[ok]))
            v = ok & np.isfinite(err) & np.isfinite(sg)
            fam_pred = mod_fam[mm]
            for tag, s in (("raw", np.ones(n)),
                           ("global", cal.scale_for(fam_pred, k, False) if cal else None),
                           ("family", cal.scale_for(fam_pred, k, True) if cal else None)):
                if s is None:
                    continue
                ss = sg * s
                r[f"{name}_cover68_{tag}_{who}"] = float((err[v] <= Z68 * ss[v]).mean())
                r[f"{name}_cover95_{tag}_{who}"] = float((err[v] <= Z95 * ss[v]).mean())
                r[f"{name}_halfwidth68_{tag}_{who}"] = float(np.median(Z68 * ss[v]))
    return r
