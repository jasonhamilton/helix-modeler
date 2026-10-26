#!/usr/bin/env python3
"""T224: dry-signal ablation (human 2026-10-25, decisions 4 and 5). MEASUREMENT ONLY; nothing is adopted. LOCAL ONLY.

Per song (seven songs, T207/T211 windows) the SAME search is run on different dry inputs:

  lib    (a) the generic Guitar-TECHS library DI the T200 picker chose for the window, set to -28.08 dBFS loud-frame
  plain  (b) the plain Basic Pitch rebuild from the prs-7-tone profile (the T211 dry), -28.08 dBFS
  env    (c) the same rebuild with the stem's note-level envelope imposed (rebuild_dry.py --envelope-from-stem), -28.08 dBFS
  own    (d) the human's own aligned take (Mother take1, DS-Sample take2), true level (no scaling)
  plain36   (b) at -36.12 dBFS (the calibration level): the level-only arm

The search is `match_block.py --category drive --shortlist afxrep --shortlist-n 8 --final-rank afxrep --knob-tol 0.15
--pack-lufs`, no fine-tune. AFx-Rep distances of different arms are NOT comparable as fidelity (the dry differs); only the
picks and the ear are.

    .venv/bin/python scripts/dry_ablation_t224.py prep            # dries -> cache/dry-ablation-t224/dry/<song>/<arm>/
    .venv/bin/python scripts/dry_ablation_t224.py run             # the searches (resumable; LoadGuard; nice 10; 3 hosts)
    .venv/bin/python scripts/dry_ablation_t224.py ledger          # one ledger row per finished run
    .venv/bin/python scripts/dry_ablation_t224.py pack            # blind packs + answer sheets + sealed key + summary
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from tonematch import songs as SG  # noqa: E402
from tonematch.dry_rebuild import loud_level_db  # noqa: E402

SR = 48_000
BASE = ROOT / "cache" / "dry-ablation-t224"
PLAY_DB = -28.08
CAL_DB = -36.12
# T207/T211 windows (song seconds): the `window` field of cache/matches/<id>-dryrebuild-prs7/result.json
WINDOWS = {"mother": (28.3, 38.3), "teen-spirit": (167.0, 177.0), "territorial": (9.5, 19.5), "seek-destroy": (0.5, 10.5),
           "satisfaction": (0.5, 10.5), "cherub-rock": (189.0, 199.0), "ds-sample": (30.0, 45.0)}
ARMS = ["lib", "plain", "env", "own", "plain36"]
ARM_NAME = {"lib": "library DI (T200 picker)", "plain": "plain Basic Pitch rebuild (T211 dry)",
            "env": "rebuild + stem note envelope", "own": "the human's own take", "plain36": "plain rebuild at -36.12 dBFS"}
SEARCH_FLAGS = ["--category", "drive", "--shortlist", "afxrep", "--shortlist-n", "8", "--final-rank", "afxrep",
                "--knob-tol", "0.15", "--pack-lufs"]
WORKERS = "3"
WALL_MIN = "60"


def arms_for(song: str) -> list[str]:
    from rebuild_dry import song_take

    return [a for a in ARMS if a != "own" or song_take(song) is not None]


def scale_to(x: np.ndarray, level_db: float, peak_cap_db: float = -1.0):
    g = level_db - loud_level_db(x)
    y = x * 10 ** (g / 20)
    pk = float(np.abs(y).max()) or 1.0
    y = y * min(1.0, 10 ** (peak_cap_db / 20) / pk)
    return y.astype("float32"), round(float(g), 2), round(float(loud_level_db(y)), 2)


def write_dry(d: Path, x: np.ndarray, t0: float, info: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    sf.write(str(d / "dry.wav"), x, SR, subtype="FLOAT")
    (d / "dry_align.json").write_text(json.dumps({"alignment": {"take_sample0_song_s": float(t0)}, "level": info}))


def mono(p: Path) -> np.ndarray:
    x, sr = sf.read(str(p), dtype="float32", always_2d=True)
    assert sr == SR, sr
    return x.mean(1)


# ----------------------------------------------------------------------------------------------- prep

def library_di(song: str, win: tuple[float, float]) -> tuple[np.ndarray, dict]:
    """The T200 picker's choice for the window: dry.pick_di through match_song.choose_di (12 picks), the first with sound in
    at least 0.8 of its half-second blocks (scripts/t200_run.py). The five T200 songs reuse their saved di_choice.wav."""
    saved = ROOT / "cache" / "matches" / song / "di_choice.wav"
    meta = ROOT / "cache" / "matches" / song / "di_choice.json"
    if saved.exists() and meta.exists() and song != "mother" and song != "ds-sample":
        j = json.loads(meta.read_text())
        return mono(saved), {"source": "T200 di_choice.wav", "pick_di_rank": j["di_rank_in_pick_di"], "file": j["di"][0]["path"],
                             "start_s": j["di"][0]["start_s"], "style": j["di"][0]["style"]}
    import match_song as MS

    sref = SG.resolve(song)
    s = MS.load_song(str(sref.stems_dir))
    a, b = win
    seg = s.guitar[round(a * MS.SR):round(b * MS.SR)]
    picks, desc = MS.choose_di(seg, b - a, n=12)

    def active_fraction(x, sr=MS.SR):
        blk = sr // 2
        r = np.array([np.sqrt(np.mean(x[i:i + blk].astype(np.float64) ** 2)) for i in range(0, len(x) - blk + 1, blk)])
        return float(np.mean(r > r.max() * 1e-2))

    fr = [active_fraction(d) for _, d in picks]
    idx = next((i for i, f in enumerate(fr) if f >= 0.8), int(np.argmax(fr)))
    p, di = picks[idx]
    return di.astype("float32"), {"source": "choose_di (t200_run rule, recomputed)", "pick_di_rank": idx + 1,
                                  "file": Path(p.path).name, "start_s": float(p.start_s), "style": p.style,
                                  "target_style": desc.get("style")}


def prep(songs: list[str]) -> None:
    from rebuild_dry import song_take

    for song in songs:
        win = WINDOWS[song]
        a, b = win
        meta: dict = {"song": song, "window": list(win)}
        di, dinfo = library_di(song, win)
        n = round((b - a) * SR)
        di = np.pad(di[:n], (0, max(0, n - len(di))))      # exactly the window length (the picker can return one sample short)
        y, g, lv = scale_to(di, PLAY_DB)
        write_dry(BASE / "dry" / song / "lib", y, a, {**dinfo, "gain_db": g, "loud_level_db": lv, "samples": len(y)})
        meta["lib"] = {**dinfo, "loud_level_db": lv}
        for arm, var in (("plain", "t224-plain"), ("env", "t224-envelope")):
            w = mono(ROOT / "data" / "songs" / f"{song}-rebuilt" / var / "rebuilt_dry_profiled.wav")
            y, g, lv = scale_to(w, PLAY_DB)
            write_dry(BASE / "dry" / song / arm, y, 0.0, {"src": var, "gain_db": g, "loud_level_db": lv})
            meta[arm] = {"src": var, "loud_level_db": lv, "gain_db": g}
            if arm == "plain":
                y36, g36, lv36 = scale_to(w, CAL_DB)
                write_dry(BASE / "dry" / song / "plain36", y36, 0.0, {"src": var, "gain_db": g36, "loud_level_db": lv36})
                meta["plain36"] = {"src": var, "loud_level_db": lv36, "gain_db": g36}
        take = song_take(song)
        if take is not None:
            x = mono(take)
            aj = take.with_name(take.stem.removesuffix("_dry") + "_align.json")
            t0 = json.loads(aj.read_text())["alignment"]["take_sample0_song_s"]
            d = BASE / "dry" / song / "own"
            d.mkdir(parents=True, exist_ok=True)
            (d / "dry.wav").unlink(missing_ok=True)
            os.symlink(take, d / "dry.wav")
            (d / "dry_align.json").write_text(json.dumps({"alignment": {"take_sample0_song_s": float(t0)},
                                                          "level": {"take": take.name, "loud_level_db": round(float(loud_level_db(x)), 2)}}))
            covers = (t0 <= a) and (t0 + len(x) / SR >= b)
            meta["own"] = {"take": take.name, "loud_level_db": round(float(loud_level_db(x)), 2), "covers_window": bool(covers),
                           "take_span_song_s": [round(t0, 2), round(t0 + len(x) / SR, 2)]}
            if not covers:
                print(f"WARNING {song}: the take does not cover the window", flush=True)
        (BASE / "dry" / song / "meta.json").write_text(json.dumps(meta, indent=1))
        print(song, json.dumps({k: (v.get("loud_level_db") if isinstance(v, dict) else v) for k, v in meta.items()}), flush=True)


# ----------------------------------------------------------------------------------------------- run

def run_dir(song: str, arm: str) -> Path:
    return BASE / "runs" / song / arm


def search_cmd(song: str, arm: str) -> list:
    sref = SG.resolve(song)
    a, b = WINDOWS[song]
    d = BASE / "dry" / song / arm
    if arm == "own":                    # a take that starts/ends a few ms inside the window: trim the window to the take (disclosed)
        o = json.loads((d.parent / "meta.json").read_text())["own"]
        lo, hi = o["take_span_song_s"]
        a, b = max(a, np.ceil(lo * 20) / 20), min(b, np.floor(hi * 20) / 20)
    return [sys.executable, ROOT / "scripts" / "match_block.py", "--target-stems", sref.stems_dir, "--target-window", f"{a}:{b}",
            "--di", d / "dry.wav", "--di-json", d / "dry_align.json", "--song", f"{song}-{arm}", *SEARCH_FLAGS,
            "--workers", WORKERS, "--wall-min", WALL_MIN, "--out", run_dir(song, arm)]


def run(songs: list[str], arms: list[str] | None, dry_run: bool) -> None:
    from tonematch.engines.host_budget import LoadGuard

    guard = LoadGuard()
    env = dict(os.environ, TONEMATCH_AGENT="dry-signal")
    for song in songs:
        for arm in arms_for(song):
            if arms and arm not in arms:
                continue
            out = run_dir(song, arm)
            if (out / "result.json").exists():
                print(f"{song}/{arm}: done, skipped", flush=True)
                continue
            cmd = [str(c) for c in search_cmd(song, arm)]
            if dry_run:
                print(" ".join(cmd))
                continue
            guard.wait(lambda x: print(f"pause: busy cores {x}", flush=True))
            subprocess.run([sys.executable, str(ROOT / "scripts" / "proc_check.py")], cwd=ROOT, check=False)
            out.parent.mkdir(parents=True, exist_ok=True)
            t = time.time()
            with open(BASE / "logs" / f"search-{song}-{arm}.log", "ab") as f:
                f.write(("$ " + " ".join(cmd) + "\n").encode())
                f.flush()
                rc = subprocess.run(["nice", "-n", "10", *cmd], cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, env=env, check=False).returncode
            print(f"{song}/{arm}: exit {rc} in {time.time() - t:.0f} s", flush=True)
            subprocess.run([sys.executable, str(ROOT / "scripts" / "proc_check.py")], cwd=ROOT, check=False)


# ----------------------------------------------------------------------------------------------- results

def short(m: str) -> str:
    return str(m).replace("HD2_DM4", "").replace("HD2_Dist", "").replace("HD2_", "")


def load_result(song: str, arm: str) -> dict | None:
    f = run_dir(song, arm) / "result.json"
    if not f.exists():
        return None
    r = json.loads(f.read_text())
    ranked = ((r.get("shortlist") or {}).get("ranked")) or []
    fin = (r.get("finals") or [{}])[0]
    return {"model": fin.get("model"), "values": fin.get("values", {}), "distance": ranked[0]["distance"] if ranked else None,
            "runner_up": ranked[1]["model"] if len(ranked) > 1 else None,
            "runner_up_distance": ranked[1]["distance"] if len(ranked) > 1 else None,
            "top3": [(x["model"], round(x["distance"], 4)) for x in ranked[:3]],
            "renders": r.get("renders"), "wall_s": r.get("wall_s"), "workers": r.get("workers"), "path": r.get("knob_path")}


def ledger(songs: list[str]) -> None:
    for song in songs:
        for arm in arms_for(song):
            r = load_result(song, arm)
            if not r or r["distance"] is None:
                continue
            note = (f"T224 dry ablation (measurement only; distances NOT comparable across arms) arm {arm}: {ARM_NAME[arm]}; pick {r['model']} "
                    f"{', '.join(f'{k} {round(v, 2)}' for k, v in r['values'].items())}; runner-up {r['runner_up']}; "
                    f"T207 search afxrep shortlist 8 final afxrep knob-tol 0.15 no fine-tune; coarse-grid knobs low confidence")
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "search", "--engine", "native",
                            "--dataset", f"dry-ablation-{song}-{arm}", "--metric", "afxrep_distance", "--value", f"{r['distance']:.4f}",
                            "--renders", str(r["renders"] or ""), "--wall-clock-s", str(round(r["wall_s"] or 0)), "--notes", note],
                           cwd=ROOT, env=dict(os.environ, TONEMATCH_AGENT="dry-signal"), check=True)


# ----------------------------------------------------------------------------------------------- pack

def level_match_pack(target: np.ndarray, matches: dict[str, np.ndarray], lufs: float = -23.0, cap_db: float = -1.0):
    """Target and every match to the same K-weighted integrated loudness; one common gain drop if any peak would pass cap_db.
    The target gain is the same for every arm, so a song's clips are comparable to each other."""
    from tonematch.separation.audio import integrated_loudness

    def lu(x):
        return float(integrated_loudness(x.reshape(1, -1).astype(np.float32)))

    gt = lufs - lu(target)
    gm = {k: lufs - lu(v) for k, v in matches.items()}
    pk = max([20 * np.log10(max(float(np.abs(target).max()), 1e-9)) + gt] +
             [20 * np.log10(max(float(np.abs(v).max()), 1e-9)) + gm[k] for k, v in matches.items()])
    drop = max(0.0, pk - cap_db)
    f = lambda g: np.float32(10 ** ((g - drop) / 20))  # noqa: E731
    return target * f(gt), {k: v * f(gm[k]) for k, v in matches.items()}, {"target": gt - drop, **{k: g - drop for k, g in gm.items()}}


