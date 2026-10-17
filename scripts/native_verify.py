#!/usr/bin/env python3
"""Native verification suite (T103, human top priority 2026-10-17): every setting we send lands
exactly, and renders are consistent. Ledger dataset ``native-verify-v1`` (kinds smoke/parity).

Pass/fail rules were fixed in docs/DECISIONS.md (2026-10-17, native-engine, T103) BEFORE the
first run. Results go to ``cache/native/verify/<check>.json`` (local: derived from the user's
Native install, never committed) and one or more ledger rows per check.

  audit     (1 + 2b) random (model, settings) pairs on each template's own CC grid, template + CC
            against a drop of the same values (no controller assignments) rendered from its state
            on a host that never opened an editor; plus every baked-param model at library values.
            Drops are batched: many files per editor session (``editor_drop_many``).
  baked     (2a, 2c) baked values equal the presets'; search never frees a baked param.
  repeat    (3) the same setting 5x on one host interleaved with other models, settings and clip
            lengths in random order, then once on each of 3 more hosts; golden multi-block chains
            included. ``--warmup 0`` reproduces the pre-T103 host (no warm-up after a load).
  canary    (4) 24 host opens; every accepted host's probes against a reference host.
  sentinel  (5) forced drift mid-run is detected and corrected; counts in stats().

At most 6 Native hosts at once (the audit uses 3, repeat 4, canary 4). No pedal. Every host is
closed on exit.

  TONEMATCH_AGENT=native-engine .venv/bin/python scripts/native_verify.py audit
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import copy
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tonematch.engines import native as nat
from tonematch.engines import native_library as NL
from tonematch.engines.base import EngineError, UnsupportedModelChange

DATASET = "native-verify-v1"
CATEGORIES = ("drive", "delay", "reverb", "comp", "eq", "mod", "amp", "cab", "other")
DI = ROOT / "fixtures" / "smoke" / "di_2s_real.wav"
PASS_DB = -100.0          # rule 1/3: null at or below this passes
NOISY_MARGIN_DB = 3.0     # noisy model: null <= its repeat floor + this
SILENT_DBFS = -200.0      # both renders below this RMS: "silent", passes
END_STEPS = (0, 1, 2, 125, 126, 127)
END_P = 0.35              # probability a param is drawn at an end of its range
SR = nat.SAMPLE_RATE


def out_dir() -> Path:
    return nat.cache_dir() / "verify"


# --------------------------------------------------------------------------- pure helpers (tested)


def population(index: dict, time_variant: set, invalid: set) -> tuple[dict[str, list[str]], list[str]]:
    """Library models per category that the audit may draw, and the ones skipped (time-variant,
    failed, refused by Native) with the reason."""
    pop: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    skipped = []
    for m, e in sorted(index.get("models", {}).items()):
        if m in time_variant or e.get("time_variant"):
            skipped.append(f"{m} (time-variant)")
            continue
        if m in invalid:
            skipped.append(f"{m} (refused by Native)")
            continue
        pop.setdefault(e.get("category", "other"), []).append(m)
    skipped += [f"{m} (no template: {str(v)[:60]})" for m, v in sorted(index.get("failed", {}).items())]
    return pop, skipped


def draw_models(pop: dict[str, list[str]], per_cat: int, extra: int, rng: random.Random) -> list[tuple[str, str]]:
    """``per_cat`` (model, category) draws per category (without replacement until a category runs
    out, then again with new settings), plus ``extra`` from all models."""
    out = []
    for c in CATEGORIES:
        ms = list(pop.get(c, []))
        if not ms:
            continue
        bag: list[str] = []
        for _ in range(per_cat):
            if not bag:
                bag = ms[:]
                rng.shuffle(bag)
            out.append((bag.pop(), c))
    allm = [(m, c) for c in CATEGORIES for m in pop.get(c, [])]
    out += [rng.choice(allm) for _ in range(extra)] if allm else []
    return out


def live_params(tpl) -> list[tuple[str, str, float, float]]:
    """(ref, name, lo, hi) of every CC-verified continuous param of ``tpl`` (bypass CCs excluded)."""
    out = []
    for ref in sorted(tpl.verified):
        name = ref.split(":", 1)[1]
        if name.startswith("@") or ref not in tpl.map or ref not in tpl.ranges:
            continue
        lo, hi = (float(x) for x in tpl.ranges[ref])
        if hi > lo:
            out.append((ref, name, lo, hi))
    return out


def draw_cc(rng: random.Random, params, end_p: float = END_P) -> dict[str, int]:
    """A CC step per param ref: an end step with probability ``end_p``, else uniform 0..127."""
    return {ref: (rng.choice(END_STEPS) if rng.random() < end_p else rng.randint(0, nat.CC_STEPS))
            for ref, *_ in params}


def apply_cc(preset, tpl, cc: dict[str, int]):
    """A copy of ``preset`` with each ref in ``cc`` set to ``cc_value`` on ``tpl``'s own range."""
    p = copy.deepcopy(preset)
    for ref, c in cc.items():
        slot, name = ref.split(":", 1)
        lo, hi = (float(x) for x in tpl.ranges[ref])
        p.slots[int(slot)].params[name] = nat.cc_value(lo, hi, int(c))
    return p


def grid_cc(preset, tpl) -> dict[str, int]:
    """The CC step nearest each CC-verified param's value in ``preset`` (library values are catalog
    defaults, mostly off the 128-step grid, where a CC cannot land exactly)."""
    out = {}
    for ref, name, lo, hi in live_params(tpl):
        slot = int(ref.split(":", 1)[0])
        v = preset.slots[slot].params.get(name)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[ref] = round(nat.cc_norm(float(v), lo, hi) * nat.CC_STEPS)
    return out


def rms_dbfs(y: np.ndarray) -> float:
    return 10 * math.log10(max(float(np.mean(np.square(y, dtype=np.float64))), 1e-40)) if len(y) else -400.0


