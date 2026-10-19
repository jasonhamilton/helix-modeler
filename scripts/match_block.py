#!/usr/bin/env python3
"""Single-block match on Helix Native (T164). LOCAL ONLY: no song audio leaves the machine.

    .venv/bin/python scripts/match_block.py --target-stems cache/stems/<hash>/htdemucs_6s \\
      --target-window 28.3:38.3 --di data/songs/<song>/takes/<take>_dry.wav --di-json <take>_align.json \\
      --category drive --wall-min 25 --workers auto --out cache/matches/<name>

What it does: every searchable model of ONE category in the Native single-block library used by
Gate 2 (block-library-v1.2, `synthetic_v1.build_pools` without the pool subsample; time-variant
models dropped) is searched with the Gate 2 hybrid (`tonematch.search.hybrid`: broad space-filling
draws over all models, top-k narrowing, successive halving with CMA-ES; the gain specialist when it
applies). The pool is split into shards, one Native host per shard (the host scaler decides how
many run at once); each shard refines its own best models, the parent merges them and re-scores
the finalists on the full excerpt.

Score: the provisional, UNADMITTED no-DI loss of scripts/match_song.py (ltas_tilt,
crest_flatness_hnr, envelope, equal weights) plus its level guard. The DI of a play-along take is not
sample-aligned with the target, so frame-by-frame `mrstft_v2` is NOT used to search or rank; its
value (with `level_db`) is reported for the winner only, for reference.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import match_song as MS  # noqa: E402
from tonematch import cli_result

SR = MS.SR
LABEL = "unadmitted loss"
SEARCH_CLIP_S = 3.0
RESERVE_S = 300.0           # kept for the final check, the files and the report
KNOB_TOL_DEFAULT = 0.15      # T176: drive only; other categories keep the old behaviour unless asked
T164_PLANNED_RENDERS = 31_627   # cache/matches/Mother-0-68s-drive/shard*.log: 5996+6648+6407+6133+6443 planned renders (run stopped, no totals)
HYBRID_DEFAULT = {"f": 0.4, "k": 2, "scale": 0.9}     # Gate 2 attempt 3 (f 0.4, scale 0.9); k per shard


# --------------------------------------------------------------------------------------- pool

def category_pool(category: str, catalog=None) -> dict:
    """model -> free params for EVERY searchable model of `category` (no pool subsample)."""
    import synthetic_v1 as SV

    keep = SV.POOL_PER_CATEGORY
    SV.POOL_PER_CATEGORY = 10 ** 6
    try:
        return SV.build_pools(catalog=catalog, categories=[category])[category]
    finally:
        SV.POOL_PER_CATEGORY = keep


def make_blocks(pool: dict, category: str, catalog) -> dict:
    import synthetic_v1 as SV

    return {m: SV.Block(m, category, free, catalog) for m, free in pool.items()}


def shard_models(models: list[str], i: int, n: int) -> list[str]:
    """Round-robin shard `i` of `n` over the sorted model list (same split in every process)."""
    return [m for j, m in enumerate(sorted(models)) if j % n == i]


# --------------------------------------------------------------------------------------- section / kit

def build_target_section(args):
    """(section, window) for the target window and the DI cut the way match_song.user_di_clip does.
    With --target-wav and --dry-wav (T183, held-out checks) both files are used whole: no song stems, no mask."""
    if getattr(args, "target_wav", None):
        from tonematch.search import Section

        tgt, dry = MS._mono(Path(args.target_wav)), MS._mono(Path(args.dry_wav))
        n = min(len(tgt), len(dry))
        win = {"start_s": 0.0, "end_s": n / SR, "dur_s": n / SR}
        return Section(args.song, "block", tgt[:n].copy(), dry[:n].copy(), stems=None, mask=None), win
    song = MS.load_song(args.target_stems)
    total_s = len(song.guitar) / SR
    fc, fp = MS.frame_confidence(song.mask_tf, song.guitar)
    win = MS.manual_window(fc, fp, song.sections, *MS.parse_target(args.target_window), total_s)
    _, dry = MS.user_di_clip(args.di, args.di_json, win)
    return MS.build_section(song, win, fc, "block", dry, args.song), win


ALIGNED_WEIGHTS = {"mrstft_v2": 1.0, "level_db": 1e-7}      # the Gate 2 weights; only for a sample-aligned take
_ALIGNED = False


def _weights() -> dict:
    """The provisional no-DI weights, or mrstft_v2 + level_db when the take is declared sample-aligned (T178)."""
    return dict(ALIGNED_WEIGHTS) if _ALIGNED else MS._weights()


def make_kit(section, tol_db: float, slope: float):
    """match_song's kit: masked provisional loss + level guard (RMS reference = the target's own level)."""
    from tonematch.metrics import LossKit

    kit = MS.LevelGuardKit(MS.MaskedKit(LossKit(), section.mask), tol_db, slope)
    kit.ref_db = MS.LevelGuardKit.level_db(section.target)
    return kit


# --------------------------------------------------------------------------------------- search

def hybrid_rank(engine, blocks: dict, section, kit, *, renders: int, clip_s: float, seed: int, k: int,
                f: float, scale: float, specialists: tuple[str, ...] = (), level_free: bool = True,
                progress: ProgressWriter | None = None, log=print, knob_tol: float = 0.0) -> list[dict]:
    """The Gate 2 hybrid over `blocks`; one row per refined model (best first), then the unrefined by
    screen score. Each row: model, values (name -> value), screen, refined (or None)."""
    import synthetic_v1 as SV
    from tonematch.search import Budget
    from tonematch.search.hybrid import choose_models_hybrid
    from tonematch.search.measured_seed import load_calibration, measure, usage_counts, usage_rank

    weights = _weights()
    bud = Budget(max_renders=renders, clip_s=clip_s, seed=seed, weights=weights)
    cal = load_calibration(SV.SEED_CAL) if Path(SV.SEED_CAL).is_file() else {}
    try:
        meas = measure(section.target, section.dry)
    except Exception as e:  # noqa: BLE001 - seeds are optional
        log(f"measured seed unavailable ({type(e).__name__}: {e}); no seeds")
        meas = None
    ranges = {m: dict(b.rng) for m, b in blocks.items()}
    usage = usage_rank(list(blocks), usage_counts(SV.POOL_USAGE)) if Path(SV.POOL_USAGE).is_file() else None
    disp = None
    if specialists:
        from tonematch.search.family_adapters import build_specialists
        from tonematch.search.specialists import build_dispatch
        sps = build_specialists(specialists)
        disp = build_dispatch(specialists, section.target, section.dry, [(m, b.category) for m, b in blocks.items()],
                              cal=cal, ranges=ranges, specialists=sps)
    if progress is not None and hasattr(engine, "find"):
        progress.renders = lambda: getattr(engine, "renders", 0)
        broad = ProgressKit(getattr(kit, "inner", kit), engine, progress, guard=kit)
        kit = ProgressKit(kit, engine, progress)
        kit.inner = broad                      # choose_models_hybrid reads kit.inner as the level-free broad kit
    ch = choose_models_hybrid(section, engine, kit, SV.model_candidates(blocks), bud,
                              meas=meas if cal else None, cal=cal or None, ranges=ranges, f=f, k=k,
                              refine_scale=scale, broad_kit=getattr(kit, "inner", None) if level_free else None,
                              usage=usage, dispatch=disp, knob_tol=knob_tol)
    log(f"  renders used: {ch.renders:.0f} (ceiling {renders}), knob tolerance {knob_tol:g}")
    if progress is not None:
        progress.flush(force=True)
    for n in ch.notes:
        log(f"  note: {n}")
    rows = []
    for s in ch.ranked:
        b = blocks[s.model]
        if s.values is not None:
            vals = {n: float(x) for (_, n), x in zip(b.space.refs, s.values, strict=True)}
        else:
            vals = {n: float(s.start.get((0, n), d)) for (_, n), d in zip(b.space.refs, b.space.start, strict=True)}
        rows.append({"model": s.model, "values": vals, "screen": float(s.screen),
                     "refined": None if s.refined is None else float(s.refined)})
    return rows


def probe_seconds(engine, blocks: dict, section, kit, n: int = 3) -> float:
    """Seconds for one render+score on the (search) section."""
    b = next(iter(blocks.values()))
    engine.load(b.with_values({}))
    best = 1e9
    for _ in range(n):
        t = time.perf_counter()
        y = engine.render(section.dry)
        kit.score(section.target, y, stems=section.stems, weights=_weights())
        best = min(best, time.perf_counter() - t)
    return best


def final_check(engine, blocks: dict, rows: list[dict], section, kit, top: int, log=print) -> list[dict]:
    """Full-excerpt re-score (stems + mask) of the best `top` rows; values rounded as written to the .hlx."""
    w = _weights()
    raw = kit.inner
    out = []
    for r in rows[:top]:
        b = blocks[r["model"]]
        vals = {n: round(float(v), 6) for n, v in r["values"].items()}
        p = b.with_values(vals)
        engine.load(p)
        y = engine.render(section.dry)
        sc = kit.score(section.target, y, stems=section.stems, mask=section.mask, weights=w)
        plain = raw.score(section.target, y, stems=section.stems, mask=section.mask, weights=w)
        out.append({"model": r["model"], "values": vals, "loss": float(sc.total), "plain_loss": float(plain.total),
                    "terms": {k: float(v) for k, v in sc.terms.items() if isinstance(v, (int, float, np.floating))},
                    "level_db": MS.LevelGuardKit.level_db(y), "search_loss": r["refined"], "screen": r["screen"],
                    "preset": p, "y": y})
        log(f"final {r['model']}: {sc.total:.4f}")
    out.sort(key=lambda d: d["loss"])
    return out


def rank_by_afxrep(final: list[dict], ranked: list[dict]) -> list[dict]:
    """T185 option `--final-rank afxrep`: the final models in AFx-Rep order (nearest first). Each row keeps its provisional
    `loss`; `provisional_rank` records where the re-score had put it. A model AFx-Rep did not rank goes last, by its loss."""
    order = {r["model"]: i for i, r in enumerate(ranked)}
    prov = {d["model"]: i + 1 for i, d in enumerate(final)}
    out = sorted(final, key=lambda d: (order.get(d["model"], len(order)), d["loss"]))
    for d in out:
        d["provisional_rank"] = prov[d["model"]]
    return out


def reference_scores(engine, winner: dict, section, kit) -> dict:
    """Raw-DI baseline (provisional loss) and the winner's mrstft_v2 + level_db (reference only, unadmitted here)."""
    from tonematch.metrics import LossKit

    w = _weights()
    raw = kit.inner
    dry_loss = float(raw.score(section.target, section.dry, stems=section.stems, mask=section.mask, weights=w).total)
    ref = {"dry_loss": dry_loss, "dry_level_db": MS.LevelGuardKit.level_db(section.dry),
           "target_level_db": MS.LevelGuardKit.level_db(section.target)}
    try:
        sc = LossKit().score(section.target, winner["y"], weights={"mrstft_v2": 1.0, "level_db": 1e-7})
        ref["winner_mrstft_v2_level_db"] = float(sc.total)
        ref["winner_terms"] = {k: float(v) for k, v in sc.terms.items() if isinstance(v, (int, float, np.floating))}
        sd = LossKit().score(section.target, section.dry, weights={"mrstft_v2": 1.0, "level_db": 1e-7})
        ref["dry_mrstft_v2_level_db"] = float(sd.total)
    except Exception as e:  # noqa: BLE001 - reference only
        ref["mrstft_error"] = f"{type(e).__name__}: {e}"
    return ref


