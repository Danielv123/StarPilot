#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

try:
  from joblib import load
except ModuleNotFoundError as e:
  raise SystemExit(
    "Missing tuning dependencies. Run with:\n"
    "  uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard "
    "python tools/tuning/optimize_ioniq5_response_tune.py"
  ) from e

from tools.tuning import train_vehicle_response_model as response_data


DEFAULT_MODEL = REPO_ROOT / "artifacts" / "tuning" / "vehicle_response_time_series_h6_20260721" / "vehicle_response_model.joblib"
DEFAULT_OUTPUT = REPO_ROOT / "artifacts" / "tuning" / "ioniq5_response_tune" / "optimization.json"
BASE_LAT_ACCEL_FACTOR = 3.172929
BASE_FRICTION = 0.096019
JERK_GAIN = 0.22


@dataclass(frozen=True)
class Tune:
  name: str
  base_lat_accel_factor_mult: float
  ff_reduction_left: float
  ff_reduction_right: float
  turn_in_boost_left: float
  turn_in_boost_right: float
  unwind_taper_left: float
  unwind_taper_right: float
  turn_in_threshold_reduction_left: float
  turn_in_threshold_reduction_right: float
  unwind_threshold_increase_left: float
  unwind_threshold_increase_right: float
  turn_in_friction_boost_left: float
  turn_in_friction_boost_right: float
  unwind_friction_reduction_left: float
  unwind_friction_reduction_right: float
  friction_scale_mult: float
  center_taper_max: float
  center_taper_lat: float
  center_taper_lat_width: float
  center_taper_speed: float
  center_taper_speed_width: float
  sustained_turn_in_ff_boost_left: float = 0.0
  sustained_turn_in_ff_boost_right: float = 0.0
  sustained_turn_in_ff_speed: float = 13.5
  sustained_turn_in_ff_speed_width: float = 1.8
  sustained_turn_in_ff_lat_start: float = 1.10
  sustained_turn_in_ff_lat_end: float = 3.60
  sustained_turn_in_ff_lat_width: float = 0.30
  steady_high_lat_taper: float = 0.0
  steady_high_lat_start: float = 0.35
  steady_high_lat_width: float = 0.12
  steady_jerk_width: float = 0.08
  hkg_friction_threshold: bool = False
  friction_jerk_gain: float = JERK_GAIN


LOGGED_TUNE = Tune(
  name="logged_local",
  base_lat_accel_factor_mult=1.2600,
  ff_reduction_left=0.12,
  ff_reduction_right=0.22,
  turn_in_boost_left=0.34,
  turn_in_boost_right=0.22,
  unwind_taper_left=0.92,
  unwind_taper_right=1.02,
  turn_in_threshold_reduction_left=0.08,
  turn_in_threshold_reduction_right=0.05,
  unwind_threshold_increase_left=0.36,
  unwind_threshold_increase_right=0.38,
  turn_in_friction_boost_left=0.04,
  turn_in_friction_boost_right=0.03,
  unwind_friction_reduction_left=0.34,
  unwind_friction_reduction_right=0.34,
  friction_scale_mult=0.7290,
  center_taper_max=0.18,
  center_taper_lat=0.12,
  center_taper_lat_width=0.03,
  center_taper_speed=16.0,
  center_taper_speed_width=2.5,
)

UPSTREAM_TUNE = Tune(
  name="upstream",
  base_lat_accel_factor_mult=1.22,
  ff_reduction_left=0.15,
  ff_reduction_right=0.25,
  turn_in_boost_left=0.10,
  turn_in_boost_right=0.04,
  unwind_taper_left=0.92,
  unwind_taper_right=1.04,
  turn_in_threshold_reduction_left=0.05,
  turn_in_threshold_reduction_right=0.03,
  unwind_threshold_increase_left=0.46,
  unwind_threshold_increase_right=0.50,
  turn_in_friction_boost_left=0.02,
  turn_in_friction_boost_right=0.01,
  unwind_friction_reduction_left=0.42,
  unwind_friction_reduction_right=0.44,
  friction_scale_mult=1.0,
  center_taper_max=0.18,
  center_taper_lat=0.16,
  center_taper_lat_width=0.04,
  center_taper_speed=15.0,
  center_taper_speed_width=2.2,
  sustained_turn_in_ff_boost_left=0.10,
  sustained_turn_in_ff_boost_right=0.16,
  hkg_friction_threshold=True,
)


