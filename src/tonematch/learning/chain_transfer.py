"""chain-transfer-v1 (T214, learning): do the single-block slot heads still name their block inside a full chain?

Human 2026-10-23 ("yes" to the lead's proposal; DECISIONS "chain transfer test approved"). Measurement only: no pass
bar, nothing adopted. About 300 random chains in the fixed slot order (human 2026-10-22)

    drive (p 0.7) -> amp (always) -> cab (always) -> mod (p 0.2) -> delay (p 0.3) -> reverb (p 0.3) -> EQ (p 0.2)

with models from the same pools as slot-dataset-v1 / drive-dataset-v1 and knobs drawn exactly as there (uniform on the
CC grid, level knobs at their default). A chain with a time effect (mod, delay, reverb) gets the approved random
0.5-2 s lead-in, read and rendered through the whole chain, then dropped. DI: only the drive-dataset-v1 `test` split
files (P1/P2 Guitar-TECHS; P3 never read). Rendered block-serially on Helix Native (one single-block preset per
block, each block on the previous block's output; T045: nulls at about -67 dB against the same chain in one
instance), one stage (slot) at a time so a host never switches block category mid-stage. Features as training:
AFx-Rep `mid`, three 3.33 s crops of the 10 s clip. Every stage's output is embedded too (a diagnostic: where along
the chain a head loses its block).

Pure functions here (planning, the stage-wise renderer over an injected pool, scoring) so tests use fakes.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence

import numpy as np

SR = 48_000
CLIP_S = 10.0
CHAIN_ORDER = ("drive", "amp", "cab", "mod", "delay", "reverb", "eq")      # human 2026-10-22
PROBS = {"drive": 0.7, "amp": 1.0, "cab": 1.0, "mod": 0.2, "delay": 0.3, "reverb": 0.3, "eq": 0.2}
TIME_SLOTS = ("mod", "delay", "reverb")
LEAD_S = (0.5, 2.0)                    # = slot-dataset-v1 lead-in for time-based effects (approved)
SEED = 214
N_CHAINS = 300
SILENCE_DB = -240.0                    # T212 B: the engine's digital-zero floor
KNOB_TOL = 0.15
GAIN_KNOBS = ("Drive", "Gain", "PreGain", "Pre Gain", "Overdrive", "DriveNorm", "NrmDrive", "DriveBright", "BrtDrive",
              "Drive 1", "Ch1 Drive")   # the amp's gain-type knob, for one breakdown


# ------------------------------------------------------------------------------------------ plan

def make_chains(files: Sequence[str], starts: dict[str, list[int]], pools: dict[str, dict], *, n: int = N_CHAINS,
                seed: int = SEED, probs: dict[str, float] | None = None, lead_s: tuple = LEAD_S,
                player_of: Callable[[str], str] | None = None) -> list[dict]:
    """Seeded random chains. `pools[slot]` = {"models": [...], "knobs": {model: [drawn knob names]},
    "families": {model: family}}; every listed knob is drawn uniformly on CC 0..127 (the caller lists only the
    knobs the training plan drew, so level knobs stay at their default). `files` are the DI files allowed (the
    test split); `starts[file]` the active 10 s window starts (drive's activity check)."""
    probs = dict(PROBS if probs is None else probs)
    rng = np.random.default_rng(seed)
    files = [f for f in files if starts.get(f)]
    if not files:
        raise ValueError("no DI file with an active window")
    player_of = player_of or (lambda f: f.split("/")[1][:2] if "/" in f else "")
    chains = []
    for k in range(n):
        f = files[int(rng.integers(len(files)))]
        s = int(starts[f][int(rng.integers(len(starts[f])))])
        blocks = []
        for slot in CHAIN_ORDER:
            if slot not in pools or rng.random() >= probs.get(slot, 0.0):
                continue
            p = pools[slot]
            m = p["models"][int(rng.integers(len(p["models"])))]
            cc = {nm: int(rng.integers(0, 128)) for nm in p["knobs"][m]}
            blocks.append({"slot": slot, "model": m, "family": p.get("families", {}).get(m, m), "cc": cc})
        has_time = any(b["slot"] in TIME_SLOTS for b in blocks)
        lead = int(round(rng.uniform(*lead_s) * SR)) if has_time and lead_s[1] > 0 else 0
        lead = min(lead, s)
        chains.append({"id": f"k{k:04d}", "di_file": f, "player": player_of(f), "start": s - lead, "lead": lead,
                       "blocks": blocks})
    return chains


def block_of(chain: dict, slot: str) -> dict | None:
    for b in chain["blocks"]:
        if b["slot"] == slot:
            return b
    return None


def slots_of(chain: dict) -> list[str]:
    return [b["slot"] for b in chain["blocks"]]


# ------------------------------------------------------------------------------------------ render

def mean_db(y: np.ndarray) -> float:
    return 10 * math.log10(float(np.mean(np.asarray(y, np.float64) ** 2)) + 1e-30)


def fold(y, n: int) -> np.ndarray:
    """Mono, `n` samples (as drive_dataset_render.render_one: stereo summed to mono, cut to the input length)."""
    y = np.asarray(y, np.float32)
    if y.ndim == 2:
        y = y.mean(axis=1 if y.shape[1] <= 2 else 0)
    y = y[:n]
    return y if len(y) == n else np.pad(y, (0, n - len(y)))


def check(y: np.ndarray, silence_db: float = SILENCE_DB) -> str | None:
    if not np.all(np.isfinite(y)):
        return "non-finite output"
    if mean_db(y) <= silence_db:
        return "Native returned silence"
    return None


def render_stage(pool, jobs: list[tuple[np.ndarray, object]], *, make_job, chunk: int = 24, guard=None,
                 log=print, silence_db: float = SILENCE_DB) -> tuple[list[np.ndarray | None], dict[int, str]]:
    """Render (input, preset) jobs on `pool` (``render_batch(list, on_error="none")``, as NativeRenderPool) in
    chunks, waiting on the busy-core `guard` (LoadGuard) before each chunk; a failed or silent job is retried once,
    then reported in errors. `make_job(dry, preset)` builds the pool's job object."""
    out: list[np.ndarray | None] = [None] * len(jobs)
    errs: dict[int, str] = {}
    pending = list(range(len(jobs)))
    for attempt in range(2):
        failed = []
        for c0 in range(0, len(pending), chunk):
            idx = pending[c0:c0 + chunk]
            if guard is not None:
                guard.wait(log=lambda d: log(f"busy cores {d}: pausing new renders"))
            ys = pool.render_batch([make_job(jobs[i][0], jobs[i][1]) for i in idx], on_error="none")
            le = getattr(pool, "last_errors", None) or {}
            for j, i in enumerate(idx):
                y = ys[j]
                if y is None:
                    errs[i] = str(le.get(j, "render failed"))[:300]
                    failed.append(i)
                    continue
                y = fold(y, len(jobs[i][0]))
                e = check(y, silence_db)
                if e:
                    errs[i] = e
                    failed.append(i)
                    continue
                errs.pop(i, None)
                out[i] = y
        if failed and attempt == 0:
            log(f"{len(failed)} jobs failed; retrying once")
        pending = failed
        if not pending:
            break
    return out, errs


def run_chains(chains: list[dict], presets: Callable[[dict], object], read_excerpt, open_pool, embed, *,
               make_job, guard=None, log=print, chunk: int = 24, silence_db: float = SILENCE_DB,
               order: Sequence[str] = CHAIN_ORDER, embed_stages: str = "all",
               stage_hook: Callable | None = None) -> dict:
    """Stage-wise block-serial render of every chain. Stage s renders the s-slot block of every chain that has one,
    on the previous stage's output (the DI for the first block). A fresh pool per stage (`open_pool()`, closed
    after; a Native host that switches block category slows down, slot-datasets-v1 notes). After each stage the
    chains' outputs (lead-in dropped) are embedded: `embed(y)` -> (crops, 512).

    Returns {"crops": (n, len(order), C, D) float16 (zeros where absent), "has": (n, len(order)) bool,
    "out_db": (n, len(order)) (nan where absent), "failed": {chain id: message}, "stage_s": {slot: s}}.
    A chain whose block fails twice is dropped from later stages and reported in "failed".
    `embed_stages="final"` (T215 training data) embeds only each chain's finished output (its last block's stage).
    A chain with a "gain_db" key has its DI excerpt scaled by that gain before the first block (T233 input-level
    randomisation; chains without the key are unchanged). `stage_hook(slot, i, x_in, y_out)` (optional) sees every
    successful block render: the stage input and output of chain i, lead-in included (T233 measures the EQ curve)."""
    n = len(chains)
    nclip = int(CLIP_S * SR)
    cur: dict[int, np.ndarray] = {}
    for i, c in enumerate(chains):
        cur[i] = np.asarray(read_excerpt(c["di_file"], c["start"], nclip + int(c["lead"])), np.float32)
        if c.get("gain_db"):
            cur[i] = (cur[i] * np.float32(10.0 ** (float(c["gain_db"]) / 20.0))).astype(np.float32)
    failed: dict[str, str] = {}
    crops = None
    has = np.zeros((n, len(order)), bool)
    out_db = np.full((n, len(order)), np.nan)
    stage_s = {}
    for si, slot in enumerate(order):
        todo = [i for i, c in enumerate(chains) if c["id"] not in failed and block_of(c, slot) is not None]
        if not todo:
            continue
        t0 = time.time()
        pool = open_pool()
        try:
            ys, errs = render_stage(pool, [(cur[i], presets(block_of(chains[i], slot))) for i in todo],
                                    make_job=make_job, chunk=chunk, guard=guard, log=log, silence_db=silence_db)
        finally:
            pool.close()
        r_s = time.time() - t0
        for j, i in enumerate(todo):
            c = chains[i]
            if ys[j] is None:
                failed[c["id"]] = f"{slot} {block_of(c, slot)['model']}: {errs.get(j)}"
                cur.pop(i, None)
                continue
            if stage_hook is not None:
                stage_hook(slot, i, cur[i], ys[j])
            cur[i] = ys[j]
            if embed_stages == "final" and slots_of(c)[-1] != slot:
                continue
            y = ys[j][int(c["lead"]):int(c["lead"]) + nclip]
            e = np.asarray(embed(y), np.float32)
            if crops is None:
                crops = np.zeros((n, len(order)) + e.shape, np.float16)
            crops[i, si] = e.astype(np.float16)
            has[i, si] = True
            out_db[i, si] = round(mean_db(y), 2)
        stage_s[slot] = {"render_s": round(r_s, 1), "total_s": round(time.time() - t0, 1), "jobs": len(todo)}
        log(f"stage {slot}: {len(todo)} blocks, render {r_s:.0f} s, all {time.time() - t0:.0f} s, "
            f"failed so far {len(failed)}")
    if crops is None:
        crops = np.zeros((n, len(order), 3, 512), np.float16)
    return {"crops": crops, "has": has, "out_db": out_db, "failed": failed, "stage_s": stage_s}


# ------------------------------------------------------------------------------------------ scoring

def item_scores(lj: np.ndarray, lf: np.ndarray, mu: np.ndarray, y_m: np.ndarray, y_f: np.ndarray, K: np.ndarray,
                tol: float = KNOB_TOL) -> dict:
    """Per-item hits. lj (n, M) calibrated joint log p (masked models at -inf), lf (n, F) family log p, mu (n, M, W)
    knob means, K (n, W) true knob positions (nan where the true model has no j-th knob). Knob hits use the TRUE
    model's knob predictions (as slot_dataset.score_slot `knob_within_true`)."""
    n = len(y_m)
    rank = np.argsort(-lj, 1)
    idx = np.arange(n)
    m_true = mu[idx, y_m]
    has = np.isfinite(K)
    ok = has & np.isfinite(m_true)
    hit = np.where(ok, np.abs(K - m_true) <= tol, False)
    return {"top1": rank[:, 0] == y_m, "top3": (rank[:, :3] == y_m[:, None]).any(1),
            "top5": (rank[:, :5] == y_m[:, None]).any(1), "fam1": lf.argmax(1) == y_f,
            "knob_hit": hit, "knob_has": has, "pred": rank[:, 0]}


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def aggregate(s: dict, sel: np.ndarray | None = None, *, boot: int = 1000, seed: int = 0) -> dict:
    """Model top-1/3/5 and family top-1 with Wilson 95 % intervals; knobs within tol pooled over every knob of the
    selected items, with a 95 % interval from a bootstrap over items (knobs of one clip are not independent)."""
    n_all = len(s["top1"])
    sel = np.ones(n_all, bool) if sel is None else np.asarray(sel, bool)
    n = int(sel.sum())
    r = {"n": n}
    for k in ("top1", "top3", "top5", "fam1"):
        h = int(s[k][sel].sum())
        r[k] = h / n if n else math.nan
        r[k + "_ci"] = wilson(h, n)
    hit, has = s["knob_hit"][sel], s["knob_has"][sel]
    nh = has.sum(1)
    hh = (hit & has).sum(1)
    r["knob_n"] = int(nh.sum())
    r["knob"] = float(hh.sum() / nh.sum()) if nh.sum() else math.nan
    if n and nh.sum() and boot:
        rng = np.random.default_rng(seed)
        b = []
        for _ in range(boot):
            j = rng.integers(0, n, n)
            d = nh[j].sum()
            if d:
                b.append(hh[j].sum() / d)
        r["knob_ci"] = (float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5)))
    else:
        r["knob_ci"] = (math.nan, math.nan)
    return r


