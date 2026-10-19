# Scoring models for distortion tones: what we tried, what exists, what to try next ([T169](../../board/TASKS.md#t169 "Scoring-models report for the human"))

Owner: `research`. Written 2026-10-18 for the human. Research only: no code, no renders, no Line 6 files. Not legal advice.

**This report recommends and ranks. It decides nothing.** Under the standing rule of 2026-10-18 (DECISIONS, "model and method decisions require the human's approval"), every score, weights file, test design or bar below is a proposal for you to accept or reject.

**Sibling note.** `docs/notes/distortion-identification.md` (T167c, how each pedal stage can be read from audio) did not exist when I wrote this. When it lands, its measurements are the raw material for section 3B and section 4 here; I have not duplicated it. Companion notes that do exist: [no-di-survey-metrics.md](no-di-survey-metrics.md) ([T158](../../board/TASKS.md#t158 "No-DI method survey (human 2026-10-18") part 2), [no-di-survey-estimation.md](no-di-survey-estimation.md) ([T158](../../board/TASKS.md#t158 "No-DI method survey (human 2026-10-18") part 1), [afx-rep.md](afx-rep.md), [seed-quality.md](seed-quality.md), and the project's own [TONE-COMPARISON.md](../TONE-COMPARISON.md).

**How to read the evidence marks.** **[R]** = measured in this repo (file or ledger row named). **[S]** = from a search-result or fetch summary of a paper; not checked against the full text. **[M]** = from my memory, unverified. **[P]** = my proposal or analysis, not literature. Where a claim has no mark and no link it is a plain definition.

Short glossary (more inline where each term first matters):

- **Score**: a number that says how far apart a target tone and a candidate tone sound. Lower is closer.
- **Target**: the guitar tone we want (the song's separated guitar, or a recording of your pedal).
- **Candidate**: the same dry guitar played through a trial setting in Helix Native.
- **DI**: "direct input", the clean guitar recorded before any amp or pedal.
- **Rank-1**: in a trust check, the share of tests where the score puts the true setting first among about 20 wrong ones. Chance is about 0.05.

---

## 0. One-page summary

Verdicts: **dropped** = tried and failed for our job; **keep** = works for a narrow job; **promising** = worth testing first; **unproven** = no evidence yet. "Needs dry" = needs the clean DI of the same performance. "Note-robust" = still sensible when the notes, guitar or pickup differ.

| Score | Type | Tried? | Result so far | Needs dry? | Note-robust? | Perceptual evidence | Verdict |
|---|---|---|---|---|---|---|---|
| `mrstft` (multi-resolution spectrogram distance) | spectral | Yes | rank-1 0.73 same notes after separation stand-in; 0.18 different notes [R] | Yes, sample-aligned | No | none for pedals | dropped (superseded by v2) |
| `mrstft_v2` (ear-scaled bands) + `level_db` | spectral | Yes | 0.99 aligned (drive 0.92; drive 0.67 after separation stand-in); 0.98 on amp/drive with level tie-break; at chance blind (2 of 12) [R] | Yes, sample-aligned | No | none; the human heard its matches as not close | keep for case (c) only |
| `ltas_tilt` (average spectrum) | spectral | Yes | 0.03 different notes; 0.35 same notes [R] | No | Weak (pickup, notes move it) | none for pedals | dropped as a score; keep as diagnostic |
| `ltas_eqfit` (spectrum after a bounded tilt/shelf fit) | spectral | Built, not run on Helix | passes offline note-invariance check; [T161](../../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)") stopped before any Helix number [R] | No | Unknown | none | unproven |
| `crest_flatness_hnr` (distortion statistics) | distortion | Yes | 0.13 different notes; 0.40 same notes [R] | No | Weak (technique moves it) | none | dropped as a score; seed for a fingerprint |
| `envelope` (dynamics) | distortion/dynamics | Yes | 0.05 different notes [R] | No | Weak | none | dropped |
| `embed_afxrep` (AFx-Rep learned embedding) | learned | Yes | 0.15 different notes; best of the group against random chains (0.78) but no better on near misses [R] | No | Partly | 23-engineer test in its own paper [S] | keep as a coarse "same family" filter |
| `texmod`, `mmd_cep`, `presence_nodi` (no-DI candidates) | texture / distribution | Built, not run on Helix | `texmod`, `mmd_cep` fail offline note-invariance [R] | No | Unknown | none for guitar | unproven (texmod, mmd_cep weak) |
| Harmonic profile vs level, even/odd, static curve, knee | circuit-grounded | Only as parity diagnostics | not yet used as a score [R: `nonlinear.py`] | Easier with it | Medium | indirect (harmonic audibility is classic) | promising |
| PEAQ, ViSQOL, PEMO-Q, HAAQI | perceptual quality | No | not tested here | Yes (reference-based) | No | tracks clipping degradation; made for coding/hearing-aid quality [S] | unproven; probably wrong tool |
| CDPAM / DPAM | learned perceptual | No | not tested | No (both clips only) | Partly | trained on human triplet judgments, 9 datasets [S] | unproven |
| MERT, CLAP, BEATs, PANNs embeddings | learned general | No | not tested | No | Pitch-heavy | weak for effects [S] | probably dropped |
| Fx-Encoder / Fx-Encoder++ | learned effect embedding | No | Fx-Encoder++ R@1 26% on its own task [S] | No | Partly | engineer tests in papers [S] | unproven; Fx-Encoder++ is non-commercial |
| Open-Amp guitar encoder | learned guitar tone | No | classification 72-99% on devices [S] | No | Yes by design | none for similarity | promising (check weights) |
| EG-VAE tone code | learned guitar tone | No | tone MOS 4.30 vs 2.30 (own comparison) [S] | No | Yes by design | 14-listener MOS [S] | unproven (weights unknown) |
| FAD / KAD / MMD / Wasserstein | distribution | MMD built (`mmd_cep`) | fails offline note-invariance with that feature set [R] | No | Depends on features | FAD about 0.5 Spearman at best [S] | unproven |
| Siamese / triplet metric on our Native renders | learned, ours | No | none | No | Yes by design | none until fitted to your ears | promising |
| Metric fitted to your A/B judgements | learned, ours | No | none; no stored ratings exist [R] | No | By construction | the definition of perceptual | promising (and the check on everything else) |
| Analysis-by-synthesis (fit gain/clip/EQ, compare parameters) | parametric | No | none | No | Yes if the fit is good | none for similarity; fits judged by ear in papers [S] | unproven, high ceiling |

---

## 1. The three situations a score must handle

A score is only useful against a specific nuisance: the things that differ between target and candidate that are NOT the pedal.

**(a) Dry known, different performance.** You record your own take of the part (a DI), we render it through each candidate, and we compare with the song's separated guitar. The notes are roughly the same but the timing is loose (the align tool scored your Mother takes 0.31 to 0.34 on onset correlation with a 30 ms lag spread [R: TASKS [T163](../../board/TASKS.md#t163 "Play-along test (human 2026-10-18)")]), the guitar and pickup differ from the record, and the target is a separated stem with artefacts. This is what a user can actually do. The nuisance is moderate and, importantly, the candidate can be re-rendered through the same DI the target roughly came from.

**(b) Blind.** Output audio only, no dry. The nuisance is the largest: notes, guitar, pickup, playing technique, and how hard the gain stage was driven are all unknown. This is the only honest model of "match a pedal I hear on a record". The human's 2026-10-18 rulings make this the target of Gate 3, and say the end-user match must be cheap and predictor-led.

**(c) Same dry, identical notes (Gate 2).** The synthetic targets and the candidates were rendered from the identical DI by the same engine. Any difference between them can only be the preset.

**Why passing (c) told us little about (a) and (b).** [R unless marked]

1. In (c) the biggest nuisance is zero by construction. The measured size of that nuisance on Helix renders: changing only the notes moves every old score 4.5 to 10 times more than moving one or two knobs does ([DECISIONS 2026-10-16, T067](../../docs/DECISIONS.md#2026-10-16-tone-metrics-t067-helix-trust-set-registered-before-the-run "2026-10-16 (tone-metrics, T067 Helix trust set registered before the run)")). `mrstft_v2` was never tested on that axis; it was tuned and passed where the nuisance is absent.
2. [T041](../../board/TASKS.md#t041 "End-to-end on a real song") showed the same preset scoring 7.7, 11.0 and 22.7 on three different DIs (22.7 on a palm-muted one) while the gap between close presets was about 0.015. A score that moves 15 points with the DI cannot have an opinion about a 0.015 gap.
3. The (c) test is circular in one more way: target and candidate come from one engine (Native), so engine differences, microphone, pickup and separation damage are all absent.
4. The pass rule was loosened along the way. Level-only twins became ties (rule B, extended), which was reasonable for Native but means the gate no longer measures loudness matching, and Gate 2 was passed on the third sealed set (retrospective items 2 and 3, DECISIONS 2026-10-18). The human has since marked the setup unapproved.
5. When the same score was run blind, the first honest test ([T162](../../board/TASKS.md#t162 "No-DI step 2: corrupted-seed experiment") pilot, 12 items) put it at chance: right model 2 of 12 against about 0.19 expected by luck [R: seed-quality.md]. That is the direct measurement of "(c) does not transfer to (b)".
6. There is no measurement at all for (a) that uses real aligned-by-hand data, apart from the T041/T163/T164 searches. In those, the human heard the amp/cab match on Mother as not close and the top drive matches (DM4 Sub Oct Fuzz 5.37, Horizon Drive 5.81) as nothing like the target [R: TASKS [T163](../../board/TASKS.md#t163 "Play-along test (human 2026-10-18)"), [T164](../../board/TASKS.md#t164 "Play-along single-block match")].

The honest conclusion: **we have a score that is excellent at telling Native presets apart when nothing else varies, and no evidence that any score tracks how a human hears distortion similarity under realistic variation.** Section 5 puts the cheapest fix for the second gap first.

---

## 2. What we already tried, with the measured results

Everything in this section is from the repo. Be aware of one limit before the numbers: **every trust check measures "does the score put the true preset first among wrong presets", which is a proxy for sounding close. Nothing in the repo measures a score against your ears.** The only ear data is a few comments (below).

### 2.1 The scores, one by one

**`mrstft` (v1)**, [src/tonematch/metrics/mrstft.py](../../src/tonematch/metrics/mrstft.py). What it measures: short-time spectra at three window sizes (512, 2048, 8192 samples), spectral convergence plus log-magnitude difference, moment by moment. Trusted: same notes after the separation stand-in 0.73 (0.875 against audible distractors) on `helix-trust-v1`; on `helix-trust-v2` aligned 0.78 [R]. Failed: with different notes 0.18, i.e. it is not a valid use [R]. Why: it compares the exact waveform shape of each moment, so any change of notes ruins it.

**`mrstft_v2`**, [mrstft_v2.py](../../src/tonematch/metrics/mrstft_v2.py), weights [gate2-known-di-v3.json](../../data/weights/gate2-known-di-v3.json). What it measures: the same three window sizes, but the spectrum grouped into 64 bands spaced like the ear's frequency resolution (ERB bands, the ear's critical-band scale), distance = summed absolute band-magnitude difference over the target's total, and both clips scaled to equal loudness first. Trusted: `helix-trust-v2` test, 0.99 aligned and 0.95 after the separation stand-in, tuned on the other half with no overfit [R]. Drive was its worst kind: 0.92 aligned and 0.67 after separation [R]. With the `level_db` tie-break (a whole-clip loudness difference at weight 1e-7) the amp and drive test went from 0.90 to 0.98 (drive 37 of 45 to 43 of 45) [R]. Failed: (i) blind at chance ([T162](../../board/TASKS.md#t162 "No-DI step 2: corrupted-seed experiment"): 2 of 12 right model, 4 of 12 right category) [R]; (ii) DI dependence, the 7.7 / 11.0 / 22.7 above [R]; (iii) by design it cannot see a pure volume change, and the pure-volume knobs in Native (Master, Output, Boost) are identical sounds after loudness matching, which is why `level_db` exists [R]; (iv) the human heard its eventual matches as not close. It is a precise instrument for a situation we will almost never be in.

**`ltas_tilt`**, [ltas.py](../../src/tonematch/metrics/ltas.py). What it measures: the long-term average spectrum (the time-averaged power per frequency band, one-third-octave bands 80 Hz to 12 kHz) and its tilt (slope in dB per octave). Different notes: 0.03; same notes after separation stand-in: 0.35 [R]. A note change alone moves it by about 2.1 units, a one- or two-knob change by 0.2, and a different model of the same block by 1.8 [R]. Failed because the average spectrum follows the notes and the pickup as much as the pedal.

**`crest_flatness_hnr`**, [crest.py](../../src/tonematch/metrics/crest.py). What it measures: crest factor (peak over average level; clipping lowers it), spectral flatness (distortion fills the gaps between harmonics), and harmonic-to-noise ratio, as median and spread over frames. Different notes 0.13, different recording 0.03, same notes 0.40 [R]. The closest of the old terms to a distortion statistic, and still far from a usable score.

**`envelope`**, [envelope.py](../../src/tonematch/metrics/envelope.py). What it measures: attack time, decay, sustain slope, dynamic range, noise floor per note. Different notes 0.05 [R]. Useless for matching drive amount because it describes the playing as much as the pedal.

**Combined provisional sum** (the three above at weight 1). Different notes 0.05, different recording 0.03 [R]. This is the score the [T041](../../board/TASKS.md#t041 "End-to-end on a real song") and Mother searches actually used. Its real-use failures [R: TONE-COMPARISON]: it preferred the Essex A15 by 0.015 where you preferred the Placater; it pushed knobs to the ends of their ranges; it let a search turn the output down 52 dB because every term ignores level (the level guard is a patch); it liked more delay than you did.

**`embed_afxrep`**, [afxrep.py](../../src/tonematch/metrics/afxrep.py), [afx-rep.md](afx-rep.md). What it measures: cosine distance between 512-number embeddings from the ST-ITO "AFx-Rep" network (a PANNs-type CNN trained to tell which effect preset was applied). Different notes 0.15, different recording 0.075, same notes after separation 0.075; against random chains 0.78 (the best of any term) [R]. On the proxy set its held-out rank-1 was 0.067 against chance 0.0625 [R]. The note change moves it only about a fifth as far as the gap to a random chain, the best note-robustness of anything we have; but it is no better than the others on near misses. The paper itself says it does not enforce content invariance and does not work well for guitar tone matching [S, verify before quoting; per no-di-survey-metrics.md]. Cost 0.07 to 0.11 s per 10 s clip on CPU.

**Level and guards.** `level_db` and the level guard in the search exist because all the above are level-blind [R]. Not scores of tone; part of the safety harness.

**Presence detector**, [presence.py](../../src/tonematch/metrics/presence.py). Not a similarity score, but it shares the signal-processing: "is a gain stage present" is the one reliable no-DI call (0.81 precision / 0.89 recall without DI) [R]. Drive versus amp told apart only 0.38 to 0.58 precision [R].

**Family specialists** ([family-specialists.md](family-specialists.md), [TONE-COMPARISON](../TONE-COMPARISON.md)). The linear specialist (cab and EQ computed from a frequency-response curve) found the right model 24 of 24 with about 6 renders, but it needs the dry and handles only linear blocks [R]. The modulation score `mod_distance` got 0.94 against 0.27 for `mrstft_v2` when the wobble starts elsewhere [R]. These show that **a score designed around the physics of one effect family beats a general score by a wide margin.** There is no equivalent for distortion yet, and that is the gap this report is about. The amp/drive specialist is a search method, not a score; drive was the weakest family in Gate 2 attempt 2 (right drive model on 6 of 12) [R].

**`nonlinear.py`** ([source](../../src/tonematch/metrics/nonlinear.py)). Measures harmonic levels 1 to 12 against the fundamental on frames with a clear pitch, inharmonic share of 2-10 kHz (aliasing), and the part of the output that is not a linear function of the input (magnitude-squared coherence). It was built to compare Stomp and Native, and confirmed their drive tones agree to within 0.45 dB in spectrum and 0.3 dB in harmonic level on five drives [R: TONE-COMPARISON]. **It has never been used as a score of target versus candidate.** It is the closest existing code to what the circuit-grounded measurements in section 3B need.

**[T161](../../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)") invariance candidates**, [nodi_candidates.py](../../src/tonematch/metrics/nodi_candidates.py): `ltas_eqfit`, `texmod`, `mmd_cep`, `presence_nodi`. Registered 2026-10-18; the run was stopped by the human before any Helix number existed. Offline, `texmod` and `mmd_cep` already failed the kit's note-invariance test (same tone with other notes is as far as another tone) and `ltas_eqfit` and `presence_nodi` passed [R: DECISIONS [T161](../../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)") amendment]. `sepcons` (separation-consistent term) is in the contract; I found no trust result for it (unverified).

### 2.2 Failure patterns that cut across all of them

1. **Note and DI sensitivity swamps knob sensitivity**, 4.5 to 10 times for the general scores [R].
2. **10 % of distractors are the same sound** (78 of 760: a drive tone knob in front of a clean amp, a compressor barely engaging) and no score can rank them [R]. For distortion this means a score cannot be judged on knobs that do not change the sound.
3. **Pure-volume knobs** make several distinct settings identical after loudness matching [R].
4. **Separation damage** is as large as an amp change in dense mixes (spectrum term goes 2.0 to 2.6 against 3.4 for clean versus driven amp) [R: sep-stress-v1].
5. **Trust checks used the score's own training distribution.** The synthetic targets, the pool and the candidate renders were all Native. A score can pass that and still be wrong about a record or a real pedal. This is a testing-method flaw rather than a score flaw, and is the main reason section 5 starts with your ears.
6. **No ear data is stored.** I found no A/B session results in the repo (`ab_session.py` exists, no output files; unverified beyond a file search). The only ear evidence is anecdote: Placater over Essex A15 against the score's 0.015 gap; delay mix 0.10 over the matched 0.25; Mother amp/cab "not very close"; top drive matches "nothing alike" [R: TONE-COMPARISON, TASKS].

---

## 3. A catalogue of candidate scores

Format for each: **measures**, **pros / cons**, **robust to notes, guitar, pickup?**, **needs dry?**, **tracks perception?**, **cost**, **licence**, **fit for distortion**. "Robust" is my analysis [P] unless a source is given. For perception I only cite studies I found; for most items there is no study for guitar distortion and I say so.

### 3A. Signal and spectral

**Multi-resolution STFT (MRSTFT) and variants.** Measures how close two spectrograms (frequency content over time) are at several time resolutions. Used as a training loss throughout audio ML. Pros: cheap, differentiable, precise when aligned. Cons: moment-by-moment, so it punishes different notes and loose timing; blind to level when normalised. Robust: no. Needs dry: yes, aligned. Perception: used as a metric in amp-modelling papers together with listening tests, and those papers note objective metrics "do not tell the whole story" [S: [Wright et al., DAFx](https://dafx.de/paper-archive/details/FnATd0Lou6FQKgN2tAfAqg); [search summary](https://arxiv.org/pdf/2403.08559)]. Cost 0.06 s per 10 s [R]. Licence: ours. Distortion fit: good when aligned (drive 0.92), poor otherwise [R].

**Log-mel / ERB / Bark band spectra.** Measures energy in bands spaced like the ear (mel, ERB, Bark are three versions of that scale). Our `mrstft_v2` is the ERB case [R]. Same pros and cons as MRSTFT when compared frame by frame. Averaged over time it becomes LTAS and inherits LTAS problems. Cost milliseconds. Distortion fit: tells you the EQ half of the pedal, not the gain half.

**LTAS and tilt.** Average spectrum and its slope. Pros: instant, no dry, no alignment, directly captures the post-clipper EQ and cab. Cons: notes, pickup position (comb-like notches, neck versus bridge balance; physics from [Till](https://till.com/articles/PickupResponse) and [Lollar](https://www.lollarguitars.com/inductance), [S]) and technique move it as much as the pedal. Distortion fit: indirect. A fitted low-order correction (`ltas_eqfit`) tries to subtract the pickup part but may also subtract real EQ [P]. Measured weak [R].

**Cepstral distance (MFCC and relatives).** The cepstrum is the spectrum of the log spectrum; low-order coefficients (MFCC) describe the smooth spectral envelope, separating the vocal-tract-like envelope from the pitch. Pros: pitch-light, standard in timbre similarity. Cons: envelope only, so it sees filtering but little of clipping's fine harmonic structure; MFCC distances were used inside our `mmd_cep`, which failed the offline note test [R]. Cost milliseconds. Distortion fit: weak alone.

**Spectral flatness and contrast.** Flatness (how noise-like the spectrum is) and contrast (peak-to-valley difference in each band) both rise with clipping. Our `crest_flatness_hnr` is the existing version [R]. Pros: cheap, no dry. Cons: depends on how hard the DI hits the gain stage (a hidden variable), so palm-muting changes it [R: no-di-survey-metrics.md]. Fit: it is a distortion-amount cue, not a distortion-character cue.

### 3B. Distortion-specific measures grounded in the circuit

The pedal model the human gave us: **gain stage → clipping stage → EQ stage**. An overdrive is the input boosted, then squashed by a clipper (diodes or op-amp saturation), then tone-shaped. Each stage leaves a trace in the output. These are the measurements that are specific to this task. Reading of the circuit side is in T167a/b; reading from audio is T167c (pending). General background: distortion pedals as a "filter, memoryless nonlinearity, filter" block structure, the Wiener-Hammerstein model ([Colonel, Comunità and Reiss, AES 2022](https://www.eecs.qmul.ac.uk/~josh/documents/2022/21955.pdf), [S]; [DAFx block-oriented modelling](https://dafx.de/paper-archive/details/bKxNxdS0ua9iEdRrM6zPlg), [S]).

| Measure | What it reads | Needs dry? | Robust to notes/guitar/pickup? | Fit | Cost | Licence |
|---|---|---|---|---|---|---|
| **Harmonic profile versus level.** Level of harmonics 2, 3, 4... relative to the note, measured at several input levels (louder and softer notes in the same clip) | the clipper's curve shape, and how drive changes with picking strength | No, if a pitch tracker can find the note | Medium: measured per note then averaged, so pitch drops out; pickup colours the input spectrum [P] | High | ms | ours |
| **Even / odd harmonic ratio.** Even harmonics (2, 4, 6) come from an asymmetric clipper (a single diode, a germanium fuzz); odd (3, 5, 7) from a symmetric one | clipping symmetry | No (pitch needed) | Medium to high: a ratio of ratios cancels most of the pickup colour [P] | High (symmetric versus asymmetric is the biggest audible split between pedal families) | ms | ours |
| **Intermodulation in chords.** Two simultaneous notes create sum and difference tones (f1+f2, f2-f1) that sound "muddy" in a hard-clipping stage and are cleaner in a soft one | softness of the clip; one reason a power chord distinguishes pedals | No, polyphonic pitch needed | Medium: depends on chord shapes in the passage [P] | High for chordal material | tens of ms | ours |
| **Estimated static transfer curve and knee.** Plot instantaneous output against estimated pre-clipper input; the knee is where the curve starts to bend | clip threshold, hardness | Easy with dry; hard blind because the input is unknown, but a Hammerstein-style fit can estimate it jointly | Needs a fit; pickup and filters before the clipper confuse it [P] | Highest | seconds | ours |
| **Amplitude histogram and crest factor.** Distribution of sample values: a hard clipper piles up samples at the rails | clipping depth | No | Medium: depends on the dynamic range of the playing | Medium (our `crest` term is the summary) | ms | ours |
| **Compression and sustain.** Envelope decay of a note compared with a clean decay; heavy clipping flattens the envelope and lengthens perceived sustain | gain amount, compression | No | Low to medium: note length and damping vary [P] | Medium | ms | ours |
| **Pre-EQ versus post-EQ estimates.** Pre-EQ (a low cut before the clipper) shapes which frequencies clip; post-EQ (tone stack, cab) shapes the result | the two EQ stages separately | Easy with dry; hard blind | Pre-EQ is hidden under the pickup; post-EQ is robust | High | seconds | ours |
| **Noise and gate behaviour.** Noise floor when the string stops; hiss gain from the high-gain stage; whether the pedal or a gate cuts the tail | gain amount, circuit class | No | Medium: depends on pauses in the passage | Low to medium | ms | ours |
| **Wiener-Hammerstein fit error.** Fit pre-filter, waveshaper, post-filter to the target; report how well it fits and the parameters | all three stages at once | Easier with dry; blind needs the joint fit | Good if the model class is right | Highest ceiling, and the whole of section 3G | seconds to minutes | ours |

Evidence: I did not find a published listening study that ties these specific measures to similarity judgements of guitar distortion. What exists: harmonic spectra and sensory consonance of distorted chords change perceived heaviness and valence ([Herbst, heaviness and electric guitar](https://eprints.hud.ac.uk/33381/), [S]); high-gain amp modelling listening tests where harmonic distortion is clearly audible on bass notes ([Wright et al.](https://dafx.de/paper-archive/details/FnATd0Lou6FQKgN2tAfAqg), [S]); and a 177-listener test where hardware amps versus digital profiles were hard to tell apart, d' = 0.34 ([Confusingly Similar](https://labs.sonicfield.org/library/confusingly-similar-discerning-between-hardware-guitar-amplifier-sounds-and-simu), [S]). So the plain truth is that these measures are physically well-motivated and perceptually unvalidated. Their validation is exactly the first experiment in section 5.

### 3C. Perceptual audio-quality models

These come from codec and hearing-aid research. They compare a reference to a degraded copy and predict how much a listener would mark it down. **They are designed for "same signal, small degradations", so they need the aligned dry and are the wrong shape for case (a) or (b).** They might still be useful as sanity metrics in case (c).

| Model | What it measures | Robust? | Needs dry? | Perception evidence | Cost | Licence | Distortion fit |
|---|---|---|---|---|---|---|---|
| **PEAQ** (ITU-R BS.1387) | difference of ear-model excitation patterns and modulations between reference and test | no | yes, aligned | ITU standard; a DAFx study found PEAQ Basic, PEMO-Q and HAAQI track progressive degradations (hum, hiss, clipping, glitches) while PEAQ Advanced failed [S: [DAFx evaluation](https://dafx.de/paper-archive/details/zPYxwsZQRNgBM4XY2Azt4A), [PEAQ analysis](https://arxiv.org/pdf/2212.01467)] | slow-ish | open-source implementations vary; check [M] | low: tuned for coding artefacts, not 100 % distortion |
| **ViSQOL v3** | similarity of spectro-temporal patches (NSIM) between reference and degraded | no | yes | most consistent in robustness tests but low sensitivity to hum and glitches [S: same DAFx study; [ViSQOL v3](https://arxiv.org/pdf/2004.09584)] | moderate | Apache-2.0 [M, verify] | low |
| **PEMO-Q** | auditory model plus modulation filterbank, correlation of internal representations | no | yes | effective in the DAFx degradation test [S]; Huber and Kollmeier 2006 [M] | moderate | the original is not open [M] | low to medium (modulation-aware) |
| **HAAQI** | hearing-aid music quality: auditory model of the cochlea, then envelope and fine-structure correlation | no | yes | effective and consistent in the same study [S]; Kates and Arehart, validated on hearing-impaired ratings [M] | moderate | open Python ports exist [M] | low to medium |
| **Zwicker loudness, sharpness, roughness** | loudness (sone), spectral weight toward the highs (acum), amplitude-modulation roughness (asper), standard psychoacoustic quantities | yes: single-clip statistics, no reference | no | sharpness and roughness correlate with the "bite" and "fizz" of distortion in general psychoacoustics [M] | ms | standards (ISO 532-1, DIN 45692); MoSQITo is open [M] | medium as descriptors; not a similarity |
| **Auditory models: gammatone filterbank, modulation filterbank** | a better "front end": filterbank like the cochlea then envelope and modulation rate analysis | n/a | no | basis of PEMO-Q and of sound-texture work [M] | ms | many open | useful as the front end for 3B, 3E and 3G, not as a score by itself |

Verdict: the *front ends* are worth borrowing (an ear-like filterbank for harmonic measurement, modulation analysis for "fuzziness"); the *full scores* are expected to be poor at our task because they assume the same note sequence. I would test one (HAAQI or PEMO-Q) as a case (c) sanity metric only if the human wants a published-standard comparison.

### 3D. Learned perceptual metrics

| Model | What it measures | Robust? | Needs dry? | Perception evidence | Cost | Licence | Distortion fit |
|---|---|---|---|---|---|---|---|
| **DPAM** | deep features trained to match human just-noticeable-difference ratings on small perturbations | partly | no | needed many human annotations and generalised poorly beyond its training perturbations [S: via CDPAM] | GPU or CPU, moderate | research [M] | low (trained on speech perturbations) |
| **CDPAM** | contrastive learning plus human triplet judgments, multi-dimensional | partly | no | correlates with human responses across nine varied datasets [S: [arXiv 2102.05109](https://arxiv.org/pdf/2102.05109)] | moderate | Adobe/Princeton research code; licence not read | unproven; never tested on distortion |
| **CLAP, MERT, BEATs, PANNs** | general music/audio embeddings | pitch- and content-heavy | no | MERT-v1 FAD correlated very weakly with human ratings in one study [S: [arXiv 2403.17508](https://arxiv.org/pdf/2403.17508)]; not built for effects | 0.1 to 1 s | MERT-v1 CC BY-NC 4.0 and some CLAP variants NC [S: no-di-survey-metrics.md] | low |
| **AFx-Rep** (ST-ITO) | effect-preset embedding | partly | no | 23 audio engineers rated similarity (0 to 100) in the paper [S: [arXiv 2410.21233](https://arxiv.org/html/2410.21233v1)] | 0.07 s | Apache-2.0 tag; training data includes NC sets [R: afx-rep.md] | tried; weak [R] |
| **Fx-Encoder / Fx-Encoder++** | contrastive effect embedding; negatives share content, differ in effects | partly | no | disentanglement and mixing-style tests; R@1 26.1 % among 500 candidates on mixtures for ++ [S: [Fx-Encoder](https://arxiv.org/pdf/2211.02247), [Fx-Encoder++](https://arxiv.org/html/2507.02273v1)] | moderate | Fx-Encoder code MIT, weights trained on MUSDB18; Fx-Encoder++ CC BY-NC 4.0 [S] | low to medium; no guitar in the ++ evaluation |
| **Open-Amp encoder** | SimCLR on guitar: positives = different clips through the same device, negatives = other devices; 113k parameters | **yes by design** | no | device classification 72 to 99.8 % across datasets [S: [arXiv 2411.14972](https://arxiv.org/html/2411.14972v1)]; no similarity listening test | 0.01-0.1 s | paper says CC BY 4.0; checkpoint existence and licence unconfirmed [S] | highest of the published learned options (guitar, 101 pedals) |
| **EG-VAE tone code** | VAE splits per-frame content from one global "tone" vector | yes by design | no | tone MOS 4.30 vs 2.30 for seen tones from 14 listeners [S: [arXiv 2608.05513](https://arxiv.org/abs/2608.05513)] | GPU for training | weights and code not stated [S] | high if released; trained on Neural DSP tones |
| **TRR** (TimberAgent texture descriptor) | Gram matrix of mid-level wav2vec2 features, random-projected; no training | unknown | no | 26-person study vs a MusicGen baseline; cosine 0.825 vs 0.573 (CLAP) on preset retrieval [S: [arXiv 2603.09332](https://arxiv.org/html/2603.09332)] | one wav2vec2 pass | code promised [S]; wav2vec2 weights [M] | unproven for distortion |
| **Chen et al. tone embedding** (ISMIR 2024) | SimCLR: same tone, different playing content | yes | no | t-SNE clusters by amp only, no retrieval or listening [S: [arXiv 2407.10646](https://arxiv.org/html/2407.10646v1)] | small | no checkpoints found [S] | unproven |

Notes: the literature measures device classification, retrieval among presets, and parameter regression. **Nobody publishes the measurement we need**, which is "does it still see a 10 percent gain change when the guitar and the notes are different" (no-di-survey-metrics.md). So a published learned score has to be checked by us in any case.

### 3E. Distribution distances

These ask whether two clips draw their sound frames from the same population, rather than whether frame k matches frame k. That is exactly the right idea for case (b), because it throws away the note order. All need **seconds to minutes of audio per side** (several clips ideally), not one note.

| Distance | What it does | Needs | Perception | Cost | Licence | Fit |
|---|---|---|---|---|---|---|
| **FAD** (Fréchet audio distance) | fits a Gaussian to each clip set's embeddings, measures the Fréchet distance | many clips; biased for small samples because of the Gaussian assumption [S] | Spearman about 0.5 with human ratings at best [S: [arXiv 2403.17508](https://arxiv.org/pdf/2403.17508)] | embedding cost | open code | poor for single clips |
| **KAD** (kernel audio distance) | MMD with a kernel, unbiased, distribution-free | works with fewer samples [S: [arXiv 2502.15602](https://arxiv.org/html/2502.15602v1)] | claims better human alignment than FAD [S] | low | open [S] | plausible if the frame features keep distortion and drop pitch |
| **MMD on our own frame features** | same idea on cepstral and distortion features | one clip each | none; our `mmd_cep` failed the offline note-invariance test with MFCC-style features [R] | ms | ours | needs distortion-bearing features, not just MFCC |
| **Wasserstein / sliced Wasserstein** | earth-mover distance between feature clouds | one clip each | none for audio [M] | ms | ours | same dependence on features |

The decisive point: the **choice of frame features** matters far more than the distance. Pitch-heavy embeddings (CLAP, MERT) will make "different notes" look like "different tone".

### 3F. Discriminative and contrastive scores trained on our own Native renders

We can generate unlimited pairs of "same pedal settings, different DI" and "different settings, same DI", with ground truth, on a single machine overnight. A learned metric trained to pull the first pair together and push the second apart is the Open-Amp / Chen idea with our own data.

**Siamese or triplet metric.** Train a small network f so that f(A) is close to f(B) when A and B are the same drive setting on different DIs. Pros: directly optimises the thing we need (note and guitar invariance), cheap to run, can be trained on CPU for small models [P]. Cons: only as good as the variety of DIs (5 h of Guitar-TECHS, three players; DI library nearly exhausted per retrospective item 6 [R]), and risks learning "Native" rather than "distortion" (a model trained on Native renders may not transfer to a real pedal; the human has said weights trained on Native renders may be published [R: DECISIONS 2026-10-18]). Licence: ours. Perception: none until compared with your ratings. Needs dry: no. Fit: high, if the positives are drives only. The [T165](../../board/TASKS.md#t165 "Predictor-led matching design") predictor design ([predictor-design.md](predictor-design.md)) already trains something similar for model identification; a metric is the same data with a different loss.

**A learned metric fitted to the human's own A/B judgements.** Take a feature vector for each clip (the fingerprint of 3B, or an embedding), and learn a distance (a weight per feature, or a small linear or Mahalanobis metric) from your ratings of pairs. Pros: it *is* your ear, with known statistical machinery. Cons: needs ratings (see section 5, A) and risks overfitting 40 to 100 ratings; you would need to guard with held-out pairs. Licence: ours. Fit: the best match to "the ear is the final judge", but it does not by itself fix invariance.

### 3G. Analysis-by-synthesis

Instead of comparing audio, fit a parametric model of gain → clip → EQ to the target, then compare the fitted *parameters* (clip threshold, symmetry, knee hardness, pre/post EQ corners). Differentiable gray-box versions exist: Wiener-Hammerstein with graphic-EQ pre/de-emphasis and a parameterised waveshaper ([Colonel et al.](https://www.eecs.qmul.ac.uk/~josh/documents/2022/21955.pdf) [S]), differentiable black-box versus gray-box nonlinear effect models including distortion and fuzz with listening tests ([Comunità, Steinmetz, Reiss, 2025](https://arxiv.org/abs/2502.14405) [S]), differentiable white-box circuit models whose component values are learned from input/output audio ([Differentiable White-Box VA](https://dafx.de/paper-archive/details/BuG_2piIUOi3oZvJGTn48g) [S]), and a diode-clipper-with-low-pass as the dominant distortion mechanism ([Simplified Physically-Informed Models](https://dafx.de/paper-archive/details/mIq7vlQKu6pUlF467vvhgw) [S]).

Pros: the comparison space is the circuit, which is the space the human wants us to understand; a few numbers describe a tone; invariant to notes in principle because the fit explains them away. Cons: needs a good model class; a bad fit (local minima, ambiguous pre-EQ vs post-EQ) gives confidently wrong parameters; needs the input signal unless fitted blind (blind fit is far harder); published work uses the dry. Needs dry: yes for the known methods. Cost: seconds to minutes per fit. Perception: fits were judged by listening in the papers, not as a similarity metric [S]. Licence: ours. Fit for distortion: it is the textbook approach for memoryless distortion; the heavy-handed blind variants are the "UnNAFx / LOUDAR / Distortion Recovery" family, which instead recover the dry ([no-di-survey-estimation.md](no-di-survey-estimation.md)).

---

## 4. Unproven ideas with potential

All of these are **unproven**. Each has: why it might work, the risk, and the cheapest test. Data: "on disk" means I believe the renders exist, not that I checked; the drive-specialist calibration cache was deleted on 2026-10-18 for disk space (TASKS [T162](../../board/TASKS.md#t162 "No-DI step 2: corrupted-seed experiment")) so any test that needs it must re-render a small subset.

**U1. Circuit-parameter space as the comparison space.**
Estimate clip threshold, clip symmetry (even/odd ratio), knee hardness, and pre/post EQ corners from each clip; score = distance between the estimates, in units scaled by how much they change per knob. Why it might work: the human's own model says a pedal *is* those few numbers; target and candidate differ only in them. Risk: estimates from polyphonic, separated audio may be noisy; pre-EQ is confusable with post-EQ. Cheapest test: on the Native drive renders at known settings, estimate the numbers from the wet only and report error against the known truth, with the DI changed between estimate and truth. If the estimates move with the DI more than with the knob, drop it. No new score needed to find that out.

**U2. A "tone fingerprint" with per-measurement weights learned from your ratings.**
A vector of 15 to 30 distortion measurements (section 3B plus `crest_flatness_hnr` plus post-EQ bands), distance = weighted sum; weights fitted to the human's ratings of pairs. Why: it uses the physics for invariance and the ear for weighting, and it is interpretable (you can read which measurement carried a verdict). Risk: your ratings are the bottleneck and may be noisy; weights fit on 50 pairs overfit easily. Cheapest test: compute the fingerprint for the clips already in listening packs (cache/matches/*/listen/, 10 s each), ask for 30 pairwise ranks in 20 minutes, report rank correlation of each single measurement against your ranks, no fitting.

**U3. Score on matched note events.**
Detect note onsets in both clips, pair notes by pitch (within a semitone) and similar duration, and compare per note (harmonic profile, decay, attack bite), averaging over the pairs. Why: it removes the biggest nuisance (which notes) without throwing away the content like LTAS does; it works for the loosely aligned case (a) because only onset pairing is needed, not sample alignment. Risk: polyphonic chords and a separated stem make pitch pairing noisy; a second guitar part would confuse it. Cheapest test: on helix-trust-v1 renders (different notes, same recording), pair notes between target and candidate and compute the same per-note statistics; repeat the rank-1 check. Compare to LTAS at 0.03.

**U4. Score only the sustained portion.**
Compare the rendered DI against the target only on segments where a note or chord is held and steady (after the attack, before the decay), where the clipper has reached its steady state and the playing technique matters least. Why: attack transients are where picking technique, pickup and room make recordings differ; sustain is where the clipper and EQ dominate. Risk: songs with palm mutes or fast riffs have little sustain; compression/sag is lost. Cheapest test: use `envelope.py`'s onset finder to mask to sustain and rerun the existing terms on helix-trust-v1 held-out notes; if rank-1 does not move off 0.03 to 0.15 the idea is dead.

**U5. Differentiable gray-box pedal as the scorer.**
Fit a Wiener-Hammerstein-style model (graphic-EQ pre filter, parameterised waveshaper, post filter) to the target with gradient descent; the loss is the fit residual and the compared quantity is the fitted waveshaper curve plus EQ. Why: a measured static curve is the thing a human reading a circuit diagram would compare. Risk: the waveshaper is memoryless, but many pedals have memory (op-amp slew, diode capacitance, tone stack interaction); Helix models can include those. Cheapest test: reproduce the published fit on one Native drive in the known-DI case first; if the fitted curve does not match the one measured with a sine sweep (the project already has block probes), stop.

**U6. Ear-in-the-loop active learning.**
Show you the pair about which the current metric is most uncertain, take your answer, update the metric (a few-line update of a linear weight vector), repeat for 20 to 40 queries. Why: it spends your scarce attention where it changes the metric most, and gives a calibration curve for every score in the report. Risk: ear fatigue; the metric only learns your taste for the pairs shown. Cheapest test: replay in software with a simulated listener first (labels from a known weight vector), check that 30 queries recover it, before taking your time.

**U7. Re-separation-consistent score.**
Add the candidate to a mix of the other stems, run the same separator, and compare the separated result with the target stem. Why: the target carries separator artefacts; comparing like with like removes them. This is the `sepcons` term already in the contract [R]. Risk: costs a separation per candidate, so only practical as a final re-ranker; no trust number exists. Cheapest test: check whether `sepcons` is run in any existing report; otherwise one sep stress run on the Mother window with 5 candidates.

**U8. Dry-estimate bridge.**
Estimate a dry guitar from the wet target with a distortion-recovery network (Distortion Recovery 2024, EG-VAE tone removal, LOUDAR, UnNAFx, listed in no-di-survey-estimation.md) and then use the known-DI scores (case (c)) against it. Why: it turns blind into the case we already score best. Risk: recovery error on heavy distortion is large (ill-posed for hard clipping per Imort et al. [S]) and errors in the recovered dry look like pedal differences. Cheapest test: take a Native drive render, recover its dry with a released model if one has public weights (unconfirmed for all), and see how far the recovered dry is from the true dry in `mrstft_v2` terms.

**U9. Consistency across several dry signals (a "robust ranking" score).**
Score a candidate against the target on K different DIs, take the median or minimum, and also record the spread. Why: [T041](../../board/TASKS.md#t041 "End-to-end on a real song") shows the DI-wide spread is the dominant error source; the spread itself is a confidence measure. Risk: K times the render cost ([T041](../../board/TASKS.md#t041 "End-to-end on a real song") estimated 3x). Cheapest test: reuse the [T041](../../board/TASKS.md#t041 "End-to-end on a real song") DS-Sample numbers (6.94 vs 9.51 vs 16.04) and the helix-trust-v1 renders on different DIs to see whether the median ordering of candidates is more stable than any single DI's ordering.

**U10. Level-sweep consistency.**
Compare how the tone changes when the playing gets louder or softer, from natural dynamics in the clip, between target and candidate. Why: a pedal's character is mostly *how it responds to input level*, which a single-level average hides. Risk: needs enough dynamic range in the passage; a compressor in the original track flattens it. Cheapest test: on Native drive renders with the DI amplitude swept by a known gain, compute harmonic content versus input level and check that two drives with equal average spectrum but different gain-curve shapes separate.

**U11. Sound-texture statistics tuned for distortion.**
Extend `texmod` (McDermott-Simoncelli style subband envelope moments, cross-band correlation and modulation power, [paper](https://mcdermottlab.mit.edu/papers/McDermott_Simoncelli_2011_sound_texture_synthesis.pdf)) with cepstral smoothing across pitch. Why: distortion is texture-like (a dense harmonic haze). Risk: the offline check already says current `texmod` fails note-invariance [R]. Cheapest test: re-run that offline check after a pitch-normalising change.

---

## 5. Ranked shortlist (proposals for the human)

Ranked by the chance of getting a decisive answer for least cost. Nothing is started.

**Proposal 1 (do this first): collect your ear, and test every existing score against it.**
- *Why first:* we have never validated any score against your hearing. Every other proposal needs this as its yardstick.
- *Experiment:* build 20 to 30 A/B/target triples from clips that already exist (the listening packs under `cache/matches/*/listen/`, the [T164](../../board/TASKS.md#t164 "Play-along single-block match") drive matches). You rank which candidate is closer to the target. We then compute Spearman rank correlation of each existing score (`mrstft_v2` where aligned, `ltas_tilt`, `crest_flatness_hnr`, `envelope`, `embed_afxrep`, and the section 3B measures) with your ranks. 30 triples gives a coarse answer in 20 minutes of your time.
- *Listening check:* this is the listening check. Blind (scores hidden), randomised order, and a repeat of 5 triples to measure your own consistency.
- *Costs:* your time; minutes of compute.
- *Success looks like:* some score reaching rank correlation above 0.6, or learning that none do.

**Proposal 2: the distortion fingerprint, measured and checked for invariance (U1 plus 3B).**
- *Experiment:* compute harmonic profile versus level, even/odd ratio, crest/flatness, post-EQ band shape, and a coarse static-curve estimate for each clip. On Native drive renders we have (helix-trust-v2 train drive items; helix-trust-v1 distortion items; regenerate a small subset if the calibration cache is gone), measure for each feature the **invariance ratio R**: spread of the feature for the same drive setting on different DIs, divided by spread across different settings on one DI. R below 1 is a usable feature. This is the [T161](../../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)") protocol applied to new features, no new Native renders needed.
- *Listening check:* take the features that pass, order the [T164](../../board/TASKS.md#t164 "Play-along single-block match") drive candidates by them, and have you rank the top 5 against the target (the same procedure as Proposal 1).
- *Cost:* hours of coding, minutes of compute.

**Proposal 3: Open-Amp-style contrastive metric on our own drive renders (3F, siamese).**
- *Experiment:* confirm whether the Open-Amp encoder ships weights and its licence (a ten-minute check); in parallel train a small siamese network on existing drive renders with positives = same setting on a different DI. Evaluate by the same held-out-model, held-out-DI rank-1 check, not on training models.
- *Listening check:* same as Proposal 1, on the network's top 5 for the [T164](../../board/TASKS.md#t164 "Play-along single-block match") target.
- *Cost:* moderate, CPU-scale (small model). Licence: ours if retrained; if Open-Amp's weights are used, check them first. A model trained on Native renders is derived from Native's output; the human has accepted publishing the weights (2026-10-18), with the residual EULA risk noted there.

**Proposal 4: matched-note and sustained-segment scoring (U3, U4).**
- *Experiment:* a mask-and-pair wrapper around the existing terms; rerun the helix-trust-v1 held-out-notes check (baseline 0.03 to 0.15). Use known truth to see if rank-1 moves materially.
- *Listening check:* only if rank-1 passes 0.5; then repeat Proposal 1 with it.
- *Cost:* hours of coding; minutes of compute. Works in case (a) too.

**Proposal 5: gray-box fit as the scorer (U5, 3G).**
- *Experiment:* in the known-DI case first, fit one Native drive with a Wiener-Hammerstein model and compare the fitted waveshaper to the swept static curve from the block probes. If it matches to a few percent, move to the different-DI case and finally the blind case.
- *Listening check:* resynthesise the fitted model on your DI and listen against the real Native drive (is the fit audibly the same?), then apply to the target.
- *Cost:* highest effort; highest ceiling; this is the closest to the human's "figure out the gain, clipping and EQ" instruction.

Not on the shortlist: PEAQ / ViSQOL / HAAQI (right idea, wrong situation; the test would take an afternoon if you want them as references), MERT/CLAP (note-heavy, NC licences), Fx-Encoder++ (NC), FAD (needs many clips). `mrstft_v2` stays as the case (c) instrument for any aligned check, with the caveat that it passes where nothing is hard.

**Proposed order of work for you to approve:** 1 → 2 (in parallel with the Open-Amp licence look from 3) → 4 → 3 → 5. Items 1 and 2 reuse data on disk and can run in a few hours. The decision of which score to adopt, and what its bar is, is yours; I recommend the bar be set from your ratings (Proposal 1), not from the synthetic rank-1 test alone.

---

## 6. Licence flags (not legal advice)

- NC: Fx-Encoder++ (code CC BY-NC 4.0), MERT-v1 (CC-BY-NC 4.0), some CLAP checkpoints (CC BY-NC-SA 4.0 for one music variant) [S: no-di-survey-metrics.md]. Anything built on them is non-commercial, per the 2026-10-18 DECISIONS entry.
- AFx-Rep: Apache-2.0 tag, but training sources include MUSDB18 (educational-use only) and others probably NC; unsettled for bundled release [R: afx-rep.md].
- Fx-Encoder: code MIT; checkpoints trained on MUSDB18 only [S].
- Open-Amp: paper says CC BY 4.0 for the package; checkpoint presence and licence of the tone packs unconfirmed [S].
- EG-VAE, TRR, Chen et al.: no weights or code licence stated [S].
- Everything in 3B, 3E (own features) and 3F is ours. A metric trained on Native renders is derived from Native's output (project legal summary); weights may be published per the human's 2026-10-18 ruling, with a model card and the `licence-check` first.

## 7. What I could not settle

- No published study ties any of these scores to listener judgements of guitar distortion similarity. The closest are amp-modelling listening tests, the engineers' test for AFx-Rep, and MOS in EG-VAE, all [S].
- Most paper claims here come from fetch or search summaries, marked [S]. Two fetches failed outright (the DAFx25 PDF text and the QMUL Colonel PDF, a TLS error); nothing is claimed from them beyond search-result summaries.
- I did not read the DAFx25 paper's numbers (PEAQ, PEMO-Q, ViSQOL, HAAQI under clipping); only its summary.
- Whether the old trust sets still exist on disk was not checked; `cache/drive-specialist-v0` is recorded as deleted.
- [T161](../../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)") produced no Helix numbers; any statement about `ltas_eqfit` or the others on Helix renders is therefore open.
- I did not look at `contracts/` beyond the loss kit or at any sealed set.
