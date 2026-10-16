# Setlist survey (126-preset HX Stomp export, firmware 3.80)

Script: `scripts/setlist_survey.py` (`coverage`, `stats`). Input `fixtures/golden/HXStompSetlist.hls`
(local, gitignored; Line 6 content). Outputs go to `cache/setlist/` (local). This note keeps
method, structure and aggregate numbers only: no preset bodies, no model lists.

## 1. Coverage

`read_hlx`-style parse (`Preset.from_dict`), `to_hlx_dict`, `validate` (fw 0x3800000, 3.82 catalog)
and `stompd-compile` (offline) over all 126.

| Step | Result |
|---|---|
| Parse and write back (dict and text identical, snapshot sync off) | 126 / 126 |
| Validate (after the fixes below) | 125 / 126 |
| Compile onto `base_twopath.bin`, as is | 1 / 126 |
| Compile with `STOMPD_FORCE_BUILD=1` (offline layout and block-type coverage only) | 90 / 126 |

Validation failures found and fixed in `preset.py`:
- 42 two-path presets: snapshot bypass maps include a `split` (and can include `join`) key; the
  check treated it as an unknown block. Fixed.
- 28 presets: snapshot controller values differ from block values by design (snapshot-controlled
  and expression/footswitch-controlled parameters). Handled by the explicit opt-in below
  (`Preset.keep_snapshot_varying()`, `hlx_validate.py --keep-snapshot-values`). One preset
  remains flagged on purpose: a snapshot-controlled value whose block value is not the current
  snapshot's.

Compiler refusals, by kind (126 presets, base `base_twopath.bin`, which comes from the single
device preset 04C):
- **Controller assignments (125, all bases tried):** the base keeps its own assignments on slot 3, and
  the compiler refuses to change or empty a slot that has them. 125 of 126 files need slot 3
  changed or emptied. Every device document captured so far derives from 04C, so the assignment is
  in all of them. This is a property of the base, not of the preset. Need: a base captured from a
  preset with no assignments (load an assignment-free preset on the pedal and `backup`).
- **Force-build, remaining 36 refusals:** 22 "slot would be emptied but has controller
  assignments" (same base issue, not covered by the force flag), 11 Send/Return blocks
  ("not built"), 3 parameter values 0.00999999 / similar rejected as outside 0.01..0.8 (float
  tolerance in the range check of the compiler; 2 hold-time, 1 decay).
- **Not compiled at all:** `tone.controller` assignments in the file. The compiler only edits
  values of assignments the base already has; the controller sections of the 70 presets that have them are
  not written (the report warns per assignment).
- **Two-path:** 42 presets; 17 compile under force-build, the rest fall into the buckets above.
- **IR table:** 6 presets with `irUuidTable`; 3 compile (the others fall into the buckets above).
  User IR content is not carried by the preset (only the table of UUIDs).

## 2. Two-path encoding: HX Edit versus our dual builder

All 42 HX Edit two-path presets are the Y-split topology (`global.@topology0` = `SABJ`, Split, A,
B, Join). None uses an independent Path B input or output. Our dual builder
(`scripts/stomp_hw.py make_dual` + compiler Path B support) produces the pedal's *independent*
two-path layout. Differences, all of them:

| Item | HX Edit (42 of 42 unless noted) | Our dual |
|---|---|---|
| `global.@topology0` | `SABJ` | `A` (unchanged from the single-path file) |
| `inputB.@input` | 0 | 6 (USB 5/6 dry) |
| `outputB.@output` | 0 | 6 (USB 3/4) |
| `inputA.@input` | 1 | 6 (reamp, by design) |
| `split.@position` / `join.@position` | split 0-5, join 3-8, region varies | 0 and 8 (full width) |
| `split` Balance A/B | 0.5 / 0.5 (33 of 36 that have it) | 0 / 1 (inherited from the golden) |
| `join` A Pan / B Pan | 0.5 / 0.5 (38 of 42) | 0 / 1 |
| `join` A/B Level, Level, polarity | 0 / 0 / 0 (mostly), false | 0 / 0 / 0, false |
| `outputB.Type`, gain, pan | true, 0, 0.5 | same |
| Path B block `@path`, `@position` | `@path` 1, `@position` within [split, join); 32 of 42 also have path-A blocks inside that range | `@path` 1, `@position` 0 (equal to split), A at 1 and 2 |
| Snapshot `blocks.dsp0` | includes every path-B block and `split` | lacks the path-B block and `split` |
| Device document | not captured for SABJ | independent layout: slots 1-8 Path A, `Split` slot 10 ... `Join` slot 19 |

Not compiled from the file (kept from the base document): split Balance, join Pan/Level/Polarity,
`@topology0`. So the split/join values in our dual renders are the base's, not the file's.

For the unexplained -18..-22 dB gap, candidates are therefore: (a) the topology (independent
paths versus the Y-split both paths share), (b) the join Pan 0 / 1 and split Balance 0 / 1 in the
base (HX Edit uses 0.5 / 0.5 when both paths are used), (c) both inputs fed by USB 5/6. None is
tested. A decisive experiment: capture a device backup while the pedal holds one HX Edit SABJ
preset from the setlist, compile onto that base, render the same chain alone and in the SABJ
layout.

