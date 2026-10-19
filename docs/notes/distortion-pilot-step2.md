# Distortion measurement pilot, step 2: results ([T173](../../board/TASKS.md#t173 "Distortion measurement pilot, step 2"))

Owner: `tone-metrics`. Run 2026-10-18 on Helix Native 3.82. Plan: [distortion-identification.md](distortion-identification.md) section 8, step 2. Approved by the human ("yes measure", [DECISIONS](../DECISIONS.md) 2026-10-18), with the five method choices from [distortion-id-steps01.md](distortion-id-steps01.md).

**Nothing here is adopted.** The pass/fail lines are placeholders from the plan, reported as measured. The following all still need the human:

- step 3;
- the circuit-class list;
- every score and every bar.

No song audio was used. The test signal is synthetic, and only small fingerprints were kept, no audio.

## In one paragraph

Six Helix drive models were measured at 9 settings each; the two models with only one knob were measured at 5 settings. The test signal is a 113-second synthetic probe.

What worked:

- **Repeat renders agree exactly.** They are bit-identical, including across two separate sessions.
- **The fuzz is strongly asymmetric**, as expected.
- **Gain rises with Drive** on every model.
- **The measuring method finds the textbook filters:**
  - the Tube Screamer's bass cut sits before the clipping, near 730 Hz at low Gain (the circuit says 720 Hz);
  - the RAT's Filter knob moves a low-pass filter after the clipping, from 8.9 kHz to 750 Hz, while the filter before the clipping stays put;
  - the Big Muff shows its mid scoop at Tone 0.5.

What did not work:

- **The clean boost is not clean.** Kinky Boost compresses 6 to 18 dB and distorts heavily at -6 dBFS.
- **The Tube Screamer's bass-cut corner moves down with Gain** (to 356 Hz at Gain 0.85), unlike the circuit.
- **The "not a simple three-stage circuit" flag tuned in step 1 does not separate the Native models.**

Sweep length: by the rule I fixed before looking, the probe needs the 6 s sweeps, because the germanium fuzz reads differently with 1.5 s sweeps.

## Words used

