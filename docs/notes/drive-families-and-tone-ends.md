# Drive tone-knob ends and drive families ([T179](../../board/TASKS.md#t179 "Tone-knob ends"), [T180](../../board/TASKS.md#t180 "Circuit families from the measured fingerprints"))

Owner: `tone-metrics`. Run 2026-10-18 on Helix Native 3.82. Approved by the human ("go ahead", [DECISIONS](../DECISIONS.md) 2026-10-18, the five post-v4 recommendations, items 2 and 4). Builds on step 3, [distortion-step3.md](distortion-step3.md).

**Nothing here is adopted.** Two measurements and two maps:
- the tone-knob map, for search ([T178](../../board/TASKS.md#t178 "Per-knob tolerance")) to set its own tolerances from;
- the family map, which the human approved building.

Search decides what to do with them. Only fingerprints were stored, no audio. The test signal is the synthetic dfp-v1 probe, and no song audio was used.

## In one paragraph

- **Tone knobs: most are not packed at their ends.**
  - Across the 52 two-knob drives, the median share of a tone knob's effect that happens in its first and last tenth is 0.18. A perfectly even knob would give 0.20.
  - 23 knobs are close to linear.
  - Only 6 are end-loaded, and only 3 put more than half their effect in the end zones: Scream 808 (0.63), Deranged Master (0.54) and Legendary Drive (0.53).
  - 8 are tapers (the effect per step grows or shrinks steadily along the knob).
  - 7 are the opposite of end-loaded: their ends hardly move the sound.
  - So the Scream 808 finding from step 3 is real but uncommon.
  - With an equal-effect rule, 10 models would want an end tolerance tighter than search's current ±0.05.
- **Families: 15 measured families.**
  - 9 have 2 to 10 members, and 6 are single models: four of the six flagged fuzzes, plus Ratatouille and DM4 Octave Fuzz.
  - The families are reproducible: two independent halves of 200 resamples give the same 15 families exactly.
  - But the boundaries are soft. The models do not fall into sharply separated groups, and 11 of the 60 sit with their family mates in fewer than half of the resamples.
  - The families agree with the circuit-note labels only weakly. Only one circuit group stays whole: the C group, KWB, Top Secret OD and DM4 Overdrive.

## Words used

- **Tone knob effect between two settings:** how much the pedal's response changes, as a root-mean-square (RMS) difference in dB.
  - The response is the level of the fundamental across 100 Hz to 10 kHz, at three input levels: -66, -30 and 0 dBFS (decibels relative to digital full scale).
  - It includes the overall level change, because Level stays fixed and a tone knob that also changes loudness is heard that way.
  - The "level-free" variant removes each input level's average change, keeping only the change of shape.
- **Per 0.1:** the effect over one tenth of the knob's travel.
  - The knob runs 0 to 1 over its range; Native plays MIDI step c as c/127.
  - The end zones are [0, 0.1024] and [0.8976, 1], MIDI steps 0 to 13 and 114 to 127. The middle is everything in between.
- **Total effect:** the sum of the effects of the six steps along the knob (0, 0.1, 0.2, 0.5, 0.8, 0.9, 1). A knob that moves the sound one way and then back counts both moves.
- **ARI (adjusted Rand index):** agreement between two groupings of the same models. 1 is identical, 0 is chance.
- **Co-assignment:** the share of resamples in which two models land in the same family.
- **Member confidence:** a model's average co-assignment with the other members of its family.
- **PAC (proportion of ambiguous clustering):** the share of model pairs that are together in between 10% and 90% of resamples. 0 means every pair is always together or always apart.

## Part 1. The tone-knob ends ([T179](../../board/TASKS.md#t179 "Tone-knob ends at 0/0.1/0.9/1 on all drive models"))

### What was measured

- **Settings.** Tone 0, 0.1, 0.9 and 1.0 (MIDI steps 0, 13, 114 and 127), at Drive 0.5 and 0.85 (steps 64 and 108), for every two-knob drive: 52 models, 416 renders.
  - Level, the other knobs and the unmapped switches stayed at the library defaults, as in step 3.
  - The probe is dfp-v1, the same as step 3.
  - Joined with step 3's tone 0.2, 0.5 and 0.8 at the same two Drive values, each Drive has a 7-point line along the tone knob.
- **Controls.** Two step-3 settings (Scream 808 at the centre, DM4 Screamer at Drive 0.85 and Tone 0.8) were rendered again in the same run. Both fingerprints are byte-identical to step 3's, so the new points join the old ones without an offset.
- **Run.**
  - 418 renders (416, plus the 2 controls), 0 failed.
  - Wall-clock time 14.5 minutes, of which 5 minutes was rendering.
  - The host scaler granted 1 to 4 hosts. Analysis ran in 3 processes.
  - The 1-minute load, sampled after each batch, averaged 6.0 and peaked at 7.6. The run never had to pause, because no batch started above 9.
  - `proc_check` found no orphans before or after.

### What it shows

| Measure, 52 tone knobs | Value |
|---|---|
| Median share of the effect in [0, 0.1] / middle / [0.9, 1] | 0.09 / 0.82 / 0.08 (an even knob: 0.10 / 0.80 / 0.10) |
| Median effect per 0.1: bottom / middle / top | 1.2 / 1.4 / 1.0 dB |
| Models with more than half the effect in the two end zones | 3 (Scream 808 0.63, Deranged Master 0.54, Legendary Drive 0.53) |
| Shape label the same at Drive 0.5 and 0.85 | 52 of 52 |

**Shapes** (level included; the rules are below):

| Shape | Count | Models |
|---|---|---|
| linear | 23 | DM4 Fuzz Pi, DM4 L6 Distortion, DM4 Overdrive, DM4 Sub Oct, Alpaca Rouge, Ampeg Scrambler, Dark Dove, Deez One Vintage, Heir Apparent, Horizon, KWB, Minotaur, Obsidian 7000, Pillars, Prize Drive, Regal Bass DI, Stupor OD, Swedish Chainsaw, Thrifter, Tone Sovereign, Triangle, Wringer, Killer Z |
| end-loaded | 6 | Scream 808 (both ends), Deranged Master (both), Legendary Drive (bottom), DM4 Facial Fuzz (bottom), DM4 Line 6 Drive (bottom), Megaphone (bottom) |
| taper | 8 | Heavy at the bottom: DM4 Color Drive, DM4 Jet Fuzz, DM4 Jumbo Fuzz, DM4 Octave Fuzz, Compulsive Drive. Heavy at the top: Clawthorn, Ratatouille, Vermin |
| middle-loaded (ends slack) | 7 | Ballistic, Deez One Mod, Dhyana, Hedgehog D9, Ram's Head, Vital Dist, Xenomorph |
| uneven | 8 | DM4 Buzz Saw (top slack), DM4 Classic Dist (bottom slack), DM4 Heavy Dist (top slack), Teemah (bottom slack), ZeroAmp (top slack), DM4 Screamer, DM4 Tube Drive, Valve Driver |

- **The extremes.**
  - Scream 808 is the clearest end-loaded knob: 2.8 dB per 0.1 at the bottom, 0.6 in the middle and 4.8 at the top. That agrees with step 3's file-truth check, where the knob was flat from 0.2 to 0.8.
  - Legendary Drive does half its work in the first tenth: 6.5 dB per 0.1, against 0.9 in the middle.
  - At the other extreme, Dhyana's and Vital Dist's bottom tenths move the sound by under 0.1 dB per 0.1, and Ballistic's top tenth by 0.2 dB.
  - All the RAT-type Filter knobs (Classic Dist, Ratatouille, Vermin, Vital Dist) do almost nothing in their first tenth (0.06 to 0.21 dB per 0.1).
- **Level-free shapes.** Removing the level change gives a similar picture: 19 linear, 8 end-loaded, 7 taper, 7 middle-loaded, 11 uneven. Pillars, Prize Drive and the Ampeg Scrambler become end-loaded once loudness is set aside, because their ends change mostly shape.
- **Where the tone acts.** For 44 knobs the 0-to-1 effect is the same at every input level (within 1 dB), so the tone acts after the clipping. For 8 it is not:
  - Tone Sovereign: 12.7 dB at a quiet input against 4.0 dB at full scale, so its tone acts before the clipping.
  - Ratatouille: 11.5 against 6.4.
  - Regal Bass DI: 9.5 against 4.5.
  - Vital Dist and Deranged Master: smaller differences.
  - Clawthorn: grows with level.
  - Wringer and Xenomorph: two of the flagged fuzzes.

  The map marks these as `level_dependent`.

**Shape rules** (`drive_tone_families.knob_shape`), checked in this order on the effect per 0.1:

1. **Little effect:** the whole travel moves the sound by under 1 dB (no knob qualified).
2. **Taper:** the six step effects trend one way along the knob (rank correlation at least 0.8) and one end acts at least 3 times as hard as the other.
3. **End-loaded:** an end acts at least twice as hard as the middle.
4. **Middle-loaded:** both ends act at most half as hard as the middle.
5. **Linear:** both ends are within a factor of 2 of the middle, and so are the middle's two halves (0.2-0.5 and 0.5-0.8).
6. **Uneven:** anything else.

### For search ([T178](../../board/TASKS.md#t178 "Per-knob tolerance (Tone about +-0.1, finer near the ends"))

The map is `reports/local/distortion-id/tone-ends/knob_sensitivity.json`. A copy is at `cache/drive-maps/knob_sensitivity.json`: 75 KB, holding only model ids, knob names and numbers. It is written to the format search's loader reads (`src/tonematch/search/knob_tol.py`).

- **Per model:**
  - `drive_knobs` and `tone_knobs`;
  - `knobs`, giving the tone knob's measured entry: its range, the effect per 0.1 at the bottom, in the middle and at the top (with and without level), the shares, the shape, the knob step that moves the sound by 1 dB in each zone, the per-input-level effect and `level_dependent`.
  - The 8 one-knob models carry `drive_knobs` only.
- **No tolerance keys.** The file deliberately leaves out `tone_tol`, `tone_end_tol` and `end_zone`, so search's own defaults still apply (Tone ±0.1, ±0.05 within 0.1 of an end). Choosing them is search's decision.
- **`suggested_tol`** (not read by the loader) applies one stated rule: an end step should move the sound by as much as a ±0.1 step does in the middle, capped at 0.1 and floored at 0.02.
  - It suggests an end tolerance tighter than ±0.05 for 10 models: Scream 808 (0.02 at both ends), Deranged Master (0.025 / 0.021), Legendary Drive (0.020 bottom), Compulsive Drive (0.029 bottom), DM4 Facial Fuzz (0.033), DM4 Jet Fuzz (0.038), Megaphone (0.043), DM4 Line 6 Drive (0.044), Clawthorn (0.046 top) and DM4 Color Drive (0.048).
  - For 27 models no end needs to be tighter than the middle's 0.1. For those, the current ±0.05 at the ends is stricter than the measurement asks.
- **The middle.** The median knob moves the sound by 1 dB over about 0.07 of travel in the middle. Whether ±0.1 is tight enough there is for search's listening evidence (v4) to decide, not this measurement.

Figures, in `reports/local/distortion-id/tone-ends/`:
- `per0p1_<shape>.svg`: the effect per 0.1 along the knob, one line per model, one file per shape;
- `shares.svg`: the three shares for every model.

## Part 2. Families from the measured fingerprints ([T180](../../board/TASKS.md#t180 "Circuit families from the measured fingerprints, circuit names as…"))

### How the families were made

- **Features.** Step 3's 720 fingerprints (60 models, 12 settings each), in two blocks of equal weight:
  - the card without level (knee, onset, even share, pre/post corners, mid feature);
  - the stored curves without level (block-weighted).

  The circuit names were not used.
- **Why a consensus.** A single clustering of the model averages (Ward) is not stable. Resampling each model's settings, or dropping 20% of the features, gives an ARI of only 0.4 to 0.65 against the full-data clusters, at any number of families from 4 to 16. Different feature choices did no better: card only, curves only, knob-invariant "character" features, and a distance to the nearest setting of each model. The drive models do not fall into sharply separated groups.
- **Consensus clustering.** 200 resamples, each a bootstrap of every model's 12 settings plus a random 80% of the features. Each resample is clustered with Ward. The families are the average-linkage cut of how often each pair lands together.
- **How many families.** The rule, set before looking at the names:
  - take k from 4 to 16;
  - keep only k where the families built from two independent halves of the resamples agree (ARI at least 0.9);
  - and where at most 2 single-model families are not flagged models;
  - then pick the lowest PAC.

  That gives **k = 15**, with the halves agreeing at ARI 1.0 and PAC 0.24. Coarser cuts that are also reproducible exist: k = 5 (ARI 0.99) and k = 12 (ARI 1.0). But at k = 5 one family holds 37 models, which would not narrow a search.
- **Robustness.**
  - A single resample agrees with the consensus at ARI 0.58 on average (0.37 at the 5th percentile). The core of each family is stable, and the edge models move.
  - Re-clustering without the six flagged models changes the other 54 assignments little (ARI 0.73).
  - Plain Ward on the full data at the same k agrees at 0.84.
- **Names.** Each family is named from the medians of its members' measured character:
  - **Symmetry,** from the even share: under -60 dB symmetric, under -30 slightly asymmetric, otherwise asymmetric.
  - **Softness,** from the static curve's Rapp smoothness, using only sides whose fit holds: under 2.5 soft, under 5 medium, otherwise hard.
  - **Gain,** from the knee: above -40 dBFS low, above -60 medium, otherwise high; or "nonlinear at every level" / "compressing from the quietest input" / "clean until near full scale".
  - **The mid feature at the centre setting** (hump or scoop and its frequency); "mixed" when no kind has half the members.
  - **Pre-HPF / post-LPF:** a bass cut before the clipper or a treble cut after it, when at least half the members have one.
  - **"Drive after the clipper":** Drive raises the gain without moving the knee.

### The families

Members in descending order of confidence are in `cache/drive-maps/drive-families-v1.json` (`member_confidence`). Here, a model's confidence follows it in brackets when it is under 0.5.

| Id | Measured character | Members | Circuit labels inside (labels only) |
|---|---|---|---|
| F01 | soft asymmetric, low gain, mixed mids | DM4 Buzz Saw, DM4 Color Drive, DM4 Fuzz Pi, DM4 Jumbo Fuzz, DM4 Overdrive, Alpaca Rouge, Ampeg Scrambler OD, KWB, Top Secret OD, Vital Boost | C (Distortion+ / OD-250 types, the whole group), I (Ge fuzzes), G (Fuzz Pi), M (Colorsound), Q, L (Ampeg), K (Vital Boost) |
| F02 | medium slightly asymmetric, medium gain, mid-hump 0.7 kHz, post-LPF 1.4 kHz | DM4 Classic Dist (0.32), DM4 Facial Fuzz (0.48), DM4 Line 6 Drive, DM4 Sub Oct Fuzz (0.48), DM4 Tube Drive, Clawthorn (0.31), Dhyana, Horizon (0.47), Teemah (0.32) | D, H, M, J, N, R, A, B: the loosest family |
| F03 | soft slightly asymmetric, high gain, mid-scoop 0.9 kHz | DM4 Heavy Dist, DM4 L6 Distortion, Ballistic, Dark Dove, Ram's Head, Triangle, Killer Z | G (Big Muff types), F (the two MT-2 types), O (DM4 L6 Distortion) |
| F04 | hard slightly asymmetric, compressing from the quietest input, mid-hump 0.7 kHz, post-LPF 0.4 kHz | DM4 Jet Fuzz (flagged) | fuzz with a phaser |
| F05 | slightly asymmetric, nonlinear at every level, flat mids, post-LPF 8.9 kHz | DM4 Octave Fuzz | J (octave) |
| F06 | medium symmetric, medium gain, mid-hump 0.9 kHz, post-LPF 0.7 kHz | DM4 Screamer, Compulsive Drive, Heir Apparent, Minotaur, Pillars, Prize Drive, Scream 808, Tone Sovereign, Vermin | A (Tube Screamer types), B (Klon, Bluesbreaker types), P, D (one RAT type). The clearest family: every member above 0.88 |
| F07 | soft asymmetric, medium gain, mixed mids | Arbitrator Fuzz, Deez One Mod, Deez One Vintage, Obsidian 7000, Pocket Fuzz (0.38), Swedish Chainsaw, Valve Driver (0.46) | H (Fuzz Face), E (the two DS-1 types), L (Obsidian), G2 (Pocket Fuzz), F (HM-2), N (Tube Driver) |
| F08 | soft asymmetric, medium gain, mid-hump 1.8 kHz, post-LPF 4.8 kHz | Deranged Master, Hedgehog D9, Legendary Drive, Stupor OD, Vital Dist (0.46) | K (Rangemaster type), E (SD9), F (Legendary Drive), A2 (Stupor OD), D (Vital Dist) |
| F09 | asymmetric, nonlinear at every level, flat mids, pre-HPF, post-LPF 8.8 kHz, drive after the clipper | Industrial Fuzz (0.36), Wringer Fuzz (0.36), both flagged | two fuzzes |
| F10 | asymmetric, nonlinear at every level, mid-scoop 2.3 kHz | Kinky Boost, Thrifter Fuzz, Bronze Master | K, O, L |
| F11 | asymmetric, nonlinear at every level, mid-hump 0.8 kHz | Megaphone (flagged) | lo-fi |
| F12 | medium slightly asymmetric, high gain, mid-hump 1.8 kHz, post-LPF 3.8 kHz | Ratatouille | D (RAT type) |
| F13 | medium slightly asymmetric, clean until near full scale, mid-scoop 0.8 kHz, drive after the clipper | Regal Bass DI, ZeroAmp Bass DI | L (bass DIs) |
| F14 | asymmetric, nonlinear at every level, flat mids, drive after the clipper | Tycoctavia Fuzz (flagged) | J (octave) |
| F15 | symmetric, nonlinear at every level, flat mids, post-LPF 10.6 kHz | Xenomorph Fuzz (flagged) | triggered oscillator |

- **The six flagged fuzzes** ("card not meaningful at quiet input": Xenomorph, Wringer, Tycoctavia, Megaphone, Industrial and Jet Fuzz) never join a family of ordinary models. Four stand alone, and Industrial and Wringer pair with each other. So family expansion in search will not pull them in by accident. Their family descriptions describe what the probe saw, not circuit facts.
- **Loose members.** 11 models have a confidence under 0.5, listed above. Their second-closest family is in `second_choice`. The nearest calls:
  - Clawthorn: F07 at 0.44;
  - Teemah: F08 at 0.41;
  - DM4 Classic Dist: F06 at 0.40;
  - Hedgehog D9: F07 at 0.40.

### Where the circuit-note labels and the measured families disagree

The agreement with the labels of [distortion-circuits.md](distortion-circuits.md) is above chance but small:

| Label set | ARI | Random grouping, 95th percentile |
|---|---|---|
| 17 confusable groups | 0.13 | 0.035 |
| Family (boost / overdrive / distortion / fuzz / other) | 0.09 | 0.032 |
| Provisional six-way class (not approved) | 0.05 | 0.041 |

- **Groups kept whole:** only C (KWB, Top Secret OD, DM4 Overdrive, all in F01).
- **Same-pedal pairs kept together:** Scream 808 and DM4 Screamer (F06), Deez One Vintage and Mod (F07), DM4 Heavy Dist and Killer Z (F03).
- **Same-pedal pairs split:**
  - Valve Driver (F07) and DM4 Tube Drive (F02);
  - Triangle Fuzz (F03) and DM4 Fuzz Pi (F01);
  - Arbitrator Fuzz (F07) and DM4 Facial Fuzz (F02);
  - Tycoctavia (F14) and DM4 Octave Fuzz (F05);
  - Vermin (F06) against both DM4 Classic Dist (F02) and Ratatouille (F12).

  Step 3 already found the last four pairs as far apart as unrelated models.
- **Label families that the measurement mixes:**
  - **Vermin (a RAT) measures with the Tube Screamers and Klons (F06):** perfectly symmetric, a 0.9 kHz hump, a dark post-clipper low-pass at the centre setting. The other three RAT types go to three other families.
  - **The Big Muff types and the MT-2 types share F03:** high gain, soft, scooped mids. The circuit notes keep them apart (fuzz against distortion). Pocket Fuzz, labelled with the Muffs, measures elsewhere (F07, low confidence).
  - **The legacy DM4 fuzzes Buzz Saw, Jumbo Fuzz and Fuzz Pi sit with low-gain overdrives (F01):** at the library defaults they are lower gain and less scooped than their newer counterparts.
  - **The Tube Screamer group A splits three ways:** Scream 808, DM4 Screamer and Pillars in F06; Dhyana and Horizon in F02; Stupor OD in F08 with the D9, Legendary Drive and Deranged Master (asymmetric, a 1.8 kHz hump). Step 3 already measured Dhyana and Stupor as clearly asymmetric.
  - **Compulsive Drive (labelled asymmetric MOSFET) is in the symmetric family F06,** as step 3's even-share measurement said.
  - **Arbitrator Fuzz (a Fuzz Face type) sits with the DS-1 types and Valve Driver (F07).**

Figures, in `reports/local/distortion-id/families/`:
- `stability_vs_k.svg`: the halves' ARI, the single-resample ARI, PAC and the median member confidence for k = 4 to 16;
- `model_distance_by_family.svg`: 1 minus the co-assignment, ordered by family;
- `family_map.svg`: a 2-D map of the fingerprint distance, coloured by family, flagged models starred;
- `family_map_circuit_labels.svg`: the same map coloured by the circuit-note family.

## Limits

- **The friendliest case.** Everything is measured with the synthetic probe and the dry signal known. Step 4 ([T181](../../board/TASKS.md#t181 "Step 4")) tests the fingerprints on real guitar takes.
- **Tone effect is a response difference, not a listening result.** It says where a tone knob moves the sound most, not how much of that the ear notices. The approved Tone tolerance comes from listening (v4). The tone map only says where the knob is faster or slower than average.
- **Only the "tone" knob of step 3 was measured.** For the legacy three-band models that is Treble, with Bass and Mid at their defaults. Other knobs (Bass, Mid, Presence) were not measured.
- **The families are soft.** They are reproducible as a consensus, but a quarter of the model pairs (PAC 0.24) are neither clearly together nor clearly apart. Treat a family as a shortlist widener, not as a statement about circuits.

## Code, tests and files

- **`src/tonematch/metrics/drive_tone_families.py`** (new):
  - the tone-end settings;
  - the effect measure (`response_block`, `step_effect`, `tone_line_summary`), the shape rules (`knob_shape`), the per-model report and search entry, and the suggestion rule (`suggested_tol`);
  - for the families: the features, the consensus clustering (`consensus_report`, `choose_k_consensus`, `pac`), the Rapp smoothness filter, the naming (`describe_family`) and the label comparison (`label_disagreements`).
- **`scripts/drive_tone_families.py`:** `plan`, `run` (at most 4 hosts from the scaler, at most 3 analysis workers, no batch started above load 9, resumable), `tone`, `families` and `ledger`. Documented in `docs/TOOLS.md`.
- **`tests/test_drive_tone_families.py`:** 16 tests.
  - The settings.
  - Level against shape.
  - The linear, end-loaded, taper, middle-loaded and "little effect" shapes on synthetic knobs.
  - The level-dependence flag.
  - The search entry and the suggestion rule.
  - Consensus clustering recovering clear synthetic families, and the singleton rule.
  - The Rapp fit filter.
  - The family names.
  - The label comparison.
  - Both hand-off files loading with search's own loaders.
- **Files.**
  - Fingerprints in `cache/distortion-fp/dfp-v1-tone-ends/` (gitignored), 418 files.
  - `reports/local/distortion-id/tone-ends/` and `families/` (local).
  - `cache/drive-maps/knob_sensitivity.json` and `cache/drive-maps/drive-families-v1.json`, both measurement-derived and holding model ids, knob names and numbers only.
- **Ledger:** 20 rows dated [2026-10-19T00:11:42](../../ledger/runs.csv?plain=1#L1380 "benchmark · native · distortion-fp-v1-tone-ends · valid_fingerprints = 418.0 (first of 8 rows at this time)") (8 rows) to 00:11:43 UTC.
  - 10 rows `benchmark / native / distortion-fp-v1-tone-ends`;
  - 10 rows `benchmark / - / drive-families-v1`.
- **Re-run:**
  - `nice -n 10 .venv/bin/python scripts/drive_tone_families.py run --hosts 4 --workers 3` (skips stored settings);
  - then `tone` and `families`, which use the stored fingerprints only and take seconds to a few minutes.