def sigmoid(x: np.ndarray) -> np.ndarray:
  positive = x >= 0.0
  out = np.empty_like(x, dtype=np.float64)
  out[positive] = 1.0 / (1.0 + np.exp(-x[positive]))
  z = np.exp(x[~positive])
  out[~positive] = z / (1.0 + z)
  return out


def side(desired: np.ndarray, left: float, right: float) -> np.ndarray:
  return np.where(desired >= 0.0, left, right)


def tune_terms(tune: Tune, desired: np.ndarray, jerk: np.ndarray, speed: np.ndarray, error: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
  abs_lat = np.abs(desired)
  low_speed = 1.0 / (1.0 + (np.maximum(speed, 0.0) / 12.5) ** 2)
  phase = np.tanh((desired * jerk) / 0.10)
  turn_in = np.maximum(phase, 0.0)
  unwind = np.maximum(-phase, 0.0)
  envelope = sigmoid((abs_lat - 0.10) / 0.05) * sigmoid((1.20 - abs_lat) / 0.30) * low_speed

  base_reduction = side(desired, tune.ff_reduction_left, tune.ff_reduction_right) * envelope
  turn_boost = 1.0 + side(desired, tune.turn_in_boost_left, tune.turn_in_boost_right) * turn_in * (0.35 + 0.65 * low_speed)
  unwind_taper = 1.0 - side(desired, tune.unwind_taper_left, tune.unwind_taper_right) * unwind * (0.35 + 0.65 * low_speed)
  sustained = np.zeros_like(desired)
  sustained_mask = desired * jerk > 0.0
  if np.any(sustained_mask):
    sustained_speed = sigmoid((np.maximum(speed, 0.0) - tune.sustained_turn_in_ff_speed) / tune.sustained_turn_in_ff_speed_width)
    sustained_onset = sigmoid((abs_lat - tune.sustained_turn_in_ff_lat_start) / tune.sustained_turn_in_ff_lat_width)
    sustained_cutoff = sigmoid((tune.sustained_turn_in_ff_lat_end - abs_lat) / tune.sustained_turn_in_ff_lat_width)
    sustained = side(desired, tune.sustained_turn_in_ff_boost_left, tune.sustained_turn_in_ff_boost_right) * sustained_speed * sustained_onset * sustained_cutoff
    sustained = np.where(sustained_mask, sustained, 0.0)
  ff_scale = (1.0 + sustained) * (1.0 - base_reduction) * turn_boost * np.maximum(unwind_taper, 0.0)
  steady_weight = np.exp(-((jerk / tune.steady_jerk_width) ** 2))
  steady_high_lat_weight = sigmoid((abs_lat - tune.steady_high_lat_start) / tune.steady_high_lat_width)
  ff_scale *= 1.0 - tune.steady_high_lat_taper * steady_weight * steady_high_lat_weight

  center_speed = sigmoid((speed - tune.center_taper_speed) / tune.center_taper_speed_width)
  center_lat = sigmoid((tune.center_taper_lat - abs_lat) / tune.center_taper_lat_width)
  center_taper = 1.0 - tune.center_taper_max * center_speed * center_lat

  if tune.hkg_friction_threshold:
    base_threshold = np.full_like(speed, 0.39)
  else:
    mph = np.array([1.0, 20.0, 75.0]) * 0.44704
    base_threshold = np.interp(speed, mph, [0.16, 0.19, 0.27])
  threshold_scale = 1.0 - side(desired, tune.turn_in_threshold_reduction_left, tune.turn_in_threshold_reduction_right) * envelope * turn_in
  threshold_scale += side(desired, tune.unwind_threshold_increase_left, tune.unwind_threshold_increase_right) * envelope * unwind
  threshold = base_threshold * np.clip(threshold_scale, 0.86, 1.18)

  friction_scale = 1.0 + side(desired, tune.turn_in_friction_boost_left, tune.turn_in_friction_boost_right) * envelope * turn_in
  friction_scale -= side(desired, tune.unwind_friction_reduction_left, tune.unwind_friction_reduction_right) * envelope * unwind
  friction_scale = np.clip(friction_scale, 0.86, 1.04)
  friction_scale = 1.0 + (friction_scale - 1.0) * center_taper
  friction_scale *= tune.friction_scale_mult

  lat_factor = BASE_LAT_ACCEL_FACTOR * tune.base_lat_accel_factor_mult
  friction = np.clip(
    (error + tune.friction_jerk_gain * jerk) / threshold, -1.0, 1.0,
  ) * BASE_FRICTION * lat_factor
  feedforward = desired * ff_scale * center_taper + friction_scale * friction
  return feedforward, np.full_like(desired, lat_factor)


def phase_masks(desired: np.ndarray, jerk: np.ndarray) -> dict[str, np.ndarray]:
  active = np.abs(desired) >= 0.08
  return {
    "all": np.ones_like(active, dtype=bool),
    "center": ~active,
    "steady": active & (np.abs(jerk) < 0.08),
    "turn_in_left": active & (jerk * desired > 0.0) & (desired >= 0.0),
    "turn_in_right": active & (jerk * desired > 0.0) & (desired < 0.0),
    "unwind_left": active & (jerk * desired < 0.0) & (desired >= 0.0),
    "unwind_right": active & (jerk * desired < 0.0) & (desired < 0.0),
  }


class Evaluator:
  def __init__(self, model: Any, x: np.ndarray, feature_names: list[str]):
    self.model = model.estimators_[7] if hasattr(model, "estimators_") else model
    self.x = x.astype(np.float32, copy=False)
    self.feature_names = feature_names
    self.index = {name: i for i, name in enumerate(feature_names)}
    self.history_suffixes = ["_t"] + [name.removeprefix("desired_lateral_accel") for name in feature_names if name.startswith("desired_lateral_accel_t_minus_")]
    self.desired = self.x[:, self.index["desired_lateral_accel_t"]].astype(np.float64)
    self.jerk = self.x[:, self.index["desired_lateral_jerk_t"]].astype(np.float64)
    self.masks = phase_masks(self.desired, self.jerk)

  def counterfactual(self, tune: Tune) -> tuple[np.ndarray, np.ndarray]:
    candidate = self.x.copy()
    command_delta_t = np.zeros(candidate.shape[0], dtype=np.float64)
    for suffix in self.history_suffixes:
      desired = candidate[:, self.index[f"desired_lateral_accel{suffix}"]].astype(np.float64)
      jerk = candidate[:, self.index[f"desired_lateral_jerk{suffix}"]].astype(np.float64)
      speed = candidate[:, self.index[f"v_ego{suffix}"]].astype(np.float64)
      error = candidate[:, self.index[f"torque_state_error{suffix}"]].astype(np.float64)
      p = candidate[:, self.index[f"torque_state_p{suffix}"]].astype(np.float64)
      i = candidate[:, self.index[f"torque_state_i{suffix}"]].astype(np.float64)
      d = candidate[:, self.index[f"torque_state_d{suffix}"]].astype(np.float64)
      logged_ff, logged_factor = tune_terms(LOGGED_TUNE, desired, jerk, speed, error)
      candidate_ff, candidate_factor = tune_terms(tune, desired, jerk, speed, error)
      pid_without_ff = p + i + d
      logged_reconstructed = -(pid_without_ff + logged_ff) / logged_factor
      candidate_reconstructed = -(pid_without_ff + candidate_ff) / candidate_factor
      delta = candidate_reconstructed - logged_reconstructed
      if suffix == "_t":
        command_delta_t = delta
      for prefix in ("cmd_torque", "cmd_output_torque", "torque_state_output"):
        idx = self.index[f"{prefix}{suffix}"]
        candidate[:, idx] = np.clip(candidate[:, idx] + delta, -1.0, 1.0)
      f_idx = self.index[f"torque_state_f{suffix}"]
      candidate[:, f_idx] = candidate[:, f_idx] + (candidate_ff - logged_ff)
    return candidate, command_delta_t

  def evaluate(self, tune: Tune) -> dict[str, Any]:
    candidate, command_delta = self.counterfactual(tune)
    prediction = self.model.predict(candidate).astype(np.float64)
    error = prediction - self.desired
    phases: dict[str, Any] = {}
    for name, mask in self.masks.items():
      if not np.any(mask):
        continue
      phase_error = error[mask]
      phases[name] = {
        "samples": int(np.sum(mask)),
        "rmse": float(np.sqrt(np.mean(phase_error ** 2))),
        "mae": float(np.mean(np.abs(phase_error))),
        "bias": float(np.mean(phase_error)),
      }
    active_mask = np.abs(self.desired) >= 0.08
    phase_rmse = [phases[name]["rmse"] for name in ("turn_in_left", "turn_in_right", "unwind_left", "unwind_right") if name in phases]
    balanced_rmse = float(np.mean(phase_rmse)) if phase_rmse else phases["all"]["rmse"]
    command_rms = float(np.sqrt(np.mean(command_delta[active_mask] ** 2))) if np.any(active_mask) else 0.0
    objective = balanced_rmse + 0.20 * phases["center"]["rmse"] + 0.10 * command_rms
    return {
      "name": tune.name,
      "objective": float(objective),
      "balanced_transition_rmse": balanced_rmse,
      "command_delta_rms": command_rms,
      "phases": phases,
      "tune": asdict(tune),
    }


def optimize(evaluator: Evaluator) -> tuple[Tune, list[dict[str, Any]]]:
  current = UPSTREAM_TUNE
  history = [evaluator.evaluate(LOGGED_TUNE), evaluator.evaluate(UPSTREAM_TUNE)]
  fields = {
    "base_lat_accel_factor_mult": (0.02, 1.16, 1.30),
    "turn_in_boost_left": (0.05, 0.0, 0.40),
    "turn_in_boost_right": (0.05, 0.0, 0.35),
    "unwind_taper_left": (0.08, 0.60, 1.12),
    "unwind_taper_right": (0.08, 0.60, 1.12),
    "center_taper_max": (0.04, 0.06, 0.30),
    "sustained_turn_in_ff_boost_left": (0.05, 0.0, 0.25),
    "sustained_turn_in_ff_boost_right": (0.05, 0.0, 0.30),
  }
  best_result = history[-1]
  for pass_index in range(3):
    improved = False
    for field, (initial_step, lo, hi) in fields.items():
      step = initial_step / (2 ** pass_index)
      values = sorted({float(np.clip(getattr(current, field) + offset * step, lo, hi)) for offset in (-2, -1, 0, 1, 2)})
      candidates = [replace(current, name=f"search_{field}_{value:.4f}", **{field: value}) for value in values]
      results = [evaluator.evaluate(candidate) for candidate in candidates]
      history.extend(results)
      winner = min(results, key=lambda result: result["objective"])
      if winner["objective"] + 1e-7 < best_result["objective"]:
        current = replace(candidates[results.index(winner)], name="optimized")
        best_result = evaluator.evaluate(current)
        history.append(best_result)
        improved = True
    if not improved:
      break
  return current, history


def clean_samples(x: np.ndarray, feature_names: list[str]) -> np.ndarray:
  idx = {name: i for i, name in enumerate(feature_names)}
  history_suffixes = ["_t"] + [name.removeprefix("desired_lateral_accel") for name in feature_names if name.startswith("desired_lateral_accel_t_minus_")]
  required = [
    idx[f"{prefix}{suffix}"]
    for suffix in history_suffixes
    for prefix in (
      "cmd_torque", "cmd_output_torque", "v_ego", "torque_state_error", "torque_state_p", "torque_state_i",
      "torque_state_d", "torque_state_f", "torque_state_output", "desired_lateral_accel", "desired_lateral_jerk",
    )
  ]
  clean = (
    (x[:, idx["car_control_lat_active_t"]] > 0.5)
    & (x[:, idx["torque_state_active_t"]] > 0.5)
    & (x[:, idx["steering_pressed_t"]] < 0.5)
    & (x[:, idx["torque_state_saturated_t"]] < 0.5)
    & (x[:, idx["v_ego_t"]] >= 3.0)
    & np.isfinite(x[:, required]).all(axis=1)
  )
  return x[clean]


def cap_samples(x: np.ndarray, limit: int, seed: int) -> np.ndarray:
  if x.shape[0] <= limit:
    return x
  rng = np.random.default_rng(seed)
  selected = np.sort(rng.choice(x.shape[0], limit, replace=False))
  return x[selected]


def load_samples(args: argparse.Namespace, metadata: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, list[str], list[str], list[str]]:
  load_args = SimpleNamespace(
    root=args.root,
    log_type=metadata.get("log_type", "rlog"),
    max_segments=args.max_segments,
    route_prefix=[],
    segment_metrics=None,
    segment_list_key="validation_segments",
    horizon_s=float(metadata.get("horizon_s", 0.25)),
    stride=int(metadata.get("stride", 5)),
    max_samples_per_segment=args.max_samples_per_segment,
    brand=metadata.get("brand_filter", "hyundai"),
    car_fingerprint_contains=metadata.get("car_fingerprint_contains", "IONIQ5"),
    driver_torque_threshold=float(metadata.get("driver_torque_threshold", 0.0)),
    history_steps=int(metadata.get("history_steps", 6)),
    history_step=int(metadata.get("history_step", 2)),
  )
  examples = response_data.load_examples(load_args)
  feature_names = list(metadata["feature_names"])
  holdout_examples = examples[::3]
  search_examples = [example for index, example in enumerate(examples) if index % 3 != 0]
  search_x = clean_samples(np.vstack([example.x for example in search_examples]), feature_names)
  holdout_x = clean_samples(np.vstack([example.x for example in holdout_examples]), feature_names)
  search_x = cap_samples(search_x, args.max_samples, 1)
  holdout_x = cap_samples(holdout_x, max(args.max_samples // 2, 1000), 2)
  return (
    search_x,
    holdout_x,
    feature_names,
    [example.segment for example in search_examples],
    [example.segment for example in holdout_examples],
  )


def main() -> None:
  parser = argparse.ArgumentParser(description="Optimize the Ioniq 5 torque profile against a trained vehicle-response model.")
  parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
  parser.add_argument("--root", type=Path, default=response_data.DEFAULT_LOG_ROOT)
  parser.add_argument("--max-segments", type=int, default=40)
  parser.add_argument("--max-samples-per-segment", type=int, default=3000)
  parser.add_argument("--max-samples", type=int, default=30000)
  parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
  args = parser.parse_args()

  artifact = load(args.model)
  model = artifact["response_model"] if isinstance(artifact, dict) else artifact
  metadata = artifact.get("metadata", {}) if isinstance(artifact, dict) else {}
  search_x, holdout_x, feature_names, search_segments, holdout_segments = load_samples(args, metadata)
  if search_x.shape[0] < 100 or holdout_x.shape[0] < 100:
    raise SystemExit(f"Need at least 100 clean samples in each split; search={search_x.shape[0]}, holdout={holdout_x.shape[0]}.")
  print(f"optimizing on {search_x.shape[0]} clean samples from {len(search_segments)} segments; "
        f"holding out {holdout_x.shape[0]} samples from {len(holdout_segments)} segments")
  search_evaluator = Evaluator(model, search_x, feature_names)
  optimized, history = optimize(search_evaluator)
  holdout_evaluator = Evaluator(model, holdout_x, feature_names)
  results = {
    "model": str(args.model),
    "search_segments": search_segments,
    "holdout_segments": holdout_segments,
    "search_clean_samples": int(search_x.shape[0]),
    "holdout_clean_samples": int(holdout_x.shape[0]),
    "search": {
      "logged": search_evaluator.evaluate(LOGGED_TUNE),
      "upstream": search_evaluator.evaluate(UPSTREAM_TUNE),
      "optimized": search_evaluator.evaluate(optimized),
    },
    "holdout": {
      "logged": holdout_evaluator.evaluate(LOGGED_TUNE),
      "upstream": holdout_evaluator.evaluate(UPSTREAM_TUNE),
      "optimized": holdout_evaluator.evaluate(optimized),
    },
    "search_evaluations": len(history),
    "method": {
      "prediction_horizon_s": metadata.get("horizon_s"),
      "history_steps": metadata.get("history_steps"),
      "counterfactual": "bounded torque-input perturbation relative to the logged local tune",
      "objective": "balanced turn-in/unwind RMSE + 0.20 center RMSE + 0.10 command-delta RMS",
    },
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(json.dumps({key: results[key] for key in ("search_clean_samples", "holdout_clean_samples", "search", "holdout")}, indent=2))
  print(f"optimization report: {args.output}")


if __name__ == "__main__":
  main()
