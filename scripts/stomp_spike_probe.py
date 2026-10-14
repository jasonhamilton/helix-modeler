#!/usr/bin/env python3
"""T003 spike probe for the HX Stomp (owner: stomp-engine). Not the reamp-capture script (that is T004).

Confirms with audio that a control change reached the pedal, and pokes the MIDI CC fallback.
Only synthetic test signals are played; nothing is sent anywhere. Recordings go to
cache/stomp/spike/ (gitignored).

  python scripts/stomp_spike_probe.py devices
  python scripts/stomp_spike_probe.py render --tag before        # plays to USB 5/6, records USB 1/2
  python scripts/stomp_spike_probe.py render --tag after
  python scripts/stomp_spike_probe.py compare before after
  python scripts/stomp_spike_probe.py cc --cc 20 --value 127 [--channel 1]
  python scripts/stomp_spike_probe.py pc --program 5 [--channel 1]
      (changes the active preset: afterwards restore the original preset, runbook step H9)
  python scripts/stomp_spike_probe.py selftest                   # offline: signal + compare maths

Needs: numpy, soundfile, sounddevice (audio), python-rtmidi (MIDI). On macOS with Python 3.14,
python-rtmidi has no wheel; build it with:
  pip install python-rtmidi --config-settings=setup-args="-Djack=false"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

SR = 48_000
ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "cache" / "stomp" / "spike"
NAME_HINT = "HX Stomp"
# 1-based channel numbers as sounddevice mappings expect them.
PLAY_TO = [5, 6]     # host -> pedal: USB 5/6 (set the preset's Input block to USB 5/6)
RECORD_FROM = [1, 2]  # pedal -> host: USB 1/2 (Path A processed)


def test_signal(seconds: float = 2.0) -> np.ndarray:
    """Deterministic -18 dBFS-ish probe: log sweep 80 Hz..8 kHz, then a pink-ish noise burst."""
    n = int(seconds * SR)
    half = n // 2
    t = np.arange(half) / SR
    f0, f1, dur = 80.0, 8000.0, half / SR
    k = np.log(f1 / f0)
    sweep = np.sin(2 * np.pi * f0 * dur / k * (np.exp(t * k / dur) - 1))
    rng = np.random.default_rng(3)
    white = rng.standard_normal(n - half)
    spec = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(white.size, 1 / SR)
    spec[1:] /= np.sqrt(freqs[1:])
    spec[0] = 0
    pink = np.fft.irfft(spec, white.size)
    pink /= np.max(np.abs(pink)) + 1e-12
    sig = np.concatenate([sweep, pink]).astype(np.float32)
    target_rms = 10 ** (-18 / 20)
    sig *= target_rms / (np.sqrt(np.mean(sig**2)) + 1e-12)
    fade = int(0.005 * SR)
    ramp = np.linspace(0, 1, fade, dtype=np.float32)
    sig[:fade] *= ramp
    sig[-fade:] *= ramp[::-1]
    return np.clip(sig, -1, 1)


def describe(x: np.ndarray) -> dict:
    x = x.astype(np.float64)
    rms = np.sqrt(np.mean(x**2)) + 1e-12
    peak = np.max(np.abs(x)) + 1e-12
    mag = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    freqs = np.fft.rfftfreq(x.size, 1 / SR)
    centroid = float(np.sum(freqs * mag) / (np.sum(mag) + 1e-12))
    return {
        "rms_dbfs": round(20 * np.log10(rms), 2),
        "peak_dbfs": round(20 * np.log10(peak), 2),
        "centroid_hz": round(centroid, 1),
        "nan": bool(np.isnan(x).any()),
        "samples": int(x.size),
    }


def changed(a: dict, b: dict) -> tuple[bool, str]:
    d_rms = b["rms_dbfs"] - a["rms_dbfs"]
    d_cent = (b["centroid_hz"] - a["centroid_hz"]) / max(a["centroid_hz"], 1.0)
    verdict = abs(d_rms) > 0.5 or abs(d_cent) > 0.02
    return verdict, f"delta rms {d_rms:+.2f} dB, delta centroid {100 * d_cent:+.1f}%"


def find_audio_device():
    import sounddevice as sd

    for i, d in enumerate(sd.query_devices()):
        if NAME_HINT.lower() in d["name"].lower() and d["max_input_channels"] >= 2 and d["max_output_channels"] >= 6:
            return i, d
    return None, None


def cmd_devices(_a) -> int:
    report: dict = {}
    try:
        import sounddevice as sd

        report["audio_devices"] = [
            {"index": i, "name": d["name"], "in": d["max_input_channels"], "out": d["max_output_channels"],
             "default_sr": d["default_samplerate"]}
            for i, d in enumerate(sd.query_devices())
        ]
        idx, dev = find_audio_device()
        report["stomp_audio"] = None if dev is None else {"index": idx, "name": dev["name"]}
        if dev is not None:
            try:
                sd.check_input_settings(device=idx, channels=max(RECORD_FROM), samplerate=SR, dtype="float32")
                sd.check_output_settings(device=idx, channels=max(PLAY_TO), samplerate=SR, dtype="float32")
                report["stomp_48k_ok"] = True
            except Exception as e:  # noqa: BLE001
                report["stomp_48k_ok"] = f"no: {e}"
    except Exception as e:  # noqa: BLE001
        report["audio_error"] = repr(e)
    try:
        import rtmidi

        report["midi_out"] = rtmidi.MidiOut().get_ports()
        report["midi_in"] = rtmidi.MidiIn().get_ports()
    except Exception as e:  # noqa: BLE001
        report["midi_error"] = repr(e)
    print(json.dumps(report, indent=2))
    return 0 if report.get("stomp_audio") else 1


def cmd_render(a) -> int:
    import sounddevice as sd
    import soundfile as sf

    idx, dev = find_audio_device()
    if dev is None:
        print("No HX Stomp audio device found (need >=2 in and >=6 out).", file=sys.stderr)
        return 1
    dry = test_signal(a.seconds)
    pre, tail = int(0.5 * SR), int(1.5 * SR)
    mono = np.concatenate([np.zeros(pre, np.float32), dry, np.zeros(tail, np.float32)])
    play = np.stack([mono, mono], axis=1)
    rec = sd.playrec(play, samplerate=SR, device=(idx, idx), dtype="float32",
                     input_mapping=RECORD_FROM, output_mapping=PLAY_TO, blocking=True)
    wet = rec[:, 0]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{a.tag}.wav"
    sf.write(path, wet, SR, subtype="FLOAT")
    stats = describe(wet)
    stats["file"] = str(path)
    (OUT_DIR / f"{a.tag}.json").write_text(json.dumps(stats, indent=2))
    print(json.dumps(stats))
    if stats["rms_dbfs"] < -60:
        print("WARNING: near-silent capture. Is the Input block set to USB 5/6 and the output on USB 1/2?",
              file=sys.stderr)
        return 2
    return 0


def cmd_compare(a) -> int:
    sa = json.loads((OUT_DIR / f"{a.a}.json").read_text())
    sb = json.loads((OUT_DIR / f"{a.b}.json").read_text())
    ok, msg = changed(sa, sb)
    print(f"{a.a} -> {a.b}: {msg}: {'CHANGED' if ok else 'no audible change detected'}")
    return 0 if ok else 3


def _midi_out():
    import rtmidi

    out = rtmidi.MidiOut()
    ports = out.get_ports()
    for i, p in enumerate(ports):
        if NAME_HINT.lower() in p.lower():
            out.open_port(i)
            return out, p
    raise SystemExit(f"No MIDI out port containing {NAME_HINT!r}; ports: {ports}")


def cmd_cc(a) -> int:
    out, name = _midi_out()
    if not (0 <= a.cc <= 127 and 0 <= a.value <= 127 and 1 <= a.channel <= 16):
        raise SystemExit("cc and value are 0..127, channel 1..16")
    out.send_message([0xB0 | (a.channel - 1), a.cc, a.value])
    print(f"sent CC {a.cc}={a.value} on channel {a.channel} to {name}")
    return 0


def cmd_pc(a) -> int:
    out, name = _midi_out()
    out.send_message([0xC0 | (a.channel - 1), a.program])
    print(f"sent Program Change {a.program} on channel {a.channel} to {name}")
    print("reminder: the active preset changed; restore the original preset when done (runbook H9)")
    return 0


def cmd_selftest(_a) -> int:
    s = test_signal()
    d = describe(s)
    assert s.size == 2 * SR and not d["nan"], d
    assert -19.5 < d["rms_dbfs"] < -16.5, d
    louder = describe(s * 1.5)
    assert changed(d, louder)[0]
    assert not changed(d, describe(s))[0]
    print("selftest ok", d)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices")
    r = sub.add_parser("render")
    r.add_argument("--tag", required=True)
    r.add_argument("--seconds", type=float, default=2.0)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    cc = sub.add_parser("cc")
    cc.add_argument("--cc", type=int, required=True)
    cc.add_argument("--value", type=int, required=True)
    cc.add_argument("--channel", type=int, default=1)
    pc = sub.add_parser("pc", help="send a Program Change; this switches the pedal's preset, so "
                        "restore the original preset afterwards (runbook step H9)")
    pc.add_argument("--program", type=int, required=True)
    pc.add_argument("--channel", type=int, default=1)
    sub.add_parser("selftest")
    a = p.parse_args()
    return {"devices": cmd_devices, "render": cmd_render, "compare": cmd_compare, "cc": cmd_cc,
            "pc": cmd_pc, "selftest": cmd_selftest}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
