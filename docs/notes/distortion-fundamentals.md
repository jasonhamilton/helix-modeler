# Distortion pedals from first principles: gain, clipping, EQ

Task [T167](../../board/TASKS.md#t167 "TOP PRIORITY (human 2026-10-18)")(a), research, 2026-10-18. Human's model: a distortion pedal is (1) a gain stage, (2) a clipping stage, (3) an EQ stage. This note tests that model against the published circuit analyses and DAFx/AES literature, gives numbers wherever a source has them, and ends with measurable fingerprints.

How to read the citations. Every claim carries a bracketed tag that resolves to a link in "Sources" at the end. Tags marked (U) are unverified or conflicting; they are collected in "Unverified and conflicting" below. "Derived" means I computed it from a stated formula or component values; the inputs are cited. Summaries are in my own words; no schematics are reproduced.

Access caveat: electrosmash.com and geofex.com could not be fetched from this machine (DNS failure and connection resets). The ElectroSmash numbers below were read from a public mirror of the site's pages (electrosmash.mas-effects.com, the "ElectroSmash Archive"); the tags point to the mirror and the canonical URL is given beside it. R.G. Keen's GEO articles were not read directly; the only GEO-derived facts here come from forum posts that quote them [MEF-RGK]. Several DAFx PDFs downloaded as compressed binary and could not be text-extracted, so for those papers I used the abstracts only and say so.

---

## 0. Verdict on the three-stage model

The model holds as a first-order description and matches how the academic literature structures pedal models. Yeh, Abel and Smith describe the Distortion and Tube Screamer pedals as a cascade of a conditioning filter, a memoryless nonlinearity and an equalisation filter, with the primary distortion mechanism being a diode clipper that has a low-pass filter embedded in it [YEH07]. Eichas and Zölzer fit the same shape (linear filter, nonlinear block, linear filter) to measured pedals [EICHAS15][EICHAS16].

Three refinements matter for this project:

1. The "gain stage" is not only a level knob. In most pedals it is also a filter (a high-pass in the gain leg, a low-pass in the feedback path), so it is part of the pre-clip EQ. It also sets how far the clipper is driven.
2. The clipping stage is not purely static. Capacitors sit inside or around it, and the signal source (the guitar) interacts with the first stage in fuzzes. Section 5 covers where the cascade view fails.
3. EQ appears twice, before and after the clipper, and the two positions do different jobs (section 4).

---

## 1. Boost vs overdrive vs distortion vs fuzz

### 1.1 Working definitions from the sources

| Family | Intent | Where it clips | Typical signature |
|---|---|---|---|
| Boost | Raise level; ideally no added harmonics | Only if overdriven; example gains are 26 dB (MicroAmp) and 38 dB (Rangemaster, which soft-clips above its 2.6 kHz corner) [ES-RM] | Treble booster, clean boost, or "push the amp" |
| Overdrive | Imitate a tube amp at breakup, with a response that follows picking strength [WIKI-OD] | Soft: diodes in an op-amp feedback loop, or gentle shunt clipping | Dynamic, cleans up with the guitar volume, moderate sustain |
| Distortion | A more extreme, more compressed and saturated tone that depends less on playing dynamics [WIKI-OD][WIKI-DIST] | Hard: diodes to ground after the gain stage, or cascaded rail clipping | Tight, sustained, buzzy highs, strong compression |
| Fuzz | Extreme clipping that approaches a square wave, buzzy and harmonically rich [WIKI-OD][WIKI-DIST] | Transistor saturation and cutoff in a feedback pair, or cascaded diode stages with huge gain | Gated, sputtery at low battery, very input-impedance-sensitive |

The boundaries are blurry in practice. Wikipedia itself says "distortion" is the more extreme end of the same effect as "overdrive" [WIKI-OD]. The Big Muff, labelled fuzz, is built from two diode-clipped stages, and ElectroSmash measures a total gain around 77 dB for it [ES-BM]. The Klon, labelled overdrive, uses hard-clipping germanium diodes to ground [ES-KLON][CODA-KLON]. Use the circuit, not the name.

### 1.2 Gain ranges and clipping places, measured from circuit analyses

| Pedal | Gain range | Where clipping happens | Source |
|---|---|---|---|
| Dallas Rangemaster (booster) | about 38 dB, one germanium stage; the comparison MXR MicroAmp is 26 dB | Input cap and about 12 kohm input make a 2.6 kHz corner; above that, full gain and soft clipping | [ES-RM] |
| Tube Screamer (overdrive) | 12 to 118 (21.5 to 41.5 dB) | Diode pair in the op-amp feedback loop | [ES-TS][STOMP-TS9] |
| Klon Centaur (overdrive) | ES: up to about 40 dB near 1 kHz; a build article calculates 4.6 to 25.8 (13 to 28 dB) for the saturating path | Germanium diode pair to ground after the op-amp, summed with a clean path | [ES-KLON][CODA-KLON] |
| MXR Distortion+ | 1.5 to 213 (3.5 to 46.5 dB) | Germanium (1N270) pair to ground after a 10 kohm resistor | [ES-DP] |
| Boss DS-1 | transistor booster 35 dB, then op-amp 0 to 26.5 dB | 1N4148 pair to ground (about 0.7 V); the booster itself soft-clips asymmetrically | [ES-DS1] |
| ProCo RAT | 1 to 2305 (0 to 67 dB) at low frequencies, falling to about 30 dB at 10 kHz at maximum | 1N914 pair to ground after the op-amp | [ES-RAT] |
| Marshall Guvnor | 0 to 33.3 dB, then 36 dB inverting stage, about 70 dB total | Red LEDs (1.8 to 2 V), a hard knee | [ES-GUV] |
| Big Muff Pi | input 16.7 dB, clip stages 23 and 25 dB, output 13 dB, about 77 dB total | Diode pairs in the collector-base feedback of two stages, about 0.6 V each | [ES-BM] |
| Fuzz Face (germanium) | about 48 dB total | Transistor saturation: soft and asymmetric when small, hard on both half-cycles when large | [ES-FF][ES-GEFF] |

The gain that matters is the gain beyond the point where the clipper engages. With a diode clipper, extra gain only makes the clipped region wider, which is why raising Drive on a Tube Screamer changes the wave shape more than the volume. ES makes the same observation: the diodes limit the effective top gain, and the rest of the Drive range changes the slope and shape of the clipped signal [ES-TS].

### 1.3 Harmonic spectra, hardness, and dynamics

- Symmetric hard clipping drives a sine towards a square wave. A square wave holds only odd harmonics with amplitude proportional to 1/n [WIKI-CLIP]. Derived: relative to the fundamental that is -9.5 dB at the 3rd, -14.0 dB at the 5th, -16.9 dB at the 7th, -19.1 dB at the 9th.
- Symmetric soft clipping (tanh, arctan, a diode pair in feedback) also contains only odd harmonics but with much faster roll-off. A summary of waveshaper analyses says that soft-clipping functions such as tanh and arctan add only odd harmonics, and that unbalancing the shaper (different gain for positive and negative samples) introduces even ones [WAVESHAPER-SEARCH]. This is a statement about symmetry, not softness. Wikipedia's clipping article says soft clipping gives even harmonics, which conflicts with this unless the soft clipper is asymmetric; I treat symmetry as what decides odd versus even (U, see list).
- Asymmetric clipping (different thresholds up and down, for example one diode against two in series) adds even harmonics. The Boss SD-1 uses one diode one way and two the other, a 2:1 threshold ratio [GPX-DIODE][SD1-GW]. A pedal-forum rule of thumb says 2:1 mimics triode behaviour, 1.6:1 is pleasant and 3:1 is extreme [MADBEAN] (U, opinion, search summary only).
- Touch sensitivity and clean-up. The germanium Fuzz Face has a very low input impedance (about 5 to 8 kohm per ES) so the guitar's volume knob changes the source impedance and the drive: an ES build article reports tones from a mild crunch at 10% guitar volume to full overload at 90% [ES-GEFF][ES-FF]. Wikipedia's Fuzz Face entry says the same about cleaning up with the volume control [WIKI-FF]. The Tube Screamer, by contrast, has an input buffer near 446 kohm, so the guitar's volume change acts mostly as a level change [ES-TS].
- Sustain follows from compression: ES measures the Big Muff clip stages as limiting each stage to roughly plus or minus 0.6 V, with about 77 dB of total gain, so weak notes are lifted to the same output level as strong ones [ES-BM].
- Gating and sputter: a germanium Fuzz Face can gate when Q2 saturates and breaks the feedback loop, giving a "ripping velcro" cutoff of decaying notes [PPCB-BJT]. Low battery voltage and temperature change the bias of germanium fuzzes, and the sound drifts as the pedal warms [WIKI-FF][PPCB-BJT]. The Boss HM-2 has a passive back-to-back diode gate that makes crossover distortion as the signal passes through zero [MIROSOL] (U, from a search summary of the page; the fetched page text did not repeat it).

---

## 2. The gain stage

### 2.1 Non-inverting op-amp with frequency-dependent gain

Non-inverting op-amp gain is 1 + Rf/Rg. When Rg is in series with a capacitor, the gain falls towards 1 (0 dB) below the corner fc = 1/(2 pi Rg Cg). That makes the gain leg a pre-clip high-pass: bass passes at unity gain rather than being amplified into the clipper.

| Pedal | Gain leg | Corner (derived or stated) | Feedback or output filter | Source |
|---|---|---|---|---|
| Tube Screamer | 4.7 kohm + 47 nF | 720 Hz; below it gain falls | Feedback cap 51 pF across 51 kohm + Drive pot: pole 61 kHz at minimum Drive, about 5.7 kHz at maximum | [ES-TS][SANDEL]; the 61 kHz and 5.7 kHz figures also derived |
| After the TS clipper | 1 kohm + 0.22 uF to ground | 723 Hz low-pass | Tone control then adds a 3.2 kHz action | [ES-TS][MEF-RGK] |
| MXR Distortion+ | R4 + C3 | 723 Hz at maximum gain, 3.4 Hz at minimum gain | C5 low-pass at 15.9 kHz | [PG-DP][ES-DP] |
| Boss DS-1 op-amp | VR1 100 kohm / R13 4.7 kohm gives 22.3 (26.5 dB) maximum | high-pass 72 Hz at maximum, 3 Hz at minimum; low-pass 7.2 kHz | the transistor booster ahead has 3.3 and 33 Hz high-pass corners | [ES-DS1] |
| ProCo RAT | 47 ohm + 2.2 uF and 560 ohm + 4.7 uF | 1539 Hz and 60 Hz (derived: 1/(2 pi RC)), ES gives the same | 100 kohm + 100 pF feedback pole at 16 kHz | [ES-RAT] |
| Boss Blues Driver | two cascaded discrete gain stages | stage 1 about 700 Hz, stage 2 about 72 Hz with more bass gain | simulated inductor bass boost about 120 Hz (+6 dB) | [PG-BD2] |
| Marshall Guvnor | op-amp input stage | 16 Hz and 723 Hz high-pass; 13.2 kHz input low-pass; 15.3 kHz output low-pass | | [ES-GUV] |

Reading the table: 720 Hz-ish corners (TS, Distortion+, Blues Driver stage 1, Guvnor) belong to the mid-focused family. The RAT's two corners (60 Hz and 1.5 kHz) give it a different shape: the 47 ohm leg lifts gain only above 1.5 kHz, so the RAT is bass-heavy where a TS is not, and ES notes that the finite gain-bandwidth product makes low frequencies get more gain than high ones at the top of the range [ES-RAT][PPCB-RAT]. The DS-1's pre-clip corners are far lower (72 Hz), which is why it is described as less mid-focused than a TS (U, inference from the numbers).

### 2.2 Discrete stages: BJT, JFET, MOSFET, germanium and silicon fuzz

- Common-emitter BJT stages. The DS-1 booster is two high-pass filters in series followed by a common-emitter stage with 35 dB gain; it removes bass before the op-amp and adds a little distortion on its own, about 9 Vpp output from 200 mVpp input with asymmetric soft clipping [ES-DS1][WIKI-DS1].
- Fuzz Face. Two transistors with a 100 kohm shunt-series feedback from Q2's emitter to Q1's base; ES measures stage gains of 49 dB theoretical for Q1 cut to 18.6 dB by feedback, with the output stage at 8.2 to 18 dB depending on the Fuzz pot [ES-FF]. Bias targets: Q1 collector about -0.5 to -0.7 V and Q2 collector about -4.5 V (half the 9 V supply) for a germanium build [ES-GEFF]. Best results come from a lower-gain first transistor (hFE about 80) and a higher-gain second (about 120) [ES-GEFF]. Leakage in Q1 is strongly temperature dependent and shifts bias [PPCB-BJT].
- Germanium versus silicon. Early Fuzz Faces used germanium (NKT275, AC128); silicon replaced it from the late 1960s for stability. Germanium has less gain and less noise and cleans up with the guitar volume; silicon can sound thin and brittle when played the same way [WIKI-FF]. ES gives a silicon second-stage gain range of about 8.5 to 230 (18.6 to 47 dB) [ES-FF] (U for the exact Si vs Ge split: the page text I read mixes both).
- Tone Bender MkII. Three germanium OC75 transistors with more gain, a deeper clip and more sustain than a Fuzz Face; the MkI has two transistors and less gain [TB-PPCB][TB-EDB]. Both are leakage-biased and temperature-touchy.
- JFET and discrete op-amp. The Blues Driver uses two FETs facing each other plus a bipolar transistor to make a "discrete op-amp"; its clipping diodes plus overdriven stages give an asymmetric clip [PG-BD2]. The Fulltone OCD overdrives a JFET op-amp (TL082) then hard-clips with MOSFETs [OCD-DIYSB].
- MOSFET. The OCD clipper uses MOSFET body diodes (the MOSFET type itself barely matters per a builder thread) and, from v1.4 on, a germanium diode as the third element for asymmetry [OCD-DIYSB][OCD-DAFX] (U on forum details).

### 2.3 Input impedance and pickup loading

Input impedance sets how much the pedal loads the pickup (a high-impedance source with inductance and cable capacitance). Low impedances drain highs and add guitar-volume interaction.

| Pedal | Input impedance | Source |
|---|---|---|
| Tube Screamer | 446 kohm | [ES-TS] |
| ProCo RAT | 494 kohm | [ES-RAT] |
| Boss DS-1 | 470 kohm | [ES-DS1] |
| MXR Distortion+ | about 1.18 Mohm | [ES-DP] |
| Klon Centaur | about 1 Mohm (buffered when off) | [ES-KLON][CODA-KLON2] |
| Big Muff Pi | about 39 kohm ("tone sucking" risk) | [ES-BM] |
| Fuzz Face | about 8 kohm calculated, 5.2 to 8.4 kohm over the Fuzz range | [ES-FF] (U, a search summary of the same site quoted 500 to 900 ohm at 1 kHz) |
| Dallas Rangemaster | about 10 to 12 kohm | [ES-RM] |

### 2.4 Bias, supply and headroom

- Single 9 V supply, 4.5 V virtual ground: the op-amp output can swing about 4.5 V either side of the bias before the rails, and an 18 V supply puts the bias at 9 V and doubles the swing [WAMPLER]. The JRC4558 output stays about 1.5 V inside each supply rail [ES-JRC], so a 9 V pedal clips its op-amp around plus or minus 3 V (derived).
- Diode clipping hardly changes with supply voltage; JFET or op-amp rail clipping does [WAMPLER]. The Boss MT-2 clips its op-amp at roughly 8 Vpp before its diodes get involved [ED-MT2].
- Klon: a MAX1044 charge pump makes +18 V, +9 V, -9 V and a 4.5 V bias, giving about 27 V total headroom and more clean headroom before the op-amp clips [ES-KLON][CODA-KLON2].
- Fuzzes use the battery as part of the bias network; its voltage and internal resistance are audible [PPCB-BJT].

### 2.5 Op-amp slew rate and bandwidth

- The RAT's LM308 with a 30 pF compensation capacitor has a slew rate of 0.3 V/us, about 40 times slower than a TL071, and ES says the gain-bandwidth limit cuts the 67 dB maximum gain to about 30 dB at 10 kHz [ES-RAT]. Forum posts give 0.15 V/us for the same part and 0.3 V/us as "just below detection" (U) [PPCB-LM308][DIYSB-4558]. A forum claim of 13 V/us for an LM308 in the RAT is contradicted by ES (the 13 V/us figure matches a TL07x) [PPCB-RAT].
- Slew limit math (derived): the largest sine a part can reproduce satisfies A x 2 pi f <= SR. At 0.3 V/us and a 3.5 V swing, that is about 13.6 kHz. ES states an audible limit of 5.3 kHz for the RAT [ES-RAT], which corresponds to about a 9 V amplitude and so looks too pessimistic for a 9 V supply (U).
- The effect reported by builders is a softer top end at high gain: slow slewing cannot follow the fast edges of a clipped waveform, so the highest harmonics are lost. TL07x is about 12 V/us and JRC4558 about 1.7 V/us on datasheets, which is why TL072 is called brighter [DIYSB-4558] (U, forum numbers). Wikipedia notes that the type of op-amp in a TS has little effect compared with the diodes [WIKI-TS].

---

## 3. The clipping stage

### 3.1 Topologies

| Topology | How it works | Examples | Sound |
|---|---|---|---|
| A. Diodes in the op-amp feedback loop | The diodes shunt the feedback resistor. When the voltage across them nears the knee the stage gain falls smoothly towards 1, so the output rises more slowly with input. | Tube Screamer [ES-TS][STOMP-TS9], Timmy, Zendrive [GPX-DIODE] | Soft, rounded, dynamic. STOMP-TS9 notes non-linearity starting about 200 to 250 mV, below the 0.6 to 0.7 V turn-on, because diode resistance already falls there [STOMP-TS9]. |
| B. Diodes to ground after a resistor | A divider formed by a series resistor and the diodes' falling resistance caps the voltage at about one forward drop. | RAT [ES-RAT], DS-1 [ES-DS1], Distortion+ [ES-DP], Klon [ES-KLON] | Harder, flatter tops. |
| C. Transistor or op-amp rail clipping | The active device runs out of voltage swing or leaves its linear range. | Fuzz Face [ES-FF], Blues Driver stages [PG-BD2], MT-2 op-amp [ED-MT2] | Depends on the supply, bias and device; changes with battery voltage. |
| D. Diodes in the collector-base feedback of a transistor stage, cascaded | Each stage soft-clips; two stages in a row approach hard clipping. | Big Muff [ES-BM][WIKI-BM] | Thick, sustained, with strong compression. Werner et al. use the Big Muff clipping stage as the example of a mixed transistor-and-diode circuit [WERNER15]. |
| E. Clipper in parallel with a clean path | Output = clipped plus clean; the blend control moves between them. | Klon [ES-KLON][CODA-KLON] | Keeps the original note and attack on top of distortion. |

The builder shorthand "feedback-loop diodes are soft, shunt diodes are hard" holds as a rule of thumb [GPX-DIODE]. A caveat from Boss circuits: DS-1 and Blues Driver both have a diode clipper *and* an overdriven earlier stage, and a builder analysis of the BD-2 says most of its distortion character comes from the overdriven gain stages, not the diodes [PG-BD2]. Likewise ED's MT-2 analysis says frequency response defines that pedal's sound much more than diode choice [ED-MT2].

### 3.2 Devices and forward voltages

| Device | Forward voltage | Knee and effect | Source |
|---|---|---|---|
| Schottky (BAT41, BAT46, 1N5819) | about 0.25 to 0.36 V | Earliest clip, most compression | [GPX-DIODE] |
| Germanium (1N34A, D9E, AA116, 1N270) | about 0.2 to 0.35 V (1N34A about 0.21 V measured; samples span 0.19 to 0.32 V); ES quotes 0.3 to 0.45 V for 1N270 | Clips early, heavy compression, low output; sample-to-sample spread is large | [GPX-DIODE][ES-DP][PPCB-DIODE] |
| Silicon (1N4148, 1N914, 1S1588) | about 0.57 to 0.7 V (1N4148 0.68 V); some samples under 0.5 V | The reference for TS, DS-1, RAT, Big Muff; moderate compression | [GPX-DIODE][PPCB-DIODE] |
| LED | red about 1.7 V; blue, white, UV 3.2 to 3.8 V | Late clip, least compression, more output and dynamics | [GPX-DIODE] |
| MOSFET (body diode) | not verified | Used in the OCD; asymmetry from a germanium diode as the third element | [OCD-DIYSB][OCD-DAFX] (U on voltage) |

Derived consequences of forward voltage, using the ceiling set by the knee:

- Output ceiling ratio relative to silicon (20 log10 of the Vf ratio): germanium 0.3 V is about 6.7 dB lower; Schottky 0.25 V about 8.3 dB lower; red LED 1.7 V about 8.4 dB higher; blue LED 3.2 V about 13.9 dB higher. This is why LED and germanium versions of the same circuit need very different makeup volume. Pedal builders say the same in words: lower Vf means earlier and heavier compression [GPX-DIODE].
- Two diodes in series double the threshold (and the ceiling by +6 dB); an odd count in the two directions makes the clip asymmetric [GPX-DIODE]. "Even pairs produce smoother results; 2:1 ratios create textured tonality (Boss OD-1, SD-1)" [GPX-DIODE].
- Knee shape. Pedal builders say germanium knees differ in "curvature" between sample types [GPX-DIODE] and call germanium diodes soft; I found no measurement that proves a softer germanium knee, so treat "softer germanium knee" as (U). What the equation says: the Shockley model gives a current that rises by a factor of ten for each n x 59.5 mV (derived from nVT ln 10 with VT = 25.85 mV [WIKI-SHOCK]). A silicon 1N4148 modelled with n = 1.752 (the common SPICE card, Is = 2.52 nA [SPICE-1N4148]) rises ten-fold per about 104 mV and reaches 0.375 V at 10 uA, 0.48 V at 100 uA, 0.58 V at 1 mA, 0.69 V at 10 mA (derived). So the visible "knee" spans about 0.3 V in silicon. A germanium diode with a similar n would show the same knee width at a lower voltage (U); relative to the audio signal it then sits further past the knee, sounding harder. Germanium SPICE cards found online gave Vf inconsistent with measurements (a 1N34A card with Is = 200 pA and n = 2.19 yields about 0.87 V at 1 mA by the formula, derived) so I do not rely on them (U) [SPICE-1N4148].

### 3.3 Symmetric versus asymmetric clipping

- Rule: an odd-symmetric transfer function f(-x) = -f(x) can only create odd harmonics; any asymmetry or DC offset adds even ones. Pakarinen and Yeh's review of tube-amp modelling states the same for guitar preamp clipping [PAKARINEN09] (tool-summarised PDF; consistent with [WIKI-DIST]).
- Small-signal laws (derived): with f(x) = x + a x^2 the 2nd harmonic is a A^2 / 2 and rises 2 dB per dB of input; with a cubic term the 3rd rises 3 dB per dB. So even harmonics dominate at low drive in asymmetric stages and odd ones take over as clipping gets heavy.
- Examples: Tube Screamer, RAT, DS-1 diode pair, Distortion+, Klon, Big Muff: symmetric diode pairs [ES-TS][ES-RAT][ES-DS1][ES-DP][ES-KLON][ES-BM]. SD-1 and OD-1: 1 against 2 diodes [GPX-DIODE]. Fulltone OCD: asymmetric by design [OCD-DAFX]. HM-2: an asymmetric clipping amplifier, then two clipper pairs [MIROSOL]. Fuzz Face: Q1 collector at about -1.6 V at the mid Fuzz setting gives asymmetric soft clipping for small signals, but both half-cycles hard-clip for large ones [ES-FF]. Blues Driver: differential stage clips asymmetrically [PG-BD2]. DS-1 booster: asymmetric soft clip [ES-DS1].
- A PedalPCB thread that calls the stock RAT's two 1N914 diodes "asymmetrical" is a misstatement: anti-parallel diodes are symmetric, as ES's schematic reading says [PPCB-MUR][ES-RAT] (U on that thread).

### 3.4 Static transfer curves

| Curve | Form | Harmonics | Where it models |
|---|---|---|---|
| Hard clip | clamp(x, -c, c) | odd only, 1/n decay (square wave limit) | Diode to ground at high drive, op-amp rail |
| tanh | tanh(kx) | odd only, faster decay | Generic soft clip; used as the easy diode approximation [BALTIC] |
| arctan | atan(kx) | odd only | Slightly slower knee than tanh; tends to hard clip at high k and many stages [WAVESHAPER-SEARCH] |
| Anti-parallel Shockley pair | I = 2 Is sinh(V / (n VT)) | odd only | Shunt clipper (topology B); derived from the diode equation [WIKI-SHOCK]; exact solution with a series resistor needs the Lambert W function [WERNER15][WERNER-SEARCH] |
| Feedback diode clipper | Vout approximately Vin + (diode drop) at large signal, gain to 1 | odd only if symmetric | Topology A. Gain falls from 1 + Rf/Rg to about 1 as the diodes conduct |
| Asymmetric sum | different k or threshold per polarity | odd and even | SD-1, OCD, HM-2, fuzz |

For these static curves the key shape parameters are the gain before the knee, the knee voltage (device Vf), the knee sharpness (n, series resistance), and the asymmetry ratio. A baltic-lab article that implements the three options (step function, exponential, tanh) says the step-function version is the closest to guitar pedals and tanh gives the softest clip [BALTIC].

### 3.5 Dynamic effects: capacitors around the clipper, bias shift, memory

- Capacitor inside the loop. Yeh et al. identify the diode clipper with an embedded low-pass filter as the heart of both the Distortion and Tube Screamer pedals, and say that solving it needs an ordinary differential equation [YEH07][ECIRCUIT]. With the capacitor, the voltage at the diodes depends on frequency: high notes charge/discharge the capacitor and clip less than low ones at the same input amplitude. So "how hard it clips" is a function of frequency, not only level.
- State count. A neural-ODE study separates a first-order diode clipper (one capacitor voltage) from a second-order one with high-pass filtering and asymmetric clipping (two state variables) [NODE22]. Chowdhury chose recurrent networks for the Klon because its gain stage is stateful [CHOWDHURY20]. A 2025 study of 40 devices extended Wiener-Hammerstein models with hysteresis and memory to match them [DIFFBB25].
- Bias shift and blocking (mechanism from circuit theory, not measured here; (U) as a pedal-specific claim): when a coupling capacitor sits before an asymmetric clipper, loud peaks conduct more on one side, the average current charges the capacitor, and the operating point drifts until the burst ends. The recovery time is set by the coupling RC. This is the likely cause of "sag", "blocking" and sputter in fuzzes. The only sourced instance is Fuzz Face gating when Q2 saturates and the feedback loop stops biasing Q1 [PPCB-BJT].
- Fuzz Face modelling needs circuit-level simulation: Overton's thesis solved Ebers-Moll transistor equations and tabulated the result, and reports the sound was reasonable but not perfectly modelled [OVERTON].

---

## 4. The EQ stage

### 4.1 Per-pedal EQ

| Pedal | Pre-clip EQ | Post-clip EQ | Source |
|---|---|---|---|
| Tube Screamer | Gain-leg high-pass 720 Hz plus the 51 pF feedback pole (6 to 61 kHz): mid-forward | 723 Hz low-pass, then Tone: low-pass to a 3.2 kHz high-pass shelf action; ES reports -6 dB/octave roll-off above the 720 Hz stage | [ES-TS][MEF-RGK][RIGTALK-TS] |
| RAT | gain legs at 60 Hz and 1.5 kHz | passive low-pass "Filter" swept 475 Hz to 32 kHz | [ES-RAT] |
| Big Muff | cascaded Miller-capacitor filtering: below 90 Hz and above 1.2 kHz fall at 60 dB/decade through the clip stages | tone stack blends a low-pass and a high-pass; at the midpoint, a notch near 1 kHz with about 6.5 dB loss (13.5 dB with the overall loss) | [ES-BM] |
| Big Muff variants (corner pairs, low-pass/high-pass) | | Triangle 723/1809 Hz, Ram's Head 1973 482/1206 Hz, Ram's Head 1975 408/1809 Hz (deepest scoop), Russian/Civil War 796/1855 Hz | [CODA-BM] |
| DS-1 | booster high-passes at 3.3 and 33 Hz, op-amp high-pass 72 to 3 Hz | seesaw tone: 234 Hz low-pass vs 1063 Hz high-pass, notch near 500 Hz at centre (about -20 dB), -12 dB overall level loss | [ES-DS1] |
| MXR Distortion+ | input high-pass 23.5 Hz, mid-hump peak about 1.5 kHz | fixed 15.9 kHz low-pass | [ES-DP] |
| Klon Centaur | clean path low-passed (feed-forward 106 Hz, summing 495 Hz) which brings out the mid hump near 1 kHz | treble control: shelf at 408 Hz, +18 to -8 dB | [ES-KLON] |
| Boss MT-2 | high-pass 106 Hz, bandpass about 1 kHz, low-pass 340 Hz (a mid-hump in front of the clipper) | scooped response with gyrator peaks at 4.9 kHz and 105 Hz; Low gyrator 105 Hz; High shelf +/-20 dB; sweepable Mid 240 Hz to 4.7 kHz, +/-15 dB | [ED-MT2] |
| Boss HM-2 | | three active gyrator bands, +/-20 dB; low band about 86 Hz, high band peaks at 900 Hz and 1.2 kHz (U, from a search summary of the page) | [MIROSOL] |
| Marshall Guvnor | 723 Hz and 16 Hz high-pass | bass/mid/treble FMV-type stack, subtle scoop between 1 and 3 kHz | [ES-GUV] |
| Univox Super-Fuzz | | switch engages a 1 kHz filter that scoops the mids | [WIKI-SF] |
| Timmy | Bass cut is a pre-distortion control, Treble cut a post-distortion one | cut-style, not boost-style | [TIMMY-REVERB] |
| Dallas Rangemaster | 2.6 kHz corner from the input cap and input impedance | | [ES-RM] |

### 4.2 Pre-clip versus post-clip EQ

- Pre-clip EQ changes what the clipper sees. A cut or boost before the nonlinearity changes how hard each frequency is driven, so it changes harmonic content and intermodulation. Removing bass before the clipper stops low notes from dominating the clipped shape and muddying the highs; the DS-1 booster and the TS 720 Hz leg do this [WIKI-DS1][ES-TS]. A mid-hump before the clip (TS, MT-2) distorts mids more than lows and highs. (Mechanism: standard nonlinear-systems reasoning; (U) as a quantified effect.)
- Post-clip EQ is a linear filter on whatever the clipper produced. It re-shapes the spectrum (scoop, treble roll-off, the RAT's low-pass) but cannot create or remove harmonic *relationships*. An ES-style tone stack or RAT filter is therefore recoverable from the output alone if the harmonic structure is known. ED's MT-2 analysis argues that post-distortion shaping defines the scooped character that listeners attribute to the pedal [ED-MT2].
- Real pedals use both: the TS has a pre-clip high-pass (720 Hz) *and* a post-clip low-pass (723 Hz), and the MT-2 has a pre-clip mid bandpass *and* a post-clip scoop. A single-position EQ model cannot represent that.

---

## 5. How the stages combine: the Wiener-Hammerstein view

- Wiener-Hammerstein (WH): a linear filter H1, a static nonlinearity f, a linear filter H2 (Eichas and Zölzer fit this to pedals with Levenberg-Marquardt optimisation [EICHAS15][EICHAS16]). It matches the human's three-stage model one to one: H1 holds the gain-leg and input filtering, f is the clipper, H2 is the tone control and output filtering. Yeh et al. derive the same cascade (conditioning filter, memoryless nonlinearity, equalisation filter) from circuit analysis [YEH07].
- Where WH breaks down:
  1. Dynamic clippers. Capacitors across or around the diodes make the nonlinearity stateful and frequency-dependent [YEH07][NODE22]. WH approximates this by moving the filter outside the clipper, which loses level-and-frequency interaction.
  2. Bias drift and memory. Coupling capacitors and asymmetric clipping move the operating point over time; this needs states, not a static f [DIFFBB25][CHOWDHURY20].
  3. Source and load interaction. In a Fuzz Face the pedal's input impedance and the guitar's volume setting change H1 and the gain, so the pedal is not an LTI-then-static system independent of the guitar [ES-FF][ES-GEFF].
  4. Fuzz circuits. Feedback loops in a Fuzz Face and cascaded transistor stages in the Big Muff do not decompose cleanly; Eichas and Zölzer report that some circuits could not be modelled with high accuracy [EICHAS16]. A tool-generated reading of that paper says the Tube Screamer stage was the hardest of three circuits (a diode clipper, a Big Muff stage, a TS stage) but this came from a garbled PDF extraction and I could not confirm it (U).
  5. Multiple clippers. Pedals with cascaded clippers (BD-2, DS-1, MT-2, HM-2, Big Muff) need H-f-H-f-H structures, since linear filters sit between nonlinearities [PG-BD2][ES-DS1][ED-MT2].
  6. Intermodulation is captured by a static nonlinearity only if the pre-filter (and so the amplitude of each frequency component entering the clipper) is right; a wrong H1 gives wrong intermodulation even when single-note harmonics look fine (my inference).
- Implications for modelling (my inference):
  - A block (gain, pre-EQ, clipper with threshold and asymmetry, post-EQ, level) is a good parametric family for TS/RAT/DS-1/Dist+/Klon-like pedals and a poor one for Fuzz Face / Tone Bender / Octavia.
  - Frequency-dependent clipping (capacitor in the loop) should be modelled by a clipper with an embedded low-pass, not by a clipper between two ordinary filters.
  - Identification should treat the pre-clip filter as observable through level-dependent harmonic changes, the post-clip filter through the linear spectral shape (more in track c).

---

## 6. Taxonomy of classic pedals

(U) marks cells I could not verify from a fetched source. Gain figures are for the circuit's controls at minimum to maximum.

| # | Pedal | Family | Gain topology | Clipping device and topology | Sym? | EQ character and key frequencies | Distinguishing sound | Sources |
|---|---|---|---|---|---|---|---|---|
| 1 | Ibanez TS808 / TS9 | Overdrive | Non-inverting op-amp (JRC4558), 12 to 118; 4.7k + 47 nF leg | 2 Si diodes anti-parallel in feedback (soft) | Sym | 720 Hz gain-leg HP; 723 Hz post LP; Tone to 3.2 kHz; 446k in, 1.2k out | Mid-forward, compressed, bass-trimmed; TS9 slightly brighter than 808 | [ES-TS][STOMP-TS9][WIKI-TS] |
| 2 | Boss SD-1 | Overdrive | TS-like op-amp | 1 diode one way, 2 the other, in feedback | Asym 2:1 | TS-like mid-forward (U on values) | Softer, a little more even-harmonic than a TS | [GPX-DIODE][SD1-GW] |
| 3 | Klon Centaur | Overdrive | Op-amp gain path plus clean path, summed; charge pump 18 V | 1N34A pair to ground after the op-amp | Sym | Mid hump about 1 kHz; treble shelf 408 Hz +18/-8 dB; buffer | Transparent, clean blend, large headroom | [ES-KLON][CODA-KLON][CODA-KLON2] |
| 4 | Boss BD-2 Blues Driver | Overdrive | Two discrete FET/BJT gain stages, HP about 700 Hz and 72 Hz, then an op-amp recovery stage | Diode pairs to ground (D7-D10) plus overdriven asymmetric stages | Asym | Bass boost about 120 Hz (+6 dB); bass accumulates | Fuzzy at high gain, responsive to touch | [PG-BD2] |
| 5 | Fulltone OCD | Overdrive / light distortion | TL082 JFET op-amp overdriven | MOSFET body diodes plus 1N34A (v1.4+) | Asym | Switchable "peak" setting (U details) | Tube-like, dynamic | [OCD-DIYSB][OCD-DAFX] |
| 6 | Paul Cochrane Timmy | Overdrive (transparent) | Op-amp (U) | Switchable symmetric (two levels) or asymmetric clipping | Both | Cut-style Bass (pre) and Treble (post); no mid hump | Amp-like, little added colour | [TIMMY-REVERB][TIMMY-THOMANN] |
| 7 | Marshall Bluesbreaker / Analogman King of Tone | Overdrive | Two op-amp stages (TL072), first variable gain; KoT has two such circuits | Soft clipper diodes (1N4148, 1S1588 or MA856) with a series resistor in the feedback path; KoT adds hard-clip option | Sym | Presence trimmer (KoT) (U values) | Amp-like, low gain, stackable | [BB-PPCB][KOT-EDB] |
| 8 | ProCo RAT | Distortion | Non-inverting LM308, 0 to 67 dB; legs 47 ohm/2.2 uF and 560 ohm/4.7 uF | 1N914 pair to ground (hard) | Sym | Passive Filter low-pass 475 Hz to 32 kHz; slew 0.3 V/us | Aggressive, bass-heavy, filter sweeps bright to dark | [ES-RAT][WIKI-RAT] |
| 9 | Boss DS-1 | Distortion | 35 dB BJT booster then op-amp 0 to 26.5 dB | 1N4148 pair to ground (about 0.7 V) | Sym at diodes | Seesaw tone, notch near 500 Hz (-20 dB), -12 dB level | Bright, scooped, buzzy | [ES-DS1][WIKI-DS1] |
| 10 | MXR Distortion+ | Distortion | LM741 non-inverting, 3.5 to 46.5 dB | 1N270 germanium pair to ground (clip about 350 mV) | Sym | HP 723 Hz at max gain; 15.9 kHz LP; hump about 1.5 kHz | Compressed, fuzzy, quiet output | [ES-DP][PG-DP] |
| 11 | Marshall Guvnor | Distortion | Non-inverting 0 to 33 dB, then inverting 36 dB; about 70 dB | Red LEDs (hard knee, about 1.8 to 2 V) | Sym | FMV-type bass/mid/treble, scoop 1 to 3 kHz | Marshall-style crunch to heavy | [ES-GUV] |
| 12 | Boss MT-2 Metal Zone | Distortion | Pre-stage plus main stage; ES-quoted gain range x2 to x252 for the pre-stage | Op-amp rail clip plus Si diodes (plus or minus 0.6 to 0.7 V) after a 2.2k | Sym | Pre-clip mid bandpass about 1 kHz (HP 106 Hz, LP 340 Hz); post-clip scoop; Mid sweep 240 Hz to 4.7 kHz +/-15 dB | Modern metal; EQ-defined | [ED-MT2] |
| 13 | Boss HM-2 | Distortion | Transistor pre-gain plus an asymmetric clipping amp | Soft diodes, hard diodes, germanium pair; passive gate diodes | Asym | Three gyrator bands +/-20 dB (about 86 Hz; 900 Hz and 1.2 kHz) | Swedish "chainsaw"; gating artefacts | [MIROSOL] (U on the band numbers) |
| 14 | EHX Big Muff Pi (Triangle, Ram's Head, Russian) | Fuzz | 4 BJT stages: 16.7, 23, 25, 13 dB; 39k input | Two diode pairs in the collector-base loops of stages 2 and 3 | Sym | Tone stack with scoop near 1 kHz (6.5 to 13.5 dB); corners per variant 723/1809, 482/1206, 408/1809, 796/1855 Hz | Thick wall of sustain, scooped | [ES-BM][CODA-BM][WIKI-BM] |
| 15 | Big Muff op-amp variant | Fuzz | Three op-amp gain stages | (U) | (U) | Mid-heavy, tone-bypass switch | Less scooped | [WIKI-BM] |
| 16 | Fuzz Face (germanium) | Fuzz | 2 PNP transistors, 100k feedback; about 48 dB | Transistor saturation and cutoff | Asym small / hard both when large | Z_in 5 to 8 kohm; HPs at 14, 8 and 31 Hz; Q1C about -0.5 to -1.6 V, Q2C about -4.5 V | Cleans up with guitar volume; temperature and battery drift; gating | [ES-FF][ES-GEFF][WIKI-FF] |
| 17 | Fuzz Face (silicon) | Fuzz | Same, NPN BC108-type | Same | | Same | More gain and brightness; can sound thin or brittle | [WIKI-FF][ES-FF] |
| 18 | Sola Sound Tone Bender MkII | Fuzz | 3 germanium OC75, leakage-biased; MkI has 2 | Transistor stage saturation | | Gain high enough that no treble trimming needed (U) | More gain and sustain than a Fuzz Face | [TB-PPCB][TB-EDB] |
| 19 | Roger Mayer Octavia | Octave fuzz | Transformer phase-splitter and rectifier (first versions germanium, later silicon) | Full-wave rectification then fuzz | | Octave-up (ring-mod-like) | Upper octave, very chord-hostile | [OCTAVIA] |
| 20 | Univox Super-Fuzz | Octave fuzz | Multiple transistors (hFE about 110 to 140 preferred) | Two germanium diodes for square clipping, full-wave rectify | | 1 kHz mid-scoop switch | Octave up plus a slight octave down | [WIKI-SF][SUPERFUZZ-PPCB] |
| 21 | Dallas Rangemaster | Booster | Single germanium PNP stage, about 38 dB | Soft clip above 2.6 kHz | | 2.6 kHz corner | Treble boost for a tube amp | [ES-RM] |
| 22 | Horizon Devices Precision Drive | Tight overdrive | (U) 1N4148 and 2N3904 reported | (U) | (U) | Attack and Bright controls (marketing) | Tight, punchy for high-gain amps | [PD-PPCB][PD-EDB] (U throughout) |

---

## 7. What a listener hears for each stage

These perceptual mappings are the field's general understanding, supported where noted; the quantified links to the circuit numbers are mine.

**Gain stage (how much, and what is amplified)**
- More gain widens the clipped portion of each cycle: more compression, longer sustain, a flatter dynamic envelope, brighter and rougher sound [HERBST][WIKI-DIST]. Herbst's listening tests found both distortion level and chord complexity reduce sensory consonance, and that the complexity of chord intervals matters [HERBST].
- Where the gain-leg corner sits tells the ear how "tight" the pedal is. With a 720 Hz corner the bass is not pushed into clipping and the sound is focused; with 60 to 72 Hz corners (RAT, DS-1) the low strings distort and the sound is fatter (derived from [ES-TS][ES-RAT][ES-DS1]).
- Input impedance gives touch response: a fuzz that loads the pickup changes timbre with the guitar volume, a high-impedance overdrive just gets quieter [ES-GEFF][ES-TS].
- Slew limiting is heard as a softer, less fizzy top at high gain (RAT) [ES-RAT].

**Clipping stage (what shape)**
- Hard clipping: buzz and bite from strong high odd harmonics; soft clipping: rounder, fewer high harmonics [WIKI-DIST][WIKI-CLIP].
- Clipping threshold: low-Vf devices (germanium, Schottky) are heard as more compression and less level; LEDs as open, dynamic and loud [GPX-DIODE].
- Asymmetry: even harmonics, heard as a warmer, "octave-ish" colour and a more tube-like feel [WIKI-DIST][SD1-GW] (U on the "octave" wording, forum language).
- Intermodulation: with several notes at once, clipping makes sum and difference tones. Fifths give the least objectionable products and minor seconds the worst, which is why power chords survive high gain and jazz chords turn to mush [IMD-MEF] (U, forum source).
- Dynamics: gating, sputter, bias drift and cleanup with the pick are the time-domain cues; they come from state (section 3.5) and are the hardest to fake with a static curve.

**EQ stage (where the energy sits)**
- Pre-clip EQ is heard as how the distortion *behaves* (tight vs flubby, which strings break up first); post-clip EQ as the *colour* (scooped, dark, bright, mid-hump) [ED-MT2][ES-BM].
- The 700 to 1000 Hz hump of a TS or Klon pushes the guitar forward in a mix; a Big Muff's 1 kHz scoop sits behind other instruments; a DS-1's 500 Hz notch gives the bright, hollow character (derived from [ES-TS][ES-KLON][ES-BM][ES-DS1]).
- A low-pass at 475 Hz to 32 kHz on the RAT turns the same distortion from dark to fizzy [ES-RAT].

---

## 8. Fingerprints: measurable properties that separate these pedals

Magnitudes are from the cited component values or derived; (U) where I could not source a measured value. "Needs dry" means the property needs a known input; the others can be estimated from the output alone with caveats (track c covers this).

1. **Even-to-odd harmonic ratio (H2 - H3, dB) on a single note.** Symmetric clippers (TS, RAT, DS-1, Dist+, Klon, Big Muff) are ideally odd-only; asymmetric ones (SD-1 2:1, OCD, BD-2, HM-2, fuzzes at low level) add H2. Size: ideal symmetric gives no H2 at all; real units (U) show finite residual from bias and component spread. Strongest single discriminator for symmetry. Needs a known pitch, not the dry signal.
2. **Harmonic growth law with level.** Small-signal slopes of 2 dB/dB (H2) and 3 dB/dB (H3) (derived), flattening as clipping engages; hard clippers pin H3 near -9.5 dB re the fundamental. Differences between the soft TS and the hard RAT show up as the level at which H3 stops growing. Needs two or more levels of the same passage, or a dynamics range inside the track.
3. **Clip ceiling and crest factor.** Ceiling follows the forward voltage: germanium about -6.7 dB, Schottky -8.3 dB, red LED +8.4 dB, blue LED +13.9 dB vs silicon (derived from [GPX-DIODE]). A flat-topped waveform has a crest factor near that of a square wave (0 dB) for hard clipping vs about 3 dB for a sine. Confounded by post-EQ and volume, so use shape, not absolute level.
4. **Knee sharpness.** The input-level span over which THD rises from low to saturated; an exponential diode knee spans roughly 0.3 V in silicon (derived), so the span in dB depends on pre-gain. Soft (feedback-diode TS) versus hard (shunt RAT/DS-1) separates at the high-drive end. (U on typical dB spans.)
5. **Pre-clip corner as a function of note frequency.** Harmonic generation vs pitch: TS, Dist+, BD-2 and Guvnor roll off distortion below about 720 Hz (gain falls toward 1; 6 dB/octave, about 20 dB per decade [ES-TS][PG-DP][ES-GUV]); RAT corners at 60 Hz and 1.5 kHz [ES-RAT]; DS-1 72 Hz [ES-DS1]. Low-E (82 Hz) harmonic content relative to A-string content is the observable. A 1 to 20 dB spread between pedal families.
6. **Post-clip low-pass shape.** RAT 475 Hz to 32 kHz [ES-RAT]; TS 723 Hz and a 3.2 kHz tone action [ES-TS]; Dist+ 15.9 kHz [ES-DP]; DS-1 234 Hz / 1063 Hz seesaw [ES-DS1]. Visible as the high-frequency slope of the output spectrum above 2 kHz; spread of tens of dB at 5 to 10 kHz across settings.
7. **Mid notch or hump.** Hump: TS about 720 Hz to 1 kHz, Klon about 1 kHz [ES-KLON], Dist+ about 1.5 kHz [ES-DP]. Notch: Big Muff near 1 kHz, 6.5 to 13.5 dB [ES-BM]; DS-1 near 500 Hz, -20 dB [ES-DS1]; Guvnor 1 to 3 kHz, shallow [ES-GUV]; Super-Fuzz 1 kHz switch [WIKI-SF]; MT-2 sweepable 240 Hz to 4.7 kHz +/-15 dB [ED-MT2]; HM-2 +/-20 dB peaks (U). Easiest EQ fingerprint, measured on the long-term spectrum divided by a reference guitar spectrum.
8. **High-frequency harmonic roll-off from slew and gain-bandwidth limits.** RAT: gain falls from 67 dB to about 30 dB at 10 kHz [ES-RAT]; slew 0.3 V/us. Observable as missing energy above roughly 5 to 13 kHz at high gain, relative to a TL07x-based pedal (U on size).
9. **Level-dependent clean-up (touch response).** Change in spectral centroid and THD when the input drops by 10 and 20 dB. Fuzz Face: tone moves from near-clean at 10% guitar volume to full fuzz at 90% [ES-GEFF]; Big Muff stays saturated because of 77 dB total gain [ES-BM]; overdrives in between. Needs level variation in the clip, which real playing gives.
10. **Envelope and state effects.** Decay tails that gate (Fuzz Face [PPCB-BJT], HM-2 gate diodes (U)), bias-drift recovery after loud notes, attack bloom. Measured as THD and level versus time after a loud note. Size unverified; qualitative differences are large between fuzzes and op-amp pedals.
11. **Intermodulation on two-note chords.** Two-tone IMD products relative to harmonics; hard clippers with high gain have the highest IMD [IMD-MEF]. Octave fuzzes (Octavia, Super-Fuzz) add a signal at 2f from rectification, so the 2nd harmonic can exceed the fundamental [OCTAVIA][WIKI-SF] (U on amount).
12. **Source-impedance sensitivity.** Only low-impedance fuzzes (Fuzz Face 5 to 8 kohm, Rangemaster 10 to 12 kohm, Big Muff 39 kohm) change tone with the pickup/volume/cable; buffered 400 kohm to 1 Mohm pedals do not [ES-FF][ES-RM][ES-BM][ES-TS]. Estimable only by comparing with different guitars or volume settings, not from one clip.

---

## Unverified and conflicting claims

1. Pedal-specific measurements of how soft the germanium knee is: none found (section 3.2).
2. RAT LM308 slew rate: 0.3 V/us (ES), 0.15 V/us and 0.3 V/us (forum), 13 V/us (forum, likely a TL07x figure) [ES-RAT][PPCB-LM308][PPCB-RAT]. ES's 5.3 kHz audible limit corresponds to about a 9 V amplitude, inconsistent with a 9 V supply (derived).
3. Klon gain: 4.6 to 25.8 (CODA-KLON) versus 40 dB near 1 kHz (ES-KLON); different definitions of the path.
4. Fuzz Face input impedance: 5.2 to 8.4 kohm (ES fetched) versus 500 to 900 ohm (search summary of the same site).
5. ES-mirror anomalies: the TS 51 pF filter was reported as 6.24 MHz to 6.24 kHz (should be about 61 kHz to 5.7 kHz by the component values, 6.24 kHz is for the 500k pot alone); a "1 V" diode forward voltage for the TS contradicts the 0.6 to 0.7 V in other sources. I used the derived values.
6. Wikipedia's clipping article says soft clipping gives even harmonics; the waveshaper literature says symmetric soft clipping is odd-only [WIKI-CLIP][WAVESHAPER-SEARCH].
7. Eichas and Zölzer 2016: which circuits were modelled and which was hardest came from a garbled PDF read; the abstract only says three circuits and that some were hard [EICHAS16].
8. Yeh, Pakarinen, Holters, Werner papers: abstracts only (PDFs not text-extractable here); the Holters EUSIPCO paper was only seen as a search hit [HOLTERS15].
9. Boss HM-2 band numbers and gate-diode behaviour, OCD switch details, Timmy circuit specifics, King of Tone details, Precision Drive circuit, Octavia details, Super-Fuzz transistor count: from search summaries or builder threads (see tags).
10. MOSFET clipping forward voltage: not found.
11. Asymmetry ratios 1.6:1 / 2:1 / 3:1 are one forum opinion [MADBEAN].
12. The bias-shift and blocking description is circuit theory, not a pedal-specific measurement.
13. Not read: Vicanek's waveshaper harmonics note (https://www.vicanek.de/articles/WaveshaperHarmonix.pdf), Yeh's thesis PDF, Esqueda/Parker antiderivative papers, GEO articles.

---

## Sources

ElectroSmash pages (read through the mirror; canonical in brackets).
- [ES-TS] https://electrosmash.mas-effects.com/tube-screamer-analysis.html (canonical https://www.electrosmash.com/tube-screamer-analysis)
- [ES-RAT] https://electrosmash.mas-effects.com/proco-rat.html (https://www.electrosmash.com/proco-rat)
- [ES-FF] https://electrosmash.mas-effects.com/fuzz-face.html (https://www.electrosmash.com/fuzz-face)
- [ES-BM] https://electrosmash.mas-effects.com/big-muff-pi-analysis.html (https://www.electrosmash.com/big-muff-pi-analysis)
- [ES-DS1] https://electrosmash.mas-effects.com/boss-ds1-analysis.html (https://www.electrosmash.com/boss-ds1-analysis)
- [ES-DP] https://electrosmash.mas-effects.com/mxr-distortion-plus-analysis.html (https://www.electrosmash.com/mxr-distortion-plus-analysis)
- [ES-KLON] https://electrosmash.mas-effects.com/klon-centaur-analysis.html (https://www.electrosmash.com/klon-centaur-analysis)
- [ES-GUV] https://electrosmash.mas-effects.com/marshall-guvnor-analysis.html (https://www.electrosmash.com/marshall-guvnor-analysis)
- [ES-RM] https://electrosmash.mas-effects.com/dallas-rangemaster.html (https://www.electrosmash.com/dallas-rangemaster)
- [ES-JRC] https://electrosmash.mas-effects.com/jrc4558-analysis.html (https://www.electrosmash.com/jrc4558-analysis)
- [ES-GEFF] https://electrosmash.mas-effects.com/germanium-fuzz.html (https://www.electrosmash.com/germanium-fuzz)

DIY and builder references.
- [STOMP-TS9] https://stompboxelectronics.com/2023/04/03/an-analysis-of-the-ibanez-ts-9-clipping-circuit/
- [SANDEL] https://sandelinos.me/diy/tube-screamer/
- [MEF-RGK] https://music-electronics-forum.com/forum/effectification/guitar-effects/991273-rg-keen-s-description-of-tube-screamer-functionality
- [RIGTALK-TS] https://www.rig-talk.com/forum/threads/what-is-the-tubescreamer-eq-curve.70320
- [GPX-DIODE] https://www.guitarpedalx.com/news/gpx-blog/a-brief-hobbyist-primer-on-clipping-diodes
- [PPCB-DIODE] https://forum.pedalpcb.com/goto/post?id=193790
- [WAMPLER] https://www.wamplerpedals.com/blog/latest-news/2015/04/70when-to-use-pedals-at-18-volts/
- [PPCB-BJT] https://forum.pedalpcb.com/threads/biasing-bjts-part-3.6138/latest
- [PPCB-RAT] https://forum.pedalpcb.com/posts/395694/
- [PPCB-MUR] https://forum.pedalpcb.com/threads/muroidea-proco-rat.12237/
- [PPCB-LM308] https://forum.pedalpcb.com/posts/222101/
- [DIYSB-4558] https://diystompboxes.com/smfforum/index.php?msg=561558
- [PG-DP] https://www.premierguitar.com/gear/mxr-distortion-plus-mods
- [PG-BD2] https://www.premierguitar.com/gear/boss-bd-2-mods
- [ED-MT2] https://electricdruid.net/boss-mt-2-metal-zone-pedal-analysis/
- [MIROSOL] https://mirosol.kapsi.fi/2014/05/boss-hm-2-heavy-metal/
- [CODA-KLON] https://www.coda-effects.com/p/klon-centaur-circuit-analysis.html
- [CODA-KLON2] https://www.coda-effects.com/2015/09/klon-centaur-clone-and-mods-aion.html
- [CODA-BM] https://www.coda-effects.com/p/big-muff-tonestack-dealing-with-mids.html
- [OCD-DIYSB] https://diystompboxes.com/smfforum/index.php?msg=914090
- [MADBEAN] https://madbeanpedals.com/forum/index.php?msg=87088
- [TB-PPCB] https://forum.pedalpcb.com/threads/tonebender-mk-ii-transistor-clarification.28158/post-364827
- [TB-EDB] https://www.effectsdatabase.com/model/solasound/tonebender/mk2/dam
- [OCTAVIA] https://spicersmusic.com/blogs/history/jimi-hendrix-effects-roger-mayer-octavia
- [SUPERFUZZ-PPCB] https://forum.pedalpcb.com/goto/post?id=288750
- [TIMMY-REVERB] https://reverb.com/p/paul-cochrane-timmy
- [TIMMY-THOMANN] https://www.thomann.de/be/mxr_custom_shop_timmy_csp027.htm
- [KOT-EDB] https://www.effectsdatabase.com/model/vfe/bluesking
- [BB-PPCB] https://forum.pedalpcb.com/threads/blue-breaker-weird-question-comparison.3076/post-24857
- [PD-PPCB] https://forum.pedalpcb.com/threads/dropping-the-hammer-cloning-the-precision-drive.18540/latest
- [PD-EDB] https://www.effectsdatabase.com/model/horizon/precisiondrive
- [SD1-GW] https://www.guitarworld.com/features/boss-sd-1-super-overdrive-pedal
- [IMD-MEF] https://music-electronics-forum.com/forum/amplification/guitar-amps/theory-design/3093-intermodulation-distortion
- [BALTIC] https://baltic-lab.com/2023/08/dsp-diode-clipping-algorithm-for-overdrive-and-distortion-effects/
- [ECIRCUIT] https://www.ecircuitcenter.com/Circuits/distortion_box/op_disto1.htm
- [SPICE-1N4148] https://assets.nexperia.com/documents/spice-model/1N4148.txt
- [WAVESHAPER-SEARCH] https://faustlibraries.grame.fr/libs/aanl/ (search-result summary of waveshaper and antialiasing pages; also https://www.vicanek.de/articles/WaveshaperHarmonix.pdf listed but unread)

Reference pages.
- [WIKI-TS] https://en.wikipedia.org/wiki/Ibanez_Tube_Screamer
- [WIKI-BM] https://en.wikipedia.org/wiki/Electro-Harmonix_Big_Muff
- [WIKI-FF] https://en.wikipedia.org/wiki/Fuzz_Face
- [WIKI-RAT] https://en.wikipedia.org/wiki/Pro_Co_Rat
- [WIKI-DS1] https://en.wikipedia.org/wiki/Boss_DS-1
- [WIKI-DIST] https://en.wikipedia.org/wiki/Distortion_(music)
- [WIKI-OD] https://en.wikipedia.org/wiki/Overdrive_(music)
- [WIKI-CLIP] https://en.wikipedia.org/wiki/Clipping_(audio)
- [WIKI-SHOCK] https://en.wikipedia.org/wiki/Shockley_diode_equation
- [WIKI-SF] https://en.wikipedia.org/wiki/Univox_Super-Fuzz

Papers.
- [YEH07] Yeh, Abel, Smith, "Simplified, physically-informed models of distortion and overdrive guitar effects pedals", DAFx-07 (abstract): https://dafx.de/paper-archive/details/mIq7vlQKu6pUlF467vvhgw
- [YEH08] Yeh, Smith, "Simulating guitar distortion circuits using wave digital and nonlinear state-space formulations", DAFx-08 (abstract): https://dafx.de/paper-archive/details/gZvqz8Bk39w9LqNNr1SDlg
- [YEH-PUBS] Yeh publications page (thesis and CMJ 2008 listing): https://ccrma.stanford.edu/~dtyeh/papers/pubs.html
- [WERNER15] Werner, Nangia, Smith, Abel, "Resolving wave digital filters with multiple/multiport nonlinearities", DAFx-15 (abstract): https://dafx.de/paper-archive/details/4fsq4pZNdCbUzyzAL8xsQg
- [WERNER-SEARCH] Werner et al., AES 2015 diode clipper model (Lambert W), seen as a search hit: https://aes2.org/publications/elibrary-page/?id=17918
- [EICHAS15] Eichas, Möller, Zölzer, "Block-oriented modeling of distortion audio effects using iterative minimization", DAFx-15 (abstract): https://dafx.de/paper-archive/details/bKxNxdS0ua9iEdRrM6zPlg
- [EICHAS16] Eichas, Zölzer, "Black-box modeling of distortion circuits with block-oriented models", DAFx-16 (abstract): https://dafx.de/paper-archive/details/aW-6gR1PsD9EqSfEI9CYcg
- [PAKARINEN09] Pakarinen, Yeh, "A review of digital techniques for modeling vacuum-tube guitar amplifiers", CMJ 33(2), 2009 (PDF read through a lossy summariser): https://www.effectrode.com/wp-content/uploads/2018/08/a_review_of_digital_techniques_for_modeling_guitar_amplifiers.pdf
- [HOLTERS15] Holters, Zölzer, "A generalized method for the derivation of non-linear state-space models from circuit schematics", EUSIPCO 2015 (search hit only): https://new.eurasip.org/Proceedings/Eusipco/Eusipco2015/papers/1570103545.pdf
- [NODE22] "Virtual analog modeling of distortion circuits using neural ordinary differential equations": https://arxiv.org/html/2205.01897
- [DIFFBB25] "Differentiable black-box and gray-box modeling of nonlinear audio effects": https://arxiv.org/html/2502.14405
- [CHOWDHURY20] Chowdhury, "A comparison of virtual analog modelling techniques for desktop and embedded implementations" (Klon): https://arxiv.org/html/2009.02833
- [OVERTON] Overton, "Digital circuit-level emulation of transistor-based guitar distortion effects" (Fuzz Face): https://repository.gatech.edu/entities/publication/47748b51-c826-4f0a-9b4f-6b0b7ac2b980
- [OCD-DAFX] Giampiccolo et al., "Explicit wave digital model of the Fulltone OCD pedal based on canonical piecewise-linear functions", DAFx-26 (abstract): https://dafx.de/paper-archive/details/FR5oopmtXLuPh9fiskDhrg
- [HERBST] Herbst, "Distortion and rock guitar harmony: the influence of distortion level and structural complexity on sensory consonance": https://pure.hud.ac.uk/en/publications/distortion-and-rock-guitar-harmony-the-influence-of-distortion-le/
