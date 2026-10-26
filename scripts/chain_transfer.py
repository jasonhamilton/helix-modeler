#!/usr/bin/env python3
"""chain-transfer-v1 (T214, learning): the single-block slot heads (T213, T191/T196) on full block-serial chains.

Human 2026-10-23 "yes" (DECISIONS "chain transfer test approved"). Measurement only: no pass bar, nothing adopted.

    .venv/bin/python scripts/chain_transfer.py plan                 # seeded chain list -> cache/chain-transfer-v1/chains.json
    nice -n 10 .venv/bin/python scripts/chain_transfer.py render --hosts 3   # Native, stage by stage -> features.npz
    .venv/bin/python scripts/chain_transfer.py score [--ledger]     # heads on the chain output -> results.{json,md}

Plan: about 300 chains drive (p 0.7) > amp > cab > mod (0.2) > delay (0.3) > reverb (0.3) > EQ (0.2); models and
drawn knobs from slot-dataset-v1's plans (and drive-dataset-v1's pool for drive); every drawn knob uniform on the CC
grid, level knobs at their default; the 0.5-2 s lead-in when a time effect is in the chain. DI: drive-dataset-v1
`test` split files only (P3 never read). Render: block-serial on Helix Native (library templates, preroll 3 s as
the training renders), at most --hosts hosts (host scaler lease), a fresh pool per stage, busy-core LoadGuard before
each chunk, -240 dBFS silence floor, one retry. Features: AFx-Rep mid, 3 x 3.33 s crops of the 10 s clip, after
every stage. Audio is not kept. Score: each slot's full-size MLP head (logreg for reference) and the drive head of
`match_block --shortlist predictor` on the finished chain output, next to the same head's single-block test numbers.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("TONEMATCH_AFXREP_DEVICE", "cpu")
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tonematch.learning import chain_transfer as CT  # noqa: E402

OUT = ROOT / "cache/chain-transfer-v1"
DRIVE_PLAN = ROOT / "cache/drive-dataset-v1/plan.json"
SLOT_BASE = ROOT / "cache/slot-dataset-v1"
SLOT_MODELS = ROOT / "cache/models/slot-dataset-v1"
DRIVE_HEADS = {"mlp": ROOT / "cache/models/predictor-dataset-v1/heads_main_v2data_mlp_multi.pkl",
               "logreg": ROOT / "cache/models/predictor-dataset-v1/heads_main_v2data_logreg_multi.pkl"}
SLOTS = ("amp", "cab", "mod", "delay", "reverb", "eq")
DATASET = "chain-transfer-v1"


def log(msg: str, out: Path = OUT) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "run.log", "a") as f:
        f.write(line + "\n")


# ------------------------------------------------------------------------------------------ pools

def drive_pool_knobs() -> tuple[dict, dict]:
    """drive-dataset-v1's models, model -> drawn knob names (draw_setting: every free knob except level knobs,
    unless the level knob is the model's Drive or Tone knob), and families."""
    import drive_dataset_render as DD
    from tonematch.learning import drive_pilot as dp
    plan = json.loads(DRIVE_PLAN.read_text())
    pool, kn = DD.drive_pool()
    _, _, m2f = dp.families()
    models = [m for m in plan["models"] if m in pool]
    knobs = {m: [n for n, *_ in pool[m] if n not in DD.LEVEL_NAMES or n in kn[m]] for m in models}
    return {"models": models, "knobs": knobs, "families": {m: m2f[m] for m in models}}, {m: pool[m] for m in models}


def slot_pools() -> dict:
    out = {}
    for s in SLOTS:
        p = json.loads((SLOT_BASE / s / "plan.json").read_text())
        out[s] = {"models": list(p["models"]), "knobs": {m: list(p["knobs"][m]) for m in p["models"]},
                  "families": dict(p["families"])}
    return out


# ------------------------------------------------------------------------------------------ plan

