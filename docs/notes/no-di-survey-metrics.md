# No-DI survey, part 2: content-invariant tone scores and how to trust them ([T158](../../board/TASKS.md#t158 "No-DI method survey (human 2026-10-18"))

Owner: research. Task [T158](../../board/TASKS.md#t158 "No-DI method survey (human 2026-10-18"), part 2 of 2. Written 2026-10-18. Research only: no code, no method chosen, no Line 6 files, no sealed labels, no `data/presets` or `data/ir`, no Native run. Not legal advice.

**How to read the evidence marks.** Most papers below were read through a fetch tool that returns a model-written summary of the page, not the full text. Every fact marked **(S)** comes from such a summary or from search-result text and has not been checked against the paper. **(M)** is from memory. **(P)** is my proposal, not literature. Two summaries came back garbled or vague (arXiv 2504.04751 and 2604.22276), so nothing is claimed from them. Our own facts ([T032](../../board/TASKS.md#t032 "Wire AFx-Rep (ST-ITO) in as loss term embed_afxrep"), [T041](../../board/TASKS.md#t041 "End-to-end on a real song"), specialists) are from the repo files listed in the first paragraph of the task.

## The problem in numbers

- Admitted known-DI score `mrstft_v2` (+ `level_db`) was tuned on aligned audio. [T041](../../board/TASKS.md#t041 "End-to-end on a real song") measured one preset scoring 7.7, 11.0 and 22.7 on three Guitar-TECHS DIs, the 22.7 DI being palm-muted ([DECISIONS, T041](../DECISIONS.md)). So the nuisance is DI-wide (technique, guitar, register, level into the gain stage), not only note identity.
- Our only learned-embedding trust data: `embed_afxrep` on the proxy set had held-out rank-1 0.067 (chance 0.0625), pairwise 0.844, below `ltas_tilt` and `envelope` ([afx-rep.md](afx-rep.md)). That check used the same-pool "held-out notes" condition, not different guitars.
- Family terms already admitted or tried (`mod_distance`, time IR distance, linear response) all need the DI. In a no-DI or different-DI condition they would have to be re-derived; this note covers only the generic score.

## 1. Learned effect embeddings

| Model | Trained for | Data | How invariance was measured | Guitar? | Weights licence |
|---|---|---|---|---|---|
| AFx-Rep (ST-ITO, [arXiv 2410.21233](https://arxiv.org/html/2410.21233v1)) | Predict which effect/preset was applied, mid/side PANNs encoder | 63 VST plug-ins x 10 presets on 7 datasets, about 60 h (S) | Zero-shot style classification 0.86 vs 0.78 for DeepAFx-ST+, retrieval, parameter estimation vs CLAP; 23-engineer 0-100 similarity listening test (S). **The paper says it does not enforce content invariance, and that it does not work well for guitar tone matching (S, verify in the paper before quoting).** | Test content included IDMT-SMT-Guitar; no guitar tone-match success claimed | Apache-2.0 tag on the card; training sources include non-commercial sets ([afx-rep.md](afx-rep.md)) |
| Fx-Encoder (Koo et al., ICASSP 2023, [arXiv 2211.02247](https://arxiv.org/pdf/2211.02247)) | Contrastive: negatives share content but differ in effects | MUSDB18 with Fx-normalisation (S) | Disentanglement analysis plus mixing-style-transfer tests (S, details not read) | No (mixes) | Code MIT, checkpoints on Google Drive, trained on MUSDB18 only (S, [repo](https://github.com/jhtonykoo/music_mixing_style_transfer)); MUSDB18 is educational-use only (see afx-rep.md) |
| Fx-Encoder++ ([arXiv 2507.02273](https://arxiv.org/html/2507.02273v1), ISMIR 2025) | Instrument-wise effect embeddings from mixtures with an "extractor" | MoisesDB, 240 tracks, wet stems, Fx-normalisation (S) | No explicit content-invariance test (S). Retrieval of the right effect setting among 500 candidates: R@1 26.1, R@5 40.7 on MUSDB mixtures (S). Parameter matching with a differentiable chain, MR-STFT | Electric guitar not in the evaluation instruments (S) | Code **CC BY-NC 4.0**, weights `yytung/fxencoder-plusplus` on Hugging Face (S, [repo](https://github.com/SonyResearch/Fx-Encoder_PlusPlus)) |
| Open-Amp guitar encoder ([arXiv 2411.14972](https://arxiv.org/html/2411.14972v1), ICASSP 2025) | SimCLR: positives are different clips through the same effect model, negatives other models | 35 min of clean IDMT-Guitar through 160 NN captures (59 amps, 101 pedals), rendered online (S); 113k-parameter 1-D CNN, 64-d (S) | Content invariance is the training objective. Evaluated as classification: GFX 89.3% (mono discrete), cross-dataset 91.8 GFX, 72.0 EGFX, 91.1 EGDB, 99.8 Open30 (S); also mono vs two-string polyphonic input (S) | **Yes**, the closest match to our content-vs-effect split | Paper says package CC BY 4.0 (S); README gives no licence for the capture packs or the dataset; whether an encoder checkpoint is shipped is **unconfirmed** ([repo](https://github.com/Alec-Wright/OpenAmp)) |
| Tone embedding for one-to-many amp modelling (Chen et al., ISMIR 2024, [arXiv 2407.10646](https://arxiv.org/html/2407.10646v1)) | SimCLR with positives "same tone, different playing content" on mel input | 9 commercial amp models, 30 min clean guitar, CC BY 4.0 data (S) | t-SNE clusters by amp only; no retrieval figure, no listening test, no pickup or guitar discussion (S) | Yes | No checkpoints found (S) |
| EG-VAE (Yeh, Hung, Yang, [arXiv 2608.05513](https://arxiv.org/abs/2608.05513), Aug 2026) | VAE splitting frame-level content from a global tone code; tone transfer and tone removal (DI recovery) | EGDB dry DIs through Neural DSP plug-ins (218 seen and 96 unseen presets) plus EGDB-PG, about 758 h (S) | Preset classification from the tone code, pitch prediction from the content code (checks leakage both ways), t-SNE, perceptual path length, MOS tone similarity from 14 listeners (EG-VAE 4.30 vs 2.30 on seen tones) (S). Compared with HDemucs, DPTNet, DCUNet and Distortion Recovery, **not** with AFx-Rep, Fx-Encoder, CLAP or MERT (S) | Yes, newest relevant work | Paper CC BY 4.0 (S); checkpoints and code not stated (S). **Newer than the plan lists; add to the watch list.** |
| TRR, Texture Resonance Retrieval in TimberAgent ([arXiv 2603.09332](https://arxiv.org/html/2603.09332)) | Gram matrix of three mid-level wav2vec2 layers, random projection to 32 channels, 1024-d, no training | 1,063 knowledge-base presets, 204 queries, guitar effects (S) | Retrieval of preset parameters: cosine 0.825 vs 0.529 (wav2vec mean pool) and 0.573 (CLAP); 26-person listening study matched a MusicGen baseline (S). Authors list real-audio robustness and cross-instrument transfer as unverified (S) | Yes, synthetic queries | Code promised on acceptance (S) |
| General: MERT, CLAP | Music tagging and audio-text | Large | Not built for effects. MERT-v1 FAD showed very weak correlation with human ratings in one environmental-sound study ([arXiv 2403.17508](https://arxiv.org/pdf/2403.17508), S) | n/a | MERT-v1 **CC-BY-NC-4.0** (S); LAION CLAP checkpoints vary by variant, one music variant is CC BY-NC-SA 4.0 (S); the base licence was not found |

Observations that matter for us:

1. **Nobody publishes the measurement we need.** The literature measures effect or device classification, retrieval among presets, and parameter regression, almost always with held-out content but a single instrument class and fixed hardware. Only Open-Amp trains on "same effect, different clip", and it reports classification of discrete devices, not fine knob sensitivity. Nothing found tests a different guitar or pickup against the same effect.
2. Fx-Encoder++ retrieves the exact setting from 500 candidates only 26% of the time at mixture level (S). Fine resolution under content change is hard even for the best-resourced model.
3. Our target needs fine sensitivity (knob-level, neighbouring models). Classification accuracy on 160 discrete devices is a weak predictor of that, and AFx-Rep's own [T032](../../board/TASKS.md#t032 "Wire AFx-Rep (ST-ITO) in as loss term embed_afxrep") result fits this.
4. **Licences.** Fx-Encoder++ (CC BY-NC code), MERT (CC-BY-NC) and AFx-Rep (training on non-commercial data) share the "private research yes, bundled release unsettled" class. A Native-rendered retrain would be derived from Native output (see PLAN legal section); a retrain on hardware renders or on our own non-Line-6 chain would avoid that. Send to `reviewer` before any bundle decision.

## 2. Hand-built statistics, and what each is sensitive to

Robustness columns are **my analysis (P)** unless a source is given. "Notes" = pitch and rhythm of the played passage, "technique" = palm mute, pick attack, dynamics, "pickup" = pickup type and position and the guitar's own tone settings.

| Statistic | Measures | Notes | Technique | Pickup | Comment |
|---|---|---|---|---|---|
| Long-term average spectrum, tilt | Cab, EQ, amp voicing | Medium: LTAS of a short passage follows the note distribution; improves with 30 s plus | Medium (palm mute moves the low-mid balance and drive level) | **High**: pickup position gives comb-like notches, neck vs bridge changes the fundamental/upper balance (physics: [till.com](https://till.com/articles/PickupResponse), [Lollar](https://www.lollarguitars.com/inductance); no quantified study in amp-model literature found) | Same quantity as the cab/EQ cue, so removing pickup sensitivity risks removing signal |
| Crest factor, spectral flatness, HNR | Distortion amount | Low | **High**: attack and sustain change crest and flatness; level into the gain stage is set by the DI | Medium | Gain-stage input level is the hidden variable, not the pickup alone |
| Envelope and attack statistics | Compression, gating | Medium: depends on onset density | **High** | Low | Needs the passage's onset density matched |
| Harmonic distortion measures (harmonic ratios at fixed fundamental) | Clipping asymmetry, drive | **High** unless computed per pitch | Medium | Medium | Hard to compute on polyphonic material without pitch tracking |
| Modulation spectrum (envelope modulation 0.2 to 25 Hz, per band) | Chorus, tremolo, phaser, vibrato | Medium: playing rhythm adds its own peaks | Medium | Low | Already in `mod_specialist` with the DI; no-DI version needs the playing rhythm removed |
| Delay/reverb measured directly (autocorrelation of onset envelope, tail decay) | Time effects | Medium (needs sparse onsets or sustained tails) | Medium | Low | Content-sensitive in a different way: requires isolated notes |
| Sound-texture statistics (McDermott and Simoncelli, [2011](https://mcdermottlab.mit.edu/papers/McDermott_Simoncelli_2011_sound_texture_synthesis.pdf)): subband-envelope moments, cross-band correlations, modulation power | Time-averaged texture of a signal | By design low for textures; **untested for pitched guitar** | Medium | Medium | Built for stationary textures, not harmonic instruments (M). Related: Gram-style audio texture losses ([Gram-matrix audio textures, arXiv 1806.08002](https://arxiv.org/abs/1806.08002), S) and TRR above |
| Distribution distances: MMD over frame features, kernel audio distance ([KAD, arXiv 2502.15602](https://arxiv.org/html/2502.15602v1)); sliced Wasserstein (M) | Whether two clips draw frames from one distribution | Depends on the frame features: raw embeddings carry pitch, so use pitch-light features (MFCC/cepstral envelope, log-mel with pitch removed) (P) | Medium | Medium | KAD claims faster convergence at small sample sizes and better human alignment than FAD (S); designed for generative-model evaluation, not for knob sensitivity |
| LFO-phase handling | Mod effects with unknown starting phase | n/a | n/a | n/a | Take the best of several phase offsets: ([arXiv 2601.04867](https://arxiv.org/pdf/2601.04867), S). Our `mod_distance` is already phase-invariant by using statistics over time |

Takeaway: the statistics that are most content-free (texture, modulation, distribution) say least about gain stage detail; the ones that say most about voicing (LTAS, crest) are the ones the guitar and technique move. A score that is robust is probably a bundle plus an explicit nuisance model, not one number.

## 3. How tone similarity metrics are validated in the literature

- **Human listening.** ST-ITO: 23 audio engineers, 0 to 100 similarity (S). EG-VAE: 14 listeners, tone similarity and quality 1 to 5 (S). LLM2Fx-Tools ([arXiv 2512.01559](https://arxiv.org/html/2512.01559v2)): MUSHRA with 16 participants, plus MR-STFT, DSP-feature and AFx-Rep/Fx-Encoder similarity as objective metrics (S; weights CC BY-NC-SA 4.0 (S)). TimberAgent: 26 participants (S). Guitar amp models: ABX and MUSHRA for realism and preference ([Wright et al., DAFx](https://dafx.de/paper-archive/details/FnATd0Lou6FQKgN2tAfAqg), S). FAD against human ratings: Spearman about 0.5 at best, MERT-FAD very weak ([arXiv 2403.17508](https://arxiv.org/pdf/2403.17508), S).
- **Triplet or pairwise tasks.** I found no guitar-tone triplet benchmark. Image and audio perceptual-similarity work (CDPAM, [arXiv 2102.05109](https://arxiv.org/pdf/2102.05109)) trains on human same/different judgments; none is for effect chains.
- **Parameter recovery with mismatched source.** ST-ITO's parameter-estimation task processes a random recording with the target setting to make the reference, then optimises on a different random recording as input, reporting MSE and correlation per parameter (S). That is the one published "different content" recovery protocol; it uses speech/music, not guitar, and single effects. Yu et al. 2025 report up to 33% lower parameter MSE with a Gaussian prior on vocal effects (MedleyDB) ([arXiv 2505.11315](https://arxiv.org/abs/2505.11315), S).
- **Gaps for us.** (a) No study correlates an embedding distance with listener ratings when the two clips differ in notes and guitar. (b) No study varies pickups. (c) ST-ITO says it fails on guitar tone matching (S). So **we have to build the different-DI check ourselves**, and the human listening part (the ear is the final judge) is for the human, not for scoring by an agent.

## 4. Pickup and guitar mismatch compensation

- **Literature.** Nothing found on compensating pickups in a tone-matching loss. Neural amp literature treats input guitar as a data-diversity issue (EGDB, Guitar-TECHS uses three players with different instruments and hardware, [arXiv 2501.03720](https://arxiv.org/pdf/2501.03720), S) and the 2025 EGDB-PG paper on tone and content variation ([arXiv 2504.07406](https://arxiv.org/pdf/2504.07406); text not readable through the fetch tool, so not summarised).
- **Closest precedent for normalising the source.** Fx-Normalization ([sony/fxnorm-automix](https://github.com/sony/fxnorm-automix), not read) is used in Fx-Encoder and Fx-Encoder++ training to make existing effects and spectrum equal across samples (S). It normalises training inputs, not the comparison at test time.
- **Options to test (P):**
  1. *Reference-DI spectral normalisation*: flatten each DI to a common long-term average spectrum before rendering. This equalises the DI side but not the target's unknown source, so a residual tilt stays in the target.
  2. *Nuisance-EQ profiling inside the score*: compare target to candidate after fitting a low-dimensional correction (tilt plus one shelf, bounded to a plausible pickup range) and report the fitted correction as a diagnostic. Risk: it absorbs true EQ and cab differences. The bound and the DoF are the knobs to study.
  3. *Pairing by register and style*: choose reference DIs whose pitch range, density and technique resemble the target's (estimated by pitch tracking on the separated stem). The [T041](../../board/TASKS.md#t041 "End-to-end on a real song") palm-muted DI shows this matters; cost is a larger DI library (the Guitar-TECHS P3_music `directinput_04` clip is reserved for specialist calibration).
  4. *Several DIs per candidate and a robust aggregate* (median, or min over DIs). Costs K renders per candidate; [T041](../../board/TASKS.md#t041 "End-to-end on a real song") already noted 3x.

## Ranked shortlist for `loss-trust-check` in a different-DI condition

Rank is by expected chance of passing times cost, and is a hypothesis to test, not a recommendation of a method.

| Rank | Candidate | Why | Cost | Licence |
|---|---|---|---|---|
| 1 | `ltas_tilt` with low-DoF nuisance-EQ profiling (and the unprofiled version as control) | Cheapest; tests the single most pickup-sensitive cue; shows how much the profile absorbs | milliseconds | none |
| 2 | Texture/modulation bundle: subband-envelope moments, cross-band correlations, modulation power (extends `envelope`, `mod_periodicity`) | Time-averaged, content-agnostic by construction; no weights | milliseconds | none |
| 3 | Open-Amp-style guitar encoder (`embed_openamp`): first confirm a checkpoint exists and its licence; otherwise treat as a retrain candidate trained on hardware renders or non-Line-6 chains | Only published encoder trained on "same effect, different clip" for guitar | about 0.01 to 0.1 s per clip (small CNN, (S) 113k parameters) | unconfirmed; check before use |
| 4 | EG-VAE tone code (watch: checkpoints not stated); TRR Gram descriptor (no training, few lines) | New, closest in spirit; TRR is nearly free to try | TRR: one wav2vec2 pass | EG-VAE unknown; wav2vec2 (M) licence to check |
| 5 | Distribution distance (KAD/MMD or sliced Wasserstein) on pitch-light frame features | Uses whole-clip frame statistics; needs several seconds of audio | low | KAD toolkit open (S) |
| 6 | `embed_afxrep` (baseline) and `embed_fxenc` (MIT code, MUSDB-trained) | Already wired or cheap; AFx-Rep weak on proxy and said to fail on guitar (S) | 0.065 s per 10 s clip | see afx-rep.md |
| 7 | Fx-Encoder++ | CC BY-NC, no guitar in its evaluation, 26% R@1 on its own task (S) | model-sized | CC BY-NC 4.0 |
| control | MERT/CLAP, `mrstft_v2` unaligned | Negative controls: expected note- and DI-dependent | per model | MERT CC-BY-NC |

Direct measurements (`delay_echo`, `reverb_tail`) stay as seeds and diagnostics; they are not part of this shortlist because they work only on suitable passages.

## Protocol for the different-DI trust check (P)

**Conditions.** For each true preset P with target T = P(DI_a), score true and distractor candidates rendered on DI_b:

| Condition | DI_b relative to DI_a | Tests |
|---|---|---|
| C0 same audio | identical | ceiling; reproduces the known-DI figure |
| C1 same guitar, other passage | same player and take, different notes | note invariance (the existing kit test) |
| C2 other technique/register | same guitar, palm-muted vs open, low vs high | gain-stage input and technique |
| C3 other guitar | different player and instrument (Guitar-TECHS has three) | pickup and guitar |
| C4 synthetic pickup perturbation | DI_a with a known tilt or comb applied (for example +-3 dB tilt, notch pattern from a pickup-position model) | isolates the pickup effect from notes; cheap to sweep |

**Candidates per item.** The true preset, same-family models at the same knobs, the true model with knobs moved by 10% and 25% of range, a different-family chain, and DI-only. Report 24 neighbours as in the [T122](../../board/TASKS.md#t122 "Family dispatch and time-based specialist") trust bar so the numbers compare.

**Splits.** Fit every free choice (EQ-bound, feature set, weights, aggregation) on a train split of presets and DIs; score once on held-out presets x held-out DIs. Keep specialist calibration DIs out. Exact set names and thresholds belong to `benchmark`; I did not open any set.

**Metrics.**
1. Rank-1 and pairwise win of the true preset against each distractor type, per condition.
2. Invariance ratio R = median over items of d(P(a), P(b)) divided by median over distractors of d(P(a), Q(a)): same preset on another DI against a different preset on the same DI. R below 1 means the term sees the preset before the DI; the [T041](../../board/TASKS.md#t041 "End-to-end on a real song") numbers (7.7 vs 22.7) suggest `mrstft_v2` has R above 1 across techniques.
3. Candidate-order agreement: Spearman correlation of the ordering of the 25 candidates between two other DIs. Needs no labels, so it can also run on real songs.
4. For terms with a nuisance fit: the fitted correction size by condition (should stay near zero in C0 to C2 and grow with C3, C4).

**Bars (suggested for `benchmark` to set).** A term is "different-DI trusted" if C2 and C3 rank-1 are at least 0.9 of its C1 rank-1, and C1 matches the admission threshold of the existing rule. With n items the binomial standard error at 0.8 is 0.4/sqrt(n), so 60 items resolve a 0.10 gap only just; use at least 60 items and 3 or more DIs per condition, and report intervals.

**Cheapest discriminating experiment (P).** Reuse renders that already exist rather than rendering new ones: the family-specialists note says the time-specialist training used single-block renders of the helix-trust-v2 **train** split on 96 different DIs. If those renders are still cached, compute for each shortlist term, on that train split only, the invariance ratio R and the candidate-order agreement (metrics 2 and 3). It needs no Native run and no labels beyond the preset id, and costs minutes (embedding about 3 min for 3,000 clips at 0.065 s with AFx-Rep; statistics are seconds). Add the C4 synthetic perturbation on a handful of DIs for the pickup question. Terms with R near or above 1 drop out before any rank-1 test spends renders. I did not check that the cache exists (unverified), and the held-out half stays unread until the one scored run.

## What this does not settle

- Whether any published encoder keeps knob-level sensitivity under a changed guitar. No source tests this; our check is the first evidence.
- Whether the Open-Amp encoder ships weights and under what licence, whether EG-VAE releases a checkpoint, and the licence of the base CLAP checkpoint. All three need a direct look.
- Whether Distortion Recovery, EG-VAE tone removal or Take et al.'s chain-and-dry recovery ([DAFx 2024](https://dafx.de/paper-archive/details/ndvW6hLizkhxZP2em_SaEA)) could supply an estimated DI and so reopen the known-DI score. That belongs to part 1 of [T158](../../board/TASKS.md#t158 "No-DI method survey (human 2026-10-18").
