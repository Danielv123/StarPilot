#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

try:
  from joblib import dump
  from sklearn.ensemble import HistGradientBoostingRegressor
  from sklearn.metrics import mean_absolute_error, root_mean_squared_error
  from sklearn.multioutput import MultiOutputRegressor
except ModuleNotFoundError as e:
  raise SystemExit(
    "Run with: uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard python tools/tuning/train_lateral_plant_model.py"
  ) from e

from openpilot.tools.tuning import train_vehicle_response_model as log_data


DEFAULT_LOG_ROOT = Path(r"D:\comma_driving_logs\10.30.1.75\realdata")
DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts/tuning/lateral_plant_20260721"

BASE_FEATURES = (
  "applied_torque",
  "actual_lateral_accel",
  "steering_angle_deg",
  "steering_rate_deg",
  "steering_torque_eps",
  "v_ego",
  "a_ego",
)
STATE_FEATURES = (
  "actual_lateral_accel",
  "steering_angle_deg",
  "steering_rate_deg",
  "steering_torque_eps",
)
DIAGNOSTIC_FIELDS = (
  "desired_lateral_accel",
  "desired_lateral_jerk",
  "controller_output",
  "controller_i",
  "lat_active",
  "driver_overlay",
  "saturated",
)


@dataclass
class Trajectory:
  segment: str
  route: str
  brand: str
  car_fingerprint: str
  times: np.ndarray
  values: dict[str, np.ndarray]
  lateral_active_rows: int
  driver_overlay_rows: int


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


def finite(value: Any, default: float = math.nan) -> float:
  try:
    value = float(value)
    return value if math.isfinite(value) else default
  except Exception:
    return default


def route_name(segment: str) -> str:
  return segment.rsplit("--", 1)[0] if "--" in segment else segment


def read_trajectory(path: Path, brand_filter: str, fingerprint_filter: str, sample_step: int) -> Trajectory | None:
  latest: dict[str, Any] = {}
  brand = ""
  fingerprint = ""
  accepted = False
  times: list[float] = []
  rows: dict[str, list[float]] = {name: [] for name in (*BASE_FEATURES, *DIAGNOSTIC_FIELDS)}
  lateral_active_rows = 0
  driver_overlay_rows = 0

  for msg in log_data.iter_log_messages(path):
    try:
      which = msg.which()
    except Exception:
      continue
    if which == "carParams":
      brand = str(nested(msg.carParams, "brand") or "")
      fingerprint = str(nested(msg.carParams, "carFingerprint") or "")
      accepted = log_data.identity_matches(brand, fingerprint, brand_filter, fingerprint_filter)
      if not accepted:
        return None
      continue
    if which not in ("carState", "carControl", "carOutput", "controlsState"):
      continue
    if not accepted:
      continue
    latest[which] = getattr(msg, which)
    if which != "carState":
      continue

    car_state = latest.get("carState")
    car_control = latest.get("carControl")
    car_output = latest.get("carOutput")
    controls_state = latest.get("controlsState")
    torque_state = None
    lateral_state = nested(controls_state, "lateralControlState")
    if lateral_state is not None:
      try:
        if lateral_state.which() == "torqueState":
          torque_state = lateral_state.torqueState
      except Exception:
        pass
    if torque_state is None:
      continue

    lat_active = bool(nested(car_control, "latActive"))
    driver_overlay = lat_active and bool(nested(car_state, "steeringPressed"))
    lateral_active_rows += int(lat_active)
    driver_overlay_rows += int(driver_overlay)

    if len(times) % sample_step != 0:
      # Count intervention state at full rate, but retain plant rows at sample_step.
      times.append(math.nan)
      continue

    applied = finite(nested(car_output, "actuatorsOutput", "torque"))
    if not math.isfinite(applied):
      applied = finite(nested(car_control, "actuators", "torque"))
    row = {
      "applied_torque": applied,
      "actual_lateral_accel": finite(nested(torque_state, "actualLateralAccel")),
      "steering_angle_deg": finite(nested(car_state, "steeringAngleDeg")),
      "steering_rate_deg": finite(nested(car_state, "steeringRateDeg")),
      "steering_torque_eps": finite(nested(car_state, "steeringTorqueEps")),
      "v_ego": finite(nested(car_state, "vEgo")),
      "a_ego": finite(nested(car_state, "aEgo")),
      "desired_lateral_accel": finite(nested(torque_state, "desiredLateralAccel")),
      "desired_lateral_jerk": finite(nested(torque_state, "desiredLateralJerk")),
      "controller_output": finite(nested(torque_state, "output")),
      "controller_i": finite(nested(torque_state, "i")),
      "lat_active": float(lat_active),
      "driver_overlay": float(driver_overlay),
      "saturated": float(bool(nested(torque_state, "saturated"))),
    }
    if not all(math.isfinite(row[name]) for name in (*BASE_FEATURES, "desired_lateral_accel", "desired_lateral_jerk", "controller_output", "controller_i")):
      times.append(math.nan)
      continue
    times.append(msg.logMonoTime / 1e9)
    for name, value in row.items():
      rows[name].append(value)

  sampled_times = np.asarray([time for time in times if math.isfinite(time)], dtype=np.float64)
  if len(sampled_times) < 20:
    return None
  # Values were only appended for finite sampled times.
  values = {name: np.asarray(value, dtype=np.float32) for name, value in rows.items()}
  return Trajectory(
    segment=path.parent.name,
    route=route_name(path.parent.name),
    brand=brand,
    car_fingerprint=fingerprint,
    times=sampled_times,
    values=values,
    lateral_active_rows=lateral_active_rows,
    driver_overlay_rows=driver_overlay_rows,
  )