- **dBFS:** level relative to digital full scale (0 dBFS is the loudest possible sample). "Peak" levels are used throughout.
- **Knee:** the input level at which the output first falls 1 dB short of a straight line (the 1 dB compression point), measured with a 1 kHz tone.
- **Pre-filter / post-filter:** the tone shaping before and after the clipping stage, split apart by the method approved in step 1 (the fundamental's level curves are lined up across frequencies).
- **Small-signal response:** the frequency response for a very quiet input (-78 dBFS), where nothing clips.
- **Saturated response:** the response of the fundamental at 0 dBFS input, where the clipper is fully driven. Its shape mostly shows the post-filter.
- **Even-harmonic share:** how much of the clipping curve's harmonic content is in the 2nd, 4th, ... harmonics. A symmetric clipper has almost none. An asymmetric one (positive and negative peaks clip differently) has a lot.
- **W-H (Wiener-Hammerstein):** the textbook three-stage model: pre-filter, then a clipper with no memory, then post-filter.

## What was measured

### The six models

The names were checked against `match_block.category_pool('drive')` (60 models) and mapped with [distortion-circuits.md](distortion-circuits.md).

| Class asked for | Helix model | Based on | Knobs varied | Settings |
|---|---|---|---|---|
| Tube Screamer type | Scream 808 | Ibanez TS808 | Gain x Tone | 9 |
| RAT type | Vermin Dist | Pro Co RAT | Gain x Filter | 9 |
| Big Muff type | Triangle Fuzz | EHX Big Muff Pi (triangle) | Sustain x Tone | 9 |
| Germanium fuzz | Arbitrator Fuzz | Arbiter Fuzz Face | Fuzz only (the model has no tone knob) | 5 |
| Clean boost | Kinky Boost | Xotic EP Booster | Drive only (see below) | 5 |
| Treble booster | Deranged Master | Dallas Rangemaster | Drive x Treble (Bass at its default) | 9 |

- **A treble booster exists in the pool:** Deranged Master. No substitute was needed.
- **Settings.**
  - Two-knob models: Drive at 0.20 / 0.50 / 0.85 times the tone knob at 0.20 / 0.50 / 0.80. These are Native's own MIDI-control steps 25 / 64 / 108 and 25 / 64 / 102, so every value is exactly what Native plays.
  - One-knob models (Arbitrator, Kinky): Drive at 0, 0.20, 0.50, 0.85 and 1.
- **Kinky Boost's switches could not be set.** Its `Boost` and `Bright` switches exist in the catalog, but the single-block template does not map them, so only Drive was varied.
- **Every other knob** is at the library default, and Level is fixed at the library preset's value.

### The probe (one 112.8 s synthetic signal, 48 kHz)

| Part | What | Why |
|---|---|---|
| Lead-in | 0.5 s of silence | Output noise floor |
| Sweeps | 13 exponential sine sweeps, 20 Hz to 20 kHz, 6 s each with a 1 s gap. Levels: -78 dBFS (the small-signal sweep), then every 6 dB from -66 to 0 | Harmonic-by-harmonic response at each level; the pre/post split. The 6 dB spacing closes the 18 dB gap step 1 found |
| Tone staircases | 1 kHz and 220 Hz, 15 steps of 5 dB from -71 to -1 dBFS (includes -6), 0.3 s per step | The knee (approved choice 1); the static curve read about 10 dB above the knee (choice 2) |
| Two-tone | 440+554, 110+165, 220+330 and 80 Hz + 2 kHz, at 4 levels, 0.5 s each | Intermodulation |
| Bursts | 220 Hz and 1 kHz at -30 / -6 / -30 dBFS, 0.5 s each | Bias drift after a loud note (the memory flag) |
| Short-sweep variant | Lead-in plus the same 13 sweeps at 1.5 s | Sweep-length decision (choice 5) |

### Renders and cost

- **72 Native renders in total:**
  - 46 settings;
  - 2 repeats;
  - 12 short-sweep renders (each model at its low and its high Drive, tone in the middle);
  - 12 re-renders of those same 12 settings in a second session. These were added after I found my first ringing measure was flawed (see "Sweep length"). They also gave a second, cross-session repeat check and a look at the Kinky Boost's output noise.
- **0 failed renders.**
- **Time and load.**
  - The main run took 370 s on the wall clock. Of that, the renders took 65 s; the scaler granted 1, then 2, then 3 hosts.
  - The recheck took 102 s.
  - The 1-minute load average stayed at 4.2 or below. `proc_check` found no orphans before or after.
- **Storage.** 60 fingerprints, 6.6 KB each on average, 456 KB in all, plus a 107 KB summary. That is twice the 3 KB the plan expected. Most of it is the float16 response tables for all 13 sweep levels and some per-level lists in the scalars. It could be cut to about 3 KB by dropping what can be recomputed.

## Pass/fail, as measured

| Check (placeholder) | What I measured | Verdict |
|---|---|---|
| **Repeat renders agree within the Native spread** | Both in-session repeats (Scream 808 centre, Triangle Fuzz high Sustain) are bit-identical: the waveform difference is exactly zero. All 12 second-session re-renders give byte-identical fingerprints. The Native spread is zero. For scale, the nearest other setting of the same model differs by 0.31 dB (Scream 808) and 1.31 dB (Triangle Fuzz), median over the response table. | **Met** |
| **The boost shows no clipping up to about -6 dBFS** (my line: under 1 dB of compression and under 1% distortion, -40 dB, at -6 dBFS, 1 kHz) | Kinky Boost, all 5 Drive settings: compression 6.1 to 17.8 dB, total harmonic distortion -9.1 to -6.5 dB (35 to 47%), knee -14.8 dBFS (Drive 0) down to -24.9 dBFS (Drive 1). Its clipping is asymmetric too: even share -9 to -11 dB, positive peaks reach 1.4 to 1.7 times further than negative ones. | **Not met, 0 of 5** |
| **The fuzz shows a large even-harmonic share** (my line: at least -20 dB; the asymmetric diode in step 1 read -22.8) | Arbitrator Fuzz, all 5 settings: -6.5 dB (Fuzz 0 and 0.2), -11.8 (0.5), -15.3 (0.85), -17.3 (1). Peak ratio 0.47 to 0.91. For contrast, the Scream 808 and Vermin read -121 to -144 dB, which is numerically zero. | **Met, 5 of 5** (the share falls as Fuzz rises) |
| **Gain (g0) rises with Drive** (my line: more than 0.5 dB per Drive step, in every tone series) | 14 of 14 series rise. At 1 kHz, Drive 0.2 / 0.5 / 0.85: Scream 808 10.8 / 15.5 / 25.0 dB; Vermin 11.7 / 32.3 / 44.3; Triangle 13.3 / 18.8 / 26.0; Deranged Master -5.3 / 11.0 / 30.3. One-knob, Drive 0 / 0.2 / 0.5 / 0.85 / 1: Arbitrator 18.5 / 20.1 / 22.6 / 29.8 / 32.6; Kinky 0.5 / 1.5 / 9.5 / 11.9 / 13.0. | **Met** |
| **TS: bass cut before the clipping, near 720 Hz** (my line: a first-order high-pass fit to the pre-filter between 480 and 1080 Hz; at least 6 dB down at the lowest frequency the split can read; the post-filter not cut there) | Pre-filter high-pass fit 730 Hz at Gain 0.2, 564 Hz at 0.5 and 356 Hz at 0.85. At 250 Hz the pre-filter reads -8.1 dB against 1 kHz; a 720 Hz high-pass predicts -7.9. The post-filter is +3.4 to +4.9 dB at the same frequencies, so the cut is before the clipping, not after. | **6 of 9**. Fails at Gain 0.85: the Helix corner moves down with Gain; the circuit's does not. |
| **TS: mid hump** (my line: small-signal peak between 400 Hz and 2.5 kHz, at least 6 dB above 100 Hz and 3 dB above 10 kHz) | Peak at 794 to 891 Hz, 10.6 to 13.0 dB above 100 Hz, 16.7 to 21.7 dB above 10 kHz. The post-filter low-pass fit is 651 to 868 Hz; the circuit note gives 723 Hz for the tone low-pass. | **9 of 9** |
| **RAT: the filter is a post low-pass that moves with Filter** (my line: the post-filter corner moves the same way at every Gain, by at least a factor of 2, and further than the pre-filter corner) | Post-filter low-pass fit 8,934 / 2,518 / 752 Hz at Filter 0.2 / 0.5 / 0.8. Identical at all three Gain values, a factor of 11.9. The pre-filter corner does not move with Filter at all: 548 / 231 / 56 Hz at Gain 0.2 / 0.5 / 0.85, so it moves with Gain, as the RAT gain leg does. The saturated response agrees: 10.1 / 3.0 / 0.85 kHz. | **Met, 3 of 3 Gain values** |
| **Big Muff: mid scoop** (my line: a dip of at least 3 dB between 300 Hz and 3 kHz in the saturated or small-signal response) | At Tone 0.5: a 3.1 to 3.3 dB dip at 891 Hz in the saturated response (3.4 to 3.5 dB in the post-filter), at all three Sustain values. My arithmetic from the circuit gave 1.14 kHz. At Tone 0.2 the tone stack is a low-pass (post-filter corner about 710 Hz); at Tone 0.8 it is a high-pass. That is what a low-pass/high-pass blend should do. | **Met at Tone 0.5** (3 of 3). Not at Tone 0.2 or 0.8, as expected. |
| **Fuzz asymmetry** (even share at least -20 dB, or peak ratio outside 0.8 to 1.25) | See the fuzz row above. | **Met, 5 of 5** |
| **The boost stays clean** | See the boost row above. | **Not met** |
| **Sweep length, decided from measured Native ringing** | Rule fixed before the subset was compared: 1.5 s is enough only if, on every subset setting, (a) the 1.5 s and 6 s tables agree within 0.5 dB (95th percentile, orders 1 to 5, only where the order carries energy), and (b) the leakage a 1.5 s sweep would add, measured on the 6 s record, is at most -30 dB (at most a 0.27 dB error). Met on 10 of 12 settings. Fails: Arbitrator Fuzz at both settings, and Vermin at Gain 0.85 on (b) only (details below). | **6 s** by the rule |

### Sweep length, the evidence

- **The method's own floor.** A probe passed through unchanged shows -44 dB of leakage, from the band edges of the sweep.
- **Agreement between 1.5 s and 6 s sweeps,** worst order, 95th percentile:

  | Model | Agreement | Worst order |
  |---|---|---|
  | Scream 808 | 0.03 dB | |
  | Vermin | 0.03 dB | |
  | Triangle Fuzz | 0.09 to 0.15 dB | |
  | Kinky Boost | 0.12 to 0.19 dB | |
  | Deranged Master | 0.26 to 0.34 dB | order 4 |
  | Arbitrator Fuzz | 0.56 to 0.74 dB | orders 4 and 5; worst single bins 1.5 to 2.3 dB |

- **Leakage a 1.5 s sweep would add** (95th percentile, orders 1 to 4):

  | Model | Leakage |
  |---|---|
  | Scream 808 | -36 to -56 dB |
  | Triangle Fuzz | -44 to -47 dB |
  | Deranged Master | -50 to -61 dB |
  | Kinky Boost | -35 to -42 dB |
  | Vermin, Gain 0.2 | -30 to -35 dB |
  | Vermin, Gain 0.85 | -26 to -32 dB, even though its tables agree to 0.03 dB |
  | Arbitrator Fuzz | -13 to -29 dB |

- **Reading.** Most Native drives ring for much less than the 1.5 s gaps, so the shorter sweep would serve 4 of the 6 classes. The germanium fuzz is the exception. It is also the model whose burst test shows the most memory, so its difference between sweep lengths may come from its bias responding to how fast the sweep moves, rather than from ringing. Either way, the rule says 6 s.
- **A correction on the way.** My first ringing measure ("last time the response is above -40 dB") picked up the neighbouring, stronger harmonic. It is replaced by the leakage measure above, which has its own unit tests. The old numbers are still in the 60 stored fingerprints, but nothing reads them any more.

## Surprises

1. **The "clean boost" clips.** Kinky Boost's knee is at -15 to -25 dBFS. At -6 dBFS input it is 6 to 18 dB compressed, with 35 to 47% distortion and asymmetric peaks. It also has a DC offset that moves with the signal:
   - -0.0064 at rest, about -44 dBFS, which is what first read as a "noise floor";
   - the offset after the first sweep is -65 dBFS at Drive 0.2 and -45 dBFS at Drive 0.85.

   Every other model outputs exact digital silence. Either the Helix model really saturates (the EP-3 is a preamp), or Native's full scale is a very hot guitar level. Which matters depends on how loud real DI takes are inside Native. That was not measured here.
2. **Most Native drives are perfectly linear up to a threshold.** On Scream 808, Vermin, Arbitrator and Deranged Master:
   - below a certain level the 2nd and 3rd harmonics sit at -130 to -160 dB relative to the fundamental, which is numerical zero;
   - within a single 5 dB step they jump to about -30 dB.

   A real diode starts to bend gradually. The Big Muff is the exception: its 3rd harmonic is already -37 dB at -71 dBFS. So on Native the "knee" is a sharp switch-on level, and the level where the harmonics switch on is a crisp number to fingerprint: -41 to -56 dBFS on the Scream 808, -41 to below -71 on the Vermin, -41 on the Deranged Master. (On the Kinky Boost the moving DC offset blurs it.)
3. **The Scream 808 Tone knob does almost nothing on Native.** Across Tone 0.2 to 0.8, the small-signal response changes by at most 2.2 dB and the saturated response by at most 1.6 dB. The old probe-v1 fingerprints agree: the 4 kHz versus 1 kHz ratio moves by only 0.6 dB. Either Helix's TS tone is subtle, or the parameter does not fully reach Native through the template. I did not check which; it is a question for `native-engine` or the ear.
4. **On the Deranged Master, Drive does not move the knee.** The knee stays at -38.4 to -39.1 dBFS for every setting, while the small-signal gain rises 36 dB. So in this model Drive acts after the point where it clips, more like an output level. Treble moves the pre-filter corner (632 to 1,262 Hz). The circuit note's "high-pass near 1 kHz before the transistor" is confirmed: -22 dB at 62 Hz against 1 kHz.
5. **The two highest-gain settings clip below the -71 dBFS tone staircase.** The Vermin at Gain 0.85 and the Triangle Fuzz at Sustain 0.85 are already 2.5 dB and 1.2 dB compressed on the first step. The sweeps, which start at -78 dBFS, put their knees at -76 and -73 dBFS. The approved "5 dB staircase as the knee source" needs to start lower for high-gain models, or hand over to the sweep knee.
6. **The step-1 flag for "not a simple three-stage circuit" does not carry over.** On synthetic circuits the odd-harmonic residual was at most 0.10 dB for true three-stage circuits and at least 0.60 for the others. On Native it is a continuum:

   | Model | Residual |
   |---|---|
   | Vermin | 0.05 to 0.13 dB |
   | Scream 808 | 0.10 to 0.23 dB |
   | Deranged Master | 0.14 to 0.34 dB |
   | Triangle Fuzz | 0.19 to 0.24 dB |
   | Arbitrator Fuzz | 0.39 to 0.67 dB |
   | Kinky Boost | 0.46 to 0.92 dB |

   The Big Muff, the one circuit we know is two clippers in a row, does not stand apart from the Tube Screamer.
7. **Memory flags.** The burst test flags all four transistor and boost models (Triangle Fuzz, Arbitrator, Kinky Boost, Deranged Master) and neither op-amp model (Scream 808, Vermin).
8. **The hardness fit is unstable on asymmetric curves.** On the fuzz and boost, the soft-to-hard "Rapp" exponent fitted to one side jumps to its limits (0.2 or 200). The hardness number is not usable there as it stands.

## Code, tests, files

- `src/tonematch/metrics/distortion_probe.py`:
  - the probe (`build_probe`);
  - the fingerprint (`analyse`, `pack`, `unpack`);
  - the sweep-leakage measure (`leakage`);
  - the corner, scoop and hump readers;
  - the first-order fits;
  - the sweep-referenced knee.

  It builds on `distortion_id.py` from step 1.
- `scripts/distortion_pilot_step2.py`: `plan`, `run`, `recheck` and `summarise`. It renders through `NativeRenderPool` on the single-block library templates, at most `--hosts`, leased from the host scaler. It is documented in `docs/TOOLS.md`.
- `tests/test_distortion_probe.py`: 8 tests on a known synthetic circuit. They cover:
  - the probe layout;
  - pre-filter corner, post-filter corner, symmetry and knee recovery;
  - asymmetry and a clean gain;
  - the fingerprint round trip, small and with no audio;
  - the leakage floor and a 50 ms echo;
  - two-tone intermodulation of a cubic;
  - the corner, scoop and fit helpers.

  These plus the 12 step-1 tests pass (20 in 24 s). `tests/test_tools_doc.py` still fails, only because `rating_v3.py` (`benchmark`, in progress) is not yet listed.
- **Fingerprints** (local, gitignored): `cache/distortion-fp/dfp-v1-pilot/<model>/<setting>.npz`, with `index.json` beside them.
- **Summary** (local): `reports/local/distortion-id/pilot/summary.json` and `recheck.json`.
- **Figures** (local), three per model:
  - `<model>_curve.svg`: the static curves;
  - `<model>_harmonics.svg`: gain and harmonics against level;
  - `<model>_eq.svg`: the pre- and post-filters and the small-signal response.
- **Ledger:** 9 rows, `benchmark / native / distortion-pilot-v1`, dated [2026-10-18T21:01:40](../../ledger/runs.csv?plain=1#L1297 "benchmark · native · distortion-pilot-v1 · valid_fingerprints = 60.0 (first of 9 rows at this time)") (9 rows).
- **Re-run:**
  - `nice -n 10 .venv/bin/python scripts/distortion_pilot_step2.py run --hosts 3` (60 renders), then `... recheck --hosts 3` (12 renders);
  - `... summarise` rebuilds the summary from the stored fingerprints with no renders.

## Decisions for the human before step 3

1. **Sweep length.**
   - The rule says 6 s. That keeps the probe at 113 s.
   - 1.5 s would cut about 70 s from every render. It would be fine for 4 of the 6 classes, but it reads the germanium fuzz 0.5 to 0.7 dB differently on harmonics 4 and 5.
   - Accept the 6 s, or accept 1.5 s with that known error on fuzz.
2. **Where the staircase starts.** -71 dBFS is not low enough for the highest-gain settings. Options:
   - start at -86 dBFS (3 more steps, about 1 s more);
   - use the sweep knee when the first step is already compressed.
3. **The boost expectation.** The Native "clean boost" clips from about -15 to -25 dBFS. Options:
   - accept this as a property of the model and drop the check;
   - restate it as "clean up to the level real DI takes reach in Native". That needs that level measured first; I can measure it from the human's own takes without any song audio.
4. **The "not a simple three-stage circuit" flag.** As tuned on synthetic circuits it does not separate the Native models. Options:
   - drop it;
   - keep the raw residual as one fingerprint number;
   - re-tune it on Native data. This risks tuning to these six models.
5. **What the fingerprint keeps.** At 6.6 KB per setting, step 3's roughly 2,560 settings come to about 17 MB. Accept that, or trim to about 3 KB. Also: add the harmonic switch-on level (surprise 2) as a stored number.
6. **The Scream 808 Tone question.** Before step 3 builds a knob map on it, should `native-engine` check that Tone reaches Native fully, or should the human compare Tone 0 and Tone 1 by ear?
7. **One-knob models and unmapped switches.** For Kinky Boost's Boost and Bright, and the Kinky and Arbitrator settings plan generally:
   - keep 5 Drive points for one-knob models;
   - decide whether the unmapped switches should be measured through another route.
8. **Whether to run step 3 at all,** and on which models (all 61 drives, or the 12-model core set first).
   - Cost estimate: with 6 s sweeps, about 1.1 s of rendering per setting at 3 hosts, plus about 6 s of analysis per setting.
   - The 732-setting screen would take about 15 minutes of rendering. Analysis would take 75 minutes on one core, or about 25 to 35 minutes if the analysis runs on 2 or 3 cores. That change is not made yet.
9. **The class list.** Still unapproved. The pilot suggests which signatures are measurable and separate cleanly on Native:
   - linear up to a threshold, or nonlinear at every level;
   - symmetric or asymmetric;
   - whether the main filter sits before or after the clipping;
   - memory, or none.

   Which classes to use remains the human's call.
