# Loss kit interface, v0

Owner: `tone-metrics`. Consumed by `search`, `benchmark`, `learning`.

## One call

`tonematch.metrics.LossKit.score(target: Audio, candidate: Audio, *, stems: Stems | None = None, mask: Mask | None = None, weights: dict[str, float] | None = None) -> Score`

- `target`: the isolated guitar (or the user-supplied stem) for the section. `candidate`: the engine's render of the DI through the candidate preset.
- `stems`: the song's other stems, when the separation-consistent term should run (final candidates only; it costs a separation).
- `mask`: the separator's confidence mask, when the confidence-masked term should run.
- `Score.total` is a scalar, lower is better. `Score.terms` is the per-term breakdown keyed by term name. `Score.measured` carries direct measurements (delay time, feedback, reverb decay, modulation rate and depth) that `search` may use as starting values.

## Terms (names are stable; they are ledger columns)

`ltas_tilt`, `crest_flatness_hnr`, `envelope`, `embed_afxrep`, `embed_fxenc`, `embed_openamp`, `delay_echo`, `reverb_tail`, `mod_periodicity`, `mrstft` (needs an aligned DI), `sepcons` (needs stems), `mrstft_v2` (needs an aligned DI; added 2026-10-16 by the lead, additive: v1 rows stay comparable), `level_db` (absolute level difference in dB; needs an aligned DI, known-DI mode only, like `mrstft_v2`; added 2026-10-18 by the lead's instruction for T146, additive: no existing term, weight or ledger column changes).

## Admission rule

A term joins the default weights only after `loss-trust-check` shows it ranks the true preset first on the synthetic set at the threshold `benchmark` sets. Until then it is available but weighted 0 by default. Terms admitted only for a mode (for example `mrstft_v2` for the known-DI mode) are listed in that mode's weights (`KNOWN_DI_WEIGHTS`), not in `DEFAULT_WEIGHTS`, because they are invalid outside it.

## Invariances

Terms other than `mrstft`, `mrstft_v2` and `level_db` (the known-DI terms) must be insensitive to which notes were played: `score(target, candidate_same_tone_different_notes)` must be small. That is the point of the kit and it is tested.
