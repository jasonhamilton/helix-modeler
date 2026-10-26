# chain-transfer-v1: do the single-block heads still name their block inside a chain? ([T214](../../board/TASKS.md#t214 "Chain transfer test"))

Owner: learning. Run 2026-10-23. Approved by the human the same day ("yes"; DECISIONS, "chain transfer test approved"). **This is a measurement only. There is no pass bar and nothing is adopted.**

## Answer

**No, except partly for the cab.** On a finished chain, the drive and amp heads are at chance or barely above it. The cab and mod heads keep some information. Delay, reverb and EQ fall to near chance. Knob accuracy falls to about the level of a constant mid-range guess, which scores about 0.3 at ±0.15.

The pipeline itself is sound. Where a head's input matches its training conditions, it reproduces its single-block numbers:
- the drive head on the drive-only prefix gets 0.92 / 0.99;
- the amp head right after an amp fed straight from the DI gets 0.60 / 0.80.

The loss comes from the other blocks:
- **Anything in front of a block hurts it.** A drive in front of the amp drops amp top-1 from 0.60 to 0.16.
- **A cab after a block nearly erases it.** After the cab, drive top-1 is 0.03 and amp top-1 is 0.01.

Every record has the drive/amp → cab core, so heads trained on single blocks cannot be used as shortlists in a chain matcher as they are.

## Method

- **Chains.** `scripts/chain_transfer.py plan`, seed 214, 300 chains, in the fixed slot order:

  | Slot | Drive | Amp | Cab | Mod | Delay | Reverb | EQ |
  |---|---|---|---|---|---|---|---|
  | p | 0.7 | 1 | 1 | 0.2 | 0.3 | 0.3 | 0.2 |
  | Chains drawn | 210 | 300 | 300 | 66 | 103 | 81 | 58 |

  - Models are drawn uniformly from the same pools as training: the slot-dataset-v1 plans (amp 224, cab 87, mod 11, delay 43, reverb 5, EQ 8) and drive-dataset-v1's 54 drives.
  - The knobs drawn are the training plan's own per-model lists, each uniform on CC 0–127. Level knobs, amp Ch Vol and cab Early Reflections stay at their default.
  - 186 chains have a time effect. Each of these gets the approved 0.5–2 s lead-in, which is rendered through the whole chain and then dropped.
  - The chain list is `cache/chain-transfer-v1/chains.json`. It stays local because it holds model ids.
- **DI.** Only the 18 drive-dataset-v1 `test` split files (P1/P2 Guitar-TECHS), identical to every slot plan's test files (checked). The heads never trained on them. P3 was never read. One random active 10 s window is taken per chain.
- **Render.** Block-serial on Helix Native: each block is a single-block library preset rendered on the previous block's output. NativeEngine already trims its 16-sample latency. T045/T121 measured this path at -67 dB null against the same 8-block chain in one instance.
  - Rendering is stage-wise (all drives, then all amps, and so on), with a fresh pool of 3 hosts per stage, so no host switches block category within a stage.
  - The run used nice 10, a busy-core LoadGuard before every 24-job chunk, a -240 dBFS silence floor and one retry.
  - Stereo outputs are summed to mono before the next block, as in training. A real Stomp chain would carry stereo after a stereo delay or reverb.
  - 5 chains failed twice with digital-zero output and were dropped (a drive, three amps or preamps, a swell delay), leaving 295 chains.
  - Cost: 1,106 block renders in 500 s wall, **8.3 min** (0.45 s per block, embedding included). proc_check showed 0 orphans before and after.
- **Features.** As in training: frozen AFx-Rep `mid`, three contiguous 3.33 s crops of the 10 s clip (lead-in dropped). Every stage's output is embedded, so a head can also be scored right after its own block and after each later block. Audio is not kept.
- **Heads.**
  - Each slot's full-size [T213](../../board/TASKS.md#t213 "slot-dataset-v1 overnight run") checkpoints `cache/models/slot-dataset-v1/<slot>/n<clips>/heads_main_{mlp,logreg}_multi.pkl` (amp `n114347-refill5526`).
  - The drive head is the T191/T196 `heads_main_v2data_{mlp,logreg}_multi.pkl` used by `match_block --shortlist predictor`, masked to the 54 drive-dataset-v1 models as that path does.
  - Calibrated temperature as stored.
