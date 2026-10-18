#!/usr/bin/env python3
"""T121 live check: the chain builder's Native renderer (``engines.native_chain``) against one instance.

Three chains of 3-4 blocks, one Native host at a time (``NativeRenderPool(workers=1)``):

  A. C8p3 (comp -> drive -> amp) and C8p4 (+ cab), T045's drop-captured whole-chain templates
     (``cache/blockserial/C8p{3,4}_trim16``) against the same blocks rendered block-serially by
     ``native_chain.NativeChainRenderer`` from T045's single-block captures (exact values, so
     only the rendering differs). T045 method: aligned null by cross-correlation, residual above
     20 Hz (4th-order high-pass on both); pass bar from DECISIONS 2026-10-15: -60 dB above 20 Hz,
     and lag 0 (proves the engine's latency trim is counted once). Also: the prefix-cached
     renderer equals the plain ``render_chain`` (same host, reset renders).
  B. Informational: C8p4 block-serial from the block LIBRARY templates (CC-driven values, Hum and
     Ripple 0.5 set by CC) against A's exact-value block-serial render.
  C. amp -> cab -> 70s Chorus -> Ganymede at library starts: no whole-chain template exists, so
     repeatability (two fresh renderers) and cached-vs-direct only.

Refuses to start when the Native hosts already running plus this one exceed ``--max-total``
(default 6: other specialists share the machine). Guitar-TECHS DI only, never song audio.
Outputs (local): cache/chain_render/result.json and wavs.

  .venv/bin/python scripts/chain_render_check.py [--ledger] [--max-total 6]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("TONEMATCH_NATIVE_MANY_HOSTS", "1")

import numpy as np

SR = 48_000
DI = ROOT / "cache" / "parity" / "di_10s_tail.wav"
BS = ROOT / "cache" / "blockserial"
OUT = ROOT / "cache" / "chain_render"
DATASET = "chain-render-v1"
PASS_DB = -60.0
CHAIN_C = [("amp", "HD2_AmpEssexA15"), ("cab", "HD2_Cab1x12BlueBell"), ("mod", "HD2_Chorus70sChorus"),
           ("reverb", "HD2_ReverbGanymede")]


def host_count() -> int:
    """Native host processes (python -m tonematch.engines.native_host), shells excluded."""
    r = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True, check=False)
    n = 0
    for line in r.stdout.splitlines():
        a = line.split()
        if "-m" in a and a.index("-m") + 1 < len(a) and a[a.index("-m") + 1] == "tonematch.engines.native_host":
            n += 1
    return n


def hp20(x: np.ndarray) -> np.ndarray:
    from scipy.signal import butter, sosfilt

    return sosfilt(butter(4, 20.0, "highpass", fs=SR, output="sos"), np.asarray(x, np.float64))


def compare(ref: np.ndarray, y: np.ndarray) -> dict:
    """T045 method: lag by cross-correlation, null aligned, then above 20 Hz."""
    from blockserial_spike import xcorr_lag

    from tonematch.engines.native import null_db

    n = min(len(ref), len(y))
    a, b = np.asarray(ref[:n], np.float64), np.asarray(y[:n], np.float64)
    lag = xcorr_lag(a.astype(np.float32), b.astype(np.float32), max_lag=480)
    if lag >= 0:
        a2, b2 = a[: n - lag], b[lag:]
    else:
        a2, b2 = a[-lag:], b[: n + lag]
    g = float(np.dot(a2, b2) / max(np.dot(b2, b2), 1e-30))
    ha, hb = hp20(a2), hp20(b2)
    return {"lag_samples": int(lag), "null_db": round(null_db(a2, b2), 2),
            "null_hp20_db": round(null_db(ha, hb), 2), "gain_db": round(20 * np.log10(abs(g) or 1e-30), 4),
            "null_hp20_gain_matched_db": round(null_db(ha, hb * g), 2),
            "ref_rms_dbfs": round(10 * np.log10(np.mean(a ** 2) + 1e-30), 2)}


class ExactLib:
    """A BlockLibrary over T045's per-block presets (their captured values, nothing moves)."""

    def __init__(self, presets):
        self.p = {p.slots[0].model_id: p for p in presets}

    def preset(self, model, values):
        import copy

        p = copy.deepcopy(self.p[model])
        p.slots[0].params.update(values)
        return p

    def shortlist(self, category, n):
        return [m for m, p in self.p.items() if p.slots[0].category == category][:n]

    def free_params(self, model):
        return {}


