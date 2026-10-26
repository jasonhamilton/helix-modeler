#!/usr/bin/env python3
"""Run Spotify Basic Pitch on the separated guitar stems, tune its settings, write MIDI (T203). Local only.

Basic Pitch is an EXTERNAL tool in its own venv (cache/venvs/basic-pitch, ONNX runtime, no TensorFlow); it is not a
project dependency. This script re-launches itself with that venv's python when basic_pitch is not importable.

    python scripts/transcribe_stems.py tune            # sweep settings against the references from the dry takes
    python scripts/transcribe_stems.py run             # transcribe all 7 stems with the chosen settings (+ MIDI)
    python scripts/transcribe_stems.py run --songs mother --settings '{"onset_threshold": 0.6}'
    python scripts/transcribe_stems.py posteriors --songs mother              # T205 (whole stem; add --seconds N to crop): cache/basic-pitch/<song>/posteriors.npz

Model outputs (frame/onset/contour posteriors) are cached in cache/basic-pitch/ so a sweep re-decodes without
re-running the network. Song audio and transcriptions stay on this machine (data/songs and cache are gitignored).
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tonematch import note_tune as NT  # noqa: E402

VENV_PY = ROOT / "cache" / "venvs" / "basic-pitch" / "bin" / "python"
CACHE = ROOT / "cache" / "basic-pitch"
STEMS = ROOT / "cache" / "stems"
SONGS = ROOT / "data" / "songs"
FPS = 22050 / 256

# Friendly ids for known songs (aliases only; any other separated song is found under its slug, see find_stems_all)
# song id -> substring of the manifest "source" that identifies its stem set
SONG_SOURCES = {
    "mother": "Mother_0-68s", "ds-sample": "DS-Sample.mp3", "teen-spirit": "Teen Spirit", "territorial": "Territorial",
    "seek-destroy": "Seek & Destroy", "satisfaction": "Satisfaction", "cherub-rock": "Cherub Rock",
}
# Song-time caps (lo, hi) for the tuning comparison against the human's take; only ids listed here are capped.
REF_CAPS = {"mother": (0.0, 68.0), "ds-sample": (30.0, 50.0)}


def discover_refs(songs_root: Path | None = None) -> dict:
    """Human clean dry takes found under data/songs/<id>-playalong/takes/: id -> {take, align, cap}.
    Per song the take with the best alignment score in its *_align.json is the reference (takes with no align json are skipped)."""
    root = SONGS if songs_root is None else songs_root
    refs = {}
    for pack in sorted(root.glob("*-playalong")):
        best = None
        for wav in sorted((pack / "takes").glob("*_dry.wav")):
            al = wav.with_name(wav.stem.removesuffix("_dry") + "_align.json")
            if not al.exists():
                continue
            try:
                sc = float(json.loads(al.read_text()).get("score", 0.0))
            except (ValueError, OSError):
                continue
            if best is None or sc > best[0]:
                best = (sc, wav, al)
        if best:
            sid = pack.name.removesuffix("-playalong")
            refs[sid] = {"take": best[1], "align": best[2], "cap": REF_CAPS.get(sid, (0.0, float("inf")))}
    return refs


# Basic Pitch library defaults (predict() signature)
DEFAULTS = {"onset_threshold": 0.5, "frame_threshold": 0.3, "minimum_note_length": 127.70, "minimum_frequency": None,
            "maximum_frequency": None, "melodia_trick": True, "multiple_pitch_bends": False}
FILTER_DEFAULTS = {"min_pitch": None, "max_pitch": None, "rel_amp": None, "restrike_gap_s": None}
# Filled in by the sweep (see docs/notes/basic-pitch-t203.md). Used by `run` unless --settings overrides.
CHOSEN = {"params": {**DEFAULTS, "onset_threshold": 0.4, "frame_threshold": 0.3, "minimum_note_length": 58.0,
                     "melodia_trick": False, "minimum_frequency": 75.0, "maximum_frequency": 1400.0},
          "filters": {**FILTER_DEFAULTS, "rel_amp": 0.5}}


# --------------------------------------------------------------------------- the Basic Pitch calls (mocked in tests)

def bp_infer(audio_path: str | Path) -> dict:
    """Network posteriors for an audio file: {'note','onset','contour'} arrays (frames at 22050/256 Hz)."""
    from basic_pitch import ICASSP_2022_MODEL_PATH
    from basic_pitch.inference import Model, run_inference
    onnx = Path(str(ICASSP_2022_MODEL_PATH)).with_suffix(".onnx")     # force the ONNX runtime (lightest)
    return run_inference(audio_path, Model(onnx))


def bp_decode(output: dict, params: dict) -> list[tuple]:
    """Posteriors -> note list [(onset_s, offset_s, pitch, amp)] with the given Basic Pitch settings."""
    import basic_pitch.note_creation as infer
    p = {**DEFAULTS, **params}
    min_len = int(np.round(p["minimum_note_length"] / 1000 * FPS))
    _, ev = infer.model_output_to_notes(
        output, onset_thresh=p["onset_threshold"], frame_thresh=p["frame_threshold"], min_note_len=min_len,
        min_freq=p["minimum_frequency"], max_freq=p["maximum_frequency"], include_pitch_bends=False,
        multiple_pitch_bends=p["multiple_pitch_bends"], melodia_trick=p["melodia_trick"])
    return [(float(a), float(b), int(c), float(d)) for a, b, c, d, *_ in ev]


def write_midi(notes, path: Path) -> None:
    import pretty_midi
    pm = pretty_midi.PrettyMIDI(initial_tempo=120)
    inst = pretty_midi.Instrument(program=29)
    for on, off, pit, amp in notes:
        inst.notes.append(pretty_midi.Note(velocity=int(np.clip(round(amp * 127), 1, 127)), pitch=int(pit),
                                           start=float(on), end=float(max(off, on + 0.01))))
    pm.instruments.append(inst)
    path.parent.mkdir(parents=True, exist_ok=True)
    pm.write(str(path))


# --------------------------------------------------------------------------- shared logic

def cached_output(audio: Path, cache_dir: Path = CACHE, infer=None) -> dict:
    infer = infer or bp_infer
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{audio.resolve()}:{audio.stat().st_size}:{audio.stat().st_mtime_ns}".encode()).hexdigest()[:16]
    f = cache_dir / f"{audio.stem}-{key}.npz"
    if f.exists():
        z = np.load(f)
        return {k: z[k] for k in z.files}
    out = infer(audio)
    np.savez_compressed(f, **out)
    return out


def find_stems(stems_root: Path = STEMS) -> dict[str, Path]:
    """song id -> guitar.wav of its htdemucs_6s stem set, via the manifests."""
    found = {}
    for man in sorted(stems_root.glob("*/htdemucs_6s/manifest.json")):
        src = json.loads(man.read_text()).get("source", "")
        for sid, sub in SONG_SOURCES.items():
            if sub in src and (man.parent / "guitar.wav").exists():
                found[sid] = man.parent / "guitar.wav"
    return found


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def find_stems_all(stems_root: Path = STEMS) -> dict[str, Path]:
    """find_stems plus every other separated song under its own slug id (the source file's stem, lower-case, hyphenated)."""
    found = {}
    for man in sorted(stems_root.glob("*/htdemucs_6s/manifest.json")):
        if (man.parent / "guitar.wav").exists():
            src = json.loads(man.read_text()).get("source", "")
            if src:
                found[_slug(Path(src).stem)] = man.parent / "guitar.wav"
    found.update(find_stems(stems_root))
    return found


def resolve_stem(song_id: str, stem: Path | None = None) -> Path:
    """The guitar stem for a song id: explicit path, else data/songs/<id>-rebuilt/stem.wav, else the separated stems cache."""
    if stem is not None:
        return Path(stem)
    local = SONGS / f"{song_id}-rebuilt" / "stem.wav"
    if local.exists():
        return local
    found = find_stems_all()
    if song_id not in found:
        raise SystemExit(f"no guitar stem for {song_id!r}: pass --stem PATH, or run separate.py on the song first "
                         f"(known: {', '.join(sorted(found)) or 'none'})")
    return found[song_id]


def apply_settings(output: dict, settings: dict, decode=None) -> list[tuple]:
    decode = decode or bp_decode
    s = {"params": {**DEFAULTS, **settings.get("params", {})}, "filters": {**FILTER_DEFAULTS, **settings.get("filters", {})}}
    return NT.filter_notes(decode(output, s["params"]), **s["filters"])


def reference_notes(song: str, decode=None, infer=None, ref_params: dict | None = None) -> tuple[list, tuple[float, float]]:
    """Basic Pitch on the human's clean dry take, shifted to song time; also the song-time span the take covers."""
    refs = discover_refs()
    if song not in refs:
        raise SystemExit(f"no aligned human take for {song!r} under data/songs/{song}-playalong/takes/ (run align_takes.py first)")
    r = refs[song]
    out = cached_output(r["take"], infer=infer)
    notes = apply_settings(out, {"params": ref_params or {"minimum_frequency": 75.0, "maximum_frequency": 1400.0}}, decode)
    t0 = json.loads(r["align"].read_text())["alignment"]["take_sample0_song_s"]
    n_s = out["note"].shape[0] / FPS
    lo, hi = max(r["cap"][0], t0), min(r["cap"][1], t0 + n_s)
    return NT.crop(NT.shift(notes, t0), lo, hi), (lo, hi)


def score(ref, est, span, tol=0.05) -> dict:
    est = NT.crop(est, *span)
    ex = NT.f_measure(ref, est, tol)
    oc = NT.f_measure(ref, est, tol, octave_tolerant=True)
    return {**ex, "f_octave": oc["f"], "p_octave": oc["precision"], "r_octave": oc["recall"]}


# --------------------------------------------------------------------------- commands

CORE_GRID = {
    "onset_threshold": [0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
    "frame_threshold": [0.2, 0.3, 0.4, 0.5],
    "minimum_note_length": [58.0, 90.0, 127.7, 180.0, 260.0],
    "range": [(None, None), (75.0, 1400.0), (80.0, 1000.0), (82.0, 700.0)],
    "melodia_trick": [True, False],
}
FILTER_GRID = {"min_pitch": [None, 40, 45], "max_pitch": [None, 76, 84, 88], "rel_amp": [None, 0.3, 0.5, 0.7],
               "restrike_gap_s": [None, 0.05, 0.12]}


def sweep(songs: dict, grid: dict = CORE_GRID, fgrid: dict = FILTER_GRID, top_k: int = 12, decode=None,
          log=print) -> dict:
    """songs: {id: {'out': posteriors, 'ref': notes, 'span': (lo, hi)}}. Returns rows for the core grid, the filters on
    the top_k core settings, and the default row. Ranking key: mean of the songs' F (exact pitch, 50 ms)."""
    decode = decode or bp_decode

    def evaluate(settings, cache_notes={}):
        per = {}
        for sid, d in songs.items():
            per[sid] = score(d["ref"], apply_settings(d["out"], settings, decode), d["span"])
        mean_f = float(np.mean([v["f"] for v in per.values()]))
        return {"settings": settings, "per_song": per, "mean_f": mean_f,
                "mean_p": float(np.mean([v["precision"] for v in per.values()])),
                "mean_r": float(np.mean([v["recall"] for v in per.values()])),
                "mean_f_octave": float(np.mean([v["f_octave"] for v in per.values()])),
                "pooled": NT.pooled(list(per.values()))}

    rows = []
    keys = list(grid)
    for combo in itertools.product(*[grid[k] for k in keys]):
        c = dict(zip(keys, combo))
        lo, hi = c.pop("range")
        c["minimum_frequency"], c["maximum_frequency"] = lo, hi
        rows.append(evaluate({"params": c, "filters": {}}))
    log(f"core grid: {len(rows)} settings")
    default = evaluate({"params": dict(DEFAULTS), "filters": {}})
    best = sorted(rows, key=lambda r: -r["mean_f"])[:top_k]
    frows = []
    fkeys = list(fgrid)
    for b in best:
        for combo in itertools.product(*[fgrid[k] for k in fkeys]):
            f = dict(zip(fkeys, combo))
            frows.append(evaluate({"params": b["settings"]["params"], "filters": f}))
    log(f"filter grid: {len(frows)} settings")
    return {"default": default, "core": rows, "filtered": frows}


def window_crop_start(t: float) -> tuple[int, float]:
    """First frame (a multiple of the model's window length) at or before time t, and the time to add to cropped
    frame times so they equal full-file times."""
    from basic_pitch.constants import ANNOT_N_FRAMES, AUDIO_N_SAMPLES, AUDIO_SAMPLE_RATE, FFT_HOP
    k = max(0, int(t * FPS) // ANNOT_N_FRAMES)
    w_off = (FFT_HOP / AUDIO_SAMPLE_RATE) * (ANNOT_N_FRAMES - (AUDIO_N_SAMPLES / FFT_HOP)) + 0.0018
    return k * ANNOT_N_FRAMES, k * ANNOT_N_FRAMES / FPS - k * w_off


def cmd_tune(a) -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    refs = discover_refs()
    if not refs:
        raise SystemExit("tune needs at least one aligned human take under data/songs/<id>-playalong/takes/ (see align_takes.py)")
    songs = {}
    for sid in refs:
        out = cached_output(resolve_stem(sid))
        ref, span = reference_notes(sid)
        # decode only the reference span (+1 s margin each side) to keep the sweep fast; times are then relative to f0
        # (frame times drift by a fixed amount per 2 s model window, so the crop starts on a window boundary and the
        # time of cropped frames is corrected exactly: t_full = t_crop + f0/FPS - k*window_offset)
        f0, off = window_crop_start(span[0] - 1.0)
        f1 = int((span[1] + 1.0) * FPS)
        out = {k: v[f0:f1] for k, v in out.items()}
        ref, span = NT.shift(ref, -off), (span[0] - off, span[1] - off)
        songs[sid] = {"out": out, "ref": ref, "span": span, "off": off}
        print(sid, "ref notes", len(ref), "span", [round(x, 1) for x in span])
    res = sweep(songs)
    # leave-one-song-out: choose on one song, report on the other
    loso = {}
    for held in songs:
        other = [s for s in songs if s != held][0]
        best = max(res["core"] + res["filtered"], key=lambda r: r["per_song"][other]["f"])
        loso[held] = {"chosen_on": other, "settings": best["settings"], "f_on_held": best["per_song"][held]["f"],
                      "default_f_on_held": res["default"]["per_song"][held]["f"]}
    res["leave_one_out"] = loso
    # reference-sensitivity and tolerance check for the overall best
    allrows = res["core"] + res["filtered"]
    res["best"] = max(allrows, key=lambda r: r["mean_f"])
    sens = {}
    for name, rp in {"ref_default_range": {}, "ref_minlen80": {"minimum_frequency": 75.0, "maximum_frequency": 1400.0,
                                                              "minimum_note_length": 80.0},
                     "ref_onset0.6": {"minimum_frequency": 75.0, "maximum_frequency": 1400.0, "onset_threshold": 0.6}}.items():
        row = {}
        for sid in songs:
            ref, span = reference_notes(sid, ref_params={**rp})
            ref, span = NT.shift(ref, -songs[sid]["off"]), (span[0] - songs[sid]["off"], span[1] - songs[sid]["off"])
            for tag, st in (("default", res["default"]["settings"]), ("best", res["best"]["settings"])):
                row[f"{sid}_{tag}"] = score(ref, apply_settings(songs[sid]["out"], st), span)["f"]
        sens[name] = row
    res["reference_sensitivity"] = sens
    tol = {}
    for t in (0.03, 0.05, 0.1):
        tol[str(t)] = {sid: {tag: score(songs[sid]["ref"], apply_settings(songs[sid]["out"], st), songs[sid]["span"], t)["f"]
                             for tag, st in (("default", res["default"]["settings"]), ("best", res["best"]["settings"]))}
                       for sid in songs}
    res["tolerance"] = tol
    # human take-to-take ceiling: how well do two takes of the same part agree under this scoring?
    res["take_ceiling"] = take_ceiling()
    outp = Path(a.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(res, indent=1, default=str))
    print("default", json.dumps({k: res["default"][k] for k in ("mean_f", "mean_p", "mean_r", "mean_f_octave")}))
    print("best   ", json.dumps(res["best"]["settings"]), json.dumps({k: res["best"][k] for k in ("mean_f", "mean_p", "mean_r", "mean_f_octave")}))
    return 0


def take_ceiling() -> dict:
    """Agreement between the human's own takes of the same part (a reference-quality ceiling)."""
    out = {}
    for pack in sorted(SONGS.glob("*-playalong")):
        sid = pack.name.removesuffix("-playalong")
        stamps = sorted({w.name.split("_take")[0] for w in (pack / "takes").glob("*_take*_dry.wav")})
        if not stamps:
            continue
        sets = {}
        for n in (1, 2, 3):                       # the latest session's takes 1 to 3
            wav = pack / "takes" / f"{stamps[-1]}_take{n}_dry.wav"
            al = wav.with_name(f"{stamps[-1]}_take{n}_align.json")
            if not (wav.exists() and al.exists()):
                continue
            o = cached_output(wav)
            nts = apply_settings(o, {"params": {"minimum_frequency": 75.0, "maximum_frequency": 1400.0}})
            t0 = json.loads(al.read_text())["alignment"]["take_sample0_song_s"]
            sets[n] = (NT.shift(nts, t0), t0, t0 + o["note"].shape[0] / FPS)
        res = {}
        for i, j in itertools.permutations(sets, 2):
            lo, hi = max(sets[i][1], sets[j][1]), min(sets[i][2], sets[j][2])
            if hi - lo < 5:
                continue
            res[f"take{j}_vs_ref_take{i}"] = {k: round(v, 3) for k, v in
                                               NT.f_measure(NT.crop(sets[i][0], lo, hi), NT.crop(sets[j][0], lo, hi)).items() if k in ("f", "precision", "recall")}
        out[sid] = res
    return out


# Historical, Mother-specific: facts as passed on by the lead (from the human's tab header): EADGBE, B minor, no capo, intro 128 bpm.
B_NAT_MINOR = [11, 1, 2, 4, 6, 7, 9]            # B C# D E F# G A
BORROWED = [10, 8, 5]                            # A# (harmonic minor), G# (dorian), F (blues flat five)


def tempo_curve(stem: Path, win_s: float = 16.0, hop_s: float = 8.0) -> list[dict]:
    """Onset-autocorrelation tempo per window on the stem: the strongest lag between 0.25 and 2.2 s, reported as the
    implied beats-per-minute for one lag = one beat, and for one lag = two beats (so 0.94 s reads as 64 or 128)."""
    import importlib.util
    import soundfile as sf
    spec = importlib.util.spec_from_file_location("align_takes", ROOT / "scripts" / "align_takes.py")
    al = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(al)
    x, _ = sf.read(str(stem), dtype="float32", always_2d=True)
    env = al.onset_env(x.mean(axis=1))
    env = np.maximum(env - np.convolve(env, np.ones(50) / 50, "same"), 0)
    fps = al.SR / al.HOP
    rows = []
    for lo in np.arange(0, len(env) / fps - win_s + 1e-6, hop_s):
        seg = env[int(lo * fps): int((lo + win_s) * fps)]
        ac = np.correlate(seg - seg.mean(), seg - seg.mean(), "full")[len(seg) - 1:]
        ac = ac / (ac[0] + 1e-12)
        lags = np.arange(len(ac)) / fps
        idx = [i for i in range(1, len(ac) - 1) if 0.25 < lags[i] < 2.2 and ac[i] > ac[i - 1] and ac[i] >= ac[i + 1]]
        if idx:
            i = max(idx, key=lambda k: ac[k])
            rows.append({"t0": float(lo), "t1": float(lo + win_s), "lag_s": round(float(lags[i]), 3), "ac": round(float(ac[i]), 3),
                         "bpm_if_two_beats": round(120 / float(lags[i]), 1)})
    return rows


def cmd_facts(a) -> int:
    """Historical, Mother-specific. Mother song-facts diagnostics: pitch floor, key flags (flag only), tempo-over-time check."""
    stem = resolve_stem("mother")
    out = cached_output(stem)
    ref, span = reference_notes("mother")
    notes = apply_settings(out, CHOSEN)
    pit = [n[2] for n in notes]
    flags = NT.flag_out_of_scale(notes, B_NAT_MINOR + BORROWED)
    sc = score(ref, notes, span)
    res = {"pitch": {"lowest_midi": int(min(pit)), "n_below_E2": int(sum(p < 40 for p in pit)), "n_notes": len(notes),
                     "pc_histogram": NT.pc_histogram(notes)},
           "key_flags": {"allowed_pcs": B_NAT_MINOR + BORROWED, "flagged": int(sum(flags)),
                         "flagged_fraction": round(sum(flags) / len(notes), 3),
                         "flagged_pcs": NT.pc_histogram([n for n, f in zip(notes, flags) if f]),
                         "flagged_if_natural_minor_only": int(sum(NT.flag_out_of_scale(notes, B_NAT_MINOR)))},
           "flag_vs_reference": {"precision_all": round(sc["precision"], 3),
                                 "precision_flagged": round(NT.f_measure(ref, [n for n, f in zip(notes, flags) if f])["precision"], 3) if any(flags) else None,
                                 "precision_unflagged": round(NT.f_measure(ref, [n for n, f in zip(notes, flags) if not f])["precision"], 3)},
           "tempo_windows": tempo_curve(stem)}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    return 0


def cmd_run(a) -> int:
    settings = json.loads(a.settings) if a.settings else CHOSEN
    if a.settings:
        settings = {"params": {**CHOSEN["params"], **settings.get("params", settings if "filters" not in settings else {})},
                    "filters": {**CHOSEN["filters"], **settings.get("filters", {})}}
    ids = a.songs or sorted(find_stems_all())
    if a.stem and len(ids) != 1:
        raise SystemExit("--stem needs exactly one --songs id")
    for sid in ids:
        out = cached_output(resolve_stem(sid, a.stem))
        notes = apply_settings(out, settings)
        d = SONGS / f"{sid}-rebuilt"
        write_midi(notes, d / "transcription.mid")
        (d / "transcription_settings.json").write_text(json.dumps(settings, indent=1))
        print(f"{sid}: {len(notes)} notes -> {d / 'transcription.mid'}")
    return 0


def posteriors_path(song: str, root: Path = CACHE) -> Path:
    return root / song / "posteriors.npz"


def load_posteriors(song: str, seconds: float | None = None, root: Path = CACHE) -> dict | None:
    """Cached {'note','onset','fps'} for the first `seconds` of the song's stem (None: whole stem), or None if absent
    or cached for a different length (a file without a 'seconds' field is treated as unknown length)."""
    f = posteriors_path(song, root)
    if not f.exists():
        return None
    z = np.load(f)
    if "note" not in z.files or "onset" not in z.files or "seconds" not in z.files:
        return None
    if seconds is not None and abs(float(z["seconds"]) - seconds) > 1e-6:
        return None
    return {"note": z["note"], "onset": z["onset"], "fps": float(z["fps"]) if "fps" in z.files else FPS,
            "seconds": float(z["seconds"])}


def cmd_posteriors(a) -> int:
    """Basic Pitch note and onset posteriors for the first --seconds of each song's stem, cached per song."""
    import soundfile as sf
    if not a.songs:
        raise SystemExit("posteriors needs --songs ID [ID ...]")
    for sid in a.songs:
        f = posteriors_path(sid)
        if not a.force and a.seconds and load_posteriors(sid, a.seconds) is not None:
            print(f"{sid}: cached {f}")
            continue
        x, sr = sf.read(str(resolve_stem(sid, a.stem)), dtype="float32", always_2d=True)
        if a.seconds:
            x = x[: int(a.seconds * sr)]
        crop = f.parent / f"stem{int(a.seconds) if a.seconds else 'full'}.wav"
        crop.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(crop), x.mean(axis=1), sr, subtype="PCM_16")
        out = bp_infer(crop)
        np.savez_compressed(f, note=out["note"], onset=out["onset"], fps=FPS,
                            seconds=float(a.seconds if a.seconds else len(x) / sr))
        print(f"{sid}: {out['note'].shape[0]} frames -> {f}")
    return 0


def ensure_posteriors(song: str, seconds: float | None) -> dict:
    """Cached posteriors, computed through the Basic Pitch venv (a subprocess) when missing. For other scripts."""
    got = load_posteriors(song, seconds)
    if got is not None:
        return got
    import subprocess
    py = VENV_PY if VENV_PY.exists() else Path(sys.executable)
    cmd = [str(py), str(Path(__file__).resolve()), "posteriors", "--songs", song, "--force"]
    if seconds:
        cmd += ["--seconds", str(seconds)]
    subprocess.run(cmd, check=True)
    return load_posteriors(song, seconds)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tune")
    t.add_argument("--out", default=str(ROOT / "cache" / "basic-pitch" / "sweep.json"))
    r = sub.add_parser("run")
    r.add_argument("--songs", nargs="*", metavar="ID", help="song ids (default: every separated song found in cache/stems)")
    r.add_argument("--stem", type=Path, default=None, help="guitar stem wav (with exactly one --songs id); default: found via the id")
    r.add_argument("--settings", help="JSON: {'params': {...}, 'filters': {...}} (merged over the chosen settings)")
    f = sub.add_parser("facts", help="Mother song-facts diagnostics (pitch floor, key flags, tempo over time)")
    f.add_argument("--out", default=str(ROOT / "cache" / "basic-pitch" / "mother_facts.json"))
    q = sub.add_parser("posteriors", help="cache Basic Pitch note/onset posteriors for the first N seconds of a stem (T205)")
    q.add_argument("--songs", nargs="*", metavar="ID", help="song ids (required)")
    q.add_argument("--stem", type=Path, default=None, help="guitar stem wav (with exactly one --songs id)")
    q.add_argument("--seconds", type=float, default=None, help="crop to the first N seconds (default: the whole stem)")
    q.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    try:
        import basic_pitch  # noqa: F401
    except ImportError:
        if VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve():
            os.execv(str(VENV_PY), [str(VENV_PY), *sys.argv])
        print("basic_pitch is not installed; see docs/notes/basic-pitch-t203.md", file=sys.stderr)
        return 2
    return {"tune": cmd_tune, "run": cmd_run, "facts": cmd_facts, "posteriors": cmd_posteriors}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