def verdict(null: float | None, floor: float | None, silent: bool = False) -> tuple[bool, str]:
    """Rules 1 and 3 (DECISIONS 2026-10-17, T103)."""
    if silent:
        return True, "silent"
    if null is None or not math.isfinite(null):
        return False, "no null"
    if null <= PASS_DB:
        return True, "exact"
    if floor is not None and math.isfinite(floor) and floor > PASS_DB and null <= floor + NOISY_MARGIN_DB:
        return True, "noisy (within its repeat floor)"
    return False, "miss"


def compare(ref: np.ndarray, y: np.ndarray, floor: float | None = None) -> dict:
    silent = rms_dbfs(ref) < SILENT_DBFS and rms_dbfs(y) < SILENT_DBFS
    nd = None if silent else nat.null_db(ref, y)
    ok, why = verdict(nd, floor, silent)
    return {"null_db": None if nd is None else round(nd, 1), "ok": ok, "why": why}


def hlx_doc(preset) -> dict:
    """The drop file for a truth render: the preset's values written in, no controller assignments."""
    from tonematch.preset import sync_hlx_snapshots

    d = sync_hlx_snapshots(preset.to_hlx_dict())
    ctl = d.get("data", {}).get("tone", {}).get("controller")
    if isinstance(ctl, dict):
        for k in list(ctl):
            ctl[k] = {}
    return d


# --------------------------------------------------------------------------- Native helpers


def load_di(seconds: float | None = None) -> np.ndarray:
    import soundfile as sf

    x, sr = sf.read(str(DI), dtype="float32", always_2d=True)
    if sr != SR:
        raise SystemExit(f"{DI} is {sr} Hz, need {SR}")
    x = x.mean(axis=1).astype(np.float32)
    return x[: int(seconds * SR)] if seconds else x


def plugin_and_start() -> tuple[str, bytes]:
    p = nat.locate_plugin()
    if p is None or not nat.start_state_path().is_file():
        raise SystemExit("Helix Native or cache/native/start_stomp.state missing")
    return str(p), nat.start_state_path().read_bytes()


def drop_states(paths: list[Path], plugin: str, start: bytes, max_sessions: int = 8) -> tuple[list, int]:
    """Drop every file in batched editor sessions (one window for many files) on hosts opened in
    the start state; a session ends at a rejected file and the rest go to the next session.
    Returns (state bytes or an error string per path, sessions used)."""
    out: list = [None] * len(paths)
    pending = list(range(len(paths)))
    sessions = 0
    snaps = out_dir() / "dialogs"
    snaps.mkdir(parents=True, exist_ok=True)
    while pending and sessions < max_sessions:
        sessions += 1
        host = nat.HostProcess(plugin, None, initial_state=start)
        try:
            gui = host.call("gui_available")[0]
            if not gui.get("gui"):
                raise SystemExit(f"no GUI session for drops: {gui.get('reason')}")
            hdr, blob = host.call("editor_drop_many", paths=[str(paths[j].resolve()) for j in pending],
                                  load_timeout_s=8.0, poll_s=0.1, stable_polls=2, settle_s=0.3,
                                  dialog_snap_prefix=str(snaps / f"{time.strftime('%Y%m%dT%H%M%S')}_{os.getpid()}"))
        finally:
            host.close()
        results = hdr.get("results", [])
        if hdr.get("error") and not results:
            raise SystemExit(f"editor session failed: {hdr['error']}")
        pos = 0
        for j, res in zip(pending, results, strict=False):
            n = int(res.get("state_len", 0))
            st, pos = blob[pos:pos + n], pos + n
            out[j] = f"rejected: {res.get('error') or 'no state'}" if (res.get("error") or not st) else st
        pending = pending[len(results):]
        print(f"  drop session {sessions}: {len(results)} files, {len(pending)} left", flush=True)
    for j in pending:
        out[j] = "not dropped (sessions exhausted)"
    return out, sessions


class Renderer:
    """A NativeEngine on the v1.2 library, rendering presets (template + CC) or raw states."""

    def __init__(self, label: str):
        self.label = label
        self.eng = nat.NativeEngine(templates_dir=NL.library_templates(), on_demand=False, autocapture=False)

    def preset(self, p, x: np.ndarray) -> tuple[np.ndarray, str]:
        self.eng.load(p)
        return self.render(x), nat.template_stem(self.eng.current_template)

    def state(self, state: bytes, chain, x: np.ndarray) -> np.ndarray:
        e = self.eng
        e._set_state(state)
        e.current_chain, e.current_template, e._loaded_preset = tuple(chain), None, None
        return self.render(x)

    def render(self, x: np.ndarray) -> np.ndarray:
        try:
            return self.eng.render(x)
        except EngineError as err:  # FX loops, sends: Native is silent on purpose
            if "silence" not in str(err):
                raise
            e = self.eng
            lat, pre = e.latency(), e.preroll
            padded = np.concatenate([np.zeros(pre, np.float32), x, np.zeros(lat, np.float32)])
            return e._raw_render(padded)[pre + lat:pre + lat + len(x)]

    def add_template(self, tpl) -> None:
        self.eng._variants.setdefault(tpl.chain, [])
        if not any(t is tpl for t in self.eng._variants[tpl.chain]):
            self.eng._variants[tpl.chain].append(tpl)

    def close(self) -> None:
        self.eng.close()


LEDGER = True  # --no-ledger (shakedown runs) prints the row instead


def ledger(kind: str, metric: str, value, renders: int | None, wall: float, notes: str) -> None:
    if not LEDGER:
        print(f"[no-ledger] {kind} {metric}={value} {notes[:300]}")
        return
    env = dict(os.environ)
    env.setdefault("TONEMATCH_AGENT", "native-engine")
    cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", kind, "--engine", "native",
           "--dataset", DATASET, "--metric", metric, "--value", str(value), "--wall-clock-s", f"{wall:.1f}",
           "--notes", notes[:1500]]
    if renders is not None:
        cmd += ["--renders", str(int(renders))]
    subprocess.run(cmd, check=True, env=env)


