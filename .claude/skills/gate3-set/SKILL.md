---
name: gate3-set
description: Verify or score the sealed Gate 3 blind set gate3-v1 (single drive blocks on the reserved player P3). Scoring needs the lead's launch reference and uses the pre-registered, proposed bars; never read sealed/ otherwise.
---

# gate3-set

Script: `scripts/gate3_set.py` (owner: `benchmark`). Set and bars: `docs/notes/gate3-v1.md`; bars registered in `docs/DECISIONS.md` 2026-10-25 (proposed, not approved until the human says so).

- `python scripts/gate3_set.py verify` re-checks the hashes of the target files; it opens no label.
- `python scripts/gate3_set.py score --predictions <json> --launched-by <ref> [--reference <json>] [--bars-approved <ref>]` only when the lead has launched the run. One scoring attempt per frozen method; register its code hash first. A changed method needs a new set (reserve P3 files 02, 03, 08).
- P3 stays out of every training path. Do not regenerate with `--force` once anyone has run the set.
