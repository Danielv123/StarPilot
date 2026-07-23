#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from joblib import load

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

from openpilot.tools.tuning import optimize_ioniq5_closed_loop as closed_loop
from openpilot.tools.tuning import train_lateral_plant_model as plant_data
from openpilot.tools.tuning.train_ioniq5_nnff import flux_predict
from openpilot.starpilot.controls.lib.nnff_path_preview import get_ioniq_5_early_unwind_lateral_accel


DEFAULT_STOCK = REPO_ROOT / "starpilot/assets/nnff_models/HYUNDAI_IONIQ_5.json"
DEFAULT_CUSTOM = REPO_ROOT / "artifacts/tuning/ioniq5_nnff_20260723/HYUNDAI_IONIQ_5.json"
DEFAULT_OUTPUT = REPO_ROOT / "artifacts/tuning/ioniq5_nnff_20260723/closed_loop_comparison.json"
PATH_OFFSETS = (-6, -4, -2, 8, 14, 22, 32)
CURRENT_INDEX = 6


def build_nnff_batch(trajectories: list[plant_data.Trajectory], route_prefix: str, history_steps: int,
                     rollout_steps: int, max_windows: int, seed: int) -> tuple[closed_loop.RolloutBatch, np.ndarray]:
  windows: list[tuple[plant_data.Trajectory, int]] = []
  for trajectory in trajectories:
    if not trajectory.route.startswith(route_prefix):
      continue
    values = trajectory.values
    for source in range(max(history_steps - 1, -min(PATH_OFFSETS)), len(trajectory.times) - rollout_steps - max(PATH_OFFSETS) - 1):
      active = slice(source, source + rollout_steps + 1)
      if values["lat_active"][active].min() < 0.5 or values["driver_overlay"][active].max() > 0.5:
        continue
      if values["saturated"][active].max() > 0.5 or values["v_ego"][source] < 5.0:
        continue
      timeline = trajectory.times[source + min(PATH_OFFSETS):source + rollout_steps + max(PATH_OFFSETS) + 1]
      if np.max(np.diff(timeline)) >= 0.09:
        continue
      windows.append((trajectory, source))
  if len(windows) > max_windows:
    rng = np.random.default_rng(seed)
    windows = [windows[index] for index in np.sort(rng.choice(len(windows), max_windows, replace=False))]
  if not windows:
    raise SystemExit(f"No clean NNFF rollout windows for route prefix {route_prefix}.")

  history = np.stack([
    np.stack([
      np.asarray([trajectory.values[name][source - lag] for name in plant_data.BASE_FEATURES], dtype=np.float32)
      for lag in range(history_steps)
    ])
    for trajectory, source in windows
  ])

  def field(name: str, start: int, length: int) -> np.ndarray:
    return np.stack([
      trajectory.values[name][source + start:source + start + length]
      for trajectory, source in windows
    ])

  path_length = rollout_steps + max(PATH_OFFSETS) - min(PATH_OFFSETS) + 1
  path_desired = field("desired_lateral_accel", min(PATH_OFFSETS), path_length)
  batch = closed_loop.RolloutBatch(
    history=history,
    desired=field("desired_lateral_accel", 0, rollout_steps),
    jerk=field("desired_lateral_jerk", 0, rollout_steps),
    v_ego=field("v_ego", 0, rollout_steps),
    a_ego=field("a_ego", 0, rollout_steps),
    logged_applied=field("applied_torque", 0, rollout_steps),
    logged_controller_output=field("controller_output", 0, rollout_steps),
    initial_i=np.zeros(len(windows), dtype=np.float32),
    target_desired=field("desired_lateral_accel", 1, rollout_steps),
    target_jerk=field("desired_lateral_jerk", 1, rollout_steps),
    segments=[trajectory.segment for trajectory, _ in windows],
  )
  return batch, path_desired


def neural_rollout(model: Any, batch: closed_loop.RolloutBatch, path_desired: np.ndarray,
                   payload: dict[str, Any], dt: float, error_gain: float = 1.0) -> closed_loop.RolloutTrace:
  history = batch.history.copy()
  base_index = {name: index for index, name in enumerate(plant_data.BASE_FEATURES)}
  state_indexes = [base_index[name] for name in plant_data.STATE_FEATURES]
  integral = np.zeros(len(history), dtype=np.float64)
  errors = []
  command_deltas = []
  commands = []
  states = []
  for step in range(batch.desired.shape[1]):
    desired = batch.desired[:, step].astype(np.float64)
    jerk = batch.jerk[:, step].astype(np.float64)
    speed = batch.v_ego[:, step].astype(np.float64)
    actual = history[:, 0, base_index["actual_lateral_accel"]].astype(np.float64)
    path_index = CURRENT_INDEX + step
    path_points = np.column_stack([path_desired[:, path_index + offset] for offset in PATH_OFFSETS])
    control_desired = np.asarray([
      get_ioniq_5_early_unwind_lateral_accel(value, preview)
      for value, preview in zip(desired, path_points[:, 5], strict=True)
    ])
    zeros = np.zeros((len(history), 7), dtype=np.float64)
    friction_input = 0.7 * (control_desired - actual) + 0.4 * jerk
    feedforward_input = np.column_stack((speed, control_desired, friction_input, np.zeros(len(history)), path_points, zeros))
    setpoint_input = np.column_stack((
      speed, control_desired, jerk, np.zeros(len(history)), np.repeat(control_desired[:, None], 7, axis=1), zeros,
    ))
    measurement_input = np.column_stack((
      speed, actual, np.zeros(len(history)), np.zeros(len(history)), np.repeat(actual[:, None], 7, axis=1), zeros,
    ))
    feedforward = flux_predict(payload, feedforward_input)
    torque_error = error_gain * (flux_predict(payload, setpoint_input) - flux_predict(payload, measurement_input))
    integral += 0.3 * dt * torque_error
    controller_output = np.clip(-(feedforward + torque_error + integral), -1.0, 1.0)
    limiter_gap = batch.logged_applied[:, step] - batch.logged_controller_output[:, step]
    applied = np.clip(controller_output + limiter_gap, -1.0, 1.0)
    history[:, 0, base_index["applied_torque"]] = applied

    prediction_delta = model.predict(history.reshape((len(history), -1)))
    next_state = history[:, 0, state_indexes] + prediction_delta
    errors.append(next_state[:, 0] - batch.target_desired[:, step])
    command_deltas.append(applied - batch.logged_applied[:, step])
    commands.append(applied)
    states.append(next_state)

    next_base = history[:, 0].copy()
    next_base[:, base_index["v_ego"]] = batch.v_ego[:, min(step + 1, batch.v_ego.shape[1] - 1)]
    next_base[:, base_index["a_ego"]] = batch.a_ego[:, min(step + 1, batch.a_ego.shape[1] - 1)]
    next_base[:, state_indexes] = next_state
    history[:, 1:] = history[:, :-1]
    history[:, 0] = next_base
  return closed_loop.RolloutTrace(
    errors=np.stack(errors, axis=1),
    command_delta=np.stack(command_deltas, axis=1),
    commands=np.stack(commands, axis=1),
    states=np.stack(states, axis=1),
  )


