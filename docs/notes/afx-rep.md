# AFx-Rep (ST-ITO) as loss term `embed_afxrep`

Owner: `tone-metrics`. Task [T032](../../board/TASKS.md#t032 "Wire AFx-Rep (ST-ITO) in as loss term embed_afxrep"). Read 2026-10-14. Not legal advice.

## Sources and licence wording (read 2026-10-14)

| What | URL | Revision read | Licence, exact wording |
|---|---|---|---|
| Code, ST-ITO | https://github.com/csteinmetz1/st-ito | commit `ec50e0cc647cc3637d0cbbfc01d059c682c9fb27` | `LICENSE` file: "Apache License / Version 2.0, January 2004 / http://www.apache.org/licenses/", the unmodified Apache-2.0 text (the appendix boilerplate still reads "Copyright [yyyy] [name of copyright owner]", so there is no named copyright line). `setup.py`: `license="Apache License 2.0"`, classifier `"License :: OSI Approved :: Apache Software License"`, author "Christian J. Steinmetz". |
| `st_ito/models/panns.py` (the AFx-Rep encoder, `Cnn14`) | same repo | same commit | Header comment: "# Adapted from https://github.com/qiuqiangkong/audioset_tagging_cnn/blob/master/pytorch/models.py" / "# Under MIT License". |
| Checkpoint card | https://huggingface.co/csteinmetz1/afx-rep | repo sha `6582cb5414094146759319819a4242967ecc7e39`, last modified 2024-06-30 | `README.md` is only the YAML front matter `license: apache-2.0`. The card has no other text: no model description, no training-data statement, no usage terms. |
| Files used | `afx-rep.ckpt` (1,156,477,784 bytes, LFS sha256 `3587c4f3a1a8ecbc53b8023c480a0e6ff80719bcc26ce6ee6d08b8daf41d75d4`, matched locally after download), `config.yaml` (4,255 bytes) | same sha | as the card |

The same HF repo also holds `BEATs_iter3_plus_AS2M.pt`, `FXencoder_ps.pt` and `plugins.tar`. These are other people's models and a plugin bundle, re-hosted, so the card's `apache-2.0` tag may not apply to them. They were **not downloaded**.

## Training data (not stated on the card; read from `config.yaml`)

The card states no training data and no data licences. `config.yaml` (`data.init_args.train_tarfiles`) lists eight sources, each rendered through randomly configured VST3 plug-in presets (`plugin_json: plugins/valid_plugins.json`; the plug-ins themselves are not named on the card):

| Source in config | Licence | Status |
|---|---|---|
| musdb18 | Zenodo record 1117372: "provided for educational purposes only and the material contained in it should not be used for any commercial purpose without express permission" | read in search summary only, not the full text |
| mtg-jamendo | per-track Creative Commons, many NC; dataset metadata CC BY-NC-SA | from memory, not verified |
| ursing (URSing, Rochester) | Zenodo record 6404999; licence not found in search results | unknown |
| enst-drums | research licence agreement (non-commercial) | from memory, not verified |
| fsd50k | per-clip CC0 / CC BY / CC BY-NC / Sampling+ | from memory, not verified |
| librispeech | CC BY 4.0 | from memory, not verified |
| medley-solos-db | CC BY 4.0 | from memory, not verified |
| guitarset (also the validation set) | CC BY 4.0 | from memory, not verified |

**For `reviewer` and the lead:** the weights are tagged Apache-2.0 by their author. At least one training source (MUSDB18) is restricted to educational, non-commercial use, and others probably are too. Whether those terms carry over to a trained model is a legal question, not settled here. For private local research this is the same class of case as `diffq`. Before any bundled or commercial release, either get the human's ruling or retrain on licence-clean data (a job for `learning`).

## What we use, and how

- Nothing from st-ito is installed. Its `install_requires` pulls in `pedalboard` (GPLv3, which we only allow behind a process boundary), `wandb`, `laion_clap`, `transformers`, `wav2clip` and more, none of which the encoder needs. Its `load_param_model` also shells out to `wget` into `./tmp/`. Instead, `src/tonematch/metrics/_afxrep_model.py` vendors the `Cnn14` encoder class: Apache-2.0, adapted from MIT code, with an attribution header and a list of our changes. It needs only `torch` and `torchlibrosa` (MIT, by the PANNs author).
- Optional extra `embed` in `pyproject.toml`: `torch`, `torchlibrosa`, `pyyaml`. Not a base dependency.
- Checkpoint and config live in `cache/models/afx-rep/` (gitignored via `cache/`). Override the location with `TONEMATCH_AFXREP_DIR`. On first load, the encoder's weights (84 of the checkpoint's 92 tensors, 81.9 M parameters) are copied out to `cache/models/afx-rep/encoder.pt` (about 330 MB). The rest of the 1.16 GB is optimiser state. Loading uses `torch.load(weights_only=True)`, so pytorch-lightning is not needed and no pickle code runs.
- To fetch the checkpoint again: `hf download csteinmetz1/afx-rep afx-rep.ckpt config.yaml --revision 6582cb5414094146759319819a4242967ecc7e39 --local-dir cache/models/afx-rep`.
- The st-ito source is cloned for reading at `cache/models/st-ito-src/` (local, never committed). Its `docs/audio/*.mp3` demo files stay there.

