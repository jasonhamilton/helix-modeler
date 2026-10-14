# data/di: local reference DI library (never committed)

Audio here is gitignored (`*.wav`, `*.flac`) and `manifest.json` should stay local too. Nothing is downloaded automatically.

1. Download a licence-clean DI set (recommended: Guitar-TECHS, CC BY 4.0, https://guitar-techs.github.io/) and unpack the DI WAVs under this folder, any subfolder layout.
2. Build the index: `python -c "from tonematch.dry import index_library; index_library('data/di', source='Guitar-TECHS', licence='CC BY 4.0')"` (add `rebuild=True` after adding files).
3. Pick: `from tonematch.dry import pick_di; pick_di((52, 71), 'chords', n=3, library='data/di')`.

With no library present, `pick_di` warns and returns the SYNTHETIC `fixtures/smoke/di_2s.wav`. Format and heuristics: `docs/notes/dry-signal.md`.