def save(name: str, res: dict) -> Path:
    out_dir().mkdir(parents=True, exist_ok=True)
    f = out_dir() / f"{name}.json"
    f.write_text(json.dumps(res, indent=1, default=str))
    return f


def library_items(seed: int, per_cat: int, extra: int) -> tuple[list[dict], list[str]]:
    idx = NL.load_index()
    pop, skipped = population(idx, set(NL.time_variant_models()), nat.invalid_models())
    rng = random.Random(seed)
    items = []
    for m, c in draw_models(pop, per_cat, extra, rng):
        tpl = NL.template_for(m)
        cc = draw_cc(rng, live_params(tpl))
        items.append({"model": m, "category": c, "template": nat.template_stem(tpl), "tpl": tpl,
                      "cc": cc, "preset": apply_cc(NL.block_preset(m), tpl, cc)})
    return items, skipped


def worst(vals) -> float | None:
    v = [x for x in vals if x is not None]
    return max(v) if v else None


def engine_drifts(rs: list[Renderer]) -> dict:
    st = [r.eng.stats() for r in rs]
    return {k: sum(int(s.get(k) or 0) for s in st) for k in
            ("renders", "sentinel_checks", "sentinel_drifts", "sentinel_fixed", "sentinel_recycles",
             "canary_respawns", "canary_fixed")}


# --------------------------------------------------------------------------- check 1 + 2b: audit


def cmd_audit(a) -> int:
    t0 = time.perf_counter()
    plugin, start = plugin_and_start()
    items, skipped = library_items(a.seed, a.per_cat, a.extra)
    idx = NL.load_index()
    baked_models = sorted(m for m, e in idx["models"].items() if e.get("unverified")
                          and not (m in NL.time_variant_models() or e.get("time_variant")))
    for m in baked_models if not a.no_baked else []:  # 2b: every baked-param model at library values
        tpl = NL.template_for(m)  # CC-driven params on the grid nearest the library value, baked ones exact
        cc = grid_cc(NL.block_preset(m), tpl)
        items.append({"model": m, "category": idx["models"][m]["category"], "template": nat.template_stem(tpl),
                      "tpl": tpl, "cc": cc, "preset": apply_cc(NL.block_preset(m), tpl, cc), "kind": "baked"})
    for it in items:
        it.setdefault("kind", "audit")
    d = out_dir() / "audit_drops"
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.hlx"):
        f.unlink()
    paths = []
    for i, it in enumerate(items):
        f = d / f"{i:03d}_{it['kind']}_{it['model']}.hlx"
        f.write_text(json.dumps(hlx_doc(it["preset"]), indent=1))
        paths.append(f)
    print(f"audit: {sum(it['kind'] == 'audit' for it in items)} pairs + {len(baked_models)} baked models; "
          f"dropping {len(paths)} files", flush=True)
    t_drop = time.perf_counter()
    states, sessions = drop_states(paths, plugin, start)
    t_drop = time.perf_counter() - t_drop
    x = load_di()
    rt, rd = Renderer("template"), Renderer("truth")
    try:
        def template_side():
            out = []
            for it in items:
                y1, stem = rt.preset(it["preset"], x)
                y2, _ = rt.preset(it["preset"], x)
                out.append((y1, y2, stem))
            return out

        def truth_side(lat):
            rd.eng._plugin_latency = lat
            out = []
            for it, st in zip(items, states, strict=True):
                if not isinstance(st, bytes):
                    out.append((None, None))
                    continue
                z1 = rd.state(st, nat.chain_key(it["preset"]), x)
                z2 = rd.state(st, nat.chain_key(it["preset"]), x)
                out.append((z1, z2))
            return out

        rt.eng.load(items[0]["preset"])
        lat = rt.eng.latency()
        with cf.ThreadPoolExecutor(2) as ex:
            ft, fd = ex.submit(template_side), ex.submit(truth_side, lat)
            tr, dr = ft.result(), fd.result()
        guard = baked_guard_live(rt, idx)  # 2d on a real engine
        drifts = engine_drifts([rt, rd])
    finally:
        rt.close()
        rd.close()
    rows, fails = [], []
    for it, st, (y1, y2, stem), (z1, z2) in zip(items, states, tr, dr, strict=True):
        tpl = it["tpl"]
        row = {"kind": it["kind"], "model": it["model"], "category": it["category"], "template_used": stem,
               "template_expected": it["template"],
               "settings": {r.split(":", 1)[1]: {"cc": c, "value": round(nat.cc_value(*tpl.ranges[r], c), 6),
                                                 "range": tpl.ranges[r]} for r, c in it["cc"].items()}}
        if not isinstance(st, bytes):
            row.update({"ok": False, "why": f"truth drop failed: {st}", "null_db": None})
        elif stem != it["template"]:
            row.update({"ok": False, "why": f"engine picked {stem}, not the library template", "null_db": None})
        else:
            fl = worst([nat.null_db(y1, y2) if rms_dbfs(y1) > SILENT_DBFS else None,
                        nat.null_db(z1, z2) if rms_dbfs(z1) > SILENT_DBFS else None])
            row.update(compare(z1, y1, fl))
            row["floor_db"] = None if fl is None else round(fl, 1)
            row["out_dbfs"] = round(rms_dbfs(z1), 1)
        rows.append(row)
        if not row["ok"]:
            fails.append(row)
    res = {"rules": "DECISIONS 2026-10-17 (native-engine, T103) rules 1 and 2b", "seed": a.seed,
           "skipped": skipped, "sessions": sessions, "drop_s": round(t_drop, 1), "rows": rows,
           "baked_guard_live": guard, "engine_stats": drifts}
    # Diagnose failing pairs one param at a time (which param, which value).
    if fails and not a.no_diagnose:
        res["diagnosis"] = diagnose([it for it, r in zip(items, rows, strict=True) if not r["ok"]], plugin, start, x)
    wall = time.perf_counter() - t0
    res["wall_s"] = round(wall, 1)
    f = save("audit", res)
    au = [r for r in rows if r["kind"] == "audit"]
    bk = [r for r in rows if r["kind"] == "baked"]
    cov = {}
    for it in items:
        for r, c in it["cc"].items():
            k = f"{it['model']}|{r}"
            cov.setdefault(k, set()).add("end" if c in END_STEPS else "mid")
    n_params = sum(len(it["cc"]) for it in items if it["kind"] == "audit")
    n_end = sum(1 for it in items if it["kind"] == "audit" for c in it["cc"].values() if c in END_STEPS)
    for name, rs in (("audit", au), ("baked", bk)):
        if not rs:
            continue
        ok = sum(r["ok"] for r in rs)
        wn = worst(r["null_db"] for r in rs if r["ok"] and r["why"] == "exact")
        noisy = [f"{r['model']} {r['null_db']} (floor {r['floor_db']})" for r in rs if r["why"].startswith("noisy")]
        silent = [r["model"] for r in rs if r["why"] == "silent"]
        bad = "; ".join(f"{r['model']}: {r['why']} {r['null_db']} {json.dumps({k: v['value'] for k, v in r['settings'].items()})}"
                        for r in rs if not r["ok"])
        print(f"{name}: {ok}/{len(rs)} pass; worst exact null {wn}; noisy {noisy}; silent {silent}")
        if bad:
            print("  FAILURES:", bad)
        kind = "parity"
        metric = "truth_audit_pass" if name == "audit" else "baked_render_pass"
        notes = (f"T103 rule {'1' if name == 'audit' else '2b'}: {ok}/{len(rs)} pass (template+CC vs drop truth, "
                 f"no controllers, fresh-state host); worst exact null {wn} dB; noisy {len(noisy)} {noisy[:4]}; "
                 f"silent {len(silent)} {silent[:6]}; ")
        if name == "audit":
            notes += (f"{n_params} param settings ({n_end} at range ends) over {len(au)} pairs seed {a.seed}; "
                      f"skipped {skipped}; ")
        notes += f"drops {len(paths)} files in {sessions} editor session(s) {t_drop:.0f} s; sentinel {drifts}; "
        notes += f"failures: {bad or 'none'}; file {f.relative_to(ROOT)}"
        ledger(kind, metric, ok, drifts["renders"], wall, notes)
        ledger(kind, metric.replace("_pass", "_worst_null_db"), wn if wn is not None else 0.0, None, wall,
               f"T103 rule {'1' if name == 'audit' else '2b'}: worst null among exact passes; see {metric} row")
    print(f"wrote {f}")
    return 0 if not fails else 1


