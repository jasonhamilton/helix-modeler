"""Per-engine parameter policy (stomp-engine, 2026-10-15).

Some parameters render differently on the HX Stomp (fw 3.50) and Helix Native (3.82); the first is
the cab's Early Reflections. ``param_policy.json`` (next to this file) lists them per engine:

- ``freeze``: a preset bound to that engine holds the parameter at ``value`` (search never moves it).
- ``forbid``: the parameter must not be set at all for that engine.

``frozen_for("stomp")`` gives ``{(category, param): value}``, the form ``scripts/match_song.py`` uses.
``violations(preset, engine)`` lists what a preset carries against the policy; ``apply`` returns a
copy with frozen values written in. Engines report violations; they do not silently rewrite a preset.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

POLICY_PATH = Path(__file__).with_name("param_policy.json")
ACTIONS = ("freeze", "forbid")


@lru_cache(maxsize=4)
def load_policy(path: str | None = None) -> dict:
    data = json.loads(Path(path or POLICY_PATH).read_text(encoding="utf-8"))
    for r in data.get("rules", []):
        for eng, rule in r.get("engines", {}).items():
            if rule.get("action") not in ACTIONS:
                raise ValueError(f"param policy: {r['category']}.{r['param']} on {eng}: bad action {rule}")
            if rule["action"] == "freeze" and "value" not in rule:
                raise ValueError(f"param policy: {r['category']}.{r['param']} on {eng}: freeze needs a value")
    return data


def _rules(engine: str, policy: dict | None = None):
    for r in (policy or load_policy()).get("rules", []):
        rule = r.get("engines", {}).get(engine)
        if rule:
            yield r["category"], r["param"], rule


def frozen_for(engine: str, policy: dict | None = None) -> dict[tuple[str, str], float]:
    return {(c, p): float(rule["value"]) for c, p, rule in _rules(engine, policy) if rule["action"] == "freeze"}


def forbidden_for(engine: str, policy: dict | None = None) -> set[tuple[str, str]]:
    return {(c, p) for c, p, rule in _rules(engine, policy) if rule["action"] == "forbid"}


def violations(preset, engine: str, policy: dict | None = None, tol: float = 1e-6) -> list[str]:
    frozen, forbidden = frozen_for(engine, policy), forbidden_for(engine, policy)
    out = []
    for s in preset.blocks():
        for name, v in s.params.items():
            key = (s.category, name)
            if key in forbidden:
                out.append(f"{s.key or s.model_id}.{name}: forbidden on {engine}")
            elif key in frozen and abs(float(v) - frozen[key]) > tol:
                out.append(f"{s.key or s.model_id}.{name} = {v}: frozen at {frozen[key]} on {engine}")
    return out


def apply(preset, engine: str, policy: dict | None = None):
    """A copy of the preset with frozen values written in and forbidden parameters removed."""
    frozen, forbidden = frozen_for(engine, policy), forbidden_for(engine, policy)
    out = copy.deepcopy(preset)
    for s in out.blocks():
        for name in list(s.params):
            key = (s.category, name)
            if key in forbidden:
                del s.params[name]
            elif key in frozen:
                s.params[name] = frozen[key]
    return out
