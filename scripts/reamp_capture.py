#!/usr/bin/env python3
"""reamp-capture: send a dry clip through the HX Stomp over USB and capture the result (owner: stomp-engine).

  python scripts/reamp_capture.py --calibrate [--ledger]          # impulse latency x3 + level reference
  python scripts/reamp_capture.py dry.wav --out wet.wav [--ledger]  # one reamp, trimmed and level-matched
  python scripts/reamp_capture.py --user-di --seconds 20 --out take  # record dry USB 5/6 + wet USB 1/2

Skill: .claude/skills/reamp-capture. Calibration: cache/stomp/latency.json (gitignored).

With the control helper (TONEMATCH_STOMPD, see tonematch.engines.stomp_bridge) the script reads the
firmware, routes the Input block to USB 5/6 and, for --calibrate, bypasses every block so the chain
is transparent. It puts back exactly what it changed (edit buffer only, nothing is saved). Without
the helper, set the pedal up by hand and pass --firmware.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Self

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch import reamp
from tonematch.engines import stomp_bridge


def ledger(*args: str) -> None:
    env = dict(os.environ)
    env.setdefault("TONEMATCH_AGENT", "stomp-engine")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), *args], check=True, env=env)


class Prepared:
    """Edit-buffer changes made for a capture, undone on exit."""

    def __init__(self, bridge: stomp_bridge.StompBridge | None, input_value: int, transparent: bool):
        self.b, self.input_value, self.transparent = bridge, input_value, transparent
        self.info: dict = {}
        self.undo: list[tuple[str, dict]] = []

    def __enter__(self) -> Self:
        if self.b is None:
            return self
        try:
            self.info = self.b.call("info")
            chain = self.b.call("chain")
            for s in chain["slots"]:
                if s["kind"] == "Input" and s["pos"] == 0 and s.get("routing") != self.input_value:
                    self.undo.append(("route_key", {"pos": 0, "key": 51, "to": s.get("routing")}))
                    self.b.call("route_key", pos=0, key=51, to=self.input_value)
                if self.transparent and s["kind"] == "Block" and s["enabled"]:
                    self.undo.append(("enable", {"pos": s["pos"], "on": True}))
                    self.b.call("enable", pos=s["pos"], on=False)
            time.sleep(0.3)
        except BaseException:
            self._undo()  # partial setup: put back whatever was changed
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self._undo()

    def _undo(self) -> None:
        """Undo every recorded edit, newest first; one failure does not stop the rest."""
        if self.b is None:
            return
        failed = []
        for cmd, args in reversed(self.undo):
            try:
                self.b.call(cmd, **args)
            except Exception as e:  # noqa: BLE001 - keep undoing; report at the end
                failed.append(f"{cmd} {args}: {e}")
        self.undo.clear()
        if failed:
            print("reamp-capture: could not undo: " + "; ".join(failed), file=sys.stderr)


def open_bridge(no_bridge: bool) -> stomp_bridge.StompBridge | None:
    if no_bridge or stomp_bridge.helper_path() is None:
        return None
    return stomp_bridge.StompBridge()


def cmd_calibrate(a, bridge) -> int:
    with Prepared(bridge, reamp.STOMP_INPUT_USB56, transparent=True) as prep:
        fw = prep.info.get("firmware") or a.firmware
        if not fw:
            print("need --firmware (no control helper to read it)", file=sys.stderr)
            return 1
        t0 = time.perf_counter()
        cal = reamp.calibrate(fw, prep.info.get("build", ""), repeats=a.repeats)
        wall = time.perf_counter() - t0
        cal.notes = {"preset": f"{prep.info.get('label', '?')} {prep.info.get('name', '')}".strip(),
                     "chain": "all blocks bypassed in the edit buffer" if bridge else "set by hand",
                     "os": sys.platform}
    path = cal.save()
    print(json.dumps({"saved": str(path), **cal.__dict__}, indent=1))
    if a.ledger:
        # render-cost, not smoke: smoke rows are reserved for a genuine engine-smoke.
        ledger("--kind", "render-cost", "--engine", "stomp", "--dataset", "smoke", "--metric", "latency_samples",
               "--value", str(cal.latency_samples), "--wall-clock-s", f"{wall:.2f}",
               "--notes", f"reamp-capture --calibrate; impulse USB 5/6 -> USB 1/2 through a bypassed chain; "
                          f"measurements {cal.measurements}; firmware {cal.firmware}; "
                          f"level_gain_db {cal.level_gain_db}; PortAudio latency={cal.pa_latency!r}")
    return 0


def cmd_capture(a, bridge) -> int:
    cal = reamp.Calibration.load()
    dry, sr = sf.read(a.dry, dtype="float32")
    if sr != reamp.SR or dry.ndim != 1:
        print(f"need 48 kHz mono, got {sr} Hz shape {dry.shape}", file=sys.stderr)
        return 1
    with Prepared(bridge, cal.input_usb56, transparent=False) as prep:
        cal.check_firmware(prep.info.get("firmware"))
        t0 = time.perf_counter()
        wet = reamp.capture(dry, cal, tail_s=a.tail)
        wall = time.perf_counter() - t0
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out, wet, reamp.SR, subtype="FLOAT")
    peak = 20 * np.log10(max(float(np.max(np.abs(wet))), 1e-12))
    print(json.dumps({"out": str(out), "samples": len(wet), "dry_samples": len(dry),
                      "rms_dbfs": round(reamp.rms_db(wet[:len(dry)]), 2), "peak_dbfs": round(peak, 2),
                      "latency": cal.latency_samples, "gain_db": cal.level_gain_db, "wall_s": round(wall, 3),
                      "preset": f"{prep.info.get('label', '?')} {prep.info.get('name', '')}"}))
    if a.ledger:
        ledger("--kind", "render-cost", "--engine", "stomp", "--dataset", a.dataset or Path(a.dry).stem,
               "--metric", "seconds_per_render", "--value", f"{wall:.4f}", "--renders", "1",
               "--wall-clock-s", f"{wall:.3f}",
               "--notes", f"reamp-capture {Path(a.dry).name} ({len(dry) / reamp.SR:.2f} s; pre-roll {reamp.PRE_S} s "
                          f"+ tail {a.tail} s); preset {prep.info.get('label', '?')}; firmware {cal.firmware}")
    return 0


def cmd_user_di(a, _bridge) -> int:
    import sounddevice as sd

    dev = reamp.find_device()
    n_in = int(sd.query_devices(dev)["max_input_channels"])
    print(f"recording {a.seconds} s: dry USB 5/6 and processed USB 1/2 ...", file=sys.stderr)
    rec = sd.rec(int(a.seconds * reamp.SR), samplerate=reamp.SR, device=dev, channels=n_in, dtype="float32",
                 blocking=True)
    base = Path(a.out)
    base.parent.mkdir(parents=True, exist_ok=True)
    sf.write(base.with_name(base.name + "_dry.wav"), rec[:, 4], reamp.SR, subtype="FLOAT")
    sf.write(base.with_name(base.name + "_wet.wav"), reamp.to_mono(rec[:, :2]), reamp.SR, subtype="FLOAT")
    print(json.dumps({"dry_rms_dbfs": round(reamp.rms_db(rec[:, 4]), 2),
                      "wet_rms_dbfs": round(reamp.rms_db(rec[:, :2]), 2)}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dry", nargs="?")
    p.add_argument("--out")
    p.add_argument("--calibrate", action="store_true")
    p.add_argument("--user-di", action="store_true")
    p.add_argument("--seconds", type=float, default=20.0)
    p.add_argument("--tail", type=float, default=reamp.TAIL_S)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--firmware", help="firmware version when no control helper is available")
    p.add_argument("--dataset", help="ledger dataset id (default: the clip's file stem)")
    p.add_argument("--no-bridge", action="store_true", help="do not touch the pedal's control interface")
    p.add_argument("--ledger", action="store_true")
    a = p.parse_args()
    if not (a.calibrate or a.user_di or (a.dry and a.out)):
        p.error("give --calibrate, --user-di --out <base>, or <dry.wav> --out <wet.wav>")
    bridge = None if a.user_di else open_bridge(a.no_bridge)
    try:
        if a.calibrate:
            return cmd_calibrate(a, bridge)
        if a.user_di:
            return cmd_user_di(a, bridge)
        return cmd_capture(a, bridge)
    except reamp.ReampError as e:
        print(f"reamp-capture: {e}", file=sys.stderr)
        return 1
    finally:
        if bridge is not None:
            bridge.close()


if __name__ == "__main__":
    sys.exit(main())
