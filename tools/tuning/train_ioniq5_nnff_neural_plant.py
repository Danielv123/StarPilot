#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

try:
  import torch
  import torch.nn.functional as F
except ModuleNotFoundError as e:
  raise SystemExit(
    "Run with torch, numpy, pycapnp, and zstandard installed."
  ) from e

from openpilot.tools.tuning import train_goal_based_ioniq5_nnff as legacy
from openpilot.tools.tuning import train_lateral_plant_model as plant_data
from openpilot.tools.tuning import train_neural_lateral_plant as neural_plant
from openpilot.tools.tuning.train_ioniq5_nnff import INPUT_VARS as LEGACY_INPUT_VARS, flux_predict


DEFAULT_PLANT = REPO_ROOT / "artifacts/tuning/neural_lateral_plant_20260723/neural_lateral_plant.pt"
DEFAULT_INITIAL_MODEL = REPO_ROOT / "starpilot/assets/nnff_models/HYUNDAI_IONIQ_5.json"
DEFAULT_LOG_ROOT = Path(r"D:\comma_driving_logs\10.30.1.75\realdata")
DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts/tuning/ioniq5_nnff_neural_plant_20260724"
DEFAULT_MODEL_OUTPUT = DEFAULT_OUTPUT_DIR / "HYUNDAI_IONIQ_5.json"
PATH_TIMES_S = (-0.3, -0.2, -0.1, 0.4, 0.7, 1.1, 1.6)
REGIMES = ("sharp_turn_in", "turn_in", "unwind", "steady", "center")
IONIQ_5_WHEELBASE_M = 2.97
IONIQ_5_STEER_RATIO = 14.26
INPUT_VARS = (*LEGACY_INPUT_VARS, "desired_curvature")
STATE_CLAMPS = (
  (-5.0, 5.0),
  (-180.0, 180.0),
  (-500.0, 500.0),
  (-500.0, 500.0),
)


@dataclass
class PolicyWindows:
  history: np.ndarray
  path: np.ndarray
  curvature: np.ndarray
  jerk: np.ndarray
  v_ego: np.ndarray
  a_ego: np.ndarray
  regimes: np.ndarray
  segments: list[str]

  def __len__(self) -> int:
    return len(self.history)

  def subset(self, indexes: np.ndarray) -> PolicyWindows:
    return PolicyWindows(
      history=self.history[indexes],
      path=self.path[indexes],
      curvature=self.curvature[indexes],
      jerk=self.jerk[indexes],
      v_ego=self.v_ego[indexes],
      a_ego=self.a_ego[indexes],
      regimes=self.regimes[indexes],
      segments=[self.segments[index] for index in indexes],
    )


def path_offsets(sample_period_s: float) -> tuple[int, ...]:
  return tuple(round(seconds / sample_period_s) for seconds in PATH_TIMES_S)


def classify_regime(desired: np.ndarray, jerk: np.ndarray, speed: np.ndarray,
                    curvature: np.ndarray | None = None,
                    curvature_rate: np.ndarray | None = None) -> np.ndarray:
  low_speed = speed < 8.0
  accel_product = desired * jerk
  if curvature is None or curvature_rate is None:
    curvature = np.zeros_like(desired)
    curvature_rate = np.zeros_like(desired)
  curvature_product = curvature * curvature_rate
  product = np.where(low_speed, curvature_product * 100.0, accel_product)
  center = np.where(low_speed, np.abs(curvature) < 0.003, np.abs(desired) < 0.08)
  sharp = (
    (product > 0.10)
    & (
      (np.abs(desired) >= 0.30)
      | (np.abs(jerk) >= 0.55)
      | (low_speed & ((np.abs(curvature) >= 0.01) | (np.abs(curvature_rate) >= 0.02)))
    )
    & (speed < 18.0)
  )
  labels = np.full(len(desired), "steady", dtype="<U16")
  labels[product < -0.01] = "unwind"
  labels[product > 0.01] = "turn_in"
  labels[sharp] = "sharp_turn_in"
  labels[center] = "center"
  return labels


def balanced_indexes(labels: np.ndarray, size: int, rng: np.random.Generator,
                     speeds: np.ndarray | None = None) -> np.ndarray:
  if speeds is None:
    groups = [(name, labels == name) for name in REGIMES if np.any(labels == name)]
  else:
    speed_buckets = neural_plant.speed_bucket_indexes(speeds)
    groups = [
      ((name, speed_index), (labels == name) & (speed_buckets == speed_index))
      for name in REGIMES
      for speed_index in range(len(neural_plant.SPEED_BUCKETS_MPS))
      if np.any((labels == name) & (speed_buckets == speed_index))
    ]
  if not groups:
    raise ValueError("No policy regimes are present.")
  quota, remainder = divmod(size, len(groups))
  selected: list[np.ndarray] = []
  for index, (_, mask) in enumerate(groups):
    candidates = np.flatnonzero(mask)
    count = quota + (1 if index < remainder else 0)
    selected.append(rng.choice(candidates, count, replace=len(candidates) < count))
  result = np.concatenate(selected)
  rng.shuffle(result)
  return result


def discover_trajectories(root: Path, prefixes: list[str], sample_step: int) -> list[plant_data.Trajectory]:
  paths = sorted({
    path.resolve()
    for prefix in prefixes
    for path in root.glob(f"{prefix}--*/rlog.zst")
  })
  if not paths:
    raise SystemExit(f"No rlogs matched route prefixes: {', '.join(prefixes)}")
  trajectories: list[plant_data.Trajectory] = []
  for index, path in enumerate(paths, 1):
    trajectory = plant_data.read_trajectory(path, "hyundai", "IONIQ5", sample_step=sample_step)
    if trajectory is not None:
      trajectories.append(trajectory)
    if index % 10 == 0 or index == len(paths):
      print(f"loaded {index}/{len(paths)} rlogs; usable={len(trajectories)}", flush=True)
  return trajectories


