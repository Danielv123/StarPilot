#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, replace
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

try:
  import torch
except ModuleNotFoundError as e:
  raise SystemExit("Run with torch, numpy, joblib, pycapnp, and zstandard installed.") from e

from openpilot.tools.tuning import optimize_ioniq5_closed_loop as legacy
from openpilot.tools.tuning import optimize_ioniq5_response_tune as tune_math
from openpilot.tools.tuning import delay_alignment
from openpilot.tools.tuning import train_lateral_plant_model as plant_data
from openpilot.tools.tuning import train_neural_lateral_plant as neural_plant


DEFAULT_PLANT = REPO_ROOT / "artifacts/tuning/neural_lateral_plant_20260723/neural_lateral_plant.pt"
DEFAULT_LOG_ROOT = Path(r"D:\comma_driving_logs\10.30.1.75\realdata")
DEFAULT_OUTPUT = REPO_ROOT / "artifacts/tuning/ioniq5_neural_closed_loop_20260724/optimization.json"
MEASUREMENT_RATE_FILTER_RC = 1.0 / (2.0 * math.pi * (2.5 - 0.5))


class NeuralEnsemblePredictor:
  def __init__(self, model_path: Path, device: torch.device):
    self.models, self.stats, self.payload = neural_plant.load_ensemble_artifact(
      model_path, device, differentiable=False,
    )
    self.device = device
    self.config = neural_plant.ModelConfig(**self.payload["config"])
    self.disagreement_steps: list[np.ndarray] = []

  def clear_disagreement(self) -> None:
    self.disagreement_steps.clear()

  def predict(self, flattened_history: np.ndarray) -> np.ndarray:
    history = torch.as_tensor(
      flattened_history.reshape(
        len(flattened_history), self.config.history_steps, len(plant_data.BASE_FEATURES),
      ),
      dtype=torch.float32,
      device=self.device,
    )
    with torch.no_grad():
      prediction, disagreement = neural_plant.ensemble_predict_delta(
        self.models, history, self.stats,
      )
    self.disagreement_steps.append(disagreement.cpu().numpy())
    return prediction.cpu().numpy()

  def disagreement_summary(self) -> dict[str, Any]:
    if not self.disagreement_steps:
      return {"mean": None, "p95": None, "by_state": {}}
    values = np.stack(self.disagreement_steps, axis=1)
    state_std = self.stats["state_std"].detach().cpu().numpy()
    normalized = values / state_std
    return {
      "mean": float(np.mean(normalized)),
      "p95": float(np.percentile(normalized, 95)),
      "by_state": {
        name: {
          "mean": float(np.mean(normalized[..., index])),
          "p95": float(np.percentile(normalized[..., index], 95)),
        }
        for index, name in enumerate(plant_data.STATE_FEATURES)
      },
    }


