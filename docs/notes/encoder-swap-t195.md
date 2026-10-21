# encoder-swap-v1: permissive encoders in place of AFx-Rep for the drive predictor ([T195](../../board/TASKS.md#t195 "Permissive encoders PANNs and DAC vs AFx-Rep"))

Owner: learning. Written 2026-10-20. **A measurement only. Nothing is adopted, and there is no pass bar.** The human decides whether any encoder replaces AFx-Rep. Pre-registration: docs/DECISIONS.md, 2026-10-20 (learning, [T195](../../board/TASKS.md#t195 "Permissive encoders PANNs and DAC vs AFx-Rep on a drive-dataset-v1…")).

RESULTS_PLACEHOLDER

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
