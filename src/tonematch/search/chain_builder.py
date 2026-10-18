"""Chain builder (T105): build a chain from the analysis, one block at a time.

Human direction 2026-10-17: model individual blocks, then build the chain from what the audio
shows. Pipeline (design note: docs/notes/chain-builder.md):

1. presence probabilities per category (`tonematch.metrics.presence`, injected as `presence_fn`);
2. the top-k ordered structures from the chain prior (`chain_prior.top_structures`, optional context);
3. per structure: a model shortlist per slot, then coordinate descent (one slot refined while the
   others stay fixed), then a joint refine;
4. successive halving across the structures;
5. the winner as a `Preset` (`assemble_preset`), validated, ready for `tonematch.preset.write_hlx`.

Rendering is block-serial (T045): one single-block template per slot, chained in order, every
instance's output trimmed by 16 samples (`TRIM`) before it feeds the next. `render_chain` is the
plain serial form; `ChainRenderer` batches many chains through `base.render_many` and caches every
prefix, so a coordinate move on slot i re-renders only slot i and the blocks after it.

Cost unit: one block render (one single-block template through the engine). Budget is a hard cap
on block renders; `BudgetExhausted` stops the stage that hit it and the best chain so far stands.

Scoring: with a known DI the weights are `KNOWN_DI_WEIGHTS` plus the Ruling 3 level guard (the
target's own active level is the reference). Without a DI the weights are a parameter and the
result is labelled provisional. Nothing here admits a loss.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from tonematch.engines.base import EngineError, render_many
from tonematch.search.cmaes import CMAES

SR = 48_000
TRIM = 16            # one Native frame of latency per instance (T045 correction 1)
BAD = 1e9
SEEDED_WIDTH = 0.04   # search width of a measured parameter, as a fraction of its range
MAX_BLOCKS = 8
KNOWN_DI_LEVEL_TOL_DB = 0.25   # same constants as scripts/match_song.py LevelGuardKit (Ruling 3)
KNOWN_DI_LEVEL_SLOPE = 0.08


class BudgetExhausted(RuntimeError):
    """The hard render cap would be exceeded; nothing was rendered for the refused request."""


# ------------------------------------------------------------------ blocks and the library

@dataclass(frozen=True)
class BlockSpec:
    """One block of a chain: category, model, and the parameter values that differ from the
    library start (all values, once a slot has been refined)."""
    category: str
    model: str
    params: tuple[tuple[str, float], ...] = ()

    def values(self) -> dict[str, float]:
        return dict(self.params)

    def with_values(self, values: dict[str, float]) -> BlockSpec:
        return BlockSpec(self.category, self.model, tuple(sorted((k, round(float(v), 5)) for k, v in values.items())))


class BlockLibrary(Protocol):
    """What the builder needs from the single-block library (native_library adapter, or a fake)."""

    def preset(self, model: str, values: dict[str, float]):
        """A single-block Preset (the library template) with `values` applied."""

    def shortlist(self, category: str, n: int) -> list[str]:
        """Up to n models of the category, best first (fingerprint match, then usage frequency)."""

    def free_params(self, model: str) -> dict[str, tuple[float, float, float]]:
        """name -> (lo, hi, start) for the parameters search may move."""


def render_chain(engine, blocks: Sequence, dry: np.ndarray, trim: int = TRIM) -> np.ndarray:
    """Block-serial render: `blocks` are single-block Presets in signal order. Each goes through
    `base.render_many` on the pool/engine, its output trimmed by `trim` samples (zero padded at
    the end to keep the length) before it feeds the next. Proposal for native-engine: implement the
    same contract against the real pool (see docs/notes/chain-builder.md)."""
    x = np.asarray(dry, np.float32)
    for p in blocks:
        x = _trim(render_many(engine, x, [p])[0], len(x), trim)
    return x


def _trim(y: np.ndarray, n: int, trim: int) -> np.ndarray:
    y = np.asarray(y, np.float32)
    y = y[trim:] if trim else y
    if len(y) < n:
        y = np.pad(y, (0, n - len(y)))
    return y[:n]


class ChainRenderer:
    """Renders many chains of BlockSpecs against one dry signal with a prefix cache.

    `spent` counts block renders; `cap` is the hard limit. A request that would pass the cap
    raises `BudgetExhausted` before any render. Failed blocks (EngineError) make every chain
    through them None."""

    def __init__(self, engine, library: BlockLibrary, dry: np.ndarray, cap: float, trim: int = TRIM,
                 cache_size: int = 96, validator: Callable | None = None):
        self.engine, self.lib, self.dry, self.cap, self.trim = engine, library, np.asarray(dry, np.float32), cap, trim
        self.cache_size, self.validator = cache_size, validator
        self.spent = 0
        self._cache: dict[tuple, np.ndarray | None] = {}

    @property
    def remaining(self) -> float:
        return self.cap - self.spent

    @staticmethod
    def _key(spec: BlockSpec) -> tuple:
        return (spec.model, spec.params)

    def cost_of(self, chains: Sequence[Sequence[BlockSpec]]) -> int:
        """Block renders a request would need (cached prefixes are free)."""
        seen = set()
        for ch in chains:
            for d in range(1, len(ch) + 1):
                k = tuple(map(self._key, ch[:d]))
                if k not in self._cache:
                    seen.add(k)
        return len(seen)

    def render_batch(self, chains: Sequence[Sequence[BlockSpec]]) -> list[np.ndarray | None]:
        chains = [list(c) for c in chains]
        need = self.cost_of(chains)
        if self.spent + need > self.cap:
            raise BudgetExhausted(f"{need} block renders needed, {self.remaining:.0f} left")
        pending: dict[tuple, tuple[tuple, BlockSpec]] = {}
        for ch in chains:
            for d in range(1, len(ch) + 1):
                k = tuple(map(self._key, ch[:d]))
                if k not in self._cache and k not in pending:
                    pending[k] = (k[:-1], ch[d - 1])
        by_depth: dict[int, dict[tuple, list[tuple[tuple, BlockSpec]]]] = {}
        for k, (parent, spec) in pending.items():
            by_depth.setdefault(len(k), {}).setdefault(parent, []).append((k, spec))
        for depth in sorted(by_depth):
            for parent, items in by_depth[depth].items():
                x = self.dry if not parent else self._cache.get(parent)
                if x is None:
                    for k, _ in items:
                        self._cache[k] = None
                    continue
                presets = [self.lib.preset(s.model, s.values()) for _, s in items]
                try:
                    ys = render_many(self.engine, x, presets)
                except EngineError:
                    ys = []
                    for p in presets:
                        try:
                            ys.append(render_many(self.engine, x, [p])[0])
                        except EngineError:
                            ys.append(None)
                self.spent += len(items)
                for (k, _), y in zip(items, ys, strict=True):
                    self._cache[k] = None if y is None else _trim(y, len(self.dry), self.trim)
        out = [self._cache.get(tuple(map(self._key, ch))) if ch else self.dry for ch in chains]
        self._evict(keep={tuple(map(self._key, ch)) for ch in chains})
        return out

    def _evict(self, keep: set) -> None:
        extra = len(self._cache) - self.cache_size
        for k in list(self._cache):
            if extra <= 0:
                break
            if k not in keep:
                del self._cache[k]
                extra -= 1


# ------------------------------------------------------------------ scoring

def active_level_db(x, frame: int = 2048, span_db: float = 40.0) -> float:
    """RMS level (dBFS) over active frames, as `LevelGuardKit.active_level_db`."""
    x = np.asarray(x, np.float64).ravel()
    n = len(x) // frame
    if n < 2:
        return float(20 * np.log10(max(float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0, 1e-12)))
    p = (x[: n * frame].reshape(n, frame) ** 2).mean(axis=1)
    act = p >= p.max() * 10 ** (-span_db / 10)
    return float(10 * np.log10(max(float(p[act].mean()), 1e-24)))


@dataclass
class ScoreMode:
    """Weights and level guard for one run. `label` goes into the report."""
    weights: dict | None
    known_di: bool
    label: str
    level_tol_db: float = KNOWN_DI_LEVEL_TOL_DB
    level_slope: float = KNOWN_DI_LEVEL_SLOPE
    ref_db: float | None = None     # song mode only: None means no level penalty

    @classmethod
    def known_di_mode(cls) -> ScoreMode:
        from tonematch.metrics import KNOWN_DI_WEIGHTS
        return cls(dict(KNOWN_DI_WEIGHTS), True, "known-DI, admitted KNOWN_DI_WEIGHTS + Ruling 3 level guard")

    @classmethod
    def song_mode(cls, weights: dict | str | None = "provisional", ref_db: float | None = None,
                  tol_db: float = 6.0, slope: float = 0.5) -> ScoreMode:
        if weights == "provisional":
            from tonematch.metrics import PROVISIONAL_WEIGHTS
            weights = dict(PROVISIONAL_WEIGHTS)
        return cls(weights, False, "song mode, PROVISIONAL weights (not admitted)", tol_db, slope, ref_db)


class ChainEvaluator:
    """Scores chains of BlockSpecs: render through the renderer, `kit.score`, level guard.
    Scores are memoised by chain."""

    def __init__(self, renderer: ChainRenderer, kit, target: np.ndarray, mode: ScoreMode):
        n = min(len(target), len(renderer.dry))
        self.rend, self.kit, self.target, self.mode = renderer, kit, np.asarray(target, np.float32)[:n], mode
        self._memo: dict[tuple, float] = {}
        self._ref = active_level_db(self.target) if mode.known_di else mode.ref_db

    @staticmethod
    def _ck(chain) -> tuple:
        return tuple((s.model, s.params) for s in chain)

    def score_many(self, chains: Sequence[Sequence[BlockSpec]]) -> list[float]:
        todo = [c for c in dict.fromkeys(map(self._ck, chains)) if c not in self._memo]
        if todo:
            by_key = {self._ck(c): c for c in chains}
            ys = self.rend.render_batch([by_key[k] for k in todo])
            for k, y in zip(todo, ys, strict=True):
                self._memo[k] = BAD if y is None else self._score(y)
        return [self._memo[self._ck(c)] for c in chains]

    def _score(self, y: np.ndarray) -> float:
        n = len(self.target)
        try:
            kw = {"weights": self.mode.weights} if self.mode.weights is not None else {}
            total = float(self.kit.score(self.target, y[:n], **kw).total)
        except Exception:  # noqa: BLE001 - a failed score is a bad candidate
            return BAD
        if not math.isfinite(total):
            return BAD
        if self._ref is not None:
            d = active_level_db(y[:n]) - self._ref
            total += self.mode.level_slope * max(0.0, abs(d) - self.mode.level_tol_db)
        return total


# ------------------------------------------------------------------ per-structure search

def _to_unit(v: float, lo: float, hi: float) -> float:
    return float(np.clip((v - lo) / max(hi - lo, 1e-12), 0, 1))


@dataclass
class StructureState:
    """One candidate structure and its search state (resumable across halving rungs)."""
    structure: tuple[str, ...]
    prior_p: float
    chain: list[BlockSpec] = field(default_factory=list)
    score: float = math.inf
    passes: int = 0
    spent_start: int = 0
    renders: int = 0                          # block renders attributed to this structure
    ranking: dict[int, list[tuple[float, str]]] = field(default_factory=dict)
    rungs: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dropped: bool = False
    invalid: list[str] = field(default_factory=list)


class StructureSearch:
    """Coordinate descent and joint refine for the structures, sharing one renderer and evaluator."""

    def __init__(self, ev: ChainEvaluator, lib: BlockLibrary, *, models_per_slot: int = 4, sigma0: float = 0.25,
                 seed: int = 0, seed_fn: Callable | None = None, estimates: dict | None = None,
                 rescreen: int = 2):
        self.ev, self.lib, self.rend = ev, lib, ev.rend
        self.models_per_slot, self.sigma0, self.seed, self.rescreen = models_per_slot, sigma0, seed, rescreen
        self.seed_fn, self.estimates = seed_fn, estimates or {}
        self.seeded: set[tuple[str, str]] = set()   # (model, param) set from a measurement: refined only locally

    # -- start
    def _start_spec(self, category: str, model: str) -> BlockSpec:
        vals = {k: s for k, (_, _, s) in self.lib.free_params(model).items()}
        if self.seed_fn is not None:
            got = {k: v for k, v in (self.seed_fn(category, self.estimates.get(category, {}), model) or {}).items()
                   if k in vals}
            vals.update(got)
            self.seeded |= {(model, k) for k in got}
        return BlockSpec(category, model).with_values(vals)

    def init(self, st: StructureState) -> bool:
        chain = []
        for cat in st.structure:
            models = self.lib.shortlist(cat, self.models_per_slot)
            if not models:
                st.invalid.append(f"no models for category {cat}")
                return False
            chain.append(self._start_spec(cat, models[0]))
        st.chain = chain
        return True

    # -- one step of the search for a share of renders
    def step(self, st: StructureState, cap: float) -> None:
        """Spend about `cap` block renders: a coordinate pass (70 %) then a joint refine (30 %)."""
        start = self.rend.spent
        try:
            if st.score == math.inf:
                st.score = self.ev.score_many([st.chain])[0]
            self._coordinate_pass(st, start, cap * 0.7)
            self._joint(st, start, cap)
        except BudgetExhausted:
            st.notes.append("render cap reached mid-step")
        finally:
            st.renders += self.rend.spent - start

    def _coordinate_pass(self, st: StructureState, start: int, cap: float) -> None:
        n = len(st.structure)
        for i in range(n):
            share = (start + cap - self.rend.spent) / (n - i)
            if share <= 0:
                break
            slot_start = self.rend.spent
            self._choose_model(st, i)
            self._refine(st, [i], slot_start, max(share - (self.rend.spent - slot_start), 0.0))
        st.passes += 1

    def _choose_model(self, st: StructureState, i: int) -> None:
        cat = st.structure[i]
        if st.passes == 0:
            models = self.lib.shortlist(cat, self.models_per_slot)
            alts = [m for m in models if m != st.chain[i].model]
        else:
            ranked = [m for _, m in sorted(st.ranking.get(i, [])) if m != st.chain[i].model]
            alts = ranked[:self.rescreen]
        if not alts:
            return
        cands = [self._start_spec(cat, m) for m in alts]
        if st.passes > 0:   # carry the tuned knobs over by name: models of a category share most knob meanings
            tuned = st.chain[i].values()
            cands = [c.with_values({**c.values(), **{k: v for k, v in tuned.items() if k in c.values()}}) for c in cands]
        scores = self.ev.score_many([[*st.chain[:i], c, *st.chain[i + 1:]] for c in cands])
        rank = st.ranking.setdefault(i, [])
        rank.append((st.score, st.chain[i].model))
        rank.extend((s, c.model) for s, c in zip(scores, cands, strict=True))
        j = int(np.argmin(scores))
        if scores[j] < st.score:
            st.chain[i], st.score = cands[j], scores[j]

    def _refine(self, st: StructureState, slots: list[int], start: int, cap: float, sigma0: float | None = None) -> None:
        """CMA-ES over the free parameters of `slots` with the other blocks fixed."""
        dims = [(i, k, *self.lib.free_params(st.chain[i].model)[k][:2]) for i in slots
                for k in sorted(self.lib.free_params(st.chain[i].model))]
        if not dims or cap <= 0:
            return
        cur = [st.chain[i].values() for i in range(len(st.chain))]
        u0 = np.array([_to_unit(cur[i].get(k, self.lib.free_params(st.chain[i].model)[k][2]), lo, hi)
                       for i, k, lo, hi in dims])
        # CMA-ES runs on offsets v in [0, 1] around the current point (v = 0.5): u = u0 + w (v - 0.5).
        # A parameter measured from the audio (delay time, say) gets a narrow w: the loss is a needle there.
        w = np.array([SEEDED_WIDTH if (st.chain[i].model, k) in self.seeded else 1.0 for i, k, _, _ in dims])
        es = CMAES(np.full(len(dims), 0.5), sigma0=sigma0 or self.sigma0, popsize=min(max(4 + int(3 * math.log(max(len(dims), 2))), 6), 16),
                   seed=self.seed + 17 * st.passes + len(st.chain))
        last_cost = 0
        while self.rend.spent - start + last_cost <= cap:
            xs = es.ask()
            chains = []
            for x in xs:
                u = np.clip(u0 + w * (es.clip(x) - 0.5), 0.0, 1.0)
                vals = [dict(v) for v in cur]
                for (i, k, lo, hi), ui in zip(dims, u, strict=True):
                    vals[i][k] = lo + ui * (hi - lo)
                chains.append([st.chain[i].with_values(vals[i]) if i in slots else st.chain[i]
                               for i in range(len(st.chain))])
            before = self.rend.spent
            f = self.ev.score_many(chains)
            last_cost = self.rend.spent - before
            es.tell(xs, f)
            j = int(np.argmin(f))
            if f[j] < st.score:
                st.score, st.chain = f[j], chains[j]
                cur = [st.chain[i].values() for i in range(len(st.chain))]
            if last_cost == 0 and es.sigma < 2e-3:
                break

    def _joint(self, st: StructureState, start: int, cap: float) -> None:
        self._refine(st, list(range(len(st.chain))), self.rend.spent, max(start + cap - self.rend.spent, 0.0),
                     sigma0=0.4 * self.sigma0)   # from a coordinate-descent point: a local move


# ------------------------------------------------------------------ top level

@dataclass
class ChainBudget:
    max_renders: int = 2000        # hard cap on block renders, all stages
    top_k: int = 6                 # structures from the prior
    eta: int = 2                   # halving: keep 1/eta per rung
    models_per_slot: int = 4
    clip_s: float = 10.0
    final_frac: float = 0.15       # reserved for the winner's last joint refine
    seed: int = 0


@dataclass
class ChainResult:
    chain: list[BlockSpec]
    score: float
    structure: tuple[str, ...]
    prior_p: float
    structures: list[dict]         # every structure considered: order, prior p, score, renders, dropped-at-rung
    renders: int
    budget: ChainBudget
    mode_label: str
    presence: dict
    preset: object | None = None
    notes: list[str] = field(default_factory=list)


def rung_sizes(k: int, eta: int) -> list[int]:
    sizes = [max(k, 1)]
    while sizes[-1] > 1:
        sizes.append(max(1, math.ceil(sizes[-1] / eta)))
    return sizes


def halve_structures(search: StructureSearch, states: list[StructureState], budget: ChainBudget,
                     total_cap: int) -> list[str]:
    """Successive halving over `states` (alive ones). Each rung splits its share equally among the
    survivors, steps each, keeps the best 1/eta. Returns notes. The renderer's cap is the real limit."""
    live = [s for s in states if not s.dropped]
    sizes = rung_sizes(len(live), budget.eta)
    pool = total_cap * (1 - budget.final_frac)
    per_rung = pool / len(sizes)
    notes: list[str] = []
    for ri, size in enumerate(sizes):
        live = sorted(live, key=lambda s: s.score)[:size] if ri else live
        share = per_rung / max(len(live), 1)
        for s in live:
            search.step(s, share)
            s.rungs.append({"rung": ri, "live": len(live), "share": round(share, 1), "score": s.score})
        if search.rend.remaining <= 0:
            notes.append(f"cap reached in rung {ri}")
            break
        if ri + 1 < len(sizes):
            ordered = sorted(live, key=lambda s: s.score)
            for s in ordered[sizes[ri + 1]:]:
                s.dropped = True
                s.notes.append(f"dropped after rung {ri}")
            live = ordered[:sizes[ri + 1]]
    return notes