def cmd_plan(a) -> int:
    import drive_dataset_render as DD
    out = Path(a.out)
    pp = out / "chains.json"
    if pp.is_file() and not a.force:
        print(f"chain list exists: {pp} (--force to resample)")
        return 0
    dplan = json.loads(DRIVE_PLAN.read_text())
    files = [f for f in dplan["files"]["test"] if DD.player_of(f) in ("P1", "P2") and "/P3" not in f]
    for s in SLOTS:     # the slot plans reuse drive's split; check the held-out files are the same
        sp = json.loads((SLOT_BASE / s / "plan.json").read_text())
        assert sorted(sp["files"]["test"]) == sorted(files), f"{s} test files differ from drive's"
    starts = {f: DD.active_starts(DD.read_mono_file(f)) for f in files}
    dpool, _ = drive_pool_knobs()
    pools = {"drive": dpool, **slot_pools()}
    chains = CT.make_chains(files, starts, pools, n=a.n, seed=a.seed, player_of=DD.player_of)
    from tonematch.dry_rebuild import loud_level_db
    n = int(CT.CLIP_S * CT.SR)
    for c in chains:
        x = DD.read_mono_file(c["di_file"], c["start"] + c["lead"], n)
        c["di_level_db"] = round(loud_level_db(np.asarray(x, np.float32)), 2)
    from collections import Counter
    cnt = Counter(s for c in chains for s in CT.slots_of(c))
    doc = {"brief": "T214 chain-transfer-v1: random chains in the fixed slot order, drive-dataset-v1 test-split DI "
                    "files only (P3 never read); measurement only, nothing adopted", "seed": a.seed, "n": len(chains),
           "probs": CT.PROBS, "lead_s": CT.LEAD_S, "order": CT.CHAIN_ORDER, "silence_db": CT.SILENCE_DB,
           "files": files, "slot_counts": dict(cnt), "chains": chains}
    out.mkdir(parents=True, exist_ok=True)
    from tonematch.engines.elastic import atomic_write_json
    atomic_write_json(pp, doc, indent=1)
    print(json.dumps({"n": len(chains), "files": len(files), "slot_counts": dict(cnt),
                      "with_lead": sum(1 for c in chains if c["lead"])}, indent=1))
    return 0


# ------------------------------------------------------------------------------------------ render

def embed_crops_fn():
    import torch
    torch.set_num_threads(2)
    from tonematch.metrics import afxrep
    emb = afxrep.Embedder(threads=2)
    n = int(CT.CLIP_S * CT.SR)

    def embed(y):   # = drive_dataset_render.embed_clip's crops: three contiguous n//3 crops of the 10 s clip
        y = y[:n] if len(y) >= n else np.pad(y, (0, n - len(y)))
        L = n // 3
        return np.stack([np.asarray(f.mid, np.float32) for f in emb.embed_batch([y[k * L:(k + 1) * L] for k in range(3)])])
    return embed


def make_presets(chains: list[dict]):
    """presets(block) -> the single-block library preset with the block's CC knobs (as the training renders)."""
    import match_block as MB
    from tonematch.catalog import load_catalog
    catalog = load_catalog()
    _, dfree = drive_pool_knobs()
    need: dict[str, set] = {}
    for c in chains:
        for b in c["blocks"]:
            need.setdefault(b["slot"], set()).add(b["model"])
    blocks: dict[str, dict] = {}
    for slot, ms in need.items():
        if slot == "drive":
            pool = {m: dfree[m] for m in ms}
            cat = "drive"
        else:
            from tonematch.learning import slot_dataset as SD
            full = SD.slot_pool(slot, catalog)
            missing = [m for m in ms if m not in full]
            if missing:
                raise RuntimeError(f"{slot}: {len(missing)} models not in this catalog/library")
            pool = {m: full[m] for m in ms}
            cat = SD.SLOT_CONFIG[slot]["category"]
        blocks[slot] = MB.make_blocks(pool, cat, catalog)

    def presets(b):
        blk = blocks[b["slot"]][b["model"]]
        return blk.with_values(blk.from_knobs(b["cc"]))
    return presets


def pool_opener(hosts: int):
    """open_pool() -> a NativeRenderPool of <= hosts hosts on the single-block library templates (3 s preroll)."""
    from tonematch.engines import native_library as NL
    from tonematch.engines.native import NativeRenderPool
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    hv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and hv.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(hv)
    tdir = NL.library_templates()

    def open_pool():
        return NativeRenderPool(workers=hosts, preroll=144_000, templates_dir=tdir)
    return open_pool


