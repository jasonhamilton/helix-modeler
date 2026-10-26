#!/usr/bin/env python3
"""T229: colour comparison on seven songs (human 2026-10-25 verdicts on T228). LOCAL ONLY; nothing is adopted.

Variants per song (all from the T211 calibrated dry, Native, one host at a time, LoadGuard, nice 10):
  a    the fine-tuned drive match (cache/matches/<song>-finetune-prs7), last run's best
  c    a + fitted Graphic 10 Band EQ (+-6 dB, LTAS ratio; T228 code and rule; reused from T228 for the three pilot songs)
  r25  c + Room reverb Decay .40 Predelay .011 LowCut 150 HighCut 3700 Mix .25
  r35  same at Mix .35
  p35  c + Plate reverb, the same Decay/Predelay/LowCut/HighCut, Mix .35

    nice -n 10 .venv/bin/python scripts/colour_t229.py run [--songs ...]
    .venv/bin/python scripts/colour_t229.py compare | loadlist | ledger | table
"""
from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import colour_pilot_t228 as CP  # noqa: E402

BASE = ROOT / "cache" / "colour-t229"
T228 = ROOT / "cache" / "colour-pilot-t228"
CP.BASE = BASE                                   # Ctx writes under cache/colour-t229/<song>
SR = 48_000
SONGS = ("mother", "seek-destroy", "teen-spirit", "territorial", "satisfaction", "cherub-rock", "ds-sample")
PILOT = ("mother", "seek-destroy", "teen-spirit")
VARIANTS = ("a", "c", "r25", "r35", "p35")
ROOM = "HD2_ReverbRoom"
PLATE = "HD2_ReverbPlate"
REV = {"Decay": 0.40, "Predelay": 0.011, "LowCut": 150.0, "HighCut": 3700.0}
SPEC = {"r25": (ROOM, 0.25), "r35": (ROOM, 0.35), "p35": (PLATE, 0.35)}
DESC = {"a": "Last run's best: the fine-tuned drive match",
        "c": "a + 10-band graphic EQ, ±6 dB (fitted)",
        "r25": "c + Room reverb, Mix 0.25 (Decay 0.40, Predelay 11 ms, LowCut 150 Hz, HighCut 3700 Hz)",
        "r35": "c + Room reverb, Mix 0.35 (same settings)",
        "p35": "c + Plate reverb, Mix 0.35 (same Decay, Predelay, LowCut, HighCut)"}
NAME = {"mother": "Mother", "seek-destroy": "Seek & Destroy", "teen-spirit": "Smells Like Teen Spirit", "territorial": "Territorial Pissings",
        "satisfaction": "Satisfaction", "cherub-rock": "Cherub Rock", "ds-sample": "DS-Sample"}


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def load_wav(song, name):
    return sf.read(str(BASE / song / "audio" / f"{name}.wav"), dtype="float32")[0]


def run_song(song: str) -> None:
    sp = BASE / song / "state.json"
    if sp.exists() and "done" in json.loads(sp.read_text()):
        log(f"{song}: done, skipping")
        return
    ctx = CP.Ctx(song)
    ctx.open()
    try:
        st = ctx.state
        if song in PILOT:                         # reuse T228 renders (identical dry, engine and block values)
            old = json.loads((T228 / song / "state.json").read_text())
            blocks_a = [(m, v) for m, v in old["a"]["blocks"]]
            ya = sf.read(str(T228 / song / "audio" / "a.wav"), dtype="float32")[0]
            yc = sf.read(str(T228 / song / "audio" / "c.wav"), dtype="float32")[0]
            stem0 = sf.read(str(T228 / song / "audio" / "stem.wav"), dtype="float32")[0]
            st["reuse_check"] = {"stem_max_abs_diff": float(np.max(np.abs(stem0[: len(ctx.stem)] - ctx.stem[: len(stem0)])))}
            ctx.wav("a", ya); ctx.wav("stem", ctx.stem); ctx.wav("dry", ctx.dry); ctx.wav("c", yc)
            st["a"] = {"blocks": blocks_a, "renders": 0, "reused_from": "T228"}
            st["c"] = {**old["c"], "renders": 0, "reused_from": "T228", "blocks_all": [(m, v) for m, v in old["c"]["blocks_all"]]}
            blocks_c = st["c"]["blocks_all"]
        else:
            r = CP.stage_a(ctx)
            ya, blocks_a = r["y"], r["blocks"]
            m = sf.read(str(ROOT / f"cache/matches/{song}-finetune-prs7/match.wav"), dtype="float32")[0]
            n = min(len(m), len(ya))
            st["a_vs_match_wav_max_abs_diff"] = float(np.max(np.abs(m[:n] - ya[:n])))
            r = CP.stage_eq(ctx, ya, blocks_a, "c")
            yc, blocks_c = r["y"], r["blocks"]
        st["c_is_a"] = len(blocks_c) == len(blocks_a)
        for v, (model, mix) in SPEC.items():
            vals = {**REV, "Mix": mix}
            y = ctx.render_block(model, vals, yc)
            ctx.wav(v, y)
            st[v] = {"blocks_all": [*blocks_c, (model, vals)], "renders": 1}
        st["renders_total"] = ctx.renders
        st["wall_s"] = time.time() - ctx.t0
        write_hlx(ctx)
        st["done"] = True
        ctx.save()
        log(f"{song}: renders this run {ctx.renders}")
    finally:
        ctx.close()


