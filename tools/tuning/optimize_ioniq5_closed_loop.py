#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
from joblib import load

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

from openpilot.tools.tuning import optimize_ioniq5_response_tune as tune_math
from openpilot.tools.tuning import train_lateral_plant_model as plant_data


DEFAULT_MODEL = REPO_ROOT / "artifacts/tuning/lateral_plant_20260721/lateral_plant_model.joblib"
DEFAULT_TUNE_REPORT = REPO_ROOT / "artifacts/tuning/ioniq5_response_tune_20260721/optimization.json"
DEFAULT_OUTPUT = REPO_ROOT / "artifacts/tuning/ioniq5_closed_loop_20260721/optimization.json"
KP_SPEEDS = np.asarray([1.0, 1.5, 2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 30.0])
KP_VALUES = np.asarray([250.0, 120.0, 65.0, 30.0, 11.5, 5.5, 3.5, 2.0, 0.6])
LOW_SPEED_X = np.asarray([0.0, 10.0, 20.0, 30.0])
LOW_SPEED_Y = np.asarray([12.0, 10.5, 8.0, 5.0])
KI = 0.35

CURRENT_CODE_TUNE = tune_math.Tune(
  name="current_code",
  base_lat_accel_factor_mult=1.36,
  ff_reduction_left=0.2625,
  ff_reduction_right=0.415,
  turn_in_boost_left=0.135,
  turn_in_boost_right=0.02,
  unwind_taper_left=1.15,
  unwind_taper_right=1.39,
  turn_in_threshold_reduction_left=0.125,
  turn_in_threshold_reduction_right=0.085,
  unwind_threshold_increase_left=0.28,
  unwind_threshold_increase_right=0.30,
  turn_in_friction_boost_left=0.02,
  turn_in_friction_boost_right=0.01,
  unwind_friction_reduction_left=0.42,
  unwind_friction_reduction_right=0.44,
  friction_scale_mult=1.0,
  center_taper_max=0.17,
  center_taper_lat=0.16,
  center_taper_lat_width=0.04,
  center_taper_speed=15.0,
  center_taper_speed_width=2.2,
  sustained_turn_in_ff_boost_left=0.0,
  sustained_turn_in_ff_boost_right=0.0,
  sustained_turn_in_ff_speed=13.5,
  sustained_turn_in_ff_speed_width=1.8,
  sustained_turn_in_ff_lat_start=1.10,
  sustained_turn_in_ff_lat_end=3.60,
  sustained_turn_in_ff_lat_width=0.30,
  steady_high_lat_taper=0.015,
  hkg_friction_threshold=True,
)


@dataclass
class RolloutBatch:
  history: np.ndarray
  desired: np.ndarray
  jerk: np.ndarray
  v_ego: np.ndarray
  a_ego: np.ndarray
  logged_applied: np.ndarray
  logged_controller_output: np.ndarray
  initial_i: np.ndarray
  target_desired: np.ndarray
  target_jerk: np.ndarray
  segments: list[str]


@dataclass
class RolloutTrace:
  errors: np.ndarray
  command_delta: np.ndarray
  commands: np.ndarray
  states: np.ndarray


def current_tune(path: Path | None, variant: str = "optimized") -> tune_math.Tune:
  if path is None:
    return CURRENT_CODE_TUNE
  payload = json.loads(path.read_text(encoding="utf-8"))
  values = dict(payload["holdout"][variant]["tune"])
  values["name"] = "current_tune"
  return tune_math.Tune(**values)


def load_validation_trajectories(metadata: dict[str, Any]) -> list[plant_data.Trajectory]:
  root = Path(metadata["log_root"])
  trajectories = []
  segments = metadata["validation_segments"]
  for index, segment in enumerate(segments, 1):
    path = root / segment / "rlog.zst"
    try:
      trajectory = plant_data.read_trajectory(path, "hyundai", "IONIQ5", int(metadata["sample_step"]))
    except Exception as e:
      print(f"skip {segment}: {e}", file=sys.stderr)
      continue
    if trajectory is not None and trajectory.route not in set(metadata["excluded_routes"]):
      trajectories.append(trajectory)
    if index % 25 == 0 or index == len(segments):
      print(f"loaded validation trajectories {index}/{len(segments)}; usable={len(trajectories)}", flush=True)
  return trajectories


