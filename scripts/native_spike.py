#!/usr/bin/env python3
"""T002/T013 spike: can a whole preset be written into Helix Native's saved state from code?

Owner: native-engine. Runs on a machine with the user's own Helix Native 3.8x (VST3 preferred, AU
accepted; never Stadium). Every artefact goes to cache/native/spike/ (gitignored): state dumps,
renders and parameter lists are Line 6 data. Never commit them.

Phases (each records evidence in report.json; the verdict is printed and optionally ledgered):
  locate     find the plugin (or --plugin), read its Info.plist, refuse Stadium Native
  host       open it in the pedalboard host process (a child process; GPL stays out of this one)
  state      read raw_state; unwrap VST3 (JUCE VC2!) or AU (bplist Line6PresetData); map Native's
             CMXH chunk container; scan for JSON/XML/zlib/gzip/base64 layers with Helix markers;
             try replaying a record from Native's own preset library into the DPCP/BDCP chunks
             (opaque bytes, nothing decrypted)
  gui        (--gui) open Native's editor; the human opens a preset file and assigns Knob 1 /
             Switch 1, then closes the window; capture that state, render it, and replay the
             captured blob into a *fresh* host process (the automation route's core assumption)
  roundtrip  only if a preset document was found in the state (it is not, on 3.82)
  automation knob_*/switch_*/snapshot_index, plus cc_<n> for CCs the --golden file assigns:
             set low/high, see whether the render moves, check determinism
  template   (--gui with --golden) save the captured state as the chain template for that file's
             chain, with the knob/switch map (--map-json, default DEFAULT_GUI_MAP) kept only for
             host params that were shown to move the render
  cost       latency by impulse, determinism, seconds per 2 s render, parallel throughput,
             chain-change settling, on the captured chain if there is one

Usage:
  python scripts/native_spike.py                      # headless phases
  python scripts/native_spike.py --gui --golden fixtures/smoke/known.hlx --ledger
  python scripts/native_spike.py --capture-template amp_cab --chain-json chain.json --map-json map.json
  --ledger appends render-cost / smoke rows (TONEMATCH_AGENT=native-engine)
Exit: 0 spike ran, 2 Native not installed, 1 error.
"""
from __future__ import annotations

import argparse
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
AUTOMATION_RE = re.compile(r"^(knob_\d+|switch_\d+|snapshot_index)$")
LIBRARY = Path.home() / "Library" / "Application Support" / "Line 6" / "Helix Native"
# What the --gui instructions ask the human to assign on fixtures/smoke/known.hlx
# (slots in chain order: 0 amp, 1 cab, 2 delay). Kept only for params proven to move the render.
DEFAULT_GUI_MAP = {"0:Drive": "knob_01", "2:@enabled": "switch_01"}
MOVES_DB = -40.0  # a host param "moves the render" if low vs high nulls worse than this


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
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from engine_smoke import read_wav_f32

            return read_wav_f32(DI)
        except (ImportError, OSError, ValueError, AssertionError):
            return nat.test_signal()


def render(host: nat.HostProcess, x: np.ndarray, pre: int = nat.NativeEngine.PREROLL) -> np.ndarray:
    """Reset render with Native's post-reset fade-in pre-rolled over silence, pre-roll trimmed."""
    px = np.concatenate([np.zeros(pre, np.float32), x]).astype("<f4")
    return np.frombuffer(host.call("render", px.tobytes(), reset=True)[1], dtype="<f4")[pre:].copy()


def component(raw: bytes) -> bytes:
    return ns.plugin_component(raw)[1]


def golden_ccs(path: Path | None) -> list[int]:
    """MIDI CC numbers the file's controller section assigns (to probe cc_<n> host params)."""
    if not path:
        return []
    try:
        ctl = json.loads(path.read_text())["data"]["tone"].get("controller", {})
    except (OSError, ValueError, KeyError):
        return []
    out = set()
    for dsp in ctl.values():
        for blk in (dsp or {}).values():
            for prm in (blk or {}).values():
                if isinstance(prm, dict) and isinstance(prm.get("@cc"), int):
                    out.add(prm["@cc"])
    return sorted(out)


