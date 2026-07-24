#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
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
  from joblib import load
  from torch import nn
except ModuleNotFoundError as e:
  raise SystemExit(
    "Run with: uv run --no-project --with torch --with scikit-learn --with joblib " +
    "--with pycapnp==2.1.0 --with zstandard python tools/tuning/train_goal_based_ioniq5_nnff.py"
  ) from e

from openpilot.tools.tuning import train_lateral_plant_model as plant_data
from openpilot.tools.tuning.train_ioniq5_nnff import INPUT_VARS, flux_predict


DEFAULT_PLANT = REPO_ROOT / "artifacts/tuning/lateral_plant_all_goal_20260723/lateral_plant_model.joblib"
DEFAULT_INITIAL_MODEL = REPO_ROOT / "starpilot/assets/nnff_models/HYUNDAI_IONIQ_5.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts/tuning/ioniq5_nnff_goal_20260723"
DEFAULT_MODEL_OUTPUT = DEFAULT_OUTPUT_DIR / "HYUNDAI_IONIQ_5.json"
PATH_OFFSETS = (-6, -4, -2, 8, 14, 22, 32)
CURRENT_INDEX = -min(PATH_OFFSETS)
STATE_CLAMPS = (
  (-5.0, 5.0),
  (-180.0, 180.0),
  (-500.0, 500.0),
  (-500.0, 500.0),
)


@dataclass
class WindowSet:
  history: np.ndarray
  path: np.ndarray
  jerk: np.ndarray
  v_ego: np.ndarray
  a_ego: np.ndarray
  logged_applied: np.ndarray
  logged_controller: np.ndarray
  target_states: np.ndarray
  segments: list[str]

  def __len__(self) -> int:
    return len(self.history)

  def subset(self, indexes: np.ndarray) -> WindowSet:
    return WindowSet(
      history=self.history[indexes],
      path=self.path[indexes],
      jerk=self.jerk[indexes],
      v_ego=self.v_ego[indexes],
      a_ego=self.a_ego[indexes],
      logged_applied=self.logged_applied[indexes],
      logged_controller=self.logged_controller[indexes],
      target_states=self.target_states[indexes],
      segments=[self.segments[index] for index in indexes],
    )


class NeuralPlant(nn.Module):
  def __init__(self, input_size: int, output_size: int):
    super().__init__()
    self.network = nn.Sequential(
      nn.Linear(input_size, 128),
      nn.SiLU(),
      nn.Linear(128, 128),
      nn.SiLU(),
      nn.Linear(128, 64),
      nn.SiLU(),
      nn.Linear(64, output_size),
    )

  def forward(self, values: torch.Tensor) -> torch.Tensor:
    return self.network(values)


class FluxPolicy(nn.Module):
  def __init__(self, hidden_sizes: tuple[int, ...] = (24, 12, 6), input_size: int = 18):
    super().__init__()
    sizes = (input_size, *hidden_sizes, 1)
    self.layers = nn.ModuleList([
      nn.Linear(input_size, output_size)
      for input_size, output_size in zip(sizes[:-1], sizes[1:], strict=True)
    ])

  def forward(self, values: torch.Tensor) -> torch.Tensor:
    for layer in self.layers[:-1]:
      values = torch.sigmoid(layer(values))
    return self.layers[-1](values)


def load_segment_trajectories(metadata: dict[str, Any], segment_key: str) -> list[plant_data.Trajectory]:
  root = Path(metadata["log_root"])
  excluded = set(metadata.get("excluded_routes", []))
  trajectories: list[plant_data.Trajectory] = []
  segments = list(dict.fromkeys(metadata[segment_key]))
  for index, segment in enumerate(segments, 1):
    path = root / segment / "rlog.zst"
    try:
      trajectory = plant_data.read_trajectory(
        path, "hyundai", "IONIQ5", int(metadata["sample_step"]),
      )
    except Exception as e:
      print(f"skip {segment}: {e}", file=sys.stderr)
      continue
    if trajectory is not None and trajectory.route not in excluded:
      trajectories.append(trajectory)
    if index % 25 == 0 or index == len(segments):
      print(f"loaded {segment_key} {index}/{len(segments)}; usable={len(trajectories)}", flush=True)
  return trajectories


