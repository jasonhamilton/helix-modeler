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

- **Native** cannot take a whole preset through `load`: its saved state is opaque, encrypted-looking preset records (see `docs/notes/native-spike.md`). `load` works by replaying a chain template captured once per chain structure in the GUI, then driving the 16 knob and 16 switch automation slots. A chain with no captured template, or a model change, raises `UnsupportedModelChange`; `search` groups trials by chain. Every Native render is a reset render (the host's state reset clears every tail, measured 2026-10-16) with a pre-roll of 4,800 samples, or 3 s for chains with a delay, reverb or "other" block (`TAIL_PREROLL`; renders compare only at equal pre-roll because it shifts LFO phase); `flush` mode is opt-in and does not match reset renders.
- **Stomp** control: USB control on macOS is confirmed on hardware (firmware 3.50). Linux is expected, Windows is unconfirmed with MIDI CC the fallback. `load` and `set_params` touch only the pedal's edit buffer and never a flash slot. `load` may raise `UnsupportedModelChange` for a layout change it cannot make in place, as Native does. HX Edit must be quit during runs.
- `cost()` may return `load_penalty_s = NaN` when no model change has been measured; consumers must handle it.

## Amendment 2026-10-16: optional `render_many` (lead-approved, additive)

```python
def render_many(self, dry: Audio, variants: list[Preset | dict[ParamRef, float]]) -> list[Audio]: ...
```

- Optional. An engine without it is served by `tonematch.engines.base.render_many(engine, dry, variants)`, the serial fallback (for each variant: `load` if it is a `Preset`, else `set_params` relative to the currently loaded preset; then `render`). Callers always go through that helper, never `getattr`.
- Results are in `variants` order and must equal, within the engine's repeat floor, what the serial fallback returns. An `UnsupportedModelChange` for one variant is raised, not skipped.
- After the call the engine's loaded state is unspecified; callers `load` again before relying on it.
- Native: the render pool (hosts capped by `max_hosts_by_memory`). Stomp: two variants per pedal pass on Path A and Path B where both fit the DSP and are single-path chains (two-path rendering equals single-path on fw 3.80, ledger twopath-parity-fw380); otherwise serial.
- Affects: search, benchmark, learning (may adopt it), native-engine and stomp-engine (implement it). No existing method changes. Measured basis: Native pool 10.0 tests/s at 6 hosts (2 s clip; ledger 2026-10-16T00:34:16, renders_per_s 10.025); Stomp two-path Path A -72.9 dB vs a -68.7 dB repeat floor and Path B -52.6 dB vs -42.7 dB (ledger twopath-parity-fw380, 2026-10-16T15:44:03).

## Still open

- Stomp control on Windows and Linux (hardware not available yet).
- Whether `UnsupportedModelChange` handling needs a capability flag; today `search` catches the exception.
