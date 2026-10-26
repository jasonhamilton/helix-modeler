#!/usr/bin/env bash
# T213: launch the slot-dataset-v1 overnight supervisor (scripts/slot_overnight.py) in the background: nohup,
# nice 10 (renders run at 10, training at 15) and caffeinate -i (macOS). Arguments go to slot_overnight.py.
#
#   scripts/slot_overnight.sh --wait-pid 78416 --deadline "2026-10-23 09:00"
#   cat cache/slot-dataset-v1/overnight/status.txt       one status line (render lane, training lane, shards)
#   tail -f cache/slot-dataset-v1/overnight/overnight.log
#   kill <PID>                                           stop safely (render finishes its shard; training stops)
# TONEMATCH_SLOT_BASE / TONEMATCH_SLOT_MODELS move the whole tree (smoke runs in a scratch directory).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${TONEMATCH_SLOT_BASE:-$ROOT/cache/slot-dataset-v1}"
DIR="$BASE/overnight"
mkdir -p "$DIR"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
if [ -f "$DIR/overnight.pid" ] && kill -0 "$(cat "$DIR/overnight.pid")" 2>/dev/null; then
  echo "the overnight supervisor is already running: PID $(cat "$DIR/overnight.pid"). Stop it with: kill $(cat "$DIR/overnight.pid")"
  exit 1
fi
cd "$ROOT"
export TONEMATCH_AGENT="${TONEMATCH_AGENT:-learning}"
export TONEMATCH_AFXREP_DEVICE=cpu
nohup nice -n 10 "$PY" "$ROOT/scripts/slot_overnight.py" "$@" > "$DIR/supervisor.out" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$DIR/overnight.pid"
if command -v caffeinate >/dev/null 2>&1; then
  nohup caffeinate -i -w "$PID" >/dev/null 2>&1 < /dev/null &
fi
cat <<MSG
started the slot-dataset-v1 overnight supervisor: PID $PID
  status:  cat $DIR/status.txt
  log:     tail -f $DIR/overnight.log      (stdout/stderr: $DIR/supervisor.out)
  slots:   $PY scripts/slot_dataset_render.py status
  stop:    kill $PID     (the render finishes its current shard and closes its hosts; training is stopped)
MSG
