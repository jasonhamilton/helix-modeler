"""How it learns (docs/PLAN.md). Pilot code only so far.

`drive_pilot` is predictor-pilot-v0 (T186): a small log-mel CNN trained on single drive-block Native renders
already on disk, with a family head (cache/drive-maps/drive-families-v1.json), a model head hierarchical within the
family, and Drive / Tone regressions with uncertainty. A measurement: nothing here is adopted or used by search.
"""