def build_batch(trajectories: list[plant_data.Trajectory], routes: set[str], history_steps: int,
                rollout_steps: int, max_windows: int, seed: int) -> RolloutBatch:
  windows: list[tuple[plant_data.Trajectory, int]] = []
  for trajectory in trajectories:
    if trajectory.route not in routes:
      continue
    values = trajectory.values
    for source in range(history_steps - 1, len(trajectory.times) - rollout_steps):
      future = slice(source, source + rollout_steps + 1)
      if values["lat_active"][future].min() < 0.5 or values["driver_overlay"][future].max() > 0.5:
        continue
      if values["saturated"][future].max() > 0.5 or values["v_ego"][source] < 3.0:
        continue
      if np.max(np.diff(trajectory.times[source:source + rollout_steps + 1])) >= 0.09:
        continue
      windows.append((trajectory, source))
  if len(windows) > max_windows:
    rng = np.random.default_rng(seed)
    windows = [windows[index] for index in np.sort(rng.choice(len(windows), max_windows, replace=False))]
  if not windows:
    raise SystemExit(f"No clean rollout windows from {len(routes)} routes.")

  history = np.stack([
    np.stack([
      np.asarray([trajectory.values[name][source - lag] for name in plant_data.BASE_FEATURES], dtype=np.float32)
      for lag in range(history_steps)
    ])
    for trajectory, source in windows
  ])

  def future_field(name: str, offset: int) -> np.ndarray:
    return np.stack([
      trajectory.values[name][source + offset:source + offset + rollout_steps]
      for trajectory, source in windows
    ])

  return RolloutBatch(
    history=history,
    desired=future_field("desired_lateral_accel", 0),
    jerk=future_field("desired_lateral_jerk", 0),
    v_ego=future_field("v_ego", 0),
    a_ego=future_field("a_ego", 0),
    logged_applied=future_field("applied_torque", 0),
    logged_controller_output=future_field("controller_output", 0),
    initial_i=np.asarray([trajectory.values["controller_i"][source] for trajectory, source in windows]),
    target_desired=future_field("desired_lateral_accel", 1),
    target_jerk=future_field("desired_lateral_jerk", 1),
    segments=[trajectory.segment for trajectory, _ in windows],
  )


