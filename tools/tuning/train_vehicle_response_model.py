#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bz2
import csv
import json
import math
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import zstandard as zstd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

try:
  from joblib import dump, load
  from sklearn.dummy import DummyClassifier
  from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
  from sklearn.metrics import accuracy_score, mean_absolute_error, precision_recall_fscore_support, root_mean_squared_error
  from sklearn.multioutput import MultiOutputRegressor
except ModuleNotFoundError as e:
  missing = e.name or str(e)
  raise SystemExit(
    f"Missing dependency {missing!r}. Run with:\n"
    "  uv sync --extra tuning\n"
    "or:\n"
    "  uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard "
    "python tools/tuning/train_vehicle_response_model.py ...\n"
  ) from e

try:
  import capnp
  from cereal import log as capnp_log
except ModuleNotFoundError as e:
  missing = e.name or str(e)
  raise SystemExit(
    f"Missing dependency {missing!r}. Run with:\n"
    "  uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard "
    "python tools/tuning/train_vehicle_response_model.py ...\n"
  ) from e


DEFAULT_LOG_ROOT = Path(r"D:\comma_driving_logs\10.30.1.75\realdata")
DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts" / "tuning" / "vehicle_response"


FEATURE_NAMES = [
  "car_control_enabled",
  "car_control_lat_active",
  "car_control_long_active",
  "cmd_torque",
  "cmd_steering_angle_deg",
  "cmd_curvature",
  "cmd_accel",
  "cmd_long_control_state",
  "cmd_output_torque",
  "cmd_output_steering_angle_deg",
  "cmd_output_curvature",
  "cmd_output_accel",
  "cmd_output_gas",
  "cmd_output_brake",
  "cmd_output_speed",
  "v_ego",
  "a_ego",
  "v_ego_raw",
  "v_ego_cluster",
  "yaw_rate",
  "steering_angle_deg",
  "steering_rate_deg",
  "steering_torque",
  "steering_torque_eps",
  "steering_pressed",
  "steering_disengage",
  "gas_pressed",
  "brake_pressed",
  "standstill",
  "left_blinker",
  "right_blinker",
  "controls_curvature",
  "controls_desired_curvature",
  "controls_up_accel_cmd",
  "controls_ui_accel_cmd",
  "controls_uf_accel_cmd",
  "controls_long_control_state",
  "torque_state_active",
  "torque_state_error",
  "torque_state_error_rate",
  "torque_state_p",
  "torque_state_i",
  "torque_state_d",
  "torque_state_f",
  "torque_state_output",
  "torque_state_saturated",
  "actual_lateral_accel",
  "desired_lateral_accel",
  "desired_lateral_jerk",
]

TARGET_NAMES = [
  "future_steering_angle_deg",
  "future_steering_rate_deg",
  "future_steering_torque",
  "future_steering_torque_eps",
  "future_v_ego",
  "future_a_ego",
  "future_yaw_rate",
  "future_actual_lateral_accel",
]

TARGET_VALUE_KEYS = [
  "steering_angle_deg",
  "steering_rate_deg",
  "steering_torque",
  "steering_torque_eps",
  "v_ego",
  "a_ego",
  "yaw_rate",
  "actual_lateral_accel",
]

INTERVENTION_TARGET_NAMES = [
  "future_lateral_driver_torque_overlay",
  "future_steering_pressed",
  "future_steering_disengage",
  "future_any_lateral_intervention",
]

INTERVENTION_VALUE_KEYS = [
  "lateral_driver_torque_overlay",
  "steering_pressed",
  "steering_disengage",
  "any_lateral_intervention",
]


@dataclass
class SegmentExamples:
  segment: str
  brand: str
  car_fingerprint: str
  x: np.ndarray
  y: np.ndarray
  interventions: np.ndarray
  times: np.ndarray
  future_times: np.ndarray


def as_float(value: Any, default: float = math.nan) -> float:
  try:
    return float(value)
  except (TypeError, ValueError):
    return default


def bool_float(value: Any) -> float:
  try:
    return 1.0 if bool(value) else 0.0
  except Exception:
    return math.nan


def enum_float(value: Any) -> float:
  if value is None:
    return math.nan
  try:
    return float(int(value))
  except (TypeError, ValueError):
    pass
  text = str(value)
  if "." in text:
    text = text.rsplit(".", 1)[-1]
  stable = {
    "off": 0.0,
    "pid": 1.0,
    "stopping": 2.0,
    "starting": 3.0,
  }
  return stable.get(text, math.nan)


