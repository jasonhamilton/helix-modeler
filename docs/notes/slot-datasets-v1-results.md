# slot-dataset-v1 overnight results ([T213](../../board/TASKS.md#t213 "slot-dataset-v1 overnight run"))

Measurement only. Nothing here is admitted into the matcher. All slot-dataset-v1 defaults as approved on 2026-10-22; [T212](../../board/TASKS.md#t212 "Diagnose the slot-dataset-v1 amp \"Native returned silence\" failures…") options A (amp refill at -240 dBFS, out_db stored, digital-zero clips skipped, training with --with-refill keeping every clip) and B (new slot plans store the -240 dBFS floor and out_db). Training: the [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1") heads (logreg, MLP), multi-crop, CPU, at half (the first half of the plan's shards, a seeded random half) and full size. Knob accuracy: within +-0.15 of the true position with the true model's knob predictions, pooled over the model's knobs (true) and with the head's own top-1 model (own). Evaluation on the held-out DI files (test) and the T186/T189 test files (v1test).

Written by scripts/slot_overnight.py, 2026-10-23 06:49 local; window ends 2026-10-23 08:00.

## Clips and failures

| Slot | Planned | Rendered ok | Failed | Silence (digital zero) | Refill planned / ok / failed | Render wall (this supervisor) |
|---|---|---|---|---|---|---|
| amp | 120,000 | 114,347 | 5,653 | 5653 (127) | 5526 / 5526 / 0 | 0:16 |
| cab | 50,000 | 50,000 | 0 | 0 (0) | - | 2:38 |
| delay | 45,000 | 44,683 | 317 | 317 (317) | - | 2:04 |
| mod | 12,000 | 11,985 | 15 | 15 (15) | - | 0:33 |
| reverb | 6,000 | 6,000 | 0 | 0 (0) | - | 0:16 |
| eq | 8,000 | 8,000 | 0 | 0 (0) | - | 0:22 |

## Training (main arm, within player)

| Slot | Size | Clips | Head | Eval | Model top-1 | Top-3 | Knobs within +-0.15 true / own | All knobs within (true) | Fit s |
|---|---|---|---|---|---|---|---|---|---|
| amp | half | 59,943 | logreg s0 | test | 0.391 | 0.620 | 0.443 / 0.169 | 0.007 | 401 |
| amp | half | 59,943 | logreg s0 | v1test | 0.388 | 0.611 | 0.444 / 0.169 | 0.008 | 401 |
| amp | half | 59,943 | mlp s0 | test | 0.502 | 0.759 | 0.436 / 0.213 | 0.008 | 2882 |
| amp | half | 59,943 | mlp s0 | v1test | 0.497 | 0.745 | 0.433 / 0.209 | 0.008 | 2882 |
| amp | full | 119,873 | logreg s0 | test | 0.427 | 0.665 | 0.457 / 0.190 | 0.010 | 935 |
| amp | full | 119,873 | logreg s0 | v1test | 0.419 | 0.648 | 0.452 / 0.185 | 0.009 | 935 |
| amp | full | 119,873 | mlp s0 | test | 0.543 | 0.797 | 0.449 / 0.237 | 0.009 | 5698 |
| amp | full | 119,873 | mlp s0 | v1test | 0.535 | 0.778 | 0.444 / 0.230 | 0.009 | 5698 |
| cab | half | 25,000 | logreg s0 | test | 0.642 | 0.813 | 0.528 / 0.333 | 0.112 | 72 |
| cab | half | 25,000 | logreg s0 | v1test | 0.614 | 0.802 | 0.525 / 0.313 | 0.106 | 72 |
| cab | half | 25,000 | mlp s0 | test | 0.708 | 0.867 | 0.605 / 0.414 | 0.173 | 570 |
| cab | half | 25,000 | mlp s0 | v1test | 0.688 | 0.867 | 0.600 / 0.401 | 0.166 | 570 |
| cab | full | 50,000 | logreg s0 | test | 0.678 | 0.844 | 0.560 / 0.373 | 0.137 | 169 |
| cab | full | 50,000 | logreg s0 | v1test | 0.665 | 0.839 | 0.553 / 0.361 | 0.124 | 169 |
| cab | full | 50,000 | mlp s0 | test | 0.761 | 0.910 | 0.636 / 0.472 | 0.204 | 1009 |
| cab | full | 50,000 | mlp s0 | v1test | 0.756 | 0.903 | 0.629 / 0.466 | 0.194 | 1009 |
| delay | half | 22,346 | logreg s0 | test | 0.484 | 0.670 | 0.413 / 0.213 | 0.035 | 39 |
| delay | half | 22,346 | logreg s0 | v1test | 0.473 | 0.664 | 0.414 / 0.209 | 0.029 | 39 |
| delay | half | 22,346 | mlp s0 | test | 0.527 | 0.716 | 0.413 / 0.224 | 0.027 | 351 |
| delay | half | 22,346 | mlp s0 | v1test | 0.526 | 0.707 | 0.411 / 0.223 | 0.027 | 351 |
| delay | full | 44,683 | logreg s0 | test | 0.502 | 0.695 | 0.422 / 0.225 | 0.035 | 88 |
| delay | full | 44,683 | logreg s0 | v1test | 0.495 | 0.684 | 0.422 / 0.224 | 0.034 | 88 |
| delay | full | 44,683 | mlp s0 | test | 0.558 | 0.744 | 0.426 / 0.245 | 0.038 | 707 |
| delay | full | 44,683 | mlp s0 | v1test | 0.551 | 0.730 | 0.427 / 0.244 | 0.032 | 707 |
| mod | half | 5,992 | logreg s0 | test | 0.688 | 0.875 | 0.592 / 0.415 | 0.401 | 5 |
| mod | half | 5,992 | logreg s0 | v1test | 0.661 | 0.873 | 0.574 / 0.386 | 0.376 | 5 |
| mod | half | 5,992 | mlp s0 | test | 0.708 | 0.886 | 0.602 / 0.435 | 0.426 | 65 |
| mod | half | 5,992 | mlp s0 | v1test | 0.668 | 0.878 | 0.586 / 0.387 | 0.412 | 65 |
| mod | full | 11,985 | logreg s0 | test | 0.718 | 0.891 | 0.601 / 0.441 | 0.418 | 10 |
| mod | full | 11,985 | logreg s0 | v1test | 0.699 | 0.889 | 0.577 / 0.414 | 0.385 | 10 |
| mod | full | 11,985 | mlp s0 | test | 0.750 | 0.907 | 0.625 / 0.475 | 0.451 | 132 |
| mod | full | 11,985 | mlp s0 | v1test | 0.723 | 0.908 | 0.597 / 0.436 | 0.419 | 132 |
| reverb | half | 3,000 | logreg s0 | test | 0.451 | 0.794 | 0.461 / 0.226 | 0.015 | 2 |
| reverb | half | 3,000 | logreg s0 | v1test | 0.449 | 0.817 | 0.468 / 0.230 | 0.020 | 2 |
| reverb | half | 3,000 | mlp s0 | test | 0.476 | 0.807 | 0.464 / 0.237 | 0.015 | 32 |
| reverb | half | 3,000 | mlp s0 | v1test | 0.489 | 0.835 | 0.492 / 0.261 | 0.025 | 32 |
| reverb | full | 6,000 | logreg s0 | test | 0.458 | 0.793 | 0.459 / 0.226 | 0.020 | 6 |
| reverb | full | 6,000 | logreg s0 | v1test | 0.458 | 0.817 | 0.474 / 0.235 | 0.019 | 6 |
| reverb | full | 6,000 | mlp s0 | test | 0.467 | 0.808 | 0.481 / 0.248 | 0.021 | 63 |
| reverb | full | 6,000 | mlp s0 | v1test | 0.502 | 0.833 | 0.511 / 0.279 | 0.024 | 63 |
| eq | half | 4,000 | logreg s0 | test | 0.705 | 0.960 | 0.617 / 0.453 | 0.339 | 4 |
| eq | half | 4,000 | logreg s0 | v1test | 0.750 | 0.970 | 0.608 / 0.477 | 0.394 | 4 |
| eq | half | 4,000 | mlp s0 | test | 0.757 | 0.983 | 0.592 / 0.455 | 0.301 | 45 |
| eq | half | 4,000 | mlp s0 | v1test | 0.789 | 0.988 | 0.574 / 0.457 | 0.355 | 45 |
| eq | full | 8,000 | logreg s0 | test | 0.732 | 0.973 | 0.631 / 0.478 | 0.397 | 9 |
| eq | full | 8,000 | logreg s0 | v1test | 0.771 | 0.972 | 0.622 / 0.492 | 0.417 | 9 |
| eq | full | 8,000 | mlp s0 | test | 0.824 | 0.995 | 0.625 / 0.520 | 0.398 | 88 |
| eq | full | 8,000 | mlp s0 | v1test | 0.840 | 0.991 | 0.617 / 0.515 | 0.421 | 88 |

## Training (cross player)

| Slot | Size | Arm | Head | Eval | Model top-1 | Top-3 | Knobs within true / own |
|---|---|---|---|---|---|---|---|
| amp | half | p2p1 | logreg | test | 0.253 | 0.443 | 0.415 / 0.108 |
| amp | half | p2p1 | logreg | v1test | 0.232 | 0.409 | 0.413 / 0.101 |
| amp | half | p2p1 | mlp | test | 0.288 | 0.494 | 0.398 / 0.118 |
| amp | half | p2p1 | mlp | v1test | 0.252 | 0.439 | 0.394 / 0.104 |
| amp | half | p1p2 | logreg | test | 0.163 | 0.306 | 0.392 / 0.065 |
| amp | half | p1p2 | logreg | v1test | 0.219 | 0.390 | 0.398 / 0.087 |
| amp | half | p1p2 | mlp | test | 0.169 | 0.327 | 0.382 / 0.068 |
| amp | half | p1p2 | mlp | v1test | 0.237 | 0.424 | 0.385 / 0.092 |
| amp | full | p2p1 | logreg | test | 0.281 | 0.476 | 0.425 / 0.124 |
| amp | full | p2p1 | logreg | v1test | 0.245 | 0.430 | 0.423 / 0.109 |
| amp | full | p2p1 | mlp | test | 0.312 | 0.516 | 0.411 / 0.133 |
| amp | full | p2p1 | mlp | v1test | 0.278 | 0.466 | 0.405 / 0.118 |
| amp | full | p1p2 | logreg | test | 0.174 | 0.319 | 0.400 / 0.073 |
| amp | full | p1p2 | logreg | v1test | 0.232 | 0.407 | 0.403 / 0.094 |
| amp | full | p1p2 | mlp | test | 0.187 | 0.344 | 0.388 / 0.076 |
| amp | full | p1p2 | mlp | v1test | 0.262 | 0.448 | 0.393 / 0.105 |
| cab | half | p2p1 | logreg | test | 0.280 | 0.511 | 0.434 / 0.107 |
| cab | half | p2p1 | logreg | v1test | 0.289 | 0.481 | 0.427 / 0.114 |
| cab | half | p2p1 | mlp | test | 0.304 | 0.505 | 0.465 / 0.131 |
| cab | half | p2p1 | mlp | v1test | 0.285 | 0.466 | 0.446 / 0.125 |
| cab | half | p1p2 | logreg | test | 0.340 | 0.535 | 0.451 / 0.145 |
| cab | half | p1p2 | logreg | v1test | 0.361 | 0.581 | 0.448 / 0.154 |
| cab | half | p1p2 | mlp | test | 0.314 | 0.519 | 0.472 / 0.139 |
| cab | half | p1p2 | mlp | v1test | 0.337 | 0.553 | 0.473 / 0.147 |
| cab | full | p2p1 | logreg | test | 0.322 | 0.549 | 0.443 / 0.131 |
| cab | full | p2p1 | logreg | v1test | 0.318 | 0.518 | 0.432 / 0.127 |
| cab | full | p2p1 | mlp | test | 0.317 | 0.528 | 0.473 / 0.140 |
| cab | full | p2p1 | mlp | v1test | 0.297 | 0.506 | 0.450 / 0.132 |
| cab | full | p1p2 | logreg | test | 0.338 | 0.550 | 0.467 / 0.151 |
| cab | full | p1p2 | logreg | v1test | 0.376 | 0.585 | 0.469 / 0.165 |
| cab | full | p1p2 | mlp | test | 0.347 | 0.549 | 0.490 / 0.162 |
| cab | full | p1p2 | mlp | v1test | 0.371 | 0.593 | 0.489 / 0.173 |
| delay | half | p2p1 | logreg | test | 0.318 | 0.478 | 0.370 / 0.137 |
| delay | half | p2p1 | logreg | v1test | 0.299 | 0.465 | 0.377 / 0.132 |
| delay | half | p2p1 | mlp | test | 0.293 | 0.458 | 0.343 / 0.117 |
| delay | half | p2p1 | mlp | v1test | 0.287 | 0.444 | 0.351 / 0.122 |
| delay | half | p1p2 | logreg | test | 0.227 | 0.382 | 0.375 / 0.100 |
| delay | half | p1p2 | logreg | v1test | 0.271 | 0.439 | 0.379 / 0.114 |
| delay | half | p1p2 | mlp | test | 0.234 | 0.388 | 0.358 / 0.101 |
| delay | half | p1p2 | mlp | v1test | 0.261 | 0.427 | 0.360 / 0.109 |
| delay | full | p2p1 | logreg | test | 0.332 | 0.478 | 0.379 / 0.148 |
| delay | full | p2p1 | logreg | v1test | 0.307 | 0.444 | 0.380 / 0.137 |
| delay | full | p2p1 | mlp | test | 0.316 | 0.473 | 0.359 / 0.138 |
| delay | full | p2p1 | mlp | v1test | 0.291 | 0.448 | 0.361 / 0.130 |
| delay | full | p1p2 | logreg | test | 0.250 | 0.405 | 0.374 / 0.110 |
| delay | full | p1p2 | logreg | v1test | 0.284 | 0.465 | 0.386 / 0.126 |
| delay | full | p1p2 | mlp | test | 0.245 | 0.391 | 0.362 / 0.108 |
| delay | full | p1p2 | mlp | v1test | 0.272 | 0.444 | 0.366 / 0.115 |
| mod | half | p2p1 | logreg | test | 0.549 | 0.795 | 0.530 / 0.295 |
| mod | half | p2p1 | logreg | v1test | 0.489 | 0.765 | 0.503 / 0.246 |
| mod | half | p2p1 | mlp | test | 0.604 | 0.814 | 0.548 / 0.319 |
| mod | half | p2p1 | mlp | v1test | 0.511 | 0.780 | 0.470 / 0.236 |
| mod | half | p1p2 | logreg | test | 0.472 | 0.736 | 0.526 / 0.265 |
| mod | half | p1p2 | logreg | v1test | 0.511 | 0.766 | 0.524 / 0.289 |
| mod | half | p1p2 | mlp | test | 0.446 | 0.718 | 0.520 / 0.245 |
| mod | half | p1p2 | mlp | v1test | 0.499 | 0.776 | 0.536 / 0.277 |
| mod | full | p2p1 | logreg | test | 0.560 | 0.807 | 0.540 / 0.313 |
| mod | full | p2p1 | logreg | v1test | 0.491 | 0.769 | 0.511 / 0.256 |
| mod | full | p2p1 | mlp | test | 0.600 | 0.819 | 0.541 / 0.332 |
| mod | full | p2p1 | mlp | v1test | 0.513 | 0.784 | 0.492 / 0.251 |
| mod | full | p1p2 | logreg | test | 0.482 | 0.742 | 0.524 / 0.262 |
| mod | full | p1p2 | logreg | v1test | 0.514 | 0.785 | 0.530 / 0.295 |
| mod | full | p1p2 | mlp | test | 0.474 | 0.719 | 0.538 / 0.269 |
| mod | full | p1p2 | mlp | v1test | 0.497 | 0.770 | 0.557 / 0.279 |
| reverb | half | p2p1 | logreg | test | 0.406 | 0.731 | 0.455 / 0.202 |
| reverb | half | p2p1 | logreg | v1test | 0.389 | 0.700 | 0.429 / 0.167 |
| reverb | half | p2p1 | mlp | test | 0.406 | 0.736 | 0.437 / 0.198 |
| reverb | half | p2p1 | mlp | v1test | 0.374 | 0.719 | 0.446 / 0.176 |
| reverb | half | p1p2 | logreg | test | 0.321 | 0.749 | 0.406 / 0.142 |
| reverb | half | p1p2 | logreg | v1test | 0.351 | 0.762 | 0.425 / 0.162 |
| reverb | half | p1p2 | mlp | test | 0.374 | 0.743 | 0.416 / 0.163 |
| reverb | half | p1p2 | mlp | v1test | 0.391 | 0.787 | 0.392 / 0.158 |
| reverb | full | p2p1 | logreg | test | 0.394 | 0.740 | 0.452 / 0.193 |
| reverb | full | p2p1 | logreg | v1test | 0.402 | 0.723 | 0.458 / 0.190 |
| reverb | full | p2p1 | mlp | test | 0.389 | 0.754 | 0.455 / 0.188 |
| reverb | full | p2p1 | mlp | v1test | 0.389 | 0.746 | 0.460 / 0.184 |
| reverb | full | p1p2 | logreg | test | 0.305 | 0.749 | 0.418 / 0.128 |
| reverb | full | p1p2 | logreg | v1test | 0.349 | 0.726 | 0.435 / 0.157 |
| reverb | full | p1p2 | mlp | test | 0.311 | 0.754 | 0.428 / 0.144 |
| reverb | full | p1p2 | mlp | v1test | 0.361 | 0.752 | 0.445 / 0.167 |
| eq | half | p2p1 | logreg | test | 0.582 | 0.864 | 0.504 / 0.387 |
| eq | half | p2p1 | logreg | v1test | 0.496 | 0.821 | 0.478 / 0.320 |
| eq | half | p2p1 | mlp | test | 0.630 | 0.930 | 0.452 / 0.321 |
| eq | half | p2p1 | mlp | v1test | 0.579 | 0.913 | 0.440 / 0.303 |
| eq | half | p1p2 | logreg | test | 0.507 | 0.795 | 0.480 / 0.295 |
| eq | half | p1p2 | logreg | v1test | 0.600 | 0.886 | 0.498 / 0.340 |
| eq | half | p1p2 | mlp | test | 0.478 | 0.839 | 0.449 / 0.257 |
| eq | half | p1p2 | mlp | v1test | 0.592 | 0.855 | 0.446 / 0.282 |
| eq | full | p2p1 | logreg | test | 0.622 | 0.900 | 0.512 / 0.396 |
| eq | full | p2p1 | logreg | v1test | 0.546 | 0.853 | 0.485 / 0.340 |
| eq | full | p2p1 | mlp | test | 0.672 | 0.972 | 0.526 / 0.390 |
| eq | full | p2p1 | mlp | v1test | 0.583 | 0.899 | 0.495 / 0.346 |
| eq | full | p1p2 | logreg | test | 0.517 | 0.807 | 0.472 / 0.278 |
| eq | full | p1p2 | logreg | v1test | 0.614 | 0.887 | 0.499 / 0.344 |
| eq | full | p1p2 | mlp | test | 0.485 | 0.809 | 0.453 / 0.263 |
| eq | full | p1p2 | mlp | v1test | 0.576 | 0.858 | 0.474 / 0.318 |

## Per knob, within +-0.15 (main MLP, held-out test files, true model): half vs full

### amp

| Knob | Half | Full | n (full) |
|---|---|---|---|
| 125Hz | 0.569 | 0.492 | 124 |
| 150Hz | 0.369 | 0.306 | 124 |
| 1kHz | 0.897 | 0.895 | 124 |
| 2200Hz | 0.523 | 0.511 | 372 |
| 240Hz | 0.599 | 0.602 | 372 |
| 250Hz | 0.483 | 0.419 | 124 |
| 2kHz | 0.504 | 0.532 | 248 |
| 300Hz | 0.338 | 0.274 | 124 |
| 33Hz | 0.308 | 0.282 | 124 |
| 4kHz | 0.983 | 0.976 | 124 |
| 500Hz | 0.517 | 0.613 | 124 |
| 5kHz | 0.400 | 0.347 | 124 |
| 600Hz | 0.262 | 0.266 | 124 |
| 62p5Hz | 0.448 | 0.548 | 124 |
| 6600Hz | 0.715 | 0.782 | 372 |
| 750Hz | 0.651 | 0.691 | 372 |
| 80Hz | 0.346 | 0.329 | 496 |
| 8kHz | 0.308 | 0.290 | 124 |
| 900Hz | 0.323 | 0.363 | 124 |
| Bass | 0.467 | 0.486 | 13893 |
| Bias | 0.390 | 0.406 | 6824 |
| BiasX | 0.303 | 0.300 | 6824 |
| Boost | 0.367 | 0.376 | 919 |
| Bright | 0.259 | 0.325 | 126 |
| BrtDrive | 0.629 | 0.684 | 803 |
| Ch1 Bass | 0.627 | 0.697 | 277 |
| Ch1 Drive | 0.634 | 0.700 | 277 |
| Ch1 Master | 0.276 | 0.300 | 277 |
| Ch1 Mid | 0.299 | 0.379 | 277 |
| Ch1 Mid Freq | 0.328 | 0.321 | 277 |
| Ch1 Treble | 0.761 | 0.812 | 277 |
| Ch1Drive | 0.618 | 0.625 | 280 |
| Ch2 Bass | 0.561 | 0.630 | 262 |
| Ch2 Drive | 0.568 | 0.553 | 262 |
| Ch2 Master | 0.326 | 0.351 | 262 |
| Ch2 Mid | 0.295 | 0.447 | 262 |
| Ch2 Mid Freq | 0.265 | 0.321 | 262 |
| Ch2 Treble | 0.606 | 0.683 | 262 |
| Ch2Drive | 0.556 | 0.685 | 276 |
| Cut | 0.334 | 0.370 | 584 |
| Deep | 0.377 | 0.476 | 126 |
| Depth | 0.351 | 0.348 | 696 |
| Drive | 0.635 | 0.649 | 10963 |
| Drive 1 | 0.512 | 0.483 | 333 |
| Drive 2 | 0.382 | 0.352 | 270 |
| Drive Trem | 0.212 | 0.270 | 63 |
| Drive1 | 0.295 | 0.352 | 88 |
| Drive2 | 0.413 | 0.471 | 223 |
| DriveBright | 0.347 | 0.320 | 815 |
| DriveNorm | 0.314 | 0.299 | 815 |
| EQLevel | 0.262 | 0.315 | 124 |
| Effect | 0.353 | 0.325 | 120 |
| Gain | 0.502 | 0.570 | 663 |
| HPF | 0.420 | 0.428 | 145 |
| Hi Pass | 0.828 | 0.934 | 61 |
| High | 0.556 | 0.648 | 270 |
| HighMid | 0.606 | 0.692 | 669 |
| Input | 0.552 | 0.508 | 61 |
| LeadDrive | 0.340 | 0.393 | 117 |
| LeadGain | 0.396 | 0.444 | 117 |
| Low | 0.407 | 0.481 | 270 |
| Low Pass | 1.000 | 0.984 | 61 |
| LowMid | 0.385 | 0.415 | 270 |
| Master | 0.360 | 0.372 | 13920 |
| Mid | 0.425 | 0.433 | 11880 |
| Mid Freq | 0.304 | 0.307 | 140 |
| Middle | 0.525 | 0.488 | 129 |
| NFB | 0.323 | 0.318 | 66 |
| Norm Drive | 0.778 | 0.814 | 156 |
| NrmDrive | 0.407 | 0.462 | 805 |
| ODDrive | 0.327 | 0.268 | 123 |
| PostGain | 0.391 | 0.441 | 379 |
| PreGain | 0.473 | 0.586 | 379 |
| PreMid | 0.339 | 0.288 | 236 |
| PreMidFc | 0.323 | 0.297 | 236 |
| Presence | 0.341 | 0.356 | 6747 |
| Resonance | 0.312 | 0.339 | 186 |
| Sag | 0.309 | 0.298 | 13328 |
| TFCoarse | 0.804 | 0.792 | 120 |
| TFFine | 0.333 | 0.300 | 120 |
| Threshold | 0.262 | 0.293 | 409 |
| Tone | 0.493 | 0.496 | 1235 |
| Treble | 0.563 | 0.584 | 13893 |
| TuningFork | 0.255 | 0.317 | 120 |

### cab

| Knob | Half | Full | n (full) |
|---|---|---|---|
| Angle | 0.347 | 0.351 | 3331 |
| Distance | 0.559 | 0.609 | 6445 |
| High Cut | 0.550 | 0.631 | 217 |
| HighCut | 0.621 | 0.646 | 6228 |
| Low Cut | 0.738 | 0.789 | 76 |
| LowCut | 0.801 | 0.829 | 6369 |
| Position | 0.545 | 0.579 | 3331 |

### delay

| Knob | Half | Full | n (full) |
|---|---|---|---|
| Attack | 0.317 | 0.329 | 514 |
| AutoEQ | 0.269 | 0.246 | 134 |
| Bass | 0.353 | 0.355 | 566 |
| Cents1 | 0.212 | 0.244 | 127 |
| Crossfeed | 0.277 | 0.276 | 134 |
| Decay | 0.370 | 0.364 | 275 |
| DelayVoice1 | 0.349 | 0.262 | 126 |
| DelayVoice2 | 0.397 | 0.294 | 126 |
| Depth | 0.370 | 0.392 | 2319 |
| Diffusion | 0.338 | 0.316 | 136 |
| Drive | 0.307 | 0.352 | 270 |
| Ducking | 0.307 | 0.336 | 265 |
| DynAttack | 0.263 | 0.294 | 143 |
| DynRel | 0.287 | 0.301 | 143 |
| FBTone | 0.321 | 0.349 | 146 |
| FX Level | 0.348 | 0.302 | 275 |
| Feedback | 0.398 | 0.415 | 4245 |
| FeedbackA | 0.415 | 0.343 | 134 |
| FeedbackB | 0.246 | 0.321 | 134 |
| Headroom | 0.340 | 0.346 | 1317 |
| HighCut | 0.461 | 0.504 | 1608 |
| LFeedback | 0.333 | 0.302 | 116 |
| LTime | 0.263 | 0.319 | 116 |
| Left Time | 0.306 | 0.257 | 140 |
| LeftFeedback | 0.318 | 0.321 | 140 |
| LevelRootVoice | 0.429 | 0.341 | 126 |
| LevelVoice1 | 0.381 | 0.317 | 126 |
| LevelVoice2 | 0.286 | 0.325 | 126 |
| LowCut | 0.356 | 0.379 | 1608 |
| Mix | 0.743 | 0.781 | 5242 |
| MixL | 0.459 | 0.486 | 140 |
| MixR | 0.518 | 0.471 | 140 |
| ModDepth | 0.285 | 0.320 | 409 |
| ModFreq | 0.326 | 0.295 | 275 |
| ModRate | 0.277 | 0.336 | 134 |
| ModSpeed | 0.407 | 0.435 | 398 |
| Noise | 0.315 | 0.365 | 394 |
| Offset | 0.277 | 0.276 | 116 |
| PanA | 0.246 | 0.276 | 134 |
| PanB | 0.292 | 0.261 | 134 |
| PanRootVoice | 0.238 | 0.286 | 126 |
| PanVoice1 | 0.397 | 0.294 | 126 |
| PanVoice2 | 0.254 | 0.278 | 126 |
| RTime | 0.368 | 0.345 | 116 |
| Ramp | 0.346 | 0.336 | 146 |
| RandDepth | 0.341 | 0.302 | 275 |
| RandSpeed | 0.274 | 0.298 | 275 |
| Rate | 0.292 | 0.309 | 724 |
| Res | 0.507 | 0.545 | 143 |
| Right Time | 0.282 | 0.286 | 140 |
| RightFeedback | 0.271 | 0.279 | 140 |
| Scale | 0.301 | 0.289 | 1586 |
| Speed | 0.433 | 0.437 | 917 |
| Splice | 0.308 | 0.274 | 146 |
| Step Time | 0.394 | 0.533 | 137 |
| SweepDepth | 0.478 | 0.507 | 148 |
| SweepResonance | 0.348 | 0.392 | 148 |
| SweepSpeed | 0.478 | 0.473 | 148 |
| SweepSpread | 0.290 | 0.318 | 148 |
| SweepStart | 0.319 | 0.318 | 148 |
| SweepSymmetry | 0.275 | 0.338 | 148 |
| Swell | 0.667 | 0.773 | 150 |
| Tap1Delay | 0.326 | 0.237 | 257 |
| Tap1Level | 0.279 | 0.268 | 257 |
| Tap1Pan | 0.304 | 0.324 | 380 |
| Tap2Delay | 0.364 | 0.296 | 257 |
| Tap2Level | 0.318 | 0.327 | 257 |
| Tap2Pan | 0.294 | 0.279 | 380 |
| Tap3Delay | 0.287 | 0.265 | 257 |
| Tap3Level | 0.264 | 0.296 | 257 |
| Tap3Pan | 0.325 | 0.274 | 380 |
| Tap4Delay | 0.318 | 0.272 | 257 |
| Tap4Level | 0.279 | 0.307 | 257 |
| Tap4Pan | 0.294 | 0.311 | 380 |
| Tap5Delay | 0.172 | 0.248 | 121 |
| Tap5Level | 0.281 | 0.314 | 121 |
| Tap5Pan | 0.372 | 0.352 | 244 |
| Tap6Delay | 0.312 | 0.240 | 121 |
| Tap6Level | 0.312 | 0.240 | 121 |
| Tap6Pan | 0.248 | 0.295 | 244 |
| Threshold | 0.559 | 0.585 | 504 |
| Time | 0.400 | 0.420 | 4855 |
| TimeA | 0.385 | 0.351 | 134 |
| TimeB | 0.354 | 0.313 | 134 |
| Tone | 0.552 | 0.545 | 143 |
| Treble | 0.420 | 0.403 | 566 |
| VerbDecay | 0.472 | 0.359 | 103 |
| VerbMix | 0.566 | 0.553 | 103 |
| Wow Flt | 0.474 | 0.470 | 270 |
| WowFlutter | 0.370 | 0.363 | 281 |

### mod

| Knob | Half | Full | n (full) |
|---|---|---|---|
| BassFreq | 0.192 | 0.307 | 140 |
| ChorusIntensity | 0.484 | 0.563 | 167 |
| Depth | 0.545 | 0.589 | 706 |
| DutyCycle | 0.584 | 0.649 | 299 |
| Headroom | 0.391 | 0.426 | 296 |
| Intensity | 0.841 | 0.843 | 568 |
| Mix | 0.714 | 0.740 | 867 |
| Shape | 0.343 | 0.324 | 287 |
| Speed | 0.730 | 0.729 | 1417 |
| Tone | 0.338 | 0.382 | 144 |
| TrebFreq | 0.397 | 0.457 | 140 |
| VolSens | 0.267 | 0.318 | 154 |
| Volsens | 0.338 | 0.271 | 133 |

### reverb

| Knob | Half | Full | n (full) |
|---|---|---|---|
| Decay | 0.521 | 0.549 | 765 |
| HighCut | 0.316 | 0.318 | 765 |
| LowCut | 0.311 | 0.307 | 765 |
| Mix | 0.875 | 0.878 | 765 |
| Predelay | 0.296 | 0.354 | 765 |

### eq

| Knob | Half | Full | n (full) |
|---|---|---|---|
| 125Hz | 0.426 | 0.394 | 132 |
| 16kHz | 0.918 | 0.879 | 132 |
| 1kHz | 0.820 | 0.833 | 132 |
| 2200Hz | 0.718 | 0.773 | 141 |
| 240Hz | 0.654 | 0.773 | 141 |
| 250Hz | 0.393 | 0.455 | 132 |
| 2kHz | 0.787 | 0.742 | 132 |
| 31p25Hz | 0.328 | 0.371 | 132 |
| 4kHz | 0.967 | 0.939 | 132 |
| 500Hz | 0.492 | 0.583 | 132 |
| 62p5Hz | 0.393 | 0.318 | 132 |
| 6600Hz | 0.936 | 0.943 | 141 |
| 750Hz | 0.718 | 0.837 | 141 |
| 80Hz | 0.487 | 0.631 | 141 |
| 8kHz | 0.820 | 0.848 | 132 |
| HighCut | 0.902 | 0.920 | 261 |
| HighFreq | 0.463 | 0.474 | 251 |
| HighGain | 0.582 | 0.601 | 371 |
| HighQ | 0.300 | 0.280 | 132 |
| LowCut | 0.854 | 0.862 | 261 |
| LowFreq | 0.357 | 0.303 | 132 |
| LowGain | 0.485 | 0.539 | 371 |
| LowQ | 0.300 | 0.318 | 132 |
| MidFreq | 0.357 | 0.364 | 132 |
| MidGain | 0.485 | 0.536 | 252 |
| MidQ | 0.300 | 0.318 | 132 |
| Shimmer | 0.353 | 0.452 | 124 |
| Tilt | 0.692 | 0.789 | 90 |
| Top | 0.941 | 0.968 | 124 |

## Wall times

| Step | Slot | Start | End | Wall | Result |
|---|---|---|---|---|---|
| wait | amp | 21:09 | 23:12 | 2:02 | PID 78416 exited |
| refill-plan | amp | 23:12 | 23:12 | 0:00 | 5526 clips added (rc 0) |
| render | amp-refill | 23:12 | 23:28 | 0:16 | 56/56 shards, ok 5526, failed 0 |
| plan | cab | 23:28 | 23:29 | 0:00 | rc 0 |
| train | amp half | 23:28 | 01:17 | 1:48 | n 59943; main MLP test top-1 0.502 top-3 0.759 knobs 0.436 |
| render | cab | 23:29 | 02:07 | 2:38 | 500/500 shards, ok 50000, failed 0 |
| plan | delay | 02:07 | 02:08 | 0:00 | rc 0 |
| render | delay | 02:08 | 04:12 | 2:04 | 450/450 shards, ok 44683, failed 317 |
| plan | mod | 04:12 | 04:13 | 0:00 | rc 0 |
| render | mod | 04:13 | 04:46 | 0:33 | 120/120 shards, ok 11985, failed 15 |
| plan | reverb | 04:46 | 04:46 | 0:00 | rc 0 |
| train | amp full | 01:17 | 04:53 | 3:36 | n 119873; main MLP test top-1 0.543 top-3 0.797 knobs 0.449 |
| render | reverb | 04:46 | 05:03 | 0:16 | 60/60 shards, ok 6000, failed 0 |
| plan | eq | 05:03 | 05:03 | 0:00 | rc 0 |
| train | cab half | 04:53 | 05:15 | 0:21 | n 25000; main MLP test top-1 0.708 top-3 0.867 knobs 0.605 |
| render | eq | 05:03 | 05:25 | 0:22 | 80/80 shards, ok 8000, failed 0 |
| train | cab full | 05:15 | 05:52 | 0:37 | n 50000; main MLP test top-1 0.761 top-3 0.910 knobs 0.636 |
| train | delay half | 05:52 | 06:06 | 0:13 | n 22346; main MLP test top-1 0.527 top-3 0.716 knobs 0.413 |
| train | delay full | 06:06 | 06:33 | 0:27 | n 44683; main MLP test top-1 0.558 top-3 0.744 knobs 0.426 |
| train | mod half | 06:33 | 06:35 | 0:02 | n 5992; main MLP test top-1 0.708 top-3 0.886 knobs 0.602 |
| train | mod full | 06:35 | 06:40 | 0:04 | n 11985; main MLP test top-1 0.750 top-3 0.907 knobs 0.625 |
| train | reverb half | 06:40 | 06:42 | 0:01 | n 3000; main MLP test top-1 0.476 top-3 0.807 knobs 0.464 |
| train | reverb full | 06:42 | 06:44 | 0:02 | n 6000; main MLP test top-1 0.467 top-3 0.808 knobs 0.481 |
| train | eq half | 06:44 | 06:46 | 0:01 | n 4000; main MLP test top-1 0.757 top-3 0.983 knobs 0.592 |
| train | eq full | 06:46 | 06:49 | 0:03 | n 8000; main MLP test top-1 0.824 top-3 0.995 knobs 0.625 |

## What went wrong

- Nothing recorded by the supervisor.

