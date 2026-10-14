# T002 / T013 Native spike: can a whole preset be written into Helix Native's state?

Owner: native-engine. Status 2026-10-14: **whole-preset state write: NO. Automation/replay route: pending one GUI step.**
Plugin: Helix Native **3.82** (original, not Stadium), both builds installed on the dev Mac:

- VST3 `/Library/Audio/Plug-Ins/VST3/Line 6/Helix Native.vst3` (`com.line6.helixnative.vst`, 3.82). The installer puts it in a `Line 6/` subfolder.
- AU `/Library/Audio/Plug-Ins/Components/Helix Native.component` (`com.line6.helixnative.au`, AU name "Line 6: Helix Native", 3.82, universal arm64/x86_64).

Evidence files (local, gitignored, Line 6 data, never commit): `cache/native/spike/vst3_headless/report.json`,
`cache/native/spike/au_headless/report.json`, `params.json`, `state_default.bin`, `cache/native/cost.json`.

## Verdict so far

| Question | Answer | Evidence |
|---|---|---|
| Does pedalboard host Native? | **Yes, both builds** | pedalboard 0.9.26, in the host child process only. VST3: 167 params, stereo I/O (mono in is duplicated, out folded), open 5.4 s. AU: 37 params, mono, open 1.8 s. Both report 3.82 and refuse nothing. |
| Is it Stadium? | **No** | `Info.plist`: CFBundleName "Helix Native", ids `com.line6.helixnative.{vst,au}`, version 3.82. `is_stadium()` now checks the plist as well as the path. |
| Where does preset data live in the state? | In an **opaque record** inside a chunk container | See "State layout" below. |
| Preset JSON/XML inside the state? | **No** | No JSON, XML, zlib, gzip or base64 layer at any depth; no Helix markers (`"tone"`, `"dsp0"`, `@model`, `HD2_`). The preset record bodies run at 7.996 bits/byte. |
| Can we write a preset into the state? | **No** | Splicing a record from Native's own library (same 46,616-byte format) into DPCP, or into DPCP and BDCP, is accepted silently, then the plugin falls back to its default: readback differs and the render is unchanged (null vs default -322.7 dB). Tried 2 Stomp-library and 1 Helix-library record, on VST3 and AU. |
| Can captured states be replayed? | **Pending GUI** | This is the automation route's core assumption. `--gui` tests it in a fresh process. |
| Do the 16 knobs and 16 switches work from the host? | **Exposed; effect pending GUI** | `knob_01..16` and `switch_01..16` (range 0..1), `snapshot_index` (1..8), and `master_input_level[_db]`, `master_output_level[_db]`, `master_bypass`. On the empty default preset none moves the render (expected, since nothing is assigned). |
| MIDI CC? | **VST3 exposes `cc_0`..`cc_129` as host params** | Presumably Native's VST3 MIDI-CC mapping. Whether setting `cc_110` acts like CC 110 is tested in the GUI step: `known.hlx` assigns CC 110 to the DL4 Mix and Feedback. The AU has no `cc_*` params. |
| MIDI preset/setlist recall? | **AU and AAX only** | Pilot's Guide p. 59 (installed copy): "remote MIDI control of setlist and preset changes is supported only with the AU (Mac) and Pro Tools AAX ... not supported for the VST2 or VST3". PC 0-125 selects presets in HX Stomp HC mode. CC 69 selects snapshots. pedalboard cannot send MIDI to an effect, so this route needs DawDreamer. |

## State layout (black-box, structure only)

`raw_state` wrappers: VST3 is JUCE `VC2!` + XML, with one `<IComponent>` (93,432 bytes) and no IEditController part. AU is an AU ClassInfo bplist with keys `manufacturer, type, subtype, version, name, data` (308 bytes) and `Line6PresetData` (93,432 bytes). Both carry the same container:

```
"CMXH" uint32 size
then chunks: tag[4] uint32 version uint32 size uint32 reserved payload[size]
  RHCP v1   4 B
  ILCP v2  16 B
  VGCP v4  16 B
  VTCP v1  32 B
  DPCP v2 46616 B   record: uint32 46612, uint32 5, 46608 B body (= 2913 x 16, entropy 7.996)
  BDCP v2 46616 B   same format, different bytes from DPCP even in the default state
  IUCP v2   9 B     editor window 1034 x 656 (reserved word nonzero; not CRC32/Adler32/CRC16 of any obvious span)
  + 3 trailing bytes
```