def nested(obj: Any, *names: str) -> Any | None:
  cur = obj
  for name in names:
    if cur is None:
      return None
    try:
      cur = getattr(cur, name)
    except Exception:
      return None
  return cur


def normalize_identity_text(value: str) -> str:
  return "".join(ch for ch in value.upper() if ch.isalnum())


def identity_matches(brand: str, car_fingerprint: str, brand_filter: str | None, fingerprint_contains: str | None) -> bool:
  if brand_filter and normalize_identity_text(brand) != normalize_identity_text(brand_filter):
    return False
  if fingerprint_contains and normalize_identity_text(fingerprint_contains) not in normalize_identity_text(car_fingerprint):
    return False
  return True


def expanded_feature_names(history_steps: int, history_step: int) -> list[str]:
  names = []
  for history_idx in range(history_steps):
    sample_lag = history_idx * history_step
    suffix = "t" if sample_lag == 0 else f"t_minus_{sample_lag}"
    names.extend(f"{name}_{suffix}" for name in FEATURE_NAMES)
  return names


def discover_log_files(
  root: Path,
  log_type: str,
  max_segments: int | None,
  route_prefixes: list[str],
  segment_names: set[str] | None = None,
) -> list[Path]:
  candidates = sorted({
    path
    for filename in (log_type, f"{log_type}.zst", f"{log_type}.bz2")
    for path in root.glob(f"*/{filename}")
    if path.is_file()
  })
  if route_prefixes:
    candidates = [p for p in candidates if any(p.parent.name.startswith(prefix) for prefix in route_prefixes)]
  if segment_names is not None:
    candidates = [p for p in candidates if p.parent.name in segment_names]
  candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
  if max_segments is not None:
    candidates = candidates[:max_segments]
  return candidates


def decompress_log_bytes(path: Path) -> bytes:
  data = path.read_bytes()
  if path.suffix == ".bz2" or data.startswith(b"BZh9"):
    return bz2.decompress(data)
  if path.suffix == ".zst" or data.startswith(b"\x28\xB5\x2F\xFD"):
    dctx = zstd.ZstdDecompressor()
    with dctx.stream_reader(data) as reader:
      return reader.read()
  return data


def iter_log_messages(path: Path) -> list[Any]:
  data = decompress_log_bytes(path)
  messages = []
  try:
    for event in capnp_log.Event.read_multiple_bytes(data):
      messages.append(event)
  except capnp.KjException:
    pass
  messages.sort(key=lambda event: event.logMonoTime)
  return messages


def stream_log_messages(path: Path) -> Iterator[Any]:
  """Yield events in their recorded order without materializing and sorting a full rlog."""
  data = decompress_log_bytes(path)
  try:
    yield from capnp_log.Event.read_multiple_bytes(data)
  except capnp.KjException:
    return