## Embedding, as in ST-ITO

`st_ito.utils.get_param_embeds`: resample to 48 kHz, peak-normalise each item, run `Cnn14` (log-mel with 2048 window, 1024 hop, 128 mels, 20 Hz to 20 kHz, min-max input norm, CNN14, max+mean pooling over time), giving `fc_mid` and `fc_side` 512-d outputs, each L2-normalised. A stereo input is split into mid=(L+R)/2 and side=(L-R)/2. A mono input runs the mid head only, and side equals mid. ST-ITO's search objective (`st_ito/style_transfer.py`, `run_es`) is the **negative cosine similarity per embedding, averaged over mid and side**. `scripts/run_optim.py` also offers L2 (`mse_loss`), but the default and the paper's setting is cosine.

Our term is `1 - cos(mid_t, mid_c)`, averaged with the same quantity for side when both inputs are stereo. It lies in [0, 2], 0 means the embeddings are identical, and it ranks candidates exactly as ST-ITO does. The loss kit passes mono audio, so in the kit the term is the mid embedding only.

## Cost on this machine (Apple M1 Pro, 2026-10-14, load average about 3)

| Step | Time |
|---|---|
| First load from `afx-rep.ckpt` (extracts `encoder.pt`) | 1.2-4.3 s |
| Load from `encoder.pt` | 0.5-0.7 s |
| Device probe (once per process, `auto`) | 1.5 s |
| Embed one 10 s mono clip, CPU, 2 threads | **0.065 s** (median of 10) |
| Same, CPU, 1 thread | 0.083 s |
| Same, MPS | 0.24-0.25 s (agrees with CPU to 1 - cos about 1e-7, but slower; batch of 16: 0.26 s per clip) |
| `LossKit.score(..., weights={"embed_afxrep": 1})`, target cached | 0.07 s per candidate |

Batching does not help on the CPU (0.22 s per clip in a batch of 16). `auto` therefore picks the CPU here. Compared with a Native render (about 2.25 s per 10 s clip), the term adds about 3% per candidate.

## Proxy trust check (NOT Helix)

Seeds 9201-9203, 120 items, paired with `ltas_tilt` and `envelope`. Report: `cache/loss-trust/proxy-dsp-v0-T032-afxrep-12s.json`. Ledger: [2026-10-14T23:14:41](../../ledger/runs.csv?plain=1#L56 "loss-trust · - · proxy-dsp-v0 · rank1_rate = 0.0667 (first of 6 rows at this time)") (6 rows).

| Term | Held-out rank-1 | Same-notes rank-1 | Held-out vs random only | Pooled pairwise win |
|---|---|---|---|---|
| embed_afxrep | 0.067 | 0.083 | 0.825 | 0.844 |
| ltas_tilt | 0.133 | 0.292 | 0.933 | 0.882 |
| envelope | 0.150 | 0.242 | 0.750 | 0.861 |

Chance is 0.0625. AFx-Rep is worse on the proxy. Without the proxy separator (diagnostic, seed 9201 only), it is clean-target strong on boost, cab, eq and random (pairwise 0.98-1.00), but it always prefers the gate-perturbed distractor (pairwise 0.26). Details and reasoning: `docs/DECISIONS.md`, [T032](../../board/TASKS.md#t032 "Wire AFx-Rep (ST-ITO) in as loss term embed_afxrep").