class DampedClosedLoopEvaluator(legacy.ClosedLoopEvaluator):
  def __init__(self, model: Any, batch: legacy.RolloutBatch, sample_period_s: float,
               wobble_weight: float, damping_gain: float,
               turn_exit_damping_gain: float | None = None,
               turn_exit_damping_gain_right: float | None = None,
               reversal_damping_gain: float | None = None,
               reversal_hold_seconds: float = 0.40,
               steering_rate_feedback_gain: float = 0.0,
               highway_damping_gain: float | None = None,
               damping_reference_gain: float = 0.0):
    super().__init__(model, batch, sample_period_s, wobble_weight)
    self.damping_gain = damping_gain
    self.turn_exit_damping_gain = (
      damping_gain if turn_exit_damping_gain is None else turn_exit_damping_gain
    )
    self.turn_exit_damping_gain_right = (
      self.turn_exit_damping_gain
      if turn_exit_damping_gain_right is None else turn_exit_damping_gain_right
    )
    self.reversal_damping_gain = (
      self.turn_exit_damping_gain
      if reversal_damping_gain is None else reversal_damping_gain
    )
    self.reversal_hold_seconds = reversal_hold_seconds
    self.steering_rate_feedback_gain = steering_rate_feedback_gain
    self.highway_damping_gain = (
      damping_gain if highway_damping_gain is None else highway_damping_gain
    )
    self.damping_reference_gain = damping_reference_gain

  def _initial_measurement_rate_filter(self) -> np.ndarray:
    actual_index = self.base_index["actual_lateral_accel"]
    oldest_to_newest = self.batch.history[:, ::-1, actual_index].astype(np.float64)
    filtered = np.zeros(len(oldest_to_newest), dtype=np.float64)
    alpha = self.dt / (MEASUREMENT_RATE_FILTER_RC + self.dt)
    previous = oldest_to_newest[:, 0]
    for index in range(1, oldest_to_newest.shape[1]):
      actual = oldest_to_newest[:, index]
      raw_rate = (actual - previous) / self.dt
      filtered = (1.0 - alpha) * filtered + alpha * raw_rate
      previous = actual
    return filtered

  def rollout_many(self, tunes: list[tune_math.Tune]) -> list[legacy.RolloutTrace]:
    candidate_count = len(tunes)
    window_count = len(self.batch.history)
    history = np.tile(self.batch.history, (candidate_count, 1, 1))
    integral = np.tile(self.batch.initial_i.astype(np.float64), candidate_count)
    errors = []
    command_deltas = []
    commands = []
    states = []
    turn_exit_remaining = np.zeros(candidate_count * window_count, dtype=np.int32)
    turn_exit_side = np.ones(candidate_count * window_count, dtype=np.float64)
    turn_exit_lookback_steps = max(1, int(round(0.75 / self.dt)))
    reversal_remaining = np.zeros(candidate_count * window_count, dtype=np.int32)
    reversal_hold_steps = max(1, int(round(self.reversal_hold_seconds / self.dt)))
    measurement_rate_filter = np.tile(
      self._initial_measurement_rate_filter(), candidate_count,
    )
    measurement_rate_alpha = self.dt / (MEASUREMENT_RATE_FILTER_RC + self.dt)
    for step in range(self.batch.desired.shape[1]):
      desired = np.tile(self.batch.desired[:, step].astype(np.float64), candidate_count)
      jerk = np.tile(self.batch.jerk[:, step].astype(np.float64), candidate_count)
      response_jerk_source = (
        self.batch.target_jerk if self.batch.response_jerk is None else self.batch.response_jerk
      )
      response_jerk = np.tile(
        response_jerk_source[:, step].astype(np.float64), candidate_count,
      )
      speed = np.tile(self.batch.v_ego[:, step].astype(np.float64), candidate_count)
      actual = history[:, 0, self.base_index["actual_lateral_accel"]].astype(np.float64)
      previous_actual = history[:, 1, self.base_index["actual_lateral_accel"]].astype(np.float64)
      steering_rate = history[:, 0, self.base_index["signed_steering_rate_deg_s"]].astype(np.float64)
      previous_steering_rate = history[:, 1, self.base_index["signed_steering_rate_deg_s"]].astype(np.float64)
      raw_measurement_rate = (actual - previous_actual) / self.dt
      measurement_rate_filter = (
        (1.0 - measurement_rate_alpha) * measurement_rate_filter
        + measurement_rate_alpha * raw_measurement_rate
      )
      measurement_rate = np.clip(measurement_rate_filter, -2.5, 2.5)
      kp = np.interp(speed, legacy.KP_SPEEDS, legacy.KP_VALUES)
      low_speed_factor = (
        np.interp(speed, legacy.LOW_SPEED_X, legacy.LOW_SPEED_Y) / np.maximum(speed, 0.3)
      ) ** 2
      error = desired - actual
      error_lsf = error * (1.0 + low_speed_factor / np.maximum(kp, 1e-3))
      p = kp * error_lsf
      integral += legacy.KI * self.dt * error_lsf
      unwind = (
        (speed >= 8.0) & (speed < 15.0)
        & (np.abs(desired) >= 0.12)
        & (desired * jerk < -0.01)
      )
      turn_exit_remaining = np.maximum(turn_exit_remaining - 1, 0)
      turn_exit_remaining[unwind] = turn_exit_lookback_steps
      turn_exit_side[unwind] = np.sign(desired[unwind])
      turn_exit = (
        (speed >= 8.0) & (speed < 15.0)
        & (unwind | ((turn_exit_remaining > 0) & (np.abs(desired) < 0.35)))
      )
      rate_reversal = (
        turn_exit
        & (steering_rate * previous_steering_rate < 0.0)
        & (np.minimum(np.abs(steering_rate), np.abs(previous_steering_rate)) >= 0.5)
      )
      reversal_remaining = np.maximum(reversal_remaining - 1, 0)
      reversal_remaining[rate_reversal] = reversal_hold_steps
      reversal_recovery = turn_exit & (reversal_remaining > 0)
      turn_exit_gain = np.where(
        turn_exit_side >= 0.0,
        self.turn_exit_damping_gain,
        self.turn_exit_damping_gain_right,
      )
      base_damping_gain = np.interp(
        speed,
        (0.0, 18.0, 22.0, 30.0),
        (
          self.damping_gain,
          self.damping_gain,
          self.highway_damping_gain,
          self.highway_damping_gain,
        ),
      )
      effective_damping_gain = np.where(turn_exit, turn_exit_gain, base_damping_gain)
      effective_damping_gain = np.where(
        reversal_recovery, self.reversal_damping_gain, effective_damping_gain,
      )
      # The rack responds to the command from one physical-delay interval ago. Referencing
      # that delayed command rate prevents expected post-ramp motion from looking like an
      # overshoot that needs a momentary counter-command. Keep the existing turn-exit
      # behavior isolated; its reversal schedule was validated separately.
      damping_reference_jerk = np.where(turn_exit, 0.0, response_jerk)
      d = effective_damping_gain * (
        self.damping_reference_gain * damping_reference_jerk - measurement_rate
      )
      steering_rate_feedback = np.where(
        reversal_recovery, self.steering_rate_feedback_gain * steering_rate, 0.0,
      )
      controller_output = np.empty_like(desired)
      for candidate_index, tune in enumerate(tunes):
        candidate = slice(candidate_index * window_count, (candidate_index + 1) * window_count)
        feedforward, factor = tune_math.tune_terms(
          tune, desired[candidate], jerk[candidate], speed[candidate], error_lsf[candidate],
        )
        controller_output[candidate] = np.clip(
          -(
            p[candidate] + integral[candidate] + d[candidate]
            + steering_rate_feedback[candidate] + feedforward
          ) / factor,
          -1.0, 1.0,
        )
      limiter_gap = np.tile(
        self.batch.logged_applied[:, step] - self.batch.logged_controller_output[:, step],
        candidate_count,
      )
      applied = np.clip(controller_output + limiter_gap, -1.0, 1.0)
      history[:, 0, self.base_index["applied_torque"]] = applied
      prediction_delta = self.model.predict(history.reshape((len(history), -1)))
      next_state = history[:, 0, self.state_indexes] + prediction_delta
      errors.append(
        next_state[:, 0] - np.tile(self.batch.target_desired[:, step], candidate_count),
      )
      command_deltas.append(
        applied - np.tile(self.batch.logged_applied[:, step], candidate_count),
      )
      commands.append(applied)
      states.append(next_state)

      next_base = history[:, 0].copy()
      next_base[:, self.base_index["v_ego"]] = np.tile(
        self.batch.v_ego[:, min(step + 1, self.batch.v_ego.shape[1] - 1)],
        candidate_count,
      )
      next_base[:, self.base_index["a_ego"]] = np.tile(
        self.batch.a_ego[:, min(step + 1, self.batch.a_ego.shape[1] - 1)],
        candidate_count,
      )
      next_base[:, self.state_indexes] = next_state
      history[:, 1:] = history[:, :-1]
      history[:, 0] = next_base
    combined = legacy.RolloutTrace(
      errors=np.stack(errors, axis=1).reshape((candidate_count, window_count, -1)),
      command_delta=np.stack(command_deltas, axis=1).reshape((candidate_count, window_count, -1)),
      commands=np.stack(commands, axis=1).reshape((candidate_count, window_count, -1)),
      states=np.stack(states, axis=1).reshape(
        (candidate_count, window_count, -1, len(self.state_indexes)),
      ),
    )
    return [
      legacy.RolloutTrace(
        combined.errors[index],
        combined.command_delta[index],
        combined.commands[index],
        combined.states[index],
      )
      for index in range(candidate_count)
    ]


