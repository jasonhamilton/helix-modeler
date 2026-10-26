# Setup guide (for people)

Written for [T218](../board/TASKS.md#t218 "Setup guides for humans and agents") on 2026-10-23. This gets a fresh clone to the point where you can give it one song and one guitar and get back a Line 6 HX Stomp drive preset (a single distortion/overdrive block), plus a local page to listen on. The agent version of this guide is [SETUP-AGENTS.md](SETUP-AGENTS.md).

What the single-block song pipeline does, in order: you **calibrate your guitar** once (a ten-minute recording), it **separates** the guitar from the song, **rebuilds a dry guitar track** that plays the song's notes with your guitar's tone and level (a "synthetic" dry track, because the record's guitar is never clean), then **matches** the song's guitar tone by searching every drive model and its knobs on Helix Native with that dry track as the input. You listen and decide. Nothing is adopted until your ear says so.

Status, honestly: the pieces were measured on a handful of songs and the tone comparison is not yet validated ([project status](../reports/status/README.md)). The ear is the judge, and this guide does not claim the result is right, only that it runs.

## 1. What you must own and have

| You need | Why | If you do not have it |
|---|---|---|
| **A computer**: macOS (Apple Silicon or Intel), Windows, or Linux. Python 3.11 or newer. | Everything runs locally. | macOS is the tested path. See section 3 for the others. |
| **Helix Native** (Line 6, paid licence, installed and activated, the VST3 or AU) | It is the render engine of `song_match.py` today. | No Native means no render engine for this pipeline. The HX Stomp itself can render in real time, but `song_match.py` does not use it yet; the Stomp engine path is for verification and needs `stompd` (section 6). |
| **HX Edit** (Line 6, free, version 3.82 is the one tested) | The model catalog is read from your own install at runtime. It is also how you save and load presets. | Required. Nothing from Line 6 is shipped in this repo. |
| **One preset exported from HX Edit** into `fixtures/golden/HXStomp.hlx` | `make_smoke_hlx.py` builds the smoke-test preset from it. | In HX Edit pick any preset and use its export command. Local only, gitignored. |
| **HX Stomp** (optional) | Today the guitar calibration recorder uses the Stomp's dry input as the audio interface, and `--load-stomp` puts the result on the pedal. | Without it: record your calibration takes with any recorder and use `guitar_profile_build.py --free-wav` (a weaker "stand-in" profile, section 7), and import `best.hlx` in HX Edit or Native yourself. |
| **An electric guitar and a cable** | Calibration. | Needed for a personal result. Without calibration the pipeline uses the Guitar-TECHS library guitar's notes. |
| **A song file** you own (mp3, wav, flac) | The thing to match. | It stays on your machine under `data/songs/` (gitignored). |
| A GPU (optional) | Speeds up separation only. | CPU works. See the backend table in section 3. |

## 2. Disk and time

Estimates from this project's own runs unless marked.

| Item | Disk | Time |
|---|---|---|
| `.venv` (torch, demucs, librosa, ...) | about 3 to 5 GB on a fresh pip install (1.1 GB measured here with shared uv caches) | 5 to 15 min, network bound (estimate) |
| `cache/native/hostvenv` (pedalboard) | 60 MB | under a minute |
| `cache/venvs/basic-pitch` | 470 MB | 2 to 5 min (estimate) |
| Guitar-TECHS P1/P2 single notes and scales | 1.15 GB download, about the same on disk | download bound; DI index about 1 min |
| Guitar-TECHS P1/P2 techniques + chords (optional) | 2.85 GB | download bound |
| AFx-Rep checkpoint | 1.16 GB (plus 330 MB extracted encoder) | download bound |
| htdemucs_6s weights | 52 MB | seconds |
| Native single-block library (605 models) | 190 MB | about 6 min of Native time in the original run; not yet repeated on a clean machine |
| Per song: stems, rebuilt tracks, listening page | a few hundred MB | separation: 68 s of audio took 9 s on Apple MPS, a 4 minute song is about 20 to 40 s on MPS and several times that on CPU (planning estimate, not measured on your machine). The search has a 10 minute budget and the knob fine-tune 120 renders; the first song has not been timed end to end yet. |
| Calibration session | 100 to 200 MB | about 10 min of playing |

Total fresh install: roughly 10 to 15 GB with everything, and about 2 hours of mostly waiting for downloads.

## 3. Platform notes and the torch backend

| Machine | Torch install | What is detected (`TONEMATCH_DEVICE` overrides) |
|---|---|---|
| Apple Silicon | the default wheel (includes MPS) | `mps` |
| Intel Mac | default wheel | `cpu` |
| Windows or Linux with NVIDIA | install torch from the CUDA index at https://pytorch.org/get-started/locally/ **before** running the venv step | `cuda` |
| Linux with AMD | the ROCm wheel from the pytorch.org index | `rocm` |
| Windows without NVIDIA | default wheel (CPU). `onnxruntime-directml` gives a DirectML backend for ONNX tools only; demucs has no DirectML path | `cpu` (or `directml` for ONNX) |

Check what was found: `.venv/bin/python -c "from tonematch.gpu import detect_backend as d; print(d())"`. Separation falls back to CPU if an operation is missing on the accelerator; the ledger row records the backend actually used.

**macOS.** Reamping and calibration through the Stomp need Microphone permission for the app that runs Python (Terminal, iTerm, VS Code: System Settings > Privacy & Security > Microphone). Without it macOS hands the program digital silence and there is no error message. Quit HX Edit while the program talks to the Stomp.

**Windows.** Run `scripts/setup.sh` from Git Bash or WSL, or follow the same steps by hand (the commands are in the script). Helix Native, HX Edit and the Line 6 driver run natively. Not tested on Windows: the Native host, the template capture (`native_hc_once.py` and the hands-off capture are macOS-only today), `stompd`, and the Basic Pitch step (`transcribe_stems.py` expects `cache/venvs/basic-pitch/bin/python`; on Windows the venv has `Scripts/python.exe`). Expect to fix paths.

**Linux.** Untested for Native. Helix Native and HX Edit have no Linux build; the best-effort route is Wine plus yabridge ([DEV-ENV.md](DEV-ENV.md), "Wine + yabridge"). Stem separation, transcription and the dry rebuild do not need Native and should work. `apt install libsndfile1 libportaudio2 libasound2-dev` first.

## 4. Step by step

1. **Clone** the repo and `cd` into it.
2. **See what is missing**, changing nothing:

   ```bash
   scripts/setup.sh --check
   ```

3. **Run the setup.** It builds the three Python environments, then asks before each download, and says what is manual:

   ```bash
   scripts/setup.sh
   ```

   It asks you, one at a time, to type `yes` to: the Guitar-TECHS download (size and licence shown), the AFx-Rep download (size, source and the caveat below), the demucs weights, the Native window click, and the Native library capture. Anything you decline is skipped and shown as missing next time. `scripts/setup.sh --only venv,hostvenv,bp` builds environments only; `--skip-downloads` never downloads.

4. **Export one preset** from HX Edit to `fixtures/golden/HXStomp.hlx` (its preset export command) if you have not, then run `scripts/setup.sh --only smokehlx,hc,templates,smoke`. The `hc` step opens a Helix Native window once: tick "Don't ask me again", keep "Change the current HC mode to HX Stomp and load the preset", click OK, close the window. The `templates` step opens Native windows for a few minutes while it captures the block library; leave the keyboard alone.
5. **Load the environment variables** in each new terminal: `source cache/setup/env.sh` (sets `TONEMATCH_NATIVE_PYTHON`, and `TONEMATCH_STOMPD` if you have the helper).
6. **Check again**: `scripts/setup.sh --check` should show everything present. `engine_smoke.py` passing on your engine is the exit test:

   ```bash
   .venv/bin/python scripts/engine_smoke.py --no-ledger
   ```

What `setup.sh` runs for the local steps, in case you prefer to do it by hand: `catalog_import.py` (reads your HX Edit install into `cache/catalog/`), `make_smoke_hlx.py`, `native_hc_once.py`, `native_block_library.py capture --sessions 3 --max-hosts 6`, `engine_smoke.py`, `licence_check.py`, `proc_check.py`. All of them are described in [TOOLS.md](TOOLS.md).

**Optional: drive maps.** `cache/drive-maps/drive-families-v1.json` and `knob_sensitivity.json` improve the model shortlist for a drive match. Matching does not use them unless you pass `--families` to `match_block.py` (the families prior is opt-in, not an approved default). They come from a long Native job (`distortion_step3.py run`, then `drive_tone_families.py run`, `tone`, `families`); `scripts/setup.sh --only drivemaps` runs the chain after asking. Matching works without them (it falls back and says so). `drive_tone_families.py` writes them straight to `cache/drive-maps/`; an older copy under `data/` is still read, with a warning.

### The AFx-Rep caveat (read before you say yes)

AFx-Rep (ST-ITO, Apache-2.0 code) is the audio encoder the matcher uses to compare tones. The checkpoint's only licence text is the uploader's "apache-2.0" tag. It was trained on datasets several of which carry non-commercial or research-only terms (ENST-Drums, MTG-Jamendo, parts of FSD50K, possibly MUSDB18), and whether those terms reach the weights is unsettled. This project downloads it to your machine for local use, pinned to one revision and sha256-checked, and never redistributes it. Do not publish the file or put it in a commercial product. Full trail: [notes/afxrep-licence.md](notes/afxrep-licence.md). Decline the download if that is not acceptable to you; the tools that need it will say so.

## 5. Your first song

Use your own guitar and any song file. The commands assume the guitar is called `strat-main` (any name) and the song is `data/songs/My Song.mp3`.

### 5a. Calibrate your guitar (once per guitar and pickup/knob setting)

The pipeline rebuilds the song's guitar from real notes. Calibration measures your guitar's level and tone so the rebuilt track sounds like your instrument, not the library guitar's.

1. Guitar cable into the HX Stomp's main input, Stomp on USB, HX Edit closed. The preset's Input block must be the guitar input (Main L/R or Multi), not USB 5/6. Leave the pickup selector and the tone and volume knobs where you normally play, and keep them the same for the whole session.
2. Level check (3 s; play normally, then hard). Aim for a hard-pick peak between -12 and -3 dBFS:

   ```bash
   .venv/bin/python scripts/guitar_profile_record.py --guitar-id strat-main --check
   ```

3. The guided session, about ten minutes. It asks for Enter before each segment, counts down, records and shows the peak (redo on a warning): open strings, every fret on each string, chords and power chords, soft/medium/hard picking, palm mutes, and 30 s of free playing in your style. `--list` shows the script without recording.

   ```bash
   .venv/bin/python scripts/guitar_profile_record.py --guitar-id strat-main
   ```

4. Build the profile:

   ```bash
   .venv/bin/python scripts/guitar_profile_build.py --guitar-id strat-main
   ```

   Result: `data/guitars/strat-main/profile.json` (level, tone correction, pitch coverage) and `bank.json` (your labelled notes). Local only, gitignored. Keep the session folder; the rebuild reads the note files from it. Method and what the profile does not do: [notes/guitar-profile-t204.md](notes/guitar-profile-t204.md).

No Stomp? Record yourself with any interface (a few minutes of dry playing, mono wav, no effects), then `guitar_profile_build.py --guitar-id strat-main --free-wav my_take1.wav my_take2.wav`. That is a stand-in profile: free playing only, so no soft/hard range, no string labels and no palm mutes, and a noisier tone fit.

### 5b. Add a song

Copy the file into `data/songs/` (gitignored; the song never leaves your machine). Nothing else to do: `song_match.py` separates it first if needed.

### 5c. Run the match

See the plan first, which renders nothing:

```bash
.venv/bin/python scripts/song_match.py --song "data/songs/My Song.mp3" --guitar-id strat-main --dry-run
```

Then run it. Pick the 10 s window yourself where the guitar is exposed and distorted (`--window 28.3:38.3`, song seconds), or leave it out and the loudest-guitar picker chooses one:

```bash
source cache/setup/env.sh
nice -n 10 .venv/bin/python scripts/song_match.py --song "data/songs/My Song.mp3" --guitar-id strat-main --window 28.3:38.3
```

It runs these steps and skips any whose inputs are unchanged, so re-running is cheap: separate, window, transcribe (Basic Pitch), rebuild the dry track, level, search (drive category, AFx-Rep shortlist, Native), knob fine-tune (only with `--finetune`), package. Useful options:

| Option | Meaning |
|---|---|
| `--finetune` | also run the knob fine-tune (opt-in; Output/Master stay fixed). Without it `best.hlx` is the search winner |
| `--level-db N` | loud-frame level of the dry track (default: your profile's level, else -28.08 dBFS) |
| `--chords "B5,G5,A5"` | tell it the song's chords (power chords X5, major X, minor Xm); off by default, for songs you know |
| `--force` | re-run every step |
| `--load-stomp` | also put `best.hlx` in the Stomp's edit buffer (needs `stompd`, section 6) |

While it runs the machine will be busy: it uses up to three Native render workers, one core each, and pauses itself when the computer is too loaded. Do not start other heavy jobs. If you interrupt it, run `.venv/bin/python scripts/proc_check.py --kill` to stop orphaned workers.

### 5d. Listen

Open `cache/song-match/<song-id>-strat-main/index.html` in a browser (local file, no server). It has the separated song guitar, the match, the match before fine-tuning, and the dry track the match plays, loudness-matched so the comparison is fair. `summary.md` lists the chosen model, the knob values and the AFx-Rep distance. That distance ranks candidates against each other; it is not a fidelity score. Your ear decides.

### 5e. Get the preset onto your gear

`cache/song-match/<song-id>-strat-main/best.hlx` is a validated Line 6 `.hlx` preset for the HX Stomp. Two ways:

- **HX Edit or Helix Native:** import `best.hlx` with HX Edit's import command, save it to a slot on the pedal from HX Edit. This works with no `stompd`.
- **From code:** `--load-stomp` loads it into the Stomp's edit buffer only (never flash). It is not saved; switch presets and it is gone, so save it on the pedal if you like it. Needs `stompd`.

## 6. stompd: the HX Stomp control helper (optional, not in this repo)

`stompd` is our own small Rust program (about 2,600 lines) that holds one USB session with the HX Stomp's control interface and speaks JSON lines on stdin and stdout. It links the MIT-licensed crates of [tonepush](https://github.com/crmne/tonepush) (pinned at rev `53a6a65b543693913311daff4941a6138327faa8`) and is intended to be MIT too. By the human's decision of 2026-10-14 its **source is not in this repo**; it is kept locally in `cache/stomp/stompd-src/` and the binary at `cache/stomp/bin/stompd`. The protocol it speaks is fully documented in [notes/stompd.md](notes/stompd.md), including the command allowlist, session end handling and how to build your own to that protocol.

**What works without it:** everything in this guide. Separation, transcription, the dry rebuild, the Native search, the listening page, `best.hlx`, importing the preset through HX Edit, and the guitar calibration recorder (that one uses the Stomp's USB audio through PortAudio, not `stompd`).

**What needs it:** loading a preset onto the Stomp from code (`song_match.py --load-stomp`), and the Stomp render engine (`TONEMATCH_ENGINE=stomp`, parity and verification runs). Others cannot reproduce the Stomp results until the wrapper is published or they write their own to the protocol; the Python side only depends on the protocol (`tonematch.engines.stomp_bridge`).

If you have a binary: `export TONEMATCH_STOMPD=/absolute/path/to/stompd` (and `TONEMATCH_STOMPD_COMPILE` for the offline compile build). Quit HX Edit first; it holds the interface. If the pedal stops answering, pull its 9 V adapter for a few seconds.

## 7. The manual steps behind the one command

For when you want one stage alone, or `song_match.py` stops and you need to see why. All of them are in [TOOLS.md](TOOLS.md).

| Stage | Command |
|---|---|
| Stems | `.venv/bin/python scripts/separate.py "data/songs/My Song.mp3"` |
| Transcribe the guitar stem | `.venv/bin/python scripts/transcribe_stems.py run --songs my-song` |
| Rebuild the dry track | `.venv/bin/python scripts/rebuild_dry.py --song-id my-song --profile strat-main` |
| Play along yourself (needs the Stomp) | `.venv/bin/python scripts/playalong_record.py --song-id my-song --takes 3`, then `align_takes.py --song-id my-song <takes>` |
| Match one block | `scripts/match_block.py` (see TOOLS.md, section 4) |

## 8. Licences, and what never leaves the machine

- **Never leaves your machine:** your songs, the stems, the rebuilt dry tracks, your guitar recordings and profile, the listening pages, and everything under `cache/` and `data/`. No hosted separation API is used. The only network traffic is the downloads you agreed to in setup.
- **No Line 6 files in the repo.** The model catalog, Native templates, the block library, your exported presets and `known.hlx` are produced on your machine from your own installs and stay in gitignored folders. The scripts never download or copy a Line 6 asset.
- **This project:** MIT. **Third party, kept apart:** `pedalboard` is GPLv3, so it lives only in `cache/native/hostvenv` and runs as a child process; Basic Pitch (Apache-2.0) is an external tool in its own venv; Guitar-TECHS is CC BY 4.0 (the licence text and the attribution line are written to `data/di/LICENCE-Guitar-TECHS.txt`; keep them with any clip you share); AFx-Rep, see the caveat above; htdemucs_6s weights are stated MIT upstream. Several transitive dependencies are LGPL, MPL or non-commercial (`soxr`, `lameenc`, `certifi`, `tqdm`, `diffq`) and are reviewed in [licence-allowlist.json](licence-allowlist.json): fine for private local use, to be re-reviewed before any bundled release. `scripts/licence_check.py` re-checks after any dependency change.
- **Helix Native renders:** what you may do with audio rendered through Native is governed by Line 6's EULA ([notes/line6-eula-flags.md](notes/line6-eula-flags.md)); local use for matching is the intended use.

## 9. Known gaps

- A from-scratch Native library capture and the whole of `setup.sh` have not been run on a clean machine, only checked against this one.
- `scripts/doctor.py` (one report of Python, torch backend, engines, HX Edit and driver state) does not exist yet; `scripts/setup.sh --check` is the nearest thing.
- Windows and Linux are untested for Native, `stompd` and the recorder.