Native's preset library (`~/Library/Application Support/Line 6/Helix Native/#PRESET_LIBRARY*/#SETLISTn/#PRESETnnn`) stores each preset as exactly this 46,616-byte record. The body length is a multiple of 16 and the entropy is at the maximum, which is consistent with a block cipher. **Analysis stopped there.** Going further would mean attacking the cipher, which is outside the project's interop-only rule (CLAUDE.md, DECISIONS 2026-10-13) and plausibly a DMCA 1201 issue. Host parameter changes (for example `master_output_level_db`) do not alter any chunk, and two reads of an unchanged state give identical chunks.

Open formats seen in the same install (read in place, not copied): `res/default_preset*.hlx` (plain `.hlx` JSON), and `res/Factory_*.hls` setlists: a JSON wrapper `{version, meta, encoding: Base64, encoded_data, compression: {type: zlib, crc32, decompressed_size}, schema: L6Setlist}` whose payload is JSON `{meta, presets: [...]}`. Native imports `.hlx` and `.hls` through its GUI.

## Render behaviour (measured, Native 3.82, empty default preset)

- **Default preset** is empty, so the plugin passes through. Gain is 0.99997 (-0.0002 dB). The DI through Native, latency-trimmed, nulls against the input at **-92.0 dB**.
- **Latency: 16 samples**, by impulse, on both builds and every block size (reported latency is 0).
- **Post-reset fade-in**: after `reset=True`, the output ramps from 0 and reaches unity within 0.1% at about **3,180 samples**. An impulse at sample 0 is swallowed completely, which is why the first spike run's latency probe failed. Fix: `NativeEngine.PREROLL = 4800` samples of silence before every render, and the latency impulse is placed after it.
- **Reset dominates the cost**: `process(reset=True)` takes **about 1.59 s fixed**, whatever the clip length (0.1 s to 10 s) or block size (128 to 8192). Without a reset the empty chain runs at about **0.01× real time**. `set_state` takes 16 ms, causes no fade-in, and does *not* clear DSP memory (the 16-sample delay line kept the previous signal). Hence the opt-in `reset_mode="flush"` (no reset, `flush_s` of silence first). It stays opt-in until the GUI step shows that flush renders match reset renders on a chain with an amp and a delay. The test is in `report.json` → `gui.render_modes*`.
- **Determinism**: two reset renders null at -322.7 dB.
- **Cost** (`cost()` measured, cached in `cache/native/cost.json`, VST3, empty preset, reset mode): **1.628 s per 2 s render**. Throughput is 0.61 / 1.19 / 1.85 / 2.26 / 2.24 / 1.98 renders/s for 1 / 2 / 4 / 6 / 8 / 9 processes, so **parallelism = 6** on this 10-core Mac. Chain-change settling is **0.016 s** (state load plus polls, measured from the opening state to the current one; with a single distinct state this is a reload). These are lower bounds: the GUI run re-measures on the `known.hlx` chain.

Ledger: `2026-10-14T16:37:06+00:00 render-cost native smoke seconds_per_render 1.6278` and `smoke native smoke passthrough_null_db -92.0`. Both are labelled empty-preset. Neither is engine-smoke or Gate 1 evidence.

## The one GUI step (human, about 3 minutes)

```bash
cd ~/Documents/code/helix_pedal && TONEMATCH_AGENT=native-engine .venv/bin/python scripts/native_spike.py --gui --golden fixtures/smoke/known.hlx --ledger
```

1. Wait about 30 s for the headless phases. Native's window then opens (it may be behind the terminal). If it asks you to sign in or authorise, do so. This uses one of your 4 authorisations.
2. Click the **Open** menu button at the top of the Signal Flow, choose **Open From Disk**, and pick `fixtures/smoke/known.hlx`. If Native offers to switch to **HX Stomp** HC mode, accept. The signal flow should show Essex A-15, the 1x12 Blue Bell cab and the DL4 Ping Pong (bypassed).
3. Right-click the amp's **Drive** slider and choose **Knob 1** (automation controller).
4. Right-click the delay block (or its bypass button) and assign its bypass to **Switch 1**.
5. Close Native's window. The spike carries on by itself for about 5 minutes and prints the verdict.

If Native refuses `known.hlx`, use `--golden fixtures/golden/HXStomp.hlx` instead and open that file. That file's chain has 8 blocks, so the slot numbers in the default map will not fit and the template is saved without a map.

What the run decides, in `cache/native/spike/report.json`:

- `gui.preset_loaded`: the GUI-opened preset changed the render.
- `gui.replay_ok`: the captured blob, set into a **fresh** host process, renders within -60 dB of the in-session render. **This is the automation route's YES or NO.**
- `automation.effective`: which of `knob_*`, `switch_*`, `snapshot_index` and `cc_110` move the render.
- `template`: the chain template saved under `cache/native/templates/` for `known.hlx`'s chain, so that `NativeEngine.load(read_hlx("fixtures/smoke/known.hlx"))` works and `engine-smoke` can run on Native.
- `gui.render_modes*`: whether `reset_mode="flush"` matches `reset` (with the delay on, via Switch 1).
- `cost`, re-measured on that chain, plus new `render-cost` and `smoke` (`replay_null_db`) ledger rows.

## Routes left, in order

1. **Replay + 16/16 automation** (the default route in `native.py`). One GUI open per chain structure; the captured state is a template. `load()` raises `UnsupportedModelChange` for chains without a template, and `search` groups trials by `chain_key`. Proven or not by the GUI step.
2. **Fewer clicks per chain.** If a Native-exported `.hlx` shows how Knob/Switch assignments are written in the `controller` section (like `@controller 8` / `@cc 110` for MIDI), `preset` could emit Native-ready files with the assignments already in. The human would then only open the file. Optional extra for the GUI step: export the preset to `cache/native/spike/knob_export.hlx` (it stays local).
3. **AU + MIDI PC.** Generate a setlist (`.hls`, an open format, up to 126 Stomp presets). The human imports it once, then a MIDI-capable host (DawDreamer, also GPLv3 and also behind the process boundary) sends PC n, and we capture each state as a template. Not built yet.
4. GUI scripting of Native's import: last resort.

## Install notes (for `platform`, T007)

Native is the user's own licence. Never copy the bundle, its presets, its library, its `res/` files or any state dump into the repo.

- **macOS** (Apple Silicon or Intel): install original **Helix Native 3.8x** from line6.com, not Helix Stadium Native. The engine refuses Stadium by path and by Info.plist. The 3.82 installer puts the VST3 at `/Library/Audio/Plug-Ins/VST3/Line 6/Helix Native.vst3` and the AU at `/Library/Audio/Plug-Ins/Components/Helix Native.component`. Both are found automatically, with the VST3 preferred. Open the plugin window once (the `--gui` step) to sign in and authorise. Then `pip install -e '.[native]'`. Python 3.11+; arm64 wheels exist for pedalboard. Override the path with `TONEMATCH_NATIVE_PLUGIN=/path/to/Helix Native.vst3` (or `.component`).
- **Windows**: the VST3 goes to `C:\Program Files\Common Files\VST3\Helix Native.vst3` (or a `Line 6\` subfolder; both are searched). 64-bit Python 3.11+, `pip install -e .[native]`. Authorise in the plugin window on first run (`--gui`). MIDI preset recall would need the AAX build, which no Python host loads, so Windows uses route 1 or 2.
- **Linux (best effort, Wine + yabridge)**: install Native's Windows build under Wine (wine-staging 9+), then `yabridgectl add "$HOME/.wine/drive_c/Program Files/Common Files/VST3" && yabridgectl sync`. The bridged bundle appears at `~/.vst3/yabridge/Helix Native.vst3`, which is a searched path. The editor needs an X/Wayland display; authorisation under Wine is unverified. Don't block on it.
- **Host interpreter**: to keep GPL code out of the main environment entirely, put pedalboard in a separate venv and set `TONEMATCH_NATIVE_PYTHON=/path/to/that/python`. The engine spawns its host with it.
- **Speed knob**: `TONEMATCH_NATIVE_RESET=flush` selects the fast render mode (see above). Use it only after the spike shows it matches on your chains.

## Observations for the lead

- `scripts/engine_smoke.py` imports `tonematch.preset.load_hlx`, but `preset.py` defines `read_hlx`. As written, engine-smoke fails on every engine with an ImportError (owners: benchmark / preset).
- Design choices in `native.py` that touch contract wording (contract unchanged): a fixed calibration gain (0 dB, Native's digital output is the reference); latency once per chain; every render is a reset render with a 4,800-sample pre-roll; `reset_mode="flush"` is opt-in.
- The 1.6 s fixed reset cost means about 2.3 renders/s across 6 processes on this Mac (empty chain). Search and learning budgets should come from `cost()`, and flush mode (if proven) or batching several clips per reset would change them by more than 10×.