def library_records(limit: int = 2) -> list[Path]:
    """A couple of records from the user's own Native preset library (read in place, never copied)."""
    out = []
    for sub in ("#PRESET_LIBRARY_STOMP/#SETLIST0", "#PRESET_LIBRARY/#SETLIST0"):
        d = LIBRARY / sub
        if d.is_dir():
            out += sorted(d.glob("#PRESET0*"))[:limit]
    return out


def splice_test(host: nat.HostProcess, raw: bytes, x: np.ndarray, wet_default: np.ndarray) -> dict:
    """Replay library records (opaque, same format as DPCP) into the state; does Native take them?"""
    comp = component(raw)
    size = next((c["size"] for c in ns.container_chunks(comp) if c["tag"] == "DPCP"), None)
    res: dict = {"tried": []}
    for rec_path in library_records():
        rec = rec_path.read_bytes()
        row = {"record": f"{rec_path.parent.parent.name}/{rec_path.parent.name}/{rec_path.name}",
               "bytes": len(rec), "same_size_as_DPCP": len(rec) == size}
        if len(rec) != size:
            res["tried"].append(row)
            continue
        for tags in (("DPCP",), ("DPCP", "BDCP")):
            try:
                host.call("set_state", ns.replace_component(raw, ns.replace_chunk_payloads(
                    comp, {t: rec for t in tags})))
                back = component(host.call("get_state")[1])
                y = render(host, x)
                row["+".join(tags)] = {
                    "accepted_readback": ns.chunk_payload(back, "DPCP") == rec,
                    "render_null_vs_default_db": round(nat.null_db(wet_default, y), 1),
                    "render_changed": nat.null_db(wet_default, y) > MOVES_DB}
            except Exception as e:  # noqa: BLE001 - a bad blob may kill the host; record and move on
                row["+".join(tags)] = {"error": str(e)}
                host.restart()
        res["tried"].append(row)
    host.call("set_state", raw)
    res["any_loaded"] = any(v.get("render_changed") or v.get("accepted_readback")
                            for r in res["tried"] for k, v in r.items() if isinstance(v, dict))
    return res


def render_modes(host: nat.HostProcess, x: np.ndarray, flush_s: float = 2.0) -> dict:
    """reset (deterministic, ~1.6 s fixed) vs flush (no reset, silence pre-roll): speed and agreement."""
    def timed(f):
        t0 = time.perf_counter()
        y = f()
        return y, time.perf_counter() - t0

    def flush():
        n = int(flush_s * SAMPLE_RATE)
        px = np.concatenate([np.zeros(n, np.float32), x]).astype("<f4")
        return np.frombuffer(host.call("render", px.tobytes(), reset=False)[1], dtype="<f4")[n:].copy()

    (a1, ta), (a2, _) = timed(lambda: render(host, x)), timed(lambda: render(host, x))
    (b1, tb), (b2, _) = timed(flush), timed(flush)
    return {"reset_s": round(ta, 3), "flush_s": round(tb, 3), "flush_seconds_of_silence": flush_s,
            "reset_repeat_null_db": round(nat.null_db(a1, a2), 1),
            "flush_repeat_null_db": round(nat.null_db(b1, b2), 1),
            "flush_vs_reset_null_db": round(nat.null_db(a1, b1), 1)}