def diagnose(items: list[dict], plugin: str, start: bytes, x: np.ndarray) -> list[dict]:
    """Each failing pair again, one param at its drawn step at a time; the others on the grid step
    nearest their library value (off-grid values cannot land; they would mask the culprit)."""
    sub, paths = [], []
    d = out_dir() / "diag_drops"
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.hlx"):
        f.unlink()
    for it in items:
        base = NL.block_preset(it["model"]) if not it["model"].startswith("golden:") else it["preset"]
        g = grid_cc(base, it["tpl"])
        for ref, c in [("base", None)] + list(it["cc"].items()):
            p = apply_cc(base, it["tpl"], {**g, **({ref: c} if c is not None else {})})
            f = d / f"{len(paths):03d}_{it['model'].replace(':', '_')}_{ref.replace(':', '_')}.hlx"
            f.write_text(json.dumps(hlx_doc(p), indent=1))
            sub.append((it, ref, c, p))
            paths.append(f)
    if not paths:
        return []
    states, _ = drop_states(paths, plugin, start)
    r = Renderer("diag")
    out = []
    try:
        for (it, ref, c, p), st in zip(sub, states, strict=True):
            y, _ = r.preset(p, x)
            z = r.state(st, nat.chain_key(p), x) if isinstance(st, bytes) else None
            cmp = compare(z, y) if z is not None else {"ok": False, "why": str(st), "null_db": None}
            out.append({"model": it["model"], "param": "(all on the grid nearest library)" if c is None else ref.split(":", 1)[1],
                        "cc": c, "value": None if c is None else round(nat.cc_value(*it["tpl"].ranges[ref], c), 6), **cmp})
    finally:
        r.close()
    return out


def baked_guard_live(r: Renderer, idx: dict) -> dict:
    """2d on Native: moving an unverified param off its baked value is refused, not sent as a CC."""
    m = next((m for m, e in sorted(idx["models"].items()) if e.get("unverified")
              and any(not u.split(":", 1)[1].startswith("@") for u in e["unverified"])), None)
    if m is None:
        return {"model": None}
    tpl = NL.template_for(m)
    ref = next(u for u in idx["models"][m]["unverified"] if not u.split(":", 1)[1].startswith("@"))
    name = ref.split(":", 1)[1]
    p = NL.block_preset(m)
    r.eng.load(p)
    v0 = p.slots[0].params[name]
    lo, hi = tpl.ranges.get(ref, [0.0, 1.0])
    v1 = float(lo) + 0.37 * (float(hi) - float(lo))
    out = {"model": m, "param": name, "baked": v0, "tried": v1}
    try:
        r.eng.set_params({(0, name): v0})
        out["same_value"] = "accepted"
    except UnsupportedModelChange as e:
        out["same_value"] = f"refused: {e}"
    try:
        r.eng.set_params({(0, name): v1})
        out["moved"] = "SENT (violation)"
        out["ok"] = False
    except UnsupportedModelChange as e:
        out["moved"] = f"refused: {str(e)[:120]}"
        out["ok"] = out["same_value"] == "accepted"
    r.eng.load(NL.block_preset(m))
    return out