def get_feature_row(latest: dict[str, Any]) -> list[float]:
  car_control = latest.get("carControl")
  car_state = latest.get("carState")
  car_output = latest.get("carOutput")
  controls_state = latest.get("controlsState")
  actuators = nested(car_control, "actuators")
  actuators_output = nested(car_output, "actuatorsOutput")

  torque_state = None
  lateral_state = nested(controls_state, "lateralControlState")
  if lateral_state is not None:
    try:
      if lateral_state.which() == "torqueState":
        torque_state = lateral_state.torqueState
    except Exception:
      torque_state = None

  return [
    bool_float(nested(car_control, "enabled")),
    bool_float(nested(car_control, "latActive")),
    bool_float(nested(car_control, "longActive")),
    as_float(nested(actuators, "torque")),
    as_float(nested(actuators, "steeringAngleDeg")),
    as_float(nested(actuators, "curvature")),
    as_float(nested(actuators, "accel")),
    enum_float(nested(actuators, "longControlState")),
    as_float(nested(actuators_output, "torque")),
    as_float(nested(actuators_output, "steeringAngleDeg")),
    as_float(nested(actuators_output, "curvature")),
    as_float(nested(actuators_output, "accel")),
    as_float(nested(actuators_output, "gas")),
    as_float(nested(actuators_output, "brake")),
    as_float(nested(actuators_output, "speed")),
    as_float(nested(car_state, "vEgo")),
    as_float(nested(car_state, "aEgo")),
    as_float(nested(car_state, "vEgoRaw")),
    as_float(nested(car_state, "vEgoCluster")),
    as_float(nested(car_state, "yawRate")),
    as_float(nested(car_state, "steeringAngleDeg")),
    as_float(nested(car_state, "steeringRateDeg")),
    as_float(nested(car_state, "steeringTorque")),
    as_float(nested(car_state, "steeringTorqueEps")),
    bool_float(nested(car_state, "steeringPressed")),
    bool_float(nested(car_state, "steeringDisengage")),
    bool_float(nested(car_state, "gasPressed")),
    bool_float(nested(car_state, "brakePressed")),
    bool_float(nested(car_state, "standstill")),
    bool_float(nested(car_state, "leftBlinker")),
    bool_float(nested(car_state, "rightBlinker")),
    as_float(nested(controls_state, "curvature")),
    as_float(nested(controls_state, "desiredCurvature")),
    as_float(nested(controls_state, "upAccelCmd")),
    as_float(nested(controls_state, "uiAccelCmd")),
    as_float(nested(controls_state, "ufAccelCmd")),
    enum_float(nested(controls_state, "longControlState")),
    bool_float(nested(torque_state, "active")),
    as_float(nested(torque_state, "error")),
    as_float(nested(torque_state, "errorRate")),
    as_float(nested(torque_state, "p")),
    as_float(nested(torque_state, "i")),
    as_float(nested(torque_state, "d")),
    as_float(nested(torque_state, "f")),
    as_float(nested(torque_state, "output")),
    bool_float(nested(torque_state, "saturated")),
    as_float(nested(torque_state, "actualLateralAccel")),
    as_float(nested(torque_state, "desiredLateralAccel")),
    as_float(nested(torque_state, "desiredLateralJerk")),
  ]


def get_target_values(latest: dict[str, Any]) -> list[float]:
  car_state = latest.get("carState")
  controls_state = latest.get("controlsState")
  torque_state = None
  lateral_state = nested(controls_state, "lateralControlState")
  if lateral_state is not None:
    try:
      if lateral_state.which() == "torqueState":
        torque_state = lateral_state.torqueState
    except Exception:
      torque_state = None

  return [
    as_float(nested(car_state, "steeringAngleDeg")),
    as_float(nested(car_state, "steeringRateDeg")),
    as_float(nested(car_state, "steeringTorque")),
    as_float(nested(car_state, "steeringTorqueEps")),
    as_float(nested(car_state, "vEgo")),
    as_float(nested(car_state, "aEgo")),
    as_float(nested(car_state, "yawRate")),
    as_float(nested(torque_state, "actualLateralAccel")),
  ]


def get_intervention_values(latest: dict[str, Any], driver_torque_threshold: float) -> list[int]:
  car_state = latest.get("carState")
  car_control = latest.get("carControl")
  lat_active = bool(nested(car_control, "latActive"))
  steering_torque = abs(as_float(nested(car_state, "steeringTorque"), 0.0))
  steering_pressed = int(bool(nested(car_state, "steeringPressed")))
  steering_disengage = int(bool(nested(car_state, "steeringDisengage")))
  torque_threshold_hit = driver_torque_threshold > 0.0 and steering_torque >= driver_torque_threshold
  lateral_driver_torque_overlay = int(lat_active and bool(steering_pressed or torque_threshold_hit))
  any_lateral_intervention = int(bool(lateral_driver_torque_overlay or steering_disengage))
  return [lateral_driver_torque_overlay, steering_pressed, steering_disengage, any_lateral_intervention]


def build_history_features(x_arr: np.ndarray, source_idxs: np.ndarray, history_steps: int, history_step: int) -> np.ndarray:
  offsets = np.arange(history_steps, dtype=np.int64) * history_step
  history_idxs = source_idxs[:, None] - offsets[None, :]
  return x_arr[history_idxs].reshape(source_idxs.shape[0], history_steps * x_arr.shape[1])