def build_windows(trajectories: list[plant_data.Trajectory], history_steps: int,
                  rollout_steps: int) -> WindowSet:
  histories: list[np.ndarray] = []
  paths: list[np.ndarray] = []
  jerks: list[np.ndarray] = []
  speeds: list[np.ndarray] = []
  accelerations: list[np.ndarray] = []
  applied: list[np.ndarray] = []
  controllers: list[np.ndarray] = []
  targets: list[np.ndarray] = []
  segments: list[str] = []
  base_names = list(plant_data.BASE_FEATURES)
  state_names = list(plant_data.STATE_FEATURES)
  path_relative = np.arange(min(PATH_OFFSETS), rollout_steps + max(PATH_OFFSETS) + 1)

  for trajectory in trajectories:
    values = trajectory.values
    lower = max(history_steps - 1, -min(PATH_OFFSETS))
    upper = len(trajectory.times) - rollout_steps - max(PATH_OFFSETS) - 1
    if upper <= lower:
      continue
    source = np.arange(lower, upper)
    active_index = source[:, None] + np.arange(rollout_steps + 1)
    path_index = source[:, None] + path_relative
    clean = (
      (values["lat_active"][active_index].min(axis=1) > 0.5)
      & (values["driver_overlay"][active_index].max(axis=1) < 0.5)
      & (values["saturated"][active_index].max(axis=1) < 0.5)
      & (values["v_ego"][source] >= 5.0)
      & (np.diff(trajectory.times[path_index], axis=1).max(axis=1) < 0.09)
    )
    source = source[clean]
    path_index = path_index[clean]
    if not len(source):
      continue
    history_index = source[:, None] - np.arange(history_steps)
    future_index = source[:, None] + np.arange(rollout_steps)
    target_index = source[:, None] + np.arange(1, rollout_steps + 1)
    histories.append(np.stack(
      [np.column_stack([values[name][indexes] for name in base_names]) for indexes in history_index],
    ))
    paths.append(values["desired_lateral_accel"][path_index])
    jerks.append(values["desired_lateral_jerk"][future_index])
    speeds.append(values["v_ego"][future_index])
    accelerations.append(values["a_ego"][future_index])
    applied.append(values["applied_torque"][future_index])
    controllers.append(values["controller_output"][future_index])
    targets.append(np.stack(
      [np.column_stack([values[name][indexes] for name in state_names]) for indexes in target_index],
    ))
    segments.extend([trajectory.segment] * len(source))

  if not histories:
    raise SystemExit("No clean rollout windows found.")
  return WindowSet(
    history=np.concatenate(histories).astype(np.float32),
    path=np.concatenate(paths).astype(np.float32),
    jerk=np.concatenate(jerks).astype(np.float32),
    v_ego=np.concatenate(speeds).astype(np.float32),
    a_ego=np.concatenate(accelerations).astype(np.float32),
    logged_applied=np.concatenate(applied).astype(np.float32),
    logged_controller=np.concatenate(controllers).astype(np.float32),
    target_states=np.concatenate(targets).astype(np.float32),
    segments=segments,
  )


def load_or_build_windows(cache: Path, metadata: dict[str, Any], segment_key: str,
                          history_steps: int, rollout_steps: int) -> WindowSet:
  cache_file = cache / f"{segment_key}_h{history_steps}_r{rollout_steps}.npz"
  segment_file = cache / f"{segment_key}_h{history_steps}_r{rollout_steps}_segments.json"
  if cache_file.exists() and segment_file.exists():
    arrays = np.load(cache_file)
    result = WindowSet(
      history=arrays["history"],
      path=arrays["path"],
      jerk=arrays["jerk"],
      v_ego=arrays["v_ego"],
      a_ego=arrays["a_ego"],
      logged_applied=arrays["logged_applied"],
      logged_controller=arrays["logged_controller"],
      target_states=arrays["target_states"],
      segments=json.loads(segment_file.read_text(encoding="utf-8")),
    )
    print(f"loaded cached {segment_key} windows={len(result)}")
    return result

  trajectories = load_segment_trajectories(metadata, segment_key)
  result = build_windows(trajectories, history_steps, rollout_steps)
  cache.mkdir(parents=True, exist_ok=True)
  np.savez_compressed(
    cache_file,
    history=result.history,
    path=result.path,
    jerk=result.jerk,
    v_ego=result.v_ego,
    a_ego=result.a_ego,
    logged_applied=result.logged_applied,
    logged_controller=result.logged_controller,
    target_states=result.target_states,
  )
  segment_file.write_text(json.dumps(result.segments) + "\n", encoding="utf-8")
  print(f"cached {segment_key} windows={len(result)} at {cache_file}")
  return result


def split_validation(metadata: dict[str, Any], windows: WindowSet) -> tuple[WindowSet, WindowSet]:
  holdout_prefixes = tuple(metadata.get("forced_holdout_route_prefixes", []))
  holdout = np.asarray([
    any(segment.rsplit("--", 1)[0].startswith(prefix) for prefix in holdout_prefixes)
    for segment in windows.segments
  ])
  return windows.subset(np.flatnonzero(~holdout)), windows.subset(np.flatnonzero(holdout))


