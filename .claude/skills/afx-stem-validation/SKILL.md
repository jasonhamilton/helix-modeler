---
name: afx-stem-validation
description: Build, play and ingest the blind session that checks whether AFx-Rep distance orders candidate drive models the way the human hears them against a separated stem (T223). Never score it yourself; the analysis is pre-registered in the session files.
---

# afx-stem-validation

Script: `scripts/rating_rank.py` (owner: `benchmark`). Protocol and pre-registration: `docs/notes/afx-stem-validation-t223.md`.

1. `python scripts/proc_check.py`, then `python scripts/rating_rank.py build [--name afx-stem-validation-v1]` (renders 25 candidates on one Native host at nice 10 behind LoadGuard; writes the page under `reports/local/ratings/<name>/` and the key under `cache/ratings/<name>.key.json`; ledger `trials_built`).
2. Tell the human the page URL and the one scoring command. Do not open the key, listen, guess or describe the clips.
3. After the human saves `reports/local/ratings/<name>.answers.json`: `python scripts/rating_rank.py score <name>` prints the pre-registered analysis and writes ledger rows. Report the listener's test-retest first.
4. Nothing is adopted by this session; AFx-Rep's role is the human's decision.