def read_segment_examples(
  log_file: Path,
  horizon_s: float,
  stride: int,
  max_samples: int | None,
  brand_filter: str | None,
  fingerprint_contains: str | None,
  driver_torque_threshold: float,
  history_steps: int,
  history_step: int,
) -> SegmentExamples | None:
  latest: dict[str, Any] = {}
  brand = ""
  car_fingerprint = ""
  identity_accepted = brand_filter is None and fingerprint_contains is None
  times: list[float] = []
  features: list[list[float]] = []
  targets: list[list[float]] = []
  interventions: list[list[int]] = []

  for msg in iter_log_messages(log_file):
    try:
      which = msg.which()
    except Exception:
      continue
    if which == "carParams":
      brand = str(nested(msg.carParams, "brand") or "")
      car_fingerprint = str(nested(msg.carParams, "carFingerprint") or "")
      identity_accepted = identity_matches(brand, car_fingerprint, brand_filter, fingerprint_contains)
      if not identity_accepted:
        return None
      continue

    if which not in ("carState", "carControl", "carOutput", "controlsState"):
      continue
    if not identity_accepted:
      continue

    latest[which] = getattr(msg, which)
    if which != "carState":
      continue

    target = get_target_values(latest)
    if not np.all(np.isfinite(target[:5])):
      continue

    times.append(msg.logMonoTime / 1e9)
    features.append(get_feature_row(latest))
    targets.append(target)
    interventions.append(get_intervention_values(latest, driver_torque_threshold))

  if len(times) < 2:
    return None

  time_arr = np.asarray(times, dtype=np.float64)
  x_arr = np.asarray(features, dtype=np.float32)
  target_values = np.asarray(targets, dtype=np.float32)
  intervention_values = np.asarray(interventions, dtype=np.int8)
  future_idxs = np.searchsorted(time_arr, time_arr + horizon_s, side="left")
  source_idxs = np.arange(len(time_arr))
  valid = future_idxs < len(time_arr)
  source_idxs = source_idxs[valid]
  future_idxs = future_idxs[valid]
  min_source_idx = (history_steps - 1) * history_step
  valid_history = source_idxs >= min_source_idx
  source_idxs = source_idxs[valid_history]
  future_idxs = future_idxs[valid_history]

  if stride > 1:
    source_idxs = source_idxs[::stride]
    future_idxs = future_idxs[::stride]
  if max_samples is not None and len(source_idxs) > max_samples:
    source_idxs = source_idxs[:max_samples]
    future_idxs = future_idxs[:max_samples]

  y_arr = target_values[future_idxs]
  z_arr = np.asarray([
    np.max(intervention_values[source_idx:future_idx + 1], axis=0)
    for source_idx, future_idx in zip(source_idxs, future_idxs, strict=True)
  ], dtype=np.int8)
  y_valid = np.all(np.isfinite(y_arr), axis=1)
  source_idxs = source_idxs[y_valid]
  future_idxs = future_idxs[y_valid]
  z_arr = z_arr[y_valid]
  if len(source_idxs) == 0:
    return None

  return SegmentExamples(
    segment=log_file.parent.name,
    brand=brand,
    car_fingerprint=car_fingerprint,
    x=build_history_features(x_arr, source_idxs, history_steps, history_step),
    y=y_arr[y_valid],
    interventions=z_arr,
    times=time_arr[source_idxs],
    future_times=time_arr[future_idxs],
  )


def load_examples(args: argparse.Namespace) -> list[SegmentExamples]:
  root = Path(args.root)
  segment_names = None
  if args.segment_metrics is not None:
    segment_payload = json.loads(Path(args.segment_metrics).read_text(encoding="utf-8"))
    selected_segments = segment_payload.get(args.segment_list_key)
    if not isinstance(selected_segments, list) or not selected_segments:
      raise SystemExit(f"No non-empty list named {args.segment_list_key!r} in {args.segment_metrics}")
    segment_names = {str(segment) for segment in selected_segments}
    print(f"selecting {len(segment_names)} segments from {args.segment_metrics}:{args.segment_list_key}")
  log_files = discover_log_files(root, args.log_type, args.max_segments, args.route_prefix, segment_names)
  if not log_files:
    raise SystemExit(f"No {args.log_type}.zst files found under {root}")

  examples: list[SegmentExamples] = []
  started = perf_counter()
  for i, log_file in enumerate(log_files, start=1):
    try:
      segment_examples = read_segment_examples(
        log_file,
        args.horizon_s,
        args.stride,
        args.max_samples_per_segment,
        args.brand,
        args.car_fingerprint_contains,
        args.driver_torque_threshold,
        args.history_steps,
        args.history_step,
      )
    except Exception as e:
      print(f"skip {log_file.parent.name}: {e}", file=sys.stderr)
      continue
    if segment_examples is not None:
      examples.append(segment_examples)
    if i % 10 == 0 or i == len(log_files):
      sample_count = sum(e.x.shape[0] for e in examples)
      print(f"loaded {i}/{len(log_files)} segments, usable={len(examples)}, samples={sample_count}")

  if not examples:
    raise SystemExit("No usable training samples found.")
  vehicle_counts: dict[tuple[str, str], int] = {}
  for example in examples:
    key = (example.brand or "unknown", example.car_fingerprint or "unknown")
    vehicle_counts[key] = vehicle_counts.get(key, 0) + 1
  print("vehicle segments:")
  for (brand, car_fingerprint), count in sorted(vehicle_counts.items(), key=lambda item: (-item[1], item[0])):
    print(f"  {count:4d}  brand={brand}  carFingerprint={car_fingerprint}")
  print(f"extracted {sum(e.x.shape[0] for e in examples)} samples from {len(examples)} segments in {perf_counter() - started:.1f}s")
  return examples