def cmd_render(a) -> int:
    import drive_dataset_render as DD
    from tonematch.engines import host_budget as hb
    from tonematch.engines.native import RenderJob

    out = Path(a.out)
    doc = json.loads((out / "chains.json").read_text())
    chains = doc["chains"][: a.limit] if a.limit else doc["chains"]
    assert not any("/P3" in c["di_file"] for c in chains), "P3 in the chain list"
    presets = make_presets(chains)
    open_pool = pool_opener(a.hosts)
    guard = hb.LoadGuard(a.max_busy)
    embed = embed_crops_fn()
    t0 = time.time()
    log(f"render: {len(chains)} chains, hosts <= {a.hosts}, pid {os.getpid()}", out)
    res = CT.run_chains(chains, presets, DD.read_mono_file, open_pool, embed,
                        make_job=lambda d, p: RenderJob(dry=d, preset=p), guard=guard,
                        log=lambda m: log(m, out), chunk=a.chunk)
    wall = time.time() - t0
    np.savez_compressed(out / "features.npz", crops=res["crops"], has=res["has"], out_db=res["out_db"],
                        ids=np.array([c["id"] for c in chains]), order=np.array(CT.CHAIN_ORDER))
    n_renders = int(res["has"].sum()) + len(res["failed"])
    meta = {"n_chains": len(chains), "renders_ok": int(res["has"].sum()), "failed": res["failed"],
            "stage_s": res["stage_s"], "wall_s": round(wall, 1), "hosts": a.hosts}
    (out / "render_meta.json").write_text(json.dumps(meta, indent=1))
    log(f"render done: {json.dumps({k: meta[k] for k in ('n_chains', 'renders_ok', 'wall_s')})}, "
        f"failed {len(res['failed'])}", out)
    if a.ledger:
        ledger(["--kind", "render-cost", "--engine", "native", "--dataset", DATASET, "--metric", "wall_s_per_block",
                "--value", f"{wall / max(1, n_renders):.3f}", "--renders", str(n_renders), "--wall-clock-s",
                f"{wall:.0f}", "--notes", f"T214 block-serial chain renders, {len(chains)} chains, <= {a.hosts} "
                f"hosts, fresh pool per stage, includes AFx-Rep embedding after each stage; failed chains "
                f"{len(res['failed'])}; P3 unread"])
    return 0


def ledger(args: list[str]) -> None:
    os.environ.setdefault("TONEMATCH_AGENT", "learning")
    subprocess.run([sys.executable, str(ROOT / "scripts/ledger_append.py"), *args], check=False, cwd=str(ROOT))


# ------------------------------------------------------------------------------------------ score

class SlotHead:
    """A pickled head (slot-dataset-v1 or the T191 drive head): heads, calibration, model list, families."""

    def __init__(self, path: Path, slot: str, allowed: list[str] | None = None):
        import pickle
        d = pickle.load(open(path, "rb"))
        h = d["heads"]
        if isinstance(getattr(h, "net", None), dict):
            net = h._build(512, h.n_fam, h.M)
            net.load_state_dict(h.net)
            h.net = net.eval()
            h.device = "cpu"
        self.path, self.slot, self.h, self.cal = Path(path), slot, h, d["calibration"]
        self.models = list(d["models"])
        self.mod_fam = np.asarray(h.mod_fam)
        self.allowed = None if allowed is None else np.array([m in set(allowed) for m in self.models])

    def logp(self, crops: np.ndarray):
        from tonematch.learning import afx_heads as ah
        o = self.h.predict(np.asarray(crops, np.float32))
        T = self.cal.T if self.cal is not None else 1.0
        lj, lf = ah.joint_logp(o["fl"], o["lc"], self.mod_fam, T)
        if self.allowed is not None:
            lj = lj.copy()
            lj[:, ~self.allowed] = -np.inf
        return lj, lf, o["mu"]


def truth(head: SlotHead, blocks: list[dict], knobs: dict[str, list[str]] | None, drive_kn: dict | None = None):
    """y_m, y_f, K for the given true blocks. Slots: column j = knobs[model][j] (the training plan's order).
    Drive: columns (Drive knob, Tone knob) = the T191 labels."""
    mi = {m: i for i, m in enumerate(head.models)}
    y = np.array([mi[b["model"]] for b in blocks])
    yf = head.mod_fam[y]
    if drive_kn is not None:
        K = np.full((len(blocks), 2), np.nan)
        for i, b in enumerate(blocks):
            dk, tk = drive_kn[b["model"]]
            for j, k in enumerate((dk, tk)):
                if k and k in b["cc"]:
                    K[i, j] = b["cc"][k] / 127.0
        return y, yf, K
    from tonematch.learning import slot_dataset as SD
    lab = [{"model": b["model"], "cc": b["cc"]} for b in blocks]
    return y, yf, SD.knob_matrix(lab, head.models, knobs)


