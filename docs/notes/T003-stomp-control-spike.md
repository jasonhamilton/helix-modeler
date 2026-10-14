# T003 spike: driving the HX Stomp from code (stomp-engine, 2026-10-13)

Status: **offline half done 2026-10-13; hardware batch H1-H9 run on macOS 2026-10-14 (results in "Hardware batch results" below).** H3a/H3b (HX Edit exclusivity) were skipped on instruction (HX Edit not launched); H10/H11 need Linux/Windows machines. Upstream code was read and built in a scratch directory; none of it, and no Line 6 file, is in this repo.

## Hardware batch results (2026-10-14, macOS 27 arm64, HX Edit closed)

**Firmware: 3.50** (build `v3.15-207-g09aad57`, device id `0x00210006`, serial 3554786), read over USB in H2. Not 3.80: generated `.hlx` files for this pedal must carry device_version for 3.50 (the HX Edit golden says 3.50, consistent; the Native golden's 3.80 is not this pedal).

Tooling (scratchpad only, not in the repo): `stompd`, a long-lived JSON-lines helper (about 450 lines, written for this task) built as an example inside the scratch tonepush checkout at `53a6a65`. It holds one session, sends keepalives and drains notifications every second when idle, and exposes edit-buffer operations only. Save, rename, slot write, settings write and IR commands are not exposed. Python client: `src/tonematch/engines/stomp_bridge.py` (`TONEMATCH_STOMPD` points at the binary). Sessions opened in about 0.5 s; about 20 sessions were opened and closed this run without a wedge.

| # | Result | Evidence |
|---|---|---|
| H1 | PASS | `system_profiler SPUSBHostDataType`: HX Stomp, VID 0x0e41, PID 0x4246, 480 Mb/s; `fretwire detect`: "HX Stomp: present"; `tonepush list`: "HX Stomp (pid 0x4246) serial 3554786 - 126 presets" |
| H2 | PASS (firmware 3.50, not 3.80) | Original preset **04C "Smoke Known"** (setlist 0, index 11). Edit buffer backed up to `cache/stomp/spike/orig_editbuffer.bin` (2460 bytes) and chain to `cache/stomp/spike/orig_chain.json`. `read_preset_at` (op 4, read a stored slot) fails on 3.50: "preset blob was not an l6-helix document". |
| H3a/b | SKIPPED | HX Edit was not launched (instruction). Exclusivity still rests on upstream evidence only. |
| H3c | PASS | "HX Stomp" CoreAudio device has 8 in and 8 out at 48 kHz (no volume or mute controls; one stream each way); MIDI in and out "HX Stomp". Control `info` calls made during an 8 s render: no error, no dropout. USB control, USB audio and USB MIDI (H8, H8b) coexist. |
| H4 | PASS, after a fix | **Input "USB 5/6" is value 6 on the Stomp**, not the Floor menu's 15. tonepush's name lookup picks 15, which the device accepts and echoes back, but no audio arrives. A sweep of 0..16 found 6 = host 5/6 (5 to L, 6 to R) and 7 = host 7/8. Values 3, 5, 9, 10, 12 and 13 are refused. Record side: 1/2 = Path A, 3/4 = Path B, 5/6 = dry Main L/R in (digital zero with nothing plugged in). |
| H5 | PASS | Drive 6.1, 2.0, 8.0: probe renders changed (aligned null p0/p2 -15.0 dB, p1/p2 -6.0 dB, p0/p1 -6.8 dB); repeat at the same setting -30.8 dB. Without input, Drive and Ch Vol at 10 raised the idle noise floor from -86.99 to -71.65 dBFS, and putting them back gave -86.99 again. |
| H6 | PASS | Essex A15 to US Deluxe Nrm: model command 0.242 s; render null +10.9 dB (louder and brighter); repeat -48.4 dB. **`set_model` on an Amp+Cab drops the cab**; the engine uses `set_model_pair`. |
| H7 | PASS | Amp bypass: render changed (rms -5.6 dB, null -1.7 dB); re-enabled render nulls at -67.3 dB against the earlier one. Bypassed amp with idle input: -186.6 dBFS. |
| H8 | PASS | Drive assigned to MIDI (source 8) and CC 20; reads back as "MIDI CC", CC 20. CC 20 = 0 / 127 on channel 1 gives Drive 0.0 / 10.0 (read back over USB); renders 27 dB apart. Narrowed range Min 0.4 / Max 0.6: CC 0, 1, 64, 127 give 0.400, 0.4016, 0.5008, 0.600, so the step is 0.00157 of the range. |
| H8b | PASS | Program Change 12 moves the pedal to 05A, PC 11 back to 04C, with the USB control session open throughout. |
| H9 | PASS | Re-selecting 04C reloads it from flash; chain, routing, values and assignments match H2 exactly (`chain_signature`, `cache/stomp/spike/final_chain.json`). The original buffer had no unsaved edits. No flash write was made at any point in this batch. |

Protocol findings on firmware 3.50 (fed back into the helper; tonepush is tested on 3.80):

- **Output-block routing is confirmed under key 52, not 51.** tonepush's `set_routing` sends 51 for both blocks. On an Output block this left it routed to None and silent, and re-sending 51 did not undo it. `{98: 9, 52: 1}` on op 42 fixed it. The engine never routes the Output block.
- `request` does not check reply status, so a refused write looks like success. The helper's `set` and `route_key` check the status.
- An Amp+Cab's cab parameters are op 30 with key 26 = 1 (fretwire's `set_paired_value`). The cab's **mic** sits past its `Helix.sym` list (an "extra" value): key 29 = false, key 28 = 0, int wire type. `.hlx` calls it `@mic`. tonepush's `.hlx` import does not apply cab settings at all.
- A whole-buffer write (op 21, `write_preset`) restored the slots and the same assignments, in a different order. One op-21 write was made, to the edit buffer only, during the first engine dry run.

