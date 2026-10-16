# HX Stomp speed ([T061](../../board/TASKS.md#t061 "Stomp speed improvements and assignment-free base"), stomp-engine, 2026-10-16, firmware 3.80)

Every number below was measured on the pedal (HX Stomp, fw 3.80, macOS, PortAudio `low`). Each run started and ended on 04C "Smoke Known", with the edit buffer byte-checked against the backup taken at open. Nothing was written to flash. Run folders: `cache/stomp/hw/<stamp>-speed-*/summary.json`. The script is `scripts/stomp_speed.py`. Ledger: `kind=render-cost`, `engine=stomp`, `dataset=stomp-speed`, from 2026-10-15T23:47 to 2026-10-16T00:18.

## Result: time per test

| Batch | Before | After | Speed-up | Ledger |
|---|---|---|---|---|
| Search, chain without delay/reverb (K), 10 amp-parameter trials, per trial | 10.86 s | 3.76 s | 2.9x | `search_batch_per_trial_s_*` (00:18) |
| Search, chain with a delay whose settings stay fixed (Cdly0), per trial | 15.42 s | 6.57 s | 2.3x | same (00:01) |
| Verification, 5 finalists, each whole-written plus 2 x 10 s renders, per finalist | 29.23 s | 24.65 s | 1.19x | `verify_batch_per_finalist_s_*` (00:10) |
| Session cost per test (open, restore, restore check) | 3.81 s per test | 0.77 s per batch | | `session_overhead_per_test_*` (23:47) |

- **Before** means: a 10 s clip, a full readback on every load, the safe upper-bound wait before each render, and for verification a session per finalist plus a separate restore-check session.
- **After** means: a 3 s search clip, a full readback on every 10th parameter-only load, decay-sized waits, one session, and the restore check in the same session.
- **Search is limited by clip length; verification by the 10 s renders.** The renders themselves cannot get shorter than the clip.

## Item 1: one session per batch

