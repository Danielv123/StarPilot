# Train a vehicle response model

This trains a local model from copied comma route logs. It learns a short-horizon response from recent car state plus commanded/actual actuator outputs to future steering angle, steering rate, steering torque, speed, acceleration, yaw rate, and lateral acceleration. It also trains intervention detectors for driver steering torque overlay while comma lateral control is active, raw `steeringPressed`, steering disengage, and any lateral intervention over the prediction horizon.

The default log root is:

```powershell
D:\comma_driving_logs\10.30.1.75\realdata
```

By default, the trainer only uses segments where `carParams.brand` is `hyundai` and the normalized `carParams.carFingerprint` contains `IONIQ5`. To intentionally train on another car, pass different `--brand` / `--car-fingerprint-contains` values, or pass empty strings to disable those filters.

Install or resolve the optional tooling with `uv`:

```powershell
uv add --optional tuning scikit-learn joblib --no-sync
```

On Windows, avoid syncing the full project if `xattr` fails to build. Run the trainer with transient packages instead:

```powershell
uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard python tools\tuning\train_vehicle_response_model.py train --max-segments 120 --validation-fraction 0.2
```

The default output is ignored by git:

```powershell
artifacts\tuning\vehicle_response\vehicle_response_model.joblib
artifacts\tuning\vehicle_response\metrics.json
artifacts\tuning\vehicle_response\validation_predictions.csv
```

Training prints extraction progress, the train/test sample split, response MAE/p95 error, and intervention accuracy, balanced accuracy, precision, recall, and F1. The default split is 80:20 by sample count while keeping whole segments in the held-out test set.

The main intervention label is `future_lateral_driver_torque_overlay`, defined as `carControl.latActive` plus `carState.steeringPressed` within the prediction horizon. You can also pass `--driver-torque-threshold <native-units>` to count large absolute `carState.steeringTorque` as overlay even when `steeringPressed` is not set.

For a time-series model, increase `--history-steps`. For example, `--history-steps 6 --history-step 2` gives the model the current feature row plus 5 prior rows spaced 2 logged samples apart. Rows at the start of a segment are dropped until enough history exists.

After selecting the history length on held-out data, add `--refit-all` to preserve the honest train/test metrics while refitting the saved artifact on every extracted sample:

```powershell
uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard python tools\tuning\train_vehicle_response_model.py train --history-steps 6 --history-step 2 --max-segments 0 --validation-fraction 0.2 --refit-all --output-dir artifacts\tuning\vehicle_response_time_series_h6
```

The metrics file records `artifact_refit_all` and `artifact_fit_samples` so it is clear whether the saved model was fit only on the training slice or refit on the complete dataset.

For a quick smoke test, use fewer segments and fewer samples:

```powershell
uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard python tools\tuning\train_vehicle_response_model.py train --max-segments 2 --max-samples-per-segment 1500 --max-iter 20
```

Evaluate an existing model on a separate segment slice:

```powershell
uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard python tools\tuning\train_vehicle_response_model.py evaluate --model artifacts\tuning\vehicle_response\vehicle_response_model.joblib --max-segments 20
```

Evaluation writes `evaluation_metrics.json` under `--output-dir`. Use repeated `--route-prefix` options for selected trips. To replay the exact validation set from a training run, pass its metrics file:

```powershell
uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard python tools\tuning\train_vehicle_response_model.py evaluate --model artifacts\tuning\vehicle_response_time_series_h3\vehicle_response_model.joblib --history-steps 3 --history-step 2 --max-segments 0 --segment-metrics artifacts\tuning\vehicle_response_time_series_h6\metrics.json --segment-list-key validation_segments --validation-csv-rows 0 --output-dir artifacts\tuning\comparison_h3
```

## Optimize the Ioniq 5 lateral profile

The optimizer replays bounded changes around the logged controller tune, updates the torque-related features across the model's full history window, and scores predicted lateral acceleration by phase. Entire segments are held out from the parameter search.

```powershell
uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard python tools\tuning\optimize_ioniq5_response_tune.py --model artifacts\tuning\vehicle_response_time_series_h6_20260721\vehicle_response_model.joblib --max-segments 40 --output artifacts\tuning\ioniq5_response_tune\optimization.json
```

This is a response approximation, not a full closed-loop simulator. Only adopt bounded changes that improve both the search and held-out segment scores, then verify them with new driving data.

## Controller-independent lateral plant model

