# Dev environment notes

Python 3.11+. Audio is 48 kHz float32 throughout. Setup: `python -m venv .venv`, activate, `pip install -e .[dev]`, `pytest`.
Backend check: `python -c "from tonematch.gpu import detect_backend as d; print(d())"`. Override with `TONEMATCH_DEVICE=cpu|cuda|rocm|mps|directml`.

## Windows
- Python from python.org or winget; PowerShell activate: `.venv\Scripts\Activate.ps1`.
- NVIDIA: install torch from the CUDA index (https://pytorch.org/get-started/locally/). No NVIDIA/AMD CUDA: `onnxruntime-directml` gives the DirectML backend; otherwise CPU.
- Native (VST3) and HX Edit run natively. Line 6 driver needed for the Stomp USB audio.

## macOS
- Python via Homebrew or python.org. Default torch wheel includes MPS on Apple Silicon.
- Native runs as AU/VST3; HX Edit runs natively. Intel Macs: CPU only.
- `soundfile` needs libsndfile (bundled in recent wheels; `brew install libsndfile` if missing).
- **Stomp reamping needs Microphone permission for the app that runs Python** (Terminal, iTerm, VS Code: System Settings > Privacy & Security > Microphone). Without it, macOS hands the process digital silence on the HX Stomp's inputs. There is no error, so the engine reports "the Stomp returned silence for a non-silent input" ([T018s](../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp"), 2026-10-14). In an agent's sandboxed shell the stream does not start at all (PortAudio -9986). USB control (`stompd`) does not need this permission.

## Linux
- `apt install libsndfile1 libportaudio2 libasound2-dev` (or distro equivalent).
- NVIDIA: CUDA torch wheel. AMD: ROCm torch wheel (index per pytorch.org; detected as `rocm`). Else CPU.
- Pedal control: see "Pedal control and versions" below. MIDI CC works everywhere.
- Helix Native and HX Edit have no Linux build, see below.

### Wine + yabridge for Native (best effort)
1. Install Wine (staging recommended) and a Windows Helix Native VST3 into a wine prefix, using your own licence and installer. Nothing from Line 6 enters this repo.
2. Install yabridge and yabridgectl; `yabridgectl add "$WINEPREFIX/drive_c/Program Files/Common Files/VST3"`, then `yabridgectl sync`.
3. The bridged VST3 appears under `~/.vst3/yabridge/`; point the native engine at that path.
4. Expect: higher latency, possible UI/licence-activation quirks, no guarantees. Offline rendering is the supported path; verify with `engine-smoke`.

## Pedal control and versions
- Pedal control: on macOS this was confirmed on hardware on 2026-10-14 (firmware 3.50; `docs/notes/T003-stomp-control-spike.md`). It goes through USB interface 0 via the `stompd` helper, a small original wrapper over tonepush's crates. The helper is built outside this repo and found through `TONEMATCH_STOMPD`; for what it is, how to build it and the reproducibility limitation, see `docs/notes/stompd.md`. Linux is proposed, not yet run. On Windows, MIDI CC is the likely route. MIDI CC is the fallback everywhere. Install the extras with `pip install -e .[stomp]` (and `.[spike]` for the probe script's MIDI).
- Quit HX Edit during Stomp runs, since it competes for the device.
- Target versions: HX Edit 3.82, Helix Native 3.8x, Stomp firmware 3.80. The pedal on this Mac reports firmware 3.50 (read over USB on 2026-10-14).
- python-rtmidi on Python 3.14 needs JACK disabled when building: `pip install python-rtmidi --config-settings=setup-args="-Djack=false"`.
