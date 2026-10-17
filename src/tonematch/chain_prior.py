"""Chain-pattern prior: how real presets order block CATEGORIES.

Two halves.
  * Extraction (`categorize`, `extract_chain`, `build_prior`): turns preset dicts (the `tone` tree of
    an `.hlx` / `.hls` entry) into category sequences and aggregates them. Used offline by
    `scripts/chain_patterns.py`.
  * Query API (`load_prior`, `sequence_logprob`, `top_structures`, `typical_position`): reads the
    committed aggregate `data/chain_prior/category_prior.json`. Category level only: no model
    names, no preset names.

Signal flow: blocks are ordered by `@position`. Two-path presets (split/join) become
`pre + (A | B) + post`; the two branches are parallel, so no bigram links A to B.
Cabs (`cab0`, `cab1`) follow the amp they belong to (first, second amp).
"""
from __future__ import annotations

import itertools
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRIOR_PATH = ROOT / "data" / "chain_prior" / "category_prior.json"
SCHEMA = 1

CATEGORIES = ("drive", "amp", "preamp", "cab", "comp", "eq", "mod", "delay", "reverb",
              "pitch", "filter", "wah", "volpan", "looper", "sendreturn", "other")
START, END = "<s>", "</s>"
DEFAULT_ALPHA = 0.5
DEFAULT_KAPPA = 100.0       # backoff strength: pseudo-counts of the global prior mixed into a context
MIN_CONTEXT_N = 15          # a context with fewer presets is ignored (backs off to the global prior)
CONTEXT_KEYS = ("decade", "style", "effect", "group")  # committed; "artist" stays in local priors

# catalog `source_file` stem -> chain category
_BY_FILE = {
    "amp": "amp", "preamp": "preamp", "cab": "cab", "cabmicirs": "cab", "cabmicirswithpan": "cab",
    "distortion": "drive", "compressor": "comp", "gate": "comp", "eq": "eq", "modulation": "mod",
    "delay": "delay", "reverb": "reverb", "pitch-synth": "pitch", "filter": "filter",
    "wah": "wah", "volumepan": "volpan", "sendreturn": "sendreturn",
}
# model-id prefixes (after "HD2_") for use without a catalog, longest match wins
_PREFIX = [
    ("Preamp", "preamp"), ("Amp", "amp"), ("Cab", "cab"), ("ImpulseResponse", "cab"),
    ("Dist", "drive"), ("Comp", "comp"), ("Dyn", "comp"), ("Gate", "comp"), ("EQ", "eq"),
    ("Delay", "delay"), ("DL4", "delay"), ("Reverb", "reverb"),
    ("Chorus", "mod"), ("Flanger", "mod"), ("Phaser", "mod"), ("Tremolo", "mod"),
    ("Rotary", "mod"), ("Vibe", "mod"), ("Mod", "mod"), ("Pitch", "pitch"), ("Synth", "pitch"),
    ("Filter", "filter"), ("FM4", "filter"), ("Wah", "wah"), ("VolPan", "volpan"),
    ("Looper", "looper"), ("FXLoop", "sendreturn"), ("Send", "sendreturn"),
    ("Return", "sendreturn"),
]
_FLOW = ("input", "output", "split", "join")


def categorize(model_id: str, catalog: dict | None = None) -> str:
    """Chain category of a model id. Uses the catalog when given, else a name heuristic."""
    if catalog:
        m = catalog.get("models", {}).get(model_id)
        if m:
            stem = str(m.get("source_file", "")).replace(".models", "")
            if stem == "fixed":
                return "looper" if "Looper" in model_id else (
                    "cab" if "ImpulseResponse" in model_id else "other")
            if stem in _BY_FILE:
                return _BY_FILE[stem]
            return m.get("category") if m.get("category") in CATEGORIES else "other"
    name = model_id.split("_", 1)[1] if "_" in model_id else model_id
    best = ""
    cat = "other"
    for pre, c in _PREFIX:
        if name.startswith(pre) and len(pre) > len(best):
            best, cat = pre, c
    if cat == "other" and model_id.startswith(("VIC_", "L6", "Victoria")):
        cat = "delay"
    return cat


