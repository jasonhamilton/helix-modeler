#!/usr/bin/env python3
"""reamp-capture: send a dry clip through the HX Stomp over USB and capture the result (owner: stomp-engine).

  python scripts/reamp_capture.py --calibrate [--ledger]          # impulse latency x3 + level reference
  python scripts/reamp_capture.py dry.wav --out wet.wav [--ledger]  # one reamp, trimmed and level-matched
  python scripts/reamp_capture.py --user-di --play data/songs/X.mp3 --section 32.45:46.45 \\
      --target 36.45:46.45 --countin 4 --takes 3                  # play-along DI, see below
  python scripts/reamp_capture.py --user-di --seconds 20            # free play after a count-in

--user-di plays the song section (and a click count-in on the song's beat grid) on host USB out 1/2,
which the Stomp sends to its outputs and headphones, while one duplex stream records the dry Main
L/R inputs (USB in 5/6) and the processed Path A (USB in 1/2). Takes are trimmed by the calibrated
round trip so sample 0 is the section start on the song timeline, and saved with a JSON (alignment,
levels, clipping, silence) under cache/user_di/<song-id>/take<k>_{dry,wet}.wav. It never writes to
the pedal: the control helper, if present, is only read (firmware, Input routing, unchanged check).

Skill: .claude/skills/reamp-capture. Calibration: cache/stomp/latency.json (gitignored).

With the control helper (TONEMATCH_STOMPD, see tonematch.engines.stomp_bridge) the script reads the
firmware, routes the Input block to USB 5/6 and, for --calibrate, bypasses every block so the chain
is transparent. It puts back exactly what it changed (edit buffer only, nothing is saved). Without
the helper, set the pedal up by hand and pass --firmware.
"""
from __future__ import annotations

import argparse
import datetime as dt
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

from tonematch import reamp, user_di
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


def _pedal_snapshot(bridge) -> dict:
    """Read-only: firmware, preset and chain. --user-di never writes to the pedal."""
    if bridge is None:
        return {}
    info = bridge.call("info")
    chain = bridge.call("chain")
    inp = next((s for s in chain["slots"] if s["kind"] == "Input" and s["pos"] == 0), {})
    return {"info": info, "chain": chain, "input_routing": inp.get("routing")}


def _out_dir(a, song_id: str) -> Path:
    out = Path(a.out).resolve() if a.out else user_di.USER_DI_DIR / song_id
    inside_repo = out == ROOT or ROOT in out.parents
    if inside_repo and not (ROOT / "cache") in [out, *out.parents]:
        raise reamp.ReampError(f"{out} is inside the repo but not under cache/ (gitignored); song-derived audio "
                               "stays local, so takes go under cache/user_di/ or outside the repo")
    return out


def _wait_for_take(a, k: int, n_takes: int, first: bool) -> None:
    if a.no_prompt or not sys.stdin.isatty():
        time.sleep(1.0 if first else a.gap)
        return
    input(f"take {k} ({n_takes} to go): press Enter to start the count-in, Ctrl-C to stop ... ")


