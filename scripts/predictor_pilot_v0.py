#!/usr/bin/env python3
"""predictor-pilot-v0 (T186): drive-only blind predictor on cached renders. Measurement only.

  features   log-mel of every indexed render -> cache/models/predictor-pilot-v0/feats.npy (fp16, deleted after)
  probe      2-minute speed probe: feature rate and training samples/s, projects total time
  train      --arm all|xplayer   trains, calibrates on the cal split, evaluates on the test split
             all: every train-split render; xplayer: P2 train renders only, tested on P1 test files

Writes cache/models/predictor-pilot-v0/{index.json, <arm>/model.pt, <arm>/result.json}.
CPU threads <= 2, nice 10 is the caller's job (run with `nice -n 10`).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("OMP_NUM_THREADS", "2")

import torch

from tonematch.gpu import detect_backend
from tonematch.learning import drive_pilot as dp

OUT = dp.ROOT / "cache/models/predictor-pilot-v0"
FR = 501                      # frames for 10 s at hop 960, center=True
CROP = 300                    # 6 s training crop
torch.set_num_threads(2)


def device():
    b = detect_backend()
    if b == "mps":
        return torch.device("mps")
    if b in ("cuda", "rocm"):
        return torch.device("cuda")
    return torch.device("cpu")


def cmd_features(limit: int | None = None, ram: bool = False) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    recs = dp.build_index()
    if limit:
        recs = recs[:: max(1, len(recs) // limit)][:limit]
    dev = device()
    fe = dp.mel_frontend(dev)
    n = len(recs)
    path = OUT / ("feats.npy" if not limit else "feats_probe.npy")
    mm = np.zeros((n, dp.N_MELS, FR), np.float16) if ram else \
        np.lib.format.open_memmap(path, mode="w+", dtype=np.float16, shape=(n, dp.N_MELS, FR))
    nfr, rms = np.zeros(n, np.int32), np.zeros(n, np.float32)
    t0, B = time.time(), 32
    for i in range(0, n, B):
        chunk = recs[i:i + B]
        auds = []
        for j, r in enumerate(chunk):
            a = np.load(r.path).astype(np.float32).reshape(-1)
            nfr[i + j] = min(FR, a.size // dp.HOP + 1)
            rms[i + j] = dp.audio_rms_db(a)
            auds.append(np.pad(a[:10 * dp.SR], (0, max(0, 10 * dp.SR - a.size))))
        x = torch.from_numpy(np.stack(auds)).to(dev)
        # level: the frontend RMS-normalises and the net subtracts each crop's mean log-energy, so level is free
        with torch.no_grad():
            m = fe(x)
        mm[i:i + len(chunk)] = m.cpu().numpy().astype(np.float16)
        if i % (B * 50) == 0:
            print(f"{i}/{n} {time.time() - t0:.0f}s", flush=True)
    if not ram:
        mm.flush()
    dt = time.time() - t0
    idx = {"records": dp.to_jsonable(recs), "nframes": nfr.tolist(), "rms_db": rms.tolist(),
           "feature_s": dt, "frontend": {"n_fft": dp.N_FFT, "hop": dp.HOP, "n_mels": dp.N_MELS,
                                         "fmin": dp.FMIN, "fmax": dp.FMAX, "norm": "per-clip RMS"}}
    (OUT / ("index.json" if not limit else "index_probe.json")).write_text(json.dumps(idx))
    print(f"features: {n} clips in {dt:.1f}s -> {'RAM' if ram else path}")
    return {"n": n, "s": dt, "X": mm if ram else None}


class Data:
    def __init__(self, probe=False, X=None):
        idx = json.loads((OUT / ("index_probe.json" if probe else "index.json")).read_text())
        self.recs = idx["records"]
        self.nfr = np.array(idx["nframes"])
        self.rms = np.array(idx["rms_db"])
        self.X = X if X is not None else np.load(OUT / ("feats_probe.npy" if probe else "feats.npy"), mmap_mode="r")
        self.fams, self.models, self.m2f = dp.families()
        self.fi = {f: i for i, f in enumerate(self.fams)}
        self.mi = {m: i for i, m in enumerate(self.models)}
        self.mod_fam = torch.tensor([self.fi[self.m2f[m]] for m in self.models])
        self.y_m = np.array([self.mi[r["model"]] for r in self.recs])
        self.y_f = np.array([self.fi[r["family"]] for r in self.recs])
        self.y_k = np.array([[r["drive"], r["tone"]] for r in self.recs], np.float32)
        self.has_knob = np.ones((len(self.models), 2), bool)
        for j, r in enumerate(self.recs):
            if math.isnan(r["tone"]):
                self.has_knob[self.y_m[j], 1] = False
        self.ok = self.rms > -90.0                      # drop digital-silence renders

    def sel(self, split, player=None):
        s = np.array([r["split"] == split and (player is None or r["player"] == player) for r in self.recs])
        return np.where(s & self.ok)[0]


def batches(D, ids, dev, Xd, bs, crop, rng, train=True):
    ids = rng.permutation(ids) if train else ids
    for i in range(0, len(ids), bs):
        b = ids[i:i + bs]
        if train:
            st = np.array([rng.integers(0, max(1, D.nfr[j] - crop)) for j in b])
        else:
            st = np.maximum(0, (D.nfr[b] - crop) // 2)
        x = torch.stack([Xd[j, :, s:s + crop] for j, s in zip(b, st)]).float()
        if train:                                         # pickup-like tilt +-3 dB over the mel axis
            tilt = torch.from_numpy(rng.uniform(-0.69, 0.69, len(b)).astype(np.float32)).to(x.device)
            ramp = torch.linspace(-1, 1, x.shape[1], device=x.device)
            x = x + tilt[:, None, None] * ramp[None, :, None]
        yield b, x


def losses(net, D, x, b, dev, mod_fam):
    z, fl, ml = net(x)
    lp, _ = dp.joint_logp(fl, ml, mod_fam, len(D.fams))
    ym = torch.from_numpy(D.y_m[b]).to(dev)
    l_cls = -lp.gather(1, ym[:, None]).mean()
    mu, ls = net.knobs(z, ym)
    yk = torch.from_numpy(D.y_k[b]).to(dev)
    msk = ~torch.isnan(yk)
    yk0 = torch.nan_to_num(yk, 0.0)
    nll = 0.5 * ((yk0 - mu) / ls.exp()).pow(2) + ls
    l_k = (nll * msk).sum() / msk.sum().clamp_min(1)
    return l_cls, l_k


def train(D, tr_ids, epochs, dev, seed=0, max_s=None, log=print):
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    net = dp.make_model(len(D.fams), len(D.models)).to(dev)
    nparam = sum(p.numel() for p in net.parameters())
    Xd = torch.from_numpy(np.ascontiguousarray(D.X[np.sort(tr_ids)])).to(dev)
    remap = {j: k for k, j in enumerate(np.sort(tr_ids))}

    class View:
        def __getitem__(self, a):
            return Xd[remap[a[0]]][a[1:]] if isinstance(a, tuple) else Xd[remap[a]]
    V = View()
    mod_fam = D.mod_fam.to(dev)
    bs = 64
    steps = epochs * math.ceil(len(tr_ids) / bs)
    opt = torch.optim.AdamW(net.parameters(), lr=2e-3, weight_decay=1e-2)
    sch = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=2e-3, total_steps=steps, pct_start=0.1)
    t0, seen, step = time.time(), 0, 0
    for ep in range(epochs):
        net.train()
        tot = [0.0, 0.0, 0]
        for b, x in batches(D, tr_ids, dev, V, bs, CROP, rng):
            l_cls, l_k = losses(net, D, x, b, dev, mod_fam)
            loss = l_cls + l_k
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            sch.step()
            step += 1
            seen += len(b)
            tot[0] += float(l_cls) * len(b); tot[1] += float(l_k) * len(b); tot[2] += len(b)
            if max_s and time.time() - t0 > max_s:
                return net, {"samples_per_s": seen / (time.time() - t0), "params": nparam, "steps": step}
        log(f"epoch {ep + 1}/{epochs} cls {tot[0] / tot[2]:.3f} knob {tot[1] / tot[2]:.3f} "
            f"{time.time() - t0:.0f}s {seen / (time.time() - t0):.0f} samples/s")
    del Xd
    return net, {"samples_per_s": seen / (time.time() - t0), "params": nparam, "steps": step,
                 "train_s": time.time() - t0}


@torch.no_grad()
def infer(net, D, ids, dev, crop):
    """Per clip: joint log p(model) (logits, T=1 applied later), family logits, knob mu/logsigma for every model."""
    net.eval()
    mod_fam = D.mod_fam.to(dev)
    out = {"fl": [], "ml": [], "mu": [], "ls": []}
    order = np.argsort(D.nfr[ids], kind="stable")
    ids_s = ids[order]
    allm = torch.arange(len(D.models), device=dev)
    for i in range(0, len(ids_s), 64):
        b = ids_s[i:i + 64]
        L = int(D.nfr[b].min()) if crop is None else crop
        st = np.maximum(0, (D.nfr[b] - L) // 2)
        x = torch.stack([torch.from_numpy(np.asarray(D.X[j, :, s:s + L], np.float32)) for j, s in zip(b, st)]).to(dev)
        z, fl, ml = net(x)
        B = len(b)
        zz = z.repeat_interleave(len(D.models), 0)
        mm = allm.repeat(B)
        mu, ls = net.knobs(zz, mm)
        out["fl"].append(fl.cpu()); out["ml"].append(ml.cpu())
        out["mu"].append(mu.view(B, -1, 2).cpu()); out["ls"].append(ls.view(B, -1, 2).cpu())
    inv = np.argsort(order)
    return {k: torch.cat(v)[inv] for k, v in out.items()}, mod_fam.cpu()


def fit_temperature(D, o, mod_fam, ids):
    ym = torch.from_numpy(D.y_m[ids])
    best = (1e9, 1.0)
    for T in np.arange(0.5, 5.01, 0.05):
        lp, _ = dp.joint_logp(o["fl"], o["ml"], mod_fam, len(D.fams), float(T))
        nll = float(-lp.gather(1, ym[:, None]).mean())
        best = min(best, (nll, float(T)))
    return best[1]


def ece(conf, correct, nb=10):
    conf, correct = np.asarray(conf), np.asarray(correct, float)
    e = 0.0
    for lo in np.linspace(0, 1, nb, endpoint=False):
        m = (conf > lo) & (conf <= lo + 1 / nb)
        if m.any():
            e += m.mean() * abs(conf[m].mean() - correct[m].mean())
    return float(e)


def knob_sel(o, mvec):
    i = torch.arange(len(mvec))
    return o["mu"][i, mvec].numpy(), o["ls"][i, mvec].exp().numpy()


def evaluate(D, o, mod_fam, ids, T, sscale, train_mean):
    lp, lf = dp.joint_logp(o["fl"], o["ml"], mod_fam, len(D.fams), T)
    p = lp.exp().numpy()
    ym, yf, yk = D.y_m[ids], D.y_f[ids], D.y_k[ids]
    rank = np.argsort(-p, 1)
    top1 = rank[:, 0] == ym
    top5 = (rank[:, :5] == ym[:, None]).any(1)
    fam_pred = lf.argmax(1).numpy()
    fam_top3 = (np.argsort(-lf.numpy(), 1)[:, :3] == yf[:, None]).any(1)
    res = {"n": len(ids), "model_top1": float(top1.mean()), "model_top5": float(top5.mean()),
           "family_top1": float((fam_pred == yf).mean()), "family_top3": float(fam_top3.mean()),
           "model_top1_from_family_of_top1": float((mod_fam.numpy()[rank[:, 0]] == yf).mean()),
           "ece_model_top1": ece(p.max(1), top1),
           "mean_conf_top1": float(p.max(1).mean())}
    tol = np.array([0.15, 0.10])
    for cond, mv in (("oracle_model", ym), ("top1_model", rank[:, 0])):
        mu, sg = knob_sel(o, mv)
        sg = sg * sscale
        for k, name in ((0, "drive"), (1, "tone")):
            v = ~np.isnan(yk[:, k])
            if cond == "top1_model":
                # Tone of the predicted model is only comparable if both have a tone knob
                v = v & D.has_knob[mv, k]
            err = np.abs(mu[v, k] - yk[v, k])
            zabs = err / sg[v, k]
            res[f"{name}_{cond}_within_tol"] = float((err <= tol[k]).mean())
            res[f"{name}_{cond}_mae"] = float(err.mean())
            res[f"{name}_{cond}_cover68"] = float((zabs <= 1.0).mean())
            res[f"{name}_{cond}_cover95"] = float((zabs <= 1.96).mean())
            res[f"{name}_{cond}_median_sigma"] = float(np.median(sg[v, k]))
            if cond == "top1_model":
                res[f"{name}_top1_correct_within_tol"] = float((err[top1[v]] <= tol[k]).mean()) if top1[v].any() else None
            if cond == "oracle_model":
                bm = np.abs(train_mean[k] - yk[v, k])
                res[f"{name}_baseline_trainmean_within_tol"] = float((bm <= tol[k]).mean())
                res[f"{name}_baseline_trainmean_mae"] = float(bm.mean())
    # p-sigma: is a confident model call more often right? accuracy in confidence bins
    bins = [0, 0.25, 0.5, 0.75, 1.01]
    res["acc_by_conf"] = {f"{lo:.2f}-{hi:.2f}": [int(((p.max(1) >= lo) & (p.max(1) < hi)).sum()),
                                                 float(top1[(p.max(1) >= lo) & (p.max(1) < hi)].mean())
                                                 if ((p.max(1) >= lo) & (p.max(1) < hi)).any() else None]
                          for lo, hi in zip(bins[:-1], bins[1:])}
    return res, rank, p


def chance(D, tr_ids, te_ids):
    nm = len(D.models)
    fq = np.bincount(D.y_m[tr_ids], minlength=nm)
    ffq = np.bincount(D.y_f[tr_ids], minlength=len(D.fams))
    yt, yft = D.y_m[te_ids], D.y_f[te_ids]
    return {"model_top1_uniform": 1 / nm, "model_top5_uniform": 5 / nm,
            "model_top1_prior": float((yt == fq.argmax()).mean()),
            "model_top5_prior": float(np.isin(yt, np.argsort(-fq)[:5]).mean()),
            "family_top1_uniform": 1 / len(D.fams),
            "family_top1_prior": float((yft == ffq.argmax()).mean()),
            "family_top1_size_weighted": float(np.sum((np.bincount(yft, minlength=len(D.fams)) / len(yft)) ** 2))}


def cpu_latency(net, D):
    net = net.to("cpu").eval()
    torch.set_num_threads(1)
    x = torch.randn(1, dp.N_MELS, FR)
    with torch.no_grad():
        for _ in range(3):
            net(x)
        t0 = time.time()
        for _ in range(20):
            z, fl, ml = net(x)
            net.knobs(z.repeat(5, 1), torch.arange(5))
    torch.set_num_threads(2)
    return (time.time() - t0) / 20


def cmd_train(arm: str, epochs: int, max_s=None):
    X = None
    if not (OUT / "feats.npy").is_file():          # disk is tight: rebuild the features in RAM (about 40 s)
        X = cmd_features(ram=True)["X"]
    D = Data(X=X)
    dev = device()
    if arm == "all":
        tr, ca, te = D.sel("train"), D.sel("cal"), D.sel("test")
    elif arm == "xplayer":
        tr, ca, te = D.sel("train", "P2"), D.sel("cal", "P2"), D.sel("test", "P1")
    else:
        raise SystemExit(f"unknown arm {arm}")
    od = OUT / arm
    od.mkdir(parents=True, exist_ok=True)
    logf = open(od / "train.log", "w")

    def log(s):
        print(s, flush=True)
        logf.write(s + "\n"); logf.flush()
    log(f"arm {arm}: train {len(tr)} cal {len(ca)} test {len(te)} on {dev}")
    t0 = time.time()
    net, info = train(D, tr, epochs, dev, log=log, max_s=max_s)
    train_mean = np.nanmean(D.y_k[tr], 0)
    oc, mod_fam = infer(net, D, ca, dev, CROP)
    T = fit_temperature(D, oc, mod_fam, ca) if len(ca) >= 50 else 1.0
    # sigma scale per knob on the cal split, top-1-model conditioned: 68% of |err|/sigma within 1
    lpc, _ = dp.joint_logp(oc["fl"], oc["ml"], mod_fam, len(D.fams), T)
    mu, sg = knob_sel(oc, D.y_m[ca])
    sscale = np.ones(2)
    for k in range(2):
        v = ~np.isnan(D.y_k[ca, k])
        if v.sum() >= 30:
            sscale[k] = float(np.quantile(np.abs(mu[v, k] - D.y_k[ca, k][v]) / sg[v, k], 0.6827))
    results = {"arm": arm, "train_n": len(tr), "cal_n": len(ca), "test_n": len(te),
               "temperature": T, "sigma_scale": sscale.tolist(), **info, "chance": chance(D, tr, te)}
    srcs = np.array([D.recs[j]["source"] for j in te])
    plys = np.array([D.recs[j]["player"] for j in te])
    for clen, crop in (("6s", CROP), ("3s", 150), ("full", None)):
        o, _ = infer(net, D, te, dev, crop)
        r_raw, _, _ = evaluate(D, o, mod_fam, te, 1.0, np.ones(2), train_mean)
        r, rank, p = evaluate(D, o, mod_fam, te, T, sscale, train_mean)
        r["ece_model_top1_uncalibrated"] = r_raw["ece_model_top1"]
        for k in ("drive", "tone"):
            for c in ("oracle_model", "top1_model"):
                r[f"{k}_{c}_cover68_unscaled"] = r_raw[f"{k}_{c}_cover68"]
                r[f"{k}_{c}_cover95_unscaled"] = r_raw[f"{k}_{c}_cover95"]
        sub = {}
        for name, m in (("ds_new_di_seen_settings", srcs == "ds"), ("trust_new_di_new_settings", srcs != "ds"),
                        ("P1", plys == "P1"), ("P2", plys == "P2")):
            if m.sum():
                o2 = {kk: vv[torch.from_numpy(m)] for kk, vv in o.items()}
                sub[name], _, _ = evaluate(D, o2, mod_fam, te[m], T, sscale, train_mean)
        results[clen] = {"all": r, **sub}
        if clen == "6s":
            # per-family and confusion summary
            fam_acc = {}
            for f, fname in enumerate(D.fams):
                m = D.y_f[te] == f
                if m.any():
                    fam_acc[fname] = {"n": int(m.sum()), "model_top1": float((rank[m, 0] == D.y_m[te][m]).mean()),
                                      "model_top5": float((rank[m, :5] == D.y_m[te][m][:, None]).any(1).mean()),
                                      "n_models": int((mod_fam.numpy() == f).sum())}
            results["per_family_6s"] = fam_acc
            conf = {}
            for a, b in zip(D.y_m[te], rank[:, 0]):
                if a != b:
                    key = f"{D.models[a]} -> {D.models[b]}"
                    conf[key] = conf.get(key, 0) + 1
            results["top_confusions_6s"] = sorted(conf.items(), key=lambda kv: -kv[1])[:20]
    results["wall_s"] = time.time() - t0
    results["cpu_1thread_s_per_10s_clip"] = cpu_latency(net, D)
    torch.save({"state": net.state_dict(), "models": D.models, "families": D.fams, "temperature": T,
                "sigma_scale": sscale.tolist(), "frontend": json.loads((OUT / "index.json").read_text())["frontend"]},
               od / "model.pt")
    (od / "result.json").write_text(json.dumps(results, indent=1))
    log(json.dumps({k: results["6s"]["all"][k] for k in ("model_top1", "model_top5", "family_top1",
                                                          "drive_oracle_model_within_tol",
                                                          "tone_oracle_model_within_tol")}))
    return results


def cmd_probe():
    t0 = time.time()
    f = cmd_features(limit=400)
    per = f["s"] / f["n"]
    D = Data(probe=True)
    dev = device()
    ids = np.arange(len(D.recs))
    _, info = train(D, ids, 100, dev, max_s=60)
    n_all = len(dp.build_index())
    proj_feat = per * n_all
    proj_train = 30 * 8700 / info["samples_per_s"]
    out = {"feature_s_per_clip": per, "clips": n_all, "proj_feature_s": proj_feat,
           "train_samples_per_s": info["samples_per_s"], "params": info["params"],
           "proj_train_all_30ep_s": proj_train, "proj_total_s": proj_feat + 1.6 * proj_train + 300,
           "probe_wall_s": time.time() - t0}
    print(json.dumps(out, indent=1))
    (OUT / "probe.json").write_text(json.dumps(out, indent=1))
    for p in ("feats_probe.npy", "index_probe.json"):
        (OUT / p).unlink(missing_ok=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["features", "probe", "train"])
    ap.add_argument("--arm", default="all")
    ap.add_argument("--epochs", type=int, default=30)
    a = ap.parse_args()
    if a.cmd == "features":
        cmd_features()
    elif a.cmd == "probe":
        cmd_probe()
    else:
        cmd_train(a.arm, a.epochs)
