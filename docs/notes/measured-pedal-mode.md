# Measured-pedal mode: the capture tool and a simulation of the real-world nuisances ([T187](../../board/TASKS.md#t187 "Measured-pedal mode preparation"))

*Written 2026-10-18 by stomp-engine, in the 12-hour unattended run. **Preparation and measurement only.** The human has not approved measured-pedal mode. Nothing here is adopted, and nothing here ran on the pedal: the Stomp's control channel was not opened, and no audio went through it. Tools: `scripts/pedal_probe.py` (capture), `scripts/pedal_probe_sim.py` (simulation). Library code: `src/tonematch/pedal_probe.py`. Tests: `tests/test_pedal_probe.py`. Ledger: `benchmark / native / pedal-probe-sim-v1`.*

## The short answer

**The tool is ready for a first session with the human. The mode itself is the human's decision.**

**The simulation.** 54 Native drive models at hidden off-grid knob settings stand in for real pedals: the step-4 targets, one per model. Each one's probe was put through simulated interface nuisances and matched against the 54-model library.

| Condition (54 targets) | Model top-1 | Family top-1 | Drive OK (±0.15) | Tone OK (±0.1) |
|---|---|---|---|---|
| Probe straight into the model (reference) | **1.00** | 1.00 | 0.94 | 0.90 |
| **The prepared pipeline**: L1 + L2 done (input 1 dB off, noise -80 dBFS, tilt corrected, 2 samples of latency), noise mask | **0.96** [0.91, 1.00] | 0.96 | 0.85 | 0.90 |
| The same with no mask | 0.87 | 0.91 | 0.85 | 0.85 |
| L1 only, no L2: input 6 dB off; mask + software level search | 0.96 | 0.96 | 0.72 | 0.90 |
| L1 only, no L2: input 6 dB off; mask only | 0.89 | 0.91 | **0.50** | 0.85 |

[brackets: 95% interval, resampling models]

**What matters, in order:**
1. **The input level the pedal sees.**
   - The model is named through ±6 dB (0.94 to 1.00), but Drive moves about 0.02 per dB: within tolerance 0.94 at 0 dB, 0.54 at +6 dB, 0.31 at +12 dB.
   - Only L2 (Send into the guitar input) measures it. A software level search gets the model back at ±12 dB, but it costs Drive accuracy whenever the level is already right.
2. **Noise.**
   - At -70 dBFS the model drops to 0.70. A noise take and a mask bring it back to 0.87.
   - At -80 dBFS the mask gives 0.96. Averaging 4 takes lowers noise by 6 dB.
3. **Interface tilt.**
   - After the pedal it costs Tone (0.90 to 0.62 at ±2 dB/decade), and the loopback correction removes it exactly.
   - Before the pedal it costs Tone less (0.81), and no correction helps.
4. **A low pedal input impedance.** With a pickup-like source, a 10 kΩ load costs model 0.83 and Tone 0.48 (the misses stay in the family); 47 kΩ costs Tone 0.77; 220 kΩ and 1 MΩ cost nothing. The Stomp's Send is not a pickup, so this is an upper bound on what source loading can do.
5. **Latency and return level do not matter.** Up to 1207 samples (the whole USB round trip) left untrimmed changes nothing. A Return clipping at +12 dB lost 1 of 54.

**A pedal that is not in the library** (each model left out in turn):
- The nearest remaining model is in the true family 56% of the time (chance 13%); the family is in the top 3 85% of the time.
- Clean, the distance flags "not in the library" well (AUC 0.98). With -80 dBFS noise it does so poorly (AUC 0.78).

**Hardware not used.** The control channel was never opened and no audio went through the Stomp. **Renders:** 810 new Native probe renders on 1 host, 0 failed; 1,782 fingerprints; no audio kept.


## 1. The idea

Step 4 ([distortion-step4.md](distortion-step4.md)) found that the dfp-v1 probe, played straight into a Native drive model, names the model every time (90 of 90), and reads Drive within ±0.15 98% of the time and Tone within ±0.1 86% of the time. Guitar playing is what made identification hard.

