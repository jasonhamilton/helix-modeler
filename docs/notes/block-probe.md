# block-probe ([T046](../../board/TASKS.md#t046 "block-probe skill, offline part"), offline part)

Owner: `tone-metrics`. Rendering through Native or the Stomp is [T047](../../board/TASKS.md#t047 "block-probe rendering") (`native-engine`); this note
covers the probe signal, the analysis and the fingerprint format. Code: `src/tonematch/probe/`
(`signals.py`, `analysis.py`, `fingerprint.py`, `synthetic.py`), CLI `scripts/block_probe.py`, tests
`tests/test_probe.py`, skill `.claude/skills/block-probe/SKILL.md`.

Purpose: characterise ONE Helix block by a controlled input and its output, to give `search` starting
points, give `learning` per-block models, and audit per-block parity (Stomp vs Native, the cab
EarlyReflections kind of mismatch). Matches are still judged on the whole chain
(`docs/DECISIONS.md`, 2026-10-15 per-block approach).

## Probe set `probe-v1`

One mono float32 file at 48 kHz. Levels are dBFS **peak of a sine**. Built deterministically from
code (`build_probe_set()`); `make` writes `cache/probe/probe-v1.wav` and `probe-v1.json` (layout:
every segment's start, length and parameters, plus `sha256` of the samples). Without the local DI
library the variant is `probe-v1-nodi` (the same synthetic part without the DI excerpts).

| # | segment | content | measures |
|---|---|---|---|
| | lead-in | 0.5 s silence | |
| 1 | `impulse` | one sample at -6 dBFS, 4 s tail | delay, reverb |
| 2 | `mod_sine` | 1 kHz, -18 dBFS, 4 s (+0.5 s gap) | modulation rate and depth |
| 3 | `ess_-40/-24/-12/-6` | Farina ESS 20 Hz-20 kHz, 1.5 s, 0.75 s gap each | linear frequency response per level |
| 4 | `steps_110/440/1760` | 8 levels -45..-3 dBFS (6 dB steps), 0.25 s each, ascending, 1 s gap after each frequency | H2..H9, THD, odd/even, static curve, clipping curve |
| 5 | `tt_440_554_*`, `tt_110_165_*` | two-tone, -24 and -12 dBFS per tone, 0.5 s | IMD (440+554: IMD2/IMD3; 110+165 = 3:2 power-chord interval: total non-harmonic products) |
| 6 | `level_step` | 1 kHz: -40 dBFS 1.5 s, -10 dBFS 1.0 s, -40 dBFS 1.5 s | attack, release, sag (GR envelope), level-dependent gain |
| 7 | `bursts` | 1 kHz -12 dBFS, 3 x (50 ms on, 250 ms off) | attack overshoot |
| 8 | `gate_stair` | 1 kHz -30..-80 dBFS in 10 dB steps, 0.25 s on, 0.15 s off | gate / expander threshold |
| 9 | `di_chords`, `di_palmmute`, `di_bends` | 2.5 s real DI each (Guitar-TECHS, CC BY 4.0), peak-normalised to -10 dBFS, 0.5 s gaps | history-dependent behaviour on real playing |
| 10 | `marker` | 50 ms chirp 500 Hz-4 kHz, -12 dBFS, then 0.5 s | latency (sync marker) |

**Duration: 47.25 s with the DI excerpts, 38.25 s without** (`probe-v1-nodi`).

DI excerpts are **referenced, not copied**: the layout stores the path under `data/di`, start, length,
the gain applied and a SHA-256 of the excerpt. Paths: `P1_chords/.../directinput_Drop3_7.wav` at 144 s,
`P1_techniques/.../directinput_PalmMute.wav` at 52 s, `P1_techniques/.../directinput_Bendings.wav` at
220 s (that one is 44.1 kHz and is resampled with `resample_poly`).

Order rationale: time-based segments come first, after silence, so their tails are not polluted by
louder earlier segments. The sync marker is at the end so its own reverb or echo cannot leak into the
impulse. Stepped sines ascend in level so a compressor only has to attack between steps; the 1 s gap
after each frequency and the 1.5 s low parts of `level_step` let a release up to about 250 ms settle
(e^-4 or better).

## Analysis methods

| section | method |
|---|---|
| alignment | cross-correlation of the wet end with the chirp; earliest arrival within 6 dB of the best; or `--latency N` from the engine |
| linear | ESS deconvolution with the Farina inverse filter; linear IR windowed from 125 ms before (the deconvolved sweep has long low-frequency pre-ringing; H2 sits 150 ms before and stays outside) to the end of the gap; divided by the same pipeline run on the dry sweep (self-calibrating); power-averaged over +-1/24 octave onto a 1/6-octave grid 31.25 Hz-16 kHz |
| harmonics | least-squares fit of DC + H1..H9 over the last 0.14 s of each step (exact for known frequencies, no window leakage) |
| static_curve | two-slope fit of output vs input level in dB at each frequency: knee, slopes, ratio (slope below / slope above); 1 dB and 3 dB compression points; `static_curve` at top level is 440 Hz |
| transfer_curve | memoryless y = f(x) binned in 41 bins from the -3 dBFS 110 Hz step, best lag of +-16 samples; ceilings and symmetry (pos / neg); `r2` says whether the block is memoryless enough for the curve to mean what it says |
| imd | least squares at the products: IMD2 (f2-f1, f1+f2), IMD3 (2f1-f2, 2f2-f1), total incl. 5th order; for 110+165 all products fall on the 55 Hz grid, so only the total of non-harmonic grid lines |
| dynamics | complex demodulation at 1 kHz of wet and dry with the SAME 300 Hz low-pass, gain = ratio (filter smear cancels); attack and release time constants from a log-linear fit of the deviation from the settled gain (between 80 % and 5 % of the initial deviation); gain-reduction envelope at 1, 3, 10, 30, 100, 300, 900 ms; burst overshoot; gate threshold = midpoint between the last open and first closed staircase level (> 6 dB drop) |
| delay | 1 ms RMS envelope of the impulse response (window opened 10 ms early so a latency that includes group delay cannot cut off the direct sound); echoes at multiples of the strongest peak after 15 ms, each refined on the raw peak; time = linear fit of positions; feedback = log-linear fit of echo energies; echo level re direct; detected only if at least half the tail energy sits in the echo windows (`discreteness`) |
| reverb | Schroeder backward integration of the tail from 5 ms after the direct sound; noise floor = median of 10 ms block powers over the last 0.5 s, subtracted; integration ends at the last point 6 dB above the floor, with Lundeby-style compensation for the cut-off energy (capped at 10 % of the measured energy); T20 and T30 need the decay range plus 5 dB; RT60 = T30, else T20; EDT = 6 x time to -10 dB of the FULL EDC (includes the direct sound, so a mostly dry block has a short EDT) |
| modulation | demodulated 1 kHz carrier, wet / dry; amplitude (dB) and unwrapped phase tracks at 1 kHz; LFO rate from the periodogram (at least 2 cycles in 3.4 s, so >= 0.59 Hz), refined by least squares; significant only with p-p over threshold (0.3 dB, 0.02 ms), R^2 > 0.5 and stationarity > 0.6 (sinusoid amplitude equal on both halves: rejects the step pattern of echo build-up); depth = amplitude dB p-p, delay ms p-p, FM cents |
| di | per excerpt: RMS gain, crest in/out, spectral centroid in/out, magnitude-squared coherence and effective response |Sxy/Sxx| per octave band 63 Hz-8 kHz, nonlinear fraction = output power not coherent with the input |

## Fingerprint format, schema `tonematch.block-fingerprint` version 1

Strict JSON (no NaN; missing measurements are `null`). Stored locally (for example under
`cache/probe/fingerprints/`); a fingerprint of a Line 6 model is derived data from the user's own
install and stays local like other renders unless the human decides otherwise.

```text
schema            "tonematch.block-fingerprint"
schema_version    1
probe_version     "probe-v1" | "probe-v1-nodi"
probe_sha256      SHA-256 of the probe samples (float32 little-endian)
sample_rate       48000
created           ISO 8601 UTC
subject           free object: engine, model id, settings, firmware, wet_file ...
channel           "mid" | "left" | "right"
alignment         {latency_samples, source: marker|given|default-0, marker_latency_samples, marker_match}
linear            {freqs_hz[55], levels_dbfs[4], mag_db[4][55], gain_1k_db[4],
                   ir_peak_offset_samples[4], polarity[4]}
harmonics         {per_freq: [{freq_hz, levels_dbfs[8], fund_out_dbfs[8], gain_db[8],
                   h_orders [2..9], h_db[8][8] (re fundamental, floor -140, null above 0.45 fs),
                   thd_pct[8], thd_db[8], odd_even_db[8], dc_re_fund[8],
                   static {freq_hz, knee_dbfs|null, slope_below, slope_above, ratio|null,
                           fit_rms_db, small_signal_gain_db, p1db_in_dbfs|null,
                           p3db_in_dbfs|null, out_max_dbfs}}] x 3}
static_curve      the 440 Hz `static` object
transfer_curve    {x[41] (re x_scale), x_scale, y[41], r2, lag_samples, pos_ceiling,
                   neg_ceiling, symmetry|null, level_dbfs}
imd               [{name, f1, f2, level_dbfs_per_tone, fund_dbfs[2], imd2_db|null,
                    imd3_db|null, imd_total_db}] x 4
dynamics          {carrier_hz, step_levels_dbfs[3], gain_low_db, gain_high_db,
                   level_dependent_gain_db, attack_ms|null, release_ms|null, gr_times_ms[7],
                   gr_after_up_db[7], gr_after_down_db[7], burst_overshoot_db,
                   gate_levels_dbfs[6], gate_gain_db[6], gate_threshold_dbfs|null}
delay             {detected, time_ms, feedback, echo_level_db, n_echoes, discreteness}
reverb            {rt60_s, t20_s, t30_s, edt_s, decay_range_db, wet_level_db}
modulation        {kind: none|amplitude|pitch|both, rate_hz, am_depth_db_pp, delay_depth_ms_pp,
                   fm_depth_cents, am_r2, pm_r2, am_stationarity, pm_stationarity,
                   am_rate_hz, pm_rate_hz}
di                [{name, path, start_s, rms_gain_db, crest_in_db, crest_out_db,
                    centroid_in_hz, centroid_out_hz, bands_hz[8], coherence[8],
                    response_db[8], nonlinear_fraction_db}]  (empty for probe-v1-nodi)
notes             [strings]
```

Versioning: any change to the meaning or layout of a field bumps `schema_version`; any change to the
probe samples bumps `PROBE_VERSION` (the sha256 check in `probe_from_layout` refuses a stale layout).

## Compare

`fingerprint.compare(a, b)` (CLI `compare A.json B.json`) diffs b against a per feature with the
tolerances in `fingerprint.TOLERANCES` (dB unless named; relative where marked) and returns rows sorted
by |diff| / tol. Arrays report the worst point and where it is (frequency, level, harmonic order,
band); the frequency response also reports the RMS over the grid per sweep level. Harmonic and IMD
levels below -80 dB on both sides count as equal; modulation depths are compared only above their
detection floors; a feature measured on one side only (a delay found in A but not B) is a `differs`
row. Exit code 1 when anything differs. The tolerances are provisional: they should be set from the
Native repeat-render spread and the Stomp-vs-Native spread on blocks known to be in parity ([T049](../../board/TASKS.md#t049 "Per-block parity audit on the Stomp using the probe set")).

## Accuracy on KNOWN synthetic blocks (`validate`, 2026-10-15)

Each block also delays by 37 samples, to exercise the alignment. 63 of 63 checks pass.

| block | feature | true | measured | tolerance |
|---|---|---|---|---|
| identity | FR max abs, THD, delay/mod detected, level-dep. gain | 0 | 0 dB, 0 %, no, no, 0 dB | 0.1 dB / 0.01 % |
| biquad EQ (HP 80, +6 dB @1k Q1, shelf -4 dB @4k) | FR max abs error 40 Hz-16 kHz | 0 | 0.09 dB | 0.5 dB |
| | gain at 1 kHz | 5.985 dB | 5.978 dB | 0.2 dB |
| tanh(10x) | small-signal gain | 20.00 dB | 19.99 dB | 0.3 dB |
| | ceiling, symmetry | 1.0, 1.0 | 1.000, 1.000 | 5 %, 0.05 |
| | odd/even (top levels) | > 40 dB | 72 dB | |
| hard clip x4, +0.3 / -0.5 | small-signal gain | 12.04 dB | 12.04 dB | 0.3 dB |
| | pos / neg ceiling, symmetry | 0.3 / 0.5, 0.6 | 0.300 / 0.500, 0.600 | 5 %, 0.05 |
| | H2 at -3 dBFS | > -40 dB | -29.1 dB | |
| compressor T -30, 4:1, 10 / 100 ms | knee, ratio | -30, 4 | -30.0, 3.997 | 1.5 dB, 10 % |
| | attack, release | 10, 100 ms | 10.0, 100.0 ms | 15 % |
| | level-dependent gain at -10 dBFS | -15.0 dB | -15.0 dB | 0.5 dB |
| compressor T -24, 8:1, 3 / 250 ms, -90 dBFS noise | knee, ratio | -24, 8 | -24.0, 8.14 (+1.7 %) | 1.5 dB, 10 % |
| | attack, release | 3, 250 ms | 3.05 (+1.6 %), 243.7 (-2.5 %) ms | 15 % |
| | level-dependent gain | -12.25 dB | -12.20 dB | 0.5 dB |
| gate at -55 dBFS | threshold | -55 | -55 (staircase resolution 10 dB) | 5 dB |
| delay 300 ms, fb 0.5, mix 0.6 | time, feedback, echo level | 300 ms, 0.5, -4.44 dB | 300.0, 0.500, -4.44 | 0.1 ms, 0.03, 0.3 dB |
| delay 450 ms, fb 0.6, low-pass 2.5 kHz in loop | time | 450 ms | 450.05 ms | 0.5 ms |
| | feedback (filter loss included) | <= 0.6 | 0.545 | |
| Schroeder reverb RT60 1.5 s | RT60 | 1.5 s | 1.5005 s | 10 % |
| Schroeder RT60 2.5 s, -90 dBFS noise | RT60 (T20; 37 dB range, T30 withheld) | 2.5 s | 2.459 s (-1.6 %) | 10 % |
| tremolo 4 Hz, depth 0.5 | rate, depth | 4 Hz, 6.02 dB p-p | 3.998 Hz, 6.02 dB | 3 %, 0.5 dB |
| vibrato 1.5 Hz, +-1 ms | rate, delay depth | 1.5 Hz, 2 ms p-p | 1.499 Hz, 2.000 ms | 3 %, 10 % |
| amp-like (EQ, tanh x20, cab-like HP/LP) | small-signal gain at 440 Hz | 28.51 dB | 28.47 dB | 0.3 dB |
| | FR at -40 dBFS vs small-signal truth, 60 Hz-12 kHz | 0 | 0.22 dB max | 0.5 dB |
| | gain at 1 kHz, -6 vs -40 dBFS sweep | < -3 dB | -20.7 dB | |
| all | latency (37 samples) | 37 | 37 (EQ 38, amp-like 40: filter group delay at the marker band) | 2 (5 for amp-like) |

Delay, reverb and modulation also stay at "not detected" on every block that has none of them
(identity, EQ, delay for modulation, reverb for delay). These are numpy blocks, NOT Helix models:
the table says the analysis recovers what it claims to on signals with known answers, nothing about
how close any Helix block is to them.

## Known limits

- **Slow dynamics.** Release above about 250 ms has not settled before the quietest stepped sines and
  biases the knee and ratio. Sag or optical-compressor releases of seconds will read as level-dependent
  gain plus a long `gr_after_down_db` tail, not as a clean time constant. Gate threshold resolution is
  the 10 dB staircase step.
- **Impulse SNR on hardware.** One sample at -6 dBFS against a -90 dBFS noise floor leaves about 37 dB
  of decay range, so T30 is withheld and RT60 comes from T20. Averaging several Stomp captures, or a
  longer ESS segment for reverbs, is the fix if [T049](../../board/TASKS.md#t049 "Per-block parity audit on the Stomp using the probe set") needs better.
- **Tails leak forward.** A long reverb or delay keeps sounding into the following segments. The
  steady-state fits are robust to that, but THD and IMD floors rise; for time-based blocks read the
  impulse and modulation sections.
- **Modulation.** One 1 kHz carrier; LFOs slower than about 0.6 Hz are not resolved; a phaser or
  flanger notch far from 1 kHz shows as weak amplitude modulation only.
- **Latency.** The marker measures group delay at 0.5-4 kHz too; a 100 %-wet delay or a modulated
  delay reads its delayed arrival. Pass the engine's known latency (`--latency`) when it exists.
- **Stereo.** Analysed as mid by default (`--channel left|right` per side); width and ping-pong are
  not characterised yet.
- **Transfer curve** is only meaningful when `r2` is near 1; for amp-and-cab blocks use the stepped
  sine harmonics and the static curve.
- **DI section** reports coherence and response only; it does not model history dependence yet.
