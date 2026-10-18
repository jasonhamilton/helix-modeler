"""The chain builder (T105) on real Helix Native (T121): a block library adapter and block-serial rendering.

Two pieces, both consumed by ``tonematch.search.chain_builder``:

``NativeBlockLibrary`` implements the chain builder's ``BlockLibrary`` over the single-block
library (``native_library``, T048):

- ``preset(model, values)`` is ``native_library.block_preset(model)`` with ``values`` written in.
  A CC-driven value inside its template's CC range is snapped to the 128-step CC grid
  (``native.cc_value``): that is what Native plays, so the assembled ``.hlx`` carries exactly the
  rendered value. ``snap=False`` keeps values as given.
- ``shortlist(category, n)`` ranks the category's models by fingerprint match against the
  target's measured estimates (``estimates``, from ``metrics.presence``), then by corpus usage
  (``reports/local/preset-corpus/model_usage.json``, local). Left out: models with no library
  template, time-variant models, unverifiable models (time-variant and FX Loop / Send / Return,
  ``native_library.unverifiable_models``), stereo-only models (catalog ``mono`` false: the host
  renders mono in, folded mono out, so they cannot be serialised, T045) and the dual cab pan
  twins (``...WithPan``); also IR blocks unless ``allow_ir`` (their sound is the user's IR in
  that slot, not the model). ``excluded`` lists each with its reason.
- ``free_params(model)`` is every CC-verified continuous param of the model's template over the
  template's own CC range (the grid ``set_params`` scales by), start at the library value, minus
  ``param_policy`` frozen params, ``native_library.CC_UNRELIABLE`` and ``exclude_params`` (default
  ``FIXED_PARAMS``, the same set ``scripts/match_song.py`` never frees).

Block-serial rendering (T045: one single-block instance per block, chained, equal to the chain in
one instance within -60 dB above 20 Hz). T045 trimmed 16 samples (one Native frame) from every raw
instance output. ``NativeEngine.render`` already removes exactly that: it trims its measured
pass-through latency (16 samples, ``latency()``), per the engine contract. So the trim applied here
is ``trim - already_trimmed``: 0 on Native, 16 on a fake host that returns raw, late output (the
chain builder's test fakes). Applying the chain builder's ``ChainRenderer`` default trim on top of
a contract engine would shift every block 16 samples early; use ``NativeChainRenderer`` (or
``build_chain_native``) with a real engine.

- ``render_chain(pool, presets, dry, trim=16)`` renders one chain serially (same contract as
  ``chain_builder.render_chain``: single-block presets in signal order, output ``len(dry)``).
- ``NativeChainRenderer`` is the chain builder's prefix-cached ``ChainRenderer`` with one change:
  at each depth every pending block, whatever its input, goes to the engine in one call. On a
  ``NativeRenderPool`` that is one ``render_batch`` (jobs carry their own input; the pool groups
  them by template so each host loads a state once per template); on an engine it is one
  ``base.render_many`` per distinct input (same-input variants together). Budget accounting,
  the prefix cache and its keys are the chain builder's.

Intermediate audio stays in this process's memory (the prefix cache), never on disk. It is not
kept inside the host process: every block is a different template, so a host-side chain would
still pay a state load per block, and it would bypass ``NativeEngine.render``'s per-render checks
(sentinel count, NaN and silence checks, memory watchdog). Moving a 10 s clip over the pipe is
about 2 MB.

The plugin host (pedalboard, GPLv3) lives in a child process (``native_host``) and is never
imported here; this module is plain Python + numpy.
"""
from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

from .base import EngineError, render_many
from .native import CC_STEPS, cc_norm, cc_value