def expanded_feature_names(history_steps: int) -> list[str]:
  return [f"{name}_t_minus_{lag}" for lag in range(history_steps) for name in BASE_FEATURES]


def trajectory_samples(trajectory: Trajectory, history_steps: int) -> tuple[np.ndarray, np.ndarray]:
  values = trajectory.values
  base = np.column_stack([values[name] for name in BASE_FEATURES])
  state = np.column_stack([values[name] for name in STATE_FEATURES])
  source = np.arange(history_steps - 1, len(trajectory.times) - 1)
  continuous = (trajectory.times[source + 1] - trajectory.times[source]) < 0.09
  clean = (
    (values["lat_active"][source] > 0.5)
    & (values["lat_active"][source + 1] > 0.5)
    & (values["driver_overlay"][source] < 0.5)
    & (values["driver_overlay"][source + 1] < 0.5)
    & (values["v_ego"][source] >= 3.0)
    & continuous
  )
  source = source[clean]
  if len(source) == 0:
    return np.empty((0, history_steps * len(BASE_FEATURES)), dtype=np.float32), np.empty((0, len(STATE_FEATURES)), dtype=np.float32)
  history = np.stack([base[source - lag] for lag in range(history_steps)], axis=1)
  target_delta = state[source + 1] - state[source]
  finite_rows = np.isfinite(history).all(axis=(1, 2)) & np.isfinite(target_delta).all(axis=1)
  return history[finite_rows].reshape((-1, history_steps * len(BASE_FEATURES))), target_delta[finite_rows]


def route_intervention_stats(trajectories: list[Trajectory]) -> dict[str, dict[str, float]]:
  stats: dict[str, dict[str, float]] = {}
  for trajectory in trajectories:
    item = stats.setdefault(trajectory.route, {"lateral_active_rows": 0, "driver_overlay_rows": 0})
    item["lateral_active_rows"] += trajectory.lateral_active_rows
    item["driver_overlay_rows"] += trajectory.driver_overlay_rows
  for item in stats.values():
    item["driver_overlay_fraction"] = item["driver_overlay_rows"] / max(item["lateral_active_rows"], 1)
  return stats


def load_trajectories(args: argparse.Namespace) -> tuple[list[Trajectory], dict[str, dict[str, float]], list[str]]:
  paths = log_data.discover_log_files(Path(args.root), "rlog", args.max_segments, [], None)
  trajectories: list[Trajectory] = []
  started = perf_counter()
  for index, path in enumerate(paths, 1):
    try:
      trajectory = read_trajectory(path, args.brand, args.car_fingerprint_contains, args.sample_step)
    except Exception as e:
      print(f"skip {path.parent.name}: {e}", file=sys.stderr)
      continue
    if trajectory is not None:
      trajectories.append(trajectory)
    if index % 25 == 0 or index == len(paths):
      print(f"loaded {index}/{len(paths)} segments; usable={len(trajectories)}", flush=True)
  stats = route_intervention_stats(trajectories)
  excluded = sorted(route for route, item in stats.items() if item["driver_overlay_fraction"] > args.max_route_driver_overlay)
  trajectories = [trajectory for trajectory in trajectories if trajectory.route not in excluded]
  print(f"excluded {len(excluded)}/{len(stats)} routes above {args.max_route_driver_overlay:.0%} driver overlay")
  for route in excluded:
    print(f"  exclude {route}: {stats[route]['driver_overlay_fraction']:.1%} overlay")
  print(f"retained {len(trajectories)} segments from {len({t.route for t in trajectories})} routes in {perf_counter() - started:.1f}s")
  return trajectories, stats, excluded