def write_hlx(ctx) -> None:
    from tonematch.preset import write_hlx as wh
    from tonematch.search.chain_builder import BlockSpec, assemble_preset

    cat_of = lambda m: ctx.catalog["models"][m]["category"]  # noqa: E731
    out = {}
    for v in VARIANTS:
        bl = ctx.state["a"]["blocks"] if v == "a" else (ctx.state["c"]["blocks_all"] if v == "c" else ctx.state[v]["blocks_all"])
        specs = [BlockSpec(cat_of(m), m).with_values(vals) for m, vals in bl]
        p = assemble_preset(specs, ctx.lib, name=f"{ctx.song[:6]}-{v}")
        from tonematch.preset import validate as pv
        viol = pv(p, ctx.catalog)
        assert not [x for x in viol if getattr(x, 'severity', 'error') == 'error'], viol
        path = ctx.dir / "hlx" / "best.hlx" if False else ctx.dir / "hlx" / f"{v}" / "best.hlx"
        path.parent.mkdir(parents=True, exist_ok=True)
        wh(p, path)
        code, text = CP.validate_hlx(path)
        out[v] = {"hlx": str(path), "validate_exit": code, "validate_last": text.splitlines()[-1] if text else "",
                  "blocks": [m for m, _ in bl]}
    ctx.state["hlx"] = out


def cmd_run(songs):
    for s in songs:
        run_song(s)


def cmd_compare(songs):
    import dry_ablation_t224 as DA

    out = BASE / "compare"
    secs = []
    for s in songs:
        st = json.loads((BASE / s / "state.json").read_text())
        stem = load_wav(s, "stem")
        mats = {v: load_wav(s, v) for v in VARIANTS}
        n = min([len(stem)] + [len(y) for y in mats.values()])
        t2, m2, gains = DA.level_match_pack(stem[:n], {v: y[:n] for v, y in mats.items()})
        d = out / s
        d.mkdir(parents=True, exist_ok=True)
        sf.write(str(d / "song.wav"), t2, SR, subtype="FLOAT")
        for v in VARIANTS:
            sf.write(str(d / f"{v}.wav"), m2[v], SR, subtype="FLOAT")
            sf.write(str(d / f"L-song_R-{v}.wav"), np.stack([t2, m2[v]], 1), SR, subtype="FLOAT")
            if v != "a":
                sf.write(str(d / f"L-a_R-{v}.wav"), np.stack([m2["a"], m2[v]], 1), SR, subtype="FLOAT")
        pl = lambda src: f'<audio controls preload="none" src="{src}"></audio>'  # noqa: E731
        h = f'<section><h2>{html.escape(NAME[s])}</h2><div class="v"><h3>The song\'s guitar (separated stem)</h3>{pl(f"{s}/song.wav")}</div>'
        for v in VARIANTS:
            chain = " > ".join(m.replace("HD2_", "") for m in st["hlx"][v]["blocks"])
            h += f'<div class="v"><h3>{v}. {html.escape(DESC[v])}</h3><p class="m">{html.escape(chain)}</p>'
            h += f"<b>Left: song's guitar · Right: {v}</b>{pl(f'{s}/L-song_R-{v}.wav')}"
            if v != "a":
                h += f"<b>Left: last run's best (a) · Right: {v}</b>{pl(f'{s}/L-a_R-{v}.wav')}"
            h += f"<b>{v} alone</b>{pl(f'{s}/{v}.wav')}</div>"
        secs.append(h + "</section>")
    head = open(T228 / "compare" / "index.html").read().split("<main>")[0].replace("Colour Pilot Listening", "Colour Comparison T229")
    body = ("<main><header><h1>Colour Comparison, seven songs</h1><p>Local only. Every clip is loudness-matched (LUFS −23). \"a\" is the best "
            "from the last run (the fine-tuned drive match). c adds the fitted 10-band graphic EQ to a. r25 and r35 add a light Room reverb "
            "(Mix 0.25 and 0.35) after c; p35 adds a Plate reverb (Mix 0.35, same decay and filters) after c. Headphones for the left/right "
            "players. Nothing is adopted.</p></header>" + "".join(secs) + "</main>")
    (out / "index.html").write_text(head + body)
    log(f"page {out / 'index.html'}")