def single_block_drive(head: SlotHead, drive_kn: dict, split: str = "test") -> dict:
    """The drive head's single-block numbers on drive-dataset-v1's held-out `test` files (new renders), scored with
    this module's code (the T191 summary has no top-3 and used +-0.10 for Tone)."""
    import drive_dataset_render as DD
    sdir = ROOT / "cache/drive-dataset-v1/shards"
    L, C = [], []
    for f in sorted(sdir.glob("s*.json")):
        sid = f.stem
        try:
            lab = [json.loads(x) for x in (sdir / f"{sid}.labels.jsonl").read_text().splitlines() if x.strip()]
            sel = [i for i, r in enumerate(lab) if r.get("split") == split]
            if not sel:
                continue
            c = np.load(sdir / f"{sid}.crops.npy")
        except (OSError, ValueError):
            continue
        if len(c) != len(lab):
            continue
        L += [lab[i] for i in sel]
        C.append(c[sel].astype(np.float32))
    C = np.concatenate(C)
    blocks = [{"model": r["model"], "cc": r["cc"]} for r in L]
    keep = [i for i, b in enumerate(blocks) if b["model"] in set(head.models)]
    blocks, C = [blocks[i] for i in keep], C[keep]
    y, yf, K = truth(head, blocks, None, drive_kn)
    out = []
    for i in range(0, len(C), 4096):
        out.append(head.logp(C[i:i + 4096]))
    lj, lf, mu = (np.concatenate([o[k] for o in out]) for k in range(3))
    s = CT.item_scores(lj, lf, mu, y, yf, K)
    r = CT.aggregate(s, boot=200)
    r["files"] = len({x["di_file"] for x in L})
    for j, nm in enumerate(("Drive", "Tone")):
        h = s["knob_hit"][:, j][s["knob_has"][:, j]]
        r[f"knob_{nm}"] = float(h.mean()) if len(h) else math.nan
    _ = DD
    return r


def single_block_ref(slot: str, head: str) -> dict:
    import glob
    fs = sorted(glob.glob(str(SLOT_BASE / slot / "summary-n*.json")))
    fs = [f for f in fs if "frac" not in Path(f).name]
    d = json.loads(Path(fs[-1]).read_text())
    r = d["results"][f"main/{head}/multi/s0"]["test"]
    return {"top1": r["model_top1"], "top3": r["model_top3"], "top5": r["model_top5"], "fam1": r["family_top1"],
            "knob": r["knob_within_true"], "n": r["n"], "files": r["files"], "summary": Path(fs[-1]).name,
            "by_name": r.get("by_name", {})}


def slot_head_path(slot: str, head: str) -> Path:
    d = sorted(p for p in (SLOT_MODELS / slot).iterdir() if p.is_dir() and not p.name.startswith("frac"))
    return d[-1] / f"heads_main_{head}_multi.pkl"


