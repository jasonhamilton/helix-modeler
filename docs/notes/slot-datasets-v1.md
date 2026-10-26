# slot-datasets-v1: training-data render and train commands for amp, cab, mod, delay, reverb and EQ ([T208](../../board/TASKS.md#t208 "Commands for the next training sessions on the other slots"))

Owner: learning. Written 2026-10-22 at the human's request: "let's also get the commands ready for the next training sessions on the other blocks". The tooling is built and tested, and a few clips per slot were smoke-rendered to measure speed. **No long render has run, and nothing is adopted.**

**Approval needed first.** Every design choice below is a proposal. Under the 2026-10-18 rule, the human approves the method before it takes effect. Approve, change or reject the items in [Decisions for the human](#decisions-for-the-human) before running the commands.

Slot order (human, 2026-10-22): drive > amp > cab > mod > delay > reverb > EQ, every slot optional. Drive already has its dataset ([drive-dataset-v1](drive-dataset-v1.md)).

## What it does

The job is drive-dataset-v1 with `--slot`. It shares that job's code and its approved conventions.

1. **Plan** (seed 208, one plan per slot, `cache/slot-dataset-v1/<slot>/plan.json`).
   - Models: the slot's searchable models, read at runtime from the user's catalog and the Native block library. No Line 6 file enters the repo.
   - Each clip is one block on one random 10 s excerpt that passes drive's activity check, from a Guitar-TECHS P1/P2 DI file. P3 is never read. No human takes and no song audio are used.
   - Every free knob is drawn uniformly on the CC grid. Level-type knobs stay at their default: drive's list, plus amp Ch Vol and cab Early Reflections (which the Stomp checks).
   - **The DI file split is drive-dataset-v1's**: train 40 files, cal 5, test 18, v1test 29. A file held out for drive is held out for every slot.
   - Mod, delay and reverb clips also get a random lead-in of 0.5–2 s. It is read and rendered before the 10 s clip, then dropped (see decision 7).
   - Each label also records the DI window's loud-frame level (`di_level_db`).
2. **Render** with drive's elastic supervisor and the same guards:
   - at most 4 Native hosts, granted by the host scaler;
   - a busy-core pause above 0.95 × cores, resuming below 0.85 × cores;
   - a disk floor of 8.5 GB and an `--hours` deadline;
   - one retry per clip, then the clip is skipped and logged;
   - SIGTERM stops between shards;
   - atomic 100-clip shards, so any rerun resumes.

   Features are drive's: frozen AFx-Rep `mid`, a 6 s centre crop plus three 3.33 s crops, stored as float16. The audio is dropped.
3. **Train** the [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1") heads (logistic regression and the 2-layer MLP), multi-crop, calibrated as [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1").
   - There is one knob column per free knob of each model, so every knob is predicted, not only two.
   - Arms: within player (`main`) and across players (`p2p1`, `p1p2`).
   - Evaluation: the `test` files and the `v1test` files.
   - Metrics: model top-1 and top-5, family top-1, and knobs within ±0.15 (with the true model, and with the head's own top-1). The summary also shows all-knobs-right, knob MAE, 68/95 % coverage, ECE, and accuracy per knob name.

## Commands for the human

Run them from the repo root. Use one batch at a time, and do not run a batch while a match or another render is going.

**1. Plan once per slot.** This takes about 35 s per slot and reads the DI files only.

```bash
.venv/bin/python scripts/slot_dataset_render.py plan --slot amp
```

- Optional: `--target N` (default: the recommended count below) and `--all-native` (decision 3).
- A plan is never replaced once a shard is rendered: `plan` refuses. Before the first shard, `--force` resamples it.

**2. Render in batches.** Each batch resumes, skips finished shards and never overwrites one.

```bash
scripts/slot_batch.sh amp 2          # slot, hours; prints the PID
```

- It runs `render --slot amp --hours 2` under nohup, nice 10 and `caffeinate -i`.
- It refuses to start while another slot or drive batch is running.
- When the batch ends, it appends a `proc_check` report to the batch log.
- Extra arguments go to `render`, e.g. `scripts/slot_batch.sh amp 2 --hosts 3`.

Without the wrapper (the drive-dataset-v1 form):

```bash
TONEMATCH_AGENT=learning TONEMATCH_AFXREP_DEVICE=cpu nohup caffeinate -i nice -n 10 \
  .venv/bin/python scripts/slot_dataset_render.py render --slot amp --hours 2 \
  > cache/slot-dataset-v1/amp/batch-$(date +%m%d-%H%M).log 2>&1 &
```

**3. Progress.**

```bash
.venv/bin/python scripts/slot_dataset_render.py status   # every slot: clips done / planned, failed, rate, ETA, summaries
cat cache/slot-dataset-v1/amp/progress.json              # this run's rate, ETA, free disk, busy cores, stop reason
tail -f cache/slot-dataset-v1/amp/render.log             # one line per shard, pauses, stops
```

**4. Stop safely.** The current shard finishes (up to about a minute or two) and the hosts close. Never use a pattern kill.

```bash
kill <PID>                                 # printed at start; also in cache/slot-dataset-v1/amp/job.pid
.venv/bin/python scripts/proc_check.py     # afterwards
```

**5. Resume.** Run the same `scripts/slot_batch.sh amp <hours>` again.

**6. Train.** Training can run at any point on what is done. Earlier results are kept.

```bash
TONEMATCH_AGENT=learning .venv/bin/python scripts/slot_dataset_render.py train --slot amp
```

- It writes `summary.md` and `summary.json`, plus kept copies `summary-n<clips>.md` and `.json`.
- Checkpoints go to `cache/models/slot-dataset-v1/amp/n<clips>/`. A repeat at the same count gets a time suffix, so nothing is overwritten.
- It appends `benchmark / - / slot-dataset-v1-amp` ledger rows; `--no-ledger` skips them.
- Optional: `--heads mlp` (faster), `--device auto` (decision 12).

As with drive, train at about half the target and again at the full target, then compare the two summaries. Continue only if the second is clearly better.

## Where results land

- `cache/slot-dataset-v1/<slot>/`: `plan.json`, `shards/` (features, about 4 KB per clip), `progress.json`, `render.log`, `batch-*.log`, `summary*.md` and `summary*.json`.
- `cache/models/slot-dataset-v1/<slot>/n<clips>/heads_main_{logreg,mlp}_multi.pkl`: not adopted. Card: `models/CARDS.md`.
- `ledger/runs.csv`: the `render-cost / native / slot-dataset-v1-<slot>` smoke rows, and the `benchmark / - / slot-dataset-v1-<slot>` training rows.

## Per-slot pools, speed and cost

Smoke run, 2026-10-22, after the seven-song match batch had finished:
- `smoke --clips 24`: 24 clips per slot on one fresh Native host per slot, 144 clips in all;
- 143 rendered and embedded, 1 failed. The failure was `HD2_PreampSVT4Pro` at one random setting, which returned silence twice; it was skipped and logged, as designed;
- proc_check before and after: 0 orphans;
- ledger: `render-cost / native / slot-dataset-v1-<slot>`, metric `wall_s_per_clip_1host`, six rows at [2026-10-22T21:14:09](../../ledger/runs.csv?plain=1#L1963 "render-cost · native · slot-dataset-v1-amp · wall_s_per_clip_1host = 0.627 (first of 6 rows at this time)") (6 rows)Z;
- features were kept only in the scratchpad.

**Warning: one host must not switch slots.** A Native host that has rendered one category and then switches to another slows from about 0.3–0.5 s to about 1.5 s per render. This was measured twice, in two slot orders. A real batch renders one slot per job, so it is not affected. Drive held 0.37 s per render over 150k clips of one category. The smoke command now opens a fresh host per slot.

Models come from the user's catalog and Native library at runtime. They are counted here, not listed; the ids are in each local `plan.json`.

| Slot | Models in pool | Families | Knob columns (max per model) | Lead-in | Recommended clips (per model) | Render s/clip | Embed s/clip | Wall s/clip, 1 host | Est. clips/s, 4 hosts | **Render time, 4 hosts** (3 hosts) | Est. train time |
|---|---|---|---|---|---|---|---|---|---|---|---|
| amp | 224 (111 amp + 113 preamp) | 113 | 15 | none | 120,000 (536) | 0.47 | 0.15 | 0.63 | 6.1 | **5.4 h** (7.2 h) | 2.5–4 h |
| cab | 87 (41 legacy + 46 mic-IR) | 67 | 5 | none | 50,000 (575) | 0.46 | 0.13 | 0.60 | 6.4 | **2.2 h** (2.9 h) | 0.7–1.2 h |
| mod | 11 (approved Native stand-ins) | 11 | 6 | 0.5–2 s | 12,000 (1,091) | 0.30 | 0.13 | 0.44 | 8.8 | **0.4 h** (0.5 h) | under 0.5 h |
| delay | 43 (time-variant models dropped) | 43 | 23 | 0.5–2 s | 45,000 (1,047) | 0.38 | 0.13 | 0.52 | 7.5 | **1.7 h** (2.2 h) | 0.7–1.2 h |
| reverb | 5 (approved Native stand-ins) | 5 | 5 | 0.5–2 s | 6,000 (1,200) | 0.32 | 0.13 | 0.46 | 8.4 | **0.2 h** (0.3 h) | under 0.5 h |
| EQ | 8 | 8 | 11 | none | 8,000 (1,000) | 0.27 | 0.13 | 0.41 | 9.5 | **0.2 h** (0.3 h) | under 0.5 h |
| **all six** | 378 | | | | 241,000 | | | | | **about 10 h** (13 h) | about 5–8 h |

How the estimates were made:
- **Render rates.** clips/s = hosts / (wall s per clip on one host) × 0.96. The factor 0.96 is drive-dataset-v1's measured efficiency at 4 hosts: 6.5–7 clips/s at 0.57 s per clip per host.
- **Uncertainty.** 24 clips per slot is a small sample, and amp and cab models differ in cost, so expect ±30 %. The first batch's `progress.json` gives the real rate.
- **Lead-in.** The lead-in is already included in the mod, delay and reverb times.
- **Train times** are scaled from drive-dataset-v1 (114k clips, 60 models: 2.4 h for every arm, both heads and 2 MLP seeds) and are **not measured**. Amp has more classes and knob columns, so its range is wider.
- **Disk.** About 4 KB per clip, so about 1 GB for all six slots. A plan file is about 17 MB per 45k clips.

**This Mac** (10 cores) is the only render machine. The busy-core guard pauses renders above 9.5 busy cores, so a batch slows or pauses while you use the Mac heavily.

**Linux:** drive-dataset-v1's notes record no Linux machine or path, and Helix Native has no Linux build. Renders are therefore Mac-only. Training needs only the features. It could run on a Linux box with `train --slot S --device cuda` after copying `cache/slot-dataset-v1/<slot>/` (shards and plan) together with the repo, but this is untested.

**Suggested schedule** (an estimate, like drive's). Each line is a separate set of batches.

| Session | Commands | About |
|---|---|---|
| 1 | `plan --slot amp`; `slot_batch.sh amp 3`; `train --slot amp` at about 60k | 3 h + 2 h |
| 2 | `slot_batch.sh amp 3`; `train --slot amp` at 120k; compare the two summaries | 3 h + 3 h |
| 3 | cab: plan, `slot_batch.sh cab 2.5`, train | 2.5 h + 1 h |
| 4 | mod, then delay, then reverb (plan, batch, train each) | 2.5 h + 2 h |
| 5 | EQ (plan, batch, train) | 0.3 h + 0.3 h |

## Decisions for the human

Each item gives a recommendation and the alternatives. Nothing takes effect until you approve it.

"Mirrors drive" means it follows a convention that was already used for drive-dataset-v1. Every point where this job deviates from drive is marked **deviation**.

1. **Which slots, and in what order.**
   - Recommended: the slot order, one slot at a time: amp, cab, mod, delay, reverb, EQ.
   - Any slot can be skipped (every slot is optional).
   - Alternative: skip the cab and EQ renders and synthesise that data from the linear tables (see item 2).

2. **Single block per slot (the default built here) or the lead's chain proposal (not approved).**
   - The chain proposal: render only the nonlinear core (drive > amp) on Native, and apply cab and EQ offline as the [T117](../../board/TASKS.md#t117 "Linear specialist for cab and EQ") linear tables (87 cabs and 8 EQs, error 0.04–0.17 dB). mod, delay and reverb would also be applied offline.
   - Cost, part 1: code. The chain proposal is not built. It needs an offline chain step in the worker (core render, then table filter, then embed). I estimate one to two days.
   - Cost, part 2: phase. The tables hold magnitude at 1/12 octave only, so phase is approximated. That needs a check against real serial chain renders: block-serial rendering nulls at about -67 dB, and about 500 renders, under 10 minutes, would do.
   - Cost, part 3: the time effects. mod, delay and reverb have no tables, because they are time-variant or have feedback. They still need Native, either:
     - serial renders on the core's output (about 2 × render time per clip); or
     - keeping the core audio and processing it later. As 16-bit FLAC that is about 0.9 MB per clip, so 100k clips need about 90 GB, against about 0.4 GB as features.
   - Cost, part 4: embedding. Every offline variant of a core render needs its own AFx-Rep pass, about 0.13–0.2 s each. N cab/EQ variants per core render therefore cost about N × 0.15 s, against N × 0.6 s on Native.
   - Gain: the data looks like real records. An amp is always followed by a cab, and effects sit on a distorted signal. Single-block data never does this; this is the [T202](../../board/TASKS.md#t202 "Training-data coverage report per slot") gap, where behind an amp almost every block reads as "drive".
   - Recommended: approve single-block as a first, cheap measurement (it is ready now and mirrors drive). Then measure whether single-block heads transfer to a small set of serial chain renders (about one hour) before choosing the chain design.

3. **Model pools.**
   - Recommended defaults: amp all 224, cab 87, mod 11, delay 43, reverb 5, EQ 8.
   - amp: all 224 models (111 amps and 113 preamps). Alternative: a shortlist chosen by you.
   - cab: all 87. The catalog's pan twins are not duplicated.
   - mod: the 11 approved Native stand-ins (the tremolos, 70s Chorus, Bubble Vibrato and MM4 Analog Chorus; DECISIONS 2026-10-16). Alternative: `--all-native`, all 56 Native mods. This needs a stand-in ruling, because Native may not sound like the Stomp for those 45 models.
   - delay: 43 models; the time-variant ones are dropped, as in search.
   - reverb: the 5 stand-ins. Alternative: `--all-native`, all 24.
   - EQ: all 8.

4. **Clip counts** (table above).
   - Recommended: about 500–600 clips per model for amp and cab, and about 1,000–1,200 for the small slots, with training at half and at full, as for drive.
   - Evidence: drive went from 145 to 1,950 clips per model, and new-file top-1 rose from 0.55 to 0.93. No learning curve exists for other slots, so the half-way summary is the check.
   - Alternative: more amp clips (224 models: 250k would take about 11 h of rendering).

5. **Amp without a cab.**
   - Recommended: the first measurement renders the amp alone, as gain-trust-v1 did; treat cab-after-amp as the next step.
   - Alternative A: the catalog-linked cab (`cablink`) rendered serially after the amp. This is not built and costs about 2 × render time.
   - Alternative B: the linked cab applied from the linear tables. This is part of item 2 and is not built.
   - Reason to change: an amp heard without a cab sounds unlike any record, so heads trained only on it may not transfer.

6. **Knob sampling** (mirrors drive).
   - Every free knob is drawn uniformly on CC 0–127. Level-type knobs stay at their default: drive's list, plus amp Ch Vol and cab Early Reflections.
   - Mix knobs are uniform too, so about 10 % of mod, delay and reverb clips have Mix below 0.1. They are nearly dry and hard to identify. Alternative: a Mix floor such as CC 13 or above.
   - Delay Time uses the template range (0–1.27 s for most models, 10 ms per CC step), so it needs no narrowing.
   - Recommended: uniform, as built.

7. **Lead-in for mod, delay and reverb (deviation).**
   - Each clip is read and rendered from a random 0.5–2 s before its 10 s window, and that lead-in is dropped before embedding. LFOs then start at a random phase, and delay lines and reverb tails are already filled by earlier playing.
   - Cost: about 12 % more audio rendered on those slots; it is included in the table.
   - Alternative: no lead-in (drive's convention). Every clip would then start with an empty delay line and an LFO at the same phase after load.
   - Recommended: the lead-in.

8. **Input level** (mirrors drive).
   - The DI is used at its raw level, as in drive-dataset-v1 (median loud-frame level about -34 dBFS).
   - New: each clip's `di_level_db` is stored, so a level-conditioned head ([T194](../../board/TASKS.md#t194 "Explain the Drive anomaly") v2cond) or the guitar-profile convention ([T204](../../board/TASKS.md#t204 "Guitar calibration (human 2026-10-22)")) can be applied at training time without re-rendering.
   - Alternative: level every DI to -26.7 dBFS, or to the profile level, before rendering.
   - This matters most for amp gain.

9. **DI split** (mirrors drive).
   - Recommended: reuse drive-dataset-v1's file split (train 40, cal 5, test 18, v1test 29). Every held-out file then stays held out for every slot, which a later chain test needs.
   - Alternative: `--fresh-splits`.

10. **Heads and targets.**
    - The [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1") heads (logistic regression and MLP), multi-crop, calibrated as [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1").
    - **Deviation:** one knob column per free knob of each model (up to 15 for amp, 23 for delay), instead of drive's two named knobs.
    - Families (proposed): an amp and its preamp twin share a family, and so do a legacy cab and its mic-IR twin. Every other slot has one family per model.
    - Alternatives: predict a few named knobs per slot (e.g. amp Drive, Bass, Mid, Treble, Master); or flat families.

11. **Metrics and bars.**
    - Reported: model top-1 and top-5, family top-1, and knob within ±0.15 (with the true model, and with the head's own top-1), plus all-knobs-within, MAE, 68/95 % coverage, ECE, and accuracy per knob name.
    - ±0.15 for every knob is my proposal; drive used ±0.15 for Drive and ±0.10 for Tone.
    - No pass bar is proposed, and nothing is adopted from these numbers. Each retrain is compared with the previous ledger row for its own slot dataset.

12. **Training device** (mirrors drive). CPU by default. `--device auto` (MPS on this Mac) is available but unmeasured.

13. **Stereo** (mirrors drive). Stereo outputs (reverbs, stereo delays) are summed to mono before embedding, as drive's renderer does, so width cues are lost.

## Tests

`tests/test_slot_dataset.py` (14 tests) covers:
- P3 is never read, and drive's split is reused;
- level knobs stay at their default, and the plan is seeded;
- the lead-in lands on an active window, and the render loop reads and crops it;
- the k-knob heads and old 2-knob pickles;
- scoring;
- training end to end, keeping every result;
- the re-plan guard;
- the batch script.

`tests/test_drive_dataset_render.py` and `tests/test_afx_heads.py` are unchanged and pass, and the real [T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option") checkpoint still loads and predicts.

## Amp silence failures ([T212](../../board/TASKS.md#t212 "Diagnose the slot-dataset-v1 amp silence failures"), 2026-10-22)

Seen in [T210](../../board/TASKS.md#t210 "slot-dataset-v1 amp: plan + first 2-hour batch"), batch 1. After 17,400 attempted clips, 818 had failed, 4.7 %. Every failure was "Native returned silence", and 84 % of them were preamps.

**Cause: the renderer's absolute silence floor, not Native.**
- Drive's `render_one` rejects any clip whose mean power is at or below **-90 dBFS**. That rule was fine for drives. The engine's own guard has been at -240 dB (digital zero) since [T139](../../board/TASKS.md#t139 "i062 silent true-preset render"), because Native legitimately renders far quieter than -100 dBFS at low gain knobs.

**Probe on one host, behind the LoadGuard, about 200 renders.** It ran during the second batch; proc_check showed 0 orphans before and after.
- Of 24 clips that failed with the renderer's -90 dB check:
  - all 24 render **the same audio again** (a repeat nulls at -187 to -210 dB);
  - they are **quiet, not empty**: -90 to -148 dBFS mean power, peaks -70 to -122 dBFS;
  - their **spectral shape is unchanged** by raising Master/Drive/Gain to 64 (correlation 0.94–1.00).
- Preroll and latency are not the cause: the first second is not systematically quieter than the rest.
- 4 failures out of 4 checked from the 16 "silence for a non-silent input" errors are the engine's own guard: the output was digital zero. Each had a Master, Gain or PreGain pot at **exactly CC 0**, which is real silence: the pot is fully down.
- **Preamps are quiet overall.** With every knob at 64 they give -50 to -58 dBFS from a DI at about -34 dBFS, and their pots fall steeply below about CC 16. Examples:
  - PV Vitriol Clean, Master 16 / 8 / 4 / 1: -61 / -106 / -130 / -147 dBFS;
  - Essex A30, Drive 8: -92 dBFS.
- **For preamps, Master behaves as a pure volume control.** Spectral correlation with the boosted render was 1.000 in every preamp case, against 0.94–0.98 for full amps, where Master also changes the tone.
- AFx-Rep peak-normalises each clip, so these quiet renders give valid, level-invariant features.

**Bias if nothing is done.** The missing clips are not random. They take away the bottom of the gain and level knobs, mostly for preamps.

| Knob range (CC) | 0 | 1–7 | 8–15 | 16–31 | 32–63 | 64–127 |
|---|---|---|---|---|---|---|
| Preamp, any Master knob: share lost | 0.58 | 0.48 | 0.27 | 0.09 | 0.05 | 0.02 |
| Preamp, any Drive/Gain knob | 0.44 | 0.33 | 0.20 | 0.11 | 0.06 | 0.03 |
| Amp, any Master knob | 0.16 | 0.13 | 0.04 | 0.01 | 0.00 | 0.00 |
| Amp, any Drive/Gain knob | 0.06 | 0.08 | 0.04 | 0.02 | 0.01 | 0.01 |

- Models: 12 of 224 lose more than 20 % of their clips, 40 lose more than 10 %, and 106 lose none.
- Worst: Preamp SV Beast Brt 33 %, PV Vitriol Clean 32 %, Busy One Ch2 31 %, German Xtra Blue 28 %, Solo Lead Clean 27 %, Busy One Ch1 25 %.
- The splits are hit evenly (train 4.5 %, cal 5.2 %, test 4.4 %, v1test 5.7 %), so the test sets carry the same bias.
- Effect on a head: low Master/Drive settings of clean preamps are under-represented, so knob predictions there would lean high, and those models are slightly under-trained.

**Proposed fix (needs the human's approval; nothing is adopted).** The running batch and the main plan are not touched. Workers it respawns load the new code, but a main plan carries no floor key, so it still uses drive's -90 dB floor and the original labels (tested).
- **A (recommended): refill, then choose at training time.**
  - `refill` re-renders only the failed clips, with their identical records, into a separate plan `cache/slot-dataset-v1/amp-refill/`.
  - The floor is the engine's digital-zero guard (-240 dBFS), and each clip's output level is stored as `out_db`.
  - The 16 digital-zero clips (a pot at CC 0) are skipped, because no floor can keep them.
  - Training adds the refill with `--with-refill`. `--min-out-db X` can then drop clips you consider unrealistically quiet. The default keeps all of them; the cut is a design choice for you.
  - Cost: 836 clips so far, about 2.5 min on 4 hosts. It is append-only: run `refill` again after later batches, and only new failures are added, as new shards.
- **B: set a floor for future slot plans.** For example `silence_db: -240` plus `record_out_db` in every new `plan.json`, so that cab, mod, delay, reverb and EQ never hit this. It is not built as a default.
- **C: a sampling floor.** Draw Master and Drive/Gain from CC 16 or above (or CC 1 or above), so inaudible settings are never planned. That changes the knob-sampling design and needs a new plan.
- **D: treat preamp Master as a level knob** (keep it at its default, like Ch Vol), since it changes only the level. That changes the drawn knobs and needs a new plan for preamps.

**Commands (only after approval).**

```bash
.venv/bin/python scripts/slot_dataset_render.py refill --slot amp --dry-run     # counts only, writes nothing
.venv/bin/python scripts/slot_dataset_render.py refill --slot amp               # writes amp-refill/plan.json (append-only)
scripts/slot_batch.sh amp-refill 0.5                                            # after the amp batches; refuses while one runs
TONEMATCH_AGENT=learning .venv/bin/python scripts/slot_dataset_render.py train --slot amp --with-refill [--min-out-db -100]
```

`train --with-refill` writes checkpoints to `cache/models/slot-dataset-v1/amp/n<clips>-refill<k>/` and its summary as `summary-n<clips>-refill<k>.md`. It asserts that no refill clip id is already in the main shards. The summary JSON records `n_refill` and `n_refill_dropped_quiet`.
