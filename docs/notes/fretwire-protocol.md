# fretwire and related protocol notes (own summary)

Status: T009, research. Source: [fretwire README](https://github.com/john-baxter-dev/fretwire) (read via summary). The detailed protocol docs live in the repo and were not read line by line; open them before implementing.

## What it is

- Independent Linux-native editor for the HX family (Floor, LT, Rack, Stomp, Stomp XL, HX Effects, POD Go). GTK/WebKitGTK app, headless HTTP daemon mode, browser UI with a mock device, and an MCP interface. Dual Apache-2.0 / MIT.
- Packaged for Debian, Ubuntu, Fedora, Arch and AppImage. Linux only today.

## Protocol facts (from README)

- Line 6 USB vendor ID 0x0E41; control interface is MI_00 (a Windows-style interface name, a hint the interface exists on every OS); per-device product IDs, HX Stomp 0x4246.
- Reverse engineered from observed USB traffic. Control plane only; no firmware, flash or DFU operations are sent (deliberate safety choice).
- Preset streams use MessagePack.
- Operations: preset read/write, parameter edits, block routing, snapshots, device backup/restore.
- Needs reference data (names, ranges, model IDs) imported from the user's own HX Edit install at runtime; nothing from Line 6 is shipped.
- One concurrent session per device. So HX Edit and our tool cannot control the pedal at once (matches T003's coexistence question).
- Verification: HX Stomp fully verified; Floor reads verified; LT/Rack reads only; Effects/XL user-reported; POD Go verified.
- Crates: data parsers, protocol codecs, USB transport, core session, command layer, CLI, GUI, HTTP server, MCP.

## Second source: TonePush

[crmne/tonepush](https://github.com/crmne/tonepush) (MIT) is another open client with PROTOCOL.md and an opcode dictionary (`docs/_reference/opcodes.md`) and model-catalog format notes. Per its README: MessagePack RPC framing over several channels, strict ordered sessions where a timeout poisons the connection, unsolicited device notifications must be drained or writes time out, HX Edit must be closed (exclusive access), and it uses the Rust `nusb` crate, which is cross-platform. Good candidate for the Windows/macOS port question in T003. Also see [helix_usb](https://github.com/Pedalnetics) (Python scripts, per search result; not inspected).

## Implications for the plan

- Cross-platform port looks plausible: pure-Rust USB, but Windows needs a driver that exposes the vendor interface; coexistence with the Line 6 driver is untested (T003).
- Reuse two independent implementations to cross-check opcodes before trusting either.
- Keep to read-only/control-plane operations; do not send anything firmware-related.
- Add TonePush to the doc index (done in PLAN.md).
