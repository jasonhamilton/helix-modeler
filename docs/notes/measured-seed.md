# Measured seeding before screening ([T113](../../board/TASKS.md#t113 "Gate 2 step, raise single-block model accuracy"))

Owner: `search`. Code: `src/tonematch/search/measured_seed.py`, `scripts/seed_calibrate.py`, flag `--seed-measured` in `scripts/synthetic_v1.py`. Tests: `tests/test_measured_seed.py` (fake engines). Known-DI mode only.

## Why

On dev16 (items i000-i015 of synthetic-v1) the model-choice stage ([T050](../../board/TASKS.md#t050 "Model choice per slot from the block library, fingerprint-informed…")) got the right category on 12 of 16 items and the right model on 6, with rank-1 of the score at 16 of 16. So the score was right and the misses were search misses. The true delay and reverb sat at screen rank 25 to 43 of 47: screening a delay at its catalog time, or a reverb at its catalog decay, is far from a target that has a different time or decay, whatever the model.

## Design

1. **Measure** (`measure(target, dry)`): the target against its DI, with the [T102](../../board/TASKS.md#t102 "Block-presence detector for the chain builder") / [T066](../../board/TASKS.md#t066 "Parity metrics for time-based effects") measures.
   - delay: echo time, feedback (second echo against the first, dB), echo level (dB re the direct sound);
   - reverb: RT60 (`presence` known-DI estimate), late-to-direct energy (dB), pre-delay (`metrics.reverb.predelay_ms`);
   - mod: LFO rate, amplitude depth (dB), pitch depth (cents);
   - the presence probabilities. A category is seeded when its probability is at least 0.10.
2. **Map each measurement to the knobs of each candidate model** by a per-model *calibration*, not by guessing units. `scripts/seed_calibrate.py` renders each model with its knobs on a 5-point grid across the template range (others at the library defaults) and measures the same quantities. One-knob groups are a line (Time, Predelay, Speed, Depth); two-knob groups are a 5 x 5 grid inverted jointly (Feedback with Mix; reverb Decay with Mix), because the measured decay and the measured echo level both move with Mix. The curves are inverted at run time: zero renders per item. The calibration DI is a Guitar-TECHS file that no synthetic-v1 item uses. 360 renders, 466 s, one host, once per library version, `cache/measured-seed/calibration.json` (local).
3. **Narrow band.** A seeded knob is searched in a band around its seed: +-4 % of the template range for time-like knobs (delay Time, Predelay, LFO Speed), +-12 % for level-like knobs (Feedback, Mix, Depth), +-20 % for reverb Decay (the RT60 estimate depends on the DI and the mix: seed error reached 0.14 of range on dev items). `BandedSpace` maps the CMA-ES unit cube onto the band and still snaps to the template's CC grid.
4. **Stages** (`choose_models_seeded`), same total budget as `choose_models` (`screen_frac` 0.5):
   - A: one render per pool model: its catalog defaults, or for a seeded model its seeds set (the seed replaces the default render);
   - B: second rung. Every model gets the same number of its remaining fingerprint settings (centre, then corners; seeded knobs held at the seed), as many as the screen budget (half of the total) allows. Leftover renders go to the top 12 by first score and the top 4 by seeded score;
   - C: successive halving over the best `beam` ([T050](../../board/TASKS.md#t050 "Model choice per slot from the block library, fingerprint-informed…")'s `halve`), seeded models refined inside their band.
5. **Pool order by corpus usage** (`reports/local/preset-corpus/model_usage.json`, local only): models are rendered in usage order, and ties in the ranking go to the more used model. A missing file leaves pool order.

## Limits seen

- Multi-voice delays: Harmony Delay measures the voice, not the Time knob, so its Time seed is wrong (0.57 s measured for Time 1.02 s).
- Models with no rate knob (70s Chorus) get no seed and keep the default-screen result.
- Reverb Decay is the weakest seed (0.14 of range off on both dev reverbs), hence the wide band. A model whose decay passes the measurable range (Plate above 0.7) is clipped at the edge of its calibration.
- Cab, comp and EQ misses are not addressed by seeding.

## Result (dev16, items i000-i015 only; dev, not gate evidence)

Native, admitted known-DI weights (`data/weights/gate2-known-di-mrstft_v2-2.0.json`), 300 search renders per item, 3 shards of 1 host. Runs in `cache/synthetic-v1/runs/`.

| | pool pass (v3_dev16_before) | model choice [T050](../../board/TASKS.md#t050 "Model choice per slot from the block library, fingerprint-informed…") (v3_dev16_after) | + measured seeds (v4b_dev16_seed) |
|---|---|---|---|
| model | 6/16 | 6/16 | **10/16** |
| category | 6/16 | 12/16 | **14/16** |
| param MAE (items with the right model) | 0.217 | 0.126 | 0.113 (0.146 vs 0.126 on the 6 items both got right) |
| rank-1 / audible | 14/16 / 16/16 | 14/16 / 16/16 | 14/16 / 16/16 |
| renders per item (incl. rank-1) | 375 | 369 | 353 |

By category: delay 1/2 (DL4 Dynamic found; Harmony Delay stays wrong), reverb 2/2 (true model was screen rank 28 and 28 before; 2 and 1 now), mod 2/2, drive 2/2, eq 1/2, amp 1/2, cab 1/2 (Cab4x10 vs Fullerton), comp 0/2.

First attempt, kept for the record (`v4_dev16_seed`): the second rung gave extra fingerprint settings only to the top 12 by default score. Model 8/16, category 10/16: drive and amp models whose default setting scores badly but whose corner setting scores well (Vital Dist: default 1.08, best setting 0.34) fell out. The rung is now a priority order for leftover renders; every model gets the same extra settings as in [T050](../../board/TASKS.md#t050 "Model choice per slot from the block library, fingerprint-informed…").

Seeds on the dev items: Time and Predelay within 0.005 of range, tremolo Speed within 0.002, Mix within 0.01 to 0.06, Feedback 0.05, reverb Decay 0.14 off (both reverbs). The measured MAE did not improve on the shared hits: the narrower bands help the time-like knobs and do nothing for the others, and the reverb Decay band (+-20 %) is looser than the seed.
