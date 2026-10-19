# drive-dataset-v1: overnight drive dataset render and predictor retrain ([T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render"))

Owner: learning. Status: built and shaken down on 2026-10-19; the real run is started by the human. This is a measurement only. Nothing is adopted, and the checkpoints stay local in `cache/`.

## What it does

1. **Plan** (seed 191, already written to `cache/drive-dataset-v1/plan.json`).
   - 60,000 clips (the default `--target` cap). At 4 hosts and 0.85 s per clip, 18 hours would fit about 305,000.
   - 54 drive models: `match_block.category_pool('drive')` minus the six fuzzes flagged in [T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…").
   - Each clip draws every free knob uniformly on the CC grid. Level-type knobs (Output, Volume, *Level) stay at their default.
   - Each clip is a random 10 s excerpt that passes an activity check: at least 60% active 100 ms frames and at least 3 onsets.
   - The excerpts come from all 92 Guitar-TECHS P1/P2 DI files. P3 is never read. No human takes and no song audio are used.
2. **Split by whole DI file.**

   | Split | Files | Clips | Use |
   |---|---|---|---|
   | train | 40 | 42,200 | training (the T186/T189 train files plus files [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") did not use) |
   | cal | 5 | 2,554 | calibration pool (the T186/T189 calibration files) |
   | test | 18 (9 P1, 9 P2) | 7,643 | new held-out recordings; T186/T189 never used these files |
   | v1test | 29 | 7,603 | new random renders on the T186/T189 test files; evaluation only, never trained on |

3. **Render** on Helix Native through `match_block.make_engine` / `make_blocks`, the same path as T181/T185.
   - An elastic supervisor uses the host scaler, with at most 4 hosts.
   - Before each clip, the worker pauses while the 1-minute load is above 9.
   - Before each 100-clip shard, the worker checks free disk and stops cleanly below 8.5 GB.
   - It also stops after `--hours` (default 18).
   - A Native error or silent output is retried once, then the clip is skipped and logged in the shard JSON.
   - Features are computed straight away and the audio is dropped. They are frozen AFx-Rep `mid` embeddings on CPU with 2 threads: the 6 s centre crop and three 3.33 s crops (the [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1") format), stored as float16.
4. **Train** the [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1") heads (hierarchical logistic regression with ridge knobs, and the 2-layer MLP), multi-crop.
   - Two data arms: `v1data` (the [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") train set only, re-fitted as the baseline) and `v2data` ([T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") train plus the new train renders).
   - Calibration follows [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1"): out-of-fold predictions over the train files plus the calibration files.
   - Within player (`main`) and across players (`p2p1`, `p1p2`), each evaluated on three sets:
     - the T186/T189 test split (old renders, comparable with v1);
     - the new held-out files;
     - new renders on the v1 test files.
   - The tables use the predictor-pilot-v1 format.
   - The step appends `benchmark / - / predictor-dataset-v1` ledger rows itself, metric `<arm>_<data>_<head>_<evalset>_model_top1`.

## Commands for the human

Start (from the repo root):

```bash
scripts/overnight_drive.sh
```

It prints the PID. It runs `drive_dataset_render.py all` under nohup, nice 10 and `caffeinate -i`, and logs to `cache/drive-dataset-v1/run.log`.

Check progress:

```bash
cat cache/drive-dataset-v1/progress.json        # shards and clips done, failures, free disk, load, stop reason
tail -f cache/drive-dataset-v1/render.log       # one line per shard, pauses, stops
.venv/bin/python -m tonematch.engines.host_budget status   # hosts granted (optional)
```

Stop safely:

```bash
kill <PID>        # the PID printed at start, also in cache/drive-dataset-v1/job.pid
```

Workers finish their current shard (up to about a minute), close their Native hosts and exit; training is skipped. Then run `.venv/bin/python scripts/proc_check.py`. Never use a pattern kill.

Resume: run `scripts/overnight_drive.sh` again. Finished shards are skipped. Train on what is done without rendering more:

```bash
.venv/bin/python scripts/drive_dataset_render.py train
```

## Running in batches (human 2026-10-19)

Render in batches of any length, then train once. Each `render` run resumes: it skips every finished 100-clip shard and never overwrites one. Do not re-run `overnight_drive.sh` per batch, since `all` retrains at the end of every run and overwrites the summary. Do not re-plan after batches have started.

1. **Once:** `.venv/bin/python scripts/drive_dataset_render.py plan --target 150000 --force`. This replaces the unused 60k shakedown-era plan.
2. **Each batch:**
   ```
   TONEMATCH_AGENT=learning TONEMATCH_AFXREP_DEVICE=cpu nohup caffeinate -i nice -n 10 \
     .venv/bin/python scripts/drive_dataset_render.py render --hours 2 \
     > cache/drive-dataset-v1/batch-$(date +%m%d-%H%M).log 2>&1 &
   ```
   - Run one batch at a time.
   - Stop with `kill <PID>`; find it with `pgrep -f "drive_dataset_render.py render"`.
   - Run `.venv/bin/python scripts/proc_check.py` after each batch.
3. **Train:** the same prefix with `train` in place of `render --hours N`. Training overwrites `summary.md`, `summary.json` and the checkpoints, so copy the summary first to keep it, e.g. `summary-50k.md`.

**Lead's suggested schedule (an estimate, not measured):**
- render about 4 h (50–60k clips), then train and keep `summary-50k.md`;
- render about 3 h more (to about 100k), then train and keep `summary-100k.md`;
- compare the two, on the new held-out files and the cross-player rows especially. Continue toward 150k only if 100k is clearly better; otherwise more DI recordings, not more renders, are the limit.

| Clips | Render | Train | Total |
|---|---|---|---|
| 60k | 2.5–4 h | 2–3 h | 5–7 h |
| 100k | 4–6 h | 3–5 h | 7–11 h |
| 150k | 6–9 h | 5–7 h | 11–16 h |

## Where results land

- `cache/drive-dataset-v1/summary.md` and `summary.json`: the tables.
- `cache/drive-dataset-v1/shards/`: features (about 4 KB per clip, about 0.3 GB for 60,000).
- `cache/models/predictor-dataset-v1/heads_main_{v1data,v2data}_{logreg,mlp}_multi.pkl`: checkpoints, not adopted.
- `ledger/runs.csv`: the `predictor-dataset-v1` rows.
- `cache/drive-dataset-v1/failure.json`: written only if the job crashed.

## Expected cost

- Shakedown rate on one host: 0.49 s per clip (0.34 s render, 0.14 s embedding).
- With 4 hosts (if the load guard allows), 60,000 clips take about 2.5 to 4 hours. Training (MLP on about 50,000 clips, 7 MLP fits with out-of-fold predictions) takes about 2 to 3 hours.
- Disk: about 0.3 GB.
- To use more of the night, raise the cap: `scripts/overnight_drive.sh --target 200000` needs a new plan, so first delete `cache/drive-dataset-v1/plan.json`. Training then takes longer.

## Shakedown (2026-10-19)

- `all --limit 40 --hours 0.1 --hosts 2 --old-limit 1500 --epochs 5 --seeds 1` into a scratch dir:
  - 40 clips rendered and embedded, 0 failed;
  - training and evaluation ran end to end (summary written);
  - proc_check showed no orphans;
  - output deleted.
- SIGTERM test: on a 200-clip plan, the supervisor stopped after the current shard (100 clips, 49 s) and closed the host. proc_check showed no orphans.
- Tests: `tests/test_drive_dataset_render.py` (P3 exclusion, held-out split, resume, retries, disk guard, deadline, stop).
