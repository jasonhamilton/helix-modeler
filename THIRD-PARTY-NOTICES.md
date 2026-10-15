# Third-party notices

This project's own code is MIT (see `LICENSE`). It does not bundle or redistribute any of the components below. They are installed by the user from their own indexes (optional extras in `pyproject.toml`), each under its own licence. If a bundled or frozen release is ever built, review this file and `docs/licence-allowlist.json` first.

| Component | Licence | How we use it | Notes |
|---|---|---|---|
| pedalboard (Spotify) | GPLv3 | Optional `native` extra. Loaded only inside a child process (`src/tonematch/engines/native_host.py`); never imported by the main process or by tests (a test guards this). | MIT code may call a GPL program across a process boundary. Anyone who redistributes pedalboard together with this code must follow the GPL for the combined distribution. |
| PyObjC (pyobjc-core, pyobjc-framework-Cocoa; Ronald Oussoren et al.) | MIT | Optional `native-gui` extra, macOS only. Imported only inside the Native host child process (`src/tonematch/engines/native_gui.py`, loaded by `native_host`) to drop a generated preset file on Helix Native's editor. | Unmodified, dynamically installed. Uses only public AppKit APIs inside our own process; no Line 6 code is touched. |
| lameenc | LGPL-3.0-or-later | Transitive dependency of demucs (`separation` extra); never called. | Unmodified, dynamically installed. |
| soxr (python-soxr) | LGPL-2.1-or-later | Transitive dependency of `separation-roformer`. | Unmodified, dynamically installed. |
| diffq | CC BY-NC 4.0 (non-commercial) | Transitive dependency of `separation-roformer`. | Blocks any bundled, commercial or redistributed release that includes it. Private local research use only. |
| certifi, tqdm | MPL-2.0 (tqdm: MPL-2.0 AND MIT) | Transitive dependencies of optional extras. | Unmodified. |
| sphn | Apache-2.0 | Transitive dependency of demucs. | Per its shipped LICENSE file. |
| demucs `htdemucs_6s` weights | MIT per the Demucs README | Downloaded at run time into the gitignored `cache/models/`. | Never bundled. |
| tonepush (crmne) | MIT | Protocol cross-check; the local `stompd` helper is built on its crates at a pinned revision. | Helper source is not in this repository. See `docs/notes/stompd.md`. |
| fretwire | MIT OR Apache-2.0 | Protocol cross-check only. | No code copied. |
| AFx-Rep encoder code (ST-ITO, Christian J. Steinmetz) | Apache-2.0 (the file says it is adapted from PANNs, MIT, Qiuqiang Kong) | **Vendored** (the one exception to "nothing bundled"): `src/tonematch/metrics/_afxrep_model.py`, the `Cnn14` class, trimmed for inference. The changes are listed in its header. | Licence texts in `licenses/Apache-2.0-st-ito.txt` and `licenses/MIT-panns-audioset_tagging_cnn.txt` (Copyright (c) 2018-2020 Qiuqiang Kong). Source github.com/csteinmetz1/st-ito at commit ec50e0cc. |
| AFx-Rep weights `afx-rep.ckpt` | Apache-2.0 per the HF card tag (card has no other text) | Optional `embed` extra, downloaded by the user into the gitignored `cache/models/afx-rep/`. | Never bundled. The training data includes MUSDB18 (educational, non-commercial use only) and other datasets with unverified licences: see `docs/notes/afx-rep.md`. Re-review before any bundled or commercial release. |
| torchlibrosa | MIT | Optional `embed` extra. | Unmodified. |
| Guitar-TECHS (DI dataset) | CC BY 4.0 (project page; unverified) | Optional local reference-DI library, never committed. | Keep the licence text and attribution with the data. |

Line 6 software (HX Edit, Helix Native, firmware, catalogs) is never in this repository. It is read from the user's own installation at run time, under the user's own Line 6 licence.
