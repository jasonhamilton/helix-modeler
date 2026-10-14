#!/usr/bin/env python3
"""T002 spike: can a whole preset be written into Helix Native's saved state from code?

Owner: native-engine. Runs on a machine with the user's own Helix Native 3.8x VST3 (never Stadium).
Every artefact goes to cache/native/spike/ (gitignored): state dumps are Line 6 data, never commit.

Phases (each records evidence in report.json; the verdict is printed and optionally ledgered):
  locate     find the VST3 (or --plugin), refuse Stadium Native
  host       open it in the pedalboard host process; dump the parameter list (params.json)
  state      read raw_state + preset_data; unwrap JUCE VC2!/XML/base64; scan IComponent for
             JSON/XML/zlib/gzip/base64 layers carrying Helix markers ("tone", "dsp0", "@model", HD2_)
  gui        (--gui) open Native's editor; the human loads a golden preset (or builds the fixed
             chain and assigns controllers), closes the window; capture that state + render it
  roundtrip  if a preset document was found: (a) identity re-encode accepted and read back,
             (b) a mutated parameter persists and changes the render, (c) with --golden, the golden
             .hlx written into the *default* state renders the same as the GUI-loaded one
             (null below -60 dB). Success writes cache/native/state_codec.json {proven: true}.
  automation enumerate host params; for knob/switch-like ones, set 0 and 1 and see if the render
             moves (needs controllers assigned in the GUI state to mean anything)
  cost       latency by impulse, determinism, seconds per 2 s render, parallel throughput,
             load penalty

Usage:
  python scripts/native_spike.py                      # headless phases
  python scripts/native_spike.py --gui --golden fixtures/golden/native_stomp.hlx
  python scripts/native_spike.py --capture-template amp_cab --chain-json chain.json --map-json map.json
  add --ledger to append render-cost / smoke rows (TONEMATCH_AGENT=native-engine)
Exit: 0 spike ran, 2 Native not installed, 1 error.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch.engines import native as nat
from tonematch.engines import native_state as ns
from tonematch.engines.base import SAMPLE_RATE

DI = ROOT / "fixtures" / "smoke" / "di_2s.wav"
AUTOMATION_RE = re.compile(r"knob|switch|ctrl|controller|auto|assign|macro|cc\b", re.IGNORECASE)


def ledger(*args: str) -> None:
    env = dict(os.environ)
    env.setdefault("TONEMATCH_AGENT", "native-engine")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), *args], check=True, env=env)


def load_di() -> np.ndarray:
    try:
        import soundfile as sf

        x, sr = sf.read(str(DI), dtype="float32", always_2d=False)
        assert sr == SAMPLE_RATE
        return x if x.ndim == 1 else x.mean(axis=1)
    except (ImportError, OSError, RuntimeError, ValueError, AssertionError):  # no soundfile / unreadable DI
        return nat.test_signal()


def render(host: nat.HostProcess, x: np.ndarray) -> np.ndarray:
    return np.frombuffer(host.call("render", x.astype("<f4").tobytes(), reset=True)[1], dtype="<f4").copy()


def find_numeric(doc, want=("Drive", "Gain", "Level", "Mix", "Bass")):
    """Path to a numeric param inside a block that has '@model' (prefer amp-ish names)."""
    best = None

    def walk(d, path):
        nonlocal best
        if isinstance(d, dict):
            if "@model" in d:
                for k, v in d.items():
                    if not k.startswith("@") and isinstance(v, (int, float)) and not isinstance(v, bool):
                        score = 2 if k in want else 1
                        if best is None or score > best[0]:
                            best = (score, path + [k], v)
            for k, v in d.items():
                walk(v, path + [k])
        elif isinstance(d, list):
            for i, v in enumerate(d):
                walk(v, path + [i])

    walk(doc, [])
    return best


def get_in(d, path):
    for k in path:
        d = d[k]
    return d


def set_in(d, path, v):
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = v


def find_subtree(doc: dict, key: str = "tone") -> list:
    """Path to the dict holding `key` (e.g. ['data'] when Native stores a whole .hlx)."""
    if key in doc:
        return []
    for k, v in doc.items():
        if isinstance(v, dict):
            sub = find_subtree(v, key)
            if sub is not None:
                return [k] + sub
    return None  # type: ignore[return-value]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin")
    ap.add_argument("--plugin-name")
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--golden", type=Path)
    ap.add_argument("--capture-template")
    ap.add_argument("--chain-json", type=Path)
    ap.add_argument("--map-json", type=Path)
    ap.add_argument("--max-workers", type=int)
    ap.add_argument("--out", type=Path, default=nat.cache_dir() / "spike")
    ap.add_argument("--ledger", action="store_true")
    a = ap.parse_args()
    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    rep: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "platform": sys.platform}

    def save():
        (out / "report.json").write_text(json.dumps(rep, indent=1, default=str))

    # ---- locate
    plugin = a.plugin or (str(nat.locate_plugin()) if nat.locate_plugin() else None)
    rep["searched"] = [str(p) for p in nat.candidate_paths()]
    rep["host_available"] = nat.host_available()
    if not plugin:
        rep["installed"] = False
        rep["verdict"] = "Helix Native is not installed on this machine; spike not run."
        save()
        print(rep["verdict"], "Searched:", *rep["searched"], sep="\n  ")
        if a.ledger:
            ledger("--kind", "render-cost", "--engine", "native", "--dataset", "smoke", "--metric", "native_available",
                   "--value", "0", "--notes", "T002 spike: Helix Native not installed on this Mac; no render measured")
        return 2
    if nat.is_stadium(plugin):
        print("Refusing Helix Stadium Native; install original Helix Native 3.8x.")
        return 1
    rep["installed"], rep["plugin"] = True, plugin

    # ---- host
    t0 = time.perf_counter()
    host = nat.HostProcess(plugin, a.plugin_name)
    info = host.info
    rep["host"] = {k: v for k, v in info.items() if k != "params"}
    rep["host"]["open_s"] = time.perf_counter() - t0
    (out / "params.json").write_text(json.dumps(info["params"], indent=1))
    auto = [p["name"] for p in info["params"] if AUTOMATION_RE.search(p["name"])]
    rep["automation_names_matched"] = bool(auto)
    if not auto:  # names did not match: probe the first 32 parameters instead
        auto = [p["name"] for p in info["params"][:32]]
    rep["params"] = {"count": len(info["params"]), "automation_like": auto[:64],
                     "first_names": [p["name"] for p in info["params"][:40]]}
    print(f"hosted {info.get('name')} {info.get('version')}: {len(info['params'])} params, "
          f"{len(auto)} automation-like, latency reported {info.get('reported_latency_samples')}")
    if a.capture_template:
        return capture_template(host, a)

    # ---- state
    raw = host.call("get_state")[1]
    (out / "state_default.bin").write_bytes(raw)
    parts = ns.split_vst3_state(raw)
    rep["state"] = {"raw_bytes": len(raw), "juce_vc2": raw.startswith(ns.JUCE_MAGIC),
                    "parts": {k: ns.summarize(v) for k, v in parts.items()}}
    try:
        pd = host.call("get_state", which="preset")[1]
        (out / "preset_data_default.vstpreset").write_bytes(pd)
        rep["state"]["preset_data"] = ns.summarize(pd)
    except Exception as e:  # noqa: BLE001
        rep["state"]["preset_data"] = f"unavailable: {e}"
    comp = parts.get("IComponent", raw)
    cand = ns.best_preset_candidate(ns.scan(comp))
    rep["state"]["preset_document_found"] = cand is not None
    print("state:", len(raw), "bytes; preset document:", cand.describe() if cand else "NOT found")
    save()

    x = load_di()
    wet_default = render(host, x)
    np.save(out / "wet_default.npy", wet_default)

    # ---- gui
    wet_gui = gui_raw = None
    if a.gui:
        print("\nNative's window is opening. Load the golden preset (or build the chain and assign the 16 knobs /"
              " 16 switches), then CLOSE the window to continue.")
        host.call("editor")
        gui_raw = host.call("get_state")[1]
        (out / "state_gui.bin").write_bytes(gui_raw)
        gp = ns.split_vst3_state(gui_raw)
        gcomp = gp.get("IComponent", gui_raw)
        rep["gui"] = {"raw_bytes": len(gui_raw), "component": ns.summarize(gcomp),
                      "component_changed": gcomp != comp,
                      "params_changed": _param_diff(host, info)}
        wet_gui = render(host, x)
        np.save(out / "wet_gui.npy", wet_gui)
        rep["gui"]["null_vs_default_db"] = nat.null_db(wet_gui, wet_default)
        gcand = ns.best_preset_candidate(ns.scan(gcomp))
        if gcand is not None:
            cand, comp, raw = gcand, gcomp, gui_raw
            rep["state"]["preset_document_found"] = True
        save()

    # ---- roundtrip
    rep["roundtrip"] = rt = {"attempted": cand is not None and cand.kind == "json"}
    if rt["attempted"]:
        doc = json.loads(cand.data)
        (out / "native_doc.json").write_text(json.dumps(doc, indent=1))
        rt["top_keys"] = list(doc)[:20]
        rt["subtree_for_tone"] = find_subtree(doc)
        # (a) identity
        host.call("set_state", ns.join_vst3_state(raw, {"IComponent": ns.replace(comp, cand.path, cand.data)}))
        back = ns.split_vst3_state(host.call("get_state")[1]).get("IComponent", b"")
        bcand = ns.best_preset_candidate(ns.scan(back))
        rt["identity_doc_equal"] = bool(bcand and json.loads(bcand.data) == doc)
        base_wet = render(host, x)
        # (b) mutation
        hit = find_numeric(doc)
        if hit:
            _, path, v0 = hit
            v1 = (v0 * 0.2 if v0 else 0.8) if isinstance(v0, float) else (0 if v0 else 1)
            doc2 = copy.deepcopy(doc)
            set_in(doc2, path, v1)
            new = json.dumps(doc2, separators=(",", ":")).encode()
            try:
                host.call("set_state", ns.join_vst3_state(raw, {"IComponent": ns.replace(comp, cand.path, new)}))
                back = ns.split_vst3_state(host.call("get_state")[1]).get("IComponent", b"")
                bc = ns.best_preset_candidate(ns.scan(back))
                rt["mutation"] = {"path": path, "from": v0, "to": v1,
                                  "persisted": bool(bc and get_in(json.loads(bc.data), path) == v1),
                                  "render_null_db": nat.null_db(base_wet, render(host, x))}
            except Exception as e:  # noqa: BLE001
                rt["mutation"] = {"path": path, "error": str(e)}
                host.restart()
        # (c) golden into default state vs GUI-loaded golden
        if a.golden and wet_gui is not None and rt["subtree_for_tone"] is not None:
            hlx = json.loads(a.golden.read_text())
            hsub = find_subtree(hlx)
            d0 = json.loads(ns.best_preset_candidate(ns.scan(ns.split_vst3_state(
                (out / "state_default.bin").read_bytes())["IComponent"])).data) if parts else None
            if d0 is not None and hsub is not None:
                tgt = get_in(d0, rt["subtree_for_tone"])
                tgt.clear()
                tgt.update(get_in(hlx, hsub))
                dflt = (out / "state_default.bin").read_bytes()
                dcomp = ns.split_vst3_state(dflt)["IComponent"]
                dcand = ns.best_preset_candidate(ns.scan(dcomp))
                blob = ns.join_vst3_state(dflt, {"IComponent": ns.replace(
                    dcomp, dcand.path, json.dumps(d0, separators=(",", ":")).encode())})
                try:
                    host.call("set_state", blob)
                    time.sleep(1.0)
                    wet_gen = render(host, x)
                    rt["golden_null_vs_gui_db"] = nat.null_db(wet_gui, wet_gen)
                    rt["proven"] = rt["golden_null_vs_gui_db"] < -60 and rt.get("identity_doc_equal", False)
                except Exception as e:  # noqa: BLE001
                    rt["golden_error"] = str(e)
                    host.restart()
        if rt.get("proven"):
            (nat.cache_dir() / "state_codec.json").write_text(json.dumps(
                {"proven": True, "subtree": rt["subtree_for_tone"], "evidence": "cache/native/spike/report.json",
                 "plugin_version": info.get("version")}, indent=1))
    save()

    # ---- automation
    if gui_raw:
        host.call("set_state", gui_raw)
    rep["automation"] = am = {"tested": []}
    for name in auto[:32]:
        try:
            r0 = host.call("set_params", values={name: 0.0})[0]["read_back"][name]
            w0 = render(host, x)
            r1 = host.call("set_params", values={name: 1.0})[0]["read_back"][name]
            w1 = render(host, x)
            w1b = render(host, x)
            am["tested"].append({"name": name, "read_back": [r0, r1],
                                 "moves_render_db": nat.null_db(w0, w1), "deterministic_db": nat.null_db(w1, w1b)})
        except Exception as e:  # noqa: BLE001
            am["tested"].append({"name": name, "error": str(e)})
    am["effective"] = [t["name"] for t in am["tested"] if t.get("moves_render_db", -999) > -40]
    if gui_raw:
        host.call("set_state", gui_raw)
    save()

    # ---- cost
    host.close()
    eng = nat.NativeEngine(plugin, a.plugin_name)
    if gui_raw:
        eng._set_state(gui_raw)
    rep["cost"] = c = {}
    c["latency_samples"] = eng.latency()
    y1, y2 = eng.render(x), eng.render(x)
    c["determinism_null_db"] = nat.null_db(y1, y2)
    c["peak_dbfs"] = float(20 * np.log10(max(np.max(np.abs(y1)), 1e-12)))
    t0 = time.perf_counter()
    cost = eng.measure_cost(max_workers=a.max_workers)
    c["measure_wall_s"] = time.perf_counter() - t0
    c.update(cost.__dict__)
    eng.close()
    rep["verdict"] = verdict(rep)
    save()
    print("\nVERDICT:", rep["verdict"])
    print("report:", out / "report.json")
    if a.ledger:
        ledger("--kind", "render-cost", "--engine", "native", "--dataset", "smoke", "--metric", "seconds_per_render",
               "--value", f"{cost.seconds_per_render:.4f}", "--renders", "1",
               "--notes", f"T002 spike; parallelism={cost.parallelism}; load_penalty_s={cost.load_penalty_s:.3f}; "
                          f"latency={c['latency_samples']}; route={'state' if rt.get('proven') else 'automation'}")
        ledger("--kind", "smoke", "--engine", "native", "--dataset", "smoke", "--metric", "peak_dbfs",
               "--value", f"{c['peak_dbfs']:.2f}", "--notes", "T002 spike render of fixtures/smoke/di_2s.wav")
    return 0


def _param_diff(host, info) -> list:
    now = host.call("get_params")[0]["values"]
    before = {p["name"]: p.get("raw_value") for p in info["params"]}
    return [n for n, v in now.items() if before.get(n) is not None and abs(v - before[n]) > 1e-6][:64]


def verdict(rep: dict) -> str:
    rt = rep.get("roundtrip", {})
    if rt.get("proven"):
        return ("YES: whole presets via state blob. Golden .hlx written into Native state renders "
                f"{rt['golden_null_vs_gui_db']:.1f} dB null vs the GUI-loaded preset.")
    found = rep.get("state", {}).get("preset_document_found")
    eff = rep.get("automation", {}).get("effective", [])
    s = "State blob: " + ("preset document found but round trip not proven (see report)" if found
                          else "no preset document found (opaque/encrypted)")
    s += f". Automation: {len(eff)} host params move the render" + (f" ({', '.join(eff[:4])}...)" if eff else "")
    s += ". Route: " + ("automation (16+16 per chain)" if eff else "undecided; try --gui with controllers assigned, then MIDI CC via DawDreamer")
    return s


def capture_template(host: nat.HostProcess, a) -> int:
    """Human builds the chain in Native's GUI and assigns controllers; we save state + map."""
    if not (a.chain_json and a.map_json):
        print("--capture-template needs --chain-json (8 x [category, model_id|null]) and --map-json "
              "({\"<slot>:<param>\": \"<host param name>\"}, names from params.json)")
        return 1
    print("Build the chain in Native, assign the knobs/switches named in the map, then close the window.")
    host.call("editor")
    tpl = nat.ChainTemplate(chain=tuple(tuple(x) for x in json.loads(a.chain_json.read_text())),
                            state=host.call("get_state")[1], map=json.loads(a.map_json.read_text()),
                            name=a.capture_template)
    known = {p["name"] for p in host.info["params"]}
    bad = [v for v in tpl.map.values() if v not in known]
    if bad:
        print("map names unknown to the host:", bad)
        return 1
    print("saved", tpl.save())
    host.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