- **Metrics.** Model top-1/3/5, family top-1 and knobs within ±0.15 of the true position (true model's predictions, pooled over its drawn knobs), as in [T213](../../board/TASKS.md#t213 "slot-dataset-v1 overnight run")'s `score_slot`. A test checks that this code gives identical numbers. Intervals: Wilson 95 % for accuracies, and a bootstrap over chains for knobs.
- **Single-block reference.**
  - Slots: the [T213](../../board/TASKS.md#t213 "slot-dataset-v1 overnight run") summaries' `test` numbers (same 18 files).
  - Drive: recomputed on the drive-dataset-v1 test shards with this code (19,012 clips). It reproduces [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render"): top-1 0.927; Drive within ±0.15 0.731, Tone 0.831; top-3 0.990, which [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") did not report.

## Results: finished chain output vs single blocks (MLP head)

Chance is 1/M for top-1 and 3/M for top-3, where M is the number of models in the pool.

| Slot | Models | n | Top-1 chain (95 %) | Top-1 single | Top-3 chain (95 %) | Top-3 single | Top-3 chance | Family top-1 chain / single | Knobs ±0.15 chain (95 %) | Knobs single |
|---|---|---|---|---|---|---|---|---|---|---|
| drive | 54 | 206 | 0.02 (0.01–0.06) | 0.93 | 0.10 (0.06–0.15) | 0.99 | 0.06 | 0.20 / 0.94 | 0.35 (0.30–0.40) | 0.78 |
| amp | 224 | 295 | 0.01 (0.00–0.02) | 0.54 | 0.02 (0.01–0.05) | 0.80 | 0.01 | 0.01 / 0.60 | 0.31 (0.29–0.33) | 0.45 |
| cab | 87 | 295 | 0.25 (0.21–0.31) | 0.76 | 0.40 (0.35–0.46) | 0.91 | 0.03 | 0.28 / 0.78 | 0.43 (0.40–0.45) | 0.64 |
| mod | 11 | 66 | 0.21 (0.13–0.33) | 0.75 | 0.45 (0.34–0.57) | 0.91 | 0.27 | 0.21 / 0.75 | 0.37 (0.30–0.45) | 0.63 |
| delay | 43 | 102 | 0.10 (0.05–0.17) | 0.56 | 0.19 (0.12–0.27) | 0.74 | 0.07 | 0.10 / 0.56 | 0.32 (0.29–0.35) | 0.43 |
| reverb | 5 | 80 | 0.28 (0.19–0.38) | 0.47 | 0.68 (0.57–0.77) | 0.81 | 0.60 | 0.28 / 0.47 | 0.32 (0.28–0.36) | 0.48 |
| EQ | 8 | 57 | 0.12 (0.06–0.23) | 0.82 | 0.42 (0.30–0.55) | 0.99 | 0.38 | 0.12 / 0.82 | 0.34 (0.28–0.39) | 0.63 |

Logistic regression for reference, top-1 / top-3:

| Slot | Chain | Single |
|---|---|---|
| drive | 0.04 / 0.11 | 0.88 / 0.97 |
| amp | 0.01 / 0.02 | 0.43 / 0.66 |
| cab | 0.23 / 0.35 | 0.68 / 0.84 |
| mod | 0.33 / 0.56 | 0.72 / 0.89 |
| delay | 0.10 / 0.21 | 0.50 / 0.69 |
| reverb | 0.30 / 0.69 | 0.46 / 0.79 |
| EQ | 0.12 / 0.37 | 0.73 / 0.97 |

The linear head transfers no better than the MLP, except slightly for mod.

**Knobs by name, chain vs single (MLP, true model):**

| Slot | Knob | Chain | Single |
|---|---|---|---|
| drive | Drive | 0.36 | 0.73 |
| drive | Tone | 0.33 | 0.83 |
| amp | Drive | 0.33 | 0.65 |
| amp | Bass | 0.27 | 0.49 |
| amp | Mid | 0.36 | 0.43 |
| amp | Treble | 0.35 | 0.58 |
| amp | Master | 0.25 | 0.37 |
| amp | Presence | 0.38 | 0.36 |
| cab | Distance | 0.49 | 0.61 |
| cab | HighCut | 0.46 | 0.65 |
| cab | LowCut | 0.37 | 0.83 |
| mod | Mix | 0.33 | 0.74 |
| mod | Speed | 0.41 | 0.73 |
| mod | Depth | 0.25 | 0.59 |
| delay | Mix | 0.27 | 0.78 |
| delay | Time | 0.39 | 0.42 |
| delay | Feedback | 0.24 | 0.42 |
| reverb | Mix | 0.26 | 0.88 |
| reverb | Decay | 0.34 | 0.55 |

## Where the block is lost (along the chain, MLP top-1 / top-3)

Each line follows the same chains, scored on the output after each stage.

| Head | Right after its block | After amp | After cab | After later blocks |
|---|---|---|---|---|
| drive | 0.92 / 0.99 | 0.17 / 0.35 | 0.03 / 0.12 | 0.01–0.03 / 0.03–0.10 |
| amp | 0.29 / 0.41 (all chains); **0.60 / 0.80 with no drive in front (n 89)**; 0.16 / 0.24 with a drive in front (n 206) | — | 0.01 / 0.03 | 0.00 / 0.00–0.01 |
| cab | 0.33 / 0.53 | — | — | after mod 0.23 / 0.39, delay 0.19 / 0.34, reverb 0.24 / 0.41, EQ 0.21 / 0.28 |
| mod | 0.26 / 0.47 | — | — | |
| delay | 0.12 / 0.21 | — | — | |
| reverb | 0.26 / 0.69 | — | — | |
| EQ | 0.12 / 0.42 | — | — | |

Notes on the table:
- The two sanity points (in bold for amp, and drive right after its block) are the only places where a head's input matches training: the drive on the raw DI, and the amp on the raw DI. Both reproduce the single-block test numbers. Amp knobs there are 0.46 against 0.45 single.
- **Upstream blocks are as damaging as downstream ones.** The cab head right after the cab sees an amp's output, never a raw DI, and is already down to 0.33. Mod, delay, reverb and EQ always come after amp + cab, so they never get a clean input.
- **The cab after an amp erases most of what identifies the amp and the drive**, from 0.17 to 0.03 for drive and from 0.29 to 0.01 for amp. The cab head itself barely cares which amp is in front:

  | Amp in front of the cab | Cab top-1 / top-3 |
  |---|---|
  | Preamp | 0.31 / 0.51 |
  | Full amp | 0.35 / 0.54 |
  | Amp gain knob CC < 32 | 0.30 / 0.50 |
  | Amp gain knob CC ≥ 96 | 0.33 / 0.48 |

## Breakdown by what else is in the chain (finished output, MLP top-1 / top-3, n)

| Head | Context | Yes | No |
|---|---|---|---|
| amp | drive in front | 0.01 / 0.02 (206) | 0.00 / 0.02 (89) |
| amp | any block after the cab | 0.00 / 0.01 (200) | 0.02 / 0.06 (95) |
| cab | delay or reverb after | 0.21 / 0.35 (156) | 0.30 / 0.47 (139) |
| cab | any block after it | 0.21 / 0.35 (200) | 0.34 / 0.51 (95) |
| cab | drive in front | 0.24 / 0.38 (206) | 0.29 / 0.45 (89) |
| drive | delay or reverb after | 0.01 / 0.07 (107) | 0.04 / 0.13 (99) |
| drive | amp gain knob ≥ 64 | 0.03 / 0.10 (92) | 0.02 / 0.10 (104) |
| mod | drive in front | 0.11 / 0.35 (46) | 0.45 / 0.70 (20) |
| reverb | delay in front | 0.15 / 0.46 (26) | 0.33 / 0.78 (54) |
| delay | any block after it | 0.04 / 0.15 (46) | 0.14 / 0.21 (56) |

The full tables (every slot × every context, with knobs) are in `cache/chain-transfer-v1/results.md`. Once the amp + cab core is in place, the later blocks change little: the drive and the amp are already near chance.

**Collapse onto one class.** Out of distribution, each head piles its wrong top-1 picks onto one class, a typical sign of inputs unlike anything seen in training:

| Head | Wrong top-1 picks pile onto |
|---|---|
| drive | Sub Oct Fuzz |
| amp | Line 6 Kinetic, or a few preamps |
| mod | 70s Chorus (most errors) |
| reverb | Hall |
| EQ | Parametric |

## What it means for a chain matcher (for the human; nothing adopted)

1. **Shortlists.** The single-block heads should not be used as shortlists inside a chain matcher as they stand.
   - Drive and amp are at chance on any realistic signal.
   - The cab head keeps a weak signal: top-3 0.40, against 0.03 chance, and about 20× chance at top-1 (0.25 against 0.011).
   - The mod head keeps a little: top-3 0.45 against 0.27.
   - A shortlist built from these would miss the true model most of the time. The predictor-first drive path ([T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option")) that worked on single-drive targets would not carry over to chains.
2. **Why single-block training does not transfer.** The AFx-Rep features of a block's output depend strongly on what feeds it and what follows it. The drive → amp → cab core is in every record, and it alone takes drive and amp to chance. This is the [T202](../../board/TASKS.md#t202 "Training-data coverage report per slot") gap ("no chain training data at all") made concrete.
3. **Options, each needing the human's approval. None is started.**
   - **A. Chain-rendered training data** (the lead's chain proposal, slot-datasets-v1 decision 2).
     - Block-serial chains cost about 1.7 s per chain on 3 hosts (measured here), and one chain labels every slot at once. That is about 2,100 chains an hour, or about 2,800 on 4 hosts by the drive rate scaling.
     - A first pilot of about 5,000 chains (about 2 h) with drive/amp/cab heads trained on chain outputs would show whether chain context can be learned at all, before a large run.
     - A cheaper variant for the core only is drive → amp → cab with the cab and EQ applied from the [T117](../../board/TASKS.md#t117 "Linear specialist for cab and EQ") linear tables. It is not built, and its phase approximation needs checking.
   - **B. Context augmentation per slot.** Train each slot head on its block with random upstream and downstream blocks (for example amp with a random drive in front and a random cab after). This is the same render cost per clip as A, but it gives one slot label per render, so A dominates it.
   - **C. Keep the heads only as weak priors.** Use the cab (and maybe mod) head's top-k as one input to a render-based search, and drop drive/amp prediction in chains. This costs nothing new and gains little.
   - My recommendation, if the human wants a chain matcher with a learned start: **A as a measured pilot first.** Scoring would use the same 295-chain test set, which never trained anything.
4. **Caveats.**
   - The chains are random. Knobs are uniform and blocks are independent, so some are harsher than real records, such as a fuzz into a high-gain amp under a 100 % wet delay. The drive → amp → cab core, which does the damage, is in every real record anyway.
   - Stereo is folded to mono between blocks, as in training.
   - Block-serial rendering equals one-instance chains only to about -67 dB.
   - Mod, EQ and reverb have small n (57–80), so read their intervals.

## Files

- Code: `scripts/chain_transfer.py` (plan / render / score) and `src/tonematch/learning/chain_transfer.py`. Tests: `tests/test_chain_transfer.py` (10 tests).
- Data (local, gitignored), all under `cache/chain-transfer-v1/`:
  - `chains.json`
  - `features.npz` (every stage's crops)
  - `render_meta.json`
  - `results.json` and `results.md` (every breakdown, the knobs by name, the confusions)
  - `run.log`
- Ledger: `render-cost / native / chain-transfer-v1` (wall_s_per_block 0.45), and seven rows `benchmark / native / chain-transfer-v1`, metric `<slot>_mlp_model_top3`, at 2026-10-23T15:49Z.
