# Training-data coverage per chain slot ([T202](../../board/TASKS.md#t202 "Training-data coverage report per slot"))

## Update 2026-10-23 (after the [T213](../../board/TASKS.md#t213 "slot-dataset-v1 overnight run") overnight slot datasets)

Owner: learning. This is measurement only. Nothing is admitted into the matcher and nothing is published. No render or training was run for this update.

**How the recount was made**
- Recounted from disk at 08:37 local on 2026-10-23, from the shard metadata (`s*.json`) and label files (`s*.labels.jsonl`) of `cache/drive-dataset-v1` and `cache/slot-dataset-v1/*`, plus the checkpoints under `cache/models/slot-dataset-v1/`.
- Clips per model are counted on the train and cal splits only. Test and v1test labels were read only to count clips, files and players per split, not broken down by model.
- Predictor numbers are copied from `cache/slot-dataset-v1/<slot>/summary-n*.json` and `summary-frac0.5-*.json` (the same numbers as [slot-datasets-v1-results.md](slot-datasets-v1-results.md)), and for drive from `cache/drive-dataset-v1/summary-150k.json`. Nothing was retrained.
- Older sets (sections 1 and 2 below) are unchanged on disk and still valid as counts of those sources. The **slot totals, the "at scale" verdicts and the amp, cab and EQ gaps below are superseded** by this section.

**Headline**
- **Every slot in the order now has a single-block dataset of its own.** The new datasets hold 240,541 clips over six slots, plus drive's 149,698: 390,239 feature clips in all (frozen AFx-Rep `mid`, 6 s centre crop plus three 3.33 s crops; no audio kept). Together they take 1.8 GB on disk (slot sets 1.1 GB, drive 0.69 GB), plus 25 MB of slot heads.
- **Amp and cab went from a thin audio set to all models at hundreds of clips each:**
  - amp: 1,549 renders over 111 of 224 models, now 119,873 over 224 of 224;
  - cab: 581 audio renders over 45 of 87, now 50,000 over 87 of 87.