@dataclass
class Chain:
    """Active blocks of one preset as categories: pre, parallel branches A and B, post."""
    pre: list[str] = field(default_factory=list)
    a: list[str] = field(default_factory=list)
    b: list[str] = field(default_factory=list)
    post: list[str] = field(default_factory=list)
    models: list[tuple[str, str]] = field(default_factory=list)  # (category, model id)

    @property
    def linear(self) -> list[str]:
        return self.pre + self.a + self.b + self.post

    @property
    def two_path(self) -> bool:
        return bool(self.a and self.b)

    def edges(self) -> list[tuple[str, str]]:
        """Directed transitions including START and END; branches are parallel."""
        branches = [x for x in (self.a, self.b) if x]
        seqs = [self.pre] if self.pre else []
        out: list[tuple[str, str]] = []
        for s in seqs:
            out += list(itertools.pairwise([START, *s]))
        tail = self.pre[-1] if self.pre else START
        for br in branches:
            out += list(itertools.pairwise([tail, *br]))
        ends = [br[-1] for br in branches] or [tail]
        nxt = self.post[0] if self.post else END
        out += [(e, nxt) for e in ends]
        if self.post:
            out += list(itertools.pairwise(self.post))
            out.append((self.post[-1], END))
        return out

    def comparable(self, i: int, j: int) -> bool:
        """False when linear indices i, j sit in different parallel branches."""
        na, nb = len(self.pre) + len(self.a), len(self.b)
        in_a = len(self.pre) <= i < na
        in_b = na <= i < na + nb
        j_a = len(self.pre) <= j < na
        j_b = na <= j < na + nb
        return not ((in_a and j_b) or (in_b and j_a))

    def notation(self) -> str:
        parts = list(self.pre)
        if self.a or self.b:
            parts.append("(" + ",".join(self.a) + "|" + ",".join(self.b) + ")")
        return ">".join(parts + self.post)


def _snapshot_enabled(tone: dict, dsp: str, key: str) -> bool:
    for sk, sv in tone.items():
        if (sk.startswith("snapshot") and isinstance(sv, dict)
                and sv.get("blocks", {}).get(dsp, {}).get(key) is True):
            return True
    return False


def preset_tone(obj: dict) -> dict | None:
    """The `tone` tree of an hlx wrapper, a bare preset, or a tone dict."""
    if "tone" in obj:
        return obj["tone"]
    if isinstance(obj.get("data"), dict) and "tone" in obj["data"]:
        return obj["data"]["tone"]
    if "dsp0" in obj:
        return obj
    return None


def extract_chain(obj: dict, catalog: dict | None = None) -> Chain:
    """Category chain of one preset. Blocks bypassed in every snapshot (and in the base) are dropped."""
    tone = preset_tone(obj)
    chain = Chain()
    if tone is None:
        return chain
    pre: list[tuple] = []
    a: list[tuple] = []
    b: list[tuple] = []
    post: list[tuple] = []
    cabs: dict[int, tuple[str, str]] = {}
    for di, dsp in enumerate(("dsp0", "dsp1")):
        d = tone.get(dsp)
        if not isinstance(d, dict):
            continue
        sp = d.get("split", {}).get("@position") if "split" in d else None
        jn = d.get("join", {}).get("@position") if "join" in d else None
        items = []
        for key, v in d.items():
            if not isinstance(v, dict) or "@model" not in v or key.startswith(_FLOW):
                continue
            active = bool(v.get("@enabled")) or _snapshot_enabled(tone, dsp, key)
            cat = categorize(v["@model"], catalog)
            if key.startswith("cab"):
                try:
                    cabs[int(key[3:])] = (cat, v["@model"]) if active else None
                except ValueError:
                    pass
                continue
            if not active:
                continue
            pos = v.get("@position", 0)
            items.append((di, pos, v.get("@path", 0), key, cat, v["@model"]))
        items.sort(key=lambda t: (t[1], t[3]))
        max_b = max([t[1] for t in items if t[2] == 1], default=None)
        for it in items:
            _di, pos, path, _key, _cat, _model = it
            if sp is None or max_b is None:
                (post if (a or b or post) else pre).append(it)
            elif path == 1:
                b.append(it)
            elif pos < sp:
                pre.append(it)
            elif pos > max(jn if jn is not None else max_b, max_b):
                post.append(it)
            else:
                a.append(it)
    # cabs attach to amps in flow order: cab0 to the first amp, cab1 to the second
    order = [("pre", pre), ("a", a), ("b", b), ("post", post)]
    amp_n = 0
    result = {"pre": [], "a": [], "b": [], "post": []}
    for name, lst in order:
        for it in lst:
            cat, model = it[4], it[5]
            result[name].append((cat, model))
            if cat in ("amp", "preamp"):
                cb = cabs.get(amp_n)
                amp_n += 1
                if cb:
                    result[name].append(cb)
    chain.pre = [c for c, _ in result["pre"]]
    chain.a = [c for c, _ in result["a"]]
    chain.b = [c for c, _ in result["b"]]
    chain.post = [c for c, _ in result["post"]]
    chain.models = [m for name in ("pre", "a", "b", "post") for m in result[name]]
    return chain


