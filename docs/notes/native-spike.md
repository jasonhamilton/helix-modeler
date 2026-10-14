# T002 Native spike: can a whole preset be written into Helix Native's state?

Owner: native-engine. Status 2026-10-14: **not answered. Helix Native is not installed on the dev Mac.**
The harness is built and proven against stand-ins. The spike runs in one command once Native is present.

## Answer so far

| Question | Answer | Evidence |
|---|---|---|
| Is Helix Native installed on this Mac? | **No** | No `Helix Native.vst3` in `/Library/Audio/Plug-Ins/VST3`, `~/Library/Audio/Plug-Ins/VST3` or any AU/VST folder; `pkgutil --pkgs` has only `com.line6.application.hxedit.package`; Spotlight finds no Helix Native bundle. Only `/Applications/Line6/HX Edit.app` (3.51) is present. Ledger: `render-cost / native_available = 0` (2026-10-14T06:02:26Z). |
| Can pedalboard host a VST3 and read and write its state on this Mac? | Yes | pedalboard 0.9.26 on Python 3.12 (arm64) loaded `Reason Rack Plugin Effect` (VST3, 2567 params). `raw_state` read, decoded, re-encoded byte-exactly and written back. |
| What does the state wrapper look like? | JUCE `VC2!` + uint32 length + XML `<VST3PluginState><IComponent>…</IComponent><IEditController>…</IEditController>` + NUL; parts in JUCE MemoryBlock base64 (`<size>.<chars>`, JUCE alphabet, LSB-first) | Seen on the stand-in. Codec in `src/tonematch/engines/native_state.py`, byte-exact on the real blob. |
| Does writing a state back give byte-identical readback? | Only for IComponent | On the stand-in, IComponent came back identical; the plugin rewrote IEditController (116 bytes in, 58 out). So the round-trip test compares IComponent and the decoded preset document, not the whole blob. |
| Is preset JSON inside Native's IComponent? | **Unknown** (needs Native) | `scripts/native_spike.py` scans recursively for JSON, XML, zlib, gzip and base64 layers carrying `"tone"`, `"dsp0"`, `"@model"` or `HD2_`. Public sources do not document Native's chunk format. |
| 16 knobs + 16 switches from the host? | Documented by Line 6; untested here | [Native automation forum post](https://line6.com/support/topic/31714-unleash-the-power-of-helix-native-automation-in-your-tracks), [Stadium Native manual, automation panel](https://manuals.line6.com/helix-stadium-native/live/automation-controller-panel) (the original 3.8x Pilot's Guide is T009, research). |
| MIDI CC | pedalboard cannot send MIDI to an effect along with audio (its MIDI `process` overload is for instruments), so this route needs DawDreamer | pedalboard 0.9.26 `VST3Plugin.process` docstring. |

## What the harness proves today (no Native needed)

`tests/test_native.py` (10 tests, all pass) and a stand-in run of the full spike:

- **Process boundary**: the host runs as `python -m tonematch.engines.native_host`. That module is the only one that imports pedalboard. It uses a framed JSON+payload protocol, and plugin stdout goes to stderr.
- **Latency trim**: measured once per chain with an impulse. On a wet-only 10 ms delay it measured 479 samples, and the trimmed output nulls against the input.
- **Determinism**: two renders of the same input null at -316 dB (builtin Delay) and -323 dB (Reason Rack VST3).
- **Cost** is measured and cached in `cache/native/cost.json`: seconds per 2 s render, parallel throughput for k = 1, 2, 4, ... processes, and load penalty (state load plus polls until renders stop changing).
- **Batch render service**: `NativeRenderPool` groups jobs by chain across N host processes; its output matches the single engine.
- **State codec**: a synthetic Helix-like blob (zlib JSON behind length fields) round-trips: the document is found, mutated and written back, and the length fields are fixed up. No Line 6 data is used.

The stand-in timings (about 0.6 ms per 2 s render) are **not** Native's cost and are not ledgered as such.

## Run it once Native is installed (about 15 minutes, one human step)

```bash
pip install -e '.[dev,native]'            # pedalboard (GPLv3), host process only
export TONEMATCH_AGENT=native-engine
# 1. headless: hosting, parameter list, state dump and scan, automation probe, cost
python scripts/native_spike.py
# 2. with the golden template (T010): Native's window opens; File > Import the golden .hlx, close the window
python scripts/native_spike.py --gui --golden fixtures/golden/<native-stomp-export>.hlx --ledger
```

Everything lands in `cache/native/spike/` (gitignored, Line 6 data, never commit): `report.json` (the verdict and evidence), `params.json`, `state_*.bin`, `native_doc.json` (if found) and the renders.

What decides the verdict:

1. **State route = YES** if (a) the identity re-encode is accepted and reads back the same document, (b) a mutated parameter persists and moves the render, and (c) the golden `.hlx` written into the *default* state renders within -60 dB null of the GUI-imported golden. Then the spike writes `cache/native/state_codec.json {proven: true}` and `NativeEngine` switches to `route="state"`, where `load()` takes whole presets, model changes included.
2. **Otherwise, automation route**: in the GUI, build the fixed chain and assign the parameters being tuned to knobs 1–16 and switches 1–16, then capture it:
   `python scripts/native_spike.py --capture-template amp_cab --chain-json chain.json --map-json map.json`
   (`chain.json` is 8 × `[category, model_id|null]`; `map.json` is `{"<slot>:<param>": "<host param name from params.json>"}`). The report's `automation.effective` lists the host params that move the render. Proof means at least one knob and one switch move the render deterministically. `load()` then raises `UnsupportedModelChange` for chains with no template.
3. **Else MIDI CC** via DawDreamer, which is not built yet. Last resort is GUI-scripted preset import.

## Install notes (for `platform`, T007)

Native is the user's own licence. Never copy the bundle, its presets or its state dumps into the repo.

- **macOS** (Apple Silicon or Intel): install original **Helix Native 3.8x** from line6.com (not Helix Stadium Native; the engine refuses any path containing "Stadium"). Expected bundle: `/Library/Audio/Plug-Ins/VST3/Helix Native.vst3`. Open it once in any DAW, or via `--gui`, to sign in and authorise. Then `pip install -e '.[native]'`. Python 3.11+, arm64 wheels exist for pedalboard. Override the path with `TONEMATCH_NATIVE_PLUGIN=/path/Helix Native.vst3`.
- **Windows**: the installer puts the VST3 at `C:\Program Files\Common Files\VST3\Helix Native.vst3`. 64-bit Python 3.11+, `pip install -e .[native]`. Authorise in the plugin window on first run (`--gui`).
- **Linux (best effort, Wine + yabridge)**: install Native's Windows build under Wine (wine-staging 9+), then `yabridgectl add "$HOME/.wine/drive_c/Program Files/Common Files/VST3" && yabridgectl sync`. The bridged bundle appears at `~/.vst3/yabridge/Helix Native.vst3`, which is a searched path. The editor needs an X/Wayland display; authorisation under Wine is unverified. Don't block on it.
- **Host interpreter**: if pedalboard must live in a separate venv (for example, to keep GPL out of the main environment), set `TONEMATCH_NATIVE_PYTHON=/path/to/that/python`. The engine spawns its host with it.

## Observations for the lead

- HX Edit on this Mac is **3.51**, while T010 assumes Stomp firmware 3.80 and Native 3.82. A 3.51 catalog may lack 3.8x models; `capabilities().models` comes from the catalog.
- Design choices made in `native.py` that touch contract wording (proposed to the lead, contract unchanged):
  - "Level-matched to the engine's reference level" is implemented as a **fixed calibration gain** (`calibration_db`, default 0). Native's digital output is the reference. Per-clip normalisation would lift the noise floor and fail engine-smoke's silence-margin check.
  - Latency is measured **once per chain**, not once per engine, because blocks can add latency.
  - The state route needs a serialiser from `Preset` to the `.hlx` dict (`Preset.to_hlx_dict()` or equivalent) from `preset` (T006).