def cmd_landing(a) -> int:
    """Where does a CC land when it misses its drop truth? For each (model, param, step) that failed
    alone (``--cases`` or the audit's diagnosis), the template's state with only that CC sent at
    step c is nulled against drops of candidate values (the other params at their exact library
    values): grid steps c-2..c+2, the grid value rounded to 1, 2 and 3 decimals, computed in float64,
    and +-1 float32 ulp. Diagnostic, no pass/fail rule; one smoke row with the findings."""
    t0 = time.perf_counter()
    plugin, start = plugin_and_start()
    if a.cases:
        cases = [(m, n, int(c)) for m, n, c in (x.split(":") for x in a.cases.split(","))]
    else:
        aud = json.loads((out_dir() / "audit.json").read_text())
        diag = aud.get("diagnosis", [])
        base_ok = {d["model"] for d in diag if d["cc"] is None and d["ok"]}
        cases = sorted({(d["model"], d["param"], d["cc"]) for d in diag
                        if d["cc"] is not None and not d["ok"] and d["model"] in base_ok})
    if not cases:
        print("no single-param failures with a passing base in audit.json")
        return 0
    sub, paths = [], []
    d = out_dir() / "landing_drops"
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.hlx"):
        f.unlink()
    for m, name, c in cases:
        tpl = NL.template_for(m)
        ref = f"0:{name}"
        lo, hi = (float(x) for x in tpl.ranges[ref])
        base = NL.block_preset(m)
        p0 = apply_cc(base, tpl, {ref: c})  # the other params at their exact library values (state)
        v = nat.cc_value(lo, hi, c)
        cands = {f"grid c={k}": nat.cc_value(lo, hi, k) for k in (c - 2, c - 1, c, c + 1, c + 2) if 0 <= k <= nat.CC_STEPS}
        cands.update({f"round{k}": round(v, k) for k in (1, 2, 3)})
        cands["float64 lo+c/127*(hi-lo)"] = lo + c / 127.0 * (hi - lo)
        cands["+1 ulp"] = float(np.nextafter(np.float32(v), np.float32(np.inf)))
        cands["-1 ulp"] = float(np.nextafter(np.float32(v), np.float32(-np.inf)))
        for label, val in cands.items():
            p = copy.deepcopy(p0)
            p.slots[0].params[name] = float(val)
            f = d / f"{len(paths):03d}_{m}_{name}_{c}.hlx"
            f.write_text(json.dumps(hlx_doc(p), indent=1))
            sub.append((m, name, c, label, float(val), p0))
            paths.append(f)
    states, sessions = drop_states(paths, plugin, start)
    x = load_di()
    r = Renderer("landing")
    rows = []
    try:
        ycc = {}
        for (m, name, c, label, val, p0), st in zip(sub, states, strict=True):
            key = (m, name, c)
            if key not in ycc:  # the template's state plus this one CC (nothing else sent)
                tpl = NL.template_for(m)
                lo, hi = tpl.ranges[f"0:{name}"]
                e = r.eng
                e._set_state(tpl.state)
                e.current_chain, e.current_template, e._loaded_preset = tpl.chain, None, None
                e.host.call("set_params", values={tpl.map[f"0:{name}"]: nat.cc_norm(nat.cc_value(lo, hi, c), lo, hi)})
                ycc[key] = r.render(x)
            z = r.state(st, nat.chain_key(p0), x) if isinstance(st, bytes) else None
            rows.append({"model": m, "param": name, "cc": c, "candidate": label, "value": round(val, 6),
                         "null_db": None if z is None else round(nat.null_db(z, ycc[key]), 1)})
    finally:
        r.close()
    by = {}
    for row in rows:
        by.setdefault((row["model"], row["param"], row["cc"]), []).append(row)
    summary = []
    for (m, name, c), rs in by.items():
        best = min((x for x in rs if x["null_db"] is not None), key=lambda x: x["null_db"], default=None)
        summary.append({"model": m, "param": name, "cc": c,
                        "grid_value": next((x["value"] for x in rs if x["candidate"] == f"grid c={c}"), None),
                        "lands_on": best["candidate"] if best and best["null_db"] <= PASS_DB else "none of the candidates",
                        "best_null_db": best["null_db"] if best else None,
                        "nulls": {x["candidate"]: x["null_db"] for x in rs}})
    wall = time.perf_counter() - t0
    f = save("landing", {"cases": summary, "rows": rows, "sessions": sessions, "wall_s": round(wall, 1)})
    for s_ in summary:
        print(f"{s_['model']} {s_['param']}@{s_['cc']} (grid {s_['grid_value']}): lands on {s_['lands_on']} "
              f"{s_['nulls']}")
    ledger("smoke", "cc_landing_misses", len(summary), len(rows), wall,
           "T103 landing probe (diagnostic, no rule): CC step vs drop truths of candidate values; " + "; ".join(
               f"{s_['model']} {s_['param']}@{s_['cc']}: {s_['lands_on']} ({s_['best_null_db']} dB)" for s_ in summary)
           + f"; file {f.relative_to(ROOT)}")
    return 0


# --------------------------------------------------------------------------- check 2a, 2c: baked


