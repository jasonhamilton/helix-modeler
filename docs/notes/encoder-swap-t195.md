# encoder-swap-v1: permissive encoders in place of AFx-Rep for the drive predictor ([T195](../../board/TASKS.md#t195 "Permissive encoders PANNs and DAC vs AFx-Rep"))

Owner: learning. Written 2026-10-20. **A measurement only. Nothing is adopted, and there is no pass bar.** The human decides whether any encoder replaces AFx-Rep. Pre-registration: docs/DECISIONS.md, 2026-10-20 (learning, [T195](../../board/TASKS.md#t195 "Permissive encoders PANNs and DAC vs AFx-Rep on a drive-dataset-v1…")).

## Result in one paragraph

- **Neither permissive encoder matches AFx-Rep.** Under the same heads, calibration and splits, AFx-Rep is better on every arm and on nearly every metric, with intervals well clear of zero.
- **DAC is the closer of the two.** The DAC encoder (24 kHz, MIT) with the MLP head is behind AFx-Rep by:
  - **0.10** in model top-1 within player (0.79 against 0.89);
  - **0.21–0.23** for P2→P1;
  - **0.07–0.09** for P1→P2.
- **PANNs is far behind.** PANNs Cnn14 (CC BY 4.0) loses **0.37–0.51** in top-1 on every arm. Its AudioSet embedding hardly separates drive models (top-1 0.37–0.38 within player).
- **One exception.** DAC is level with AFx-Rep, or slightly ahead, on Drive within ±0.15 when the true model is given (within player +0.03 [+0.01, +0.04] and +0.01 [-0.01, +0.02]). Its weakness is choosing the model, not reading the gain.
- **Nothing is adopted.** On these numbers AFx-Rep stays the best encoder measured. The licence question is not removed by either swap (see Licences).

## Results

The subset holds 71,517 clips: train 27,000 (40 files), cal 6,516 (5), newtest 19,012 (18), v1files_new 18,989 (29). There were no render failures.
- **Re-render check:** 1,431 clips, minimum cosine with the stored AFx-Rep crops 0.99999, median 1.0. The AFx-Rep arm and the new encoders saw the same audio.
- **Time.** Render and embed took 8.7 h of wall time, on one host. Pauses came from the shared load: [T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option")'s 3 hosts, and the human's other work raising the load average to 18–31. Training took 2.7 h.

Model top-1 / top-5 / family; Drive ±0.15 and Tone ±0.10 given as **true model / head's own top-1 model**.

### Within player (both players' train files)

| Encoder | Head | newtest (18 files) | Drive | Tone | v1files_new (29 files) | Drive | Tone |
|---|---|---|---|---|---|---|---|
| AFx-Rep (l2, [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") input) | logreg | 0.84 / 0.98 / 0.88 | 0.68 / 0.62 | 0.61 / 0.55 | 0.82 / 0.98 / 0.86 | 0.66 / 0.60 | 0.61 / 0.55 |
| AFx-Rep (l2) | mlp | **0.89** / 0.99 / 0.91 | 0.71 / 0.67 | 0.67 / 0.62 | **0.87** / 0.99 / 0.90 | 0.67 / 0.64 | 0.66 / 0.61 |
| AFx-Rep (z) | logreg | 0.85 / 0.98 / 0.89 | 0.69 / 0.64 | 0.63 / 0.57 | 0.83 / 0.98 / 0.87 | 0.66 / 0.61 | 0.63 / 0.56 |
| AFx-Rep (z) | mlp | **0.90** / 0.99 / 0.92 | 0.71 / 0.68 | 0.68 / 0.63 | **0.88** / 0.99 / 0.91 | 0.68 / 0.64 | 0.67 / 0.62 |
| PANNs Cnn14 (z) | logreg | 0.38 / 0.71 / 0.54 | 0.60 / 0.42 | 0.31 / 0.24 | 0.37 / 0.69 / 0.53 | 0.56 / 0.40 | 0.30 / 0.24 |
| PANNs Cnn14 (z) | mlp | 0.38 / 0.73 / 0.54 | 0.67 / 0.44 | 0.36 / 0.26 | 0.37 / 0.71 / 0.53 | 0.64 / 0.41 | 0.35 / 0.25 |
| DAC-24k (z) | logreg | 0.67 / 0.92 / 0.75 | 0.65 / 0.54 | 0.50 / 0.41 | 0.65 / 0.90 / 0.74 | 0.59 / 0.50 | 0.50 / 0.41 |
| DAC-24k (z) | mlp | **0.79** / 0.97 / 0.83 | 0.73 / 0.64 | 0.63 / 0.55 | **0.77** / 0.96 / 0.82 | 0.68 / 0.60 | 0.62 / 0.53 |

### Cross player P2 → P1 (train and calibrate on P2, test on P1 files)

| Encoder | Head | newtest (10 files) | Drive | Tone | v1files_new (14 files) | Drive | Tone |
|---|---|---|---|---|---|---|---|
| AFx-Rep (l2) | logreg | 0.71 / 0.93 / 0.78 | 0.59 / 0.52 | 0.55 / 0.46 | 0.66 / 0.91 / 0.74 | 0.57 / 0.49 | 0.53 / 0.43 |
| AFx-Rep (l2) | mlp | **0.72** / 0.94 / 0.79 | 0.59 / 0.53 | 0.57 / 0.50 | **0.68** / 0.93 / 0.75 | 0.57 / 0.50 | 0.53 / 0.45 |
| AFx-Rep (z) | mlp | 0.72 / 0.93 / 0.79 | 0.58 / 0.52 | 0.56 / 0.49 | 0.67 / 0.92 / 0.76 | 0.57 / 0.49 | 0.53 / 0.45 |
| PANNs Cnn14 (z) | mlp | 0.24 / 0.55 / 0.41 | 0.55 / 0.39 | 0.30 / 0.22 | 0.19 / 0.48 / 0.38 | 0.53 / 0.36 | 0.29 / 0.22 |
| DAC-24k (z) | logreg | 0.46 / 0.75 / 0.59 | 0.59 / 0.45 | 0.42 / 0.32 | 0.43 / 0.73 / 0.56 | 0.51 / 0.40 | 0.40 / 0.30 |
| DAC-24k (z) | mlp | **0.49** / 0.75 / 0.61 | 0.55 / 0.46 | 0.51 / 0.38 | **0.47** / 0.75 / 0.59 | 0.52 / 0.42 | 0.48 / 0.36 |

### Cross player P1 → P2

| Encoder | Head | newtest (8 files) | Drive | Tone | v1files_new (15 files) | Drive | Tone |
|---|---|---|---|---|---|---|---|
| AFx-Rep (l2) | logreg | 0.56 / 0.86 / 0.64 | 0.58 / 0.50 | 0.48 / 0.39 | 0.59 / 0.88 / 0.67 | 0.51 / 0.46 | 0.48 / 0.40 |
| AFx-Rep (l2) | mlp | **0.58** / 0.87 / 0.67 | 0.63 / 0.54 | 0.51 / 0.43 | **0.62** / 0.90 / 0.70 | 0.53 / 0.48 | 0.54 / 0.46 |
| AFx-Rep (z) | mlp | 0.59 / 0.86 / 0.67 | 0.64 / 0.54 | 0.52 / 0.44 | 0.63 / 0.89 / 0.71 | 0.53 / 0.48 | 0.54 / 0.47 |
| PANNs Cnn14 (z) | mlp | 0.19 / 0.48 / 0.37 | 0.55 / 0.37 | 0.32 / 0.23 | 0.19 / 0.46 / 0.37 | 0.52 / 0.35 | 0.33 / 0.25 |
| DAC-24k (z) | logreg | 0.46 / 0.77 / 0.59 | 0.52 / 0.43 | 0.43 / 0.33 | 0.48 / 0.79 / 0.60 | 0.49 / 0.42 | 0.45 / 0.34 |
| DAC-24k (z) | mlp | **0.52** / 0.82 / 0.63 | 0.57 / 0.47 | 0.49 / 0.39 | **0.53** / 0.83 / 0.63 | 0.52 / 0.44 | 0.52 / 0.41 |

The PANNs logreg cross-player rows are in `summary.md`: 0.18–0.24 top-1.

### Paired differences against AFx-Rep (l2), same head, 95% interval over test files

Model top-1:

| Arm | Eval | PANNs logreg | PANNs mlp | DAC logreg | DAC mlp |
|---|---|---|---|---|---|
| within | newtest | -0.46 [-0.48, -0.44] | -0.51 [-0.53, -0.49] | -0.17 [-0.19, -0.16] | **-0.10 [-0.11, -0.08]** |
| within | v1files_new | -0.46 [-0.47, -0.44] | -0.51 [-0.52, -0.49] | -0.17 [-0.19, -0.15] | **-0.10 [-0.12, -0.09]** |
| P2→P1 | newtest | -0.48 [-0.50, -0.46] | -0.48 [-0.50, -0.47] | -0.25 [-0.27, -0.22] | -0.23 [-0.26, -0.20] |
| P2→P1 | v1files_new | -0.46 [-0.49, -0.44] | -0.49 [-0.52, -0.46] | -0.22 [-0.25, -0.20] | -0.21 [-0.24, -0.18] |
| P1→P2 | newtest | -0.38 [-0.42, -0.33] | -0.39 [-0.44, -0.35] | -0.10 [-0.12, -0.08] | **-0.07 [-0.09, -0.05]** |
| P1→P2 | v1files_new | -0.40 [-0.44, -0.36] | -0.43 [-0.46, -0.39] | -0.11 [-0.14, -0.09] | -0.09 [-0.12, -0.07] |

DAC mlp minus AFx-Rep (l2), other metrics. Each cell is newtest; v1files_new.

| Arm | Family | Drive ±0.15 (true) | Drive ±0.15 (own) | Tone ±0.10 (true) | Tone ±0.10 (own) |
|---|---|---|---|---|---|
| within | -0.08 [-0.09, -0.07]; -0.09 [-0.10, -0.07] | **+0.03 [+0.01, +0.04]**; +0.01 [-0.01, +0.02] | -0.03 [-0.04, -0.01]; -0.04 [-0.05, -0.03] | -0.04 [-0.06, -0.03]; -0.05 [-0.06, -0.03] | -0.07 [-0.09, -0.06]; -0.07 [-0.09, -0.06] |
| P2→P1 | -0.17 [-0.20, -0.14]; -0.16 [-0.19, -0.13] | -0.04 [-0.06, -0.03]; -0.05 [-0.07, -0.04] | -0.07 [-0.09, -0.05]; -0.08 [-0.10, -0.07] | -0.06 [-0.08, -0.05]; -0.05 [-0.07, -0.03] | -0.11 [-0.13, -0.10]; -0.09 [-0.11, -0.07] |
| P1→P2 | -0.04 [-0.06, -0.03]; -0.07 [-0.09, -0.04] | -0.06 [-0.10, -0.02]; -0.02 [-0.04, +0.01] | -0.07 [-0.09, -0.05]; -0.04 [-0.06, -0.02] | -0.02 [-0.04, -0.00]; -0.02 [-0.03, +0.00] | -0.04 [-0.06, -0.03]; -0.05 [-0.06, -0.03] |

- **Against AFx-Rep (z).** The same prep for all, and slightly stronger than l2, by +0.01 top-1 within player. The gaps then grow by about 0.01 (DAC mlp within: -0.11 [-0.12, -0.10] and -0.12 [-0.13, -0.10]). All pairs are in `result.json` and `summary.md`.
- **Context.** With the full [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") training set (105k new + 8.7k old clips), the AFx-Rep MLP reached 0.93 on newtest. Here, with 27k clips, it reaches 0.89. Encoder choice moves top-1 far more than a 4× change in data.

## What this suggests (INTERPRETATION, for the human)

1. **AFx-Rep's effect-specific pretraining is worth a lot for this task:** about 0.10 top-1 over a general audio codec and about 0.5 over an AudioSet tagger.
   - A permissive drop-in costs real accuracy.
   - The cost is largest for a new player (P2→P1: -0.21 to -0.23 with DAC).
2. **DAC preserves gain detail.** Its Drive-given-model accuracy is at least AFx-Rep's, and it improves the most with a non-linear head (logreg → MLP: +0.12).
   - A licence-clean route would be a supervised or contrastive projection trained on frozen DAC features, or a fine-tuned DAC encoder.
   - The plan names this route as candidate 2 in [afxrep-licence.md](afxrep-licence.md). This run measured none of it.
3. **DAC is not licence-clean on data.** It was trained partly on MUSDB and Jamendo, so it does not resolve the data-terms question any better than AFx-Rep. PANNs (AudioSet only, CC BY 4.0 weights) is the cleanest of the three and clearly the weakest.
4. **Not measured here:**
   - the 44 kHz DAC model;
   - the PANNs pre-fc1 pooled features;
   - concatenations of encoders;
   - more MLP seeds;
   - any real-pedal or human-take audio.

## Method

- **Data.** A subset of drive-dataset-v1 ([T191](drive-dataset-v1.md)), seed 195, with the same whole-file splits:
  - 27,000 of its 105k train clips, drawn uniformly;
  - every cal, test (`newtest`, 18 new held-out files) and v1test (`v1files_new`, the 29 T186/T189 test files) clip.
  - The [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") old renders are not used. P3 is never read; no song audio is used.
- **Re-render.**
  - [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") kept only AFx-Rep features, so every subset clip is rendered again on one Helix Native 3.82 host from the plan (model, CC values, DI file, start), through the [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") path (`match_block.make_engine` / `make_blocks`, `drive_dataset_render.render_one`).
  - The clip is embedded and the audio dropped.
  - A LoadGuard (busy cores, [T193](../../board/TASKS.md#t193 "Switch the remaining scripts' own os.getloadavg() guards to…")) is checked before every clip. The engine leases its host from the host budget.
  - Shards are resumable. Remaining clips were re-ordered train/cal first (DECISIONS amendment).
- **Re-render check.** Every 50th clip is also re-embedded with AFx-Rep and compared with the stored [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") crops (minimum cosine over the 3 crops). This shows that the AFx-Rep arm, which reuses the stored features, sees the same audio as the new encoders.
- **Encoders** (each on 3 × 3.33 s crops of the 10 s clip, as [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1")):
  - **AFx-Rep:** the stored [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") `mid` embeddings, 512-d, unit norm.
  - **PANNs Cnn14:** `Cnn14_mAP=0.431.pth`, which is the 32 kHz, 64-mel AudioSet model.
    - It uses the vendored Cnn14 class (`tonematch.metrics._afxrep_model`) with batchnorm, plus PANNs' `fc1`.
    - Feature: the published PANNs `embedding`, relu(fc1(pooled conv features)), 2048-d.
    - Input: peak-normalised mono, resampled to 32 kHz.
    - Sanity check: on a tanh-distorted Guitar-TECHS DI excerpt, its AudioSet head ranks "Guitar 0.94, Effects unit 0.68, Distortion 0.56".
  - **DAC encoder:** Descript Audio Codec, the **24 kHz** model (release 0.0.4).
    - Re-implemented in `tonematch.learning.encoders`, with weight norm folded at load. It matches the legacy weight_norm path to 1.4e-5 on a real clip.
    - Feature: mean and std over time of the 1024-channel pre-quantiser latent, 2048-d.
    - Input: mono, resampled to 24 kHz, at -24 dBFS RMS (DAC trains on excerpts at -24 LUFS).
    - The 44 kHz model was not used. On MPS it costs 0.61 s per clip against 0.35 s, which would be about 12 h for this subset. On CPU (2 threads) it costs 3.4 s per clip.
    - Runs on MPS. MPS and CPU embeddings agree, cosine 0.9999999.
- **Feature sets.**
  - `afx_l2`: the [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") input, as is.
  - `afx_z`, `panns_z`, `dac_z`: per-dimension z-score fitted on the arm's train crops, divided by sqrt(d). This puts every encoder on the same scale for the same heads.
- **Heads, pooling, calibration, arms.** Exactly the [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") `run_train` recipe:
  - hierarchical logistic regression with per-model ridge knobs, and the 2-layer MLP (256, 60 epochs, 1 seed);
  - multi-crop predictions averaged;
  - temperature and sigma scales fitted on out-of-fold predictions over the train files (5 group folds) plus the cal files;
  - arms: main (both players), P2→P1 and P1→P2 (train and calibrate on one player, test on the other).
- **Paired differences.** Encoder minus AFx-Rep, on the same test clips. The 95% percentile interval comes from a bootstrap over whole test DI files (2,000 resamples), for both references `afx_l2` and `afx_z`.
- **Load.** One Native host plus 2 torch CPU threads (PANNs, and AFx-Rep for the check), with DAC on the GPU (MPS). proc_check was clean before and after.

## Licences (not legal advice)

| Encoder | Code | Weights | Training data | Notes |
|---|---|---|---|---|
| AFx-Rep (ST-ITO) | Apache-2.0 | HF tag `apache-2.0` only (csteinmetz1/afx-rep, rev 6582cb5) | Includes research-only / non-commercial sets: ENST-Drums, MTG-Jamendo, FSD50K in part, MUSDB18 per the shipped config | See [afxrep-licence.md](afxrep-licence.md) |
| PANNs Cnn14 | MIT (Qiuqiang Kong; `licenses/MIT-panns-audioset_tagging_cnn.txt`) | **CC BY 4.0**, read on [Zenodo 3987831](https://zenodo.org/records/3987831) v3 (Kong, Cao, Iqbal, Wang, Wang, Plumbley). File `Cnn14_mAP=0.431.pth`, 327.4 MB, md5 `541141fa2ee191a88f24a3219fff024e` (verified) | AudioSet: YouTube clips. Google releases the labels and features under CC BY 4.0. The audio itself has no dataset licence (per-video rights, UNVERIFIED for this use) | CC BY needs attribution: name the authors, link the record and state changes (here: no change to the weights) |
| DAC (Descript Audio Codec) | MIT, Copyright (c) 2023-present Descript (`licenses/MIT-descript-audio-codec.txt`) | **MIT**: the [README](https://github.com/descriptinc/descript-audio-codec) says "Weights are released as part of this repo under MIT license". File `weights_24khz.pth` from GitHub release 0.0.4, sha256 `44bad592fc393e03eb0be7a5120b7d487fe9612fa41269dc03fca3d4b87e20ad` | Per the paper ([arXiv 2306.06546](https://arxiv.org/html/2306.06546v2) §4.1): DAPS, DNS Challenge 4, Common Voice, VCTK, **MUSDB**, **Jamendo** and AudioSet | The weights licence is permissive. But **MUSDB ("educational purposes only ... not ... commercial") and MTG-Jamendo (non-commercial research) are in its training data**: the same open question as for AFx-Rep (INTERPRETATION). |

- **Where the weights are.** Only under `cache/models/panns/` and `cache/models/dac/` (gitignored by `.gitignore` line 2, `cache/`, confirmed by `git check-ignore`). No weights are in the repo.
- **Vendored code.** The PANNs model is the Cnn14 class already vendored. The DAC encoder is a re-implementation of MIT code, and its licence text is added under `licenses/`.
- **Fetch commands** (the user's machine):

  ```bash
  curl -L -o "cache/models/panns/Cnn14_mAP=0.431.pth" "https://zenodo.org/records/3987831/files/Cnn14_mAP%3D0.431.pth?download=1"
  curl -L -o cache/models/dac/weights_24khz_8kbps_0.0.4.pth https://github.com/descriptinc/descript-audio-codec/releases/download/0.0.4/weights_24khz.pth
  ```

  Both are hash-checked at load.
- **Reading (INTERPRETATION).**
  - Of the two, only PANNs avoids non-commercial data in the encoder's own training set. The AudioSet question (YouTube audio, per-video rights) remains, but it is the common, widely published case, and the weights carry an explicit CC BY 4.0 grant.
  - DAC's weights are MIT, but it was trained partly on MUSDB and Jamendo. It is therefore not cleaner than AFx-Rep on the data question, only on the weights tag.
- **Heads trained here** hold none of the encoders' weights. They are trained on Helix Native renders of Guitar-TECHS DI (CC BY 4.0). The Native-render question is in `line6-eula-flags.md`. The human's 2026-10-17 ruling covers publishing weights trained on Native renders.

## Files

- `src/tonematch/learning/encoders.py`: PANNs and DAC embedders (load, hash check, prep).
- `scripts/encoder_swap_t195.py`: `select`, `render`, `train`.
- `tests/test_encoder_swap_t195.py`: weight-norm folding, DAC shapes and level, PANNs wrapper, paired bootstrap, z-prep.
- `cache/encoder-swap-t195/`:
  - `subset.json` and `shards/`: features, float16;
  - `result.json` and `summary.md`;
  - `run.log`.
- `cache/models/encoder-swap-t195/heads_main_*.pkl`: local, not adopted.
