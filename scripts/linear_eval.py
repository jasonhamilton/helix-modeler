#!/usr/bin/env python3
"""Linear specialist evaluation (T117, trust bar `linear-v1` in docs/DECISIONS.md).

  run        Native, ONE host: per item of a synthetic set, render the true preset on DI A, run the
             screen (`screen_hint`) on every item, and on cab and EQ items the specialist (table fit +
             probe-render refinement, candidates = the pool's cab and EQ models), the held-out loss on
             DI C (admitted known-DI weights, as the Gate 2 harness) and the family-score trust check
             (rank-1 of the true preset among the item's neighbours, same notes, rule B);
             per-item JSON in cache/<set>/runs/<tag>/
  summarize  no Native: the specialist's summary, and with --hybrid-tag the paired comparison against
             a `synthetic_v1.py run --hybrid` directory of the same set; --ledger writes kind=search rows
             (and kind=loss-trust for the family score)

The probe is synthetic (no DI clip); targets are the set's own DI clips (Guitar-TECHS, no song audio).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

LINEAR = ("cab", "eq")
WEIGHTS = ROOT / "data" / "weights" / "gate2-known-di-mrstft_v2-2.0.json"
HELDOUT_FROM = 48          # linear-v1 split: i000-i047 tuning, i048-i095 held out


def half_of(item_id: str) -> str:
    return "tune" if int(item_id[1:]) < HELDOUT_FROM else "heldout"


def cmd_run(a) -> int:
    import soundfile as sf
    import synthetic_v1 as S

    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL
    from tonematch.metrics import LossKit
    from tonematch.metrics import linear_specialist as L
    from tonematch.metrics.trust import null_db, rank1_rule_b

    S.set_paths(a.set)
    man = json.loads((S.SET_DIR / "manifest.json").read_text())
    labels = json.loads((S.SET_DIR / "labels.json").read_text())
    items = [it for it in labels if (a.ids is None or it["id"] in a.ids)
             and (a.half == "all" or half_of(it["id"]) == a.half)
             and (not a.linear_only or it["category"] in LINEAR)]
    cat = load_catalog()
    pool = {m: S.Block(m, c, S.free_params(m, NL.template_for(m), cat, c), cat)
            for c in man["categories"] for m in man["pool"][c]}
    lin_models = [m for m, b in pool.items() if b.category in LINEAR]
    sp = L.LinearSpecialist(models=lin_models)
    missing = [m for m in lin_models if m not in sp.tables]
    if missing:
        print(f"no response table for {missing}: run scripts/linear_tables.py", file=sys.stderr)
        return 2
    weights, _admitted, wlabel = S.load_weights(str(WEIGHTS))
    kit = LossKit()
    running = S.machine_hosts()
    if running + 1 > S.MAX_MACHINE_HOSTS:
        print(f"refusing: {running} native_host processes running", file=sys.stderr)
        return 3
    run_dir = S.SET_DIR / "runs" / a.tag
    run_dir.mkdir(parents=True, exist_ok=True)
    probe = L.probe_signal()
    engine = S.make_engine()
    clip = int(man["clip_seconds"] * S.SR)
    print(f"{len(items)} items, linear candidates {len(lin_models)}, budget {a.budget}, weights {wlabel}", flush=True)
    try:
        for it in items:
            f = run_dir / f"{it['id']}.json"
            if f.exists() and not a.redo:
                continue
            t0 = time.time()
            r0 = engine.renders
            dry = {t: sf.read(str(ROOT / it["di"][t]["file"]), dtype="float32")[0][:clip] for t in ("A", "C")}
            tb = pool[it["model"]]

            def render(b, values, x):
                engine.load(b.with_values(values))
                return np.asarray(engine.render(x), np.float32)
            try:
                target = render(tb, it["values"], dry["A"])
            except Exception as e:  # noqa: BLE001 - recorded like the Gate 2 harness, counted as a miss
                res = {"id": it["id"], "half": half_of(it["id"]), "category": it["category"], "model": it["model"],
                       "error": f"{type(e).__name__}: {e}", "renders": engine.renders - r0}
                f.write_text(json.dumps(res, indent=1))
                print(f"{it['id']} {it['category']:6s} target render failed: {res['error']}", flush=True)
                continue
            h = sp.screen_hint(target, dry["A"], models=lin_models)
            res = {"id": it["id"], "half": half_of(it["id"]), "category": it["category"], "model": it["model"],
                   "incoherent_db": h.incoherent_db, "family_prob": h.family_prob, "screen_linear": h.family_prob > 0.5,
                   "hint_category_probs": h.category_probs, "hint_shortlist": h.shortlist, "hint_fit_db": h.fit_db}
            if it["category"] in LINEAR:
                ts = time.time()
                n_probe = 0

                def render_cand(model, values, dry=dry):
                    nonlocal n_probe
                    n_probe += 1
                    return render(pool[model], values, probe if a.on == "probe" else dry["A"])
                fits, used = sp.refine(render_cand, target, dry["A"], models=lin_models, budget=a.budget,
                                       top=a.top, corrections=a.corrections, on=a.on)
                best = fits[0]
                pb = pool[best.model]
                res.update({"pred_model": best.model, "pred_category": pb.category, "pred_values": best.values,
                            "pred_knobs": best.knobs, "fit_db": best.fit_db, "rendered": best.rendered,
                            "model_ok": best.model == it["model"], "category_ok": pb.category == it["category"],
                            "search_renders": used, "search_s": round(time.time() - ts, 2),
                            "ranked": [[x.model, round(x.fit_db, 4), x.rendered] for x in fits[:6]]})
                if res["model_ok"]:
                    tn, pn = tb.norm(it["values"]), tb.norm(best.values)
                    res["param_abs_err"] = {n: abs(tn[n] - pn[n]) for n in tn}
                    res["param_mae_item"] = float(np.mean(list(res["param_abs_err"].values())))
                tc = render(tb, it["values"], dry["C"])
                fc = render(pb, best.values, dry["C"])
                res["loss_heldout"] = float(kit.score(tc, fc, weights=weights).total)
                res["response_heldout_db"] = float(sp.score(tc, fc, dry["C"]))
                if not a.no_rank1:
                    from loss_trust_check import aligned_capture

                    cap = aligned_capture(target, np.random.default_rng([S.SEED, 98, int(it["id"][1:])]))
                    t_score = sp.score(cap, target, dry["A"], gain_free=True)
                    nb_s, nb_n = [], []
                    for nb in it["neighbours"]:
                        b = pool[nb["model"]]
                        y = render(b, b.from_knobs(nb["knobs"]), dry["A"])
                        nb_s.append(sp.score(cap, y, dry["A"], gain_free=True))
                        nb_n.append(float(null_db(target, y)))
                    rk = rank1_rule_b(t_score, nb_s, nb_n, S.SAME_SOUND_DB)
                    res.update({"rank1": rk.rank1, "rank1_audible": rk.rank1_audible, "n_same_sound": rk.n_same_sound,
                                "rank1_margin_audible": rk.margin_audible, "true_score": t_score})
            res["renders"] = engine.renders - r0
            res["wall_s"] = round(time.time() - t0, 1)
            f.write_text(json.dumps(res, indent=1))
            print(f"{it['id']} {it['category']:6s} {it['model'][4:]:28s} inc {h.incoherent_db:6.1f} dB p {h.family_prob:.2f}"
                  + (f" -> {res['pred_model'][4:]:28s} {'ok' if res['model_ok'] else 'NO'} "
                     f"mae {res.get('param_mae_item', float('nan')):.3f} fit {res['fit_db']:.3f} dB "
                     f"{res['search_renders']} renders rank1 {res.get('rank1_audible', '-')}" if "pred_model" in res else ""),
                  flush=True)
    finally:
        engine.close()
    return 0


# --------------------------------------------------------------------------- summary
def _load(d: Path) -> dict[str, dict]:
    return {json.loads(p.read_text())["id"]: json.loads(p.read_text()) for p in sorted(d.glob("i*.json"))}


def boot_diff(a: np.ndarray, b: np.ndarray, n: int = 4000, seed: int = 0) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    d = np.asarray(a, float) - np.asarray(b, float)
    if d.size == 0:
        return float("nan"), float("nan"), float("nan")
    bs = [np.mean(d[rng.integers(0, d.size, d.size)]) for _ in range(n)]
    return float(np.mean(d)), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def summarize(spec: dict[str, dict], hyb: dict[str, dict] | None, ids: list[str]) -> dict:
    out: dict = {"n": len(ids)}
    rs = [spec[i] for i in ids if i in spec]
    out["model_acc"] = float(np.mean([r["model_ok"] for r in rs])) if rs else float("nan")
    out["block_f1"] = float(np.mean([r["category_ok"] for r in rs])) if rs else float("nan")
    out["category_acc_with_screen"] = float(np.mean([r["category_ok"] and r["screen_linear"] for r in rs])) if rs else float("nan")
    out["param_mae"] = float(np.mean([r["param_mae_item"] for r in rs if r.get("model_ok")])) if any(r.get("model_ok") for r in rs) else float("nan")
    out["search_renders_mean"] = float(np.mean([r["search_renders"] for r in rs])) if rs else float("nan")
    out["loss_heldout_median"] = float(np.median([r["loss_heldout"] for r in rs])) if rs else float("nan")
    out["response_heldout_db_median"] = float(np.median([r["response_heldout_db"] for r in rs])) if rs else float("nan")
    if rs and all("rank1_audible" in r for r in rs):
        out["rank1_rate_audible"] = float(np.mean([r["rank1_audible"] for r in rs]))
        out["rank1_rate"] = float(np.mean([r["rank1"] for r in rs]))
    out["by_category"] = {c: {"n": sum(r["category"] == c for r in rs),
                              "model_ok": sum(r["model_ok"] for r in rs if r["category"] == c),
                              "category_ok": sum(r["category_ok"] for r in rs if r["category"] == c)} for c in LINEAR}
    if hyb is not None:
        hs = [hyb[i] for i in ids if i in hyb]
        both = [i for i in ids if i in hyb and i in spec]
        out["hybrid_n"] = len(hs)
        out["hybrid_model_acc"] = float(np.mean([h["model_ok"] for h in hs])) if hs else float("nan")
        out["hybrid_block_f1"] = float(np.mean([h["category_ok"] for h in hs])) if hs else float("nan")
        hm = [h["param_mae_item"] for h in hs if h.get("model_ok")]
        out["hybrid_param_mae"] = float(np.mean(hm)) if hm else float("nan")
        out["hybrid_search_renders_mean"] = float(np.mean([sum((h.get("stage_renders") or {}).values()) or h["renders"]
                                                           for h in hs])) if hs else float("nan")
        out["hybrid_loss_heldout_median"] = float(np.median([h["loss_heldout"] for h in hs if "loss_heldout" in h])) if hs else float("nan")
        out["paired_model_diff"] = boot_diff([spec[i]["model_ok"] for i in both], [hyb[i]["model_ok"] for i in both])
        bothok = [i for i in both if spec[i]["model_ok"] and hyb[i]["model_ok"]]
        out["both_right_n"] = len(bothok)
        out["paired_mae_diff"] = boot_diff([spec[i]["param_mae_item"] for i in bothok],
                                           [hyb[i]["param_mae_item"] for i in bothok])
        out["model_wins_losses"] = [sum(spec[i]["model_ok"] and not hyb[i]["model_ok"] for i in both),
                                    sum(hyb[i]["model_ok"] and not spec[i]["model_ok"] for i in both)]
    return out


def screen_summary(spec: dict[str, dict], ids: list[str]) -> dict:
    rs = [spec[i] for i in ids if i in spec and "error" not in spec[i]]
    lin = [r for r in rs if r["category"] in LINEAR]
    oth = [r for r in rs if r["category"] not in LINEAR]
    by = {}
    for r in oth:
        by.setdefault(r["category"], []).append(r["incoherent_db"])
    return {"linear_called_linear": float(np.mean([r["screen_linear"] for r in lin])) if lin else float("nan"),
            "other_called_not_linear": float(np.mean([not r["screen_linear"] for r in oth])) if oth else float("nan"),
            "n_linear": len(lin), "n_other": len(oth),
            "linear_incoherent_db_max": float(max(r["incoherent_db"] for r in lin)) if lin else float("nan"),
            "other_incoherent_db_min_by_category": {c: round(float(min(v)), 1) for c, v in sorted(by.items())}}


def ledger(kind: str, dataset: str, metric: str, value: float, renders=None, notes: str = "") -> None:
    if value is None or not np.isfinite(value):
        return
    env = {**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "tone-metrics")}
    cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", kind, "--engine", "native",
           "--dataset", dataset, "--metric", metric, "--value", f"{value:.6g}", "--notes", notes]
    if renders is not None:
        cmd += ["--renders", str(int(renders))]
    subprocess.run(cmd, check=True, env=env, cwd=ROOT)


def cmd_summarize(a) -> int:
    d = ROOT / "cache" / a.set / "runs"
    spec = _load(d / a.tag)
    hyb = _load(d / a.hybrid_tag) if a.hybrid_tag else None
    lin_ids = sorted(i for i, r in spec.items() if r["category"] in LINEAR)
    report = {"all24": summarize(spec, hyb, lin_ids),
              "tune": summarize(spec, hyb, [i for i in lin_ids if half_of(i) == "tune"]),
              "heldout": summarize(spec, hyb, [i for i in lin_ids if half_of(i) == "heldout"]),
              "screen_all": screen_summary(spec, sorted(spec)),
              "screen_tune": screen_summary(spec, sorted(i for i in spec if half_of(i) == "tune")),
              "screen_heldout": screen_summary(spec, sorted(i for i in spec if half_of(i) == "heldout"))}
    (d / a.tag / "summary.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    if a.ledger:
        for key, ds in (("all24", f"{a.set}-cabeq24"), ("heldout", f"{a.set}-cabeq-heldout12")):
            s = report[key]
            note = (f"dev not gate evidence; linear specialist T117 (table fit + refine renders on the target DI A; candidates = pool cab+eq; "
                    f"dispatch to linear assumed) vs general hybrid ({a.hybrid_tag}; f0.4 K4 scale0.9 level-free; budget 300); "
                    f"{s['n']} items; specialist search renders mean {s['search_renders_mean']:.1f}; "
                    f"hybrid model {s.get('hybrid_model_acc', float('nan')):.3f} cat {s.get('hybrid_block_f1', float('nan')):.3f} "
                    f"mae {s.get('hybrid_param_mae', float('nan')):.3f}; registered bar linear-v1 DECISIONS 2026-10-17"
                    + (f"; {a.note}" if a.note else ""))
            r = s["search_renders_mean"] * s["n"]
            for m in ("model_acc", "block_f1", "param_mae", "search_renders_mean", "loss_heldout_median"):
                ledger("search", ds, f"{m}_linear_specialist", s.get(m), r, note)
            if hyb is not None:
                for m in ("model_acc", "block_f1", "param_mae", "search_renders_mean"):
                    ledger("search", ds, f"{m}_hybrid", s.get(f"hybrid_{m}"), None, note)
            if key == "all24" and "rank1_rate_audible" in s:
                ledger("loss-trust", ds, "rank1_rate", s["rank1_rate_audible"], None,
                       "term=linear_response (LinearSpecialist.score; gain-free) rule B same notes aligned capture; "
                       f"{s['n']} cab+eq items x 24 neighbours; bar 0.95 (linear-v1); plain rank1 {s['rank1_rate']:.3f}")
        sc = report["screen_all"]
        ledger("search", f"{a.set}", "linear_screen_recall", sc["linear_called_linear"], None,
               f"screen_hint family_prob>0.5 on {sc['n_linear']} cab+eq items; bar 0.95 (linear-v1)")
        ledger("search", f"{a.set}", "linear_screen_specificity", sc["other_called_not_linear"], None,
               f"screen_hint family_prob<=0.5 on {sc['n_other']} non-linear items; bar 0.90 (linear-v1)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--ids", nargs="+", default=None)
    r.add_argument("--half", choices=("tune", "heldout", "all"), default="tune")
    r.add_argument("--linear-only", action="store_true", help="skip the screen-only (non cab/eq) items")
    r.add_argument("--budget", type=int, default=8, help="candidate renders for refine")
    r.add_argument("--on", choices=("dry", "probe"), default="dry", help="refine renders the target's DI or the probe")
    r.add_argument("--top", type=int, default=4)
    r.add_argument("--corrections", type=int, default=2)
    r.add_argument("--no-rank1", action="store_true")
    r.add_argument("--redo", action="store_true")
    s = sub.add_parser("summarize")
    s.add_argument("--hybrid-tag", default=None)
    s.add_argument("--ledger", action="store_true")
    s.add_argument("--note", default="")
    for p_ in (r, s):
        p_.add_argument("--set", default="synthetic-lindev")
        p_.add_argument("--tag", default="linear_v1")
    a = ap.parse_args(argv)
    return cmd_run(a) if a.cmd == "run" else cmd_summarize(a)


if __name__ == "__main__":
    raise SystemExit(main())