def cmd_loadlist(songs):
    L = ["# T229 colour comparison: Stomp load list (local only)", "",
         "Nothing was loaded on the Stomp by this task. Each row is one validated preset (`hlx_validate` exit code shown). To put one in the edit buffer "
         "(never a flash write):", "", "    .venv/bin/python scripts/stomp_live_preview.py --dir <folder with the .hlx as current_best.hlx> --once --no-restore-on-exit",
         "", "or import the .hlx in HX Edit.", "", "| song | variant | what | blocks | hlx | validate |", "|---|---|---|---|---|---|"]
    for s in songs:
        st = json.loads((BASE / s / "state.json").read_text())
        for v in VARIANTS:
            h = st["hlx"][v]
            L.append(f"| {s} | {v} | {DESC[v]} | {' > '.join(m.replace('HD2_', '') for m in h['blocks'])} | `{h['hlx']}` | exit {h['validate_exit']} |")
    (BASE / "load_list.md").write_text("\n".join(L) + "\n")
    log(f"load list {BASE / 'load_list.md'}")


def cmd_ledger(songs):
    env = dict(os.environ, TONEMATCH_AGENT="search")
    for s in songs:
        st = json.loads((BASE / s / "state.json").read_text())
        for v in VARIANTS:
            if v == "a":
                n = 0 if st["a"].get("reused_from") else 1
                note = "fine-tuned drive match (last run's best)" + ("; T228 render reused" if n == 0 else "")
            elif v == "c":
                n = st["c"].get("renders", 0)
                note = f"+ EQ Graphic10 +-6 dB; LTAS residual {st['c']['residual_before_db']:.2f} -> {st['c']['residual_after_db']:.2f} dB" + \
                       ("; T228 render reused" if st["c"].get("reused_from") else "")
            else:
                n = 1
                mdl, mix = SPEC[v]
                note = f"c + {mdl.replace('HD2_Reverb', '')} reverb Mix {mix}, Decay .40 Predelay .011 LowCut 150 HighCut 3700"
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "search", "--engine", "native",
                            "--dataset", f"colour-t229-{s}-{v}", "--metric", "renders", "--value", str(n), "--renders", str(n),
                            "--notes", "T229 colour comparison (measurement for the ear; nothing adopted): " + note], cwd=ROOT, env=env, check=True)


def cmd_table(songs):
    tot = 0
    for s in songs:
        st = json.loads((BASE / s / "state.json").read_text())
        c = st["c"]
        print(s, "renders", st["renders_total"], "| gains", [round(g, 1) for g in c["gains_db"]], "| resid", round(c["residual_before_db"], 2),
              "->", round(c["residual_after_db"], 2), "| c_is_a", st["c_is_a"], "| a_vs_match", st.get("a_vs_match_wav_max_abs_diff"),
              "| reuse", st.get("reuse_check"))
        tot += st["renders_total"]
    print("total", tot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "compare", "loadlist", "ledger", "table"])
    ap.add_argument("--songs", nargs="+", default=list(SONGS))
    a = ap.parse_args()
    {"run": cmd_run, "compare": cmd_compare, "loadlist": cmd_loadlist, "ledger": cmd_ledger, "table": cmd_table}[a.cmd](a.songs)


if __name__ == "__main__":
    main()