So, for a real pedal the human owns: play the same probe through the pedal once per setting, and match that fingerprint against the Native library. The HX Stomp is the audio interface; its FX loop puts the pedal in the path.

## 2. Design

### 2.1 Signal path (edit buffer only)

```
host USB out 5/6 -> Input block (USB 5/6) -> FX Loop 1 (Send 0 dB, Return 0 dB, Mix 100 %, trails off)
     -> SEND L jack -> the pedal -> RETURN L jack -> Path A output -> host USB in 1/2
```

The tool also records host USB in 5/6 (the dry guitar input, used by L2) and 7/8 (the dry returns, a check).

### 2.2 The template preset, and why it is loaded in place

- **The block.** `HD2_FXLoopMono1` ("FX Loop 1": Send L and Return L) goes into the first slot of the current edit buffer that holds a single block. On 04C "Smoke Known" that is the Ping Pong delay at device slot 3. The Amp+Cab at slot 2 is bypassed, and the Input is routed to USB 5/6.
- **The compiler refuses it.** `stompd compile` was tried offline on the 04C backup (it never opens USB). It answered `model 141: Send/Return blocks are not built in v0`. So a whole-preset write is not possible today. `pedal_probe.py template --compile` repeats this check.
- **So the load is in place**, with `StompEngine(whole_preset="off")` and `plan_load`, as the engine already does for model changes:
  1. the device's own `model` command on slot 3;
  2. Send, Return and Mix set as parameters;
  3. the amp bypassed;
  4. `route_key` to USB 5/6;
  5. the full read-back check.
- **Nothing is saved.** No flash write, no rename, no whole-document write. `close()` re-selects 04C, compares the chain with the one recorded at open, and reports `restore_report` (with `verify_bytes`, the buffer is backed up again and compared).
- **First-run risk.** The `model` command has built Distortion, Delay, Reverb, Dynamics, EQ and Modulation blocks before. It has never built an FX Loop (category "other", native category 12). If the device refuses, the read-back raises, and `close()` restores the buffer.

### 2.3 Calibration

| Step | Cables | Measures | Divided out / used for |
|---|---|---|---|
| **L1 loopback** (once per session) | SEND L -> RETURN L | latency (3 impulses, must agree within 2 samples); loop gain at 1 kHz; small-signal response on the 49-point grid (the "interface tilt"); noise floor; worst harmonic on the 1 kHz staircase; clipping; the probe's residual lag after the trim | latency trim; the gain; the response, when it is not flat within ±0.25 dB |
| **L2 input scale** (optional, recommended) | SEND L -> GUITAR INPUT | the level the guitar input's converter sees, from the -16 dBFS 1 kHz step | sets the FX Loop **Send** so the pedal sees the probe at the level a Native model sees it |
| **Noise take** (per setting, optional) | pedal in the loop | the pedal's output with silence instead of the probe, analysed like a capture | the noise mask (section 4.2) |

- **L1 runs at Send -6 dB.** With a unity loop, the probe's 0 dBFS sweeps arrive exactly at the Return's full scale. The fake-device test caught this: any tilt pushed them over. The Send block is a digital gain, so the 6 dB is put back exactly, and the stored loop gain is normalised to Send 0 dB.
- **Why L2 exists.** The loopback measures Send × Return only; it cannot say how loud the probe is at the pedal's input. That input level is what sets the clipping, and the simulation shows it trades directly against the Drive reading (section 4.1).
  - Native's 0 dBFS is, by design, the Helix guitar input's 0 dBFS. So the Send that makes the guitar input read the probe at its own level puts the pedal on Native's scale.
  - Two assumptions here: the Stomp's guitar input matches Native's input scale (pad off), and the guitar input's impedance does not load the Send. The tool reports the number. The human decides whether to trust it.
- **Corrections, in order:**
  1. trim by the L1 latency;
  2. divide by the loop gain;
  3. divide by the loop response, applied entirely on the output side;
  4. average the repeats (noise falls 3 dB per doubling).
