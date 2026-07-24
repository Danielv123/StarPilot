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

Routes `00000109` and `0000010b` are a route-separated evaluation cohort by
default.
Routes with more than 50% driver-torque overlay are excluded before any
cohort is selected. Candidate selection uses other complete routes only.
Training uses recursive multi-step loss rather than one-step teacher forcing
alone. The bounded early-stopping evaluation uses a deterministic random
sample across the full validation window set rather than its first rows.

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
`--candidate-file` for additional controlled experiments. Each profile writes
a distinct report such as `temporal_search.json` or
`architecture_search.json`; a candidate file named `finalists.json` writes
`finalists_search.json`.

Then train a three-member ensemble with the selected configuration. For
the final comparison and training run, pass the preceding report through
`--split-report`. The trainer verifies the data inventory and reuses its exact
training, validation, and holdout route cohorts. For example, for a two-layer,
256-wide GRU using a dense two-second history:

```bash
uv run --no-project --with torch --with joblib --with scikit-learn --with pycapnp==2.1.0 --with zstandard \
  python -u tools/tuning/train_neural_lateral_plant.py train \
  --current-root /path/to/realdata \
  --split-report artifacts/tuning/neural_lateral_plant_search/architecture_search.json \
  --family gru --sample-step 2 --history-steps 100 \
  --hidden-sizes 256 --gru-layers 2 \
  --output-dir artifacts/tuning/neural_lateral_plant_final
```

The ignored output directory contains:

- `neural_lateral_plant.pt`: model states, normalization, architecture, data
  inventory, route splits, and ensemble metadata;
- `training.json`: reviewable metrics without model tensors;
- `current_trajectories.joblib`: the reusable full-rate extraction cache.

The promoted self-contained model is checked in under
`artifacts/tuning/neural_lateral_plant_20260723/`. It includes its architecture,
normalization, route splits, and evaluation metadata. Search reports, rejected
candidates, intermediate checkpoints, extraction caches, and duplicate JSON
exports remain ignored. The promoted `.pt` is stored as a normal Git blob;
Git LFS is not used.

A downstream policy integration should use the ensemble mean and penalize or
reject commands with high member disagreement. The helper
`load_ensemble_artifact()` reconstructs the members, and
`ensemble_predict_delta()` returns both mean response and disagreement.
For differentiable policy training, `ensemble_rollout()` returns the
autoregressive ensemble mean and disagreement across the full horizon.
This makes model uncertainty visible instead of allowing a controller to
silently exploit one surrogate's error.

On CUDA, load a GRU plant with
`load_ensemble_artifact(..., differentiable=True)` for downstream policy
training. This keeps the plant weights frozen while enabling cuDNN to retain
the state needed for gradients through the rollout.

The current `train_goal_based_ioniq5_nnff.py` command still consumes the older
joblib plant format and cannot load this `.pt` artifact directly. Wiring this
ensemble and its disagreement penalty into that trainer is separate follow-up
work.

Camera-only Pond archives cannot be used for this model. They do not contain
the steering torque, vehicle state, and controller messages required for
training; an archive must contain raw `rlog`, `rlog.zst`, `rlog.bz2`, or
equivalent telemetry.

### Promoted Ioniq 5 plant

The checked-in artifact was trained from 1,258 current-tire rlogs. Routes with
more than 50% driver overlay were excluded, and routes `00000109` and
`0000010b` were excluded from fitting and used as an acceptance cohort.

The promoted three-member GRU ensemble uses 100 Hz samples and three seconds
of history. It has 40,228 parameters per member and 120,684 in total. It
scored `0.370714` over 20,000 validation windows and `0.407723` over 30,000
acceptance-cohort windows. Because that cohort was used to choose this GRU
over the validation-selected TCN, the latter score is selection-biased and is
not an unbiased holdout estimate.