def current_code_evaluator(model: Any, batch: legacy.RolloutBatch,
                           sample_period_s: float, wobble_weight: float,
                           args: argparse.Namespace) -> DampedClosedLoopEvaluator:
  return DampedClosedLoopEvaluator(
    model, batch, sample_period_s, wobble_weight,
    args.baseline_damping_gain,
    args.baseline_damping_gain,
    args.baseline_damping_gain,
    args.baseline_reversal_damping_gain,
    args.baseline_reversal_hold_seconds,
    highway_damping_gain=getattr(
      args, "baseline_highway_damping_gain", args.baseline_damping_gain,
    ),
    damping_reference_gain=getattr(args, "baseline_damping_reference_gain", 0.0),
  )


def discover_trajectories(root: Path, prefixes: list[str],
                          sample_step: int) -> list[plant_data.Trajectory]:
  paths = sorted({
    path.absolute()
    for prefix in prefixes
    for path in root.glob(f"{prefix}--*/rlog.zst")
  })
  if not paths:
    raise SystemExit(f"No rlogs matched route prefixes: {', '.join(prefixes)}")
  trajectories: list[plant_data.Trajectory] = []
  for index, path in enumerate(paths, 1):
    trajectory = plant_data.read_trajectory(path, "hyundai", "IONIQ5", sample_step=sample_step)
    if trajectory is not None:
      desired = trajectory.values["desired_lateral_accel"]
      jerk = trajectory.values["desired_lateral_jerk"]
      if np.count_nonzero(np.abs(jerk) > 1e-6) < max(10, len(jerk) // 100):
        dt = np.maximum(np.gradient(trajectory.times), 1e-3)
        trajectory.values["desired_lateral_jerk"] = np.clip(
          np.gradient(desired) / dt, -2.5, 2.5,
        )
      trajectories.append(trajectory)
    if index % 10 == 0 or index == len(paths):
      print(f"loaded {index}/{len(paths)} rlogs; usable={len(trajectories)}", flush=True)
  return trajectories


def make_batch(trajectories: list[plant_data.Trajectory], history_steps: int,
               rollout_steps: int, max_windows: int, seed: int,
               response_delay_steps: int = 0) -> legacy.RolloutBatch:
  routes = {trajectory.route for trajectory in trajectories}
  return legacy.build_batch(
    trajectories, routes, history_steps, rollout_steps, max_windows, seed,
    response_delay_steps,
  )


def tune_from_report(report: dict[str, Any], name: str) -> tune_math.Tune:
  values = dict(report["tune"])
  values["name"] = name
  return tune_math.Tune(**values)


def unique_candidates(history: list[dict[str, Any]], limit: int) -> list[tune_math.Tune]:
  ordered = sorted(history, key=lambda item: float(item["objective"]))
  seen: set[tuple[tuple[str, Any], ...]] = set()
  unique_values: list[dict[str, Any]] = []
  for item in ordered:
    values = dict(item["tune"])
    values.pop("name", None)
    key = tuple(sorted(values.items()))
    if key in seen:
      continue
    seen.add(key)
    unique_values.append(values)
  if len(unique_values) <= limit:
    selected_values = unique_values
  else:
    best_count = max(1, limit // 2)
    spread_count = limit - best_count
    spread_indexes = np.linspace(
      best_count, len(unique_values) - 1, spread_count, dtype=int,
    ).tolist()
    indexes = list(range(best_count)) + spread_indexes
    selected_values = [unique_values[index] for index in indexes]
  return [
    tune_math.Tune(name=f"candidate_{index:02d}", **values)
    for index, values in enumerate(selected_values)
  ]


def path_safe(candidate: dict[str, Any], baseline: dict[str, Any],
              max_regression: float) -> bool:
  phase_safe = all(
    candidate["phases"][name]["rmse"]
    <= baseline["phases"][name]["rmse"] * (1.0 + max_regression)
    for name in ("turn_in_left", "turn_in_right", "unwind_left", "unwind_right")
  )
  return (
    candidate["balanced_transition_rmse"]
    <= baseline["balanced_transition_rmse"] * (1.0 + max_regression)
    and candidate["phases"]["center"]["rmse"]
    <= baseline["phases"]["center"]["rmse"] * (1.0 + max_regression)
    and phase_safe
  )


def turn_exit_safe(candidate: dict[str, Any], baseline: dict[str, Any],
                   max_score_regression: float,
                   max_reversal_regression: float) -> bool:
  candidate_wobble = candidate["wobble"]
  baseline_wobble = baseline["wobble"]
  return (
    candidate_wobble["turn_exit_score"]
    <= baseline_wobble["turn_exit_score"] * (1.0 + max_score_regression)
    and candidate_wobble["turn_exit_rate_reversal_rms_deg_s"]
    <= baseline_wobble["turn_exit_rate_reversal_rms_deg_s"] * (1.0 + max_reversal_regression)
    and candidate_wobble["turn_exit_steering_rate_rms_deg_s"]
    <= baseline_wobble["turn_exit_steering_rate_rms_deg_s"] * 1.01
  )


def shape_safe(candidate: dict[str, Any], baseline: dict[str, Any],
               max_regression: float = 0.0) -> bool:
  return (
    candidate["shape"]["highway_score"]
    <= baseline["shape"]["highway_score"] * (1.0 + max_regression) + 1e-12
    and candidate["shape"]["ramp_hold_score"]
    <= baseline["shape"]["ramp_hold_score"] * (1.0 + max_regression) + 1e-12
  )


def promotion_improvements(
  validation: dict[str, Any],
  validation_baseline: dict[str, Any],
  acceptance: dict[str, Any],
  acceptance_baseline: dict[str, Any],
) -> dict[str, float]:
  def improvement(candidate: float, baseline: float) -> float:
    return (baseline - candidate) / max(abs(baseline), 1e-9)

  return {
    "validation_objective": improvement(
      validation["objective"], validation_baseline["objective"],
    ),
    "acceptance_objective": improvement(
      acceptance["objective"], acceptance_baseline["objective"],
    ),
    "validation_turn_exit_reversal": improvement(
      validation["wobble"]["turn_exit_rate_reversal_rms_deg_s"],
      validation_baseline["wobble"]["turn_exit_rate_reversal_rms_deg_s"],
    ),
    "acceptance_turn_exit_reversal": improvement(
      acceptance["wobble"]["turn_exit_rate_reversal_rms_deg_s"],
      acceptance_baseline["wobble"]["turn_exit_rate_reversal_rms_deg_s"],
    ),
    "validation_highway_shape": improvement(
      validation["shape"]["highway_score"],
      validation_baseline["shape"]["highway_score"],
    ),
    "acceptance_highway_shape": improvement(
      acceptance["shape"]["highway_score"],
      acceptance_baseline["shape"]["highway_score"],
    ),
    "validation_ramp_hold": improvement(
      validation["shape"]["ramp_hold_score"],
      validation_baseline["shape"]["ramp_hold_score"],
    ),
    "acceptance_ramp_hold": improvement(
      acceptance["shape"]["ramp_hold_score"],
      acceptance_baseline["shape"]["ramp_hold_score"],
    ),
  }


def evaluate_with_uncertainty(evaluator: legacy.ClosedLoopEvaluator,
                              predictor: NeuralEnsemblePredictor,
                              tune: tune_math.Tune) -> dict[str, Any]:
  predictor.clear_disagreement()
  result = evaluator.evaluate(tune)
  result["plant_disagreement"] = predictor.disagreement_summary()
  return result


def evaluate_many_chunked(evaluator: legacy.ClosedLoopEvaluator,
                          tunes: list[tune_math.Tune],
                          chunk_size: int = 20) -> list[dict[str, Any]]:
  results: list[dict[str, Any]] = []
  for start in range(0, len(tunes), chunk_size):
    results.extend(evaluator.evaluate_many(tunes[start:start + chunk_size]))
  return results


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Tune the Ioniq 5 conventional controller through the neural plant ensemble.",
  )
  parser.add_argument("--plant-model", type=Path, default=DEFAULT_PLANT)
  parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
  parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
  parser.add_argument("--search-route-prefix", action="append", default=[])
  parser.add_argument("--validation-route-prefix", action="append", default=[])
  parser.add_argument("--acceptance-route-prefix", action="append", default=[])
  parser.add_argument("--rollout-steps", type=int, default=100)
  parser.add_argument("--max-search-windows", type=int, default=300)
  parser.add_argument("--max-validation-windows", type=int, default=400)
  parser.add_argument("--max-acceptance-windows", type=int, default=500)
  parser.add_argument("--validation-candidates", type=int, default=20)
  parser.add_argument("--wobble-weight", type=float, default=0.35)
  parser.add_argument(
    "--response-delay-s",
    type=float,
    default=delay_alignment.DEFAULT_RESPONSE_DELAY_S,
    help="Physical command-to-response delay represented in the tracking target.",
  )
  parser.add_argument("--max-path-regression", type=float, default=0.01)
  parser.add_argument("--max-acceptance-regression", type=float, default=0.02)
  parser.add_argument("--max-disagreement-regression", type=float, default=0.10)
  parser.add_argument("--max-objective-regression", type=float, default=0.0001)
  parser.add_argument("--min-promotion-improvement", type=float, default=0.004)
  parser.add_argument("--max-turn-exit-score-regression", type=float, default=0.0001)
  parser.add_argument("--max-turn-exit-reversal-regression", type=float, default=0.0)
  parser.add_argument("--baseline-damping-gain", type=float, default=0.02)
  parser.add_argument("--baseline-highway-damping-gain", type=float, default=0.07)
  parser.add_argument("--baseline-damping-reference-gain", type=float, default=0.0)
  parser.add_argument("--baseline-reversal-damping-gain", type=float, default=0.0175)
  parser.add_argument("--baseline-reversal-hold-seconds", type=float, default=0.60)
  parser.add_argument("--damping-gain", type=float, action="append", default=[])
  parser.add_argument("--highway-damping-gain", type=float, action="append", default=[])
  parser.add_argument("--turn-exit-damping-gain", type=float, action="append", default=[])
  parser.add_argument("--turn-exit-damping-gain-right", type=float, action="append", default=[])
  parser.add_argument("--reversal-damping-gain", type=float, action="append", default=[])
  parser.add_argument("--reversal-hold-seconds", type=float, action="append", default=[])
  parser.add_argument("--steering-rate-feedback-gain", type=float, action="append", default=[])
  parser.add_argument(
    "--damping-reference-gain", type=float, default=0.0,
    help="Scale the delay-aligned desired response rate in the derivative error.",
  )
  parser.add_argument(
    "--tune-search-field", action="append", default=[],
    help="Limit coordinate descent to selected tune fields; repeat for multiple fields.",
  )
  parser.add_argument(
    "--candidate-tune-report", type=Path,
    help="Evaluate the validation.optimized tune from an earlier report without repeating search.",
  )
  parser.add_argument(
    "--search-start-tune-report", type=Path,
    help="Start coordinate descent from the validation.optimized tune in an earlier report.",
  )
  parser.add_argument("--skip-tune-search", action="store_true")
  parser.add_argument("--device", default="cuda")
  parser.add_argument("--random-state", type=int, default=23)
  args = parser.parse_args()
  if args.candidate_tune_report is not None and args.search_start_tune_report is not None:
    parser.error("--candidate-tune-report and --search-start-tune-report are mutually exclusive")
  if not args.search_route_prefix:
    args.search_route_prefix = ["00000112"]
  if not args.validation_route_prefix:
    args.validation_route_prefix = ["00000113"]
  if not args.acceptance_route_prefix:
    args.acceptance_route_prefix = ["00000109", "0000010b"]
  if not args.damping_gain:
    args.damping_gain = [args.baseline_damping_gain]
  if not args.highway_damping_gain:
    args.highway_damping_gain = [0.05, 0.06, 0.07, 0.08]
  if not args.turn_exit_damping_gain:
    args.turn_exit_damping_gain = [args.baseline_damping_gain]
  if not args.turn_exit_damping_gain_right:
    args.turn_exit_damping_gain_right = args.turn_exit_damping_gain
  if not args.reversal_damping_gain:
    args.reversal_damping_gain = [0.015, 0.0175, args.baseline_damping_gain]
  if not args.reversal_hold_seconds:
    args.reversal_hold_seconds = [0.40, 0.60, 0.80]
  if not args.steering_rate_feedback_gain:
    args.steering_rate_feedback_gain = [0.0]
  if args.device.startswith("cuda") and not torch.cuda.is_available():
    raise SystemExit("CUDA was requested but is unavailable.")

  device = torch.device(args.device)
  predictor = NeuralEnsemblePredictor(args.plant_model, device)
  config = predictor.config
  try:
    response_delay_steps, effective_response_delay_s = delay_alignment.quantize_response_delay(
      args.response_delay_s, config.sample_period_s, config.history_steps - 1,
    )
  except ValueError as e:
    raise SystemExit(str(e)) from e
  search_trajectories = discover_trajectories(
    args.log_root, args.search_route_prefix, config.sample_step,
  )
  validation_trajectories = discover_trajectories(
    args.log_root, args.validation_route_prefix, config.sample_step,
  )
  acceptance_trajectories = discover_trajectories(
    args.log_root, args.acceptance_route_prefix, config.sample_step,
  )
  search_batch = make_batch(
    search_trajectories, config.history_steps, args.rollout_steps,
    args.max_search_windows, args.random_state, response_delay_steps,
  )
  validation_batch = make_batch(
    validation_trajectories, config.history_steps, args.rollout_steps,
    args.max_validation_windows, args.random_state + 1, response_delay_steps,
  )
  acceptance_batch = make_batch(
    acceptance_trajectories, config.history_steps, args.rollout_steps,
    args.max_acceptance_windows, args.random_state + 2, response_delay_steps,
  )
  window_summary = " ".join([
    f"device={device}",
    f"plant={config.name}",
    f"windows search={len(search_batch.history)}",
    f"validation={len(validation_batch.history)}",
    f"acceptance={len(acceptance_batch.history)}",
  ])
  print(window_summary, flush=True)

  baseline_tune = legacy.current_tune(None)
  search_start_tune = baseline_tune
  if args.search_start_tune_report is not None:
    search_start_payload = json.loads(args.search_start_tune_report.read_text(encoding="utf-8"))
    search_start_values = dict(search_start_payload["validation"]["optimized"]["tune"])
    search_start_values["name"] = "search_start"
    search_start_tune = tune_math.Tune(**search_start_values)
  search_evaluator = current_code_evaluator(
    predictor, search_batch, config.sample_period_s, args.wobble_weight, args,
  )
  search_baseline = search_evaluator.evaluate(baseline_tune)
  validation_evaluator = current_code_evaluator(
    predictor, validation_batch, config.sample_period_s, args.wobble_weight, args,
  )
  validation_baseline = validation_evaluator.evaluate(baseline_tune)
  if args.candidate_tune_report is not None:
    candidate_payload = json.loads(args.candidate_tune_report.read_text(encoding="utf-8"))
    candidate_values = dict(candidate_payload["validation"]["optimized"]["tune"])
    candidate_values["name"] = "provided_candidate"
    history = []
    candidates = [tune_math.Tune(**candidate_values)]
  elif args.skip_tune_search:
    history: list[dict[str, Any]] = []
    candidates: list[tune_math.Tune] = []
  else:
    _, history = legacy.optimize(
      search_evaluator, search_start_tune, args.max_path_regression,
      set(args.tune_search_field) or None,
    )
    candidates = unique_candidates(history, args.validation_candidates)
  candidate_search_results = evaluate_many_chunked(search_evaluator, candidates)
  validation_results = evaluate_many_chunked(validation_evaluator, candidates)
  candidate_trials = [
    {
      "tune": asdict(candidate),
      "path_safe": (
        path_safe(search_result, search_baseline, args.max_path_regression)
        and path_safe(validation_result, validation_baseline, args.max_path_regression)
      ),
      "search": {
        "objective": search_result["objective"],
        "balanced_transition_rmse": search_result["balanced_transition_rmse"],
        "center_rmse": search_result["phases"]["center"]["rmse"],
        "shape": search_result["shape"],
      },
      "validation": {
        "objective": validation_result["objective"],
        "balanced_transition_rmse": validation_result["balanced_transition_rmse"],
        "center_rmse": validation_result["phases"]["center"]["rmse"],
        "shape": validation_result["shape"],
      },
    }
    for candidate, search_result, validation_result
    in zip(candidates, candidate_search_results, validation_results, strict=True)
  ]
  eligible = [
    (candidate, search_result, validation_result)
    for candidate, search_result, validation_result
    in zip(candidates, candidate_search_results, validation_results, strict=True)
    if (
      path_safe(search_result, search_baseline, args.max_path_regression)
      and path_safe(validation_result, validation_baseline, args.max_path_regression)
    )
  ]
  if eligible:
    selected, selected_search, selected_validation = min(
      eligible, key=lambda item: item[2]["objective"],
    )
    selected = replace(selected, name="neural_plant_optimized")
    selected_validation = validation_evaluator.evaluate(selected)
    selected_search = search_evaluator.evaluate(selected)
  else:
    print("No retuned feedforward candidate passed validation; sweeping damping on the current tune.", flush=True)
    selected = replace(baseline_tune, name="neural_plant_damping_base")
    selected_search = search_baseline
    selected_validation = validation_baseline

  acceptance_evaluator = current_code_evaluator(
    predictor, acceptance_batch, config.sample_period_s, args.wobble_weight, args,
  )
  acceptance_baseline = evaluate_with_uncertainty(
    acceptance_evaluator, predictor, baseline_tune,
  )
  selected_acceptance_undamped = acceptance_evaluator.evaluate(selected)
  if not path_safe(
    selected_acceptance_undamped, acceptance_baseline, args.max_acceptance_regression,
  ):
    selected = replace(baseline_tune, name="neural_plant_damping_base")
  damping_results: list[dict[str, Any]] = []
  damping_candidates: list[
    tuple[float, float, float, float, float, float, float, dict[str, Any], dict[str, Any], dict[str, Any]]
  ] = []
  evaluated_damping_candidates: list[
    tuple[float, float, float, float, float, float, float, dict[str, Any], dict[str, Any], dict[str, Any]]
  ] = []
  damping_schedules = list(product(
    sorted(set(args.damping_gain)),
    sorted(set(args.highway_damping_gain)),
    sorted(set(args.turn_exit_damping_gain)),
    sorted(set(args.turn_exit_damping_gain_right)),
    sorted(set(args.reversal_damping_gain)),
    sorted(set(args.reversal_hold_seconds)),
    sorted(set(args.steering_rate_feedback_gain)),
  ))
  for schedule_index, (
    gain,
    highway_gain,
    turn_exit_gain,
    turn_exit_gain_right,
    reversal_gain,
    reversal_hold_seconds,
    steering_rate_feedback_gain,
    ) in enumerate(damping_schedules, 1):
    schedule_summary = (
      f"evaluating damping schedule {schedule_index}/{len(damping_schedules)}"
      + f" base={gain:.4f} highway={highway_gain:.4f}"
      + f" reference={args.damping_reference_gain:.3f}"
    )
    print(
      schedule_summary,
      flush=True,
    )
    evaluator_kwargs = {
      "highway_damping_gain": highway_gain,
      "damping_reference_gain": args.damping_reference_gain,
    }
    damped_search_evaluator = DampedClosedLoopEvaluator(
      predictor, search_batch, config.sample_period_s, args.wobble_weight,
      gain, turn_exit_gain, turn_exit_gain_right, reversal_gain,
      reversal_hold_seconds, steering_rate_feedback_gain,
      **evaluator_kwargs,
    )
    damped_validation_evaluator = DampedClosedLoopEvaluator(
      predictor, validation_batch, config.sample_period_s, args.wobble_weight,
      gain, turn_exit_gain, turn_exit_gain_right, reversal_gain,
      reversal_hold_seconds, steering_rate_feedback_gain,
      **evaluator_kwargs,
    )
    damped_acceptance_evaluator = DampedClosedLoopEvaluator(
      predictor, acceptance_batch, config.sample_period_s, args.wobble_weight,
      gain, turn_exit_gain, turn_exit_gain_right, reversal_gain,
      reversal_hold_seconds, steering_rate_feedback_gain,
      **evaluator_kwargs,
    )
    search_result = damped_search_evaluator.evaluate(selected)
    validation_result = damped_validation_evaluator.evaluate(selected)
    acceptance_result = damped_acceptance_evaluator.evaluate(selected)
    safe = (
      path_safe(search_result, search_baseline, args.max_path_regression)
      and path_safe(validation_result, validation_baseline, args.max_path_regression)
      and path_safe(acceptance_result, acceptance_baseline, args.max_acceptance_regression)
      and turn_exit_safe(
        search_result, search_baseline,
        args.max_turn_exit_score_regression,
        args.max_turn_exit_reversal_regression,
      )
      and turn_exit_safe(
        validation_result, validation_baseline,
        args.max_turn_exit_score_regression,
        args.max_turn_exit_reversal_regression,
      )
      and turn_exit_safe(
        acceptance_result, acceptance_baseline,
        args.max_turn_exit_score_regression,
        args.max_turn_exit_reversal_regression,
      )
      and shape_safe(search_result, search_baseline)
      and shape_safe(validation_result, validation_baseline)
      and shape_safe(acceptance_result, acceptance_baseline)
    )
    damping_results.append({
      "gain": gain,
      "highway_gain": highway_gain,
      "turn_exit_gain": turn_exit_gain,
      "turn_exit_gain_right": turn_exit_gain_right,
      "reversal_gain": reversal_gain,
      "reversal_hold_seconds": reversal_hold_seconds,
      "steering_rate_feedback_gain": steering_rate_feedback_gain,
      "damping_reference_gain": args.damping_reference_gain,
      "safe": safe,
      "search": search_result,
      "validation": validation_result,
      "acceptance": acceptance_result,
    })
    current_schedule = (
      gain == args.baseline_damping_gain
      and highway_gain == args.baseline_highway_damping_gain
      and turn_exit_gain == args.baseline_damping_gain
      and turn_exit_gain_right == args.baseline_damping_gain
      and reversal_gain == args.baseline_reversal_damping_gain
      and reversal_hold_seconds == args.baseline_reversal_hold_seconds
      and steering_rate_feedback_gain == 0.0
    )
    evaluated_candidate = (
      gain, highway_gain, turn_exit_gain, turn_exit_gain_right, reversal_gain,
      reversal_hold_seconds, steering_rate_feedback_gain,
      search_result, validation_result, acceptance_result,
    )
    evaluated_damping_candidates.append(evaluated_candidate)
    if safe or current_schedule:
      damping_candidates.append(evaluated_candidate)
  if not damping_candidates:
    print(
      "No damping schedule preserved all guardrails; reporting the least-regressive candidate as rejected.",
      flush=True,
    )
    damping_candidates = evaluated_damping_candidates
  def candidate_key(
    item: tuple[float, float, float, float, float, float, float, dict[str, Any], dict[str, Any], dict[str, Any]],
  ) -> tuple[float, float, float]:
    split_results = item[7:]
    split_baselines = (search_baseline, validation_baseline, acceptance_baseline)
    worst_shape_ratio = max(
      max(
        result["shape"][name] / max(baseline["shape"][name], 1e-9)
        for name in ("highway_score", "ramp_hold_score")
      )
      for result, baseline in zip(split_results, split_baselines, strict=True)
    )
    worst_reversal_ratio = max(
      result["wobble"]["turn_exit_rate_reversal_rms_deg_s"]
      / max(baseline["wobble"]["turn_exit_rate_reversal_rms_deg_s"], 1e-9)
      for result, baseline in zip(split_results, split_baselines, strict=True)
    )
    return worst_shape_ratio, worst_reversal_ratio, item[8]["objective"]

  (
    damping_gain,
    highway_damping_gain,
    turn_exit_damping_gain,
    turn_exit_damping_gain_right,
    reversal_damping_gain,
    reversal_hold_seconds,
    steering_rate_feedback_gain,
    selected_search,
    selected_validation,
    _,
  ) = min(
    damping_candidates, key=candidate_key,
  )
  final_acceptance_evaluator = DampedClosedLoopEvaluator(
    predictor, acceptance_batch, config.sample_period_s, args.wobble_weight,
    damping_gain, turn_exit_damping_gain, turn_exit_damping_gain_right,
    reversal_damping_gain, reversal_hold_seconds, steering_rate_feedback_gain,
    highway_damping_gain=highway_damping_gain,
    damping_reference_gain=args.damping_reference_gain,
  )
  acceptance_selected = evaluate_with_uncertainty(
    final_acceptance_evaluator, predictor, selected,
  )
  selected_tune_values = asdict(selected)
  baseline_tune_values = asdict(baseline_tune)
  selected_tune_values.pop("name", None)
  baseline_tune_values.pop("name", None)
  candidate_changed = (
    selected_tune_values != baseline_tune_values
    or damping_gain != args.baseline_damping_gain
    or highway_damping_gain != args.baseline_highway_damping_gain
    or turn_exit_damping_gain != args.baseline_damping_gain
    or turn_exit_damping_gain_right != args.baseline_damping_gain
    or reversal_damping_gain != args.baseline_reversal_damping_gain
    or reversal_hold_seconds != args.baseline_reversal_hold_seconds
    or steering_rate_feedback_gain != 0.0
    or args.damping_reference_gain != args.baseline_damping_reference_gain
  )
  candidate_improvements = promotion_improvements(
    selected_validation, validation_baseline,
    acceptance_selected, acceptance_baseline,
  )
  meaningful_improvement = (
    max(candidate_improvements.values()) >= args.min_promotion_improvement
  )
  shape_improvement = (
    min(
      candidate_improvements["validation_highway_shape"],
      candidate_improvements["acceptance_highway_shape"],
      candidate_improvements["validation_ramp_hold"],
      candidate_improvements["acceptance_ramp_hold"],
    ) >= 0.0
    and max(
      candidate_improvements["validation_highway_shape"],
      candidate_improvements["acceptance_highway_shape"],
    ) >= args.min_promotion_improvement
    and max(
      candidate_improvements["validation_ramp_hold"],
      candidate_improvements["acceptance_ramp_hold"],
    ) >= args.min_promotion_improvement
  )
  accepted = (
    candidate_changed
    and meaningful_improvement
    and shape_improvement
    and selected_search["objective"]
    <= search_baseline["objective"] * (1.0 + args.max_objective_regression)
    and selected_validation["objective"]
    <= validation_baseline["objective"] * (1.0 + args.max_objective_regression)
    and path_safe(selected_search, search_baseline, args.max_path_regression)
    and path_safe(selected_validation, validation_baseline, args.max_path_regression)
    and path_safe(acceptance_selected, acceptance_baseline, args.max_acceptance_regression)
    and turn_exit_safe(
      selected_search, search_baseline,
      args.max_turn_exit_score_regression,
      args.max_turn_exit_reversal_regression,
    )
    and shape_safe(selected_validation, validation_baseline)
    and shape_safe(acceptance_selected, acceptance_baseline)
    and shape_safe(selected_search, search_baseline)
    and turn_exit_safe(
      selected_validation, validation_baseline,
      args.max_turn_exit_score_regression,
      args.max_turn_exit_reversal_regression,
    )
    and turn_exit_safe(
      acceptance_selected, acceptance_baseline,
      args.max_turn_exit_score_regression,
      args.max_turn_exit_reversal_regression,
    )
    and acceptance_selected["objective"]
    <= acceptance_baseline["objective"] * (1.0 + args.max_objective_regression)
    and acceptance_selected["plant_disagreement"]["p95"]
    <= acceptance_baseline["plant_disagreement"]["p95"] * (1.0 + args.max_disagreement_regression)
  )
  report = {
    "plant_model": str(args.plant_model),
    "plant_config": predictor.payload["config"],
    "sample_period_s": config.sample_period_s,
    "alignment": "predicted response at time t is scored against the command reference at t minus response_delay_s",
    "requested_response_delay_s": args.response_delay_s,
    "response_delay_steps": response_delay_steps,
    "effective_response_delay_s": effective_response_delay_s,
    "rollout_steps": args.rollout_steps,
    "rollout_seconds": args.rollout_steps * config.sample_period_s,
    "search_route_prefixes": args.search_route_prefix,
    "validation_route_prefixes": args.validation_route_prefix,
    "acceptance_route_prefixes": args.acceptance_route_prefix,
    "windows": {
      "search": len(search_batch.history),
      "validation": len(validation_batch.history),
      "acceptance": len(acceptance_batch.history),
    },
    "objective": {
      "wobble_weight": args.wobble_weight,
      "max_path_regression": args.max_path_regression,
      "max_acceptance_regression": args.max_acceptance_regression,
      "max_disagreement_regression": args.max_disagreement_regression,
      "max_objective_regression": args.max_objective_regression,
      "min_promotion_improvement": args.min_promotion_improvement,
      "max_turn_exit_score_regression": args.max_turn_exit_score_regression,
      "max_turn_exit_reversal_regression": args.max_turn_exit_reversal_regression,
      "baseline_damping_gain": args.baseline_damping_gain,
      "baseline_highway_damping_gain": args.baseline_highway_damping_gain,
      "baseline_damping_reference_gain": args.baseline_damping_reference_gain,
      "baseline_reversal_damping_gain": args.baseline_reversal_damping_gain,
      "baseline_reversal_hold_seconds": args.baseline_reversal_hold_seconds,
    },
    "candidate_changed": candidate_changed,
    "meaningful_improvement": meaningful_improvement,
    "shape_improvement": shape_improvement,
    "promotion_improvements": candidate_improvements,
    "accepted": accepted,
    "recommended_damping_gain": damping_gain if accepted else args.baseline_damping_gain,
    "recommended_highway_damping_gain": (
      highway_damping_gain if accepted else args.baseline_highway_damping_gain
    ),
    "recommended_turn_exit_damping_gain": (
      turn_exit_damping_gain if accepted else args.baseline_damping_gain
    ),
    "recommended_turn_exit_damping_gain_right": (
      turn_exit_damping_gain_right if accepted else args.baseline_damping_gain
    ),
    "recommended_reversal_damping_gain": (
      reversal_damping_gain if accepted else args.baseline_reversal_damping_gain
    ),
    "recommended_reversal_hold_seconds": reversal_hold_seconds,
    "recommended_steering_rate_feedback_gain": steering_rate_feedback_gain if accepted else 0.0,
    "searched_damping_reference_gain": args.damping_reference_gain,
    "recommended_damping_reference_gain": (
      args.damping_reference_gain if accepted else args.baseline_damping_reference_gain
    ),
    "damping_search": damping_results,
    "search": {
      "current": search_baseline,
      "optimized": selected_search,
    },
    "validation": {
      "current": validation_baseline,
      "optimized": selected_validation,
    },
    "acceptance": {
      "current": acceptance_baseline,
      "optimized": acceptance_selected,
    },
    "recommended_tune": asdict(selected) if accepted else asdict(baseline_tune),
    "search_evaluations": len(history),
    "validation_candidates": len(candidates),
    "candidate_trials": candidate_trials,
    "tune_search_fields": args.tune_search_field,
    "candidate_tune_report": (
      str(args.candidate_tune_report) if args.candidate_tune_report is not None else None
    ),
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(json.dumps({
    "accepted": accepted,
    "validation": report["validation"],
    "acceptance": report["acceptance"],
    "recommended_tune": report["recommended_tune"],
    "recommended_damping_gain": report["recommended_damping_gain"],
    "recommended_highway_damping_gain": report["recommended_highway_damping_gain"],
    "recommended_turn_exit_damping_gain": report["recommended_turn_exit_damping_gain"],
    "recommended_turn_exit_damping_gain_right": report["recommended_turn_exit_damping_gain_right"],
    "recommended_reversal_damping_gain": report["recommended_reversal_damping_gain"],
    "recommended_reversal_hold_seconds": report["recommended_reversal_hold_seconds"],
    "recommended_steering_rate_feedback_gain": report["recommended_steering_rate_feedback_gain"],
  }, indent=2), flush=True)
  print(f"report: {args.output}", flush=True)
  if not accepted:
    raise SystemExit("No conventional tune passed all neural-plant gates.")


if __name__ == "__main__":
  main()