def build_windows(trajectories: list[plant_data.Trajectory], history_steps: int,
                  rollout_steps: int, offsets: tuple[int, ...],
                  window_stride: int, max_windows: int, seed: int) -> PolicyWindows:
  histories: list[np.ndarray] = []
  paths: list[np.ndarray] = []
  curvatures: list[np.ndarray] = []
  jerks: list[np.ndarray] = []
  speeds: list[np.ndarray] = []
  accelerations: list[np.ndarray] = []
  regimes: list[np.ndarray] = []
  segments: list[str] = []
  base_names = list(plant_data.BASE_FEATURES)
  current_index = -min(offsets)
  path_relative = np.arange(min(offsets), rollout_steps + max(offsets) + 1)

  for trajectory in trajectories:
    values = trajectory.values
    lower = max(history_steps - 1, -min(offsets))
    upper = len(trajectory.times) - rollout_steps - max(offsets) - 1
    if upper <= lower:
      continue
    source = np.arange(lower, upper, window_stride)
    active_index = source[:, None] + np.arange(rollout_steps + 1)
    path_index = source[:, None] + path_relative
    clean = (
      (values["lat_active"][active_index].min(axis=1) > 0.5)
      & (values["driver_overlay"][active_index].max(axis=1) < 0.5)
      & (values["saturated"][active_index].max(axis=1) < 0.5)
      & (values["v_ego"][source] >= plant_data.MIN_TRAIN_SPEED_MPS)
      & (np.diff(trajectory.times[path_index], axis=1).max(axis=1) < 0.035)
    )
    source = source[clean]
    path_index = path_index[clean]
    if not len(source):
      continue
    history_index = source[:, None] - np.arange(history_steps)
    future_index = source[:, None] + np.arange(rollout_steps)
    history = np.stack([
      np.column_stack([values[name][indexes] for name in base_names])
      for indexes in history_index
    ])
    path = values["desired_lateral_accel"][path_index]
    curvature = values["desired_curvature"][path_index]
    jerk = values["desired_lateral_jerk"][future_index]
    # NNFF logs do not populate desiredLateralJerk. Reconstruct the same path
    # derivative used by the runtime controller.
    if np.count_nonzero(np.abs(jerk) > 1e-6) < max(10, jerk.size // 100):
      desired_future = values["desired_lateral_accel"][future_index]
      time_future = trajectory.times[future_index]
      jerk = np.gradient(desired_future, axis=1) / np.maximum(
        np.gradient(time_future, axis=1), 1e-3,
      )
      jerk = np.clip(jerk, -2.5, 2.5)
    speed = values["v_ego"][future_index]
    curvature_rate = (
      curvature[:, current_index + 10] - curvature[:, current_index]
    ) / 0.10
    regime = classify_regime(
      path[:, current_index],
      jerk[:, 0],
      speed[:, 0],
      curvature[:, current_index],
      curvature_rate,
    )
    histories.append(history)
    paths.append(path)
    curvatures.append(curvature)
    jerks.append(jerk)
    speeds.append(speed)
    accelerations.append(values["a_ego"][future_index])
    regimes.append(regime)
    segments.extend([trajectory.segment] * len(source))

  if not histories:
    raise SystemExit("No clean policy windows found.")
  result = PolicyWindows(
    history=np.concatenate(histories).astype(np.float32),
    path=np.concatenate(paths).astype(np.float32),
    curvature=np.concatenate(curvatures).astype(np.float32),
    jerk=np.concatenate(jerks).astype(np.float32),
    v_ego=np.concatenate(speeds).astype(np.float32),
    a_ego=np.concatenate(accelerations).astype(np.float32),
    regimes=np.concatenate(regimes),
    segments=segments,
  )
  if max_windows and len(result) > max_windows:
    indexes = balanced_indexes(
      result.regimes,
      max_windows,
      np.random.default_rng(seed),
      result.v_ego[:, 0],
    )
    result = result.subset(indexes)
  print(f"windows={len(result)} regimes={dict(Counter(result.regimes))}", flush=True)
  return result


def policy_input_stats(windows: PolicyWindows, offsets: tuple[int, ...],
                       max_rows: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
  rng = np.random.default_rng(seed)
  window_indexes = rng.choice(len(windows), min(len(windows), max_rows), replace=False)
  step_indexes = rng.integers(0, windows.jerk.shape[1], len(window_indexes))
  base_actual_index = plant_data.BASE_FEATURES.index("actual_lateral_accel")
  actual = windows.history[window_indexes, 0, base_actual_index]
  current_index = -min(offsets)
  path_points = np.column_stack([
    windows.path[window_indexes, current_index + step_indexes + offset]
    for offset in offsets
  ])
  desired = windows.path[window_indexes, current_index + step_indexes]
  friction = 0.7 * (desired - actual) + 0.4 * windows.jerk[window_indexes, step_indexes]
  zeros = np.zeros((len(window_indexes), 7), dtype=np.float32)
  inputs = np.column_stack((
    windows.v_ego[window_indexes, step_indexes],
    desired,
    friction,
    np.zeros(len(window_indexes), dtype=np.float32),
    path_points,
    zeros,
    windows.curvature[window_indexes, current_index + step_indexes],
  )).astype(np.float64)
  mean = inputs.mean(axis=0)
  std = inputs.std(axis=0)
  std[std < 1e-4] = 1.0
  return mean, std


def tensor_batch(windows: PolicyWindows, indexes: np.ndarray,
                 device: torch.device) -> dict[str, torch.Tensor]:
  return {
    "history": torch.as_tensor(windows.history[indexes], device=device),
    "path": torch.as_tensor(windows.path[indexes], device=device),
    "curvature": torch.as_tensor(windows.curvature[indexes], device=device),
    "jerk": torch.as_tensor(windows.jerk[indexes], device=device),
    "v_ego": torch.as_tensor(windows.v_ego[indexes], device=device),
    "a_ego": torch.as_tensor(windows.a_ego[indexes], device=device),
  }


def early_unwind(desired: torch.Tensor, preview: torch.Tensor) -> torch.Tensor:
  desired_abs = desired.abs()
  preview_abs = torch.where(desired * preview > 0.0, preview.abs(), torch.zeros_like(preview))
  drop = desired_abs - preview_abs
  gate = torch.clamp((drop - 0.12) / 0.25, 0.0, 1.0)
  reduction = torch.minimum(0.25 * drop * gate, 0.15 * desired_abs)
  enabled = (desired_abs >= 0.35) & (drop > 0.12)
  return torch.where(enabled, torch.sign(desired) * (desired_abs - reduction), desired)


@dataclass(frozen=True)
class PlantPredictor:
  models: list[torch.nn.Module]
  stats: dict[str, torch.Tensor]
  low_speed_models: list[torch.nn.Module] | None = None
  low_speed_stats: dict[str, torch.Tensor] | None = None
  low_speed_full_below_mps: float = 5.0
  low_speed_off_above_mps: float = 8.0

  def predict(self, history: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    delta, disagreement = neural_plant.ensemble_predict_delta(self.models, history, self.stats)
    if self.low_speed_models is None or self.low_speed_stats is None:
      return delta, disagreement

    low_delta, low_disagreement = neural_plant.ensemble_predict_delta(
      self.low_speed_models, history, self.low_speed_stats,
    )
    speed_index = plant_data.BASE_FEATURES.index("v_ego")
    speed = history[:, 0, speed_index]
    blend = torch.clamp(
      (self.low_speed_off_above_mps - speed)
      / (self.low_speed_off_above_mps - self.low_speed_full_below_mps),
      0.0,
      1.0,
    )[:, None]
    return (
      torch.lerp(delta, low_delta, blend),
      torch.lerp(disagreement, low_disagreement, blend),
    )


def rollout_policy(policy: legacy.FluxPolicy, plant: PlantPredictor,
                   policy_mean: torch.Tensor, policy_std: torch.Tensor,
                   batch: dict[str, torch.Tensor], offsets: tuple[int, ...],
                   sample_period_s: float, command_rate_limit_per_s: float,
                   low_speed_angle_assist_gain: float,
                   low_speed_angle_assist_max: float) -> dict[str, torch.Tensor]:
  history = batch["history"].clone()
  base_index = {name: index for index, name in enumerate(plant_data.BASE_FEATURES)}
  state_indexes = [base_index[name] for name in plant_data.STATE_FEATURES]
  actual_index = plant_data.STATE_FEATURES.index("actual_lateral_accel")
  rate_index = plant_data.STATE_FEATURES.index("signed_steering_rate_deg_s")
  current_index = -min(offsets)
  preview_offset = round(1.1 / sample_period_s)
  integral = torch.zeros(len(history), device=history.device)
  previous_command = history[:, 0, base_index["applied_torque"]]
  errors = []
  commands = []
  states = []
  disagreements = []

  def evaluate(values: torch.Tensor) -> torch.Tensor:
    return policy((values - policy_mean) / policy_std)[:, 0]

  rate_limit = command_rate_limit_per_s * sample_period_s
  for step in range(batch["jerk"].shape[1]):
    desired = batch["path"][:, current_index + step]
    preview = batch["path"][:, current_index + step + preview_offset]
    control_desired = early_unwind(desired, preview)
    actual = history[:, 0, base_index["actual_lateral_accel"]]
    speed = batch["v_ego"][:, step]
    jerk = batch["jerk"][:, step]
    path_points = torch.column_stack([
      batch["path"][:, current_index + step + offset] for offset in offsets
    ])
    zeros = torch.zeros((len(history), 7), device=history.device)
    friction = 0.7 * (control_desired - actual) + 0.4 * jerk
    feedforward_input = torch.column_stack((
      speed, control_desired, friction, torch.zeros(len(history), device=history.device), path_points, zeros,
      batch["curvature"][:, current_index + step],
    ))
    setpoint_input = torch.column_stack((
      speed, control_desired, jerk, torch.zeros(len(history), device=history.device),
      control_desired[:, None].repeat(1, 7), zeros, batch["curvature"][:, current_index + step],
    ))
    steering_angle = history[:, 0, base_index["steering_angle_deg"]]
    actual_curvature = -torch.deg2rad(steering_angle) / (IONIQ_5_STEER_RATIO * IONIQ_5_WHEELBASE_M)
    measurement_input = torch.column_stack((
      speed, actual, torch.zeros(len(history), device=history.device),
      torch.zeros(len(history), device=history.device), actual[:, None].repeat(1, 7), zeros,
      actual_curvature,
    ))
    torque_error = evaluate(setpoint_input) - evaluate(measurement_input)
    integral = integral + 0.3 * sample_period_s * torque_error
    raw_command = torch.clamp(-(evaluate(feedforward_input) + torque_error + integral), -1.0, 1.0)
    desired_angle = torch.rad2deg(
      -batch["curvature"][:, current_index + step] * IONIQ_5_STEER_RATIO * IONIQ_5_WHEELBASE_M,
    )
    angle_error = desired_angle - steering_angle
    needs_turn_in = desired_angle * angle_error > 0.0
    low_speed_blend = torch.clamp((8.0 - speed) / 3.0, 0.0, 1.0)
    angle_assist = torch.clamp(
      low_speed_angle_assist_gain * angle_error,
      -low_speed_angle_assist_max,
      low_speed_angle_assist_max,
    )
    raw_command = torch.clamp(
      raw_command + torch.where(needs_turn_in, low_speed_blend * angle_assist, 0.0),
      -1.0,
      1.0,
    )
    command_delta = rate_limit * torch.tanh((raw_command - previous_command) / max(rate_limit, 1e-6))
    command = torch.clamp(previous_command + command_delta, -1.0, 1.0)
    history[:, 0, base_index["applied_torque"]] = command
    delta, disagreement = plant.predict(history)
    next_state = history[:, 0, state_indexes] + delta
    next_state = torch.column_stack([
      torch.clamp(next_state[:, index], lower, upper)
      for index, (lower, upper) in enumerate(STATE_CLAMPS)
    ])
    target_desired = batch["path"][:, current_index + step + 1]
    errors.append(next_state[:, actual_index] - target_desired)
    commands.append(command)
    states.append(next_state)
    disagreements.append(disagreement)

    next_base = history[:, 0].clone()
    next_base[:, base_index["v_ego"]] = batch["v_ego"][:, min(step + 1, batch["v_ego"].shape[1] - 1)]
    next_base[:, base_index["a_ego"]] = batch["a_ego"][:, min(step + 1, batch["a_ego"].shape[1] - 1)]
    next_base[:, state_indexes] = next_state
    next_base[:, base_index["steering_rate_deg"]] = next_state[:, rate_index].abs()
    history = torch.cat((next_base[:, None, :], history[:, :-1, :]), dim=1)
    previous_command = command
  return {
    "errors": torch.stack(errors, dim=1),
    "commands": torch.stack(commands, dim=1),
    "states": torch.stack(states, dim=1),
    "disagreement": torch.stack(disagreements, dim=1),
    "state_std": plant.stats["state_std"],
  }


def policy_loss(trace: dict[str, torch.Tensor], batch: dict[str, torch.Tensor],
                offsets: tuple[int, ...], sample_period_s: float,
                command_rate_limit_per_s: float, wobble_weight: float,
                uncertainty_weight: float, slew_weight: float,
                inside_bias_weight: float, low_speed_angle_weight: float,
                low_speed_rate_weight: float) -> tuple[torch.Tensor, dict[str, float]]:
  current_index = -min(offsets)
  desired = torch.stack([
    batch["path"][:, current_index + step + 1] for step in range(trace["errors"].shape[1])
  ], dim=1)
  jerk = batch["jerk"]
  speed = batch["v_ego"]
  error = trace["errors"]
  commands = trace["commands"]
  curvature = torch.stack([
    batch["curvature"][:, current_index + step + 1]
    for step in range(trace["errors"].shape[1])
  ], dim=1)
  current_curvature = batch["curvature"][:, current_index]
  curvature_rate = torch.diff(
    torch.cat((current_curvature[:, None], curvature), dim=1),
    dim=1,
  ) / sample_period_s
  low_speed = speed < 8.0
  turn_signal = torch.where(low_speed, 100.0 * curvature * curvature_rate, desired * jerk)
  center = torch.where(low_speed, curvature.abs() < 0.003, desired.abs() < 0.08)
  turn_in = turn_signal > 0.01
  sharp_turn_in = (
    turn_in
    & (
      (desired.abs() >= 0.30)
      | (jerk.abs() >= 0.55)
      | (low_speed & ((curvature.abs() >= 0.01) | (curvature_rate.abs() >= 0.02)))
    )
    & (speed < 18.0)
  )
  intersection = sharp_turn_in & (speed < 8.0)
  unwind = turn_signal < -0.01
  steady = (~center) & torch.where(low_speed, curvature_rate.abs() < 0.005, jerk.abs() < 0.08)
  signed_error = torch.sign(desired) * error
  inside = signed_error > 0.0
  weights = (
    torch.ones_like(error)
    + 0.50 * center.float()
    + 1.50 * turn_in.float()
    + 3.00 * sharp_turn_in.float()
    + 1.00 * unwind.float()
    + 0.75 * inside.float()
  )
  tracking = (
    weights * F.smooth_l1_loss(error, torch.zeros_like(error), beta=0.05, reduction="none")
  ).sum() / weights.sum().clamp_min(1.0)
  initial_command = batch["history"][:, 0, plant_data.BASE_FEATURES.index("applied_torque")]
  command_delta = torch.diff(torch.cat((initial_command[:, None], commands), dim=1), dim=1)
  normalized_delta = command_delta / max(command_rate_limit_per_s * sample_period_s, 1e-6)
  slew_gate = torch.where(sharp_turn_in, 0.20, torch.where(turn_in, 0.55, torch.ones_like(error)))
  slew = (slew_gate * normalized_delta.square()).mean()
  effort = commands.square().mean()
  saturation = torch.relu(commands.abs() - 0.90).square().mean()
  rate_index = plant_data.STATE_FEATURES.index("signed_steering_rate_deg_s")
  angle_index = plant_data.STATE_FEATURES.index("steering_angle_deg")
  steering_rate = trace["states"][:, :, rate_index]
  steering_angle = trace["states"][:, :, angle_index]
  desired_angle = torch.rad2deg(
    -curvature * IONIQ_5_STEER_RATIO * IONIQ_5_WHEELBASE_M,
  )
  current_desired_angle = torch.rad2deg(
    -current_curvature * IONIQ_5_STEER_RATIO * IONIQ_5_WHEELBASE_M,
  )
  current_angle = batch["history"][:, 0, plant_data.BASE_FEATURES.index("steering_angle_deg")]
  desired_angle = desired_angle + (current_angle - current_desired_angle)[:, None]
  desired_rate = torch.diff(
    torch.cat((current_angle[:, None], desired_angle), dim=1),
    dim=1,
  ) / sample_period_s
  desired_rate = torch.clamp(desired_rate, -180.0, 180.0)
  angle_error = steering_angle - desired_angle
  rate_error = steering_rate - desired_rate
  low_speed_blend = torch.clamp((8.0 - speed) / 7.5, 0.0, 1.0)
  angle_tracking = (
    low_speed_blend
    * F.smooth_l1_loss(
      angle_error / 5.0,
      torch.zeros_like(angle_error),
      beta=0.20,
      reduction="none",
    )
  ).sum() / low_speed_blend.sum().clamp_min(1.0)
  rate_tracking = (
    low_speed_blend
    * F.smooth_l1_loss(
      rate_error / 30.0,
      torch.zeros_like(rate_error),
      beta=0.20,
      reduction="none",
    )
  ).sum() / low_speed_blend.sum().clamp_min(1.0)
  quiet = center | steady | unwind
  wobble = (
    (steering_rate / 12.0).square() * quiet.float()
  ).sum() / quiet.float().sum().clamp_min(1.0)
  state_std = trace["state_std"]
  uncertainty = (trace["disagreement"] / state_std).square().mean()
  turn_in_bias_value = (
    (signed_error * turn_in.float()).sum() / turn_in.float().sum().clamp_min(1.0)
  )
  unwind_bias_value = (
    (signed_error * unwind.float()).sum() / unwind.float().sum().clamp_min(1.0)
  )
  steady_bias_value = (
    (signed_error * steady.float()).sum() / steady.float().sum().clamp_min(1.0)
  )
  inside_bias = (
    torch.relu(turn_in_bias_value).square()
    + 2.0 * torch.relu(unwind_bias_value).square()
    + torch.relu(steady_bias_value).square()
  )
  loss = (
    tracking
    + slew_weight * slew
    + 0.002 * effort
    + 0.03 * saturation
    + wobble_weight * wobble
    + uncertainty_weight * uncertainty
    + inside_bias_weight * inside_bias
    + low_speed_angle_weight * angle_tracking
    + low_speed_rate_weight * rate_tracking
  )

  def rmse(mask: torch.Tensor) -> float:
    return float(torch.sqrt(error[mask].square().mean()).detach().cpu()) if mask.any() else 0.0

  def bias(mask: torch.Tensor) -> float:
    return float(signed_error[mask].mean().detach().cpu()) if mask.any() else 0.0

  return loss, {
    "loss": float(loss.detach().cpu()),
    "tracking": float(tracking.detach().cpu()),
    "rmse": float(torch.sqrt(error.square().mean()).detach().cpu()),
    "sharp_turn_in_rmse": rmse(sharp_turn_in),
    "turn_in_rmse": rmse(turn_in),
    "turn_in_bias": bias(turn_in),
    "unwind_rmse": rmse(unwind),
    "unwind_bias": bias(unwind),
    "steady_rmse": rmse(steady),
    "center_rmse": rmse(center),
    "low_speed_angle_rmse_deg": float(
      torch.sqrt(angle_error[low_speed_blend > 0.0].square().mean()).detach().cpu()
    ) if (low_speed_blend > 0.0).any() else 0.0,
    "intersection_angle_rmse_deg": float(
      torch.sqrt(angle_error[intersection].square().mean()).detach().cpu()
    ) if intersection.any() else 0.0,
    "low_speed_rate_rmse_deg_s": float(
      torch.sqrt(rate_error[low_speed_blend > 0.0].square().mean()).detach().cpu()
    ) if (low_speed_blend > 0.0).any() else 0.0,
    "angle_tracking": float(angle_tracking.detach().cpu()),
    "rate_tracking": float(rate_tracking.detach().cpu()),
    "slew": float(slew.detach().cpu()),
    "effort": float(effort.detach().cpu()),
    "saturation": float(saturation.detach().cpu()),
    "wobble": float(wobble.detach().cpu()),
    "uncertainty": float(uncertainty.detach().cpu()),
    "inside_bias_penalty": float(inside_bias.detach().cpu()),
  }


def evaluate_policy(policy: legacy.FluxPolicy, plant: PlantPredictor,
                    policy_mean: torch.Tensor, policy_std: torch.Tensor,
                    windows: PolicyWindows, args: argparse.Namespace,
                    offsets: tuple[int, ...], sample_period_s: float,
                    seed: int, low_speed_angle_assist_gain: float | None = None) -> dict[str, float]:
  rng = np.random.default_rng(seed)
  indexes = balanced_indexes(
    windows.regimes,
    min(len(windows), args.max_policy_validation_windows),
    rng,
    windows.v_ego[:, 0],
  )
  totals: dict[str, list[tuple[float, int]]] = {}
  policy.eval()
  with torch.no_grad():
    for start in range(0, len(indexes), args.policy_batch_size):
      selected = indexes[start:start + args.policy_batch_size]
      batch = tensor_batch(windows, selected, args.torch_device)
      trace = rollout_policy(
        policy, plant, policy_mean, policy_std, batch, offsets,
        sample_period_s, args.command_rate_limit_per_s,
        (
          args.low_speed_angle_assist_gain
          if low_speed_angle_assist_gain is None
          else low_speed_angle_assist_gain
        ),
        args.low_speed_angle_assist_max,
      )
      _, metrics = policy_loss(
        trace, batch, offsets, sample_period_s, args.command_rate_limit_per_s,
        args.wobble_weight, args.uncertainty_weight, args.slew_weight,
        args.inside_bias_weight, args.low_speed_angle_weight,
        args.low_speed_rate_weight,
      )
      for name, value in metrics.items():
        totals.setdefault(name, []).append((value, len(selected)))
  return {
    name: float(sum(value * count for value, count in values) / sum(count for _, count in values))
    for name, values in totals.items()
  }


def train_policy(policy: legacy.FluxPolicy, plant: PlantPredictor,
                 policy_mean: torch.Tensor, policy_std: torch.Tensor,
                 train_windows: PolicyWindows, validation_windows: PolicyWindows,
                 args: argparse.Namespace, offsets: tuple[int, ...],
                 sample_period_s: float) -> tuple[dict[str, Any], dict[str, Any]]:
  initial = evaluate_policy(
    policy, plant, policy_mean, policy_std, validation_windows,
    args, offsets, sample_period_s, args.random_state + 2, 0.0,
  )
  optimizer = torch.optim.AdamW(policy.parameters(), lr=args.policy_learning_rate, weight_decay=2e-5)
  rng = np.random.default_rng(args.random_state)
  best_state = copy.deepcopy(policy.state_dict())
  best_loss = initial["loss"]
  best_metrics = dict(initial)
  patience = 0
  started = perf_counter()
  epoch = 0
  for epoch in range(1, args.policy_epochs + 1):
    policy.train()
    train_losses: list[float] = []
    for _ in range(args.policy_steps_per_epoch):
      indexes = balanced_indexes(
        train_windows.regimes,
        args.policy_batch_size,
        rng,
        train_windows.v_ego[:, 0],
      )
      batch = tensor_batch(train_windows, indexes, args.torch_device)
      trace = rollout_policy(
        policy, plant, policy_mean, policy_std, batch, offsets,
        sample_period_s, args.command_rate_limit_per_s,
        args.low_speed_angle_assist_gain, args.low_speed_angle_assist_max,
      )
      loss, metrics = policy_loss(
        trace, batch, offsets, sample_period_s, args.command_rate_limit_per_s,
        args.wobble_weight, args.uncertainty_weight, args.slew_weight,
        args.inside_bias_weight, args.low_speed_angle_weight,
        args.low_speed_rate_weight,
      )
      optimizer.zero_grad(set_to_none=True)
      loss.backward()
      torch.nn.utils.clip_grad_norm_(policy.parameters(), 5.0)
      optimizer.step()
      train_losses.append(metrics["loss"])
    validation = evaluate_policy(
      policy, plant, policy_mean, policy_std, validation_windows,
      args, offsets, sample_period_s, args.random_state + 2,
    )
    constrained = (
      validation["sharp_turn_in_rmse"]
      <= initial["sharp_turn_in_rmse"] * (1.0 + args.max_turn_in_accel_regression)
      and validation["turn_in_rmse"]
      <= initial["turn_in_rmse"] * (1.0 + args.max_turn_in_accel_regression)
      and validation["center_rmse"] <= initial["center_rmse"] * (1.0 + args.max_center_regression)
      and validation["unwind_rmse"] <= initial["unwind_rmse"] * (1.0 + args.max_unwind_regression)
      and validation["turn_in_bias"] <= max(initial["turn_in_bias"] + args.max_inside_bias_increase, 0.01)
      and validation["unwind_bias"] <= max(initial["unwind_bias"] + args.max_inside_bias_increase, 0.01)
      and validation["wobble"] <= initial["wobble"] * (1.0 + args.max_wobble_regression)
      and validation["slew"] <= initial["slew"] * (1.0 + args.max_slew_regression)
      and validation["uncertainty"] <= initial["uncertainty"] * (1.0 + args.max_uncertainty_regression)
      and validation["intersection_angle_rmse_deg"]
      <= initial["intersection_angle_rmse_deg"] * (1.0 - args.min_intersection_angle_improvement)
      and validation["low_speed_rate_rmse_deg_s"]
      <= initial["low_speed_rate_rmse_deg_s"] * (1.0 + args.max_low_speed_rate_regression)
    )
    if constrained and validation["loss"] < best_loss - 2e-5:
      best_loss = validation["loss"]
      best_metrics = dict(validation)
      best_state = copy.deepcopy(policy.state_dict())
      patience = 0
    else:
      patience += 1
    if epoch == 1 or epoch % 5 == 0:
        progress = " ".join([
          f"policy epoch={epoch:03d}",
          f"train={np.mean(train_losses):.6f}",
          f"validation={validation['loss']:.6f}",
          f"rmse={validation['rmse']:.6f}",
          f"sharp={validation['sharp_turn_in_rmse']:.6f}",
          f"intersection_angle={validation['intersection_angle_rmse_deg']:.3f}",
          f"low_speed_rate={validation['low_speed_rate_rmse_deg_s']:.3f}",
          f"unwind_bias={validation['unwind_bias']:+.6f}",
        ])
        print(progress, flush=True)
    if patience >= args.policy_patience:
      break
  policy.load_state_dict(best_state)
  policy.eval()
  return (
    {"initial": initial, "optimized": best_metrics},
    {"epochs": epoch, "fit_seconds": perf_counter() - started},
  )


def export_policy(policy: legacy.FluxPolicy, mean: np.ndarray, std: np.ndarray,
                  report: dict[str, Any]) -> dict[str, Any]:
  objective = report.get("objective", {})
  layers = []
  for index, layer in enumerate(policy.layers, 1):
    layers.append({
      f"dense_{index}_W": layer.weight.detach().cpu().numpy().tolist(),
      f"dense_{index}_b": layer.bias.detach().cpu().numpy()[:, None].tolist(),
      "activation": "identity" if index == len(policy.layers) else "sigmoid",
    })
  return {
    "input_std": std[:, None].tolist(),
    "model_test_loss": report["validation"]["optimized"]["rmse"],
    "input_size": len(INPUT_VARS),
    "current_date_and_time": datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S"),
    "input_mean": mean[:, None].tolist(),
    "input_vars": list(INPUT_VARS),
    "output_size": 1,
    "training_car": "HYUNDAI_IONIQ_5",
    "training_method": report.get("method", "goal_based_speed_conditioned_plant"),
    "low_speed_angle_assist_gain": objective.get("low_speed_angle_assist_gain", 0.0),
    "low_speed_angle_assist_max": objective.get("low_speed_angle_assist_max", 0.0),
    "training_rows": report["data"]["train_windows"],
    "training_windows": report["data"]["train_windows"],
    "validation_windows": report["data"]["validation_windows"],
    "holdout_windows": report["data"]["holdout_windows"],
    "layers": layers,
  }


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Train an Ioniq 5 NNFF through the checked-in neural plant ensemble.",
  )
  parser.add_argument("--plant-model", type=Path, default=DEFAULT_PLANT)
  parser.add_argument(
    "--low-speed-plant-model",
    type=Path,
    help="Optional low-speed specialist blended in below 8 m/s and used fully below 5 m/s.",
  )
  parser.add_argument("--initial-model", type=Path, default=DEFAULT_INITIAL_MODEL)
  parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
  parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
  parser.add_argument("--model-output", type=Path, default=DEFAULT_MODEL_OUTPUT)
  parser.add_argument("--train-route-prefix", action="append", default=[])
  parser.add_argument("--validation-route-prefix", action="append", default=[])
  parser.add_argument("--holdout-route-prefix", action="append", default=[])
  parser.add_argument("--rollout-steps", type=int, default=50)
  parser.add_argument("--window-stride", type=int, default=5)
  parser.add_argument("--max-train-windows", type=int, default=12000)
  parser.add_argument("--max-validation-windows", type=int, default=5000)
  parser.add_argument("--max-holdout-windows", type=int, default=6000)
  parser.add_argument("--policy-epochs", type=int, default=50)
  parser.add_argument("--policy-patience", type=int, default=10)
  parser.add_argument("--policy-steps-per-epoch", type=int, default=20)
  parser.add_argument("--policy-batch-size", type=int, default=64)
  parser.add_argument("--policy-learning-rate", type=float, default=2e-4)
  parser.add_argument("--max-policy-validation-windows", type=int, default=2000)
  parser.add_argument("--policy-stats-rows", type=int, default=20000)
  parser.add_argument("--command-rate-limit-per-s", type=float, default=1.5)
  parser.add_argument("--wobble-weight", type=float, default=0.003)
  parser.add_argument("--uncertainty-weight", type=float, default=0.01)
  parser.add_argument("--slew-weight", type=float, default=0.01)
  parser.add_argument("--inside-bias-weight", type=float, default=5.0)
  parser.add_argument("--low-speed-angle-weight", type=float, default=0.15)
  parser.add_argument("--low-speed-rate-weight", type=float, default=0.03)
  parser.add_argument("--low-speed-angle-assist-gain", type=float, default=0.0)
  parser.add_argument("--low-speed-angle-assist-max", type=float, default=0.25)
  parser.add_argument("--max-wobble-regression", type=float, default=0.03)
  parser.add_argument("--max-slew-regression", type=float, default=0.05)
  parser.add_argument("--max-center-regression", type=float, default=0.02)
  parser.add_argument("--max-unwind-regression", type=float, default=0.02)
  parser.add_argument("--max-turn-in-accel-regression", type=float, default=0.05)
  parser.add_argument("--max-uncertainty-regression", type=float, default=0.10)
  parser.add_argument("--max-inside-bias-increase", type=float, default=0.01)
  parser.add_argument("--min-intersection-angle-improvement", type=float, default=0.01)
  parser.add_argument("--max-low-speed-rate-regression", type=float, default=0.02)
  parser.add_argument(
    "--hidden-sizes",
    type=int,
    nargs="+",
    help="Policy hidden sizes; defaults to the initial model architecture.",
  )
  parser.add_argument("--device", default="cuda")
  parser.add_argument("--random-state", type=int, default=23)
  args = parser.parse_args()
  if not args.train_route_prefix:
    args.train_route_prefix = ["00000112"]
  if not args.validation_route_prefix:
    args.validation_route_prefix = ["00000113"]
  if not args.holdout_route_prefix:
    args.holdout_route_prefix = ["00000109", "0000010b"]
  if args.device.startswith("cuda") and not torch.cuda.is_available():
    raise SystemExit("CUDA was requested but is unavailable.")
  args.torch_device = torch.device(args.device)
  torch.manual_seed(args.random_state)
  if torch.cuda.is_available():
    torch.cuda.manual_seed_all(args.random_state)

  models, plant_stats, plant_payload = neural_plant.load_ensemble_artifact(
    args.plant_model, args.torch_device, differentiable=True,
  )
  low_speed_models = None
  low_speed_stats = None
  low_speed_payload = None
  if args.low_speed_plant_model is not None:
    low_speed_models, low_speed_stats, low_speed_payload = neural_plant.load_ensemble_artifact(
      args.low_speed_plant_model, args.torch_device, differentiable=True,
    )
    primary_shape = {key: value for key, value in plant_payload["config"].items() if key != "name"}
    low_speed_shape = {key: value for key, value in low_speed_payload["config"].items() if key != "name"}
    if low_speed_shape != primary_shape:
      raise SystemExit("Primary and low-speed plant artifacts must use the same model config.")
  plant = PlantPredictor(models, plant_stats, low_speed_models, low_speed_stats)
  config = neural_plant.ModelConfig(**plant_payload["config"])
  sample_period_s = config.sample_period_s
  offsets = path_offsets(sample_period_s)
  plant_summary = " ".join([
    f"plant={config.name}",
    f"device={args.torch_device}",
    f"history={config.history_s:.2f}s",
    f"rollout={args.rollout_steps * sample_period_s:.2f}s",
    f"offsets={offsets}",
  ])
  print(plant_summary, flush=True)
  train_trajectories = discover_trajectories(args.log_root, args.train_route_prefix, config.sample_step)
  validation_trajectories = discover_trajectories(
    args.log_root, args.validation_route_prefix, config.sample_step,
  )
  holdout_trajectories = discover_trajectories(
    args.log_root, args.holdout_route_prefix, config.sample_step,
  )
  train_windows = build_windows(
    train_trajectories, config.history_steps, args.rollout_steps, offsets,
    args.window_stride, args.max_train_windows, args.random_state,
  )
  validation_windows = build_windows(
    validation_trajectories, config.history_steps, args.rollout_steps, offsets,
    args.window_stride, args.max_validation_windows, args.random_state + 1,
  )
  holdout_windows = build_windows(
    holdout_trajectories, config.history_steps, args.rollout_steps, offsets,
    args.window_stride, args.max_holdout_windows, args.random_state + 2,
  )

  policy_mean_np, policy_std_np = policy_input_stats(
    train_windows, offsets, args.policy_stats_rows, args.random_state,
  )
  policy_mean = torch.as_tensor(policy_mean_np, dtype=torch.float32, device=args.torch_device)
  policy_std = torch.as_tensor(policy_std_np, dtype=torch.float32, device=args.torch_device)
  initial_payload = json.loads(args.initial_model.read_text(encoding="utf-8"))
  source_input_size = int(initial_payload["input_size"])
  source_hidden_sizes = tuple(
    len(next(value for key, value in layer.items() if key.endswith("_b")))
    for layer in initial_payload["layers"][:-1]
  )
  policy_hidden_sizes = tuple(args.hidden_sizes) if args.hidden_sizes else source_hidden_sizes
  policy = legacy.FluxPolicy(policy_hidden_sizes, len(INPUT_VARS)).to(args.torch_device)
  source = legacy.FluxPolicy(source_hidden_sizes, source_input_size).to(args.torch_device)
  legacy.initialize_policy(
    source, initial_payload, policy_mean_np[:source_input_size], policy_std_np[:source_input_size],
  )
  same_outputs = [layer.out_features for layer in policy.layers] == [
    layer.out_features for layer in source.layers
  ]
  if same_outputs and policy.layers[0].in_features >= source.layers[0].in_features:
    with torch.no_grad():
      for index, (target_layer, source_layer) in enumerate(zip(policy.layers, source.layers, strict=True)):
        target_layer.bias.copy_(source_layer.bias)
        if index == 0:
          target_layer.weight.zero_()
          target_layer.weight[:, :source_input_size].copy_(source_layer.weight)
        else:
          target_layer.weight.copy_(source_layer.weight)
  else:
    legacy.distill_policy(source.cpu(), policy.cpu(), args.random_state)
    policy = policy.to(args.torch_device)

  holdout_initial = evaluate_policy(
    policy, plant, policy_mean, policy_std, holdout_windows,
    args, offsets, sample_period_s, args.random_state + 3, 0.0,
  )
  validation_report, policy_fit = train_policy(
    policy, plant, policy_mean, policy_std,
    train_windows, validation_windows, args, offsets, sample_period_s,
  )
  holdout_optimized = evaluate_policy(
    policy, plant, policy_mean, policy_std, holdout_windows,
    args, offsets, sample_period_s, args.random_state + 3,
  )
  accepted = (
    validation_report["optimized"]["loss"] < validation_report["initial"]["loss"]
    and holdout_optimized["rmse"] <= holdout_initial["rmse"] * 1.02
    and holdout_optimized["sharp_turn_in_rmse"]
    <= holdout_initial["sharp_turn_in_rmse"] * (1.0 + args.max_turn_in_accel_regression)
    and holdout_optimized["unwind_rmse"] <= holdout_initial["unwind_rmse"] * 1.02
    and holdout_optimized["unwind_bias"]
    <= max(holdout_initial["unwind_bias"] + args.max_inside_bias_increase, 0.01)
    and holdout_optimized["wobble"] <= holdout_initial["wobble"] * (1.0 + args.max_wobble_regression)
    and holdout_optimized["slew"] <= holdout_initial["slew"] * (1.0 + args.max_slew_regression)
    and holdout_optimized["uncertainty"]
    <= holdout_initial["uncertainty"] * (1.0 + args.max_uncertainty_regression)
    and holdout_optimized["intersection_angle_rmse_deg"]
    <= holdout_initial["intersection_angle_rmse_deg"] * (1.0 - args.min_intersection_angle_improvement)
    and holdout_optimized["low_speed_rate_rmse_deg_s"]
    <= holdout_initial["low_speed_rate_rmse_deg_s"] * (1.0 + args.max_low_speed_rate_regression)
  )
  report = {
    "method": (
      "goal_based_speed_conditioned_hybrid_plant"
      if args.low_speed_plant_model is not None
      else "goal_based_speed_conditioned_plant"
    ),
    "plant_artifact": str(args.plant_model),
    "low_speed_plant_artifact": (
      str(args.low_speed_plant_model) if args.low_speed_plant_model is not None else None
    ),
    "plant_config": plant_payload["config"],
    "accepted": accepted,
    "data": {
      "log_root": str(args.log_root),
      "train_route_prefixes": args.train_route_prefix,
      "validation_route_prefixes": args.validation_route_prefix,
      "holdout_route_prefixes": args.holdout_route_prefix,
      "segment_count": len(train_trajectories) + len(validation_trajectories) + len(holdout_trajectories),
      "train_windows": len(train_windows),
      "validation_windows": len(validation_windows),
      "holdout_windows": len(holdout_windows),
      "train_regimes": dict(Counter(train_windows.regimes)),
      "validation_regimes": dict(Counter(validation_windows.regimes)),
      "holdout_regimes": dict(Counter(holdout_windows.regimes)),
    },
    "objective": {
      "balanced_regimes": list(REGIMES),
      "sharp_turn_in_extra_weight": 3.0,
      "inside_error_extra_weight": 0.75,
      "turn_in_slew_gate": 0.55,
      "sharp_turn_in_slew_gate": 0.20,
      "wobble_weight": args.wobble_weight,
      "uncertainty_weight": args.uncertainty_weight,
      "slew_weight": args.slew_weight,
      "inside_bias_weight": args.inside_bias_weight,
      "low_speed_angle_weight": args.low_speed_angle_weight,
      "low_speed_rate_weight": args.low_speed_rate_weight,
      "low_speed_angle_assist_gain": args.low_speed_angle_assist_gain,
      "low_speed_angle_assist_max": args.low_speed_angle_assist_max,
      "max_wobble_regression": args.max_wobble_regression,
      "max_slew_regression": args.max_slew_regression,
      "max_center_regression": args.max_center_regression,
      "max_unwind_regression": args.max_unwind_regression,
      "max_turn_in_accel_regression": args.max_turn_in_accel_regression,
      "max_uncertainty_regression": args.max_uncertainty_regression,
      "max_inside_bias_increase": args.max_inside_bias_increase,
      "min_intersection_angle_improvement": args.min_intersection_angle_improvement,
      "max_low_speed_rate_regression": args.max_low_speed_rate_regression,
      "command_rate_limit_per_s": args.command_rate_limit_per_s,
    },
    "validation": validation_report,
    "holdout": {
      "initial": holdout_initial,
      "optimized": holdout_optimized,
    },
    "policy_fit": policy_fit,
    "policy": {
      "hidden_sizes": list(policy_hidden_sizes),
      "parameters": sum(parameter.numel() for parameter in policy.parameters()),
    },
  }
  payload = export_policy(policy, policy_mean_np, policy_std_np, report)
  roundtrip_inputs = np.random.default_rng(args.random_state).normal(
    policy_mean_np, policy_std_np, size=(128, len(INPUT_VARS)),
  )
  with torch.no_grad():
    torch_prediction = policy(torch.as_tensor(
      (roundtrip_inputs - policy_mean_np) / policy_std_np,
      dtype=torch.float32, device=args.torch_device,
    )).cpu().numpy()[:, 0]
  json_prediction = flux_predict(payload, roundtrip_inputs)
  report["roundtrip_max_abs_error"] = float(np.max(np.abs(torch_prediction - json_prediction)))
  if report["roundtrip_max_abs_error"] > 1e-5:
    raise RuntimeError(f"Flux export mismatch: {report['roundtrip_max_abs_error']}")

  args.output_dir.mkdir(parents=True, exist_ok=True)
  args.model_output.parent.mkdir(parents=True, exist_ok=True)
  args.model_output.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
  (args.output_dir / "training.json").write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
  )
  torch.save({
    "policy": {name: value.detach().cpu() for name, value in policy.state_dict().items()},
    "policy_mean": policy_mean_np,
    "policy_std": policy_std_np,
    "report": report,
  }, args.output_dir / "training.pt")
  print(json.dumps({
    "accepted": report["accepted"],
    "validation": report["validation"],
    "holdout": report["holdout"],
    "parameters": report["policy"]["parameters"],
    "roundtrip_max_abs_error": report["roundtrip_max_abs_error"],
  }, indent=2), flush=True)
  print(f"model: {args.model_output}", flush=True)


if __name__ == "__main__":
  main()
