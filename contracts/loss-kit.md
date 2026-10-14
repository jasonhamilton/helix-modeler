# Loss kit interface, v0

Owner: `tone-metrics`. Consumed by `search`, `benchmark`, `learning`.

## One call

`tonematch.metrics.LossKit.score(target: Audio, candidate: Audio, *, stems: Stems | None = None, mask: Mask | None = None, weights: dict[str, float] | None = None) -> Score`

- `target`: the isolated guitar (or the user-supplied stem) for the section. `candidate`: the engine's render of the DI through the candidate preset.
- `stems`: the song's other stems, when the separation-consistent term should run (final candidates only; it costs a separation).
- `mask`: the separator's confidence mask, when the confidence-masked term should run.
- `Score.total` is a scalar, lower is better. `Score.terms` is the per-term breakdown keyed by term name. `Score.measured` carries direct measurements (delay time, feedback, reverb decay, modulation rate and depth) that `search` may use as starting values.

## Terms (names are stable; they are ledger columns)

`ltas_tilt`, `crest_flatness_hnr`, `envelope`, `embed_afxrep`, `embed_fxenc`, `embed_openamp`, `delay_echo`, `reverb_tail`, `mod_periodicity`, `mrstft` (needs an aligned DI), `sepcons` (needs stems).

## Admission rule

A term joins the default weights only after `loss-trust-check` shows it ranks the true preset first on the synthetic set at the threshold `benchmark` sets. Until then it is available but weighted 0 by default.

## Invariances

Terms other than `mrstft` must be insensitive to which notes were played: `score(target, candidate_same_tone_different_notes)` must be small. That is the point of the kit and it is tested.
