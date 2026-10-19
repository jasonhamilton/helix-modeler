"""docs/TOOLS.md stays in step with scripts/: every script is documented once, the counts add up, and the
flags used in its example commands exist in the script's source. Reads files only; runs no tool."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
SCRIPTS = sorted(p.stem for p in (ROOT / "scripts").glob("*.py"))
NAME = re.compile(r"`([a-z0-9_]+)\.py`")


def _section(title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", DOC, re.DOTALL | re.MULTILINE)
    assert m, title
    return m.group(1)


def _classes():
    internal = {m.group(1) for m in re.finditer(r"^\| `([a-z0-9_]+)\.py` \|", _section("Internal helpers"), re.MULTILINE)}
    spikes = {m.group(1) for m in re.finditer(r"^\| `([a-z0-9_]+)\.py` \|", _section("Historical spikes"), re.MULTILINE)}
    user = set()
    for line in DOC.splitlines():
        if line.startswith("#"):
            user.update(NAME.findall(line))
    lib = _section("10. Library preparation")
    user.update(m.group(1) for m in re.finditer(r"^\| `([a-z0-9_]+)\.py` \|", lib, re.MULTILINE))
    user -= internal | spikes
    return user, internal, spikes


def test_every_script_is_in_exactly_one_class():
    user, internal, spikes = _classes()
    assert not (user & internal or user & spikes or internal & spikes)
    assert set(SCRIPTS) == user | internal | spikes, sorted(set(SCRIPTS) ^ (user | internal | spikes))
    assert "seed_quality_grid.sh" in DOC


def test_class_counts_in_the_header_match():
    user, internal, spikes = _classes()
    for label, n in (("user-runnable", len(user)), ("internal", len(internal)), ("historical spike", len(spikes))):
        assert re.search(rf"\| {label} \| {n} \|", DOC), (label, n)
    assert len(SCRIPTS) == len(user) + len(internal) + len(spikes)
    assert f"{len(SCRIPTS)} Python files" in DOC


def test_example_flags_exist_in_the_script():
    bad = []
    for block in re.findall(r"```bash\n(.*?)```", DOC, re.DOTALL):
        text = block.replace("\\\n", " ")
        for cmd in text.splitlines():
            m = re.search(r"scripts/([a-z0-9_]+)\.py", cmd)
            if not m or m.group(1) not in SCRIPTS:
                continue
            src = (ROOT / "scripts" / f"{m.group(1)}.py").read_text(encoding="utf-8")
            for flag in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", cmd[m.end():]):
                if flag in ("--help",):
                    continue
                if flag == "--result-json" and "cli_result.add_result_arg" in src:
                    continue
                if f'"{flag}"' not in src and f"'{flag}'" not in src:
                    bad.append((m.group(1), flag))
    assert not bad, bad


def test_result_json_claims_match_the_code():
    wired = DOC.split("Wired tools:")[1].split(".\n")[0]
    for name in ("separate", "match_song", "match_block", "synthetic_v1", "loss_trust_check", "engine_smoke",
                 "playalong_record", "align_takes", "hlx_validate", "licence_check", "proc_check",
                 "no_di_invariance", "gate2_report"):
        assert f"`{name}" in wired, name
        assert "cli_result" in (ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8"), name
