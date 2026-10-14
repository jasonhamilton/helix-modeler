# Synthetic recovery set v1: design, cost, capture request, proposed thresholds (T024)

Owner: `benchmark`. Status: design plus offline code; nothing rendered, no benchmark ledger rows. Code: `scripts/synthetic_benchmark.py`, tests `tests/test_synthetic_benchmark.py`, the real manifest and `labels.json` (180 labelled items, no audio) are written by `--generate` to gitignored `cache/synthetic/` because they carry real Line 6 model ids (`licence_check` fails them under `fixtures/`). The committed `fixtures/synthetic/manifest.example.json` (`--example`) has the same shape with `EXAMPLE_nn` ids. The model ids remain as identifiers in `scripts/synthetic_benchmark.py` (not a data file; reviewer to confirm that is acceptable). Rendered audio stays local.

## Constraint and the honest consequence

Native renders only a chain with a GUI-captured template, one per chain structure, and varies only 16 knobs and 16 switches. The Stomp renders any model or parameter change in its loaded layout at about 2.7 s per render (+0.66 s per model change), one at a time. So v1 is two sub-sets, reported separately (same `dataset=synthetic-v1`, different `engine` column; the regression check keys on kind, dataset, engine and metric):

| Sub-set | Engine | Varies | What it can honestly measure | What it cannot |
|---|---|---|---|---|
| v1-native | Native | 3 fixed chain structures x 40 items: 6 bypass switches (block-type), 16 knobs | block F1 (bypass detection), parameter MAE, rank-1 of the loss, structure choice | Model accuracy beyond "which of 3 structures"; models inside a structure are fixed. Reported as `model_acc` but chance is 1/3, so it is a weak signal. |
| v1-stomp | Stomp | 60 items in structure A's 8-block layout: a random model per slot from a pool of 6 (reverb 3), 6 switches, the same 16 knobs | real model accuracy (chance about 1/6), block F1, parameter MAE | Speed: attended, serial, hardware time. |