- An input-side tilt cannot be removed this way: the pedal's clipping sits between the two sides.
- **QA on every capture:** peak and clipping, and the probe's residual lag after the trim.

### 2.4 Signals and time

| Signal | Length | What it is |
|---|---|---|
| calibration signal | 116.7 s | 0.5 s lead-in, 3 impulses 0.25 s apart, then the dfp-v1 probe |
| capture | 115.2 s per repeat | the dfp-v1 probe (layout "v1") |
| noise take | 115.2 s | silence of the probe's length |

A session is about 4 minutes for L1 + L2, then about 2 to 4 minutes per pedal setting.

### 2.5 Output

- **Format.** `cache/distortion-fp/pedals/<pedal>/<setting>.npz`, packed like every dfp-v1 fingerprint (`distortion_probe.pack`: float16 arrays, scalars as JSON, no audio).
- **Index.** `index.json` has the dfp-v1 index shape. Each entry has `model` "PEDAL:<name>", an empty `cc`, `knobs` (the clock positions the human wrote down), the loopback date, the corrections, quality, and `tag` "measured-pedal". The tag means `distortion_step4.load_library` never mixes a pedal into the Helix library (tested).
- **Identification** (`pedal_probe.py identify`):
  - the level-free curves against the 54-model library (dfp-v1 + tone ends);
  - the nearest models with their [T180](../../board/TASKS.md#t180 "Circuit families from the measured fingerprints, circuit names as…") families;
  - the knob positions read off each model's map.
- **What the knob readings are.** They are positions of the HELIX model's knobs that best explain the pedal. They are not the pedal's own knobs.

### 2.6 What was tested without the pedal (`tests/test_pedal_probe.py`, 17 tests)

- **Template.** The slot choice; the preset; the plan (slot 3, bypass slot 2, model command, no flash).
- **The in-place load on a fake 04C bridge.** These commands are sent: `model` FX Loop at slot 3, Send/Return/Mix set, amp bypassed, USB 5/6 routing. No save, rename or whole write is sent. `close()` restores the chain.
- **The compiler refusal.** A fake compiler refuses, and the tool takes the in-place route. A local test runs the real compiler offline on the 04C backup.
- **`--human` guard.** Without it, `loopback` and `capture` exit and never open the engine.
- **The maths.** Latency from impulses; cross-correlation lag; trim. The tilt and pickup-load filters, checked against sine measurements; noise level; averaging; inverting the response correction.
- **LoopCal problem flags.**
- **The level shift.** Identity at 0 dB, and exact on a linear system. Also the noise mask and the noise floor.
- **Fingerprint files.** Write and read back, replace in the index, and stay out of the Helix library.
- **End to end on a fake loop.** Latency 1207, gain -0.4 dB, tilt +1.5 dB/decade, a guitar input at -1.5 dB, and a toy Tube-Screamer pedal.
  - L1 recovers the latency exactly, the gain within 0.05 dB and the tilt within 0.15 dB, with no clipping.
  - L2 recovers -1.5 dB within 0.02.
  - The corrected capture's fingerprint matches the toy pedal without the loop: gain within 0.1 dB, knee within 0.5 dB, response within 0.5 dB.
- **`pedal_probe.py selftest --identify`** runs the same pipeline from the command line. The toy pedal's nearest models are Vermin Dist and Pillars, both in the Tube-Screamer family. Its distance is 0.79, against a median of 0.12 for an in-library model at a hidden setting (section 4.5).

## 3. The human's checklist

`pedal_probe.py checklist` prints the same text.

**Before you start**
1. Quit HX Edit. Select the scratch preset 04C "Smoke Known".
   - The tool changes only the EDIT BUFFER: it turns 04C's delay slot into an FX Loop 1 block, bypasses the amp, and routes the Input to USB 5/6.
   - It puts the buffer back at the end. It never saves. Do not press Save yourself.
2. Turn the HX Stomp's big VOLUME knob fully down, and take off headphones. The probe's sweeps reach 0 dBFS, and they also come out of the main outputs. (USB recording is not affected by the knob; the L1 gain check confirms it.)
3. Global Settings > Ins/Outs:
   - if Send/Return offers Instrument or Line, choose Instrument;
   - Guitar In pad off.

   Note what you chose; it is part of the calibration.
4. Short instrument cables. Power the pedal from its usual supply, and note whether it is a battery or a supply.

**Step L1, the loopback (once per session, about 2 minutes)**
5. A cable from SEND L straight to RETURN L. Nothing else in the loop.
6. Run `.venv/bin/python scripts/pedal_probe.py loopback --human --l2`. It checks:
   - that the latencies agree;
   - loop gain near 0 dB;
   - a flat response;
   - no distortion;
   - no clipping.

**Step L2, the input scale (2 more minutes; recommended, see section 4.1)**

7. When asked, move the cable to run from SEND L to the Stomp's GUITAR INPUT. The tool measures what the guitar input sees and sets the Send so the pedal gets the probe at the level a Helix model gets it.

**For each pedal setting (about 2 minutes, plus 2 more for the noise take)**
8. Cables: SEND L to the pedal's INPUT, the pedal's OUTPUT to RETURN L. Switch the pedal ON (effect engaged).
9. Write down every knob as a clock position (7:00 = fully left, 12:00 = noon, 5:00 = fully right), any switches, and the pedal's name and version.
10. Run `.venv/bin/python scripts/pedal_probe.py capture --human --pedal "NAME" --setting "D 2:00 T 12:00 L 10:00" --knob drive=2:00 --knob tone=12:00 --knob level=10:00 --noise-take`.
    - If it reports clipping, turn the pedal's Level down and repeat. The curves ignore the output level.
    - If the noise take is above about -80 dBFS, add `--repeats 2` (or 4).
11. Optional: one take with the pedal switched OFF. It should look like the loopback.

**After**
12. The tool restores the edit buffer and the selected preset, and says whether the restore matched. Unplug the loop cables. Nothing on the pedal was saved or changed.


## 4. The simulation

**Stand-ins.** 54 of the step-4 targets, one per model (the lowest target id of each). Each is a Native drive at a hidden Drive and Tone between the measured grid points. The probe never uses the step-4 DI excerpts, so the step-4 dev/test split does not apply here; the model is the unit, and the intervals resample models.

**Library.** The 54-model pool, dfp-v1 plus the [T179](../../board/TASKS.md#t179 "Tone-knob ends at 0/0.1/0.9/1 on all drive models") tone ends (1,032 settings), matched on the level-free curves (`distortion_step4`).

**Scoring.**
- Drive and Tone OK are read off the TRUE model's map, as in step 4.
- "model+knobs" needs the right model first, with both knobs in tolerance.
- Families are [T180](../../board/TASKS.md#t180 "Circuit families from the measured fingerprints, circuit names as…")'s 15.

**Fixes, set before any result was seen:**
- the mask margin (6 dB over the noise take);
- the level-search grid (-15 to +15 dB in 1.5 dB steps; the 3 best by the cheap nearest-setting distance are ranked in full).

The choice between mask and floor below is made on the same 54 targets, and is flagged as such.

**Conditions.**
- **Input side** (rendered on Native, 15 renders per target):
  - the probe 1, 3, 6 and 12 dB louder or quieter;
  - a pickup-like source (6.5 kΩ + 2.5 H) into a 10 k, 47 k, 220 k or 1 MΩ load: first-order low-passes at 1.05, 3.4, 14.4 and 64 kHz;
  - ±2 dB/decade of tilt.
- **Output side** (on the base render):
  - white noise at -90, -80 and -70 dBFS RMS;
  - ±2 dB/decade of tilt, raw and divided by the simulated loopback (the tilted probe through the bare loop, measured by the tool's own `loop_from_fingerprint`);
  - 2, 48, 480 and 1207 samples of latency left in;
  - the return 6 and 12 dB hot, clipped at 0 dBFS.
- **Combinations:**
  - `real_cal`: after L1 and L2, the input 1 dB off, a +0.5 dB/decade tilt corrected, noise -80, 2 samples of latency;
  - `real_l1`: after L1 only, the input 6 dB off, a +1 dB/decade tilt corrected, noise -80;
  - `real_l1_n70`: the same with noise at -70.
- **Noise takes:** the same output-side operations on silence.

### 4.1 Input level (the send scale)

| Input level off by | Model top-1 | Drive OK | Tone OK | Median Drive shift | With level search: model / Drive |
|---|---|---|---|---|---|
| 0 | 1.00 | 0.94 | 0.90 | 0 | 1.00 / 0.89 |
| -1 / +1 dB | 1.00 / 1.00 | 0.94 / 0.89 | 0.85 / 0.92 | | 1.00 / 0.76, 1.00 / 0.81 |
| -3 / +3 dB | 1.00 / 1.00 | 0.87 / 0.74 | 0.85 / 0.96 | -0.07 / +0.08 | 0.98 / 0.76, 1.00 / 0.70 |
| -6 / +6 dB | 0.94 / 0.98 | 0.61 / 0.54 | 0.88 / 0.85 | -0.12 / +0.12 | 0.98 / 0.70, 0.98 / 0.63 |
| -12 / +12 dB | 0.76 / 0.83 | 0.33 / 0.31 | 0.73 / 0.73 | -0.24 / +0.22 | 0.98 / 0.81, 0.94 / 0.63 |

- **Knob and level trade off.** Input level and Drive trade off almost one for one: about 0.02 of Drive per dB. A drive's Drive knob is mostly gain in front of the clipper, so a pedal hit 6 dB harder looks like the same pedal with Drive 0.12 higher.
- **The level search cannot separate them.** Its level estimate is off by a median of 1.5 to 3 dB. It rescues the model at large offsets, but at small offsets it costs Drive (0.94 to 0.76 at ±1 dB).
- **A second limit.** High-gain models already compress at the probe's quietest step (-78 dBFS), so a fingerprint shifted upward cannot be normalised exactly.
- **So L2 is the fix for the knobs.** The level search is a fallback for naming the model when L2 was not done.

### 4.2 Noise floor

| Noise (dBFS RMS) | none: model / Drive / Tone | mask: model / Drive / Tone | floor: model / Drive / Tone |
|---|---|---|---|
| -90 | 0.89 / 0.91 / 0.83 | 0.96 / 0.91 / 0.83 | 0.98 / 0.91 / 0.81 |
| -80 | 0.85 / 0.85 / 0.85 | 0.96 / 0.87 / 0.85 | 0.94 / 0.93 / 0.83 |
| -70 | 0.70 / 0.78 / 0.79 | 0.87 / 0.87 / 0.75 | 0.91 / 0.91 / 0.69 |

- **What noise hurts.** It fills the quiet staircase steps and the harmonic tables, where a clean model reads -100 dB. Even -90 dBFS costs 6 models in 54.
- **The noise take is the fix.** The **mask** drops every entry within 6 dB of the noise take's own reading. The **floor** raises the capture and every library entry to the noise level, shifted by the gain difference, and rebuilds the library per query (about 2 s).
- **Mask or floor.** The two are within a target or two of each other. The mask is simpler and is what `identify` uses; this choice was made on these same targets.
- **Averaging.** 2 takes lower the noise by 3 dB, 4 by 6 dB.

### 4.3 Interface tilt, latency, return level, input loading

| Condition | Model | Drive OK | Tone OK |
|---|---|---|---|
| tilt after the pedal +2 / -2 dB/decade | 1.00 / 1.00 | 0.89 / 0.93 | 0.62 / 0.67 |
| the same, divided by the loopback | 1.00 / 1.00 | 0.94 / 0.94 | **0.90 / 0.90** (= reference) |
| tilt before the pedal +2 / -2 | 1.00 / 1.00 | 0.91 / 0.91 | 0.81 / 0.81 |
| the same, "corrected" by the loopback | 1.00 / 1.00 | 0.93 / 0.89 | 0.81 / 0.77 |
| latency 2 / 48 / 480 / 1207 samples | 1.00 | 0.94 | 0.90 / 0.90 / 0.88 / 0.88 |
| return +6 / +12 dB, clipped at 0 dBFS | 0.98 / 0.98 | 0.96 / 0.94 | 0.92 / 0.88 |
| load 1 MΩ / 220 kΩ / 47 kΩ / 10 kΩ | 1.00 / 1.00 / 1.00 / 0.83 | 0.94 / 0.93 / 0.93 / 0.81 | 0.88 / 0.92 / 0.77 / 0.48 |

- **Tilt.** The loopback correction makes a tilt after the pedal harmless. A tilt before the pedal stays (Tone 0.81), because the clipping sits between the two sides.
  - This is why the tool checks the loop's flatness, and why the correction assumes the output side.
  - A real Stomp's converters should be flat to a fraction of a dB, and L1 will show it.
- **Latency and return level.** Neither matters for the fingerprint. The impulses are kept for QA. One loud model (Regal Bass DI) was lost when the return clipped; the tool flags clipping.
- **10 kΩ load.** 9 models are missed, mostly by a near neighbour: the true model is second in 7 of the 9 cases and third in one (Scream 808 to Minotaur, Screamer to Overdrive, Tube Drive to Color Drive). Ratatouille falls to 30th. This loss is what source loading could do to a fuzz-like input. The Stomp's Send is a low-impedance source, so it is not expected; a pedal whose sound depends on a guitar's impedance is a limit of the method, not a calibration step.

### 4.4 Combined, and which calibration fixes what

| Pipeline | fix | Model | Family | Drive OK | Tone OK | Model+knobs |
|---|---|---|---|---|---|---|
| after L1 + L2 (`real_cal`) | none | 0.87 | 0.91 | 0.85 | 0.85 | 0.65 |
| | **mask** | **0.96** | 0.96 | 0.85 | 0.90 | 0.74 |
| | floor | 0.96 | 0.96 | 0.93 | 0.85 | 0.78 |
| | level search alone | 0.80 | 0.83 | 0.50 | 0.85 | 0.39 |
| after L1 only (`real_l1`, input +6 dB) | none | 0.81 | 0.89 | 0.48 | 0.83 | 0.31 |
| | mask | 0.89 | 0.91 | 0.50 | 0.85 | 0.35 |
| | mask + level search | 0.96 | 0.96 | 0.72 | 0.90 | 0.65 |
| L1 only, noise -70 | mask + level search | 0.93 | 0.96 | 0.72 | 0.83 | 0.59 |

- **Level search on a noisy capture without the mask is worse than nothing** (0.80; its level estimate is off by a median of 9.5 dB). Noise must be handled first.

| Nuisance | Calibration that fixes it | Measured effect of the fix |
|---|---|---|
| input level (send scale) | **L2** (Send -> guitar input), then Send set | Drive 0.54 -> 0.94 at a 6 dB error (a 1 dB residual costs about 0.02 Drive) |
| | (no L2) software level search | model 0.76-0.83 -> 0.94-0.98 at ±12 dB; Drive only partly (0.31-0.33 -> 0.63-0.81); hurts when the level is right |
| noise floor | **noise take + mask** (or floor); repeats | model 0.70 -> 0.87 at -70; 0.85 -> 0.96 at -80 |
| output-side tilt, loop gain | **L1** response and gain | Tone 0.62 -> 0.90 at 2 dB/decade (exact) |
| input-side tilt | none (L1 cannot split the sides) | stays at Tone 0.81 at 2 dB/decade |
| latency | **L1** impulses (QA only) | no effect up to 25 ms |
| return clipping | capture QA, pedal Level down | 1 model of 54 lost at +12 dB |
| low input impedance | none (a limit of the method) | model 0.83, Tone 0.48 at 10 kΩ |

### 4.5 A pedal that is not in the library (leave one model out)

Each model was taken out of the library in turn: the FeatureSpace and the maps were rebuilt without it, and its target was matched.

| Condition | Nearest model in the true family | Family in the top 3 | Novelty AUC | Flagged as new at the in-library 95th percentile |
|---|---|---|---|---|
| clean probe | **0.56** (chance 0.13) | 0.85 | **0.98** | 0.98 |
| after L1 + L2, noise -80, mask | 0.54 | 0.79 | 0.78 | 0.07 |
| after L1 only, mask + level search | 0.46 | 0.69 | 0.89 | 0.20 |

52 of the 54 targets are in multi-model families. For a real pedal the nearest family is a sensible answer about half the time, and the right family is in the top 3 about 4 times in 5.

The distance says "not like anything in the library" reliably only on a clean capture. With noise at -80 dBFS, in-library captures are already far from their maps (median distance 0.46, against 0.12 clean). So a novelty threshold has to be set per noise level, and is weak there. More repeats help.

## 5. Decisions for the human

1. **Approve measured-pedal mode for a first hardware session, or not.**
   - The tool is ready, and its maths are tested on fake devices.
   - Three things are untested until the pedal is used:
     - the device's `model` command building an FX Loop block;
     - the dry-return channels (USB in 7/8);
     - whether the Stomp's guitar input matches Native's input scale.
   - The session needs about 4 minutes of calibration plus 2 to 4 minutes per pedal setting, with the human at the pedal.
2. **Do L2 (Send into the guitar input), or accept the level search?** Without L2, the model is still named but Drive readings are biased by about 0.02 per dB of send-scale error. Recommended: do L2.
3. **The noise fix:** the mask (simpler; what the tool does now) or the floor. They measured within a target or two of each other.
4. **The knob readings are positions of the matched Helix model's knobs.** Are they useful for Gate 4 as they are, or only the model and family?
5. **A low-impedance pedal input** (some fuzzes): treat it as a known limit, or add a series-resistor "pickup" adapter between Send and pedal for those pedals? That is a hardware choice, not measured here.

## 6. Files and costs

- `src/tonematch/pedal_probe.py`:
  - template and plan;
  - signals;
  - L1/L2 maths and `LoopCal`;
  - corrections;
  - the nuisance filters;
  - `shift_level`, `noise_mask`, `noise_floor_arrays`;
  - `Library`;
  - the dfp-v1 writer.
- `scripts/pedal_probe.py`: `checklist`, `template`, `selftest`, `loopback`, `capture`, `identify` (the hardware steps need `--human`).
- `scripts/pedal_probe_sim.py`: `plan`, `run`, `analyse`, `ledger`.
- `tests/test_pedal_probe.py`: 17 tests (about 30 s; one runs the real compiler offline when it is present).
- Results: `reports/local/pedal-probe-sim/` (`summary.json`, `table.md`, `rows.json`, `loo_rows.json`); fingerprints in `cache/pedal-probe-sim/` (4 MB).
- **Renders.** 810 Native probe renders kept, 0 failed, on 1 host. The main run asked for 1, to limit memory; the earlier starts asked for 2 and the scaler granted 1. About 72 minutes of rendering and fingerprinting, plus about 12 minutes of matching.
  - Two earlier starts were stopped while paused for disk; their 90 renders were kept, not redone.
  - The first plan (all 120 step-4 targets, 1,800 renders) was cut to one target per model to keep the new renders few.
- **Load and disk.**
  - The run paused whenever the 1-minute load passed 9. The system load peaked at 18.2 once, from other jobs, while this run was paused.
  - The disk was already under 8 GB free when the run started: 6.9 to 7.8 GB, held by swap files that other jobs' memory pressure had left.
  - The run waited about 35 minutes. It then went ahead with 1 host, free memory at 73 to 81% (so no new swap), and a stop rule that closes the host below 6.4 GB free (lowest seen: 6.53 GB). This run wrote 4 MB. The disk was back above 10 GB during the run.
- `proc_check` before and after: no orphans.
- **Ledger:** `benchmark / native / pedal-probe-sim-v1`, 17 rows at 2026-10-19T07:08.

