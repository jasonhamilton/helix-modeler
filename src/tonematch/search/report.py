"""Per-run report: reports/<song>/<section>.md."""
from __future__ import annotations

from pathlib import Path

from tonematch.search.types import Budget, Plan, RankedPreset, Section


def write_report(section: Section, ranked: list[RankedPreset], plan: Plan, budget: Budget,
                 root: str | Path = "reports") -> Path:
    path = Path(root) / section.song / f"{section.name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = (f"Engine: {plan.engine}; budget: {plan.max_renders} full-render equivalents "
         f"(requested {budget.max_renders}), {plan.seconds_per_render:.3g} s/render, parallelism {plan.parallelism}, "
         f"successive halving {'on' if plan.halving else 'off'}; est. wall {plan.est_wall_s:.0f} s.")
    L = [f"# {section.song} / {section.name}", "", head, ""]
    L += [f"Note: {n}" for n in plan.notes]
    for i, r in enumerate(ranked, 1):
        L += ["", f"## Rank {i}: score {r.score:.4f}", "",
              f"- chain: {' > '.join(str(m) for _, m in r.chain)}",
              f"- engine: {r.engine}; renders used {r.renders}; wall {r.wall_s:.1f} s; final check: {r.final_checked}",
              f"- loss: {r.loss_label}; clip length {r.clip_s:.1f} s per score",
              "- terms: " + (", ".join(f"{k}={v:.3f}" for k, v in r.terms.items()) or "none"),
              "- key settings:"]
        L += [f"  - slot {s} {k}: {v:.3f}" for (s, k), v in r.values.items()]
        L += [f"- stage {g['stage']}: {g['generations']} generations, {g['renders']} renders, "
              f"{g['score_in']:.3f} -> {g['score_out']:.3f}" for g in r.stages]
        L += [f"- {n}" for n in r.notes]
    path.write_text("\n".join(L) + "\n")
    return path