- **Mod and reverb are still only the approved Native stand-ins** (11 of 56 and 5 of 25), now at about 800–900 clips per model.
- **Still 0 chains.** [T214](../../board/TASKS.md#t214 "Chain transfer test") (the chain transfer test) is marked started on the board, but at 08:37 there was no chain-transfer output in `cache/` and no ledger row: it is **running**, with nothing on disk yet.
- **Still 0 mixed-and-separated Helix audio.** Still two library players (P1, P2); P3 was never read.
- **The slot heads look like drive before [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render"):** fair within player (model top-1 0.47–0.82, against drive's 0.93), weak for a new player (0.19–0.67). Knob accuracy is near what a constant guess scores on most amp, delay and reverb knobs.

### U1. Recount per slot (drive > amp > cab > mod > delay > reverb > EQ)

All rows are one block on a random 10 s excerpt of a raw-level Guitar-TECHS DI (CC BY 4.0), rendered in Helix Native 3.82, clean (no mix). Knobs are drawn uniformly on CC 0–127, with level knobs kept at their default. Every row uses the same 92 DI files, split as drive-dataset-v1: train 40, cal 5, test 18, v1test 29.

| Slot | Dataset | Rendered ok, all splits (planned) | Models: pool / Native library | Train+cal clips per model, min / median | Players (clips) | Single block / chain | Failed | Refill |
|---|---|---|---|---|---|---|---|---|
| **drive** | drive-dataset-v1 ([T191](drive-dataset-v1.md)) | 149,698 (150,000) | 54 / 61. The 6 fuzzes and Bitcrusher are excluded | 1,941 / 2,065 | P1 73,679, P2 76,019 | single, 0 chains | 302: 280 at the old -90 dBFS floor, 22 digital zero | none. The plan predates the -240 floor |
| **amp** | slot-dataset-v1 amp + amp-refill | 119,873 = 114,347 + 5,526 refilled (120,000) | 224 / 224 (111 amp + 113 preamp) | 346 / 398 | P1 59,056, P2 60,817 | single, **no cab after** | 5,653: 5,526 at the -90 floor, 127 digital zero (a pot at CC 0) | 5,526 of 5,526 re-rendered at the -240 floor ([T212](../../board/TASKS.md#t212 "Diagnose the slot-dataset-v1 amp silence failures") A), all ok. Their output level has a median of -102 dBFS (range -230 to -90). The 127 digital-zero clips are skipped |
| **cab** | slot-dataset-v1 cab | 50,000 (50,000) | 87 / 87 | 385 / 429 | P1 24,465, P2 25,535 | single, on a raw DI, **never after an amp** | 0 | not needed |
| **mod** | slot-dataset-v1 mod | 11,985 (12,000) | **11 / 56** (approved Native stand-ins) | 762 / 802 | P1 5,943, P2 6,042 | single | 15, all digital zero | not needed |
| **delay** | slot-dataset-v1 delay | 44,683 (45,000) | 43 / 47 (time-variant models dropped) | 508 / 782 | P1 22,148, P2 22,535 | single | 317, all digital zero | not needed. The -240 floor kept 359 clips quieter than -90 dBFS that the old floor would have rejected |
| **reverb** | slot-dataset-v1 reverb | 6,000 (6,000) | **5 / 25** (approved Native stand-ins) | 823 / 899 | P1 2,959, P2 3,041 | single | 0 | not needed |
| **EQ** | slot-dataset-v1 eq | 8,000 (8,000) | 8 / 8 | 722 / 752 | P1 3,980, P2 4,020 | single | 0 | not needed |
| comp | none new | 510 audio renders ([T202](../../board/TASKS.md#t202 "Training-data coverage report per slot"), unchanged) | 17 / 17 | | | single | | |
| **chains** | [T214](../../board/TASKS.md#t214 "Chain transfer test") | **0 on disk; running** | | | | | | |
| **mixed + separated** | | **0 through Helix** | | | | | | |

**Models with any labelled render, new and old sets together:** drive 61 of 61, amp 224 of 224, cab 87 of 87, mod 37 of 56, delay 46 of 47, reverb 6 of 25, EQ 8 of 8. Mod and reverb are unchanged, because their new pools were already covered.

### U2. Per-slot predictors (measurement only; none adopted)

Every head is the [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1") design:
- **Encoder:** frozen AFx-Rep `mid` (512-d), multi-crop.
- **Heads:** logistic regression and a 2 × 256 MLP, one seed (s0), trained on CPU and calibrated as [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1").
- **Knob targets:** one column per free knob of each model.
- **Checkpoints:** `cache/models/slot-dataset-v1/<slot>/{frac0.5-of-n…, n…}/heads_main_{logreg,mlp}_multi.pkl`.

The table shows the MLP, which beats the logistic regression on top-1 in every slot.
- **"Within player"** is the held-out test files of P1 and P2 (18 files). For drive this is "newtest".
- **"New player"** is train on one player, test on the other (P2→P1 / P1→P2), on the test files.
- **Knobs** are scored within ±0.15 of the true position, pooled over every knob of the model. "True" uses the true model's knob predictions; "own" uses the head's own top-1 model.

| Slot | Models (chance top-1 / top-3) | Train clips, half → full | Model top-1 within player, half → full | Top-3 within player (full) | Top-1 new player, P2→P1 / P1→P2 (full) | Top-3 new player (full) | Knobs ±0.15, true / own (full) |
|---|---|---|---|---|---|---|---|
| **drive** ([T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") v2data MLP, the one in use) | 54 (0.019 / 0.056) | 8,711 → 113,892 (v1data → v2data) | 0.55 → **0.93** | not recorded (top-5 1.00) | 0.75 / 0.62 | top-5 0.94 / 0.87 | Drive 0.73 / 0.71. Tone 0.70 / 0.67, but at ±0.10 |
| **amp** | 224 (0.004 / 0.013) | 42,132 → 84,219 | 0.502 → **0.543** | 0.797 | 0.312 / 0.187 | 0.516 / 0.344 | 0.449 / 0.237 |
| **cab** | 87 (0.011 / 0.034) | 17,523 → 35,044 | 0.708 → **0.761** | 0.910 | 0.317 / 0.347 | 0.528 / 0.549 | 0.636 / 0.472 |
| **mod** | 11 (0.091 / 0.27) | 4,146 → 8,351 | 0.708 → **0.750** | 0.907 | 0.600 / 0.474 | 0.819 / 0.719 | 0.625 / 0.475 |
| **delay** | 43 (0.023 / 0.070) | 15,621 → 31,412 | 0.527 → **0.558** | 0.744 | 0.316 / 0.245 | 0.473 / 0.391 | 0.426 / 0.245 |
| **reverb** | 5 (0.20 / 0.60) | 2,073 → 4,176 | 0.476 → **0.467** (v1test 0.489 → 0.502) | 0.808 | 0.389 / 0.311 | 0.754 / 0.754 | 0.481 / 0.248 |
| **EQ** | 8 (0.125 / 0.375) | 2,844 → 5,664 | 0.757 → **0.824** | 0.995 | 0.672 / 0.485 | 0.972 / 0.809 | 0.625 / 0.520 |

**Key knobs within ±0.15** (full MLP, test files, true model; per-knob tables in [slot-datasets-v1-results.md](slot-datasets-v1-results.md)).

A **constant mid-range guess scores about 0.3** on ±0.15: a uniform knob falls within 0.15 of 0.5 about 30 % of the time (39 of 128 CC steps). Values near 0.3 therefore carry almost no information.

| Slot | Key knobs |
|---|---|
| drive | Drive 0.73; Tone 0.70 (at ±0.10) |
| amp | Drive 0.65, Treble 0.58, Bass 0.49, Mid 0.43, Master 0.37, Presence 0.36, Sag 0.30, BiasX 0.30 |
| cab | Low Cut 0.83, High Cut 0.65, Distance 0.61, Position 0.58, Angle 0.35 |
| mod | Intensity 0.84, Mix 0.74, Speed 0.73, Depth 0.59, Shape 0.32 |
| delay | Mix 0.78, Time 0.42, Feedback 0.42; most multi-tap, pan and modulation knobs 0.24–0.35 |
| reverb | Mix 0.88, Decay 0.55, Predelay 0.35, High Cut 0.32, Low Cut 0.31 |
| EQ | High Cut 0.92, Low Cut 0.86, 4 kHz 0.94, 6.6 kHz 0.94; Q knobs 0.28–0.32 |

**Readings** (measurement, no bar implied):
- **Doubling the data helped in 5 of 6 slots,** by 3 to 7 points of top-1: amp +4.1, cab +5.3, mod +4.2, delay +3.1, EQ +6.7.
- **Reverb did not improve on the test files** (-0.9 points; +1.3 on v1test). Its 5-model top-3 of 0.81 is close to the chance level of 0.60.
- This corrects the lead's 2026-10-23 read-out in DECISIONS, which says "full beats half on every slot".
- **The new-player drop is as large as drive's was before [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render"), or larger:**
  - amp falls from 0.54 to 0.19–0.31;
  - cab from 0.76 to 0.32–0.35;
  - delay from 0.56 to 0.25–0.32.

  Drive's drop is now 0.93 to 0.62–0.75.
- **Own-model knob accuracy is low** wherever top-1 is low: amp 0.24, delay 0.25, reverb 0.25.

### U3. Gaps that remain

| Gap | State on 2026-10-23 |
|---|---|
| **Only two library players** | Every new dataset uses Guitar-TECHS P1 and P2 only, about 50/50. P3 (12 files) is the proposed Gate 3 reserve and was never read. Cross-player top-1 is the weakest number in every slot |
| **No chains** | 0 chain renders on disk. [T214](../../board/TASKS.md#t214 "Chain transfer test") (about 300 drive → amp → cab chains, with optional mod/delay/reverb/EQ, on test-split DIs) is running; no output yet. Every amp clip is rendered without a cab, and every cab clip on a raw DI |
| **No separated or mixed training audio** | Nothing rendered through Helix has been mixed with backing and separated. sep-stress-v1 (a numpy stand-in, P3) is still the only mixed-and-separated audio. The backing source is still the human's open decision |
| **The human's PRS calibration session is not used for training** | `data/guitars/prs-7-tone`: 26 segments, 5.3 min, a 263-note own bank and a profile at -36.12 dBFS ([T204](guitar-profile-t204.md)). So far it has been used only to rebuild dry tracks for matching ([T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar")). No slot or drive dataset renders from it, so the heads have never heard this guitar. Using it for training would be a new design and needs the human's approval |
| **Stand-in pools for mod and reverb** | Mod is 11 of 56 models and reverb 5 of 25 (the approved Native stand-ins). The other 45 mods and 20 reverbs have no data at scale, and 19 of each have none at all. Adding them needs a stand-in ruling (`--all-native`) or Stomp renders |
| Delay pool | 43 of 47; the time-variant models are dropped (as in search) |
| Drive fuzzes | Still absent from the at-scale drive set (54 of 61); only drive-specialist and the trust sets have them |
| Input level | All new slot sets are raw level, as drive-dataset-v1, and every clip stores `di_level_db` (and `out_db` from cab on). No level-conditioned slot head has been trained |
| Quiet amp clips | The 5,526 refilled amp clips are all at or below -90 dBFS output (median -102). Training keeps all of them (`--min-out-db` unset). Whether to cut them is the human's design choice |

Ledger: `benchmark / - / training-data-coverage`, 2026-10-23 row (this refresh). The training rows are `benchmark / - / slot-dataset-v1-<slot>`.

---

*Original report follows (2026-10-22). Numbers marked "superseded 2026-10-23" are replaced by the update above. Everything else still describes the older sets correctly.*

Owner: learning. Written 2026-10-22. This is an inventory only. No renders were made, nothing was trained and nothing is adopted. The slot order is the one the human approved on 2026-10-22: drive > amp > cab > mod > delay > reverb > EQ, every slot optional. Comp (dynamics) is shown as an extra row.

Structured data, including every model id, the per-source counts and the samples: `reports/local/t202/coverage.json` (local; it names Line 6 model ids). Ledger: `benchmark / - / training-data-coverage`.

**How the counts were made**
- Only manifests, plans, indexes and file listings were read.
- **Not read:**
  - the labels of synthetic-v1, -v2 and -v3 (sealed);
  - the labels of every held-out test split: drive-dataset-v1 `test` and `v1test`, the helix-trust-v2 test split, the gain-trust-v1 test split and all of gain-trust-v1b (they are in the [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") predictor test);
  - the dev sets' `labels.json`.

  These sets are counted, but their renders are not broken down by model.
- Song audio (`data/songs`, `cache/stems`) is excluded. The human's own takes sit under `data/songs`, have been used only as test targets ([T196](predictor-first-t196.md)) and are not counted.
- **"Labelled renders"** are renders whose model and knob values are known, from splits that may be read. For drive-dataset-v1 they are feature vectors, because its audio was dropped. Everything else is audio.
- Catalog counts come from `cache/catalog/current.json` (HX Edit 3.82). "Native" is the Native block-library index (`block-library-v1.2`). Cabs differ between the two: the catalog lists the mic-IR cabs twice (with and without pan), the library once.

## 1. Summary per slot

| Slot | Catalog models | Native library | Labelled renders | Models with any labelled render | Largest source | At scale? |
|---|---|---|---|---|---|---|
| **drive** | 61 | 61 | 125,058 (111,697 are features) | 61 of 61 | drive-dataset-v1 (54 models) | **Yes**, for 54 models (unchanged) |
| **amp** | 224 (111 amp + 113 preamp) | 224 | 1,549 *(superseded 2026-10-23: see Update U1)* | 111 of 224 *(superseded 2026-10-23: see Update U1)* | gain-trust-v1 fit (816) *(superseded 2026-10-23: see Update U1)* | No: a median of 13 per model *(superseded 2026-10-23: see Update U1)* |
| **cab** | 133 (41 legacy + 46 mic-IR, plus 46 pan twins) | 87 | 581 in audio; 87 of 87 as response tables *(superseded 2026-10-23: see Update U1)* | 45 of 87 in audio *(superseded 2026-10-23: see Update U1)* | linear-tables (not audio) *(superseded 2026-10-23: see Update U1)* | No *(superseded 2026-10-23: see Update U1)* |
| **mod** | 56 | 56 | 4,893 *(superseded 2026-10-23: see Update U1)* | 37 of 56 | mod-specialist (3,034, one P3 file) *(superseded 2026-10-23: see Update U1)* | Partly; 1 to 3 DI files per source *(superseded 2026-10-23: see Update U1)* |
| **delay** | 48 | 47 | 3,194 *(superseded 2026-10-23: see Update U1)* | 46 of 47 | time-specialist (2,600, 3 P2 excerpts) *(superseded 2026-10-23: see Update U1)* | Partly; one player *(superseded 2026-10-23: see Update U1)* |
| **reverb** | 25 | 25 | 847 *(superseded 2026-10-23: see Update U1)* | 6 of 25 | helix-trust-v1 (309) *(superseded 2026-10-23: see Update U1)* | No *(superseded 2026-10-23: see Update U1)* |
| **EQ** | 8 | 8 | 508 in audio; 8 of 8 as tables *(superseded 2026-10-23: see Update U1)* | 8 of 8 | linear-tables (not audio) *(superseded 2026-10-23: see Update U1)* | No (but tables) *(superseded 2026-10-23: see Update U1)* |
| comp (dynamics) | 18 | 17 | 510 | 17 of 17 | helix-trust-v1 (267) | No |
| **chains (any order)** | | | **0** | | presence-v2 is planned but not rendered | **No** |
| **mixed and separated** | | | **0 through Helix** | | sep-stress-v1 is a numpy stand-in | **No** |

Test-split renders exist on top of these numbers but are not broken down: helix-trust-v2 test 1,920 renders, gain-trust-v1 test 1,632, gain-trust-v1b 1,530, drive-dataset-v1 test and v1test 38,001 features. [predictor-design.md](predictor-design.md) section 3.1 counts helix-trust-v2 over both splits at amp 210, cab 87, drive 60, delay 44, comp 17, mod 11, EQ 8 and reverb 5 models.

Labelled training audio takes about 54 GB. *(2026-10-23: the six new slot feature sets add 1.1 GB; see the Update.)* drive-specialist alone is 21 GB, because it stores 10 s float32 npy.

## 2. Sources, one by one

Every source is single-block, made with Helix Native 3.82, on Guitar-TECHS DI (CC BY 4.0), and clean (DI > block, no mix), unless the row says otherwise.

Licence: every Native render is made under the owner's licence. The human ruled on 2026-10-17 that weights trained on Native renders may be published ([line6-eula-flags.md](line6-eula-flags.md)). The audio and the model ids stay local.

| Source (task) | Clips / renders | Slots and models | Kept | DI and players | Input level ([T194](../../board/TASKS.md#t194 "Explain the Drive anomaly")) | Settings sampling | Notes |
|---|---|---|---|---|---|---|---|
| **drive-dataset-v1** ([T191](drive-dataset-v1.md)) | 149,698 ok (302 failed): train 105,181 / cal 6,516 / test 19,012 / v1test 18,989 | drive 54 of 61; the 6 fuzzes and Bitcrusher are excluded | **Features only**: AFx-Rep `mid` 512-d float16, 6 s centre crop plus 3 × 3.33 s crops. The audio was dropped | 92 Guitar-TECHS P1/P2 files (40 / 5 / 18 / 29). Train-plus-cal clips: P1 53,976, P2 57,721. P3 is never read | **Raw** file level: median loud-frame -34.1 dBFS, gain to reference median +7.4 dB (5–95 % +0.5 to +14.8). Per-clip gain is in `cache/drive-anomaly-t194/input_levels.json` | **Random**, uniform on the CC grid 0–127 for every free knob. Level knobs (Output, Volume, Level …) stay at their default. Random 10 s excerpts that pass an activity check | AFx-Rep's training-data terms are unresolved ([afxrep-licence.md](afxrep-licence.md)) |
| encoder-swap-t195 ([T195](encoder-swap-t195.md)) | 71,517 re-rendered: train 27,000 / cal 6,516 / test 19,012 / v1test 18,989 | drive 54 | Features only: DAC-24k and PANNs Cnn14 embeddings | as drive-dataset-v1 | Raw | the same plan | DAC is MIT, PANNs CC BY 4.0 |
| **drive-specialist** calibration | 12,000 (60 models × 25 settings × 8 DIs) | drive 60 of 61 (includes the 6 fuzzes; no Bitcrusher) | **Audio** (npy float32, 10 s) | 8 excerpts: P1 4, P2 4 | **Levelled** to -26.7 dBFS | Random: 25 settings per model (CC 6–121 plus the default), the **same 25 on every DI** | [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") test files Drop3_m7b5 and allsinglenotes are in here |
| helix-trust-v1 ([T067](../../board/TASKS.md#t067 "Gate 2 priority 1: loss-trust-check on real Helix Native renders")) | 2,400 (40 items × 20 × 3 DIs) | 5–6 models in each of 8 categories; block-library-**v1.1** | Audio + DI | P1 960, P2 1,220, P3 220 | Levelled -26.66 | Trust design: true setting, 9 near misses, neighbours, other categories | Older templates; predictor-design says leave it out |
| **helix-trust-v2** ([T096](../../board/TASKS.md#t096 "Gate 2 option A + rule B")) | 3,840 (192 items × 20); train 1,920 / test 1,920 | Train side: amp 105, cab 43, drive 30, delay 22, comp 8, mod 5, reverb 5, EQ 4 | Audio + DI | 104 files, 52 train / 52 test. Train renders: P1 720, P2 1,160, P3 40 | Levelled -26.7 | Trust design (clustered around the truth) | "aligned" and "sep" conditions are made at score time, not stored |
| gain-trust-v1 ([T144](../../board/TASKS.md#t144 "Gain-stage score term for Gate 2 attempt 3")) | 3,264; fit 1,632 / test 1,632 | Fit side: amp 48, drive 30. Amps have **no cab** | Audio + DI | Fit renders: P1 476, P2 1,156 | Levelled | Gain-stage neighbours: truth, 9 near, 4 gain-compensated, 3 small moves | |
| gain-trust-v1b ([T146](../../board/TASKS.md#t146 "Extended rule B (human 2026-10-18, follow-up to T144)")) | 1,530 (90 × 17), 8 s, test split only | amp and drive (not read) | Audio + DI | P1 833, P2 680, P3 17 | Levelled | as gain-trust-v1 | Its drive renders are in the [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") test |
| mod-trust-v1 ([T116](../../board/TASKS.md#t116 "Modulation specialist")) | 1,239 (59 items) | mod 37 (944 renders), plus one comp, delay, EQ and reverb distractor per item | Audio + DI | 43 files; P1 432, P2 496, P3 16 mod renders | Levelled | True plus near misses; two LFO phases | Consumed trust set |
| **mod-specialist** calibration | 3,034 (37 × 41 × 2) | mod 37 | Audio (npy) | **One file: P3_music 04**, two passages | Levelled | Defaults plus 40 random settings (CC 0.05–0.95, all knobs at once) | Also 624 renders on synthetic-devfam items (a dev set; keep out of training). Uses P3 |
| **time-specialist** ([T122](../../board/TASKS.md#t122 "Family dispatch and time-based specialist")) | 3,340 | delay 46 (2,600), mod 11 (440), reverb 5 (300) | Audio (FLAC 24-bit), with `index.jsonl` knobs | **3 excerpts, all P2 scales** | Levelled | Latin hypercube, all knobs moved, about 54–60 per model | `trust/` holds 96 devfam-derived npz (dev set) |
| **linear-tables** ([T117](../../board/TASKS.md#t117 "Linear specialist for cab and EQ")) | 95 tables from 90,991 probe renders | cab 87 of 87 (84,429), EQ 8 of 8 (6,562) | **Not audio**: frequency response, 1/12 octave, 31 Hz–16 kHz, at a grid of knob settings | Synthetic multisine probe | -20 dBFS RMS probe | **Grid**: all 128 CC steps per one-knob group; nodes for interacting groups | Error 0.04–0.17 dB. Could synthesise LTI cab and EQ audio offline |
| presence-v1 ([T102](../../board/TASKS.md#t102 "Block-presence detector for the chain builder")) | 2,016 train + 2,016 test feature rows | helix-trust-v2's 8 categories | Hand features (jsonl); coefficients in `data/presence/` | helix-trust-v2's | Levelled | = helix-trust-v2 | |
| presence-v2 ([T102](../../board/TASKS.md#t102 "Block-presence detector for the chain builder")) | **0 rendered**; 120 chains / 543 block renders planned | CHAINS of 1–7 blocks | Manifest only | helix-trust-v2 test models and files | -26.7 (planned) | Category sets from the chain prior | Status "proposed, not rendered" |
| block-probe (T046/T047) | 603 fingerprints | one per Native model: drive 61, amp 224, cab 87, mod 56, delay 46, reverb 24, EQ 8, comp 17, other 79 | Not audio (fingerprint JSON) | Synthetic probe | probe | Default settings | |
| dfp-v1 ([T174](../../board/TASKS.md#t174 "Distortion measurement step 3"), [T179](../../board/TASKS.md#t179 "Tone-knob ends at 0/0.1/0.9/1 on all drive models")) | 722 + 418 (tone ends) + 60 (pilot) npz | drive 60 / 52 / 6 | Not audio (fingerprints) | Probe | probe | Knob grid | |
| distortion-step4 ([T181](distortion-step4.md)) | 120 truth probes, 120 twins, 5,184 gallery embeddings | drive 54 | No audio: npz and AFx-Rep embeddings | Guitar-TECHS excerpts | Levelled | Drive × Treble grid per model | Its 5,424 renders were reduced to features |
| pedal-probe-sim ([T187](measured-pedal-mode.md)) | 1,782 fingerprint npz (54 targets × 15 inputs) | drive | Not audio | Probe plus excerpt | Includes input-level variants | 54 targets | Simulation only |
| drive-xdi ([T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive")) | 150 targets | drive 25 | Features (`features.pkl`) | 8 DIs, P1 4 / P2 4 | Levelled | True plus knob neighbours | |
| Dev sets: synthetic-devfam / -lindev / -devgm / -devmod2 | 96 / 96 / 72 / 60 targets | Single block; pools of at most 6 models per category (devfam: mod, reverb, delay, amp; devgm items: mod, drive, amp) | Audio targets + DI (labels not read) | Guitar-TECHS | Levelled -26.7 | Random knobs | **Keep out of training** (dev sets for predict-then-search) |
| Sealed: synthetic-v1 / -v2 / -v3 | 96 each, 12 per category | 8 categories | Not opened | Guitar-TECHS | -26.7 | | **Never training data** |
| [T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching") / [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") / [T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option") caches | 24 / 96 / 72 targets | drive | Plans and result rows only; no audio kept | Held-out Guitar-TECHS files; the human's takes ([T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option")) | [T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option") records the level | Random off-grid Drive/Tone | Evaluation targets, never training |
| sep-stress-v1 ([T070](../../board/TASKS.md#t070 "Separation stress curve sep-stress-v1")) | 39 mixes plus truth and separated stems | **Not Helix**: a numpy drive+cab stand-in | Audio | 3 P3 clips | 0 / ±6 dB guitar-to-backing | 3 clips × 5 densities × 3 levels | The only **mixed and separated** audio on disk |
| spot380, blockserial, chain_render | about 49 parity files; about 10 chain checks | Single-block parity; C8 (8 blocks), K, C8p2–p7 chains | Audio | Parity DI | | Fixed | Infrastructure checks. The serial and whole-chain renders null at about -67 dB |
| Preset corpus (`data/presets`, human's local data) | 1,932 .hlx files; 129 parsed into `data/chain_prior` | Chain structures and settings | Presets, not audio | | | Real-world settings | Could seed realistic chains |
| Guitar-TECHS DI (`data/di`) | 104 files: P1 46, P2 46, P3 12 | | Audio, 3.8 GB | | Raw | | CC BY 4.0; P3 is the proposed Gate 3 reserve |

## 3. Gap table: what the chain predictor needs and does not have

| Slot | Gap | What exists | What the predictor needs |
|---|---|---|---|
| all | **No chain training data at all** | 0 chains rendered for training. presence-v2 (120 chains, 543 block-serial renders) is planned but was never rendered. Block-serial rendering works: the 8-block chain nulls at about -67 dB against a whole-chain render. The preset corpus has 1,932 local .hlx files | Chains in the approved order, every slot optional, with labels per slot |
| all | **Nothing mixed with backing and then separated, through Helix** | The helix-trust-v2 "sep" condition is a mask stand-in applied at score time. sep-stress-v1 is not Helix and uses P3 only | Mixes with backing run through htdemucs_6s, so the predictor learns to ignore separation artefacts. The backing source is still open (DECISIONS 2026-10-22) |
| all | **Input-level conventions are mixed** | drive-dataset-v1 and encoder-swap are raw; everything else is levelled to -26.7. This causes a Drive bias of +0.09 to +0.26 across conventions ([T194](drive-anomaly-t194.md)) | One convention, or the input gain as a feature or label |
| all | **Few players** | Training uses only Guitar-TECHS P1 and P2. P3 is meant as the reserve, but it appears in mod-specialist (all of it), helix-trust-v1/-v2 and gain-trust-v1b. The human's takes are test only (one session) | More DI players and styles. Cross-player top-1 is 0.58–0.75, against 0.89–0.93 within player |
| all | **Settings design** | Only drive-dataset-v1 is uniform 0–127. The trust sets cluster around a "true" setting. Level knobs are never varied | Uniform or preset-informed settings; levels varied, or normalised away |
| drive | **Fuzzes are missing from the at-scale set** | drive-dataset-v1 has 54 of 61 models. The 6 fuzzes (Jet, Industrial, Megaphone, Tycoctavia, Wringer, Xenomorph) appear only in drive-specialist (25 settings × 8 DIs, levelled) and the trust sets. Bitcrusher appears only in helix-trust-v1 and is Stomp-checked | The fuzzes at scale. [T200](../../board/TASKS.md#t200 "Blind single-drive matches of five songs the human added to data/songs") Satisfaction (a bass-DI model at p 1.00) shows the blind spot |
| drive | **No amp after the drive** | Every drive render is DI > drive | drive > amp > cab renders. Behind an amp, presence-v1 calls almost every block "drive" |
| amp | **No amp renders at scale** *(superseded 2026-10-23: 119,873 single-amp clips over 224 of 224; still no cab after the amp)* | 1,549 labelled renders over 111 of 224 models: a median of 13 per model, and only 5 models with 50 or more. 113 models (52 of them preamps) have no labelled render in the splits read here; some are in the test splits. Every amp was rendered without a cab | Tens of thousands of renders over all 224 models, or a shortlist the human approves, with a cab after the amp |
| cab | **Few cab renders in audio** *(superseded 2026-10-23: 50,000 single-cab feature clips over 87 of 87; still never after an amp)* | 581 renders over 45 of 87 cabs, on a raw DI, never after an amp. linear-tables cover all 87, but as responses, not audio | Cabs after amps. Since cabs are LTI, the table responses could be convolved onto amp renders offline (proposal, not measured) |
| mod | **19 of 56 models missing; narrow DIs** *(narrow DIs superseded 2026-10-23: 11,985 clips of the 11 stand-ins on 92 DI files; the 19 missing models remain)* | 4,893 renders over 37 models. The 3,034 mod-specialist renders all use one P3 file. Missing: the ring mods, rotary drums, panners, Barberpole, Dimension, frequency shifter, PolyChorus, Sample and Hold, Sweeper, Tape Eater, Warble-Matic, Retro Reel, Double Double and Dynamix Flanger. Native stands in only for the tremolos, 70s Chorus, Bubble Vibrato and MM4 Analog Chorus | Many DIs with random LFO phase, plus a stand-in ruling or Stomp renders for the rest |
| delay | **One player, 3 excerpts** *(superseded 2026-10-23: 44,683 clips over 43 models, P1 and P2, 92 DI files)* | 3,194 renders over 46 of 47 models; time-specialist's 2,600 use 3 P2 excerpts. Shuffling Delay is missing. It and ADT are time-variant. Native moves Time in 63 ms steps | Many DIs and both players, with a narrower Time range |
| reverb | **19 of 25 models missing** *(still true 2026-10-23; the 5 stand-ins now have 6,000 clips)* | 847 renders over 6 models (Room, Hall, Plate, '63 Spring, Chamber, plus Ganymede). Only the first 5 are Native stand-ins. Octo is time-variant | The other 19, which needs a stand-in ruling or Stomp renders |
| EQ | Small audio set *(superseded 2026-10-23: 8,000 single-EQ clips over 8 of 8)* | 508 renders over all 8 models, plus full response tables | EQ at the end of a chain; the tables could synthesise this offline |
| comp | Small, and level-dependent | 510 renders over 17 of 17; no dynamics specialist | Input-level labels matter most here |

## 4. Predictors trained so far (all drive-only except presence-v1; nothing adopted)

*Superseded 2026-10-23 in part: per-slot heads for amp, cab, mod, delay, reverb and EQ now exist (Update U2). The drive numbers below are unchanged.*

Shared terms:
- **Within player:** held-out DI recordings of known players (P1/P2).
- **Cross player:** train on one Guitar-TECHS player, test on the other.
- **Drive ±0.15** and **Tone ±0.10** are scored with the true model given, then with the head's own top-1 model ("true / own").
- Chance is 0.017 for model top-1, 0.083 for top-5 and 0.067 for family.

### 4.1 presence-v1 ([T102](../DECISIONS.md), 2026-10-17, tone-metrics)
- **Architecture:** hand features (transfer spectrum, echo and decay measures, modulation, gain) with a one-vs-rest logistic regression per category, in known-DI and no-DI modes.
- **Outputs:** presence of drive, amp, cab, comp, EQ, mod, delay and reverb (plus drive_or_amp), and rule-based parameters (delay time, LFO rate, RT60, gain index).
- **Training:** helix-trust-v2 train split, 96 items × 20 single blocks plus the DI (2,016 targets). L2 chosen by 4-fold CV grouped by item.
- **Checkpoint:** `data/presence/presence-v1-{known_di,no_di}.json`.
- **Test:** helix-trust-v2 test, models disjoint except reverb; 1,950 scored.
  - Known-DI, precision/recall: cab 0.89/0.95 (pass), drive_or_amp 0.82/0.87, mod 0.71/0.69, reverb 0.60/0.68, delay 0.47/0.78, comp 0.74/0.49, EQ 0.63/0.65, amp 0.58/0.75, drive 0.47/0.73. Macro F1 0.66.
  - No-DI F1: drive_or_amp 0.85 (pass), delay 0.39, reverb 0.31, mod 0.28.
  - Chains (spot380 diagnostic): behind an amp and cab, the extra block is called "drive" almost always.
- **Status:** a soft prior only (lead, 2026-10-17).
- **Ledger:** `benchmark / native / presence-v1`, 30 rows, [2026-10-17T17:21:45](../../ledger/runs.csv?plain=1#L776 "benchmark · native · presence-v1 · macro_f1_known_di = 0.6616 (first of 10 rows at this time)") (10 rows)Z.

### 4.2 predictor-pilot-v0, the [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") CNN ([note](predictor-pilot-v0.md))
- **Architecture:** log-mel CNN trained from scratch. 128 bands, 20 ms hop, per-clip RMS normalisation, 6 conv blocks, 256-d embedding, 1.49 M parameters, 23 min on MPS.
- **Outputs:** family (15), model (60, hierarchical), and Drive/Tone as a mean and log-sigma.
- **Training data:** 14,640 cached renders: drive-specialist 12,000, helix-trust-v2 train 211, gain-trust-v1 1,592, gain-trust-v1b 837. All levelled, P1/P2.
  - Split by DI file: train 38 files / 8,711 clips, cal 5 / 1,583, test 29 / 4,337.
- **Calibration:** on one calibration DI: temperature 2.6, sigma × 5.40 for Drive (over-corrects) and × 1.75 for Tone.
- **Checkpoints:** `cache/models/predictor-pilot-v0/{all,xplayer}/model.pt`.
- **Within player:** top-1 0.436, top-5 0.764, family 0.588. Drive 0.636 / 0.503, Tone 0.468 / 0.407.
- **Cross player (P2→P1):** top-1 0.130, top-5 0.310, family 0.200.
- **AFx-Rep plus logistic regression on the same split:** 0.563 / 0.899 / 0.648, and P2→P1 0.421.
- **Ledger:** `benchmark / - / predictor-pilot-v0`, 17 rows, [2026-10-19T06:10:41](../../ledger/runs.csv?plain=1#L1506 "benchmark · - · predictor-pilot-v0 · cnn_model_top1 = 0.4356 (first of 4 rows at this time)") (4 rows)Z.

### 4.3 predictor-pilot-v1, the [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1") AFx-Rep heads ([note](predictor-pilot-v1.md))
- **Encoder:** frozen AFx-Rep `mid` (512-d), multi-crop (3 × 3.33 s, predictions averaged).
- **Heads:**
  - logreg: a flat model logistic regression summed into the family, a per-family model logistic regression, and a ridge per model for Drive/Tone;
  - MLP: 2 × 256 with dropout 0.3 and a Gaussian knob head; 3 seeds.
- **Data:** the same as [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk").
- **Calibration:** out-of-fold over 43 files, with players and files equally weighted. One temperature and one sigma scale per knob.
- **Checkpoints:** `cache/models/predictor-pilot-v1/heads_main_*.pkl`.
- **Within player, multi-crop:**
  - logreg: 0.68 / 0.96 / 0.74, Drive 0.64 / 0.55, Tone 0.49 / 0.41;
  - MLP: 0.73 / 0.97 / 0.76, Drive 0.66 / 0.58, Tone 0.56 / 0.47.
- **Cross player:** P2→P1 0.48 (logreg) and 0.53 (MLP); P1→P2 0.36–0.42.
- **Ledger:** `benchmark / - / predictor-pilot-v1`, 23 rows, [2026-10-19T09:12:45](../../ledger/runs.csv?plain=1#L1638 "benchmark · - · predictor-pilot-v1 · main_logreg_single_model_top1 = 0.562 (first of 10 rows at this time)") (10 rows)Z.

### 4.4 predictor-dataset-v1, the [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") v1data/v2data logreg and MLP ([note](drive-dataset-v1.md), `cache/drive-dataset-v1/summary-150k.md`)
- **Architecture:** the [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1") heads, multi-crop. 54 models; the 6 fuzzes are masked.
- **Data arms:**
  - v1data: the [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") train set only (8,711 clips, levelled);
  - v2data: plus 105,181 drive-dataset-v1 train clips (raw level). Calibration adds 6,516 new clips. P1/P2.
- **Calibration:** as [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1"). The [T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option") matcher uses temperature 0.77 and sigma scales 0.92 (Drive) and 0.79 (Tone).
- **Checkpoints:** `cache/models/predictor-dataset-v1/heads_main_{v1data,v2data}_{logreg,mlp}_multi.pkl`. The **v2data MLP is the predictor used in [T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option") and [T200](../../board/TASKS.md#t200 "Blind single-drive matches of five songs the human added to data/songs")**.
- **Test sets:**
  - the old [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") test: 4,337 clips, 29 files;
  - newtest: 19,012 clips on 18 files no head had seen;
  - v1files_new: 18,989 new renders on the 29 old test files.

| Head | Old test top-1 / top-5 / family | newtest top-1 / top-5 / family | newtest Drive true / own | newtest Tone true / own |
|---|---|---|---|---|
| v1data logreg | 0.68 / 0.96 / 0.74 | 0.54 / 0.84 / 0.65 | 0.48 / 0.42 | 0.47 / 0.37 |
| v1data MLP | 0.72 / 0.96 / 0.77 | 0.55 / 0.85 / 0.66 | 0.48 / 0.42 | 0.46 / 0.38 |
| v2data logreg | 0.83 / 0.97 / 0.86 | 0.88 / 0.99 / 0.91 | 0.71 / 0.66 | 0.65 / 0.60 |
| **v2data MLP** | **0.89 / 0.98 / 0.91** | **0.93 / 1.00 / 0.94** | **0.73 / 0.71** | **0.70 / 0.67** |

- **Cross player, v2data MLP:**
  - P2→P1: old 0.59, newtest 0.75 (top-5 0.94), Drive 0.59 / 0.54;
  - P1→P2: old 0.65, newtest 0.62, Drive 0.65 / 0.55.
- **Known flaw:** on the old renders, Drive is worse (0.53 within player, 0.29 P1→P2). [T194](../../board/TASKS.md#t194 "Explain the Drive anomaly") traced this to the input-level convention.
- **Ledger:** `benchmark / - / predictor-dataset-v1`, 36 rows, [2026-10-20T14:33:35](../../ledger/runs.csv?plain=1#L1681 "benchmark · - · predictor-dataset-v1 · main_v1data_logreg_v1test_old_model_top1 = 0.676")Z.

### 4.5 encoder-swap-v1, the [T195](../../board/TASKS.md#t195 "Permissive encoders PANNs and DAC vs AFx-Rep on a drive-dataset-v1…") encoder swaps ([note](encoder-swap-t195.md))
- **Setup:** the same heads on frozen AFx-Rep (as in [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render"), or z-scored), DAC-24k (MIT) or PANNs Cnn14 (CC BY 4.0).
- **Data:** a re-render of 27,000 train and 6,516 cal clips of drive-dataset-v1 (raw level, P1/P2). Test: newtest and v1files_new.
- **Checkpoints:** `cache/models/encoder-swap-t195/heads_main_{afx_l2,afx_z,panns_z,dac_z}_{logreg,mlp}.pkl`. They hold no encoder weights.
- **Model top-1, MLP, newtest:**
  - within player: AFx-Rep 0.89, DAC 0.79, PANNs 0.38;
  - P2→P1: 0.72, 0.49, 0.24;
  - P1→P2: 0.58, 0.52, 0.19.
- **DAC minus AFx-Rep, paired:** within -0.10 [-0.11, -0.08], P2→P1 -0.23 [-0.26, -0.20], P1→P2 -0.07 [-0.09, -0.05]. On Drive with the true model, DAC is +0.03 [+0.01, +0.04].
- **Ledger:** `benchmark / - / encoder-swap-v1`, 48 rows, [2026-10-21T03:33:36](../../ledger/runs.csv?plain=1#L1868 "benchmark · - · encoder-swap-v1 · main_afx_l2_logreg_newtest_model_top1 = 0.8407 (first of 12 rows at this time)") (12 rows)Z.

### 4.6 The [T194](../../board/TASKS.md#t194 "Explain the Drive anomaly") input-gain retrain ([note](drive-anomaly-t194.md))
- **Arms:** the [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") MLP, one seed, refit on existing features:
  - v1: old data only;
  - v2: all new data;
  - v2rand: a random 15,494 new clips;
  - v2lev: 15,494 new clips with the DI within 3 dB of the reference;
  - **v2cond:** all new data, with the DI gain in dB/10 appended to each crop.
- **Calibration:** on the calibration files only.
- **No checkpoint saved:** results only, in `cache/drive-anomaly-t194/result.json`.

| Arm, within player | Drive ±0.15, old test | Drive ±0.15, newtest |
|---|---|---|
| v1 | 0.667 | 0.478 |
| v2 | 0.534 | 0.731 |
| v2lev | 0.688 | 0.486 |
| **v2cond** | **0.703** | **0.771** |

- v2cond's model top-1 is 0.91 on the old test and 0.93 on newtest.
- P1→P2 v2cond: old test Drive 0.323, still with a +0.22 bias. Level conditioning does not fix the bias for a new player.
- **Ledger:** `benchmark / - / predictor-dataset-v1-anomaly`, 6 rows, [2026-10-20T16:49:09](../../ledger/runs.csv?plain=1#L1717 "benchmark · - · predictor-dataset-v1-anomaly · main_v2data_mlp_v1test_old_drive_bias = 0.094 (first of 4 rows at this time)") (4 rows)Z.

### 4.7 The predictor in use
- **[T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option"), predictor-first matcher** ([note](predictor-first-t196.md); ledger `search / native / predictor-first-v1`, 120 rows). The v2data MLP picks up to 6 models, the matcher makes 5 confirming renders for each, and AFx-Rep orders them.
  - Guitar-TECHS blind (48 targets): top-1 85 %, top-3 100 %, family 90 %, Drive 26/41, all knobs right 40 %, about 10 renders.
  - The human's takes blind (24 targets): top-1 67 %, top-3 88 %, Drive 6/16, all knobs right 17 %.
- **[T200](../../board/TASKS.md#t200 "Blind single-drive matches of five songs the human added to data/songs"), five real songs, blind, no DI** ([note](five-songs-t200.md); ledger `search / native / song-<id>`; verdict rows `tone-matcher-lead / ab / stomp`).

| Song | Predictor top-1 (p) | Final pick | Human, live on the Stomp (informal) |
|---|---|---|---|
| teen-spirit | Zero Amp Bass DI (0.82) | Zero Amp Bass DI | "pretty off" |
| territorial | Tube Drive (0.39; a three-way split) | Classic Distortion (AFx-Rep order) | "the closest so far" |
| seek-destroy | Heavy Distortion (0.99) | Heavy Distortion | "pretty off" |
| satisfaction | Zero Amp Bass DI (1.00) | Zero Amp Bass DI | "somewhat close" |
| cherub-rock | KillerZ (0.69) | KillerZ | "somewhat close" |

Satisfaction is a fuzz record, and the predictor has never seen the six fuzzes. Every song needs a chain; the predictor knows single drive blocks only.

### 4.8 Other learned components (specialists, not chain predictors)
- **time-specialist:** two logistic heads (four-way delay/reverb/mod/other, 0.88 in grouped CV) and a per-model GP regressor for knob seeds, on 60 renders per model.
- **mod-specialist:** kNN seeds from 3,034 calibration renders.
- **linear specialist:** least-squares knob fit on the response tables.
- **distortion-step4 twin fit** and **measured-pedal mode** ([T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…"), [T187](../../board/TASKS.md#t187 "Measured-pedal mode preparation")): fingerprint estimators that need a probe or a DI.

## 5. Samples (train or calibration splits only)

CC values are 0–127 steps of each knob's template range. "Gain to reference" is the gain the levelling applied to bring the DI to -26.7 dBFS.

| Slot | Source | Model | Knobs (CC) | DI file, start | Player | Level |
|---|---|---|---|---|---|---|
| drive | drive-dataset-v1 train | KillerZ | Contour 20, Dist 30, Gain 4, Mid Amt 101, Mid Freq 46 (Drive label 0.236, Tone 0.157) | P2_chords Set2_dim, 130.0 s | P2 | raw, -38.9 dBFS (would need +12.2 dB) |
| drive | drive-dataset-v1 train | HD2_DistRegalBassDI | Bass 63, Treble 71, Volume 119 | P2_chords Drop3_m7, 11.0 s | P2 | raw, -30.1 dBFS (+3.4) |
| drive | drive-dataset-v1 train | HD2_DistTriangleFuzz | Sustain 110, Tone 2 | P1_chords Set1_min, 18.5 s | P1 | raw, -33.5 dBFS (+6.8) |
| drive | drive-dataset-v1 train | HD2_DistPrizeDrive | Drive 1, Spectrum 96 | P1_chords Set4_min, 60.5 s | P1 | raw, -35.3 dBFS (+8.6) |
| drive | gain-trust-v1 fit | HD2_DM4JumboFuzz | Bass 72, Drive 79, Mid 59, Output 25, Treble 110 | P1_techniques PalmMute, 72 s | P1 | levelled (+8.19 dB) |
| amp | gain-trust-v1 fit | HD2_AmpEVPanamaBlue | Bias 15, BiasX 77, Gain 52, High 100, Low 99, Master 68, Mid 53, Presence 19, Sag 98 | P2_chords Set4_min, 88 s | P2 | levelled (+7.92) |
| amp | gain-trust-v1 fit | HD2_PreampEVPanamaBlue | Gain 81, High 23, Low 109, Master 55, Mid 71, Sag 70 | P2_chords min, 24 s | P2 | levelled (+11.74) |
| amp | helix-trust-v2 train | HD2_PreampMatchstickCh1 | Bass 35, Ch1Drive 43, Master 28, Sag 25, Treble 55 | P1_chords min, 124 s | P1 | levelled (+6.69) |
| amp | helix-trust-v2 train | HD2_AmpEssexA30 | Bass 13, Bias 85, BiasX 68, Cut 83, Drive 85, Master 103, Presence 20, Sag 91, Treble 37 | P2_techniques Harmonics, 68 s | P2 | levelled (+6.54) |
| cab | helix-trust-v2 train | HD2_CabMicIr_1x12OpenCast | Angle 13, Distance 84, HighCut 36, LowCut 27, Position 44 | P1_chords m7, 20 s | P1 | levelled (+10.59) |
| cab | helix-trust-v2 train | HD2_CabMicIr_2x12SilverBell | Angle 43, Distance 101, HighCut 72, LowCut 41, Position 50 | P1_chords Maj7, 72 s | P1 | levelled (+7.42) |
| cab | helix-trust-v1 | HD2_Cab4x12UberV30 | Distance 75, HighCut 84, LowCut 89 | P1_scales Db, 136 s | P1 | levelled (+3.4) |
| mod | time-specialist | HD2_Chorus70sChorus | ChorusIntensity 23, Headroom 87, Mix 90 | P2_scales Gb, 124 s | P2 | levelled (+2.91) |
| mod | helix-trust-v2 train | HD2_TremoloTremolo | DutyCycle 69, Intensity 61, Speed 23 | P1_chords min, 52 s | P1 | levelled (+7.37) |
| mod | mod-specialist | HD2_MM4AnalogChorus | Depth 85, Mix 87, Speed 70, Tone 29 | P3_music 04, 12 s | P3 | levelled |
| mod | helix-trust-v1 | HD2_PhaserDeluxePhaser | Depth 26, Feedback 9, Mix 20, Offset 45, Rate 56 | P2_chords min, 88 s | P2 | levelled (+9.73) |
| delay | time-specialist | HD2_DL4AnalogDelayStereo | Bass 61, Feedback 94, Mix 46, Time 63, Treble 48 | P2_scales Gb, 124 s | P2 | levelled (+2.91) |
| delay | time-specialist | HD2_DL4DigDelay | Bass 105, Mix 43, Time 66, Treble 93 | P2_scales Gb, 124 s | P2 | levelled (+2.91) |
| delay | helix-trust-v2 train | HD2_DelayVintageDigital | Depth 24, Mix 42, Rate 25, Time 51 | P1_scales Ab, 52 s | P1 | levelled (+4.21) |
| reverb | time-specialist | HD2_ReverbHall | Decay 33, HighCut 92, LowCut 103, Mix 114, Predelay 102 | P2_scales Gb, 124 s | P2 | levelled (+2.91) |
| reverb | helix-trust-v2 train | HD2_ReverbChamber | Decay 61, HighCut 70, LowCut 23, Mix 73, Predelay 92 | P1_chords min, 32 s | P1 | levelled (+6.06) |
| reverb | helix-trust-v2 train | HD2_Reverb63Spring | Decay 68, HighCut 38, LowCut 111, Mix 32, Predelay 76 | P2_scales Bb, 48 s | P2 | levelled (+11.62) |
| EQ | helix-trust-v2 train | HD2_CaliQ | 80Hz 80, 240Hz 110, 750Hz 93, 2200Hz 33, 6600Hz 66 | P2_chords m7b5, 4 s | P2 | levelled (+2.09) |
| EQ | helix-trust-v2 train | HD2_EQLowCutHighCut | HighCut 85, LowCut 18 | P1_techniques Harmonics, 68 s | P1 | levelled (+14.05) |
| EQ | helix-trust-v2 train | L6SPB_AcousGtrSim | Shimmer 106, Top 92 | P1_chords min, 24 s | P1 | levelled (+6.47) |
| comp | helix-trust-v2 train | HD2_CompressorOptoComp | Blend 94, Compression 77, Release 106 | P1_chords dim, 140 s | P1 | levelled (+4.86) |
| comp | helix-trust-v1 | HD2_CompressorLAStudioComp | Emphasis 54, Mix 78, PeakReduction 104 | P1_chords m7b5, 32 s | P1 | levelled (+5.54) |
| comp | helix-trust-v2 train | HD2_CompressorRedSqueeze | Mix 41, Sensitivity 74 | P2_techniques Bendings, 20 s | P2 | levelled (+8.65) |

## 6. Size and disk footprint

| What | Size |
|---|---|
| Labelled training audio (drive-specialist 21G, helix-trust-v2 7.2G, mod-specialist 6.5G, gain-trust-v1 6.2G, helix-trust-v1 4.5G, time-specialist 4.2G, gain-trust-v1b 2.3G, mod-trust-v1 2.3G) | about 54 GB |
| Feature-only sets (drive-dataset-v1 0.69 GB, encoder-swap-t195 1.6 GB) | about 2.3 GB for 221,215 clip-feature sets |
| Tables and fingerprints (linear-tables 62M, probe 52M, distortion-step4 40M, drive-xdi 18M, pedal-probe-sim 14M, distortion-fp 10M, presence-v1 29M) | about 0.23 GB |
| Dev sets (keep out of training) | 3.1 GB |
| Sealed sets (never training) | 3.5 GB |
| Guitar-TECHS DI | 3.8 GB |
| Encoder weights (AFx-Rep, DAC, PANNs, HTDemucs) | 2.2 GB |
| sep-stress-v1 (not Helix) | 7.7 GB |

On the drive-dataset-v1 format (features only, about 4 KB per clip), 100,000 renders take about 0.5 GB. On the drive-specialist format (10 s float32 audio, about 1.9 MB each), they would take about 190 GB.

## 7. Notes for the next step (proposals; nothing adopted)

1. **The chain predictor's first data job is chains, not more single drives.** *(2026-10-23: the single-block slot sets have since been rendered, and chains are still at zero; see the Update.)* Drive is the only slot at scale, and only as a single block on a raw DI. Amp, cab and reverb are short by one to two orders of magnitude, and chains and separated mixes are both at zero.
2. **Choose the level convention before any new render** ([T194](../../board/TASKS.md#t194 "Explain the Drive anomaly") option a or b). The human chooses.
3. **Some coverage needs no Native time:**
   - cab and EQ by convolving the linear-table responses onto amp renders (they are LTI);
   - realistic chain structures and settings from the 1,932-preset corpus and the chain prior.

   Neither has been measured.
4. **The model shortlist per slot, the backing source and the bars are the human's open decisions** (DECISIONS 2026-10-22). The missing-model lists in `coverage.json` are the input for the shortlist.
