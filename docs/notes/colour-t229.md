# Colour comparison, [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") ([T229](../../board/TASKS.md#t229 "Colour comparison"))

Owner: search. Date: 2026-10-25. **Measurement for the ear; nothing is adopted.** Local only: the page holds song audio and stays under `cache/` (gitignored).

## What to open

- Page: `/Users/jason/Documents/code/helix_pedal/cache/colour-t229/compare/index.html` (same style as the [T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…") page: seven songs; per variant: song's guitar left / variant right; last run's best (a) left / variant right; the variant alone; the block chain printed; every clip LUFS-matched to -23). Not blind: labelled.
- Presets: `cache/colour-t229/<song>/hlx/<variant>/best.hlx` (35 files; all pass `preset.validate` with no error and `hlx_validate` exit 0). Load list: `/Users/jason/Documents/code/helix_pedal/cache/colour-t229/load_list.md`. **Nothing was loaded on the Stomp.**

## Variants (all rendered on Native from the [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") calibrated dry; block-serial; Room and Plate are folded to mono as in [T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…"))

| variant | chain |
|---|---|
| a | the fine-tuned drive match (`cache/matches/<song>-finetune-prs7`), last run's best |
| c | a + Graphic 10 Band, +-6 dB, fitted on the long-term-spectrum ratio of stem to render ([T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…") code and rule, bands under 100 Hz ignored) |
| r25 | c + Room reverb, Decay 0.40, Predelay 0.011, LowCut 150, HighCut 3700, Mix 0.25 |
| r35 | the same at Mix 0.35 ([T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…")'s "g" setting, but on top of c this time) |
| p35 | c + Plate reverb, Mix 0.35, same Decay, Predelay, LowCut and HighCut (not level-matched to Room: the two algorithms have different tail levels at the same Mix) |

For mother, seek-destroy and teen-spirit, a and c are the [T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…") renders (same dry, block values and engine; the stem is bit-identical, max difference 0). For the other four songs a and c were rendered here (the a render matches the old `match.wav` exactly up to the listening gain, correlation 1.0000).

## Renders

| song | drive model | a + c renders | reverb renders | total |
|---|---|---|---|---|
| mother | DistZeroAmpBassDI | reused [T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…") | 3 | 3 |
| seek-destroy | DM4HeavyDistortion | reused [T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…") | 3 | 3 |
| teen-spirit | DM4HeavyDistortion | reused [T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…") | 3 | 3 |
| territorial | DistVerminDist | 1 + 2 | 3 | 6 |
| satisfaction | DistObsidian7000 | 1 + 2 | 3 | 6 |
| cherub-rock | DM4HeavyDistortion | 1 + 2 | 3 | 6 |
| ds-sample | DistValveDriver | 1 + 2 | 3 | 6 |

**Final deliverable: 33 renders** (3 x 3 for the pilot songs, 4 x 6 for the new ones). **Development: 3 more** (the first mother run crashed after rendering, on a validation call that does not exist on the preset object; its renders were repeated). **Total 36**, under the 120 cap. The 10 x 3 s excerpts are the [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") windows (ds-sample 15 s).

## Fitted EQ curves (gains actually set, dB; 31 and 62 Hz stay at 0)

| song | 125 | 250 | 500 | 1k | 2k | 4k | 8k | 16k | LTAS residual before to after (dB RMS) |
|---|---|---|---|---|---|---|---|---|---|
| mother | -2.9 | -1.8 | +1.7 | +1.3 | -0.1 | -0.8 | -4.6 | -6.0 | 3.56 to 1.73 |
| seek-destroy | +3.4 | +0.4 | -1.1 | +0.6 | +1.6 | -2.3 | -6.0 | -6.0 | 8.17 to 5.94 |
| teen-spirit | -6.0 | -5.3 | -3.3 | +0.2 | +3.2 | -2.2 | -6.0 | -6.0 | 11.39 to 10.05 |
| territorial | -2.1 | -3.2 | -2.7 | -0.7 | +0.7 | +1.9 | +3.7 | +5.5 | 3.03 to 0.53 |
| satisfaction | -6.0 | -0.9 | +3.1 | +0.2 | +0.3 | +2.1 | -5.6 | -6.0 | 5.78 to 3.11 |
| cherub-rock | -4.4 | -3.5 | -2.3 | +1.4 | +5.2 | -2.8 | -6.0 | -6.0 | 10.07 to 7.47 |
| ds-sample | -3.0 | -2.2 | -2.9 | -3.1 | +2.6 | +3.8 | -5.3 | -6.0 | 4.66 to 3.14 |

The +-6 dB limit is reached at 16 kHz on six of seven songs (and at 8 kHz on three): the drive block keeps treble the separated stem lacks, and a graphic band cannot remove more than 6 dB ([T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…") section 4; the parametric high-cut was variant h there). Territorial is the only song where the fit asks for treble boost (+3.7 at 8k, +5.5 at 16k) and reaches a residual of 0.53 dB. The residual is the spectrum difference that was fitted, not a sound-comparison score.

## Not done

No listening, no scoring. No Stomp load. Ledger: `search / native / colour-t229-<song>-<a|c|r25|r35|p35>` (35 rows; reused renders are counted 0).

Code: `scripts/colour_t229.py` (`run`, `compare`, `loadlist`, `ledger`, `table`); it reuses `scripts/colour_pilot_t228.py` and `src/tonematch/search/colour.py` unchanged. No library code was added, so no new tests.
