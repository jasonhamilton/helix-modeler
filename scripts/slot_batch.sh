#!/usr/bin/env bash
# T208: one unattended render batch of slot-dataset-v1 for ONE slot, in the background: nohup, nice 10 and
# caffeinate -i (macOS). The batch resumes the slot's plan, skips finished shards and never overwrites one; it stops
# after HOURS (or on low disk, or on `kill <PID>`), then proc_check reports orphans into the batch log. It never trains (train separately).
#
#   scripts/slot_batch.sh <slot> <hours> [extra render args, e.g. --hosts 3]
#   scripts/slot_batch.sh amp-refill <hours>          the refill plan of amp's failed clips (T210)
#   cat cache/slot-dataset-v1/<slot>/progress.json    progress, this run's rate and ETA (updated every ~30 s)
#   tail -f cache/slot-dataset-v1/<slot>/render.log   one line per shard, pauses, stops
#   kill <PID>                                         stop safely (current shard finishes, hosts close)
set -euo pipefail
SLOT="${1:?usage: scripts/slot_batch.sh <amp|cab|mod|delay|reverb|eq> <hours> [render args]}"
HOURS="${2:?usage: scripts/slot_batch.sh <slot> <hours> [render args]}"
shift 2
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/cache/slot-dataset-v1/$SLOT"
BASESLOT="${SLOT%-refill}"            # T210: "amp-refill" renders the refill plan of amp's failed clips
REFILL=""
[ "$BASESLOT" != "$SLOT" ] && REFILL="--refill"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
if [ ! -f "$OUT/plan.json" ]; then
  echo "no plan for $SLOT: run first   $PY scripts/slot_dataset_render.py $([ -n "$REFILL" ] && echo "refill --slot $BASESLOT" || echo "plan --slot $SLOT")"
  exit 2
fi
for f in "$ROOT"/cache/slot-dataset-v1/*/job.pid "$ROOT/cache/drive-dataset-v1/job.pid"; do
  if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then
    echo "a batch is already running: PID $(cat "$f") ($f). One batch at a time; stop it with: kill $(cat "$f")"
    exit 1
  fi
done
cd "$ROOT"
export TONEMATCH_AGENT="${TONEMATCH_AGENT:-learning}"
export TONEMATCH_AFXREP_DEVICE=cpu
LOG="$OUT/batch-$(date +%m%d-%H%M).log"
# the PID is the render supervisor itself (nice execs it), so `kill <PID>` reaches it directly
nohup nice -n 10 "$PY" "$ROOT/scripts/slot_dataset_render.py" render --slot "$BASESLOT" $REFILL --hours "$HOURS" ${@+"$@"} \
  > "$LOG" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$OUT/job.pid"
# after the batch ends (or is killed): proc_check (report only) appended to the batch log
nohup bash -c "while kill -0 $PID 2>/dev/null; do sleep 20; done; \"$PY\" \"$ROOT/scripts/proc_check.py\"" \
  >> "$LOG" 2>&1 < /dev/null &
if command -v caffeinate >/dev/null 2>&1; then
  nohup caffeinate -i -w "$PID" >/dev/null 2>&1 < /dev/null &
fi
cat <<EOF
started slot-dataset-v1 $SLOT batch ($HOURS h): PID $PID
  progress:  cat $OUT/progress.json
  log:       tail -f $OUT/render.log      (full output: $LOG)
  all slots: $PY scripts/slot_dataset_render.py status
  stop:      kill $PID     (finishes the current shard, closes the Native hosts; may take a few minutes)
  next:      scripts/slot_batch.sh $SLOT <hours>   (resumes; skips finished shards)
  train:     $PY scripts/slot_dataset_render.py train --slot $SLOT
EOF