TRIM = 16   # one Native frame of latency per instance (T045 correction 1)
_ROOT = Path(__file__).resolve().parents[3]
USAGE_PATH = _ROOT / "reports" / "local" / "preset-corpus" / "model_usage.json"
# Never freed, as in scripts/match_song.py (FIXED_PARAMS): hum and ripple, bias, offsets, stereo
# spread and block output levels. Pass ``exclude_params=()`` to free Level.
FIXED_PARAMS = frozenset({"Hum", "Ripple", "Bias", "BiasX", "Offset", "Spread", "Level"})
NO_MATCH_BUCKET = 1   # a model with no fingerprint distance ranks between close and far matches


# ------------------------------------------------------------------ fingerprint match

def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _span_distance(x: float, vals: list[float]) -> float | None:
    """Octaves from ``x`` to the span of ``vals`` (0 inside it); None without values."""
    vals = [float(v) for v in vals if _finite(v) and float(v) > 0]
    if not vals or not _finite(x) or x <= 0:
        return None
    lo, hi = min(vals), max(vals)
    if lo <= x <= hi:
        return 0.0
    return float(abs(math.log2(x / (lo if x < lo else hi))))


MOD_KINDS = {"tremolo": {"amplitude", "both"}, "chorus": {"pitch", "both"}, "vibrato": {"pitch", "both"},
             "flanger": {"pitch", "both"}, "phaser": {"amplitude", "pitch", "both"}}


def fingerprint_distance(category: str, fp: dict | None, estimate: dict | None) -> float | None:
    """How far a model's block fingerprints (``native_library.fingerprint_index`` entry) are
    from the target's measured estimate for its category, in octaves (0 = inside the span the
    fingerprint settings cover). None when nothing comparable is measured.

    Compared: delay time (``time_s`` against the settings' ``delay_ms``), reverb decay
    (``rt60_s``) and modulation (``subtype`` against ``mod_kind``, then ``rate_hz`` against
    ``mod_rate_hz``). Gain, EQ, cab and comp estimates have no calibrated mapping onto the
    fingerprint summaries yet (tone-metrics), so those categories rank by usage alone."""
    if not fp or not estimate:
        return None
    summ = [s.get("summary") or {} for s in fp.get("settings", [])]
    if category == "delay":
        t = estimate.get("time_s")
        return _span_distance(float(t) * 1000.0, [s.get("delay_ms") for s in summ]) if _finite(t) else None
    if category == "reverb":
        t = estimate.get("rt60_s")
        return _span_distance(float(t), [s.get("rt60_s") for s in summ]) if _finite(t) else None
    if category == "mod":
        want = MOD_KINDS.get(str(estimate.get("subtype") or ""))
        kinds = {s.get("mod_kind") for s in summ}
        if want and not kinds & want:
            return 2.0
        r = estimate.get("rate_hz")
        if _finite(r):
            ok = [s.get("mod_rate_hz") for s in summ if not want or s.get("mod_kind") in want]
            d = _span_distance(float(r), ok)
            if d is not None:
                return d
        return 0.0 if want else None
    return None


def bucket(d: float | None) -> int:
    """0: within a quarter octave; 1: within an octave, or not comparable; 2: further."""
    if d is None:
        return NO_MATCH_BUCKET
    return 0 if d <= 0.25 else 1 if d <= 1.0 else 2


def load_usage(path: Path | str | None = USAGE_PATH) -> dict[str, int]:
    """model id -> preset count in the local corpus (``corpus_report.py``); {} when absent."""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):
        return {}
    out: dict[str, int] = {}
    for cat in d.values() if isinstance(d, dict) else []:
        for row in (cat or {}).get("ranked", []) if isinstance(cat, dict) else []:
            if row and isinstance(row[0], str):
                out[row[0]] = out.get(row[0], 0) + int(row[1])
    return out


# ------------------------------------------------------------------ the library adapter