def knob_by_name(s: dict, names: list[list[str]], y_m: np.ndarray, sel: np.ndarray | None = None) -> dict:
    sel = np.ones(len(y_m), bool) if sel is None else sel
    by: dict[str, list[bool]] = {}
    for i in np.where(sel)[0]:
        for j, nm in enumerate(names[int(y_m[i])]):
            if j < s["knob_has"].shape[1] and s["knob_has"][i, j]:
                by.setdefault(nm, []).append(bool(s["knob_hit"][i, j]))
    return {nm: {"within": float(np.mean(v)), "n": len(v)} for nm, v in sorted(by.items())}


def amp_gain_cc(chain: dict) -> int | None:
    b = block_of(chain, "amp")
    if b is None:
        return None
    for k in GAIN_KNOBS:
        if k in b["cc"]:
            return int(b["cc"][k])
    return None


def contexts(chain: dict, slot: str) -> dict[str, bool | None]:
    """Context flags for one block of a chain: which other slots are before / after it, and the amp's gain knob.
    None = not applicable (e.g. "drive before" for the drive block itself)."""
    sl = slots_of(chain)
    pos = sl.index(slot)
    before, after = set(sl[:pos]), set(sl[pos + 1:])
    out: dict[str, bool | None] = {}
    for o in CHAIN_ORDER:
        if o == slot or o in ("amp", "cab"):
            continue
        out[f"{o} present"] = o in sl
    out["time effect after"] = bool(after & set(TIME_SLOTS))
    out["delay or reverb after"] = bool(after & {"delay", "reverb"})
    out["any block after (besides cab)"] = bool(after - {"cab"})
    out["time effect before"] = bool(before & set(TIME_SLOTS))
    g = amp_gain_cc(chain)
    out["amp gain knob >= 64"] = None if g is None or slot == "amp" else g >= 64
    if slot == "amp":
        b = block_of(chain, "amp")
        out["preamp model"] = b["model"].startswith("HD2_Preamp")
    out["5+ blocks"] = len(sl) >= 5
    return out