# ----------------------------------------------------------------------------- aggregation

def norm_ctx(v) -> str:
    """Canonical context value: lower case, no apostrophes/spaces ("80's" and "80S" -> "80s")."""
    return "".join(ch for ch in str(v).lower() if ch.isalnum() or ch in "-&")


def build_conditional(chains: list[Chain], contexts: list[dict], keys=CONTEXT_KEYS,
                      min_n: int = 5) -> dict:
    """Per-context sub-priors: {key: {value: {n, bigram_counts, share_present, n_blocks_hist}}}.
    `contexts[i]` is the metadata dict of `chains[i]` (decade, style, effect, group, artist ...)."""
    groups: dict = defaultdict(lambda: defaultdict(list))
    for ch, ctx in zip(chains, contexts):
        if not ch.linear:
            continue
        for k in keys:
            if ctx.get(k):
                groups[k][norm_ctx(ctx[k])].append(ch)
    out: dict = {}
    for k, vals in groups.items():
        for v, chs in vals.items():
            if len(chs) < min_n:
                continue
            big: Counter = Counter()
            pres: Counter = Counter()
            nb: Counter = Counter()
            for ch in chs:
                for e in ch.edges():
                    big[f"{e[0]}>{e[1]}"] += 1
                for c in set(ch.linear):
                    pres[c] += 1
                nb[str(len(ch.linear))] += 1
            out.setdefault(k, {})[v] = {
                "n": len(chs), "bigram_counts": dict(sorted(big.items())),
                "share_present": {c: round(x / len(chs), 4) for c, x in sorted(pres.items())},
                "n_blocks_hist": dict(sorted(nb.items(), key=lambda t: int(t[0])))}
    return out


def build_prior(chains: list[Chain], costs: dict[str, float] | None = None,
                contexts: list[dict] | None = None, context_keys=CONTEXT_KEYS) -> dict:
    """Aggregate category-level statistics from extracted chains. No model or preset names.
    With `contexts` (one metadata dict per chain) also stores the conditional sub-priors."""
    n = len(chains)
    big: Counter = Counter()
    seqs: Counter = Counter()
    tp_seqs: Counter = Counter()
    count_hist: dict = defaultdict(Counter)
    present: Counter = Counter()
    co: dict = defaultdict(Counter)
    pos_norm: dict = defaultdict(list)
    pos_idx: dict = defaultdict(Counter)
    pos_from_end: dict = defaultdict(Counter)
    before: Counter = Counter()
    both: Counter = Counter()
    nblocks: Counter = Counter()
    two_path = 0
    for ch in chains:
        lin = ch.linear
        if not lin:
            continue
        two_path += ch.two_path
        for e in ch.edges():
            big[f"{e[0]}>{e[1]}"] += 1
        (tp_seqs if ch.two_path else seqs)[">".join(lin) if not ch.two_path else ch.notation()] += 1
        nblocks[len(lin)] += 1
        cnt = Counter(lin)
        for c in CATEGORIES:
            count_hist[c][str(cnt.get(c, 0))] += 1
        cats = set(lin)
        for c in cats:
            present[c] += 1
        for x in cats:
            for y in cats:
                if x != y:
                    co[x][y] += 1
        m = len(lin)
        for i, c in enumerate(lin):
            pos_norm[c].append(i / (m - 1) if m > 1 else 0.5)
            pos_idx[c][str(i)] += 1
            pos_from_end[c][str(m - 1 - i)] += 1
        first: dict = {}
        for i, c in enumerate(lin):
            first.setdefault(c, i)
        for x, y in itertools.permutations(first, 2):
            if ch.comparable(first[x], first[y]):
                both[f"{x}|{y}"] += 1
                if first[x] < first[y]:
                    before[f"{x}|{y}"] += 1
    all_seq = seqs + tp_seqs
    prior = {
        "schema": SCHEMA,
        "n_presets": n,
        "n_two_path": two_path,
        "categories": list(CATEGORIES),
        "alpha": DEFAULT_ALPHA,
        "bigram_counts": dict(sorted(big.items())),
        "count_hist": {c: dict(h) for c, h in count_hist.items() if present[c]},
        "share_present": {c: round(present[c] / n, 4) for c in CATEGORIES if present[c]},
        "cooccurrence": {x: {y: round(k / present[x], 4) for y, k in sorted(ys.items())}
                         for x, ys in co.items()},
        "position": {
            c: {"mean_norm": round(statistics.mean(v), 4), "median_norm": round(statistics.median(v), 4),
                "n": len(v), "index_hist": dict(pos_idx[c]), "from_end_hist": dict(pos_from_end[c])}
            for c, v in pos_norm.items()},
        "order_before": {k: [before[k], both[k]] for k in sorted(both)},
        "n_blocks_hist": {str(k): v for k, v in sorted(nblocks.items())},
        "top_sequences": [[s, k] for s, k in seqs.most_common(20)],
        "top_two_path": [[s, k] for s, k in tp_seqs.most_common(10)],
        "distinct_sequences": len(all_seq),
    }
    if costs:
        prior["dsp_cost_median"] = costs
    if contexts is not None:
        cond = build_conditional(chains, contexts, context_keys)
        if cond:
            prior["conditional"] = cond
            prior["min_context_n"] = MIN_CONTEXT_N
            prior["kappa"] = DEFAULT_KAPPA
    return prior


