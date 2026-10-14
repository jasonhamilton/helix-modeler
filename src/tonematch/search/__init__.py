"""Search: CMA-ES over a fixed chain, successive halving, chain grouping. See docs/notes/search-design.md."""
from tonematch.search.loop import Evaluator, match, plan_budget, refine_chain
from tonematch.search.report import write_report
from tonematch.search.space import ParamSpace, Prior, fit_prior
from tonematch.search.types import Budget, Plan, RankedPreset, Section

__all__ = ["Budget", "Evaluator", "ParamSpace", "Plan", "Prior", "RankedPreset", "Section", "fit_prior",
           "match", "plan_budget", "refine_chain", "write_report"]
