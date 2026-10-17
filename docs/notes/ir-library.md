# User IR library: what it can do for the project ([T106](../../board/TASKS.md#t106 "IR library analysis"))

Owner: `tone-metrics`, 2026-10-17. Offline only (no Native, no pedal). This note is aggregate and
names no vendor, pack or file. The full report, with names, stays local in
`reports/local/ir-library/README.md`. The library is the human's own files in `data/ir/`
(gitignored, never committed; DECISIONS 2026-10-17).

Code: `src/tonematch/metrics/cab_response.py` (IR and cab responses on the block-probe grid,
features, distances, deconvolution estimators), `src/tonematch/ir_library.py` (name parser),
`scripts/ir_library_analyze.py` (`all | inventory | analyze | summary | helix | cluster | estimate`,
outputs only under `reports/local/ir-library/work/`), `tests/test_cab_response.py` (9 tests, synthetic
signals only).

Distances are RMS dB over 100 Hz - 8 kHz after the best gain, on the 1/6-octave fingerprint grid.
Scale from the library itself: 0.8 dB is the next mic distance step on one cab, 1.5 dB the next
mic position, 2.8 dB another mic on the same cab, 5.5 dB a random other cab.

## Inventory (aggregate)

- 2,435 WAV files (588 MB). 2,415 are IRs; **19 are long recordings** (demos, covers, test
  noise), treated as song audio. The other files are Windows thumbnail caches and macOS folder files.
- 2,153 distinct responses after removing 178 exact or same-audio duplicates and near-identical copies.
- 62 guitar cab folders, **90 % 4x12**; mics led by dynamic (SM57, MD421), ribbon (R121, M160)
  and condenser (U87) types; 38 multi-mic blends; plus bass (48), acoustic (33), misc (192) and
  artist or song "IRs" (367).
- Format: 48 kHz 70 %, 44.1 kHz 27 %, 96 kHz 3 %; mostly 24-bit mono; median 200 ms long, but
  99.99 % of the energy sits in the first 2,048 samples (median).
- File-name grids of the form number + letter were decoded from duplicate pairs: the number is the
  mic position (cap to cone edge), the letter the distance. Acoustically, a higher number is darker
  in 77 % of grid folders.
- **Licence:** no licence or readme text anywhere. 48 files carry an embedded copyright notice
  (whole amp-plus-cab captures, not cab IRs); the rest looks like commercial packs or forum-shared
  files of unknown origin. Treat all of it as the owner's personal-use material.

## Findings

- **Clusters follow the capture session, then the mic, not the cab size.** Agreement with cab
  folder 0.47 and mic 0.22, cab size 0.11 (normalised mutual information, 12 clusters). From the
  response of an unseen cab, the nearest other cab gives the right mic 46 % of the time (guessing
  25 %), but the cab size is no better than always saying 4x12.
- **Helix cabs are linear and identifiable from DI.** On 24 single-cab renders of real DI, the
  measured response has coherence 0.9997, and matching it against the 87 cab fingerprints ranks the
  true model first in 22 of 24.
- **Coverage of real cabs.** From each library IR, the nearest sampled Helix cab setting is 2.6 dB
  away (median); an equally sized random set of other cabs' IRs is 2.2 dB away and closer in 81 % of
  cases. The Helix side was sampled sparsely (default mic and position, 4 distances), so 2.6 dB is
  an upper bound.
- **Deconvolution works only with the right amp and the same notes.** A library IR applied to a
  Helix amp render is recovered with 0.55 dB error (exact IR found 94 %) when the amp-only render is
  the right amp, 1.2 dB (right cab folder 84 %) with other amp knobs, and 8.4 dB (useless) with
  another amp model. With different notes the spectral ratio is 8 dB off raw and 2.7 dB after
  1/3-octave smoothing. Cost: 13 ms per convolution, 26 ms per estimate, 0.7 ms to search the
  whole library.
- **Artist and song IRs are finished-tone EQ shapes, not cab captures**: +4 dB more low bump, +3
  to 4 dB more upper presence and far less sub-bass cut than cab IRs, longer tails, and none
  matches a cab IR.

## Verdicts

| | Verdict |
|---|---|
| (a) Library IR as a cab candidate | Promising, small gain (about 0.4-0.7 dB median), not proven. Do two checks first: the IR block equals plain convolution (one Native render with a script-made IR), and a dense Helix cab sweep to measure the real gap. If the gap holds, search can treat "IR from the user's library" as a cab candidate by convolving offline, behind a flag. |
| (b) Cab estimation by deconvolution | Yes, as a second stage once the amp is right in known-DI mode: a cab seed and plausibility check in about 30 ms instead of renders. Not before the amp is right; with different notes only as a coarse shortlist. |
| (c) Training data | Local experiments and augmentation only. No model trained on it leaves the machine until the human confirms licences. Licence-clean option: synthetic cab-like IRs drawn from the aggregate statistics. |
| (d) Artist and song IRs as a prior | Weak: a tone-shape hint at most, unknown provenance. Test with the song-context leave-one-out test before any use; never in a shipped prior. |
| (e) Practical limits | HX Stomp holds 128 IRs of 2,048 samples (42.7 ms) at 48 kHz; HX Edit converts to 16-bit mono (left channel). Truncation to 2,048 is harmless here (99th percentile 0.42 dB); 1,024 costs up to 0.7 dB of low end for 10 % of files. Trim pre-delay first (46 files peak after 1,024 samples). Our USB layer cannot upload IRs (allowlist forbids it); the human loads them with HX Edit. A preset only references an IR slot, not the audio. |

## Next steps

`native-engine`: IR block = convolution check, and the dense cab sweep when Native is free.
`search`: try `transfer_response` + `nearest` as a cab re-ranker after the amp stage.
Human: licences, and whether the 19 recordings should stay in `data/ir/`.

Sources: [Line 6 KB, Impulse Response (IRs)](https://kb.line6.com/impulse-response-irs),
[Line 6 support, importing IRs](https://line6.com/support/topic/46067-importing-irs/),
[Line 6 KB, IR import memory error](https://kb.line6.com/importing-ir-impulse-response-out-of-memory-condition-error).