def pack(songs: list[str], seed: int) -> None:
    rng = random.Random(seed)
    out_root = BASE / "packs"
    summary = {}
    for song in songs:
        arms = [a for a in arms_for(song) if (run_dir(song, a) / "listen" / "match.wav").exists()]
        if len(arms) < 2:
            print(f"{song}: not enough finished arms ({arms})", flush=True)
            continue
        tgt = mono(run_dir(song, arms[0]) / "listen" / "target_stem.wav")
        mats = {a: mono(run_dir(song, a) / "listen" / "match.wav") for a in arms}
        n = min([len(tgt)] + [len(v) for v in mats.values()])
        t2, m2, gains = level_match_pack(tgt[:n], {a: v[:n] for a, v in mats.items()})
        letters = list("ABCDEFGH")[: len(arms)]
        rng.shuffle(letters)
        key = dict(zip(letters, arms))
        d = out_root / song
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*"):
            old.unlink()
        for L in sorted(key):
            sf.write(str(d / f"clip_{L}.wav"), np.stack([t2, m2[key[L]]], 1), SR, subtype="FLOAT")
        rows = "\n".join(f'<section><b>Clip {L}</b><audio controls preload="none" src="clip_{L}.wav"></audio></section>' for L in sorted(key))
        (d / "index.html").write_text(
            f"""<!doctype html><meta charset="utf-8"><title>{song}: blind pack</title>
<style>body{{font:16px/1.5 system-ui;max-width:46rem;margin:2rem auto;padding:0 1rem}}section{{border:1px solid #ccc;border-radius:6px;padding:.6rem 1rem;margin:.6rem 0}}audio{{width:100%}}</style>
<h1>{song}: blind pack</h1><p>Headphones. Each clip is stereo: <b>left = the separated song guitar, right = a candidate match</b>
(use balance or mono-L/mono-R to switch). Every clip is loudness-matched (K-weighted LUFS), with the same guitar on the left.
Rate every clip in <code>answer_sheet.md</code>. <b>Do not open key_SEALED.json until you have rated.</b></p>{rows}""")
        (d / "answer_sheet.md").write_text(
            f"# {song}: answer sheet (blind)\n\nListen to the left channel (the song's guitar) then the right channel (the candidate). "
            f"Score how close the RIGHT is to the LEFT in tone: 1 = far, 5 = very close. Then rank all clips, best first.\n"
            f"Do not open `key_SEALED.json` before filling this in. If two clips sound identical, say so under notes.\n\n"
            f"| clip | closeness 1-5 | notes |\n|---|---|---|\n" + "\n".join(f"| {L} |  |  |" for L in sorted(key)) +
            "\n\nRanking, best first (letters): \n\nRepeat listen (optional): \n")
        (d / "key_SEALED.json").write_text(json.dumps({"song": song, "key": key, "gains_db": gains, "seed": seed,
                                                        "warning": "do not open before rating"}, indent=1))
        summary[song] = {"clips": sorted(key), "dir": str(d)}
        print(song, "pack:", d, "clips", ",".join(sorted(key)), flush=True)
    (out_root / "README.md").write_text(
        "# T224 blind packs (local only; song audio inside)\n\nOne folder per song: `index.html` (players), `clip_<letter>.wav`, `answer_sheet.md`, "
        "`key_SEALED.json` (do not open before rating). Letters are random per song.\n\nThe clip is the search's rendered match through its own dry "
        "track, so notes differ between arms that use the library DI (it plays other notes); that is a property of the arm, not a fault of the pack.\n")


def summary_table(songs: list[str]) -> str:
    lines = ["| song | arm | pick | runner-up | AFx-Rep distance (not comparable across arms) | knobs |", "|---|---|---|---|---|---|"]
    for song in songs:
        for arm in arms_for(song):
            r = load_result(song, arm)
            if not r:
                lines.append(f"| {song} | {arm} | (not run) | | | |")
                continue
            lines.append(f"| {song} | {arm} | {short(r['model'])} | {short(r['runner_up'] or '-')} | {r['distance']:.3f} | "
                         + ", ".join(f"{k} {v:.2f}" for k, v in r["values"].items()) + " |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("step", choices=["prep", "run", "ledger", "pack", "table"])
    ap.add_argument("--songs", nargs="*", default=list(WINDOWS))
    ap.add_argument("--arms", nargs="*", default=None)
    ap.add_argument("--seed", type=int, default=224)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    (BASE / "logs").mkdir(parents=True, exist_ok=True)
    if a.step == "prep":
        prep(a.songs)
    elif a.step == "run":
        run(a.songs, a.arms, a.dry_run)
    elif a.step == "ledger":
        ledger(a.songs)
    elif a.step == "pack":
        pack(a.songs, a.seed)
    else:
        print(summary_table(a.songs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