Rules of generation (all in the manifest, seeded `20261014`): switch-on probabilities comp .30, drive .50, eq .40, mod .35, delay .60, reverb .70 (placeholders to be refit on community presets by `dry-signal`/`catalog` work); amp and cab always on; knob values Beta(2,2), so they sit mid-range like real presets; knob split comp 1, drive 2, amp 5, eq 2, mod 2, delay 2, reverb 2 = 16; amp+cab move as a pair on the Stomp (cab follows the amp's `cablink`, no independent cab variation in v1). Each item has two DI clips (fit, and held-out notes) from the `dry-signal` library (clip ids to be filled when T021 lands). Mixing with stems is offline (no engine cost): v1 default is `d0` (solo guitar, oracle separation); the stress set `stress-v1` reuses the same truth renders at `d1..d3` (guitar +6, 0, -6 dB against the stems), which needs the human's chosen multitrack stems and `separate` (T020). Truth renders are cached, so stress costs no extra engine time.

## Cost (measured rates: Native 1.883 s/render, Stomp 2.682 s/render, 0.658 s per model change; ledger 2026-10-14)

Printed by `python scripts/synthetic_benchmark.py --plan --budget 150`. Assumes `parallelism=1` (not yet measured; `cost()` will report it).

| Step | Renders | Wall-clock |
|---|---|---|
| Native truth (120 items x 2 clips) | 240 | 7.5 min, one-off |
| Native loss-trust neighbours (24 per item, fit clip) | 2,880 | 1.5 h, one-off per structure set |
| Native CI run (6 items x budget 40) | 240 | 7.5 min per change |
| Native full recovery (120 items x budget 150) | 18,000 | 9.4 h, nightly/overnight, not per change |
| Stomp truth (60 items x 2 clips, about 2.5 model changes per item) | 120 | about 10 min, attended batch |
| Stomp recovery, 10-item subset x budget 150 (each render about 4 s with model changes) | 1,500 | about 1.7 h; the full 60 items is about 10 h |

Consequences: "every change (CI)" in the plan can only be the 6-item Native smoke recovery plus the label/metric unit tests (which run in under a second); the full Native pass is nightly; Stomp recovery is milestone-only. If the plan wants per-change numbers on a bigger set, the lever is `parallelism` (several Native instances) or a cheaper reset render, both for `native-engine`. The loss-trust check dominates one-off cost, but it is run once per structure set, not per change.

## Request to the human: Native chain captures (one GUI session, 3 chains)

Each capture is the usual one-time GUI step (Open From Disk / build, save the chain template, assign 16 knobs and 16 switches as in `docs/notes/native-spike.md`). Each chain is full: 8 blocks, amp and cab fixed on, six blocks on a bypass switch. Model ids are from the 3.82 catalog (verify names on screen):

| Structure | comp | drive | amp (+ its default cab) | eq | mod | delay | reverb |
|---|---|---|---|---|---|---|---|
| A_clean | Deluxe Comp | Kinky Boost | US Double Vib | Parametric | Chorus | Transistor Tape | Plate |
| B_crunch | Red Squeeze | Scream 808 | Brit 2204 | Simple 3 Band | Script Mod Phase | Simple Delay | Room |
| C_highgain | LA Studio Comp | Horizon Drive | Revv Gen Red | Parametric | Optical Trem | Ducked Delay | Hall |

Knob assignment (16, same in all three): comp 1 (main level/amount), drive 2 (Drive, Level), amp 5 (Drive, Bass, Mid, Treble, Ch Vol), eq 2, mod 2 (rate/speed, depth/mix), delay 2 (time, mix), reverb 2 (decay/time, mix). Switches: bypass of comp, drive, eq, mod, delay, reverb (six of the 16). This is the minimum that gives three amp voicings and distinct models in every slot; three captures is the whole request. If the human wants model accuracy on Native beyond 3-way classification, each extra structure adds one more capture; I do not recommend more for v1 because the Stomp sub-set covers that axis.

## Proposed thresholds for Gate 2 (PROPOSED, not decided; lead records in `docs/DECISIONS.md`)

Evaluated at density `d0` (solo, oracle stem) for the recovery search; the stress curve (`d1..d3`) is recorded, not gated, except that `d1` should stay within the tolerance band below.

| Metric | Proposed Gate 2 threshold | Chance / trivial baseline | Reasoning |
|---|---|---|---|
| `block_f1` (micro, six switchable categories) | >= 0.85 at d0; >= 0.75 at d1 | 0.64 for "everything on" (computed from the priors; test checks it) | Clearly above the trivial predictor, and low enough to forgive the hard cases (a quiet reverb or compressor is near-inaudible; the metric counts it as a miss). |
| `model_acc`, Stomp sub-set, enabled slots | >= 0.60 | about 0.17 (pool of 6) | The search must pick the right amp/drive/etc. more often than not, but sibling models (two similar overdrives) are legitimately confusable; confusion matrix to be reported. |
| `model_acc`, Native sub-set | >= 0.80 (structure choice) | 0.33 | Weak signal by construction; reported, not weighted in the gate beyond this floor. |
| `param_mae` (normalized, enabled blocks with the right model) | <= 0.15 | 0.19 for "always 0.5" (3/16 under Beta(2,2); tested) | A mid-guess already scores 0.19, so 0.15 demands real information; parameters with little audible effect (Sag, Hum, Ripple excluded from the 16 knobs for this reason) would otherwise inflate it. Median reported alongside. |
| `rank1_rate`, whole loss kit (loss-trust-check) | >= 0.95 | n/a | Gate 2 item 1: the true preset beats all 24 neighbours (single-parameter +/-0.15 nudges and single-switch flips). Ties do not count. |
| `rank1_rate`, per term for admission to default weights | >= 0.90 | n/a | Matches the loss-kit admission rule; terms below stay at weight 0. |
| `loss_heldout` gap | recovered preset's loss on held-out notes <= 1.25 x its loss on the fit clip | 1.0 | Catches overfitting to the played notes; defined here, to be confirmed with `tone-metrics`. |

These numbers are priors from baselines and from what a human would call "found the preset", not from measurements. Plan: after the first run, measure two baselines at the same budget (prior-mean preset; random search) and reset each threshold to `max(proposed, baseline + 0.10)` for F1/accuracy and `min(proposed, baseline - 0.05)` for MAE, then freeze for Gate 2. Regression tolerances (manifest): F1 0.02, accuracy 0.02, MAE 0.01, rank-1 0.02; any worse than the previous row for the same kind, dataset and engine freezes merges.

## Not done here, and what unblocks it

- Rendering the truth set: needs the three captures, `dry-signal` clip ids (T021) and, for the Stomp half, a human go for an attended batch (restore check afterwards). The `--render` and `--ci` flags exit 2 until then.
- Search recovery runs need T022 (loss kit) and T023 (search).
- Multitrack stems for `d1..d3`: human picks and keeps them local.
- Housekeeping: the Native-vs-Stomp split needs `engine` in the regression key (already so in `regressions()`); `contracts/ledger-format.md` says "same kind and dataset", so this is a clarification request to the lead, not a contract change by me.
- Full-repo `ruff` currently fails on two other files (`src/tonematch/search/loop.py:129` RUF046, `src/tonematch/separation/models.py:113` RUF012); my files are clean.

## Proposal to the lead: ledger regression key (contracts/ledger-format.md, not edited)

Add to the Rules paragraph: "A regression is judged against the previous row with the same `kind`, `dataset`, `engine` and `metric`." Today the text says only kind and dataset; with one dataset name for the Native and Stomp sub-sets of `synthetic-v1`, that would compare unlike engines. Breaks nobody: `scripts/synthetic_benchmark.py` already keys this way; the lead and `reviewer` should read regressions the same way.