class ClosedLoopEvaluator:
  def __init__(self, model: Any, batch: RolloutBatch, sample_period_s: float, wobble_weight: float = 0.12):
    self.model = model
    self.batch = batch
    self.dt = sample_period_s
    self.wobble_weight = wobble_weight
    self.base_index = {name: index for index, name in enumerate(plant_data.BASE_FEATURES)}
    self.state_indexes = [self.base_index[name] for name in plant_data.STATE_FEATURES]

  def rollout_many(self, tunes: list[tune_math.Tune]) -> list[RolloutTrace]:
    candidate_count = len(tunes)
    window_count = len(self.batch.history)
    history = np.tile(self.batch.history, (candidate_count, 1, 1))
    integral = np.tile(self.batch.initial_i.astype(np.float64), candidate_count)
    errors = []
    command_deltas = []
    commands = []
    states = []
    for step in range(self.batch.desired.shape[1]):
      desired = np.tile(self.batch.desired[:, step].astype(np.float64), candidate_count)
      jerk = np.tile(self.batch.jerk[:, step].astype(np.float64), candidate_count)
      speed = np.tile(self.batch.v_ego[:, step].astype(np.float64), candidate_count)
      actual = history[:, 0, self.base_index["actual_lateral_accel"]].astype(np.float64)
      kp = np.interp(speed, KP_SPEEDS, KP_VALUES)
      low_speed_factor = (np.interp(speed, LOW_SPEED_X, LOW_SPEED_Y) / np.maximum(speed, 0.3)) ** 2
      error = desired - actual
      error_lsf = error * (1.0 + low_speed_factor / np.maximum(kp, 1e-3))
      p = kp * error_lsf
      integral += KI * self.dt * error_lsf
      controller_output = np.empty_like(desired)
      for candidate_index, tune in enumerate(tunes):
        candidate = slice(candidate_index * window_count, (candidate_index + 1) * window_count)
        feedforward, factor = tune_math.tune_terms(tune, desired[candidate], jerk[candidate], speed[candidate], error_lsf[candidate])
        controller_output[candidate] = np.clip(
          -(p[candidate] + integral[candidate] + feedforward) / factor, -1.0, 1.0,
        )
      limiter_gap = np.tile(self.batch.logged_applied[:, step] - self.batch.logged_controller_output[:, step], candidate_count)
      applied = np.clip(controller_output + limiter_gap, -1.0, 1.0)
      history[:, 0, self.base_index["applied_torque"]] = applied

      prediction_delta = self.model.predict(history.reshape((len(history), -1)))
      next_state = history[:, 0, self.state_indexes] + prediction_delta
      errors.append(next_state[:, 0] - np.tile(self.batch.target_desired[:, step], candidate_count))
      command_deltas.append(applied - np.tile(self.batch.logged_applied[:, step], candidate_count))
      commands.append(applied)
      states.append(next_state)

      next_base = history[:, 0].copy()
      next_base[:, self.base_index["v_ego"]] = np.tile(
        self.batch.v_ego[:, min(step + 1, self.batch.v_ego.shape[1] - 1)], candidate_count,
      )
      next_base[:, self.base_index["a_ego"]] = np.tile(
        self.batch.a_ego[:, min(step + 1, self.batch.a_ego.shape[1] - 1)], candidate_count,
      )
      next_base[:, self.state_indexes] = next_state
      history[:, 1:] = history[:, :-1]
      history[:, 0] = next_base
    combined = RolloutTrace(
      errors=np.stack(errors, axis=1).reshape((candidate_count, window_count, -1)),
      command_delta=np.stack(command_deltas, axis=1).reshape((candidate_count, window_count, -1)),
      commands=np.stack(commands, axis=1).reshape((candidate_count, window_count, -1)),
      states=np.stack(states, axis=1).reshape((candidate_count, window_count, -1, len(self.state_indexes))),
    )
    return [
      RolloutTrace(combined.errors[index], combined.command_delta[index], combined.commands[index], combined.states[index])
      for index in range(candidate_count)
    ]

  def rollout(self, tune: tune_math.Tune) -> RolloutTrace:
    return self.rollout_many([tune])[0]

  @staticmethod
  def _rms(values: np.ndarray, mask: np.ndarray) -> float:
    selected = values[mask]
    return float(np.sqrt(np.mean(selected ** 2))) if len(selected) else 0.0

  def wobble_metrics(self, trace: RolloutTrace) -> dict[str, float]:
    desired = self.batch.target_desired
    jerk = self.batch.target_jerk
    center = np.abs(desired) < 0.08
    steady = (np.abs(desired) >= 0.08) & (np.abs(jerk) < 0.08)
    quiet = center | steady

    rate_index = plant_data.STATE_FEATURES.index("signed_steering_rate_deg_s")
    steering_rate = trace.states[:, :, rate_index]
    initial_rate = self.batch.history[:, 0, self.base_index["signed_steering_rate_deg_s"]]
    steering_accel = np.diff(np.column_stack((initial_rate, steering_rate)), axis=1) / self.dt
    initial_command = self.batch.history[:, 0, self.base_index["applied_torque"]]
    command_slew = np.diff(np.column_stack((initial_command, trace.commands)), axis=1) / self.dt

    center_rate_rms = self._rms(steering_rate, center)
    steady_rate_rms = self._rms(steering_rate, steady)
    quiet_steering_accel_rms = self._rms(steering_accel, quiet)
    quiet_command_slew_rms = self._rms(command_slew, quiet)
    # Dimensionless combination calibrated to the observed Ioniq 5 ranges. Keeping the
    # components visible makes it possible to retune the weighting from measured drives.
    wobble_score = (
      0.35 * center_rate_rms / 10.0
      + 0.35 * steady_rate_rms / 10.0
      + 0.15 * quiet_steering_accel_rms / 200.0
      + 0.15 * quiet_command_slew_rms / 2.5
    )
    return {
      "score": float(wobble_score),
      "center_steering_rate_rms_deg_s": center_rate_rms,
      "steady_steering_rate_rms_deg_s": steady_rate_rms,
      "quiet_steering_accel_rms_deg_s2": quiet_steering_accel_rms,
      "quiet_command_slew_rms_per_s": quiet_command_slew_rms,
      "quiet_steering_rate_p95_deg_s": float(np.percentile(np.abs(steering_rate[quiet]), 95)) if np.any(quiet) else 0.0,
      "quiet_command_slew_p95_per_s": float(np.percentile(np.abs(command_slew[quiet]), 95)) if np.any(quiet) else 0.0,
    }

  def evaluate_trace(self, tune: tune_math.Tune, trace: RolloutTrace) -> dict[str, Any]:
    errors = trace.errors
    command_delta = trace.command_delta
    commands = trace.commands
    desired = self.batch.target_desired
    jerk = self.batch.target_jerk
    flat_error = errors.ravel()
    flat_desired = desired.ravel()
    flat_jerk = jerk.ravel()
    masks = tune_math.phase_masks(flat_desired, flat_jerk)
    phases = {}
    for name, mask in masks.items():
      values = flat_error[mask]
      phases[name] = {
        "samples": int(mask.sum()),
        "rmse": float(np.sqrt(np.mean(values ** 2))),
        "mae": float(np.mean(np.abs(values))),
        "bias": float(np.mean(values)),
      }
    transition_rmse = float(np.mean([phases[name]["rmse"] for name in ("turn_in_left", "turn_in_right", "unwind_left", "unwind_right")]))
    active = np.abs(desired) >= 0.08
    command_delta_rms = float(np.sqrt(np.mean(command_delta[active] ** 2)))
    command_rms = float(np.sqrt(np.mean(commands[active] ** 2)))
    wobble = self.wobble_metrics(trace)
    objective = (
      transition_rmse
      + 0.20 * phases["center"]["rmse"]
      + 0.20 * phases["steady"]["rmse"]
      + 0.08 * command_delta_rms
      + 0.02 * command_rms
      + self.wobble_weight * wobble["score"]
    )
    horizons = {
      f"{(step + 1) * self.dt:.2f}s": {
        "rmse": float(np.sqrt(np.mean(errors[:, step] ** 2))),
        "mae": float(np.mean(np.abs(errors[:, step]))),
        "bias": float(np.mean(errors[:, step])),
      }
      for step in range(errors.shape[1])
    }
    return {
      "name": tune.name,
      "objective": objective,
      "balanced_transition_rmse": transition_rmse,
      "command_delta_rms": command_delta_rms,
      "command_rms": command_rms,
      "wobble": wobble,
      "phases": phases,
      "horizons": horizons,
      "tune": asdict(tune),
    }

  def evaluate_many(self, tunes: list[tune_math.Tune]) -> list[dict[str, Any]]:
    return [self.evaluate_trace(tune, trace) for tune, trace in zip(tunes, self.rollout_many(tunes), strict=True)]

  def evaluate(self, tune: tune_math.Tune) -> dict[str, Any]:
    return self.evaluate_many([tune])[0]


