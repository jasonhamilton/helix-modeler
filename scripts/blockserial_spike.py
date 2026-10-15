#!/usr/bin/env python3
"""T045 spike: does a chain of single-block Native instances equal the same chain in one instance?

For each configuration, every preset involved (the whole chain, and one preset per block) is
written as an HX Stomp ``.hlx`` with ``tonematch.preset`` and captured by in-process drop
(``capture_chain``, macOS GUI session, ~13 s of editor on screen each; skipped when the capture
is already cached). Then one host (watchdog on) renders the DI through the whole-chain state,
and through each block's state in order, feeding each output into the next. The two renders
are aligned by cross-correlation and compared (null, gain, per-octave residual), and each
instance's latency, Native's global in/out level params and the output level after every
block are recorded.

Configurations (``--configs``):
  K        cache/stomp/inject/K.hlx as is (Amp+Cab block, delay bypassed) vs amp-alone -> cab-alone
  K_split  the same amp and cab as two blocks (@type 1, @type 2) in one instance vs serial
  K_amp    Amp+Cab block (@type 3) in one instance vs the K_split one-instance chain (no serial)
  C8       comp, drive, amp, cab, EQ, mod, delay, reverb (catalog defaults, mono, fw 3.50 models)

  .venv/bin/python scripts/blockserial_spike.py [--configs K,K_split,K_amp,C8] [--ledger]

Outputs (local, gitignored): cache/blockserial/<config>/{*.hlx, templates/, result.json,
single.wav, serial.wav, residual.wav}. Synthetic or Guitar-TECHS DI only, never song audio.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from tonematch.engines.base import SAMPLE_RATE
from tonematch.engines.native import (
    ChainTemplate,
    NativeEngine,
    capture_chain,
    chain_id,
    chain_key,
    locate_plugin,
    null_db,
    onset_latency,
    start_state_path,
)
from tonematch.preset import Preset, Slot, read_hlx, write_hlx

OUT = ROOT / "cache" / "blockserial"
DI = ROOT / "cache" / "parity" / "di_10s_tail.wav"
K_HLX = ROOT / "cache" / "stomp" / "inject" / "K.hlx"
PREROLL_S = 3.0
TAIL_S = 0.5
DATASET = "blockserial-v0"

C8_MODELS = [  # (category, model id): HX Stomp, no min firmware (fw 3.50 ok), mono-capable
    ("comp", "HD2_CompressorRedSqueeze"),
    ("drive", "HD2_DM4TubeDrive"),
    ("amp", "HD2_AmpEssexA15"),
    ("cab", "HD2_Cab1x12BlueBell"),
    ("eq", "HD2_EQParametric"),
    ("mod", "HD2_ChorusPlastiChorus"),
    ("delay", "HD2_DelaySimpleDelay"),
    ("reverb", "HD2_ReverbGanymede"),
]
BLOCK_TYPE = {"amp": 1, "cab": 2, "delay": 7, "reverb": 7}  # @type in HX files (others 0)


# ----------------------------------------------------------------------------- presets


def _empty_like(base: Preset, name: str) -> Preset:
    p = copy.deepcopy(base)
    p.name, p.slots, p.snapshots = name, [], []
    return p


def block_slot(category: str, model_id: str, params: dict, extra: dict, position: int,
               key: str, enabled: bool = True) -> Slot:
    ex = {k: v for k, v in extra.items() if k not in ("@cab", "@type", "@position", "@enabled")}
    ex["@type"] = BLOCK_TYPE.get(category, 0)
    ex.setdefault("@path", 0)
    return Slot(category=category, model_id=model_id, params=dict(params), enabled=enabled,
                key=key, position=position, extra=ex)


def k_parts(k: Preset) -> tuple[Slot, Slot]:
    """K's amp and cab as standalone blocks (@type 1 amp, @type 2 cab)."""
    amp = next(s for s in k.slots if s.category == "amp")
    cab = next(s for s in k.slots if s.category == "cab")
    a = block_slot("amp", amp.model_id, amp.params, amp.extra, 0, "block0")
    c = block_slot("cab", cab.model_id, cab.params, cab.extra, 1, "block1")
    return a, c