def cmd_baked(a) -> int:
    t0 = time.perf_counter()
    import match_song as MS
    import synthetic_v1 as SV

    from tonematch.catalog import load_catalog

    catalog = load_catalog()
    idx = NL.load_index()
    lib = NL.library()
    viol_a, viol_c, n_baked, n_models = [], [], 0, 0
    for m, tpl in sorted(lib.items()):
        e = idx["models"][m]
        p = NL.block_preset(m, catalog)
        if e.get("unverified"):
            n_models += 1
            for ref in e["unverified"]:
                n_baked += 1
                slot, name = ref.split(":", 1)
                pv = nat._scalar(p.slots[int(slot)].params.get(name))
                if tpl.baked.get(ref) != pv:
                    viol_a.append(f"{m} {ref}: baked {tpl.baked.get(ref)} != preset {pv}")
            if nat.baked_fingerprint(p, tpl.map, tpl.verified)[1] != tpl.baked_hash:
                viol_a.append(f"{m}: baked_hash does not match the library preset")
        free = MS.derive_free(p, tpl, catalog, MS.NATIVE_FROZEN)
        for i, name in free:
            if f"{i}:{name}" not in tpl.verified:
                viol_c.append(f"derive_free {m} {i}:{name} (unverified)")
        for name, *_ in SV.free_params(m, tpl, catalog, e["category"]):
            if f"0:{name}" not in tpl.verified:
                viol_c.append(f"free_params {m} 0:{name} (unverified)")
    # Code reading (2c): derive_free skips the verified check when ``template.verified`` is empty.
    hole = []
    for m, tpl in sorted(lib.items()):
        if any(not u.split(":", 1)[1].startswith("@") for u in idx["models"][m].get("unverified", [])):
            t2 = copy.copy(tpl)
            t2.verified = []
            p = NL.block_preset(m, catalog)
            hole = [f"{i}:{n}" for i, n in MS.derive_free(p, t2, catalog, MS.NATIVE_FROZEN)]
            hole = {"model": m, "freed_with_empty_verified": hole}
            break
    wall = time.perf_counter() - t0
    res = {"models_with_baked": n_models, "baked_params": n_baked, "violations_2a": viol_a,
           "violations_2c": viol_c, "empty_verified_hole": hole, "library_models": len(lib)}
    f = save("baked", res)
    print(json.dumps({k: v for k, v in res.items()}, indent=1)[:3000])
    ledger("smoke", "baked_violations", len(viol_a) + len(viol_c), None, wall,
           f"T103 rules 2a+2c: {n_models} v1.2 models bake {n_baked} params; baked value != preset value "
           f"{len(viol_a)}; search frees an unverified param {len(viol_c)} (match_song.derive_free with "
           f"NATIVE_FROZEN, synthetic_v1.free_params, all {len(lib)} models); latent: derive_free frees every "
           f"mapped param when template.verified is [] (e.g. {hole.get('model') if hole else None}: "
           f"{(hole or {}).get('freed_with_empty_verified', [])[:6]}), no v1.2 template has an empty list; "
           f"proposal to search in DECISIONS; file {f.relative_to(ROOT)}")
    return 0 if not (viol_a or viol_c) else 1


# --------------------------------------------------------------------------- check 3: repeat


def golden_items(rng: random.Random, random_cc: bool = True) -> list[dict]:
    """Every golden multi-block chain (match_song.TEMPLATE_SETS), with random CC-grid settings
    (``random_cc``) or at the preset's own values."""
    import match_song as MS

    from tonematch.catalog import load_catalog
    from tonematch.preset import read_hlx

    catalog = load_catalog()
    out = []
    for name, (hlx, tdir) in MS.TEMPLATE_SETS.items():
        f, d = ROOT / hlx, ROOT / tdir
        if not f.is_file() or not d.is_dir():
            continue
        preset = MS.freeze(read_hlx(f, catalog), MS.NATIVE_FROZEN)
        tpl = MS.pick_template(nat.ChainTemplate.load_variants(d).get(nat.chain_key(preset), []), preset)
        if tpl is None:
            continue
        cc = draw_cc(rng, live_params(tpl)) if random_cc else {}
        out.append({"model": f"golden:{name}", "category": "chain", "tpl": tpl, "cc": cc,
                    "preset": apply_cc(preset, tpl, cc)})
    return out


