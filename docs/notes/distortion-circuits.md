# Distortion circuits and the Helix drive models (T167b)

Owner: research. Task: [T167](../../board/TASKS.md#t167), track (b). Research only, no renders. Date: 2026-10-18.
Sibling notes: `distortion-fundamentals.md` (track a), `distortion-identification.md` (track c).

## Read this first: how far to trust it

- **Model-to-pedal mapping: verified.** It comes from Line 6's own model list for firmware 3.80 ([Line 6 Helix models page](https://line6.com/helix-models/)), cross-checked against a second copy of the list ([dshowmusic Helix effect models](https://dshowmusic.com/line-6-helix-effect-models/)). The knob names come from the local catalog `cache/catalog/current.json` (model ids and knob names only; nothing from it is copied into the repo).
- **Circuit facts: mixed.** Each fact carries one of these tags:
  - `[S]` = stated by a source I could read this session (linked).
  - `[D]` = derived by me from a component value or formula (the arithmetic is shown, so check it).
  - `[R]` = recalled from the commonly published schematic or from general knowledge, not re-read this session. Treat as a lead, not a fact.
  - `[?]` = could not verify; may be wrong.
- **Fetch limits this session.** ElectroSmash, geofex and the PDF papers could not be fetched (DNS failure, connection reset, binary PDF). Their content reached me only as search-result summaries. So where the note says "ElectroSmash analysis" the number came through a snippet; open the page before building a model on it. I had no shell, so the pool command was not run (see "Pool").
- **Helix is not the pedal.** Line 6 models the pedal's character, with its own knob tapers, level matching and (on some) a 9 V/18 V "Voltage" switch. The real circuit gives a prior on the shape of the transfer curve and the EQ; Native's render is the only truth for the numbers. Measure each model on Native before trusting any frequency below.

## Pool

`category_pool('drive')` was not run (no shell). From the files:

- The Native 3.82 library index (`cache/native/library/block-library-v1.2/index.json`) has **61** `drive` models: 44 current `HD2_Dist*`, 15 legacy `HD2_DM4*`, plus the legacy `KillerZ` and `Line6BronzeMaster`.
- The catalog (`cache/catalog/current.json`) has 59 drive models. `KillerZ` and `Line6BronzeMaster` are not in it.
- `synthetic_v1.eligible` drops `Bitcrusher`. `build_pools` also drops models that are `time_variant`, invalid on the user's Native, without a template, or without a verified continuous knob. No drive model carries the index `time_variant` flag. `HD2_DM4JetFuzz` has a `Rate` knob (a phaser LFO) and is the likeliest to behave as time-varying; worth a repeat-null check.
- **Expected search pool: about 58, minus `native_unavailable()` and models without continuous knobs. Not verified.** The lead should run the command in the task text and diff it with the table below. `HD2_DistToneSovereign` has `Gain 1`, `Tone 1`, ... names with spaces; `free_params` must handle them. `HD2_DistRegalBassDI` and `HD2_DistRatatouilleDist` list knobs from several sub-models (mode-dependent), so their knob sets need a check.
- Mapped in this note: **61** models (all drive entries in the library index).

## Part 1. The Helix drive models and the pedals behind them

### 1.1 The signal chain every drive pedal follows

Gain stage (pre-filter, op-amp or transistor gain) -> clipping stage (diodes, MOSFET, transistor saturation, or op-amp rail) -> tone/EQ stage -> level. The three places a circuit differs, and so the three things to identify per model:

1. **Pre-clip filter and gain.** Gain-leg RC (sets which frequencies get gain), input HPF.
2. **Clip topology.** Feedback (diodes around the op-amp, gain compresses smoothly, soft knee), to ground after the amp (hard knee, symmetric by default), transistor saturation (asymmetric, bias-dependent), op-amp rail clip (hard, from low headroom), or tube grid/plate.
3. **Post-clip EQ.** Passive tilt (Big Muff, DS-1), LPF (RAT), shelf (Klon), cut-only (Timmy), active (HM-2, MT-2).

### 1.2 Mapping, family and class (one row per model)

"Group" letters are the confusable groups defined in 1.4. Family: B = boost, OD = overdrive, D = distortion, F = fuzz, O = other. Id is `HD2_Dist<name>` or `HD2_DM4<name>` (legacy "DM4" names are the old Line 6 stompbox set).

| Helix model (id suffix) | Line 6 "based on" | Fam | Circuit class | Clipping type | EQ signature | Group |
|---|---|---|---|---|---|---|
| Scream 808 (Scream808) | Ibanez TS808 | OD | Op-amp, diodes in feedback | Si pair, feedback, symmetric, soft | Pre-clip HPF ~720 Hz, hump 0.7-1 kHz, post LPF | A |
| Screamer (DM4Screamer) | Ibanez Tube Screamer | OD | same | same | same, plus a Bass knob | A |
| Stupor OD (StuporOD) | BOSS SD-1 | OD | TS-like op-amp | asymmetric diodes in feedback (1 vs 2 diodes `[S]` below) | mid hump, tone = HF control | A2 |
| Pillars OD (Pillars) | EQD Plumes | OD | TS-like, JFET op-amp | 3 modes: LED symmetric, none (boost), Si asymmetric `[S]` | TS hump | A |
| Dhyana Drive (DhyanaDrive) | Hermida Zendrive | OD | TS-like op-amp | MOSFET+diode in feedback `[S]` | variable pre-clip bass cut and post-clip treble cut `[S]` | A/B |
| Horizon Drive (HorizonDrive) | Horizon Devices Precision Drive | OD | TS-style op-amp, tight | not published `[?]` | 6-position pre-clip bass response (Attack), Bright, gate `[S]` | A3 |
| Hedgehog D9 (HedgehogD9) | Maxon SD9 Sonic Distortion | D | op-amp, dual diodes | diode pair (stated "dual diodes") | mid scoop, bright, unlike TS `[S]` | E |
| Minotaur (Minotaur) | Klon Centaur | OD/B | op-amp + clean/dirty blend | Ge pair to ground, mostly inactive until high gain `[S]` | mid-hump gain leg, treble shelf `[S]/[R]` | B |
| Teemah! (Teemah) | Paul Cochrane Timmy | OD | op-amp clipper + gain stage | switch: 3 diode sets, symmetric x2 and asymmetric `[S]` | pre-clip Bass cut, post-clip Treble cut, cut-only `[S]` | B |
| Heir Apparent (HeirApparent) | Analogman Prince of Tone | OD | Bluesbreaker-lineage `[R]` | selectable diodes (Clipping), GainMod, Voltage | Tone, Presence | B |
| Tone Sovereign (ToneSovereign) | Analogman King of Tone (V4) | OD | two Prince-style channels in one model (`Gain 1/2`, `Tone 1/2`, ...) | as above, per channel | per channel | B |
| Prize Drive (PrizeDrive) | Nobels ODR-1 | OD | op-amp, low gain | one anti-parallel hard pair to ground (stock) `[S]` | Spectrum: mild mid boost below noon, mid cut above `[S]`; bassy `[S]` | B |
| Compulsive Drive (CompulsiveDrive) | Fulltone OCD | OD | op-amp + MOSFET clipper | MOSFET clipper to Vref, asymmetric, body diode one way `[S]` | HP/LP switch on pre-clip bass (`LPHP`), Tone, `Version` knob | P |
| Alpaca Rouge (AlpacaRouge) | Way Huge Red Llama (modded) | OD | not verified `[?]` | not verified | `HiCut` | Q |
| Top Secret OD (TopSecretOD) | DOD OD-250 | OD | op-amp + diodes to ground | Si pair to ground, hard | gain-leg HPF 720 Hz at max `[D]` | C |
| Overdrive (DM4Overdrive) | DOD Overdrive/Preamp 250 | OD | same | same | same, with fixed 3-band EQ | C |
| KWB (KWB) | Benadrian Kowloon Walled Bunny | D | Dist+/250/Ross family `[S]` | diodes in sockets; Ge on one side, LED on other (asymmetric) `[S]`; `PushDiode`, `PullDiode`, `Asym` | big coupling caps, full bass `[S]` | C |
| Vermin Dist (VerminDist) | Pro Co RAT | D | LM308 op-amp + diodes to ground | 1N914 pair to ground, hard `[S]` | LPF "filter" 0.5-32 kHz `[D]` | D |
| Ratatouille Dist (RatatouilleDist) | 1984 Pro Co RAT | D | same | same | same | D |
| Classic Dist (DM4ClassicDistortion) | Pro Co RAT | D | same | same | `Filter` knob | D |
| Vital Dist (VitalDist) | EQD Life Pedal (distortion "Amplitude") | D | RAT-style "Whiteface" `[S]` + octave `[S]` | RAT-like to ground; `Clipping` choice | `Filter` | D |
| Deez One Vintage (DeezOneVintage) | BOSS DS-1 (MIJ) | D | transistor boost + op-amp + diodes to ground | Si pair to ground, hard | tilt tone; stage HPFs 30 and 70 Hz `[S]` | E |
| Deez One Mod (DeezOneMod) | BOSS DS-1 (Keeley mod) | D | same, modded | `Clipping` choice | same | E |
| L6 Distortion (DM4Line6Distortion) | Line 6 original | D | unknown `[?]` | unknown | fixed 3-band EQ | O |
| Swedish Chainsaw (SwedishChainsaw) | BOSS HM-2 (MIJ) | D | transistor pre-gain + asym op-amp clipper + two clipper pairs `[S]` | asymmetric first, symmetric pairs | active 3-band gyrator EQ, big low and high boosts `[S]` | F |
| Heavy Dist (DM4HeavyDistortion) | BOSS Metal Zone MT-2 | D | two cascaded gain stages + parametric mid `[R]` | diodes to ground `[R]` | 3-band with sweepable mid `[R]` | F |
| Killer Z (KillerZ, not in catalog) | BOSS Metal Zone MT-2 | D | same as Heavy Dist | same | same | F |
| Legendary Drive (LegendaryDrive) | Carvin VLD1 Legacy Drive (hi gain) | D | hi-gain preamp channel `[?]` | `[?]` | Bass/Middle/Treble/Presence stack | F |
| Triangle Fuzz (TriangleFuzz) | EHX Big Muff Pi | F | 4 BJT stages, 2 clip stages | diode pair in each clip stage feedback | LP/HP blend, mid scoop | G |
| Bighorn Fuzz (RamsHead) | 1973 EHX Ram's Head | F | same | same | scoop deeper, lower `[S]` | G |
| Dark Dove Fuzz (DarkDoveFuzz) | EHX Russian Big Muff | F | same | same | different tone values `[?]` | G |
| Fuzz Pi (DM4FuzzPi) | EHX Big Muff Pi | F | same | same | fixed 3-band EQ | G |
| Ballistic Fuzz (BallisticFuzz) | Euthymia ICBM Fuzz | F | IC (op-amp) Big Muff variant, silicon `[S]` | `[R]` op-amp-based clip | Sustain, Tone | G2 |
| Pocket Fuzz (PocketFuzz) | Jordan Boss Tone | F | Big Muff precursor, copied from it `[S]` | transistor | none (Drive, Level) | G2 |
| Arbitrator Fuzz (ArbitratorFuzz) | Arbiter Fuzz Face | F | 2-transistor feedback pair | transistor saturation, asymmetric | none (Fuzz, Level) | H |
| Facial Fuzz (DM4FacialFuzz) | Arbiter Fuzz Face | F | same | same | fixed 3-band EQ | H |
| Jumbo Fuzz (DM4JumboFuzz) | Vox Tone Bender | F | 2-3 Ge transistors | transistor | fixed 3-band EQ | I |
| Buzz Saw (DM4BuzzSaw) | Maestro Fuzz-Tone | F | 2-3 Ge transistors `[?]` | transistor | fixed 3-band EQ | I |
| Wringer Fuzz (WringerFuzz) | "Garbage's modded BOSS FZ-2" | F | FZ-2 Hyper Fuzz derivative `[?]` | `FuzzType` selects variant | Bass, Treble | I |
| Industrial Fuzz (IndustrialFuzz) | Z.Vex Fuzz Factory | F | 2 Ge transistors with positive-feedback bias | unstable, bias-dependent | Gate, Compress, Stab | O |
| Xenomorph Fuzz (XenomorphFuzz) | Subdecay Harmonic Antagonizer | F | triggered oscillator + square-wave fuzz + VCA `[S]` | digital-style square | `OscLevel`, `OscTone`, `MinFreq`, `MaxFreq` | O |
| Thrifter Fuzz (ThrifterFuzz) | Line 6 original | F | unknown `[?]` | unknown | `Attack`, `Thick` | O |
| Tycoctavia Fuzz (TycoctaviaFuzz) | Tycobrahe Octavia | F | Ge fuzz + full-wave rectifier octave-up `[S]` | transistor + rectified | none (Fuzz, Level) | J |
| Octave Fuzz (DM4OctaveFuzz) | Tycobrahe Octavia | F | same | same | fixed 3-band EQ | J |
| Sub Oct Fuzz (DM4SubOctFuzz) | PAiA Roctave Divider | F | octave-down divider `[R]` | square wave (flip-flop) | `Sub` | J2 |
| Clawthorn Drive (ClawthornDrive) | Wounded Paw Battering Ram | OD+F | parallel OD and fuzz channels with optional octave-up `[S]` | OD soft + fuzz | `ODLowBoost`, `FuzzOct` | R |
| Kinky Boost (KinkyBoost) | Xotic EP Booster | B | clean boost, EP-3 preamp voicing `[R]` | essentially none | `Boost`, `Bright` | K |
| Deranged Master (DerangedMaster) | Dallas Rangemaster | B | 1 Ge transistor, 5 nF input cap `[S]` | transistor, asymmetric at high gain | high-pass character | K |
| Vital Boost (VitalBoost) | EQD Life Pedal (boost "Magnitude") | B | boost | none | one knob (`Boost`) | K |
| Valve Driver (ValveDriver) | Chandler Tube Driver | OD | 12AX7 starved-plate `[S]` | tube, asymmetric, even-order | Bass, Treble | N |
| Tube Drive (DM4TubeDrive) | Chandler Tube Driver | OD | same | same | fixed 4-knob stack | N |
| Colordrive (DM4ColorDrive) | Colorsound Overdriver | OD/F | 3 NPN BJT `[S]` | transistor | Bass/Mid/Treble | M |
| L6 Drive (DM4Line6Drive) | Colorsound Overdriver (modded) | OD | same, modded | transistor | Bass/Mid/Treble | M |
| Jet Fuzz (DM4JetFuzz) | Roland Jet Phaser | F+mod | fuzz + phaser, `Rate`, `Feedback` | `[?]` | `Tone` | O (time-varying) |
| Ampeg Scrambler (AmpegScramblerOD) | Ampeg Scrambler bass OD | OD | bass drive with `Blend` | `[?]` | `Treble` | L |
| ZeroAmp Bass DI (ZeroAmpBassDI) | Tech 21 SansAmp Bass Driver DI v1 | OD | DI preamp with blend | `[?]` | Bass, Treble, Presence | L |
| Regal Bass DI (RegalBassDI) | Noble Preamp bass DI | OD | DI preamp | `[?]` | Bass, Treble | L |
| Obsidian 7000 (Obsidian7000) | Darkglass Microtubes B7K | D | bass drive, 6 controls, Grunt/Attack `[R]` | `[?]` | 4-band EQ with swept mids | L |
| Bronze Master (Line6BronzeMaster, not in catalog) | Maestro Bass Brassmaster | D | bass fuzz `[?]` | `[?]` | `[?]` | L |
| Megaphone (Megaphone) | "Megaphone" (not a pedal) | O | band-limited lo-fi voice | `[?]` | `Focus` band-pass, `Space`, `Mix` | O |
| Bitcrusher (Bitcrusher) | Line 6 original | O | digital: bit depth, sample rate | quantisation | `LowCut`, `HighCut` | excluded from search |

Notes on rows:

- **Hedgehog D9 is probably not a Tube Screamer clone.** The earlier assumption (SD9 = TS) is contradicted: a review says the SD-9's "biggest difference is the mid boost in the TS and the mid scoop in the SD-9" ([Legendary Tones on OD-9 and SD-9](https://legendarytones.com/maxon-od-9-overdrive-and-sd-9-sonic-distortion/)). Put it nearer the DS-1 than the TS in a first guess; confirm by measurement.
- **Stupor OD**: BOSS SD-1 clips asymmetrically with different diode counts per polarity ([BOSS SD-1 reviews and history](https://www.guitarworld.com/features/boss-sd-1-super-overdrive-pedal)), so it produces even and odd harmonics, unlike the TS.
- **Dhyana Drive** diode/MOSFET topology is from the forum thread [Zendrive solderable breadboard](https://forum.pedalpcb.com/threads/zendrive-solderable-breadboard.29780/post-388530) (summary, not read in full).
- **Clawthorn Drive**: params `ODGain`, `ODTone`, `ODLevel`, `ODLowBoost`, `Fuzz`, `FuzzOct`, `FuzzGain`, `FuzzTone`, `FuzzLevel` match the Battering Ram's two channels ([Reverb listing](https://reverb.com/ca/item/523366-wounded-paw-battering-ram-v2-1-overdrive-fuzz-for-guitar-or-bass)); a parallel dual channel, so one model is two circuits summed.

### 1.3 What each Helix knob most likely maps to

Probabilities are my judgement, not measured. `P` = priority for the lead to measure.

| Helix knob (where seen) | Likely circuit meaning |
|---|---|
| `Gain` / `Drive` (all op-amp models) | The op-amp feedback pot: raises gain `1 + Rf/Rg` above the gain-leg corner and drives the clip. In a ground-clipper (RAT, Dist+, DS-1) it sets how hard the clipper is driven; in a feedback-clipper (TS, Zendrive) it sets where the diodes start to bite. Mostly changes harmonic content and low-level compression; very little EQ change in the TS, a lot in the Dist+ (gain-leg corner moves from 3 Hz to 720 Hz, see 2.4). |
| `Level` / `Volume` / `Output` | Post-clip attenuator. Does not change the distortion; shifts level, and (in Native) the level guard matters. Excluded from the parameter error in `synthetic_v1` by design. |
| `Tone` (TS, SD-1, Timmy-like, Klon, Heir Apparent) | TS/SD-1: post-clip HF shelf/LPF. Klon: treble shelf. Big Muff (`Tone`): LP-HP blend, which moves the mid scoop. Teemah's `Treble` is the post-clip cut. |
| `Bass` | Pre-clip HPF corner (Timmy, Zendrive-style) or fixed post-EQ low shelf (DM4 stacks). |
| `Filter` (RAT family) | RAT LPF: 1.5 kΩ + 100 kΩ pot with 3.3 nF, corner 0.5-32 kHz `[D]` (2.3). |
| `Sustain` (Big Muff family) | Level fed into the clip stages; sets how saturated, not an EQ. |
| `Fuzz` (Fuzz Face family) | Feedback/loading around the second transistor; at max, the highest loop gain. |
| `Clipping`, `PushDiode`, `PullDiode`, `Asym` (Teemah, KWB, Heir Apparent, Deez One Mod, Vital Dist) | Selects the diode set: symmetry and threshold. Strongest effect on even/odd harmonic balance. |
| `Voltage` (Prize Drive, Heir Apparent, Tone Sovereign) | A 9 V/18 V supply toggle (bool in the catalog): headroom. 18 V = more clean headroom, later hard rail clip. |
| `LPHP` and `Version` (Compulsive Drive) | The OCD's HP/LP bass switch and an internal version selection `[R]`. |
| `Spectrum` (Prize Drive) | The ODR-1 mid tilt `[S]`. |
| `Mode` (Pillars) | Plumes three-way clip mode `[S]`. |
| `FuzzType` (Wringer Fuzz), `WaveShape`/`Sensitivity` (Xenomorph) | Variant selectors; treat as a discrete model choice. |

### 1.4 Confusable groups (near-duplicates in sound)

Same circuit idea, small differences; a matcher is expected to confuse these, and the listener may not care.

| Group | Members | What separates them |
|---|---|---|
| A. Tube Screamer lineage | Scream 808, Screamer (DM4), Pillars OD, Dhyana Drive, Horizon Drive; A2: Stupor OD | Pre-clip bass cut, diode type or symmetry (Stupor asymmetric), clip modes (Pillars), extra controls. Scream 808 vs Screamer is probably the closest pair in the set. |
| B. Transparent / Bluesbreaker lineage | Minotaur, Teemah!, Heir Apparent, Tone Sovereign, Prize Drive | Clean/dirty blend (Klon) vs diode feedback (others); all are low-to-medium gain with a treble control. Heir Apparent vs Tone Sovereign are the same circuit idea, one vs two channels. |
| C. Distortion+ / OD-250 / Ross | Top Secret OD, Overdrive (DM4), KWB | Diode type and symmetry (KWB Ge/LED asymmetry). The pedal-side evidence is that these are "essentially the same circuit" ([PedalPCB thread](https://forum.pedalpcb.com/goto/post?id=81442), as summarised by search). |
| D. RAT | Vermin Dist, Ratatouille Dist, Classic Dist, Vital Dist | Era (1978 vs 1984), filter and clip details; Vital Dist adds an octave section. |
| E. DS-1 / SD9 | Deez One Vintage, Deez One Mod, Hedgehog D9 | Mod and diode swaps; SD9 relation to DS-1 is unverified. |
| F. Heavy / metal | Swedish Chainsaw (HM-2), Heavy Dist and Killer Z (MT-2), Legendary Drive | EQ architecture (gyrator vs parametric vs amp stack). **Heavy Dist and Killer Z are the same pedal in two entries (legacy duplicate).** |
| G. Big Muff | Triangle Fuzz, Bighorn Fuzz, Dark Dove Fuzz, Fuzz Pi; G2: Ballistic Fuzz, Pocket Fuzz | Tone-stack values (2.8). **Triangle Fuzz and Fuzz Pi are the same pedal in two entries.** |
| H. Fuzz Face | Arbitrator Fuzz, Facial Fuzz | Same pedal in two entries (current vs legacy); the legacy one adds a fixed EQ stack. |
| I. Ge fuzz (other) | Jumbo Fuzz (Tone Bender), Buzz Saw (Fuzz-Tone), Wringer Fuzz | Tone Bender has an extra gain stage; Fuzz-Tone is simpler. |
| J. Octave up | Tycoctavia Fuzz, Octave Fuzz | Same pedal, current vs legacy. J2: Sub Oct Fuzz is a different mechanism (octave down). |
| K. Boosts | Kinky Boost, Vital Boost, Deranged Master | EP Booster is a flat-ish clean boost; Rangemaster is a treble booster that clips when pushed. |
| L. Bass drives | Ampeg Scrambler, ZeroAmp Bass DI, Regal Bass DI, Obsidian 7000, Bronze Master | Blend of clean and driven low end; EQ. |
| M. Colorsound | Colordrive, L6 Drive | Original vs modded; probably the same core. |
| N. Tube Driver | Valve Driver, Tube Drive | Same pedal in two entries. |
| P/Q/R/O singletons | Compulsive Drive; Alpaca Rouge; Clawthorn; Industrial, Xenomorph, Thrifter, L6 Distortion, Megaphone, Jet Fuzz | Distinct. |

## Part 2. Circuit dossiers

All "gain" figures are small-signal gain at the stated frequency before the clipper acts. Formula for an op-amp non-inverting stage: `G = 1 + Zf/Zg`, with `Zg` a resistor in series with a capacitor, so the gain rises above the corner `fc = 1/(2 pi R C)`.

### 2.1 Ibanez Tube Screamer (TS808 / TS9) -> Scream 808, Screamer
- **Path** `[S]` (ElectroSmash analysis, via [search summary](https://electrosmash.com/tube-screamer-analysis); see also [Yeh/Abel/Smith, DAFx-07](https://dafx.de/paper-archive/details/mIq7vlQKu6pUlF467vvhgw)): JFET bypass switching, input buffer, non-inverting op-amp clipper, tone/volume, output buffer.
- **Gain stage**: `G = 1 + (51 kΩ + Drive pot) / Zg`, Zg = 4.7 kΩ + 47 nF in series `[S]` (gain 12 to about 120 `[S]`). `[D]`: with a 500 kΩ pot, minimum `1 + 51/4.7 = 11.9` = 21.5 dB, maximum `1 + 551/4.7 = 118` = 41.4 dB at high frequency.
- **Gain-leg corner** `[D]`: `1/(2 pi 4.7k 47n)` = **720 Hz**, matching the "720 Hz" reported `[S]`. Below it the stage is near unity: bass is not clipped. That is the "mid hump" mechanism.
- **Feedback cap** 51 pF `[R]`: `[D]` corner 61 kHz at minimum drive, 5.7 kHz at maximum (`1/(2 pi 551k 51p)`). So the high end rolls off by itself as drive rises.
- **Clipper**: two silicon diodes (1N914/4148 class), anti-parallel, **in the feedback loop** `[S]`. Output is limited to about +/-0.55 to 0.7 V per diode drop `[D]`, a soft knee because of the exponential I-V `[S]`. Symmetric, so odd harmonics dominate.
- **Clipping onset** `[D]`: input level where output reaches the diode drop (~0.6 V): about 5 mV at maximum drive (HF), about 50 mV at minimum. A guitar at 100 mV-1 V clips all the time; the knee moves with Drive.
- **Tone**: passive LPF 1 kΩ + 220 nF, `[D]` **723 Hz** `[R]`, plus an active shelving control; full range of the tone control not verified `[?]`.
- **Input HPF** `[R]`: 0.022 uF with 510 kΩ, about 14 Hz.
- **Signature**: strong 3rd and 5th, rolled-off bass (~720 Hz and down is clean), mild compression, ceiling around 40 dB HF gain.
- **TS808 vs TS9**: nearly identical in the gain/clip/tone path `[R]`; differences are in the bypass and buffers. Treat as one class.
- **SD-1 variant**: asymmetric diode count per polarity `[S]`; same hump, plus even harmonics.
- **Plumes (Pillars OD)** `[S]` ([review](https://www.premierguitar.com/gear/earthquaker-devices-plumes-review)): JFET op-amp, 10 MΩ input; clip mode LED symmetric / none / Si asymmetric.

### 2.2 Klon Centaur -> Minotaur
- **Sources**: [Coda Effects Klon analysis](https://www.coda-effects.com/p/klon-centaur-circuit-analysis.html) `[S]`; [ElectroSmash analysis](https://electrosmash.com/klon-centaur-analysis) (search summary only).
- **Path**: input buffer (unity op-amp) -> gain stage -> **clean + dirty summing** -> tone -> output stage.
- **Gain stage**: non-inverting, gain range **4.6x to 25.8x** (13 to 28 dB) with a dual-gang Gain pot `[S]` (Coda). Because the second gang blends the clean path and the saturated path, gain and the clean share move together: low gain = mostly clean, high gain = 100% clipped path `[S]`.
- **Clipper**: germanium diodes (1N34A type, Vf about 0.3 V `[S]`) to ground after the gain stage, anti-parallel, with series resistance `[R]`. They are "mostly inactive at moderate gain" and engage at high gain `[S]`. Real saturation comes from running the op-amps at **18 V** (charge pump from 9 V; rails VB+ 4.5 V, V- -9 V, V2+ 18 V `[S]`), giving large clean headroom.
- **Mid hump**: the gain stage has a frequency-dependent feedback network that lifts roughly 1 kHz `[R]`; exact corner `[?]`.
- **Tone**: treble shelf, `R23` 4.7 kΩ in the stock tone circuit, a common mod swaps it for 10 kΩ for more treble gain `[S]`. Shelf corner and range `[?]` (my recollection is ~+/-10 dB around the upper mids, not verified).
- **Signature**: odd-dominant, low distortion at low drive (a "boost with grit"), almost no compression until the Ge diodes engage. Clean blend preserves pick attack.
- **Consequence for matching**: the dry (blend) share moves with Gain; a model-free matcher will see "less distortion" as gain falls without any level drop.

### 2.3 Pro Co RAT -> Vermin Dist, Ratatouille Dist, Classic Dist
- **Source**: [ElectroSmash RAT](https://electrosmash.com/proco-rat) (search summary), [RAT forum threads](https://forum.pedalpcb.com/threads/rat-circuit-theory.30300/post-396000).
- **Op-amp**: LM308 `[S]`, non-inverting, variable gain with diodes shorting the output to ground to **hard clip** `[S]`. Originally 1N914, 1N4148 from RAT2 `[S]`.
- **Gain**: `G = 1 + Rf/Zg`. Zg = two RC branches to ground: 47 Ω + 2.2 uF and 560 Ω + 4.7 uF `[S]` (parts confirmed by search; roles from the standard schematic `[R]`). Feedback `Rf` = 100 kΩ Distortion pot with 100 pF `[R]`.
- **Computed** `[D]`:
  - branch corners: `1/(2 pi 47 2.2u)` = **1.54 kHz**; `1/(2 pi 560 4.7u)` = **60.5 Hz** (the search says "about 60 and 1500 Hz" `[S]`);
  - above 1.5 kHz: Zg ~ 47 || 560 = 43.4 Ω, `G = 1 + 100k/43.4` = 2305 = **67 dB**, matching the "67 dB" `[S]`;
  - between 60 Hz and 1.5 kHz: Zg ~ 560 Ω, `G` = 180 = 45 dB;
  - below 60 Hz: unity.
- **Op-amp limit** `[S]`: "due to the slew characteristics of the LM308 this 67 dB is never reached" (ElectroSmash). My `[D]` check: with a GBW near 1 MHz `[?]`, closed-loop bandwidth at G=2305 is under 500 Hz, so at full drive the op-amp itself becomes a low-pass and slew-limits (the LM308 is slow with its 30 pF compensation cap `[R]`, slew near 0.3 V/us `[R]`). The clipped output tends toward a **triangle/slewed** shape rather than a square wave.
- **Filter (tone) control** `[D]`: LPF of 1.5 kΩ fixed + 100 kΩ pot with 3.3 nF `[R]`: corner `1/(2 pi 1.5k 3.3n)` = **32 kHz** (fully open) down to `1/(2 pi 101.5k 3.3n)` = **475 Hz** (fully closed). The search describes it as "a simple low-pass; with the bass pulled back the signal is closer to a square wave, wide open nearly triangle" `[S]`.
- **Clip**: 1N914 pair to ground via a series resistor `[R]`; symmetric hard, ~+/-0.6 V. `[D]` onset: input ~0.3 mV at 67 dB, ~3 mV at 45 dB. In practice clipping is continuous.
- **Signature**: odd-rich, dense; bass-heavy sustain (560 Ω branch) with a bright top set by the Filter. Little compression of the envelope once saturated.

### 2.4 MXR Distortion+ and DOD OD-250 -> Top Secret OD, Overdrive (DM4), KWB
- **Source**: [ElectroSmash MXR Distortion+](https://electrosmash.com/mxr-distortion-plus-analysis) (search summary `[S]`).
- **Path**: one op-amp (original 741 `[R]`), two pots (Distortion, Output), germanium diode pair, no tone control `[S]`.
- **Gain** `[D]` from the stated numbers: Rf = 1 MΩ (fixed) `[R]`, Zg = 4.7 kΩ + Distortion pot (0 to 1 MΩ) in series with 47 nF. Maximum `G = 1 + 1M/4.7k` = 214 = **46.6 dB** (matches "up to 46 dB" `[S]`). Corner at max: `1/(2 pi 4.7k 47n)` = **720 Hz** `[S]` (the source says "723 Hz"). At minimum gain the corner drops to `1/(2 pi 1.0047M 47n)` = **3.4 Hz** `[S]`. So turning Distortion up shifts the clipped band from nearly all frequencies to mostly above 720 Hz, i.e. the bass is cut as you add gain.
- **Op-amp**: a 741 has GBW about 1 MHz `[R]`, so at G=214 the bandwidth is ~4.7 kHz `[D]`: a built-in treble roll-off at full drive.
- **Clipper**: 1N34A-type germanium diodes, anti-parallel to ground `[S]`, Vf about 0.25-0.3 V `[R]`. Low clip level (+/-0.3 V) is followed by the Output pot. The OD-250 is the same topology with silicon diodes `[S]` (PedalPCB thread) - harder, louder clip.
- **KWB**: same family with Ge on one side and LED on the other (asymmetric), plus a Clip knob that "breaks" one diode, and big coupling caps for low end ([effectsdatabase KWB](https://www.effectsdatabase.com/model/benadrian/kwb), [forum](https://forum.pedalpcb.com/goto/post?id=81442)) `[S]`.
- **Signature**: Dist+ Ge: fuzzy, compressed, low clipping level; OD-250: more open, higher level; KWB: asymmetry adds 2nd harmonic.

### 2.5 BOSS DS-1 -> Deez One Vintage / Mod
- **Source**: [ElectroSmash DS-1](https://electrosmash.com/boss-ds1-analysis) `[S]` (search summary).
- **Path** `[S]`: input buffer, **transistor boost stage** (two series high-pass filters then a common-emitter amp, about 35 dB, small distortion), op-amp stage with clipping diodes and filters, passive tone control, output buffer.
- **Gains** `[S]`: transistor stage about +30 dB above ~30 Hz, op-amp stage another +30 dB above ~70 Hz. So combined HF gain **about 60-65 dB** `[D]` and the bass is cut below ~30-70 Hz before clipping. Original 1978 chip was a Toshiba TA7136AP `[S]`, later a Rohm BA728N dual op-amp (1994) `[S]`; MIJ (Helix says "Made-in-Japan") is a specific era.
- **Clipper**: 1N4148 pair, hard, to ground, symmetric `[R]`; Keeley mod swaps diodes/values `[R]` (Helix `Clipping` on Deez One Mod).
- **Tone** `[?]`: passive tilt (LPF/HPF crossover near ~1 kHz). Not verified.
- **Signature**: odd-rich, tight, bright; cleaner low end than RAT.

### 2.6 EHX Big Muff Pi (Triangle / Ram's Head / Russian) -> Triangle Fuzz, Bighorn Fuzz, Dark Dove Fuzz, Fuzz Pi
- **Source**: [ElectroSmash Big Muff Pi](https://electrosmash.com/big-muff-pi-analysis) (summary `[S]`), [Coda tone-stack note](https://www.coda-effects.com/p/big-muff-tonestack-dealing-with-mids.html), [Big Muff Pi Page](https://www.bigmuffpage.com/Big_Muff_Pi_versions_schematics_part2.html).
- **Path** `[S]`: four common-emitter transistor stages: input booster, **two identical clipping stages with a diode pair in each feedback loop**, passive tone control, output booster. Clipping twice gives harsh distortion "typical of hard clipping" `[S]`.
- **Diodes** `[S]`: silicon, listed 1N914, measured about 0.5-0.6 V.
- **Gain** `[?]`: per-stage open-loop gains are large; each clip stage is limited to about +/-0.6 V by the diodes `[D]`. Total gain I cannot verify; treat the signal as saturated at all but the lowest Sustain.
- **Tone stack** `[S]` (via Coda): LPF and HPF blended by the Tone pot. Triangle: R=22k, C=10 nF and 4 nF -> corners **723 Hz** and **1809 Hz** `[D]`. Ram's Head ('73): R=33k -> **482 Hz** and **1206 Hz** `[D]`.
- **Mid scoop** `[D]`: geometric mean of the corners `sqrt(f1 f2)`: Triangle ~**1.14 kHz**, Ram's Head ~**0.76 kHz**. Conflict: one source says the Ram's Head scoops at a *slightly higher* frequency and deeper than the Triangle `[S]`; my arithmetic says lower. Resolve by measuring `Bighorn` vs `Triangle` on Native. The value depends on the Tone pot and the source/load impedance, so this is a first approximation only.
- **Russian** (Dark Dove) tone values `[?]`.
- **Sustain** `[R]` sets the level into the clip stages.
- **Signature**: heavy, compressed, sustained fuzz; near-square; strong odd harmonics; mid scoop (and bass loss in the tone stack, recovered by the output stage).
- **IC variants** (Euthymia ICBM = IC Big Muff, [effectsdatabase](https://www.effectsdatabase.com/model/euthymia/icbm), `[S]`) replace transistors with op-amps; Jordan Boss Tone is a 1971 Big Muff precursor `[S]`.

### 2.7 Dallas Arbiter Fuzz Face (Ge, Si) -> Arbitrator Fuzz, Facial Fuzz
- **Sources**: [Coda Fuzz Face analysis](https://coda-effects.com/p/circuit-analysis-fuzz-face.html), [ElectroSmash Fuzz Face](https://electrosmash.com/fuzz-face), [Silicon Chip article](https://www.siliconchip.com.au/Issue/2024/December/Dallas+Arbiter+Fuzz+Face+guitar+pedal) (all search summaries `[S]`).
- **Path** `[S]`: two transistors, common-emitter, Q1 collector direct to Q2 base, a 100 kΩ feedback from Q2 back to Q1 base. The Fuzz pot (1 kΩ) varies the feedback/loading around Q2 `[S]`; exact node placement not re-derived here `[?]`.
- **Input impedance** about **20 kΩ** `[S]`: the guitar's pickups interact directly, so the guitar volume knob cleans the fuzz up and a buffer before it changes the sound (relevant: Helix may or may not model this).
- **Gain** `[D]` order of 100x (40 dB) at full Fuzz from the feedback ratio 100k/1k; low confidence `[?]`.
- **Transistors** `[S]`: best match is low-gain Q1 (beta 70-80), high-gain Q2 (beta 110-130); AC128 beta 45-165 so units differ widely. Silicon versions (BC108-style) are tighter, brighter, and more gain.
- **Clipping** `[R]`: transistor saturation/cutoff. Asymmetric (the two halves hit different limits), so even and odd harmonics both present. Ge leakage and temperature shift the bias (the "sound" of the Ge unit).
- **Signature**: fuzzy, squarish waveform with a soft entry; rolls off the top; cleans up with guitar volume.

### 2.8 Vox / Sola Tone Bender MkII and Maestro Fuzz-Tone -> Jumbo Fuzz, Buzz Saw
- **Tone Bender MkII**: three OC75 Ge PNP transistors; MkII adds an extra gain stage in front of the MkI.5 two-transistor feedback amp ([Tone Bender, Wikipedia](https://en.wikipedia.org/wiki/Tone_Bender), [PedalPCB thread](https://forum.pedalpcb.com/posts/365064/)) `[S]`. Output collector near -8 V gives the brighter, more open tone than the Fuzz Face `[S]`. Sustain/Attack pot placement not verified `[?]`.
- **Maestro FZ-1 Fuzz-Tone**: early Ge fuzz; transistor count (2 or 3) and values `[?]`; I could not read a source this session.
- **Signature** `[R]`: gated, spitty at low settings, big octave-ish upper harmonics; asymmetric; very bias-dependent.

### 2.9 Boss Blues Driver BD-2 (not a Helix model; Blues Driver type)
- **Source**: [Premier Guitar BD-2 mods](https://www.premierguitar.com/gear/boss-bd-2-mods) `[S]`.
- Two cascaded **discrete op-amp stages** (two FETs + a bipolar), gain set by a dual-gang 250 kΩ pot; followed by a standard op-amp for gain recovery and bass boost `[S]`.
- Corners `[S]`: first stage ~**700 Hz** (R31/C22), second stage ~**72 Hz** (R34/C24).
- Clipping `[S]`: diodes to ground (D7-D10), two per side. Tone filters around 5 kHz HF cut and bass trim `[S]`. Symmetry conflicts with common lore of asymmetry `[?]`.
- **Use**: the reference for a two-stage OD; no Helix drive is "based on" it (the closest class in Helix is the TS lineage).

### 2.10 Timmy (Paul Cochrane) -> Teemah!
- **Source** `[S]` ([DIYStompboxes thread](https://www.diystompboxes.com/smfforum/index.php?msg=669958), [Mad Bean Shortbus doc](https://madbeanpedals.com/projects/_folders/DistBoostOD/pdf/Shortbus.pdf), [MXR Timmy review](https://www.bonedo.de/artikel/mxr-timmy-test)): an op-amp clipper followed by an op-amp gain stage; a 3-way clipping switch (two symmetrical levels and one asymmetric); Bass = pre-clip, **cut-only**; Treble = post-clip, **cut-only**. Component values not verified `[?]`.
- **Signature** `[S]`: "transparent" - little character of its own; the highest-headroom class in the set.
- **Helix**: `Clipping` (switch), `Bass`, `Treble`, `Gain`, `Level` match one-to-one.

### 2.11 Analogman King of Tone / Prince of Tone / Marshall Bluesbreaker -> Tone Sovereign, Heir Apparent
- **Bluesbreaker** `[S]` ([mirosol Bluesbreaker II](https://mirosol.kapsi.fi/2014/04/marshall-bb-2-blues-breaker-ii/), [Mad Bean 8 Ball doc](https://madbeanpedals.com/projects/_folders/StandardSeries/pdf/8Ball.pdf)): buffer, boosting stage, clipping amp with LEDs (or 4x 1N4148) in the feedback loop with a **series resistor (6.8k)** raising the threshold and softening the clip, recovery stage, tone, volume. With 220 kΩ feedback and 10 kΩ input, a gain of about 22x.
- **KoT** `[S]`: popular diode choices include the MA856 as used in the KoT. Two channels that can be stacked; Helix Tone Sovereign has full controls per channel (`Gain 1/2`, `Tone 1/2`, `Presence 1/2`, `Clipping 1/2`, `GainMod 1/2`, `Level 1/2`) and one `Voltage` switch.
- **Signature**: low-to-medium gain, soft, symmetric (odd-dominant) clip with a mid-forward EQ; stacking channel 1 into channel 2 gives more compression.
- The Bluesbreaker lineage claim for KoT/Prince of Tone is well known `[R]` but not read in full this session.

### 2.12 Fulltone OCD -> Compulsive Drive
- **Sources** `[S]`: [DIYStompboxes OCD versions](https://diystompboxes.com/smfforum/index.php?msg=402612), [PedalPCB MOSFET thread](https://forum.pedalpcb.com/threads/anyone-tried-diodes-instead-of-mosfets-in-an-ocd.14951/post-172719).
- **Clipper**: a MOSFET stage; "the type of MOSFET has nearly zero influence"; the body diode clips one polarity, the gate-drain-to-source path the other; connected to the voltage reference, not ground; 1 nF across it `[S]`. So it is **asymmetric**, with a mix of even and odd harmonics.
- **Op-amp**: TL082 `[S, forum]`; Drive pot gain range and the HP/LP switch capacitor values `[?]`.
- **Helix**: `LPHP` is the HP/LP switch, `Version` selects an OCD revision `[R]`.
- **Signature**: open, dynamic, amp-like breakup; strong response to pick dynamics.

### 2.13 BOSS HM-2 -> Swedish Chainsaw
- **Source** `[S]` ([BOSS article "Gain Appreciation: Analyzing the BOSS HM-2W"](https://articles.boss.info/gain-appreciation-analyzing-the-boss-hm-2w/), [mirosol HM-2 notes](https://mirosol.kapsi.fi/2014/05/boss-hm-2-heavy-metal/)): an NPN/PNP **pre-gain** transistor network, an asymmetric clipping op-amp stage, two clipper pairs, a buffer; a **three-band active Q-boosting gyrator filter** gives large boosts on selected frequencies (low band and a Color/High band). Several gain stages "all over the place" to keep noise low `[S]`. The rudimentary gate through diodes causes crossover distortion `[S]`.
- **Frequencies**: low boost ~100 Hz and upper boost ~2-3 kHz `[?]` (recollection; not verified). Chainsaw tone = those two big boosts with a mid scoop between.
- **Signature**: extremely high gain, EQ-driven character, asymmetric first stage then symmetric clip.

### 2.14 Boss Metal Zone MT-2 -> Heavy Dist, Killer Z
- Two cascaded gain stages with diode clipping, then a 3-band EQ with a sweepable parametric mid `[R]` (not read this session; MT-2 is widely documented). The Helix `Heavy Dist` has only Drive/Bass/Mid/Treble/Output `[S]`, so the sweepable mid is not exposed (it is fixed at the model's voicing).
- Signature: deep scoop with a mid-sweep spike, very high gain, hard clip.

### 2.15 Z.Vex Fuzz Factory -> Industrial Fuzz
- **Source** `[S]` ([MusicRadar FX files](https://www.musicradar.com/how-to/the-fx-files-zvex-fuzz-factory)): two NOS germanium transistors, similar to a modded Fuzz Face; five controls (Gate, Compress, Drive, Stability, Volume); at least two controls shift transistor bias; Stability sets feedback (squeal/oscillation) pitch.
- **Helix** `Compress`, `Gate`, `Drive`, `Stability`, `Oscillator`, `Level`: five match; `Oscillator` is not a Fuzz Factory control `[?]`.
- **Signature**: gated, spitty, oscillating; the most non-linear model in the set; likely **hard to match** by parametric search.

### 2.16 Dallas Rangemaster -> Deranged Master
- **Source** `[S]` ([Vintage Guitar](https://www.vintageguitar.com/20424/the-dallas-rangemaster), [ElectroSmash Rangemaster](https://electrosmash.com/dallas-rangemaster)): one Ge transistor (OC71/OC44/NKT275), 5 nF input capacitor (`[D]` this high-passes at ~1 kHz into a 5 kΩ-order input impedance) and a Boost pot.
- **Signature**: treble boost with soft transistor clipping when pushed; asymmetric.

### 2.17 Tycobrahe Octavia -> Tycoctavia Fuzz, Octave Fuzz
- **Source** `[S]` ([Fuzz Central Octavia](https://fuzzcentral.ssguitar.com/octavia.php), Roger Mayer design, runs at higher voltage): Ge fuzz plus a **transformer-based full-wave rectifier** that doubles the frequency (octave up). Output has two Ge diodes.
- **Signature** `[D]`: full-wave rectification produces strong even harmonics (2nd, 4th). The upper octave is most obvious on single notes above the 12th fret and on neck pickups.

### 2.18 Chandler Tube Driver -> Valve Driver, Tube Drive
- **Source** `[S]` ([DIYStompboxes, starved plate](https://www.diystompboxes.com/smfforum/index.php?msg=121795)): Tube Driver-type pedals run a 12AX7 at very low plate voltage ("starved plate"); some pass the signal through an IC before the tube. Exact circuit `[?]`.
- **Signature** `[R]`: tube grid/plate clipping at low voltage; asymmetric, even-order rich, compressing, with a loose low end.

### 2.19 Earlier-listed items that are not in Helix as a drive
- Marshall Bluesbreaker, Maestro FZ-1 (only via Buzz Saw), MXR Distortion+ (only via Top Secret OD / KWB), BD-2. Horizon Precision Drive: tight TS-style, with a 6-position Attack switch (baseline bass response), Bright (HF cut) and a downward-expander gate `[S]` ([Andertons](https://www.andertons.co.uk/guitar-dept/guitar-pedals/overdrive-pedals/horizon-devices-precision-drive-overdrive-gate-pedal), [RockBoard](https://www.rockboard.de/en/pedalPedia/Horizon-Devices/Precision-Drive/192205897/)); stage values `[?]`.

## Part 3. Implications for the project

1. **Class, not model, is the first decision.** About 12 distinct circuit classes cover the 61 models. Determine the class (clip topology + gain-leg corner + post-EQ) first, then choose within a group.
2. **The strongest discriminators between classes are measurable on a swept sine and a two-tone (see `distortion-identification.md`)**: symmetric vs asymmetric (2nd harmonic), soft vs hard knee (harmonic growth with level), pre-clip HPF corner (whether the bass is clipped), post-clip LPF corner, and the overall slope into the fuzz class.
3. **Where Helix may differ from the pedal**: tapers, level matching, `Voltage`, bass-cut options, model "Voltage" and sag. Do not read the pedal's component values into a Helix knob without a Native sweep.
4. **Each knob needs a Native measurement of its own effect on two things**: the gain-leg HPF corner (it moves in the Dist+; barely in the TS) and the post-clip LPF corner (it moves in the RAT `Filter` and the TS `Tone`).
5. **Confusable pairs that are the same pedal under two names** (current and legacy: Triangle/Fuzz Pi, Arbitrator/Facial, Tycoctavia/Octave, Valve Driver/Tube Drive, Heavy Dist/Killer Z, Vermin/Ratatouille/Classic) should be scored as sound-alike by the benchmark; a "wrong" answer within these is not a miss the ear will notice. This ties to the near-duplicate handling in `docs/notes/synthetic-benchmark-design.md` (NEAR_DELTA, SAME_SOUND_DB).

## Part 4. Core set to get right first

Twelve models, one or two per distinct circuit class, each the best-documented member of its group, and together covering the clipping topologies. Reasoning in brackets.

| # | Model | Class it stands for | Why |
|---|---|---|---|
| 1 | Scream 808 | A: feedback soft-clip, 720 Hz gain-leg, hump | The most-used drive in recordings; also stands for Screamer, Stupor, Pillars, Dhyana |
| 2 | Minotaur | B: clean/dirty blend, Ge ground clip, 18 V headroom | Transparent; unique blend behaviour; also Teemah, Heir Apparent, Prize Drive |
| 3 | Vermin Dist | D: LM308 slewed, hard ground clip, LPF | Defines the "distortion" class; also Ratatouille, Classic Dist, Vital Dist |
| 4 | Top Secret OD | C: op-amp, gain-leg that shifts with gain, diodes to ground | Cheap, different from TS; also Overdrive, KWB |
| 5 | Deez One Vintage | E: two-stage 60 dB, tilt tone | A DS-1 sound is distinct and common; also Deez One Mod, Hedgehog D9 |
| 6 | Compulsive Drive | P: MOSFET asymmetric | Only asymmetric-op-amp class; also tests `LPHP` switch |
| 7 | Triangle Fuzz | G: cascaded diode clip stages, scoop tone | Big Muff class; also Bighorn, Dark Dove, Fuzz Pi, Ballistic |
| 8 | Arbitrator Fuzz | H: Ge transistor, asymmetric, low Zin | Fuzz Face class; also Facial, and a proxy for Ge fuzz |
| 9 | Swedish Chainsaw | F: active gyrator EQ | EQ-dominated class; shows the post-EQ matters more than the clip |
| 10 | Heavy Dist | F: two stages + mid-sweep scoop | Second metal topology (with Killer Z duplicate) |
| 11 | Valve Driver | N: starved-plate tube | Only tube class; also Tube Drive |
| 12 | Kinky Boost | K: near-linear boost | Tests that "no distortion" can be matched; the null case |

Next ring (after the core set is working): Teemah! (Timmy, different gain structure), Heir Apparent / Tone Sovereign (KoT), Jumbo Fuzz (Tone Bender), Tycoctavia Fuzz (octave), Colordrive (BJT overdrive), Horizon Drive, Obsidian 7000 (bass), Industrial Fuzz (very non-linear).

Not recommended as targets at first: Bitcrusher (excluded), Megaphone, Xenomorph Fuzz, Jet Fuzz (time-varying), Industrial Fuzz (unstable), Thrifter Fuzz and L6 Distortion (no real pedal to learn from).

## Unverified list (do not build on these without checking)

- Fuzz Face feedback topology details and closed-loop gain (2.7); Tone Bender MkII Sustain placement (2.8); Maestro Fuzz-Tone transistor count (2.8).
- DS-1 tone network corner (2.5); HM-2 band frequencies (2.13); MT-2 stage values (2.14); Klon mid-hump and tone-shelf values (2.2); OCD Drive pot range, op-amp part and HP/LP caps (2.12); Timmy component values (2.10); Big Muff stage gains and the Russian tone stack (2.6); Dallas Rangemaster impedances (2.16).
- The Ram's Head vs Triangle scoop direction (2.6): a source and my arithmetic disagree.
- Real circuits behind Alpaca Rouge (Red Llama; I could not read a schematic; the Red Llama's lineage was not confirmed), Legendary Drive, Thrifter Fuzz, L6 Distortion, Ballistic Fuzz clip details, Pocket Fuzz, Megaphone, Ampeg Scrambler, Regal Bass DI, ZeroAmp Bass DI, Obsidian 7000, Bronze Master.
- Hedgehog D9 / SD9 topology (a review says mid scoop; the class assignment E is a guess).
- Whether the Native render matches any of these numbers: every frequency here is a prior to test, not a measurement.

## Sources used

- [Line 6 Helix models (3.80)](https://line6.com/helix-models/) and [dshowmusic copy](https://dshowmusic.com/line-6-helix-effect-models/) for the model-to-pedal map (also [guitarchalk list](https://www.guitarchalk.com/line-6-helix-effects-list/), an older partial copy).
- [ElectroSmash](https://electrosmash.com/tube-screamer-analysis): Tube Screamer, [Klon Centaur](https://electrosmash.com/klon-centaur-analysis), [ProCo RAT](https://electrosmash.com/proco-rat), [Big Muff Pi](https://electrosmash.com/big-muff-pi-analysis), [Fuzz Face](https://electrosmash.com/fuzz-face), [MXR Distortion+](https://electrosmash.com/mxr-distortion-plus-analysis), [BOSS DS-1](https://electrosmash.com/boss-ds1-analysis), [Dallas Rangemaster](https://electrosmash.com/dallas-rangemaster). All reached through search summaries only.
- [Coda Effects: Klon](https://www.coda-effects.com/p/klon-centaur-circuit-analysis.html), [Coda Big Muff tone stack](https://www.coda-effects.com/p/big-muff-tonestack-dealing-with-mids.html), [Coda Fuzz Face](https://coda-effects.com/p/circuit-analysis-fuzz-face.html).
- [Yeh, Abel, Smith, DAFx-07, "Simplified, physically-informed models of distortion and overdrive guitar effects pedals"](https://dafx.de/paper-archive/details/mIq7vlQKu6pUlF467vvhgw): models the Distortion+ and the Tube Screamer as conditioning filter, memoryless nonlinearity and equalization filter. Only the abstract was read.
- Unread but promising: a DAFx-26 paper (`DAFx26_paper_24.pdf`; the fetch returned binary, title unknown), the Guitar Science Big Muff paper (`guitarscience.net/papers/bigmuff.pdf`, binary), Yeh's Stanford thesis on distortion circuits (not fetched).
- Forums and reviews as linked inline (PedalPCB, DIYStompboxes, Premier Guitar, Legendary Tones, Vintage Guitar, MusicRadar, Andertons, Reverb, effectsdatabase).