# ----------------------------------------------------------------------------- query API

_CACHE: dict = {}


def load_prior(path: str | Path | None = None) -> dict:
    """Committed aggregate prior (cached per path). Raises FileNotFoundError if absent."""
    p = Path(path) if path else PRIOR_PATH
    key = str(p)
    if key not in _CACHE:
        _CACHE[key] = json.loads(p.read_text())
    return _CACHE[key]


def _trans(prior: dict, a: str, b: str) -> float:
    cats = prior["categories"]
    alpha = prior.get("alpha", DEFAULT_ALPHA)
    big = prior["bigram_counts"]
    nxt = [*cats, END]
    tot = sum(big.get(f"{a}>{x}", 0) for x in nxt)
    return (big.get(f"{a}>{b}", 0) + alpha) / (tot + alpha * len(nxt))


def context_priors(prior: dict, context: dict | None) -> list[dict]:
    """The sub-priors that apply to `context` ({"decade": "80s", "style": "metal"}): only those
    with at least `min_context_n` presets. Empty list means "use the global prior"."""
    cond = prior.get("conditional") or {}
    min_n = prior.get("min_context_n", MIN_CONTEXT_N)
    out = []
    for k, v in (context or {}).items():
        sub = cond.get(k, {}).get(norm_ctx(v)) if v else None
        if sub and sub["n"] >= min_n:
            out.append(sub)
    return out


def make_logtrans(prior: dict, context: dict | None = None):
    """Cached `lp(a, b)` = ln P(b | a). With a usable context each sub-prior contributes
    (count + kappa * P_global) / (total + kappa) and the sub-priors are averaged, so a sparse
    context leans on the global prior and an empty one is exactly the global prior."""
    subs = context_priors(prior, context)
    kappa = prior.get("kappa", DEFAULT_KAPPA)
    nxt = [*prior["categories"], END]
    cache: dict = {}

    def lp(a: str, b: str) -> float:
        key = (a, b)
        if key not in cache:
            pg = _trans(prior, a, b)
            if subs:
                ps = []
                for sub in subs:
                    big = sub["bigram_counts"]
                    tot = sum(big.get(f"{a}>{x}", 0) for x in nxt)
                    ps.append((big.get(f"{a}>{b}", 0) + kappa * pg) / (tot + kappa))
                pg = sum(ps) / len(ps)
            cache[key] = math.log(pg)
        return cache[key]
    return lp


def sequence_logprob(categories: list[str], prior: dict | None = None,
                     context: dict | None = None) -> float:
    """Natural-log probability of START + categories + END under the smoothed bigram model.
    Unseen but valid transitions get alpha mass, never zero. Unknown names count as `other`.
    `context` selects conditional sub-priors (see `make_logtrans`)."""
    prior = prior or load_prior()
    lp = make_logtrans(prior, context)
    seq = [START, *[c if c in prior["categories"] else "other" for c in categories], END]
    return sum(lp(a, b) for a, b in itertools.pairwise(seq))


