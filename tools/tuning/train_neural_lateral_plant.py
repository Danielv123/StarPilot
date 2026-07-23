#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
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
  from joblib import dump, load
  from torch import nn
except ModuleNotFoundError as e:
  raise SystemExit(
    "Run with: uv run --no-project --with torch --with joblib --with scikit-learn " +
    "--with pycapnp==2.1.0 --with zstandard python " +
    "tools/tuning/train_neural_lateral_plant.py"
  ) from e

from openpilot.tools.tuning import train_lateral_plant_model as plant_data
from openpilot.tools.tuning import train_vehicle_response_model as log_data


DEFAULT_CURRENT_ROOT = Path(r"D:\comma_driving_logs\10.30.1.75\realdata")
DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts/tuning/neural_lateral_plant"
FORMAT_VERSION = 1
STATE_INDEXES = tuple(plant_data.BASE_FEATURES.index(name) for name in plant_data.STATE_FEATURES)
DEFAULT_HOLDOUTS = ("00000109", "0000010b")
EVALUATION_BATCH_SIZE = 4096


@dataclass(frozen=True)
class ModelConfig:
  name: str
  family: str
  sample_step: int
  history_steps: int
  hidden_sizes: tuple[int, ...]
  gru_layers: int = 1

  @property
  def sample_period_s(self) -> float:
    return self.sample_step * 0.01

  @property
  def history_s(self) -> float:
    return self.sample_period_s * self.history_steps

  @property
  def input_size(self) -> int:
    return self.history_steps * len(plant_data.BASE_FEATURES)


DEFAULT_CANDIDATES = (
  ModelConfig("baseline_mlp", "mlp", 5, 12, (128, 128, 64)),
  ModelConfig("dense_1s_mlp", "mlp", 2, 50, (256, 256, 128)),
  ModelConfig("dense_1p5s_mlp", "mlp", 2, 75, (512, 256, 128)),
  ModelConfig("dense_2s_large_mlp", "mlp", 2, 100, (512, 512, 256, 128)),
  ModelConfig("dense_1p5s_gru", "gru", 2, 75, (192,), gru_layers=2),
  ModelConfig("dense_2s_gru", "gru", 2, 100, (256,), gru_layers=2),
)


@dataclass
class WindowBatch:
  history: np.ndarray
  future_base: np.ndarray
  target_states: np.ndarray
  routes: list[str]

  def __len__(self) -> int:
    return len(self.history)


class MLPPlant(nn.Module):
  def __init__(self, config: ModelConfig, output_size: int):
    super().__init__()
    sizes = (config.input_size, *config.hidden_sizes, output_size)
    layers: list[nn.Module] = []
    for index, (input_size, output_width) in enumerate(zip(sizes[:-1], sizes[1:], strict=True)):
      layers.append(nn.Linear(input_size, output_width))
      if index < len(sizes) - 2:
        layers.append(nn.SiLU())
    self.network = nn.Sequential(*layers)

  def forward(self, values: torch.Tensor) -> torch.Tensor:
    return self.network(values)


class GRUPlant(nn.Module):
  def __init__(self, config: ModelConfig, output_size: int):
    super().__init__()
    hidden_size = config.hidden_sizes[0]
    self.history_steps = config.history_steps
    self.feature_count = len(plant_data.BASE_FEATURES)
    self.gru = nn.GRU(
      self.feature_count,
      hidden_size,
      num_layers=config.gru_layers,
      batch_first=True,
      dropout=0.10 if config.gru_layers > 1 else 0.0,
    )
    self.head = nn.Sequential(
      nn.Linear(hidden_size, hidden_size),
      nn.SiLU(),
      nn.Linear(hidden_size, output_size),
    )

  def forward(self, values: torch.Tensor) -> torch.Tensor:
    sequence = values.reshape((-1, self.history_steps, self.feature_count)).flip(1)
    encoded, _ = self.gru(sequence)
    return self.head(encoded[:, -1])


def build_model(config: ModelConfig) -> nn.Module:
  if config.family == "mlp":
    return MLPPlant(config, len(plant_data.STATE_FEATURES))
  if config.family == "gru":
    return GRUPlant(config, len(plant_data.STATE_FEATURES))
  raise ValueError(f"Unsupported model family: {config.family}")


def parameter_count(model: nn.Module) -> int:
  return sum(parameter.numel() for parameter in model.parameters())