class NativeBlockLibrary:
    """``chain_builder.BlockLibrary`` over the Native single-block library (module docstring).

    Everything is read lazily from the user's local caches; tests inject ``catalog``,
    ``templates`` (model -> ChainTemplate-like with ``map``, ``ranges``, ``verified``),
    ``preset_fn`` (model -> single-block Preset), ``usage``, ``fingerprints``, ``time_variant``,
    ``unverifiable`` and ``frozen``. ``estimates`` (category -> estimate dict, as
    ``metrics.presence`` gives) can be set at any time; it only changes ``shortlist``."""

    def __init__(self, version: str | None = None, *, catalog: dict | None = None, templates: dict | None = None,
                 preset_fn: Callable | None = None, usage: dict[str, int] | None = None,
                 fingerprints: dict | None = None, estimates: dict | None = None,
                 time_variant: set | None = None, unverifiable: dict | None = None,
                 frozen: dict | None = None, cc_unreliable: dict | None = None,
                 exclude_params: Sequence[str] = FIXED_PARAMS, snap: bool = True,
                 match_fn: Callable = fingerprint_distance, usage_path: Path | str | None = USAGE_PATH,
                 allow_ir: bool = False):
        from . import native_library as nl

        self.nl = nl
        self.version = version or nl.VERSION
        self._catalog, self._templates, self._preset_fn = catalog, templates, preset_fn
        self._usage, self._fps = usage, fingerprints
        self._tv, self._unver, self._frozen = time_variant, unverifiable, frozen
        self.cc_unreliable = nl.CC_UNRELIABLE if cc_unreliable is None else cc_unreliable
        self.exclude_params = frozenset(exclude_params)
        self.snap, self.match_fn, self.usage_path, self.allow_ir = snap, match_fn, usage_path, allow_ir
        self.estimates: dict = dict(estimates or {})
        self._base = None
        self._presets: dict[str, object] = {}
        self._free: dict[str, dict[str, tuple[float, float, float]]] = {}
        self._by_cat: dict[str, list[str]] | None = None
        self.excluded: dict[str, str] = {}

    # -- lazily loaded sources
    @property
    def catalog(self) -> dict:
        if self._catalog is None:
            from tonematch.catalog import load_catalog

            self._catalog = load_catalog()
        return self._catalog

    @property
    def templates(self) -> dict:
        if self._templates is None:
            self._templates = self.nl.library(self.version)
        return self._templates

    @property
    def usage(self) -> dict[str, int]:
        if self._usage is None:
            self._usage = load_usage(self.usage_path)
        return self._usage

    @property
    def fingerprints(self) -> dict:
        if self._fps is None:
            self._fps = self.nl.fingerprint_index(self.version).get("models", {})
        return self._fps

    @property
    def frozen(self) -> dict:
        if self._frozen is None:
            self._frozen = self.nl.frozen_values()
        return self._frozen

    def _time_variant(self) -> set:
        if self._tv is None:
            self._tv = set(self.nl.time_variant_models(self.version))
        return self._tv

    def _unverifiable(self) -> dict:
        if self._unver is None:
            self._unver = dict(self.nl.unverifiable_models(self.version))
        return self._unver

    # -- models
    def category_of(self, model: str) -> str:
        from tonematch.chain_prior import categorize

        return categorize(model, self.catalog)

    def exclusion(self, model: str) -> str | None:
        """Why ``model`` is never shortlisted, or None."""
        m = self.catalog.get("models", {}).get(model) or {}
        if self.nl.is_routing(model):
            return "routing block or dual cab pan twin (not a block alone)"
        if model in self._time_variant():
            return "time-variant (renders do not repeat)"
        if model in self._unverifiable():
            return "unverifiable on Native: " + self._unverifiable()[model]
        if m.get("mono") is False:
            return "stereo-only (no mono form; block-serial host path is mono)"
        if "ImpulseResponse" in model and not self.allow_ir:
            return "IR block: its sound is whatever IR the user has in that slot, not the model"
        if model not in self.templates:
            return "no library template captured"
        return None

    def models(self, category: str) -> list[str]:
        """Every usable model of ``category`` (unranked, sorted by id)."""
        if self._by_cat is None:
            by: dict[str, list[str]] = {}
            for mid in sorted(self.templates):
                why = self.exclusion(mid)
                if why:
                    self.excluded[mid] = why
                    continue
                by.setdefault(self.category_of(mid), []).append(mid)
            for mid in sorted(self.catalog.get("models", {})):
                if mid not in self.templates and (self.catalog["models"][mid] or {}).get("stomp") and mid not in self.excluded:
                    self.excluded[mid] = self.exclusion(mid) or "no library template captured"
            self._by_cat = by
        return list(self._by_cat.get(category, []))

    def ranking(self, category: str) -> list[tuple[str, float | None, int]]:
        """(model, fingerprint distance, corpus count), best first."""
        est = (self.estimates or {}).get(category) or {}
        rows = []
        for mid in self.models(category):
            d = self.match_fn(category, self.fingerprints.get(mid), est) if est else None
            rows.append((mid, d, int(self.usage.get(mid, 0))))
        rows.sort(key=lambda r: (bucket(r[1]), -r[2], r[1] if r[1] is not None else math.inf, r[0]))
        return rows

    def shortlist(self, category: str, n: int) -> list[str]:
        return [m for m, _, _ in self.ranking(category)[: max(int(n), 0)]]

    # -- parameters
    def _block_preset(self, model: str):
        if model not in self._presets:
            if self._preset_fn is not None:
                self._presets[model] = self._preset_fn(model)
            else:
                if self._base is None:
                    self._base = self.nl.base_preset()
                self._presets[model] = self.nl.block_preset(model, self.catalog, self._base, self.frozen)
        return self._presets[model]

    def _template(self, model: str):
        t = self.templates.get(model)
        if t is None:
            raise KeyError(f"{model}: no library template ({self.version})")
        return t

    def free_params(self, model: str) -> dict[str, tuple[float, float, float]]:
        if model in self._free:
            return dict(self._free[model])
        tpl, p = self._template(model), self._block_preset(model)
        s = p.slots[0]
        cat = s.category
        cparams = ((self.catalog.get("models", {}).get(model) or {}).get("params") or {})
        out: dict[str, tuple[float, float, float]] = {}
        for name, v in (s.params or {}).items():
            ref = f"0:{name}"
            if name.startswith("@") or name in self.exclude_params or (cat, name) in self.frozen:
                continue
            if (model, name) in self.cc_unreliable or ref not in tpl.map or ref not in set(tpl.verified):
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            pd = cparams.get(name)
            if pd is not None and pd.get("kind", "continuous") != "continuous":
                continue
            r = (tpl.ranges or {}).get(ref)
            if r is None:
                continue
            lo, hi = float(r[0]), float(r[1])
            if hi <= lo:
                continue
            out[name] = (lo, hi, float(np.clip(float(v), lo, hi)))
        self._free[model] = out
        return dict(out)

    def snap_value(self, model: str, name: str, v: float) -> float:
        """``v`` on the CC grid of ``model``'s template when it is a CC-driven value inside the
        template's CC range; else ``v``."""
        tpl = self._template(model)
        ref = f"0:{name}"
        r = (tpl.ranges or {}).get(ref)
        if r is None or ref not in set(tpl.verified) or isinstance(v, bool) or not isinstance(v, (int, float)):
            return v
        lo, hi = float(r[0]), float(r[1])
        span = abs(hi - lo) or 1.0
        if not lo - 1e-6 * span <= float(v) <= hi + 1e-6 * span:
            return v   # outside the grid: the engine picks a wider template (T098)
        return cc_value(lo, hi, round(cc_norm(float(v), lo, hi) * CC_STEPS))

    def preset(self, model: str, values: dict[str, float]):
        p = copy.deepcopy(self._block_preset(model))
        s = p.slots[0]
        for name, v in (values or {}).items():
            if name not in (s.params or {}):
                raise KeyError(f"{model} has no parameter {name!r}")
            s.params[name] = self.snap_value(model, name, v) if self.snap else v
        return p