For lateral-controller optimization, use the dedicated plant model. Unlike the legacy general response model, its predictor contains only applied steering torque and measured vehicle feedback. Desired-path values, controller error, P/I/D/F terms, controller state flags, and intervention labels are not predictor inputs.

Training is split by complete comma routes. A route is excluded in full when driver steering overlay is present in more than 50% of its lateral-active samples. Individual overlay samples are also excluded from otherwise usable routes.

```powershell
uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard python -u tools\tuning\train_lateral_plant_model.py --max-segments 0 --output-dir artifacts\tuning\lateral_plant_20260721
```

The model predicts 50 ms feedback-state deltas and is validated autoregressively through five steps, giving an aligned 250 ms plant rollout. Each predicted state is compared with the measured state at the same timestamp.

After training, optimize the Ioniq 5 controller through the same five-step rollout. The predicted lateral acceleration at each future step is compared with the desired lateral acceleration at that exact future timestamp.

```powershell
uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard python -u tools\tuning\optimize_ioniq5_closed_loop.py --model artifacts\tuning\lateral_plant_20260721\lateral_plant_model.joblib --output artifacts\tuning\ioniq5_closed_loop_20260721\optimization.json
```

## Neural lateral plant for goal-based NNFF training

Use the neural plant trainer when a controller will be optimized through the
plant with gradients. It searches both the temporal representation and network
capacity instead of fixing the original `0.6 s @ 20 Hz`,
`96 -> 128 -> 128 -> 64 -> 4` surrogate.

The search is split into controlled profiles:

- `initial` compares the original MLP with denser MLP and GRU candidates;
- `temporal` holds a one-layer, 40,228-parameter GRU fixed while sweeping
  `10`, `20`, and `50 ms` sample intervals and `0.5`, `1.0`, `1.5`, `2.0`,
  and `3.0` second histories;
- `architecture` compares MLP, GRU, dilated TCN, and Transformer candidates,
  including the previously selected 50 Hz GRU as a control.

Routes `00000109` and `0000010b` are untouched final holdouts by default.
Candidate selection uses other complete routes only. Training uses recursive
multi-step loss rather than one-step teacher forcing alone.

Run the search on a CUDA machine:

```bash
uv run --no-project --with torch --with joblib --with scikit-learn --with pycapnp==2.1.0 --with zstandard \
  python -u tools/tuning/train_neural_lateral_plant.py search \
  --search-profile temporal \
  --current-root /path/to/realdata \
  --output-dir artifacts/tuning/neural_lateral_plant_search
```

Run `temporal` first, then adjust or run the `architecture` profile at the
best temporal setting. A JSON candidate list can be supplied with
`--candidate-file` for additional controlled experiments.

Then train a three-member ensemble with the selected configuration. For
example, for a two-layer, 256-wide GRU using a dense two-second history:

```bash
uv run --no-project --with torch --with joblib --with scikit-learn --with pycapnp==2.1.0 --with zstandard \
  python -u tools/tuning/train_neural_lateral_plant.py train \
  --current-root /path/to/realdata \
  --family gru --sample-step 2 --history-steps 100 \
  --hidden-sizes 256 --gru-layers 2 \
  --output-dir artifacts/tuning/neural_lateral_plant_final
```

The ignored output directory contains:

- `neural_lateral_plant.pt`: model states, normalization, architecture, data
  inventory, route splits, and ensemble metadata;
- `training.json`: reviewable metrics without model tensors;
- `current_trajectories.joblib`: the reusable full-rate extraction cache.

The promoted model and its training report are checked in under
`artifacts/tuning/neural_lateral_plant_20260723/`. Intermediate checkpoints
and extraction caches remain ignored.

Downstream goal-based controller training should use the ensemble mean and
penalize or reject commands with high member disagreement. The helper
`load_ensemble_artifact()` reconstructs the members, and
`ensemble_predict_delta()` returns both mean response and disagreement.
For differentiable policy training, `ensemble_rollout()` returns the
autoregressive ensemble mean and disagreement across the full horizon.
This makes model uncertainty visible instead of allowing a controller to
silently exploit one surrogate's error.

Camera-only Pond archives cannot be used for this model. They do not contain
the steering torque, vehicle state, and controller messages required for
training; an archive must contain `rlog`, `rlog.zst`, or equivalent telemetry.

### 2026-07-23 architecture search