def load_ensemble_artifact(path: Path, device: torch.device | str = "cpu") -> tuple[
  list[nn.Module], dict[str, torch.Tensor], dict[str, Any]
]:
  payload = torch.load(path, map_location=device, weights_only=False)
  if payload.get("format_version") != FORMAT_VERSION:
    raise ValueError(f"Unsupported neural plant format: {payload.get('format_version')}")
  config = ModelConfig(**payload["config"])
  models: list[nn.Module] = []
  for state in payload["members"]:
    model = build_model(config).to(device)
    model.load_state_dict(state)
    model.eval()
    models.append(model)
  stats = tensor_stats(payload["normalization"], torch.device(device))
  return models, stats, payload


def ensemble_predict_delta(models: list[nn.Module], history: torch.Tensor,
                           stats: dict[str, torch.Tensor]) -> tuple[torch.Tensor, torch.Tensor]:
  member_delta = torch.stack([
    predict_delta(model, history, stats)
    for model in models
  ])
  return member_delta.mean(dim=0), member_delta.std(dim=0, unbiased=False)


def _read_one(payload: tuple[str, str, str]) -> plant_data.Trajectory | None:
  path, brand, fingerprint = payload
  return plant_data.read_trajectory(Path(path), brand, fingerprint, sample_step=1)


def trajectory_inventory(root: Path) -> tuple[list[Path], dict[str, Any]]:
  paths = log_data.discover_log_files(root, "rlog", None, [], None)
  if not paths:
    raise FileNotFoundError(
      f"No rlog files found under {root}. Camera MP4 files do not contain the " +
      "vehicle telemetry required by this trainer."
    )
  total_bytes = sum(path.stat().st_size for path in paths)
  newest_mtime_ns = max(path.stat().st_mtime_ns for path in paths)
  return paths, {
    "root": str(root.resolve()),
    "rlog_count": len(paths),
    "rlog_bytes": total_bytes,
    "newest_mtime_ns": newest_mtime_ns,
  }


def load_trajectories(root: Path, cache_path: Path, workers: int, brand: str,
                      fingerprint: str) -> tuple[list[plant_data.Trajectory], dict[str, Any]]:
  paths, inventory = trajectory_inventory(root)
  cache_key = {**inventory, "brand": brand, "fingerprint": fingerprint, "sample_step": 1}
  if cache_path.is_file():
    cached = load(cache_path)
    if cached.get("cache_key") == cache_key:
      print(f"reused trajectory cache {cache_path}", flush=True)
      return cached["trajectories"], inventory

  started = perf_counter()
  trajectories: list[plant_data.Trajectory] = []
  payloads = [(str(path), brand, fingerprint) for path in paths]
  with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
    futures = {pool.submit(_read_one, payload): payload[0] for payload in payloads}
    for index, future in enumerate(as_completed(futures), 1):
      path = futures[future]
      try:
        trajectory = future.result()
      except Exception as e:
        print(f"skip {path}: {e}", file=sys.stderr)
        continue
      if trajectory is not None:
        trajectories.append(trajectory)
      if index % 25 == 0 or index == len(futures):
        print(f"parsed {index}/{len(futures)} rlogs; usable={len(trajectories)}", flush=True)

  trajectories.sort(key=lambda item: item.segment)
  cache_path.parent.mkdir(parents=True, exist_ok=True)
  dump({"cache_key": cache_key, "trajectories": trajectories}, cache_path, compress=3)
  print(
    f"cached {len(trajectories)} trajectories from " +
    f"{len({item.route for item in trajectories})} routes in " +
    f"{perf_counter() - started:.1f}s",
    flush=True,
  )
  return trajectories, inventory


def split_routes(trajectories: list[plant_data.Trajectory], validation_fraction: float,
                 holdout_prefixes: Iterable[str], seed: int) -> tuple[set[str], set[str], set[str]]:
  routes = sorted({trajectory.route for trajectory in trajectories})
  holdouts = {
    route for route in routes
    if any(route.startswith(prefix) for prefix in holdout_prefixes)
  }
  missing = [
    prefix for prefix in holdout_prefixes
    if not any(route.startswith(prefix) for route in routes)
  ]
  if missing:
    raise ValueError(f"No route matches holdout prefix(es): {', '.join(missing)}")
  remaining = [route for route in routes if route not in holdouts]
  if len(remaining) < 2:
    raise ValueError("Need at least two non-holdout routes.")
  rng = np.random.default_rng(seed)
  shuffled = list(np.asarray(remaining)[rng.permutation(len(remaining))])
  validation_count = max(1, round(len(shuffled) * validation_fraction))
  validation = set(shuffled[:validation_count])
  training = set(shuffled[validation_count:])
  return training, validation, holdouts


