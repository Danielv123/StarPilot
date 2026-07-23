#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

try:
  from sklearn.metrics import mean_absolute_error, root_mean_squared_error
  from sklearn.neural_network import MLPRegressor
except ModuleNotFoundError as e:
  raise SystemExit(
    "Run with: uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 "
    + "--with zstandard python tools/tuning/train_ioniq5_nnff.py"
  ) from e

from openpilot.tools.tuning import train_lateral_plant_model as plant_data
from openpilot.tools.tuning import train_vehicle_response_model as log_data


DEFAULT_ROOT = Path(r"D:\comma_driving_logs\10.30.1.75\realdata")
DEFAULT_MODEL = REPO_ROOT / "starpilot/assets/nnff_models/HYUNDAI_IONIQ_5.json"
DEFAULT_REPORT = REPO_ROOT / "artifacts/tuning/ioniq5_nnff_20260723/training.json"
PAST_OFFSETS = (-0.3, -0.2, -0.1)
FUTURE_OFFSETS = (0.4, 0.7, 1.1, 1.6)  # Includes the Ioniq 5's nominal 0.1 s actuator delay.
INPUT_VARS = (
  "v_ego",
  "lateral_accel",
  "friction_input",
  "roll",
  "lateral_accel_m03",
  "lateral_accel_m02",
  "lateral_accel_m01",
  "lateral_accel_p03",
  "lateral_accel_p06",
  "lateral_accel_p10",
  "lateral_accel_p15",
  "roll_m03",
  "roll_m02",
  "roll_m01",
  "roll_p03",
  "roll_p06",
  "roll_p10",
  "roll_p15",
)


def route_selected(route: str, prefixes: list[str]) -> bool:
  return any(route.startswith(prefix) for prefix in prefixes)


def trajectory_rows(trajectory: plant_data.Trajectory) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray]]:
  times = trajectory.times
  values = trajectory.values
  if len(times) < 40:
    return np.empty((0, len(INPUT_VARS)), dtype=np.float32), np.empty(0, dtype=np.float32), {}

  dt = float(np.median(np.diff(times)))
  offsets = [int(round(offset / dt)) for offset in (*PAST_OFFSETS, *FUTURE_OFFSETS)]
  lower = max(0, -min(offsets))
  upper = len(times) - max(offsets)
  source = np.arange(lower, upper)
  if not len(source):
    return np.empty((0, len(INPUT_VARS)), dtype=np.float32), np.empty(0, dtype=np.float32), {}

  sampled = np.stack([source + offset for offset in offsets], axis=1)
  all_indexes = np.column_stack((source, sampled))
  clean = (
    (values["lat_active"][all_indexes].min(axis=1) > 0.5)
    & (values["driver_overlay"][all_indexes].max(axis=1) < 0.5)
    & (values["saturated"][all_indexes].max(axis=1) < 0.5)
    & (values["v_ego"][source] >= 5.0)
    & ((times[source + max(offsets)] - times[source + min(offsets)]) < 2.1)
  )
  source = source[clean]
  sampled = sampled[clean]
  if not len(source):
    return np.empty((0, len(INPUT_VARS)), dtype=np.float32), np.empty(0, dtype=np.float32), {}

  desired = values["desired_lateral_accel"]
  jerk = values["desired_lateral_jerk"]
  friction_input = 0.7 * (desired[source] - values["actual_lateral_accel"][source]) + 0.4 * jerk[source]
  past_future = desired[sampled]
  # liveParameters.roll is effectively zero in these qlogs. Preserve the runtime schema and let
  # the controller's learned stock-model error response handle bank compensation.
  rolls = np.zeros((len(source), 1 + len(offsets)), dtype=np.float32)
  x = np.column_stack((
    values["v_ego"][source],
    desired[source],
    friction_input,
    rolls[:, 0],
    past_future,
    rolls[:, 1:],
  )).astype(np.float32)
  # LatControlNNFF negates its model/PID output before sending torque to carControl.
  y = (-values["applied_torque"][source]).astype(np.float32)
  diagnostics = {
    "desired": desired[source],
    "jerk": jerk[source],
    "future_1p6": past_future[:, -1],
  }
  finite = np.isfinite(x).all(axis=1) & np.isfinite(y)
  return x[finite], y[finite], {name: array[finite] for name, array in diagnostics.items()}


def load_rows(root: Path, prefixes: list[str], sample_step: int) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray], list[str]]:
  paths = log_data.discover_log_files(root, "rlog", None, prefixes, None)
  xs: list[np.ndarray] = []
  ys: list[np.ndarray] = []
  diagnostics: dict[str, list[np.ndarray]] = {"desired": [], "jerk": [], "future_1p6": []}
  segments: list[str] = []
  for index, path in enumerate(paths, 1):
    trajectory = plant_data.read_trajectory(path, "hyundai", "IONIQ5", sample_step)
    if trajectory is None or not route_selected(trajectory.route, prefixes):
      continue
    x, y, diag = trajectory_rows(trajectory)
    if len(x):
      xs.append(x)
      ys.append(y)
      segments.append(trajectory.segment)
      for name in diagnostics:
        diagnostics[name].append(diag[name])
    if index % 20 == 0 or index == len(paths):
      print(f"loaded {index}/{len(paths)} NNFF segments; retained rows={sum(map(len, xs))}", flush=True)
  if not xs:
    raise SystemExit(f"No clean NNFF rows matched: {', '.join(prefixes)}")
  return (
    np.concatenate(xs),
    np.concatenate(ys),
    {name: np.concatenate(parts) for name, parts in diagnostics.items()},
    segments,
  )