def build_chain(target: np.ndarray, dry: np.ndarray | None, engine, library: BlockLibrary, kit, *,
                budget: ChainBudget | None = None, mode: ScoreMode | None = None,
                presence_fn: Callable | None = None, probs: dict | None = None, estimates: dict | None = None,
                structures_fn: Callable | None = None, context: dict | None = None,
                validator: Callable | None = None, seed_fn: Callable | None = None,
                renderer_factory: Callable | None = None) -> ChainResult:
    """Build a chain for `target`. `dry` is the DI the candidates render from (required: the
    chain is rendered, not guessed). With a known DI pass `mode=ScoreMode.known_di_mode()`.

    `presence_fn(target, dry) -> (probs, estimates)` (default: tonematch.metrics.presence.detect);
    pass `probs` to skip it. `structures_fn(probs, k, context=) -> [(categories, p)]` defaults to
    `chain_prior.top_structures`. `validator(chain) -> [violation text]` refuses chains the device
    would (default none; use `preset_validator`). `renderer_factory(engine, library, dry, cap)`
    replaces `ChainRenderer` (T121: `engines.native_chain.NativeChainRenderer` on real Native,
    whose renders are already latency-trimmed; see `build_chain_native`)."""
    budget = budget or ChainBudget()
    if dry is None:
        raise ValueError("a dry signal is required")
    mode = mode or ScoreMode.song_mode()
    n = min(len(target), len(dry), int(budget.clip_s * SR))
    target, dry = np.asarray(target, np.float32)[:n], np.asarray(dry, np.float32)[:n]
    notes: list[str] = []
    if probs is None:
        presence_fn = presence_fn or _default_presence
        probs, est = presence_fn(target, dry if mode.known_di else None)
        estimates = estimates or est
    if structures_fn is None:
        from tonematch.chain_prior import top_structures as structures_fn
    structs = structures_fn(probs, budget.top_k, context=context)
    rend = (renderer_factory or ChainRenderer)(engine, library, dry, budget.max_renders)
    ev = ChainEvaluator(rend, kit, target, mode)
    search = StructureSearch(ev, library, models_per_slot=budget.models_per_slot, seed=budget.seed,
                             seed_fn=seed_fn, estimates=estimates)
    states = []
    for cats, p in structs:
        st = StructureState(tuple(cats), float(p))
        if len(cats) > MAX_BLOCKS:
            st.invalid.append(f"{len(cats)} blocks > {MAX_BLOCKS}")
        elif search.init(st):
            bad = validator(st.chain) if validator else []
            if bad:
                st.invalid += [str(b) for b in bad]
        if st.invalid:
            st.dropped = True
            st.notes.append("refused before rendering: " + "; ".join(st.invalid))
        states.append(st)
    live = [s for s in states if not s.dropped]
    if not live:
        raise ValueError("no valid structure: " + "; ".join(n_ for s in states for n_ in s.invalid))
    notes += halve_structures(search, states, budget, budget.max_renders)
    best = min((s for s in states if not s.dropped), key=lambda s: s.score)
    if rend.remaining > 0:   # the winner's last joint refine
        try:
            search._joint(best, rend.spent, rend.remaining)
        except BudgetExhausted:
            pass
    if validator and validator(best.chain):
        notes.append("final chain fails validation: " + "; ".join(map(str, validator(best.chain))))
    summary = [{"structure": list(s.structure), "prior_p": s.prior_p, "score": None if s.score == math.inf else s.score,
                "renders": s.renders, "dropped": s.dropped, "rungs": s.rungs, "notes": s.notes} for s in states]
    return ChainResult(best.chain, best.score, best.structure, best.prior_p, summary, rend.spent, budget,
                       mode.label, dict(probs), None, notes)