def relative(custom: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float]:
  pairs = {
    "overall_rmse": (custom["phases"]["all"]["rmse"], baseline["phases"]["all"]["rmse"]),
    "transition_rmse": (custom["balanced_transition_rmse"], baseline["balanced_transition_rmse"]),
    "center_rmse": (custom["phases"]["center"]["rmse"], baseline["phases"]["center"]["rmse"]),
    "wobble_score": (custom["wobble"]["score"], baseline["wobble"]["score"]),
    "command_rms": (custom["command_rms"], baseline["command_rms"]),
  }
  return {name: 100.0 * (value / reference - 1.0) for name, (value, reference) in pairs.items()}


def scale_flux_output(payload: dict[str, Any], gain: float) -> dict[str, Any]:
  scaled = copy.deepcopy(payload)
  last = scaled["layers"][-1]
  for key in list(last):
    if key.endswith(("_W", "_b")):
      last[key] = (np.asarray(last[key], dtype=np.float64) * gain).tolist()
  return scaled


def main() -> None:
  parser = argparse.ArgumentParser(description="Compare conventional, stock NNFF, and custom NNFF in the plant path replay.")
  parser.add_argument("--plant-model", type=Path, required=True)
  parser.add_argument("--route-prefix", required=True)
  parser.add_argument("--stock-model", type=Path, default=DEFAULT_STOCK)
  parser.add_argument("--custom-model", type=Path, default=DEFAULT_CUSTOM)
  parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
  parser.add_argument("--rollout-steps", type=int, default=30)
  parser.add_argument("--max-windows", type=int, default=1500)
  parser.add_argument("--custom-gain", type=float, default=1.0)
  parser.add_argument("--nn-error-gain", type=float, default=1.0)
  args = parser.parse_args()

  artifact = load(args.plant_model)
  metadata = artifact["metadata"]
  trajectories = closed_loop.load_validation_trajectories(metadata)
  batch, path_desired = build_nnff_batch(
    trajectories, args.route_prefix, int(metadata["history_steps"]), args.rollout_steps, args.max_windows, 31,
  )
  scorer = closed_loop.ClosedLoopEvaluator(
    artifact["plant_model"], batch, float(metadata["sample_period_s"]), wobble_weight=0.04,
  )
  conventional_trace = scorer.rollout(closed_loop.CURRENT_CODE_TUNE)
  conventional = scorer.evaluate_trace(closed_loop.CURRENT_CODE_TUNE, conventional_trace)
  stock_payload = json.loads(args.stock_model.read_text(encoding="utf-8"))
  custom_payload = scale_flux_output(
    json.loads(args.custom_model.read_text(encoding="utf-8")), args.custom_gain,
  )
  stock = scorer.evaluate_trace(
    closed_loop.CURRENT_CODE_TUNE,
    neural_rollout(artifact["plant_model"], batch, path_desired, stock_payload, scorer.dt, args.nn_error_gain),
  )
  custom = scorer.evaluate_trace(
    closed_loop.CURRENT_CODE_TUNE,
    neural_rollout(artifact["plant_model"], batch, path_desired, custom_payload, scorer.dt, args.nn_error_gain),
  )
  result = {
    "route_prefix": args.route_prefix,
    "windows": len(batch.history),
    "rollout_steps": args.rollout_steps,
    "sample_period_s": scorer.dt,
    "custom_gain": args.custom_gain,
    "nn_error_gain": args.nn_error_gain,
    "conventional": conventional,
    "stock_nnff": stock,
    "custom_nnff": custom,
    "stock_vs_conventional_percent": relative(stock, conventional),
    "custom_vs_conventional_percent": relative(custom, conventional),
    "custom_vs_stock_percent": relative(custom, stock),
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(json.dumps({
    "conventional": {key: conventional[key] for key in ("objective", "balanced_transition_rmse", "command_rms")},
    "stock_nnff": {key: stock[key] for key in ("objective", "balanced_transition_rmse", "command_rms")},
    "custom_nnff": {key: custom[key] for key in ("objective", "balanced_transition_rmse", "command_rms")},
    "custom_vs_conventional_percent": result["custom_vs_conventional_percent"],
    "custom_vs_stock_percent": result["custom_vs_stock_percent"],
  }, indent=2))
  print(f"report: {args.output}")


if __name__ == "__main__":
  main()
