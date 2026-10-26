#!/usr/bin/env python3
"""T217: one command from a song to a drive-block preset for the guitar, with a local listening page. LOCAL ONLY.

    .venv/bin/python scripts/song_match.py --song mother --guitar-id prs-7-tone --window 28.3:38.3
    .venv/bin/python scripts/song_match.py --song "data/songs/Some Song.mp3" --guitar-id prs-7-tone   # separates first

Steps (each is skipped when its inputs and settings are unchanged, unless --force; the run folder remembers them):
  separate    scripts/separate.py (htdemucs_6s stems; cache hit when already done)
  window      --window START:END, else the T200 loudest-guitar picker (10 s)
  transcribe  scripts/transcribe_stems.py run (tuned T203 Basic Pitch settings, its own venv)  -> data/songs/<id>-rebuilt/transcription.mid
  rebuild     scripts/rebuild_dry.py (--profile <guitar-id> when given; plain Basic Pitch notes; chords only with --chords)
  level       the profile's level, else -28.08 dBFS loud-frame, or --level-db
  search      scripts/match_block.py, drive category, --shortlist afxrep --shortlist-n 8 --final-rank afxrep --knob-tol 0.15 --pack-lufs
  finetune    only with --finetune (an option, human 2026-10-25): scripts/finetune_knobs.py, budget 120, Output/Master locked
  package     validated best.hlx, LUFS-matched listening page, summary.json and summary.md in --out
  load-stomp  only with --load-stomp and only when the stompd helper is present

Nothing here is a new method: it chains the T200-T211 steps. The listening page and every audio file stay on this machine.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from tonematch import cli_result  # noqa: E402
from tonematch import songs as SG  # noqa: E402

PY = sys.executable
DEFAULT_LEVEL_DB = -28.08            # loud-frame dBFS of the human's play-along (T204 stand-in), used without a profile
PEAK_CAP_DB = -1.0
SEARCH_FLAGS = ["--category", "drive", "--shortlist", "afxrep", "--shortlist-n", "8", "--final-rank", "afxrep",
                "--knob-tol", "0.15", "--pack-lufs", "--wall-min", "10"]
FINETUNE_BUDGET = 120
FINETUNE_DEFAULT = False             # fine-tuning is an option (human 2026-10-25); data/approved-methods.json
FINETUNE_LOCK_LEVEL_KNOBS = True     # when fine-tuning, Output/Master stay at the model defaults (human 2026-10-25)
STEP_ORDER = ["separate", "window", "transcribe", "rebuild", "level", "search", "finetune", "package"]

class Log:
    def __init__(self, path: Path | None = None):
        self.path = path

    def __call__(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(line + "\n")


def shell(cmd: list, log_file: Path | None = None, env: dict | None = None) -> int:
    """Run a command, output to `log_file` (or the console). The one place processes start; tests replace it."""
    cmd = [str(c) for c in cmd]
    if log_file is None:
        return subprocess.run(cmd, cwd=ROOT, env=env, check=False).returncode
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as f:
        f.write(("$ " + " ".join(cmd) + "\n").encode())
        f.flush()
        return subprocess.run(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, env=env, check=False).returncode


class Steps:
    """Per-step memory in <out>/steps.json: a step is current when its settings are unchanged, its outputs exist and no
    earlier step re-ran. `--force` makes every step run."""

    def __init__(self, out: Path, force: bool):
        self.path, self.force, self.dirty = out / "steps.json", force, False
        self.mem = json.loads(self.path.read_text()) if self.path.exists() else {}

    def current(self, name: str, sig: dict, outputs: list[Path]) -> bool:
        sig = json.loads(json.dumps(sig, default=str))
        ok = (not self.force and not self.dirty and self.mem.get(name) == sig and all(Path(p).exists() for p in outputs))
        if not ok:
            self.dirty = True
            self.mem[name] = sig
        return ok

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.mem, indent=1))


# ------------------------------------------------------------------------------------------ pieces

def scale_to_level(x, level_db: float, peak_cap_db: float = PEAK_CAP_DB):
    """Gain `x` so its loud-frame level (dry_rebuild.loud_level_db) is `level_db`; then cap the peak at `peak_cap_db`."""
    import numpy as np

    from tonematch.dry_rebuild import loud_level_db

    g = level_db - loud_level_db(x)
    y = x * 10 ** (g / 20)
    pk = float(np.abs(y).max()) or 1.0
    y = y * min(1.0, 10 ** (peak_cap_db / 20) / pk)
    return y.astype("float32"), g, loud_level_db(y)


def write_level(src: Path, dst_dir: Path, level_db: float | None) -> dict:
    """The dry track at its play level + the alignment json match_block reads. `level_db` None: use `src` as is (a profile's level)."""
    import soundfile as sf

    x, sr = sf.read(str(src), dtype="float32")
    if x.ndim > 1:
        x = x.mean(1)
    dst_dir.mkdir(parents=True, exist_ok=True)
    info = {"source": str(src), "gain_db": 0.0}
    if level_db is not None:
        x, g, got = scale_to_level(x, level_db)
        info.update(gain_db=round(float(g), 2), loud_level_db=round(float(got), 2))
    else:
        from tonematch.dry_rebuild import loud_level_db

        info["loud_level_db"] = round(float(loud_level_db(x)), 2)
    sf.write(str(dst_dir / "rebuilt_dry_playlevel.wav"), x, sr, subtype="FLOAT")
    (dst_dir / "rebuilt_dry_playlevel_align.json").write_text(json.dumps({"alignment": {"take_sample0_song_s": 0.0}, "level": info}))
    return info


def guitar_seconds(wav: Path) -> float:
    import soundfile as sf

    return float(sf.info(str(wav)).duration)


def stompd_present() -> Path | None:
    p = Path(os.environ.get("TONEMATCH_STOMPD") or ROOT / "cache" / "stomp" / "bin" / "stompd")
    return p if p.is_file() else None


def first_block_model(hlx: Path) -> str:
    d = json.loads(Path(hlx).read_text())
    blocks = [v for k, v in sorted(d["data"]["tone"]["dsp0"].items()) if k.startswith("block") and isinstance(v, dict)]
    return str(blocks[0].get("@model"))


def load_stomp(best_hlx: Path, out: Path, log) -> bool:
    """Put best.hlx in the Stomp edit buffer through scripts/stomp_live_preview.py --once (as T211 did). Never a flash write.
    Only when the stompd helper is present; otherwise says so and returns False."""
    if not stompd_present():
        log("load-stomp: the stompd helper is not present (cache/stomp/bin/stompd or TONEMATCH_STOMPD); skipped")
        return False
    d = out / "stomp-load"
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(best_hlx, d / "current_best.hlx")
    (d / "current_best.json").write_text(json.dumps({
        "seq": 1, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "loss": 0.0, "model": first_block_model(best_hlx),
        "category": "drive", "values": {}, "renders": 0, "stage": "final", "label": out.name}))
    out_f = d / "load.out"
    out_f.write_text("")
    with open(out_f, "ab") as f:
        p = subprocess.Popen([PY, str(ROOT / "scripts" / "stomp_live_preview.py"), "--dir", str(d), "--once", "--no-restore-on-exit"],
                             cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)
    ok = False
    try:
        for _ in range(30):
            time.sleep(1)
            txt = out_f.read_text(errors="ignore")
            if "seq 1" in txt:
                ok = "ERROR" not in txt and "refus" not in txt
                break
            if p.poll() is not None:
                break
        time.sleep(1)
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
    log(f"load-stomp: {'loaded into the edit buffer' if ok else 'NOT loaded (see ' + str(out_f) + ')'}; the pedal was not saved")
    return ok


def pn(m: str) -> str:
    return str(m).replace("HD2_DM4", "").replace("HD2_Dist", "").replace("HD2_", "")


def build_page(out: Path, title: str, rows: list[tuple[str, str]], facts: str) -> Path:
    """Local listening page: `rows` = (label, file relative to the page). No song audio is published; the page is for this machine."""
    items = "\n".join(f'<b>{html.escape(lab)}</b><audio controls preload="none" src="{html.escape(src)}"></audio>' for lab, src in rows)
    page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>:root{{--bg:#f5f5f1;--card:#fff;--ink:#1f2420;--muted:#5d655f;--rule:#d9dcd5}}@media (prefers-color-scheme:dark){{:root{{--bg:#141714;--card:#1c201d;--ink:#e7ebe6;--muted:#a2aba4;--rule:#323833;color-scheme:dark}}}}
body{{background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif;margin:0;padding:2rem 1rem}}main{{max-width:58rem;margin:auto;display:grid;gap:1rem}}
section{{background:var(--card);border:1px solid var(--rule);border-radius:6px;padding:1rem 1.2rem;display:grid;gap:.45rem}}p{{margin:0}}.m{{color:var(--muted);font:13px/1.5 ui-monospace,Menlo,monospace}}audio{{width:100%}}</style>
<main><h1>{html.escape(title)}</h1><p>Local only; headphones. All clips are loudness-matched (LUFS). Your ear decides.</p>
<section><p class="m">{html.escape(facts)}</p>{items}</section></main>"""
    (out / "index.html").write_text(page)
    return out / "index.html"


# ------------------------------------------------------------------------------------------ pipeline

def plan_names(a) -> list[str]:
    steps = list(STEP_ORDER)
    if not a.finetune:
        steps.remove("finetune")
    if a.load_stomp:
        steps.append("load-stomp")
    return steps


def run_pipeline(a, res=None, log=None, sh=shell) -> int:
    from tonematch.engines.host_budget import LoadGuard

    song_ref = a.song
    separate_cmd = lambda p: [PY, ROOT / "scripts" / "separate.py", p]  # noqa: E731
    out_arg = Path(a.out) if a.out else None

    # ---- resolve (separates a song file that has no stems yet)
    def _separate(p: Path) -> Path:
        rj = (out_arg or ROOT / "cache" / "song-match" / "_sep") / "separate.result.json"
        rj.parent.mkdir(parents=True, exist_ok=True)
        rc = sh([*separate_cmd(str(p)), "--result-json", str(rj)], (out_arg or rj.parent) / "logs" / "separate.log")
        if rc != 0:
            raise SystemExit(f"separate failed (exit {rc}); see logs/separate.log")
        return ROOT / json.loads(rj.read_text())["outputs"]["stems_dir"]

    try:
        sref = SG.resolve(song_ref, song_id=a.id, separate=None if a.dry_run else _separate)
    except SG.SongNotFound as e:
        if a.dry_run and SG.is_song_file(song_ref):
            sref = SG.SongRef(a.id or SG.slug(Path(song_ref).stem), Path("<stems dir: separate.py will create it>"))
        else:
            print(f"error: {e}", file=sys.stderr)
            return 2
    sid = sref.id
    tag = a.guitar_id or "plain"
    out = out_arg or ROOT / "cache" / "song-match" / f"{sid}-{tag}"
    if not a.dry_run:
        out.mkdir(parents=True, exist_ok=True)
    lg = log or Log(None if a.dry_run else out / "run.log")
    steps = Steps(out, a.force)
    guard = LoadGuard()

    dry_dir = out / "dry"
    search_dir, ft_dir = out / "search", out / "finetune"
    rebuilt = SG.profiled_dir(sid, a.guitar_id) if a.guitar_id else SG.plain_dir(sid)
    if a.chords:
        rebuilt = rebuilt.with_name(rebuilt.name + "-chords")
    dry_name = "rebuilt_dry_profiled.wav" if a.guitar_id else "rebuilt_dry_ref.wav"
    mid = SG.transcription_mid(sid)
    guitar = sref.guitar_wav

    # ---- window
    if a.window:
        win = SG.parse_window(a.window)
    elif a.dry_run and not guitar.exists():
        win = None
    else:
        wf = out / "window.json"
        if wf.exists() and not a.force and json.loads(wf.read_text()).get("stems") == str(sref.stems_dir):
            win = tuple(json.loads(wf.read_text())["window"])
        else:
            win = SG.pick_window(sref.stems_dir)
            if not a.dry_run:
                wf.write_text(json.dumps({"window": list(win), "stems": str(sref.stems_dir), "rule": "T200 loudest-guitar picker"}))
    wtxt = f"{win[0]}:{win[1]}" if win else "<T200 picker>"
    level_db = a.level_db if a.level_db is not None else (None if a.guitar_id else DEFAULT_LEVEL_DB)
    seconds = guitar_seconds(guitar) if guitar.exists() else None

    secs = f"{seconds:.3f}" if seconds else "<full length>"
    prof = ["--profile", a.guitar_id] if a.guitar_id else []
    if a.chords:
        rebuild_cmd = [PY, ROOT / "scripts" / "rebuild_dry.py", "--chords", a.chords, "--song", sid, "--stem", guitar, "--out", rebuilt,
                       "--seconds", secs, *prof]
    else:
        rebuild_cmd = [PY, ROOT / "scripts" / "rebuild_dry.py", "--midi", mid, "--stem", guitar, "--out", rebuilt, "--seconds", secs, *prof]
    search_cmd = [PY, ROOT / "scripts" / "match_block.py", "--target-stems", sref.stems_dir, "--target-window", wtxt,
                  "--di", dry_dir / "rebuilt_dry_playlevel.wav", "--di-json", dry_dir / "rebuilt_dry_playlevel_align.json",
                  "--song", sid, *SEARCH_FLAGS, "--workers", a.workers, "--out", search_dir]
    ft_cmd = [PY, ROOT / "scripts" / "finetune_knobs.py", "--songs", sid, "--stems-dir", sref.stems_dir, "--match-dir", search_dir,
              "--dry-dir", dry_dir, "--out", ft_dir, "--budget", FINETUNE_BUDGET, "--jobs", 1]
    if FINETUNE_LOCK_LEVEL_KNOBS:
        ft_cmd.append("--lock-level-knobs")
    tr_cmd = [PY, ROOT / "scripts" / "transcribe_stems.py", "run", "--songs", sid, "--stem", guitar]

    if a.dry_run:
        lg(f"plan for song '{sid}' ({sref.source or song_ref}); guitar {a.guitar_id or 'none (library bank notes)'}; out {out}")
        lg(f"window {wtxt}; level {'profile level' if level_db is None else f'{level_db} dBFS loud-frame'}; "
           f"finetune {f'budget {FINETUNE_BUDGET}, Output/Master locked' if a.finetune else 'off (search winner)'}; chords {a.chords or 'none'}")
        for n, cmd in (("transcribe", tr_cmd), ("rebuild", rebuild_cmd), ("search", search_cmd),
                       ("finetune", ft_cmd)):
            if n == "finetune" and not a.finetune:
                continue
            state = "done" if _would_skip(n, mid, rebuilt / dry_name, search_dir, ft_dir, steps, a) else "todo"
            lg(f"  {n:<11} [{state}] " + " ".join(str(c) for c in cmd))
        lg("  steps: " + " -> ".join(plan_names(a)))
        if res is not None:
            res.metrics.update({"dry_run": True, "song_id": sid, "steps": plan_names(a)})
            res.notes = "dry run: nothing executed"
        return 0

    t0 = time.time()
    logs = out / "logs"
    stage_s: dict[str, float] = {}

    def timed(name, fn):
        s = time.time()
        r = fn()
        stage_s[name] = round(time.time() - s, 1)
        return r

    # ---- transcribe (shared by every guitar: reused when data/songs/<id>-rebuilt/transcription.mid exists)
    if a.force or not mid.exists():
        lg("transcribe: Basic Pitch (tuned T203 settings)")
        if timed("transcribe", lambda: sh(tr_cmd, logs / "transcribe.log")) != 0 or not mid.exists():
            print(f"error: transcription failed; see {logs / 'transcribe.log'}", file=sys.stderr)
            return 1
    else:
        lg(f"transcribe: reuse {mid}")
    # ---- rebuild (also shared: reused when it is newer than the transcription)
    dry_src = rebuilt / dry_name
    if a.force or not dry_src.exists() or dry_src.stat().st_mtime < mid.stat().st_mtime:
        lg(f"rebuild: {'profile ' + a.guitar_id if a.guitar_id else 'library notes'}{' with chords' if a.chords else ''}")
        if timed("rebuild", lambda: sh(rebuild_cmd, logs / "rebuild.log")) != 0 or not dry_src.exists():
            print(f"error: rebuild failed; see {logs / 'rebuild.log'}", file=sys.stderr)
            return 1
    else:
        lg(f"rebuild: reuse {dry_src}")
    # ---- level
    dry_wav = dry_dir / "rebuilt_dry_playlevel.wav"
    if not steps.current("level", {"src": str(dry_src), "mtime": dry_src.stat().st_mtime_ns, "level_db": level_db}, [dry_wav]):
        info = write_level(dry_src, dry_dir, level_db)
        lg(f"level: {info}")
    else:
        lg("level: reuse")
    # ---- search
    if not steps.current("search", {"window": wtxt, "workers": a.workers, "flags": SEARCH_FLAGS, "dry": dry_wav.stat().st_mtime_ns},
                         [search_dir / "result.json", search_dir / "best.hlx"]):
        lg(f"search: drive category, window {wtxt}")
        shutil.rmtree(search_dir, ignore_errors=True)
        guard.wait(lambda x: lg(f"pause: busy cores {x}"))
        sh([PY, ROOT / "scripts" / "proc_check.py"], None)
        rc = timed("search", lambda: sh(search_cmd, logs / "search.log"))
        sh([PY, ROOT / "scripts" / "proc_check.py"], None)
        if rc != 0 or not (search_dir / "result.json").exists():
            print(f"error: search failed (exit {rc}); see {logs / 'search.log'}", file=sys.stderr)
            return 1
    else:
        lg("search: reuse")
    # ---- fine-tune
    if a.finetune:
        sig = {"search": (search_dir / "result.json").stat().st_mtime_ns, "budget": FINETUNE_BUDGET, "lock": FINETUNE_LOCK_LEVEL_KNOBS}
        if not steps.current("finetune", sig, [ft_dir / "result.json", ft_dir / "best.hlx"]):
            lg(f"finetune: budget {FINETUNE_BUDGET}, level knobs locked")
            shutil.rmtree(ft_dir, ignore_errors=True)
            guard.wait(lambda x: lg(f"pause: busy cores {x}"))
            rc = timed("finetune", lambda: sh(ft_cmd, logs / "finetune.log"))
            sh([PY, ROOT / "scripts" / "proc_check.py"], None)
            if rc != 0 or not (ft_dir / "result.json").exists():
                print(f"error: finetune failed (exit {rc}); see {logs / 'finetune.log'}", file=sys.stderr)
                return 1
        else:
            lg("finetune: reuse")
    # ---- package
    summary = package(out, sid, sref, a, win, level_db, search_dir, ft_dir, dry_dir, stage_s, t0, lg, sh)
    steps.save()
    if res is not None:
        res.outputs.update({"out_dir": out, "best_hlx": out / "best.hlx", "listen_page": out / "index.html",
                            "summary_json": out / "summary.json", "summary_md": out / "summary.md"})
        res.metrics.update({k: summary[k] for k in ("song_id", "window", "model", "values", "dist_before", "dist_after", "renders", "validate_exit")})
    if a.load_stomp:
        summary["stomp_loaded"] = load_stomp(out / "best.hlx", out, lg)
        (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    lg(f"done in {time.time() - t0:.0f} s: {summary['model']}  listening page {out / 'index.html'}")
    return 0


def _would_skip(name, mid, dry_src, search_dir, ft_dir, steps, a) -> bool:
    if a.force:
        return False
    return {"transcribe": mid.exists(), "rebuild": dry_src.exists(),
            "search": (search_dir / "result.json").exists(), "finetune": (ft_dir / "result.json").exists()}[name]


def package(out, sid, sref, a, win, level_db, search_dir, ft_dir, dry_dir, stage_s, t0, lg, sh) -> dict:
    """best.hlx (validated), listening page, summary.json/md."""
    import match_song as MS

    sres = json.loads((search_dir / "result.json").read_text())
    ranked = ((sres.get("shortlist") or {}).get("ranked")) or []
    tuned = bool(a.finetune)
    src = ft_dir if tuned else search_dir
    best = out / "best.hlx"
    shutil.copy(src / "best.hlx", best)
    code, text = MS.validate_hlx(best)
    (out / "best.validate.md").write_text(text + "\n")
    listen = out / "listen"
    listen.mkdir(exist_ok=True)
    pack = src / "listen"
    rows, names = [], {}
    for lab, f in (("Left: separated song guitar, Right: match", "ab_L-target_R-match.wav"),
                   ("Separated song guitar", "target_stem.wav"), ("Match (final preset)", "match.wav"),
                   ("Match before fine-tuning (search winner)", "t207_match.wav"), ("Dry track (the rebuilt guitar the match plays)", "di_dry.wav")):
        s = pack / f
        if not s.exists() and f == "t207_match.wav" and (search_dir / "listen" / "match.wav").exists() and tuned:
            s = search_dir / "listen" / "match.wav"
        if s.exists():
            shutil.copy(s, listen / f)
            rows.append((lab, f"listen/{f}"))
    fin = (sres.get("finals") or [{}])[0]
    if tuned:
        r = json.loads((ft_dir / "result.json").read_text())
        model, values, d0, d1 = r["model"], r["final_values"], r["dist_before"], r["dist_after"]
        renders = (sres.get("renders") or 0) + r["renders"]
        extra = {"finetune": {k: r.get(k) for k in ("start_values", "moved_over_0.15", "renders", "stopped", "wall_s", "locked_level_knobs")}}
    else:
        model, values = fin.get("model"), fin.get("values", {})
        d0 = d1 = (ranked[0]["distance"] if ranked else None)
        renders = sres.get("renders")
        extra = {}
    facts = (f"{sid} · window {win[0]}:{win[1]} s · {pn(model)} · " + ", ".join(f"{k} {round(v, 2)}" for k, v in values.items())
             + f" · AFx-Rep distance {d1:.3f}" if d1 is not None else f"{sid} · {pn(model)}")
    page = build_page(out, f"{sid}: {pn(model)}", rows, facts)
    summary = {"song_id": sid, "song_source": sref.source, "stems_dir": str(sref.stems_dir), "guitar_id": a.guitar_id,
               "window": list(win), "level_db": level_db if level_db is not None else "profile", "chords": a.chords or None,
               "model": model, "values": values, "dist_before": d0, "dist_after": d1, "fine_tuned": tuned,
               "lock_level_knobs": FINETUNE_LOCK_LEVEL_KNOBS if tuned else False, "search_top": [{"model": x["model"], "distance": round(x["distance"], 4)} for x in ranked[:5]],
               "low_confidence": sres.get("low_confidence"), "renders": renders, "search_wall_s": sres.get("wall_s"),
               "stage_wall_s": stage_s, "validate_exit": code, "engine": "native",
               "best_hlx": str(best), "listening_page": str(page), "run_wall_s": round(time.time() - t0, 1), **extra,
               "method": {"shortlist": "afxrep n=8", "final_rank": "afxrep", "knob_tol": 0.15, "finetune_budget": FINETUNE_BUDGET if tuned else 0}}
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    md = [f"# {sid}: {pn(model)}", "", f"- guitar: {a.guitar_id or 'library bank notes (no profile)'}; window {win[0]}-{win[1]} s; "
          f"level {summary['level_db']}", f"- model `{model}`", "- settings: " + ", ".join(f"{k} {round(v, 3)}" for k, v in values.items()),
          f"- AFx-Rep distance to the separated guitar: {d0 if d0 is None else round(d0, 4)} -> {d1 if d1 is None else round(d1, 4)} ({'fine-tuned' if tuned else 'search only'})",
          f"- renders {renders}, run wall {summary['run_wall_s']} s; hlx_validate exit {code}",
          "- search top: " + ", ".join(f"{pn(x['model'])} {x['distance']}" for x in summary["search_top"]),
          f"- preset: `{best}`; listening page: `{page}` (local only; the ear decides)", ""]
    (out / "summary.md").write_text("\n".join(md))
    return summary


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--song", required=True, help="song file (separated first when needed), a song id (matches a separated song) or a stems hash")
    ap.add_argument("--id", help="song id to use in folder names (default: the id given, or the file name as a slug)")
    ap.add_argument("--guitar-id", help="guitar profile in data/guitars/<id>/ for the rebuilt dry track; without it the library bank notes are used")
    ap.add_argument("--window", help="START:END in song seconds (default: the T200 loudest-guitar picker, 10 s)")
    ap.add_argument("--level-db", type=float, help=f"loud-frame level of the dry track in dBFS (default: the profile's level, else {DEFAULT_LEVEL_DB})")
    ap.add_argument("--chords", help='chord list for rebuild_dry --chords, e.g. "B5,G5,A5" (off by default; known songs only)')
    ap.add_argument("--finetune", action="store_true",
                    help="OPT-IN knob fine-tune by AFx-Rep distance (an option, not the default; human 2026-10-25). Output/Master are "
                         "locked at the model defaults. Without it best.hlx is the search winner")
    ap.add_argument("--workers", default="3", help="Native shard workers for the search: N or auto (default 3, the host cap this project uses)")
    ap.add_argument("--out", help="run folder (default cache/song-match/<id>-<guitar id|plain>)")
    ap.add_argument("--force", action="store_true", help="re-run every step")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and which steps are already done; run nothing")
    ap.add_argument("--load-stomp", action="store_true", help="load best.hlx into the Stomp edit buffer (only if the stompd helper is present)")
    cli_result.add_result_arg(ap)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse(argv)
    return cli_result.run(a, tool="song_match", body=lambda res: run_pipeline(a, res), inputs={"song": a.song})


if __name__ == "__main__":
    sys.exit(main())