def flux_predict(payload: dict[str, Any], x: np.ndarray) -> np.ndarray:
  mean = np.asarray(payload["input_mean"], dtype=np.float64).T
  std = np.asarray(payload["input_std"], dtype=np.float64).T
  output = (x - mean) / std
  for layer in payload["layers"]:
    weights = np.asarray(next(value for key, value in layer.items() if key.endswith("_W")), dtype=np.float64).T
    bias = np.asarray(next(value for key, value in layer.items() if key.endswith("_b")), dtype=np.float64).T
    output = output @ weights + bias
    if layer["activation"] != "identity":
      output = 1.0 / (1.0 + np.exp(-np.clip(output, -40.0, 40.0)))
  return output[:, 0]


def export_flux(model: MLPRegressor, mean: np.ndarray, std: np.ndarray, validation_loss: float,
                train_rows: int, validation_rows: int) -> dict[str, Any]:
  layers = []
  for index, (weights, bias) in enumerate(zip(model.coefs_, model.intercepts_, strict=True), 1):
    layers.append({
      f"dense_{index}_W": weights.T.tolist(),
      f"dense_{index}_b": bias[:, None].tolist(),
      "activation": "identity" if index == len(model.coefs_) else "sigmoid",
    })
  return {
    "input_std": std[:, None].tolist(),
    "model_test_loss": validation_loss,
    "input_size": len(INPUT_VARS),
    "current_date_and_time": datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S"),
    "input_mean": mean[:, None].tolist(),
    "input_vars": list(INPUT_VARS),
    "output_size": 1,
    "training_car": "HYUNDAI_IONIQ_5",
    "training_rows": train_rows,
    "validation_rows": validation_rows,
    "layers": layers,
  }


def metrics(y: np.ndarray, prediction: np.ndarray, diagnostics: dict[str, np.ndarray]) -> dict[str, Any]:
  masks = {
    "all": np.ones(len(y), dtype=bool),
    "transition": np.abs(diagnostics["jerk"]) >= 0.10,
    "approaching_straight": (
      (np.abs(diagnostics["desired"]) >= 0.15)
      & (np.abs(diagnostics["future_1p6"]) < 0.08)
    ),
  }
  result: dict[str, Any] = {}
  for name, mask in masks.items():
    if not np.any(mask):
      continue
    error = prediction[mask] - y[mask]
    result[name] = {
      "samples": int(mask.sum()),
      "rmse": float(root_mean_squared_error(y[mask], prediction[mask])),
      "mae": float(mean_absolute_error(y[mask], prediction[mask])),
      "bias": float(np.mean(error)),
    }
  return result


def main() -> None:
  parser = argparse.ArgumentParser(description="Train an 18-input Ioniq 5 NNFF from local comma logs.")
  parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
  parser.add_argument("--train-route-prefix", action="append", required=True)
  parser.add_argument("--validation-route-prefix", action="append", required=True)
  parser.add_argument("--sample-step", type=int, default=5)
  parser.add_argument("--model-output", type=Path, default=DEFAULT_MODEL)
  parser.add_argument("--report-output", type=Path, default=DEFAULT_REPORT)
  parser.add_argument("--random-state", type=int, default=23)
  args = parser.parse_args()

  train_x, train_y, train_diag, train_segments = load_rows(args.root, args.train_route_prefix, args.sample_step)
  validation_x, validation_y, validation_diag, validation_segments = load_rows(
    args.root, args.validation_route_prefix, args.sample_step,
  )
  mean = train_x.mean(axis=0, dtype=np.float64)
  std = train_x.std(axis=0, dtype=np.float64)
  std[std < 1e-4] = 1.0
  scaled_train = (train_x - mean) / std
  scaled_validation = (validation_x - mean) / std

  model = MLPRegressor(
    hidden_layer_sizes=(24, 12, 6),
    activation="logistic",
    solver="adam",
    alpha=2e-4,
    batch_size=512,
    learning_rate_init=2e-3,
    max_iter=500,
    early_stopping=True,
    validation_fraction=0.15,
    n_iter_no_change=30,
    random_state=args.random_state,
    verbose=True,
  )
  model.fit(scaled_train, train_y)
  custom_prediction = model.predict(scaled_validation)
  stock_payload = json.loads(DEFAULT_MODEL.read_text(encoding="utf-8"))
  stock_prediction = flux_predict(stock_payload, validation_x)
  payload = export_flux(
    model, mean, std, float(root_mean_squared_error(validation_y, custom_prediction)),
    len(train_y), len(validation_y),
  )
  exported_prediction = flux_predict(payload, validation_x)
  np.testing.assert_allclose(custom_prediction, exported_prediction, rtol=2e-5, atol=2e-5)

  report = {
    "train_route_prefixes": args.train_route_prefix,
    "validation_route_prefixes": args.validation_route_prefix,
    "train_segments": train_segments,
    "validation_segments": validation_segments,
    "train_rows": len(train_y),
    "validation_rows": len(validation_y),
    "input_vars": list(INPUT_VARS),
    "future_path_offsets_s": list(FUTURE_OFFSETS),
    "stock": metrics(validation_y, stock_prediction, validation_diag),
    "custom": metrics(validation_y, custom_prediction, validation_diag),
    "custom_train": metrics(train_y, model.predict(scaled_train), train_diag),
  }
  args.model_output.parent.mkdir(parents=True, exist_ok=True)
  args.report_output.parent.mkdir(parents=True, exist_ok=True)
  args.model_output.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
  args.report_output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(json.dumps({"stock": report["stock"], "custom": report["custom"]}, indent=2))
  print(f"model: {args.model_output}")
  print(f"report: {args.report_output}")


if __name__ == "__main__":
  main()