def cmd_score(a) -> int:
    import torch
    torch.set_num_threads(2)
    from tonematch.learning import drive_pilot as dp

    out = Path(a.out)
    doc = json.loads((out / "chains.json").read_text())
    F = np.load(out / "features.npz")
    ids = list(F["ids"])
    byid = {c["id"]: c for c in doc["chains"]}
    chains = [byid[i] for i in ids]
    crops, has = F["crops"].astype(np.float32), F["has"]
    meta = json.loads((out / "render_meta.json").read_text())
    failed = set(meta["failed"])
    order = list(CT.CHAIN_ORDER)
    last = np.array([max([k for k in range(len(order)) if has[i, k]], default=-1) for i in range(len(chains))])
    ok_chain = np.array([c["id"] not in failed and last[i] >= 0 for i, c in enumerate(chains)])
    final = crops[np.arange(len(chains)), np.maximum(last, 0)]
    pools = slot_pools()
    dpool, _ = drive_pool_knobs()
    drive_kn = dp._knob_names()
    res = {"brief": "T214 chain-transfer-v1: single-block heads on block-serial chain outputs; measurement only, "
                    "nothing adopted", "n_chains": int(ok_chain.sum()), "failed": sorted(failed), "slots": {}}
    md_rows = {}
    for slot in order:
        idx = [i for i, c in enumerate(chains) if ok_chain[i] and CT.block_of(c, slot) is not None]
        if not idx:
            continue
        blocks = [CT.block_of(chains[i], slot) for i in idx]
        si = order.index(slot)
        sr = {"n": len(idx), "heads": {}}
        for head in ("mlp", "logreg"):
            if slot == "drive":
                H = SlotHead(DRIVE_HEADS[head], "drive", allowed=dpool["models"])
                y, yf, K = truth(H, blocks, None, drive_kn)
                names = [["Drive", "Tone"]] * len(H.models)
            else:
                H = SlotHead(slot_head_path(slot, head), slot)
                y, yf, K = truth(H, blocks, pools[slot]["knobs"])
                names = [pools[slot]["knobs"][m] for m in H.models]
            lj, lf, mu = H.logp(final[idx])
            s = CT.item_scores(lj, lf, mu, y, yf, K)
            hr = {"checkpoint": str(H.path.relative_to(ROOT)), "all": CT.aggregate(s),
                  "by_name": CT.knob_by_name(s, names, y)}
            if slot == "drive":
                hr["single_block"] = single_block_drive(H, drive_kn)
            else:
                hr["single_block"] = single_block_ref(slot, head)
            # prefix diagnostic: the same head on the output right after its own block
            lj0, lf0, mu0 = H.logp(crops[idx, si])
            s0 = CT.item_scores(lj0, lf0, mu0, y, yf, K)
            hr["right_after_block"] = CT.aggregate(s0)
            # context breakdowns
            ctx = [CT.contexts(chains[i], slot) for i in idx]
            br = {}
            for key in ctx[0]:
                v = np.array([c.get(key) for c in ctx], object)
                yes, no = v == True, v == False   # noqa: E712
                if yes.sum() and no.sum():
                    br[key] = {"yes": CT.aggregate(s, yes, boot=300), "no": CT.aggregate(s, no, boot=300)}
            hr["breakdown"] = br
            # by stage reached: head on the output after each later stage (MLP only)
            if head == "mlp":
                steps = {}
                for k in range(si, len(order)):
                    sel = [j for j, i in enumerate(idx) if has[i, k]]
                    if not sel:
                        continue
                    lj2, lf2, mu2 = H.logp(crops[[idx[j] for j in sel], k])
                    s2 = CT.item_scores(lj2, lf2, mu2, y[sel], yf[sel], K[sel])
                    s1 = CT.aggregate(s0, np.isin(np.arange(len(idx)), sel), boot=0)
                    steps[order[k]] = {"after": CT.aggregate(s2, boot=0), "same_items_right_after": s1}
                hr["after_stage"] = steps
                # confusions: the most frequent wrong top-1 picks
                from collections import Counter
                wrong = Counter((H.models[int(t)], H.models[int(p)]) for t, p, h in zip(y, s["pred"], s["top1"]) if not h)
                hr["top_confusions"] = [[t, p, k] for (t, p), k in wrong.most_common(8)]
            sr["heads"][head] = hr
        res["slots"][slot] = sr
        md_rows[slot] = sr
    from tonematch.engines.elastic import atomic_write_json, atomic_write_text
    atomic_write_json(out / "results.json", res, indent=1, default=float)
    atomic_write_text(out / "results.md", results_md(res, meta, doc))
    print((out / "results.md").read_text())
    if a.ledger:
        for slot, sr in res["slots"].items():
            m = sr["heads"]["mlp"]
            al, sb, ra = m["all"], m["single_block"], m["right_after_block"]
            lg = sr["heads"]["logreg"]["all"]
            notes = (f"T214 measurement only not adopted. {slot} MLP head ({m['checkpoint']}) on finished block-serial "
                     f"chain output; n {al['n']} chains of {res['n_chains']}; top1 {al['top1']:.3f}; top3 {al['top3']:.3f} "
                     f"(95% {al['top3_ci'][0]:.2f}-{al['top3_ci'][1]:.2f}); family {al['fam1']:.3f}; knob within 0.15 true "
                     f"{al['knob']:.3f}; single-block test top1 {sb['top1']:.3f} top3 {sb['top3']:.3f} knob {sb['knob']:.3f}; "
                     f"right after its block top3 {ra['top3']:.3f}; logreg top1 {lg['top1']:.3f} top3 {lg['top3']:.3f}; "
                     f"DI drive-dataset-v1 test split only; P3 unread")
            ledger(["--kind", "benchmark", "--engine", "native", "--dataset", DATASET, "--metric",
                    f"{slot}_mlp_model_top3", "--value", f"{al['top3']:.4f}", "--renders",
                    str(meta["renders_ok"]), "--wall-clock-s", f"{meta['wall_s']:.0f}", "--notes", notes])
    return 0