def catalog_defaults(model_id: str, catalog: dict) -> tuple[dict, dict]:
    m = catalog["models"][model_id]
    params, extra = {}, {}
    for name, p in (m.get("params") or {}).items():
        d = p.get("default")
        if d is None:
            continue
        (extra if name.startswith("@") else params)[name] = d
    if m.get("stereo") and m.get("mono"):
        extra["@stereo"] = False
    return params, extra


def configs(catalog: dict) -> dict[str, dict]:
    """name -> {"single": Preset, "serial": [Preset], "compare_to": optional other config}."""
    k = read_hlx(K_HLX)
    out: dict[str, dict] = {}
    a, c = k_parts(k)
    serial_k = []
    for s in (a, c):
        p = _empty_like(k, f"BS {s.model_id[4:][:10]}")
        s1 = copy.deepcopy(s)
        s1.position, s1.key = 0, "block0"
        p.slots = [s1]
        serial_k.append(p)
    single_k = copy.deepcopy(k)
    single_k.name = "BS K"
    out["K"] = {"single": single_k, "serial": serial_k}
    split = _empty_like(k, "BS K split")
    split.slots = [copy.deepcopy(a), copy.deepcopy(c)]
    out["K_split"] = {"single": split, "serial": serial_k}
    out["K_amp"] = {"single": single_k, "serial": None, "against": split}
    c8 = _empty_like(k, "BS C8")
    slots, parts = [], []
    for i, (cat, mid) in enumerate(C8_MODELS):
        params, extra = catalog_defaults(mid, catalog)
        slots.append(block_slot(cat, mid, params, extra, i, f"block{i}"))
        p = _empty_like(k, f"BS {mid[4:][:12]}")
        p.slots = [block_slot(cat, mid, params, extra, 0, "block0")]
        parts.append(p)
    c8.slots = slots
    out["C8"] = {"single": c8, "serial": parts}
    empty = _empty_like(k, "BS empty")
    out["EMPTY8"] = {"single": empty, "serial": [empty] * 8}  # what an instance boundary adds
    for n in range(2, 8):  # C8 prefixes, to find the block that breaks the null
        pre = _empty_like(k, f"BS C8 p{n}")
        pre.slots = copy.deepcopy(slots[:n])
        out[f"C8p{n}"] = {"single": pre, "serial": parts[:n]}
    return out


# ----------------------------------------------------------------------------- capture


def capture(preset: Preset, d: Path, plugin: str, start: bytes | None, catalog: dict) -> ChainTemplate:
    """Drop-capture ``preset`` once (cached per config directory by chain + values)."""
    tdir = d / "templates"
    key = chain_key(preset)
    for other in sorted(OUT.glob("*/templates")):  # the same block file captured for another config
        for t in ChainTemplate.load_variants(other).get(key, []):
            if t.name == preset.name:
                return t
    write_hlx(preset, d / f"{chain_id([list(x) for x in key])}_{preset.name.replace(' ', '_')}.hlx")
    t0 = time.perf_counter()
    tpl = capture_chain(preset, plugin, start_state=start, templates_dir=tdir, verify_ccs=False,
                        catalog=catalog)
    print(f"  captured {preset.name!r} in {time.perf_counter() - t0:.0f} s", flush=True)
    return tpl


# ----------------------------------------------------------------------------- analysis


def xcorr_lag(a: np.ndarray, b: np.ndarray, max_lag: int = 4800) -> int:
    """Integer lag L maximising sum a[n] * b[n + L]."""
    n = len(a) + len(b)
    nfft = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)
    lags = np.r_[0:max_lag + 1, -max_lag:0]
    vals = c[lags % nfft]
    return int(lags[int(np.argmax(vals))])


