"""Statistics over a folder of user-curated .hlx presets (T109).

Offline, numpy only. Everything here returns plain dicts. Which of them may be committed is the
caller's decision: functions whose output has model ids, preset names, authors or artists are
marked LOCAL; the rest is category-level only.

Folder convention of the curated corpus (data/presets/, gitignored):
  Artist Patches/<artist>/*.hlx     Amps & Styles/<decade or style>/*.hlx
  Effects & Tones/<effect>/*.hlx    Miscellaneous/*.hlx
"""
from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import math
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import chain_prior as CP

STOMP_IDS = {2162694: "HX Stomp", 2162699: "HX Stomp XL"}
GROUPS = {"Artist Patches": "artist", "Amps & Styles": "style_folder",
          "Effects & Tones": "effect_folder", "Miscellaneous": "misc"}
_DECADE = re.compile(r"^\d0s$")
_FLOW = ("input", "output", "split", "join")
PRESENCE_CATS = ("drive", "comp", "eq", "mod", "delay", "reverb", "pitch", "wah", "volpan", "filter")
# model-id substring -> amp family (heuristic on Line 6's own model ids; LOCAL labels)
_FAMILY = [
    ("bass", ("bass", "svt", "jazzrivet", "regalbass")),
    ("vox_like", ("a30", "essex", "ac30")),
    ("british_plexi_like", ("brit", "plexi", "2203", "2204", "j45", "p75", "tucngo", "stoneage", "soupro")),
    ("us_clean_like", ("ususer", "usdeluxe", "usdouble", "ussuper", "usprincess", "ussmall", "usdripman",
                       "tweed", "mailorder", "jazz", "vitriolclean", "placaterclean", "solo", "fullerton")),
    ("modern_high_gain_like", ("cali", "panama", "rectifire", "revvgen", "german", "angl", "cartograph",
                               "dasbenz", "agua", "sledge", "mandarin", "matchstick", "grammatico",
                               "vitriol", "archetype", "placater", "gsg", "cougar", "voltage", "whowatt",
                               "interstate", "derailed", "divided", "busyone", "moon", "delsol", "woody")),
    ("line6_original", ("line6",)),
]


def amp_family(model_id: str) -> str:
    n = model_id.lower().replace("hd2_", "").replace("preamp", "", 1) if model_id else ""
    n = n.removeprefix("amp")
    for fam, keys in _FAMILY:
        if any(k in n for k in keys):
            return fam
    return "other"