def _p(x) -> str:
    return "" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:.2f}"


def _ci(r: dict, k: str) -> str:
    lo, hi = r.get(k + "_ci", (math.nan, math.nan))
    return f"{_p(r[k])} ({_p(lo)}-{_p(hi)})"


def results_md(res: dict, meta: dict, doc: dict) -> str:
    L = ["# chain-transfer-v1 (T214): results", "",
         f"Measurement only; nothing adopted; no pass bar. {res['n_chains']} chains scored "
         f"({len(res['failed'])} failed). Render wall {meta['wall_s']:.0f} s on <= {meta['hosts']} hosts.", "",
         "## Per slot: MLP head on the finished chain vs the same head on single blocks (held-out test files)", "",
         "| Slot | n | Top-1 chain (95 %) | Top-1 single | Top-3 chain (95 %) | Top-3 single | Family top-1 chain / single "
         "| Knobs +-0.15 chain (95 %) | Knobs single | Top-3 right after its block | Logreg top-1 / top-3 chain | Logreg top-1 / top-3 single |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for slot, sr in res["slots"].items():
        m, g = sr["heads"]["mlp"], sr["heads"]["logreg"]
        a, sb, ra = m["all"], m["single_block"], m["right_after_block"]
        L.append(f"| {slot} | {a['n']} | {_ci(a, 'top1')} | {_p(sb['top1'])} | {_ci(a, 'top3')} | {_p(sb['top3'])} "
                 f"| {_p(a['fam1'])} / {_p(sb['fam1'])} | {_ci(a, 'knob')} | {_p(sb['knob'])} | {_p(ra['top3'])} "
                 f"| {_p(g['all']['top1'])} / {_p(g['all']['top3'])} | {_p(g['single_block']['top1'])} / "
                 f"{_p(g['single_block']['top3'])} |")
    L += ["", "## Breakdowns by what else is in the chain (MLP; top-1 / top-3 / knobs, n)", ""]
    for slot, sr in res["slots"].items():
        br = sr["heads"]["mlp"]["breakdown"]
        if not br:
            continue
        L += [f"### {slot}", "", "| Context | yes: top-1 / top-3 / knobs (n) | no: top-1 / top-3 / knobs (n) |",
              "|---|---|---|"]
        for k, v in br.items():
            y, n = v["yes"], v["no"]
            L.append(f"| {k} | {_p(y['top1'])} / {_p(y['top3'])} / {_p(y['knob'])} ({y['n']}) "
                     f"| {_p(n['top1'])} / {_p(n['top3'])} / {_p(n['knob'])} ({n['n']}) |")
        L.append("")
    L += ["## Along the chain (MLP top-1 / top-3 on the output after each stage, same chains)", ""]
    for slot, sr in res["slots"].items():
        st = sr["heads"]["mlp"].get("after_stage", {})
        L.append(f"- **{slot}**: " + "; ".join(f"after {k} {_p(v['after']['top1'])} / {_p(v['after']['top3'])} "
                                                 f"(n {v['after']['n']})" for k, v in st.items()))
    L += ["", "## Most frequent wrong top-1 picks (MLP; true -> picked, count)", ""]
    for slot, sr in res["slots"].items():
        c = sr["heads"]["mlp"].get("top_confusions", [])
        L.append(f"- **{slot}**: " + "; ".join(f"{t} -> {p} ({k})" for t, p, k in c))
    L.append("")
    return "\n".join(L) + "\n"


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--n", type=int, default=CT.N_CHAINS)
    p.add_argument("--seed", type=int, default=CT.SEED)
    p.add_argument("--force", action="store_true")
    r = sub.add_parser("render")
    r.add_argument("--hosts", type=int, default=3)
    r.add_argument("--chunk", type=int, default=24)
    r.add_argument("--max-busy", type=float, default=None, help="pause above this many busy cores (default 0.95 x cores)")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--ledger", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("--ledger", action="store_true")
    for x in (p, r, s):
        x.add_argument("--out", default=str(OUT))
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse(argv)
    if a.hosts if a.cmd == "render" else 0:
        assert 1 <= a.hosts <= 4, "at most 4 Native hosts"
    return {"plan": cmd_plan, "render": cmd_render, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
