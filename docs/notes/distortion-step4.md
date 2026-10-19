# Distortion identification, step 4: naming a drive and its knobs from real guitar ([T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes"))

Owner: `tone-metrics`. Run 2026-10-18 on Helix Native 3.82. Plan: [distortion-identification.md](distortion-identification.md) section 8, step 4, and [distortion-step3.md](distortion-step3.md). Approved by the human ("go ahead", [DECISIONS](../DECISIONS.md) 2026-10-18, item 3).

**This is a measurement, not an admission.** The measurement plan went into DECISIONS ("[T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…"): step 4 measurement plan") before any number was computed. No bar was registered, and nothing here changes a score, a method or a weight.

## The short answer

**The test.** Can the step-3 fingerprints name a drive model and its Drive and Tone when the pedal is played with real guitar at settings that were never measured?
- 120 targets, 54 models, Drive and Tone at random positions.
- 8 excerpts of real DI: 4 Guitar-TECHS recordings and 4 of the human's own takes.
- The numbers below are on the 90 test targets. Every method choice was made on the other 30.

**1. The fingerprints themselves are excellent, when they are measured with the test signal.**
- When the step-3 test signal is played at a target's hidden setting, comparing it with the stored fingerprints gives:
  - the model right every time (90 of 90);
  - Drive within ±0.15 98% of the time;
  - Tone within ±0.1 86% of the time.
- These are the human's tolerances, and chance is 2%, 28% and 19%.
- So the knob maps, the 12 measured settings per model with the values in between interpolated, carry the knob information.

**2. Reading the fingerprint from guitar playing is the weak part.**
- **Guitar with the dry signal known.** I fit a simple three-stage model (bass cut, clipping curve, tone filter) to the dry guitar and the output, then read its fingerprint. It names the right model only 30% of the time (57% within the top 5).
  - Comparing renders of every model on the same dry guitar does far better: 92 to 93% right first.
  - The reason: the guitar covers only about 40 dB below its peak, and many Native drives are not one clipper between two filters.
- **Guitar with no dry signal.** The fingerprint features that can be read from the output alone pick the right family no better than chance.
  - Used to reorder AFx-Rep's shortlist, they make it worse: right model first falls from 44% to 21%.

**3. For the case Gate 4 describes,** only the pedal's output plus a separate dry sample of the same player's guitar:
- AFx-Rep (the learned effects embedding, approved for shortlisting only) puts the right model first 48% of the time, in the top 5 86%, and the right family first 60%.
- On the human's own takes: 49%, 91% and 67%.
- No method set the knobs well from the output alone: Drive within ±0.15 about half the time, Tone within ±0.1 about a third.

## Words used

- **Target:** one render of a Native drive model at a hidden setting, played with one real DI excerpt.
- **DI, dry signal:** the guitar straight from the pickup, before any pedal.
- **Fingerprint, card, curves:** the step-3 measurements of a model at one setting, made with a synthetic test signal (the "probe", 115 s of sweeps and tones).
  - The **card** is 12 numbers: knee, harmonic onset, even share, pre bass cut, post treble cut, mid feature, gain.
  - The **curves** are the stored response tables, about 1,700 values.
- **Knob map:** a model's 12 measured fingerprints placed at their Drive and Tone positions, with the values in between interpolated smoothly. Reading the map backwards gives the Drive and Tone that best explain a fingerprint.
- **Twin:** a simple three-stage model fitted to one (DI, output) pair:
  - a first-order bass cut before the clipping, its corner chosen by the fit;
  - a clipping curve with no memory;
  - a tone filter after it.

  The probe is then played through the twin, and its fingerprint is read with the same code as the library.
