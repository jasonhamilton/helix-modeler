# Golden diff: HX Edit (Stomp attached) vs Helix Native

Date 2026-10-14. Files are `fixtures/golden/HXStomp.hlx` and `fixtures/golden/HelixNative.hlx`
(local, gitignored). Only key names and structure are quoted here, no catalog content.

## Common frame

Both: top level `data`, `meta` (`original`, `pbn`, `premium`), `schema` = `L6Preset`, `version` = 6.
`data.device` = 2162694 in both (Native was in HX Stomp compatibility mode). `data.meta` keys:
`name`, `application`, `build_sha`, `modifieddate`, `appversion`. `appversion` is 58851328
(0x3820000, 3.82) in both. `tone.dsp1` is present and empty in both. `tone.dsp0` holds I/O and
routing blocks (`inputA`, `inputB`, `outputA`, `outputB`, `split`, `join`) plus `block0..N` and
`cab0`.

## Differences

| Item | HXStomp.hlx (HX Edit) | HelixNative.hlx (Native) |
|---|---|---|
| `data.device_version` | 55574528 = 0x3500000 (3.50) | 58720256 = 0x3800000 (3.80) |
| `meta.application` | `HX Edit`, `build_sha` set | `Helix Native`, `build_sha` empty |
| `tone` keys | `controller`, `variax`, `footswitch`, `global`, `snapshot0-2`, `dsp0`, `dsp1` | no `controller`, no `variax`; adds `irUuidTable` (128 entries, keys `000`..`127`) |
| `tone.global` | adds `@cursor_path`, `@cursor_position` | lacks both |
| `snapshot0-2` | all three `@valid: true`, each with `@custom_name`, `blocks`, `controllers` | only snapshot0 `@valid: true` with `blocks`; snapshots 1-2 `@valid: false` with only `@name`, `@tempo`, `@pedalstate`, `@ledcolor` |
| `split`/`join` | carry `@no_snapshot_bypass` | do not |
| `footswitch.dsp0.blockN` | `@fs_primary`, `@fs_index`, `@cc`, label, colour | label, enabled, index, colour, momentary (no `@cc`, `@fs_primary`) |
| Content | amp + separate `cab0` (amp has `@cab: "cab0"`, `@bypassvolume`, `@type`; cab has `@mic`), 8 blocks | no amp or cab, 6 effect blocks |

Block keys: effects carry `@enabled`, `@path`, `@position`, `@type`, `@no_snapshot_bypass`;
`@stereo` appears on EQ, comp, gate, mod and some delay blocks but not on drives, reverbs,
or amps. `@trails` on delay and reverb. The cab is not positioned (no `@position`): it follows
its amp via `@cab`.

## What this means

- **device_version.** The HX Edit golden says 3.50, the Native golden says 3.80, while both apps are
  3.82. The 3.50 is presumably the Stomp firmware HX Edit saw; this is not confirmed. HX Edit
  3.82 against a firmware older than 3.82 is the likely cause. `hlx-validate` therefore takes
  `--firmware` and warns on a mismatch rather than hard-coding 3.80. Open question for the
  human (T010): which value does the pedal report on its own System screen?
- **Snapshots.** A snapshot stores per-block on/off in `blocks.dsp0` and per-parameter values in
  `controllers.dsp0.blockN.<param>.@value`; the parameter must also appear in
  `tone.controller.dsp0.blockN.<param>` (with `@controller`, `@min`, `@max`). Native exports
  only the first snapshot. A generated 3-snapshot file should follow the HX Edit shape.
- **Writer shape.** `dumps_hlx` reproduces both files byte for byte: 2-space indent, `"k" : v`,
  floats as 17 significant digits, empty containers as `{` blank line `}`, no trailing newline.
- **Unsure.** Which `@controller` ids mean "snapshot"; the DSP budget unit (catalog `load`
  figures, stereo-aware, sum to 50.7 for the Stomp golden and 53.6 for the Native one; both load, so the limit is at least that on this scale; needs device measurement).
