#!/usr/bin/env python3
"""engine-smoke: detect engines, load a known preset, render the 2 s DI, check, write ledger rows.

Spec: .claude/skills/engine-smoke, contracts/engine-interface.md. Implements, does not change, the interface.
Exit 0 on pass, 1 on a failed check, 2 when no engine is found (no ledger row written).
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DI = ROOT / "fixtures" / "smoke" / "di_2s.wav"
PRESET = ROOT / "fixtures" / "smoke" / "known.hlx"
SR = 48_000
NO_ENGINE = "No engine found. Install Helix Native or connect an HX Stomp; at least one is required."


def read_wav_f32(path: Path) -> np.ndarray:
    b = path.read_bytes()
    i = 12
    fmt = None
    while i + 8 <= len(b):
        cid, n = b[i:i + 4], struct.unpack("<I", b[i + 4:i + 8])[0]
        body = b[i + 8:i + 8 + n]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            assert fmt and fmt[0] == 3 and fmt[1] == 1 and fmt[2] == SR and fmt[5] == 32, f"need 48k f32 mono: {fmt}"
            return np.frombuffer(body, dtype="<f4").astype(np.float32)
        i += 8 + n + (n & 1)
    raise ValueError("no data chunk")


def ledger(*args: str) -> None:
    env = dict(os.environ)
    env.setdefault("TONEMATCH_AGENT", "benchmark")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), *args], check=True, env=env)


def db(x: float) -> float:
    return 20 * np.log10(max(x, 1e-12))


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0


def check(engine) -> tuple[list[str], dict]:
    """Return (failures, measurements). `engine` is an instantiated Engine with the known preset loaded."""
    fails: list[str] = []
    dry = read_wav_f32(DI)
    t0 = time.perf_counter()
    out = engine.render(dry)
    wall = time.perf_counter() - t0
    silence = engine.render(np.zeros(SR // 2, dtype=np.float32))
    cost, caps = engine.cost(), engine.capabilities()
    if len(out) != len(dry):
        fails.append(f"length {len(out)} != {len(dry)}")
    if not np.all(np.isfinite(out)):
        fails.append("NaN or inf in output")
    peak = db(float(np.max(np.abs(out)))) if len(out) else -999.0
    if not -30 <= peak <= -1:
        fails.append(f"peak {peak:.1f} dBFS outside -30..-1")
    tail = silence[-int(0.1 * SR):]
    margin = db(rms(out)) - db(rms(tail))
    if margin < 20:
        fails.append(f"RMS only {margin:.1f} dB above silence-through-preset (need 20)")
    if not caps.firmware_version:
        fails.append("empty firmware_version")
    if not caps.models:
        fails.append("empty models")
    return fails, {"peak": peak, "wall": wall, "cost": cost.seconds_per_render}


def main() -> int:
    from tonematch.engines import registry

    found = registry.detect()
    want = os.environ.get("TONEMATCH_ENGINE", "auto").lower()
    names = sorted(found) if want == "auto" else [n for n in [want] if n in found]
    if not names:
        print(NO_ENGINE)
        return 2
    if not DI.exists():
        print(f"FAIL: missing {DI}")
        return 1
    rc = 0
    for name in names:
        engine = None
        try:
            from tonematch.preset import load_hlx  # type: ignore[attr-defined]  # preset agent, T006

            if not PRESET.exists():
                raise FileNotFoundError(f"{PRESET} missing (needs golden templates, T010)")
            engine = found[name]()
            engine.load(load_hlx(PRESET))
            fails, m = check(engine)
        except Exception as e:  # noqa: BLE001
            fails, m = [f"{type(e).__name__}: {e}"], {}
        finally:
            if engine is not None:
                engine.close()  # Stomp restores original preset; read-back check is the stomp engine's to expose
        if fails:
            print(f"{name}: FAIL " + "; ".join(fails))
            rc = 1
            continue
        print(f"{name}: OK peak {m['peak']:.1f} dBFS, {m['cost']:.3f} s/render")
        ledger("--kind", "smoke", "--engine", name, "--dataset", "smoke", "--metric", "peak_dbfs", "--value", f"{m['peak']:.2f}")
        ledger("--kind", "render-cost", "--engine", name, "--dataset", "smoke", "--metric", "seconds_per_render",
               "--value", f"{m['cost']:.4f}", "--renders", "1", "--wall-clock-s", f"{m['wall']:.3f}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
