# Identifying a distortion pedal's three stages from audio, and the Native measurement experiment ([T167](../../board/TASKS.md#t167 "T167 distortion from first principles"), part c)

Owner: `research`. Written 2026-10-18. Research and design only: no code, no renders, no sealed labels, no Line 6 files. Not legal advice.

**Everything below is a PROPOSAL.** The human's standing rule (DECISIONS, 2026-10-18, "model and method decisions require the human's approval") applies: no score, method, test set, pass bar or gate is adopted or registered by this note. Where a choice exists, the options are listed with a recommendation; numbers marked "placeholder" are there so the human has something concrete to accept, change or reject.

**Evidence tags.** **[V]** = I read the page or abstract this session. **[S]** = from a search-result snippet or a fetch-tool summary of the page (not checked against the paper body). **[M]** = from memory, not verified. **[P]** = my own derivation or proposal, not from the literature. Several PDFs could not be read here (text extraction failed, TLS error, binary); those are listed at the end.

## 0. Summary

1. **Most of the measurement machinery already exists.** `probe-v1` and the `coarse-v1` fingerprints (T046/T047) already hold an ESS at four levels, 8-level stepped sines at 110/440/1760 Hz, two tones, a static curve, a transfer curve, DC, THD and odd/even for all 61 drive models at about 6 settings each ([block-probe.md](block-probe.md), [native-spike.md](native-spike.md); `cache/probe/fingerprints/block-library-v1.2/index.json` is on disk). This note does **not** propose re-doing that. The gaps are listed in section 2.
2. **The stage model is identifiable from a dry+wet pair, with one fixed ambiguity.** A Wiener-Hammerstein (pre-EQ, memoryless clipper, post-EQ) system is identifiable from sweeps at several levels except for a scale convention (input gain versus clipper input scale). The pre/post split comes from how the harmonic-order impulse responses shift with level (section 3.3, [P], to be validated on synthetic systems first).
3. **Blind (no dry) identification recovers a family, not a model.** What can be read from the output alone is clipping hardness, symmetry, gain-versus-sustain and a rough post-EQ, each confounded by the player's input level, pickup and the later amp and cab. No paper does model-level blind identification of a pedal out of dozens. Treat model-level blind accuracy as unknown.
4. **The experiment** is about 732 full-probe settings plus about 1,830 reduced settings on the 61 drive models: 5 to 7 host-hours of rendering, 15 to 40 minutes of wall time at 3 to 6 effective hosts, and under 20 MB of stored fingerprints (raw audio is not stored; renders are bit-reproducible).
5. **Validation must avoid the Gate 2 trap** (section 1): targets that are exact replicas of a library model, bars near the random baseline, and no link to the ear. The proposal includes a leave-one-model-out arm (a target whose model is not in the library, the stand-in for a real pedal) and a blind listen with a registered-in-advance-by-the-human win count.
6. **Nothing here has been run.** All time and accuracy figures for new work are estimates.

## 1. What in the Gate 2 protocol could make a passing synthetic score fail to predict real-world quality

Asked by the lead after the human judged the current model unusable. **Basis and limits:** I read `board/GATES.md`, the Gate 2 entries in `docs/DECISIONS.md` (frozen rule, rule B and its extension, the attempt 3 result, the retrospective, the 2026-10-18 post-mortem decision), and `synthetic-benchmark-design.md`. I did not read the harness code, the sealed sets or the labels. These are hypotheses for the lead's post-mortem to confirm or reject, not findings.