def split_routes(trajectories: list[Trajectory], validation_fraction: float, seed: int) -> tuple[set[str], set[str]]:
  routes = sorted({trajectory.route for trajectory in trajectories})
  if len(routes) < 2:
    raise SystemExit(f"Need at least two retained routes for a route-level split; found {len(routes)}.")
  rng = np.random.default_rng(seed)
  shuffled = list(np.asarray(routes)[rng.permutation(len(routes))])
  validation_count = max(1, min(len(routes) - 1, round(len(routes) * validation_fraction)))
  validation = set(shuffled[:validation_count])
  return set(routes) - validation, validation


def stack_route_samples(trajectories: list[Trajectory], routes: set[str], history_steps: int, cap: int | None, seed: int) -> tuple[np.ndarray, np.ndarray]:
  samples = [trajectory_samples(t, history_steps) for t in trajectories if t.route in routes]
  samples = [(x, y) for x, y in samples if len(x)]
  if not samples:
    raise SystemExit(f"No clean plant samples for {len(routes)} selected routes.")
  x = np.vstack([item[0] for item in samples])
  y = np.vstack([item[1] for item in samples])
  if cap is not None and len(x) > cap:
    rng = np.random.default_rng(seed)
    selected = np.sort(rng.choice(len(x), cap, replace=False))
    x, y = x[selected], y[selected]
  return x, y


def make_model(args: argparse.Namespace) -> MultiOutputRegressor:
  return MultiOutputRegressor(HistGradientBoostingRegressor(
    max_iter=args.max_iter,
    learning_rate=args.learning_rate,
    max_leaf_nodes=args.max_leaf_nodes,
    l2_regularization=args.l2_regularization,
    random_state=args.random_state,
  ))