def cmd_user_di(a, bridge) -> int:
    """Play-along DI capture: song section + count-in on USB 1/2, record dry 5/6 and wet 1/2."""
    from tonematch.engines.stomp import chain_signature
    from tonematch.separation.audio import file_hash

    cal = reamp.Calibration.load(Path(a.calibration)) if a.calibration else reamp.Calibration.load()
    snap0 = _pedal_snapshot(bridge)
    fw = (snap0.get("info") or {}).get("firmware")
    cal.check_firmware(fw)  # also refuses a calibration made with other stream buffering
    pre_warn = []
    if snap0.get("input_routing") in (reamp.STOMP_INPUT_USB56, 7):
        pre_warn.append(f"the Input block is on USB (value {snap0['input_routing']}), so the wet will not hear the "
                        "guitar; set it to Main L/R or Guitar by hand (this script does not change the preset)")

    sec, sections_path, sections = None, None, None
    if a.play:
        sec = user_di.load_section(a.play, user_di.parse_span(a.section), a.song_offset, a.song_gain_db)
        sec.target = user_di.parse_span(a.target)
        sections_path = Path(a.sections) if a.sections else user_di.find_sections_json(a.play)
        sections = json.loads(sections_path.read_text()) if sections_path else None
        start_s = sec.song_offset_s
    else:
        start_s = 0.0
    grid = user_di.beat_grid(start_s, a.bpm, sections)
    pb = user_di.build_playback(sec, a.countin, grid, a.tail, a.click_dbfs, seconds=a.seconds)
    song_id = a.song_id or (Path(a.play).stem if a.play else "free")
    out_dir = _out_dir(a, song_id)
    src_hash = file_hash(a.play) if a.play else None
    clicks_song_s = [round(start_s + (i - pb.countin_n) / reamp.SR, 4) for i in pb.click_at]

    for m in pre_warn:
        print(f"WARNING: {m}", file=sys.stderr)
    print(json.dumps({"song_id": song_id, "out_dir": str(out_dir), "play_s": round(len(pb.play) / reamp.SR, 2),
                      "section_song_s": [round(start_s, 3), round(start_s + pb.n_section / reamp.SR, 3)],
                      "countin": a.countin, "beat_s": round(grid.beat_s, 4), "grid": grid.source,
                      "latency_samples": cal.latency_samples, "clicks_song_s": clicks_song_s}), file=sys.stderr)
    if a.dry_run:
        print("reamp-capture: --dry-run, nothing played or recorded", file=sys.stderr)
        return 0
    dev = reamp.find_device()
    saved, any_zero = [], False
    for j in range(a.takes):
        k = user_di.next_take_index(out_dir)
        _wait_for_take(a, k, a.takes - j, first=j == 0)
        t0 = time.perf_counter()
        rec = reamp.duplex(pb.play, user_di.SONG_TO, dev)
        wall = time.perf_counter() - t0
        take = user_di.split_take(rec, pb, cal.latency_samples, a.extra_latency)
        rep, warns = user_di.check_take(take, pb, sec, sec.target if sec else None)
        any_zero |= rep["dry"]["digital_zero"]
        meta = {
            "take": k, "song_id": song_id, "source": str(a.play) if a.play else None, "source_sha16": src_hash,
            "section_s": user_di.parse_span(a.section), "target_s": sec.target if sec else None,
            "alignment": {
                "take_sample0_song_s": round(start_s, 6),
                "song_offset_samples": round(start_s * reamp.SR),
                "latency_samples": cal.latency_samples, "extra_latency_samples": a.extra_latency,
                "recording_offset_samples": take.offset_samples, "countin_samples": pb.countin_n,
                "calibration": {"path": a.calibration or str(reamp.CAL_PATH), "date": cal.date,
                                "firmware": cal.firmware, "pa_latency": cal.pa_latency},
                "note": "song time of take sample i = take_sample0_song_s + i / 48000; dry and wet are both "
                        "trimmed by the USB round trip (see dry_to_wet for the measured residual lag)",
            },
            "countin": {"beats": a.countin, "beat_s": round(grid.beat_s, 5), "bpm": round(60 / grid.beat_s, 2),
                        "grid_source": grid.source, "sections_json": str(sections_path) if sections_path else None,
                        "click_song_s": clicks_song_s},
            "channels": {"dry": "USB in 5 (Main L), mono", "dry_right": "USB in 6 (Main R), levels only",
                         "wet": "USB in 1/2 (Path A), stereo", "playback": "USB out 1/2 (Stomp outputs/phones)"},
            "samples": len(take.dry), "section_samples": pb.n_section, "tail_samples": pb.tail_n,
            "song_gain_db": a.song_gain_db, "click_dbfs": a.click_dbfs,
            "levels": rep, "warnings": warns, "wall_s": round(wall, 2),
            "pedal": {"firmware": fw, "preset": f"{(snap0.get('info') or {}).get('label', '?')} "
                                                f"{(snap0.get('info') or {}).get('name', '')}".strip() if snap0 else None,
                      "input_routing": snap0.get("input_routing"), "read_only": True},
            "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "os": sys.platform,
        }
        paths = user_di.save_take(out_dir, k, take, meta)
        saved.append((paths["json"], meta))
        print(json.dumps({"take": k, "dry": str(paths["dry"]), "wet": str(paths["wet"]),
                          "dry_rms_dbfs": rep["dry"]["rms_dbfs"], "dry_peak_dbfs": rep["dry"]["peak_dbfs"],
                          "wet_rms_dbfs": rep["wet"]["rms_dbfs"], "warnings": len(warns)}))
        for m in warns:
            print(f"WARNING take {k}: {m}", file=sys.stderr)

    if bridge is not None:  # read-only session: confirm nothing about the preset changed
        same = chain_signature(_pedal_snapshot(bridge)["chain"]) == chain_signature(snap0["chain"])
        for path, meta in saved:
            meta["pedal"]["unchanged_after_session"] = same
            path.write_text(json.dumps(meta, indent=2, default=str))
        if not same:
            print("WARNING: the preset differs from the start of the session (knobs turned on the pedal?); "
                  "this script made no edits", file=sys.stderr)
    return 3 if any_zero else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dry", nargs="?")
    p.add_argument("--out")
    p.add_argument("--calibrate", action="store_true")
    p.add_argument("--user-di", action="store_true")
    p.add_argument("--seconds", type=float, default=20.0, help="--user-di without --play: free-play length")
    g = p.add_argument_group("--user-di play-along (song stays local)")
    g.add_argument("--play", help="the human's song or a section of it (any format soundfile reads)")
    g.add_argument("--section", help="song seconds to cut from --play, e.g. 32.45:46.45")
    g.add_argument("--song-offset", type=float, default=0.0, help="song time where --play starts (pre-cut file)")
    g.add_argument("--target", help="song seconds of the target window, e.g. 36.45:46.45 (levels per window)")
    g.add_argument("--song-id", help="default: the --play file stem; takes go to cache/user_di/<song-id>/")
    g.add_argument("--countin", type=int, default=4, help="count-in clicks before the section")
    g.add_argument("--bpm", type=float, help="click tempo; default: the separator's beat grid, else 120")
    g.add_argument("--sections", help="sections.json with beat_times (default: cache/stems/<hash>/*/)")
    g.add_argument("--takes", type=int, default=1)
    g.add_argument("--gap", type=float, default=3.0, help="seconds between takes with --no-prompt")
    g.add_argument("--no-prompt", action="store_true", help="do not wait for Enter between takes")
    g.add_argument("--song-gain-db", type=float, default=-6.0)
    g.add_argument("--click-dbfs", type=float, default=-12.0, help="click peak level")
    g.add_argument("--extra-latency", type=int, default=0, help="samples added to the calibrated round trip")
    g.add_argument("--dry-run", action="store_true", help="decode, cut and plan the playback; no audio device")
    g.add_argument("--calibration", help="latency.json (default cache/stomp/latency.json)")
    p.add_argument("--tail", type=float, default=reamp.TAIL_S, help="seconds kept after the clip or section")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--firmware", help="firmware version when no control helper is available")
    p.add_argument("--dataset", help="ledger dataset id (default: the clip's file stem)")
    p.add_argument("--no-bridge", action="store_true", help="do not touch the pedal's control interface")
    p.add_argument("--ledger", action="store_true")
    stomp_bridge.add_usb_reset_arg(p)
    a = p.parse_args()
    stomp_bridge.apply_usb_reset_arg(a)
    if not (a.calibrate or a.user_di or (a.dry and a.out)):
        p.error("give --calibrate, --user-di [--play <song>], or <dry.wav> --out <wet.wav>")
    if a.play and not a.user_di:
        p.error("--play needs --user-di")
    if a.user_di and a.dry_run:
        return _run(a)          # no audio device, no pedal
    from tonematch import pedal_lock

    # device check, then the shared pedal lock for the whole run: the audio uses the pedal even with --no-bridge
    with pedal_lock.session("reamp_capture " + ("calibrate" if a.calibrate else "user-di" if a.user_di else "capture")):
        return _run(a)


def _run(a) -> int:
    if a.user_di:
        bridge = None
        if not a.no_bridge and stomp_bridge.helper_path() is not None:
            try:  # read-only for --user-di: firmware check and Input routing warning
                bridge = stomp_bridge.StompBridge()
            except stomp_bridge.BridgeError as e:
                print(f"reamp-capture: control helper unavailable ({e}); continuing without the pedal check",
                      file=sys.stderr)
    else:
        bridge = open_bridge(a.no_bridge)
    try:
        if a.calibrate:
            return cmd_calibrate(a, bridge)
        if a.user_di:
            return cmd_user_di(a, bridge)
        return cmd_capture(a, bridge)
    except (reamp.ReampError, ValueError, FileExistsError) as e:
        print(f"reamp-capture: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("reamp-capture: stopped; the take in progress was not saved", file=sys.stderr)
        return 130
    finally:
        if bridge is not None:
            bridge.close()


if __name__ == "__main__":
    sys.exit(main())