| # | Feature of the protocol | Evidence | Why a pass may not predict real quality | Proposed check (needs approval) |
|---|---|---|---|---|
| G1 | **Targets are exact replicas.** The target is a Native render of a library model with the same DI, aligned within 0.5 sample, +-3 dB gain, pink noise 60 dB down (DECISIONS Ruling 2, line 671). The true preset is in the search space. | Rank-1 misses are decided at nulls of -117 to -142 dB (DECISIONS line 1478). | Rank-1 shows the score can spot an exact copy. It says nothing about how it orders the wrong candidates, and a real pedal or song tone is never an exact copy. The human's complaint ("nothing alike") is about the ordering of non-exact candidates. | Add a **leave-one-model-out arm** (remove the target's model from the library, ask whether the nearest remaining candidates sound close) and non-replica targets: Stomp renders (distortion parity 2 of 5 nulls, GATES line 46), other-vendor simulations, the real-pedal set of Gate 4. |
| G2 | **Bars sit near the random baseline.** Model-accuracy bar 0.60; random walk scored 0.562 and middle-with-category 0.521 on v3 (GATES line 19, DECISIONS line 1674). | 0.771 versus 0.562 is +0.208 with CI [+0.094, +0.323]. | A random walk gets the right model 56% of the time because the pool is 47 models across all families (DECISIONS line 903) and many categories have a handful of models. The pass shows "better than a weak baseline", not "identifies the model". For 61 drives alone, chance is 1.6%. | Bars as a margin over the **best** baseline, on a pool the size of the real choice (61 drives), with the baseline measured first. |
| G3 | **Pooled gate hides the family that matters.** The gate pools families. Drive was 7 right of 12 on v3 (DECISIONS line 1676), 0.58, under the model bar, with a 95% interval of about +-0.28 at n = 12. Cab, EQ and reverb were 11 to 12 of 12. | Same lines. | The pass claim is nearly silent on drive, which is now the only priority. | Per-family bars and n >= 100 drive items; report intervals. |
| G4 | **Known DI, identical notes, aligned waveform.** The admitted score is tuned on aligned audio. | The score was tuned for "aligned DI, which is what Gate 2's synthetic recovery uses" (DECISIONS line 631); [T041](../../board/TASKS.md#t041 "End-to-end on a real song") measured one preset at 7.7, 11.0 and 22.7 over three DIs ([no-di-survey-metrics.md](no-di-survey-metrics.md)); GATES line 21 lists "known-DI case only". | A song has no known DI and the stem is separated (synthetic separation stress: SDR 21.2 to 7.3 dB over densities; real songs not measured, GATES line 20). A waveform-sensitive score that ranks well on identical notes may rank on note content in a song. | Re-test the same score with **different notes and a different guitar** (the C1 to C3 conditions in the metrics note) before any claim about songs. |
| G5 | **Closed loop on one engine and one DI library.** Targets, candidates, trust sets and weights admission all come from Native renders of Guitar-TECHS DIs; 103 of 104 files were rendered somewhere ([predictor-design.md](predictor-design.md) section 3). | Same note, totals line. | Held-out is by file, not by guitar or player, so a score that learned the DI library's habits passes. Nothing here tests the Stomp, the hardware the user exports to (Gate 5 "engine comparison" is unticked). | Held-out **players** (the P3 reserve proposal) and a Stomp-rendered subset for drive. |
| G6 | **Test written after seeing the failures.** Rule B was extended after v2's misses, and v1, v2 and v3 were spent in turn. | DECISIONS lines 1476 to 1480 and the retrospective item 2. | Each change was registered first, but three attempts against one family of tests is mild tuning to the gate. The lead already recommended a fresh v4. | A fresh set built to a design the human approves, run once. |
| G7 | **Nothing ties the loss to human similarity.** The loss is validated by "true preset ranks first", never against listener judgements. | No-DI metrics note, section 3: no study (and no local test) correlates a distance with listener ratings on guitar. | A score can rank the true preset first (the null is deep) and still order the wrong ones by something the ear does not weigh, such as long-term spectrum. | A small human triplet set (which of A or B is closer to X) used to rank candidate scores **before** any admission; the human judges, agents never score. |
| G8 | **"Right model" and "parameter MAE" are not perceptual.** MAE is taken on right-model items only (46 of 96), level knobs excluded (DECISIONS lines 672, 1668). Several drives are near-duplicate circuits. | v3 report; BE-AFX finding that parameter distance and audio distance disagree ([no-di-survey-estimation.md](no-di-survey-estimation.md)). | Drive and input level are confounded (a hot pickup looks like more Drive), so a knob error can be large and inaudible, or small and audible. A wrong id between two circuits that sound alike counts as a miss, while a right id at a bad knob counts as a hit. | Report accuracy at the level of **circuit classes** derived from fingerprints (section 6) beside the strict id, and a fingerprint-space distance instead of knob error. |

Not covered here: the cab, amp and chain parts of Gate 2 (paused by the human).

## 2. What already exists, and the gaps this experiment fills

| Existing (probe-v1 / coarse-v1) | Source | Gap for distortion identification |
|---|---|---|
| ESS 20 Hz to 20 kHz, **1.5 s**, at -40/-24/-12/-6 dBFS; only the **linear** response is extracted | [block-probe.md](block-probe.md) | At 1.5 s the harmonic impulse responses of orders 9 and 10 are only 23 ms apart (formula in 3.2), so they cannot be separated. No Novak-style harmonic-order responses, so no pre/post split. |
| Stepped sines 110/440/1760 Hz, 8 levels, 6 dB steps, 0.25 s, ascending only | same | No descending run (no hysteresis check), 0.25 s may be too short for bias drift, 6 dB steps are coarse for a knee. |
| Fit of DC + H1..H9; odd/even; static curve (two-slope); transfer curve at -3 dBFS, 110 Hz, 41 bins, `r2` for memorylessness | same | One level for the curve. No level-dependent symmetry, no knee width or hardness exponent. |
| Two-tone 440+554 and 110+165 at two levels | same | No pair that probes the post-EQ at low and high frequency independently of the harmonics. |
| `level_step`, `bursts` (compressor-oriented) | same | No test for asymmetric bias settling after a loud burst. |
| `coarse-v1` grid: default + two **primary** params at CC 24/103 corners and CC 64 centre, about 6 settings per model | [native-spike.md](native-spike.md) line 276 | Only two knobs; no Tone/EQ knob where it is not primary; no grid fine enough for the knob-to-feature map. |

## 3. System identification with the dry signal known (accuracy mode)

### 3.1 The model and the literature

- The target structure, **pre-filter, memoryless nonlinearity, post-filter**, is the standard simplified model of a pedal: Yeh, Abel and Smith describe the Distortion and Overdrive pedals as "a conditioning filter, memoryless nonlinearity and equalization filter" ([DAFx-07](https://dafx.de/paper-archive/details/mIq7vlQKu6pUlF467vvhgw), [S]). Eichas, Moeller and Zoelzer fit a "parametric block-oriented Wiener-Hammerstein model" with Levenberg-Marquardt ([DAFx-15](https://dafx.de/paper-archive/details/bKxNxdS0ua9iEdRrM6zPlg), [V abstract]); Eichas and Zoelzer extend it to a linear block plus an extended parametric nonlinear block for three pedals ([DAFx-16](https://dafx.de/paper-archive/details/aW-6gR1PsD9EqSfEI9CYcg), [V abstract]) and to amplifiers, where "first the filters are measured, then the parameters for the nonlinear part are optimized" ([DAFx-17](https://dafx.de/paper-archive/details/Vbz-fSagQBocRJGPBog3qA), [JAES 2018](https://aes2.org/publications/elibrary-page/?id=19874), [S]).
- **The two-step recipe (filters first, then the curve) is the safe baseline;** the fully joint fit is non-convex. Colonel, Comunita and Reiss do the joint fit in a differentiable framework with graphic-EQ pre and de-emphasis filters and several parameterised waveshapers ([AES 2022](https://www.eecs.qmul.ac.uk/~josh/documents/2022/21955.pdf), [S, abstract via search; PDF not readable]). Moliner et al. found a cubic Catmull-Rom spline (41 to 43 control points) better than an MLP or sum-of-tanh for the static curve ([UnNAFx](https://arxiv.org/html/2504.04751), [Estimation and Restoration of Unknown Nonlinear Distortion](https://arxiv.org/html/2501.05959), [S, fetch summaries]).
- Whether the pedal is **truly** a W-H system is itself a finding: the clipper in many pedals sits in an op-amp feedback loop or has a coupling capacitor, so the "pre-filter" depends on Drive and the "clipper" has memory ([M], circuits are covered by the sibling note `distortion-circuits.md`). Comunita, Steinmetz and Reiss compare black-box and gray-box (W-H-inspired) models over 40 devices: gray-box loses a little accuracy, gains interpretability ([Frontiers 2025](https://www.frontiersin.org/journals/signal-processing/articles/10.3389/frsip.2025.1580395/full), [V summary]). Miklanek et al. combine an RNN with the DK-method to model an amp's EQ circuit with little data ([DAFx-23](https://dafx.de/paper-archive/details/UYVDlRJ95vRRQbsqJVUvqw), [S]). Damskaegg, Juvela and Valimaki model a Tube Screamer, DS-1 and Big Muff with a WaveNet-style net from 3 minutes of audio ([SMC-19](https://www.smc2019.uma.es/articles/S5/S5_02_SMC2019_paper.pdf), [S]). A DDSP amp cascades four W-H blocks, a tone stack and a power amp, and uses a size-1 GRU for the non-memoryless part ([arXiv 2408.11405](https://arxiv.org/html/2408.11405v1), [S]).

### 3.2 Sweeps and harmonic-order separation (Farina, Novak)

- **Farina** (AES 108, 2000, [E-library record](https://air.unipr.it/handle/11381/1453666), [S]): with an exponential sine sweep (ESS), harmonic distortion impulse responses are **separated in time** from the linear one, so they can be cut out of one deconvolved record.
- **Novak, Simon, Kadlec, Lotton** ([IEEE TIM 2010](https://hal.archives-ouvertes.fr/hal-02504286), [S]): non-parametric **generalized polynomial Hammerstein** model (power series each followed by a linear filter) identified from one swept sine. The **synchronized swept sine** variant and a classification criterion by dependence on input level are in the [EURASIP 2010 paper](https://link.springer.com/article/10.1155/2010/793816) ([S]; two pedals compared, one strongly and one weakly level-dependent). The **Chebyshev** version, "several parallel branches, each a Chebyshev polynomial followed by a linear filter", was tested on an overdrive pedal ([DAFx-10](https://dafx.de/paper-archive/details/13VDwDuJ0g0wHSqGzYQdWw), [V abstract]).
- **Schmitz and Embrechts** extract Hammerstein kernels from a sine sweep and handle input-amplitude dependence for a Tubescreamer-like overdrive and two tube amps ([JAES 2017](https://aes2.org/publications/elibrary-page/?id=19200), [S]). **Rebillat et al.** give a parallel-Hammerstein variant using the Welch method ([record](https://sam.ensam.eu/handle/10985/12377), [S, title only]). **Orcioni et al.** identify Volterra models of tube devices with Gaussian noise at several variances ([JAES 2018](https://labs.sonicfield.org/library/identification-of-volterra-models-of-tube-audio-devices-using-multiple-variance--Orcioni-2018), [S, title only]): the "multi-level" idea in a noise version.
- **Timing rule (Farina/Novak, [M], to be checked in the synthetic test).** For a sweep of length T from f1 to f2, let L = T / ln(f2/f1). The k-th harmonic response appears **L ln k** seconds before the linear one. For 20 Hz to 20 kHz (ln = 6.9): T = 1.5 s gives 23 ms between orders 9 and 10 (too close to separate); T = 6 s gives 91 ms (workable for pedals whose filters ring for tens of ms). Proposed T = 6 s.
- **Chebyshev point [M, standard identity]:** T_k(cos t) = cos(kt), so for a steady sine of amplitude A at the clipper input, the k-th harmonic amplitude is the k-th Chebyshev coefficient c_k(A) of the static curve, with no cross-talk from higher orders. A power-series reading mixes orders (x^5 also feeds the 3rd and 1st harmonic), which is why Chebyshev is preferred.

### 3.3 What each stage measure needs, and what it cannot give

| Quantity | Method (dry known) | Reads as | Limits |
|---|---|---|---|
| **Static curve f(x)** | (a) Sum of Chebyshev terms: f(x) = sum c_k(A) T_k(x/A) on [-A, A] from one high-level sine (Novak-Chebyshev). (b) Parametric plot of y(t) against the pre-filtered input where the filters are flat (what `transfer_curve` does at 110 Hz, [block-probe.md](block-probe.md)). (c) Fundamental amplitude out versus in at many levels (the describing function). | Small-signal slope g0; ceilings; knee; hardness | Scale convention: input gain and clipper input scale trade off. **Proposed convention:** pre-filter 0 dB at 1 kHz; report the knee as an input-referred level in dBFS at 1 kHz. `r2` of (b) flags memory. |
| **Symmetry** | Even-order Chebyshev share, sum(c_even^2) / sum(c_all^2), at a level where the curve is compressive; ratio of positive to negative ceiling; DC of the output (the probe's `dc_re_fund`) | 0 for a symmetric clipper; large for one-diode or JFET-like curves | **DC is lost if the block is output-coupled** (a capacitor), so use the even-harmonic share and the peak asymmetry of the AC waveform instead. |
| **Knee, threshold, hardness** | Fit f(x) = g0 x / (1 + |g0 x / v|^p)^(1/p) (a Rapp-type curve [M]): v = ceiling, p = hardness (p = 1 soft, p large = hard). Also the input range between the 1 dB and 10 dB compression points. Cross-check: how fast H3 and H5 rise with level (about 2 and 4 dB per dB for a weak nonlinearity, [M]) and where they saturate. | Soft/hard; threshold | Needs levels well below the knee as well as above; the probe's 6 dB steps leave the knee between two points. Proposed 5 dB steps over 55 dB. |
| **Total small-signal response** | ESS at the lowest level that is clean (-60 or -54 dBFS, to be checked against the Native noise floor) | H_pre(f) g0 H_post(f) together | **Cannot split pre from post** by itself. |
| **Pre/post split (large-signal)** | [P] derived from the W-H structure: in steady state the clipper input is a pure sine of amplitude A |H_pre(f)|, so the k-th output harmonic at frequency kf is c_k(A |H_pre(f)|) H_post(kf). Curves of c_k against level at different f are therefore the same curve **shifted along the level axis by |H_pre(f)| and scaled by H_post(kf)**. Align the curves across f (a 2-D shift in log-log) to get |H_pre(f)| (horizontal shift) and |H_post| (vertical shift). Residual of the alignment = how far the device is from W-H. | Pre-EQ and post-EQ separately, up to the scale convention | A derivation, not a published recipe; validate on synthetic W-H systems with known filters before use. Needs >= 8 levels. Fails if the clipper has memory or the pre-filter depends on level. |
| **Two-tone (IMD)** | f1/f2 pairs (fifth 220+330; 80 Hz + 2 kHz; 1000 + 1050): products at 2f1-f2 scale with H_pre(f1)^2 H_pre(f2) H_post(2f1-f2); the difference tone f2-f1 reaches H_post at low frequency without a low-frequency sweep | Independent check of the split; intermodulation grit | Needs the product grid known; aliasing folds products. |
| **Dynamic and memory effects** | Ascending versus descending level run at 220 Hz (hysteresis: a level-dependent difference in H1/H3 > 0.5 dB); a -30 / -6 / -30 dBFS burst and the **local mean and positive/negative peak asymmetry versus time** (bias settling with the coupling-capacitor time constant, [M] typically tens to hundreds of ms); `level_dependent_gain` already in the probe | Bias drift, "sag", capacitor-coupled clippers | Native's drive models may have little of it; report as a flag. |
| **Aliasing** | Inharmonic share of 2 to 10 kHz (already in `metrics/nonlinear.py`), and the odd harmonics' fold-back in the ESS | Oversampling quality | Becomes a nuisance for the Novak extraction at high orders; restrict to f <= 0.45 fs / k. |

**Joint gray-box fit as a cross-check (option).** A differentiable W-H twin (41-point spline, two or three biquads either side) fitted with a multi-resolution STFT loss, as Colonel et al. and the Frontiers study do, starting from the analytic values above. It gives a compact parametric twin per model and setting, about 25 numbers. Cost per fit is unmeasured; I guess tens of seconds on CPU [unverified]. Recommended only for a **subset** (section 7, step 4), because the analytic extraction is cheaper and the twin's own error (versus the Native render) is unknown until measured.

## 4. The same identification without the dry signal (blind; Gate 3)

### 4.1 What the literature offers

- **Closest and most recent:** UnNAFx (Moliner, Svento, Wright, Juvela, Rajmic, Valimaki; DAFx 2025; [arXiv 2504.04751](https://arxiv.org/abs/2504.04751) [V abstract], details from the [HTML](https://arxiv.org/html/2504.04751) [S]). Unpaired: a diffusion prior pretrained on clean guitar (EGDB; later fine-tuned on 16 min of IDMT) plus the wet recording; a gray-box **Wiener-Hammerstein** operator (41-point Catmull-Rom spline) or black-box GCN/S4 operators are fitted. Wet audio of 18 s to 16 min; 3 min to 1 h of optimisation. Distortion only, with clean/light/heavy levels; adversarial training is better on heavy distortion, diffusion is more stable with little data, and the W-H model was unstable under adversarial training. Limits stated: needs a clean-guitar prior from the same domain, memoryless effects only.
- **Estimation and Restoration of Unknown Nonlinear Distortion Using Diffusion** ([arXiv 2501.05959](https://arxiv.org/html/2501.05959), [S]): memoryless operator as sum-of-tanh (8 coefficients), MLP or spline (43 points), jointly with the restored signal; performance degrades at about 1 dB SDR; memory effects are "limited in scope". Hard and soft clipping, wavefolding and quantisation all recover.
- **Classical blind Hammerstein/Wiener identification** exists but assumes a statistical model for the unobserved input: Bai ([blind Hammerstein-Wiener](https://iro.uiowa.edu/esploro/outputs/journalArticle/A-blind-approach-to-the-HammersteinWiener/9984083818202771), [S, snippet]); Vanbeylen et al. ([IFAC 2008](https://skoge.folk.ntnu.no/prost/proceedings/ifac2008/data/papers/1005.pdf), [S, PDF unreadable]) assume white Gaussian input and an invertible nonlinearity; an audio paper (WASPAA 2003, [PDF](https://www.di.ens.fr/~cappe/Publications/Self-archive/pic_waspaa03.pdf), unreadable here) treats an instantaneous nonlinearity followed by an all-pole filter with only the output observed. **Guitar is not white Gaussian**, so these are method background, not off-the-shelf.
- **Clipping detection** from the amplitude histogram appears in the declipping literature, which assumes a hard clip at a known or detectable threshold ([Zaviska et al. 2021](https://arxiv.org/abs/2007.07663), [S]).
- Blind effect estimators for guitar ([Hinrichs 2024](https://asmp-eurasipjournals.springeropen.com/articles/10.1186/s13636-024-00330-0), [BE-AFX](https://arxiv.org/html/2310.11781v1), [Comunita 2021](https://arxiv.org/html/2012.03216v1)) are covered in the earlier notes; none identifies a pedal model among dozens.

### 4.2 What each blind cue can and cannot tell (my analysis, [P] unless cited)

| Cue from the output alone | Recovers | Confounded by | Verdict |
|---|---|---|---|
| **Amplitude histogram, crest factor** | Hard clipping shows as spikes at +-ceiling; the share of samples near the peak grows with Drive | Any post-EQ or cab smears the plateaus (the repo's own comment: "crest measured after the cab barely tracks drive (filters restore peaks)", `metrics/crest.py`); the amp stage after the pedal clips again | Usable only when the pedal is the last nonlinear stage and the post-EQ is mild: the single-block Gate 3 case. In a song, weak. |
| **Even harmonics / waveform skew on sustained notes** | Asymmetric clipping (even harmonics, nonzero skew of the high-passed wave) | The guitar's own even harmonics (pluck position, pickup comb); unknown signal polarity (use |skew|); DC removed by coupling and by separation | Weak but real on low notes; compare **orders >= 4**, where the string's own content has fallen away. |
| **Harmonic decay slope** on low notes (fundamental below about 150 Hz, orders 2 to 20 stay under about 3 kHz) | Hardness: a square wave decays at 1/k (about -6 dB per octave of order); soft curves decay faster | Post-EQ slope; polyphony; vibrato. Staying below 3 kHz avoids most cab roll-off | Usable family cue (hard / soft / fuzz-like). The repo has the machinery (`f0_track`, `harmonic_levels`, `metrics/nonlinear.py`). |
| **Post-EQ from the long-term spectrum** | Broad shape (mid hump, treble roll-off) | Pickup position and type, the guitar's tone knob, the player (the no-DI metrics note: pickup sensitivity is "High"); the later cab | Low confidence; band-limited statements only (is there a mid hump near 700 Hz?). |
| **Gain from sustain and compression** | Distortion raises the level of the quiet tail of a note, so the note's decay is flatter than a clean pluck's. Idea [P]: extra sustain about (gain in dB minus headroom) / (clean decay rate in dB/s) | Unknown clean decay rate (string, fret, damping); the player's dynamics; noise gates | Monotone but noisy; a feature, not a measurement. |
| **Intermodulation in chords** | A symmetric clipper adds difference tones (a root-fifth dyad such as 110+165 Hz has products on the 55 Hz grid); a bass cut before the clip (pre-EQ) reduces them | Needs the chord notes (multi-pitch tracking); the amp's own clipping adds more | Potentially the only blind cue for the **pre-EQ**; hardest to extract. Treat as a research item. |
| **Level (Drive versus input level)** | nothing | Hot pickup looks like more Drive (predictor-design.md section 1) | Cannot be recovered. |

**Not recoverable blind:** the absolute input level into the pedal (so Drive is identified only up to the player's level), the precise pre/post split, the Level knob, and the pedal's contribution once an amp and cab follow it. **The Gate 3 single-block case is therefore a best case**; a pedal in a song with an amp behind it is a harder problem than anything in this note (the cascade of two clippers is not separable from the output without the dry).

### 4.3 A blind feature set and a cheap baseline

[P] Compute about 50 hand-built features on the output: amplitude quantiles and kurtosis (broadband and in three bands), skew magnitude after a 40 Hz high-pass, crest by band, harmonic slope and even/odd ratio on low-pitched frames, inharmonic share, note-decay slope and level-range statistics, plus the repo's existing terms (`crest_flatness_hnr`, `ltas_tilt`, `envelope`). Train a gradient-boosted classifier on renders of **many DI clips** per setting, so the classifier learns the DI nuisance. Compare "existing generic features" against "plus circuit features". If the circuit features add nothing on held-out DIs, the circuit-grounded blind route is not worth building. It costs no new renders if the existing consumed render sets cover enough drive settings (unverified; `helix-trust-v2` lists 60 drive models, 3,840 renders over 442 models).

## 5. The experiment on Helix Native

### 5.1 Proposed probe, `dfp-v1` (distortion fingerprint probe)

One mono 48 kHz file per setting, in the style of `probe-v1`, rendered through the single-block drive template (no tail block, so the default short pre-roll applies; native-spike.md, [T082](../../board/TASKS.md#t082 "N6 engine push: (a) exact 0.0 command lands at lo,")). Levels are dBFS peak.

| Segment | Content | Seconds |
|---|---|---|
| lead-in | silence | 0.5 |
| ESS x 8 levels | 20 Hz to 20 kHz, **6 s**, 1 s gap each; levels -60, -42, -30, -24, -18, -12, -6, 0 | 56 |
| steady sines | 110, 220, 440 Hz at 11 levels, -54 to -4 in 5 dB steps, 0.4 s each, ascending; plus one 220 Hz **descending** run | 21 |
| two-tone | 4 pairs (440+554, 110+165, 220+330, 80 Hz + 2 kHz) x 4 levels x 0.5 s | 8 |
| bias burst | 220 Hz and 1 kHz: -30 / -6 / -30 dBFS, 0.5 s each | 4 |
| real DI clips | 4 x 4 s (strum, single-note lead, palm-mute power chords, bend), peak -10 dBFS | 16 |
| **Total** | | **105.5** |

A reduced `dfp-lite` (ESS at -42/-24/-12/0 only; the 220 Hz run; bias burst; DI) is **54 s**.

### 5.2 Settings

Level is held at 0 dB (verify once per model that Level is post-clipper by checking the static curve does not change shape when Level moves; one extra setting).

| Stage | Settings per model | Models | Settings | Probe |
|---|---|---|---|---|
| **Screen** | Drive x Tone at {0, 0.5, 1} each = 9, plus each other knob at 0 and 1 with the rest at 0.5 (about 3 on average) = 12 | 61 | 732 | full, 105.5 s |
| **Knob map** | Drive x Tone at {0, .25, .5, .75, 1} = 25, plus 5 one-at-a-time points on the other knobs = 30 | 61 | 1,830 | lite, 54 s |

"Tone" means the model's own EQ knob (Tone, Treble, Filter, Bass/Mid/Treble); the existing `PRIORITY` list in the fingerprint script picks primary knobs. Use the same CC grid values `cc_value` gives the template, so every setting is exactly what Native plays.

### 5.3 Cost (estimates; rates from the lead's measurement)

Rate used: 0.68 s per 10 s render on one host, i.e. 0.068 host-s per second of audio. **Cross-check [P]:** the [T047](../../board/TASKS.md#t047 "started 2026-10-16 (native-engine, engine push N2)") run took 3,535 s on 3 hosts for about 3,600 renders of the 47.25 s probe, i.e. about 2.9 s per render per host (0.062 s/s), consistent. Efficiency of 8 hosts is unmeasured (2 hosts gave 1.46x on short clips); the table shows 3 and 6 effective hosts.

| Step | Renders | Host-seconds each | Host-hours | Wall at 3 hosts | Wall at 6 effective |
|---|---|---|---|---|---|
| Screen, full probe | 732 | 7.2 | 1.46 | 29 min | 15 min |
| Knob map, lite probe | 1,830 | 3.7 | 1.87 | 37 min | 19 min |
| Validation (section 7): 61 models x 10 random settings x 4 held-out DI clips of 10 s | 2,440 | 0.68 | 0.46 | 9 min | 5 min |
| Optional blind-training set: 61 models x 40 random settings x 20 DI clips of 10 s | 48,800 | 0.68 | 9.2 | 3.1 h | 1.5 h |
| Optional Stomp parity subset: 10 models x 9 settings | 90 | about 4 (render 2.7 s + model change) | 0.1 | n/a (serial hardware) | n/a |

Analysis is a separate cost: the existing analysis ran 0.7 to 2.6 s per setting under load; the new Novak extraction on 8 sweeps is FFT-bound and should be similar [unverified]. Run `python scripts/proc_check.py` before and after; keep the load under the core count with the scaler.

### 5.4 Storage (9 GB free)

- **Raw audio is not kept.** One full-probe render is 105.5 s x 48,000 x 4 B = 20 MB; 2,562 settings would be 51 GB. Native renders are bit-reproducible ([T103 verification](native-spike.md)), so a setting can be re-rendered in 7 s instead of stored. Keep two models' raw renders (about 40 MB) as regression fixtures.
- **Fingerprint per setting, about 3 KB** (float16 arrays): small-signal |H| 55 points; |H| at 8 levels x 55; Chebyshev c_k(A), k = 1..9, 8 levels x 3 frequencies; harmonic-order responses k = 2..5 at 3 levels x 55; static curve 65 points; about 40 scalars (g0, ceilings, knee, hardness p, symmetry, even share, DC, IMD3/IMD2, hysteresis, bias time constant, inharmonic share); the four DI-clip statistics. About 2,562 x 3 KB = **8 MB**; stored as one `.npz` per model plus an index, under `cache/` (gitignored, like the existing fingerprints; derived from the user's install, never committed).
- **Validation renders:** 2,440 x 10 s x 48 kHz x int16 = 2.3 GB if kept; propose to keep features only and 300 clips for listening (0.3 GB). The blind-training set (48,800 renders, 94 GB as float32) is features only, or log-mel at 64 x 300 int8 (19 KB each, 0.9 GB).

### 5.5 What the fingerprint contains, per setting, and how knobs move it

Static curve and its parameters (g0 in dB, ceilings +/-, knee, hardness p, symmetry); Chebyshev profile versus level (the "harmonic profile"); pre-EQ and post-EQ magnitudes (relative, scale convention fixed in 3.3); small-signal total response; IMD ratios; memory flags. The knob map stores each scalar and each curve's principal components as a function of (Drive, Tone) on the 5 x 5 grid, which shows the **taper** of Drive (dB of g0 versus knob, which is not obvious from the name) and whether Tone moves the pre-EQ, the post-EQ or both, as in a tone control in the clipper's feedback path ([M], see `distortion-circuits.md`).

## 6. How the fingerprints would be used (proposals)

**(i) Dry track known: model family and knob region, analytic and fast.** From the (dry, wet) pair of the target, estimate the same quantities directly: coherence-based small-signal response on low-energy frames; a spline W-H fit seeded from the analytic estimates; or, simpler, regress wet on the filtered dry to read the static curve. Rank library models by distance in fingerprint space; for the top 3, invert the knob map (nearest grid point, then local interpolation), and spend 10 to 30 verification renders (about 0.84 s each; predictor-design.md section 2). Estimated cost: seconds of analysis plus about 25 renders, versus about 1,700 renders for the current song search (ledger 2026-10-18T19:00).

**(ii) Blind: from the output alone.** Features from 4.3 (the cues that survive), compared with the distribution of the same features for each (model, knob region) rendered over many DIs; return a ranked family list with an honest spread, **not** a single model. Use the circuit class (below) as the primary output and the model as a second line.

**(iii) Features and targets for the predictor ([T165](../../board/TASKS.md#t165 "Predictor-led matching design"), re-scoped to drive).** (a) Add the 50 blind features to the network's input. (b) Add **auxiliary regression heads for fingerprint scalars** (g0, hardness p, symmetry, knee, post-EQ tilt): they are smooth targets the network can learn even where model ids are confusable. (c) Use fingerprint distance for **soft labels**, so that confusing two models with near-identical fingerprints costs little; this also gives the "circuit class" labels.

**Circuit class (proposal).** Cluster the 61 models by fingerprint (hierarchical clustering on the scalars plus curve components), and compare the clusters with the "based on" mapping from [HelixHelp](https://helixhelp.netlify.com/models/) (sibling note `distortion-circuits.md`). Agreement (for example adjusted Rand index) is a sanity check on both. Classes expected [M]: clean boost, symmetric soft op-amp overdrive, hard diode-to-ground distortion, asymmetric or transistor/JFET overdrive, fuzz. The human approves the final class list.

## 7. Validation plan (options and recommendation; the human approves every part)

### 7.1 Test design options

| Option | Targets | Strength | Weakness |
|---|---|---|---|
| A (Gate-2 style) | Native renders of library models on held-out DI clips, random settings | Cheap, exact truth | Replica problem (G1); can pass while the ordering of wrong candidates is poor |
| B (recommended minimum) | A, **plus a leave-one-model-out arm**: for each model, remove it from the library and test whether the nearest remaining models are in its fingerprint class and sound close | Stand-in for a real pedal that has no exact Helix twin; same cost as A | Still all Native |
| C (recommended) | B, plus **non-replica targets**: Stomp renders of the same drive models (distortion parity 2 of 5 nulls, so these are close but not identical), and, when available, recordings of real pedals (the Gate 4 set) | The closest thing to the real task | Stomp is serial hardware time; real pedals need the human |

**Held-out sets.** Random continuous settings (off the 3 x 3 and 5 x 5 grids) so the grid points are not memorised; DI clips from players or guitars not used in fitting any fingerprint feature or weight (the P3 reserve proposed in predictor-design.md section 3.2; the human rules); and a separate leave-one-model-out split. Report cluster-bootstrap intervals over models and clips, not item-level ones (items from one model are correlated).

### 7.2 Metrics

1. Strict model top-1 and top-3, per family.
2. **Circuit-class accuracy** (section 6).
3. **Fingerprint-space distance** between the recovered and the true setting (instead of knob MAE, G8).
4. Re-render the recovered model and knobs on a held-out DI and report the existing known-DI distance to the target **as a diagnostic only**; it is the same family of score whose adequacy is in doubt.
5. **Blind listen**, the final judge (below).

### 7.3 Baselines (all measured first, on the same items and DIs)

- B0 chance: 1/61 and class-frequency majority.
- B1 **the current generic search** (the drive-only search that produced the "nothing alike" results, [T164](../../board/TASKS.md#t164 "Play-along single-block match")), run on the same targets. The number to beat.
- B2 a dumb physical baseline: match only small-signal gain and long-term spectrum (shows how much the nonlinear cues add).
- B3 the learned predictor from [T165](../../board/TASKS.md#t165 "Predictor-led matching design") when it exists.

### 7.4 Bars (placeholders for the human to accept, change or reject)

I cannot justify numeric bars from the literature: the largest class count in the evidence is about 160 devices with classification only ([no-di-survey-estimation.md](no-di-survey-estimation.md)). Method I recommend: run the baselines on a **development split first**, show the human those numbers, and let the human set bars as a margin over the best baseline, with intervals, before the held-out set is touched. Placeholders to start the conversation:

| Mode | Metric | Placeholder bar |
|---|---|---|
| Dry known | model top-3 | >= 0.90; and >= B1 + 0.15 with the interval excluding zero |
| Dry known | circuit-class accuracy | >= 0.90 |
| Dry known, leave-one-model-out | nearest library neighbour in the true class | >= 0.80 |
| Blind | circuit-class accuracy | >= 0.70 (no literature at this scale) |
| Blind | model top-5 | >= B1 + 0.20 |
| All | do not sit within the noise of the best baseline | by construction |

### 7.5 The ear

Prepare, do not score. Proposed session: **40 trials**, about 15 to 20 minutes. Each trial: the target X, then A = this method's pick and B = B1's pick (random order, loudness-matched by the existing level term), 4 to 6 s of a held-out DI. Include 8 catch trials (A the exact replica, B a random drive) to screen the listening. Placeholder pass: A chosen in at least 27 of 40 non-catch trials (one-sided binomial p about 0.02; 26 of 40 gives about 0.04; normal approximation). Optional second arm for G7: 60 human triplets (which of two candidates is closer to X) to rank the candidate **scores** by agreement with the ear before any score is admitted.

### 7.6 Traps to avoid

- A held-out clip appearing in any fingerprint probe (the four DI clips in `dfp-v1` must not be validation clips).
- Native's two numeric modes and the amp latch (native-spike.md, [T103](../../board/TASKS.md#t103 "Native verification suite scripts/native_verify.py")): use the verified engine and the canary.
- Reporting the strict id only; reporting only the pooled family result.
- Choosing bars after seeing the held-out result (the lead's own retrospective item).

## 8. Recommended step-by-step plan (each step needs the human's go-ahead)

| # | Step | Renders | Cost | Pass / fail check (placeholder, for approval) |
|---|---|---|---|---|
| 0 | **Mine the existing `coarse-v1` drive fingerprints**: pairwise distances between the 61 models with the stored scalars (`gain_1k_db`, `thd_440_db`, `tilt_4k_250_db`, `ratio`, `knee_dbfs`, symmetry), clustering; compare with the "based on" classes; do the top results of [T164](../../board/TASKS.md#t164 "Play-along single-block match") fall in different clusters? | 0 | minutes | Model id from a different knob setting at top-1 well above 1/61; if the existing summaries cannot separate models at all, the new probe is justified. Gives the first quantitative picture of "nothing alike". |
| 1 | **Validate the estimators on synthetic W-H systems with known truth** (no Native): tanh, hard clip, asymmetric diode, Tube-Screamer-like (clipper with a high-pass in front), a bias-drift case. Check the timing rule and the level-shift alignment. | 0 | small build, seconds to run | Static curve error < 0.5 dB; pre/post filter error < 1 dB where the response is above -40 dB; knee within 1.5 dB; alignment residual large for a system that is not W-H. If the pre/post split fails, drop it and keep the small-signal total. |
| 2 | **Pilot on 6 models x 9 settings** (full probe), chosen to span classes (a Tube Screamer type, a RAT type, a Big Muff type, a germanium fuzz, a clean boost, a treble booster; names [M], check in HelixHelp). | 54 | about 7 host-min (2 to 3 min wall) | Repeat renders agree within the Native spread; boost shows no clipping to -6 dBFS and fuzz shows large even share; g0 rises with Drive; the extractor matches the class expectations. |
| 3 | **Screen, then knob map, on all 61 drives.** | 732 + 1,830 | 3.3 host-h; 35 min to 1.1 h wall | >= 95% valid fingerprints; between-model spread larger than the repeat spread on the main scalars; knob maps monotone where expected. |
| 4 | **Dry-known identification test** on the held-out validation set (section 7), plus the leave-one-model-out arm; optional twin fit on a 10-model subset to compare with the analytic route. | 2,440 | 0.5 host-h | Bars in 7.4, once, after the human approves them and after the baselines are shown. |
| 5 | **Blind feature baseline** (4.3) with the same renders; ablation of generic versus circuit features. Optionally the 48,800-render training set. | 0 (or 48,800) | minutes (or 1.5 to 3 h) | Circuit features add a lift on held-out DIs, interval excluding zero; otherwise stop the blind route. |
| 6 | **Human listening session** (7.5) and, optionally, the triplet set (G7). | 300 clips kept | 20 min of the human's time | >= 27 of 40, set by the human. |
| 7 | Hand features and auxiliary targets to [T165](../../board/TASKS.md#t165 "Predictor-led matching design") (re-scoped to drive). | none | none | Only if steps 4 to 6 pass. |

Dependencies: the engine must stay on the verified settings; run `proc_check` around every heavy run; steps 2 to 4 need the hosts, not the GPU.

## 9. Approvals requested

1. The `dfp-v1` probe design and the render budget in 5.3 (steps 2 and 3).
2. The validation design: option B or C in 7.1, the held-out DI rule, the baselines in 7.3.
3. The bars (7.4) and the listening protocol (7.5), after the baselines are shown.
4. The circuit-class list (6).
5. Whether to run the optional 48,800-render training set.

## 10. What I could not verify

- Bodies of: Colonel et al. (AES 2022; TLS error on the QMUL PDF), Bai, Vanbeylen (IFAC 2008) and the WASPAA 2003 paper (binary PDFs), Schmitz and Embrechts, Orcioni et al., Rebillat et al. (only titles or abstracts seen). UnNAFx and the posterior-sampling paper come from a fetch-tool summary of the HTML, not the full text. The earlier no-DI note called the UnNAFx summary garbled; this one is better but still a summary.
- The timing rule and the Chebyshev-branch statements are from memory of Farina/Novak; the **level-shift method for the pre/post split is my derivation** and untested.
- Typical decay rates, the "2 dB per dB" harmonic growth, bias time constants and the pedal-circuit statements are from memory and need the circuit notes.
- All costs for new work are estimates; the 8-host efficiency, the analysis time of the new extractor and the twin-fit time are unmeasured; the Native noise floor at -60 dBFS is unchecked.
- Whether existing consumed render sets hold enough drive settings for step 5 without new renders.
- Whether any real drive pedal or non-Helix simulation is available to the human for option C.
- Gate 2 section: I did not read the harness, the sealed sets or the labels; items G1 to G8 are hypotheses.

## Sources (all external links used; the count goes in the ledger)

1 [Farina 2000](https://air.unipr.it/handle/11381/1453666) [S]; 2 [Novak et al. 2010, IEEE TIM](https://hal.archives-ouvertes.fr/hal-02504286) [S]; 3 [Novak et al. 2010, EURASIP](https://link.springer.com/article/10.1155/2010/793816) [S]; 4 [Novak et al. DAFx-10](https://dafx.de/paper-archive/details/13VDwDuJ0g0wHSqGzYQdWw) [V]; 5 [Schmitz and Embrechts 2017](https://aes2.org/publications/elibrary-page/?id=19200) [S]; 6 [Rebillat et al.](https://sam.ensam.eu/handle/10985/12377) [S]; 7 [Orcioni et al. 2018](https://labs.sonicfield.org/library/identification-of-volterra-models-of-tube-audio-devices-using-multiple-variance--Orcioni-2018) [S]; 8 [Eichas et al. DAFx-15](https://dafx.de/paper-archive/details/bKxNxdS0ua9iEdRrM6zPlg) [V]; 9 [Eichas and Zoelzer DAFx-16](https://dafx.de/paper-archive/details/aW-6gR1PsD9EqSfEI9CYcg) [V]; 10 [Eichas et al. DAFx-17](https://dafx.de/paper-archive/details/Vbz-fSagQBocRJGPBog3qA) [S]; 11 [Eichas and Zoelzer JAES 2018](https://aes2.org/publications/elibrary-page/?id=19874) [S]; 12 [Yeh, Abel, Smith DAFx-07](https://dafx.de/paper-archive/details/mIq7vlQKu6pUlF467vvhgw) [S]; 13 [Yeh and Smith DAFx-08](https://dafx.de/paper-archive/details/gZvqz8Bk39w9LqNNr1SDlg) [S]; 14 [Pakarinen and Yeh, CMJ 2009 (publication list)](https://ccrma.stanford.edu/~dtyeh/papers/pubs.html) [S]; 15 [Colonel, Comunita, Reiss 2022](https://www.eecs.qmul.ac.uk/~josh/documents/2022/21955.pdf) [S]; 16 [Comunita, Steinmetz, Reiss 2025](https://www.frontiersin.org/journals/signal-processing/articles/10.3389/frsip.2025.1580395/full) [V summary]; 17 [Miklanek et al. DAFx-23](https://dafx.de/paper-archive/details/UYVDlRJ95vRRQbsqJVUvqw) [S]; 18 [Damskaegg et al. SMC-19](https://www.smc2019.uma.es/articles/S5/S5_02_SMC2019_paper.pdf) [S]; 19 [DDSP Guitar Amp](https://arxiv.org/html/2408.11405v1) [S]; 20 [UnNAFx](https://arxiv.org/abs/2504.04751) [V abstract, S body]; 21 [Estimation and Restoration of Unknown Nonlinear Distortion](https://arxiv.org/html/2501.05959) [S]; 22 [Zaviska et al. 2021](https://arxiv.org/abs/2007.07663) [S]; 23 [Bai, blind Hammerstein-Wiener](https://iro.uiowa.edu/esploro/outputs/journalArticle/A-blind-approach-to-the-HammersteinWiener/9984083818202771) [S]; 24 [Vanbeylen et al. IFAC 2008](https://skoge.folk.ntnu.no/prost/proceedings/ifac2008/data/papers/1005.pdf) [S]; 25 [WASPAA 2003 blind Hammerstein distortion](https://www.di.ens.fr/~cappe/Publications/Self-archive/pic_waspaa03.pdf) [unread]; 26 [Hinrichs 2024](https://asmp-eurasipjournals.springeropen.com/articles/10.1186/s13636-024-00330-0) [S]; 27 [BE-AFX](https://arxiv.org/html/2310.11781v1) [V in earlier note]; 28 [Comunita 2021](https://arxiv.org/html/2012.03216v1) [V in earlier note]; 29 [HelixHelp models](https://helixhelp.netlify.com/models/).

Local sources: `docs/notes/block-probe.md`, `docs/notes/native-spike.md`, `docs/notes/no-di-survey-estimation.md`, `docs/notes/no-di-survey-metrics.md`, `docs/notes/predictor-design.md`, `docs/notes/host-budget.md`, `docs/notes/synthetic-benchmark-design.md`, `src/tonematch/metrics/nonlinear.py`, `src/tonematch/metrics/crest.py`, `board/GATES.md`, `docs/DECISIONS.md` (Gate 2 entries, 2026-10-18 entries).