def split_examples(examples: list[SegmentExamples], validation_fraction: float) -> tuple[list[SegmentExamples], list[SegmentExamples]]:
  if len(examples) == 1 or validation_fraction <= 0:
    return examples, examples
  total_samples = sum(example.x.shape[0] for example in examples)
  target_validation_samples = total_samples * validation_fraction
  validation_count = 1
  running_samples = 0
  best_diff = float("inf")
  for i in range(1, len(examples)):
    running_samples += examples[i - 1].x.shape[0]
    diff = abs(running_samples - target_validation_samples)
    if diff <= best_diff:
      validation_count = i
      best_diff = diff
  validation_count = min(validation_count, len(examples) - 1)
  return examples[validation_count:], examples[:validation_count]


def stack_examples(examples: list[SegmentExamples]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
  return np.vstack([e.x for e in examples]), np.vstack([e.y for e in examples]), np.vstack([e.interventions for e in examples])


def evaluate_model(model: Any, x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
  pred = model.predict(x)
  metrics: dict[str, Any] = {
    "samples": int(y.shape[0]),
    "overall_mae": float(mean_absolute_error(y, pred)),
    "overall_rmse": float(root_mean_squared_error(y, pred)),
    "targets": {},
  }
  for i, target_name in enumerate(TARGET_NAMES):
    metrics["targets"][target_name] = {
      "mae": float(mean_absolute_error(y[:, i], pred[:, i])),
      "rmse": float(root_mean_squared_error(y[:, i], pred[:, i])),
      "p95_abs_error": float(np.percentile(np.abs(y[:, i] - pred[:, i]), 95)),
    }
  return metrics


def fit_intervention_models(x: np.ndarray, z: np.ndarray, args: argparse.Namespace) -> list[Any]:
  models = []
  for i, target_name in enumerate(INTERVENTION_TARGET_NAMES):
    values = z[:, i]
    if np.unique(values).shape[0] < 2:
      model = DummyClassifier(strategy="constant", constant=int(values[0]))
    else:
      model = HistGradientBoostingClassifier(
        max_iter=args.intervention_max_iter,
        learning_rate=args.learning_rate,
        max_leaf_nodes=args.max_leaf_nodes,
        l2_regularization=args.l2_regularization,
        random_state=args.random_state,
      )
    started = perf_counter()
    model.fit(x, values)
    print(f"  intervention {target_name:28s} positives={int(values.sum()):7d}/{len(values):7d} fit={perf_counter() - started:.1f}s")
    models.append(model)
  return models


def predict_interventions(models: list[Any], x: np.ndarray) -> np.ndarray:
  return np.column_stack([model.predict(x).astype(np.int8) for model in models])


def evaluate_intervention_models(models: list[Any], x: np.ndarray, z: np.ndarray) -> dict[str, Any]:
  pred = predict_interventions(models, x)
  metrics: dict[str, Any] = {
    "samples": int(z.shape[0]),
    "targets": {},
  }
  for i, target_name in enumerate(INTERVENTION_TARGET_NAMES):
    precision, recall, f1, _ = precision_recall_fscore_support(z[:, i], pred[:, i], average="binary", zero_division=0)
    positives = z[:, i] == 1
    negatives = ~positives
    true_positives = int(np.sum(positives & (pred[:, i] == 1)))
    false_positives = int(np.sum(negatives & (pred[:, i] == 1)))
    true_negatives = int(np.sum(negatives & (pred[:, i] == 0)))
    false_negatives = int(np.sum(positives & (pred[:, i] == 0)))
    true_positive_rate = true_positives / max(1, true_positives + false_negatives)
    true_negative_rate = true_negatives / max(1, true_negatives + false_positives)
    if np.any(positives) and np.any(negatives):
      balanced_accuracy = (true_positive_rate + true_negative_rate) / 2.0
    elif np.any(positives):
      balanced_accuracy = true_positive_rate
    else:
      balanced_accuracy = true_negative_rate
    metrics["targets"][target_name] = {
      "positive_rate": float(np.mean(z[:, i])),
      "predicted_positive_rate": float(np.mean(pred[:, i])),
      "accuracy": float(accuracy_score(z[:, i], pred[:, i])),
      "balanced_accuracy": float(balanced_accuracy),
      "precision": float(precision),
      "recall": float(recall),
      "f1": float(f1),
      "true_positives": true_positives,
      "false_positives": false_positives,
      "true_negatives": true_negatives,
      "false_negatives": false_negatives,
    }
  return metrics


def print_response_metrics(label: str, metrics: dict[str, Any]) -> None:
  print(f"{label} response target MAE:")
  for target_name, target_metrics in metrics["targets"].items():
    print(f"  {target_name:30s} mae={target_metrics['mae']:.5f} p95={target_metrics['p95_abs_error']:.5f}")


def print_intervention_metrics(label: str, metrics: dict[str, Any]) -> None:
  print(f"{label} intervention metrics:")
  for target_name, target_metrics in metrics["targets"].items():
    print(
      f"  {target_name:28s} "
      f"pos={target_metrics['positive_rate']:.3f} "
      f"acc={target_metrics['accuracy']:.3f} "
      f"bal_acc={target_metrics['balanced_accuracy']:.3f} "
      f"precision={target_metrics['precision']:.3f} "
      f"recall={target_metrics['recall']:.3f} "
      f"f1={target_metrics['f1']:.3f}"
    )


def make_response_model(args: argparse.Namespace) -> MultiOutputRegressor:
  return MultiOutputRegressor(
    HistGradientBoostingRegressor(
      max_iter=args.max_iter,
      learning_rate=args.learning_rate,
      max_leaf_nodes=args.max_leaf_nodes,
      l2_regularization=args.l2_regularization,
      random_state=args.random_state,
    )
  )


def write_validation_csv(path: Path, response_model: Any, intervention_models: list[Any], examples: list[SegmentExamples], max_rows: int) -> None:
  written = 0
  with path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    header = ["segment", "source_time_s", "target_time_s"]
    for name in TARGET_NAMES:
      header.extend([f"actual_{name}", f"predicted_{name}", f"error_{name}"])
    if intervention_models:
      for name in INTERVENTION_TARGET_NAMES:
        header.extend([f"actual_{name}", f"predicted_{name}"])
    writer.writerow(header)

    for example in examples:
      pred = response_model.predict(example.x)
      intervention_pred = predict_interventions(intervention_models, example.x) if intervention_models else None
      for i in range(example.y.shape[0]):
        row: list[Any] = [example.segment, f"{example.times[i]:.3f}", f"{example.future_times[i]:.3f}"]
        for j in range(example.y.shape[1]):
          actual = float(example.y[i, j])
          predicted = float(pred[i, j])
          row.extend([f"{actual:.6f}", f"{predicted:.6f}", f"{predicted - actual:+.6f}"])
        if intervention_pred is not None:
          for j in range(example.interventions.shape[1]):
            row.extend([int(example.interventions[i, j]), int(intervention_pred[i, j])])
        writer.writerow(row)
        written += 1
        if written >= max_rows:
          return


def train(args: argparse.Namespace) -> None:
  examples = load_examples(args)
  train_examples, validation_examples = split_examples(examples, args.validation_fraction)
  x_train, y_train, z_train = stack_examples(train_examples)
  x_val, y_val, z_val = stack_examples(validation_examples)

  response_model = make_response_model(args)

  split_pct = 100.0 * x_train.shape[0] / (x_train.shape[0] + x_val.shape[0])
  print(f"split samples: train={x_train.shape[0]} ({split_pct:.1f}%) test={x_val.shape[0]} ({100.0 - split_pct:.1f}%)")
  print("training response model")
  started = perf_counter()
  response_model.fit(x_train, y_train)
  print(f"response training finished in {perf_counter() - started:.1f}s")

  train_metrics = evaluate_model(response_model, x_train, y_train)
  validation_metrics = evaluate_model(response_model, x_val, y_val)
  print_response_metrics("train", train_metrics)
  print_response_metrics("test", validation_metrics)

  print("training intervention models")
  intervention_models = fit_intervention_models(x_train, z_train, args)
  train_intervention_metrics = evaluate_intervention_models(intervention_models, x_train, z_train)
  validation_intervention_metrics = evaluate_intervention_models(intervention_models, x_val, z_val)
  print_intervention_metrics("train", train_intervention_metrics)
  print_intervention_metrics("test", validation_intervention_metrics)

  metrics = {
    "log_root": str(Path(args.root)),
    "log_type": args.log_type,
    "horizon_s": args.horizon_s,
    "stride": args.stride,
    "history_steps": args.history_steps,
    "history_step": args.history_step,
    "max_segments": args.max_segments,
    "route_prefixes": args.route_prefix,
    "max_samples_per_segment": args.max_samples_per_segment,
    "brand_filter": args.brand,
    "car_fingerprint_contains": args.car_fingerprint_contains,
    "driver_torque_threshold": args.driver_torque_threshold,
    "base_feature_names": FEATURE_NAMES,
    "feature_names": expanded_feature_names(args.history_steps, args.history_step),
    "target_names": TARGET_NAMES,
    "target_value_keys": TARGET_VALUE_KEYS,
    "intervention_target_names": INTERVENTION_TARGET_NAMES,
    "intervention_value_keys": INTERVENTION_VALUE_KEYS,
    "vehicles": [
      {"segment": e.segment, "brand": e.brand, "carFingerprint": e.car_fingerprint}
      for e in examples
    ],
    "train_segments": [e.segment for e in train_examples],
    "validation_segments": [e.segment for e in validation_examples],
    "response": {
      "train": train_metrics,
      "test": validation_metrics,
    },
    "interventions": {
      "train": train_intervention_metrics,
      "test": validation_intervention_metrics,
    },
    "artifact_refit_all": args.refit_all,
    "artifact_fit_samples": int(x_train.shape[0]),
  }

  if args.refit_all:
    print("refitting selected response and intervention models on all samples")
    x_all = np.concatenate((x_train, x_val), axis=0)
    y_all = np.concatenate((y_train, y_val), axis=0)
    z_all = np.concatenate((z_train, z_val), axis=0)
    response_model = make_response_model(args)
    started = perf_counter()
    response_model.fit(x_all, y_all)
    print(f"full response refit finished in {perf_counter() - started:.1f}s")
    intervention_models = fit_intervention_models(x_all, z_all, args)
    metrics["artifact_fit_samples"] = int(x_all.shape[0])

  output_dir = Path(args.output_dir)
  output_dir.mkdir(parents=True, exist_ok=True)
  model_path = output_dir / "vehicle_response_model.joblib"
  metrics_path = output_dir / "metrics.json"
  csv_path = output_dir / "validation_predictions.csv"
  dump({"response_model": response_model, "intervention_models": intervention_models, "metadata": metrics}, model_path)
  metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  if args.validation_csv_rows > 0:
    write_validation_csv(csv_path, response_model, intervention_models, validation_examples, args.validation_csv_rows)

  print(f"model: {model_path}")
  print(f"metrics: {metrics_path}")
  if args.validation_csv_rows > 0:
    print(f"validation csv: {csv_path}")


def evaluate(args: argparse.Namespace) -> None:
  artifact = load(args.model)
  response_model = artifact["response_model"] if isinstance(artifact, dict) and "response_model" in artifact else artifact
  intervention_models = artifact.get("intervention_models", []) if isinstance(artifact, dict) else []
  examples = load_examples(args)
  x, y, z = stack_examples(examples)
  metrics: dict[str, Any] = {
    "model": str(args.model),
    "log_root": str(args.root),
    "log_type": args.log_type,
    "route_prefixes": args.route_prefix,
    "history_steps": args.history_steps,
    "history_step": args.history_step,
    "segments": [example.segment for example in examples],
    "vehicles": [
      {"segment": example.segment, "brand": example.brand, "carFingerprint": example.car_fingerprint}
      for example in examples
    ],
    "response": evaluate_model(response_model, x, y),
  }
  if intervention_models:
    metrics["interventions"] = evaluate_intervention_models(intervention_models, x, z)
  print(json.dumps(metrics, indent=2, sort_keys=True))
  output_dir = Path(args.output_dir)
  output_dir.mkdir(parents=True, exist_ok=True)
  metrics_path = output_dir / "evaluation_metrics.json"
  metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(f"evaluation metrics: {metrics_path}")
  if args.validation_csv_rows > 0:
    csv_path = output_dir / "evaluation_predictions.csv"
    write_validation_csv(csv_path, response_model, intervention_models, examples, args.validation_csv_rows)
    print(f"evaluation csv: {csv_path}")


def add_common_args(parser: argparse.ArgumentParser) -> None:
  parser.add_argument("--root", type=Path, default=DEFAULT_LOG_ROOT, help="Local realdata root containing segment folders.")
  parser.add_argument("--log-type", choices=("rlog", "qlog"), default="rlog", help="Log file type to train from.")
  parser.add_argument("--brand", default="hyundai", help="Only use segments whose carParams.brand matches this value. Use an empty string to disable.")
  parser.add_argument(
    "--car-fingerprint-contains",
    default="IONIQ5",
    help="Only use segments whose normalized carParams.carFingerprint contains this text. Use an empty string to disable.",
  )
  parser.add_argument("--horizon-s", type=float, default=0.25, help="Future prediction horizon in seconds.")
  parser.add_argument(
    "--driver-torque-threshold",
    type=float,
    default=0.0,
    help="Optional absolute steeringTorque threshold to count as driver torque overlay in addition to steeringPressed.",
  )
  parser.add_argument("--stride", type=int, default=5, help="Use every Nth source row after horizon alignment.")
  parser.add_argument("--history-steps", type=int, default=1, help="Number of current/prior feature rows to include.")
  parser.add_argument("--history-step", type=int, default=2, help="Logged-sample spacing between history rows.")
  parser.add_argument("--max-segments", type=int, default=120, help="Newest segment count to use. Set 0 for all.")
  parser.add_argument("--route-prefix", action="append", default=[], help="Only use segment directories starting with this route prefix; repeat as needed.")
  parser.add_argument("--segment-metrics", type=Path, help="JSON metrics file containing an exact segment-name list to use.")
  parser.add_argument("--segment-list-key", default="validation_segments", help="List key to read from --segment-metrics.")
  parser.add_argument("--max-samples-per-segment", type=int, default=6000, help="Cap aligned samples per segment. Set 0 for all.")
  parser.add_argument("--validation-csv-rows", type=int, default=1000, help="Rows of actual vs predicted samples to export.")
  parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)


def normalize_limits(args: argparse.Namespace) -> None:
  if args.max_segments == 0:
    args.max_segments = None
  if args.max_samples_per_segment == 0:
    args.max_samples_per_segment = None
  if args.brand == "":
    args.brand = None
  if args.car_fingerprint_contains == "":
    args.car_fingerprint_contains = None
  if args.history_steps < 1:
    raise SystemExit("--history-steps must be at least 1")
  if args.history_step < 1:
    raise SystemExit("--history-step must be at least 1")


def main() -> None:
  parser = argparse.ArgumentParser(description="Train and evaluate a local vehicle-response model from comma driving logs.")
  subparsers = parser.add_subparsers(dest="command")

  train_parser = subparsers.add_parser("train", help="Train a response model and write artifacts.")
  add_common_args(train_parser)
  train_parser.add_argument("--validation-fraction", type=float, default=0.2)
  train_parser.add_argument("--max-iter", type=int, default=180)
  train_parser.add_argument("--learning-rate", type=float, default=0.06)
  train_parser.add_argument("--max-leaf-nodes", type=int, default=31)
  train_parser.add_argument("--l2-regularization", type=float, default=0.02)
  train_parser.add_argument("--intervention-max-iter", type=int, default=100)
  train_parser.add_argument("--random-state", type=int, default=1)
  train_parser.add_argument("--refit-all", action="store_true", help="After held-out evaluation, refit the saved artifact on all extracted samples.")
  train_parser.set_defaults(func=train)

  eval_parser = subparsers.add_parser("evaluate", help="Evaluate an existing response model on logs.")
  add_common_args(eval_parser)
  eval_parser.add_argument("--model", type=Path, default=DEFAULT_OUTPUT_DIR / "vehicle_response_model.joblib")
  eval_parser.set_defaults(func=evaluate)

  args = parser.parse_args()
  if args.command is None:
    args = parser.parse_args(["train", *sys.argv[1:]])
  normalize_limits(args)
  args.func(args)


if __name__ == "__main__":
  main()
