# stompd: the HX Stomp control helper (stomp-engine, 2026-10-14)

## What it is

`stompd` is a small **original** wrapper (about 450 lines of Rust, written for this project by `stomp-engine`). It holds one long-lived USB session on the HX Stomp's control interface (interface 0) and speaks JSON lines on stdin and stdout. It is **not part of upstream**. It links three crates from tonepush:

- `hx-usb`: the USB session (handshake, keepalive, pacing, teardown).
- `hx-proto`: messages and the preset document.
- `hx-catalog`: model and parameter names, read at runtime from the user's own HX Edit install.

- Upstream: https://github.com/crmne/tonepush, pinned at rev `53a6a65b543693913311daff4941a6138327faa8`.
- Licence: tonepush is MIT (LICENSE file checked: "MIT License, Copyright (c) 2026 Carmine Paolino"). The wrapper is intended to be MIT as well.
- Protocol cross-check: fretwire (MIT OR Apache-2.0), https://github.com/john-baxter-dev/fretwire. Two things came from its published protocol notes: paired-cab parameters and "extra" values.
- Interop only. Neither project is decompiled firmware; no Line 6 file is involved.

**Decision (the human, 2026-10-14):** the wrapper's source is **not** in this repo. This note gives the link and build notes only.

## Limitation

The wrapper source is not published. The Stomp results in `docs/notes/T003-stomp-control-spike.md` and the Stomp ledger rows of 2026-10-14 depend on it, so **others cannot reproduce them yet**. They need either the published wrapper or their own wrapper to this protocol. The Python side does not depend on the wrapper's internals, only on the protocol below: `tonematch.engines.stomp_bridge`, `tonematch.engines.stomp` and `scripts/reamp_capture.py`.

## Protocol (what `StompBridge` expects)

