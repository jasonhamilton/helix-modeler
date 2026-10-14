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