def one_step_metrics(model: Any, x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
  prediction = model.predict(x)
  targets = {}
  for index, name in enumerate(STATE_FEATURES):
    targets[f"delta_{name}"] = {
      "mae": float(mean_absolute_error(y[:, index], prediction[:, index])),
      "rmse": float(root_mean_squared_error(y[:, index], prediction[:, index])),
      "p95_abs_error": float(np.percentile(np.abs(prediction[:, index] - y[:, index]), 95)),
    }
  return {"samples": int(len(x)), "targets": targets}


def rollout_metrics(model: Any, trajectories: list[Trajectory], routes: set[str], history_steps: int, rollout_steps: int,
                    sample_period_s: float, cap: int, seed: int) -> dict[str, Any]:
  windows: list[tuple[Trajectory, int]] = []
  for trajectory in trajectories:
    if trajectory.route not in routes:
      continue
    values = trajectory.values
    for source in range(history_steps - 1, len(trajectory.times) - rollout_steps):
      future = slice(source, source + rollout_steps + 1)
      if values["lat_active"][future].min() < 0.5 or values["driver_overlay"][future].max() > 0.5 or values["v_ego"][source] < 3.0:
        continue
      if np.max(np.diff(trajectory.times[source:source + rollout_steps + 1])) >= 0.09:
        continue
      windows.append((trajectory, source))
  if len(windows) > cap:
    rng = np.random.default_rng(seed)
    windows = [windows[index] for index in np.sort(rng.choice(len(windows), cap, replace=False))]

  history = np.stack([
    np.stack([
      np.asarray([trajectory.values[name][source - lag] for name in BASE_FEATURES], dtype=np.float32)
      for lag in range(history_steps)
    ])
    for trajectory, source in windows
  ])
  errors: list[np.ndarray] = []
  state_indices = [BASE_FEATURES.index(name) for name in STATE_FEATURES]
  for step in range(rollout_steps):
    prediction_delta = model.predict(history.reshape((len(history), -1)))
    next_state = history[:, 0, state_indices] + prediction_delta
    actual_next = np.stack([
      np.asarray([trajectory.values[name][source + step + 1] for name in STATE_FEATURES])
      for trajectory, source in windows
    ])
    errors.append(next_state - actual_next)
    next_base = np.stack([
      np.asarray([trajectory.values[name][source + step + 1] for name in BASE_FEATURES])
      for trajectory, source in windows
    ])
    next_base[:, state_indices] = next_state
    history[:, 1:] = history[:, :-1]
    history[:, 0] = next_base

  result: dict[str, Any] = {"windows": len(windows), "horizons": {}}
  for step, error in enumerate(errors, 1):
    result["horizons"][f"{step * sample_period_s:.2f}s"] = {
      name: {
        "mae": float(np.mean(np.abs(error[:, index]))),
        "rmse": float(np.sqrt(np.mean(error[:, index] ** 2))),
        "p95_abs_error": float(np.percentile(np.abs(error[:, index]), 95)),
      }
      for index, name in enumerate(STATE_FEATURES)
    }
  return result


def train(args: argparse.Namespace) -> None:
  trajectories, route_stats, excluded_routes = load_trajectories(args)
  train_routes, validation_routes = split_routes(trajectories, args.validation_fraction, args.random_state)
  x_train, y_train = stack_route_samples(trajectories, train_routes, args.history_steps, args.max_train_samples, args.random_state)
  x_validation, y_validation = stack_route_samples(trajectories, validation_routes, args.history_steps, args.max_validation_samples, args.random_state + 1)
  print(f"plant samples: train={len(x_train)} validation={len(x_validation)}")
  print(f"routes: train={len(train_routes)} validation={len(validation_routes)}")

  model = make_model(args)
  started = perf_counter()
  model.fit(x_train, y_train)
  print(f"plant model fit in {perf_counter() - started:.1f}s")
  train_metrics = one_step_metrics(model, x_train, y_train)
  validation_metrics = one_step_metrics(model, x_validation, y_validation)
  rollout = rollout_metrics(model, trajectories, validation_routes, args.history_steps, args.rollout_steps,
                            args.sample_period_s, args.max_rollout_windows, args.random_state + 2)
  for name, target in validation_metrics["targets"].items():
    print(f"validation {name:32s} rmse={target['rmse']:.6f} p95={target['p95_abs_error']:.6f}")
  for horizon, target in rollout["horizons"].items():
    lat = target["actual_lateral_accel"]
    print(f"rollout {horizon}: lateral rmse={lat['rmse']:.6f} p95={lat['p95_abs_error']:.6f}")

  metadata = {
    "model_type": "controller_independent_lateral_plant_delta",
    "log_root": str(Path(args.root)),
    "sample_step": args.sample_step,
    "sample_period_s": args.sample_period_s,
    "history_steps": args.history_steps,
    "base_feature_names": list(BASE_FEATURES),
    "feature_names": expanded_feature_names(args.history_steps),
    "state_feature_names": list(STATE_FEATURES),
    "predictor_excludes": [
      "desired path and lateral-acceleration request",
      "controller error and P/I/D/F terms",
      "controller saturation and active state",
      "driver intervention labels",
    ],
    "route_driver_overlay_threshold": args.max_route_driver_overlay,
    "route_intervention_stats": route_stats,
    "excluded_routes": excluded_routes,
    "train_routes": sorted(train_routes),
    "validation_routes": sorted(validation_routes),
    "train_segments": [t.segment for t in trajectories if t.route in train_routes],
    "validation_segments": [t.segment for t in trajectories if t.route in validation_routes],
    "one_step": {"train": train_metrics, "validation": validation_metrics},
    "open_loop_rollout": rollout,
    "artifact_fit_samples": int(len(x_train)),
  }
  output = Path(args.output_dir)
  output.mkdir(parents=True, exist_ok=True)
  dump({"plant_model": model, "metadata": metadata}, output / "lateral_plant_model.joblib")
  (output / "metrics.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(f"model: {output / 'lateral_plant_model.joblib'}")
  print(f"metrics: {output / 'metrics.json'}")


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Train a controller-independent autoregressive lateral plant model.")
  parser.add_argument("--root", type=Path, default=DEFAULT_LOG_ROOT)
  parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
  parser.add_argument("--brand", default="hyundai")
  parser.add_argument("--car-fingerprint-contains", default="IONIQ5")
  parser.add_argument("--max-segments", type=int, default=0)
  parser.add_argument("--sample-step", type=int, default=5, help="100 Hz carState rows per retained plant step.")
  parser.add_argument("--history-steps", type=int, default=6)
  parser.add_argument("--rollout-steps", type=int, default=5)
  parser.add_argument("--max-route-driver-overlay", type=float, default=0.50)
  parser.add_argument("--validation-fraction", type=float, default=0.20)
  parser.add_argument("--max-train-samples", type=int, default=350000)
  parser.add_argument("--max-validation-samples", type=int, default=90000)
  parser.add_argument("--max-rollout-windows", type=int, default=30000)
  parser.add_argument("--max-iter", type=int, default=180)
  parser.add_argument("--learning-rate", type=float, default=0.06)
  parser.add_argument("--max-leaf-nodes", type=int, default=31)
  parser.add_argument("--l2-regularization", type=float, default=0.03)
  parser.add_argument("--random-state", type=int, default=7)
  parsed = parser.parse_args()
  parsed.max_segments = None if parsed.max_segments == 0 else parsed.max_segments
  parsed.max_train_samples = None if parsed.max_train_samples == 0 else parsed.max_train_samples
  parsed.max_validation_samples = None if parsed.max_validation_samples == 0 else parsed.max_validation_samples
  parsed.sample_period_s = parsed.sample_step * 0.01
  return parsed


if __name__ == "__main__":
  args = parse_args()
  train(args)