The search used 1,258 current-tire rlogs from 50 routes. Thirty routes had
clean windows for every candidate; four validation routes selected the
architecture, while routes `00000109` and `0000010b` remained untouched.
No older-data pretraining was performed because the available Pond archive
contained camera video rather than telemetry.

The fixed-capacity temporal sweep selected a `10 ms` sample interval with
`3.0 s` of history. The score is the weighted normalized error across the
complete two-second autoregressive rollout; lower is better.

| Sample interval | Best history | Best validation score |
|---:|---:|---:|
| 10 ms (100 Hz) | 3.0 s | **0.293978** |
| 20 ms (50 Hz) | 2.0 s | 0.307082 |
| 50 ms (20 Hz) | 3.0 s | 0.338630 |

At 100 Hz, the five tested histories scored between `0.293978` and
`0.298511`, so interval mattered more than the exact history length. Three
seconds was the measured winner, but the small margin over 1.5 seconds
(`0.294851`) should not be treated as a universal optimum outside this data
and rollout objective.

The architecture screen used the 100 Hz, 3.0-second history, except for the
previous 50 Hz GRU control. It used a shorter recursive training horizon and
1,000 validation windows to rank candidates before the expensive finalist
pass.

| Architecture | Parameters | Screen score |
|---|---:|---:|
| 7-block, 192-channel TCN | 1,590,916 | **0.347909** |
| 3-layer, 384-wide GRU | 2,377,348 | 0.363150 |
| 7-block, 128-channel TCN | 708,356 | 0.373436 |
| 4-layer, 192-wide Transformer | 1,876,996 | 0.392425 |
| 3-layer MLP | 1,624,324 | 0.394568 |
| 4-layer, 128-wide Transformer | 849,924 | 0.409778 |
| 2-layer, 192-wide GRU | 376,516 | 0.412320 |
| Previous 50 Hz GRU control | 376,516 | 0.424192 |
| 2-layer, 64-wide Transformer | 124,292 | 0.439513 |

The Transformers were valid trainable candidates, but none reached the TCN
or large-GRU screen scores. This sequence-prediction problem and dataset did
not reward attention enough to offset its weaker sample efficiency.

The two screen winners then received the same 0.5-second recursive training
loss and 20,000-window evaluation used for final selection:

| Finalist | Parameters | Validation score |
|---|---:|---:|
| 7-block, 192-channel TCN | 1,590,916 | **0.303054** |
| 3-layer, 384-wide GRU | 2,377,348 | 0.314029 |

Neither larger finalist beat the `0.299563` score from the earlier 50 Hz GRU
search, while the fixed-capacity 100 Hz temporal winner scored `0.293978`.
The temporal winner was therefore the only new candidate promoted to
three-seed ensemble training.

#### Initial architecture search

| Candidate | History | Parameters | Validation score |
|---|---:|---:|---:|
| 1.5 s GRU, 50 Hz | 1.5 s | 376,516 | **0.299563** |
| 1.5 s MLP, 50 Hz | 1.5 s | 472,452 | 0.300110 |
| 1.0 s MLP, 50 Hz | 1.0 s | 201,860 | 0.300581 |
| 2.0 s large MLP, 50 Hz | 2.0 s | 837,508 | 0.305101 |
| 2.0 s GRU, 50 Hz | 2.0 s | 665,860 | 0.305229 |
| Original-shape MLP, 20 Hz | 0.6 s | 37,444 | 0.352507 |

That search initially selected the 1.5-second GRU. The expanded search
superseded it with the 100 Hz, 3.0-second, one-layer GRU. The final
three-seed ensemble has 40,228 parameters per member and 120,684 parameters
in total. It scored `0.279085` across 68,950 validation windows. On the
previously untouched `00000109` and `0000010b` routes, it scored `0.339295`
across 38,736 windows.

The superseded 50 Hz ensemble scored `0.293589` on validation and `0.351401`
on the same untouched routes. The denser temporal ensemble therefore
improved the normalized score by 4.9% on validation and 3.4% on the holdout,
despite using fewer weights. The 3.0-second recurrent computation remains
substantially richer than the old 1.5-second input even though parameter
count alone is lower.

For a route-matched reference, a separately trained three-seed ensemble using
the original 37,444-parameter shape scored `0.334715` on validation and
`0.376804` on the holdout. The final temporal ensemble improved those scores
by 16.6% and 10.0%, respectively. Window counts differ because the final
model retains 100 Hz samples while the reference retains 20 Hz samples.