def compare(single: np.ndarray, serial: np.ndarray, lo: int, hi: int) -> dict:
    a = single[lo:hi].astype(np.float64)
    lag = xcorr_lag(single[lo:hi], serial[lo:hi])
    b = serial[lo + lag:hi + lag].astype(np.float64)
    g = float(np.dot(a, b) / max(np.dot(b, b), 1e-30))
    bands = []
    for f0 in (62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000):
        A, B = np.fft.rfft(a), np.fft.rfft(b * g)
        f = np.fft.rfftfreq(len(a), 1 / SAMPLE_RATE)
        m = (f >= f0 / np.sqrt(2)) & (f < f0 * np.sqrt(2))
        ea = float(np.sum(np.abs(A[m]) ** 2)) or 1e-30
        er = float(np.sum(np.abs(A[m] - B[m]) ** 2)) or 1e-30
        bands.append({"hz": f0, "residual_db_re_band": round(10 * np.log10(er / ea), 1),
                      "single_db": round(10 * np.log10(ea), 1)})
    return {
        "lag_samples": lag,
        "null_unaligned_db": round(null_db(single[lo:hi], serial[lo:hi]), 2),
        "null_aligned_db": round(null_db(a, b), 2),
        "gain_db": round(20 * np.log10(abs(g) or 1e-30), 3),
        "null_aligned_gain_db": round(null_db(a, b * g), 2),
        "single_rms_dbfs": round(10 * np.log10(np.mean(a ** 2) + 1e-30), 2),
        "serial_rms_dbfs": round(10 * np.log10(np.mean(b ** 2) + 1e-30), 2),
        "bands": bands,
    }


def rms_db(x: np.ndarray) -> float:
    return round(10 * np.log10(float(np.mean(np.square(x, dtype=np.float64))) + 1e-30), 2)


