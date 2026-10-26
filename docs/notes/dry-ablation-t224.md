# Dry-signal ablation, [T224](../../board/TASKS.md#t224 "Dry-signal ablation (decision 4) + level-only ablation") ([T224](../../board/TASKS.md#t224 "Dry-signal ablation"))

Owner: dry-signal. Date: 2026-10-25. **Measurement only; nothing is adopted.** Human approval: the 2026-10-25 "yes go" bundle, decisions 4 (four dry arms, blind pack) and 5 (level convention and the level-only arm), recorded in [DECISIONS](../DECISIONS.md) as provisional. Local only: the packs hold song audio and stay under `cache/` (gitignored).

**Please rate the packs before reading section 2 onward: the picks below can colour what you hear. Do not open any `key_SEALED.json` until you have filled in the answer sheet.**

## 0. Read this first

- The AFx-Rep distances below come from **different dry tracks**. They are NOT comparable across arms as fidelity (decision 2 of the audit, and the [T221](../../board/TASKS.md#t221 "DI-invariance ratio check") note). Only the picks (model, knobs) and your ear mean anything. A lower number in one arm than another says nothing about which match is better.
- The knobs are the coarse-grid ones (`knob_path coarse_grid`, low confidence). The grid is coarse enough that many arms land on identical knobs; the AFx-Rep model order inside the shortlist of 8 is what moves.
- Several picks sit on a razor-thin margin (section 2), so a different pick between arms can be noise, not signal.

## 1. What was run

For each of the seven songs (ids and windows as T207/T211; window = song seconds), the same search: `scripts/match_block.py --category drive --shortlist afxrep --shortlist-n 8 --final-rank afxrep --knob-tol 0.15 --pack-lufs`, no fine-tune, 3 Native workers, `nice -n 10`, `LoadGuard` and `proc_check` before and after each run (driver `scripts/dry_ablation_t224.py`; commands in its docstring). Dry arms:

| arm | what the search renders through | level |
|---|---|---|
| `lib` (a) | the Guitar-TECHS library DI the [T200](../../board/TASKS.md#t200 "Blind single-drive matches of five songs the human added to data/songs") picker chose for the window (the five [T200](../../board/TASKS.md#t200 "Blind single-drive matches of five songs the human added to data/songs") songs reuse their saved `di_choice.wav`; Mother and DS-Sample were picked the same way, first of `pick_di`'s 12 with sound in >= 0.8 of its half-second blocks) | -28.08 dBFS loud-frame (DS-Sample: -31.87, see section 5) |
| `plain` (b) | the plain Basic Pitch rebuild from the `prs-7-tone` profile (`rebuild_dry.py --profile prs-7-tone`, Basic Pitch velocity), regenerated and found sample-identical to the [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") dry before scaling | -28.08 |
| `env` (c) | the same rebuild with `--envelope-from-stem` (new, off by default; TOOLS.md): same notes, pitch and timing; dynamics from the stem's note-level RMS per attack moment, smoothed (+-0.25 s), chord-corrected (-10 log10 n), limited to +-12 dB | -28.08 |
| `own` (d) | the human's own aligned take: Mother take1 (`20261018-113158`, covers the window), DS-Sample take2 (`20261020-122700`) | true level (Mother -27.82, DS-Sample -27.02 loud-frame) |
| `plain36` | `plain` at the calibration level | -36.12 |

Reproducibility check: `plain36` is the [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") dry, and its AFx-Rep distances equal the [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") shortlist values to the printed precision for all seven songs (Mother 0.508, Teen Spirit 0.573, Territorial 0.380, Seek & Destroy 0.335, Satisfaction 0.525, Cherub Rock 0.513, DS-Sample 0.540), so the search is deterministic and this run is comparable to [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar").

Cost: 30 searches, 10,320 renders, 72.5 min of search wall time (3 hosts). Ledger: `search / native / dry-ablation-<song>-<arm>`, metric `afxrep_distance`, 30 rows, 2026-10-25T20:08. Outputs: `cache/dry-ablation-t224/` (`dry/` the dry inputs and their levels, `runs/<song>/<arm>/` the full `match_block` outputs including `best.hlx`, `logs/`, `packs/`).

## 2. Picks per song and arm

Distance is the winner's AFx-Rep distance to the separated guitar in that arm (not comparable across arms). Margin is runner-up minus winner in the same run (small = fragile pick).

| song (window) | arm | pick | knobs (coarse grid) | distance | margin |
|---|---|---|---|---|---|
| mother (28.3-38.3) | lib | DM4 Heavy Distortion | Drive .85, Bass .73, Mid .31, Treble .70, Output .73 | 0.655 | +0.105 |
| | plain | Zero Amp Bass DI | Drive .85, Bass .59, Treble .70, Blend .40, Presence .50 | 0.480 | +0.160 |
| | env | Zero Amp Bass DI | same as plain | 0.468 | +0.168 |
| | own | Zero Amp Bass DI | same as plain | 0.396 | +0.208 |
| | plain36 | Zero Amp Bass DI | same as plain | 0.508 | +0.147 |
| teen-spirit (167-177) | lib | DM4 Heavy Distortion | Drive .85, Bass .73, Mid .31, Treble .70, Output .73 | 0.587 | +0.062 |
| | plain | DM4 Heavy Distortion | same as lib | 0.562 | +0.063 |
| | env | DM4 Heavy Distortion | same as lib | 0.571 | +0.089 |
| | plain36 | DM4 Heavy Distortion | same as lib | 0.573 | +0.094 |
| territorial (9.5-19.5) | lib | Wringer Fuzz | Gain .20, Bass .50, Treble .70 | 0.290 | +0.029 |
| | plain | Wringer Fuzz | same as lib | 0.403 | +0.013 |
| | env | Wringer Fuzz | Gain **.50**, Bass .50, Treble .70 | 0.403 | +0.012 |
| | plain36 | **Vermin Dist** | Gain .50, Filter .30 | 0.380 | +0.023 |
| seek-destroy (0.5-10.5) | lib | DM4 Heavy Distortion | Drive **.50**, Bass .73, Mid .31, Treble .70, Output .73 | 0.434 | +0.135 |
| | plain | DM4 Heavy Distortion | Drive **.20**, rest as lib | 0.330 | +0.071 |
| | env | DM4 Heavy Distortion | Drive .20 | 0.336 | +0.084 |
| | plain36 | DM4 Heavy Distortion | Drive .50 | 0.335 | +0.090 |
| satisfaction (0.5-10.5) | lib | Obsidian 7000 | Drive .50 (Blend 1.0, Master .55, Treble 8, Bass 5.5, LoMid -5, HiMid -4.6) | 0.578 | +0.031 |
| | plain | Obsidian 7000 | same as lib | 0.540 | **+0.000** (tie with Zero Amp Bass DI) |
| | env | Obsidian 7000 | Drive **.85** | 0.521 | +0.024 |
| | plain36 | Obsidian 7000 | Drive .50 | 0.525 | +0.032 |
| cherub-rock (189-199) | lib | DM4 Heavy Distortion | Drive **.85**, Bass .73, Mid .31, Treble .70, Output .73 | 0.567 | +0.099 |
| | plain | DM4 Heavy Distortion | Drive **.20**, rest as lib | 0.508 | +0.070 |
| | env | DM4 Heavy Distortion | Drive **.50** | 0.504 | +0.094 |
| | plain36 | DM4 Heavy Distortion | Drive .20 | 0.513 | +0.080 |
| ds-sample (30-45) | lib | DM4 Line6 Drive | Drive .50, Bass .89, Mid .47, Treble .70, Output .50 | 0.678 | +0.023 |
| | plain | **DM4 Sub Oct Fuzz** | Drive .85, Bass 1.0, Sub .28, Treble .70, Output .50 | 0.547 | +0.009 |
| | env | **DM4 Buzz Saw** | Drive .50, Bass .50, Mid .50, Treble .70, Output .52 | 0.569 | +0.014 |
| | own (window 30.05-45.0) | **DM4 Classic Distortion** | Drive .20, Bass .48, Filter .30, Treble .50, Output .36 | 0.495 | +0.060 |
| | plain36 | **Valve Driver** | Gain .20, Bass .55, Treble .70 | 0.540 | +0.028 |

### What the picks show (counts only; no claim about which is closer)

- Model agreement with `plain` (the T211-style dry at the play-along level): `env` 6 of 7 (DS-Sample differs); `lib` 5 of 7 (Mother and DS-Sample differ); `plain36` 5 of 7 (Territorial and DS-Sample differ); `own` 1 of 2 (Mother same, DS-Sample differs).
- Knob agreement given the same model: `env` moves one knob in 3 songs (Territorial Gain, Satisfaction Drive, Cherub Rock Drive); `lib` moves Drive in Seek & Destroy and Cherub Rock; the level-only arm moves Drive in Seek & Destroy (.20 to .50). Drive tracks level and dry, as [T194](../../board/TASKS.md#t194 "Explain the Drive anomaly") found; the other knobs sit on the same grid values.
- Only DS-Sample changes its model in every arm (five arms, five different models); its `plain` margin is 0.009, `env` 0.014. That song's pick is not stable under any of these changes, which fits its earlier history (Sub Oct Fuzz in [T207](../../board/TASKS.md#t207 "All seven songs (human 2026-10-22 \"render the dry tracks for all of…"), Valve Driver in [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar")).
- Satisfaction `plain` is a dead tie with the runner-up (margin 0.000). Territorial `plain`/`env` margins are 0.012 to 0.013.
- The level-only arm changes the model for 2 of 7 songs (Territorial, DS-Sample), both of which have margins under 0.03 in at least one of the two levels.
- Mother and DS-Sample `own` take: distances 0.396 and 0.495 are the lowest in their songs, but that is not a fidelity statement (different dry, different performance, take level -27.8 and -27.0).

## 3. Blind listening packs (one per song)

`/Users/jason/Documents/code/helix_pedal/cache/dry-ablation-t224/packs/<song>/`, for `mother`, `teen-spirit`, `territorial`, `seek-destroy`, `satisfaction`, `cherub-rock`, `ds-sample`. Each folder holds:

- `index.html` (local players), `clip_<letter>.wav`, `answer_sheet.md`, `key_SEALED.json` (**do not open before rating**). `packs/README.md` repeats this.
- Each clip is stereo, **left = the separated song guitar (same in every clip), right = the arm's match** (that arm's `best.hlx` rendered through that arm's own dry), all K-weighted LUFS-matched to -23 LUFS integrated (checked), one common gain drop if any peak passed -1 dBFS.
- Letters are random per song (seed 224, sealed in the key). Five clips for Mother and DS-Sample (they have an `own` arm), four for the others.
- Answer sheet: closeness 1-5 per clip (right channel against the left), then a ranking, best first, plus a notes column; say so if two clips sound identical.

Cautions for the listener (not hidden in the key because they follow from the design): the library-DI arm plays other notes than the song (it is a different performance), so it can be told apart by its notes, which breaks blindness for that arm; the `plain` and `plain36` clips, and on some songs `env`, can use the same model and knobs and then differ only by the dry (level, dynamics), so near-identical clips are expected there. The pack matches the human's request ("left = the separated song guitar, right = the arm's match"); rendering every arm's preset through one common dry, which isolates the preset from the dry, would be a different and cleaner pack and has not been built (a follow-up if wanted, about 30 renders).

## 4. Things I changed

- `rebuild_dry.py --envelope-from-stem` (off by default), `dry_rebuild.stem_envelope_db`, a `rel_db` argument on `dry_rebuild.render` and `guitar_profile.render_profiled`; tests in `tests/test_dry_rebuild.py` (4) and `tests/test_chord_rebuild.py` (1); documented in `docs/TOOLS.md` under `rebuild_dry.py`. The default path is unchanged (`rel_db=None`; the plain rebuild reproduces the [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") dry exactly).
- `scripts/dry_ablation_t224.py`, the driver for prep, run, ledger, pack and table.
- Envelope constants (0.30 s window, +-0.25 s smoothing, +-12 dB limit, 50 ms moment grouping) are my first choice, not tuned, not compared with alternatives.

## 5. Caveats and deviations

- **DS-Sample library arm** could not reach -28.08 dBFS loud-frame under the -1 dBFS peak cap (the chosen file is `PinchHarmonics`, a high-crest clip); it ran at -31.87. Every other `lib`, `plain`, `env` arm is at -28.08 (loud-frame), `plain36` at -36.12.
- **DS-Sample own take** covers song 30.03-45.03 s, so that one arm used the window 30.05-45.0 (50 ms shorter at the start, 0 at the end) and its pack target is that window.
- Mother's library DI was one sample short of the window on the first try, which made the first Mother `lib` run fail in one second with no result; the DI was padded by one sample and the run repeated. No other run failed.
- Mother and DS-Sample rebuild dries are 68 s long (as in [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar")); the other five are full length. All windows are inside.
- The `lib` arm is the first-pick-with-sound DI of the [T200](../../board/TASKS.md#t200 "Blind single-drive matches of five songs the human added to data/songs") rule: one DI file per song, not several. The DI-mismatch bias of a single clip is therefore part of what `lib` shows.
- `--pack-lufs` inside each `match_block` run also wrote its own pairwise A/B file (`runs/<song>/<arm>/listen/ab_L-target_R-match.wav`); the blind packs use their own common-gain levelling and do not use those files.
- No hardware used; no Stomp renders; proc_check was clean before and after every run and at the end.
- Nothing here changes a default, admits a score, or adopts a method.

## 6. For the human

Rate the seven packs, then open the keys. If you want a decision, the questions this raises are yours to rule on: whether `--envelope-from-stem` is worth keeping as an option, whether the play-along level (-28.08) or the calibration level (-36.12) should stay the rebuild default (decision 5 provisional), and whether a common-dry pack should be built.
