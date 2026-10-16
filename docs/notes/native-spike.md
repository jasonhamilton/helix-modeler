# [T002](../../board/TASKS.md#t002 "Spike: can pedalboard write a whole preset into Helix Native's saved…") / [T013](../../board/TASKS.md#t013 "Run scripts/native_spike.py --gui --golden ..") Native spike: can a whole preset be written into Helix Native's state?

Owner: native-engine. Status 2026-10-14: **whole-preset state write: NO. Automation/replay route: YES ([T013](../../board/TASKS.md#t013 "Run scripts/native_spike.py --gui --golden ..") GUI run, replay null -72.9 dB). Render-time gap explained and fixed ([T026](../../board/TASKS.md#t026 "Explain and re-measure the Native render-time gap"), below). Hands-off loading of generated presets: YES on macOS VST3 by in-process file drop, with model changes and MIDI-CC assignments written into the file ([T018n](../../board/TASKS.md#t018n "Hands-off Native loading"), below).**
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
| Can captured states be replayed? | **Yes** | [T013](../../board/TASKS.md#t013 "Run scripts/native_spike.py --gui --golden .."): fresh-process replay null -72.9 dB. |
| Do the 16 knobs and 16 switches work from the host? | **Exposed; effect pending GUI** | `knob_01..16` and `switch_01..16` (range 0..1), `snapshot_index` (1..8), and `master_input_level[_db]`, `master_output_level[_db]`, `master_bypass`. On the empty default preset none moves the render (expected, since nothing is assigned). |
| MIDI CC? | **Yes, and beyond 16 knobs** | VST3 host params `cc_<n>` act as CC n on whatever the *loaded preset file* assigns with `@controller 8, @cc n, @min, @max`. [T013](../../board/TASKS.md#t013 "Run scripts/native_spike.py --gui --golden .."): `cc_110` moved known.hlx's DL4 Mix/Feedback (-11 dB). [T018n](../../board/TASKS.md#t018n "Hands-off Native loading"): 13 of 17 file-assigned CCs moved a dropped chain; unassigned ones did not. 90 CCs are free in HC modes, 125 in Off (Pilot's Guide p. 59). The AU has no `cc_*` params (MIDI only). |
| MIDI preset/setlist recall? | **AU and AAX only** | Pilot's Guide p. 59 (installed copy): "remote MIDI control of setlist and preset changes is supported only with the AU (Mac) and Pro Tools AAX ... not supported for the VST2 or VST3". PC 0-125 selects presets in HX Stomp HC mode. CC 69 selects snapshots. [T018n](../../board/TASKS.md#t018n "Hands-off Native loading"): pedalboard 0.9.26 refuses MIDI for effects; DawDreamer 0.9.0 delivers PC to the AU headlessly, but PC only selects presets already in Native's (encrypted) library, which only the GUI import fills. |

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

## Render-time gap ([T026](../../board/TASKS.md#t026 "Explain and re-measure the Native render-time gap"), 2026-10-14, known.hlx chain)

Question: `cost()` reports 1.63 s per render (empty preset) and 1.88 s (known.hlx), but search's smoke demo (`scripts/search_smoke.py`) took about 10 s per render. Measured with `scripts/native_render_probe.py` (synthetic `test_signal` only, raw JSON in `cache/native/probe/`). Each section was run twice: run 1 at 13:47 PDT on a quiet machine, run 2 at 13:59 PDT with two unidentified, non-native, CPU-bound Python processes running. The Mac has 10 cores (8 performance and 2 efficiency).

**Cause: a cost-cache miss, not rendering.** `measure_cost()` keyed its disk cache on a hash of Native's raw state. A knob move changes the raw state (`knob_01`: `e0622cc67dd3` to `a2a2ca82d838`). The demo loads the target with Drive=0.75, and `plan_budget()` then calls `cost()`, so every new knob value missed the cache and re-ran the whole measurement inside the demo: a warm-up, 3 renders, a parallel scan opening 9 host processes one after another at about 5 s each, and the settling test. I reproduced it on a scratch cache copy: **202 s wall, of which 156 s was `measure_cost`**. That is 23 render calls at 8.8 s each, while the renders themselves had a median of 1.92 s (load 3.35, no foreign jobs). Running at the same time as separation and tone-metrics (18:19 to 18:27 UTC) pushed the original run to about 10 s per render.

**Fix (in `native.py`):** the cache key is now `chain:<chain_id>` once a chain is loaded. Before any load, `cost()` measures on a captured template (a real chain) and restores the state afterwards. The parallel scan opens hosts concurrently and stops at the first k that gains less than 15%. `capabilities().extras` gains the contract's `control_route`. After the fix the demo took **46 s, 2.4 s per render call** (including a 7.4 s engine open), with `cost()` at 0.0 s and the same answer (Drive 0.765). A cold `cost()` on a new chain still takes about 130 s, but only once per chain per machine.

| Measurement (2 s clip unless stated) | Run 1, quiet (load1 1.3) | Run 2, loaded (load1 3.9-4.5, 2 foreign jobs at 100%) |
|---|---|---|
| (a) pure render, no `set_params` (median of 5) | 1.869 s | 1.958 s |
| (b) `set_params(knob_01)` then render | 1.863 s; `set_params` 0.5 ms | 1.962 s; 0.5 ms |
| (c) reset render, 0.5 / 2 / 10 s | 1.820 / 1.915 / 2.248 s | 1.877 / 1.926 / 2.249 s |
| (c) flush render, 0.5 / 2 / 10 s (2 s flush pre-roll) | 0.113 / 0.180 / 0.544 s | 0.113 / 0.182 / 0.546 s |
| (d) pool, renders/s at 1 / 2 / 4 / 6 processes | 0.53 / 1.03 / 1.68 / 2.13 | 0.53 / 0.91 / 1.70 / 2.03 (load1 4.95 before; no foreign jobs during d) |
| (d) seconds per render per process | 1.89 / 1.95 / 2.38 / 2.81 | 1.87 / 2.21 / 2.35 / 2.95 |

- **`set_params` re-sends no state and adds no reset.** It costs 0.5 ms. Two renders after a knob move null at -320 dB, and the knob moves the render (-3.2 dB residual for Drive 0.9 against 0.3).
- **The reset cost is fixed.** On this chain a reset render costs about 1.80 s, plus about 0.045 s per second of audio (amp, cab and delay DSP). A 10 s clip costs only 1.2x a 2 s clip, so longer sections are nearly free on Native. Flush mode shows the same 0.045x DSP rate with no fixed cost, but it is still opt-in.
- **Pool:** per-process render time grows from 1.9 s to 2.8 to 2.95 s at 6 processes. The knee is at 6 on a quiet machine. The fixed cost() re-measure (21:02:08 UTC row, load1 4.9 to 6.5) picked 8 (k=6 1.98, k=8 2.35, k=9 2.37 renders/s), so the knee is noisy under load.
- **For `search`:** `Plan.est_wall_s` divides by `parallelism`, but `match()` renders serially through one engine. The demo estimated 3.9 s and took 46 s. See DECISIONS 2026-10-14 ([T026](../../board/TASKS.md#t026 "Explain and re-measure the Native render-time gap")).

Ledger: 11 `render-cost` rows, [2026-10-14T21:02:07](../../ledger/runs.csv?plain=1#L41 "render-cost · native · smoke · seconds_per_render = 1.8685 (first of 8 rows at this time)") (8 rows) to 08, each noting its load. The quiet 2 s figure (1.8685) is in line with the 16:57 row (1.883), so no regression.

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

## [T018n](../../board/TASKS.md#t018n "Hands-off Native loading"): hands-off loading of generated presets (2026-10-14, human approved)

The human's requirement: the program controls every parameter, with no human at the GUI. **Result: met on macOS with the VST3, by route C' below. A new chain (model changes included) is captured in about 66 s once per machine, then searched without a human through MIDI-CC host parameters written into the file.** Raw probe output lives in the session scratchpad. Generated files and states are under `cache/native/drop/`, `cache/native/spike/` and `cache/native/templates/` (local only). The Line 6 files were read in place, nothing was copied, and no preset record in the library changed (checked by mtime).

### (B) Setlist route: format yes, headless import no

- **Format: fully open, and we can write it.** `.hls` is JSON `{version: 2, meta, encoding: "Base64", encoded_data, compression: {type: "zlib", crc32, decompressed_size}, schema: "L6Setlist"}`. `crc32` is CRC-32 of the **decompressed** JSON, which is `{meta: {name}, presets: [...]}`; each entry is exactly an `.hlx` file's `data` object. Read in place from the user's HX Edit 3.82 export (`~/Downloads/HX Stomp Setlist.hls`, 126 presets) and Native's own `res/Factory_*.hls`, `factory1/2.hls`, `templates.hls` (126-128 presets each). `tonematch.engines.native_setlist.encode_hls/decode_hls` re-encodes all 7 and decodes back equal; `tests/test_native_setlist.py`.
- **Library placement: no.** Native's library (`~/Library/Application Support/Line 6/Helix Native/#PRESET_LIBRARY[_STOMP|_STOMP_XL|_EFFECTS|_OFF]/#SETLISTn/#PRESETnnn`) stores every preset as the same opaque 46,616-byte record as the state (2,430 files, 123 distinct; the Helix library is "blank" except slot 0, and the Stomp library holds the 126 factory presets). Only Native's GUI import writes these records. Writing them ourselves would mean producing the encrypted record (out of scope, interop-only rule). The `.install_*_factory` 1-byte markers and root-owned `res/Factory_*.hls` hint at a factory re-install path; I did not test it, because it would mean altering the user's install and wiping a library.
- **MIDI PC on the AU: works headlessly, but only selects what is already in the library.** pedalboard 0.9.26 refuses MIDI for effect plugins ("expects audio as input"). DawDreamer 0.9.0 (GPLv3, tested in a scratch venv only) delivers PC and CC to the AU: the ILCP chunk's preset field follows the PC (`05`, `11` for PC 5, PC 17), and a repeated PC gives an identical state (render null -327.6 dB). With the state's HC-mode word at HX Stomp, PC 0/5/17/40 load distinct factory presets (RMS -17.5/-22.2/-37.5/-12.3 dB). A post-PC state replayed in a fresh VST3 process matches the DawDreamer render (PC 5: -93.7 dB; PC 0/40 -47.6/-32.2 dB because DawDreamer does not reset between programs; PC 17 has a time-variant block). In Helix mode, PC 1-127 all load the same blank preset, because that library is empty. **Verdict:** the setlist route is hands-off only *after* a human does one GUI Setlist Import per 126 presets (HX Stomp mode), or per 128 (8 setlists via CC 32 in Helix/Off mode). Route C' needs no import, so B is now a fallback.

### (A) Knob/Switch assignments in an exported .hlx: not needed any more

- `fixtures/golden/HelixNative.hlx` has no `controller` section. None of the 766 presets in Native's six bundled setlists (nor the 126 in the user's HX Edit export) contains a Native automation (Knob/Switch) controller id; they only carry device controllers (1/2 EXP, 3-7 footswitches, 8 MIDI CC, 9/12/19 snapshot). So the Knob/Switch encoding is unknown. Getting it would take one human export (below), **but it is no longer needed**: MIDI-CC assignments (`@controller 8`), which the format already documents, do the same job, number 90 instead of 16, and the VST3 drives them as `cc_<n>`.
- If the lead wants it anyway (for example, for Windows parity of knob maps), the one human step is: open Native (`python scripts/native_spike.py --gui ...` or any DAW), load `fixtures/smoke/known.hlx`, right-click Drive and choose Knob 1, right-click the delay bypass and choose Switch 1, then Export the preset to `cache/native/spike/knob_export.hlx`. That is one file, and it stays local.

### (C) macOS accessibility scripting: possible only blind, needs a permission, fragile

- Native's whole editor is **one custom-drawn `XGView`** (Line 6's UI framework) inside JUCE's view. Its accessibility role is `AXUnknown` with **0 accessibility children**, so System Events sees no Open button and no menu. AppleScript could only click coordinates. That needs **Accessibility** for the controlling app (Terminal or the agent's process) and **Automation → System Events**. Neither is granted here (`AXIsProcessTrusted() = false`, checked without a prompt), and I enabled nothing. It would break on any window move, resize, scaling or UI change. Not recommended.

### (C') In-process file drop: YES, hands-off, no permission

- The same `XGView` registers `public.file-url` as a drag type. The Pilot's Guide (3.8x, "Loading vs. Importing") documents dropping an `.hlx` onto the Signal Flow as a way to **load** it. The editor runs inside our own host process, so the host calls the view's public `NSDraggingDestination` methods (`draggingEntered/Updated`, `prepare/performDragOperation`, `concludeDragOperation`) with a file-URL pasteboard. This is ordinary AppKit inside our own process: no TCC permission, no synthetic OS events, no Line 6 code touched or patched. Built as `src/tonematch/engines/native_gui.py` (PyObjC, imported only by the host), the host command `editor_drop`, and `scripts/native_drop_load.py`.
- **Proof 1, same chain:** `drop_drive.hlx` (known.hlx with Drive 0.2), dropped hands-off. Its captured state, rendered in a fresh process, equals the [T013](../../board/TASKS.md#t013 "Run scripts/native_spike.py --gui --golden ..") template plus `knob_01=0.2` in another fresh process at **-315.7 dB** (bit-exact).
- **Proof 2, model change plus CC map:** `model_a30.hlx` (Essex A15 swapped for A30, with 19 written CC assignments). Captured with no dialog; it renders differently from the start state (+3.4 dB), repeats at -313.5 dB, `cc_4` (Cut) moves the render by -10.5 dB, and a template (`4d54671b96a6`) was saved. In a per-CC fresh-process test, 13 of 17 assigned CCs moved the render (-35.7 to +53 dB). The non-movers are explained: Time is ignored while TempoSync is on, and Hum, Cut and the Level range barely change this signal. Unassigned `cc_5/100/127` did not move.
- **Engine:** `NativeEngine.load()` on a CC-mapped template takes 0.15 s, and `set_params` drives the CCs (Drive 0.45 to 0.9: -3.0 dB; returning to the original values nulls at -313 dB). The known chain was re-captured the same way (20 CC maps; the [T013](../../board/TASKS.md#t013 "Run scripts/native_spike.py --gui --golden ..") knob template is in `cache/native/templates_t013_backup/`).
- **HC-mode gotchas (measured):** (1) If the file's device differs from the instance's Hardware Compatibility mode, Native opens an `L6DialogWindow` ("Change the current HC mode to HX Stomp and load the preset / Translate ...") and the load waits. (2) **Native ignores a state that changes HC mode once the instance has processed any audio**, and the host's channel probe processes audio. Fix in `native_host.reload_with_state` (fresh instance, state set first) and in `NativeEngine._set_state`, which reloads when the HC-mode word (ILCP bytes 8-12, plain integer: `0x210100` Helix, `0x210006` HX Stomp) changes. An HC switch costs **7.2 s**; keep all templates in one mode (HX Stomp) to avoid it.
- **Start state.** To avoid the dialog, the instance starts in HX Stomp mode. For this spike I produced `cache/native/spike/start_stomp.state` by setting the ILCP mode word, a plain integer field outside the encrypted record (**lead ruling requested, DECISIONS [T018n](../../board/TASKS.md#t018n "Hands-off Native loading")**). The no-edit alternative is one human click: when the dialog appears once, tick "Don't ask me again" and choose "Change the current HC mode" (stored in `Helix Native.prefs` `ChangeHWModeForFile`, currently 2), after which no start state is needed.
- **Cost:** about **66 s wall per new chain**, once per machine: 3 host spawns, editor open, drop, wait and close in 13.1 s, plus verification renders. The editor window appears on screen for about 13 s, so this needs a logged-in GUI session (not SSH or CI). Nothing is clicked and the user's library is not written.
- **Robustness:** depends on the drop point lying inside the Signal Flow (`DEFAULT_DROP_POINT = (650, 584)` in the 1034 x 656 editor of 3.82). The window size is restored from state (IUCP chunk), so it is stable per version, but re-check after a Native update. `draggingEntered` returns 0 yet `draggingUpdated` and `performDragOperation` accept; any visible `L6DialogWindow` after the drop is reported (`dialogs > 0`) and the template is not saved. Windows and Wine: not applicable (AppKit). An `IDropTarget` equivalent is plausible but untested.

### [T018n](../../board/TASKS.md#t018n "Hands-off Native loading") stage 2 (2026-10-15, after the human's one click)

- **Clean start, no state edit.** The human ran `scripts/native_hc_once.py` (23:50:48Z). They ticked "Don't ask me again" and "Change HC mode", and Native's pref `ChangeHWModeForFile` went from 2 to 1. The resulting `cache/native/start_stomp.state` is HX Stomp mode (`0x210006`), made by Native itself. The edited spike state was deleted. Since the click, a **fresh Helix-mode instance takes an HX Stomp file with no dialog** and switches to HX Stomp, so the start state is optional.
- **Re-measured from the clean flow** (ledger [2026-10-14T23:58:32](../../ledger/runs.csv?plain=1#L88 "smoke · native · smoke · drop_load_null_vs_knob_db = -315.7 (first of 3 rows at this time)") (3 rows), superseding the provisional 23:40:10 rows): dropped Drive 0.2 equals template + knob 0.2 at -315.7 dB; A30 model change +3.4 dB vs start, repeat -313.5 dB, `cc_4` -10.5 dB; 66.8 s per capture.
- **Autocapture in `load()`** (`TONEMATCH_NATIVE_AUTOCAPTURE=1` or `NativeEngine(autocapture=True)`, default off): an unknown chain is written with a CC map, dropped in a fresh host, saved as a template and loaded. If it cannot capture, `load()` raises `UnsupportedModelChange` with the reason (no GUI session or PyObjC, Native asked a question, state unchanged). Code: `capture_chain()` in `native.py`, host command `gui_available`. Fake-host tests are in `tests/test_native_autocapture.py`.
- **Parity files.** `scripts/native_capture_files.py` captures each of `cache/stomp/inject/{K,A,B0,B}.hlx` as itself into `cache/native/templates_files/<stem>/`, so benchmark renders exactly what the Stomp received, bypass states and unmapped cab Hz values included. Capture takes 27.9-28.1 s each. With a 3 s pre-roll, fresh-process replay nulls are K -72.8, A -320.0, B0 -314.1, B -314.2 dB. Pairs: K~A -8.0 dB, B0~B -22.8 dB.
- **Finding: delay lines survive Native's host reset.** With an active DL4 Ping Pong (file B), the default 0.1 s pre-roll gives renders that alternate between two versions (-19.8 dB; the difference is in the first 0.5 s and decays). A 3 s pre-roll brings repeats to -62.7/-65.4 dB. Use `preroll >= 3 s` for chains with an enabled delay (and probably reverb); it costs about 0.14 s per render.
- **Bypass via MIDI CC: unresolved.** No real preset (2,823 decoded, read in place) assigns `@enabled` to a CC. A candidate `controller` entry `"@enabled": {"@controller": 8, "@cc": n}` changed amp and cab renders but did not revert at CC 0, and did nothing for the delay. The `footswitch` section's `@cc` does nothing in Native. Until this is solved, bypass patterns are captured per file (above), or per (chain, bypass pattern) for search.

### Stale-template fix (2026-10-15)

Templates are reused only when the preset's *baked* values match: unmapped params, CCs that did not verify by render, bypass states and block extras. Matching uses a hash stored with the template. Capture maps continuous params with catalog ranges and verifies each CC by one render. On DS-Sample, every CC verified except amp Hum. Bisect files reusing the template through CCs matched their own drop captures at -61 to -326 dB. Unhashed (older) templates are never reused. See DECISIONS 2026-10-15 (stale-template fix).

## Routes, in order (after [T018n](../../board/TASKS.md#t018n "Hands-off Native loading"))

1. **C', in-process drop + MIDI-CC map** (macOS VST3, hands-off). `scripts/native_drop_load.py <hlx> --cc --start-state ...` captures a template per chain; then `load()` plus `set_params` drive up to 90 parameters per chain through `cc_<n>`. `load()` still raises `UnsupportedModelChange` for an uncaptured chain. The proposal is that the engine (or the render service) calls the drop capture itself, see DECISIONS.
1b. **Autocapture in `load()`** (`TONEMATCH_NATIVE_AUTOCAPTURE=1`), the same drop done on demand; per-file capture for parity with `scripts/native_capture_files.py`.
2. **Replay + 16/16 knobs** ([T013](../../board/TASKS.md#t013 "Run scripts/native_spike.py --gui --golden ..")) for already-captured chains, and on Windows, where C' is untested.
3. **AU + DawDreamer + MIDI PC** after one human Setlist Import per 126 presets, generated by `native_setlist.encode_hls`. Useful for bulk template capture on a machine where the editor cannot open.
4. Accessibility scripting: not recommended (blind coordinates, needs permissions).

## Install notes (for `platform`, [T007](../../board/TASKS.md#t007 "Dev environment notes for Windows, macOS, Linux"))

Native is the user's own licence. Never copy the bundle, its presets, its library, its `res/` files or any state dump into the repo.

- **macOS** (Apple Silicon or Intel): install original **Helix Native 3.8x** from line6.com, not Helix Stadium Native. The engine refuses Stadium by path and by Info.plist. The 3.82 installer puts the VST3 at `/Library/Audio/Plug-Ins/VST3/Line 6/Helix Native.vst3` and the AU at `/Library/Audio/Plug-Ins/Components/Helix Native.component`. Both are found automatically, with the VST3 preferred. Open the plugin window once (the `--gui` step) to sign in and authorise. Then `pip install -e '.[native]'`. Python 3.11+; arm64 wheels exist for pedalboard. Override the path with `TONEMATCH_NATIVE_PLUGIN=/path/to/Helix Native.vst3` (or `.component`).
- **Windows**: the VST3 goes to `C:\Program Files\Common Files\VST3\Helix Native.vst3` (or a `Line 6\` subfolder; both are searched). 64-bit Python 3.11+, `pip install -e .[native]`. Authorise in the plugin window on first run (`--gui`). MIDI preset recall would need the AAX build, which no Python host loads, so Windows uses route 1 or 2.
- **Linux (best effort, Wine + yabridge)**: install Native's Windows build under Wine (wine-staging 9+), then `yabridgectl add "$HOME/.wine/drive_c/Program Files/Common Files/VST3" && yabridgectl sync`. The bridged bundle appears at `~/.vst3/yabridge/Helix Native.vst3`, which is a searched path. The editor needs an X/Wayland display; authorisation under Wine is unverified. Don't block on it.
- **Host interpreter**: to keep GPL code out of the main environment entirely, put pedalboard in a separate venv and set `TONEMATCH_NATIVE_PYTHON=/path/to/that/python`. The engine spawns its host with it.
- **Hands-off chain capture ([T018n](../../board/TASKS.md#t018n "Hands-off Native loading"), macOS)**: the host interpreter also needs PyObjC: `pip install pyobjc-framework-Cocoa` (MIT) into the `TONEMATCH_NATIVE_PYTHON` venv along with pedalboard. It needs a logged-in GUI session, because the editor window shows for about 13 s per new chain. No Accessibility, Automation or Screen Recording permission is needed. On Windows and Linux the drop capture is not available (use route 2 or 3).
- **Speed knob**: `TONEMATCH_NATIVE_RESET=flush` selects the fast render mode (see above). Use it only after the spike shows it matches on your chains.

## Observations for the lead

- `scripts/engine_smoke.py` imports `tonematch.preset.load_hlx`, but `preset.py` defines `read_hlx`. As written, engine-smoke fails on every engine with an ImportError (owners: benchmark / preset).
- Design choices in `native.py` that touch contract wording (contract unchanged): a fixed calibration gain (0 dB, Native's digital output is the reference); latency once per chain; every render is a reset render with a 4,800-sample pre-roll; `reset_mode="flush"` is opt-in.
- The 1.6 s fixed reset cost means about 2.3 renders/s across 6 processes on this Mac (empty chain). Search and learning budgets should come from `cost()`, and flush mode (if proven) or batching several clips per reset would change them by more than 10×.