**Startup.**
- The binary takes no arguments. It finds the first HX device on USB and opens a session; tonepush's open includes its single built-in retry.
- It prints one line: `{"ok": true, "event": "ready", "device", "presets", "serial", "catalog", "open_ms", "teardown", "release"}`. `teardown: "session-close"` marks a build that ends sessions the way HX Edit does ([T059](../../board/TASKS.md#t059 "Bug: pedal blocks stay locked after any control session"), build of 2026-10-15 on).
- If it cannot open the device, it exits with an error on stderr. A common cause is HX Edit holding the interface.

**Requests and replies.**
- Each request is one JSON object per line: `{"cmd": <name>, ...arguments}`.
- Each reply is one line. Success: `{"ok": true, "ms": <elapsed>, ...}`. Failure: `{"ok": false, "error": <text>}`.
- While idle, the helper polls device notifications and sends keepalives about once a second.
- If the session is lost, it prints `{"ok": false, "event": "lost", "error": ...}` and exits.

**Allowlist.** The client sends only these commands, enforced in `stomp_bridge.ALLOWED_COMMANDS`. All are reads or edit-buffer edits. Nothing saves, renames, writes a slot, writes a setting or touches IRs.

| Command | Arguments | Reply fields | Effect |
|---|---|---|---|
| `info` | none | `device`, `device_id`, `firmware`, `build`, `setlist`, `index`, `label`, `name` | read |
| `chain` | none | `firmware`, `build`, `tempo`, `snapshots`, `setlist`, `index`, `label`, `name`, `assignments` (one text line per controller assignment), `slots` (see below) | read |
| `backup` | `path` | `path`, `bytes`, `checksum` | reads the edit buffer document and writes it to a local file |
| `restore` | `path` | `restored` | writes a device document into the **edit buffer** (op 21, never flash). The client gate (`stomp_bridge.DOCUMENT_WRITE`): by default only a backup taken by the same bridge session may be written (what `close()` uses when the buffer was dirty at open); any other document, such as one compiled from a generated `.hlx`, needs `StompBridge(allow_document_write=True)` ([T018s](../../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp"), `StompEngine(whole_preset=...)`) |
| `select` | `setlist`, `index` | `setlist`, `index`, `label`, `name` | loads a preset (op 20), waiting until the device reports it current |
| `model` | `pos`, `model` (catalog id, display name or number), optional `cab` | `pos`, `model`, optional `cab` | changes a block's model; with `cab` it makes an Amp+Cab pair. A model change resets that block's parameters |
| `set` | `pos`, `param` (index, or catalog id/name), `value` (native units) or `text` (display units), optional `paired` (bool, the Amp+Cab's cab), optional `wire` (`int`, `bool` or `float`, for values without a catalog entry) | `pos`, `param`, `native`, `paired`, `text` | sets one parameter (op 30). The reply status is checked, so a refused write is an error. Values past the model's parameter list ("extra" values such as a cab's mic) use the extra addressing |
| `enable` | `pos`, `on` | `pos`, `on` | block on or off (bypass) |
| `route_key` | `pos`, `key` (51 or 52), `to` | `status`, `reply` | sets an input's or output's routing (op 42) with an explicit key. On the Stomp's Input block, USB 5/6 is key 51 value 6 |
| `quit` | optional `release` (bool, default true; false only for diagnosis) | `bye`, `release` | ends the session, then exits. After `bye` the helper prints one more line, the `closed` report (below) |

**The `slots` field of `chain`.** Each slot has:
- `pos` (the device's slot index), `kind` (Input, Output, Block, Split, Join, Looper, Empty) and `enabled`.
- When present: `model`, `model_id`, `name`, `paired`, `paired_id`, `paired_name` and `routing`.
- `params` and `paired_params`: lists of `{i, value, sym, id, name, text, min, max, kind}`. A value past the model's parameter list carries `extra: <n>` instead of `sym`.

The helper binary also has a few read-only diagnostic commands outside this list (for example notification polling and a raw assignment read). The client never sends them.

## Session end ([T059](../../board/TASKS.md#t059 "Bug: pedal blocks stay locked after any control session"), 2026-10-15)

**Cause of the "blocks locked after a session" fault.** tonepush's `Session` (rev 53a6a65) ends a session by draining and acknowledging only; it sends no closing handshake (its `Drop` says so; upstream removed the close in commit 9fd6fe2, 2026-08-05, after a hardware suite could not re-open the device). The pedal then believes an editor is still attached and keeps block editing locked until USB is unplugged. That is why unmodified tonepush shows the same fault (ledger `session-release` rows, fw 3.50 and 3.80).

**What HX Edit does on quit** (observed traffic only; interop):
- tonepush's own HX Edit captures (`captures/mac-*-capture.log` in the tonepush repo, 10 clean quits, all the same): one bare frame per channel, the 8-byte channel header with type `0x02` (the handshake type) and no body, flags `0x18`, the channel's next sequence number and current acknowledgement. Order: events `0x1002` and data `0x1080` (back to back), each answered by the device with its own `0x02`, then control `0x1001` last, answered. The interface is released about 1.3 s later.
- fretwire `docs/protocol.md` "Session teardown - the panel lock fix" and `STATUS.md` (verified live 2026-06-25): the same frames in the order status `0x1002`, edit `0x1080`, primary `0x1001`; "skipping this leaves the device believing the editor is still attached: the front panel stays in the connected/locked state". Request/response matters: sending the frames blind and releasing at once did not release the panel; each answer is awaited (300 ms cap) and a short settle (150 ms) precedes the release.

**What the helper does now.** The session runs over the helper's own copy of hx-usb's USB transport (`src/link.rs`, opened through hx-usb's public `Session::replaying(wire, profile)`; no upstream code changed). The transport watches every frame, so after hx-usb's own drain and final ACKs it knows each channel's last sequence number and acknowledgement. It then sends the three close frames in HX Edit's order, waits up to 300 ms for each answer, drains for up to 300 ms, waits `STOMPD_CLOSE_SETTLE_MS` (default 300 ms), and releases interface 0. Then it prints:
`{"ok": true, "event": "closed", "release": true, "release_sent": true, "acks": {"events": bool, "data": bool, "control": bool}, "ms": n}`
(`skipped` instead of `acks` when a write had timed out: a pedal that stopped reading gets no close.) This also runs when stdin closes or the session is lost. `STOMPD_NO_RELEASE=1` turns it off (diagnosis only).

Offline check: replaying all 10 HX Edit captures through the same bookkeeping predicts every one of HX Edit's 30 close frames exactly (sequence and acknowledgement); the helper's unit test reproduces the three frames of `mac-rename-capture.log` byte for byte. Hardware confirmation is pending ([T059](../../board/TASKS.md#t059 "Bug: pedal blocks stay locked after any control session") phase 3).

**Fallback: `stompd reset [wait_s]`.** A second mode re-enumerates the pedal's USB (nusb `Device::reset`, IOKit `USBDeviceReEnumerate` on macOS), the software form of an unplug, then waits up to `wait_s` (default 15) for a new device instance: `{"ok": true, "reset": true, "reappeared": bool, "old_instance_listed": bool, "reset_ms", "ms"}`. It claims nothing and sends no protocol frame. **Untested on hardware, and tonepush's PROTOCOL.md says this call took their HX Stomp off the bus until a physical replug**, so it is a fallback only. `StompBridge.close()` runs it per `TONEMATCH_STOMP_USB_RESET` / `StompBridge(usb_reset=...)`: `auto` (default) only when a session was open and the helper did not report `release_sent` (an older helper, a killed helper, or the release turned off); `always`; `off`. Never while `tonematch.reamp` has a stream open (waits up to 30 s, then skips), never on a wedged pedal (a reset does not clear a wedge; the 9V does). After a reset that comes back, PortAudio's device list is re-scanned; one that does not come back prints a plain "unplug and replug the USB cable" message. `reamp_capture.py`, `parity_null.py run` and `stomp_inject.py run` take `--usb-reset {auto,always,off}`.

**Builds.** `cache/stomp/bin/stompd` is the new build (sha256 `756071b2...`); the previous one is kept as `stompd.pre-T059` (`30422bed...`). The new build also contains the `compile` and `decode` modes. The source before this change is in `cache/stomp/stompd-src-backup-pre-T059/`. `stompd-compile` is unchanged.

## Offline compile mode ([T018s](../../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp") build only)

A second build, `cache/stomp/bin/stompd-compile` (source kept locally in `cache/stomp/stompd-src/`, not in the repo), adds two things. The USB helper the engine uses, `cache/stomp/bin/stompd`, is unchanged.
- `stompd compile <hlx> <base document> <out document>`: offline. It never opens USB. It compiles a `.hlx` onto a device document (an edit-buffer backup) and prints one JSON line, `{"ok", "out", "report": {"slots", "changed", "bytes", "identical_to_base", "firmware"}}` or `{"ok": false, "error"}`. Rules are in `docs/notes/T003-stomp-control-spike.md`, section [T018s](../../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp"). It uses `hx-proto` (document codec, section table) and `hx-catalog` (model numbers, parameter order, engine class, second count, from the user's HX Edit install). `STOMPD_FORCE_BUILD=1` builds every slot from the file, as a self-check of the builder.
- A `write_doc` JSON command (op 21 plus a read-back with byte compare). It is not in the client allowlist and was not used: the [T018s](../../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp") run wrote through `restore` on the approved binary.
- `stompd decode <document>` ([T051](../../board/TASKS.md#t051 "Bug: rank2_H_live delay read back Mix 0.72 / Feedback 0.52 / Time…"), 2026-10-15): offline, never opens USB. Prints `{"ok", "doc", "chain"}`. `chain` has the same shape as the `chain` command, without the session fields, plus `snapshot_controllers`: one `{snapshot, place, flag, value, slot, paired, param}` per stored controller value.
- [T051](../../board/TASKS.md#t051 "Bug: rank2_H_live delay read back Mix 0.72 / Feedback 0.52 / Time…") compile changes: each snapshot stores the values of controller-assigned parameters at `tone[10][10][n][2]` as `[flag, place, value]`, with `place` being the assignment's key 0 and 64 meaning unused. **The device recalls them over the block values on load.** The compiler now takes them from the file (`snapshotN.controllers.dsp0.<key>.<param>.@value`, else the block value). Before this it kept the base's, so rank2_H's delay ran Feedback 0.52 instead of 0.40. It also takes the global tempo and the per-snapshot tempos from the file. The report gains `warnings`, which lists controller assignments where the file and the base disagree (the assignments themselves are still the base's). The build is sha256 `6300d2a5...`; the previous one is kept as `cache/stomp/bin/stompd-compile.pre-T051` (sha256 `de374807...`). Both outputs are byte-identical on K, A, B0, B, C and the old H, and `STOMPD_FORCE_BUILD=1` still rebuilds 04C byte for byte.
- **Do not point `TONEMATCH_STOMPD_COMPILE` at the USB helper.** A build without `compile`/`decode` ignores its arguments and opens a USB session. `tonematch.engines.stomp.supports()` therefore looks for the mode's usage text in the binary and refuses to run it when the text is missing. Previously `compile_document` fell back to the USB helper.

## Building (prose)

1. Install Rust. The project used toolchain 1.98.0, the version tonepush pins.
2. Make a new standalone Cargo binary package outside this repo. Give it git dependencies on `hx-usb`, `hx-proto` and `hx-catalog` from `https://github.com/crmne/tonepush`, all at rev `53a6a65b543693913311daff4941a6138327faa8`, plus `anyhow` and `serde_json`. An empty `[workspace]` table keeps it out of any outer workspace.
3. Implement the protocol above on the `Session` methods (`read_preset`, `preset_info`, `select_preset`, `set_model`/`set_model_pair`, `set_enabled`, `write_preset`, `keepalive`, `poll_notifications`). For `set` and `route_key`, use `request_full` so the reply status can be checked. Open the session over your own `hx_usb::Wire` implementation (`Session::replaying(wire, profile)`, with the interface claimed, released and claimed again as hx-usb does) so the session-close above can be sent after the session is dropped; add `nusb = "0.2"` for that.
4. Run `cargo build --release`. The binary is `target/release/stompd`. A build from a clean checkout at the pinned rev took about 15 s on Apple Silicon.

## Using it

- Set `TONEMATCH_STOMPD` to the absolute path of the binary, e.g. `export TONEMATCH_STOMPD=/path/to/stompd`. Without it, `stomp_bridge.helper_path()` looks for `stompd` on `PATH`. `StompEngine.is_available()` is false when neither is found.
- Quit HX Edit first: it holds interface 0 exclusively.
- On macOS, rendering (not control) needs Microphone permission for the terminal app. Without it, captures are digital silence and the engine raises "returned silence" (see `docs/DEV-ENV.md`).
- Run one session at a time. The engine and `reamp_capture.py` each open their own.
- If the pedal stops answering, pull its 9V adapter for a few seconds; a USB replug does not clear a wedge. Do not retry in a loop.
- If the pedal answers but its blocks cannot be edited after a session (the editor lock), unplugging and replugging USB releases it. With the session-close above this should no longer happen; report it if it does.

## 2026-10-15 ([T056](../../board/TASKS.md#t056 "HX Stomp priorities (human, 2026-10-15)"))

- **Health check:** `StompBridge` runs `info` with a 3 s timeout on open (`health_check=True`). On no answer, or a hang on open, it raises `PowerCycleNeeded` with a plain message for the user.
- **`stompd compile`** (build sha 62b12a19; previous build kept as `stompd-compile.pre-pathB`) also writes Path B's input (`inputB.@input`, split slot body 14, key 5) and output (`outputB.@output`, join slot body 16, key 6). On the Stomp, output value 6 = USB 3/4; 1 and 5 merge into USB 1/2. Byte-identical to the previous build on every earlier preset.
- **MIDI checks** use a separate python with `python-rtmidi` (`TONEMATCH_MIDI_PYTHON`). The project venv does not have it.

## Compiler, 2026-10-15 (fw 3.80): IR-style cabs and assignment-free building

- **IR-style (`HD2_CabMicIr_...`) cabs are built the way the device stores them** (fretwire `captures/pairing_sweep.md`, measured live on an HX Stomp): block class 33 when paired with an amp (31 alone, 32 dual; legacy cabs are 18, 15, 16), values = the symbol's parameters without the trailing `IrData` (7 of 8, Mic first), no extra mic value, both counts equal. Before this, N2 (Essex A15 + 4x12 Cartog Guv) was compiled as class 18 with 9 values; the pedal accepted the op-21 write and never showed it back. Now N2 and N3 (Brit 2203 + 4x12 Greenback30) land and read back (ledger [2026-10-15T23:40:21](../../ledger/runs.csv?plain=1#L236 "smoke · stomp · fw380-write · ir_cab_pairs_landed = 2.0 (first of 4 rows at this time)") (4 rows)).
- **Assignments on changed slots are dropped instead of refusing the build.** When a slot changes model or is emptied, its controller assignments (`tone[4][source]`, parameter and bypass), its footswitch bindings (`tone[3][8][switch]`) and the snapshot controller entries of the removed parameter assignments (set to `[false, 64, nil]`) are removed. Edit buffer only; the report's `changed` lists what was dropped. `STOMPD_KEEP_ASSIGNMENTS=1` restores the old refusal. So 04C's buffer is now an assignment-free base for any slot: one block per category (Screamer, Digital Delay, Room reverb, Red Comp, Simple EQ, Opto Tremolo) was built into 04C's delay slot, written and read back on fw 3.80 (ledger 23:40:21; `scripts/stomp_fw380.py`).
- Outputs for every earlier preset (K, A, B0, B, C, H, the ER sweep, N1) are byte-identical to the previous build, and the force-build self-check still reproduces 04C byte for byte.
- `stompd dump <document> <key>...` prints a part of a document's tone (offline, diagnosis).
- Builds: `cache/stomp/bin/stompd-compile` sha256 36669475...; previous build kept as `stompd-compile.pre-micir` (62b12a19...). The USB helper `stompd` is unchanged.
