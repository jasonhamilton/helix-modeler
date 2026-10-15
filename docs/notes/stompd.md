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
- It prints one line: `{"ok": true, "event": "ready", "device", "presets", "serial", "catalog", "open_ms"}`.
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
| `restore` | `path` | `restored` | writes a device document into the **edit buffer** (op 21, never flash). The client gate (`stomp_bridge.DOCUMENT_WRITE`): by default only a backup taken by the same bridge session may be written (what `close()` uses when the buffer was dirty at open); any other document, such as one compiled from a generated `.hlx`, needs `StompBridge(allow_document_write=True)` (T018s, `StompEngine(whole_preset=...)`) |
| `select` | `setlist`, `index` | `setlist`, `index`, `label`, `name` | loads a preset (op 20), waiting until the device reports it current |
| `model` | `pos`, `model` (catalog id, display name or number), optional `cab` | `pos`, `model`, optional `cab` | changes a block's model; with `cab` it makes an Amp+Cab pair. A model change resets that block's parameters |
| `set` | `pos`, `param` (index, or catalog id/name), `value` (native units) or `text` (display units), optional `paired` (bool, the Amp+Cab's cab), optional `wire` (`int`, `bool` or `float`, for values without a catalog entry) | `pos`, `param`, `native`, `paired`, `text` | sets one parameter (op 30). The reply status is checked, so a refused write is an error. Values past the model's parameter list ("extra" values such as a cab's mic) use the extra addressing |
| `enable` | `pos`, `on` | `pos`, `on` | block on or off (bypass) |
| `route_key` | `pos`, `key` (51 or 52), `to` | `status`, `reply` | sets an input's or output's routing (op 42) with an explicit key. On the Stomp's Input block, USB 5/6 is key 51 value 6 |
| `quit` | none | `bye` | clean teardown, then exit |

**The `slots` field of `chain`.** Each slot has:
- `pos` (the device's slot index), `kind` (Input, Output, Block, Split, Join, Looper, Empty) and `enabled`.
- When present: `model`, `model_id`, `name`, `paired`, `paired_id`, `paired_name` and `routing`.
- `params` and `paired_params`: lists of `{i, value, sym, id, name, text, min, max, kind}`. A value past the model's parameter list carries `extra: <n>` instead of `sym`.

The helper binary also has a few read-only diagnostic commands outside this list (for example notification polling and a raw assignment read). The client never sends them.

## Offline compile mode (T018s build only)

A second build, `cache/stomp/bin/stompd-compile` (source kept locally in `cache/stomp/stompd-src/`, not in the repo), adds two things. The USB helper the engine uses, `cache/stomp/bin/stompd`, is unchanged.
- `stompd compile <hlx> <base document> <out document>`: offline. It never opens USB. It compiles a `.hlx` onto a device document (an edit-buffer backup) and prints one JSON line, `{"ok", "out", "report": {"slots", "changed", "bytes", "identical_to_base", "firmware"}}` or `{"ok": false, "error"}`. Rules are in `docs/notes/T003-stomp-control-spike.md`, section T018s. It uses `hx-proto` (document codec, section table) and `hx-catalog` (model numbers, parameter order, engine class, second count, from the user's HX Edit install). `STOMPD_FORCE_BUILD=1` builds every slot from the file, as a self-check of the builder.
- A `write_doc` JSON command (op 21 plus a read-back with byte compare). It is not in the client allowlist and was not used: the T018s run wrote through `restore` on the approved binary.

## Building (prose)

1. Install Rust. The project used toolchain 1.98.0, the version tonepush pins.
2. Make a new standalone Cargo binary package outside this repo. Give it git dependencies on `hx-usb`, `hx-proto` and `hx-catalog` from `https://github.com/crmne/tonepush`, all at rev `53a6a65b543693913311daff4941a6138327faa8`, plus `anyhow` and `serde_json`. An empty `[workspace]` table keeps it out of any outer workspace.
3. Implement the protocol above on `hx_usb::list()`, `Found::open()` and the `Session` methods (`read_preset`, `preset_info`, `select_preset`, `set_model`/`set_model_pair`, `set_enabled`, `write_preset`, `keepalive`, `poll_notifications`). For `set` and `route_key`, use `request_full` so the reply status can be checked.
4. Run `cargo build --release`. The binary is `target/release/stompd`. A build from a clean checkout at the pinned rev took about 15 s on Apple Silicon.

## Using it

- Set `TONEMATCH_STOMPD` to the absolute path of the binary, e.g. `export TONEMATCH_STOMPD=/path/to/stompd`. Without it, `stomp_bridge.helper_path()` looks for `stompd` on `PATH`. `StompEngine.is_available()` is false when neither is found.
- Quit HX Edit first: it holds interface 0 exclusively.
- On macOS, rendering (not control) needs Microphone permission for the terminal app. Without it, captures are digital silence and the engine raises "returned silence" (see `docs/DEV-ENV.md`).
- Run one session at a time. The engine and `reamp_capture.py` each open their own.
- If the pedal stops answering, pull its 9V adapter for a few seconds; a USB replug does not clear a wedge. Do not retry in a loop.