def fw_string(v) -> str | None:
    """device_version 0x0311xxxx -> "3.11"."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return f"{v >> 24}.{(v >> 16) & 0xFF:02x}"


# ----------------------------------------------------------------------------- artists (LOCAL)

def norm_name(s: str) -> str:
    s = s.lower().replace("&", "and")
    s = re.sub(r"\b(the|and)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def artist_crosswalk(folders: list[str], listed: list[str], cutoff: float = 0.86) -> dict:
    """Normalise folder names and cross-reference Artist List.txt. LOCAL."""
    nl = {norm_name(x): x for x in listed}
    nf: dict = defaultdict(list)
    for f in folders:
        nf[norm_name(f)].append(f)
    exact, fuzzy, folder_only = {}, {}, []
    for k, fs in nf.items():
        if k in nl:
            exact[fs[0]] = nl[k]
        else:
            close = difflib.get_close_matches(k, list(nl), n=1, cutoff=cutoff)
            if close:
                fuzzy[fs[0]] = nl[close[0]]
            else:
                folder_only.append(fs[0])
    matched = {norm_name(v) for v in exact.values()} | {norm_name(v) for v in fuzzy.values()}
    list_only = sorted(x for x in listed if norm_name(x) not in matched)
    dup_folders = {k: v for k, v in nf.items() if len(v) > 1}
    return {"folders": len(folders), "listed": len(listed), "exact": exact, "fuzzy": fuzzy,
            "folder_not_in_list": sorted(folder_only), "list_not_in_folders": list_only,
            "folders_normalising_together": list(dup_folders.values())}


# ----------------------------------------------------------------------------- scanning

@dataclass
class Rec:
    rel: str
    group: str = ""
    sub: str = ""
    name: str = ""
    author: str = ""
    ctx: dict = field(default_factory=dict)       # group, artist, decade, style, effect
    ok: bool = False
    err: str = ""
    device: int | None = None
    fw: str | None = None
    chain: CP.Chain | None = None
    thash: str = ""
    shash: str = ""                                # model sequence + enabled pattern (params ignored)
    n_total: int = 0                               # every block slot incl. cabs, any DSP
    n_cab: int = 0
    n_dsp1: int = 0
    n_active: int = 0
    n_snap: int = 0
    unknown: list = field(default_factory=list)    # model ids missing from the catalog
    not_stomp: list = field(default_factory=list)  # catalog models flagged stomp=False
    amp_model: str = ""
    amp_drive: float | None = None
    models: list = field(default_factory=list)     # (category, model id) of active blocks
    kind: str = "hlx"                              # hlx | setlist_member | setlist | encrypted

    @property
    def cats(self) -> list[str]:
        return self.chain.linear if self.chain else []

    @property
    def stomp_family(self) -> bool:
        return self.device in STOMP_IDS

    @property
    def stomp_blocks(self) -> int:
        """Blocks that count against the Stomp's 8: every `blockN`; `cab0` keys are the amp's own cab."""
        return self.n_total - self.n_cab

    @property
    def fits_stomp(self) -> bool:
        return self.ok and self.stomp_blocks <= 8 and self.n_dsp1 == 0


def _blocks(tone: dict):
    for di, dsp in enumerate(("dsp0", "dsp1")):
        d = tone.get(dsp)
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, dict) and "@model" in v and not k.startswith(_FLOW):
                    yield di, k, v


def _rec_from_obj(obj: dict, rel: str, group: str, sub: str, name: str, catalog: dict | None) -> Rec:
    r = Rec(rel=rel, group=group, sub=sub, name=name)
    try:
        data = obj["data"] if isinstance(obj.get("data"), dict) and "tone" in obj["data"] else obj
        tone = data["tone"]
        r.device = data.get("device", obj.get("device"))
        r.fw = fw_string(data.get("device_version", obj.get("device_version")))
        meta = data.get("meta") or obj.get("meta") or {}
        r.name = str(meta.get("name") or name)
        r.author = str(meta.get("author") or "")
        r.chain = CP.extract_chain(obj, catalog)
    except Exception as e:  # noqa: BLE001
        r.err = f"{type(e).__name__}: {str(e)[:60]}"
        return r
    r.ok = True
    r.thash = hashlib.sha1(json.dumps(tone, sort_keys=True).encode()).hexdigest()[:16]
    cat_models = (catalog or {}).get("models", {})
    sig = []
    first_amp = None
    for di, k, v in sorted(_blocks(tone), key=lambda t: (t[0], t[2].get("@position", 0), t[1])):
        m = v["@model"]
        cat = CP.categorize(m, catalog)
        r.n_total += 1
        r.n_cab += k.startswith("cab")
        r.n_dsp1 += di == 1
        info = cat_models.get(m)
        if catalog and info is None:
            r.unknown.append(m)
        elif info is not None and info.get("stomp") is False:
            r.not_stomp.append(m)
        active = bool(v.get("@enabled")) or CP._snapshot_enabled(tone, f"dsp{di}", k)
        r.n_active += active
        sig.append((m, bool(active)))
        if active and cat in ("amp", "preamp") and first_amp is None:
            first_amp = v
            r.amp_model = m
            d = v.get("Drive", v.get("Gain"))
            r.amp_drive = float(d) if isinstance(d, (int, float)) else None
    r.shash = hashlib.sha1(repr(sig).encode()).hexdigest()[:16]
    r.n_snap = sum(1 for k in tone if k.startswith("snapshot") and isinstance(tone[k], dict)
                   and tone[k].get("@valid", True))
    r.models = list(r.chain.models)
    return r


def read_recs(path: Path, root: Path, catalog: dict | None) -> list[Rec]:
    """One Rec for a plain .hlx; one per preset for a setlist saved with an .hlx extension
    (those get group "setlist" and no style/artist context). Encrypted files are NOT decrypted."""
    rel = path.relative_to(root).as_posix()
    parts = rel.split("/")
    group = GROUPS.get(parts[0], "misc")
    sub = parts[1] if len(parts) > 2 else ""
    try:
        obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        return [Rec(rel=rel, group=group, sub=sub, name=path.stem, err=f"json {type(e).__name__}")]
    if isinstance(obj, dict) and obj.get("schema") == "L6Setlist" or "encryption" in obj:
        if "encryption" in obj:
            return [Rec(rel=rel, group=group, sub=sub, name=path.stem, err="encrypted container (not read)",
                        kind="encrypted")]
        try:
            from .engines.native_setlist import decode_hls
            presets = decode_hls(obj)["presets"]
        except Exception as e:  # noqa: BLE001
            return [Rec(rel=rel, group=group, sub=sub, name=path.stem, kind="setlist",
                        err=f"setlist {type(e).__name__}: {str(e)[:40]}")]
        out = []
        for i, pr in enumerate(presets):
            r = _rec_from_obj(pr, f"{rel}#{i}", "setlist", "", f"{path.stem}#{i}", catalog)
            r.kind = "setlist_member" if "tone" in pr else "empty_slot"
            out.append(r)
        return out
    return [_rec_from_obj(obj, rel, group, sub, path.stem, catalog)]


def context_of(r: Rec) -> dict:
    ctx = {"group": r.group}
    if r.group == "artist" and r.sub:
        ctx["artist"] = r.sub
    elif r.group == "style_folder" and r.sub:
        key = "decade" if _DECADE.match(CP.norm_ctx(r.sub)) else "style"
        ctx[key] = CP.norm_ctx(r.sub)
    elif r.group == "effect_folder" and r.sub:
        ctx["effect"] = CP.norm_ctx(r.sub)
    return ctx


def scan(root: Path, catalog: dict | None = None) -> list[Rec]:
    recs = []
    for p in sorted(root.rglob("*.hlx")):
        for r in read_recs(p, root, catalog):
            r.ctx = context_of(r)
            recs.append(r)
    return recs


def dedupe(recs: list[Rec]) -> tuple[list[Rec], dict]:
    """Keep the first of every identical `tone` (sorted path order). Returns (kept, stats)."""
    seen: dict = {}
    kept, dup = [], 0
    for r in recs:
        if not r.ok:
            continue
        if r.thash in seen:
            dup += 1
            continue
        seen[r.thash] = r
        kept.append(r)
    names = Counter((CP.norm_ctx(r.name), r.author) for r in kept)
    shapes = Counter(r.shash for r in kept)
    return kept, {"exact_duplicates_removed": dup,
                  "same_name_and_author_among_kept": sum(v - 1 for v in names.values() if v > 1),
                  "same_models_and_enabled_among_kept": sum(v - 1 for v in shapes.values() if v > 1)}


# ----------------------------------------------------------------------------- order patterns

def collapse(seq: list[str], mapping: dict | None = None) -> list[str]:
    out: list[str] = []
    for c in seq:
        c = (mapping or {}).get(c, c)
        if not out or out[-1] != c:
            out.append(c)
    return out


AMP_UNIT = {"amp": "amp_unit", "preamp": "amp_unit", "cab": "amp_unit"}


def position_stats(seqs: list[list[str]], kmax: int = 7, kend: int = 3) -> dict:
    """Share of sequences with category c at index k from the start (denominator: sequences
    longer than k) and at k from the end (0 = last, 1 = last-1 ...). Category level."""
    start: dict = {}
    for k in range(kmax + 1):
        pool = [s[k] for s in seqs if len(s) > k]
        if pool:
            cnt = Counter(pool)
            start[str(k)] = {"n": len(pool), "share": {c: round(v / len(pool), 4) for c, v in cnt.most_common()}}
    end: dict = {}
    for k in range(kend + 1):
        pool = [s[-1 - k] for s in seqs if len(s) > k]
        if pool:
            cnt = Counter(pool)
            end[str(k)] = {"n": len(pool), "share": {c: round(v / len(pool), 4) for c, v in cnt.most_common()}}
    return {"n": len(seqs), "from_start": start, "from_end": end,
            "mean_length": round(float(np.mean([len(s) for s in seqs])), 3) if seqs else 0}


def run_lengths(seqs: list[list[str]]) -> dict:
    """For each category: how often consecutive same-category blocks form a group of size 1, 2, 3+."""
    out: dict = defaultdict(Counter)
    for s in seqs:
        for c, g in itertools.groupby(s):
            out[c][min(len(list(g)), 3)] += 1
    return {c: {"groups": sum(v.values()), "size1": v[1], "size2": v[2], "size3plus": v[3]}
            for c, v in out.items()}


def pair_stats(seqs: list[list[str]], cats: list[str], shuffles: int = 20, seed: int = 0) -> dict:
    """For every category pair with both present: n, share x-before-y (first occurrences), swap
    index (0 fixed order, 1 coin flip), adjacency rate and its lift against shuffled orders."""
    rng = random.Random(seed)
    rows: dict = {}
    obs: dict = defaultdict(lambda: [0, 0, 0, 0.0])   # both, x_first, adjacent, expected adjacent
    for s in seqs:
        present = [c for c in cats if c in s]
        if len(present) < 2:
            continue
        first = {c: s.index(c) for c in present}
        adj = {frozenset(p) for p in itertools.pairwise(s) if p[0] != p[1]}
        exp: Counter = Counter()
        for _ in range(shuffles):
            t = s[:]
            rng.shuffle(t)
            for p in {frozenset(q) for q in itertools.pairwise(t) if q[0] != q[1]}:
                exp[p] += 1
        for x, y in itertools.combinations(present, 2):
            o = obs[(x, y)]
            o[0] += 1
            o[1] += first[x] < first[y]
            o[2] += frozenset((x, y)) in adj
            o[3] += exp[frozenset((x, y))] / shuffles
    for (x, y), (n, xf, ad, ex) in obs.items():
        p = xf / n
        rows[f"{x}|{y}"] = {"n": n, "x_first": round(p, 4), "swap_index": round(1 - abs(2 * p - 1), 4),
                            "adjacent": round(ad / n, 4), "adjacent_if_shuffled": round(ex / n, 4),
                            "adjacency_lift": round((ad / n) / (ex / n), 3) if ex else None}
    return rows


def contiguity(seqs: list[list[str]], members: set[str], shuffles: int = 100, seed: int = 0,
               min_members: int = 2) -> dict:
    """Among sequences with >= min_members blocks from `members`: share where those blocks form
    one contiguous run, versus the same sequences shuffled. Lift > 1 means they habitually sit together."""
    rng = random.Random(seed)

    def contig(s):
        idx = [i for i, c in enumerate(s) if c in members]
        return idx[-1] - idx[0] + 1 == len(idx)

    n = ok = 0
    exp = 0.0
    for s in seqs:
        if sum(c in members for c in s) < min_members or len({c for c in s if c in members}) < 2:
            continue
        n += 1
        ok += contig(s)
        t = s[:]
        h = 0
        for _ in range(shuffles):
            rng.shuffle(t)
            h += contig(t)
        exp += h / shuffles
    return {"n": n, "contiguous": round(ok / n, 4) if n else None,
            "contiguous_if_shuffled": round(exp / n, 4) if n else None,
            "lift": round((ok / n) / (exp / n), 3) if n and exp else None}


def propose_groups(seqs: list[list[str]], cats: list[str], min_n: int = 60, top: int = 12,
                   shuffles: int = 60) -> list[dict]:
    """Data-driven candidates: every subset of 2-4 frequent categories scored by contiguity lift."""
    out = []
    for r in (2, 3, 4):
        for sub in itertools.combinations(cats, r):
            c = contiguity(seqs, set(sub), shuffles=shuffles)
            if c["n"] >= min_n and c["lift"]:
                out.append({"members": list(sub), **c})
    # reward lift and sample size; prefer groups that beat their own best sub-pair by a margin
    out.sort(key=lambda d: -(d["lift"] * (1 - 1 / (1 + d["n"] / 100))))
    return out[:top]


def cluster_categories(seqs: list[list[str]], cats: list[str], min_n: int = 40) -> dict:
    """Average-linkage clustering of categories by position profile + predecessor/successor
    profile (Hellinger distance). Returns merges (a, b, distance) and the feature table."""
    feats: dict = {}
    names = [*cats, "<s>", "</s>"]
    for c in cats:
        pos = np.zeros(6)
        pred: Counter = Counter()
        succ: Counter = Counter()
        n = 0
        for s in seqs:
            if c not in s or len(s) < 2:
                continue
            for i, x in enumerate(s):
                if x == c:
                    n += 1
                    pos[min(int(5 * i / (len(s) - 1)), 5)] += 1
                    pred[s[i - 1] if i else "<s>"] += 1
                    succ[s[i + 1] if i + 1 < len(s) else "</s>"] += 1
        if n < min_n:
            continue
        vec = np.concatenate([pos / n, [pred[x] / n for x in names], [succ[x] / n for x in names]])
        feats[c] = vec
    keys = list(feats)
    if len(keys) < 3:
        return {"merges": [], "categories": keys}
    X = np.sqrt(np.array([feats[k] for k in keys]))
    D = np.sqrt(((X[:, None, :] - X[None, :, :]) ** 2).sum(-1) / 2)
    clusters = {i: [i] for i in range(len(keys))}
    merges = []
    while len(clusters) > 1:
        best = None
        for a, b in itertools.combinations(clusters, 2):
            d = np.mean([D[i, j] for i in clusters[a] for j in clusters[b]])
            if best is None or d < best[0]:
                best = (d, a, b)
        d, a, b = best
        merges.append({"left": [keys[i] for i in clusters[a]], "right": [keys[i] for i in clusters[b]],
                       "distance": round(float(d), 3)})
        clusters[a] = clusters[a] + clusters.pop(b)
    return {"merges": merges, "categories": keys}


# ----------------------------------------------------------------------------- usage (LOCAL)

def concentration(counts: Counter) -> dict:
    tot = sum(counts.values())
    if not tot:
        return {}
    v = np.array(sorted(counts.values(), reverse=True), float)
    p = v / v.sum()
    cum = np.cumsum(p)
    out = {"blocks": int(tot), "models": len(v), "effective_models": round(float(math.exp(-(p * np.log(p)).sum())), 1)}
    for k in (1, 3, 5, 10, 20):
        out[f"top{k}_share"] = round(float(cum[min(k, len(v)) - 1]), 4)
    for q in (0.5, 0.8, 0.9, 0.95):
        out[f"models_for_{int(q * 100)}pct"] = int(np.searchsorted(cum, q) + 1)
    return out


def model_usage(recs: list[Rec]) -> dict:
    """LOCAL: per category the ranked models (blocks, presets using them) and concentration."""
    blocks: dict = defaultdict(Counter)
    presets: dict = defaultdict(Counter)
    for r in recs:
        for cat, m in r.models:
            blocks[cat][m] += 1
        for cat, m in set(r.models):
            presets[cat][m] += 1
    return {cat: {"concentration": concentration(blocks[cat]),
                  "ranked": [[m, n, presets[cat][m]] for m, n in blocks[cat].most_common()]}
            for cat in sorted(blocks)}


# ----------------------------------------------------------------------------- metadata strength

def features(r: Rec) -> dict:
    cats = r.cats
    cnt = Counter(cats)
    n = len(cats)
    f = {"n_blocks": "<=4" if n <= 4 else "5-6" if n <= 6 else "7-8" if n <= 8 else "9+",
         "two_path": "yes" if r.chain and r.chain.two_path else "no",
         "delay_count": str(min(cnt["delay"], 2)), "reverb_count": str(min(cnt["reverb"], 2)),
         "drive_count": str(min(cnt["drive"], 2))}
    for c in PRESENCE_CATS:
        f[f"has_{c}"] = "yes" if cnt[c] else "no"
    d = r.amp_drive
    f["gain"] = "no_amp" if not r.amp_model else ("unknown" if d is None else
                                                  "low" if d < 0.35 else "mid" if d <= 0.65 else "high")
    f["amp_family"] = amp_family(r.amp_model) if r.amp_model else "no_amp"
    f["shape_set"] = "+".join(sorted(set(cats) - {"cab"}))
    f["group_sequence"] = ">".join(collapse(cats))
    return f


def _entropy(counts) -> float:
    v = np.array([c for c in counts if c > 0], float)
    p = v / v.sum()
    return float(-(p * np.log2(p)).sum())


def mutual_info(x: list, y: list) -> float:
    n = len(x)
    cx, cy, cxy = Counter(x), Counter(y), Counter(zip(x, y))
    return sum(c / n * math.log2((c / n) / ((cx[a] / n) * (cy[b] / n))) for (a, b), c in cxy.items())


def mi_vs_shuffle(feat: list, ctx: list, perms: int = 100, seed: int = 0) -> dict:
    """Bias-corrected mutual information (bits) of feature and context: observed MI minus the
    mean MI when context labels are shuffled. `frac` = corrected / H(feature); z against the null."""
    rng = random.Random(seed)
    h = _entropy(Counter(feat).values())
    mi = mutual_info(feat, ctx)
    nulls = []
    c = ctx[:]
    for _ in range(perms):
        rng.shuffle(c)
        nulls.append(mutual_info(feat, c))
    mu, sd = float(np.mean(nulls)), float(np.std(nulls)) or 1e-9
    return {"n": len(feat), "feature_values": len(set(feat)), "context_values": len(set(ctx)),
            "H_feature": round(h, 3), "MI": round(mi, 4), "null_mean": round(mu, 4),
            "corrected_MI": round(mi - mu, 4), "frac_of_H": round((mi - mu) / h, 4) if h else 0.0,
            "z": round((mi - mu) / sd, 1)}


def _folds(n: int, k: int, groups: list | None, seed: int) -> list[list[int]]:
    rng = random.Random(seed)
    if groups is None:
        idx = list(range(n))
        rng.shuffle(idx)
        return [idx[i::k] for i in range(k)]
    gs = sorted(set(groups))
    rng.shuffle(gs)
    fold_of = {g: i % k for i, g in enumerate(gs)}
    out: list[list[int]] = [[] for _ in range(k)]
    for i, g in enumerate(groups):
        out[fold_of[g]].append(i)
    return out


def cv_lookup(feat: list, ctx: list, groups: list | None = None, k: int = 5, kappa: float = 3.0,
              seed: int = 0) -> dict:
    """Held-out predictor of `feat` from `ctx`: P(f|c) = (n_cf + kappa P(f)) / (n_c + kappa), P(f)
    the smoothed train marginal. Reports accuracy and log-loss (bits) against the marginal only.
    `groups` (e.g. authors) keeps all presets of a group in one fold."""
    vocab = sorted(set(feat))
    V = len(vocab)
    n = len(feat)
    ll_c = ll_m = 0.0
    acc_c = acc_m = 0
    for te in _folds(n, k, groups, seed):
        tes = set(te)
        tr = [i for i in range(n) if i not in tes]
        if not tr or not te:
            continue
        marg = Counter(feat[i] for i in tr)
        pm = {f: (marg[f] + 0.5) / (len(tr) + 0.5 * V) for f in vocab}
        major = max(marg, key=marg.get)
        cf: dict = defaultdict(Counter)
        for i in tr:
            cf[ctx[i]][feat[i]] += 1
        for i in te:
            c = cf.get(ctx[i], Counter())
            nc = sum(c.values())
            pc = {f: (c[f] + kappa * pm[f]) / (nc + kappa) for f in vocab}
            ll_c += -math.log2(pc[feat[i]])
            ll_m += -math.log2(pm[feat[i]])
            acc_c += max(pc, key=pc.get) == feat[i]
            acc_m += major == feat[i]
    return {"n": n, "acc_ctx": round(acc_c / n, 4), "acc_majority": round(acc_m / n, 4),
            "logloss_ctx": round(ll_c / n, 4), "logloss_marginal": round(ll_m / n, 4),
            "gain_bits": round((ll_m - ll_c) / n, 4)}


def metadata_strength(recs: list[Rec], contexts=("group", "decade", "style", "effect", "artist", "author"),
                      feature_names=None, perms: int = 100, min_n: int = 100) -> dict:
    """{context: {feature: {mi.., cv_random.., cv_by_author..}}} over the presets that have that context."""
    feats = [features(r) for r in recs]
    names = feature_names or [k for k in feats[0] if k not in ("shape_set",)]
    out: dict = {}
    for ctxk in contexts:
        idx = [i for i, r in enumerate(recs) if (r.author if ctxk == "author" else r.ctx.get(ctxk))]
        if len(idx) < min_n:
            continue
        cv = [recs[i].author if ctxk == "author" else recs[i].ctx[ctxk] for i in idx]
        authors = [recs[i].author or f"?{recs[i].rel}" for i in idx]
        out[ctxk] = {"n": len(idx), "contexts": len(set(cv)), "features": {}}
        for fn in names:
            fv = [feats[i][fn] for i in idx]
            if len(set(fv)) < 2:
                continue
            ent = {"mi": mi_vs_shuffle(fv, cv, perms)}
            if fn != "group_sequence":      # hundreds of values: held-out log-loss is meaningless
                ent["cv_random"] = cv_lookup(fv, cv, None)
                if ctxk != "author":        # author-grouped folds never contain the author
                    ent["cv_by_author"] = cv_lookup(fv, cv, authors)
            out[ctxk]["features"][fn] = ent
    return out


def conditional_cv(recs: list[Rec], keys=("decade", "style", "effect", "group", "artist"), k: int = 5,
                   min_n: int = 100, seed: int = 0, kappas=(10, 20, 50, 100, 300)) -> dict:
    """Held-out bits per block (author-grouped folds) of the global bigram prior versus the
    context-conditioned prior with backoff, for several backoff strengths kappa, on the presets
    that carry that context. Positive gain = the context helps predict the order."""
    out: dict = {}
    for key in keys:
        idx = [i for i, r in enumerate(recs) if r.cats and r.ctx.get(key)]
        if len(idx) < min_n:
            continue
        sub = [recs[i] for i in idx]
        groups = [r.author or f"?{r.rel}" for r in sub]
        tot_g = 0.0
        tot_c = {kp: 0.0 for kp in kappas}
        nblk = covered = 0
        for te in _folds(len(sub), k, groups, seed):
            tes = set(te)
            tr = [sub[i] for i in range(len(sub)) if i not in tes]
            prior = CP.build_prior([r.chain for r in tr], contexts=[r.ctx for r in tr],
                                   context_keys=(*CP.CONTEXT_KEYS, "artist"))
            for i in te:
                r = sub[i]
                tot_g += CP.sequence_logprob(r.cats, prior)
                nblk += len(r.cats) + 1
                covered += bool(CP.context_priors(prior, r.ctx))
                for kp in kappas:
                    prior["kappa"] = kp
                    tot_c[kp] += CP.sequence_logprob(r.cats, prior, r.ctx)
        ln2 = math.log(2)
        out[key] = {"n": len(sub), "bits_per_block_global": round(-tot_g / nblk / ln2, 4),
                    "share_with_context_prior": round(covered / len(sub), 3),
                    "by_kappa": {str(kp): round((tot_c[kp] - tot_g) / nblk / ln2, 4) for kp in kappas}}
        best = max(out[key]["by_kappa"].items(), key=lambda t: t[1])
        out[key]["best_kappa"], out[key]["gain_bits_per_block"] = best[0], best[1]
    return out


def reverse_classifier(recs: list[Rec], target: str, k: int = 5, min_class: int = 25) -> dict | None:
    """Predict a context value (decade / style / effect / group) from chain features with
    logistic regression, author-grouped folds. None when scikit-learn is absent."""
    try:
        from sklearn.feature_extraction import DictVectorizer
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return None
    sub = [r for r in recs if r.ctx.get(target) and r.cats]
    cnt = Counter(r.ctx[target] for r in sub)
    sub = [r for r in sub if cnt[r.ctx[target]] >= min_class]
    if len({r.ctx[target] for r in sub}) < 2 or len(sub) < 100:
        return None
    fx = []
    for r in sub:
        f = features(r)
        d = {f"{a}={b}": 1 for a, b in f.items() if a not in ("shape_set", "group_sequence")}
        d["amp_drive"] = r.amp_drive if r.amp_drive is not None else 0.0
        d["len"] = len(r.cats)
        fx.append(d)
    y = np.array([r.ctx[target] for r in sub])
    X = DictVectorizer().fit_transform(fx)
    groups = [r.author or f"?{r.rel}" for r in sub]
    correct = base = 0
    for te in _folds(len(sub), k, groups, 0):
        tes = set(te)
        tr = [i for i in range(len(sub)) if i not in tes]
        if len(set(y[tr])) < 2 or not te:
            continue
        clf = LogisticRegression(max_iter=300, C=0.5).fit(X[tr], y[tr])
        correct += int((clf.predict(X[te]) == y[te]).sum())
        major = Counter(y[tr]).most_common(1)[0][0]
        base += int((y[te] == major).sum())
    n = len(sub)
    return {"n": n, "classes": len(set(y)), "acc": round(correct / n, 4), "acc_majority": round(base / n, 4)}