# ------------------------------------------------------------------ block-serial rendering

def _is_pool(engine) -> bool:
    return hasattr(engine, "render_batch") and hasattr(engine, "engines")


def engine_trim(engine) -> int:
    """Samples the engine already trims from each render: a Native engine's (or pool's first
    engine's) measured pass-through latency; 0 for an engine without ``latency()`` (fakes that
    return raw output). Call after a render: ``NativeEngine.latency()`` is cached by then."""
    eng = engine.engines[0] if _is_pool(engine) and engine.engines else engine
    lat = getattr(eng, "latency", None)
    if not callable(lat):
        return 0
    try:
        return int(lat())
    except (EngineError, TypeError, ValueError):
        return 0


def shift(y: np.ndarray, n: int, s: int) -> np.ndarray:
    """``y`` advanced by ``s`` samples (delayed when negative), zero padded, length ``n``."""
    y = np.asarray(y, np.float32)
    if s > 0:
        y = y[s:]
    elif s < 0:
        y = np.concatenate([np.zeros(-s, np.float32), y])
    if len(y) < n:
        y = np.pad(y, (0, n - len(y)))
    return y[:n]


def render_jobs(engine, jobs: Sequence[tuple[np.ndarray, object]]) -> tuple[list[np.ndarray | None], dict[int, str]]:
    """Render each (input, single-block preset) job. A ``NativeRenderPool`` takes them all in one
    ``render_batch``; any other engine gets one ``base.render_many`` per distinct input (by
    identity), falling back to one call per preset when the batch fails. Returns (renders, errors):
    a failed job is None with its message in ``errors[index]``."""
    out: list[np.ndarray | None] = [None] * len(jobs)
    errs: dict[int, str] = {}
    if not jobs:
        return out, errs
    if _is_pool(engine):
        from .native import RenderJob

        ys = engine.render_batch([RenderJob(dry=np.asarray(x, np.float32), preset=p) for x, p in jobs], on_error="none")
        for i, e in (getattr(engine, "last_errors", None) or {}).items():
            errs[int(i)] = str(e)
        return list(ys), errs
    groups: dict[int, list[int]] = {}
    for i, (x, _) in enumerate(jobs):
        groups.setdefault(id(x), []).append(i)
    for idx in groups.values():
        x = np.asarray(jobs[idx[0]][0], np.float32)
        try:
            ys = render_many(engine, x, [jobs[i][1] for i in idx])
            for i, y in zip(idx, ys, strict=True):
                out[i] = y
        except EngineError:
            for i in idx:
                try:
                    out[i] = render_many(engine, x, [jobs[i][1]])[0]
                except EngineError as e:
                    errs[i] = str(e)
    return out, errs