Audio and timing (`tonematch.reamp`, `cache/stomp/latency.json`):

- Round-trip latency (impulse, USB 5/6 to USB 1/2, transparent chain): **1206 samples (25.1 ms)** with PortAudio `latency='low'` (measurements 1207, 1206, 1206, 1206, 1206). With default buffering it is 12498 to 12499 (260 ms); blocksize 256 gives 1490 to 1491, blocksize 512 gives 8914 to 8915. The impulse comes back as a single sample at unity. **Stream-start jitter is 0 to 1 sample on every setting.** `render()` cancels it by aligning (±2 samples) to the chain's first render of the same clip.
- Level: -18 dBFS 1 kHz through the transparent chain comes back at -18.000 dBFS, so the calibration gain is 0.0 dB.
- Repeatability on known.hlx, di_2s, after alignment: -31 to -36 dB null between repeat renders. Probably sub-sample variation (the Essex model's Hum and Ripple may add some); fractional alignment is a possible improvement. `load()` rebuilding the chain from a different amp, then rendering, nulls at **-50.3 dB** against the flash-loaded preset.
- Cost (engine, 2 s clip): **2.667 s per render**, parallelism 1. `load_penalty_s` **0.658 s** (whole `load()` with one Amp+Cab model change; the model command alone is 0.12 s). No settle is needed: a render straight after `load()` nulls as well as a repeat does. A `load()` with nothing to change takes 0.081 s. The reamp-capture script's capture (1 s pre-roll, 2 s tail) takes 5.82 s for the 2 s clip.
- Not measured: parallel Path A/B renders (`parallelism=2`), user-DI capture (`--user-di` is written but not run on hardware, nothing plugged in).

Machine: macOS 27.0.1 (26A434), Apple Silicon arm64, Python 3.14.8. Rust 1.99.0 and 1.98.0, installed for this spike under the session scratchpad only (`RUSTUP_HOME`/`CARGO_HOME` pointed there; nothing added to `~/.cargo` or the shell profile).

## Summary: control route per OS (macOS confirmed on hardware 2026-10-14; Linux and Windows still proposed)

| OS | Route | Confidence | Why |
|---|---|---|---|
| **macOS** | **USB control (interface 0) through tonepush's `hx-usb` crate, behind the `stompd` helper. CONFIRMED: H1 to H9 pass, and `engine-smoke` is green on firmware 3.50.** MIDI CC (H8) and PC (H8b) work alongside it. | Confirmed | tonepush was developed and hardware-tested on exactly this setup (HX Stomp fw 3.80, macOS 27.0, Apple Silicon, HX Edit 3.82). On macOS interface 0 has no kernel driver bound. Audio and MIDI are class-compliant and live on other interfaces. Both projects build and pass their offline tests here. |
| **Linux** | USB control: fretwire (its home OS) or tonepush (also supports Linux, with a udev rule) | High | fretwire is verified on the HX Stomp on Linux. It needs the udev rule. Pin 48 kHz for audio. |
| **Windows** | **MIDI CC fallback** on templates; USB control is unlikely | Medium | fretwire states that the Line 6 driver owns interface 0 on Windows. `nusb` on Windows can only claim an interface bound to WinUSB. Rebinding with Zadig would break HX Edit and probably the Line 6 ASIO audio. tonepush ships Windows builds, but nothing in its tree or issues shows a Windows claim succeeding with the Line 6 driver installed. One hardware check settles it (H9). |

Coexistence, all OSes: **HX Edit and our controller cannot hold interface 0 at the same time.** HX Edit claims it exclusively; tonepush's claim error tells the user to quit HX Edit, and fretwire documents one session per device. So the rule is sequential use: quit HX Edit before a Stomp-mode run. USB audio (interfaces 1 to 3) and USB MIDI (interface 4) are separate interfaces. tonepush's MIDI-clock hardware test drives CoreMIDI while its control session is open, which is evidence that MIDI and control coexist on macOS. That is their test, not ours; H3 repeats it with audio streaming.

## Evidence (offline, reproducible)

| Check | Result |
|---|---|
| fretwire @ `fa27c819d00e` (2026-10-13), `cargo build --release -p fretwire-protocol -p fretwire-usb -p fretwire-core -p fretwire-cli` on macOS arm64 | Builds unmodified, 24 s |
| fretwire offline tests (`-p fretwire-protocol -p fretwire-usb -p fretwire-core -p fretwire-data`) | 281 passed, 0 failed |
| `fretwire detect` (enumerates via nusb, IOKit on macOS; claims nothing) | Runs: "no HX device found" (correct, nothing plugged in) |
| tonepush @ `53a6a65b5436` (2026-10-13), toolchain pinned 1.98.0, `cargo build --release -p hx-proto -p hx-usb -p hx-catalog -p tonepush-cli` | Builds, 27 s |
| tonepush offline tests, same crates | 280 passed, 0 failed, 27 ignored (the hardware tests) with `HX_EDIT_RESOURCES` pointing nowhere. With this Mac's HX Edit 3.51 resources found automatically, 1 fails: `hlx::tests::reads_a_preset_shipped_with_hx_edit` skips split/join in 3.51's `default_preset.hlx`. That is an `.hlx` import edge case with old resources, not a USB or macOS problem. |
| `tonepush list` | Runs: "no supported devices found" |
| Python audio/MIDI stack in a scratch venv: `sounddevice` 0.5.6 (PortAudio 19.7), `python-rtmidi` 1.5.8 (CoreMIDI API) | Both import and enumerate. `python-rtmidi` has **no wheel for Python 3.14** and fails to build from source on macOS (`sem_timedwait` in the JACK backend). It builds with `pip install python-rtmidi --config-settings=setup-args="-Djack=false"`. |
| `scripts/stomp_spike_probe.py selftest` | OK (probe signal -18.0 dBFS RMS, 96000 samples, compare logic) |
| `system_profiler SPUSBDataType` | No Line 6 device on the bus |

Found on this Mac: **HX Edit 3.51** at `/Applications/Line6/HX Edit.app` (bundles `libusb-1.0.0.dylib`), plus prefs from 2022 to 2023. It is too old for firmware 3.80; T005 and T010 need HX Edit 3.82. No Line 6 kext, DriverKit extension or HAL plug-in is installed, which is consistent with the Stomp being class-compliant on macOS.

## What the source says (file references are upstream, at the commits above)

- **Device topology** (tonepush `PROTOCOL.md`, "Device topology"): interface 0 is vendor-specific, bulk `0x01` OUT / `0x81` IN (the editor channel); 1 to 3 are USB audio; 4 is USB MIDI; 5 is HID. "Interface 0 has no kernel driver bound to it, and HX Edit holds it exclusively while running." The editor protocol is not on MIDI.
- **Windows** (fretwire `crates/fretwire-usb/src/lib.rs` header; `docs/next-steps.md` Track 2; `STATUS.md`): "on Windows the Line 6 driver owns interface 0"; "Windows can't claim the interface". nusb docs: on Windows the interface must be bound to WinUSB.
- **Edit buffer vs flash** (tonepush `crates/hx-usb/src/commands.rs`): `set_param`, `set_model`, `set_model_pair`, `set_enabled`, `set_routing`, `write_preset` (a whole document) and the controller assignments all change **the edit buffer only**. Only `save_preset`, `rename_preset` and the IR, favourite and settings writes touch flash. This is how `load()`/`set_params()` can avoid writing the user's slots at all.
- **Whole-preset write** (`write_preset`, op 21): tonepush paces it on completion notification 20 and verifies by reading back; fretwire's CLI calls op 21 "the operation that has produced every device lockup on record". So prefer `set_model` plus `set_param` edits for `load()`, and use whole-document writes only to restore the original state.
- **MIDI CC assignment from code** (tonepush `assign_parameter` with source ordinal 8 = MIDI, then `set_assign_cc` op 64, then `set_assign_range` ops 65 and 66 for Min/Max): a CC template can be built in the edit buffer over USB. Narrowing Min/Max lets 128 CC steps cover a smaller range, so effective resolution is finer than 1/128.
- **Fragility** (tonepush `PROTOCOL.md`): handshake once per session; drain the IN endpoint between operations; wait for notification 20 on deferred ops; never call USB reset (the pedal drops off the bus). A wedged pedal needs the **9V adapter pulled**; a USB replug is not enough. The device keeps sequence state across connections, so tonepush claims, releases and claims again.
- **Line 6 EULA**: see `docs/notes/line6-eula-flags.md` (research, T009). This spike adds no protocol work and uses only the published results of fretwire and tonepush.

## Implications for `StompEngine` (input to T001; T004 builds it)

- **Bridge, not one-shot CLI calls.** The CLIs open a session, handshake and close on every command. That costs time per call and adds handshake churn, which is the main cause of wedges. Plan: one long-lived Rust helper (JSON lines over stdin/stdout) that holds one tonepush `hx-usb` session, sends a keepalive, and exposes `info`, `read_preset`, `select_preset`, `set_param`, `set_model`, `set_enabled`, `set_routing`, `write_preset` (restore only) and the assignment ops. Pin it to a git revision: `hx-proto` is on crates.io but `hx-usb` is not. tonepush is the first choice because its tested platform is ours; fretwire is the second, for cross-checking opcodes.
- **`load(preset)`**: select a base (the human-named scratch slot, or the current preset), then edit the buffer: `set_model`, `set_param` and `set_enabled` per slot, and `route 0` to USB 5/6. Never `save`.
- **`close()`**: on open, store the current setlist and index and `backup` the edit buffer document to `cache/stomp/`. On close, `select_preset(original)`. If the buffer was dirty on open, `write_preset(backup)`. Then read back and compare.
- **Parameter values**: the protocol takes values in the parameter's own units, not 0..1. The engine maps from the catalog's normalized range using the catalog import (T005).
- **MIDI route (Windows)**: the preset agent writes templates as `.hlx` with controller assignments. The human imports them into scratch slots once, through HX Edit. A Program Change selects a template; CCs sweep values. A model change means another template, which is a large `load_penalty_s`. A model with no template raises `UnsupportedModelChange`.

## Proposal to the lead: contract `engine-interface.md` (not changed here)

1. Allow `load()` on the Stomp MIDI route to raise `UnsupportedModelChange`, as the contract already allows for Native-by-automation. **Breaks:** `search` (it must group by chain for this engine too; it already must for Native) and `benchmark` (engine-smoke must load a template that exists).
2. Standardise two `Capabilities.extras` keys: `control_route` (`"usb"` | `"midi-cc"`) and `param_steps` (`None` = continuous, `128` = CC). **Breaks:** nobody, since it is additive. `search` should read `param_steps` for its step size.
3. Parallel Path A/B renders (`parallelism=2`) need either a `render_many(dry, presets)` method or two engines sharing one device. Defer until T004 measures whether it works. **Would break:** `search` and `learning`.
4. `close()`: "restores the pedal's original preset" should also say "including unsaved edit-buffer changes present at open". **Breaks:** `benchmark` (smoke check step 4 should compare the document, not only the index). *Implemented in StompEngine (restore_report, value signature with assignments in any order). `engine_smoke.py` now fails when `restore_report.match` is false; that is the skill's step 4, and no contract text was changed.*
5. (hardware, 2026-10-14) `preset-schema.md` assumes firmware 3.80. This pedal is on **3.50**. **Affects:** `preset` (device_version of generated files; models newer than 3.50 must be refused for this pedal), `benchmark` (Gate 3 "parity for firmware 3.80" would read "3.50" unless the human updates the pedal).
6. (hardware) On the Stomp, the Input block's "USB 5/6" is `@input` = **6**. **Affects:** `preset` (a reamp template should write 6; `known.hlx` has 1 = Main L/R). The engine routes the edit buffer to 6 itself, so nothing breaks today.
7. (hardware) v0 `load()` cannot add or remove blocks: a preset whose layout differs from the pedal's current one raises `UnsupportedModelChange`, the same as Native's automation route. **Affects:** `search` (group trials by chain layout, as for Native).

## Proposed DECISIONS.md entry (for the lead, after the hardware batch)

> Stomp control route: macOS and Linux use USB interface 0 through tonepush's `hx-usb`, pinned by git revision, behind a long-lived helper (fretwire is the cross-check). Windows uses the MIDI CC fallback on HX Edit-imported templates unless H9 shows interface 0 can be claimed with the Line 6 driver present. HX Edit must be closed during Stomp runs. The engine edits the edit buffer only and never saves. Why: T003 evidence. Who: lead, on stomp-engine's report.

## Hardware batch for the human (one sitting, about 30 to 40 min on this Mac)

Preconditions: the Stomp is connected by USB with its 9V adapter; **HX Edit is quit**; the human **names one scratch preset slot** (for example `32C`) that may be overwritten in the edit buffer. Nothing in this list saves to flash. Never run `save`, `rename`, `restore-all`, `ir-*`, `set-setting`, or tonepush's `--ignored` device tests (some of them write slots). If the pedal stops answering, **pull the 9V adapter for a few seconds**; a USB replug does not clear it.

Set up once (stomp-engine can do this before the session):
`T=<scratch>/tonepush/target/release/tonepush`; `F=<scratch>/fretwire/target/release/fretwire`; `P="<venv>/bin/python scripts/stomp_spike_probe.py"`.

| # | Step | Command | Pass when |
|---|---|---|---|
| H1 | Enumeration | `system_profiler SPUSBDataType \| grep -A8 -i "HX Stomp"`; `$F detect`; `$T list` | VID 0x0E41, PID 0x4246 seen by all three |
| H2 | Read-only session, firmware, original state | `$T info`; `$T preset > cache/stomp/spike/orig.txt`; `$T chain >> cache/stomp/spike/orig.txt`; `$T backup cache/stomp/spike/orig_editbuffer.bin` | Firmware reads 3.80; the original preset index is recorded |
| H3a | HX Edit exclusivity | Open HX Edit, wait for it to connect, run `$T info` | Fails with the claim error (expected) |
| H3b | Release | Quit HX Edit, run `$T info` | Succeeds |
| H3c | Audio and MIDI alongside control | `$P devices` (expect a "HX Stomp" audio device and MIDI ports; `stomp_48k_ok: true`). Then start `$P render --tag coexist --seconds 8` and, while it plays, run `$T info` in a second terminal | No dropout or error in either; the WAV is not silent |
| H4 | Go to scratch, route input | `$T select <scratch>`; `$T route 0 "USB 5/6"`; `$T chain` | Input shows USB 5/6 |
| H5 | **Render confirms a parameter change** | `$P render --tag p0`; `$T set <amp block> Drive 2.0`; `$P render --tag p1`; `$T set <amp block> Drive 8.0`; `$P render --tag p2`; `$P compare p0 p2` | `compare` prints CHANGED; `chain` shows the values |
| H6 | **Render confirms a model change** | `$T model <amp block> "<another amp name>"`; `$P render --tag m1`; `$P compare p2 m1` | CHANGED; time the `model` command (gives `load_penalty_s`) |
| H7 | **Render confirms a block change** | `$T enable <amp block> off`; `$P render --tag b1`; `$P compare m1 b1`; `$T enable <amp block> on` | CHANGED |
| H8 | **MIDI CC fallback on one parameter** | `$T assign-param <amp block> <drive idx> 8`; `$T assign-cc <amp block> <drive idx> 20`; then `$P cc --cc 20 --value 0`, `$P render --tag c0`, `$P cc --cc 20 --value 127`, `$P render --tag c1`, `$P compare c0 c1`; `$T assignments <amp block> 6` | CHANGED; the assignment reads back as MIDI CC 20. Check first that CC 20 is not reserved in the 3.80 manual's MIDI table; use the MIDI channel set in the Stomp's global MIDI settings. |
| H8b | Program Change (Windows-style template select) | `$P pc --program <original index>` and confirm the pedal moves; `$P pc --program <scratch index>` | The pedal shows that preset |
| H9 | Restore | `$T select <original index>`; `$T chain` and diff against `orig.txt`. If the original buffer had unsaved edits, `$T restore cache/stomp/spike/orig_editbuffer.bin` | Same preset and values as H2 |
| H10 | Linux (only if a Linux box is available) | fretwire `install-udev`, then `detect`, `pull`, `goto`, `set`, `swap`, `bypass` on the scratch slot, plus `$P render` with the rate pinned at 48 kHz | Same pass rules as H5 to H7 |
| H11 | Windows (only if a Windows PC is available, with the Line 6 driver and HX Edit installed) | tonepush MSI, then `tonepush list` and `tonepush info`. **Do not install Zadig or WinUSB over the Line 6 driver.** Then H8 and H8b through the Line 6 MIDI port | Records whether interface 0 can be claimed; the MIDI path works |

When H1 to H9 pass on macOS, stomp-engine writes the ledger rows (`smoke` for control, `render-cost` with the measured seconds per render and `load_penalty_s`). Then the engine route is settled for Gate 1, "Stomp on at least one OS", and T004 can start.

## Reproduce the offline half

```sh
S=<scratchpad>; export RUSTUP_HOME=$S/rustup CARGO_HOME=$S/cargo PATH=$S/cargo/bin:$PATH
curl -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal
git clone https://github.com/john-baxter-dev/fretwire $S/fretwire   # fa27c819d00e
git clone https://github.com/crmne/tonepush $S/tonepush             # 53a6a65b5436
(cd $S/fretwire && cargo build --release -p fretwire-cli && cargo test --release -p fretwire-protocol -p fretwire-usb -p fretwire-core -p fretwire-data)
(cd $S/tonepush && cargo build --release -p tonepush-cli && HX_EDIT_RESOURCES=/nonexistent cargo test --release -p hx-proto -p hx-usb -p hx-catalog -p tonepush-cli)
```

## Licences (for reviewer, T011)

fretwire: MIT OR Apache-2.0. tonepush: MIT. sounddevice: MIT. python-rtmidi: MIT. No dependency was added to `pyproject.toml` in this spike; T004 adds the `stomp` extra (`sounddevice`, `python-rtmidi`) and the pinned Rust helper.

## Sources

- [fretwire](https://github.com/john-baxter-dev/fretwire) (README, `crates/fretwire-usb/src/lib.rs`, `docs/next-steps.md`, `STATUS.md`)
- [tonepush](https://github.com/crmne/tonepush) (README, `PROTOCOL.md`, `crates/hx-usb/src/{lib,commands}.rs`, `crates/hx-usb/tests/device.rs`, `docs/_guide/using-the-cli.md`)
- [nusb docs: Windows needs WinUSB on the interface](https://docs.rs/nusb/latest/nusb/)
- [Line 6 forum: reserved MIDI CCs on HX Stomp](https://line6.com/support/topic/36546-reserved-midi-cc-and-external-midi-controllers-hx-stomp/) (the full list is in the 3.80 manual; check it before H8)