def optimize(evaluator: ClosedLoopEvaluator, start: tune_math.Tune,
             max_path_regression_fraction: float) -> tuple[tune_math.Tune, list[dict[str, Any]]]:
  current = replace(start, name="closed_loop_start")
  best = evaluator.evaluate(current)
  history = [best]
  path_limit = best["balanced_transition_rmse"] * (1.0 + max_path_regression_fraction)
  center_limit = best["phases"]["center"]["rmse"] * (1.0 + max_path_regression_fraction)
  fields = {
    "base_lat_accel_factor_mult": (0.02, 1.12, 1.42),
    "ff_reduction_left": (0.03, 0.0, 0.40),
    "ff_reduction_right": (0.03, 0.0, 0.45),
    "turn_in_boost_left": (0.04, 0.0, 0.55),
    "turn_in_boost_right": (0.04, 0.0, 0.45),
    "unwind_taper_left": (0.06, 0.35, 1.40),
    "unwind_taper_right": (0.06, 0.35, 1.40),
    "turn_in_threshold_reduction_left": (0.02, 0.0, 0.25),
    "turn_in_threshold_reduction_right": (0.02, 0.0, 0.25),
    "unwind_threshold_increase_left": (0.04, 0.0, 0.90),
    "unwind_threshold_increase_right": (0.04, 0.0, 0.90),
    "center_taper_max": (0.03, 0.0, 0.32),
    "steady_high_lat_taper": (0.01, 0.0, 0.10),
    "sustained_turn_in_ff_boost_left": (0.03, 0.0, 0.35),
    "sustained_turn_in_ff_boost_right": (0.03, 0.0, 0.35),
  }
  for pass_index in range(3):
    improved = False
    for field, (initial_step, low, high) in fields.items():
      step = initial_step / (2 ** pass_index)
      values = sorted({float(np.clip(getattr(current, field) + offset * step, low, high)) for offset in (-2, -1, 0, 1, 2)})
      candidates = [replace(current, name=f"search_{field}_{value:.4f}", **{field: value}) for value in values]
      results = evaluator.evaluate_many(candidates)
      history.extend(results)
      eligible = [
        index for index, result in enumerate(results)
        if result["balanced_transition_rmse"] <= path_limit and result["phases"]["center"]["rmse"] <= center_limit
      ]
      if not eligible:
        raise RuntimeError(f"No path-safe candidate remained while searching {field}.")
      winner_index = min(eligible, key=lambda index: results[index]["objective"])
      if results[winner_index]["objective"] + 1e-8 < best["objective"]:
        current = replace(candidates[winner_index], name="closed_loop_optimized")
        best = dict(results[winner_index])
        best["name"] = current.name
        best["tune"] = asdict(current)
        history.append(best)
        improved = True
      print(f"pass={pass_index + 1} field={field:42s} best={best['objective']:.6f}", flush=True)
    if not improved:
      break
  return current, history