def probe_params(host: nat.HostProcess, names: list[str], x: np.ndarray, base_state: bytes | None) -> dict:
    out: dict = {"tested": []}
    for name in names:
        try:
            if base_state:
                host.call("set_state", base_state)
            r0 = host.call("set_params", values={name: 0.0})[0]["read_back"][name]
            w0 = render(host, x)
            r1 = host.call("set_params", values={name: 1.0})[0]["read_back"][name]
            w1, w1b = render(host, x), render(host, x)
            out["tested"].append({"name": name, "read_back": [r0, r1],
                                  "moves_render_db": round(nat.null_db(w0, w1), 1),
                                  "deterministic_db": round(nat.null_db(w1, w1b), 1)})
        except Exception as e:  # noqa: BLE001
            out["tested"].append({"name": name, "error": str(e)})
    out["effective"] = [t["name"] for t in out["tested"] if t.get("moves_render_db", -999) > MOVES_DB]
    if base_state:
        host.call("set_state", base_state)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin")
    ap.add_argument("--plugin-name")
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--golden", type=Path, help="the file the human opens in Native's window (--gui)")
    ap.add_argument("--capture-template")
    ap.add_argument("--chain-json", type=Path)
    ap.add_argument("--map-json", type=Path)
    ap.add_argument("--max-workers", type=int)
    ap.add_argument("--skip-cost", action="store_true")
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
    rep["bundle"] = nat.bundle_info(plugin)
    if nat.is_stadium(plugin):
        print("Refusing Helix Stadium Native; install original Helix Native 3.8x.")
        return 1
    ver = nat.plugin_version(plugin)
    rep["installed"], rep["plugin"], rep["version_ok"] = True, plugin, ver.startswith("3.8")
    if not rep["version_ok"]:
        print(f"WARNING: plugin version {ver!r}; this spike targets original Helix Native 3.8x")

    # ---- host
    t0 = time.perf_counter()
    host = nat.HostProcess(plugin, a.plugin_name)
    info = host.info
    rep["host"] = {k: v for k, v in info.items() if k != "params"}
    rep["host"]["open_s"] = time.perf_counter() - t0
    (out / "params.json").write_text(json.dumps(info["params"], indent=1))
    names = [p["name"] for p in info["params"]]
    auto = [n for n in names if AUTOMATION_RE.match(n)]
    ccs = [f"cc_{n}" for n in golden_ccs(a.golden) if f"cc_{n}" in names]
    rep["params"] = {"count": len(names), "automation": auto, "cc_params": sum(n.startswith("cc_") for n in names),
                     "other": [n for n in names if not AUTOMATION_RE.match(n) and not n.startswith("cc_")],
                     "golden_cc_probe": ccs}
    print(f"hosted {info.get('name')} {ver or info.get('version')} ({Path(plugin).suffix}): {len(names)} params, "
          f"{len(auto)} automation, {rep['params']['cc_params']} cc_*, latency reported "
          f"{info.get('reported_latency_samples')}")
    if a.capture_template:
        return capture_template(host, a)

    # ---- state
    raw = host.call("get_state")[1]
    (out / "state_default.bin").write_bytes(raw)
    fmt, comp = ns.plugin_component(raw)
    cand = ns.best_preset_candidate(ns.scan(comp))
    rep["state"] = {"raw_bytes": len(raw), "wrapper": fmt, "component": ns.summarize(comp),
                    "container": ns.describe_container(comp), "preset_document_found": cand is not None}
    print(f"state: {len(raw)} bytes ({fmt}); container chunks "
          f"{[c['tag'] for c in rep['state']['container']['chunks']]}; preset document: "
          f"{cand.describe() if cand else 'NOT found'}")
    save()

    x = load_di()
    wet_default = render(host, x)
    np.save(out / "wet_default.npy", wet_default)
    rep["default_render"] = {"null_vs_dry_db": round(nat.null_db(x, wet_default), 1),
                             "note": "Native's empty default preset; trimmed by the latency below"}
    rep["splice"] = splice_test(host, raw, x, wet_default)
    print("library-record replay into DPCP/BDCP loaded a preset:", rep["splice"]["any_loaded"])
    save()

    # ---- gui
    wet_gui = gui_raw = None
    if a.gui:
        print("\nNative's window is opening (it may be behind the terminal). "
              f"Open {a.golden or 'a preset'} via the Open menu > Open From Disk, accept the HC-mode change, "
              "assign Amp Drive to Knob 1 and the Delay block's bypass to Switch 1 (right-click > automation), "
              "then CLOSE the window to continue.")
        host.call("editor")
        gui_raw = host.call("get_state")[1]
        (out / "state_gui.bin").write_bytes(gui_raw)
        gcomp = component(gui_raw)
        wet_gui = render(host, x)
        np.save(out / "wet_gui.npy", wet_gui)
        rep["gui"] = g = {"raw_bytes": len(gui_raw), "container": ns.describe_container(gcomp),
                          "DPCP_changed": ns.chunk_payload(gcomp, "DPCP") != ns.chunk_payload(comp, "DPCP"),
                          "null_vs_default_db": round(nat.null_db(wet_gui, wet_default), 1),
                          "peak_dbfs": round(float(20 * np.log10(max(np.max(np.abs(wet_gui)), 1e-12))), 2)}
        g["preset_loaded"] = g["null_vs_default_db"] > MOVES_DB
        # the automation route's core assumption: an opaque captured blob replays in a fresh process
        fresh = nat.HostProcess(plugin, a.plugin_name)
        try:
            fresh.call("set_state", gui_raw)
            time.sleep(0.5)
            wet_replay = render(fresh, x)
            g["replay_fresh_null_db"] = round(nat.null_db(wet_gui, wet_replay), 1)
            g["replay_ok"] = g["preset_loaded"] and g["replay_fresh_null_db"] < -60
        finally:
            fresh.close()
        host.call("set_state", gui_raw)
        g["render_modes"] = render_modes(host, x)
        host.call("set_state", gui_raw)
        gcand = ns.best_preset_candidate(ns.scan(gcomp))
        if gcand is not None:
            cand, comp, raw = gcand, gcomp, gui_raw
            rep["state"]["preset_document_found"] = True
        print(f"GUI state: preset loaded={g['preset_loaded']} ({g['null_vs_default_db']} dB vs default); "
              f"fresh-process replay null {g.get('replay_fresh_null_db')} dB")
        save()

    # ---- roundtrip (only meaningful if a readable preset document exists in the state)
    rep["roundtrip"] = rt = {"attempted": cand is not None and cand.kind == "json"}
    if rt["attempted"]:
        doc = json.loads(cand.data)
        (out / "native_doc.json").write_text(json.dumps(doc, indent=1))
        host.call("set_state", ns.replace_component(raw, ns.replace(comp, cand.path, cand.data)))
        back = component(host.call("get_state")[1])
        bcand = ns.best_preset_candidate(ns.scan(back))
        rt["identity_doc_equal"] = bool(bcand and json.loads(bcand.data) == doc)
        rt["note"] = "a document was found; extend this phase (mutation + golden null) before trusting it"
    save()

    # ---- automation
    rep["automation"] = probe_params(host, auto + ccs, x, gui_raw)
    print("host params that move the render:", rep["automation"]["effective"] or "none")
    if gui_raw and "switch_01" in rep["automation"]["effective"]:  # the delay on: tails for the flush test
        host.call("set_state", gui_raw)
        host.call("set_params", values={"switch_01": 1.0})
        rep["gui"]["render_modes_switch_01_on"] = render_modes(host, x)
        host.call("set_state", gui_raw)
    save()

    # ---- template (chain capture for the automation route)
    tpl_preset = None
    if gui_raw and a.golden and rep["gui"].get("replay_ok"):
        from tonematch.preset import read_hlx

        tpl_preset = read_hlx(a.golden)
        want = json.loads(a.map_json.read_text()) if a.map_json else DEFAULT_GUI_MAP
        eff = set(rep["automation"]["effective"])
        tpl = nat.ChainTemplate(chain=nat.chain_key(tpl_preset), state=gui_raw,
                                map={k: v for k, v in want.items() if v in eff}, name=a.golden.stem)
        rep["template"] = {"path": str(tpl.save()), "chain": tpl.chain, "map": tpl.map,
                           "dropped_from_map": sorted(set(want) - set(tpl.map))}
        print("saved chain template", rep["template"]["path"], "map", tpl.map)
        save()

    # ---- cost
    host.close()
    if a.skip_cost:
        rep["verdict"] = verdict(rep)
        save()
        print("\nVERDICT:", rep["verdict"])
        return 0
    eng = nat.NativeEngine(plugin, a.plugin_name)
    chain_note = "empty default preset"
    if tpl_preset is not None:
        eng.load(tpl_preset)
        chain_note = f"{a.golden.name} chain via template"
    elif gui_raw:
        eng._set_state(gui_raw)
        chain_note = "GUI-captured state"
    rep["cost"] = c = {"chain": chain_note}
    c["latency_samples"] = eng.latency()
    y1, y2 = eng.render(x), eng.render(x)
    c["determinism_null_db"] = round(nat.null_db(y1, y2), 1)
    c["peak_dbfs"] = round(float(20 * np.log10(max(np.max(np.abs(y1)), 1e-12))), 2)
    if not gui_raw:
        c["passthrough_null_db"] = round(nat.null_db(x, y1), 1)  # empty preset: trimmed output == input
    t0 = time.perf_counter()
    cost = eng.measure_cost(max_workers=a.max_workers)
    c["measure_wall_s"] = round(time.perf_counter() - t0, 1)
    c.update(cost.__dict__)
    detail = json.loads((nat.cache_dir() / "cost.json").read_text())
    c["detail"] = next((v for k, v in detail.items() if k.startswith(f"{plugin}|")
                        and v["cost"] == cost.__dict__), {})
    eng.close()
    rep["verdict"] = verdict(rep)
    save()
    print("\nVERDICT:", rep["verdict"])
    print("report:", out / "report.json")
    if a.ledger:
        tp = c["detail"].get("throughput_by_workers", {})
        ledger("--kind", "render-cost", "--engine", "native", "--dataset", "smoke", "--metric", "seconds_per_render",
               "--value", f"{cost.seconds_per_render:.4f}", "--renders", "1",
               "--notes", f"T013 Native {ver} {Path(plugin).suffix} on {chain_note}; 2 s clip; "
                          f"parallelism={cost.parallelism}; load_penalty_s={cost.load_penalty_s:.3f} "
                          f"({c['detail'].get('load_penalty_pair')}); latency={c['latency_samples']}; "
                          f"renders/s by workers={json.dumps(tp).replace(',', ';')}")
        if gui_raw:
            if "replay_fresh_null_db" in rep["gui"]:
                ledger("--kind", "smoke", "--engine", "native", "--dataset", "smoke", "--metric", "replay_null_db",
                       "--value", f"{rep['gui']['replay_fresh_null_db']}",
                       "--notes", f"T013 GUI-loaded {a.golden.name if a.golden else 'preset'} state replayed in a "
                                  "fresh host process vs in-session render; not engine-smoke")
        else:
            ledger("--kind", "smoke", "--engine", "native", "--dataset", "smoke", "--metric", "passthrough_null_db",
                   "--value", f"{c['passthrough_null_db']}",
                   "--notes", f"T013 fixtures/smoke/di_2s.wav through Native {ver} empty default preset; "
                              f"latency-trimmed ({c['latency_samples']} smp) output vs input; "
                              "not engine-smoke and not Gate 1 evidence")
    return 0


