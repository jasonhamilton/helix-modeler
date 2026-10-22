# Model cards

One card per checkpoint. Checkpoints themselves are never committed. Owner: `learning`.

## predictor-pilot-v0 (T186, 2026-10-18): measurement only, not adopted, not published

| Field | Value |
|---|---|
| Checkpoints | `cache/models/predictor-pilot-v0/all/model.pt` (main), `cache/models/predictor-pilot-v0/xplayer/model.pt` (cross-player arm); local, gitignored |
| Task | Drive block only: family (15, drive-families-v1), model (60, hierarchical within family), Drive and Tone knob positions with sigma, from wet audio with no dry signal |
| Architecture | Log-mel (128 bands, 20 ms hop) CNN, 1.49 M parameters, trained from scratch on MPS (23 min) |
| Data | 14,640 single-drive Helix Native 3.82 renders (block-library-v1.2) made under the owner's licence, on Guitar-TECHS DI (CC BY 4.0; attribution to the Guitar-TECHS authors), players P1 and P2. Not used: P3 (proposed Gate 3 reserve), sealed synthetic-v1/-v2/-v3, the helix-trust-v2 test split, user audio |
| Licence note | Native-derived. Local use only. Publishing needs a human ruling or a retrain from Stomp renders. No third-party weights in the CNN |
| Eval (held-out DI files, n = 4,337) | model top-1 0.436, top-5 0.764 (chance 0.017 / 0.083); family top-1 0.588 (chance 0.067); Drive within 0.15: 0.636, Tone within 0.10: 0.468 (true model given); paired AFx-Rep + logistic head: 0.563 / 0.899 / 0.648 |
| Eval (cross-player, train P2, test P1) | model top-1 0.130, top-5 0.310, family 0.200 (AFx-Rep head: 0.421 / 0.786 / 0.511) |
| Known failures | Does not transfer to an unseen player. Raw knob sigmas are over-confident; the one-file calibration over-corrects Drive |
| Ledger | `benchmark / - / predictor-pilot-v0`, 2026-10-18, agent `learning` |
| Details | `docs/notes/predictor-pilot-v0.md` |

## predictor-pilot-v1 (drive only, LOCAL, not published)

- **What:** frozen AFx-Rep `mid` embeddings (cache/models/afx-rep, apache-2.0 tag; training-data terms unresolved, see docs/notes/afxrep-licence.md) with small heads: logistic regression or an MLP, for family, model, and Drive/Tone with uncertainty. Multi-crop input: 3 × 3.33 s.
- **Trained on:** 14,631 Helix Native 3.82 single drive-block renders on Guitar-TECHS DI (CC BY 4.0), P1 and P2 only. P3 is untouched; no sealed sets; no song audio.
- **Eval (held-out DI files):**
  - right model first 0.68–0.73, top-5 0.96–0.97, family 0.74–0.76;
  - new player 0.36–0.53;
  - Drive ±0.15 0.55–0.58 and Tone ±0.10 0.41–0.47 with its own top-1 model.
- **Status:** a measurement (T189). Checkpoints in cache/models/predictor-pilot-v1/, local only. Not published: the human allows publishing project-trained weights, but this head depends on AFx-Rep, whose data terms are unresolved.

## encoder-swap-v1 heads (T195, 2026-10-20): measurement only, not adopted, not published

| Field | Value |
|---|---|
| Checkpoints | `cache/models/encoder-swap-t195/heads_main_{afx_l2,afx_z,panns_z,dac_z}_{logreg,mlp}.pkl` (main arm only); local, gitignored. They hold no encoder weights |
| Task | Drive block only: family, model (54, hierarchical), Drive and Tone with calibrated sigma, from wet audio; 3 × 3.33 s multi-crop; T189/T191 heads and calibration |
| Encoders (frozen, not shipped) | **AFx-Rep** `mid` 512-d: HF `csteinmetz1/afx-rep` rev 6582cb5, tag apache-2.0; its training data includes non-commercial sets (afxrep-licence.md). **PANNs Cnn14** `Cnn14_mAP=0.431.pth`: Zenodo 3987831, **CC BY 4.0** (Kong et al.), md5 541141fa…024e; AudioSet training data. **DAC encoder**: 24 kHz model, release 0.0.4, **MIT** (Descript), sha256 44bad592…7e20ad; training data includes MUSDB (educational only) and MTG-Jamendo (non-commercial research). Weights are only in `cache/models/{afx-rep,panns,dac}/` |
| Data | 27,000 train and 6,516 cal clips of drive-dataset-v1: Helix Native 3.82 single-drive renders, re-rendered for this task, on Guitar-TECHS DI (CC BY 4.0; P1/P2 only, P3 never read). No song audio, no sealed sets |
| Licence note | Native-derived. The human's 2026-10-17 ruling allows publishing weights trained on Native renders (line6-eula-flags.md). Each head is useless without its encoder. Publishing a PANNs head needs CC BY attribution of the PANNs weights. A DAC or AFx-Rep head carries those encoders' training-data question. Not published |
| Eval, model top-1 (MLP), newtest / v1files_new | within player: AFx-Rep 0.89 / 0.87, DAC 0.79 / 0.77, PANNs 0.38 / 0.37. P2→P1: 0.72 / 0.68, 0.49 / 0.47, 0.24 / 0.19. P1→P2: 0.58 / 0.62, 0.52 / 0.53, 0.19 / 0.19 |
| Paired (DAC mlp minus AFx-Rep mlp, top-1, 95% over files) | within -0.10 [-0.11, -0.08]; P2→P1 -0.23 [-0.26, -0.20]; P1→P2 -0.07 [-0.09, -0.05] (newtest) |
| Known failures | PANNs features hardly separate drive models. DAC loses most on a new player (P2→P1). One MLP seed only |
| Ledger | `benchmark / - / encoder-swap-v1`, 48 rows, 2026-10-21T03:33Z, agent `learning` |
| Details | `docs/notes/encoder-swap-t195.md` |