def cmd_repeat(a) -> int:
    t0 = time.perf_counter()
    if a.legacy:  # the pre-T103 host: no warm-up after a load, no amp latch at open
        a.warmup = 0
        os.environ["TONEMATCH_NATIVE_PRIME"] = "0"
    if a.warmup is not None:
        os.environ["TONEMATCH_NATIVE_WARMUP"] = str(a.warmup)  # read by every host process at start
    rng = random.Random(a.seed)
    items, skipped = library_items(a.seed, 2, 2)
    items += golden_items(rng)
    for i, it in enumerate(items):
        it["key"] = f"{i:02d}:{it['model']}"
    pop, _ = population(NL.load_index(), set(NL.time_variant_models()), nat.invalid_models())
    allm = [m for c in CATEGORIES for m in pop.get(c, [])]
    x_full = load_di()

    def distractor() -> tuple:
        if rng.random() < 0.35:  # another setting of one of the items' own chains (knob history)
            it = rng.choice(items)
            return apply_cc(it["preset"], it["tpl"], draw_cc(rng, live_params(it["tpl"]))), it["tpl"]
        m = rng.choice(allm)
        tpl = NL.template_for(m)
        return apply_cc(NL.block_preset(m), tpl, draw_cc(rng, live_params(tpl))), None

    def clip() -> np.ndarray:  # random lengths shift the host's alignment pad
        n = rng.randint(int(0.3 * SR), len(x_full))
        return x_full[:n]

    with cf.ThreadPoolExecutor(4) as ex:
        rs = list(ex.map(lambda k: Renderer(f"host{k}"), range(4)))
    try:
        for r in rs:
            for it in items:
                if it["model"].startswith("golden:"):
                    r.add_template(it["tpl"])
        h1 = rs[0]
        ref, floor = {}, {}
        for it in items:
            y1, _ = h1.preset(it["preset"], x_full)
            y2, _ = h1.preset(it["preset"], x_full)
            ref[it["key"]], floor[it["key"]] = y1, nat.null_db(y1, y2) if rms_dbfs(y1) > SILENT_DBFS else None
        seq1 = [("item", it) for it in items for _ in range(5)] + [("dist", distractor()) for _ in range(100)]
        rng.shuffle(seq1)
        seqs = []
        for _ in range(3):
            s = [("item", it) for it in items] + [("dist", distractor()) for _ in range(30)]
            rng.shuffle(s)
            seqs.append(s)
        clips = {id(e): clip() for s in [seq1] + seqs for kind, e in s if kind == "dist"}

        def run(r: Renderer, seq) -> list[tuple[str, np.ndarray, str]]:
            got, prev = [], "open"
            for kind, e in seq:
                if kind == "item":
                    got.append((e["key"], r.preset(e["preset"], x_full)[0], prev))
                    prev = f"item {e['key']}"
                else:
                    p, _ = e
                    try:
                        r.preset(p, clips[id(e)])
                    except UnsupportedModelChange:
                        pass
                    prev = f"distractor {[s.model_id for s in p.slots if s.model_id]} ({len(clips[id(e)])} samples)"
            return got

        with cf.ThreadPoolExecutor(4) as ex:
            outs = list(ex.map(run, rs, [seq1] + seqs))
        drifts = engine_drifts(rs)
    finally:
        for r in rs:
            r.close()
    per: dict[str, dict] = {it["key"]: {"model": it["model"], "category": it["category"],
                                        "floor_db": None if floor[it["key"]] is None else round(floor[it["key"]], 1),
                                        "same_host": [], "other_hosts": []} for it in items}
    fails = []
    for h, got in enumerate(outs):
        for key, y, prev in got:
            c = compare(ref[key], y, floor[key])
            per[key]["same_host" if h == 0 else "other_hosts"].append(c["null_db"])
            if not c["ok"]:
                fails.append(f"{key} host{h + 1} {c['null_db']} dB after {prev}")
    n = sum(len(v["same_host"]) + len(v["other_hosts"]) for v in per.values())
    wn = worst(x for v in per.values() for x in v["same_host"] + v["other_hosts"])
    wall = time.perf_counter() - t0
    from tonematch.engines import native_host as _nh  # the hosts' default when the env is unset

    warm = os.environ.get("TONEMATCH_NATIVE_WARMUP", f"{_nh.WARMUP} (default)") + (
        ", amp latch off" if os.environ.get("TONEMATCH_NATIVE_PRIME") == "0" else ", amp latch at open")
    res = {"rules": "DECISIONS 2026-10-17 (native-engine, T103) rule 3", "seed": a.seed, "warmup": warm,
           "items": per, "failures": fails, "renders_checked": n, "skipped": skipped, "engine_stats": drifts,
           "wall_s": round(wall, 1)}
    f = save("repeat_legacy" if a.legacy else f"repeat_warmup{a.warmup}" if a.warmup is not None else "repeat", res)
    print(f"repeat (warmup {warm}): {n - len(fails)}/{n} renders pass; worst {wn} dB; failures {len(fails)}")
    for x in fails[:40]:
        print("  FAIL", x)
    gold = [k for k in per if per[k]["model"].startswith("golden:")]
    ledger("parity", "repeat_pass", n - len(fails), drifts["renders"], wall,
           f"T103 rule 3 (warmup {warm}): {n - len(fails)}/{n} renders null <= -100 dB (or floor+3) vs the first "
           f"host-1 render; {len(items) - len(gold)} library pairs + {len(gold)} golden chains; host 1 5x each among "
           f"100 distractors (35% same-chain knob moves, random clip lengths), hosts 2-4 1x each among 30; "
           f"worst {wn} dB; failures {fails[:8]}; sentinel {drifts}; file {f.relative_to(ROOT)}")
    ledger("parity", "repeat_worst_null_db", wn if wn is not None else 0.0, None, wall,
           f"T103 rule 3 (warmup {warm}): worst null over every repeat; see repeat_pass row")
    return 0 if not fails else 1


# --------------------------------------------------------------------------- check 4: canary


def cmd_canary(a) -> int:
    t0 = time.perf_counter()
    probes_lib = ["HD2_ReverbDoubleTank", "HD2_ReverbGanymede", "HD2_EQGraphic10Band"]
    gold = {it["model"]: it for it in golden_items(random.Random(0), random_cc=False)}
    probes_gold = [k for k in ("golden:K", "golden:B", "golden:H") if k in gold]
    x = load_di()
    f = nat._canary_file(*plugin_and_start())
    votes0 = [v["n"] for v in nat._canary_votes(f)]

    def one(_k) -> dict:
        r = Renderer("canary")
        try:
            for k in probes_gold:
                r.add_template(gold[k]["tpl"])
            ys = {m: r.preset(NL.block_preset(m), x)[0] for m in probes_lib}
            ys.update({k: r.preset(gold[k]["preset"], x)[0] for k in probes_gold})
            h = r.eng.host
            return {"ys": ys, "reprepares": h.canary_reprepares, "fixed": h.canary_fixed,
                    "respawns": h.canary_respawns, "canary_null_db": h.canary_null_db,
                    "sentinel": r.eng.stats()}
        finally:
            r.close()

    ref = one(0)
    with cf.ThreadPoolExecutor(4) as ex:
        hosts = list(ex.map(one, range(a.hosts)))
    votes1 = [v["n"] for v in nat._canary_votes(f)]
    rows, bad = [], []
    for i, h in enumerate(hosts):
        nulls = {k: round(nat.null_db(ref["ys"][k], y), 1) for k, y in h["ys"].items()}
        ok = all(v <= nat.CANARY_THRESHOLD_DB for v in nulls.values())
        rows.append({"host": i, "nulls": nulls, "ok": ok, **{k: h[k] for k in ("reprepares", "fixed", "respawns")},
                     "sentinel_drifts": h["sentinel"].get("sentinel_drifts")})
        if not ok:
            bad.append(i)
    flagged = sum(1 for r in rows if r["fixed"] or r["respawns"])
    unexplained = len(bad) + sum(1 for r in rows if r["sentinel_drifts"])
    wall = time.perf_counter() - t0
    wn = worst(v for r in rows for v in r["nulls"].values())
    res = {"rules": "DECISIONS 2026-10-17 (native-engine, T103) rule 4", "hosts": rows, "votes_before": votes0,
           "votes_after": votes1, "flagged": flagged, "unexplained": unexplained, "worst_null_db": wn,
           "wall_s": round(wall, 1)}
    fp = save("canary", res)
    print(f"canary: {a.hosts} hosts, {flagged} came up in mode B and were corrected "
          f"({sum(r['fixed'] for r in rows)} in place, {sum(r['respawns'] for r in rows)} respawns); "
          f"probes worst {wn} dB vs reference; unexplained {unexplained}")
    ledger("smoke", "canary_unexplained", unexplained, None, wall,
           f"T103 rule 4: {a.hosts} host opens (4 at a time) + 1 reference; probes {probes_lib + probes_gold}; "
           f"accepted hosts vs reference worst {wn} dB; {flagged} hosts came up in Native's numeric mode B and were "
           f"corrected ({sum(r['fixed'] for r in rows)} re-prepared in place, {sum(r['reprepares'] for r in rows)} "
           f"re-prepares, {sum(r['respawns'] for r in rows)} respawns); votes {votes0} -> {votes1}; cause: each "
           f"prepareToPlay picks mode A or B (1-ulp difference amplified by the chain); file {fp.relative_to(ROOT)}")
    return 0 if unexplained == 0 else 1


