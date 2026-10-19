"""proc_check: orphan and zombie detection on canned ps output. Kills nothing."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import proc_check as P

PS = """\
  100     1 R     95.0 01:03:25 /usr/bin/python -c from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=5)
  101     1 S      0.0 01:04:34 /usr/bin/python -c from multiprocessing.resource_tracker import main;main(4)
  102   500 R     90.0 00:10:00 /usr/bin/python -c from multiprocessing.spawn import spawn_main
  103     1 S     40.0 00:20:00 /usr/bin/python scripts/synthetic_v1.py run --tag x
  104     1 R     99.0 00:30:00 /usr/bin/python -c from multiprocessing.spawn import spawn_main
  105     1 S     60.0 00:05:00 /usr/bin/python -m tonematch.engines.native_host
  106   700 Z      0.0 00:00:01 (python)
  107     1 R     98.0 00:01:00 /usr/bin/python scripts/synthetic_v1.py run --tag t --as-worker --supervisor-pid 99
"""


def test_flags_only_repo_workers_with_a_dead_parent(tmp_path):
    repo = tmp_path / "repo"
    cwds = {100: str(repo), 101: str(repo / "sub"), 102: str(repo), 103: str(repo),
            104: "/somewhere/else", 105: str(repo), 107: str(repo)}
    got = P.classify(P.parse_ps(PS), cwd=cwds.get, root=repo)
    assert [r["pid"] for r in got["orphans"]] == [100, 101, 105, 107]   # 102 has a parent, 103 is a top-level job, 104 another repo
    assert [r["pid"] for r in got["zombies"]] == [106]


def test_parse_keeps_full_command():
    rows = P.parse_ps(PS)
    assert rows[0]["cmd"].endswith("spawn_main(tracker_fd=5)") and rows[0]["pcpu"] == 95.0
