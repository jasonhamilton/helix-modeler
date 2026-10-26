# Blind reverb estimator, [T231](../../board/TASKS.md#t231 "Blind reverb estimator") ([T231](../../board/TASKS.md#t231 "Blind reverb estimator"))

Owner: tone-metrics. Date: 2026-10-25. **Measurement only; nothing is adopted.** The human's words after [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")"): "how can you not detect reverb? try harder. this is stupid." [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")")'s four signals needed either a dry reference of the same performance (the regressors), real silences (the note-tail method), or were learned embeddings and envelope percentiles that did not follow the human's ranks. This task builds an estimator that reads the reverb from the signal alone, validates it on known truth first, and only then reads the songs. Local only: song audio stays under `cache/`.

**Short answer.** One of the three estimators (abandoned-band decay averaging, `adc`) recovers the decay length and the mix level on synthetic ground truth: on the 189 [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") candidates it orders short < medium < long correctly in 36 of the 40 (type, Mix) triples where it reads at all, with a mean rank correlation of 0.86 to 0.89 against the true tail length on five songs, and its tail-level reading rises with Mix in 52 of 52 triples. It cannot tell Room from Plate from Hall (43% against 33% chance). It reads nothing on seek-destroy and teen-spirit in 10 s: those riffs never leave a frequency band alone for 250 ms, and with reverb added the drops it needs vanish altogether. The textbook running-signal estimators (spectral decay distribution, maximum-likelihood decay rate) work on synthetic noise but not on distorted guitar: the dry guitar's own note decays sit at 0.4 to 1.0 s and swamp the reverb's. **On the full-length separated stems the reading is dominated by the separator's residue:** when the guitar leaves a band, the stem keeps 10 to 13 dB of other material there, falling only 1 to 2 dB per 100 ms, the same curve the full mix gives; a real tail is readable only on Mother (0.70 s) and Territorial (0.90 s). Post hoc, on the three songs where both the stem and the candidates give a reading, the distance in (decay, tail level) between a candidate and the stem agrees with the human's ranks (rho 0.65, 0.24, 0.80; mean 0.56), and picks the human's closest candidate on two of three; this is after the fact and on three songs, so it is a lead, not a result.

## 1. Literature (brief; FACT = read in the source, UNVERIFIED = from secondary sources or memory)

- **Ratnam et al. 2003, "Blind estimation of reverberation time", JASA 114:2877-2892.** FACT (read the paper): the reverberant tail in a window is modelled as exponentially damped Gaussian noise y(n) = a^n x(n); the ML decay rate a* is the root of the score equation (their Eq. 8), solved iteratively with the variance from Eq. 11; estimates run continuously over frames and an order-statistics filter picks the final RT: the left tail of the histogram at a chosen percentile (Eq. 12) or the first dominant peak from the fast side (Eq. 13), because "the damping of sound in a room cannot occur at a rate faster than the free decay" so the fastest consistent estimates are the room. On connected speech in a room measured at 1.66 +- 0.07 s by hand-claps it read 1.7 s (peak of 1 s histograms) or 1.83 s (whole-recording histogram). Also FACT: the `blind_rt60` package on PyPI/GitHub (nuniz) implements this method, MIT licence; not used here (numpy/scipy reimplementation, 30 lines).
- **Wen, Habets, Naylor 2008, "Blind estimation of reverberation time based on the distribution of signal decay rates", ICASSP.** UNVERIFIED (abstract and citations only): decay rates are fitted per STFT bin over short segments; the shape of their distribution (the negative side) maps to RT60; the mapping was trained on speech.
- **Sub-band decay histograms / free-decay selection**: UNVERIFIED: Prego et al. (UFRJ, 2012-2015) restrict the Schroeder-style fit to free-decay regions found per sub-band; Löllmann & Vary (2010, EUSIPCO) use ML on sub-bands with a histogram. Kendrick, Cox et al. 2007 ("Blind estimation of reverberation parameters for non-diffuse rooms", Acta Acustica 93) use ML decay on music too; a 2008 follow-up for non-speech used an envelope modulation spectrum (UNVERIFIED).
- **Onset-synchronous averaging**: no paper found that names it so; it is the mirror of what is done here (offset-synchronous: average after the band is abandoned). UNVERIFIED as a published method.
- **ACE challenge (Eaton et al. 2016, IEEE/ACM TASLP; results tech report arXiv 1606.03365).** FACT (from the report's front matter and the challenge page): real-room speech, 0.2 to 1.5 s T60, DRR -1 to 18 dB, with noise at 18/12/-1 dB SNR; single-channel T60 and DRR estimation were evaluated by bias, MSE and correlation in three T60 bands. UNVERIFIED (memory): the best full-band T60 methods reached about 0.9 correlation with errors of a few hundred ms; DRR estimation was much harder, with the best single-channel methods barely better than guessing the mean.
- **Neural estimators 2020-2025**: Gotz/Looney/Bryan et al., "Joint blind room acoustic characterization from speech and music signals using convolutional recurrent neural networks" (arXiv 2010.11167, 2020): RT60, DRR, C50/C80 from speech and music with a CRNN; music in training helps (FACT from the abstract); code/model release and licence not stated on the abstract page (UNVERIFIED). Noise-aware two-stage CNN RT60 estimators (2021-2022, arXiv 2112.04726) are speech-only. None of these ship a model with a stated permissive licence that I could find; nothing was downloaded.
- **Chosen for a day's numpy/scipy work:** (1) Wen-style spectral decay distribution with the physics mapping, (2) Ratnam's ML decay rate with the same free-decay selection, (3) abandonment-synchronous decay averaging (my own construction from the idea above). All three share one spectrogram.

## 2. Method (`src/tonematch/metrics/reverb_blind.py`)

The spectrogram: 1024-point Hann frames (21 ms) every 5 ms, bins summed into 1/6-octave bands (at least 3 bins wide) between 150 Hz and 3.6 kHz (the pedal reverbs here run LowCut 150, HighCut 3700; the separator keeps little above), power smoothed over 30 ms, levels floored 90 dB under the peak so digital silence does not count as a decay.

- **sdd.** Every band, every sliding 250 ms window (25 ms step): a straight line fitted to the level in dB. "Free decay" = slope < 0, R^2 >= 0.9, at least 5 dB of fall, both halves of the window falling at rates within a factor 2 of each other (a knee, direct sound giving way to tail, fails this), and the window at least 6 dB above the band's 5th-percentile floor. RT60 = -60 / slope. Readings: the 10th percentile ("p10") and the first peak from the fast side of the log-RT histogram ("peak"), as Ratnam's order-statistics filter.
- **ml.** The same windows and selection; the decay rate is Ratnam's ML estimate (the compensated energy's centroid at the window centre; 40 bisection steps, vectorised over all windows). Readings as sdd.
- **adc.** In every band, an event is a fall of at least 10 dB within 150 ms from a level held for 100 ms (and still down 150 to 300 ms later), the held level at least 15 dB above the band's floor and within 50 dB of the peak. The event is anchored at the last frame within 3 dB of the held level, then followed for 1.5 s relative to the held level and cut where the band is re-excited (climbs 6 dB above its running minimum). The median over events at each lag is the averaged decay curve. RT60 = -60 / (slope of the curve from 100 ms until it has fallen 20 dB below its early level, or ends); "early_db" = the curve 80 to 120 ms after the cut (the tail level re the held level: higher = more reverb, a direct-to-reverberant ratio with the sign flipped). At least 20 events are needed at a lag for the curve to exist there.
- Per-group (150 to 700 Hz, 1.2 to 3.6 kHz) readings give two type features: the low/high RT ratio and the early-level tilt.
- Confidence intervals: a block bootstrap over 2 s stretches (sdd, ml) or over events (adc), 200 draws, 16th to 84th percentiles.
- Long signals are analysed in 60 s stretches and pooled (`tests/test_reverb_blind.py::test_chunked_analysis_matches_single`).

Parameters were set on the synthetic bursts (section 3.1) before the candidates were read, with two exceptions made after the first candidate pass and disclosed: the window went from 400 to 250 ms and the half-window knee test was added, because single-bin levels fluctuate too much for a 400 ms line (sdd found 2 to 14 windows per clip), and the event anchor was moved to the real cut (a bug: events were anchored 150 ms early). Nothing was changed after the validation table below was produced.

## 3. Synthetic validation

### 3.1 Noise bursts with a known tail (`reverb_estimator_t231.py synth`; `cache/reverb-estimator-t231/synth.txt`)

Held band-noise bursts, cut, through an exponentially decaying noise tail of known RT60 whose energy sits DRR dB under the direct sound. Two seeds each.

| true RT60 | DRR | sdd p10 | sdd peak | ml p10 | ml peak | **adc RT60** | adc early dB |
|---|---|---|---|---|---|---|---|
| 0.5 | 10 / 20 / 30 | 0.39 / 0.30 / 0.42 | 0.52 / 0.52 / 0.52 | 0.38 / 0.28 / 0.35 | 0.52 / 0.52 / 0.52 | **0.51 / 0.51 / -** | -20.8 / -31.7 / -41.7 |
| 1.0 | 10 / 20 / 30 | 0.76 / 0.82 / 0.83 | 0.92 / 0.92 / 0.92 | 0.76 / 0.82 / 0.83 | 0.92 / 0.92 / 1.03 | **0.97 / 0.96 / 1.07** | -13.0 / -21.9 / -35.6 |
| 2.0 | 10 / 20 / 30 | 0.81 / 1.28 / 1.29 | 1.63 / 1.45 / 1.45 | 0.80 / 1.29 / 1.29 | 1.45 / 1.45 / 1.45 | **1.99 / 1.99 / 2.00** | -13.3 / -23.1 / -33.2 |

(seed 1 shown; seed 2 within 0.07 s.) adc: median relative error 2%, max 7%, unreadable in 2 of 18 (RT 0.5 at DRR 30: the tail is at -42 dB after 100 ms and hits the floor). sdd/ml peak: median 8%, max 27% (biased short at 2 s: a 250 ms window sees only 7.5 dB of a 2 s decay, near the 5 dB minimum). The early level moves 10 dB per 10 dB of DRR, offset by the decay within the first 100 ms. Unit tests: RT 0.5/1/2 recovered within 15% (adc), three decays ordered (sdd, ml), early level monotone in DRR, level invariance exact, note-content invariance (a different seed) within 15%, no reverb gives no tail.

### 3.2 The 189 [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") candidates and the other known material (`validate`; `cache/reverb-estimator-t231/validate.txt`)

Every candidate is the song's pre-reverb render c plus one reverb of known type, Mix and tail length (short 1.05, medium 1.48, long 2.09 s, calibrated by impulse in [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")")). 10 s clips (ds-sample 15 s). Zero new renders.

**Decay length.** Per song: Spearman of the reading against the true tail length over the 27 candidates / (type, Mix) triples with the three decays in the right order / mean absolute error in seconds / readings that exist of 27.

| song | sdd peak | ml peak | **adc RT60** |
|---|---|---|---|
| mother | 0.12 / 2 of 8 / 0.79 / 26 | -0.03 / 0 of 8 / 0.79 / 26 | **0.86 / 9 of 9 / 0.44 / 27** |
| seek-destroy | -0.17 / 0 of 3 / 0.72 / 12 | 0.12 / 0 of 3 / 0.73 / 12 | **- / - / - / 0** |
| teen-spirit | -0.50 / - / 0.42 / 3 | -0.50 / - / 0.42 / 3 | **- / - / - / 0** |
| territorial | -0.11 / 2 of 9 / 0.87 / 27 | -0.09 / 2 of 9 / 0.87 / 27 | **0.89 / 9 of 9 / 0.18 / 27** |
| satisfaction | 0.06 / 1 of 9 / 0.75 / 27 | 0.00 / 1 of 9 / 0.76 / 27 | **0.11 / 2 of 6 / 0.37 / 24** |
| cherub-rock | -0.01 / 0 of 5 / 0.61 / 20 | -0.16 / 0 of 5 / 0.60 / 20 | **0.85 / 7 of 7 / 0.34 / 22** |
| ds-sample | 0.13 / 3 of 9 / 0.84 / 27 | 0.11 / 3 of 9 / 0.86 / 27 | **0.86 / 9 of 9 / 0.24 / 27** |
| all | -0.07 / 8 of 43 / 0.71 | -0.08 / 6 of 43 / 0.72 | **0.71 / 36 of 40 / 0.31** |

The p10 readings are no better (29 and 31 of 43 triples, rho 0.04). So: sdd and ml, the two textbook running-signal estimators, do not read a reverb's decay on distorted guitar, because the guitar's own note decays (0.4 to 1.0 s on the pre-reverb renders, see below) are free decays too and they outnumber the tail's. adc reads the decay on five songs, ordering the three lengths right in 36 of 40 triples with errors of 0.2 to 0.4 s (it reads short: 1.05/1.48/2.09 come out around 0.9/1.2/1.6, the curve being cut where bands are re-excited). On satisfaction it reads but barely orders (2 of 6): that song's pre-reverb render already shows a 1.15 s decay of its own (sustained ringing chords), which the reverb only lengthens a little.

**No reverb.** On the pre-reverb render c, adc finds no decay after 100 ms on six of seven songs (the curve drops to -65 to -80 dB, i.e. the band goes silent); the exception is satisfaction (1.15 s, its own ring). The rebuilt dry likewise (satisfaction 1.36 s). The sdd/ml readings on c and dry are 0.4 to 1.0 s on every song: the dry guitar's own decays.

**Mix.** adc early level (dB re the held level; mean over type and decay):

| song | Mix 0.25 | 0.35 | 0.50 | rho(early, Mix) over 27 | triples rising | c | dry | stem |
|---|---|---|---|---|---|---|---|---|
| mother | -17.3 | -14.5 | -11.0 | 0.94 | 9 of 9 | -19.5 | -16.7 | -22.4 |
| seek-destroy | -17.1 | -14.1 | -11.3 | 0.88 | 5 of 5 | -18.7 | -16.5 | -11.7 |
| teen-spirit | -22.7 | -17.2 | -12.7 | 0.90 | 4 of 4 | -69.8 | -42.8 | - |
| territorial | -23.0 | -16.5 | -11.2 | 0.94 | 9 of 9 | -72.7 | -49.0 | -10.1 |
| satisfaction | -12.3 | -11.8 | -9.7 | 0.92 | 9 of 9 | -12.5 | -13.9 | -10.9 |
| cherub-rock | -19.4 | -14.8 | -11.6 | 0.90 | 7 of 7 | -68.0 | -18.9 | - |
| ds-sample | -15.4 | -13.3 | -10.8 | 0.94 | 9 of 9 | -21.4 | -22.9 | -11.9 |

52 of 52 triples rise with Mix; about 3 dB per step of Mix. The absolute level depends on the song (satisfaction's own ring already fills the bands to -12 dB), so the reading is a within-song comparison, not a Mix knob value. Where the early level exists, Mix is readable on every song, including seek-destroy and teen-spirit (fewer events, but enough at 100 ms).

**Type.** Mean features by type over all 189: low/high RT ratio 0.93 / 0.97 / 0.87 (Room / Plate / Hall; exists for only 54 of 189), early tilt +0.4 / -1.3 / -0.8 dB, early level -16.1 / -14.4 / -13.8 dB, sdd peak 0.69 / 0.81 / 0.84 s. Leave-one-song-out nearest centroid on the 54 items with all four features: **43% right, chance 33%.** With HighCut 3700 Hz and LowCut 150 Hz on every candidate, the tails differ mostly in build-up and density, which these readings do not see. Not resolved.

**Known reverb on the real stem ([T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…") controls).** Room Decay 0.50 (about 0.95 s by the calibration curve) at Mix 0.50 and 0.35 added to the separated stem itself:

| song | plain stem | Mix 0.35 | Mix 0.50 |
|---|---|---|---|
| mother | 0.88 s, -22.4 dB | 0.92 s, -18.6 dB | 0.95 s, -13.3 dB |
| seek-destroy | - , -11.7 dB | - , -11.4 dB | - , -10.9 dB |
| teen-spirit | - (6 events) | - (4) | - (5) |

On Mother the added Room reads 0.92 and 0.95 s against about 0.95 s true, and the early level climbs 4 and 9 dB over the plain stem (the plain stem's own 0.88 s reading, see section 4, is close to the added reverb's length, so the decay reading cannot separate them; the level can). On seek-destroy only the early level exists, and it moves by 0.3 and 0.8 dB, within the bootstrap width: the stem's residue is already at -12 dB and a Mix 0.35 to 0.50 Room adds little above it. On teen-spirit nothing is readable in 10 s.

## 4. The songs: full-length separated stems (`songs`; `cache/reverb-estimator-t231/songs.txt`)

Full guitar stem (htdemucs_6s) and the full mix for comparison; [16th, 84th] percentile bootstrap; events per second in brackets.

| song | stem length | **stem adc RT60 s** | stem early dB | mix adc RT60 s | mix early dB | stem curve at 0.1 / 0.3 / 0.5 s (dB) | mix curve |
|---|---|---|---|---|---|---|---|
| mother | 68 s | **0.70 [0.64, 0.81]** (5.8/s) | -16.5 [-17.6, -15.3] | 3.82 [2.98, 4.50] | -11.9 | -16.6 / -34.0 / - | -12.0 / -15.1 / -17.7 |
| seek-destroy | 415 s | **1.51 [1.37, 1.63]** (2.1/s) | -12.5 [-12.6, -12.3] | 2.44 [2.11, 3.04] | -9.7 | -12.7 / -17.9 / -37.6 | -9.8 / -16.1 / -20.8 |
| teen-spirit | 278 s | 4.35 [2.84, 5.84] (2.9/s) | -11.7 [-11.8, -11.5] | 5.28 [4.26, 7.82] | -11.1 | -12.0 / -15.8 / -17.1 | -11.3 / -14.7 / -16.5 |
| territorial | 143 s | **0.90 [0.82, 1.08]** (3.0/s) | -13.6 [-13.9, -13.3] | 3.32 [2.76, 6.42] | -12.2 | -13.8 / -24.4 / -39.1 | -12.3 / -15.3 / -20.9 |
| satisfaction | 235 s | 4.70 [4.08, 5.41] (4.3/s) | -10.1 [-10.2, -10.1] | 3.07 [2.81, 3.36] | -10.4 | -10.2 / -15.5 / - | -10.5 / -14.5 / - |
| cherub-rock | 300 s | 8.63 [4.46, 15.19] (1.0/s) | -12.0 [-12.3, -11.8] | 6.43 [5.40, 7.70] | -11.7 | -12.2 / -14.7 / - | -11.9 / -14.6 / -16.2 |
| ds-sample | 279 s | 5.37 [3.54, 8.33] (9.0/s) | -12.5 [-12.7, -12.4] | 5.56 [4.53, 7.24] | -11.3 | -12.6 / -23.5 / -20.1 | -11.3 / -15.7 / -16.5 |

How to read it: on five of seven stems the abandoned band falls to -10 to -13 dB at once and then only 1 to 2 dB per 100 ms, which is the same curve the full mix shows. That is not a reverb tail; it is what the separator leaves in a band when the guitar leaves it (bass, drums, vocals, and whatever ambience the record has, at -10 to -13 dB). A readable decay (the curve keeps falling, 20 dB or more within 300 to 500 ms, clearly faster than the mix) exists on **Mother (0.70 s, early -16.5 dB)** and **Territorial (0.90 s, -13.6 dB)**, and partly on seek-destroy (1.51 s, but its curve sits on the -12 dB residue for 300 ms first, so the slope is read from the drop into the song's rests). Whether Mother's and Territorial's 0.7 to 0.9 s is a reverb on the guitar or the recording's room is not something this measurement can say; it is a tail of that length on the stem. The 10 s excerpts agree on Mother (0.88 s) and ds-sample (1.06 s on the excerpt, nothing on the full stem) and disagree on Territorial (1.95 s from 26 events, CI [1.64, 2.58], against 0.90 s from 432 events): the excerpt is too short for a stable reading.

The sdd and ml readings on the stems and mixes are 0.3 to 1.2 s everywhere, with the mix often longer than the stem: they read the music's own decays, as on the candidates.

## 5. Post hoc: does the estimate line up with what the human ranked closest? (`compare`; `cache/reverb-estimator-t231/compare.txt`)

Not pre-registered; the scales were fixed before this step (0.30 in log RT, i.e. a 35% decay difference, counts as much as 5 dB of early level). Distance from a candidate's (RT60, early) to the stem's; Spearman with the human's [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") average ranks over the six candidates; undefined where either side has no reading. The human's repeat consistency was 0 to 1 rank on every song ([T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")")).

| song | reference | rho | estimate's closest | human's closest |
|---|---|---|---|---|
| mother | stem excerpt (0.88 s, -22.4 dB) | 0.65 | hall-m25-medium | hall-m25-medium (tied with t229-r35) |
| mother | stem full (0.70 s, -16.5 dB) | 0.77 | t229-r35 | hall-m25-medium / t229-r35 |
| seek-destroy | - | undefined (no RT reading on any candidate) | | room-m35-short |
| teen-spirit | - | undefined | | room-m35-short |
| territorial | excerpt (1.95 s, -10.1 dB) | 0.24 | t229-p35 | room-m25-long / hall-m35-medium |
| territorial | full (0.90 s, -13.6 dB) | -0.36 | plate-m50-short | |
| satisfaction | excerpt: no RT reading; full (4.70 s = residue) | -0.44 | t229-p35 | plate-m25-long / t229-r35 |
| ds-sample | excerpt (1.06 s, -11.9 dB) | 0.80 | hall-m50-medium | hall-m50-medium (tied with two others) |
| ds-sample | full (5.37 s = residue) | 0.93 | hall-m35-long | |
| cherub-rock | excluded by the human | | | |

Mean rho: 0.56 over three songs with the excerpt reference (all positive; the human's closest candidate is the estimate's closest on mother and ds-sample), 0.22 over four with the full-stem reference. [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")")'s pre-registered signals gave 0.05, 0.04, -0.09 and -0.40. What the human ranked closest on the readable songs: Mother, the lightest candidates (Mix 0.25 Hall, Room 0.35 at 0.8 s), and the estimate says the stem has a short tail at a low level; ds-sample, the Mix 0.35 to 0.50 candidates at 1.5 to 2.1 s, and the estimate says 1.06 s at a high level (-11.9 dB); Territorial, long decays at low Mix, which the excerpt reading (1.95 s) matches better than the full-stem reading (0.90 s). The human's two strongest dislikes with a reading (mother hall-m50-short, rank 7, early -9.4 dB; ds-sample room-m25-short, rank 6.5, 0.54 s) are also the two with the largest distance on their song. Six candidates per song, three songs, chosen after the fact: a lead for a pre-registered test, not evidence.

## 6. Limits, in plain words

1. The estimator needs the guitar to leave a frequency band alone for about 250 ms with the band still 15 dB above its floor. Fast dense riffs (seek-destroy, teen-spirit) do not do that in 10 s; over a whole song the breaks and section ends do, but then what is exposed is the separator's residue.
2. On a separated stem, the floor under an abandoned band is 10 to 13 dB below the guitar, and it is the backing track, not silence. A reverb tail quieter than that is invisible; one louder than that is read as "tail level" together with the residue. The Mix reading on a stem is therefore an upper bound on how much reverb could be there, not a measurement of it, unless the decay is also readable.
3. A guitar's own sustain is a decay too. On satisfaction the dry render already reads 1.15 s; the reverb's decay cannot be separated from the ring-out there, and the "no reverb" reference is not 0 s but whatever the dry reads.
4. Type is not read. All candidates share the same low and high cut, which removes most of what tells a Plate from a Hall in a long-term sense; what is left (build-up, early density) would need an onset-side measurement and a cleaner signal.
5. The textbook estimators (sdd, ml) are out for this material; they are kept in the module for the record and the tests, not for use.
6. 10 s is too short for a stable decay reading (26 events on territorial gave 1.95 s; 432 gave 0.90 s). Use the whole song.

## 7. Files, ledger, deviations

- Code: `src/tonematch/metrics/reverb_blind.py`; tests `tests/test_reverb_blind.py` (10, including RT60 within 15% on synthetic decaying noise); driver `scripts/reverb_estimator_t231.py` (`synth | validate | songs | compare | ledger`); outputs `cache/reverb-estimator-t231/{synth,validate,songs,compare}.{json,txt}`.
- Ledger (`benchmark / - / reverb-estimator-v1`, 2026-10-25T23:00Z): `synth_adc_rt60_within15pct` 1.00 (16 of 18 readable), `synth_sdd_peak_within15pct` 0.67; `decay_rank_acc_adc_rt60` 0.90 (36 of 40 triples), `decay_rank_acc_sdd_peak` 0.19, `decay_rank_acc_ml_peak` 0.14; `mix_monotone_adc_early` 1.00 (52 of 52); `stem_full_adc_rt60_s_<song>` x 7; `posthoc_mean_rho_excerpt` 0.56, `posthoc_mean_rho_full` 0.22 (15 rows).
- No renders, no Native host, no Stomp. `proc_check` clean before and after. No contract, loss, weight or default changed; `data/approved-methods.json` untouched. The human's answers and the [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") key were read only for section 5, after [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")")'s scoring was complete.
- Not done: an onset-side (build-up) feature for type; a pre-registered test of the post hoc agreement; an estimate on a cleaner stem (a different separator, or the user's own recordings, where the residue floor is lower).

## 8. For the human

The reverb on a song's guitar can be measured from the stem alone when two things hold: the playing leaves bands alone now and then, and the separator's residue is below the tail. On your songs the first holds over a full song and the second mostly does not: five of seven stems show the same -12 dB floor as the full mix. Mother and Territorial show a real tail (0.7 and 0.9 s). If you want this to become a starting value for `search`, the next step I would propose is a pre-registered repeat of the [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") listening test on the songs where a reading exists, with the estimate's closest candidate named in advance.