def routes_matching_prefixes(routes: list[str], prefixes: list[str]) -> set[str]:
  selected = {route for route in routes if any(route.startswith(prefix) for prefix in prefixes)}
  missing = [prefix for prefix in prefixes if not any(route.startswith(prefix) for route in routes)]
  if missing:
    raise SystemExit(f"No validation route matches prefix(es): {', '.join(missing)}")
  return selected


def main() -> None:
  parser = argparse.ArgumentParser(description="Optimize the Ioniq 5 torque tune in an aligned multi-step plant rollout.")
  parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
  parser.add_argument("--starting-tune", type=Path, default=None,
                      help="Prior optimization report to start from; defaults to the current Ioniq 5 code tune.")
  parser.add_argument("--starting-tune-variant", choices=("current", "optimized"), default="optimized",
                      help="Which holdout tune to read from --starting-tune.")
  parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
  parser.add_argument("--rollout-steps", type=int, default=40, help="Closed-loop horizon (40 = 2.0 s at the default sample period).")
  parser.add_argument("--max-search-windows", type=int, default=1500)
  parser.add_argument("--max-holdout-windows", type=int, default=2000)
  parser.add_argument("--max-diagnostic-windows", type=int, default=1000)
  parser.add_argument("--search-route-prefix", action="append", default=[])
  parser.add_argument("--holdout-route-prefix", action="append", default=[])
  parser.add_argument("--diagnostic-route-prefix", action="append", default=[])
  parser.add_argument("--wobble-weight", type=float, default=0.12)
  parser.add_argument("--max-path-regression", type=float, default=0.01,
                      help="Maximum permitted fractional regression in transition and center path RMSE.")
  parser.add_argument("--random-state", type=int, default=23)
  args = parser.parse_args()

  artifact = load(args.model)
  model = artifact["plant_model"]
  metadata = artifact["metadata"]
  trajectories = load_validation_trajectories(metadata)
  routes = sorted({trajectory.route for trajectory in trajectories})
  if len(routes) < 2:
    raise SystemExit(f"Need at least two plant-validation routes; found {len(routes)}.")
  if args.search_route_prefix or args.holdout_route_prefix:
    search_routes = routes_matching_prefixes(routes, args.search_route_prefix) if args.search_route_prefix else set(routes)
    holdout_routes = routes_matching_prefixes(routes, args.holdout_route_prefix) if args.holdout_route_prefix else set(routes)
    if not args.search_route_prefix:
      search_routes -= holdout_routes
    if not args.holdout_route_prefix:
      holdout_routes -= search_routes
    if search_routes & holdout_routes:
      raise SystemExit("Search and holdout route selections overlap.")
    if not search_routes or not holdout_routes:
      raise SystemExit("Search and holdout route selections must both contain at least one route.")
  else:
    search_routes = set(routes[::2])
    holdout_routes = set(routes[1::2])
  history_steps = int(metadata["history_steps"])
  sample_period_s = float(metadata["sample_period_s"])
  search_batch = build_batch(trajectories, search_routes, history_steps, args.rollout_steps, args.max_search_windows, args.random_state)
  holdout_batch = build_batch(trajectories, holdout_routes, history_steps, args.rollout_steps, args.max_holdout_windows, args.random_state + 1)
  print(f"closed-loop windows: search={len(search_batch.history)} holdout={len(holdout_batch.history)}")

  starting_tune = current_tune(args.starting_tune, args.starting_tune_variant)
  search_evaluator = ClosedLoopEvaluator(model, search_batch, sample_period_s, args.wobble_weight)
  optimized, history = optimize(search_evaluator, starting_tune, args.max_path_regression)
  holdout_evaluator = ClosedLoopEvaluator(model, holdout_batch, sample_period_s, args.wobble_weight)
  diagnostic_routes = routes_matching_prefixes(routes, args.diagnostic_route_prefix) if args.diagnostic_route_prefix else set()
  route_diagnostics = {}
  for index, route in enumerate(sorted(diagnostic_routes)):
    batch = build_batch(trajectories, {route}, history_steps, args.rollout_steps, args.max_diagnostic_windows,
                        args.random_state + 100 + index)
    evaluator = ClosedLoopEvaluator(model, batch, sample_period_s, args.wobble_weight)
    route_diagnostics[route] = {
      "windows": len(batch.history),
      "measured_baseline_tune": evaluator.evaluate(tune_math.UPSTREAM_TUNE),
      "starting": evaluator.evaluate(starting_tune),
      "optimized": evaluator.evaluate(optimized),
    }
  result = {
    "plant_model": str(args.model),
    "starting_tune_report": str(args.starting_tune),
    "alignment": "predicted state at each rollout timestamp is scored against desired lateral acceleration at that same timestamp",
    "rollout_steps": args.rollout_steps,
    "sample_period_s": sample_period_s,
    "search_routes": sorted(search_routes),
    "holdout_routes": sorted(holdout_routes),
    "search_windows": len(search_batch.history),
    "holdout_windows": len(holdout_batch.history),
    "objective": f"balanced path RMSE + center RMSE + command cost + {args.wobble_weight:g} normalized wobble score",
    "wobble_weight": args.wobble_weight,
    "max_path_regression_fraction": args.max_path_regression,
    "wobble_horizon_s": args.rollout_steps * sample_period_s,
    "search": {
      "current": search_evaluator.evaluate(starting_tune),
      "optimized": search_evaluator.evaluate(optimized),
    },
    "holdout": {
      "current": holdout_evaluator.evaluate(starting_tune),
      "optimized": holdout_evaluator.evaluate(optimized),
    },
    "search_evaluations": len(history),
    "route_diagnostics": route_diagnostics,
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(json.dumps({"search": result["search"], "holdout": result["holdout"]}, indent=2))
  print(f"report: {args.output}")


if __name__ == "__main__":
  main()