- **Top-1 / top-5:** how often the true model is ranked first, or in the first five, of 54. Chance: 0.019 and 0.093.
- **Family top-1:** how often the first-ranked model is in the true model's measured family. The families are the 15 of [T180](../../board/TASKS.md#t180 "Circuit families from the measured fingerprints") (`cache/drive-maps/drive-families-v1.json`). Chance: 0.14.
- **Drive OK / Tone OK:** the knob read off the TRUE model's map is within ±0.15 (Drive) or ±0.1 (Tone) of the hidden value. This is measured apart from whether the model was named, so it shows what the knob step can do on its own. Chance: 0.28 and 0.19.
- **Brackets:** 95% intervals from resampling models (one model's targets are correlated).

## 1. What was tested

**Pool.** 54 drive models: the 60 of step 3 minus the six whose card is "not meaningful at quiet input" (Xenomorph, Wringer, Tycoctavia, Megaphone, Industrial, DM4 Jet Fuzz). They are left out of the targets and of every gallery.

**Audio: 8 real DI excerpts of 6 s,** each with 0.5 s of lead-in that is rendered and thrown away. All are played at their recorded level.

| Excerpt | Source | Split | Peak / RMS (dBFS) |
|---|---|---|---|
| E1 | Guitar-TECHS P1, scales (Eb, from 60 s) | dev | -16 / -35 |
| E2 | Guitar-TECHS P1, chords (Drop3 Maj7, from 114 s) | test | -19 / -41 |
| E3 | Guitar-TECHS P2, scales (C, from 121 s) | test | -26 / -35 |
| E4 | Guitar-TECHS P2, chords (Set4 dim, from 53 s) | test | -21 / -37 |
| E5 | the human, take `112935` 1, from 19.5 s | dev | -9 / -28 |
| E6 | the human, take `112935` 1, from 38.5 s | test | -10 / -28 |
| E7 | the human, take `112935` 1, from 57 s | test | -10 / -28 |
| E8 | the human, take `112611` 2, from 1.5 s | test | -12 / -39 |

- **Freshness.** None of these windows was used in a rating session (v2, v3, v4) or in the drive-specialist calibration.
- **One change from the plan.** The plan had E8 from a `113158` take. No 6.5 s window there is free of rating excerpts, so E8 comes from the earlier `112611` session, also the human's playing.
- **The split is by excerpt.** P2 never appears in the dev split.

**Targets.**
- 120 targets, 15 per excerpt. 42 models have 2 targets and 12 have 3, always on different excerpts.
- Drive and Tone are drawn uniformly from the 128 CC steps, leaving out every step that was ever measured (the step-3 grid and the [T179](../../board/TASKS.md#t179 "Tone-knob ends at 0/0.1/0.9/1 on all drive models") tone ends), so no target sits on a measured setting.
- Level and the other knobs stay at the library default.
- 15 targets are one-knob models (Drive only).

**Renders.**

| What | Renders | Use |
|---|---|---|
| Targets | 120 | the test items; audio kept in a small cache until the end, then deleted |
| Truth probes | 120 | the probe at each target's own setting; fingerprints only, a diagnostic, never used to identify anything |
| Gallery | 5,184 | 54 models × the 12 step-3 settings × 8 excerpts; features taken on the fly, no audio kept; used by the baselines and the output-only arm |

- **Totals.** 5,424 renders kept, 0 failed. About 100 gallery renders were redone, for two reasons:
  - the AFx-Rep embedder's GPU path reported failed command buffers in the worker processes, so those embeddings were discarded and the run moved to the CPU;
  - one rare frame made the output-only features crash.
- **Rendering time.** About 29 minutes in all, on at most 3 Native hosts from the host scaler.
- **Load.** The 1-minute load peaked at 8.9 and stayed under 10. Rendering pauses between batches above 9.
- **`proc_check`.** No orphans before or after every run.
- **Fitting the twins.** 15 s per target, plus 7 s to read its fingerprint, in 4 processes.

## 2. Reading a fingerprint from guitar with the dry signal known (condition A)

The step-1 estimators need the probe signal. A guitar take is not a probe, so I fit a twin to each (DI, output) pair and read the twin's fingerprint (the "Words used" section describes the twin).
- The fit is weighted so that quiet playing counts as much as loud playing.
- The curve uses knots spaced down to 72 dB below the take's peak.

On made-up circuits with a 6 s take this works well:
- a soft clipper's knee to within 0.3 dB;
- a Tube-Screamer-like circuit's knee within 0.5 dB, and its bass-cut corner and mid hump right.

**On Native drives the twin is much rougher.**
- **Fit.** Half the twins leave more than 4% of the output's energy unexplained (median residual -13.8 dB). Only 32% are better than -20 dB.
- **Against the truth probe at the same setting**, the median errors on the 90 test targets are:

| Card value | Median error | 3 in 4 within |
|---|---|---|
| Gain at 1 kHz | 1.3 dB | 4.6 dB |
| Knee | 16 dB | 35 dB |
| Harmonic onset | 30 dB | 50 dB |
| Even share | 7.8 dB | 21 dB |
| Pre bass-cut corner | 1.3 octaves | 2.7 octaves |
| Post treble-cut corner | 1.1 octaves | 2.3 octaves |
| Mid feature | 2.8 dB | 9.0 dB |

**Why the knee and onset fail.**
- The excerpts were chosen for busy playing. Their 1-in-10 quietest moments are only about 17 to 39 dB below the peak.
- High-gain models clip 50 to 80 dB below full scale. Their knee lies far below anything the guitar played, so the twin has to guess it.
- When the knee does fall inside the played range, the twin gets it right to within a few dB: Hedgehog, Minotaur, Tube Drive, Heir Apparent, Teemah, Classic Distortion.
- The onset ("harmonics within 60 dB of the tone") is too sensitive for a fitted curve. So is the gentler 40 dB version: it agrees on the state only 34% of the time.

**Systematic error.**
- The twin's errors are not random. They pull many targets toward a few "central" models. Before calibration, Stupor OD came out first for 8 of the 30 dev targets.
- **Calibration** corrects this, using the 30 dev targets, where both the twin and the truth probe are known:
  - each value's average twin error is subtracted;
  - unreliable values are down-weighted.
- The 90 test targets used the dev calibration unchanged; each dev target used a calibration from the other 29.
- This was the only choice that clearly helped on dev. On dev, these changed nothing beyond noise:
  - a linear region near zero in the clipping curve;
  - a shelf instead of a pure bass cut;
  - tighter or looser masking of levels above the take's peak;
  - leaving any one block of curves out.

## 3. Results with the dry signal known (condition A), test split, 90 targets

| Method | Model top-1 | Top-5 | Family top-1 | Drive OK | Tone OK |
|---|---|---|---|---|---|
| Chance | 0.02 | 0.09 | 0.14 | 0.28 | 0.19 |
| **Fingerprint from the guitar (twin), curves, calibrated on dev: the dev choice** | **0.30** [0.19, 0.42] | 0.57 | 0.47 | 0.69 | 0.46 |
| the same, not calibrated | 0.30 [0.19, 0.42] | 0.53 | 0.43 | 0.51 | 0.51 |
| the same, card only (calibrated) | 0.16 [0.09, 0.23] | 0.41 | 0.31 | 0.31 | 0.32 |
| the same, with the [T179](../../board/TASKS.md#t179 "Tone-knob ends at 0/0.1/0.9/1 on all drive models") tone ends in the maps | 0.30 [0.19, 0.42] | 0.54 | 0.46 | 0.68 | 0.50 |
| Baseline: `mrstft_v2`, nearest grid render on the same DI | **0.92** [0.86, 0.98] | 0.97 | 0.96 | 0.92 | 0.47 |
| Baseline: AFx-Rep, nearest grid render on the same DI | **0.93** [0.88, 0.98] | 1.00 | 0.96 | 0.94 | 0.47 |
| Diagnostic: the probe at the hidden setting, curves | 1.00 | 1.00 | 1.00 | 0.98 | **0.86** |
| the same, card only | 0.92 [0.86, 0.98] | 0.97 | 0.97 | 0.94 | 0.74 |
| the same, nearest measured setting (no interpolation) | 1.00 | 1.00 | 1.00 | 0.96 | 0.46 |
| the same, with the [T179](../../board/TASKS.md#t179 "Tone-knob ends at 0/0.1/0.9/1 on all drive models") tone ends | 1.00 | 1.00 | 1.00 | 0.96 | 0.88 |

The baselines' knobs are the true model's nearest grid setting; the fingerprint methods read them off the true model's map.

How often both the model and its knobs were right:

| Method | Model and knobs both right |
|---|---|
| Fingerprint from the guitar, calibrated | 0.19 |
| `mrstft_v2` | 0.51 |
| AFx-Rep | 0.53 |
| Probe diagnostic | 0.86 |

The dev split (30 targets) gave the same picture: twin 0.27 top-1, baselines 0.93 to 0.97, probe 1.00.

**What it shows.**
- **Model.** Rendering every model on the target's own DI and comparing (what search already does) names the model about 3 times as often as the fingerprint estimated from guitar.
- **Knobs, given the right model.**
  - Drive: the twin's inversion (0.69) is worse than simply taking the nearest of the 12 measured settings on the same DI (0.92 to 0.94).
  - Tone: they are equal (0.46 against 0.47).
  - Only a fingerprint measured with the probe sets Tone well: 0.86, against 0.46 from the nearest measured setting.
  - Interpolating the knob map is what lifts Tone, but only with a probe-quality fingerprint.
- **Tone ends.** Adding the [T179](../../board/TASKS.md#t179 "Tone-knob ends at 0/0.1/0.9/1 on all drive models") tone ends (0, 0.1, 0.9 and 1) to the maps hardly changes anything here: probe Tone 0.86 to 0.88.
- **Sources.** Guitar-TECHS and the human's takes give similar numbers:
  - twin top-1 0.33 against 0.27;
  - baselines 0.91 against 0.93 to 0.96.

## 4. Results with no dry signal (condition B), test split, 90 targets

The output-only features are those the plan named:
- the even/odd balance of harmonics 4 to 12 on sustained notes;
- the post-EQ, as the long-term spectrum minus a clean-guitar reference spectrum (the other excerpts' DIs);
- crest factor;
- compression (the spread of frame levels, and the note-decay slope).

They are compared with the same features of the gallery renders on other excerpts. The gallery is one of three:
- **All other excerpts** (registered).
- **Other players only** (registered): the song case, where the guitar is unknown.
- **Same player only:** the Gate 4 case, a separate dry sample of the player's own guitar. Added after the plan was registered because it is exactly Gate 4's set-up, and labelled as such.

| Method | Model top-1 | Top-5 | Family top-1 | Drive OK | Tone OK |
|---|---|---|---|---|---|
| Chance | 0.02 | 0.09 | 0.14 | 0.28 | 0.19 |
| AFx-Rep alone, all other excerpts | **0.44** [0.33, 0.56] | 0.84 | 0.59 | 0.50 | 0.40 |
| AFx-Rep alone, other players only | 0.39 [0.28, 0.50] | 0.76 | 0.53 | 0.46 | 0.36 |
| AFx-Rep alone, same player only (Gate 4 case) | **0.48** [0.36, 0.59] | 0.86 | 0.60 | 0.50 | 0.36 |
| Output-only fingerprint features, all other excerpts | 0.08 [0.03, 0.13] | 0.30 | 0.13 | 0.37 | 0.26 |
| the same, same player only | 0.06 [0.01, 0.11] | 0.20 | 0.19 | 0.38 | 0.33 |
| AFx-Rep top 5, reordered by the features (all) | 0.21 [0.12, 0.31] | 0.84 | 0.43 | 0.37 | 0.26 |
| AFx-Rep top 8, reordered by the features (all) | 0.23 [0.15, 0.32] | 0.68 | 0.42 | 0.37 | 0.26 |
| Baseline: `mrstft_v2` on another player's excerpt | 0.06 [0.01, 0.12] | 0.21 | 0.10 | 0.40 | 0.29 |

In the AFx-Rep rows the knobs are its nearest grid setting. In the feature rows they come from the features' knob map. The dev split agreed:
- AFx-Rep 0.47 to 0.70 top-1;
- the features 0.03 to 0.20;
- the shortlist reordered by the features below AFx-Rep alone.

On the human's own takes, AFx-Rep with the same-player gallery reached 0.49 top-1, 0.91 top-5 and 0.67 family. With the other-player gallery it reached 0.29, 0.71 and 0.51.

**What it shows.**
- **The output-only fingerprint features do not identify drives.** Their family rate (0.13 to 0.19) is at chance (0.14), and they make AFx-Rep's choice worse.
- **AFx-Rep is the only useful output-only signal,** for the model and the family. A same-player dry sample helps it a little:
  - 0.48 against 0.39 with other players' DI;
  - 0.91 against 0.71 top-5 on the human's takes.
- **No method set the knobs from the output alone much better than guessing.** That fits [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive").
- **`mrstft_v2` needs the same notes.** On another player's excerpt it is near chance, as expected.

## 5. What this means for matching the human's real pedals (Gate 4)

**Gate 4 as written.** The matcher gets the pedal's output and a separate dry sample of the player's guitar. That is condition B with a same-player gallery. On Native targets, the best available route is:
- AFx-Rep shortlists the model, right first about half the time and in the top 5 about 9 times in 10;
- then the family is widened ([T178](../../board/TASKS.md#t178 "Per-knob tolerance (Tone about +-0.1, finer near the ends") already does this).

The knobs cannot be read from the output alone with anything measured here. The fingerprint features add nothing there, and the twin needs the same performance's dry signal, which Gate 4 holds back for scoring. Real pedals will be harder than Native-on-Native, because no Helix model is an exact twin of them.

**What would change that: measure the pedal itself.**
- With the probe played through the target's own setting, the stored fingerprints named the right Native model every time, and set Drive and Tone within the human's tolerances 98% and 86% of the time.
- The probe is 115 s of synthetic sweeps and tones.
- For a real pedal, that means re-amping it once per setting to be matched.
- That is a different Gate 4 protocol: the matcher would see the pedal, not just a recording of it.
- How close the nearest Native model would sound for a real pedal is not known. These numbers are Native measured against Native.

**For [T182](../../board/TASKS.md#t182 "Re-match the Mother 28.3-38.3 s guitar stem") (the Mother re-match)** the fingerprint inversion will not set the knobs.
- I tried the [T182](../../board/TASKS.md#t182 "Re-match the Mother 28.3-38.3 s guitar stem") window (28.3 to 38.3 s) with the human's `113158` take 1 as the DI. The twin explained none of the target (fit -0.1 dB): the play-along take is another performance, not the stem's input.
- `estimate_knobs` therefore returns nothing there, and search falls back as designed.
- Its aligned-take path has the same problem in principle. `mrstft_v2` on a different performance is near chance (section 4).

## 6. Decisions for the human

1. **The blind route for drives.**
   - The output-only fingerprint features did not work. Stop building them?
   - Then AFx-Rep stays the only shortlist tool for no-DI drive matching (as [T178](../../board/TASKS.md#t178 "Per-knob tolerance (Tone about +-0.1, finer near the ends") already does), and the knobs are left to the listener: they would be offered at a few coarse settings for the ear to pick.
2. **A "measured pedal" mode for Gate 4: decide whether to allow it.**
   - The human would re-amp each real pedal with the 115 s probe at the setting to be matched, for example through the HX Stomp's send and return.
   - The match would then come from the fingerprints in seconds, with no search.
   - This needs:
     - a level calibration of the send and return (the probe is defined in dBFS);
     - the human's agreement to change what Gate 4's matcher may see.

   I would first measure 2 or 3 of the human's pedals this way and compare the nearest Native model by ear.
3. **The twin route (dry signal known).**
   - It is much weaker than rendering every model on the DI. Keep it only as the cheap `estimate_knobs` hint for search, with its fit guard? Or drop it?
   - For [T182](../../board/TASKS.md#t182 "Re-match the Mother 28.3-38.3 s guitar stem") it gives nothing (the take is a different performance).
4. **Bars.** None were registered. If the human wants this as a regression test, the numbers above are the baseline; this note sets no bar.

## 7. Limits

- **Native only.** Everything is Native measured against Native; there are no real pedals and no Stomp. Section 7.1's options B (leave one model out) and C (Stomp and real pedals) were not run.
- **One fixed Level knob.** In real use the Level knob and the input level are unknown. The DI-known numbers here use the true input level.
- **The twin is one simple form:** a first-order bass cut, one memoryless clipping curve and a tone filter. A two-stage or level-dependent form might fit Native better. That was not tried.
- **Few excerpts.** Only 8, from 3 players and 3 guitars. Dense playing was chosen on purpose; sparser playing with quiet passages might help the twin find low knees.
- **The calibration** was learned from 30 dev targets (Native renders). Whether it carries over to real pedals is unknown.
- **One robustness flaw, found while testing the search interface.** The twin's weights let near-silent output dominate. Silence in the output against non-silent input (a noise gate, or a badly aligned take) can ruin a fit. `estimate_knobs` now trims the zero-filled edge after aligning a take, and its fit guard reports a failed fit. The reported numbers use the weighting as measured, with the envelope floored 80 dB below its maximum.
  - A floor of 40 dB below was tried on the 30 dev targets afterwards. It was no worse: top-1 0.30 against 0.27, top-5 0.70 both, Tone 0.63 against 0.56.
  - It was not switched, so that the test numbers stay valid for the code as shipped.
  - It is a candidate for the next run.

## 8. Code, tests and files

**`src/tonematch/metrics/distortion_step4.py`:**
- the twin: `fit_twin`, `Twin`, `twin_fingerprint`;
- the feature blocks and masking: `feature_blocks`, `curve_vector`;
- `FeatureSpace`, block-weighted with masked values ignored;
- `KnobMap`: thin-plate interpolation over Drive and Tone, and its inversion;
- the output-only features: `blind_features`;
- the search hook:
  - `estimate_knobs(target, di, models) -> {model: {knob: position 0..1}}`;
  - `estimate_knobs_with_confidence`: the same plus the ranking, the fit, the alignment lag and a confidence of none, low or medium.
  - Without a DI, or when the DI does not explain the target (fit worse than -3 dB), it returns no knobs.
  - It reads the dev calibration from `cache/distortion-step4/calibration.npz` when present.
  - It takes about 20 s per 6 s window; a 10 s window takes proportionally longer.

**`scripts/distortion_step4.py`:** `plan`, `render`, `analyse` (with `--only B`), `ledger` and `clean`. Documented in `docs/TOOLS.md`.

**`scripts/distortion_step4_analysis.py`:** the twin workers, calibration, baselines, condition B and the ledger rows.

**`tests/test_distortion_step4.py`:** 9 tests, on synthetic circuits and a synthetic plucked-string DI, about 55 s.
- the twin recovers a soft clipper's curve and knee, and a pre bass cut;
- the save and load round trip, and the linear inner region;
- the knob map interpolates and inverts;
- masking and block weights;
- the output-only features see asymmetric clipping;
- the search hook needs a matching DI;
- the fit guard rejects an unrelated DI.

**Local files (gitignored):**
- `cache/distortion-step4/`: the plan, truth fingerprints, gallery features, twins and calibration. The target audio was deleted at the end.
- `reports/local/distortion-id/step4/summary.json` and `rows.json`.

**Ledger:** `benchmark / native / distortion-step4-dryknown` and `distortion-step4-blind`, test-split rows (see the row times in DECISIONS).

**Re-run:**
- `TONEMATCH_AFXREP_DEVICE=cpu nice -n 10 .venv/bin/python scripts/distortion_step4.py analyse` rebuilds everything from the caches. The targets would need `render --what targets` first.