def policy_input_stats(windows: WindowSet, max_rows: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
  rng = np.random.default_rng(seed)
  window_indexes = rng.choice(len(windows), min(len(windows), max_rows), replace=False)
  step_indexes = rng.integers(0, windows.jerk.shape[1], len(window_indexes))
  actual_index = plant_data.STATE_FEATURES.index("actual_lateral_accel")
  base_actual_index = plant_data.BASE_FEATURES.index("actual_lateral_accel")
  actual = np.where(
    step_indexes == 0,
    windows.history[window_indexes, 0, base_actual_index],
    windows.target_states[window_indexes, np.maximum(step_indexes - 1, 0), actual_index],
  )
  path_points = np.column_stack([
    windows.path[window_indexes, CURRENT_INDEX + step_indexes + offset]
    for offset in PATH_OFFSETS
  ])
  desired = windows.path[window_indexes, CURRENT_INDEX + step_indexes]
  friction = 0.7 * (desired - actual) + 0.4 * windows.jerk[window_indexes, step_indexes]
  zeros = np.zeros((len(window_indexes), 7), dtype=np.float32)
  inputs = np.column_stack((
    windows.v_ego[window_indexes, step_indexes],
    desired,
    friction,
    np.zeros(len(window_indexes), dtype=np.float32),
    path_points,
    zeros,
  )).astype(np.float64)
  mean = inputs.mean(axis=0)
  std = inputs.std(axis=0)
  std[std < 1e-4] = 1.0
  return mean, std


def initialize_policy(policy: FluxPolicy, payload: dict[str, Any],
                      new_mean: np.ndarray, new_std: np.ndarray) -> None:
  expected_shapes = [
    (
      len(next(value for key, value in layer.items() if key.endswith("_b"))),
      len(next(value for key, value in layer.items() if key.endswith("_W"))[0]),
    )
    for layer in payload["layers"]
  ]
  actual_shapes = [(layer.out_features, layer.in_features) for layer in policy.layers]
  if expected_shapes != actual_shapes:
    raise ValueError(f"Payload architecture {expected_shapes} does not match policy {actual_shapes}.")
  old_mean = np.asarray(payload["input_mean"], dtype=np.float64)[:, 0]
  old_std = np.asarray(payload["input_std"], dtype=np.float64)[:, 0]
  with torch.no_grad():
    for index, layer in enumerate(payload["layers"]):
      weight = np.asarray(next(value for key, value in layer.items() if key.endswith("_W")), dtype=np.float64)
      bias = np.asarray(next(value for key, value in layer.items() if key.endswith("_b")), dtype=np.float64)[:, 0]
      if index == 0:
        original = weight.copy()
        weight = original * (new_std / old_std)[None, :]
        bias = bias + original @ ((new_mean - old_mean) / old_std)
      policy.layers[index].weight.copy_(torch.as_tensor(weight, dtype=torch.float32))
      policy.layers[index].bias.copy_(torch.as_tensor(bias, dtype=torch.float32))


def distill_policy(source: FluxPolicy, target: FluxPolicy, seed: int,
                   epochs: int = 100, rows: int = 30000) -> None:
  generator = torch.Generator().manual_seed(seed)
  source_input_size = source.layers[0].in_features
  target_input_size = target.layers[0].in_features
  if target_input_size < source_input_size:
    raise ValueError("The distilled policy cannot have fewer inputs than its source.")
  inputs = torch.clamp(torch.randn((rows, target_input_size), generator=generator), -3.5, 3.5)
  inputs[:, source_input_size:] = 0.0
  with torch.no_grad():
    target.layers[0].weight[:, source_input_size:] = 0.0
  source.eval()
  with torch.no_grad():
    labels = source(inputs[:, :source_input_size])
  optimizer = torch.optim.AdamW(target.parameters(), lr=1e-3, weight_decay=1e-5)
  for epoch in range(1, epochs + 1):
    permutation = torch.randperm(rows, generator=generator)
    losses = []
    for start in range(0, rows, 2048):
      selected = permutation[start:start + 2048]
      prediction = target(inputs[selected])
      loss = F.mse_loss(prediction, labels[selected])
      optimizer.zero_grad()
      loss.backward()
      optimizer.step()
      losses.append(float(loss.detach()))
    if epoch == 1 or epoch % 20 == 0:
      print(f"policy distill epoch={epoch:03d} mse={np.mean(losses):.8f}", flush=True)


def policy_from_state_dict(state_dict: dict[str, torch.Tensor]) -> FluxPolicy:
  layer_indexes = sorted({
    int(key.split(".")[1])
    for key in state_dict
    if key.startswith("layers.") and key.endswith(".weight")
  })
  output_sizes = [int(state_dict[f"layers.{index}.weight"].shape[0]) for index in layer_indexes]
  input_size = int(state_dict[f"layers.{layer_indexes[0]}.weight"].shape[1])
  policy = FluxPolicy(tuple(output_sizes[:-1]), input_size)
  policy.load_state_dict(state_dict)
  return policy


def augmented_teacher_rows(windows: WindowSet, teacher: Any, augmentations: int,
                           max_rows: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
  rng = np.random.default_rng(seed)
  indexes = rng.choice(len(windows), min(len(windows), max_rows), replace=False)
  base = windows.history[indexes]
  rows = [base]
  for _ in range(max(0, augmentations - 1)):
    perturbed = base.copy()
    noise = rng.normal(0.0, 0.10, len(perturbed)).astype(np.float32)
    torque_index = plant_data.BASE_FEATURES.index("applied_torque")
    perturbed[:, 0, torque_index] = np.clip(perturbed[:, 0, torque_index] + noise, -1.0, 1.0)
    rows.append(perturbed)
  x = np.concatenate(rows).reshape((-1, windows.history.shape[1] * windows.history.shape[2]))
  y = teacher.predict(x).astype(np.float32)
  return x.astype(np.float32), y


def train_surrogate(teacher: Any, train_windows: WindowSet, validation_windows: WindowSet,
                    args: argparse.Namespace) -> tuple[NeuralPlant, dict[str, np.ndarray], dict[str, Any]]:
  x_train, y_train = augmented_teacher_rows(
    train_windows, teacher, args.plant_augmentations, args.max_plant_train_rows, args.random_state,
  )
  x_validation, y_validation = augmented_teacher_rows(
    validation_windows, teacher, args.plant_augmentations, args.max_plant_validation_rows, args.random_state + 1,
  )
  x_mean = x_train.mean(axis=0, dtype=np.float64)
  x_std = x_train.std(axis=0, dtype=np.float64)
  x_std[x_std < 1e-5] = 1.0
  y_mean = y_train.mean(axis=0, dtype=np.float64)
  y_std = y_train.std(axis=0, dtype=np.float64)
  y_std[y_std < 1e-6] = 1.0
  stats = {"x_mean": x_mean, "x_std": x_std, "y_mean": y_mean, "y_std": y_std}

  train_x = torch.as_tensor((x_train - x_mean) / x_std, dtype=torch.float32)
  train_y = torch.as_tensor((y_train - y_mean) / y_std, dtype=torch.float32)
  val_x = torch.as_tensor((x_validation - x_mean) / x_std, dtype=torch.float32)
  val_y = torch.as_tensor((y_validation - y_mean) / y_std, dtype=torch.float32)
  model = NeuralPlant(train_x.shape[1], train_y.shape[1])
  optimizer = torch.optim.AdamW(model.parameters(), lr=args.plant_learning_rate, weight_decay=1e-4)
  generator = torch.Generator().manual_seed(args.random_state)
  best_state = copy.deepcopy(model.state_dict())
  best_loss = math.inf
  patience = 0
  started = perf_counter()
  for epoch in range(1, args.plant_epochs + 1):
    model.train()
    permutation = torch.randperm(len(train_x), generator=generator)
    losses = []
    for start in range(0, len(permutation), args.plant_batch_size):
      selected = permutation[start:start + args.plant_batch_size]
      prediction = model(train_x[selected])
      loss = F.smooth_l1_loss(prediction, train_y[selected], beta=0.2)
      optimizer.zero_grad()
      loss.backward()
      optimizer.step()
      losses.append(float(loss.detach()))
    model.eval()
    with torch.no_grad():
      val_loss = float(F.smooth_l1_loss(model(val_x), val_y, beta=0.2))
    if val_loss < best_loss - 1e-5:
      best_loss = val_loss
      best_state = copy.deepcopy(model.state_dict())
      patience = 0
    else:
      patience += 1
    if epoch == 1 or epoch % 5 == 0:
      print(f"plant epoch={epoch:03d} train={np.mean(losses):.6f} validation={val_loss:.6f}", flush=True)
    if patience >= args.plant_patience:
      break
  model.load_state_dict(best_state)
  model.eval()
  with torch.no_grad():
    prediction = model(val_x).numpy() * y_std + y_mean
  errors = prediction - y_validation
  report = {
    "train_rows": int(len(x_train)),
    "validation_rows": int(len(x_validation)),
    "epochs": epoch,
    "fit_seconds": perf_counter() - started,
    "teacher_delta_rmse": {
      name: float(np.sqrt(np.mean(errors[:, index] ** 2)))
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
  }
  return model, stats, report


def tensor_batch(windows: WindowSet, indexes: np.ndarray) -> dict[str, torch.Tensor]:
  return {
    "history": torch.as_tensor(windows.history[indexes], dtype=torch.float32),
    "path": torch.as_tensor(windows.path[indexes], dtype=torch.float32),
    "jerk": torch.as_tensor(windows.jerk[indexes], dtype=torch.float32),
    "v_ego": torch.as_tensor(windows.v_ego[indexes], dtype=torch.float32),
    "a_ego": torch.as_tensor(windows.a_ego[indexes], dtype=torch.float32),
    "logged_applied": torch.as_tensor(windows.logged_applied[indexes], dtype=torch.float32),
    "logged_controller": torch.as_tensor(windows.logged_controller[indexes], dtype=torch.float32),
    "target_states": torch.as_tensor(windows.target_states[indexes], dtype=torch.float32),
  }


def early_unwind(desired: torch.Tensor, preview: torch.Tensor) -> torch.Tensor:
  desired_abs = desired.abs()
  preview_abs = torch.where(desired * preview > 0.0, preview.abs(), torch.zeros_like(preview))
  drop = desired_abs - preview_abs
  gate = torch.clamp((drop - 0.12) / 0.25, 0.0, 1.0)
  reduction = torch.minimum(0.25 * drop * gate, 0.15 * desired_abs)
  enabled = (desired_abs >= 0.35) & (drop > 0.12)
  return torch.where(enabled, torch.sign(desired) * (desired_abs - reduction), desired)


def plant_step(model: NeuralPlant, stats: dict[str, torch.Tensor],
               history: torch.Tensor, teacher: Any | None = None) -> torch.Tensor:
  flat = history.flatten(1)
  normalized = (flat - stats["x_mean"]) / stats["x_std"]
  surrogate_delta = model(normalized) * stats["y_std"] + stats["y_mean"]
  if teacher is None:
    delta = surrogate_delta
  else:
    # Exact boosted-tree response in the forward pass, differentiable neural
    # surrogate in the backward pass. This prevents policy exploitation of
    # small surrogate errors while retaining useful plant gradients.
    with torch.no_grad():
      teacher_delta = torch.as_tensor(
        teacher.predict(flat.detach().cpu().numpy()), dtype=torch.float32,
      )
    delta = teacher_delta + surrogate_delta - surrogate_delta.detach()
  state_indexes = [plant_data.BASE_FEATURES.index(name) for name in plant_data.STATE_FEATURES]
  return history[:, 0, state_indexes] + delta


def rollout_policy(policy: FluxPolicy, plant: NeuralPlant, plant_stats: dict[str, torch.Tensor],
                   policy_mean: torch.Tensor, policy_std: torch.Tensor,
                   batch: dict[str, torch.Tensor], rate_limit: float,
                   teacher: Any | None = None) -> dict[str, torch.Tensor]:
  history = batch["history"].clone()
  base_index = {name: index for index, name in enumerate(plant_data.BASE_FEATURES)}
  state_indexes = [base_index[name] for name in plant_data.STATE_FEATURES]
  actual_index = plant_data.STATE_FEATURES.index("actual_lateral_accel")
  rate_index = plant_data.STATE_FEATURES.index("signed_steering_rate_deg_s")
  integral = torch.zeros(len(history))
  previous_command = history[:, 0, base_index["applied_torque"]]
  errors = []
  commands = []
  states = []

  def evaluate(values: torch.Tensor) -> torch.Tensor:
    return policy((values - policy_mean) / policy_std)[:, 0]

  for step in range(batch["jerk"].shape[1]):
    desired = batch["path"][:, CURRENT_INDEX + step]
    preview = batch["path"][:, CURRENT_INDEX + step + 22]
    control_desired = early_unwind(desired, preview)
    actual = history[:, 0, base_index["actual_lateral_accel"]]
    speed = batch["v_ego"][:, step]
    jerk = batch["jerk"][:, step]
    path_points = torch.column_stack([
      batch["path"][:, CURRENT_INDEX + step + offset] for offset in PATH_OFFSETS
    ])
    zeros = torch.zeros((len(history), 7))
    friction = 0.7 * (control_desired - actual) + 0.4 * jerk
    feedforward_input = torch.column_stack((
      speed, control_desired, friction, torch.zeros(len(history)), path_points, zeros,
    ))
    setpoint_input = torch.column_stack((
      speed, control_desired, jerk, torch.zeros(len(history)),
      control_desired[:, None].repeat(1, 7), zeros,
    ))
    measurement_input = torch.column_stack((
      speed, actual, torch.zeros(len(history)), torch.zeros(len(history)),
      actual[:, None].repeat(1, 7), zeros,
    ))
    torque_error = evaluate(setpoint_input) - evaluate(measurement_input)
    integral = integral + 0.3 * 0.05 * torque_error
    raw_command = torch.clamp(-(evaluate(feedforward_input) + torque_error + integral), -1.0, 1.0)
    if teacher is None:
      delta = rate_limit * torch.tanh((raw_command - previous_command) / rate_limit)
      command = torch.clamp(previous_command + delta, -1.0, 1.0)
    else:
      limiter_gap = batch["logged_applied"][:, step] - batch["logged_controller"][:, step]
      command = torch.clamp(raw_command + limiter_gap, -1.0, 1.0)
    history[:, 0, base_index["applied_torque"]] = command
    next_state = plant_step(plant, plant_stats, history, teacher)
    next_state = torch.column_stack([
      torch.clamp(next_state[:, index], lower, upper)
      for index, (lower, upper) in enumerate(STATE_CLAMPS)
    ])
    target_desired = batch["path"][:, CURRENT_INDEX + step + 1]
    errors.append(next_state[:, actual_index] - target_desired)
    commands.append(command)
    states.append(next_state)

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
  }


def policy_loss(trace: dict[str, torch.Tensor], batch: dict[str, torch.Tensor],
                wobble_weight: float) -> tuple[torch.Tensor, dict[str, float]]:
  desired = torch.stack([
    batch["path"][:, CURRENT_INDEX + step + 1] for step in range(trace["errors"].shape[1])
  ], dim=1)
  jerk = batch["jerk"]
  error = trace["errors"]
  commands = trace["commands"]
  center = desired.abs() < 0.08
  transition = jerk.abs() >= 0.08
  unwind = transition & (desired * jerk < 0.0)
  approaching_straight = (desired.abs() >= 0.15) & (
    batch["path"][:, CURRENT_INDEX + 32:CURRENT_INDEX + 32 + error.shape[1]].abs() < 0.08
  )
  weights = (
    torch.ones_like(error)
    + 0.20 * center.float()
    + 1.50 * transition.float()
    + 0.50 * unwind.float()
    + 0.50 * approaching_straight.float()
  )
  tracking = (weights * F.smooth_l1_loss(error, torch.zeros_like(error), beta=0.05, reduction="none")).mean()
  initial_command = batch["history"][:, 0, plant_data.BASE_FEATURES.index("applied_torque")]
  command_delta = torch.diff(torch.cat((initial_command[:, None], commands), dim=1), dim=1)
  slew = (command_delta / 0.08).square().mean()
  effort = commands.square().mean()
  saturation = torch.relu(commands.abs() - 0.90).square().mean()
  rate_index = plant_data.STATE_FEATURES.index("signed_steering_rate_deg_s")
  steering_rate = trace["states"][:, :, rate_index]
  quiet = center | ((desired.abs() >= 0.08) & (jerk.abs() < 0.08))
  wobble = ((steering_rate / 12.0).square() * quiet.float()).sum() / quiet.float().sum().clamp_min(1.0)
  loss = tracking + 0.006 * slew + 0.002 * effort + 0.03 * saturation + wobble_weight * wobble
  metrics = {
    "loss": float(loss.detach()),
    "tracking": float(tracking.detach()),
    "rmse": float(torch.sqrt(error.square().mean()).detach()),
    "center_rmse": float(torch.sqrt(error[center].square().mean()).detach()) if center.any() else 0.0,
    "transition_rmse": float(torch.sqrt(error[transition].square().mean()).detach()) if transition.any() else 0.0,
    "unwind_rmse": float(torch.sqrt(error[unwind].square().mean()).detach()) if unwind.any() else 0.0,
    "approaching_straight_rmse": (
      float(torch.sqrt(error[approaching_straight].square().mean()).detach()) if approaching_straight.any() else 0.0
    ),
    "slew": float(slew.detach()),
    "effort": float(effort.detach()),
    "saturation": float(saturation.detach()),
    "wobble": float(wobble.detach()),
  }
  return loss, metrics


def evaluate_policy(policy: FluxPolicy, plant: NeuralPlant, plant_stats: dict[str, torch.Tensor],
                    policy_mean: torch.Tensor, policy_std: torch.Tensor, windows: WindowSet,
                    args: argparse.Namespace, seed: int, teacher: Any | None = None) -> dict[str, float]:
  rng = np.random.default_rng(seed)
  indexes = rng.choice(len(windows), min(len(windows), args.max_policy_validation_windows), replace=False)
  totals: dict[str, list[float]] = {}
  policy.eval()
  with torch.no_grad():
    for start in range(0, len(indexes), args.policy_batch_size):
      batch = tensor_batch(windows, indexes[start:start + args.policy_batch_size])
      trace = rollout_policy(
        policy, plant, plant_stats, policy_mean, policy_std, batch, args.command_rate_limit, teacher,
      )
      _, metrics = policy_loss(trace, batch, args.wobble_weight)
      for name, value in metrics.items():
        totals.setdefault(name, []).append(value)
  return {name: float(np.mean(values)) for name, values in totals.items()}


def train_policy(policy: FluxPolicy, plant: NeuralPlant, plant_stats: dict[str, torch.Tensor],
                 policy_mean: torch.Tensor, policy_std: torch.Tensor,
                 train_windows: WindowSet, validation_windows: WindowSet,
                 args: argparse.Namespace, teacher: Any | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
  for parameter in plant.parameters():
    parameter.requires_grad_(False)
  initial = evaluate_policy(
    policy, plant, plant_stats, policy_mean, policy_std, validation_windows, args, args.random_state + 2, teacher,
  )
  optimizer = torch.optim.AdamW(policy.parameters(), lr=args.policy_learning_rate, weight_decay=2e-5)
  rng = np.random.default_rng(args.random_state)
  best_state = copy.deepcopy(policy.state_dict())
  best_loss = initial["loss"]
  best_metrics = dict(initial)
  patience = 0
  started = perf_counter()
  for epoch in range(1, args.policy_epochs + 1):
    policy.train()
    train_metrics: list[float] = []
    for _ in range(args.policy_steps_per_epoch):
      indexes = rng.choice(len(train_windows), args.policy_batch_size, replace=len(train_windows) < args.policy_batch_size)
      batch = tensor_batch(train_windows, indexes)
      trace = rollout_policy(
        policy, plant, plant_stats, policy_mean, policy_std, batch, args.command_rate_limit, teacher,
      )
      loss, metrics = policy_loss(trace, batch, args.wobble_weight)
      optimizer.zero_grad()
      loss.backward()
      torch.nn.utils.clip_grad_norm_(policy.parameters(), 5.0)
      optimizer.step()
      train_metrics.append(metrics["loss"])
    validation = evaluate_policy(
      policy, plant, plant_stats, policy_mean, policy_std, validation_windows, args, args.random_state + 2, teacher,
    )
    if validation["loss"] < best_loss - 2e-5:
      best_loss = validation["loss"]
      best_metrics = dict(validation)
      best_state = copy.deepcopy(policy.state_dict())
      patience = 0
    else:
      patience += 1
    if epoch == 1 or epoch % 5 == 0:
      print(
        f"policy epoch={epoch:03d} train={np.mean(train_metrics):.6f} " +
        f"validation={validation['loss']:.6f} rmse={validation['rmse']:.6f} " +
        f"transition={validation['transition_rmse']:.6f}",
        flush=True,
      )
    if patience >= args.policy_patience:
      break
  policy.load_state_dict(best_state)
  policy.eval()
  return (
    {"initial": initial, "optimized": best_metrics},
    {"epochs": epoch, "fit_seconds": perf_counter() - started},
  )


def export_policy(policy: FluxPolicy, mean: np.ndarray, std: np.ndarray,
                  metadata: dict[str, Any]) -> dict[str, Any]:
  layers = []
  for index, layer in enumerate(policy.layers, 1):
    layers.append({
      f"dense_{index}_W": layer.weight.detach().cpu().numpy().tolist(),
      f"dense_{index}_b": layer.bias.detach().cpu().numpy()[:, None].tolist(),
      "activation": "identity" if index == len(policy.layers) else "sigmoid",
    })
  return {
    "input_std": std[:, None].tolist(),
    "model_test_loss": metadata["validation"]["optimized"]["rmse"],
    "input_size": len(INPUT_VARS),
    "current_date_and_time": datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S"),
    "input_mean": mean[:, None].tolist(),
    "input_vars": list(INPUT_VARS),
    "output_size": 1,
    "training_car": "HYUNDAI_IONIQ_5",
    "training_method": "goal_based_differentiable_vehicle_plant",
    "training_rows": metadata["data"]["plant_fit_samples"],
    "training_windows": metadata["data"]["policy_train_windows"],
    "validation_windows": metadata["data"]["policy_validation_windows"],
    "holdout_windows": metadata["data"]["policy_holdout_windows"],
    "layers": layers,
  }


def main() -> None:
  parser = argparse.ArgumentParser(description="Train a compact Ioniq 5 NNFF against a differentiable vehicle plant.")
  parser.add_argument("--plant-model", type=Path, default=DEFAULT_PLANT)
  parser.add_argument("--initial-model", type=Path, default=DEFAULT_INITIAL_MODEL)
  parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
  parser.add_argument("--model-output", type=Path, default=DEFAULT_MODEL_OUTPUT)
  parser.add_argument("--rollout-steps", type=int, default=24)
  parser.add_argument("--plant-augmentations", type=int, default=3)
  parser.add_argument("--max-plant-train-rows", type=int, default=180000)
  parser.add_argument("--max-plant-validation-rows", type=int, default=45000)
  parser.add_argument("--plant-epochs", type=int, default=60)
  parser.add_argument("--plant-patience", type=int, default=10)
  parser.add_argument("--plant-batch-size", type=int, default=2048)
  parser.add_argument("--plant-learning-rate", type=float, default=8e-4)
  parser.add_argument("--policy-epochs", type=int, default=80)
  parser.add_argument("--policy-patience", type=int, default=15)
  parser.add_argument("--policy-steps-per-epoch", type=int, default=40)
  parser.add_argument("--policy-batch-size", type=int, default=256)
  parser.add_argument("--policy-learning-rate", type=float, default=6e-4)
  parser.add_argument("--max-policy-validation-windows", type=int, default=3000)
  parser.add_argument("--policy-stats-rows", type=int, default=250000)
  parser.add_argument("--command-rate-limit", type=float, default=0.06)
  parser.add_argument("--wobble-weight", type=float, default=0.002)
  parser.add_argument("--hidden-sizes", type=int, nargs="+", default=[24, 12, 6])
  parser.add_argument("--reuse-surrogate", action="store_true",
                      help="Reuse the differentiable plant and normalization from output-dir/training.pt.")
  parser.add_argument("--resume-policy", action="store_true",
                      help="Continue from the policy in output-dir/training.pt instead of the initial model.")
  parser.add_argument("--random-state", type=int, default=23)
  args = parser.parse_args()

  torch.manual_seed(args.random_state)
  np.random.seed(args.random_state)
  artifact = load(args.plant_model)
  teacher = artifact["plant_model"]
  metadata = artifact["metadata"]
  history_steps = int(metadata["history_steps"])
  cache = args.output_dir / "cache"
  train_windows = load_or_build_windows(
    cache, metadata, "train_segments", history_steps, args.rollout_steps,
  )
  validation_all = load_or_build_windows(
    cache, metadata, "validation_segments", history_steps, args.rollout_steps,
  )
  validation_windows, holdout_windows = split_validation(metadata, validation_all)
  if not len(validation_windows) or not len(holdout_windows):
    raise SystemExit("Plant manifest must contain random validation routes and forced holdout routes.")
  print(
    f"goal windows: train={len(train_windows)} validation={len(validation_windows)} " +
    f"holdout={len(holdout_windows)}",
  )

  checkpoint_path = args.output_dir / "training.pt"
  if args.reuse_surrogate and checkpoint_path.exists():
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    plant = NeuralPlant(
      train_windows.history.shape[1] * train_windows.history.shape[2],
      len(plant_data.STATE_FEATURES),
    )
    plant.load_state_dict(checkpoint["plant"])
    plant.eval()
    plant_numpy_stats = checkpoint["plant_stats"]
    plant_report = dict(checkpoint["report"]["plant_surrogate"])
    plant_report["reused"] = True
    print(f"reused differentiable plant from {checkpoint_path}")
  else:
    plant, plant_numpy_stats, plant_report = train_surrogate(
      teacher, train_windows, validation_windows, args,
    )
  plant_stats = {
    name: torch.as_tensor(value, dtype=torch.float32)
    for name, value in plant_numpy_stats.items()
  }
  policy_mean_np, policy_std_np = policy_input_stats(
    train_windows, args.policy_stats_rows, args.random_state,
  )
  policy_mean = torch.as_tensor(policy_mean_np, dtype=torch.float32)
  policy_std = torch.as_tensor(policy_std_np, dtype=torch.float32)
  initial_payload = json.loads(args.initial_model.read_text(encoding="utf-8"))
  policy = FluxPolicy(tuple(args.hidden_sizes))
  source_policy = FluxPolicy()
  initialize_policy(source_policy, initial_payload, policy_mean_np, policy_std_np)
  if args.resume_policy and checkpoint_path.exists():
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    source_policy = policy_from_state_dict(checkpoint["policy"])
    print(f"loaded source goal policy from {checkpoint_path}")
  if [(layer.in_features, layer.out_features) for layer in policy.layers] == [
    (layer.in_features, layer.out_features) for layer in source_policy.layers
  ]:
    policy.load_state_dict(source_policy.state_dict())
  else:
    distill_policy(source_policy, policy, args.random_state)
    print(f"distilled source policy into hidden sizes {args.hidden_sizes}")
  validation_report, policy_fit = train_policy(
    policy, plant, plant_stats, policy_mean, policy_std,
    train_windows, validation_windows, args, teacher,
  )
  holdout_report = evaluate_policy(
    policy, plant, plant_stats, policy_mean, policy_std, holdout_windows, args, args.random_state + 3, teacher,
  )

  report: dict[str, Any] = {
    "method": "goal_based_differentiable_vehicle_plant",
    "forward_model": "exact_boosted_tree_with_neural_straight_through_gradients",
    "plant_artifact": str(args.plant_model),
    "initial_model": str(args.initial_model),
    "data": {
      "plant_fit_samples": int(metadata["artifact_fit_samples"]),
      "plant_train_routes": metadata["train_routes"],
      "plant_validation_routes": metadata["validation_routes"],
      "forced_holdout_route_prefixes": metadata.get("forced_holdout_route_prefixes", []),
      "policy_train_windows": len(train_windows),
      "policy_validation_windows": len(validation_windows),
      "policy_holdout_windows": len(holdout_windows),
    },
    "plant_surrogate": plant_report,
    "validation": validation_report,
    "holdout": holdout_report,
    "policy_fit": policy_fit,
    "objective": {
      "tracking": "balanced future planned lateral acceleration",
      "center_extra_weight": 0.20,
      "transition_extra_weight": 1.50,
      "unwind_extra_weight": 0.50,
      "approaching_straight_extra_weight": 0.50,
      "command_slew_weight": 0.006,
      "command_effort_weight": 0.002,
      "saturation_weight": 0.03,
      "wobble_weight": args.wobble_weight,
    },
    "network": {
      "architecture": [18, *args.hidden_sizes, 1],
      "parameters": sum(parameter.numel() for parameter in policy.parameters()),
    },
  }
  payload = export_policy(policy, policy_mean_np, policy_std_np, report)
  roundtrip_inputs = np.random.default_rng(args.random_state).normal(
    policy_mean_np, policy_std_np, size=(128, len(INPUT_VARS)),
  )
  with torch.no_grad():
    torch_prediction = policy(torch.as_tensor(
      (roundtrip_inputs - policy_mean_np) / policy_std_np, dtype=torch.float32,
    )).numpy()[:, 0]
  np.testing.assert_allclose(torch_prediction, flux_predict(payload, roundtrip_inputs), rtol=2e-5, atol=2e-5)

  args.output_dir.mkdir(parents=True, exist_ok=True)
  args.model_output.parent.mkdir(parents=True, exist_ok=True)
  args.model_output.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
  (args.output_dir / "training.json").write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
  )
  torch.save({
    "plant": plant.state_dict(),
    "plant_stats": plant_numpy_stats,
    "policy": policy.state_dict(),
    "policy_mean": policy_mean_np,
    "policy_std": policy_std_np,
    "report": report,
  }, checkpoint_path)
  print(json.dumps({
    "plant_surrogate": plant_report,
    "validation": validation_report,
    "holdout": holdout_report,
    "parameters": report["network"]["parameters"],
  }, indent=2))
  print(f"model: {args.model_output}")
  print(f"report: {args.output_dir / 'training.json'}")


if __name__ == "__main__":
  main()