def resampled(trajectory: plant_data.Trajectory, sample_step: int) -> tuple[np.ndarray, dict[str, np.ndarray]]:
  indexes = np.arange(0, len(trajectory.times), sample_step)
  return trajectory.times[indexes], {
    name: values[indexes]
    for name, values in trajectory.values.items()
  }


def eligible_sources(trajectory: plant_data.Trajectory, config: ModelConfig,
                     rollout_steps: int) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray]]:
  times, values = resampled(trajectory, config.sample_step)
  lower = config.history_steps - 1
  upper = len(times) - rollout_steps - 1
  if upper <= lower:
    return np.empty(0, dtype=np.int64), times, values
  source = np.arange(lower, upper, dtype=np.int64)
  future = source[:, None] + np.arange(rollout_steps + 1)
  clean = (
    (values["lat_active"][future].min(axis=1) > 0.5)
    & (values["driver_overlay"][future].max(axis=1) < 0.5)
    & (values["saturated"][future].max(axis=1) < 0.5)
    & (values["v_ego"][source] >= 3.0)
  )
  gaps = np.diff(times)
  max_gap = max(0.035, config.sample_period_s * 1.8)
  bad_prefix = np.concatenate(([0], np.cumsum(gaps > max_gap)))
  starts = source - config.history_steps + 1
  ends = source + rollout_steps
  clean &= (bad_prefix[ends] - bad_prefix[starts]) == 0
  return source[clean], times, values


def eligible_routes(trajectories: list[plant_data.Trajectory], config: ModelConfig,
                    rollout_steps: int) -> set[str]:
  return {
    trajectory.route
    for trajectory in trajectories
    if len(eligible_sources(trajectory, config, rollout_steps)[0])
  }


def build_windows(trajectories: list[plant_data.Trajectory], routes: set[str],
                  config: ModelConfig, rollout_steps: int, cap: int | None,
                  seed: int) -> WindowBatch:
  candidates: list[tuple[int, int]] = []
  prepared: dict[int, tuple[np.ndarray, dict[str, np.ndarray]]] = {}
  for trajectory_index, trajectory in enumerate(trajectories):
    if trajectory.route not in routes:
      continue
    sources, times, values = eligible_sources(trajectory, config, rollout_steps)
    if len(sources):
      prepared[trajectory_index] = (times, values)
      candidates.extend((trajectory_index, int(source)) for source in sources)
  if not candidates:
    raise ValueError(f"No clean windows for {config.name} across {len(routes)} routes.")

  if cap is not None and len(candidates) > cap:
    rng = np.random.default_rng(seed)
    selected = np.sort(rng.choice(len(candidates), cap, replace=False))
    candidates = [candidates[index] for index in selected]

  feature_names = list(plant_data.BASE_FEATURES)
  state_names = list(plant_data.STATE_FEATURES)
  history = np.empty(
    (len(candidates), config.history_steps, len(feature_names)),
    dtype=np.float32,
  )
  future_base = np.empty(
    (len(candidates), rollout_steps, len(feature_names)),
    dtype=np.float32,
  )
  target_states = np.empty(
    (len(candidates), rollout_steps, len(state_names)),
    dtype=np.float32,
  )
  route_names: list[str] = []
  history_offsets = np.arange(config.history_steps)
  future_offsets = np.arange(1, rollout_steps + 1)
  for row, (trajectory_index, source) in enumerate(candidates):
    trajectory = trajectories[trajectory_index]
    _, values = prepared[trajectory_index]
    history_indexes = source - history_offsets
    future_indexes = source + future_offsets
    history[row] = np.column_stack([values[name][history_indexes] for name in feature_names])
    future_base[row] = np.column_stack([values[name][future_indexes] for name in feature_names])
    target_states[row] = np.stack([values[name][future_indexes] for name in state_names], axis=1)
    route_names.append(trajectory.route)
  finite = (
    np.isfinite(history).all(axis=(1, 2))
    & np.isfinite(future_base).all(axis=(1, 2))
    & np.isfinite(target_states).all(axis=(1, 2))
  )
  return WindowBatch(history[finite], future_base[finite], target_states[finite],
                     [route for route, keep in zip(route_names, finite, strict=True) if keep])


