#!/usr/bin/env python3
"""T018s spike: autonomous whole-preset injection on the HX Stomp (stomp-engine).

  gen      offline: make the variants of fixtures/smoke/known.hlx for firmware 3.50 and validate them
  compile  offline: compile every variant onto a device document (stompd compile, no USB)
  run      hardware: back up the edit buffer, write each variant whole into the EDIT BUFFER (op 21),
           read back and diff, render fixtures/smoke/di_2s.wav, time the write and the settling,
           then restore the original preset and diff against the backup

Everything lands under cache/stomp/inject/ (gitignored: the variants are derived from a Line 6
export and stay local). Nothing here saves, renames or writes a flash slot. Needs HX Edit quit,
TONEMATCH_STOMPD (the USB helper) and TONEMATCH_STOMPD_COMPILE (a stompd build with `compile`).
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

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch import catalog as C
from tonematch import preset as P

OUT = ROOT / "cache" / "stomp" / "inject"
KNOWN = ROOT / "fixtures" / "smoke" / "known.hlx"
DI = ROOT / "fixtures" / "smoke" / "di_2s.wav"
FW_350 = 0x3500000
USB56 = 6
ORIGINAL = {"setlist": 0, "index": 11, "name": "Smoke Known"}  # 04C, the pedal's preset in T003/T004


def _fw_ok(model: dict) -> bool:
    m = model.get("stomp_min_firmware")
    return model.get("stomp", False) and (m is None or int(m, 16) <= FW_350)


def _defaults(cat: dict, model_id: str) -> dict:
    return {k: v["default"] for k, v in cat["models"][model_id]["params"].items() if not k.startswith("@")}


def make_variants(cat: dict) -> dict[str, P.Preset]:
    base = P.read_hlx(KNOWN, cat)
    assert base.device_version == FW_350, f"known.hlx device_version {base.device_version:#x}"
    amp = next(i for i, s in enumerate(base.slots) if s.category == "amp")
    cab = next(i for i, s in enumerate(base.slots) if s.category == "cab")
    dly = next(i for i, s in enumerate(base.slots) if s.category == "delay")

    k = copy.deepcopy(base)
    k.name = "T018 K base"

    a = copy.deepcopy(base)
    a.name = "T018 A drive"
    a.slots[amp].params.update(Drive=0.95, Master=0.7, Bass=0.65)   # was 0.61, 1.0, 0.5

    b0 = copy.deepcopy(base)
    b0.name = "T018 B0 plexi"
    b0.slots[amp].model_id = "HD2_AmpBritPlexiBrt"
    b0.slots[amp].params = _defaults(cat, "HD2_AmpBritPlexiBrt")
    b0.slots[cab].model_id = "HD2_Cab4x12Greenback25"
    b0.slots[cab].params = _defaults(cat, "HD2_Cab4x12Greenback25")
    b0.slots[cab].extra["@mic"] = 6

    b = copy.deepcopy(b0)
    b.name = "T018 B plexi delay"
    b.slots[dly].enabled = True
    for sn in b.snapshots:
        sn.enabled[b.slots[dly].key] = True
    out = {"K": k, "A": a, "B0": b0, "B": b}
    for p in out.values():
        for s in p.blocks():
            assert _fw_ok(cat["models"][s.model_id]), f"{s.model_id} is newer than 3.50"
    return out


def hlx_text(p: P.Preset) -> str:
    d = p.to_hlx_dict()
    d["data"]["tone"]["dsp0"]["inputA"]["@input"] = USB56   # Path A input on USB 5/6 for reamping
    return P.dumps_hlx(d)


def cmd_gen(_a) -> int:
    cat = C.load_catalog("current")
    OUT.mkdir(parents=True, exist_ok=True)
    rc = 0
    for name, p in make_variants(cat).items():
        f = OUT / f"{name}.hlx"
        f.write_text(hlx_text(p), encoding="utf-8")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "hlx_validate.py"), str(f),
                            "--firmware", hex(FW_350)], capture_output=True, text=True, check=False)
        print(f"{name}: {f} validate exit {r.returncode}: {r.stdout.strip().splitlines()[-1] if r.stdout else r.stderr}")
        rc = max(rc, r.returncode)
    return rc


def compiler() -> Path:
    exe = os.environ.get("TONEMATCH_STOMPD_COMPILE")
    if not exe:
        sys.exit("set TONEMATCH_STOMPD_COMPILE to a stompd build with the offline `compile` mode")
    return Path(exe)


def cmd_compile(a) -> int:
    from tonematch.engines.stomp import compile_document

    base = Path(a.base)
    for name in ("K", "A", "B0", "B"):
        rep = compile_document(OUT / f"{name}.hlx", base, OUT / f"{name}.offline.bin", compiler())
        print(name, json.dumps({k: rep[k] for k in ("bytes", "changed", "identical_to_base")}))
        print("   ", [(s["slot"], s["mode"], s.get("model"), s.get("cab")) for s in rep["slots"]])
    return 0


# ----------------------------------------------------------------------------- hardware


def features(y: np.ndarray, sr: int = 48000) -> dict:
    y = np.asarray(y, np.float64)
    rms = float(np.sqrt(np.mean(y ** 2)) + 1e-12)
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y)))) + 1e-12
    f = np.fft.rfftfreq(len(y), 1 / sr)
    band = (f > 80) & (f < 12000)
    centroid = float(np.sum(f[band] * spec[band] ** 2) / np.sum(spec[band] ** 2))
    flat = float(np.exp(np.mean(np.log(spec[band] ** 2))) / np.mean(spec[band] ** 2))
    hf = float(np.sum(spec[f > 2000] ** 2) / np.sum(spec ** 2))
    last = y[int(1.5 * sr):]
    return {"rms_db": 20 * np.log10(rms), "crest_db": float(20 * np.log10(np.max(np.abs(y)) / rms)),
            "centroid_hz": centroid, "flatness_db": float(10 * np.log10(flat)), "hf_ratio_db": 10 * np.log10(hf),
            "last_0p5s_rel_db": float(20 * np.log10(np.sqrt(np.mean(last ** 2)) + 1e-12) - 20 * np.log10(rms))}


def doc_diff(a: bytes, b: bytes) -> list[str]:
    """Differences between two device documents, ignoring the order of controller assignment
    entries (the device reorders them on a write; T003). Uses msgpack for reading only."""
    import msgpack

    def load(x: bytes):
        u = msgpack.Unpacker(raw=True, strict_map_key=False)
        u.feed(x)
        vals = list(u)
        tone = vals[2]
        for i, lst in enumerate(tone.get(4) or []):
            if isinstance(lst, list):
                tone[4][i] = sorted(repr(sorted((k, repr(v)) for k, v in e[1].items())) for e in lst)
        return vals

    out: list[str] = []

    def walk(x, y, path=""):
        if type(x) is not type(y):
            out.append(f"{path}: {x!r} -> {y!r}")
        elif isinstance(x, dict):
            for k in sorted(set(x) | set(y), key=repr):
                if k not in x or k not in y:
                    out.append(f"{path}/{k}: {x.get(k)!r} -> {y.get(k)!r}")
                else:
                    walk(x[k], y[k], f"{path}/{k}")
        elif isinstance(x, list):
            if len(x) != len(y):
                out.append(f"{path}: length {len(x)} -> {len(y)}")
            for i, (p, q) in enumerate(zip(x, y, strict=False)):
                walk(p, q, f"{path}[{i}]")
        elif x != y:
            out.append(f"{path}: {x!r} -> {y!r}")

    la, lb = load(a), load(b)
    walk(la[2], lb[2], "tone")
    return out


SILENT_DB = -100.0


def probe(eng, x: np.ndarray, run_dir: Path, tag: str) -> dict:
    """Raw render that never raises on silence: RMS of every USB input pair plus the routing the
    chain reports. Path A is USB 1/2, Path B 3/4."""
    import soundfile as sf

    from tonematch import reamp

    rec, pre = reamp.playrec(x, eng.device, pre_s=eng.PRE_S, tail_s=eng.cal.latency_samples / 48000 + 0.05,
                             in_pair=None)
    seg = rec[pre + eng.cal.latency_samples: pre + eng.cal.latency_samples + len(x)]
    pairs = {f"usb{2 * i + 1}_{2 * i + 2}_db": reamp.rms_db(seg[:, 2 * i:2 * i + 2])
             for i in range(min(4, seg.shape[1] // 2))}
    sf.write(run_dir / f"diag_{tag}.wav", seg[:, :2], 48000, subtype="FLOAT")
    chain = eng.bridge.call("chain")
    io = {f"{sl['kind']}{sl['pos']}": sl.get("routing") for sl in chain["slots"] if sl["kind"] in ("Input", "Output")}
    out = {"tag": tag, "t": time.time(), **{k: round(v, 2) for k, v in pairs.items()}, "routing": io,
           "path_a_silent": bool(pairs["usb1_2_db"] < SILENT_DB)}
    print("diag", json.dumps(out), flush=True)
    return out


def diagnose(eng, dry: np.ndarray, run_dir: Path) -> list[dict]:
    """One-run decision data for a silent render after a whole write (T018s)."""
    steps = []
    # 1. Control: the original buffer, input routed live (what T004/T019 did).
    eng.bridge.call("route_key", pos=0, key=51, to=eng.cal.input_usb56)
    time.sleep(0.3)
    steps.append(probe(eng, dry, run_dir, "c0_original_route_key"))
    eng.bridge.call("backup", path=str(run_dir / "diag_c0.bin"))
    # 2. K by whole write, WITHOUT re-sending the route; then settle; then re-send; then toggle.
    eng.REASSERT_INPUT_AFTER_WHOLE = False
    try:
        eng.load(P.read_hlx(OUT / "K.hlx", C.load_catalog("current")))
    finally:
        eng.REASSERT_INPUT_AFTER_WHOLE = True
    eng.bridge.call("backup", path=str(run_dir / "diag_k.bin"))
    steps.append(probe(eng, dry, run_dir, "k1_after_write"))
    time.sleep(0.5)
    steps.append(probe(eng, dry, run_dir, "k2_settle_0p5s"))
    time.sleep(2.0)
    steps.append(probe(eng, dry, run_dir, "k3_settle_2p5s"))
    eng.bridge.call("route_key", pos=0, key=51, to=eng.cal.input_usb56)
    time.sleep(0.3)
    steps.append(probe(eng, dry, run_dir, "k4_route_key_resent"))
    if steps[-1]["path_a_silent"]:
        eng.bridge.call("route_key", pos=0, key=51, to=1)
        time.sleep(0.3)
        eng.bridge.call("route_key", pos=0, key=51, to=eng.cal.input_usb56)
        time.sleep(0.3)
        steps.append(probe(eng, dry, run_dir, "k5_route_toggle_1_6"))
    eng.bridge.call("backup", path=str(run_dir / "diag_k_after.bin"))
    return steps


def chain_diff(chain: dict, hlx_path: Path, report: dict) -> list[str]:
    """Where the read-back chain disagrees with the .hlx (models, enabled, every named value)."""
    doc = json.loads(hlx_path.read_text())
    dsp = doc["data"]["tone"]["dsp0"]
    slots = {s["pos"]: s for s in chain["slots"]}
    out = []
    for s in report["slots"]:
        if "key" not in s:
            continue
        node, dev = dsp[s["key"]], slots.get(s["slot"], {})
        if dev.get("model_id") != node["@model"]:
            out.append(f"{s['key']}: model {dev.get('model_id')} != {node['@model']}")
        if bool(dev.get("enabled")) != bool(node.get("@enabled", True)):
            out.append(f"{s['key']}: enabled {dev.get('enabled')} != {node.get('@enabled')}")
        groups = [(node, dev.get("params", []))]
        if node.get("@cab"):
            cn = dsp[node["@cab"]]
            if dev.get("paired_id") != cn["@model"]:
                out.append(f"{s['key']}: cab {dev.get('paired_id')} != {cn['@model']}")
            groups.append((cn, dev.get("paired_params", [])))
        for n, ps in groups:
            meta = {}
            for p in ps:
                for kk in (p.get("sym"), p.get("id")):
                    if kk:
                        meta[kk] = p
                if p.get("extra") == 0 and n is not node:
                    meta["@mic"] = p
            for kk, v in n.items():
                if kk.startswith("@") and kk != "@mic":
                    continue
                m = meta.get(kk)
                if m is None:
                    continue
                if abs(float(m["value"]) - float(v)) > 1e-4 * max(1.0, abs(float(v))):
                    out.append(f"{s['key']}.{kk}: device {m['value']} != hlx {v}")
    inp = next(s for s in chain["slots"] if s["kind"] == "Input" and s["pos"] == 0)
    if inp.get("routing") != dsp["inputA"]["@input"]:
        out.append(f"inputA routing {inp.get('routing')} != {dsp['inputA']['@input']}")
    return out


def cmd_run(a) -> int:
    import soundfile as sf

    from tonematch import reamp
    from tonematch.engines.stomp import StompEngine, chain_signature
    from tonematch.engines.stomp_bridge import BridgeError, StompBridge

    if subprocess.run(["pgrep", "-f", "HX Edit"], capture_output=True, check=False).stdout.strip():
        sys.exit("HX Edit is running: quit it first (it holds the control interface)")
    compiler()  # fail early if unset
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = OUT / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    dry, sr = sf.read(DI, dtype="float32")
    summary_mode = "control-only" if a.no_render else "full"
    assert sr == 48000
    silence = np.zeros(48000, np.float32)
    summary: dict = {"stamp": stamp, "mode": summary_mode, "variants": {}, "errors": []}

    t_open = time.perf_counter()
    eng = StompEngine(whole_preset="always", session_dir=run_dir)
    summary["open_s"] = time.perf_counter() - t_open
    eng.keep_documents = run_dir / "docs"
    try:
        orig = eng.original
        summary["original"] = orig
        if (orig["setlist"], orig["index"], orig["name"]) != (ORIGINAL["setlist"], ORIGINAL["index"], ORIGINAL["name"]):
            raise SystemExit(f"the pedal is on {orig}, not 04C 'Smoke Known': stopping before any write")
        summary["backup"] = str(eng.backup_path)
        if a.diagnose:
            summary["diagnose"] = diagnose(eng, dry, run_dir)
        for name in a.variants:
            try:
                hlx = OUT / f"{name}.hlx"
                pre = P.read_hlx(hlx, C.load_catalog("current"))
                v: dict = {}
                t0 = time.perf_counter()
                eng.load(pre)
                v["load_s"] = time.perf_counter() - t0
                w = eng.whole_writes[-1]
                v.update({k: w[k] for k in ("backup_s", "compile_s", "write_s")})
                v["compile_changed"] = w["report"]["changed"]
                v["compile_slots"] = [(s["slot"], s["mode"], s.get("model"), s.get("cab")) for s in w["report"]["slots"]]
                # Read back: the device's document after the write, byte-compared with what we wrote.
                n = len(eng.whole_writes) - 1
                rb = run_dir / f"{name}_readback.bin"
                eng.bridge.call("backup", path=str(rb))
                written = (eng.keep_documents / f"{n:02d}_candidate.bin").read_bytes()
                v["readback_equal_bytes"] = rb.read_bytes() == written
                v["readback_doc_diff"] = doc_diff(written, rb.read_bytes())
                v["readback_bytes"], v["written_bytes"] = rb.stat().st_size, len(written)
                v["chain_diff"] = chain_diff(eng.chain, hlx, w["report"])
                (run_dir / f"{name}_chain.json").write_text(json.dumps(eng.chain, indent=1))
                summary["variants"][name] = v
                if a.no_render:
                    print(name, json.dumps({k: v[k] for k in ("load_s", "write_s", "readback_equal_bytes",
                                                              "readback_doc_diff", "chain_diff")}), flush=True)
                    continue
                # Settling: a render straight after the write, a repeat, then one after a 2 s wait.
                t_r = time.perf_counter()
                r0 = eng.render(dry)
                v["render_s"] = time.perf_counter() - t_r
                r1 = eng.render(dry)
                time.sleep(2.0)
                r2 = eng.render(dry)
                v["null_first_vs_settled_db"] = reamp.null_db(r0, r2)
                v["null_repeat_vs_settled_db"] = reamp.null_db(r1, r2)
                v["features"] = features(r2)
                for tag, y in (("r0", r0), ("r2", r2)):
                    sf.write(run_dir / f"{name}_{tag}.wav", y, 48000, subtype="FLOAT")
                if name in ("K", "A"):
                    idle = eng.render(silence)
                    v["idle_noise_dbfs"] = reamp.rms_db(idle[4800:])
                summary["variants"][name] = v
                print(name, json.dumps({k: v[k] for k in ("load_s", "write_s", "readback_equal_bytes", "chain_diff",
                                                          "null_first_vs_settled_db", "null_repeat_vs_settled_db")}),
                      flush=True)
            except Exception as e:
                # A render failing must not stop the others; any control (bridge) error stops the run.
                summary["variants"].setdefault(name, {})["error"] = f"{type(e).__name__}: {e}"
                summary["errors"].append(f"{name}: {type(e).__name__}: {e}")
                print(name, "ERROR", e, flush=True)
                if isinstance(e, BridgeError):
                    raise
    except SystemExit as e:
        summary["errors"].append(str(e))
    except Exception as e:  # noqa: BLE001 - record, then restore
        summary["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        t_c = time.perf_counter()
        eng.close()
        summary["close_s"] = time.perf_counter() - t_c
        summary["restore_report"] = eng.restore_report
    # H9-style check with a fresh session (the approved helper, no document write allowed).
    try:
        with StompBridge() as b:
            info = b.call("info")
            after = run_dir / "after_restore.bin"
            b.call("backup", path=str(after))
            chain = b.call("chain")
        summary["after"] = {k: info[k] for k in ("setlist", "index", "label", "name")}
        summary["after_bytes_equal_backup"] = after.read_bytes() == Path(eng.backup_path).read_bytes()
        summary["after_signature_equal"] = chain_signature(chain) == chain_signature(eng.original_chain)
    except Exception as e:  # noqa: BLE001
        summary["errors"].append(f"after-check: {type(e).__name__}: {e}")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps({k: summary.get(k) for k in ("errors", "restore_report", "after", "after_bytes_equal_backup",
                                                  "after_signature_equal")}, default=str))
    print(f"summary: {run_dir / 'summary.json'}")
    return 1 if summary["errors"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen")
    c = sub.add_parser("compile")
    c.add_argument("--base", required=True, help="a device document (an edit-buffer backup)")
    r = sub.add_parser("run")
    r.add_argument("--variants", nargs="+", default=["K", "A", "B0", "B"])
    r.add_argument("--no-render", action="store_true", help="control only: write, read back, diff; no audio")
    r.add_argument("--diagnose", action="store_true",
                   help="first: control render on the original buffer (live route_key), then K by whole write "
                        "rendered at 0/0.5/2.5 s, after re-sending route_key, and after a 1->6 toggle")
    a = ap.parse_args(argv)
    return {"gen": cmd_gen, "compile": cmd_compile, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
