"""product-chain-v1 (T233, learning): a predictor for the product chain drive -> (EQ) -> (reverb).

Human 2026-10-26 "Do both now" (DECISIONS "product-chain predictor overnight; PRS calibration recordings as a
training player"). Measurement only: nothing is admitted into the matcher until the human hears it.

The chain is the human's product chain (2026-10-25: drive, then light colouring; delay on hold; EQ kept with options;
reverb needs a model): drive always (the 54-model drive-dataset-v1 pool, every drawn knob uniform on the CC grid,
level knobs at their default), then with p 0.5 an EQ (Parametric with a 5-12 kHz high-cut, or Graphic 10 Band, every
gain within +-6 dB), then with p 0.6 a reverb (Room / Plate / Hall, Mix uniform 0.15-0.50, RT60 log-uniform over
0.8-2.5 s inverted to the model's Decay knob by the T230 impulse calibration, Predelay 11 ms, LowCut 150, HighCut
3700). No amp, cab, delay or modulation.

DI sources: Guitar-TECHS P1/P2 drive-dataset-v1 files (train split for training, test split for the held-out
evaluation; P3 is never read), and the human's prs-7-tone session (local only): training excerpts from the runs,
the chords E/A/D/G/C, the palm-muted notes and riff, and the first half of the free playing; evaluation excerpts
from the second half of the free playing and the E5/A5/D5 power chords. Each excerpt's input gain is drawn over
+-6 dB (capped so the peak stays at or below -1 dBFS) and its loud-frame input level (T194 convention, after the
gain) is a label; the level-conditioned heads get (level + 26.7) / 10 appended to each crop embedding (T194 v2cond).

Pure functions here (sources, sampling, labels, small heads, scores); the Native driver is
scripts/product_chain_t233.py.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import numpy as np

SR = 48_000
CLIP_S = 10.0
ORDER = ("drive", "eq", "reverb")
PROBS = {"drive": 1.0, "eq": 0.5, "reverb": 0.6}
LEAD_S = (0.5, 2.0)                    # approved lead-in before a time effect (slot-dataset-v1)
GAIN_DB = 6.0                          # input gain drawn uniformly over +-GAIN_DB
PEAK_CAP_DBFS = -1.0                   # ... but never above this peak
REF_LEVEL_DB = -26.7                   # T194 reference loud-frame level
EQ_MODELS = {"parametric": "HD2_EQParametric", "graphic": "HD2_EQGraphic10Band"}
EQ_LIMIT_DB = 6.0
HIGHCUT_HZ = (5000.0, 12000.0)
GRAPHIC_KNOBS = ("31p25Hz", "62p5Hz", "125Hz", "250Hz", "500Hz", "1kHz", "2kHz", "4kHz", "8kHz", "16kHz")
GRAPHIC_CENTRES = (31.25, 62.5, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)
GRAPHIC_FIXED_ZERO = ("31p25Hz", "62p5Hz")       # as the T228/T229 fits: bands under 100 Hz stay at 0 dB
CURVE_BANDS = GRAPHIC_CENTRES[2:]                 # the measured EQ-stage curve is a label on 125 Hz .. 16 kHz
PEQ_FREQS = {"LowFreq": (80.0, 400.0), "MidFreq": (300.0, 5000.0), "HighFreq": (1500.0, 8000.0)}
REVERB_MODELS = {"Room": "HD2_ReverbRoom", "Plate": "HD2_ReverbPlate", "Hall": "HD2_ReverbHall"}
REVERB_FIXED = {"Predelay": 0.011, "LowCut": 150.0, "HighCut": 3700.0}
MIX_RANGE = (0.15, 0.50)
RT60_RANGE = (0.8, 2.5)

# the human's prs-7-tone session (segment ids of scripts/guitar_profile_record.py): file, start s, end s (None = end)
PRS_TRAIN = {
    "prs:runs": [(f"run_s{k}.wav", 0.0, None) for k in range(1, 7)],               # b
    "prs:chords": [(f"chord_{c}.wav", 0.0, None) for c in ("E", "A", "D", "G", "C")],  # c (open chords)
    "prs:palm": [("palm_notes.wav", 0.0, None), ("palm_riff.wav", 0.0, None)],       # e
    "prs:free-a": [("free.wav", 0.0, 15.0)],                                         # g, first half
}
PRS_TEST = {
    "prs:free-b": [("free.wav", 15.0, None)],                                        # g, second half (held out)
    "prs:chords5": [(f"chord_{c}.wav", 0.0, None) for c in ("E5", "A5", "D5")],      # c, power chords (held out)
}


# ------------------------------------------------------------------------------------------ DI sources

def check_prs_split(train: dict = PRS_TRAIN, test: dict = PRS_TEST, durations: dict[str, float] | None = None) -> None:
    """Raise if any held-out stretch of a session file is also in a training source."""
    def spans(d):
        out = {}
        for parts in d.values():
            for f, a, b in parts:
                e = (durations or {}).get(f, math.inf) if b is None else b
                out.setdefault(f, []).append((float(a), float(e)))
        return out
    tr, te = spans(train), spans(test)
    for f, ss in te.items():
        for a, b in ss:
            for c, d in tr.get(f, []):
                if a < d and c < b:
                    raise AssertionError(f"held-out {f} {a}-{b} s overlaps training {c}-{d} s")


def build_source(parts: Sequence[tuple[str, float, float | None]], read: Callable[[str], np.ndarray],
                 sr: int = SR) -> np.ndarray:
    """Concatenate the parts (file, start s, end s or None) of a virtual DI source; `read(file)` -> mono float32."""
    xs = []
    for f, a, b in parts:
        x = np.asarray(read(f), np.float32)
        xs.append(x[int(round(a * sr)): None if b is None else int(round(b * sr))])
    return np.concatenate(xs) if xs else np.zeros(0, np.float32)


def excerpt(x: np.ndarray, start: int, n: int) -> np.ndarray:
    """x[start:start+n], zero-padded at the end."""
    y = np.asarray(x[int(start): int(start) + int(n)], np.float32)
    return y if len(y) == n else np.pad(y, (0, n - len(y)))


def fallback_starts(x: np.ndarray, sr: int = SR, clip_s: float = CLIP_S, hop_s: float = 0.5,
                    floor_db: float = -60.0) -> list[int]:
    """Window starts whose 10 s hold at least 60 % of 100 ms frames above floor_db (for short session sources whose
    windows have fewer than drive's three onsets, e.g. strummed chords)."""
    n, fr = int(clip_s * sr), int(0.1 * sr)
    if len(x) < n:
        return []
    out = []
    for s in range(0, len(x) - n + 1, int(hop_s * sr)):
        w = x[s:s + n]
        e = 10 * np.log10(np.mean(w[: len(w) // fr * fr].astype(np.float64).reshape(-1, fr) ** 2, 1) + 1e-12)
        if np.mean(e > floor_db) >= 0.6:
            out.append(s)
    return out


def level_gain(draw_db: float, peak_dbfs: float, cap_dbfs: float = PEAK_CAP_DBFS) -> float:
    """The drawn input gain, lowered if needed so the excerpt's peak stays at or below cap_dbfs."""
    return float(min(draw_db, cap_dbfs - peak_dbfs))


def level_feature(level_db) -> np.ndarray:
    """The conditioning input: (input level + 26.7) / 10 (T194 v2cond scale: gain to the reference in dB / 10)."""
    return (np.asarray(level_db, np.float64) - REF_LEVEL_DB) / 10.0


def with_level(X: np.ndarray, level_db: np.ndarray) -> np.ndarray:
    """Append the level feature to every crop: (n, C, D) -> (n, C, D + 1)."""
    f = level_feature(level_db).astype(np.float32)
    return np.concatenate([X, np.repeat(f[:, None, None], X.shape[1], 1)], 2).astype(np.float32)


# ------------------------------------------------------------------------------------------ block sampling

def rt60_curve(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """(decay knob, RT60 s) of one model's calibration rows, sorted by knob, RT60 made non-decreasing."""
    d = np.array([r["decay"] for r in rows], float)
    t = np.array([r["rt60_s"] for r in rows], float)
    o = np.argsort(d)
    return d[o], np.maximum.accumulate(t[o])


def rt60_range(rows: list[dict], lo: float = RT60_RANGE[0], hi: float = RT60_RANGE[1]) -> tuple[float, float]:
    """The part of [lo, hi] s this model can reach (Plate cannot go under ~0.97 s, Room not over ~2.27 s)."""
    _, t = rt60_curve(rows)
    a, b = max(lo, float(t.min())), min(hi, float(t.max()))
    if a >= b:
        raise ValueError(f"model cannot reach RT60 {lo}-{hi} s (its range {t.min():.2f}-{t.max():.2f})")
    return a, b


def decay_for_rt60(rows: list[dict], rt60: float) -> float:
    """Decay knob giving the RT60 by log-linear interpolation of the impulse calibration."""
    d, t = rt60_curve(rows)
    return float(np.clip(np.interp(math.log(rt60), np.log(t), d), d.min(), d.max()))


def rt60_for_decay(rows: list[dict], decay: float) -> float:
    d, t = rt60_curve(rows)
    return float(math.exp(np.interp(decay, d, np.log(t))))


def draw_reverb(rng: np.random.Generator, cal_rows: dict[str, list[dict]]) -> dict:
    t = list(REVERB_MODELS)[int(rng.integers(len(REVERB_MODELS)))]
    m = REVERB_MODELS[t]
    a, b = rt60_range(cal_rows[m])
    rt = float(math.exp(rng.uniform(math.log(a), math.log(b))))
    dec = round(decay_for_rt60(cal_rows[m], rt), 4)
    mix = round(float(rng.uniform(*MIX_RANGE)), 4)
    return {"slot": "reverb", "model": m, "family": t, "type": t, "mix": mix, "rt60_s": round(rt, 4), "decay": dec,
            "values": {"Decay": dec, **REVERB_FIXED, "Mix": mix}}


def smooth121(g: np.ndarray) -> np.ndarray:
    p = np.concatenate([[g[0]], g, [g[-1]]])
    return 0.25 * p[:-2] + 0.5 * p[1:-1] + 0.25 * p[2:]


def draw_eq(rng: np.random.Generator) -> dict:
    """Parametric or Graphic 10 Band with 50/50 odds. Graphic: 8 band gains (125 Hz..16 kHz) drawn uniform +-6, smoothed
    1-2-1 and rescaled so the largest is uniform 1.5..6 dB (a curve, not white noise); 31 and 62 Hz stay 0. Parametric:
    Low/Mid/High gains uniform +-6 dB, frequencies log-uniform in PEQ_FREQS, Q at default, high-cut log-uniform
    5-12 kHz, low-cut at default."""
    t = "parametric" if rng.random() < 0.5 else "graphic"
    if t == "graphic":
        g = smooth121(rng.uniform(-EQ_LIMIT_DB, EQ_LIMIT_DB, 8))
        g = g / max(1e-9, np.abs(g).max()) * rng.uniform(1.5, EQ_LIMIT_DB)
        g = np.clip(np.round(g, 2), -EQ_LIMIT_DB, EQ_LIMIT_DB)
        values = {k: 0.0 for k in GRAPHIC_FIXED_ZERO}
        values.update({k: float(v) for k, v in zip(GRAPHIC_KNOBS[2:], g, strict=True)})
    else:
        values = {k: round(float(rng.uniform(-EQ_LIMIT_DB, EQ_LIMIT_DB)), 2) for k in ("LowGain", "MidGain", "HighGain")}
        for k, (lo, hi) in PEQ_FREQS.items():
            values[k] = round(float(math.exp(rng.uniform(math.log(lo), math.log(hi)))), 1)
        values["HighCut"] = round(float(math.exp(rng.uniform(*np.log(HIGHCUT_HZ)))), 0)
    return {"slot": "eq", "model": EQ_MODELS[t], "family": t, "type": t, "values": values}


def draw_drive(rng: np.random.Generator, pool: dict) -> dict:
    m = pool["models"][int(rng.integers(len(pool["models"])))]
    cc = {nm: int(rng.integers(0, 128)) for nm in pool["knobs"][m]}
    return {"slot": "drive", "model": m, "family": pool.get("families", {}).get(m, m), "cc": cc}


def make_chains(sources: Sequence[str], starts: dict[str, list[int]], drive_pool: dict, cal_rows: dict, *, n: int,
                seed: int, split: str, prefix: str, probs: dict | None = None, lead_s: tuple = LEAD_S,
                source_weights: dict[str, float] | None = None, player_of: Callable[[str], str] | None = None) -> list[dict]:
    """Seeded product chains over the DI `sources` (files or virtual session sources). `source_weights` (source ->
    weight) sets the draw odds per source (default uniform). The input gain is drawn here (`gain_draw_db`); the
    planner caps it by the excerpt's peak and records the level."""
    probs = dict(PROBS if probs is None else probs)
    rng = np.random.default_rng(seed)
    srcs = [s for s in sources if starts.get(s)]
    if not srcs:
        raise ValueError("no DI source with an active window")
    w = np.array([(source_weights or {}).get(s, 1.0) for s in srcs], float)
    w = w / w.sum()
    player_of = player_of or (lambda f: "PRS" if f.startswith("prs:") else (f.split("/")[1][:2] if "/" in f else ""))
    out = []
    for k in range(n):
        f = srcs[int(rng.choice(len(srcs), p=w))]
        s = int(starts[f][int(rng.integers(len(starts[f])))])
        blocks = [draw_drive(rng, drive_pool)]
        if rng.random() < probs["eq"]:
            blocks.append(draw_eq(rng))
        if rng.random() < probs["reverb"]:
            blocks.append(draw_reverb(rng, cal_rows))
        has_time = any(b["slot"] == "reverb" for b in blocks)
        lead = int(round(rng.uniform(*lead_s) * SR)) if has_time and lead_s[1] > 0 else 0
        lead = min(lead, s)
        out.append({"id": f"{prefix}{k:05d}", "split": split, "source": "prs" if f.startswith("prs:") else "lib",
                    "di_file": f, "player": player_of(f), "start": s - lead, "lead": lead,
                    "gain_draw_db": round(float(rng.uniform(-GAIN_DB, GAIN_DB)), 2), "blocks": blocks})
    return out


# ------------------------------------------------------------------------------------------ labels

def ltas_band_db(x: np.ndarray, centres=CURVE_BANDS, n_fft: int = 8192, sr: int = SR) -> np.ndarray:
    """Long-term spectrum of the active frames, power-averaged in +-1/2 octave around each centre, dB (as
    search.colour.ltas_octave_db)."""
    x = np.asarray(x, np.float64)
    hop = n_fft // 4
    n = 1 + max(0, (len(x) - n_fft) // hop)
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    win = np.hanning(n_fft)
    fr = np.stack([x[i * hop: i * hop + n_fft] * win for i in range(n)])
    p = np.abs(np.fft.rfft(fr, axis=1)) ** 2
    e = 10 * np.log10(p.sum(1) + 1e-18)
    act = e >= np.percentile(e, 95) - 35.0
    spec = p[act].mean(0)
    f = np.fft.rfftfreq(n_fft, 1 / sr)
    return np.array([10 * np.log10(spec[(f >= c / math.sqrt(2)) & (f < c * math.sqrt(2))].mean() + 1e-18)
                     for c in centres])


def eq_curve_db(x_in: np.ndarray, y_out: np.ndarray, centres=CURVE_BANDS) -> list[float]:
    """The EQ stage's measured curve: output / input long-term spectrum per band, dB (the label shared by both EQ
    types; a Parametric high-cut shows up in the top bands)."""
    return [round(float(v), 2) for v in ltas_band_db(y_out, centres) - ltas_band_db(x_in, centres)]


def block_of(chain: dict, slot: str) -> dict | None:
    for b in chain["blocks"]:
        if b["slot"] == slot:
            return b
    return None


def aux_targets(chains: list[dict]) -> dict[str, np.ndarray]:
    """Per-chain targets of the colouring heads (nan where the block is absent)."""
    n = len(chains)
    rt = list(REVERB_MODELS)
    et = list(EQ_MODELS)
    out = {"reverb_present": np.zeros(n), "reverb_type": np.full(n, -1), "reverb_mix": np.full(n, np.nan),
           "reverb_logrt60": np.full(n, np.nan), "eq_present": np.zeros(n), "eq_type": np.full(n, -1),
           "eq_curve": np.full((n, len(CURVE_BANDS)), np.nan)}
    for i, c in enumerate(chains):
        r = block_of(c, "reverb")
        if r is not None:
            out["reverb_present"][i] = 1
            out["reverb_type"][i] = rt.index(r["type"])
            out["reverb_mix"][i] = r["mix"]
            out["reverb_logrt60"][i] = math.log(r["rt60_s"])
        e = block_of(c, "eq")
        if e is not None:
            out["eq_present"][i] = 1
            out["eq_type"][i] = et.index(e["type"])
            if e.get("curve_db") is not None:
                out["eq_curve"][i] = e["curve_db"]
    return out


# ------------------------------------------------------------------------------------------ small heads

class SmallMLP:
    """Two hidden layers on crop embeddings (each crop a sample; crops averaged at predict time). task: 'bce'
    (sigmoid outputs), 'ce' (softmax classes) or 'mse' (standardised regression targets)."""

    def __init__(self, task: str, n_out: int, hidden: int = 256, epochs: int = 40, seed: int = 0,
                 dropout: float = 0.3, lr: float = 1e-3):
        self.task, self.n_out, self.hidden, self.epochs, self.seed, self.dropout, self.lr = \
            task, n_out, hidden, epochs, seed, dropout, lr

    def fit(self, X: np.ndarray, Y: np.ndarray):
        import torch
        from torch import nn
        torch.manual_seed(self.seed)
        rng = np.random.default_rng(self.seed)
        C = X.shape[1]
        Xf = X.reshape(-1, X.shape[-1]).astype(np.float32)
        Y = np.asarray(Y)
        if self.task == "mse":
            Y = Y.reshape(len(Y), -1).astype(np.float64)
            self.mu, self.sd = np.nanmean(Y, 0), np.nanstd(Y, 0) + 1e-9
            Y = ((Y - self.mu) / self.sd).astype(np.float32)
        Yf = np.repeat(Y, C, 0)
        self.xm, self.xs = Xf.mean(0), Xf.std(0) + 1e-6
        Xf = (Xf - self.xm) / self.xs
        self.net = nn.Sequential(nn.Linear(Xf.shape[1], self.hidden), nn.GELU(), nn.Dropout(self.dropout),
                                 nn.Linear(self.hidden, self.hidden), nn.GELU(), nn.Dropout(self.dropout),
                                 nn.Linear(self.hidden, self.n_out))
        Xt = torch.tensor(Xf, dtype=torch.float32)
        Yt = torch.tensor(Yf, dtype=torch.long if self.task == "ce" else torch.float32)
        opt = torch.optim.AdamW(self.net.parameters(), lr=self.lr, weight_decay=1e-3)
        lossf = {"bce": nn.BCEWithLogitsLoss(), "ce": nn.CrossEntropyLoss(), "mse": nn.MSELoss()}[self.task]
        self.net.train()
        for _ in range(self.epochs):
            perm = rng.permutation(len(Xt))
            for i in range(0, len(perm), 256):
                b = torch.tensor(perm[i:i + 256])
                o = self.net(Xt[b] + 0.01 * torch.randn_like(Xt[b]))
                yb = Yt[b]
                if self.task == "bce" and yb.ndim == 1:
                    yb = yb[:, None]
                loss = lossf(o, yb)
                opt.zero_grad()
                loss.backward()
                opt.step()
        self.net.eval()
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        """bce: probabilities (n, n_out); ce: class probabilities (n, n_out); mse: targets in their own units."""
        import torch
        outs = []
        with torch.no_grad():
            for k in range(X.shape[1]):
                z = self.net(torch.tensor((X[:, k].astype(np.float32) - self.xm) / self.xs, dtype=torch.float32))
                outs.append(torch.sigmoid(z).numpy() if self.task == "bce" else
                            torch.softmax(z, 1).numpy() if self.task == "ce" else z.numpy())
        o = np.mean(outs, 0)
        return o * self.sd + self.mu if self.task == "mse" else o

    def __getstate__(self):
        d = dict(self.__dict__)
        if "net" in d and not isinstance(d["net"], dict):
            d["net_state"] = d.pop("net").state_dict()
        return d

    def __setstate__(self, d):
        st = d.pop("net_state", None)
        self.__dict__.update(d)
        if st is not None:
            from torch import nn
            din = st["0.weight"].shape[1]
            self.net = nn.Sequential(nn.Linear(din, self.hidden), nn.GELU(), nn.Dropout(self.dropout),
                                     nn.Linear(self.hidden, self.hidden), nn.GELU(), nn.Dropout(self.dropout),
                                     nn.Linear(self.hidden, self.n_out))
            self.net.load_state_dict(st)
            self.net.eval()


class LinearAux:
    """Logistic regression (task 'bce' / 'ce') or ridge (task 'mse') on crop embeddings, crops averaged at predict."""

    def __init__(self, task: str, C: float = 1.0, alpha: float = 1.0):
        self.task, self.C, self.alpha = task, C, alpha

    def fit(self, X: np.ndarray, Y: np.ndarray):
        from sklearn.linear_model import LogisticRegression, Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        Xf = X.reshape(-1, X.shape[-1])
        Yf = np.repeat(np.asarray(Y), X.shape[1], 0)
        if self.task == "mse":
            self.m = make_pipeline(StandardScaler(), Ridge(alpha=self.alpha)).fit(Xf, Yf)
        else:
            self.m = make_pipeline(StandardScaler(), LogisticRegression(C=self.C, max_iter=2000)).fit(Xf, Yf.astype(int))
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        outs = []
        for k in range(X.shape[1]):
            if self.task == "mse":
                o = self.m.predict(X[:, k])
                outs.append(o.reshape(len(X), -1))
            else:
                p = self.m.predict_proba(X[:, k])
                outs.append(p[:, 1:2] if self.task == "bce" else p)
        return np.mean(outs, 0)


def make_aux(kind: str, task: str, n_out: int, *, epochs: int = 40, seed: int = 0):
    return LinearAux(task) if kind == "logreg" else SmallMLP(task, n_out, epochs=epochs, seed=seed)


# ------------------------------------------------------------------------------------------ scores

def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def presence_score(p: np.ndarray, y: np.ndarray, thr: float = 0.5) -> dict:
    p, y = np.asarray(p, float).ravel(), np.asarray(y, float).ravel() >= 0.5
    pred = p >= thr
    n = len(y)
    hit = int((pred == y).sum())
    tpr = float(pred[y].mean()) if y.any() else math.nan
    tnr = float((~pred[~y]).mean()) if (~y).any() else math.nan
    try:
        from sklearn.metrics import roc_auc_score
        auc = float(roc_auc_score(y, p)) if 0 < y.sum() < n else math.nan
    except ValueError:
        auc = math.nan
    return {"n": n, "n_present": int(y.sum()), "acc": hit / n if n else math.nan, "acc_ci": wilson(hit, n),
            "balanced": (tpr + tnr) / 2, "tpr": tpr, "tnr": tnr, "auc": auc,
            "majority": float(max(y.mean(), 1 - y.mean())) if n else math.nan}


def type_score(P: np.ndarray, y: np.ndarray) -> dict:
    y = np.asarray(y, int)
    n = len(y)
    if not n:
        return {"n": 0}
    hit = int((P.argmax(1) == y).sum())
    K = P.shape[1]
    conf = np.zeros((K, K), int)
    for a, b in zip(y, P.argmax(1)):
        conf[a, b] += 1
    return {"n": n, "acc": hit / n, "acc_ci": wilson(hit, n), "chance": 1.0 / K,
            "majority": float(np.bincount(y, minlength=K).max() / n), "confusion": conf.tolist()}


def regression_score(pred: np.ndarray, y: np.ndarray, tols: Sequence[float] = ()) -> dict:
    pred, y = np.asarray(pred, float), np.asarray(y, float)
    ok = np.isfinite(y) & np.isfinite(pred)
    if pred.ndim > 1:
        ok = ok.all(1) if ok.ndim > 1 else ok
    pred, y = pred[ok], y[ok]
    n = len(y)
    if not n:
        return {"n": 0}
    err = pred - y
    base = np.abs(y - y.mean(0)).mean()
    r = {"n": n, "mae": float(np.abs(err).mean()), "bias": float(err.mean()), "mae_const": float(base)}
    if y.ndim == 1 and n > 2:
        from scipy.stats import spearmanr
        r["spearman"] = float(spearmanr(pred, y).statistic)
    else:
        r["mae_per_band"] = [float(v) for v in np.abs(err).mean(0)]
        r["mae_const_per_band"] = [float(v) for v in np.abs(y - y.mean(0)).mean(0)]
    for t in tols:
        r[f"within_{t:g}"] = float((np.abs(err) <= t).mean())
    return r


def level_buckets(level_db: np.ndarray, edges=(-50.0, -40.0, -35.0, -30.0, -25.0, -10.0)) -> np.ndarray:
    """Bucket index per clip by input level (dBFS); edges as the T194 dose-response table, in absolute terms."""
    return np.digitize(np.asarray(level_db, float), edges[1:-1])
