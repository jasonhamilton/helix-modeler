# Distortion measurement, step 3: every drive model ([T174](../../board/TASKS.md#t174 "Distortion measurement step 3"))

Owner: `tone-metrics`. Run 2026-10-18 on Helix Native 3.82. Plan: [distortion-identification.md](distortion-identification.md) section 8, step 3. Builds on the pilot, [distortion-pilot-step2.md](distortion-pilot-step2.md). Approved by the human ("go ahead", [DECISIONS](../DECISIONS.md) 2026-10-18) with these choices:

- 6 s sweeps;
- the tone staircase starts at about -90 dBFS (here -91 to -1 dBFS in 5 dB steps);
- Kinky Boost measured as it is;
- the "not a simple three-stage circuit" flag dropped;
- a quick check first that Scream 808's Tone reaches Native;
- switches the templates do not map stay at their defaults;
- all 60 drive models of `match_block.category_pool('drive')`, 12 settings each.

**Nothing here is adopted.** No class list, score or bar comes from this note. The numbers are measurements. Only small fingerprints were stored, no audio. The test signal is synthetic, and no song audio was used.

## In one paragraph

Every one of the 60 drive models was measured at 12 settings: 720 fingerprints, none failed, 5.9 MB in all. Repeat renders are bit-identical.

- **Telling models apart.** Hide one setting of one model, then ask which model the nearest remaining settings belong to. The model is named correctly 97% of the time from the stored curves. With only the twelve numbers on each model's card it is right 82% of the time. On a like-for-like test with the old coarse-v1 fingerprints, the new fingerprints are better at every level of detail: 0.52 / 0.74 / 0.81 right first time, against 0.34 / 0.59 / 0.75 before.
- **Grouping by circuit.** The fingerprints still do not group models by circuit type. Clusters agree with the circuit labels only weakly: an agreement score of 0.03 to 0.21 on a scale where 1 is perfect and random grouping scores about 0.04. That is about where coarse-v1 was.
- **The Tone check.** Scream 808's Tone does reach Native, exactly. Its effect is packed at the two ends of the knob. Between 0.2 and 0.8 it changes the sound's shape by about 1 dB. From 0.8 to 1.0 it lifts 10 kHz by 9 dB.

## Words used

- **dBFS:** level relative to digital full scale. 0 dBFS is the loudest possible sample. Peak levels are used throughout.
- **Knee:** the input level at which the output of a 1 kHz tone first falls 1 dB short of a straight line (the 1 dB compression point). Lower means the pedal compresses earlier.
- **Harmonic onset:** the input level at which the 2nd or 3rd harmonic of a 1 kHz tone first rises to within 60 dB of the tone itself. On Native most drives are exactly linear below this level, and their harmonics switch on within one 5 dB step (pilot surprise 2).
- **Even share:** how much of the clipping curve's harmonic content is in the 2nd, 4th, ... harmonics. Below about -100 dB means numerically zero: a perfectly symmetric clipper. -40 to -60 dB means slightly lopsided. Above -30 dB means clearly asymmetric (fuzz-like).
- **Pre high-pass / post low-pass:** the bass cut before the clipping stage and the treble cut after it. Each comes from the pre/post split approved in step 1, read as the corner of a first-order filter fitted to it. A corner is shown only when the fit holds: within 3 dB root-mean-square and inside 63 Hz to 16 kHz. Otherwise the table shows "-".
- **Mid feature:** a hump in the quiet-input (small-signal) response, or a scoop in the response at 0 dBFS input, whichever is larger. It is shown with its size and frequency, or as "flat" if neither reaches 3 dB.
- **Gain (g0):** the small-signal gain at 1 kHz. Level knobs were held at the library value, so this includes each model's own output level.
- **Top-1 / top-5:** the share of hidden settings for which the true model is ranked first, or among the first five, of 60. Chance is 0.017 and 0.083.
- **ARI (adjusted Rand index):** agreement between two ways of grouping the same models. 1 means identical, 0 means no better than chance.

## 1. Does Scream 808's Tone reach Native? Yes

The pilot found that Scream 808's Tone moved the response by at most 2.2 dB. The question was whether the knob reaches Native at all.

**Mapping and readback.**

