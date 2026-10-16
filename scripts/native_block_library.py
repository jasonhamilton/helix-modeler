#!/usr/bin/env python3
"""Capture the Native single-block library (T048) and check it (fresh-host replay smoke).

``capture``: every Stomp-compatible model of the HX Edit catalog (routing blocks and
``native_unavailable()`` models left out), breadth first: Distortion, Delay, Reverb; then Dynamics,
EQ, Modulation; then amps, cabs and the rest. Each model is dropped alone at catalog defaults with
the frozen values written in (Hum/Ripple 0, cab EarlyReflections 0), every param and the bypass on
a MIDI CC, and every CC checked (``verify_mode="auto"``: truth check for >= 8 params, else one
render per CC; ``known_live`` off, so nothing is taken on trust). Every template is replayed in a
fresh host after its session and its in-session render is kept in ``refs/``. Resumable: models in
the index are skipped unless ``--force``.

``smoke``: a random sample of templates loaded by model id through ``NativeEngine`` in one fresh
host and rendered against their capture-time refs.

Layout: ``tonematch.engines.native_library`` (``cache/native/library/block-library-v1.1/``).
``capture --narrowed`` re-captures only templates whose CC ranges are not the narrowed ones (T072).
Needs a logged-in GUI session (Native's editor shows during drops) and the host venv.

  .venv/bin/python scripts/native_block_library.py capture --sessions 3 --max-hosts 6
  .venv/bin/python scripts/native_block_library.py smoke --n 20
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
HOSTVENV = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
if "TONEMATCH_NATIVE_PYTHON" not in os.environ and HOSTVENV.exists():
    os.environ["TONEMATCH_NATIVE_PYTHON"] = str(HOSTVENV)

import numpy as np

from tonematch.catalog import load_catalog
from tonematch.engines import native as nat
from tonematch.engines import native_library as lib


def host_count() -> int:
    """Native host processes running on this machine (any agent's)."""
    r = subprocess.run(["pgrep", "-f", "tonematch.engines.native_host"], check=False,
                       capture_output=True, text=True)
    return len([x for x in r.stdout.split() if x.strip()])


def sessions_allowed(want: int, max_hosts: int) -> int:
    others = host_count()
    k = max(0, min(want, max_hosts - others))
    print(json.dumps({"native_hosts_running": others, "sessions": k, "max_hosts": max_hosts}), flush=True)
    return k


def narrowed_stale(catalog: dict, base, idx: dict) -> set[str]:
    """Library models whose captured template's ranges differ from ``native.cc_ranges`` now."""
    out = set()
    tdir = lib.library_templates()
    for m in lib.library_models(catalog):
        p = lib.block_preset(m, catalog, base)
        want = nat.cc_ranges(p, catalog) or {}
        if want == (nat.catalog_ranges(p, catalog) or {}):
            continue  # no narrowing applies to this model's preset
        e = idx["models"].get(m)
        t = lib._read_template(tdir, e["template"]) if e else None
        have = {r: tuple(v) for r, v in (t.ranges if t else {}).items()}
        if t is None or any(have.get(r) != tuple(v) for r, v in want.items() if r in t.map):
            out.add(m)
    return out


def cmd_capture(a) -> dict:
    catalog = load_catalog()
    plugin, start = str(nat.locate_plugin()), nat.start_state_path().read_bytes()
    base = lib.base_preset()
    idx = lib.load_index()
    idx.update({"version": lib.VERSION, "catalog_version": (catalog.get("_source") or {}).get("hxedit_version"),
                "native_version": nat.plugin_version(plugin), "base": str(lib.BASE_HLX.relative_to(ROOT)),
                "frozen": {f"{c}.{n}": v for (c, n), v in lib.frozen_values().items()},
                "order": [list(t) for t in lib.TIERS]})
    idx.setdefault("models", {})
    idx.setdefault("failed", {})
    bad0 = nat.invalid_models()
    tiers = [tuple(t.split(",")) for t in a.tiers.split(";")] if a.tiers else lib.TIERS
    tdir, rdir = lib.library_templates(), lib.library_dir() / "refs"
    rdir.mkdir(parents=True, exist_ok=True)
    only = set(a.models.split(",")) if a.models else None
    # --narrowed (T072): models whose library preset now gets a narrower CC range than its
    # template records (``native.cc_ranges``); they are re-captured, everything else is skipped.
    stale = narrowed_stale(catalog, base, idx) if a.narrowed else set()
    if a.narrowed:
        only = stale if only is None else only & stale
        print(json.dumps({"narrowed_stale": len(stale)}), flush=True)
    runs, t_all = [], time.perf_counter()
    for tier in tiers:
        mids = [m for m in lib.library_models(catalog, (tier,)) if a.force or m not in idx["models"]
                or m in stale]
        if only is not None:
            mids = [m for m in mids if m in only]
        if a.limit:
            mids = mids[: a.limit]
        if not mids:
            continue
        k = sessions_allowed(a.sessions, a.max_hosts)
        while k == 0:
            time.sleep(10)
            k = sessions_allowed(a.sessions, a.max_hosts)
        presets = [lib.block_preset(m, catalog, base) for m in mids]
        refs: dict = {}
        stats: dict = {}
        t0 = time.perf_counter()
        outs = nat.capture_chains_parallel(presets, plugin, sessions=k, stats=stats, start_state=start,
                                           templates_dir=tdir, catalog=catalog, known_live=False,
                                           verify_mode="auto", replay_every=1, replay_refs=refs)
        wall = time.perf_counter() - t0
        ok = 0
        for m, p, t in zip(mids, presets, outs, strict=True):
            cat = catalog["models"][m]["category"]
            if isinstance(t, Exception):
                idx["failed"][m] = {"category": cat, "error": str(t)}
                continue
            idx["failed"].pop(m, None)
            idx["models"][m] = lib.entry(m, cat, t, p)
            stem = nat.template_stem(t)
            if stem in refs:
                y, vals = refs[stem]
                np.savez(rdir / f"{stem}.npz", y=y.astype(np.float32), vals=json.dumps(vals))
                idx["models"][m]["ref"] = True
            ok += 1
        sess = stats["sessions"] if isinstance(stats.get("sessions"), list) else [stats]
        replay = {n: v for s in sess for n, v in (s.get("replay_null_db") or {}).items()}
        run = {"tier": list(tier), "chains": len(mids), "captured": ok, "failed": len(mids) - ok,
               "sessions": k, "wall_s": round(wall, 2), "per_chain_s": round(wall / len(mids), 3),
               "replay_worst_db": max(replay.values()) if replay else None, "replayed": len(replay),
               "rejected": [r for s in sess for r in s.get("rejected", [])],
               "sessions_stats": [{x: y for x, y in s.items() if x not in ("chains", "replay_null_db")}
                                  for s in sess],
               "chains_stats": [c for s in sess for c in s.get("chains", [])]}
        print(json.dumps({x: y for x, y in run.items() if x not in ("sessions_stats", "chains_stats")}), flush=True)
        runs.append(run)
        lib.save_index(idx)
    total = time.perf_counter() - t_all
    new_bad = sorted(nat.invalid_models() - bad0)
    by_cat = collections.Counter(e["category"] for e in idx["models"].values())
    failed = collections.Counter(e["category"] for e in idx["failed"].values())
    res = {"wall_s": round(total, 2), "chains": sum(r["chains"] for r in runs), "runs": runs,
           "captured_by_category": dict(by_cat), "failed_by_category": dict(failed),
           "new_invalid_models": new_bad, "hosts_after": host_count(),
           "load_avg": [round(v, 2) for v in os.getloadavg()]}
    print(json.dumps({x: y for x, y in res.items() if x != "runs"}), flush=True)
    return res


def cmd_time_variant(a) -> dict:
    """Models whose fresh-host replay did not null (random or free-running blocks): capture them
    with the replay check reporting only, then render each template three times in ONE fresh host.
    If that host cannot repeat itself either, the block is time-variant, not badly captured: the
    template is kept and flagged ``time_variant`` with both nulls."""
    catalog = load_catalog()
    plugin, start = str(nat.locate_plugin()), nat.start_state_path().read_bytes()
    idx = lib.load_index()
    mids = a.models.split(",") if a.models else sorted(
        m for m, e in idx["failed"].items() if "fresh host" in e.get("error", ""))
    if not mids or sessions_allowed(1, a.max_hosts) < 1:
        return {"models": mids, "skipped": True}
    presets = [lib.block_preset(m, catalog) for m in mids]
    refs: dict = {}
    stats: dict = {}
    t0 = time.perf_counter()
    outs = nat.capture_chains(presets, plugin, stats=stats, start_state=start,
                              templates_dir=lib.library_templates(), catalog=catalog, known_live=False,
                              verify_mode="auto", replay_every=1, replay_threshold_db=0.0, replay_refs=refs)
    wall = time.perf_counter() - t0
    rdir, rows = lib.library_dir() / "refs", []
    h = nat.HostProcess(plugin, None, initial_state=start)
    try:
        for m, p, t in zip(mids, presets, outs, strict=True):
            if isinstance(t, Exception):
                rows.append({"model": m, "error": str(t)})
                continue
            stem = nat.template_stem(t)
            y, vals = refs[stem]
            np.savez(rdir / f"{stem}.npz", y=y.astype(np.float32), vals=json.dumps(vals))
            rs = [nat._render_state(h, t.state, vals, 3.0) for _ in range(3)]
            rep_db = max(nat.null_db(rs[0], rs[1]), nat.null_db(rs[0], rs[2]))
            fresh_db = nat.null_db(rs[0], y)
            e = lib.entry(m, catalog["models"][m]["category"], t, p)
            e.update(ref=True, time_variant=bool(rep_db > -90.0), same_host_repeat_null_db=round(rep_db, 1),
                     fresh_replay_null_db=round(fresh_db, 1))
            if e["time_variant"] or fresh_db <= -90.0:  # time-variant, or the replay passes now
                if not e["time_variant"]:                # (an earlier fresh host was the odd one)
                    for k in ("time_variant", "same_host_repeat_null_db", "fresh_replay_null_db"):
                        e.pop(k, None)
                idx["models"][m] = e
                idx["failed"].pop(m, None)
            else:  # repeats in one host but not across hosts: a real capture problem, keep failed
                idx["failed"][m] = {"category": e["category"], "error": "fresh-host replay differs but the "
                                    f"host repeats itself ({rep_db:.1f} dB); template not trusted"}
                nat._discard(t, lib.library_templates())
            rows.append({"model": m, "time_variant": bool(e.get("time_variant")),
                         "same_host_repeat_null_db": round(rep_db, 1), "fresh_replay_null_db": round(fresh_db, 1),
                         "verified": e["verified"], "ccs": e["ccs"]})
            print(json.dumps(rows[-1]), flush=True)
    finally:
        h.close()
    lib.save_index(idx)
    return {"models": mids, "capture_wall_s": round(wall, 2), "rows": rows, "hosts_after": host_count()}


def cmd_smoke(a) -> dict:
    catalog = load_catalog()
    idx = lib.load_index()
    # time-variant blocks cannot null against any earlier render; they are not sampled
    have = sorted(m for m, e in idx["models"].items() if e.get("ref") and not e.get("time_variant"))
    rng = random.Random(a.seed)
    pick = rng.sample(have, min(a.n, len(have)))
    if sessions_allowed(1, a.max_hosts) < 1:
        raise SystemExit("too many Native hosts running")
    rdir = lib.library_dir() / "refs"
    rows = []
    t0 = time.perf_counter()
    eng = nat.NativeEngine(templates_dir=lib.library_templates(), autocapture=False)
    try:
        for m in pick:
            p = lib.block_preset(m, catalog)
            tpl = lib.template_for(m)
            eng.load(p)  # by structure + baked fingerprint, as search will
            assert eng.current_template is not None and nat.template_stem(eng.current_template) == \
                idx["models"][m]["template"], m
            z = np.load(rdir / f"{nat.template_stem(tpl)}.npz")
            vals = json.loads(str(z["vals"]))
            y = nat._render_state(eng.host, tpl.state, vals, 3.0)
            ref = z["y"]
            nd = nat.null_db(y, ref) if np.any(ref) else (-300.0 if not np.any(y) else 0.0)
            rows.append({"model": m, "category": idx["models"][m]["category"], "matches": lib.matches(tpl, p),
                         "null_db": round(float(nd), 1), "rms_db": round(nat._rms_db(ref), 1)})
            print(json.dumps(rows[-1]), flush=True)
    finally:
        eng.close()
    wall = time.perf_counter() - t0
    return {"seed": a.seed, "n": len(rows), "wall_s": round(wall, 2),
            "worst_null_db": max(r["null_db"] for r in rows) if rows else None,
            "all_match": all(r["matches"] for r in rows), "rows": rows, "hosts_after": host_count()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("capture")
    s.add_argument("--sessions", type=int, default=3)
    s.add_argument("--max-hosts", type=int, default=6, help="total native_host processes on the machine")
    s.add_argument("--limit", type=int, default=0, help="per tier, for trials")
    s.add_argument("--tiers", default="", help='e.g. "drive,delay,reverb;comp,eq,mod" (default: all)')
    s.add_argument("--force", action="store_true")
    s.add_argument("--models", default="", help="comma list: capture only these")
    s.add_argument("--narrowed", action="store_true",
                   help="only models whose template lacks the narrowed CC ranges (T072)")
    s = sub.add_parser("time-variant")
    s.add_argument("--models", default="", help="comma list (default: index failures from the replay check)")
    s.add_argument("--max-hosts", type=int, default=6)
    s = sub.add_parser("smoke")
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--seed", type=int, default=48)
    s.add_argument("--max-hosts", type=int, default=6)
    a = ap.parse_args()
    res = {"capture": cmd_capture, "time-variant": cmd_time_variant, "smoke": cmd_smoke}[a.cmd](a)
    out = lib.library_dir() / "runs"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{a.cmd}.json"
    f.write_text(json.dumps(res, indent=1))
    print("wrote", f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