def specs(presets):
    from tonematch.search.chain_builder import BlockSpec

    return [BlockSpec(p.slots[0].category, p.slots[0].model_id) for p in presets]


def ledger(metric: str, value: float, renders: int, wall: float, notes: str) -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "smoke", "--engine", "native",
                    "--dataset", DATASET, "--metric", metric, "--value", f"{value:.2f}", "--renders", str(renders),
                    "--wall-clock-s", f"{wall:.1f}", "--notes", notes.replace(",", ";")],
                   check=True, env={**os.environ, "TONEMATCH_AGENT": "native-engine"})


def write_wav(path: Path, y: np.ndarray) -> None:
    import soundfile as sf

    sf.write(path, np.asarray(y, np.float32), SR, subtype="FLOAT")


def per_block(x: np.ndarray, blocks: list, running: int, to_ledger: bool) -> int:
    """Part D: is the B gap the CC grid? Each block alone on the DI: T045's exact-value capture,
    the library template with the value as given (the engine sends the continuous normalized
    value) and with the value snapped to the CC grid. Equal library renders mean the plugin
    plays the grid value; then exact vs library is the grid's effect alone."""
    from tonematch.engines import native_library as nl
    from tonematch.engines.native import NativeRenderPool, RenderJob, null_db
    from tonematch.engines.native_chain import NativeBlockLibrary

    t_all = time.perf_counter()
    pool = NativeRenderPool(workers=1, templates_dir=BS / "C8" / "templates", autocapture=False, on_demand=False)
    try:
        exact = pool.render_batch([RenderJob(dry=x, preset=b) for b in blocks])
    finally:
        pool.close()
    raw_lib, snap_lib = NativeBlockLibrary(snap=False), NativeBlockLibrary(snap=True)
    jobs, rows = [], []
    for b in blocks:
        m = b.slots[0].model_id
        keep = raw_lib.preset(m, {}).slots[0].params
        vals = {k: v for k, v in b.slots[0].params.items() if k in keep}
        jobs += [raw_lib.preset(m, vals), snap_lib.preset(m, vals)]
        rows.append({"model": m, "moved_by_snap": {k: [v, snap_lib.preset(m, vals).slots[0].params[k]]
                                                   for k, v in vals.items()
                                                   if snap_lib.preset(m, vals).slots[0].params[k] != v}})
    pool = NativeRenderPool(workers=1, templates_dir=nl.library_templates(), autocapture=False, on_demand=False)
    try:
        ys = pool.render_batch([RenderJob(dry=x, preset=p) for p in jobs])
    finally:
        pool.close()
    for i, r in enumerate(rows):
        e, lr, ls = exact[i], ys[2 * i], ys[2 * i + 1]
        r.update({"exact_vs_library_db": round(null_db(e, lr), 2), "library_vs_snapped_db": round(null_db(lr, ls), 2),
                  "exact_vs_snapped_db": round(null_db(e, ls), 2)})
        print("D", json.dumps(r), flush=True)
    res = {"per_block": rows, "hosts_running_at_start": running, "wall_s": round(time.perf_counter() - t_all, 1)}
    (OUT / "per_block.json").write_text(json.dumps(res, indent=1))
    if to_ledger:
        worst = max(r["library_vs_snapped_db"] for r in rows)
        ledger("library_raw_vs_snapped_null_db", worst, 3 * len(rows), res["wall_s"],
               "T121 part D: C8p4 blocks alone on the DI; library template with the value as given vs snapped "
               "to the CC grid (worst of 4); exact capture vs library per block: "
               + " ".join(f"{r['model'][4:]}:{r['exact_vs_library_db']}" for r in rows) + "; 1 host")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-total", type=int, default=6, help="refuse when running hosts + 1 exceed this")
    ap.add_argument("--di", type=Path, default=DI)
    ap.add_argument("--ledger", action="store_true")
    ap.add_argument("--per-block", action="store_true",
                    help="only part D: each C8p4 block alone, T045 exact capture vs library template "
                         "(values as given, and snapped to the CC grid)")
    a = ap.parse_args()
    running = host_count()
    if running + 1 > a.max_total:
        print(f"{running} Native hosts running; +1 would exceed {a.max_total}. Not starting.")
        return 2
    import soundfile as sf
    from blockserial_spike import configs

    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as nl
    from tonematch.engines.native import ChainTemplate, NativeRenderPool, RenderJob, locate_plugin
    from tonematch.engines.native_chain import NativeBlockLibrary, NativeChainRenderer, render_chain
    from tonematch.preset import read_hlx
    from tonematch.search.chain_builder import BlockSpec

    if locate_plugin() is None:
        print("Helix Native not found")
        return 2
    x, sr = sf.read(a.di, dtype="float32")
    if sr != SR or x.ndim != 1:
        raise SystemExit(f"{a.di}: need mono {SR} Hz")
    catalog = load_catalog()
    OUT.mkdir(parents=True, exist_ok=True)
    blocks = configs(catalog)["C8"]["serial"]
    if a.per_block:
        return per_block(x, blocks[:4], running, a.ledger)
    res: dict = {"di": str(a.di.relative_to(ROOT)), "hosts_running_at_start": running, "pass_db": PASS_DB}
    t_all = time.perf_counter()

    # ---- A: block-serial (T045 exact single-block captures) vs the whole-chain capture
    pool = NativeRenderPool(workers=1, templates_dir=BS / "C8" / "templates", autocapture=False, on_demand=False)
    try:
        wholes = {}
        for name, n in (("C8p3", 3), ("C8p4", 4)):
            d = BS / f"{name}_trim16"
            whole = read_hlx(next(d.glob("*.hlx")), catalog)
            tpls = [t for ts in ChainTemplate.load_variants(d / "templates").values() for t in ts]
            assert len(tpls) == 1, (name, len(tpls))
            pool.use_template(tpls[0])
            wholes[name] = (whole, n)
        t0 = time.perf_counter()
        ys = pool.render_batch([RenderJob(dry=x, preset=w) for w, _ in wholes.values()])
        res["whole_render_s"] = round(time.perf_counter() - t0, 2)
        lib = ExactLib(blocks[:4])
        rend = NativeChainRenderer(pool, lib, x, cap=100)
        t0 = time.perf_counter()
        cached = rend.render_batch([specs(blocks[:n]) for _, n in wholes.values()])
        res["serial_cached_s"] = round(time.perf_counter() - t0, 2)
        res["serial_cached_block_renders"] = rend.spent
        res["engine_trim"] = rend.already_trimmed
        serial_exact = {}
        for (name, (_, n)), y_whole, y_cached in zip(wholes.items(), ys, cached, strict=True):
            t0 = time.perf_counter()
            y_direct = render_chain(pool, blocks[:n], x)
            r = compare(y_whole, y_cached)
            r["cached_vs_direct_null_db"] = round(compare(y_direct, y_cached)["null_db"], 2)
            r["direct_s"] = round(time.perf_counter() - t0, 2)
            r["pass"] = bool(r["null_hp20_db"] <= PASS_DB and r["lag_samples"] == 0)
            res[name] = r
            serial_exact[name] = y_cached
            write_wav(OUT / f"{name}_whole.wav", y_whole)
            write_wav(OUT / f"{name}_serial.wav", y_cached)
            print(name, json.dumps(r), flush=True)
        res["pool_stats_A"] = pool.stats()
    finally:
        pool.close()

    # ---- B and C: block library templates
    pool = NativeRenderPool(workers=1, templates_dir=nl.library_templates(), autocapture=False, on_demand=False)
    try:
        nlib = NativeBlockLibrary(snap=False)
        p4 = [nlib.preset(b.slots[0].model_id, {k: v for k, v in b.slots[0].params.items()
                                                 if k in nlib.preset(b.slots[0].model_id, {}).slots[0].params})
              for b in blocks[:4]]
        t0 = time.perf_counter()
        y_lib = render_chain(pool, p4, x)
        r = compare(serial_exact["C8p4"], y_lib)
        r["s"] = round(time.perf_counter() - t0, 2)
        res["B_C8p4_library_vs_exact"] = r
        print("B", json.dumps(r), flush=True)

        clib = NativeBlockLibrary()
        ch = [BlockSpec(c, m).with_values({k: s for k, (_, _, s) in clib.free_params(m).items()}) for c, m in CHAIN_C]
        t0 = time.perf_counter()
        r1 = NativeChainRenderer(pool, clib, x, cap=10).render_batch([ch])[0]
        t_c = time.perf_counter() - t0
        r2 = NativeChainRenderer(pool, clib, x, cap=10).render_batch([ch])[0]
        direct = render_chain(pool, [clib.preset(s.model, s.values()) for s in ch], x)
        from tonematch.engines.native import null_db

        rc = {"chain": [m for _, m in CHAIN_C], "repeat_null_db": round(null_db(r1, r2), 2),
              "cached_vs_direct_null_db": round(null_db(r1, direct), 2), "s_per_chain": round(t_c, 2),
              "s_per_block": round(t_c / len(ch), 3), "out_rms_dbfs": round(10 * np.log10(np.mean(r1.astype(np.float64) ** 2) + 1e-30), 2)}
        res["C_amp_cab_chorus_reverb"] = rc
        write_wav(OUT / "C_serial.wav", r1)
        print("C", json.dumps(rc), flush=True)
        res["pool_stats_BC"] = pool.stats()
    finally:
        pool.close()
    res["wall_s"] = round(time.perf_counter() - t_all, 1)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    ok = res["C8p3"]["pass"] and res["C8p4"]["pass"]
    print("PASS" if ok else "FAIL", f"({res['wall_s']} s)")
    if a.ledger:
        dur = len(x) / SR
        for name in ("C8p3", "C8p4"):
            r = res[name]
            ledger("blockserial_hp20_null_db", r["null_hp20_db"], 2 * int(name[-1]) + 1, res["wall_s"],
                   f"T121 {name} NativeChainRenderer vs T045 whole-chain capture; {'PASS' if r['pass'] else 'FAIL'} "
                   f"(bar {PASS_DB} dB above 20 Hz and lag 0); lag {r['lag_samples']}; full-band {r['null_db']} dB; "
                   f"gain {r['gain_db']} dB; cached vs direct {r['cached_vs_direct_null_db']} dB; engine trim "
                   f"{res['engine_trim']} so extra trim 0; 1 host; DI {res['di']} {dur:.0f} s")
        r = res["B_C8p4_library_vs_exact"]
        ledger("library_vs_exact_hp20_null_db", r["null_hp20_db"], 4, r["s"],
               f"T121 informational: C8p4 block-serial from block-library v1.2 templates (CC route) vs T045 exact "
               f"single-block captures; lag {r['lag_samples']}; gain {r['gain_db']} dB; full-band {r['null_db']} dB")
        rc = res["C_amp_cab_chorus_reverb"]
        ledger("repeat_null_db", rc["repeat_null_db"], 3 * len(CHAIN_C), rc["s_per_chain"],
               f"T121 amp-cab-70sChorus-Ganymede at library starts (no whole-chain template); two fresh renderers; "
               f"cached vs direct {rc['cached_vs_direct_null_db']} dB; {rc['s_per_block']} s per block render "
               f"({dur:.0f} s clip; 1 host)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
