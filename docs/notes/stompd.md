# stompd: the HX Stomp control helper (stomp-engine, 2026-10-14)

## What it is

`stompd` is a small **original** wrapper (about 2,600 lines of Rust as of 2026-10-23; it began at about 450, written for this project by `stomp-engine`). It holds one long-lived USB session on the HX Stomp's control interface (interface 0) and speaks JSON lines on stdin and stdout. It is **not part of upstream**. It links three crates from tonepush:

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

## Two-path presets: HX Edit's SABJ layout ([T063](../../board/TASKS.md#t063 "Two-path (Path A + B) render gap on the HX Stomp fw 3.80"), 2026-10-16, fw 3.80)

**What was wrong.** For a `.hlx` with a block on Path B (`@path` 1), `stompd compile` wrote the blocks but kept the base's junctions. On 04C's single-path buffer those are: Split Y Balance A/B 0/1 and Join A/B Pan 0/1 (hard left and right; HX Edit writes 0.5/0.5, also the catalog default), attach points 0/0 (unattached), and the path's topology code 0 ("A"). The device re-derives a layout it does not accept. For 11C it came back with the split before slot 6 and the join before slot 9, instead of HX Edit's 5/7. Cab A then sat before the split, so Path B ran amp > cab A > cab B. Result: -9.3 dB null against the same preset stored by HX Edit, and 6.4 dB quieter. The 2026-10-15 dual-path gap (-18.3 / -22.0 dB) was this same layout bug.

**How the device stores a two-path path** (from documents only: the HX Edit preset 11C read back after `select`, and what the device wrote back after our op-21 writes; interop, nothing decompiled):

| Field | Where | Values |
|---|---|---|
| Topology code | `tone[0][21]` | 0 = A (single path), 1 = SABJ (every HX Edit two-path preset in the setlist), 2 = SAB (Path B with its own output). The device re-derived 2 from a write with its own Path B input and dropped that input, so AB is not supported |
| Attach point | split body 15 / join body 17, key 13 | the slot the junction sits before = `@position` + input slot + 1 (11C: split @4 → 5, join @6 → 7) |
| Model, enabled, values | body 15/17 keys 8, 10, 7 | Split Y [BalanceA, BalanceB, bypass], Join [A Level, A Pan, B Level, B Pan, B Polarity, Level] |
| Snapshot switch | slot body key 18 | true when `@no_snapshot_bypass` is false (HX Edit); false on a SAB join |
| Junction snapshot state | `tone[10][10][n][3][slot]` | HX Edit lists `split` per snapshot, never `join`; the device keeps the join on (SABJ) or off (SAB) |
| Path B input / output | split body 14 key 5 / join body 16 key 6 | 0/0 in SABJ; SAB has output 6 = USB 3/4 and the output's values `[pan, gain, Type]` in join body 16 key 7 (empty on a single-path base: written empty, the device turned the SAB write into SABJ) |
| Path A input gate | input slot body key 7 | `[noiseGate, threshold, decay]`; 11C has the gate on at -57 dB / 0.15. Before this change the base's gate was kept |

**What the compiler does now** (`cache/stomp/bin/stompd-compile`, sha256 `7de8467d...`; previous builds kept as `stompd-compile.pre-sabj` = `f48396a2`, and the two intermediate builds `.sabj-v1` and `.sabj-v2`). Source changes are in `cache/stomp/stompd-src/src/compile.rs` and `main.rs`; the pre-change source is kept in `cache/stomp/stompd-src-backup-pre-sabj/`.
- A file with a Path B block gets all of the table above from the file. Topology: SABJ when `outputB.@output` is 0, SAB when it is set. A file that says "A" but has Path B blocks is compiled the same way, with a warning. An own Path B input (`inputB.@input` ≠ 0) is refused. Split must attach before join, and join no later than the output slot. Another junction type (crossover, A/B) is built with catalog-typed values; this is not yet tested on hardware.
- The report gains `topology` and `junctions`. `stompd decode` shows `attach` and `b_routing` on Split/Join slots, plus `path_topology`.
- **Single-path files are unchanged:** 28 of 28 earlier `.hlx` compile byte-identically with the old and new build. The input gate is compiled for every file, but all 28 have the base's gate values.
- Python side: `tonematch.engines.stomp_layout.ensure_sabj` (called by `reamp_hlx_dict`) gives a generated preset with a Path B block HX Edit's SABJ layout: Y split and join at 0.5/0.5, split @0 and join @8 (the whole row in parallel, as HX Edit does for a first Path B block), Path B input/output 0, `split` on in each snapshot. Files that already say SABJ or SAB are left as written. The readback (`stomp_readback`) checks junction model, enabled, values, attach and topology code (attach and code only from `decode`; the live `chain` of the USB helper does not report them). `stomp_hw.make_dual` now writes SAB.