def _default_presence(target, di):
    from tonematch.metrics.presence import detect
    rep = detect(target, di)
    return rep.probabilities(), {c: p.estimate for c, p in rep.categories.items()}


# ------------------------------------------------------------------ the preset

def assemble_preset(chain: Sequence[BlockSpec], library: BlockLibrary, name: str = "TONEMATCH chain"):
    """The chain as one `Preset` for `tonematch.preset.write_hlx`: the single-block templates'
    slots in order, amp linked to the cab that follows it. Cabs have no @position (file format);
    other blocks are numbered in signal order. Respect the 8-block limit before calling."""
    if len(chain) > MAX_BLOCKS:
        raise ValueError(f"{len(chain)} blocks > {MAX_BLOCKS}")
    base = None
    slots, pos, ncab, last_amp = [], 0, 0, None
    for b in chain:
        p = library.preset(b.model, b.values())
        base = base or p
        s = copy.deepcopy(p.slots[0])
        s.enabled = True
        if s.category == "cab":
            s.key, s.position = f"cab{ncab}", None
            ncab += 1
            if last_amp is not None:
                last_amp.extra["@cab"] = s.key
                last_amp = None
        else:
            s.key, s.position = f"block{pos}", pos
            pos += 1
            if s.category == "amp":
                last_amp = s
        s.extra["@path"] = 0
        slots.append(s)
    out = copy.deepcopy(base)
    out.name, out.slots = name[:16], slots
    return out


def preset_validator(library: BlockLibrary, catalog: dict | None, costs: dict | None = None,
                     engine: str | None = None) -> Callable:
    """`validator(chain)` for `build_chain`: assemble, call `tonematch.preset.validate` (block
    limit, models, parameter ranges, DSP budget) and return the failures."""
    from tonematch.preset import validate

    def check(chain):
        try:
            p = assemble_preset(chain, library)
        except ValueError as e:
            return [str(e)]
        return [str(v) for v in validate(p, catalog, costs, engine=engine) if v.severity == "fail"]
    return check
