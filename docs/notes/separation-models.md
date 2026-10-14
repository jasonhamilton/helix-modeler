# Separation models, weights and licences (separation agent, T020)

No model weights and no song audio are in the repo. Weights download on first use into `cache/models/` (gitignored); the only network traffic is that weight download (HuggingFace hub, plain GET, nothing uploaded).

| Model | Code licence | Weights licence | Status here |
|---|---|---|---|
| `htdemucs_6s` (default) | demucs, MIT (facebookresearch/demucs, archived 2025-01-01; maintained fork adefossez/demucs, 4.1.0 installed) | The Demucs README states "code and pretrained models" are MIT. The HuggingFace repo `adefossez/HTDemucs-6s` shows no licence tag of its own. | Installed, run on MPS and CPU. Weights 52 MB in `cache/models/hf/`. |
| `bs-roformer`, `mel-roformer`, `mdx23c` | audio-separator, MIT | **Per checkpoint and often unstated or non-commercial.** Community checkpoints (viperx, KimberleyJensen, ZFTurbo's MVSep-MDX23) are trained and redistributed under varying terms. Treat as FLAG: read the checkpoint's own page before use; never bundle. | Adapter written, **not installed and not run** on this machine. |

Flags for the lead and `reviewer`:

- The weights of `htdemucs_6s` are MIT per the upstream README only; the HuggingFace page itself carries no licence field. Low risk, but record it. Training data (MUSDB18-HQ plus extra licensed songs) is not redistributed with the model.
- RoFormer checkpoints: do not default to one until its licence is read. `TONEMATCH_ROFORMER_CKPT` picks the file; the defaults in `models.py` are placeholders from audio-separator's catalogue and may have restrictive terms.
- torch (BSD-3), demucs (MIT), audio-separator (MIT) are the extras' licences as declared; run `licence-check` for the installed set.

Quality: `htdemucs_6s` guitar SDR ~5.2 dB is the MVSep figure from the plan, not something measured here. On this machine only a synthetic mix was run (see ledger dataset `synthetic-sep-v0`). On it, `htdemucs_6s` did not recognise the Karplus-Strong guitar as guitar (it went to `other`; guitar-stem SDR about 0 dB), which says the synthetic source is a poor guitar, not anything about real songs. **Real-song separation quality is unmeasured.**

Backends: `gpu.detect_backend()` order; torch device maps mps -> `mps`, cuda and rocm -> `cuda`, directml and cpu -> CPU (demucs has no DirectML path). An op failing on an accelerator falls back to CPU and the ledger records the backend actually used.

Known limits of the first cut of Step 2: constant tempo and 4/4 assumed; downbeat phase is a guess from low-band energy (on the synthetic mix it is half a bar off, so the snapshot boundary lands a bar late; the override exists for this); the tone embedding is hand-built (24 log bands, centroid, flatness, crest) until `tone-metrics` supplies the learned one; the confidence mask is a dominance-times-consistency heuristic, uncalibrated.

## licence-check result for the `separation` extra (2026-10-14)

`reviews/licence-2026-10-14.md` flags two transitive dependencies pulled in by demucs 4.1: `lameenc` 1.8.4 (LGPL-3.0-or-later; an MP3 encoder demucs uses only for MP3 output, which we never call; we do not link or modify it) and `sphn` 0.2.1 (licence metadata UNKNOWN; upstream is believed MIT or Apache but that is unverified). Both need a `reviewer` ruling before the extra ships. The same run's Line 6 asset FAIL (`fixtures/synthetic/*.json`) is not from this task.

## RoFormer / MDX23C licence reading and install (2026-10-14, separation)

Read from the pages listed; "not stated" means no licence field or text was found there. Nothing was downloaded: the only guitar-capable checkpoint has no stated licence.

| Item | URL | Licence found | Commercial / redistribution |
|---|---|---|---|
| audio-separator 0.47.0 (code) | https://github.com/nomadkaraoke/python-audio-separator ; GitHub licence API: `spdx_id: MIT` | MIT. README adds: "If you choose to integrate this project into some other project using the default model or any other model trained as part of the UVR project, please honor the MIT license by providing credit to UVR and its developers!" README gives no per-model licences. | Code: yes. Models: not covered. |
| `BS-Roformer-SW.ckpt` + `.yaml` (6 stems incl. guitar; the ONLY guitar output among audio-separator's catalogue RoFormer/MDX23C entries; 699 MB) | audio-separator downloads it from https://github.com/nomadkaraoke/python-audio-separator/releases/tag/model-configs (release text: "just a holding ground to upload large model files"). Upstream mirrors: https://huggingface.co/FractalSurfer/BS-RoFormer-SW-6stem ("Upstream declares no license (license: unknown)", "grant no additional rights"), enerjazzer/BS-ROFO-SW-Fixed (licence "unknown"). Provenance issue: https://github.com/openmirlab/bs-roformer-infer/issues/3 | **Not stated.** Original uploader (jarredou on HuggingFace) account deleted; trainer unverified; maintainer quote: "The architecture's MIT license does not establish rights to this checkpoint". Re-uploads declare conflicting licences (some MIT, some unknown). | **Unknown, treat as not allowed** (no grant). Not downloaded, not bundled. Opt-in only via `TONEMATCH_ROFORMER_LICENCE_ACK=1`. |
| BS-RoFormer / Mel-Band RoFormer architecture | https://github.com/lucidrains/BS-RoFormer (GitHub API: MIT); https://github.com/ZFTurbo/Music-Source-Separation-Training (MIT) | Code MIT. A web search snippet says community RoFormer weights on HuggingFace are commonly labelled CC-BY-NC-SA-4.0 (non-commercial); I could not open those cards (HuggingFace returned 401 for the pages I tried), so that is unverified here. | Code yes; weights per checkpoint. |
| viperx BS-RoFormer ep_317 / Mel-RoFormer ep_3005 (the old placeholder names) | ZFTurbo `docs/pretrained_models.md` lists them as vocals / other; weights hosted at https://github.com/TRvlvr/model_repo (no licence file, GitHub licence API 404) | Not stated (the model_repo has none). | Unknown. **No guitar stem**, and not in audio-separator 0.47's catalogue under those names. |
| Other catalogue RoFormer checkpoints (Kim vocals, unwa/Gabox instrumental, karaoke, de-reverb, denoise, crowd, aspiration, male/female) | `audio_separator/models.json`, `roformer_download_list` | Not read individually (no guitar output, so not relevant). | n/a |
| MDX23C in the catalogue | `models.json` | Only De-Reverb and DrumSep (aufr33-jarredou) plus vocal/instrumental MDX23C; none outputs guitar. | n/a |

Decisions in `models.py`: `bs-roformer` -> `BS-Roformer-SW.ckpt` (guitar present; licence-gated by env opt-in); `mel-roformer` and `mdx23c` -> no guitar-capable catalogue checkpoint, `SeparatorUnavailable` with that message. The old placeholder filenames are removed.

Install: `uv pip install --python .venv/bin/python "audio-separator>=0.30"` (extra `separation-roformer`) added 35 packages, among them audio-separator 0.47.0, librosa 1.0.0, scikit-learn, numba, onnx-weekly, onnx2torch-py313, diffq, soxr, samplerate, rotary-embedding-torch, torchvision. `scripts/licence_check.py` then FAILs on one package it flags: **soxr 1.1.0, LGPL-2.1-or-later** (unallowlisted). Lead should propose an allowlist entry or drop the extra. Not flagged by the checker but a real concern: **diffq 0.2.4 declares "Creative Commons Attribution-NonCommercial 4.0 International"** (the checker's copyleft list has no non-commercial pattern). Also MPL-2.0 certifi (weak copyleft, unflagged). Run output with these two: scratchpad copy of `reviews/licence-2026-10-14.md` (the repo file was restored to the previous content).

Measured: nothing. No checkpoint was downloaded, so no wall-clock on MPS/CPU and no ledger row. Real-song quality remains unmeasured for every model, and the MVSep SDR figures are not ours.
