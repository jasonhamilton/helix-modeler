---
name: song-match
description: Run the single drive-block matcher end to end for one song and one guitar: separate, pick the window, transcribe, rebuild the dry track from the guitar profile, render all drive models from it on Native and rank by AFx-Rep (shortlist 8, AFx-Rep final order; this is the T207 full search, NOT the T196 predictor-first path), optionally (only with `--finetune`) fine-tune the knobs, then hand over a validated best.hlx and a local listening page. Its method defaults are pinned to `data/approved-methods.json` (human's 2026-10-25 bundle approval, interim/provisional; `tests/test_approved_methods.py`). Use when the human asks to match a song's guitar tone with a single drive block, or to repeat that run on another song or guitar.
---

# song-match

This is a skill and not an agent because the work is one fixed procedure with fixed inputs and outputs: every step already has an owner and a script, and nothing in it needs judgement beyond what the human decides. An agent would only add a hand-off. Run it as whoever was asked; the lead or `search` normally does.

Script: `scripts/song_match.py` (T217). Library: `src/tonematch/songs.py` (song id or file to stems, rebuilt-dry, match folders and windows). It chains methods already in use (T200 window picker, T203 Basic Pitch, T204 guitar profile, T207 search, T209 fine-tune, T211 run); it adds none and changes no default method. Fine-tuning is an option and is off unless `--finetune` is given.

## When to use

The human names a song (a file in `data/songs/` or an id already separated) and asks for the guitar tone as a drive block, typically with their calibrated guitar (`--guitar-id`). Not for amp, cab or effects chains (`match_song.py` and the later slot work), not for a no-DI blind run.

## Preconditions

1. Native is installed (the search renders on it); `TONEMATCH_ENGINE` is left alone. Without Native the search step fails and says so.
2. `.venv/bin/python scripts/proc_check.py` reports no orphans. Run it before and after; `--kill` only stops what it flags. Never `pkill` by pattern.
3. CPU: the script waits on the busy-core `LoadGuard` before the search and the fine-tune, and uses at most one Native host per step (the shortlist path runs no shards). Another job on Native (a training pilot, a render batch) is fine; check `uptime` and the host count first, and keep your own hosts at 3 or fewer in total.
4. Guitar profile: `data/guitars/<id>/profile.json` exists (`scripts/guitar_profile_build.py`). Without `--guitar-id` the rebuilt dry track uses library bank notes at -28.08 dBFS; say so in the report.
5. The Basic Pitch venv is `cache/venvs/basic-pitch` (only needed when the song has no `transcription.mid` yet).
6. A new song file is separated first (`scripts/separate.py`, a cache hit when done before).

## Commands

Plan first (runs nothing, shows which steps are already done):

```
.venv/bin/python scripts/song_match.py --song mother --guitar-id prs-7-tone --window 28.3:38.3 --dry-run
```

Run (resumable; rerun the same line to continue after a stop):

```
TONEMATCH_AGENT=search .venv/bin/python scripts/song_match.py --song mother --guitar-id prs-7-tone --window 28.3:38.3 --result-json cache/song-match/mother-result.json
```

- `--song` a file, an id of a separated song, or a stems hash. No `--window`: the T200 loudest-guitar picker chooses 10 s.
- `--level-db X` overrides the dry-track level (default: the profile's own level, or -28.08 without a profile). `--chords "B5,G5"` adds the chord-constrained rebuild (off by default). `--finetune` adds the knob fine-tune by AFx-Rep distance: an option, off by default (human 2026-10-25); Output/Master are always locked when it runs. Without it `best.hlx` is the search winner. Do not turn it on unasked.
- `--force` re-runs every step; `--out DIR` changes the run folder (default `cache/song-match/<id>-<guitar|plain>/`).
- Changing `--window`, `--level-db`, the guitar or the profile re-runs only the steps after the change.

Run time on this machine: about 10 to 15 minutes for the search plus the fine-tune (the search alone is shorter); the rebuild takes seconds to a minute.

## Outputs (in the run folder)

`best.hlx` (validated; `best.validate.md`), `index.html` (listening page), `listen/` (left = separated song guitar, right = match; the match before the fine-tune when `--finetune` ran; the dry track), `summary.json` and `summary.md` (model, settings, AFx-Rep distance before and after, search top five, renders, wall-clock, engine), `logs/`, `search/`, `finetune/`, `dry/`.

## Presenting the result

Give the human: the chosen model and settings, the distance numbers with what they mean (lower is closer in AFx-Rep embedding space; it is not a listening score), the search runner-ups, the guitar and level used, and the path of `index.html` to open locally. Say which steps were reused. Then stop: **the ear is the final judge**; do not score the listening yourself and do not call a match good or bad from the distance alone.

To hear it on the pedal: `--load-stomp` (or `scripts/stomp_live_preview.py --dir <run>/stomp-load --once --no-restore-on-exit`) puts `best.hlx` into the Stomp edit buffer, never a flash write. It works only when the `stompd` helper is present (`cache/stomp/bin/stompd` or `TONEMATCH_STOMPD`); it is optional and not in the repo. Hardware time is scarce: batch it and leave the pedal on its original preset afterwards. The pedal must be on the scratch preset `04C` (the preview script checks).

## Never

Publish or upload song audio, stems, the listening page or the run folder (all of `cache/` and `data/songs/` are gitignored; keep it so). Change a method, default, score, weight or pass bar on your own: any of those is a proposal to the human with evidence, recorded in `docs/DECISIONS.md` only after they approve. Edit the dry-signal scripts (`rebuild_dry.py`, `transcribe_stems.py`, `guitar_profile_*`) from here; ask `dry-signal`.

## Record

Ledger rows only when you also measure something against a benchmark; a plain run writes none. Update `board/TASKS.md` when a run is a task. A result worth keeping goes in `docs/notes/` with the run folder named.