# --------------------------------------------------------------------------------------- live progress

class ProgressWriter:
    """Writes the best preset so far to `<dir>/current_best.{hlx,json}` whenever the objective improves
    (atomic: tmp file then rename; the .hlx first, so a reader that sees the json finds its .hlx). Safe across
    shard processes: a file lock guards the read-compare-write, and `seq` counts writes over all of them.
    Only losses measured on the same clip are comparable: shards share the search clip; the parent's final
    check (full excerpt) is written with stage "final" and always replaces the file."""

    def __init__(self, directory, blocks: dict, category: str, preset_name: str = "Drive match",
                 min_interval_s: float = 3.0, log=print, run_id: str = ""):
        self.dir, self.blocks, self.category = Path(directory), blocks, category
        self.name, self.min_interval, self.log, self.run_id = preset_name, min_interval_s, log, run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.best = float("inf")
        self.pending = None
        self._last = 0.0
        self.renders = lambda: 0

    def _current(self):
        f = self.dir / "current_best.json"
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return None

    def offer(self, loss: float, model: str, values: dict, stage: str = "search", force: bool = False) -> bool:
        """Record `loss` as this process's candidate; write when it beats the file (or `force`) and the throttle allows."""
        if not np.isfinite(loss) or model not in self.blocks:
            return False
        if loss < self.best or force:
            self.best = min(self.best, loss) if not force else loss
            self.pending = (loss, model, dict(values), stage)
        return self.flush(force)

    def flush(self, force: bool = False) -> bool:
        if self.pending is None:
            return False
        now = time.monotonic()
        if not force and now - self._last < self.min_interval:
            return False
        import fcntl
        from datetime import datetime, timezone

        from tonematch.preset import write_hlx

        loss, model, values, stage = self.pending
        with open(self.dir / ".lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            cur = self._current()
            if (cur is not None and not force and cur.get("run", "") == self.run_id and cur.get("stage") == stage
                    and cur["loss"] <= loss):                    # an older run's file never blocks a new run
                self.pending, self._last = None, now
                return False
            seq = (cur["seq"] if cur else 0) + 1
            b = self.blocks[model]
            vals = {n: float(values.get(n, b.preset.slots[0].params.get(n))) for n, *_ in b.free}
            preset = b.with_values(vals)
            preset.name = f"{self.name} s{seq}"[:32]
            tmp = self.dir / "current_best.tmp.hlx"
            write_hlx(preset, tmp)
            os.replace(tmp, self.dir / "current_best.hlx")
            doc = {"seq": seq, "time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "loss": float(loss),
                   "model": model, "category": self.category, "values": vals, "renders": int(self.renders()),
                   "stage": stage, "run": self.run_id, "label": LABEL}
            tj = self.dir / "current_best.tmp.json"
            tj.write_text(json.dumps(doc, indent=1))
            os.replace(tj, self.dir / "current_best.json")
            fcntl.flock(lk, fcntl.LOCK_UN)
        self.pending, self._last = None, now
        self.log(f"  progress #{seq}: {model} {loss:.4f} ({stage})")
        return True


class ProgressEngine(MS.TimedEngine):
    """TimedEngine that remembers which preset and parameter changes produced each rendered array, so a
    wrapped kit can tell which candidate it is scoring (the kit only sees audio)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.origin: dict[int, tuple] = {}

    def render(self, dry):
        y = super().render(dry)
        if len(self.origin) > 512:
            self.origin.clear()
        self.origin[id(y)] = (y, self._preset, dict(self._applied))   # y kept alive so the id stays unique
        return y

    def find(self, audio):
        """(preset, applied changes) behind `audio` or a view of it, else None."""
        a = audio
        while a is not None:
            hit = self.origin.get(id(a))
            if hit is not None and hit[0] is a:
                return hit[1], hit[2]
            a = getattr(a, "base", None)
        return None


class ProgressKit:
    """Delegates to `inner`; reports each score to a ProgressWriter. `guard` (a LevelGuardKit) supplies the
    level penalty for an inner kit that has none (the broad stage scores without it)."""

    def __init__(self, inner, engine, writer: ProgressWriter, guard=None):
        self.inner, self.engine, self.writer, self.guard = inner, engine, writer, guard

    def score(self, target, candidate, **kw):
        sc = self.inner.score(target, candidate, **kw)
        try:
            hit = self.engine.find(candidate)
            if hit is not None:
                preset, applied = hit
                slot = preset.slots[0]
                vals = {**slot.params, **{k[1]: v for k, v in applied.items() if k[0] == 0}}
                total = float(sc.total)
                g = self.guard
                if g is not None and g.ref_db is not None:
                    total += g.slope * max(0.0, abs(MS.LevelGuardKit.level_db(candidate) - g.ref_db) - g.tol_db)
                self.writer.offer(total, slot.model_id, vals)
        except Exception as e:  # noqa: BLE001 - progress must never break the search
            self.writer.log(f"  progress hook skipped: {type(e).__name__}: {e}")
        return sc


# --------------------------------------------------------------------------------------- shards

def make_engine(preroll: int, log=print):
    """Native engine on the single-block library templates (what Gate 2 and block_preset load against)."""
    from tonematch.engines import native_library as NL
    from tonematch.engines.native import NativeEngine

    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    hv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and hv.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(hv)
    return ProgressEngine(NativeEngine(preroll=preroll, templates_dir=NL.library_templates()), log, rss_limit_mb=9000.0)


def run_shard(args) -> int:
    """One worker: its slice of the pool, own Native host, deadline-driven budget; writes rows to JSON."""
    i, n = (int(x) for x in args.shard.split("/"))
    args.knob_tol = resolve_knob_tol(args)
    from tonematch.catalog import load_catalog

    log = MS.Log(Path(args.shard_out).with_suffix(".log"))
    catalog = load_catalog()
    pool = category_pool(args.category, catalog)
    global _ALIGNED
    _ALIGNED = bool(args.aligned_take)
    if args.only_models:
        pool = {m: pool[m] for m in args.only_models.split(",") if m in pool}
    mine = shard_models(list(pool), i, n)
    blocks = make_blocks({m: pool[m] for m in mine}, args.category, catalog)
    section, _ = build_target_section(args)
    s_search = MS.search_view(section, args.search_clip)
    kit = make_kit(s_search, args.level_tol_db, args.level_slope)
    engine = make_engine(args.preroll, log)
    try:
        sec_per = probe_seconds(engine, blocks, s_search, kit)
        left = args.deadline - time.time() - RESERVE_S * 0.0
        renders = max(len(blocks) * 6, int(left / (sec_per * 1.15) * 0.9))
        log(f"shard {i}/{n}: {len(blocks)} models, {sec_per:.2f} s per score, {left:.0f} s left -> {renders} renders")
        def plog(msg):                     # one line per improvement in the shard log and in the run's run.log
            log(msg)
            with open(Path(args.out) / "run.log", "a") as fh:
                fh.write(f"[shard {i}] {msg.strip()}\n")
        writer = ProgressWriter(args.progress_dir, blocks, args.category, args.preset_name, log=plog, run_id=args.run_id) if args.progress_dir else None
        rows = hybrid_rank(engine, blocks, s_search, kit, renders=renders, progress=writer, clip_s=args.search_clip, seed=args.seed + i,
                           k=args.k, f=args.hybrid_f, scale=args.hybrid_scale, specialists=tuple(args.specialists),
                           level_free=True, log=log, knob_tol=args.knob_tol)
        Path(args.shard_out).write_text(json.dumps({"shard": i, "models": mine, "rows": rows, "renders": getattr(engine, "renders", None),
                                                   "sec_per_score": sec_per}, default=MS.jsonable))
        return 0
    finally:
        engine.close()


def merge_shards(rows_by_shard: list[list[dict]]) -> list[dict]:
    """Refined rows from every shard by search loss; the unrefined by screen score after them."""
    rows = [r for rs in rows_by_shard for r in rs]
    ref = sorted((r for r in rows if r["refined"] is not None), key=lambda r: r["refined"])
    rest = sorted((r for r in rows if r["refined"] is None), key=lambda r: r["screen"])
    return ref + rest


def worker_count(arg: str, n_models: int) -> int:
    if str(arg) != "auto":
        return max(1, min(int(arg), n_models))
    try:
        from tonematch.engines import host_budget as hb
        n = int(hb.allowed_total())
    except Exception:  # noqa: BLE001
        n = 0
    n = n or max(1, (os.cpu_count() or 4) // 2)
    return max(1, min(n, 8, n_models))


# --------------------------------------------------------------------------------------- outputs

def table_rows(final: list[dict], blocks: dict) -> list[str]:
    rows = ["| rank | model | loss | search loss | level dB | settings |", "|---|---|---|---|---|---|"]
    for k, d in enumerate(final, 1):
        st = d["search_loss"]
        sets = ", ".join(f"{n} {v:.3f}" for n, v in d["values"].items())
        rows.append(f"| {k} | {d['model']} | {d['loss']:.4f} | {'' if st is None else f'{st:.4f}'} | {d['level_db']:.1f} | {sets} |")
    return rows


def write_outputs(out: Path, ctx: dict) -> None:
    from tonematch.preset import write_hlx

    final, section, ref = ctx["final"], ctx["section"], ctx["ref"]
    out.mkdir(parents=True, exist_ok=True)
    hlx = []
    for k, d in enumerate(final[:3], 1):
        p = d["preset"]
        p.name = f"{ctx['preset_name']} r{k}"[:32]
        path = out / ("best.hlx" if k == 1 else f"rank{k}.hlx")
        write_hlx(p, path)
        code, text = MS.validate_hlx(path)
        hlx.append((path, code))
        (out / f"{path.stem}.validate.md").write_text(text + "\n")
    sigs = {"target_stem": section.target, "match": final[0]["y"]}
    for k, d in enumerate(final[1:3], 2):
        sigs[f"match_rank{k}"] = d["y"]
    sigs["di_dry"] = section.dry
    pack = {} if ctx.get("lean") else MS.write_pack(out / "listen", section.target, final[0]["y"], final[0]["y"], section.dry,
                                                    {f"match_rank{k}": d["y"] for k, d in enumerate(final[1:3], 2)})
    for stale in ("start_unmatched.wav",):                       # match_song's unmatched-start slot is unused here
        (out / "listen" / stale).unlink(missing_ok=True)
    top = final[:5]
    lines = [f"# {ctx['song']}: single {ctx['category']} block match (T164), LOCAL ONLY", "",
             f"All losses are the **{LABEL}**: `PROVISIONAL_WEIGHTS` (equal weights on `ltas_tilt`, `crest_flatness_hnr`, "
             "`envelope`) plus match_song's level guard (reference = the target's RMS level, free within +-"
             f"{ctx['level_tol_db']:.0f} dB, then {ctx['level_slope']} per dB). Not admitted by `loss-trust-check`; lower is "
             "better; the numbers rank candidates against each other and are not a fidelity score. Frame-by-frame `mrstft_v2` "
             "was not used to search or rank, because the play-along DI is not sample-aligned with the target.", "",
             "## Target and DI", "",
             f"- target: htdemucs_6s guitar stem `{ctx['target_stems']}`, window {ctx['window']['start_s']:.2f}-"
             f"{ctx['window']['end_s']:.2f} s ({ctx['window']['dur_s']:.1f} s)",
             f"- DI: `{ctx['di']}` cut to the same song window by `take_sample0_song_s` (onset-envelope alignment, lag IQR about "
             "30 ms, NOT sample-aligned); true level, not normalised",
             f"- levels: target {ref['target_level_db']:.1f} dBFS RMS, raw DI {ref['dry_level_db']:.1f}", "",
             "## Engine and budget", "",
             f"- engine: Helix Native via NativeEngine, {ctx['workers']} shard worker(s), one host each (host scaler)",
             f"- {ctx['renders']} renders in the search (shards report their own), {ctx['wall_s']:.0f} s wall clock in total "
             f"(requested {ctx['wall_min']:.0f} min)",
             f"- knob tolerance {ctx['knob_tol']:g}" + (
                 " (Drive; Tone about 0.1 and 0.05 within 0.1 of either end, per-model knob names/tolerances from the map if any)"
                 if hasattr(ctx['knob_tol'], 'vector') else "") + (
                 f": coarse Drive x Tone grid on every model, wider refine set, CMA-ES stops inside the tolerance. "
                 f"{ctx['renders']} renders against about {T164_PLANNED_RENDERS} planned for the T164 run "
                 f"(`cache/matches/Mother-0-68s-drive`, which has no render total because it was stopped)"
                 if ctx['knob_tol'] > 0 else " (off: the old behaviour)"),
             f"- pool: all {ctx['n_models']} searchable `{ctx['category']}` models of block-library-v1.2 (time-variant models and "
             "Bitcrusher dropped as in `build_pools`), single block, no cab",
             f"- method: Gate 2 hybrid (broad f {ctx['hybrid_f']}, top-k {ctx['k']} per shard, successive halving with CMA-ES, "
             f"refine scale {ctx['hybrid_scale']}, broad draws without the level guard), search on the loudest "
             f"{ctx['search_clip']:.0f} s, final check on the full {ctx['window']['dur_s']:.0f} s with stems and mask; "
             f"specialists: {', '.join(ctx['specialists']) or 'none'}",
             f"- parameters: continuous, CC-verified, `Level`/`ChVol` and power-supply minutiae fixed (as in the Gate 2 pool)", "",
             f"## Top {len(top)} models (final check, full excerpt"
             + ("; ordered by AFx-Rep distance, losses shown for information" if ctx.get("final_rank") == "afxrep" else "") + ")", ""] + table_rows(top, ctx["blocks"]) + [
             "", f"Raw DI, no block: **{ref['dry_loss']:.4f}** (same loss, same excerpt).",
             f"The previous amp/cab winner (EssexA15 + 1x12 BlueBell, `cache/matches/Mother-0-68s/report.md`): **{ctx['amp_cab_loss']}**. "
             "Losses on different candidate sets are only roughly comparable (different free parameters, the amp/cab run used the "
             "median chain level as the guard reference and a weak prior).", ""]
    if "winner_mrstft_v2_level_db" in ref:
        lines += ["## Reference only: mrstft_v2 + level_db (UNADMITTED for this task)", "",
                  f"- winner: {ref['winner_mrstft_v2_level_db']:.4f}; raw DI: {ref.get('dry_mrstft_v2_level_db', float('nan')):.4f} "
                  "(`mrstft_v2` 1.0 + `level_db` 1e-7, as the Gate 2 weights). It assumes sample alignment, which this take does not "
                  "have, so treat it as a rough sanity check at most.", ""]
    sl = ctx.get("shortlist")
    if ctx.get("aligned"):
        lines += ["## Aligned take", "", "- `--aligned-take`: the take was declared sample-aligned, so the shortlist was searched and "
                  "ranked with `mrstft_v2` + `level_db` (Gate 2 weights) instead of the provisional no-DI loss.", ""]
    if sl:
        low = ctx["knob_path"] == "coarse_grid"
        lines += ["## AFx-Rep shortlist (T178)", "",
                  f"- models ranked by embed_afxrep distance of coarse Drive x Tone renders to the target; top {len(sl['top'])}: "
                  + ", ".join(sl["top"]) + f"; with measured family: {len(sl['models'])} models "
                  f"(family data {'used' if sl['families_used'] else 'absent'}); {sl['renders']} shortlist renders",
                  f"- knob path: **{ctx['knob_path']}**" + (
                      ". LOW CONFIDENCE: the settings are the best coarse-grid point by a sound score, with no dry-take or "
                      "fingerprint evidence. Do not read them as knob accuracy." if low else ""), ""]
    lines += ["## Expectation", "",
              "The original recording almost certainly includes an amp and a cab. A drive block alone has no cab, so the match "
              "may sound fizzier or boxier than the target. This is the human's chosen experiment; no cab was added.", "",
              "## Files", "", f"- best preset: `{hlx[0][0].name}` (hlx_validate exit {hlx[0][1]}); "
              + ", ".join(f"`{p.name}` (exit {c})" for p, c in hlx[1:]),
              "- `listen/`: target_stem, match, match_rank2, match_rank3, di_dry, ab_L-target_R-match; all loudness-matched to the "
              "target RMS (`gains.json`); `di_search.wav` is the true-level DI the search used.", ""]
    (out / "report.md").write_text("\n".join(lines))
    (out / "result.json").write_text(json.dumps({
        "label": LABEL, "window": ctx["window"], "reference": ref, "wall_s": ctx["wall_s"], "renders": ctx["renders"],
        "workers": ctx["workers"], "n_models": ctx["n_models"], "pack": pack,
        "knob_path": ctx.get("knob_path"), "final_rank": ctx.get("final_rank", "provisional"), "shortlist": ctx.get("shortlist"),
        "low_confidence": ctx.get("knob_path") == "coarse_grid",
        "finals": [{k: v for k, v in d.items() if k not in ("preset", "y")} for d in final]}, default=MS.jsonable, indent=1))


# --------------------------------------------------------------------------------------- main

def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target-stems", default=str(ROOT / "cache/stems/2ec11f9795be822e/htdemucs_6s"),
                    help="separated stems dir (guitar.wav, others.wav, mask) the target is cut from")
    ap.add_argument("--target-window", help="START:END in song seconds")
    ap.add_argument("--di", help="dry wav (true level)")
    ap.add_argument("--di-json", help="take json; alignment.take_sample0_song_s places the take in song time")
    ap.add_argument("--category", default="drive")
    ap.add_argument("--song", default="song")
    ap.add_argument("--out", required=True)
    ap.add_argument("--wall-min", type=float, default=25.0)
    ap.add_argument("--workers", default="auto", help="auto|N shard workers (one Native host each)")
    ap.add_argument("--k", type=int, default=2, help="models refined per shard")
    ap.add_argument("--hybrid-f", type=float, default=HYBRID_DEFAULT["f"])
    ap.add_argument("--hybrid-scale", type=float, default=HYBRID_DEFAULT["scale"])
    ap.add_argument("--knob-tol", type=float, default=None,
                    help="stop refining a knob once within this (0..1 knob units; ear cannot tell ~0.15 apart on Drive/Tone, "
                         "T176) and spend the renders on model and family choice: coarse Drive x Tone grid on every model, "
                         "wider refine set, early-stopped CMA-ES. 0 = the old behaviour (T164)")
    ap.add_argument("--knob-tol-drive", type=float, default=None, help="Drive tolerance (default 0.15; T178 per-knob)")
    ap.add_argument("--knob-tol-tone", type=float, default=None,
                    help="Tone/Treble/Filter/Contour tolerance (default 0.1, 0.05 within 0.1 of either end; T178)")
    ap.add_argument("--knob-tol-map", help="JSON per-model knob names and tolerances (the T179 knob_sensitivity.json shape); "
                    "default: reports/local/distortion-id/tone-ends/knob_sensitivity.json or cache/drive-maps/knob_sensitivity.json if present")
    ap.add_argument("--shortlist", choices=["none", "afxrep"], default="none",
                    help="afxrep (T178): rank all models by embed_afxrep distance of coarse renders to the target, keep the top "
                         "--shortlist-n (plus their T180 family), then set knobs by fingerprint inversion (T181, if present), "
                         "the aligned take (--aligned-take) or the coarse grid only (low confidence). none = the old behaviour")
    ap.add_argument("--shortlist-n", type=int, default=8)
    ap.add_argument("--families", help="drive families JSON (default cache/drive-maps/drive-families-v1.json if present)")
    ap.add_argument("--aligned-take", action="store_true",
                    help="the --di take is sample-aligned with the target: search and rank the shortlist with mrstft_v2 + level_db")
    ap.add_argument("--target-wav", help="the target audio itself (a wav the size of --dry-wav); replaces --target-stems/--target-window")
    ap.add_argument("--dry-wav", help="the DI to render through, used whole, with --target-wav")
    ap.add_argument("--knob-path", choices=["auto", "coarse_grid", "inversion"], default="auto",
                    help="with --shortlist: auto picks the inversion (T181) when it exists, else the aligned take, else the coarse grid; "
                         "coarse_grid / inversion force one path (T183)")
    ap.add_argument("--second-choice", type=float, default=None, metavar="CONF",
                    help="with --shortlist: also add the second-choice family of every shortlisted model whose member_confidence "
                         "in the families file is below CONF (T183 test setting)")
    ap.add_argument("--final-rank", choices=["provisional", "afxrep"], default="provisional",
                    help="with --shortlist: order the final models by the provisional re-score (default) or by AFx-Rep order "
                         "(T183 proposal, T185 test; the re-score is still computed and kept in each row)")
    ap.add_argument("--lean", action="store_true", help="skip the listening pack (batch runs, no audio kept)")
    ap.add_argument("--only-models", help=argparse.SUPPRESS)
    ap.add_argument("--specialists", nargs="*", default=["gain"], help="families; empty for none")
    ap.add_argument("--search-clip", type=float, default=SEARCH_CLIP_S)
    ap.add_argument("--final-top", type=int, default=10,
                    help="models re-scored on the full excerpt; with --shortlist at least the whole expanded shortlist")
    ap.add_argument("--level-tol-db", type=float, default=12.0,
                    help="level guard is free within +-this many dB of the target RMS (match_song uses 6 around the median chain level)")
    ap.add_argument("--level-slope", type=float, default=0.5)
    ap.add_argument("--amp-cab-loss", default="3.2559")
    ap.add_argument("--preset-name", default="Drive match")
    ap.add_argument("--preroll", type=int, default=144_000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--progress-dir", help="write current_best.hlx/.json here whenever the best objective improves "
                    "(atomic; a watcher can load it onto the device)")
    ap.add_argument("--dry-run", action="store_true")
    cli_result.add_result_arg(ap)
    ap.add_argument("--run-id", default="", help=argparse.SUPPRESS)
    ap.add_argument("--shard", help=argparse.SUPPRESS)
    ap.add_argument("--shard-out", help=argparse.SUPPRESS)
    ap.add_argument("--deadline", type=float, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if bool(a.target_wav) != bool(a.dry_wav):
        ap.error("--target-wav and --dry-wav go together")
    if not a.target_wav and not (a.target_window and a.di):
        ap.error("--target-window and --di are required (or --target-wav with --dry-wav)")
    return a


def resolve_knob_tol(args):
    """--knob-tol X: the old uniform tolerance (0 = off, the T164 behaviour). Else, for the drive category or when
    --knob-tol-drive/-tone/-map is given, a per-knob `KnobTol` (Drive 0.15, Tone 0.1 and 0.05 near its ends, T178);
    0 for the other categories."""
    if args.knob_tol is not None:
        return max(float(args.knob_tol), 0.0)
    given = (args.knob_tol_drive, args.knob_tol_tone, args.knob_tol_map)
    if args.category != "drive" and all(g is None for g in given):
        return 0.0
    from tonematch.search.knob_tol import make_knob_tol

    d = KNOB_TOL_DEFAULT if args.knob_tol_drive is None else max(args.knob_tol_drive, 0.0)
    return make_knob_tol(d, args.knob_tol_tone, args.knob_tol_map)


def run(args, engine=None, rows_by_shard=None) -> int:
    """Parent: spawn shards, merge, final-check, write. `engine`/`rows_by_shard` let tests skip the processes."""
    from tonematch.catalog import load_catalog

    t0 = time.time()
    args.knob_tol = resolve_knob_tol(args)
    args.run_id = args.run_id or str(int(t0))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = MS.Log(out / "run.log")
    catalog = load_catalog()
    pool = category_pool(args.category, catalog)
    section, win = build_target_section(args)
    log(f"{len(pool)} {args.category} models; window {win['start_s']:.2f}-{win['end_s']:.2f} s; "
        f"target {MS.LevelGuardKit.level_db(section.target):.1f} dBFS, DI {MS.LevelGuardKit.level_db(section.dry):.1f}")
    if args.dry_run:
        return 0
    global _ALIGNED
    _ALIGNED = bool(args.aligned_take)
    sl, path, own = None, "search", engine is None
    if args.shortlist == "afxrep":
        if own:
            engine = make_engine(args.preroll, log)
        sl = run_shortlist(args, pool, section, catalog, engine, log)
        path = sl["path"]
        pool = {m: pool[m] for m in sl["models"]}
        args.final_top = max(args.final_top, len(pool))     # every shortlisted model is scored, or family expansion is moot
        if path != "aligned_take":          # knobs come from the inversion or the coarse grid: no shard search
            rows_by_shard = [sl["rows"]]
    n = worker_count(args.workers, len(pool))
    deadline = t0 + args.wall_min * 60 - min(RESERVE_S, 0.25 * args.wall_min * 60)
    if rows_by_shard is None:
        procs = []
        for i in range(n):
            cmd = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--run-id", args.run_id, "--shard", f"{i}/{n}",
                   "--shard-out", str(out / f"shard{i}.json"), "--deadline", repr(deadline)]
            cmd = [c for c in cmd if c != "--dry-run"]
            if sl is not None:
                cmd += ["--only-models", ",".join(pool)]
            procs.append(subprocess.Popen(cmd, cwd=ROOT))
            time.sleep(2.0)
        import signal

        def _stop(*_):                     # SIGTERM on the supervisor stops its workers too
            raise SystemExit(143)
        signal.signal(signal.SIGTERM, _stop)
        try:
            codes = [p.wait() for p in procs]
        finally:
            for p in procs:                # no worker outlives its supervisor
                if p.poll() is None:
                    p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    p.kill()
        log(f"shards exit codes {codes}")
        rows_by_shard, stats = [], []
        for i in range(n):
            f = out / f"shard{i}.json"
            if f.is_file():
                d = json.loads(f.read_text())
                rows_by_shard.append(d["rows"])
                stats.append(d)
        renders = sum(int(s.get("renders") or 0) for s in stats)
    else:
        renders = shortlist_renders_to_add(sl, path, engine) if sl is not None and path != "aligned_take" \
            else sum(len(r) for r in rows_by_shard)
    rows = merge_shards(rows_by_shard)
    if not rows:
        log("no shard produced a result")
        return 1
    blocks = make_blocks({r["model"]: pool[r["model"]] for r in rows}, args.category, catalog)
    kit = make_kit(section, args.level_tol_db, args.level_slope)
    if engine is None:
        own = True
        engine = make_engine(args.preroll, log)
    try:
        final = final_check(engine, blocks, [r for r in rows if r["refined"] is not None] or rows, section, kit,
                            args.final_top, log)
        if getattr(args, "final_rank", "provisional") == "afxrep":
            if not sl or not sl.get("ranked"):
                raise SystemExit("--final-rank afxrep needs --shortlist afxrep")
            final = rank_by_afxrep(final, sl["ranked"])
            log("final order: AFx-Rep (the provisional re-score is kept in each row)")
        ref = reference_scores(engine, final[0], section, kit)
        if args.progress_dir:
            ProgressWriter(args.progress_dir, blocks, args.category, args.preset_name, log=log,
                           run_id=args.run_id).offer(
                final[0]["loss"], final[0]["model"], final[0]["values"], stage="final", force=True)
        ctx = {"song": args.song, "category": args.category, "window": win, "section": section, "final": final,
               "ref": ref, "blocks": blocks, "target_stems": args.target_stems, "di": args.di, "workers": n,
               "renders": renders + getattr(engine, "renders", 0), "wall_s": time.time() - t0, "wall_min": args.wall_min,
               "n_models": len(pool), "knob_tol": args.knob_tol, "hybrid_f": args.hybrid_f, "hybrid_scale": args.hybrid_scale, "k": args.k,
               "search_clip": args.search_clip, "specialists": args.specialists, "level_tol_db": args.level_tol_db,
               "level_slope": args.level_slope, "shortlist": sl, "knob_path": path, "final_rank": getattr(args, "final_rank", "provisional"), "aligned": _ALIGNED, "amp_cab_loss": args.amp_cab_loss, "preset_name": args.preset_name, "lean": args.lean}
        write_outputs(out, ctx)
        log(f"done: {ctx['wall_s']:.0f} s; report {out / 'report.md'}")
        return 0
    finally:
        if own:
            engine.close()


def shortlist_renders_to_add(sl: dict, path: str, engine) -> int:
    """Renders to add to the engine's own count in the report: none when the engine counts (T183: the shortlist's
    renders went through the same engine, so adding them again counted each of them twice)."""
    return 0 if hasattr(engine, "renders") else int(sl["renders"])


def run_shortlist(args, pool: dict, section, catalog, engine, log) -> dict:
    """T178 stage 1: AFx-Rep shortlist, then pick the knob path. Returns models, path, rows (for the no-search paths)."""
    from tonematch.search import shortlist as SL

    blocks = make_blocks(pool, args.category, catalog)
    r = SL.shortlist(engine, blocks, section, args.shortlist_n, families_path=args.families, log=log,
                     second_choice_below=args.second_choice)
    inv = SL.fingerprint_inversion() if args.knob_path in ("auto", "inversion") else None
    if args.knob_path == "inversion" and inv is None:
        raise SystemExit("--knob-path inversion: tonematch.metrics.distortion_step4.estimate_knobs is not available")
    path = SL.knob_path(inversion=inv, aligned_take=bool(args.aligned_take) and args.knob_path == "auto")
    rows = []
    by = {x["model"]: x for x in r["ranked"]}
    est = {}
    declined = False
    if path == "inversion":
        est = inv(section.target, section.dry, r["models"]) or {}
        if not est:                    # T183: the DI does not explain the target (another performance): no knob estimate
            path, declined = "coarse_grid", True
            log("fingerprint inversion declined (the DI does not explain the target); falling back to the coarse grid")
    if path != "aligned_take":
        for m in r["models"]:
            b = blocks[m]
            vals = {n: float(d) for n, lo, hi, d in b.free}
            fallback = by.get(m, {}).get("best_values", {})
            got = {n: lo + min(max(float(p), 0.0), 1.0) * (hi - lo)         # the inversion speaks in knob positions 0..1
                   for n, p in est.get(m, {}).items() for nn, lo, hi, _ in b.free if nn == n}
            vals.update(got or fallback)
            rows.append({"model": m, "values": vals, "screen": float(by[m]["distance"]) if m in by else float("nan"),
                         "refined": None})
    log(f"shortlist {r['top']} -> {len(r['models'])} models (families {'used' if r['families_used'] else 'absent'}); "
        f"knob path: {path}" + ("  [LOW CONFIDENCE: coarse grid, no dry-take or fingerprint evidence]" if path == "coarse_grid" else ""))
    return {"models": r["models"], "top": r["top"], "path": path, "rows": rows, "renders": r["renders"],
            "families_used": r["families_used"], "second_choice_added": r.get("second_choice_added", []),
            "inversion_models": sorted(est), "inversion_declined": declined, "ranked": [{k: x[k] for k in ("model", "distance")} for x in r["ranked"]]}


def main(argv=None) -> int:
    args = parse(argv)
    if args.shard:
        return run_shard(args)           # shard workers write no manifest; the parent does
    return cli_result.run(args, tool="match_block", body=lambda res: _run_with_result(args, res),
                          inputs={"target_stems": args.target_stems, "di": args.di, "di_json": args.di_json})


def _run_with_result(args, res) -> int:
    rc = run(args)
    out = Path(args.out)
    res.outputs.update({"out_dir": out, "run_log": out / "run.log"})
    if args.dry_run:
        res.metrics["dry_run"] = True
        res.notes = "dry run: target, DI and pool picked; nothing rendered"
        return rc
    for name in ("best.hlx", "rank2.hlx", "rank3.hlx", "report.md", "result.json", "listen"):
        if (out / name).exists():
            res.outputs[name.replace(".", "_")] = out / name
    rj = out / "result.json"
    if rj.is_file():
        d = json.loads(rj.read_text())
        fin = d.get("finals") or []
        res.metrics.update({"label": d.get("label"), "renders": d.get("renders"), "wall_s": d.get("wall_s"),
                            "workers": d.get("workers"), "n_models": d.get("n_models"),
                            "best_model": fin[0]["model"] if fin else None,
                            "best_loss": fin[0]["loss"] if fin else None,
                            "top": [{"model": f["model"], "loss": f["loss"]} for f in fin[:5]]})
    return rc


if __name__ == "__main__":
    sys.exit(main())
