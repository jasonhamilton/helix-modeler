# Engine interface, v0

Owner: lead. Implemented by `native-engine` and `stomp-engine`. Consumed by `search`, `benchmark`, `learning`. The code form is `src/tonematch/engines/base.py`; this file is the spec.

Every engine is a class implementing `tonematch.engines.base.Engine`. Steps 1 to 3 and 5 to 7 of the pipeline never know which one is running.

## Methods

| Method | Does | Notes |
|---|---|---|
| `load(preset: Preset) -> None` | Puts a whole candidate on the engine | A `Preset` is the in-memory form of an `.hlx` (see `preset-schema.md`). Model changes happen here, not in `set_params`. |
| `set_params(changes: dict[ParamRef, float]) -> None` | Changes parameter values on the loaded preset | `ParamRef` is `(slot, param_name)`. Values are the catalog's normalized 0..1 range. Must be cheaper than `load`. |
| `render(dry: Audio) -> Audio` | Returns the processed audio for a dry clip | 48 kHz float32 mono in, mono out, same length. Level-matched to the engine's reference level and trimmed by its measured latency. |
| `cost() -> Cost` | Reports measured `seconds_per_render` for a 2 s clip, `parallelism` (how many renders can run at once), and `load_penalty_s` (extra settling after a model change) | Measured, not guessed. The optimizer picks its strategy and budget from this. |
| `capabilities() -> Capabilities` | Reports `engine` (`"native"` or `"stomp"`), `firmware_version`, `models` (the catalog ids it can run), and `snapshots` (3 for HX Stomp) | From the catalog import, never hard-coded. |

## Guarantees

- `render` after `load` and `set_params` is deterministic for Native and repeatable within measurement noise for the Stomp.
- `render` never returns NaN or silence for a non-silent input without raising `EngineError`.
- An engine leaves the device as it found it: the Stomp implementation restores the pedal's original preset on `close()`.
- `cost()` may be called at any time and is cached after the first measurement.

## Selection

`tonematch.engines.registry.detect()` returns the engines present. `TONEMATCH_ENGINE=native|stomp|auto` forces or auto-selects. Auto with both present means Native for search, Stomp for verification.

## Open until Gate 1

- Whether Native can take a whole preset through `load` (state blob) or only through 16+16 automation slots per fixed chain. If the latter, `load` is allowed to raise `UnsupportedModelChange` and `search` must group trials by chain.
- Whether Stomp mode on Windows and macOS uses the fretwire port or the MIDI CC fallback (128-step values; model changes need a template reload, reported through `load_penalty_s`).