class Renderer:
    """One NativeEngine host; renders a state with the host's state reset (history-independent)."""

    def __init__(self):
        self.e = NativeEngine(preroll=int(PREROLL_S * SAMPLE_RATE), autocapture=False)
        self.globals_seen: list[dict] = []

    def render(self, state: bytes, x: np.ndarray) -> np.ndarray:
        self.e.check_memory()
        self.e._set_state(state)
        vals = self.e.host.call("get_params")[0]["values"]
        self.globals_seen.append({k: round(v, 4) for k, v in vals.items() if k.startswith("master_")})
        y = self.e._raw_render(x, reset=True)
        assert self.e.last_reset_via == "state", self.e.last_reset_via
        return y

    def latency(self, state: bytes) -> int:
        pre = int(PREROLL_S * SAMPLE_RATE)
        imp = np.zeros(pre + SAMPLE_RATE // 2, np.float32)
        imp[pre] = 0.5
        return onset_latency(self.render(state, imp)[pre:])

    def close(self) -> None:
        self.e.close()


def write_wav(path: Path, y: np.ndarray) -> None:
    import soundfile as sf

    sf.write(path, y.astype(np.float32), SAMPLE_RATE, subtype="FLOAT")


def run(name: str, cfg: dict, tpls: dict, r: Renderer, x: np.ndarray, d: Path, trim: int = 0,
        inst_gain_db: float = 0.0) -> dict:
    pre, n = int(PREROLL_S * SAMPLE_RATE), len(x)
    padded = np.concatenate([np.zeros(pre, np.float32), x, np.zeros(int(TAIL_S * SAMPLE_RATE), np.float32)])
    lo, hi = pre, pre + n
    single_t = tpls[cfg["single"].name]
    t0 = time.perf_counter()
    g = np.float32(10 ** (inst_gain_db / 20))  # undo each instance's fixed I/O gain
    single = r.render(single_t.state, padded) * g
    if cfg.get("serial") is None:  # one-instance vs one-instance (block representation check)
        other = r.render(tpls[cfg["against"].name].state, padded)
        res = compare(single, other, lo, hi)
        res.update({"kind": "one-instance vs one-instance", "against": cfg["against"].name})
        write_wav(d / "single.wav", single)
        write_wav(d / "other.wav", other)
        return res
    y, stages = padded, []
    for p in cfg["serial"]:
        t = tpls[p.name]
        y = r.render(t.state, y) * g
        if trim:  # undo this instance's frame latency so the next block sees the signal on time
            y = np.concatenate([y[trim:], np.zeros(trim, np.float32)])
        stages.append({"block": p.slots[0].model_id if p.slots else "HD2_(empty)", "out_rms_dbfs": rms_db(y[lo:hi]),
                       "latency_samples": r.latency(t.state)})
    res = compare(single, y, lo, hi)
    res.update({"kind": "one instance vs serial instances", "stages": stages, "trim_per_instance": trim, "gain_per_instance_db": inst_gain_db,
                "single_latency_samples": r.latency(single_t.state),
                "render_wall_s": round(time.perf_counter() - t0, 2)})
    write_wav(d / "single.wav", single)
    write_wav(d / "serial.wav", y)
    write_wav(d / "residual.wav", single[lo:hi] - y[lo + res["lag_samples"]:hi + res["lag_samples"]])
    return res


def host_count() -> int:
    r = subprocess.run(["pgrep", "-f", "tonematch.engines.native_host"], capture_output=True, text=True,
                       check=False)
    return len(r.stdout.split())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--configs", default="K,K_split,K_amp,C8")
    ap.add_argument("--di", type=Path, default=DI)
    ap.add_argument("--max-hosts", type=int, default=1, help="refuse to start above this many hosts")
    ap.add_argument("--trim", type=int, default=0,
                    help="drop this many samples from each instance's output (16 = one Native frame)")
    ap.add_argument("--inst-gain-db", type=float, default=0.0,
                    help="gain applied to every instance's output (+0.000219 undoes Native's I/O loss)")
    ap.add_argument("--tag", default="", help="suffix for the output directory and ledger note")
    ap.add_argument("--ledger", action="store_true")
    a = ap.parse_args()
    import soundfile as sf

    from tonematch.catalog import load_catalog

    if host_count() > a.max_hosts:
        print(f"{host_count()} Native hosts already running; not starting")
        return 2
    plugin = str(locate_plugin() or "")
    if not plugin:
        print("Helix Native not found")
        return 2
    catalog = load_catalog()
    x, sr = sf.read(a.di, dtype="float32")
    if sr != SAMPLE_RATE or x.ndim != 1:
        raise SystemExit(f"{a.di}: need mono {SAMPLE_RATE} Hz")
    sp = start_state_path()
    start = sp.read_bytes() if sp.is_file() else None
    allc = configs(catalog)
    results = {}
    for name in a.configs.split(","):
        cfg = allc[name]
        d = OUT / (name + a.tag)
        d.mkdir(parents=True, exist_ok=True)
        tpls = {}
        for p in [cfg["single"]] + (cfg.get("serial") or []) + ([cfg["against"]] if cfg.get("against") else []):
            tpls[p.name] = capture(p, d, plugin, start, catalog)
        r = Renderer()
        try:
            res = run(name, cfg, tpls, r, x, d, trim=a.trim, inst_gain_db=a.inst_gain_db)
            res["globals"] = r.globals_seen[:1] + [g for g in r.globals_seen if g != r.globals_seen[0]][:3]
            res["host_mem_mb"] = r.e.host.mem_mb
        finally:
            r.close()
        res.update({"config": name, "di": str(a.di.relative_to(ROOT)) if a.di.is_relative_to(ROOT) else str(a.di),
                    "preroll_s": PREROLL_S})
        (d / "result.json").write_text(json.dumps(res, indent=1))
        results[name] = res
        print(f"{name:8s} lag {res['lag_samples']:+d}  null aligned {res['null_aligned_db']:8.2f} dB  "
              f"gain {res['gain_db']:+.3f} dB  gain-matched {res['null_aligned_gain_db']:8.2f} dB", flush=True)
        if a.ledger:
            kind = "parity"
            note = (f"T045 {name}{a.tag}: trim {a.trim}/instance; gain {a.inst_gain_db} dB/instance; {res['kind']}; DI {res['di']}; lag {res['lag_samples']} samples; gain {res['gain_db']} dB; "
                    f"gain-matched null {res['null_aligned_gain_db']} dB; stages "
                    + " ".join(f"{s['block'][4:]}:lat{s['latency_samples']}" for s in res.get("stages", [])))
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", kind,
                            "--engine", "native", "--dataset", DATASET, "--metric",
                            "blockserial_null_db" if res.get("stages") else "block_repr_null_db",
                            "--value", str(res["null_aligned_db"]), "--renders",
                            str(len(res.get("stages", [])) * 2 + 2), "--notes", note.replace(",", ";")],
                           check=True, env={**os.environ, "TONEMATCH_AGENT": "native-engine"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
