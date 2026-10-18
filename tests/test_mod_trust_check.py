"""mod-trust-v1 summary logic (T116) on fake scored items: rule B ties, distractor kinds, the registered bars."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mod_trust_check as MT

from tonematch.metrics import trust as TR


def _item(i, subset, scores_by_term, nulls, labels):
    out = {"item": i, "subset": subset, "true_model": "M", "labels": labels, "nulls": nulls}
    for cond in ("aligned", "phase"):
        res = {}
        for t, s in scores_by_term[cond].items():
            r = TR.rank1_rule_b(s[0], s[1:], nulls)
            kinds = {k.rstrip(":"): all(s[1 + j] > s[0] or nulls[j] <= -30 for j, lb in enumerate(labels[1:]) if lb.startswith(k))
                     for k in ("near", "mod:", "confuser:")}
            res[t] = {**r.as_dict(), "by_kind": kinds, "best_distractor": labels[1], "scores": s}
        out[cond] = res
    out["measures"] = {"is_mod": True, "mod_score": 0.9, "subtype_rule": "tremolo", "subtype_vote": "tremolo",
                       "subtype_true": "tremolo", "model_rank": 1, "model_top": ["M"], "knob_err": {"rate": [0.05]},
                       "hz_rate": {"true": 4.0, "est": 4.1}, "rate_hz": 4.1, "am_depth_db": 5.0, "pm_depth_cents": 0.3,
                       "wet_mix": 0.2, "confusers": [{"category": "eq", "mod_score": 0.1, "is_mod": False}]}
    return out


def test_summarise_rule_b_and_kinds():
    labels = ["true", "near", "mod:X", "confuser:eq"]
    good = {t: [0.1, 0.5, 0.6, 0.7] for t in MT.TERMS}
    bad = {t: [0.5, 0.1, 0.6, 0.7] for t in MT.TERMS}
    # item 0: truth first in both conditions; item 1: a near miss beats it in phase but it nulls at -40 dB (a tie)
    it0 = _item(0, "core", {"aligned": good, "phase": good}, [-10.0, -10.0, -10.0], labels)
    it1 = _item(1, "core", {"aligned": good, "phase": bad}, [-40.0, -10.0, -10.0], labels)
    it2 = _item(2, "core", {"aligned": good, "phase": bad}, [-10.0, -10.0, -10.0], labels)
    s = MT.summarise([it0, it1, it2])
    v = s["core/phase/mod_distance"]
    assert v["items"] == 3
    assert v["rank1_audible_only"] == 2 / 3 and v["rank1_rate"] == 1 / 3
    assert v["by_kind"]["near"] == 2 / 3 and v["by_kind"]["confuser"] == 1.0
    assert [f[0] for f in v["fails"]] == [2]
    assert s["core/aligned/mrstft_v2"]["rank1_audible_only"] == 1.0
    m = s["core/measures"]
    assert m["subtype_acc_vote"] == 1.0 and m["confuser_false_pos"] == 0.0 and m["hz_rate_within_5pct"] == 1.0
    assert "extended/phase/mod_distance" not in s


def test_registered_bars():
    assert MT.BAR == {"subset": "core/phase", "metric": "rank1_rate_audible", "min": 0.90}
    assert MT.FAMILY_BAR["min"] == 0.90 and MT.FAMILY_BAR["aligned_max_drop"] == 0.02 and MT.FAMILY_W == 0.2
    assert MT.TERMS == ("mod_distance", "mrstft_v2", "mod_family")