def normalization(windows: WindowBatch) -> dict[str, np.ndarray]:
  flat = windows.history.reshape((len(windows), -1))
  x_mean = flat.mean(axis=0, dtype=np.float64).astype(np.float32)
  x_std = flat.std(axis=0, dtype=np.float64).astype(np.float32)
  x_std[x_std < 1e-5] = 1.0
  current_state = windows.history[:, 0, STATE_INDEXES]
  delta = windows.target_states[:, 0] - current_state
  y_mean = delta.mean(axis=0, dtype=np.float64).astype(np.float32)
  y_std = delta.std(axis=0, dtype=np.float64).astype(np.float32)
  y_std[y_std < 1e-6] = 1.0
  state_std = windows.target_states.reshape((-1, len(plant_data.STATE_FEATURES))).std(
    axis=0, dtype=np.float64,
  ).astype(np.float32)
  state_std[state_std < 1e-5] = 1.0
  return {
    "x_mean": x_mean,
    "x_std": x_std,
    "y_mean": y_mean,
    "y_std": y_std,
    "state_std": state_std,
  }


def tensor_stats(stats: dict[str, np.ndarray], device: torch.device) -> dict[str, torch.Tensor]:
  return {
    name: torch.as_tensor(value, dtype=torch.float32, device=device)
    for name, value in stats.items()
  }


def predict_delta(model: nn.Module, history: torch.Tensor,
                  stats: dict[str, torch.Tensor]) -> torch.Tensor:
  flat = history.flatten(1)
  normalized = (flat - stats["x_mean"]) / stats["x_std"]
  return model(normalized) * stats["y_std"] + stats["y_mean"]


def rollout(model: nn.Module, history: torch.Tensor, future_base: torch.Tensor,
            stats: dict[str, torch.Tensor], steps: int) -> torch.Tensor:
  predictions: list[torch.Tensor] = []
  state_indexes = torch.as_tensor(STATE_INDEXES, device=history.device)
  for step in range(steps):
    delta = predict_delta(model, history, stats)
    next_state = history[:, 0, state_indexes] + delta
    predictions.append(next_state)
    next_base = future_base[:, step].clone()
    next_base[:, state_indexes] = next_state
    history = torch.cat((next_base[:, None], history[:, :-1]), dim=1)
  return torch.stack(predictions, dim=1)


def ensemble_rollout(models: list[nn.Module], history: torch.Tensor,
                     future_base: torch.Tensor, stats: dict[str, torch.Tensor],
                     steps: int) -> tuple[torch.Tensor, torch.Tensor]:
  member_predictions = torch.stack([
    rollout(model, history.clone(), future_base, stats, steps)
    for model in models
  ])
  return (
    member_predictions.mean(dim=0),
    member_predictions.std(dim=0, unbiased=False),
  )


def train_member(config: ModelConfig, train_windows: WindowBatch,
                 validation_windows: WindowBatch, stats: dict[str, np.ndarray],
                 seed: int, epochs: int, patience: int, batch_size: int,
                 learning_rate: float, rollout_train_steps: int,
                 steps_per_epoch: int, device: torch.device,
                 initial_state: dict[str, torch.Tensor] | None = None) -> tuple[nn.Module, dict[str, Any]]:
  torch.manual_seed(seed)
  model = build_model(config).to(device)
  if initial_state is not None:
    model.load_state_dict(initial_state)
  optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=2e-5)
  stats_t = tensor_stats(stats, device)
  rng = np.random.default_rng(seed)
  best_state = copy.deepcopy(model.state_dict())
  best_loss = math.inf
  stale_epochs = 0
  history_report: list[dict[str, float]] = []
  started = perf_counter()

  validation_limit = min(len(validation_windows), 5000)
  validation_indexes = np.arange(validation_limit)
  val_history = torch.as_tensor(validation_windows.history[validation_indexes], device=device)
  val_future = torch.as_tensor(validation_windows.future_base[validation_indexes], device=device)
  val_targets = torch.as_tensor(validation_windows.target_states[validation_indexes], device=device)
  validation_steps = min(rollout_train_steps, val_targets.shape[1])

  for epoch in range(1, epochs + 1):
    model.train()
    losses: list[float] = []
    for _ in range(steps_per_epoch):
      indexes = rng.integers(0, len(train_windows), size=min(batch_size, len(train_windows)))
      batch_history = torch.as_tensor(train_windows.history[indexes], device=device)
      batch_future = torch.as_tensor(train_windows.future_base[indexes], device=device)
      batch_targets = torch.as_tensor(train_windows.target_states[indexes], device=device)
      steps = min(rollout_train_steps, batch_targets.shape[1])
      predicted = rollout(model, batch_history, batch_future, stats_t, steps)
      scaled_error = (predicted - batch_targets[:, :steps]) / stats_t["state_std"]
      step_weights = torch.linspace(1.0, 0.5, steps, device=device)
      loss = (
        F.smooth_l1_loss(scaled_error, torch.zeros_like(scaled_error), beta=0.20, reduction="none")
        .mean(dim=(0, 2))
        .mul(step_weights)
        .sum()
        / step_weights.sum()
      )
      optimizer.zero_grad(set_to_none=True)
      loss.backward()
      torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
      optimizer.step()
      losses.append(float(loss.detach().cpu()))

    model.eval()
    with torch.no_grad():
      validation_prediction = rollout(
        model, val_history, val_future, stats_t, validation_steps,
      )
      validation_error = (
        validation_prediction - val_targets[:, :validation_steps]
      ) / stats_t["state_std"]
      validation_loss = float(torch.mean(validation_error ** 2).cpu())
    train_loss = float(np.mean(losses))
    history_report.append({"epoch": epoch, "train_loss": train_loss, "validation_loss": validation_loss})
    print(
      f"{config.name} seed={seed} epoch={epoch:03d} " +
      f"train={train_loss:.6f} validation={validation_loss:.6f}",
      flush=True,
    )
    if validation_loss < best_loss - 1e-6:
      best_loss = validation_loss
      best_state = copy.deepcopy(model.state_dict())
      stale_epochs = 0
    else:
      stale_epochs += 1
      if stale_epochs >= patience:
        break

  model.load_state_dict(best_state)
  model.eval()
  return model, {
    "seed": seed,
    "epochs": len(history_report),
    "best_validation_loss": best_loss,
    "fit_seconds": perf_counter() - started,
    "rollout_train_steps": validation_steps,
    "rollout_train_seconds": validation_steps * config.sample_period_s,
    "history": history_report,
  }


