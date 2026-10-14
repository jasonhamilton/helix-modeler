---
name: separate
description: Separate a song into stems with the chosen guitar separator on the best available backend (CUDA, MPS, ROCm, DirectML, CPU), or accept a user-supplied guitar stem alongside the mix, cache the result by song hash, and record backend, model, wall-clock and a confidence estimate. Use on every song ingest.
---

# separate

Script: `scripts/separate.py <song> [--stem <guitar.wav>] [--model htdemucs_6s|bs-roformer|mel-roformer|mdx23c] [--backend cpu|mps|cuda|rocm] [--override "1-8:0,9-16:1"] [--dataset ID] [--no-cache] [--no-ledger]` (owner: `separation`). Library: `tonematch.separation` (`separate`, `segment`, `confidence_mask`, `recombine_and_reseparate`). Needs `pip install -e '.[separation]'` for `htdemucs_6s` (torch is optional; `--stem` needs nothing extra); RoFormer needs `.[separation-roformer]` and a licence check of the checkpoint (`docs/notes/separation-models.md`). Exit 2 when the model's extra is missing.

## Steps

1. Decode, resample to 48 kHz, loudness-normalize; compute a content hash; if `cache/stems/<hash>/<model>/` exists for the requested model, reuse it.
2. If `--stem` is given: skip separation, align the supplied stem to the mix (cross-correlation), store it as `guitar.wav`, and derive `others.wav` as mix minus guitar (flagged as approximate). The mix is still required.
3. Otherwise detect the backend in order CUDA, MPS, ROCm, DirectML, CPU; run the model (default `htdemucs_6s`; RoFormer checkpoints only when installed and flagged). Keep every stem, the guitar stem is not enough.
4. Confidence: write a time-frequency mask estimating where the separator is confident (model-specific; a simple proxy is agreement between two models or between overlapping windows). Save as `confidence.npy`.
5. Write `cache/stems/<hash>/manifest.json`: source, model, backend, sample rate, wall-clock, stem list, confidence summary.
6. Segmentation: beats, bars and section boundaries from the mix, 2 s guitar windows (silence skipped) clustered into at most 3 snapshots; writes `sections.json`; print the bar-to-snapshot table; `--override` rewrites it and a plain re-run keeps the override.
7. Ledger: `--kind separate --dataset <song id> --metric wall_clock_s --value <t> --notes "<model>/<backend>"` and a second row `confidence_mean`. Cache hits write no rows. Say plainly that real-song quality is unmeasured unless a measured run exists.

## Never

Send audio to a hosted API. Hosted separation is deferred by decision.