def render_chain(pool, presets: Sequence, dry: np.ndarray, trim: int = TRIM,
                 already_trimmed: int | None = None) -> np.ndarray:
    """Block-serial render of one chain on Native (contract of ``chain_builder.render_chain``):
    ``presets`` are single-block presets in signal order; each block renders the previous
    block's output; the per-instance latency ``trim`` is removed (minus what the engine already
    trims, ``engine_trim``); the result has ``len(dry)`` samples. Raises ``EngineError`` when a
    block fails."""
    x = np.asarray(dry, np.float32)
    n = len(x)
    for p in presets:
        ys, errs = render_jobs(pool, [(x, p)])
        if ys[0] is None:
            raise EngineError(f"block {getattr(p.slots[0], 'model_id', '?')}: {errs.get(0, 'render failed')}")
        done = engine_trim(pool) if already_trimmed is None else already_trimmed
        x = shift(ys[0], n, trim - done)
    return x


def _chain_renderer_base():
    from tonematch.search.chain_builder import ChainRenderer

    return ChainRenderer


class NativeChainRenderer(_chain_renderer_base()):
    """``chain_builder.ChainRenderer`` with engine-aware trim and one engine call per depth
    (module docstring). ``already_trimmed`` overrides ``engine_trim``. ``calls`` counts engine
    calls, ``errors`` keeps the last failures (model, message)."""

    def __init__(self, engine, library, dry, cap, trim: int = TRIM, cache_size: int = 96,
                 validator=None, already_trimmed: int | None = None):
        super().__init__(engine, library, dry, cap, trim=trim, cache_size=cache_size, validator=validator)
        self.already_trimmed = already_trimmed
        self.calls = 0
        self.errors: list[tuple[str, str]] = []

    def _done(self) -> int:
        if self.already_trimmed is None:
            self.already_trimmed = engine_trim(self.engine)
        return self.already_trimmed

    def render_batch(self, chains):
        from tonematch.search.chain_builder import BudgetExhausted

        chains = [list(c) for c in chains]
        need = self.cost_of(chains)
        if self.spent + need > self.cap:
            raise BudgetExhausted(f"{need} block renders needed, {self.remaining:.0f} left")
        pending: dict[tuple, object] = {}
        for ch in chains:
            for d in range(1, len(ch) + 1):
                k = tuple(map(self._key, ch[:d]))
                if k not in self._cache and k not in pending:
                    pending[k] = ch[d - 1]
        n = len(self.dry)
        for depth in sorted({len(k) for k in pending}):
            items = [(k, s) for k, s in pending.items() if len(k) == depth]
            jobs, keys = [], []
            for k, spec in items:
                x = self.dry if depth == 1 else self._cache.get(k[:-1])
                if x is None:
                    self._cache[k] = None
                    continue
                jobs.append((x, self.lib.preset(spec.model, spec.values())))
                keys.append((k, spec))
            if not jobs:
                continue
            ys, errs = render_jobs(self.engine, jobs)
            self.calls += 1
            self.spent += len(jobs)
            for i, msg in errs.items():
                self.errors = (self.errors + [(keys[i][1].model, msg)])[-20:]
            s = self.trim - self._done()
            for (k, _), y in zip(keys, ys, strict=True):
                self._cache[k] = None if y is None else shift(y, n, s)
        out = [self._cache.get(tuple(map(self._key, ch))) if ch else self.dry for ch in chains]
        self._evict(keep={tuple(map(self._key, ch)) for ch in chains})
        return out