def typical_position(category: str, prior: dict | None = None) -> float:
    """Median normalized position (0 = first block, 1 = last) of a category; 0.5 if never seen."""
    prior = prior or load_prior()
    s = prior["position"].get(category)
    return s["median_norm"] if s else 0.5


def top_structures(present, k: int = 5, prior: dict | None = None, max_blocks: int = 8,
                   dsp_budget: float | None = None, counts: dict | None = None,
                   beam: int = 400, context: dict | None = None
                   ) -> list[tuple[tuple[str, ...], float]]:
    """The k most likely ordered chains for the detected categories.

    `present` is a set of categories (all included) or a dict category -> probability in [0, 1].
    Categories with probability < 1 may be dropped: each candidate score adds log p for an included
    category and log(1-p) for an omitted one (optional pool: the 5 likeliest below 1 with p >= .05).
    `counts` ({"drive": 2}) repeats a category. A cab is glued directly after its amp/preamp.
    Chains over `max_blocks`, or over `dsp_budget` using the prior's category median costs, are
    refused. Returned probabilities are normalized over the candidate pool considered.
    `context` ({"decade": "80s", "style": "metal", "effect": ..., "group": ...}) conditions the
    order model on corpus metadata; contexts with fewer than `min_context_n` presets back off to
    the global prior, and a prior without a `conditional` block ignores it.
    """
    prior = prior or load_prior()
    probs = ({c: 1.0 for c in present} if not isinstance(present, dict)
             else {c: float(p) for c, p in present.items()})
    probs = {c: min(max(p, 0.0), 1.0) for c, p in probs.items() if c in prior["categories"] and p > 0}
    counts = counts or {}
    must = sorted(c for c, p in probs.items() if p >= 1.0)
    maybe = sorted((c for c, p in probs.items() if 0.05 <= p < 1.0), key=lambda c: -probs[c])[:5]
    costs = prior.get("dsp_cost_median") or {}
    eps = 1e-3
    lp = make_logtrans(prior, context)

    def size(cs):
        return sum(counts.get(c, 1) for c in cs)

    pool: list[tuple[float, tuple[str, ...]]] = []
    for r in range(len(maybe) + 1):
        for extra in itertools.combinations(maybe, r):
            chosen = [*must, *extra]
            if not chosen or size(chosen) > max_blocks:
                continue
            items = [c for c in chosen for _ in range(counts.get(c, 1))]
            if dsp_budget is not None and costs and sum(costs.get(c, 0.0) for c in items) > dsp_budget:
                continue
            inc = sum(math.log(max(probs[c], eps)) for c in extra)
            inc += sum(math.log(max(1 - probs[c], eps)) for c in maybe if c not in extra)
            for score, seq in _best_orders(items, prior, beam, k, lp):
                pool.append((score + inc, seq))
    if not pool:
        return []
    pool.sort(key=lambda t: -t[0])
    mx = pool[0][0]
    z = sum(math.exp(s - mx) for s, _ in pool)
    return [(seq, math.exp(s - mx) / z) for s, seq in pool[:k]]


def _best_orders(items: list[str], prior: dict, beam: int, k: int, lp=None):
    """Top orderings of the multiset `items` by bigram logprob, cab glued after its amp."""
    units = list(items)
    glue = None
    if "cab" in units:
        for host in ("amp", "preamp"):
            if host in units:
                glue = host
                break
    if glue:
        units.remove("cab")
        units[units.index(glue)] = glue + "+cab"

    def toks(u):
        return u.split("+")

    if lp is None:
        lp = make_logtrans(prior)

    states = [(0.0, START, (), tuple(units))]
    for _ in range(len(units)):
        nxt = []
        for sc, last, seq, rest in states:
            seen = set()
            for i, u in enumerate(rest):
                if u in seen:
                    continue
                seen.add(u)
                t = toks(u)
                s2 = sc + lp(last, t[0]) + sum(lp(x, y) for x, y in itertools.pairwise(t))
                nxt.append((s2, t[-1], (*seq, *t), rest[:i] + rest[i + 1:]))
        nxt.sort(key=lambda s: -s[0])
        states = nxt[:beam]
    out = [(sc + lp(last, END), seq) for sc, last, seq, _ in states]
    out.sort(key=lambda t: -t[0])
    return out[:max(k, 1) * 2]