def verdict(rep: dict) -> str:
    st, sp, g = rep.get("state", {}), rep.get("splice", {}), rep.get("gui")
    chunks = [c["tag"] for c in st.get("container", {}).get("chunks", [])]
    s = ("STATE ROUTE: NO. " if not st.get("preset_document_found") else "STATE ROUTE: document found, unproven. ")
    s += (f"Native's state is a {st.get('wrapper')} wrapper around a CMXH chunk container {chunks}; the preset "
          "records (DPCP/BDCP) are high-entropy and opaque, no JSON/XML/zlib layer, and replaying a record from "
          f"Native's own library {'LOADED' if sp.get('any_loaded') else 'was rejected'}. ")
    eff = rep.get("automation", {}).get("effective", [])
    if g is None:
        s += "AUTOMATION ROUTE: pending the --gui step (needs a preset with Knob/Switch assignments loaded)."
    elif not g.get("preset_loaded"):
        s += "GUI step did not load a preset (render equals the empty default); re-run --gui."
    elif g.get("replay_ok"):
        s += (f"AUTOMATION ROUTE: YES for replay (captured blob replays in a fresh process at "
              f"{g['replay_fresh_null_db']} dB); host params moving the render: {eff or 'none'}.")
    else:
        s += f"AUTOMATION ROUTE: NO, captured state did not replay ({g.get('replay_fresh_null_db')} dB)."
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