# ------------------------------------------------------------------ top level

def build_chain_native(target: np.ndarray, dry: np.ndarray, engine, kit, *, library: NativeBlockLibrary | None = None,
                       validate: bool = True, estimates: dict | None = None, probs: dict | None = None,
                       presence_fn: Callable | None = None, **kw):
    """``chain_builder.build_chain`` on Native: the library adapter (its shortlists use the
    presence estimates), ``NativeChainRenderer`` on ``engine`` (a ``NativeRenderPool`` for
    parallel renders, or a ``NativeEngine``) and, with ``validate``, ``preset_validator`` with
    Native's refused models. Other keywords go to ``build_chain``."""
    from tonematch.search import chain_builder as cb

    library = library or NativeBlockLibrary()
    mode = kw.get("mode") or cb.ScoreMode.song_mode()
    kw["mode"] = mode
    if probs is None:
        budget = kw.get("budget") or cb.ChainBudget()
        n = min(len(target), len(dry), int(budget.clip_s * cb.SR))
        fn = presence_fn or cb._default_presence
        probs, est = fn(np.asarray(target, np.float32)[:n], np.asarray(dry, np.float32)[:n] if mode.known_di else None)
        estimates = estimates or est
    library.estimates = dict(estimates or {})
    if validate and "validator" not in kw:
        kw["validator"] = cb.preset_validator(library, library.catalog, engine="native")
    return cb.build_chain(target, dry, engine, library, kit, probs=probs, estimates=estimates,
                          renderer_factory=lambda e, lib, d, cap: NativeChainRenderer(e, lib, d, cap), **kw)