| | Seconds |
|---|---|
| Engine open (helper start and handshake, health `info`, `chain`, backup, audio device lookup) | 0.83-1.18 |
| Close (select 04C, compare, session-close) | 1.41-1.58 |
| Separate restore-check session (the old scripts' pattern) | 1.40-1.42 |
| **Per test, old pattern** | **3.81** (median of 3) |
| One session, 3 tests, restore byte-check in the same session (`StompEngine.close(verify_bytes=True)`) | **0.77 per test**; about 0.02 per test for a 100-test batch |

Code changes:
- `StompEngine.close(verify_bytes=True)` takes the post-restore backup and byte-compares it in the same session (`restore_report["bytes_equal_backup"]`).
- `parity_null.py` and `stomp_fw380.py` use it, which removes their second session.
- Callers keep one `StompEngine` per batch. That is already the pattern in `match_song.py` and `parity_null.py`.

## Item 2: clip length

| Clip (loudest excerpt of the 10 s DI) | 2 s | 3 s | 4 s | 10 s |
|---|---|---|---|---|
| Render wall time (median of 3, K) | 2.69 | 3.67 | 4.66 | 10.67 |

A render takes the clip length plus 0.67 s (pre-roll, latency and stream start).

API:
- `tonematch.reamp.excerpt(x, seconds)` returns the loudest window and its start sample. It is deterministic, so both engines get the same excerpt.
- `reamp.clip_for(x, "search" | "finalist")`.
- `SEARCH_CLIP_S` defaults to 3.0 and `FINALIST_CLIP_S` to 10.0. Override them with `TONEMATCH_STOMP_SEARCH_CLIP_S` and `TONEMATCH_STOMP_FINALIST_CLIP_S`.
- Wiring this into search is for `search`. It changes no contract.

## Item 3: decay-sized tail waits

`TONEMATCH_STOMP_TAIL=measured` (or `engine.tail_mode = "measured"`) works like this:
- The first render of each chain + delay/reverb setting records the tail for the upper bound.
- `measure_decay_s` finds where the tail stays below the higher of two levels: the clip's peak minus 60 dB (20 ms windows), or the idle floor plus 3 dB.
- Later renders wait that long plus 0.25 s, minus the time already spent since the previous render ended (loads, compiles).
- The default stays `bound`.

| Chain (amp Hum = Ripple = 0) | Upper-bound wait | Measured decay | Wait after | Null, measured vs bound | Bound repeats | Measured repeats |
|---|---|---|---|---|---|---|
| Cdly0: Digital Delay, Time 0.751, Feedback 0.3, Mix 0.6 | 4.81 s | 3.00 s | 3.25 s | -84.7 to -86.7 dB | -90.2 to -91.1 dB | -83.1 to -83.9 dB |
| Crev0: Room reverb at defaults | 4.00 s | 0.64 s | 0.89 s | -71.8 to -85.1 dB | -71.9 to -90.7 dB | -71.9 to -91.2 dB |

- The wait saves 1.6 s (delay) and 3.1 s (reverb) per render.
- Renders made after the shorter wait match renders made after the bound within the chain's own repeat noise, at -72 to -91 dB.
- **With Hum and Ripple at 0.5**, the free-running hum sets the floor: about -30 dB everywhere ([T056](../../board/TASKS.md#t056 "HX Stomp priorities (human, 2026-10-15)") item 3). The test is meaningless there, which is why the table uses Hum = Ripple = 0.
- **Limit:** the measurement is keyed by the chain and its delay/reverb values. A search that changes delay or reverb settings every trial gets a probe every trial, and so no saving. It helps amp, drive and EQ searches with a fixed delay or reverb, repeats, and multi-clip verification.

Also fixed: the wait now follows the previous render's tail, not only the current chain's. Before, after a chain with a long delay, a chain without one started its render at once.

## Item 5: in-place parameter changes

| Parameter-only load (6 amp params changed) | Seconds |
|---|---|
| Before: each `set` re-reads the whole preset in the helper, full readback every load | 0.356 |
| Helper `set` with the slot's model passed by the engine (no preset read), full readback every load | 0.196 |
| Same, full readback every 10th load (`TONEMATCH_STOMP_READBACK_EVERY=10`) | **0.120** |

- After every series, a forced full readback was OK.
- With a cadence above 1, the engine trusts its own copy of the chain and updates it from each `set` reply.
- Whole loads, model changes and the N-th load still read everything back.
- **Bug found and fixed on hardware:** the trusted copy was the open-time chain itself. Close then compared the restored preset with the edited one and fell back to writing the backup. Regression test: `tests/test_stomp_speed.py::test_trusted_loads_never_touch_the_original_chain`.
- Helper: `stompd` (sha256 f48396a2...) accepts `"model"` on `set`. The previous build is kept as `stompd.pre-T061`; older helpers ignore the key.

## Item 6: switching stored presets (read-only), and the flash-slot proposal

| Switch | Seconds |
|---|---|
| `select` (op 20) of stored presets 05A, 05B, 04B, until the pedal reports them current | 0.35-0.38 |
| MIDI Program Change until `info` reports the preset (6 of 6, 0.5 s apart) | 0.135-0.150 |
| Whole edit-buffer load (backup, compile, op-21 write, full readback) | 0.53 (write alone 0.29) |

A Program Change sent straight after the previous switch completed was ignored once (PC 13 never landed), so pace Program Changes.

### Proposal for the human (not done; it would write flash)

**What it would buy.** Storing generated presets in pedal slots and switching by Program Change replaces a 0.53 s whole load with about 0.15 s (plus a 0.04 s `chain` read to confirm). That saves about 0.35 s per preset change. A search trial is 3.8-6.6 s and a verification finalist is 24.7 s, so this saves 1-9% per test. Each slot also costs a flash write (tonepush's named slot write, op 8, paced by its `settle_flash`, seconds per write). **The saving only pays off when the same preset is loaded many times**, for example A/B listening sessions with a fixed set of finalists. It does not pay off for search, where every trial is a new preset. My recommendation: do not do it for search or benchmark.

If the human wants it for listening sessions, it would take:
1. **A full backup first, by the human:** HX Edit's Backup (all 126 presets, IRs and globals, `.hxb`) and a setlist export (`.hls`), kept outside the repo. Our tools would also read each target slot with op 4 (read a stored slot without loading it; read-only) and keep those documents for the restore.
2. **Slots:** only scratch slots the human names, for example a block of 4-8 empty or unused slots. Never any other slot.
3. **Write method:** a new helper command, `write_slot`. It is not in the client allowlist today, and the bridge would have to refuse every slot the human has not named. It would use op 8 (named write), wait for completion, then read the slot back with op 4 and byte-compare.
4. **Flash wear:** Line 6 does not publish the pedal's flash endurance. Assume it is limited, keep writes to a few per slot per session, and never write in a loop. Switching itself (PC or select) does not write flash.
5. **Restore:** write back the documents read in step 1 with the same command, then byte-compare. If anything fails, the human restores from the HX Edit backup. Then re-select the preset the pedal started on.
6. **Rules affected:** CLAUDE.md and the stomp-engine brief say "never write to the user's preset slots except a scratch slot the human names". This proposal stays within that, but it needs the human's explicit decision.

## Tests

`tests/test_stomp_speed.py` (9) covers:
- decay measurement on synthetic tails;
- excerpts and stage clips;
- the readback cadence: skipped chain reads, values kept, and the default of a readback on every load;
- cached routing;
- the measured-wait sequence with a fake stream;
- the same-session restore check;
- the original-chain regression.
