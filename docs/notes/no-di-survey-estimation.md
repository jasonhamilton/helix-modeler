# No-DI survey: estimating the chain and settings without the dry signal ([T158](../../board/TASKS.md#t158 "No-DI method survey (human 2026-10-18"), part 1)

Owner: `research`. Read 2026-10-18. Research only: no method is chosen and no code was written. Not legal advice. Sealed labels, `data/presets`, `data/ir` and Native were not touched. Project facts come from `docs/PLAN.md` Step 3, `docs/notes/dry-signal.md`, `docs/notes/synthetic-benchmark-design.md`, `docs/notes/afx-rep.md` and the `presence-v1` ledger rows.

How to read the evidence tags: **[V]** = read in the paper's HTML/abstract page or the repo page this session. **[S]** = from a search-result summary only. **[M]** = from memory, not verified. Where I could not open a paper body (PDF text extraction failed, Springer paywall redirect), the number is not quoted.

## Bottom line

1. Nobody in the literature does what we need end to end. Every comparable system predicts or searches a **small** effect set (3 to 13 effect types, 5-effect chains, or one effect at a time). I found no paper that identifies one model out of hundreds of near-synonymous amp, drive or reverb models from wet audio alone. Treat model-identity accuracy at our scale as unknown, not as "probably fine".
2. ST-ITO's own authors list "guitar tone matching" as a case where it does not work well [V, [HTML](https://arxiv.org/html/2410.21233v1)]. Search with a learned embedding is therefore not a drop-in answer to the mismatched-source problem for guitar.
3. Making paired training data by rendering **real** DI recordings through random plugin chains is exactly what the comparable work does [V, table below]. I found no study that trains on transcribed-and-resynthesized guitar [absence not verified].
4. Dry recovery has two 2024 to 2026 lines that are close to our case (Distortion Recovery, EG-VAE), but both train and test on other vendors' amps (BIAS FX2, Neural DSP) and none of the pages I read tests separated stems from mixes. Weights are not confirmed released for any of them.
5. The most recent hybrid (predict, then CMA-ES) is Okita and Katayose (April 2026). It is the closest published template for "predictor seeds plus search", but it covers three effect types and gives no uncertainty.

## How the comparable work makes its data (answers "do they synthesize their own audio?")

Yes, all of them render real clean guitar through plug-ins. Sizes:

