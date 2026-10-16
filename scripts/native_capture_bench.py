#!/usr/bin/env python3
"""Time Native chain captures step by step: the committed one-host-per-file flow ("before")
against the one-editor-session flow (``capture_chains``), serial or in parallel sessions.

Chains: single-block presets built from the HX catalog's defaults (a fixed spread over the
categories, as the block library will capture them) plus the 8-block C8 chain. Every capture
writes its template to a scratch directory under ``cache/native/capture_bench/`` (never the
engine's template store). ``check`` replays a sample of templates in a FRESH host and nulls the
render against the same template from the other flow, and the CC route against drop truth.

Needs a logged-in GUI session (Native's editor shows during drops) and
``TONEMATCH_NATIVE_PYTHON`` pointing at a venv with pedalboard + PyObjC.

  TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python \\
    .venv/bin/python scripts/native_capture_bench.py before --n 3
  ... after --n 16 --sessions 1          # one session, 16 chains
  ... after --n 16 --sessions 3          # three sessions in parallel
  ... check --a <run-dir> --b <run-dir>  # fresh-process replay nulls
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from tonematch.catalog import load_catalog
from tonematch.engines import native as nat
from tonematch.engines.base import SAMPLE_RATE
from tonematch.preset import Preset, Slot, read_hlx

OUT = ROOT / "cache" / "native" / "capture_bench"
K_HLX = ROOT / "cache" / "stomp" / "inject" / "K.hlx"
C8_HLX = ROOT / "cache" / "blockserial" / "C8" / "8f50d3d2e224_BS_C8.hlx"
BLOCK_TYPE = {"amp": 1, "cab": 2, "delay": 7, "reverb": 7}
# Breadth before depth (human, 2026-10-15): distortion, delay, reverb first; then dynamics, EQ,
# modulation. Amps and cabs keep their current coverage (pass --cats to include them).
CATS = ("drive", "delay", "reverb", "comp", "eq", "mod")
ALL_CATS = (*CATS, "other", "amp", "cab")


def single_block(model_id: str, catalog: dict, base: Preset) -> Preset:
    """One block at catalog defaults, mono, on path 0 (as the block library captures it)."""
    m = catalog["models"][model_id]
    params, extra = {}, {}
    for name, p in (m.get("params") or {}).items():
        if p.get("default") is not None:
            (extra if name.startswith("@") else params)[name] = p["default"]
    if m.get("stereo") and m.get("mono"):
        extra["@stereo"] = False
    extra = {k: v for k, v in extra.items() if k not in ("@cab", "@type", "@position", "@enabled")}
    extra["@type"] = BLOCK_TYPE.get(m["category"], 0)
    extra.setdefault("@path", 0)
    p = copy.deepcopy(base)
    p.name, p.snapshots = f"LIB {model_id[4:][:14]}", []
    p.slots = [Slot(category=m["category"], model_id=model_id, params=params, enabled=True,
                    key="block0", position=0, extra=extra)]
    return p


def is_routing(model_id: str) -> bool:
    """Not blocks a file can hold alone: split, join, input and output models (path structure),
    and the IR cabs' pan twins (``...WithPan``, the second cab of a dual cab block; dropped alone
    Native answers "Object not found", code -1000021, 2026-10-16)."""
    return "AppDSPFlow" in model_id or model_id.endswith("WithPan")


def library_sample(n: int, catalog: dict, offset: int = 0, cats=CATS) -> list[Preset]:
    """``n`` Stomp models (0: all) spread round-robin over the categories (deterministic);
    routing models are left out."""
    base = read_hlx(K_HLX)
    by_cat = {c: sorted(k for k, v in catalog["models"].items()
                        if v.get("stomp") and v.get("category") == c and not is_routing(k))
              for c in cats}
    n = min(n or 10**9, sum(max(0, len(v) - offset) for v in by_cat.values()))
    out, i = [], offset
    while len(out) < n:
        for c in cats:
            if i < len(by_cat[c]) and len(out) < n:
                out.append(single_block(by_cat[c][i], catalog, base))
        i += 1
    return out


def chains(a, catalog) -> list[Preset]:
    out = library_sample(a.n, catalog, a.offset, tuple(a.cats.split(",")))
    if a.c8:
        c8 = read_hlx(C8_HLX)
        c8.name = "C8"
        out.append(c8)
    return out


# ----------------------------------------------------------------------------- the old flow


def capture_before(preset, plugin: str, start: bytes, tdir: Path, catalog: dict) -> dict:
    """The committed (HEAD) capture_chain, step by step: a new Helix-mode host per file,
    reload into the start state, editor_drop with fixed 8 s + 4 s waits, a second
    reload_with_state for verification, then one render per mapped CC."""
    t: dict = {}
    t0 = tick = time.perf_counter()

    def lap(k):
        nonlocal tick
        now = time.perf_counter()
        t[k] = round(now - tick, 3)
        tick = now

    key, _cid, amap, rng, path = nat._capture_doc(preset, catalog, True)
    lap("write_file_s")
    host = nat.HostProcess(plugin, None)
    lap("host_open_s")
    try:
        host.call("gui_available")
        host.call("reload_with_state", start, path=plugin)
        lap("reload_start_s")
        before = host.call("get_state")[1]
        res = host.call("editor_drop", path=str(path.resolve()), open_wait_s=8.0, load_wait_s=4.0)[0]
        lap("editor_drop_s")
        t["editor_s"] = res.get("editor_s")
        state = host.call("get_state")[1]
        if state == before or res.get("dialogs"):
            raise RuntimeError(f"drop failed: {res}")
        host.call("reload_with_state", state, path=plugin)
        lap("reload_state_s")
        vst: dict = {}
        verified = nat._verify_ccs(host, plugin, None, state, preset, amap, rng, live_db={}, stats=vst)
        lap("verify_s")
        t.update(vst)
        t["mem_mb"] = host.mem_mb
    finally:
        host.close()
        lap("close_s")
    baked, h = nat.baked_fingerprint(preset, amap, verified)
    nat.ChainTemplate(key, state, amap, rng, name=preset.name, verified=verified, baked=baked,
                      baked_hash=h).save(tdir)
    lap("save_s")
    t["total_s"] = round(time.perf_counter() - t0, 3)
    return {"name": preset.name, "blocks": len(preset.blocks()), "ccs": len(amap), **t}


def cmd_before(a) -> dict:
    catalog = load_catalog()
    plugin, start = str(nat.locate_plugin()), nat.start_state_path().read_bytes()
    tdir = a.run / "templates"
    rows = []
    for p in chains(a, catalog):
        r = capture_before(p, plugin, start, tdir, catalog)
        print(json.dumps(r), flush=True)
        rows.append(r)
    return {"flow": "before", "rows": rows}


def cmd_after(a) -> dict:
    catalog = load_catalog()
    plugin, start = str(nat.locate_plugin()), nat.start_state_path().read_bytes()
    tdir = a.run / "templates"
    ps = chains(a, catalog)
    k = max(1, min(a.sessions, nat.max_hosts_by_memory()))
    parts = [ps[i::k] for i in range(k)]
    stats = [{} for _ in parts]
    t0 = time.perf_counter()

    def one(i):
        return nat.capture_chains(parts[i], plugin, start_state=start, templates_dir=tdir,
                                  catalog=catalog, known_live=a.known_live, stats=stats[i],
                                  verify_mode=a.verify, replay_every=a.replay_every,
                                  **({"poll_s": a.poll_s} if a.poll_s else {}),
                                  **({"stable_polls": a.stable_polls} if a.stable_polls else {}),
                                  **({"settle_s": a.settle_s} if a.settle_s is not None else {}))

    with cf.ThreadPoolExecutor(k) as ex:
        outs = list(ex.map(one, range(k)))
    wall = time.perf_counter() - t0
    fails = [str(o) for out in outs for o in out if isinstance(o, Exception)]
    res = {"flow": "after", "sessions": k, "verify": a.verify, "known_live": a.known_live,
           "chains": len(ps), "wall_s": round(wall, 2), "per_chain_s": round(wall / len(ps), 2),
           "failures": fails, "sessions_stats": stats}
    print(json.dumps({k2: v for k2, v in res.items() if k2 != "sessions_stats"}), flush=True)
    return res


# ----------------------------------------------------------------------------- correctness


def _di(seconds: float = 4.0) -> np.ndarray:
    di = ROOT / "cache" / "parity" / "di_10s_tail.wav"
    if di.is_file():
        import soundfile as sf

        x, _ = sf.read(di, dtype="float32")
        return x[: int(seconds * SAMPLE_RATE)]
    return nat.test_signal(seconds)


def _fresh_render(plugin: str, start: bytes, state: bytes, x: np.ndarray, params=None) -> np.ndarray:
    h = nat.HostProcess(plugin, None, initial_state=start)
    try:
        h.call("set_state", state)
        if params:
            h.call("set_params", values=params)
        _, y = h.call("render", x.astype("<f4").tobytes(), reset=True)
        return np.frombuffer(y, dtype="<f4")
    finally:
        h.close()


def cmd_check(a) -> dict:
    """Fresh-process replay of templates from run A and run B (same presets): null A vs B."""
    plugin, start = str(nat.locate_plugin()), nat.start_state_path().read_bytes()
    pre = 3 * SAMPLE_RATE
    x = np.concatenate([np.zeros(pre, np.float32), _di()])
    va = {t.name: t for vs in nat.ChainTemplate.load_variants(a.a / "templates").values() for t in vs}
    vb = {t.name: t for vs in nat.ChainTemplate.load_variants(a.b / "templates").values() for t in vs}
    names = sorted(set(va) & set(vb))[: a.sample]
    rows = []
    for n in names:
        ya = _fresh_render(plugin, start, va[n].state, x)[pre:]
        yb = _fresh_render(plugin, start, vb[n].state, x)[pre:]
        r = {"name": n, "null_db": round(nat.null_db(ya, yb), 1),
             "rms_db": round(nat._rms_db(ya), 1),
             "verified_a": len(va[n].verified), "verified_b": len(vb[n].verified), "ccs": len(va[n].map)}
        print(json.dumps(r), flush=True)
        rows.append(r)
    return {"flow": "check", "a": str(a.a), "b": str(a.b), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("before", "after"):
        s = sub.add_parser(name)
        s.add_argument("--n", type=int, default=3, help="single-block library chains (0: every model in --cats)")
        s.add_argument("--offset", type=int, default=0, help="start index within each category")
        s.add_argument("--c8", action="store_true", help="add the 8-block C8 chain")
        s.add_argument("--cats", default=",".join(CATS), help=f"categories in order (all: {','.join(ALL_CATS)})")
        s.add_argument("--tag", default="")
        if name == "after":
            s.add_argument("--sessions", type=int, default=1)
            s.add_argument("--verify", default="per-cc", choices=("auto", "per-cc", "truth", "none"))
            s.add_argument("--replay-every", type=int, default=10)
            s.add_argument("--poll-s", type=float, default=None)
            s.add_argument("--stable-polls", type=int, default=None)
            s.add_argument("--settle-s", type=float, default=None)
            s.add_argument("--known-live", action="store_true")
    s = sub.add_parser("check")
    s.add_argument("--a", type=Path, required=True)
    s.add_argument("--b", type=Path, required=True)
    s.add_argument("--sample", type=int, default=6)
    s.add_argument("--tag", default="")
    a = ap.parse_args()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    a.run = OUT / f"{stamp}_{a.cmd}{'_' + a.tag if a.tag else ''}"
    a.run.mkdir(parents=True, exist_ok=True)
    res = {"before": cmd_before, "after": cmd_after, "check": cmd_check}[a.cmd](a)
    res["load_avg"] = [round(v, 2) for v in __import__("os").getloadavg()]
    (a.run / "result.json").write_text(json.dumps(res, indent=1))
    print("wrote", a.run / "result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