def evaluate_model(model: nn.Module, windows: WindowBatch, stats: dict[str, np.ndarray],
                   config: ModelConfig, max_windows: int | None, device: torch.device,
                   seed: int) -> dict[str, Any]:
  rng = np.random.default_rng(seed)
  indexes = np.arange(len(windows))
  if max_windows is not None and len(indexes) > max_windows:
    indexes = np.sort(rng.choice(indexes, max_windows, replace=False))
  targets = windows.target_states[indexes]
  stats_t = tensor_stats(stats, device)
  predictions: list[np.ndarray] = []
  with torch.no_grad():
    for start in range(0, len(indexes), EVALUATION_BATCH_SIZE):
      batch_indexes = indexes[start:start + EVALUATION_BATCH_SIZE]
      history = torch.as_tensor(windows.history[batch_indexes], device=device)
      future = torch.as_tensor(windows.future_base[batch_indexes], device=device)
      predictions.append(
        rollout(model, history, future, stats_t, targets.shape[1]).cpu().numpy(),
      )
      print(
        f"{config.name} evaluation={min(start + EVALUATION_BATCH_SIZE, len(indexes))}/{len(indexes)}",
        flush=True,
      )
  prediction = np.concatenate(predictions)
  horizons: dict[str, Any] = {}
  requested_seconds = (0.10, 0.25, 0.50, 1.00, 2.00)
  selected_steps = sorted({
    min(targets.shape[1], max(1, round(seconds / config.sample_period_s)))
    for seconds in requested_seconds
  })
  for step in selected_steps:
    error = prediction[:, step - 1] - targets[:, step - 1]
    horizons[f"{step * config.sample_period_s:.2f}s"] = {
      name: {
        "rmse": float(np.sqrt(np.mean(error[:, index] ** 2))),
        "mae": float(np.mean(np.abs(error[:, index]))),
        "p95_abs_error": float(np.percentile(np.abs(error[:, index]), 95)),
      }
      for index, name in enumerate(plant_data.STATE_FEATURES)
    }
  normalized = (prediction - targets) / stats["state_std"]
  score_weights = np.asarray([3.0, 1.0, 1.5, 0.5], dtype=np.float32)
  normalized_rmse = np.sqrt(np.mean(normalized ** 2, axis=(0, 1)))
  score = float(np.average(normalized_rmse, weights=score_weights))
  return {
    "windows": len(indexes),
    "score": score,
    "normalized_rmse": {
      name: float(normalized_rmse[index])
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "horizons": horizons,
  }


def evaluate_ensemble(models: list[nn.Module], windows: WindowBatch,
                      stats: dict[str, np.ndarray], config: ModelConfig,
                      max_windows: int | None, device: torch.device, seed: int) -> dict[str, Any]:
  rng = np.random.default_rng(seed)
  indexes = np.arange(len(windows))
  if max_windows is not None and len(indexes) > max_windows:
    indexes = np.sort(rng.choice(indexes, max_windows, replace=False))
  targets = windows.target_states[indexes]
  stats_t = tensor_stats(stats, device)
  prediction_batches: list[np.ndarray] = []
  disagreement_batches: list[np.ndarray] = []
  with torch.no_grad():
    for start in range(0, len(indexes), EVALUATION_BATCH_SIZE):
      batch_indexes = indexes[start:start + EVALUATION_BATCH_SIZE]
      history = torch.as_tensor(windows.history[batch_indexes], device=device)
      future = torch.as_tensor(windows.future_base[batch_indexes], device=device)
      prediction, disagreement = ensemble_rollout(
        models, history, future, stats_t, targets.shape[1],
      )
      prediction_batches.append(prediction.cpu().numpy())
      disagreement_batches.append(disagreement.cpu().numpy())
      print(
        f"{config.name} ensemble_evaluation=" +
        f"{min(start + EVALUATION_BATCH_SIZE, len(indexes))}/{len(indexes)}",
        flush=True,
      )
  prediction = np.concatenate(prediction_batches)
  disagreement = np.concatenate(disagreement_batches)
  normalized = (prediction - targets) / stats["state_std"]
  normalized_rmse = np.sqrt(np.mean(normalized ** 2, axis=(0, 1)))
  normalized_disagreement = disagreement / stats["state_std"]
  score_weights = np.asarray([3.0, 1.0, 1.5, 0.5], dtype=np.float32)
  return {
    "windows": len(indexes),
    "score": float(np.average(normalized_rmse, weights=score_weights)),
    "normalized_rmse": {
      name: float(normalized_rmse[index])
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "mean_normalized_disagreement": {
      name: float(np.mean(normalized_disagreement[..., index]))
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "p95_normalized_disagreement": {
      name: float(np.percentile(normalized_disagreement[..., index], 95))
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
  }


def state_dict_cpu(model: nn.Module) -> dict[str, torch.Tensor]:
  return {name: value.detach().cpu() for name, value in model.state_dict().items()}


def parse_hidden_sizes(value: str) -> tuple[int, ...]:
  result = tuple(int(item) for item in value.split(",") if item.strip())
  if not result or any(item < 1 for item in result):
    raise argparse.ArgumentTypeError("hidden sizes must be comma-separated positive integers")
  return result


def config_from_args(args: argparse.Namespace) -> ModelConfig:
  return ModelConfig(
    name=args.name,
    family=args.family,
    sample_step=args.sample_step,
    history_steps=args.history_steps,
    hidden_sizes=args.hidden_sizes,
    gru_layers=args.gru_layers,
  )


def candidate_report(config: ModelConfig, model: nn.Module, fit: dict[str, Any],
                     validation: dict[str, Any]) -> dict[str, Any]:
  return {
    "config": asdict(config),
    "history_seconds": config.history_s,
    "sample_period_s": config.sample_period_s,
    "parameters": parameter_count(model),
    "fit": fit,
    "validation": validation,
  }


def common_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(add_help=False)
  parser.add_argument("--current-root", type=Path, default=DEFAULT_CURRENT_ROOT)
  parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
  parser.add_argument("--trajectory-cache", type=Path)
  parser.add_argument("--workers", type=int, default=max(1, min(16, os.cpu_count() or 1)))
  parser.add_argument("--brand", default="hyundai")
  parser.add_argument("--car-fingerprint-contains", default="IONIQ5")
  parser.add_argument("--validation-fraction", type=float, default=0.15)
  parser.add_argument("--holdout-route-prefix", action="append")
  parser.add_argument("--max-train-windows", type=int, default=300000)
  parser.add_argument("--max-validation-windows", type=int, default=60000)
  parser.add_argument("--max-holdout-windows", type=int, default=30000)
  parser.add_argument("--rollout-seconds", type=float, default=2.0)
  parser.add_argument("--rollout-train-seconds", type=float, default=0.5)
  parser.add_argument("--epochs", type=int, default=35)
  parser.add_argument("--patience", type=int, default=7)
  parser.add_argument("--batch-size", type=int, default=512)
  parser.add_argument("--steps-per-epoch", type=int, default=120)
  parser.add_argument("--learning-rate", type=float, default=6e-4)
  parser.add_argument("--random-state", type=int, default=23)
  parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
  return parser


def load_current(args: argparse.Namespace) -> tuple[list[plant_data.Trajectory], dict[str, Any]]:
  cache_path = args.trajectory_cache or args.output_dir / "current_trajectories.joblib"
  return load_trajectories(
    args.current_root, cache_path, args.workers, args.brand, args.car_fingerprint_contains,
  )


def route_holdouts(args: argparse.Namespace) -> tuple[str, ...]:
  return tuple(args.holdout_route_prefix or DEFAULT_HOLDOUTS)


def run_search(args: argparse.Namespace) -> None:
  trajectories, inventory = load_current(args)
  candidate_routes = [
    eligible_routes(
      trajectories,
      candidate,
      max(1, round(args.rollout_seconds / candidate.sample_period_s)),
    )
    for candidate in DEFAULT_CANDIDATES
  ]
  common_routes = set.intersection(*candidate_routes)
  split_trajectories = [
    trajectory for trajectory in trajectories
    if trajectory.route in common_routes
  ]
  train_routes, validation_routes, holdout_routes = split_routes(
    split_trajectories, args.validation_fraction, route_holdouts(args), args.random_state,
  )
  device = torch.device(args.device)
  results: list[dict[str, Any]] = []
  for candidate in DEFAULT_CANDIDATES:
    rollout_steps = max(1, round(args.rollout_seconds / candidate.sample_period_s))
    train_windows = build_windows(
      trajectories, train_routes, candidate, rollout_steps,
      args.max_train_windows, args.random_state,
    )
    validation_windows = build_windows(
      trajectories, validation_routes, candidate, rollout_steps,
      args.max_validation_windows, args.random_state + 1,
    )
    stats = normalization(train_windows)
    rollout_train_steps = max(
      1, round(args.rollout_train_seconds / candidate.sample_period_s),
    )
    model, fit = train_member(
      candidate, train_windows, validation_windows, stats,
      args.random_state, args.epochs, args.patience, args.batch_size,
      args.learning_rate, rollout_train_steps, args.steps_per_epoch, device,
    )
    validation = evaluate_model(
      model, validation_windows, stats, candidate,
      args.max_validation_windows, device, args.random_state + 2,
    )
    result = candidate_report(candidate, model, fit, validation)
    results.append(result)
    print(
      f"candidate {candidate.name}: parameters={result['parameters']} " +
      f"validation_score={validation['score']:.6f}",
      flush=True,
    )
    del model, train_windows, validation_windows
    if device.type == "cuda":
      torch.cuda.empty_cache()

  results.sort(key=lambda item: item["validation"]["score"])
  report = {
    "format_version": FORMAT_VERSION,
    "data": {
      "current": inventory,
      "pretraining_performed": False,
      "pretraining_note": (
        "No older telemetry was available. The supplied Pond archive contained " +
        "camera MP4 files only, so all candidates used current-tire rlogs."
      ),
      "train_routes": sorted(train_routes),
      "validation_routes": sorted(validation_routes),
      "holdout_routes": sorted(holdout_routes),
      "eligible_route_count": len(common_routes),
      "excluded_route_count": len({item.route for item in trajectories} - common_routes),
    },
    "selection_metric": "weighted normalized autoregressive rollout RMSE",
    "candidates": results,
    "selected": results[0]["config"],
  }
  args.output_dir.mkdir(parents=True, exist_ok=True)
  path = args.output_dir / "architecture_search.json"
  path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(f"selected {results[0]['config']['name']}", flush=True)
  print(f"report: {path}", flush=True)


def run_train(args: argparse.Namespace) -> None:
  trajectories, inventory = load_current(args)
  config = config_from_args(args)
  rollout_steps = max(1, round(args.rollout_seconds / config.sample_period_s))
  usable_routes = eligible_routes(trajectories, config, rollout_steps)
  split_trajectories = [
    trajectory for trajectory in trajectories
    if trajectory.route in usable_routes
  ]
  train_routes, validation_routes, holdout_routes = split_routes(
    split_trajectories, args.validation_fraction, route_holdouts(args), args.random_state,
  )
  train_windows = build_windows(
    trajectories, train_routes, config, rollout_steps,
    args.max_train_windows, args.random_state,
  )
  validation_windows = build_windows(
    trajectories, validation_routes, config, rollout_steps,
    args.max_validation_windows, args.random_state + 1,
  )
  holdout_windows = build_windows(
    trajectories, holdout_routes, config, rollout_steps,
    args.max_holdout_windows, args.random_state + 2,
  )
  stats = normalization(train_windows)
  device = torch.device(args.device)
  rollout_train_steps = max(
    1, round(args.rollout_train_seconds / config.sample_period_s),
  )
  models: list[nn.Module] = []
  member_reports: list[dict[str, Any]] = []
  ensemble_seeds = args.ensemble_seed or [23, 41, 71]
  for seed in ensemble_seeds:
    model, fit = train_member(
      config, train_windows, validation_windows, stats,
      seed, args.epochs, args.patience, args.batch_size,
      args.learning_rate, rollout_train_steps, args.steps_per_epoch, device,
    )
    validation = evaluate_model(
      model, validation_windows, stats, config,
      args.max_validation_windows, device, seed + 100,
    )
    member_reports.append(candidate_report(config, model, fit, validation))
    models.append(model)

  ensemble_validation = evaluate_ensemble(
    models, validation_windows, stats, config,
    args.max_validation_windows, device, args.random_state + 200,
  )
  ensemble_holdout = evaluate_ensemble(
    models, holdout_windows, stats, config,
    args.max_holdout_windows, device, args.random_state + 201,
  )
  artifact = {
    "format_version": FORMAT_VERSION,
    "model_type": "neural_controller_independent_lateral_plant_ensemble",
    "config": asdict(config),
    "feature_names": list(plant_data.BASE_FEATURES),
    "state_feature_names": list(plant_data.STATE_FEATURES),
    "normalization": stats,
    "members": [state_dict_cpu(model) for model in models],
    "metadata": {
      "current_data": inventory,
      "pretraining_performed": False,
      "pretraining_note": (
        "No older telemetry was available. The supplied Pond archive contained " +
        "camera MP4 files only; this ensemble was trained solely on current-tire rlogs."
      ),
      "train_routes": sorted(train_routes),
      "validation_routes": sorted(validation_routes),
      "holdout_routes": sorted(holdout_routes),
      "eligible_route_count": len(usable_routes),
      "excluded_route_count": len({item.route for item in trajectories} - usable_routes),
      "train_windows": len(train_windows),
      "validation_windows": len(validation_windows),
      "holdout_windows": len(holdout_windows),
      "member_reports": member_reports,
      "ensemble_validation": ensemble_validation,
      "ensemble_holdout": ensemble_holdout,
      "parameters_per_member": parameter_count(models[0]),
      "ensemble_parameters": sum(parameter_count(model) for model in models),
      "anti_exploitation": {
        "strategy": "ensemble mean with member disagreement exposed to downstream policy training",
        "selection_holdout": sorted(holdout_routes),
        "rollout_training_seconds": args.rollout_train_seconds,
        "rollout_training_steps": rollout_train_steps,
        "rollout_validation_seconds": args.rollout_seconds,
      },
    },
  }
  args.output_dir.mkdir(parents=True, exist_ok=True)
  model_path = args.output_dir / "neural_lateral_plant.pt"
  report_path = args.output_dir / "training.json"
  torch.save(artifact, model_path)
  report = dict(artifact["metadata"])
  report.update({
    "format_version": FORMAT_VERSION,
    "model_type": artifact["model_type"],
    "config": artifact["config"],
    "feature_names": artifact["feature_names"],
    "state_feature_names": artifact["state_feature_names"],
  })
  report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(json.dumps({
    "config": artifact["config"],
    "parameters_per_member": artifact["metadata"]["parameters_per_member"],
    "ensemble_parameters": artifact["metadata"]["ensemble_parameters"],
    "validation": ensemble_validation,
    "holdout": ensemble_holdout,
  }, indent=2), flush=True)
  print(f"model: {model_path}", flush=True)
  print(f"report: {report_path}", flush=True)


def main() -> None:
  parser = argparse.ArgumentParser(description="Search and train a neural Ioniq 5 lateral vehicle plant.")
  subparsers = parser.add_subparsers(dest="command", required=True)
  search = subparsers.add_parser("search", parents=[common_parser()])
  search.set_defaults(func=run_search)
  train = subparsers.add_parser("train", parents=[common_parser()])
  train.add_argument("--name", default="selected_neural_plant")
  train.add_argument("--family", choices=("mlp", "gru"), default="gru")
  train.add_argument("--sample-step", type=int, default=2)
  train.add_argument("--history-steps", type=int, default=100)
  train.add_argument("--hidden-sizes", type=parse_hidden_sizes, default=(256,))
  train.add_argument("--gru-layers", type=int, default=2)
  train.add_argument("--ensemble-seed", type=int, action="append")
  train.set_defaults(func=run_train)
  args = parser.parse_args()
  args.max_train_windows = None if args.max_train_windows == 0 else args.max_train_windows
  args.max_validation_windows = None if args.max_validation_windows == 0 else args.max_validation_windows
  args.max_holdout_windows = None if args.max_holdout_windows == 0 else args.max_holdout_windows
  args.func(args)


if __name__ == "__main__":
  main()