**Hardware check** (`scripts/stomp_twopath.py run`, one session per run; the pedal started and ended on 04C, restored by readback and byte-equal, session-close acknowledged on all channels). Final run `cache/stomp/hw/20261016T010428Z-twopath`, di_2s, amp Hum = Ripple = 0 on both sides:

| Null (aligned, gain-matched, median) | dB | repeat floor |
|---|---|---|
| ours (old compiler) vs HX Edit's 11C | -9.3 | -85.7 |
| ours (new compiler) vs HX Edit's 11C | **-85.7** | -85.7 |
| single path K0, old vs new build (byte-identical documents) | -79.7 | -68.7 |
| dual SAB, Path A vs K0 alone | -72.9 | -68.7 |
| dual SAB, Path B vs B00 alone | -52.6 | -42.7 |

So dual passes (two chains per render, 2.0x throughput) now equal single-path renders. Ledger rows [2026-10-16T01:06:38](../../ledger/runs.csv?plain=1#L304 "parity · stomp · stomp-two-path · null_vs_hxedit_db = -9.3 (first of 4 rows at this time)") (4 rows) (`stomp-two-path`). Two earlier runs of the same script (00:55Z and 01:00Z) found the device-side fields. In the 01:00Z run the topology code was still 0, so the attach was re-derived (-5.3 dB). Superseded.

Resolved 2026-10-16 ([T093](../../board/TASKS.md#t093 "S4 engine push: Simple Delay Scale reported missing on 11C")): the `Scale` value of HD2_DelaySimpleDelay is `stereo_only` in the catalog and 11C's delay is mono, so the device stores no Scale. See "Engine push" below.

## Engine push ([T090](../../board/TASKS.md#t090 "S3 engine push: device check")-[T095](../../board/TASKS.md#t095 "Engine-push pedal session"), 2026-10-16, fw 3.80)

**Pedal lock and device check ([T090](../../board/TASKS.md#t090 "S3 engine push: device check")).** `src/tonematch/pedal_lock.py`. Each `StompBridge` session first checks the device: the HX Stomp must be on USB (macOS: IOUSB "HX Stomp", or VID 0x0E41 / PID 0x4246; Linux: sysfs). Then it takes `cache/stomp/pedal.lock/`, which is re-entrant in one process and inherited by child processes through `TONEMATCH_PEDAL_LOCK_TOKEN`. Batch scripts hold the lock for the whole run with `pedal_lock.session(purpose)`.

| Item | Value |
|---|---|
| owner.json | pid, agent, host, purpose, created_utc, heartbeat_utc (every 30 s), token, argv |
| Stale, removed without asking, only if ALL hold | owner pid dead (same host); no `stompd` in two `ps` checks 5 s apart; heartbeat older than 120 s; no fresh (60 s) intent from a live pid in `pedal.intent/` |
| Otherwise | interactive: shows owner.json, age and the checks, asks; non-interactive: exit 75 (`LockHeld`) with instructions. A lock without owner.json (legacy mkdir) always goes to the user |
| Removal | rename aside, re-check owner.json, delete; one JSON line in `pedal.lock.log` (who, why, the checks) |
| Device missing | "HX Stomp not detected over USB: check the cable and power"; interactive retry/abort; non-interactive exit 69 (`DeviceMissing`), never loops; stompd's "no HX device found" gives the same |
| CLI | `python -m tonematch.pedal_lock status`, `break [--yes]`, `device` |

**render_many ([T091](../../board/TASKS.md#t091 "S1 engine push: StompEngine.render_many")).** `StompEngine.render_many(dry, variants)`, called through `tonematch.engines.base.render_many`. `two_path_fit(a, b)` decides whether a pair fits. `merge_two_path(a, b)` builds the SAB preset: a's frame, b's blocks renamed onto Path B, Split Y at @0 and Join at @8 at 0.5/0.5, `inputB` 0, `outputB` 6 (USB 3/4), and no controller assignments for b. One `playrec` records all inputs; Path A comes from USB 1/2 and Path B from USB 3/4, each trimmed and level-matched with the one calibration and aligned against that path's own jitter reference. Pairs that do not fit, or that are refused, render serially (`two_path_skips`). Hardware, control only: K+A and C+K merged, 84 of 84 read back, 1.2-1.9 s per pair load. The audio comparison is still open.

**Layout-changing loads ([T092](../../board/TASKS.md#t092 "S2 engine push: layout-changing loads through the whole-preset…")).** The default is now `fallback`. `plan_load` treats any Path B block on either side as a layout change. A layout the device re-derives raises `LayoutRefused` (`ReadbackMismatch` + `UnsupportedModelChange`); a value mismatch stays a plain `ReadbackMismatch`.

**Readback ([T093](../../board/TASKS.md#t093 "S4 engine push: Simple Delay Scale reported missing on 11C")).** `stomp_readback.stereo_only_params`: a file value that the catalog marks `stereo_only`, on a block with `@stereo` false, is info, not missing. 11C (selected read-only) gives 84 of 84. The device's mono Simple Delay values are Time, Feedback, Mix, Level, SyncSelect1, TempoSync1 and extra 0 (trails). Delay Mix and Time: exact after a whole write, after `set_params`, and 3 s later.

**Timing ([T094](../../board/TASKS.md#t094 "S5 engine push: whole_preset default proposal"), 2026-10-16).** Parameter-only load: 0.27-0.35 s in place, 1.42-1.62 s whole. The whole load splits into backup 0.04-0.23 s, compile 0.25-0.32 s and op-21 write 0.62-0.91 s. The write was 0.29 s on 2026-10-15; the cause of the slowdown is not known.

**Session [T095](../../board/TASKS.md#t095 "Engine-push pedal session").**
- `scripts/stomp_push_session.py [--control-only]`, runs `cache/stomp/hw/20261016T180156Z-push` and `20261016T182912Z-push`.
- CoreAudio streams failed on the whole Mac (AUHAL err 35, PortAudio -9986). A blocking `sd.playrec` then hung forever, so `reamp.duplex` now polls the stream with a deadline: the stream length plus `STREAM_GRACE_S` (10 s).
- The read-only select of 11C and back left 3 bytes of a non-chain field different (msgpack keys 9/10/11: 0/0/false against 255/65280/true). Both forms occur in 04C backups since 2026-10-15. The chain signature was equal, so the backup was not written back.


## Restore verdict: decoded equality ([T097](../../board/TASKS.md#t097 "E4: judge pedal restores by decoded equality"), 2026-10-16)

Lead ruling "S1 closed" (`docs/DECISIONS.md`, 2026-10-16). In the 20:27Z push session the edit buffer read after the restore was 2459 bytes against a 2456-byte backup, yet `stompd decode` gives the same document field for field: after another preset is selected the device re-encodes the buffer. The byte check alone gave a false failure.

- `tonematch.engines.stomp.compare_backups(backup, after, decoder)` decodes both buffers (`decode_document`, offline) and deep-compares them; dicts are compared by key, never by order. Identical bytes need no decode (`restore_compare: "bytes-identical"`). When the bytes differ and the documents agree, one info log line is written, not a failure. On a real difference, `decoded_difference` names the first differing field.
- `StompEngine.close(verify_bytes=True)` adds `decoded_equal_backup`, `restore_compare` and the headline **`restore_ok` = chain match and `decoded_equal_backup`** to `restore_report`. `bytes_equal_backup` stays as a secondary signal.
- Fallback: with no decoder (a custom compiler without one, or no stompd compiler configured, or a decode error) `decoded_equal_backup` is None, `restore_compare` is `"bytes"`, `decode_unavailable` says why, and `restore_ok` uses the bytes. Without `verify_bytes`, `restore_ok` is the chain match alone (`"chain-only"`).
- `restore_verdict(report)` reads `restore_ok`, or for reports written before [T097](../../board/TASKS.md#t097 "E4: judge pedal restores by decoded equality"), match and bytes. The pedal scripts (`stomp_push_session`, `stomp_fw380`, `parity_spot`, `parity_timefx_stimuli`, `stomp_speed`, `stomp_twopath`, `stomp_twopath_random`, `parity_null`, `stomp_inject`) print `restore_ok` and `decoded_equal_backup` next to the bytes, and their exit codes follow `restore_ok`. `benchmark_suite`'s parity check uses `restore_ok` when the summary has it, otherwise the bytes.
- Tests: `tests/test_stomp_restore_verdict.py` (fake decoders: equal, real difference, decoder missing, no compiler). It has one optional test on the two push-session buffers, read from `cache/` only and skipped when they or an offline decoder are missing. Those buffers are never committed.

## Verification ([T108](../../board/TASKS.md#t108 "HX Stomp verification suite"), stomp-verify-v1, 2026-10-17, fw 3.80)

`scripts/stomp_verify.py` (`gen` offline, `run` on the pedal, `analyse` and `ledger` offline). The pass rules were fixed in `docs/DECISIONS.md` before the first run and were not changed afterwards. There were four sessions, each holding the shared pedal lock, with HX Edit closed, working in the 04C edit buffer only. Each ended with the pedal restored to 04C "Smoke Known" and judged by decoded equality. Runs are in `cache/stomp/hw/20261017T{165159,165405,170717,171958}Z-verify/`; the ledger rows are under dataset `stomp-verify-v1`.

**Setting truth (check 1, 40 random items over 8 categories).**
- The device stores every value as **float32 of what was sent; it applies no coarser grid**, for Hz, dB, time, 0-1 knobs, menus and switches alike (288 values).
- In-place `set_params` and a whole write read back identically.
- The device reports parameter ranges as float32 copies of the catalog's. `set_params` therefore sends values that differ from the file's value below float32 precision, and once by one float32 step (VIC Rotating Decay).
- Strict readback: 23/40. With the comparison in float32: 34/40.
- The other 6:
  - 4 models the device refuses as whole documents (VIC Stutter Edit, L6SPB Inf Sustain, NonLinear, and Moon Nrm with the 4x12 Greenback30WithPan IR cab): the device accepts the op-21 write but never shows the document. `StompEngine` raises `LayoutRefused`.
  - 1 legitimately silent amp (Grammatico Brt with DriveBright 0).
  - The 1-step case above.

**Sampling.**
- With the old `PRE_S` of 0.25 s, 10-23 % of deterministic renders had a one-sample slip or a 1-6 ms burst 0.25-0.67 s after the stream start, inside the clip. PortAudio reported no xrun for any of them.
- Amps carry state from the previous render: -39 dB at 0.3 s, -56 dB at 1 s, -77 dB at 2 s, -94 dB at 3 s (RevvGen Purple).
- Engine now: `PRE_S` 1.0 s, `AMP_PRE_S` 2.0 s, `reamp.MAX_JITTER` 4. Category repeat floors went from -31 to -69 dB (drive), -39 to -82 dB (EQ) and -38 to -60 dB (comp).
- A slip still happens about once in 90 renders, at any time (one was 1.77 s into a stream). The sentinel caught one.
- Latency between renders of one setting still differs by -1..+1 samples; the alignment absorbs it.

**Write-then-render.** A render straight after `set_params` or a whole write equals settled renders (3b, 3/3 in both sessions). `set_params` settles about 100-130 ms after its command returns. A whole write changes the audio before the command returns.

**Order and route.** DL4 Echo Platter renders differently in place than after a whole write, reproducibly: -9.5 and -16.4 dB, with a fractional lag of -0.38. Glitz shows the same, -27 dB. This is consistent with a time parameter gliding after an in-place change; open.

**Sentinel.** `StompEngine(sentinel=Sentinel(...))`, or `TONEMATCH_STOMP_SENTINEL=on|off|auto` (auto: on when the purpose names a benchmark), with `TONEMATCH_STOMP_SENTINEL_EVERY`.
- Every N renders it backs the edit buffer up, loads the reference, renders the reference clip, compares the result with the first reference render (threshold max(floor + 6 dB, -60 dB)), writes the backup back and checks the chain signature.
- On a drift it raises `SentinelDrift`, keeps `reference.npy` and `drifted.npy`, and refuses every later render; `close()` still restores the pedal.
- It tripped twice on its own, both times stopping the session cleanly; the second trip was a confirmed one-sample slip. The forced +5-sample test was caught at -13 dB.

**Session close.** After a session that hit a device refusal, the pedal did not acknowledge the session-close on two of three channels. Every later session opened normally.

## Robustness follow-ups ([T110](../../board/TASKS.md#t110), [T111](../../board/TASKS.md#t111), [T112](../../board/TASKS.md#t112)), 2026-10-17, fw 3.80

Probe session `cache/stomp/hw/20261017T204855Z-robust-probe/` (diagnosis); rules for the scored session in `docs/DECISIONS.md` (2026-10-17, "registered BEFORE any of their runs").

**The pedal wedged in the probe.** The session was lost after 9 refused op-21 writes (8 on purpose, one pilot) and about 35 accepted ones. The next open failed with `usb error: write timed out`. Power-cycle the pedal (9V adapter) before the next session. After a refused write the device leaves a reset edit buffer. [T108](../../board/TASKS.md#t108 "HX Stomp verification suite scripts/stomp_verify.py") sessions, with 1-3 refusals, did not wedge. The engine now avoids refusals instead of provoking them.

**[T110](../../board/TASKS.md#t110 "Device refuses whole documents of VIC Stutter Edit, L6SPB Inf…"): why the device refused four models.** Found by comparing, slot by slot, the document the device builds itself (`model` command, then `backup`) with ours (`stompd dump <doc> 0 22`).

| Model | Device | Our catalog build |
|---|---|---|
| VIC Stutter Edit, L6SPB Inf Sustain | engine class 24, no trails value, counts n = n2 (18, 14) | class 8, a trails value, n = n2 + 1 |
| NonLinear | 18 values + trails: TempoSync2, SyncSelect2 (missing from HX Edit's symbol list), and 3 hidden values 0.318 / 0.362 / 2 | 16 + trails, hidden values 0 |
| Moon Nrm + 4x12 Greenback30**WithPan** | the `model` command itself is refused (error -21) | (not a compiler matter: the pairing does not load) |

- **Compiler fix:** `stompd-compile` has device templates (`STOMPD_TEMPLATES=<dir>`; file `m<model number>[_c<cab number>].bin`, a device backup holding that block).
  - A built slot is the device's own slot with the file's values written over it. The values go by the index the `set` command uses; the trails value goes last.
  - A negative value into an unsigned slot is now signed, not clamped to 0.
  - Build sha256 `e4280756...`. The previous build is kept as `stompd-compile.pre-t110` and the source backup as `cache/stomp/stompd-src-backup-pre-t110/`. Without templates the output is byte-identical (78 of 78 `.hlx`).
- **Engine:** `StompEngine` captures a template the first time a whole write builds a mono model or Amp+Cab pairing (`needed_templates`, `capture_templates`).
  - A capture is one `model` command on a Path A block slot and two backups. Then the buffer is written back and its chain signature checked.
  - Templates are kept per firmware in `cache/stomp/templates/<fw>/`, with `index.json`.
  - A refused `model` command is recorded there and raised as `ModelRefused` before any document is written. `TONEMATCH_STOMP_TEMPLATES=off` turns capture off.
- **Readback** (`stomp_readback`): the last extra value is the trails (or mic); extras before it are hidden values. A file parameter the symbol list does not name, at its catalog default, is info.

**[T111](../../board/TASKS.md#t111 "In-place time-parameter glide"): in-place delay/reverb changes.** Not reproduced in the probe: in-place renders equal whole-written ones at the repeat floor (Echo Platter -57 dB, Glitz -56 dB). The probe tried the full set from whole-written defaults, each parameter's round trip, and Time from the other side, with renders at 0, 5 and 20 s. [T108](../../board/TASKS.md#t108 "HX Stomp verification suite scripts/stomp_verify.py")'s sequence differed: defaults set in place, then B in place, with no render between. It is replicated in the next session, with a document backup after each step. Policy until then: a parameter change on an enabled delay or reverb block is a whole write (`ambient_changes`; `TONEMATCH_STOMP_AMBIENT_INPLACE=1` turns it off; counted in `stats()["ambient_whole"]`).

**[T112](../../board/TASKS.md#t112 "Residual one-sample USB slips"): stream-slip guard.**
- **How it works.** Every preset is written with a bypassed `HD2_EQSimple3Band` on Path B (SAB, Path B output on USB 3/4), so USB 3/4 records the dry clip through the same stream as Path A. A 30 ms marker plays after the clip; it reaches the pedal after the last returned sample.
- **The check.** `slip_check` fits the integer lag (+-16 samples around the calibrated latency) and the gain, then scores the residual per 2.5 ms window. Each window's residual is compared with that window's energy, or with the mean window energy - 30 dB when the window is quieter.
- **Re-takes.** A take over the threshold is taken again, up to `TONEMATCH_STOMP_SLIP_RETAKES` (3) times, then `StreamSlip`. A discarded take never becomes an alignment reference.
- **Modes.** `TONEMATCH_STOMP_SLIP_GUARD=on` (default): `render_many` renders serially. `pairs`: two-path passes are allowed but unguarded. `off`: no guard.
- **Not guarded,** counted by reason in `stats()["slip_guard"]["unguarded_why"]`: two-path passes, 8-block presets, an input noise gate that is on, or a session where the device refused the pilot layout once (tried once per session).
- **Status.** The threshold is set by the registered phase D, and the scored phase P is still to run. The probe's own pilot, a bypassed Gain block, was refused.

**Next session (after the power cycle):** `scripts/stomp_robust.py session`, then `analyse <dir>` and `ledger <dir>`. Rules: `docs/DECISIONS.md`.

## The control-channel wedge: cause and fix ([T168](../../board/TASKS.md#t168), 2026-10-18, fw 3.80, macOS)

**Symptom.** A bridge call fails with `write timed out` or with no reply to a transaction. After that the pedal refuses every new session until its 9V adapter is pulled (a USB replug does not clear it; a wedged pedal was still wedged 2.5 min later). Two incidents in the [T166](../../board/TASKS.md#t166 "Live preview on the Stomp") live preview (12:19:18, 12:24:44), one on 2026-10-17 ([T110](../../board/TASKS.md#t110 "Device refuses whole documents of VIC Stutter Edit, L6SPB Inf…")), and one reproduced on purpose in [T168](../../board/TASKS.md#t168 "TOP PRIORITY (human 2026-10-18 \"fix the stomp freezing issue") (13:14:54).

**What the failing call actually was.** The `restore:` label is the op-21 whole-document write. In both live-preview incidents it was the template capture's put-back (`StompEngine.capture_templates`): `model` (a new drive, first use) → `chain` → `backup` → op-21 of the buffer as it was. The kept documents (`cache/stomp/hw/t168-incident-docs/`) and the template files' mtimes (2 s and 6 s before each failure; 6 s is hx-usb's reply budget) show it.

**Mechanism (from the [T168](../../board/TASKS.md#t168 "TOP PRIORITY (human 2026-10-18 \"fix the stomp freezing issue") traces, `STOMPD_TRACE=1`).**
- During an op-21 write the device returns one flow-control credit per chunk: an empty `0x08` frame on the edit channel whose acknowledgement field is our byte count plus a constant (fretwire's protocol notes describe the same credits). Healthy non-final credits arrive in 0.7-4 ms; the final chunk is always credited late (35-210 ms) because the device applies the document first.
- The wedge: an op-21 write started while the device was still rebuilding its DSP after another DSP-changing command (a model change, or an op-21 0.4 s earlier; a `chain` read just before took 153 ms instead of 40). The device took two chunks and stopped crediting for good. hx-usb does not wait for credits (after any 80 ms of silence it sends the next chunk), so three more chunks went out, the next write blocked for the full 2 s, and the device was left holding a half-received message: the state only the 9V clears. fretwire records the same signature ("credits stopping after chunk 2"; backing off does not rescue it; read before every op-21).
- Ruled out as the cause: (1) **idle backlog**: with hx-usb's own IN scheme the device's keepalives (one per open channel per second) do queue, but the queue settles at about 4 transfers of 3 frames each, it does not grow with idle time; (2) **audio streaming**: the lead's playback began after both incidents; (3) **the IN read scheme alone**: the wedge was reproduced with the IN pump on.
- A short gap is necessary but not sufficient: 40 capture sequences on 04C's own buffer passed with a 0.1 s gap; the incident-2 document wedged the pedal both times it was written soon after a DSP change.

**What the helper does now** (`cache/stomp/bin/stompd` sha256 `defeb16f...`; previous build kept as `stompd.pre-t168` = `f48396a2`; source in `cache/stomp/stompd-src/`, pre-change source in `cache/stomp/stompd-src-backup-pre-t168/`):
- **Op-21 settle gate** (every `restore`): at least `STOMPD_OP21_GAP_MS` (default 1500) since the last `restore`, `model` or `select` ended; then a fresh read of the edit buffer that answers in under 80 ms (re-tried every 300 ms while the device is slow, up to 6 s); then 300 ms without notifications. The reply carries `gate: {gap_ms, reads_ms, quiet_ms, ms}`. Cost: up to about 1.8 s on an op-21 that follows a DSP change; nothing when the gap has already passed (e.g. after a render).
- **Credit-paced chunks**: a chunk of a long write waits until the device has taken every earlier chunk (never more than one chunk ahead of its credits), up to 3 s; a device that takes nothing for 3 s is sent nothing more. `STOMPD_PACE=0` turns it off (diagnosis).
- **IN pump**: a thread keeps one read posted at all times, as HX Edit does, and queues transfers on the host; the idle tick (time-based, once a second, also between back-to-back commands) reads everything waiting before its keepalive, and every command first reads what is waiting. `STOMPD_PUMP=0` / `STOMPD_IDLE_DRAIN=0` restore hx-usb's scheme (diagnosis).
- **Model settle**: `model` returns after the device has been quiet for `STOMPD_MODEL_SETTLE_MS` (default 300 ms, bounded 3 s); the reply carries `settle`.
- **Link stats on every reply**: `link: {writes, write_ms_max, slow_writes, in, in_bytes, backlog_max, idle_backlog_max, in_age_ms_max, credits, credit_ms_max (non-final chunks only), ahead_max, pace_wait_ms_max, idle_writes, idle_write_ms_max}`. Writes made by the idle tick (keepalives) are counted apart from command writes (`idle_*`). `ahead_max` is measured when the session reads the credit, so it can read 2-3 although the pacing (which uses the pump's arrival time) never sends a chunk before the earlier ones are taken.
- **`drain`** (read-only, allowlisted): reads everything waiting and reports `drained`.
- **Trace**: `STOMPD_TRACE=1` prints `TRACE {json}` per frame, credit, tick, gate and command on stderr.

**Python side.**
- `StompBridge` folds the link stats into `link_summary`. A slow write (over `TONEMATCH_STOMP_PRECURSOR_MS`, default 100 ms; healthy under 5, a flash `select` 18) or a slow non-final credit (over `TONEMATCH_STOMP_PRECURSOR_CREDIT_MS`, default 20 ms; healthy under 16 even with the old IN scheme), or an idle keepalive write over `TONEMATCH_STOMP_PRECURSOR_IDLE_MS` (default 1000 ms; one isolated 133 ms idle pause with normal traffic straight after was seen in the first soak and stopped it needlessly), marks the bridge `degraded`; a further `restore` then raises `LinkDegraded` before anything is sent, while reads, `select` and `quit` still work, so the run stops with the pedal still answering. An older helper sends no stats and never degrades.
- `TONEMATCH_STOMP_TRACE=1` (or a path) writes every call and reply with its link stats, plus the helper's TRACE lines, to `cache/stomp/trace/<UTC>-<pid>.log`.
- Template capture (approved by the human, 2026-10-18, "yes"): when the whole-preset load that follows rebuilds or empties the capture slot, the op-21 put-back is left out (`StompEngine.putbacks_skipped`); it is kept when nothing follows, when the load keeps the slot's model, on a refusal and on any error. Checked on the pedal: for 4 drive models the edit buffer after the load is byte-identical with and without the put-back (`cache/stomp/hw/20261018T204112Z-t168-putback-equiv`).

**Reproduction and soak.** `scripts/stomp_soak.py` (`--sequence load|capture`, `--audio`, `--helper-env K=V`, stops at the first precursor, error, slow step, or someone else's audio on the Stomp). Results (fw 3.80, macOS, 04C edit buffer only, ledger `smoke/stomp-soak` rows of 2026-10-18):

| Run | Loads | Result | Max command write | Max non-final credit | Notes |
|---|---|---|---|---|---|
| Incident replay, IN pump only (`...201451Z-t168-C-new`) | 2 cases | **wedged** at the incident-2 op-21 | 2009 ms (timeout) | credits stopped after chunk 2 | the reproduction; 9V needed |
| Incident replay, full fix (`...203902Z-t168-C-new`) | 2 cases | ok | 7.6 ms | 2.8 ms | the same documents and sequence |
| Put-back equivalence (`...204112Z-t168-putback-equiv`) | 4 models x 2 | byte-identical end states | | | |
| Soak 1 (`20261018T-t168-soak200`) | 40/200 | stopped by the idle-write precursor (one 133 ms idle keepalive); pedal fine | 18 ms (flash select) | | 33 captures, 33 put-backs skipped; led to the idle rule |
| **Soak 2** (`20261018T-t168-soak200b`) | **200/200** | **no wedge, no precursor** | 9.5 ms | 9.8 ms | idle 1-60 s, 1 h 50 min, 15 k command + 19.5 k idle writes, readback ok 200/200 |
| **Audio soak** (`20261018T-t168-soak-audio`) | **60/60** | **no wedge, no precursor** | 4.1 ms | 6.7 ms | duplex stream throughout (106,923 callbacks, 0 status flags), fresh templates: 39 captures, 39 put-backs skipped |

**If it still happens.** A wedged pedal does not recover by itself (still wedged after 2.5 min); only the 9V clears it. The bridge stops a run at the first precursor, before a further op-21. Run with `TONEMATCH_STOMP_TRACE=1` and keep the log: it shows every chunk, credit, gate and keepalive.