## 3. Snapshot-controlled parameters (encoding)

Over the 70 presets with a `controller` section (all 3.80):
- `tone.controller.dsp0.<block>.<param>` = `{"@controller": id, "@min", "@max", "@snapshot_disable"}`.
  Controller ids seen (assignment counts): **9 = snapshot** (88; 77 of them vary between
  snapshots), 1 and 2 = expression pedals (26 and 42; min/max only), 3-6 = footswitch (13; carry
  `@fs_*` keys), 8 = MIDI CC (4; carry `@cc`).
- For id 9, `@min`/`@max` equal the parameter's full catalog range in 88 of 88.
- Each valid snapshot stores `snapshotN.controllers.dsp0.<block>.<param>` =
  `{"@fs_enabled": false, "@value": v}`. Every controlled parameter (any id) has one.
- The block's own value equals the **current snapshot's** value (87 of 88; `global.@current_snapshot`
  is 0 in 122 presets). So "block values win" and "snapshot values win" are the same thing when
  the block holds the current snapshot's value.
- Per-snapshot bypass is separate: `snapshotN.blocks.dsp0.<block>` booleans (and `split`, `join`);
  `@no_snapshot_bypass` on the block (always false here) opts a block out.
- Snapshots may be `@valid: false` and then carry no `blocks`/`controllers`. Valid snapshots per
  preset over all 126: 3 in 48, 2 in 3, 1 in 67, 0 in 8.

`tonematch.preset` write API (default unchanged: block values win and every snapshot value is
synced to the block): `Preset.vary_in_snapshots(key, param, values, lo, hi | catalog)`;
`Preset.keep_snapshot_varying()` to adopt a file's; `snapshot_controlled`; `current_snapshot`.
The compiler cannot create the assignment on the device (it only updates values of assignments
the base already has), so a generated snapshot-varying parameter is for `.hlx` export and HX Edit
import, not for the Stomp engine's live path, until the compiler builds assignments.

## 4. Bypass and footswitch assignments

Bypass assignments are not under `tone.controller`; they live in `tone.footswitch.dsp0.<block>`:
`@fs_index`, `@fs_label`, `@fs_enabled`, `@fs_ledcolor`, `@fs_momentary`, optional `@fs_primary`,
`@fs_customlabel`. 375 entries over 114 presets; 317 of them for blocks with no parameter
controller. `@fs_index` values 1, 2, 3, 5 (counts 126, 139, 62, 31) and 14 (17, with
`@position`, `@wait`, `@overthresh`: an expression-pedal toe-switch style entry, not verified).

MIDI CC bypass: only 2 of 126 presets, both with `@cc` 110 on the footswitch entry of a delay
block that also has `@controller` 8 parameter entries with `@cc` 110. On the device, the same
construction decodes as three `MidiCc` assignments on that block (two `Param`, one `Bypass`) at
cc 110. In every other preset the footswitch is the only bypass assignment and the preset stores
no CC number; MIDI-CC bypass for those blocks comes from the pedal's global CC map (not in the
preset). Snapshot bypass is the `blocks` map above.

## 5. Prior statistics (126 presets)

Method: `setlist_survey.py stats`; `Preset.slots` per preset (flow blocks excluded, each cab after
its amp, ordered by `@position` across both paths); category from the catalog, "other" split by
model file. Full per-category parameter distributions (10th, median, 90th) are in
`cache/setlist/prior_stats.json` (local).

- Blocks per preset (excluding cabs): mean 5.48, median 6, max 8; histogram 0:1, 1:1, 2:1, 3:5,
  4:12, 5:27, 6:64, 7:14, 8:1.
- Share of presets with a block of the category: amp 0.75, drive 0.71, reverb 0.71, cab 0.68,
  delay 0.64, comp 0.54, mod 0.46, volume/pan 0.34, eq 0.22, wah 0.18, pitch/synth 0.17,
  send/return 0.11, filter 0.05, gate 0.04, fixed 0.04.
- Enabled rate: cab 1.00, gate 1.00, send/return 1.00, volume/pan 1.00, amp 0.97, reverb 0.92,
  eq 0.88, comp 0.86, delay 0.64, drive 0.54, mod 0.39, pitch/synth 0.38, wah 0.04.
- Mean blocks per preset: drive 0.87, cab 0.87, amp 0.80, delay 0.76, reverb 0.75, comp 0.56,
  mod 0.51, eq 0.25.
- First block: drive 32, comp 27, wah 22, volume/pan 21, pitch/synth 8, amp 8.
- Most common adjacent pairs: amp>cab 77, delay>reverb 55, drive>amp 41, cab>delay 22,
  drive>drive 21, cab>mod 21, comp>drive 20, mod>delay 19, drive>mod 19.
- Two-path 42 of 126; controller section 70; footswitch section 114; IR table 6.
