#!/usr/bin/env bash
# T191: start the unattended drive-dataset-v1 render + retrain (scripts/drive_dataset_render.py all) in the
# background: nohup, nice 10, and caffeinate -i (macOS) so the Mac does not idle-sleep while it runs.
# Extra arguments are passed to `all` (e.g. --hours 12, --target 100000).
#
#   scripts/overnight_drive.sh                 start
#   cat cache/drive-dataset-v1/progress.json   progress (updated every ~30 s)
#   tail -f cache/drive-dataset-v1/render.log  readable log
#   kill <PID>                                 stop safely (workers finish their shard, hosts close); rerun resumes
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/cache/drive-dataset-v1"
mkdir -p "$OUT"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
if [ -f "$OUT/job.pid" ] && kill -0 "$(cat "$OUT/job.pid")" 2>/dev/null; then
  echo "already running: PID $(cat "$OUT/job.pid") (stop it with: kill $(cat "$OUT/job.pid"))"
  exit 1
fi
cd "$ROOT"
export TONEMATCH_AGENT="${TONEMATCH_AGENT:-learning}"
export TONEMATCH_AFXREP_DEVICE=cpu
nohup nice -n 10 "$PY" "$ROOT/scripts/drive_dataset_render.py" all "$@" >> "$OUT/run.log" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$OUT/job.pid"
if command -v caffeinate >/dev/null 2>&1; then
  nohup caffeinate -i -w "$PID" >/dev/null 2>&1 < /dev/null &
fi
cat <<EOF
started drive-dataset-v1: PID $PID
  progress:  cat $OUT/progress.json
  log:       tail -f $OUT/render.log      (full output: $OUT/run.log)
  stop:      kill $PID     (finishes the current shard, closes the Native hosts; may take a few minutes)
  resume:    scripts/overnight_drive.sh   (skips finished shards)
  results:   $OUT/summary.md and summary.json; checkpoints in $ROOT/cache/models/predictor-dataset-v1/
  after a stop or crash: .venv/bin/python scripts/proc_check.py   (--kill stops flagged orphans only)
EOF
