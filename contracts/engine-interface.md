# Engine interface, v0 (frozen 2026-10-14, Gate 1)

Owner: lead. Implemented by `native-engine` and `stomp-engine`. Consumed by `search`, `benchmark`, `learning`. The code form is `src/tonematch/engines/base.py`; this file is the spec.

Every engine is a class implementing `tonematch.engines.base.Engine`. Steps 1 to 3 and 5 to 7 of the pipeline never know which one is running.

## Methods

| Method | Does | Notes |
|---|---|---|
| `load(preset: Preset) -> None` | Puts a whole candidate on the engine | A `Preset` is the in-memory form of an `.hlx` (see `preset-schema.md`). Model changes happen here, not in `set_params`. |
| `set_params(changes: dict[ParamRef, float]) -> None` | Changes parameter values on the loaded preset | `ParamRef` is `(slot, param_name)`. Values are the catalog's normalized 0..1 range. Must be cheaper than `load`. |
| `render(dry: Audio) -> Audio` | Returns the processed audio for a dry clip | 48 kHz float32 mono in, mono out, same length. Level-matched to the engine's reference level (a fixed calibration gain, Native's digital output being the reference; clips are never normalised individually) and trimmed by its measured latency (measured once per chain, since blocks add latency). |
| `cost() -> Cost` | Reports measured `seconds_per_render` for a 2 s clip, `parallelism` (how many renders can run at once), and `load_penalty_s` (extra settling after a model change) | Measured, not guessed. The optimizer picks its strategy and budget from this. |
| `close() -> None` | Releases the device or plugin host and leaves the device as it was found | The Stomp implementation restores the original preset and any unsaved edit-buffer changes present at open. Also run by an exit guard. |
| `capabilities() -> Capabilities` | Reports `engine` (`"native"` or `"stomp"`), `firmware_version`, `models` (the catalog ids it can run), and `snapshots` (3 for HX Stomp), and `extras`: `control_route` (`usb` or `midi-cc` on the Stomp; `automation` or `state` on Native) and `param_steps` (distinct values a parameter can take: 128 per CC step on MIDI routes, finer when the controller's Min/Max is narrowed) | From the catalog import, never hard-coded. `search` reads `param_steps`. |

## Guarantees

- `render` after `load` and `set_params` is deterministic for Native and repeatable within measurement noise for the Stomp.
- `render` never returns NaN or silence for a non-silent input without raising `EngineError`.
- An engine leaves the device as it found it: the Stomp implementation restores the pedal's original preset on `close()`.
- `cost()` may be called at any time and is cached after the first measurement.

## Selection

`tonematch.engines.registry.detect()` returns the engines present. `TONEMATCH_ENGINE=native|stomp|auto` forces or auto-selects. Auto with both present means Native for search, Stomp for verification.

## Resolved at Gate 1 (cycle 2, 2026-10-14)

- **Native** cannot take a whole preset through `load`: its saved state is opaque, encrypted-looking preset records (see `docs/notes/native-spike.md`). `load` works by replaying a chain template captured once per chain structure in the GUI, then driving the 16 knob and 16 switch automation slots. A chain with no captured template, or a model change, raises `UnsupportedModelChange`; `search` groups trials by chain. Every Native render is a reset render with a 4,800-sample pre-roll; `flush` mode is opt-in and does not match reset renders.
- **Stomp** control: USB control on macOS is confirmed on hardware (firmware 3.50). Linux is expected, Windows is unconfirmed with MIDI CC the fallback. `load` and `set_params` touch only the pedal's edit buffer and never a flash slot. `load` may raise `UnsupportedModelChange` for a layout change it cannot make in place, as Native does. HX Edit must be quit during runs.
- `cost()` may return `load_penalty_s = NaN` when no model change has been measured; consumers must handle it.

## Still open

- Stomp control on Windows and Linux (hardware not available yet).
- A batch render method (`render_many`) for Path A/B parallelism, deferred until measured.
- Whether `UnsupportedModelChange` handling needs a capability flag; today `search` catches the exception.