| Work | Dry source | Wet generation | Reported size | Tag |
|---|---|---|---|---|
| [Comunità et al.](https://arxiv.org/html/2012.03216v1) | IDMT-SMT-Audio-Effects: 624 mono notes, 420 poly chords, two guitars | 13 overdrive/distortion/fuzz plug-ins, 2 to 3 knobs each | about 250 h (the [repo](https://github.com/mcomunita/gfx_classifier) says about 305 h over four sub-sets) | V |
| [Open-Amp](https://arxiv.org/html/2411.14972v1) | IDMT-Guitar, 35 min clean | 59 amp and 101 pedal neural captures (Proteus tone packs) | 230 h from 35 min | V |
| [Okita and Katayose 2026](https://arxiv.org/html/2604.22276) | 2,231 ten-second excerpts from IDMT-SMT-Guitar, GuitarSet, EGDB, Guitar-TECHS | Pedalboard chorus, distortion, reverb; 33 wet versions per dry clip; split with no dry-track overlap | 205 h | V |
| [Distortion Recovery 2024](https://arxiv.org/html/2407.16639) | 14 professional guitarists | BIAS FX2 with 90,000+ ToneCloud presets | 80 h pairs | V |
| [EG-VAE 2026](https://arxiv.org/html/2608.05513v1) | EGDB, about 22 h | Neural DSP Archetype Cory Wong X, 474 seen tones; Morgan Amps Suite as unseen tones | about 758 h | V |
| [GOAT 2025](https://arxiv.org/html/2509.22655) | 5.9 h DI | Neural Amp Modeler, about 7,000 profiles, plus IR and optional room | 29.5 h augmented; dataset by request, research only | V |
| [AFx-Rep / ST-ITO](https://arxiv.org/html/2410.21233v1) | seven datasets (music, speech, instruments) | 63 free VST3 plug-ins, 10 k-means presets per effect | about 60 h | V |
| [Hayes et al., flow matching 2025](https://arxiv.org/html/2506.07199) | none (synth) | Surge XT, random parameters | 2 million renders per variant | V |
| [Bruford et al., AST sound matching 2024](https://arxiv.org/html/2407.16643v1) | none (synth) | Massive via Pedalboard | 1 million renders, 22 GB, about 24 h on a 2018 Mac mini | V |

Reading for us: tens to hundreds of hours is normal for 13 to 500 tones; the number of distinct DI hours can be tiny (Open-Amp 35 min) and still works for classification. Our 5 h of Guitar-TECHS DI (three players, 4,444 clips) is in the usual range. A Native budget comparable to Okita (73 k renders) is about 4.8 h at the 8-host cap if the 1.883 s per render in `synthetic-benchmark-design.md` scales linearly (parallel scaling not measured; clip length of that rate not checked). **Constraint specific to us:** Native renders only a GUI-captured template per chain structure with 16 knobs and 16 switches (`synthetic-benchmark-design.md`), so Native cannot generate the "random model in every slot" data the literature uses; that needs many templates or the serial Stomp (2.7 s per render). This limits what any predictor can learn about model identity from Native alone.

## 1. Blind estimation of type, chain and parameters from wet audio

| Work | Needs | Reported accuracy and data | Code and licence | Fit to us |
|---|---|---|---|---|
| [Comunità 2021](https://arxiv.org/html/2012.03216v1) | Paired renders, small CNN, CPU-trainable scale [M on compute] | 13 effects: 86.3% mono, 88.4% poly (discrete settings), 90.9% / 91.4% continuous; parameter MAE below 0.05 mostly. Discrete-trained nets are biased to the grid. Not tested on unseen guitars or dry input [V] | [gfx_classifier](https://github.com/mcomunita/gfx_classifier) BSD-3-Clause, dataset on Zenodo [V] | Shows type plus knob regression is feasible for drives. 13 classes, two guitars. |
| [Hinrichs 2022](https://doaj.org/article/aa41cd04717743b5b0c1fc3b97a16ad9) | Paired renders of guitar in mixes with bass, keys, drums | Up to 97.4% classification; parameter MAE below 0.05 for chorus, phaser, reverb, overdrive, inside mixes [S] | Not checked | Closest to "effect inside a mix". Small effect set. |
| [Rossi, Iacca, Turchet, DAFx 2025](https://www.dafx.de/paper-archive/details/UqJ5fuXHsDVpnOoEQm70OQ) | Four real guitars recorded, 5-effect chains, three difficulty levels (fixed; random parameters; random order and parameters) | Tremolo is the hardest class. Numbers not read (PDF text not extractable here) [V for abstract only] | No code link in the paper [V] | Confirms modulation is hard to detect without the dry signal, in line with our F1 0.22 to 0.39 on EQ/mod/delay/reverb. |
| [BE-AFX auto-encoder 2023](https://arxiv.org/html/2310.11781v1) | MUSDB18 10 s clips; EQ, compressor, clipper in DDSP or neural proxies | Audio-domain loss gives better perceptual match (mel loss 0.40 vs 0.49) than parameter loss, while parameter MSE is better with parameter loss (0.072) [V] | "Implementation on GitHub" per paper [V]; licence not checked | Supports scoring in audio space, not parameter space. Only 3 mastering effects, differentiable. |
| [Okita and Katayose 2026](https://arxiv.org/html/2604.22276) | 205 h, one dry-and-type predictor then search | Wet reconstruction SI-SDR 23.07 dB (Dry-Type-Direct + search) vs 18.18 dB (iterative predictor only); chains up to 3 of {chorus, distortion, reverb} [V] | Demo only; no repo or weights [V] | Direct precedent for predict-then-search. Tiny type set. |
| [StemFX 2026](https://arxiv.org/abs/2607.15634) | Transformer decoder predicts tokenised FX chains; about 105 K songs separated into pseudo-stems; 85 effects from 7 Python libraries | "Outperforms all baselines at all chain lengths" for retrieval; "over 4000 times faster than iterative optimization" [V abstract only]. Per-type accuracy not read | Not checked | The only work I found that trains on **separated stems** and predicts a chain. Closest to our input. Mixing-style effects, not guitar amp models. |
| [Fx-Encoder++ 2025](https://arxiv.org/html/2507.02273v1) | MoisesDB, 1 to 4 instruments per mixture, 8 effect types | Recall@1 26.1% drums, 22.1% bass, 14.2% vocals (retrieval, mixture level) [V]. No guitar numbers read | Code and weights [CC BY-NC 4.0](https://github.com/SonyResearch/Fx-Encoder_PlusPlus) [V] | Embedding not a model-id classifier. NC licence blocks bundling. |
| [Open-Amp 2024](https://arxiv.org/html/2411.14972v1) | 230 h, contrastive training | Beats baselines "in six out of eight cases" on the GFX classification task; does not estimate amp settings [V] | Dataset and tone-pack licences not stated [V] | Contrastive encoders trained on many amps are the most credible route to many-amp identity, but untested at our scale. |

**Simulation-based inference and amortized posteriors.** I found no audio-effect paper using SBI proper. The nearest are audio-synth inverters that output a distribution: [Hayes et al. (ISMIR 2025)](https://arxiv.org/html/2506.07199) use a permutation-equivariant flow-matching posterior over Surge XT's 165 parameters (including one-hot discrete ones), reported to beat an AST regression baseline, more so on the full parameter set [V]; the code is on the authors' site, licence not checked. [MNPE (May 2026)](https://arxiv.org/html/2605.13551v1) handles mixed discrete and continuous parameters and is said to be available in the `sbi` package [V on the paper page]; the sbi repo page I read does not mention MNPE, only MNLE [V], so check the installed version. `sbi` is Apache-2.0 [V]. MNPE was validated on 25 discrete configurations, not hundreds [V]. A posterior head matters for us because Helix models are confusable families (several amps share a voicing) and a regressor collapses to the mean of the family, which is the failure Hayes et al. attribute to unaddressed symmetries.

**Blind system inversion** ([Hinrichs 2024](https://asmp-eurasipjournals.springeropen.com/articles/10.1186/s13636-024-00330-0), abstract via search [S]): learns a neural copy of a reference effect instead of identifying a plug-in; tested on phaser, soft-clip and slapback only. It yields a neural clone, not Helix block settings, so it does not output what we must export.

**Many discrete models: what is known.** Highest class counts in the evidence: 13 (Comunità), 59 amps plus 101 pedals (Open-Amp, classification numbers not read), 85 effects (StemFX, per-type results not read). No paper reports top-1 identification of a specific Helix-like model out of hundreds. Our own `presence-v1` test numbers are the only local evidence (single Helix Native blocks on clean DI, 1,950 scored targets; ledger rows 776 to 805). Known DI: F1 drive 0.57, amp 0.65, cab 0.92, EQ 0.64, mod 0.70, delay 0.59, reverb 0.63, drive-or-amp 0.84. No DI: drive-or-amp 0.85, but EQ 0.22, mod 0.28, delay 0.39, reverb 0.31 (mostly false positives). Note these are single blocks on a clean DI, easier than chains on separated stems, and block presence is an easier label than model identity, so expect model identity to be worse than these numbers.

## 2. Dry-signal recovery

| Work | Needs | Reported | Heavy distortion and stems | Code and licence |
|---|---|---|---|---|
| [Imort et al. 2022](https://arxiv.org/html/2202.01664) | Paired: CEG 1.68 h clean guitar, SoX overdrive or hard clip (8.4 h total); SignalTrain-HC 24 h; Demucs 11 M params | De-overdrive Demucs SI-SDR about 31 dB; declipping about 8 dB on CEG-HC, about 15 dB on SignalTrain-HC; paper says quality falls as clipping severity rises and the problem is ill-posed for hard clipping. Only simple waveshapers, not amp emulations [V] | No stems tested [V] | No code release mentioned; demo site only [V] |
| [Distortion Recovery 2024](https://arxiv.org/html/2407.16639) | 80 h BIAS FX2 pairs, one RTX 4090; mel-denoiser Transformer plus HiFi-GAN, 101.7 M params | FAD 0.080, SI-SDR 28.65 dB vs Demucs v3 2.98 dB; MOS 3.54 quality, 3.86 dryness [V] | Same plug-in family in train and test as far as the page shows; per-gain breakdown and unseen-plug-in test not seen [V] | Demo page only; weights not confirmed [V] |
| [EG-VAE 2026](https://arxiv.org/html/2608.05513v1) | 758 h Neural DSP pairs, one RTX PRO 6000; VAE with content/tone split | Mel 1.10 vs Distortion Recovery 1.21 on seen tones; Mel 1.19 on unseen tones (Morgan Amps); dryness 3.42/5 vs 2.36/5. Smaller residuals on distortion-heavy tones [V] | Unseen **amps** tested, but all from one vendor family and one DI corpus; no separated stems [V] | No repo or weights licence stated [V] |
| [LOUDAR 2026](https://arxiv.org/html/2608.01972v1) | Unsupervised: latent diffusion prior on clean DI only, plus a 9.4 k-parameter latent operator fitted at inference (M=300 diffusion steps, 10 iterations each); GOAT supervised baselines | Best on FxEnc++ and CLAP, near LDM-Conditional on AFx-Rep; beats UnAFx; tested on EGDB, 238 clips over five amp conditions [V] | No real separated stems tested; run time and GPU not reported [V] | "Code on project website"; weights licence unstated [V] |
| [UnNAFx, DAFx 2025](https://arxiv.org/html/2504.04751) | Unsupervised: diffusion prior pretrained on EGDB (80 k iterations) and fine-tuned on 16 min IDMT guitar; wet audio as little as 18 s to 16 min; about 1 h optimisation at 16 min | Diffusion more stable with scarce data; adversarial better on heavy distortion. Distortion only [V] | Authors state the dry prior must match the target domain and the study covers memoryless distortion only [V] | [repo](https://github.com/michalsvento/UnNAFx); licence not stated on the page [V] |
| [Posterior-sampling diffusion 2025](https://arxiv.org/html/2501.05959) | Unsupervised: diffusion prior on clean guitar (IDMT 26.5 min) or speech | Blind, matches supervised baselines on perceptual metrics for hard/soft clip, wavefold, quantisation; underperforms them on reference-based metrics | Memoryless nonlinearities only; dynamic and memory effects unresolved [V] | Public GitHub per paper page; licence not checked |

How this applies to Helix: an amp-plus-cab-plus-delay-plus-reverb chain is not memoryless, so the unsupervised diffusion estimators (UnNAFx, posterior sampling) do not cover it as published. The supervised models are trained on one vendor's amps; EG-VAE is the only one to test unseen amps and it degrades gracefully (3.42 to 3.50 subjective, which is a single-vendor result). None of the pages I read evaluates a separated guitar stem from a full mix, so the accuracy of recovery from our separator output is unmeasured. Dry recovery also needs a GPU for training (RTX 4090 class in the paper) and is hours of training, which matches the plan's "GPU used for training" statement.

## 3. Search with a mismatched or unknown source

- **ST-ITO** ([paper](https://arxiv.org/html/2410.21233v1), code Apache-2.0 per `docs/notes/afx-rep.md`): CMA-ES with population 64, max 25 steps, sigma 0.3, start at 0.5; about 1 min inference; its benchmark deliberately uses **different content** for input and reference; AFx-Rep trained on about 60 h from 63 plug-ins [V]. The authors state it "does not work well for challenging applications, such as guitar tone matching" and that it needs the effect chain supplied [V]. Our own proxy trust check had AFx-Rep at rank-1 0.067 against a chance of 0.0625 (`afx-rep.md`, a proxy not Helix), consistent with that caveat.
- **Gaussian prior for ITO** ([Yu et al. 2025](https://arxiv.org/abs/2505.11315)): MAP version, parameter MSE down by up to 33%, vocals, prior fitted on the DiffVox preset set [V]. The idea transfers: our fitted preset prior (community presets) could play the same role. Not tested on guitar.
- **Zero-shot amp modelling with a tone embedding** ([Chen et al. 2024](https://arxiv.org/html/2407.10646)): the reference may have different content from the input, trained by picking an unpaired reference with the same amp tone; encoder from Positive Grid trained on a large proprietary set; code and weights not stated [V]. Evidence that a **content-invariant tone embedding for guitar** is trainable, not that it can be used off the shelf.
- **Analysis-by-synthesis with distribution losses** on guitar: no published guitar result found that scores Helix-like blocks with a statistics loss under DI mismatch [not found]. UnNAFx and LOUDAR use embedding/distribution metrics as **evaluation**, not as search losses [V].
- Local evidence that DI dependence is large: [T041](../../board/TASKS.md#t041 "End-to-end on a real song") preset loss 7.7 vs 11.0 vs 22.7 over three DIs (given in the task brief; the run is in DECISIONS).

## 4. Hybrids

- **Predict, then search**: Okita and Katayose do this on three effects, with CMA-ES or TPE and a budget M = floor(M0 * d^1.5) in the number of search dimensions d (M0 5 to 20) [V]. Table 6 evaluates with ground-truth dry signals to isolate the effect, so their "blind" number is not a clean no-DI result [V]. No uncertainty is used [V].
- **Predictor uncertainty to size the search**: not found in the audio-effect literature [not verified as absent]. Ingredients exist: Hayes et al.'s flow posterior gives samples and MNPE gives calibrated discrete posteriors [V]; deep ensembles would be the cheap alternative [M].
- **Black-box optimisation to invert a known effect type** ([Sun, Fourer, Maaref](https://arxiv.org/html/2607.19645), compressors, needs dry reference features): shows Pattern Search beating Bayesian optimisation on compressor parameters; it needs dry-signal statistics, so it is not for us [V]. Date on page August 2026 although the id is 2607; unclear which is the posting date.
- **Speed**: StemFX claims over 4,000 times faster than iterative optimisation for style transfer [V abstract].

## Licence flags (for `reviewer`)

| Item | Licence | Concern |
|---|---|---|
| Fx-Encoder++ code and weights | CC BY-NC 4.0 [V] | No bundling; fine for private research |
| AFx-Rep weights | Apache-2.0 tag; trained on MUSDB18 and others (`afx-rep.md`) | Training data terms unsettled |
| gfx_classifier code | BSD-3-Clause [V] | Clean |
| sbi | Apache-2.0 [V] | Clean |
| GOAT | By request, research only [V] | Do not train a distributed model on it without the terms |
| Distortion Recovery, EG-VAE, Okita, LOUDAR, Hayes, UnNAFx | Papers CC BY 4.0; code/weights licence missing or unchecked | Confirm before reuse |
| Pedalboard (GPLv3) used by Okita, Bruford, GOAT | GPLv3 | Reproduce only behind a process boundary |
| Guitar-TECHS, EGDB-PG | CC BY 4.0 (`dry-signal.md`) | Keep attribution |

## Ranked shortlist, with the cheapest discriminating experiment

All experiments below run on `synthetic-v1` or its renders and on DIs we already hold. None needs a new method choice.

**1. Learned wet-audio predictor with per-slot model heads and a calibrated posterior (top-k, entropy).** Why first: it is the only option that is instant at run time, it matches everything the comparable work does (render real DI through random chains), and it gives block presence the detector currently misses on EQ/mod/delay/reverb. Open risk: model identity at hundreds of classes is unevidenced. *Cheapest experiment:* train one small CNN (Comunità scale) with multi-task heads (block presence, family, model id) on a few thousand to tens of thousands of existing Native renders, with Guitar-TECHS player P3 held out as DI and one chain template held out. Compare per-block F1 and top-1/top-5 model accuracy against the `presence-v1` no-DI numbers on the same renders. If the net does not clearly beat the no-DI F1 of 0.22 to 0.39 on EQ/mod/delay/reverb at held-out DI, the learned predictor is not worth scaling. Needs a GPU only if the net grows; a small CNN trains on the Mac [M].

**2. Predict-then-search hybrid with a search budget set from predictor entropy.** The published template is Okita and Katayose. *Cheapest experiment (no training):* an oracle-noise ablation. Seed the existing search with the true labels corrupted at set rates (block flips, wrong model within family, parameter noise), then plot recovery against budget on the 6-item CI set. This gives the predictor accuracy required for seeding to beat the default start, and whether entropy-sized budgets could save renders, before any predictor exists.

**3. DI-invariant search loss (content-invariant tone embedding or statistic losses).** ST-ITO says guitar tone matching is hard, so this is a bet on a learned encoder trained on our own renders (Open-Amp style contrastive, positives = same chain on different DI) rather than AFx-Rep as is. *Cheapest experiment (reuse renders):* a DI-invariance score per candidate loss: median distance between the same chain rendered on two different DIs divided by the median distance between different chains on one DI. Compute for `embed_afxrep`, `ltas_tilt`, `envelope` and any statistic loss. A loss with ratio near or above 1 cannot work under DI mismatch. Needs renders of the same chains on 2 to 3 DIs, which [T041](../../board/TASKS.md#t041 "End-to-end on a real song") may already hold.

**4. Dry-signal recovery in front of the known-DI pipeline (Distortion Recovery or EG-VAE style, trained on Helix renders).** Highest ceiling (restores sample-level losses) but needs a GPU, a few hundred hours of Helix-rendered pairs, and the stem-domain gap is untested. *Cheapest experiment (no training):* a tolerance sweep. Replace the true DI by a degraded DI at SI-SDR of 5, 10, 15 and 20 dB (additive other-DI bleed plus mild filtering) and measure how recovery rank-1 and preset loss degrade. Compare with published SI-SDR (28.65 dB in-domain for Distortion Recovery, about 8 to 31 dB in Imort et al. depending on effect). If recovery at 15 dB SI-SDR is already worse than the reference-DI-library baseline (option B), drop option 4.

**5. Amortized posterior head (flow matching or MNPE) in place of the plain classifier in option 1.** Potentially the right treatment for confusable model families, but it adds a training stack. *Cheapest experiment:* only after option 1 exists. Fit a mixture-density or ensemble variant on the same data and compare calibration (expected calibration error) and top-5 recall on the confusable families; if top-5 recall of plain softmax is already above about 0.9 on families, skip.

Not recommended: unsupervised diffusion blind inversion (UnNAFx, posterior sampling). Published only for memoryless distortion, needs a differentiable or learned effect model, and about 1 h of optimisation per case.

## What I could not verify

- Parameter/class-level results of Hinrichs 2022 beyond search snippets; Rossi et al. numbers; StemFX type accuracy; Open-Amp classification numbers.
- Whether any 2024 to 2026 dry-recovery model has public weights, and the licence of the Hayes, UnNAFx and LOUDAR code.
- Whether any work tests dry recovery or effect estimation on separated stems from full mixes with a guitar-specific model (StemFX does stems but not amp models).
- That no paper trains on transcribe-and-resynthesize guitar; I searched for it and found none, which is not proof.
- The date of the Sun, Fourer, Maaref paper (page says August 2026, id says 2607).
- Whether `sbi` ships MNPE in the version we would install.
- Training compute for the predictor options on this machine; the GPU the project has is not stated in the files I read.
