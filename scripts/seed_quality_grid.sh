#!/bin/bash
# T162 driver: runs the registered grid (docs/DECISIONS.md 2026-10-18 search T162). Resumable: a cell with a summary.json is skipped.
# usage: scripts/seed_quality_grid.sh a|b
cd "$(dirname "$0")/.." || exit 1
COND=$1
PY=.venv/bin/python
COMMON="--set synthetic-devfam --weights data/weights/gate2-known-di-v3.json --items ${ITEMS:-32} --workers auto --priority normal"
HY="--hybrid --specialists all --hybrid-f 0.4 --hybrid-k 4 --hybrid-scale 0.9 --hybrid-level-free --family-prior 0.04 --prior-gate 0.9 --keep-top 2 --shortlist 2 --shortlist-hint 0.5 --no-rank1"
EXTRA=""; [ "$COND" = b ] && EXTRA="--di-mismatch"
RUNS=cache/synthetic-devfam/runs
# args: tag-suffix budget-or-rw ...  (see calls)
c() { tag=$1; b=$2; sc=$3; t="t162${COND}_${tag}"
  { [ -f $RUNS/$t/summary.json ] || [ -f $RUNS/$t/summary_shardall.json ]; } && { echo "skip $t"; return; }
  echo "$(date +%T) start $t"
  if [ "$sc" = rw ]; then $PY scripts/synthetic_v1.py run $COMMON $EXTRA --baseline random --budget $b --tag $t > $RUNS/../t162_$t.log 2>&1
  elif [ -n "$sc" ]; then $PY scripts/synthetic_v1.py run $COMMON $HY $EXTRA --budget $b --seed-corrupt $sc --tag $t > $RUNS/../t162_$t.log 2>&1
  else $PY scripts/synthetic_v1.py run $COMMON $HY $EXTRA --budget $b --tag $t > $RUNS/../t162_$t.log 2>&1; fi
  echo "$(date +%T) done $t"; $PY scripts/proc_check.py
}
if [ -n "$PILOT" ]; then   # human cut (2026-10-18): blind only (--di-mismatch), 12 items, budget 150
  COND=b; EXTRA="--di-mismatch"; c p0.5_s0.1_b150 150 0.5,0.1; c unseeded_b150 150 ""; c p1.0_s0.1_b150 150 1.0,0.1; c rw_b150 150 rw
elif [ "$COND" = a ]; then
  for B in 150 50 300; do c unseeded_b$B $B ""; c rw_b$B $B rw; done
  for B in 150 50 300; do
    for P in 1.0 0.5 0.25 0.75; do c p${P}_s0.1_b$B $B $P,0.1; done
  done
  for B in 150 50 300; do for S in 0.0 0.25; do c p1.0_s${S}_b$B $B 1.0,$S; done; done
  for S in 0.0 0.25; do c p0.5_s${S}_b150 150 0.5,$S; done
else
  for B in 150 50 300; do c unseeded_b$B $B ""; c rw_b$B $B rw; done
  for B in 150 50 300; do for P in 1.0 0.5 0.25; do c p${P}_s0.1_b$B $B $P,0.1; done; done
  for S in 0.0 0.25; do c p1.0_s${S}_b150 150 1.0,$S; done
fi