- The single-block template maps Tone to `cc_6` over 0 to 1. Native-engine's CC verification lists it as verified, and `cc_live.json` marks it as moving the render.
- I then used the [T103](../../board/TASKS.md#t103 "Native verification suite scripts/native_verify.py") truth route. Each setting was rendered two ways. The first is the normal template with the knob sent by MIDI CC. The second is a file with the same knob values written in and no controller assignments, dropped into Native and rendered from its own state.
- 9 settings: Gain 0.2 / 0.5 / 0.85 with Tone 0 and 1, plus Tone 0.2 / 0.5 / 0.8 at Gain 0.5.
- **All nine pairs null at -329 to -334 dB.** That is bit-identical, so the Tone value lands exactly.
- On the way, a first attempt nulled at exactly -64 dB on every setting. The cause was not Tone. The library Level of 0.67 is not on the 128-step CC grid, so the CC route plays it at step 85 (0.669) while the file plays 0.67. With Level put on its grid, the routes agree bit for bit. This is known engine behaviour ([T103](../../board/TASKS.md#t103 "Native verification suite scripts/native_verify.py") already audits on the grid). It does not affect the fingerprints, because every render goes through the same CC route.

**What Tone does** (Gain 0.5, small-signal response, file-truth route; the CC route is identical). The table gives the 1 kHz level, then the shape relative to 1 kHz:

| Tone | 1 kHz level | 100 Hz | 300 Hz | 3 kHz | 6 kHz | 10 kHz |
|---|---|---|---|---|---|---|
| 0 | 10.5 dB | -6.8 | 0.0 | -5.9 | -11.2 | -16.6 |
| 0.2 | 14.7 dB | -11.1 | -3.5 | -5.8 | -11.9 | -17.6 |
| 0.5 | 15.4 dB | -11.5 | -3.3 | -6.2 | -12.4 | -18.1 |
| 0.8 | 16.6 dB | -12.4 | -3.5 | -6.3 | -12.5 | -18.2 |
| 1 | 20.4 dB | -16.4 | -7.4 | -0.4 | -4.6 | -9.6 |

- **From 0.2 to 0.8, Tone hardly changes the shape.** It changes the shape by 1.3 dB at most and raises the level by 1.9 dB. That is what the pilot saw.
- **The ends do the work.** From 0 to 0.2 the bass falls 4 dB and the level rises 4 dB. From 0.8 to 1 the 10 kHz band rises 8.6 dB and 3 kHz rises 5.9 dB. Tone 0 against Tone 1 differs by up to 16.9 dB (9.5 dB in shape alone).
- **The same at every Gain.** The Tone 0 against Tone 1 difference is the same at all three Gain values, and in the quiet and the hot (-12 dBFS) sweep, to within 0.001 dB. So Tone acts after the clipping, as in the circuit.
- **Other Tube Screamer types, for scale** (CC route, Drive 0.5, Tone 0 against 1): DM4 Screamer 15.7 dB (8.3 dB shape), Stupor OD 18.0 dB (12.4 dB shape).
- **Conclusion.** Nothing to flag to `native-engine`. The step-3 tone grid of 0.2 / 0.8 sits inside Scream 808's flat zone, so its cards show Tone moving almost nothing. Whether other models' tone knobs are also loaded at the ends was not measured (see decision 4).
- Files: `reports/local/distortion-id/step3/tonecheck.json` and `tonecheck_scream808.svg`.

## 2. What was measured

**Probe** (`dfp-v1`, 115.2 s, 48 kHz). The pilot probe with one change: the 1 kHz and 220 Hz tone staircases now run from -91 dBFS to -1 dBFS in 5 dB steps (19 steps, 0.3 s each; -6 dBFS is included). The rest is unchanged: 13 sweeps of 6 s from -78 dBFS, then every 6 dB from -66 to 0; two-tone pairs; bursts. The odd-harmonic residual behind the dropped flag is no longer stored.

**Settings: 12 per model.**

- **Two-knob models (52):**
  - the grid: Drive 0.2 / 0.5 / 0.85 times tone 0.2 / 0.8 (MIDI steps 25 / 64 / 108 and 25 / 102), 6 settings;
  - the tone-centre line: Drive 0.2 / 0.5 / 0.85 at tone 0.5, 3 settings;
  - the Drive ends: Drive 0.05 and 1.0 at tone 0.5, 2 settings;
  - one off-grid point: Drive 0.65, tone 0.35.
  - All values are Native's own CC steps, so each is exactly what Native plays.
  - The six non-grid settings are the "level-free variant set". Only the drive and tone knobs move; no Level knob moves in any setting.
- **One-knob models (8):** Arbitrator Fuzz, Industrial Fuzz, Kinky Boost, Pocket Fuzz, Top Secret OD, Tycoctavia Fuzz, Vital Boost and Bronze Master. Each gets 12 Drive points: steps 0, 12, 25, 38, 51, 64, 76, 89, 102, 108, 118 and 127.
- **Everything else stays fixed:**
  - every Level, Output, Volume, Master and Mix knob, and every other knob, at the library default;
  - every switch the template does not map at the catalog default;
  - per model, `scripts/distortion_step3.py plan` lists exactly which knobs these are.
- **Which knob is "drive" and which is "tone"** comes from [distortion-circuits.md](distortion-circuits.md) 1.2 and 1.3. It is the second column of the card table in section 6. The choices that needed judgement:
  - **Regal Bass DI has no gain knob.** Its Volume was measured in the drive slot. The measurement says Volume acts as an output level: it raises the gain 26 dB and moves neither the knee nor the onset.
  - **Killer Z:** "Dist" is the drive and "Contour" the tone. Its "Gain" knob, probably a level, stayed at the default.
  - **Tone Sovereign:** channel 1 only (Gain 1 / Tone 1); channel 2 is at its defaults.
  - **Clawthorn Drive:** the overdrive channel (ODGain / ODTone); the fuzz switches are at their defaults.
  - **Thrifter Fuzz:** tone = Notch Gain, at the default notch frequency.
  - **Horizon Drive:** tone = Bright.
  - **Prize Drive:** tone = Spectrum.
  - **Alpaca Rouge:** tone = HiCut.
  - **Megaphone:** drive = Grit.
  - **The legacy DM4 models with a three-band stack** use Treble as the tone, with Bass and Mid at default. The DM4 Screamer uses its Tone and the DM4 Classic Distortion its Filter.
  - **Bronze Master's "mixture"** is a blend, not a tone, so the model was measured as a one-knob model.

**Renders, time and load.**

- **About 770 Native renders, 0 failed.** That is the 720 settings, 2 repeats and an 8-setting shakedown, plus up to 48 renders re-done after I stopped the first run (see the load note below). The tone check took 2 drop sessions and about 50 renders.
- **Time.** The main run took 30 minutes of wall-clock time. Of that, 15 minutes was rendering. The host scaler granted 1 host for most of the first half (other agents were rendering) and up to 5 near the end.
- **Analysis.** Each fingerprint took 6.8 s of analysis on average (9.0 s at most) on one core. It ran in 3 worker processes, overlapped with the rendering, so the analysis added almost no wall-clock time.
- **Load.** The 1-minute load average averaged 7.7 over the run.
  - **It went above 10 once:** 12.05, 11.3 and 10.7 in the first minute after I restarted with 3 workers. Another agent's 4-process run was also going.
  - I had started with 4 workers. When the load reached 9.3 with the other run active, I stopped my run with an interrupt (no orphans) and resumed with 3 workers. After that first minute it stayed at or below 9.97.
  - The run also pauses its rendering whenever the load is above 9.
- **`proc_check`** found no orphans before or after every run.

**Storage.**

- `cache/distortion-fp/dfp-v1/<model>/<setting>.npz`: 6.5 KB each on average, 5.9 MB in all, with `index.json` beside them.
- No audio was kept.
- The tone check's drop files hold the user's catalog values, so they were deleted after the drop.

## 3. Placeholder checks for step 3 (from the plan, reported as measured)

| Check (placeholder, not a bar) | Measured |
|---|---|
| At least 95% valid fingerprints | 720 of 720 (100%) |
| Between-model spread larger than the repeat spread | The repeat spread is zero: both repeats (Scream 808 centre, Arbitrator Fuzz 0.85) are bit-identical renders with byte-identical fingerprints. Any difference between models is above it. |
| Knob maps monotone where expected: gain rises with Drive (more than 0.5 dB per step) | 105 of 112 Drive series. Exceptions: Megaphone, Xenomorph Fuzz and ZeroAmp Bass DI at both tone settings, and Bronze Master. All four are bass or special-purpose models whose drive knob is not a simple gain. |
| The knee does not rise with Drive (within 1 dB) | 104 of 105 series that have a knee. The exception is Tycoctavia Fuzz, whose knee sits at the bottom of the staircase. |
| The harmonic onset does not rise with Drive | 111 of 112. The exception is Kinky Boost (pilot: its moving DC offset blurs the onset). |

## 4. Telling models apart, leave-one-setting-out

**The test.** Hide one of the 720 settings. Each model's distance to it is the distance to that model's nearest remaining setting. The hidden model keeps its other 11 settings in the gallery. Then rank the 60 models. The brackets are 95% intervals from resampling models.

The features come in three levels of detail, each with and without the features that carry the output level. Helix sets each model's output level its own way, so level alone could tell models apart.

- **Card:** the 12 numbers on the card: knee, onset, even share, pre and post corners, mid feature and their states, plus gain.
- **Scalars:** every stored number, 133 of them.
- **Curves:** the stored response tables, harmonic levels, staircases, static curve and intermodulation, 1,747 values.

| Features | Top-1, all 720 | Top-5 | Top-1 on the hardest settings |
|---|---|---|---|
| Chance | 0.017 | 0.083 | |
| Card (12) | **0.82** [0.76, 0.87] | 0.96 | Drive ends 0.76, off-grid 0.73 |
| Card without level (11) | 0.81 [0.75, 0.86] | 0.96 | ends 0.76, off-grid 0.67 |
| Scalars (133) | **0.96** [0.94, 0.97] | 0.99 | ends 0.82 |
| Scalars without level (95) | 0.93 [0.90, 0.95] | 0.99 | ends 0.76 |
| Curves (1,747) | **0.97** [0.96, 0.98] | 0.99 | ends 0.88 |
| Curves without level | 0.97 [0.95, 0.98] | 0.99 | ends 0.84 |

**Why these numbers are high, and the fair comparison.**

- With 12 settings per model, every hidden setting has a close neighbour. The centre-line settings sit between two grid settings, and they are identified 100% of the time from the curves.
- The coarse-v1 test held out corners of a much sparser grid. To compare like with like, I repeated the test using only the 2 x 2 corners plus the centre of each model, the same layout as coarse-v1; one-knob models used Drive 0.2 / 0.5 / 0.85. That gives 284 hidden settings, against 288 in coarse-v1.

| Features | coarse-v1 top-1 (top-5) | dfp-v1, same layout, top-1 (top-5) |
|---|---|---|
| A few named numbers (coarse-v1: 6 scalars; dfp-v1: the card) | 0.34 [0.24, 0.44] (0.56) | **0.52** [0.42, 0.62] (0.76) |
| Every stored number | 0.59 [0.50, 0.68] (0.82) | **0.74** [0.65, 0.82] (0.96) |
| Stored curves | 0.75 [0.67, 0.83] (0.96) | **0.81** [0.73, 0.87] (0.98) |
| Without level: card / scalars / curves | 0.30 / 0.59 / 0.74 | 0.52 / 0.75 / 0.81 |

- **The new fingerprints are better at every level.** The gain is clear for the named numbers and for all scalars, where the intervals barely overlap. For the curves it is small, and the intervals overlap.
- **The card's 12 numbers do much better than coarse-v1's 6 scalars:** 0.52 against 0.34. The knee and onset are now measured below where high-gain models clip.
- **Not because of loudness.** Dropping the level features changes almost nothing.
- **Limits.**
  - Every setting used the same synthetic probe, with the dry signal known. This is the friendliest case.
  - A hidden setting at a Drive end is the hardest: 0.76 to 0.88. The search would see such settings as extrapolations.
  - Nothing here says how the fingerprint would do on a real DI or a separated stem. That is step 4.

**When the first answer is wrong.** On the card features there are 129 misses. In those, the model chosen is in the true model's confusable group 26% of the time (chance 4.5%), in the same provisional class 43% of the time (chance 20%), and in the same family 60% of the time (chance 29%). So the misses are not random. They land near the right circuit.

**Same pedal, two entries.** These are the median distances between models on the curves without level, as a percentile of all 1,770 model pairs. Lower means closer. The pairs, from closest to farthest:

- Scream 808 and DM4 Screamer: 0.06th percentile, the closest pair in the set (coarse-v1: 12th);
- Deez One Vintage and Deez One Mod: 0.3rd;
- Vermin and Classic Dist: 2.7th;
- Valve Driver and Tube Drive: 4th;
- Triangle Fuzz and Fuzz Pi: 10th;
- Heavy Dist and Killer Z: 11th;
- Arbitrator and Facial Fuzz: 40th;
- Vermin and Ratatouille: 66th;
- Tycoctavia and Octave Fuzz: 86th.

The last three are as far apart as unrelated models. They are worth a listen before anyone treats them as twins.

## 5. Grouping by circuit (clusters against the labels)

Each model's 12 settings were averaged. The averages were clustered (Ward) and compared with three label sets from [distortion-circuits.md](distortion-circuits.md):

- its 17 confusable groups (each "O" singleton is its own label);
- its family: boost, overdrive, distortion, fuzz or other;
- the provisional six-way circuit class from step 0. This is my own reading and **not an approved class list**.

"Random" is the 95th percentile of 1,000 random groupings with the same cluster sizes.

| Clustering | vs 17 groups (random) | vs family (random) | vs 6 classes (random) |
|---|---|---|---|
| Card features, no level | 0.18 (0.04) | 0.03 (0.05) | 0.06 (0.04) |
| Curves, no level, blocks weighted equally | 0.14 (0.04) | **0.21** (0.05) | 0.03 (0.04) |
| Curves with level, blocks weighted equally | **0.19** (0.04) | 0.12 (0.04) | 0.14 (0.04) |
| coarse-v1 (step 0, for comparison) | 0.05 to 0.16 | 0.06 to 0.10 | 0.09 to 0.19 |

- **The agreement is above random, but small, and no better than coarse-v1.** The fingerprints separate models well (section 4). But the directions they separate them along are not the circuit classes.
- **What the six clusters sort by** (curves, with level):
  - **Models that are nearly silent or not tone-like at a quiet input** (4): Octave Fuzz, Tycoctavia, Wringer Fuzz and Bronze Master. A full-wave octave puts little of the input tone through, and the Wringer is gated.
  - **Xenomorph Fuzz alone:** a triggered oscillator, silent below its trigger level (gain about -220 dB at -78 dBFS).
  - **The bass DI and lo-fi models** (3): Megaphone, Regal and ZeroAmp. They distort only near full scale (onset -11 dBFS).
  - **The heavy scooped distortions** (4): Heavy Dist, Killer Z, L6 Distortion and Ratatouille.
  - **Two large mixed clusters** (21 and 27). One holds most soft overdrives and Tube Screamer types together with several legacy DM4 drives. The other holds the Big Muff and Fuzz Face types with the DS-1, Rangemaster and boosts.
- **Measured against the provisional class reading,** some labels look wrong for Native:
  - Compulsive Drive (labelled asymmetric MOSFET) measures perfectly symmetric (even share none).
  - Dhyana Drive and Teemah, labelled soft and symmetric, measure clearly asymmetric (-27 and -18 dB). Stupor OD, labelled asymmetric, is (-25 dB).
  - The asymmetry itself is common: 47 of 60 models have an even share above -60 dB at their centre setting.

  This is one more reason not to adopt a class list from the circuit notes.

## 6. Fingerprint cards

One row per model, at its centre setting (Drive 0.5, tone 0.5; one-knob models Drive 0.5). The last two columns say how the knobs move the card. "Drive 0.2 to 0.85" is the change from Drive 0.2 to 0.85, averaged over the two tone settings. "Tone 0.2 to 0.8" is the change from tone 0.2 to 0.8, averaged over the three Drive settings. Only changes of at least 3 dB (6 dB for even share) or 0.3 octave are listed; "little" means none reached that.

- **Knee and onset:** "< -91" means already compressing, or already producing harmonics, at the first step; "none" or "never" means not reached by -1 dBFS.
- **Even share:** "none" means below -60 dB.
- **Corners:** "-" means the first-order fit did not hold.

The letter after each name is its confusable group from distortion-circuits.md.

| Model (group) | Drive knob / tone knob | Knee | Onset | Even share | Pre high-pass | Post low-pass | Mid feature | Drive 0.2 to 0.85 moves | Tone 0.2 to 0.8 moves |
|---|---|---|---|---|---|---|---|---|---|
| DM4 Screamer (A) | Drive / Tone | -47 | -46 | -50 | 796 | 689 | hump 11 dB at 794 | knee -15 dB, onset -15 dB, gain +15 dB, pre -0.7 oct | gain +6 dB, post +1.4 oct, mid +3 dB |
| DhyanaDrive (A) | Gain / Tone | -51 | -56 | -27 | 154 | 1.5 k | hump 8 dB at 561 | knee -15 dB, onset -15 dB, gain +17 dB, post +0.4 oct | post +1.8 oct |
| HorizonDrive (A) | Drive / Bright | -70 | -76 | -38 | - | 1.4 k | flat | knee -57 dB, onset -15 dB, gain +15 dB | gain +13 dB, post +1.6 oct, mid +5 dB |
| Pillars (A) | Gain / Tone | -40 | -41 | none | 399 | 3.6 k | hump 10 dB at 1.3 k | knee -20 dB, onset -20 dB, gain +19 dB, post +0.4 oct, mid +4 dB | gain +10 dB, post +0.3 oct, mid +5 dB |
| Scream808 (A) | Gain / Tone | -46 | -46 | none | 564 | 730 | hump 12 dB at 794 | knee -15 dB, onset -15 dB, gain +14 dB, pre -1.0 oct | little |
| StuporOD (A) | Drive / Tone | -69 | -86 | -25 | 919 | 919 | hump 15 dB at 1.0 k | knee -14 dB, onset -15 dB, gain +14 dB, pre -0.6 oct | gain +10 dB, post +1.0 oct, mid +8 dB |
| HeirApparent (B) | Gain / Tone | -39 | -41 | none | 919 | 615 | hump 9 dB at 891 | knee -15 dB, onset -15 dB, gain +15 dB, pre +0.3 oct, mid +3 dB | gain +6 dB, post +1.7 oct, mid +5 dB |
| Minotaur (B) | Gain / Tone | -44 | -51 | none | - | 1.3 k | hump 10 dB at 1.0 k | knee -13 dB, onset -12 dB, gain +11 dB, mid +4 dB | gain +8 dB, post +2.8 oct, mid +8 dB |
| PrizeDrive (B) | Drive / Spectrum | -50 | -51 | none | - | 670 | hump 10 dB at 891 | knee -29 dB, onset -35 dB, gain +25 dB | gain +5 dB, post +1.3 oct |
| Teemah (B) | Gain / Treble | -44 | -46 | -18 | 819 | 1.2 k | hump 10 dB at 1.4 k | knee -28 dB, onset -30 dB, gain +25 dB, pre -2.8 oct, mid +7 dB | gain -6 dB, post -3.7 oct, mid -5 dB |
| ToneSovereign (B) | Gain 1 / Tone 1 | -39 | -41 | none | - | 597 | hump 13 dB at 794 | knee -19 dB, onset -20 dB, gain +19 dB, mid +8 dB | knee -7 dB, onset -8 dB, gain +6 dB, post +0.5 oct, mid +4 dB |
| DM4 Overdrive (C) | Drive / Treble | -44 | -46 | -41 | - | 8.9 k | flat | knee -31 dB, onset -30 dB, gain +32 dB, mid +16 dB | gain +3 dB |
| KWB (C) | Gain / Treble | -35 | -41 | -22 | - | - | flat | knee -26 dB, onset -25 dB, gain +26 dB, mid +6 dB | gain +13 dB |
| TopSecretOD (C) | Gain / (none) | -45 | -46 | none | - | - | flat | knee -25 dB, onset -25 dB, gain +25 dB, mid +7 dB | (no tone knob) |
| DM4 ClassicDistortion (D) | Drive / Filter | -61 | -61 | -43 | - | 3.1 k | hump 20 dB at 1.3 k | knee -42 dB, onset -45 dB, gain +42 dB | gain -4 dB, post -4.1 oct |
| RatatouilleDist (D) | Gain / Filter | -60 | -61 | -49 | - | 3.8 k | hump 13 dB at 1.8 k | knee -38 dB, onset -38 dB, gain +38 dB, mid -20 dB | post -2.9 oct, mid +5 dB |
| VerminDist (D) | Gain / Filter | -60 | -61 | none | - | 2.5 k | hump 18 dB at 1.6 k | knee -35 dB, onset -35 dB, gain +35 dB, mid +8 dB | gain -4 dB, post -3.6 oct, mid -4 dB |
| VitalDist (D) | Gain / Filter | -56 | < -91 | -7 | - | 5.5 k | hump 17 dB at 1.6 k | knee -20 dB, gain +28 dB, post +1.1 oct, mid +5 dB | knee +3 dB, gain -4 dB, post -3.5 oct, mid -4 dB |
| DeezOneMod (E) | Drive / Tone | -46 | -46 | -22 | 291 | - | scoop 5 dB at 794 | knee -19 dB, onset -20 dB, gain +18 dB, even -16 dB, mid +6 dB | gain +7 dB, mid -5 dB |
| DeezOneVintage (E) | Drive / Tone | -54 | -56 | -29 | 326 | - | scoop 4 dB at 630 | knee -19 dB, onset -20 dB, gain +19 dB, even +12 dB, pre -0.4 oct, mid +6 dB | gain +6 dB, mid -5 dB |
| HedgehogD9 (E) | Gain / Tone | -57 | -56 | -18 | - | - | hump 8 dB at 2.2 k | knee -10 dB, onset -10 dB, gain +10 dB, mid +3 dB | gain +6 dB |
| DM4 HeavyDistortion (F) | Drive / Treble | -69 | -71 | -46 | - | - | scoop 12 dB at 794 | knee -26 dB, onset -25 dB, gain +25 dB | gain +6 dB, mid -19 dB |
| LegendaryDrive (F) | Drive / Treble | -63 | -61 | -9 | - | 4.2 k | hump 10 dB at 1.8 k | knee -28 dB, onset -30 dB, gain +28 dB | post +1.5 oct, mid +3 dB |
| SwedishChainsaw (F) | Drive / Treble | -67 | -71 | -20 | - | - | hump 8 dB at 794 | knee -10 dB, onset -10 dB, gain +9 dB | gain +24 dB, mid +30 dB |
| KillerZ (F) | Dist / Contour | -77 | < -91 | none | - | 12.3 k | hump 15 dB at 891 | knee -24 dB, onset -5 dB, gain +24 dB, post +0.4 oct | onset -3 dB, mid -34 dB |
| DM4 FuzzPi (G) | Drive / Treble | -53 | -56 | -44 | - | - | scoop 6 dB at 630 | knee -35 dB, onset -35 dB, gain +35 dB, mid +12 dB | little |
| BallisticFuzz (G) | Sustain / Tone | -75 | -76 | -47 | - | - | scoop 3 dB at 561 | knee -21 dB, onset -20 dB, gain +18 dB, even -8 dB | gain +5 dB, mid +11 dB |
| DarkDoveFuzz (G) | Sustain / Tone | -64 | -66 | -37 | - | 8.4 k | flat | knee -17 dB, onset -20 dB, gain +17 dB, mid +5 dB | gain -7 dB |
| PocketFuzz (G) | Drive / (none) | -55 | -56 | -41 | - | 15.0 k | hump 11 dB at 445 | knee -16 dB, onset -15 dB, gain +17 dB | (no tone knob) |
| RamsHead (G) | Sustain / Tone | -72 | -76 | -40 | - | - | scoop 6 dB at 891 | knee -14 dB, onset -10 dB, gain +12 dB, mid -6 dB | little |
| TriangleFuzz (G) | Sustain / Tone | -69 | -86 | -50 | - | - | scoop 3 dB at 891 | knee -14 dB, onset -12 dB, gain +14 dB, mid +4 dB | little |
| DM4 FacialFuzz (H) | Drive / Treble | -39 | -41 | -26 | - | 1.2 k | flat | knee -19 dB, onset -18 dB, gain +19 dB | gain +4 dB |
| ArbitratorFuzz (H) | Fuzz / (none) | -60 | -61 | -12 | - | - | flat | knee -10 dB, onset -10 dB, gain +10 dB, even -9 dB | (no tone knob) |
| DM4 BuzzSaw (I) | Drive / Treble | -38 | -41 | -37 | 97 | - | hump 4 dB at 2.2 k | knee -5 dB, onset -5 dB, gain +4 dB | mid +8 dB |
| DM4 JumboFuzz (I) | Drive / Treble | -40 | -46 | -29 | - | - | hump 13 dB at 561 | knee -22 dB, onset -20 dB, gain +22 dB | gain +3 dB |
| WringerFuzz (I) | Gain / Treble | < -91 | < -91 | -0 | - | 1.7 k | flat | gain +15 dB | gain +5 dB |
| DM4 OctaveFuzz (J) | Drive / Treble | none | < -91 | -32 | - | 8.9 k | flat | gain +7 dB, post +0.8 oct | gain +6 dB |
| DM4 SubOctFuzz (J) | Drive / Treble | -41 | -41 | -49 | - | - | flat | knee -38 dB, onset -35 dB, gain +38 dB | gain +10 dB |
| TycoctaviaFuzz (J) | Fuzz / (none) | -90 | < -91 | -0 | - | - | flat | gain +18 dB, mid -7 dB | (no tone knob) |
| DerangedMaster (K) | Drive / Treble | -39 | -41 | -11 | 974 | 5.3 k | hump 9 dB at 2.0 k | gain +36 dB, post +0.5 oct | mid -5 dB |
| KinkyBoost (K) | Drive / (none) | -22 | < -91 | -9 | - | 10.0 k | flat | knee -9 dB, gain +10 dB | (no tone knob) |
| VitalBoost (K) | Boost / (none) | -22 | -31 | -27 | - | - | flat | gain +28 dB | (no tone knob) |
| AmpegScramblerOD (L) | Drive / Treble | -25 | -36 | -13 | - | - | scoop 4 dB at 2.5 k | knee -15 dB, onset -15 dB, gain +14 dB, even +26 dB | gain +10 dB, mid +10 dB |
| Obsidian7000 (L) | Drive / Treble | -45 | -61 | -10 | - | - | scoop 8 dB at 500 | knee -19 dB, onset -20 dB, gain +15 dB | gain +10 dB, mid -10 dB |
| RegalBassDI (L) | Volume / Treble | none | -11 | -56 | - | - | scoop 4 dB at 794 | gain +26 dB | mid -6 dB |
| ZeroAmpBassDI (L) | Drive / Treble | none | -11 | -33 | - | - | flat | onset -20 dB, gain +7 dB, even +90 dB | gain +11 dB, mid +5 dB |
| Line6BronzeMaster (L) | Drive / (none) | -40 | < -91 | -0 | - | - | scoop 16 dB at 630 | knee -27 dB, mid -4 dB | (no tone knob) |
| DM4 ColorDrive (M) | Drive / Treble | -28 | -31 | -25 | - | - | hump 7 dB at 707 | knee -25 dB, onset -25 dB, gain +26 dB, mid +12 dB | gain +12 dB, mid +9 dB |
| DM4 Line6Drive (M) | Drive / Treble | -41 | -46 | -31 | - | - | hump 9 dB at 561 | knee -35 dB, onset -30 dB, gain +34 dB | gain +6 dB |
| DM4 TubeDrive (N) | Drive / Treble | -43 | -46 | -32 | - | 1.0 k | hump 11 dB at 891 | knee -36 dB, onset -35 dB, gain +36 dB | post +4.1 oct, mid +5 dB |
| ValveDriver (N) | Gain / Treble | -53 | -51 | -24 | 774 | 7.3 k | flat | knee -38 dB, onset -40 dB, gain +38 dB, pre -2.1 oct, mid +4 dB | mid -9 dB |
| IndustrialFuzz (O:Industrial) | Drive / (none) | < -91 | < -91 | -7 | 411 | 15.9 k | flat | gain +4 dB, pre -1.7 oct | (no tone knob) |
| DM4 JetFuzz (O:JetFuzz) | Drive / Tone | < -91 | -66 | -41 | - | 399 | hump 6 dB at 707 | onset -20 dB, gain +19 dB, even -14 dB | gain +13 dB, post -2.2 oct |
| DM4 Line6Distortion (O:L6Distortion) | Drive / Treble | -70 | -71 | -31 | - | - | hump 9 dB at 2.2 k | knee -27 dB, onset -30 dB, gain +26 dB, mid -12 dB | gain +8 dB, mid +6 dB |
| Megaphone (O:Megaphone) | Grit / Tone | < -91 | < -91 | -9 | - | - | hump 32 dB at 794 | little | mid -5 dB |
| ThrifterFuzz (O:Thrifter) | Drive / Notch Gain | -16 | < -91 | -12 | - | - | scoop 7 dB at 4.0 k | knee -15 dB, gain +10 dB, even +13 dB, mid +3 dB | little |
| XenomorphFuzz (O:Xenomorph) | Gain / Tone | -90 | < -91 | none | - | 10.6 k | flat | little | mid +8 dB |
| CompulsiveDrive (P) | Gain / Tone | -56 | -56 | none | - | 2.4 k | hump 11 dB at 1.4 k | knee -10 dB, onset -10 dB, gain +11 dB | gain +8 dB, mid +9 dB |
| AlpacaRouge (Q) | Drive / HiCut | -45 | -56 | -16 | - | 7.1 k | flat | knee -13 dB, onset -15 dB, gain +13 dB | onset -5 dB, gain +4 dB |
| ClawthornDrive (R) | ODGain / ODTone | -62 | -66 | -12 | - | 4.1 k | hump 8 dB at 445 | onset -5 dB, gain +6 dB, even -12 dB | little |

### What the cards show

1. **Drive moves the knee and the onset together, almost dB for dB, and they move with the gain.**
   - Scream 808: knee -15 dB, onset -15 dB, gain +14 dB from Drive 0.2 to 0.85.
   - Vermin: -35, -35, +35.
   - Valve Driver: -38, -40, +38.

   On most Native drives the Drive knob is a gain in front of a fixed clipper.

   **Exceptions:**
   - Deranged Master: the gain rises 36 dB while the knee stays put (as in the pilot).
   - Vital Boost: +28 dB of gain, the knee fixed at -22 dBFS.
   - Regal Bass DI: Volume, +26 dB, no knee at all.

   For these three the "drive" knob acts after the clipper.
2. **Native drives are linear up to a threshold.** At the centre setting, 49 of 60 models produce no harmonics until a definite level, between -86 and -11 dBFS. The other 11 already produce harmonics at -91 dBFS, below where the staircase starts:
   - Vital Dist, Killer Z, Wringer, Octave Fuzz, Tycoctavia, Kinky Boost, Bronze Master, Industrial, Megaphone, Thrifter and Xenomorph.
   - Most of these are fuzzes, octaves, lo-fi models, or models with a DC offset.

   The -91 dBFS start removed the pilot's "first step already compressed" problem for all but these. Most high-gain models now show a measured knee: Vermin -60, Triangle Fuzz -69, Ballistic -75, Killer Z -77 dBFS.
3. **A clear pre-clipping bass cut shows on 13 models, mostly the Tube Screamer and Bluesbreaker families, the DS-1s and the Rangemaster.**
   - Scream 808 564 Hz, DM4 Screamer 796, Stupor 919, Heir Apparent 919, Teemah 819, Deranged Master 974, Valve Driver 774, Deez One 291 to 326 Hz. Also Pillars 399, Dhyana 154, Industrial 411 and Buzz Saw 97 Hz.
   - On most others the split shows no first-order bass cut ("-").
   - Drive lowers the Tube Screamer's corner by 0.6 to 1.0 octave, as in the pilot: the Helix gain leg is not the fixed 720 Hz of the circuit.
4. **The post-clipping low-pass is where most tone knobs act.**
   - The RAT Filter moves it down 2.9 to 4.1 octaves (Classic Dist, Ratatouille, Vermin, Vital Dist).
   - Minotaur's Tone moves it up 2.8 octaves, Tube Drive's Treble 4.1, and Heir Apparent, Stupor and Dhyana 1.0 to 1.8.
   - Teemah's Treble moves it the other way (-3.7 octaves). Teemah's Treble is cut-only, so this needs a second look before anyone relies on it.
   - Several stack-type tone knobs (Swedish Chainsaw, Heavy Dist, Killer Z) move the mid feature by 19 to 34 dB instead.
5. **Mid features.**
   - The hump is the norm for the Tube Screamer, Bluesbreaker and RAT types (8 to 20 dB, 0.6 to 1.8 kHz).
   - The Big Muff types scoop at their centre setting, but only by 3 to 6 dB (Triangle, Ballistic, Ram's Head, Fuzz Pi). Dark Dove is flat.
   - The deepest scoops are Heavy Dist (12 dB at 794 Hz) and Bronze Master (16 dB at 630 Hz).
6. **Models whose card is not meaningful at quiet inputs.**
   - Xenomorph Fuzz (silent below its trigger).
   - Wringer Fuzz and Tycoctavia (gain -71 and -74 dB at -78 dBFS: a gate, or an octave stage passing little of the tone).
   - Megaphone and Industrial Fuzz (nonlinear at every level).
   - DM4 Jet Fuzz: a phaser, so its knee reads below the staircase. No repeat render was made of it, so it may be time-varying.

   Their identification still works (they are easy to tell apart), but their knee, onset and corners should not be read as circuit facts.

## 7. Code, tests and files

- **`src/tonematch/metrics/distortion_probe.py`** (extended, the pilot still runs unchanged):
  - the `"v1"` probe kind (staircase from -91 dBFS, layout carries its levels);
  - `harmonic_onset`;
  - `mid_feature`.
- **`src/tonematch/metrics/distortion_step3.py`** (new):
  - the knob table (`KNOBS`, `knob_semantics`);
  - the 12-setting plan (`settings_for`);
  - the worker `fingerprint_job`;
  - the cards (`card_values`, `model_card`);
  - the identification and clusters (`loso`, `rates`, `cluster_report`);
  - the tone-check summary and the figures.
- **`scripts/distortion_step3.py`:** `tonecheck`, `plan`, `run` (at most 5 hosts from the scaler, at most 4 analysis workers, resumable, pauses above a load of 9), `analyse` and `ledger`. Documented in `docs/TOOLS.md`.
- **`tests/test_distortion_step3.py`:** 9 tests.
  - The v1 layout; the pilot layout is unchanged.
  - The onset states.
  - Hump, scoop and flat.
  - 12 settings per model of each kind.
  - A knob that is not mapped raises an error.
  - The leave-one-out ranks and rates.
  - A threshold clipper's card (onset, knee, symmetry, gain) and its stored size with no audio.
  - A clean gain has no knee and no onset.
  - The card's knob effects.

  With the pilot and step-1 tests, 29 pass in 36 s. `tests/test_tools_doc.py` still fails, only because another agent's new `drive_xdi_scores.py` is not yet listed.
- **Local files** (gitignored):
  - `reports/local/distortion-id/step3/summary.json` and `cards.md` (the full cards with ranges and both knob effects);
  - `tonecheck.json`;
  - figures: `tonecheck_scream808.svg`, `knee_vs_onset.svg`, `even_vs_postlp.svg`, `model_distance.svg` (ordered by confusable group), `loso_rates.svg`;
  - per model, `models/<model>_harmonics.svg` (gain change and harmonics against level) and `models/<model>_eq.svg` (small-signal response and the pre/post split): 120 files.
- **Ledger:** 38 rows, `benchmark / native / distortion-fp-v1`, dated [2026-10-18T21:57:23](../../ledger/runs.csv?plain=1#L1320 "benchmark · native · distortion-fp-v1 · valid_fingerprints (first of 38 rows at this time)").
- **Re-run:**
  - `nice -n 10 .venv/bin/python scripts/distortion_step3.py run --hosts 5 --workers 4` (it skips the settings already stored);
  - `... analyse` rebuilds everything from the stored fingerprints in 3 s, with no renders.

## 8. Decisions for the human

1. **What step 4 should test.** These fingerprints identify a model from a different knob setting very well when the dry signal is the synthetic probe: 0.97 top-1. Step 4 of the plan tests them on real DI takes, with held-out settings at random knob values. Run step 4, and with which of the three feature levels? My suggestion is the card and the curves: the cheapest and the best.
2. **The class list.** The fingerprints do not sort the models into the circuit classes of distortion-circuits.md (agreement 0.03 to 0.21). Several labels disagree with the Native measurement. Compulsive Drive is symmetric; Dhyana and Teemah are asymmetric. The options:
   - drop the class idea and use the model-to-model distance directly (for example as soft labels for the predictor);
   - build classes from the measured signatures instead: threshold or no threshold, symmetric or asymmetric, where the tone acts, the "drive acts after the clipper" group;
   - keep the circuit classes as names only.
3. **The near-twins that are not twins.** Arbitrator and Facial Fuzz, Vermin and Ratatouille, and Tycoctavia and Octave Fuzz are as far apart as unrelated models. Should the benchmark keep scoring them as sound-alikes, or should a short listening check decide?
4. **Tone tapers that work at the ends.** Scream 808's Tone does almost nothing between 0.2 and 0.8 and a lot at 0 and 1. This matters for the [T176](../../board/TASKS.md#t176 "Drive matching: stop refining a knob within about +-0.15 on…") rule (stop refining a knob within about ±0.15). Near the ends a 0.15 step can be a 9 dB treble change. Measure every tone knob at 0 / 0.1 / 0.9 / 1 (2 to 4 more settings per model, about 10 minutes of rendering)? Or limit the rule to the middle of the range?
5. **The models whose cards are not meaningful** (Xenomorph, Wringer, Tycoctavia, Megaphone, Industrial, Jet Fuzz). Keep them in the identification pool, or measure them with a louder or different probe?
6. **Regal Bass DI's drive slot.** Its Volume acts as an output level. Should it be treated as a model with no drive knob, with only Treble and Bass searched?
7. **Fingerprint size.** It is 6.5 KB against the plan's 3 KB. At this size 720 settings take 5.9 MB, so trimming buys little now. Keep it?