# --------------------------------------------------------------------------- check 5: sentinel


def cmd_sentinel(a) -> int:
    t0 = time.perf_counter()
    os.environ["TONEMATCH_NATIVE_SENTINEL_EVERY"] = str(a.every)
    x = load_di(1.0)
    r = Renderer("sentinel")
    try:
        e = r.eng
        p = NL.block_preset("HD2_ReverbGanymede")
        y0 = r.preset(p, x)[0]
        for _ in range(a.every - 1):
            r.preset(p, x)
        before = e.stats()
        forced = 0
        ref = e._sentinel_ref
        for _ in range(12):  # push the host into the other numeric mode behind the engine's back
            e.host.call("reprepare")
            forced += 1
            e._sentinel_raw()  # the first render after a re-prepare may be a transient
            if all(nat.null_db(ref, e._sentinel_raw()) > nat.CANARY_THRESHOLD_DB for _ in range(2)):
                break
        e._restore_after_borrow()
        in_b = nat.null_db(ref, e._sentinel_raw()) > nat.CANARY_THRESHOLD_DB
        e._restore_after_borrow()
        y_b = r.preset(p, x)[0]  # the next render triggers the due check
        after = e.stats()
        y1 = r.preset(p, x)[0]
    finally:
        r.close()
    detected = after["sentinel_drifts"] - before["sentinel_drifts"]
    corrected = (after["sentinel_fixed"] + after["sentinel_recycles"]) - (before["sentinel_fixed"] + before["sentinel_recycles"])
    n_after = nat.null_db(y0, y1)
    n_b = nat.null_db(y0, y_b)
    ok = bool(in_b and detected == 1 and corrected == 1 and n_after <= nat.CANARY_THRESHOLD_DB)
    wall = time.perf_counter() - t0
    res = {"every": a.every, "forced_reprepares": forced, "host_in_other_mode": in_b, "stats_before": before,
           "stats_after": after, "detected": detected, "corrected": corrected,
           "render_after_fix_vs_before_db": round(n_after, 1), "render_at_check_vs_before_db": round(n_b, 1), "ok": ok}
    f = save("sentinel", res)
    print(json.dumps(res, indent=1, default=str))
    ledger("smoke", "sentinel_forced_drift_detected", int(ok), after["renders"], wall,
           f"T103 rule 5: sentinel every {a.every} renders; host forced into the other numeric mode "
           f"({forced} re-prepares, confirmed {in_b}); detected {detected}, corrected {corrected} "
           f"(fixed {after['sentinel_fixed']}, recycled {after['sentinel_recycles']}); Ganymede render after the fix vs "
           f"before {n_after:.1f} dB, the render at the check {n_b:.1f} dB; file {f.relative_to(ROOT)}")
    return 0 if ok else 1


def main() -> int:
    global LEDGER
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-ledger", action="store_true", help="shakedown: print ledger rows, do not append")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("audit")
    s.add_argument("--seed", type=int, default=103)
    s.add_argument("--per-cat", type=int, default=13)
    s.add_argument("--extra", type=int, default=3)
    s.add_argument("--no-baked", action="store_true")
    s.add_argument("--no-diagnose", action="store_true")
    sub.add_parser("baked")
    s = sub.add_parser("landing", help="where missed CC steps land (reads audit.json's diagnosis)")
    s.add_argument("--cases", default="", help="model:param:step,... instead of audit.json's diagnosis")
    s = sub.add_parser("repeat")
    s.add_argument("--seed", type=int, default=1031)
    s.add_argument("--warmup", type=int, default=None, help="TONEMATCH_NATIVE_WARMUP for the hosts (0 = pre-T103)")
    s.add_argument("--legacy", action="store_true", help="pre-T103 host: warm-up 0 and no amp latch")
    s = sub.add_parser("canary")
    s.add_argument("--hosts", type=int, default=24)
    s = sub.add_parser("sentinel")
    s.add_argument("--every", type=int, default=10)
    a = ap.parse_args()
    LEDGER = not a.no_ledger
    return {"audit": cmd_audit, "baked": cmd_baked, "repeat": cmd_repeat, "canary": cmd_canary,
            "sentinel": cmd_sentinel, "landing": cmd_landing}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
