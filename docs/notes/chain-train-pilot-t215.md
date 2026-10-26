# chain-train-v1: does training on finished chains fix the heads? ([T215](../../board/TASKS.md#t215 "Chain training pilot"))

Owner: learning. Run 2026-10-23. Approved by the human the same day ("yes start pilot"; DECISIONS, "chain training pilot approved ([T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") option A)"). **This is a measurement only. There is no pass bar and nothing is adopted.** Follows [T214](../../board/TASKS.md#t214 "Chain transfer test") (`chain-transfer-t214.md`), which showed the single-block heads fail inside chains.

## Answer

**Chain training helps the slots that were already partly working, and does not rescue the drive or the amp at this size.**

- **Amp: still at chance.** 4,472 chain-rendered clips over 224 amps (about 20 per model) give top-1 0.02 and top-3 0.04 (chance 0.004 / 0.013). The early amp numbers in the training log were near chance and they stayed there: half the chains (0.037 top-3) and all of them (0.044) are inside each other's intervals, and adding the 84,219 single-amp clips does not move it (0.037). The zero-render oracle (true cab id appended) adds nothing (0.024 top-3). Nothing in this pilot says that an amp shortlist can be learned from finished-chain audio at a few thousand chains.
- **Drive: 3× chance, flat learning curve.** 3,115 chain clips over 54 drives: top-1 0.08, top-3 0.18 (chance 0.019 / 0.056). Half → full moved top-3 only 0.16 → 0.18. Mixing in the 105,181 single-drive clips makes it worse on chains (0.15) while keeping single-block accuracy at 0.93.
- **Cab: the clearest gain.** Top-3 0.40 ([T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") head) → 0.48 chain-only → **0.60 single + chain**; top-1 0.25 → 0.32 → 0.39. The half → full curve still rises (0.40 → 0.48).
- **Mod and EQ gain** (small n): mod top-3 0.45 → 0.67 / 0.70 (n 66, chance 0.27); EQ 0.42 → 0.74 (n 57, chance 0.375; the half point already had 0.72).
- **Delay** 0.19 → 0.22 chain-only, 0.33 single + chain (n 102, chance 0.07). **Reverb** unchanged (0.68; 5 models, chance 0.60).
- **Presence** of the optional slots is detectable but weak: accuracy 0.73–0.87 against majority baselines of 0.65–0.81, balanced accuracy 0.62–0.72. The heads mostly say "absent" (TPR 0.26–0.44 for mod, delay, reverb, EQ) except for the drive, where they mostly say "present" (TPR 0.94, TNR 0.45).
- **Knobs** stay at about the constant-guess level (0.26–0.46 within ±0.15) in every slot except reverb Mix (0.56–0.61) and mod Intensity (0.64–0.71).

The research survey's own scaling criterion (`research-full-chain-2026-10-23.md`, ranked option 1: "scale only if chain-trained drive and amp top-3 exceed 5× chance on the 295-chain test and oracle cab conditioning adds ≥ 0.1 to amp top-3") is **not met**: drive is 3.2× chance, amp 3.3×, and the oracle adds 0.00. That is a reading, not a ruling; the human decides.

## Method

- **Chains.** `scripts/chain_train.py plan`: 5,000 chains from the [T214](../../board/TASKS.md#t214 "Chain transfer test") generator with seed 215, the same slot probabilities (drive 0.7, amp 1, cab 1, mod 0.2, delay 0.3, reverb 0.3, EQ 0.2), the same model pools (drive 54, amp 224, cab 87, mod 11, delay 43, reverb 5, EQ 8), the approved knob sampling (uniform CC grid, level knobs at default) and the 0.5–2 s lead-in for time effects. Planned blocks: 18,550 (3.71 per chain). Shuffled, in 20 shards of 250.
- **DI.** 40 drive-dataset-v1 `train` split files only (P1 2,429 chains, P2 2,571). The 18 test files that the [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") test set uses were never read; neither was cal nor P3. `run()` asserts both.
- **Render.** The [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") block-serial path on Helix Native: 4 hosts, fresh pool per stage, nice 10, busy-core LoadGuard, -240 dBFS floor, one retry, digital-zero chains dropped. **18 of 20 shards finished in the 2 h budget: 4,472 chains, 16,547 block renders, 7,117 s (1.59 s per chain, 0.43 s per block, AFx-Rep embedding of the final output included).** 28 chains dropped. Ledger `render-cost / native / chain-train-v1 wall_s_per_chain 1.591`. The last two shards were not rendered; nothing was re-rendered for this report.
- **Features.** Frozen AFx-Rep `mid`, three 3.33 s crops of the finished chain output (lead-in dropped); no audio kept.
- **Heads.** Per slot, the [T189](../../board/TASKS.md#t189 "AFx-Rep heads") heads (`afx_heads.LinearHeads` alpha 1, `MLPHeads` 256-wide, 60 epochs, multi-crop), one knob column per drawn knob, temperature and sigma calibrated on out-of-fold predictions grouped by DI file (5 folds chain-only, 3 folds mixed, calibration pool = chain clips). Presence: one logistic regression and one multi-label MLP (sigmoid per optional slot), chain clips only.
- **Arms.** `chain`: only the chains that contain the slot. `single+chain`: the slot's single-block `train` clips (drive-dataset-v1; slot-dataset-v1 incl. amp refill) plus the chain clips. Learning curve: the first 9 shards (2,234 chains), chain-only.
- **Oracle (research-full-chain 2(b)).** The amp head, chain-only arm on all 4,472 chains, with the chain's TRUE cab model as an 87-way one-hot appended to each crop embedding (scale 1). Scored with the true cab (oracle) and, as a control, with a random cab (seed 215). Zero renders.
- **Scoring.** Exactly [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")")'s (`chain_transfer.item_scores` / `aggregate` / `contexts`) on the same 295 [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") test chains: model top-1/3 with Wilson 95 %, family top-1, knobs within ±0.15 of the true position (true model, pooled over drawn knobs, bootstrap over chains), presence accuracy at 0.5 with Wilson 95 %, balanced accuracy and the majority baseline. The same heads are also scored on each slot's single-block held-out `test` clips (same 18 files) to see what chain training costs there.
- **Run.** nice 15, 2 torch threads, CPU. The first training run was terminated after the amp single+chain logreg fit; the rerun (`train-1023-resume.log`) reused the 21 finished fits from `results.json` (a fit is reused only when its pickle is on disk) and fitted the rest. Wall for the fits: 5,271 s in total, 3,288 s of it the amp single+chain MLP (88,691 clips × 3 folds). proc_check: 0 orphans before and after.

## Results: model identification on the [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") test chains (MLP head)

Chance is 1/M for top-1 and 3/M for top-3. "[T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") head" is the single-block head scored on the same chains in [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")").

| Slot | Models | n | Chance top-1 / top-3 | [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") head top-1 / top-3 | Chain-only, half (2,234 chains) | Chain-only, full (4,472), 95 % | Single + chain, full, 95 % | Family top-1 chain-only / single+chain | Knobs ±0.15 chain-only (95 %) / single+chain ([T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") head) |
|---|---|---|---|---|---|---|---|---|---|
| drive | 54 | 206 | 0.019 / 0.056 | 0.02 / 0.10 | 0.06 / 0.16 | 0.08 (0.05–0.12) / 0.18 (0.13–0.24) | 0.05 (0.03–0.09) / 0.15 (0.10–0.20) | 0.20 / 0.20 | 0.29 (0.26–0.33) / 0.28 (0.35) |
| amp | 224 | 295 | 0.004 / 0.013 | 0.01 / 0.02 | 0.02 / 0.04 | 0.02 (0.01–0.04) / 0.04 (0.03–0.07) | 0.02 (0.01–0.04) / 0.04 (0.02–0.07) | 0.04 / 0.04 | 0.32 (0.30–0.34) / 0.31 (0.31) |
| cab | 87 | 295 | 0.011 / 0.034 | 0.25 / 0.40 | 0.24 / 0.40 | 0.32 (0.27–0.38) / 0.48 (0.43–0.54) | 0.39 (0.33–0.44) / 0.60 (0.54–0.65) | 0.35 / 0.43 | 0.43 (0.40–0.46) / 0.46 (0.43) |
| mod | 11 | 66 | 0.091 / 0.273 | 0.21 / 0.45 | 0.30 / 0.61 | 0.41 (0.30–0.53) / 0.67 (0.55–0.77) | 0.41 (0.30–0.53) / 0.70 (0.58–0.79) | 0.41 / 0.41 | 0.40 (0.33–0.48) / 0.46 (0.37) |
| delay | 43 | 102 | 0.023 / 0.070 | 0.10 / 0.19 | 0.08 / 0.17 | 0.13 (0.08–0.21) / 0.22 (0.15–0.31) | 0.14 (0.08–0.22) / 0.33 (0.25–0.43) | 0.13 / 0.14 | 0.26 (0.22–0.29) / 0.30 (0.32) |
| reverb | 5 | 80 | 0.200 / 0.600 | 0.28 / 0.68 | 0.20 / 0.66 | 0.29 (0.20–0.39) / 0.68 (0.57–0.77) | 0.28 (0.19–0.38) / 0.64 (0.53–0.73) | 0.29 / 0.28 | 0.38 (0.33–0.43) / 0.38 (0.32) |
| EQ | 8 | 57 | 0.125 / 0.375 | 0.12 / 0.42 | 0.47 / 0.72 | 0.39 (0.27–0.52) / 0.74 (0.61–0.83) | 0.37 (0.26–0.50) / 0.74 (0.61–0.83) | 0.39 / 0.37 | 0.35 (0.29–0.42) / 0.39 (0.34) |

Training chain clips per slot (full): drive 3,115, amp 4,472, cab 4,472, mod 879, delay 1,331, reverb 1,358, EQ 920. Single-block clips added in the mixed arm: drive 105,181, amp 84,219, cab 35,044, mod 8,351, delay 31,412, reverb 4,176, EQ 5,664.

Logistic regression, top-1 / top-3:

| Slot | [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") single-block logreg on chains | Chain-only half | Chain-only full | Single + chain full |
|---|---|---|---|---|
| drive | 0.04 / 0.11 | 0.06 / 0.13 | 0.09 / 0.15 | 0.04 / 0.12 |
| amp | 0.01 / 0.02 | 0.02 / 0.05 | 0.03 / 0.05 | 0.03 / 0.04 |
| cab | 0.23 / 0.35 | 0.28 / 0.42 | 0.32 / 0.50 | 0.35 / 0.50 |
| mod | 0.33 / 0.56 | 0.38 / 0.61 | 0.42 / 0.68 | 0.44 / 0.62 |
| delay | 0.10 / 0.21 | 0.08 / 0.22 | 0.14 / 0.28 | 0.09 / 0.28 |
| reverb | 0.30 / 0.69 | 0.31 / 0.69 | 0.33 / 0.66 | 0.31 / 0.65 |
| EQ | 0.12 / 0.37 | 0.39 / 0.72 | 0.44 / 0.74 | 0.33 / 0.70 |

The linear head is as good as the MLP on chains everywhere; for drive and delay it is slightly better chain-only. With a few thousand clips the MLP has no advantage.

## Half → full learning curve (chain-only MLP, top-3)

| Slot | Half (2,234 chains) | Full (4,472) | Change | Reading |
|---|---|---|---|---|
| drive | 0.16 | 0.18 | +0.02 | flat, inside the interval |
| amp | 0.04 | 0.04 | +0.01 | flat at chance |
| cab | 0.40 | 0.48 | +0.08 | still rising |
| mod | 0.61 | 0.67 | +0.06 | rising (n 66) |
| delay | 0.17 | 0.22 | +0.05 | rising, low |
| reverb | 0.66 | 0.68 | +0.02 | flat at ~chance (0.60) |
| EQ | 0.72 | 0.74 | +0.02 | flat; the gain was there at half |

Two points do not make a curve. What they say is that doubling the chains did not move the drive or the amp, and that the cab, mod and delay heads had not saturated.

## What chain training costs on single blocks (the same heads on the single-block held-out `test` clips, top-1 / top-3)

| Slot | n clips | [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") single-block head | Chain-only MLP | Single + chain MLP | Single + chain logreg |
|---|---|---|---|---|---|
| drive | 19,012 | 0.93 / 0.99 | 0.09 / 0.20 | 0.93 / 0.99 | 0.67 / 0.92 |
| amp | 15,191 | 0.54 / 0.80 | 0.01 / 0.04 | 0.54 / 0.79 | 0.36 / 0.60 |
| cab | 6,445 | 0.76 / 0.91 | 0.31 / 0.52 | 0.76 / 0.91 | 0.68 / 0.84 |
| mod | 1,584 | 0.75 / 0.91 | 0.37 / 0.70 | 0.75 / 0.90 | 0.72 / 0.88 |
| delay | 5,657 | 0.56 / 0.74 | 0.11 / 0.21 | 0.56 / 0.74 | 0.51 / 0.70 |
| reverb | 765 | 0.47 / 0.81 | 0.32 / 0.67 | 0.49 / 0.79 | 0.44 / 0.80 |
| EQ | 987 | 0.82 / 0.99 | 0.31 / 0.64 | 0.80 / 0.99 | 0.73 / 0.97 |

The mixed arm keeps the single-block numbers in every slot. The chain-only heads do not transfer back to single blocks (the mirror of [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")")).

## Presence of optional slots ([T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") test chains; chain clips only)

| Slot | present / n | Majority | Logreg acc (95 %) | Logreg balanced | MLP acc (95 %) | MLP balanced | MLP TPR / TNR |
|---|---|---|---|---|---|---|---|
| drive | 206 / 295 | 0.70 | 0.80 (0.75–0.84) | 0.72 | 0.79 (0.74–0.83) | 0.69 | 0.94 / 0.45 |
| mod | 66 / 295 | 0.78 | 0.87 (0.82–0.90) | 0.71 | 0.87 (0.83–0.91) | 0.72 | 0.44 / 1.00 |
| delay | 102 / 295 | 0.65 | 0.69 (0.64–0.74) | 0.59 | 0.73 (0.67–0.77) | 0.63 | 0.31 / 0.94 |
| reverb | 80 / 295 | 0.73 | 0.81 (0.76–0.85) | 0.70 | 0.82 (0.78–0.86) | 0.70 | 0.42 / 0.97 |
| EQ | 57 / 295 | 0.81 | 0.84 (0.79–0.88) | 0.60 | 0.83 (0.79–0.87) | 0.62 | 0.26 / 0.97 |

Half the chains gave the same numbers within 0.03 (`results.md`). Accuracy above the majority baseline is 0.05–0.10 for drive, mod, reverb and delay and 0.02 for EQ. When a mod is detected it is almost always there (TNR 1.00), but 56 % of mods are missed. A delay is missed 69 % of the time. As a structure predictor this would need a much lower threshold or many more chains.

## Breakdowns by what else is in the chain (MLP top-1 / top-3, n), [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") head → chain-only → single + chain

| Head | Context | [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") head yes | [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") head no | Chain-only yes | Chain-only no | Single+chain yes | Single+chain no |
|---|---|---|---|---|---|---|---|
| amp | drive in front | 0.01 / 0.02 (206) | 0.00 / 0.02 (89) | 0.02 / 0.04 (206) | 0.02 / 0.06 (89) | 0.01 / 0.03 (206) | 0.03 / 0.06 (89) |
| amp | any block after the cab | 0.00 / 0.01 (200) | 0.02 / 0.06 (95) | 0.01 / 0.04 (200) | 0.03 / 0.06 (95) | 0.02 / 0.04 (200) | 0.01 / 0.04 (95) |
| amp | delay or reverb after | 0.00 / 0.01 (156) | 0.01 / 0.04 (139) | 0.01 / 0.03 (156) | 0.03 / 0.06 (139) | 0.01 / 0.03 (156) | 0.02 / 0.04 (139) |
| amp | preamp model | 0.01 / 0.03 (142) | 0.00 / 0.02 (153) | 0.01 / 0.04 (142) | 0.03 / 0.05 (153) | 0.00 / 0.04 (142) | 0.03 / 0.04 (153) |
| cab | delay or reverb after | 0.21 / 0.35 (156) | 0.30 / 0.47 (139) | 0.30 / 0.44 (156) | 0.35 / 0.53 (139) | 0.36 / 0.53 (156) | 0.42 / 0.68 (139) |
| cab | any block after it | 0.21 / 0.35 (200) | 0.34 / 0.51 (95) | 0.30 / 0.43 (200) | 0.36 / 0.60 (95) | 0.35 / 0.53 (200) | 0.46 / 0.75 (95) |
| cab | drive in front | 0.24 / 0.38 (206) | 0.29 / 0.45 (89) | 0.34 / 0.51 (206) | 0.27 / 0.42 (89) | 0.40 / 0.63 (206) | 0.36 / 0.54 (89) |
| drive | delay or reverb after | 0.01 / 0.07 (107) | 0.04 / 0.13 (99) | 0.08 / 0.14 (107) | 0.07 / 0.22 (99) | 0.04 / 0.14 (107) | 0.07 / 0.15 (99) |
| drive | amp gain knob ≥ 64 | 0.03 / 0.10 (92) | 0.02 / 0.10 (104) | 0.07 / 0.15 (92) | 0.10 / 0.21 (104) | 0.03 / 0.12 (92) | 0.08 / 0.18 (104) |
| mod | drive in front | 0.11 / 0.35 (46) | 0.45 / 0.70 (20) | 0.41 / 0.61 (46) | 0.40 / 0.80 (20) | 0.33 / 0.65 (46) | 0.60 / 0.80 (20) |
| delay | any block after it | 0.04 / 0.15 (46) | 0.14 / 0.21 (56) | 0.13 / 0.22 (46) | 0.12 / 0.21 (56) | 0.11 / 0.28 (46) | 0.16 / 0.38 (56) |
| delay | drive in front | 0.10 / 0.18 (73) | 0.10 / 0.21 (29) | 0.12 / 0.19 (73) | 0.14 / 0.28 (29) | 0.14 / 0.32 (73) | 0.14 / 0.38 (29) |
| reverb | drive in front | 0.29 / 0.60 (52) | 0.25 / 0.82 (28) | 0.25 / 0.63 (52) | 0.36 / 0.75 (28) | 0.21 / 0.56 (52) | 0.39 / 0.79 (28) |
| EQ | drive in front | 0.12 / 0.40 (40) | 0.12 / 0.47 (17) | 0.40 / 0.72 (40) | 0.35 / 0.76 (17) | 0.42 / 0.72 (40) | 0.24 / 0.76 (17) |

- The amp is at chance in every context. A drive in front no longer matters because there is nothing left to lose.
- The cab head's loss from blocks after it is smaller than [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")")'s but still there: single + chain 0.75 top-3 with nothing after the cab, 0.53 with something after. Those 95 chains with the bare drive/amp → cab core are the best result in the pilot.
- Chain training removed the [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") effect of a drive in front of the mod (0.35 → 0.61 top-3 with a drive).

## Knobs by name (within ±0.15, true model; knobs with n ≥ 20)

| Slot | Knob (n) | Chain-only | Single+chain | [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") head on chains | [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") single-block |
|---|---|---|---|---|---|
| drive | Drive (118) | 0.33 | 0.31 | 0.36 | 0.73 |
| drive | Tone (57) | 0.33 | 0.33 | 0.33 | 0.83 |
| drive | Bass (92) / Treble (95) / Mid (51) / Gain (58) | 0.27 / 0.28 / 0.37 / 0.21 | 0.30 / 0.31 / 0.31 / 0.19 | | |
| amp | Drive (218) | 0.37 | 0.39 | 0.33 | 0.65 |
| amp | Bass (271) / Mid (223) / Treble (271) | 0.29 / 0.37 / 0.34 | 0.32 / 0.30 / 0.34 | 0.27 / 0.36 / 0.35 | 0.49 / 0.43 / 0.58 |
| amp | Master (269) / Presence (138) / Sag (258) | 0.29 / 0.39 / 0.24 | 0.30 / 0.35 / 0.29 | 0.25 / 0.38 / 0.30 | 0.37 / 0.36 / 0.30 |
| cab | Distance (295) | 0.43 | 0.52 | 0.49 | 0.61 |
| cab | HighCut (282) / LowCut (294) | 0.41 / 0.45 | 0.43 / 0.45 | 0.46 / 0.37 | 0.65 / 0.83 |
| cab | Angle (161) / Position (161) | 0.43 / 0.45 | 0.43 / 0.46 | 0.41 / 0.35 | 0.35 / 0.58 |
| mod | Speed (58) / Mix (39) / Depth (28) / Intensity (28) | 0.43 / 0.28 / 0.29 / 0.64 | 0.57 / 0.38 / 0.36 / 0.71 | 0.41 / 0.33 / 0.25 / 0.43 | 0.73 / 0.74 / 0.59 / 0.84 |
| delay | Mix (96) / Time (84) / Feedback (75) | 0.25 / 0.17 / 0.31 | 0.29 / 0.30 / 0.35 | 0.27 / 0.39 / 0.24 | 0.78 / 0.42 / 0.42 |
| reverb | Mix (80) / Decay (80) | 0.56 / 0.42 | 0.61 / 0.35 | 0.26 / 0.34 | 0.88 / 0.55 |
| EQ | LowGain (20) / HighGain (20) | 0.20 / 0.05 | 0.30 / 0.10 | 0.40 / 0.15 | 0.54 / 0.60 |

A constant mid-range guess scores about 0.30 at ±0.15. Only reverb Mix, mod Intensity and mod Speed (mixed arm) are clearly above that on chains. The drive and amp knobs are at the constant level; the knob heads learned nothing usable about drive, amp or cab settings from finished-chain audio at this size. Full lists are in `cache/chain-train-v1/results.json` (`by_name`).

## Zero-render oracle: amp head with the TRUE cab id appended (research-full-chain 2(b))

| Head | Unconditioned chain-only head | True cab appended (oracle), 95 % | Random cab appended (control) | Knobs true / random |
|---|---|---|---|---|
| logreg | 0.03 / 0.05 | 0.01 (0.00–0.03) / 0.04 (0.03–0.07) | 0.01 (0.00–0.02) / 0.03 (0.01–0.05) | 0.29 / 0.31 |
| MLP | 0.02 / 0.04 | 0.01 (0.00–0.03) / 0.02 (0.01–0.05) | 0.01 (0.01–0.03) / 0.03 (0.02–0.06) | 0.31 / 0.31 |

**Top-3 gain from knowing the cab: 0.00 (MLP -0.02, logreg -0.01; all inside the intervals), and the random-cab control is indistinguishable.** The survey's condition for building sequential decoding ("oracle cab conditioning adds ≥ 0.1 to amp top-3") is not met. With about 20 chains per amp and 87 cabs, the head has nothing to condition on: it cannot tell amps apart even given the cab. This does not rule out conditioning at a much larger size; it says the pilot gives no evidence for it.

## What scaling overnight would cost

Measured here: 1.59 s per chain on 4 hosts (16,547 blocks in 7,117 s = 0.43 s per block, embedding included); [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") measured 0.45 s per block on 3 hosts. Planned chains average 3.71 blocks.

| Chains | Block renders | Hours at 0.45 s/block | Hours at the measured 1.59 s/chain |
|---|---|---|---|
| 4,472 (this pilot) | 16,547 | 2.1 | 2.0 |
| 10,000 | 37,100 | 4.6 | 4.4 |
| 50,000 | 185,500 | 23.2 | 22.1 |
| 100,000 | 371,000 | 46.4 | 44.2 |

So 50k chains is one 24 h unattended run and 100k is two; features only on disk (the pilot's shards are about 1 MB per 250 chains, 17 MB for 18 shards, so about 0.4 GB at 100k). Training would also grow: the amp single+chain MLP took 55 min on 89k clips with 3 calibration folds; 100k chains plus the single-block sets would be about 2–4 h of CPU per full training pass.

At 50k chains the amp would have about 220 chains per model, 100k about 450: roughly the single-block clips per amp (median 398) that gave 0.54 single-block top-1, but spread over random drives, cabs and effects. Whether that reaches a usable shortlist is not something the pilot measures. The one thing it does measure is the direction of the half → full step, and for the amp and the drive that step is flat.

## Honest reading (for the human; nothing adopted)

1. **The amp numbers are at chance and did not move.** Half 0.037, full 0.044, with single-block clips 0.037, with the true cab 0.024: every amp top-3 figure in this pilot is between 0.02 and 0.05, against a 0.013 chance level and a 0.80 single-block level. The early log lines were not a transient; the pilot could not learn amps from 4,472 chains, and nothing in it predicts that 10–20× more would.
2. **The drive is barely better** (0.18 top-3, 3× chance) and its learning curve is flat. Mixing in the single-block data hurt on chains.
3. **Cab, mod, delay and EQ do learn from chains**, and the cab is the one slot where the half → full step and the mixed arm both add: 0.40 → 0.48 → 0.60 top-3, 0.75 top-3 when nothing follows the cab. A chain-trained cab shortlist is the one deliverable that this pilot supports as plausible at larger scale.
4. **Presence detection is weak** (0.02–0.10 over the majority baseline), biased to "absent" for everything but the drive.
5. **Knobs are not recoverable** from finished-chain audio at this size for drive, amp or cab (constant-guess level).
6. **Against the survey's bar** (drive and amp > 5× chance at top-3, oracle ≥ +0.1): not met on either count. If the human wants to continue down chain-rendered training, the pilot argues for either a much larger run with a clear stop rule for the amp, or a different formulation for the amp and the drive (for example, predicting the amp family or gain class rather than one of 224 models, or render-and-rank with a cab shortlist). None of that is proposed here; it is a list of what the numbers leave open.

## Caveats

- 295 test chains; mod, reverb and EQ have n 57–80 and wide intervals.
- 40 training DI files from two players; the test files are the same players (P1/P2), different files. Cross-player transfer is not measured here.
- Random chains with uniform knobs, as in [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")"); harsher than real records.
- Block-serial rendering, mono between blocks, as in [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")").
- Two learning-curve points only.
- The oracle uses a one-hot at scale 1 next to 512-d AFx-Rep features with no re-tuning of alpha, width or epochs; a stronger conditioning design might do better, but with the amp head at chance there is nothing for conditioning to sharpen.

## Files

- Code: `scripts/chain_train.py` (plan / render / train; `train` now resumes from `results.json` and runs the oracle by default: `--no-resume`, `--no-oracle`), `src/tonematch/learning/chain_train.py` (`run`, `load_previous`, `with_cab_onehot`, `oracle_cab`, `append_ledger`). Tests: `tests/test_chain_train.py` (7 tests, synthetic features, no Native).
- Data (local, gitignored), under `cache/chain-train-v1/`: `chains.json`, `shards/` (18 × crops/labels/json), `results.json`, `results.md`, `render-1023-0858.log`, `train-1023-1057.log` (first, terminated run), `train-1023-resume.log`, `run.log`. Heads: `cache/models/chain-train-v1/{chain,single_chain}/<slot>_{logreg,mlp}.pkl` (pilot only; not adopted; not in `models/`).
- Ledger, 2026-10-23T22:04Z, `benchmark / native / chain-train-v1`: one row per slot `<slot>_mlp_model_top3` (chain-only arm, every chain; single+chain, half and logreg numbers in the notes), five `presence_<slot>_mlp_acc` rows, one `amp_oracle_true_cab_mlp_model_top3` row; plus `render-cost / native / chain-train-v1 wall_s_per_chain 1.591` at 17:57Z.
