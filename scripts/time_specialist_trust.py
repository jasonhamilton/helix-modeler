#!/usr/bin/env python3
"""Trust check of the time family score (T122; bar registered in docs/DECISIONS.md 2026-10-17 "time-based specialist").

  render   per delay / reverb item of a set (default synthetic-devfam): the true preset and its 24 neighbours
           rendered once on the item's DI A (Native, one host); saved to cache/time-specialist/trust/<set>/<id>.npz
  score    rank-1 of the true preset against the neighbours (known-DI mode: the target is the true render passed through
           `aligned_capture`; rule B ties at a -30 dB null) under (a) the admitted known-DI mrstft_v2 and (b) the same plus
           the time family term at weight w; prints rates by category and split and, with --ledger, writes kind loss-trust rows

Rendering is separate from scoring so the weight can be tuned on the tuning half without a render.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

SEED = 20261016


def split_of(labels: list[dict], it: dict) -> str:
    """Tuning half = even positions within the category, held-out = odd (DECISIONS 2026-10-17, T122)."""
    pos = [x["id"] for x in labels if x["category"] == it["category"]].index(it["id"])
    return "tune" if pos % 2 == 0 else "heldout"


def cmd_render(a) -> int:
    import soundfile as sf
    import synthetic_v1 as S
    from time_specialist_fit import wait_for_load

    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL

    wait_for_load()
    S.set_paths(a.set)
    man = json.loads((S.SET_DIR / "manifest.json").read_text())
    S.adopt_set_categories(man)
    labels = json.loads((S.SET_DIR / "labels.json").read_text())
    cat = load_catalog()
    pool = {m: S.Block(m, c, S.free_params(m, NL.template_for(m), cat, c), cat) for c in man["categories"] for m in man["pool"][c]}
    out = ROOT / "cache" / "time-specialist" / "trust" / a.set
    out.mkdir(parents=True, exist_ok=True)
    items = [it for it in labels if it["category"] in a.categories]
    engine = S.make_engine()
    try:
        for it in items:
            f = out / f"{it['id']}.npz"
            if f.exists():
                continue
            dry = sf.read(str(ROOT / it["di"]["A"]["file"]), dtype="float32")[0][: int(man["clip_seconds"] * S.SR)]
            rows, names = [], []
            for nb in [{"model": it["model"], "knobs": it["knobs"], "label": "true"}] + ([] if a.truth_only else it["neighbours"]):
                b = pool[nb["model"]]
                engine.load(b.with_values(b.from_knobs(nb["knobs"])))
                try:
                    rows.append(np.asarray(engine.render(dry), np.float32))
                except Exception:  # noqa: BLE001 - e.g. a swell delay that renders silence
                    rows.append(np.zeros_like(dry))
                names.append(f"{nb['label']}|{nb['model']}")
            np.savez_compressed(f, dry=dry, renders=np.stack(rows), names=np.array(names))
            print(it["id"], it["category"], it["model"][4:], flush=True)
    finally:
        engine.close()
    return 0


def cmd_score(a) -> int:
    import synthetic_v1 as S
    from loss_trust_check import aligned_capture

    from tonematch.metrics import LossKit
    from tonematch.metrics.trust import null_db
    from tonematch.search.time_specialist import TimeFamilyScore

    S.set_paths(a.set)
    labels = json.loads((S.SET_DIR / "labels.json").read_text())
    weights, _admitted, wlabel = S.load_weights(a.weights)
    kit = LossKit()
    res = {}
    for it in labels:
        f = ROOT / "cache" / "time-specialist" / "trust" / a.set / f"{it['id']}.npz"
        if not f.exists():
            continue
        z = np.load(f)
        dry, R = z["dry"], z["renders"]
        if it["category"] not in ("delay", "reverb") or len(R) < 2:
            continue                                  # truth-only renders (mod, amp) are for the screen check
        if not np.any(R[0]):
            continue                                  # the true render is silent: unscoreable
        cap = aligned_capture(R[0], np.random.default_rng([SEED, 98, int(it["id"][1:])]))
        fam = TimeFamilyScore(cap, dry)
        d_fam = [fam.distance(r) if np.any(r) else 5.0 for r in R]
        base = [float(kit.score(cap, r, weights=weights).total) for r in R]
        nulls = [0.0] + [float(null_db(R[0], r)) for r in R[1:]]
        res[it["id"]] = {"category": it["category"], "split": split_of(labels, it), "base": base, "fam": d_fam, "null": nulls}
    S_rows = {}
    for w in a.weights_grid:
        for key, items in (("all", list(res)), ("tune", [i for i in res if res[i]["split"] == "tune"]),
                           ("heldout", [i for i in res if res[i]["split"] == "heldout"]),
                           ("delay", [i for i in res if res[i]["category"] == "delay"]),
                           ("reverb", [i for i in res if res[i]["category"] == "reverb"])):
            ok, ok_aud, _ties = [], [], []
            for i in items:
                r = res[i]
                loss = [b + w * d for b, d in zip(r["base"], r["fam"], strict=True)]
                fields = S.rank1_fields([{"loss": loss[0], "null_db": 0.0}] +
                                        [{"loss": l, "null_db": n} for l, n in zip(loss[1:], r["null"][1:], strict=True)])
                ok.append(fields["rank1"])
                ok_aud.append(fields["rank1_audible"])
            if ok:
                S_rows[(w, key)] = (float(np.mean(ok)), float(np.mean(ok_aud)), len(ok))
    print(f"set {a.set}; {len(res)} items scored; weights {wlabel}")
    print("w      " + "  ".join(f"{k:>16s}" for k in ("all", "tune", "heldout", "delay", "reverb")))
    for w in a.weights_grid:
        print(f"{w:<6g} " + "  ".join(
            f"{S_rows[(w, k)][0]:.3f}/{S_rows[(w, k)][1]:.3f} n{S_rows[(w, k)][2]:<3d}" if (w, k) in S_rows else f"{'-':>16s}"
            for k in ("all", "tune", "heldout", "delay", "reverb")))
    (ROOT / "cache" / "time-specialist" / f"trust_{a.set}.json").write_text(json.dumps(res))
    if a.ledger:
        import subprocess
        for w in a.ledger_weights:
            for key in ("all", "heldout"):
                if (w, key) not in S_rows:
                    continue
                for metric, v in (("rank1_rate", S_rows[(w, key)][0]), ("rank1_rate_audible", S_rows[(w, key)][1])):
                    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "loss-trust",
                                    "--engine", "native", "--dataset", f"{a.set}-time-{key}", "--metric", metric,
                                    "--value", f"{v:.4f}",
                                    "--notes", (f"n={S_rows[(w, key)][2]} items; term=time_family weight {w}; mrstft_v2 admitted known-DI weights plus the time family term; "
                                               f"true preset vs 24 neighbours, aligned capture, rule B; T122 bar in DECISIONS 2026-10-17")],
                                   check=False)
    return 0


def cmd_screen(a) -> int:
    """screen_hint and the delay / reverb / mod / other split on the true renders of a set (the screen bar)."""
    import synthetic_v1 as S

    from tonematch.search.time_specialist import TimeSpecialist

    S.set_paths(a.set)
    labels = json.loads((S.SET_DIR / "labels.json").read_text())
    sp = TimeSpecialist()
    rows, conf = [], {}
    for it in labels:
        f = ROOT / "cache" / "time-specialist" / "trust" / a.set / f"{it['id']}.npz"
        if not f.exists():
            continue
        z = np.load(f)
        y = z["renders"][0]
        if not np.any(y):
            continue
        ev = sp.evidence(y, z["dry"])
        p = ev.probs
        call = max(p, key=p.get)
        rows.append({"id": it["id"], "category": it["category"], "model": it["model"], "hint": p["delay"] + p["reverb"],
                     "probs": p, "call": call, "split": split_of(labels, it) if it["category"] in ("delay", "reverb") else "-"})
        conf[(it["category"], call)] = conf.get((it["category"], call), 0) + 1
    pos = [r for r in rows if r["category"] in ("delay", "reverb")]
    neg = [r for r in rows if r["category"] not in ("delay", "reverb")]
    hit = np.mean([r["hint"] > 0.5 for r in pos]) if pos else float("nan")
    rej = np.mean([r["hint"] <= 0.5 for r in neg]) if neg else float("nan")
    print(f"time called time (hint > 0.5): {hit:.3f} of {len(pos)}; not-time called not time: {rej:.3f} of {len(neg)}")
    print("confusion (true category -> class call):")
    for c in ("delay", "reverb", "mod", "amp"):
        print(f"  {c:7s}", {k[1]: v for k, v in sorted(conf.items()) if k[0] == c})
    for r in pos + neg:
        if (r["category"] in ("delay", "reverb")) != (r["hint"] > 0.5):
            print("  miss:", r["id"], r["category"], r["model"][4:], {k: round(v, 2) for k, v in r["probs"].items()})
    (ROOT / "cache" / "time-specialist" / f"screen_{a.set}.json").write_text(json.dumps(rows))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--set", default="synthetic-devfam")
    r.add_argument("--categories", nargs="+", default=["delay", "reverb"])
    r.add_argument("--truth-only", action="store_true", help="render only the true preset (for the screen check on the other categories)")
    s = sub.add_parser("score")
    s.add_argument("--set", default="synthetic-devfam")
    s.add_argument("--weights", default="data/weights/gate2-known-di-mrstft_v2-2.0.json")
    s.add_argument("--weights-grid", nargs="+", type=float, default=[0.0, 0.25, 0.5, 1.0, 2.0])
    s.add_argument("--ledger", action="store_true")
    s.add_argument("--ledger-weights", nargs="+", type=float, default=[0.0, 0.5])
    sc = sub.add_parser("screen")
    sc.add_argument("--set", default="synthetic-devfam")
    a = ap.parse_args(argv)
    return {"render": cmd_render, "score": cmd_score, "screen": cmd_screen}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
