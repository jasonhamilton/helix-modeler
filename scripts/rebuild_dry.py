#!/usr/bin/env python3
"""Rebuild a dry DI track from a MIDI transcription out of real Guitar-TECHS DI notes (T201). Listening only.

Writes, under --out: rebuilt_dry_{ref,hot,veryhot}.wav (loud-frame -26.7 / -20.7 / -14.7 dBFS, peaks <= -1 dBFS),
ab_L-stem_R-rebuilt.wav, ab_L-humantake_R-rebuilt.wav (LUFS-matched), report.json. Everything stays local
(data/songs is gitignored); P3 is never read. Uses one CPU core, no Native, no Stomp.

    python scripts/rebuild_dry.py --song-id mother   # data/songs/mother-rebuilt/transcription.mid + the song's stem, full length
    python scripts/rebuild_dry.py --midi X.mid --stem guitar.wav --out data/songs/x-rebuilt   # explicit files
    python scripts/rebuild_dry.py --songs all        # T203: every data/songs/<id>-rebuilt/ that has a transcription.mid
    python scripts/rebuild_dry.py --profile strat-main   # T204: own notes + tone EQ + level from data/guitars/<id>/ (add --profile-tone-only)
    python scripts/rebuild_dry.py --song-id mother --profile prs-7-tone --envelope-from-stem   # T224: dynamics from the stem, not BP velocity
    python scripts/rebuild_dry.py --chords "B5,G5,A5,F#5,D5,C#5" --song-id mother   # T205: chord-list rebuild from Basic Pitch posteriors
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from tonematch import dry_rebuild as D  # noqa: E402
from tonematch import guitar_profile as GP  # noqa: E402
from tonematch.midi_read import read_midi  # noqa: E402

SONGS = ROOT / "data" / "songs"
TAIL_S = 2.0          # added after the last note when the length comes from the MIDI (no stem)


def _ts():
    """scripts/transcribe_stems.py as a module (stem lookup, aligned human takes)."""
    import transcribe_stems as TS
    return TS


def song_take(song_id: str) -> Path | None:
    """The best aligned human dry take of the song under data/songs/<id>-playalong/takes/, if any."""
    r = _ts().discover_refs().get(song_id)
    return r["take"] if r else None


def rebuilt_songs() -> list[str]:
    """Song ids with a transcription: data/songs/<id>-rebuilt/transcription.mid."""
    return sorted(d.name.removesuffix("-rebuilt") for d in SONGS.glob("*-rebuilt") if (d / "transcription.mid").exists())


def stem_seconds(stem: Path) -> float:
    return float(sf.info(str(stem)).duration)


def _align_mod():
    spec = importlib.util.spec_from_file_location("align_takes", ROOT / "scripts" / "align_takes.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _mono(path: Path) -> np.ndarray:
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    assert sr == D.SR
    return x.mean(axis=1)


def match_lufs(a: np.ndarray, b: np.ndarray, target: float = -23.0, peak_cap: float = -1.0):
    """Scale a and b to the same integrated loudness (target), turned down together if either would pass the cap."""
    ga, gb = target - D.lufs(a), target - D.lufs(b)
    pk = max(D.peak_db(a) + ga, D.peak_db(b) + gb)
    if pk > peak_cap:
        ga, gb = ga - (pk - peak_cap), gb - (pk - peak_cap)
    return a * np.float32(10 ** (ga / 20)), b * np.float32(10 ** (gb / 20)), ga, gb


def _load_take(take: Path, seconds: float):
    """The human take and the song-time span it covers, from its _align.json (take sample 0 at song time t0)."""
    x = _mono(take)
    tj = json.loads(take.with_name(take.stem.removesuffix("_dry") + "_align.json").read_text())
    t0 = tj["alignment"]["take_sample0_song_s"]
    return x, t0, max(0.0, t0), min(seconds, t0 + len(x) / D.SR)


def rebuild_song(bank, midi: Path, stem: Path | None, out: Path, take: Path | None = None, seconds: float | None = None,
                 max_poly: int = 6, player: str | None = None, bank_check: dict | None = None,
                 prefer_position: str | None = None, notes_override: list | None = None, extra: dict | None = None,
                 profile=None, profile_tone_only: bool = False, envelope_from_stem: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    stem_x = _mono(stem) if stem is not None else None
    if stem_x is not None:                     # default length: the whole stem; --seconds N is an explicit override
        full = len(stem_x) / D.SR
        source = "stem" if not seconds else ("--seconds" if seconds < full else "stem (--seconds longer than the stem)")
        seconds = min(seconds, full) if seconds else full
        stem_x = stem_x[: int(seconds * D.SR)]
    if notes_override is not None:             # T205: notes already decided (chord-list decode); no cleaning
        raw, notes, cstats = notes_override, notes_override, {"note": "chord-list decode, no cleaning"}
    else:
        raw = read_midi(midi)
        notes, cstats = D.clean_transcription(raw, max_poly=max_poly)
    if stem_x is None:                         # no stem: the last note's end plus a tail, unless --seconds says otherwise
        last = max((n.offset for n in notes), default=0.0)
        source = "--seconds" if seconds else "midi last note + tail"
        seconds = seconds or (last + TAIL_S)
    print(f"rebuild length: {seconds:.2f} s (from {source})", flush=True)
    rel = None
    if envelope_from_stem:                     # T224: dynamics from the stem's note-level RMS, not Basic Pitch velocity
        if stem_x is None:
            raise SystemExit("--envelope-from-stem needs a stem (--stem or --song-id)")
        rel = D.stem_envelope_db(notes, stem_x)
        extra = {**(extra or {}), "envelope_from_stem": {"notes": len(rel), "rel_db_min": round(min(rel, default=0.0), 2),
                                                          "rel_db_max": round(max(rel, default=0.0), 2),
                                                          "rel_db_std": round(float(np.std(rel)) if rel else 0.0, 2)}}
        print(f"envelope from stem: {len(rel)} notes, rel dB std {extra['envelope_from_stem']['rel_db_std']}", flush=True)
    rk = {} if rel is None else {"rel_db": rel}
    if profile is not None:                    # T204: own notes first, bank notes through the tone EQ, the profile's level
        prof = GP.GuitarProfile(profile.data, [], profile.root) if profile_tone_only else profile
        y, log, psum = GP.render_profiled(notes, bank, prof, total_s=seconds, player=player, **rk)
        key = "profiled_toneonly" if profile_tone_only else "profiled"
        var = {key: {"audio": y, **psum}}
        extra = {**(extra or {}), "profile": {"guitar_id": profile.id, "tone_only": profile_tone_only, **psum}}
    else:
        y, log = D.render(notes, bank, total_s=seconds, player=player, prefer_position=prefer_position, **rk)
        var = D.level_variants(y)
        key = "ref"
    for name, v in var.items():
        sf.write(str(out / f"rebuilt_dry_{name}.wav"), v["audio"], D.SR, subtype="FLOAT")
    ref = var[key]["audio"]
    rep = {"midi": str(midi), "seconds": round(seconds, 2), "seconds_source": source, "transcription_notes": len(raw),
           "cleaning": cstats, "rendered": len(log), "missing_pitch": sum(1 for x in log if x.get("missing")),
           "short_source_notes": sum(1 for x in log if x.get("short_source")),
           "players_used": {p: sum(1 for x in log if x.get("player") == p) for p in ("P1", "P2")},
           "levels": {k: {kk: (round(float(vv), 2) if not isinstance(vv, (bool, np.ndarray)) else vv)
                          for kk, vv in v.items() if kk != "audio"} for k, v in var.items()}}
    al = None
    if stem_x is not None:
        n = min(len(stem_x), len(ref))
        s2, r2, gs, gr = match_lufs(stem_x[:n], ref[:n])
        sf.write(str(out / "ab_L-stem_R-rebuilt.wav"), np.stack([s2, r2], 1), D.SR, subtype="FLOAT")
        al = _align_mod()
        se = al.onset_env(stem_x)
        res = {}
        r = al.align(se, al.onset_env(ref[: len(stem_x)]))
        res["rebuilt"] = {k: r[k] for k in ("score", "median_corr", "lag_iqr_ms", "global_lag_s", "global_corr")} | {"windows": len(r["windows"])}
        rr = al.align(se, al.onset_env(np.roll(ref, int(3.7 * D.SR))[: len(stem_x)]))
        res["rebuilt_rolled_3.7s_control"] = {k: rr[k] for k in ("score", "median_corr", "lag_iqr_ms")}
        rep["lufs_match_stem"] = {"stem_gain_db": round(gs, 2), "rebuilt_gain_db": round(gr, 2)}
        rep["alignment_vs_stem_full"] = res
    if bank_check:
        rep["bank_alignment"] = bank_check
    if extra:
        rep.update(extra)
    if take is not None and stem_x is not None:   # T201-style comparison against the human take, in the take's own span
        tk, t0, lo, hi = _load_take(take, seconds)
        a0, a1 = int(lo * D.SR), int(hi * D.SR)
        tk_seg = tk[int((lo - t0) * D.SR): int((hi - t0) * D.SR)]
        rb = ref[a0:a1]
        m = min(len(tk_seg), len(rb))
        t2, r3, gt, gr3 = match_lufs(tk_seg[:m], rb[:m])
        sf.write(str(out / "ab_L-humantake_R-rebuilt.wav"), np.stack([t2, r3], 1), D.SR, subtype="FLOAT")
        st_seg = stem_x[a0:a1]
        se_w = al.onset_env(st_seg)
        w = {}
        for tag, sig in (("rebuilt", rb), ("human_take", tk_seg)):
            q = al.align(se_w, al.onset_env(sig[: len(st_seg)]))
            w[tag] = {k: q[k] for k in ("score", "median_corr", "lag_iqr_ms", "global_lag_s", "global_corr")} | {"windows": len(q["windows"])}
        qc = al.align(se_w, al.onset_env(np.roll(rb, int(3.7 * D.SR))[: len(st_seg)]))
        w["rebuilt_rolled_3.7s_control"] = {k: qc[k] for k in ("score", "median_corr", "lag_iqr_ms")}
        rep["alignment_vs_stem_take_span"] = w
        rep["take"] = {"file": take.name, "span_song_s": [round(lo, 2), round(hi, 2)],
                       "lufs_gain_db": {"take": round(gt, 2), "rebuilt": round(gr3, 2)}}
    (out / "report.json").write_text(json.dumps(rep, indent=1))
    return rep


def rebuild_chords(bank, spec: str, song: str, out: Path, seconds: float | None, stem: Path, take: Path | None = None,
                   bank_check: dict | None = None, posteriors: dict | None = None,
                   profile=None, profile_tone_only: bool = False, envelope_from_stem: bool = False) -> dict:
    """T205: chord names -> voicings; Viterbi over Basic Pitch posteriors restricted to them; render low-position."""
    from tonematch import chord_rebuild as CR
    chords = CR.parse_chords(spec)
    if posteriors is None:
        import transcribe_stems as TS
        # posteriors must cover exactly the length being rebuilt (no stale 68 s cache for a full song)
        posteriors = TS.ensure_posteriors(song, seconds or stem_seconds(stem))
    notes, timeline = CR.decode(posteriors["note"], posteriors["onset"], posteriors["fps"], chords)
    if not notes:
        raise SystemExit("no chord segments found: the posteriors never favour the given chords")
    chords_meta = {"chords": chords, "switch_penalty": CR.SWITCH_PENALTY, "silence": CR.SILENCE,
                   "min_segment_s": CR.MIN_SEGMENT_S, "strike_height": CR.STRIKE_HEIGHT, "min_gap_s": CR.MIN_GAP_S,
                   "segments": timeline}
    rep = rebuild_song(bank, Path("chord-list"), stem, out, take=take, seconds=seconds, bank_check=bank_check,
                       prefer_position="low", notes_override=notes,
                       profile=profile, profile_tone_only=profile_tone_only, envelope_from_stem=envelope_from_stem,
                       extra={"chord_rebuild": {k: v for k, v in chords_meta.items() if k != "segments"} | {"segments": len(timeline), "strikes": sum(t[3] for t in timeline), "notes": len(notes)}})
    (out / "chords.json").write_text(json.dumps(chords_meta, indent=0))
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--song-id", "--song", dest="song", default=None, metavar="ID",
                    help="song id: fills --midi (data/songs/<id>-rebuilt/transcription.mid), --stem (the separated guitar stem), "
                         "--take (best aligned human take in data/songs/<id>-playalong/takes/, if any) and --out (data/songs/<id>-rebuilt); "
                         "with --chords: the song whose stem and posteriors are used")
    ap.add_argument("--songs", nargs="*", help="T203 mode: song ids or 'all' (every data/songs/<id>-rebuilt/ with a transcription.mid)")
    ap.add_argument("--midi", type=Path, default=None, help="MIDI transcription (default with --song-id: data/songs/<id>-rebuilt/transcription.mid)")
    ap.add_argument("--bank-root", type=Path, default=ROOT / "data" / "di" / "Guitar-TECHS")
    ap.add_argument("--stem", type=Path, default=None, help="guitar stem wav; sets the rebuilt length (default with --song-id: found from the id). "
                    "Without any stem the length is the MIDI's last note plus 2 s")
    ap.add_argument("--take", type=Path, default=None, help="human dry take wav with its _align.json, for the A/B (default: found from --song-id)")
    ap.add_argument("--out", type=Path, default=None, help="output folder (default with --song-id: data/songs/<id>-rebuilt, or its chords/ folder)")
    ap.add_argument("--seconds", type=float, default=None,
                    help="explicit length override in seconds (default: the full stem, or the MIDI's last note + 2 s with no stem)")
    ap.add_argument("--max-poly", type=int, default=6)
    ap.add_argument("--player", choices=["P1", "P2"], default=None)
    ap.add_argument("--prefer-position", choices=["low"], default=None,
                    help="low: for each pitch use the lowest-fret bank notes (open strings first) that are long enough")
    ap.add_argument("--chords", default=None,
                    help='T205: comma-separated chord names (X5, X, Xm; # or b), e.g. "B5,G5,A5,F#5,D5,C#5"; needs --song-id; '
                         "decodes Basic Pitch posteriors restricted to them; writes data/songs/<song>-rebuilt/chords/")
    ap.add_argument("--profile", default=None, metavar="GUITAR_ID",
                    help="T204: rebuild with data/guitars/<id>/ (own notes, tone EQ on bank notes, velocity->level, profile loud level); "
                         "combines with --chords (--prefer-position is then ignored)")
    ap.add_argument("--profile-tone-only", action="store_true", help="with --profile: ignore the own notes (bank through the EQ only)")
    ap.add_argument("--envelope-from-stem", action="store_true",
                    help="T224 (off by default): note dynamics from the stem's note-level RMS (per attack moment, smoothed, chord-corrected) "
                         "instead of Basic Pitch velocity; pitch and timing stay from the transcription; needs a stem")
    ap.add_argument("--variant", default=None, help="with --songs or --song-id: write into data/songs/<id>-rebuilt/<variant>/ instead")
    a = ap.parse_args(argv)

    if a.profile_tone_only and not a.profile:
        ap.error("--profile-tone-only needs --profile")
    if a.chords and not a.song:
        ap.error("--chords needs --song-id")
    if not (a.songs or a.song or a.midi):
        ap.error("give --song-id ID, --songs ID.../all, or --midi FILE (with --stem and --out)")
    prof = GP.GuitarProfile.load(a.profile) if a.profile else None
    pk = {"profile": prof, "profile_tone_only": a.profile_tone_only, "envelope_from_stem": a.envelope_from_stem}
    bank = D.index_bank(a.bank_root)
    chk = D.check_bank_alignment(bank, 20)
    if a.chords:
        TS = _ts()
        out = a.out or SONGS / f"{a.song}-rebuilt" / (a.variant or "chords")
        stem = TS.resolve_stem(a.song, a.stem)
        rep = rebuild_chords(bank, a.chords, a.song, out, a.seconds, stem, take=a.take or song_take(a.song), bank_check=chk, **pk)
        print(json.dumps(rep["chord_rebuild"] | {"out": str(out)}, indent=1))
        return 0
    if a.songs:
        TS = _ts()
        ids = rebuilt_songs() if a.songs == ["all"] else a.songs
        if not ids:
            raise SystemExit("no song folders with a transcription found: run scripts/transcribe_stems.py run first")
        summary = {}
        for sid in ids:
            d = SONGS / f"{sid}-rebuilt"
            rep = rebuild_song(bank, d / "transcription.mid", TS.resolve_stem(sid), d / a.variant if a.variant else d, take=song_take(sid),
                               seconds=a.seconds, max_poly=a.max_poly, player=a.player, bank_check=chk, prefer_position=a.prefer_position, **pk)
            summary[sid] = rep["alignment_vs_stem_full"]["rebuilt"]["score"]
            print(sid, json.dumps(rep["alignment_vs_stem_full"]["rebuilt"]), flush=True)
        print(json.dumps(summary))
        return 0
    midi, stem, take, out = a.midi, a.stem, a.take, a.out
    if a.song:
        d = SONGS / f"{a.song}-rebuilt"
        midi = midi or d / "transcription.mid"
        stem = stem or _ts().resolve_stem(a.song)
        take = take or song_take(a.song)
        out = out or (d / a.variant if a.variant else d)
    if midi is None or out is None:
        ap.error("--midi needs --out (or use --song-id)")
    if not Path(midi).exists():
        ap.error(f"MIDI not found: {midi}")
    rep = rebuild_song(bank, midi, stem, out, take=take, seconds=a.seconds, max_poly=a.max_poly,
                       player=a.player, bank_check=chk, prefer_position=a.prefer_position, **pk)
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
