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
SIGNED_STEERING_RATE_INDEX = plant_data.BASE_FEATURES.index("signed_steering_rate_deg_s")
STEERING_RATE_INDEX = plant_data.BASE_FEATURES.index("steering_rate_deg")
SIGNED_STEERING_RATE_STATE_INDEX = plant_data.STATE_FEATURES.index("signed_steering_rate_deg_s")
V_EGO_INDEX = plant_data.BASE_FEATURES.index("v_ego")
DEFAULT_HOLDOUTS = ("00000109", "0000010b")
EVALUATION_BATCH_SIZE = 4096
SEQUENCE_EVALUATION_BATCH_SIZE = 512
SPEED_BUCKETS_MPS = ((0.5, 3.0), (3.0, 5.0), (5.0, 8.0), (8.0, 15.0), (15.0, math.inf))
LOW_SPEED_STATE_WEIGHTS = (1.0, 3.0, 2.0, 2.0)
HIGH_SPEED_STATE_WEIGHTS = (3.0, 1.0, 1.5, 0.5)


@dataclass(frozen=True)
class ModelConfig:
  name: str
  family: str
  sample_step: int
  history_steps: int
  hidden_sizes: tuple[int, ...]
  gru_layers: int = 1
  temporal_layers: int = 2
  attention_heads: int = 4
  feedforward_size: int = 256
  dropout: float = 0.10

  def __post_init__(self) -> None:
    if self.family == "gru" and len(self.hidden_sizes) != 1:
      raise ValueError("GRU configurations require exactly one hidden size.")

  @property
  def sample_period_s(self) -> float:
    return self.sample_step * 0.01

  @property
  def history_s(self) -> float:
    return self.sample_period_s * self.history_steps

  @property
  def input_size(self) -> int:
    return self.history_steps * len(plant_data.BASE_FEATURES)


@dataclass(frozen=True)
class ResidualGateConfig:
  low_speed_full_below_mps: float = 3.0
  low_speed_off_above_mps: float = 5.0
  maneuver_angle_on_deg: float = 2.0
  maneuver_angle_full_deg: float = 8.0
  maneuver_rate_on_deg_s: float = 5.0
  maneuver_rate_full_deg_s: float = 25.0
  maneuver_speed_on_below_mps: float = 5.0
  maneuver_speed_full_below_mps: float = 8.0
  maneuver_speed_full_above_mps: float = 15.0
  maneuver_speed_off_above_mps: float = 20.0
  residual_max_normalized_delta: float = 0.5
  low_speed_state_mask: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0)
  maneuver_state_mask: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0)

  def __post_init__(self) -> None:
    if self.low_speed_off_above_mps <= self.low_speed_full_below_mps:
      raise ValueError("Low-speed gate thresholds must increase.")
    if self.maneuver_angle_full_deg <= self.maneuver_angle_on_deg:
      raise ValueError("Maneuver angle thresholds must increase.")
    if self.maneuver_rate_full_deg_s <= self.maneuver_rate_on_deg_s:
      raise ValueError("Maneuver rate thresholds must increase.")
    speed_thresholds = (
      self.maneuver_speed_on_below_mps,
      self.maneuver_speed_full_below_mps,
      self.maneuver_speed_full_above_mps,
      self.maneuver_speed_off_above_mps,
    )
    if any(
      right <= left
      for left, right in zip(speed_thresholds, speed_thresholds[1:], strict=False)
    ):
      raise ValueError("Maneuver speed thresholds must increase.")
    expected_states = len(plant_data.STATE_FEATURES)
    if len(self.low_speed_state_mask) != expected_states:
      raise ValueError("Low-speed state mask has the wrong length.")
    if len(self.maneuver_state_mask) != expected_states:
      raise ValueError("Maneuver state mask has the wrong length.")
    if self.residual_max_normalized_delta <= 0.0:
      raise ValueError("Residual output bound must be positive.")


INITIAL_CANDIDATES = (
  ModelConfig("baseline_mlp", "mlp", 5, 12, (128, 128, 64)),
  ModelConfig("dense_1s_mlp", "mlp", 2, 50, (256, 256, 128)),
  ModelConfig("dense_1p5s_mlp", "mlp", 2, 75, (512, 256, 128)),
  ModelConfig("dense_2s_large_mlp", "mlp", 2, 100, (512, 512, 256, 128)),
  ModelConfig("dense_1p5s_gru", "gru", 2, 75, (192,), gru_layers=2),
  ModelConfig("dense_2s_gru", "gru", 2, 100, (256,), gru_layers=2),
)


def temporal_candidates() -> tuple[ModelConfig, ...]:
  candidates: list[ModelConfig] = []
  for sample_step in (1, 2, 5):
    for history_seconds in (0.5, 1.0, 1.5, 2.0, 3.0):
      history_steps = round(history_seconds / (sample_step * 0.01))
      name = f"temporal_{sample_step * 10}ms_{str(history_seconds).replace('.', 'p')}s_gru"
      candidates.append(ModelConfig(
        name,
        "gru",
        sample_step,
        history_steps,
        (96,),
        gru_layers=1,
        dropout=0.0,
      ))
  return tuple(candidates)


TEMPORAL_CANDIDATES = temporal_candidates()


ARCHITECTURE_CANDIDATES = (
  ModelConfig("architecture_previous_gru_50hz_1p5s", "gru", 2, 75, (192,), gru_layers=2),
  ModelConfig("architecture_temporal_gru_100hz_2s", "gru", 1, 200, (96,), gru_layers=1, dropout=0.0),
  ModelConfig("architecture_gru_100hz_2s", "gru", 1, 200, (192,), gru_layers=2),
  ModelConfig("architecture_large_gru_100hz_2s", "gru", 1, 200, (384,), gru_layers=3),
  ModelConfig("architecture_tcn_100hz_2s", "tcn", 1, 200, (128,), temporal_layers=7),
  ModelConfig("architecture_large_tcn_100hz_2s", "tcn", 1, 200, (192,), temporal_layers=7),
  ModelConfig(
    "architecture_small_transformer_100hz_2s",
    "transformer",
    1,
    200,
    (64,),
    temporal_layers=2,
    attention_heads=4,
    feedforward_size=256,
  ),
  ModelConfig(
    "architecture_transformer_100hz_2s",
    "transformer",
    1,
    200,
    (128,),
    temporal_layers=4,
    attention_heads=8,
    feedforward_size=512,
  ),
  ModelConfig(
    "architecture_large_transformer_100hz_2s",
    "transformer",
    1,
    200,
    (192,),
    temporal_layers=4,
    attention_heads=8,
    feedforward_size=768,
  ),
  ModelConfig("architecture_mlp_100hz_2s", "mlp", 1, 200, (512, 512, 256)),
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
        if config.dropout > 0.0:
          layers.append(nn.Dropout(config.dropout))
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
      dropout=config.dropout if config.gru_layers > 1 else 0.0,
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


class TemporalConvBlock(nn.Module):
  def __init__(self, channels: int, dilation: int, dropout: float):
    super().__init__()
    self.network = nn.Sequential(
      nn.Conv1d(channels, channels, kernel_size=3, padding=dilation, dilation=dilation),
      nn.GELU(),
      nn.Dropout(dropout),
      nn.Conv1d(channels, channels, kernel_size=3, padding=dilation, dilation=dilation),
      nn.GELU(),
      nn.Dropout(dropout),
    )

  def forward(self, values: torch.Tensor) -> torch.Tensor:
    return values + self.network(values)


class TCNPlant(nn.Module):
  def __init__(self, config: ModelConfig, output_size: int):
    super().__init__()
    channels = config.hidden_sizes[0]
    self.history_steps = config.history_steps
    self.feature_count = len(plant_data.BASE_FEATURES)
    self.input_projection = nn.Conv1d(self.feature_count, channels, kernel_size=1)
    self.blocks = nn.Sequential(*[
      TemporalConvBlock(channels, 2 ** layer, config.dropout)
      for layer in range(config.temporal_layers)
    ])
    self.output_norm = nn.LayerNorm(channels)
    self.head = nn.Sequential(
      nn.Linear(channels, channels),
      nn.SiLU(),
      nn.Linear(channels, output_size),
    )

  def forward(self, values: torch.Tensor) -> torch.Tensor:
    sequence = values.reshape((-1, self.history_steps, self.feature_count)).flip(1)
    encoded = self.blocks(self.input_projection(sequence.transpose(1, 2)))
    return self.head(self.output_norm(encoded[:, :, -1]))


class TransformerPlant(nn.Module):
  def __init__(self, config: ModelConfig, output_size: int):
    super().__init__()
    model_width = config.hidden_sizes[0]
    if model_width % config.attention_heads:
      raise ValueError("Transformer width must be divisible by attention heads.")
    self.history_steps = config.history_steps
    self.feature_count = len(plant_data.BASE_FEATURES)
    self.input_projection = nn.Linear(self.feature_count, model_width)
    self.position_embedding = nn.Parameter(torch.empty(1, config.history_steps, model_width))
    nn.init.normal_(self.position_embedding, mean=0.0, std=0.02)
    encoder_layer = nn.TransformerEncoderLayer(
      d_model=model_width,
      nhead=config.attention_heads,
      dim_feedforward=config.feedforward_size,
      dropout=config.dropout,
      activation="gelu",
      batch_first=True,
      norm_first=True,
    )
    self.encoder = nn.TransformerEncoder(
      encoder_layer,
      num_layers=config.temporal_layers,
      enable_nested_tensor=False,
    )
    self.output_norm = nn.LayerNorm(model_width)
    self.head = nn.Sequential(
      nn.Linear(model_width, model_width),
      nn.SiLU(),
      nn.Linear(model_width, output_size),
    )

  def forward(self, values: torch.Tensor) -> torch.Tensor:
    sequence = values.reshape((-1, self.history_steps, self.feature_count)).flip(1)
    encoded = self.encoder(self.input_projection(sequence) + self.position_embedding)
    return self.head(self.output_norm(encoded[:, -1]))


class CompactResidualPlant(nn.Module):
  def __init__(self, config: ModelConfig, output_size: int):
    super().__init__()
    candidate_lags = (0, 1, 5, 10, 25, 50, 100, 200, config.history_steps - 1)
    self.history_steps = config.history_steps
    self.feature_count = len(plant_data.BASE_FEATURES)
    self.lags = tuple(sorted({
      min(config.history_steps - 1, max(0, lag))
      for lag in candidate_lags
    }))
    input_size = len(self.lags) * self.feature_count
    self.network = nn.Sequential(
      nn.Linear(input_size, 64),
      nn.SiLU(),
      nn.Linear(64, output_size),
    )

  def forward(self, values: torch.Tensor) -> torch.Tensor:
    sequence = values.reshape((-1, self.history_steps, self.feature_count))
    return self.network(sequence[:, self.lags].flatten(1))


class SpeedGatedResidualPlant(nn.Module):
  def __init__(self, config: ModelConfig, gate: ResidualGateConfig):
    super().__init__()
    self.base = build_model(config)
    output_size = len(plant_data.STATE_FEATURES)
    self.low_speed_residual = CompactResidualPlant(config, output_size)
    self.maneuver_residual = CompactResidualPlant(config, output_size)
    self.gate = gate
    for parameter in self.base.parameters():
      parameter.requires_grad_(False)
    self._zero_residual_output(self.low_speed_residual)
    self._zero_residual_output(self.maneuver_residual)
    self.base.eval()

  @staticmethod
  def _zero_residual_output(model: nn.Module) -> None:
    heads = [module for module in model.modules() if isinstance(module, nn.Linear)]
    if not heads:
      raise ValueError("Residual plant has no linear output layer.")
    nn.init.zeros_(heads[-1].weight)
    nn.init.zeros_(heads[-1].bias)

  def predict_delta(self, history: torch.Tensor,
                    stats: dict[str, torch.Tensor]) -> torch.Tensor:
    normalized = (history.flatten(1) - stats["x_mean"]) / stats["x_std"]
    base_delta = self.base(normalized) * stats["y_std"] + stats["y_mean"]
    low_speed_gate, maneuver_gate = residual_expert_gates(history, self.gate)
    low_speed_mask = torch.as_tensor(
      self.gate.low_speed_state_mask, dtype=normalized.dtype, device=normalized.device,
    )
    maneuver_mask = torch.as_tensor(
      self.gate.maneuver_state_mask, dtype=normalized.dtype, device=normalized.device,
    )
    low_speed_delta = (
      torch.tanh(self.low_speed_residual(normalized))
      * self.gate.residual_max_normalized_delta
      * stats["y_std"]
      * low_speed_mask
    )
    maneuver_delta = (
      torch.tanh(self.maneuver_residual(normalized))
      * self.gate.residual_max_normalized_delta
      * stats["y_std"]
      * maneuver_mask
    )
    return (
      base_delta
      + low_speed_gate[:, None] * low_speed_delta
      + maneuver_gate[:, None] * maneuver_delta
    )


def build_model(config: ModelConfig) -> nn.Module:
  if config.family == "mlp":
    return MLPPlant(config, len(plant_data.STATE_FEATURES))
  if config.family == "gru":
    return GRUPlant(config, len(plant_data.STATE_FEATURES))
  if config.family == "tcn":
    return TCNPlant(config, len(plant_data.STATE_FEATURES))
  if config.family == "transformer":
    return TransformerPlant(config, len(plant_data.STATE_FEATURES))
  raise ValueError(f"Unsupported model family: {config.family}")


def parameter_count(model: nn.Module) -> int:
  return sum(parameter.numel() for parameter in model.parameters())


def evaluation_batch_size(config: ModelConfig) -> int:
  if config.family in ("tcn", "transformer") or config.history_steps >= 150:
    return SEQUENCE_EVALUATION_BATCH_SIZE
  return EVALUATION_BATCH_SIZE


def sampled_indexes(length: int, limit: int | None, seed: int) -> np.ndarray:
  indexes = np.arange(length)
  if limit is not None and len(indexes) > limit:
    indexes = np.sort(np.random.default_rng(seed).choice(indexes, limit, replace=False))
  return indexes


def speed_bucket_indexes(speeds: np.ndarray) -> np.ndarray:
  buckets = np.full(len(speeds), -1, dtype=np.int8)
  for index, (lower, upper) in enumerate(SPEED_BUCKETS_MPS):
    buckets[(speeds >= lower) & (speeds < upper)] = index
  return buckets


def speed_stratified_indexes(speeds: np.ndarray, size: int,
                             rng: np.random.Generator) -> np.ndarray:
  buckets = speed_bucket_indexes(speeds)
  present = [index for index in range(len(SPEED_BUCKETS_MPS)) if np.any(buckets == index)]
  if not present:
    raise ValueError("No speed buckets are present.")
  quota, remainder = divmod(size, len(present))
  selected: list[np.ndarray] = []
  for order, bucket in enumerate(present):
    candidates = np.flatnonzero(buckets == bucket)
    count = quota + (1 if order < remainder else 0)
    selected.append(rng.choice(candidates, count, replace=len(candidates) < count))
  result = np.concatenate(selected)
  rng.shuffle(result)
  return result


def speed_stratified_evaluation_indexes(speeds: np.ndarray, size: int,
                                        rng: np.random.Generator) -> np.ndarray:
  size = min(size, len(speeds))
  buckets = speed_bucket_indexes(speeds)
  present = [index for index in range(len(SPEED_BUCKETS_MPS)) if np.any(buckets == index)]
  if not present:
    raise ValueError("No speed buckets are present.")
  quota, remainder = divmod(size, len(present))
  selected: list[np.ndarray] = []
  for order, bucket in enumerate(present):
    candidates = np.flatnonzero(buckets == bucket)
    count = min(len(candidates), quota + (1 if order < remainder else 0))
    selected.append(rng.choice(candidates, count, replace=False))
  result = np.concatenate(selected)
  if len(result) < size:
    remaining = np.setdiff1d(np.arange(len(speeds)), result, assume_unique=False)
    result = np.concatenate((
      result,
      rng.choice(remaining, size - len(result), replace=False),
    ))
  rng.shuffle(result)
  return result


def speed_conditioned_state_weights(speed: torch.Tensor) -> torch.Tensor:
  low = torch.as_tensor(LOW_SPEED_STATE_WEIGHTS, dtype=speed.dtype, device=speed.device)
  high = torch.as_tensor(HIGH_SPEED_STATE_WEIGHTS, dtype=speed.dtype, device=speed.device)
  low_blend = torch.clamp((8.0 - speed) / 7.5, 0.0, 1.0)[..., None]
  weights = low_blend * low + (1.0 - low_blend) * high
  return weights / weights.mean(dim=-1, keepdim=True).clamp_min(1e-6)


def linear_gate(values: torch.Tensor, on: float, full: float) -> torch.Tensor:
  if full <= on:
    raise ValueError("Gate full threshold must exceed its on threshold.")
  return torch.clamp((values - on) / (full - on), 0.0, 1.0)


def residual_expert_gates(history: torch.Tensor,
                          gate: ResidualGateConfig) -> tuple[torch.Tensor, torch.Tensor]:
  speed = history[:, 0, V_EGO_INDEX]
  angle = history[:, 0, STATE_INDEXES[1]].abs()
  signed_rate = history[:, 0, STATE_INDEXES[SIGNED_STEERING_RATE_STATE_INDEX]].abs()
  low_speed = torch.clamp(
    (gate.low_speed_off_above_mps - speed)
    / (gate.low_speed_off_above_mps - gate.low_speed_full_below_mps),
    0.0,
    1.0,
  )
  maneuver = torch.maximum(
    linear_gate(angle, gate.maneuver_angle_on_deg, gate.maneuver_angle_full_deg),
    linear_gate(signed_rate, gate.maneuver_rate_on_deg_s, gate.maneuver_rate_full_deg_s),
  )
  maneuver_speed = torch.minimum(
    linear_gate(
      speed,
      gate.maneuver_speed_on_below_mps,
      gate.maneuver_speed_full_below_mps,
    ),
    torch.clamp(
      (gate.maneuver_speed_off_above_mps - speed)
      / (
        gate.maneuver_speed_off_above_mps
        - gate.maneuver_speed_full_above_mps
      ),
      0.0,
      1.0,
    ),
  )
  return low_speed, maneuver * maneuver_speed


def residual_activity_gate(history: torch.Tensor,
                           gate: ResidualGateConfig) -> torch.Tensor:
  low_speed, maneuver = residual_expert_gates(history, gate)
  return torch.maximum(low_speed, maneuver)


def residual_stratified_indexes(history: np.ndarray, size: int,
                                gate: ResidualGateConfig, expert: str,
                                rng: np.random.Generator) -> np.ndarray:
  speed = history[:, 0, V_EGO_INDEX]
  speed_buckets = speed_bucket_indexes(speed)
  angle = np.abs(history[:, 0, STATE_INDEXES[1]])
  signed_rate = np.abs(history[:, 0, STATE_INDEXES[SIGNED_STEERING_RATE_STATE_INDEX]])
  active = (
    (speed < gate.low_speed_off_above_mps)
    | (angle > gate.maneuver_angle_on_deg)
    | (signed_rate > gate.maneuver_rate_on_deg_s)
  ).astype(np.int8)
  groups = speed_buckets * 2 + active
  eligible = np.ones(len(history), dtype=bool)
  if expert == "low_speed":
    eligible = speed < gate.low_speed_off_above_mps
  elif expert == "maneuver":
    eligible = (
      (speed > gate.maneuver_speed_on_below_mps)
      & (speed < gate.maneuver_speed_off_above_mps)
      & (
        (angle > gate.maneuver_angle_on_deg)
        | (signed_rate > gate.maneuver_rate_on_deg_s)
      )
    )
  elif expert != "both":
    raise ValueError(f"Unsupported residual expert: {expert}")
  present = [group for group in np.unique(groups[eligible]) if group >= 0]
  quota, remainder = divmod(size, len(present))
  selected: list[np.ndarray] = []
  for order, group in enumerate(present):
    candidates = np.flatnonzero((groups == group) & eligible)
    count = quota + (1 if order < remainder else 0)
    selected.append(rng.choice(candidates, count, replace=len(candidates) < count))
  result = np.concatenate(selected)
  rng.shuffle(result)
  return result


def residual_validation_coverage(windows: WindowBatch, expert: str,
                                 gate: ResidualGateConfig) -> dict[str, int]:
  speed = windows.history[:, 0, V_EGO_INDEX]
  angle = np.abs(windows.history[:, 0, STATE_INDEXES[1]])
  signed_rate = np.abs(
    windows.history[:, 0, STATE_INDEXES[SIGNED_STEERING_RATE_STATE_INDEX]],
  )
  coverage = {
    f"{lower:g}-{'inf' if math.isinf(upper) else f'{upper:g}'}mps": int(np.sum(
      (speed >= lower) & (speed < upper),
    ))
    for lower, upper in SPEED_BUCKETS_MPS
  }
  coverage["maneuver_8-15mps"] = int(np.sum(
    (speed >= 8.0) & (speed < 15.0)
    & (
      (angle > gate.maneuver_angle_on_deg)
      | (signed_rate > gate.maneuver_rate_on_deg_s)
    ),
  ))
  if expert in ("low_speed", "both") and coverage["0.5-3mps"] == 0:
    raise ValueError(
      "Low-speed residual validation has no eligible 0.5-3 m/s windows. " +
      "Choose a route split with actual standstill/intersection coverage.",
    )
  if expert in ("maneuver", "both") and coverage["maneuver_8-15mps"] == 0:
    raise ValueError(
      "Maneuver residual validation has no eligible 8-15 m/s turn windows.",
    )
  return coverage


def load_ensemble_artifact(path: Path, device: torch.device | str = "cpu",
                           differentiable: bool = False) -> tuple[
  list[nn.Module], dict[str, torch.Tensor], dict[str, Any]
]:
  payload = torch.load(path, map_location=device, weights_only=False)
  if payload.get("format_version") != FORMAT_VERSION:
    raise ValueError(f"Unsupported neural plant format: {payload.get('format_version')}")
  config = ModelConfig(**payload["config"])
  models: list[nn.Module] = []
  for state in payload["members"]:
    if payload.get("model_type") == "neural_speed_gated_residual_lateral_plant_ensemble":
      model = SpeedGatedResidualPlant(
        config, ResidualGateConfig(**payload["residual_gate"]),
      ).to(device)
    else:
      model = build_model(config).to(device)
    model.load_state_dict(state)
    model.train(differentiable)
    if differentiable:
      for parameter in model.parameters():
        parameter.requires_grad_(False)
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
    & (values["v_ego"][source] >= plant_data.MIN_TRAIN_SPEED_MPS)
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


def common_source_keys(trajectories: list[plant_data.Trajectory], routes: set[str],
                       configs: tuple[ModelConfig, ...], rollout_seconds: float) -> set[tuple[int, int]]:
  common: set[tuple[int, int]] = set()
  for trajectory_index, trajectory in enumerate(trajectories):
    if trajectory.route not in routes:
      continue
    config_sources: list[set[int]] = []
    for config in configs:
      rollout_steps = max(1, round(rollout_seconds / config.sample_period_s))
      sources = eligible_sources(trajectory, config, rollout_steps)[0]
      config_sources.append({int(source) * config.sample_step for source in sources})
    shared = set.intersection(*config_sources)
    common.update((trajectory_index, source) for source in shared)
  return common


def sampled_source_keys(source_keys: set[tuple[int, int]], cap: int | None,
                        seed: int) -> set[tuple[int, int]]:
  ordered = sorted(source_keys)
  return {ordered[index] for index in sampled_indexes(len(ordered), cap, seed)}


def build_windows(trajectories: list[plant_data.Trajectory], routes: set[str],
                  config: ModelConfig, rollout_steps: int, cap: int | None,
                  seed: int, source_keys: set[tuple[int, int]] | None = None,
                  sample_with_replacement: bool = True) -> WindowBatch:
  candidates: list[tuple[int, int]] = []
  prepared: dict[int, tuple[np.ndarray, dict[str, np.ndarray]]] = {}
  for trajectory_index, trajectory in enumerate(trajectories):
    if trajectory.route not in routes:
      continue
    sources, times, values = eligible_sources(trajectory, config, rollout_steps)
    if source_keys is not None:
      sources = np.asarray([
        source for source in sources
        if (trajectory_index, int(source) * config.sample_step) in source_keys
      ], dtype=np.int64)
    if len(sources):
      prepared[trajectory_index] = (times, values)
      candidates.extend((trajectory_index, int(source)) for source in sources)
  if not candidates:
    raise ValueError(f"No clean windows for {config.name} across {len(routes)} routes.")

  if cap is not None and len(candidates) > cap:
    rng = np.random.default_rng(seed)
    candidate_speeds = np.asarray([
      prepared[trajectory_index][1]["v_ego"][source]
      for trajectory_index, source in candidates
    ])
    selected = (
      speed_stratified_indexes(candidate_speeds, cap, rng)
      if sample_with_replacement
      else speed_stratified_evaluation_indexes(candidate_speeds, cap, rng)
    )
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
  if isinstance(model, SpeedGatedResidualPlant):
    return model.predict_delta(history, stats)
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
    next_base[:, STEERING_RATE_INDEX] = next_state[:, SIGNED_STEERING_RATE_STATE_INDEX].abs()
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
                 initial_state: dict[str, torch.Tensor] | None = None,
                 residual_gate: ResidualGateConfig | None = None,
                 residual_expert: str = "both",
                 preservation_weight: float = 1.0,
                 max_mid_speed_regression: float = 0.03,
                 max_high_speed_regression: float = 0.01) -> tuple[nn.Module, dict[str, Any]]:
  torch.manual_seed(seed)
  if residual_gate is not None:
    if initial_state is None:
      raise ValueError("Gated residual training requires an initial model.")
    model = SpeedGatedResidualPlant(config, residual_gate).to(device)
    if any(name.startswith("base.") for name in initial_state):
      model.load_state_dict(initial_state)
    else:
      model.base.load_state_dict(initial_state)
    if residual_expert == "low_speed":
      for parameter in model.maneuver_residual.parameters():
        parameter.requires_grad_(False)
    elif residual_expert == "maneuver":
      for parameter in model.low_speed_residual.parameters():
        parameter.requires_grad_(False)
    elif residual_expert != "both":
      raise ValueError(f"Unsupported residual expert: {residual_expert}")
  else:
    model = build_model(config).to(device)
  if initial_state is not None and residual_gate is None:
    model.load_state_dict(initial_state)
  teacher: nn.Module | None = None
  if initial_state is not None and residual_gate is None and preservation_weight > 0.0:
    teacher = build_model(config).to(device)
    teacher.load_state_dict(initial_state)
    teacher.eval()
    for parameter in teacher.parameters():
      parameter.requires_grad_(False)
  optimizer = torch.optim.AdamW(
    [parameter for parameter in model.parameters() if parameter.requires_grad],
    lr=learning_rate,
    weight_decay=2e-5,
  )
  stats_t = tensor_stats(stats, device)
  rng = np.random.default_rng(seed)
  best_state = copy.deepcopy(model.state_dict())
  stale_epochs = 0
  history_report: list[dict[str, float]] = []
  started = perf_counter()

  validation_indexes = speed_stratified_evaluation_indexes(
    validation_windows.history[:, 0, V_EGO_INDEX],
    min(len(validation_windows), 5000),
    np.random.default_rng(seed + 10_000),
  )
  validation_limit = len(validation_indexes)
  validation_steps = min(rollout_train_steps, validation_windows.target_states.shape[1])

  def validation_metrics() -> dict[str, Any]:
    model.eval()
    validation_squared_error = 0.0
    validation_values = 0
    bucket_squared_error = torch.zeros(
      (len(SPEED_BUCKETS_MPS), len(plant_data.STATE_FEATURES)),
      dtype=torch.float64,
      device=device,
    )
    bucket_values = torch.zeros(len(SPEED_BUCKETS_MPS), dtype=torch.float64, device=device)
    cruise_squared_error = torch.zeros_like(bucket_squared_error)
    cruise_values = torch.zeros_like(bucket_values)
    maneuver_squared_error = torch.zeros(
      len(plant_data.STATE_FEATURES), dtype=torch.float64, device=device,
    )
    maneuver_values = torch.zeros((), dtype=torch.float64, device=device)
    validation_batch = min(
      evaluation_batch_size(config),
      128 if residual_gate is not None else evaluation_batch_size(config),
    )
    with torch.no_grad():
      for start in range(0, validation_limit, validation_batch):
        indexes = validation_indexes[start:start + validation_batch]
        val_history = torch.as_tensor(validation_windows.history[indexes], device=device)
        val_future = torch.as_tensor(validation_windows.future_base[indexes], device=device)
        val_targets = torch.as_tensor(validation_windows.target_states[indexes], device=device)
        validation_prediction = rollout(
          model, val_history, val_future, stats_t, validation_steps,
        )
        validation_error = (
          validation_prediction - val_targets[:, :validation_steps]
        ) / stats_t["state_std"]
        validation_weights = speed_conditioned_state_weights(
          val_future[:, :validation_steps, V_EGO_INDEX],
        )
        validation_squared_error += float(torch.sum(validation_error ** 2 * validation_weights).cpu())
        validation_values += validation_error.numel()
        validation_speed = val_future[:, :validation_steps, V_EGO_INDEX]
        cruise_mask = torch.ones_like(validation_speed, dtype=torch.bool)
        maneuver_mask = torch.zeros_like(validation_speed, dtype=torch.bool)
        if residual_gate is not None:
          validation_angle = val_future[:, :validation_steps, STATE_INDEXES[1]].abs()
          validation_rate = val_future[
            :, :validation_steps, STATE_INDEXES[SIGNED_STEERING_RATE_STATE_INDEX]
          ].abs()
          cruise_mask = (
            (validation_speed >= residual_gate.low_speed_off_above_mps)
            & (validation_angle <= residual_gate.maneuver_angle_on_deg)
            & (validation_rate <= residual_gate.maneuver_rate_on_deg_s)
          )
          maneuver_mask = (
            (validation_speed >= 8.0) & (validation_speed < 15.0)
            & (
              (validation_angle > residual_gate.maneuver_angle_on_deg)
              | (validation_rate > residual_gate.maneuver_rate_on_deg_s)
            )
          )
          if maneuver_mask.any():
            maneuver_squared_error += (validation_error[maneuver_mask] ** 2).sum(dim=0)
            maneuver_values += maneuver_mask.sum()
        for bucket_index, (lower, upper) in enumerate(SPEED_BUCKETS_MPS):
          mask = (validation_speed >= lower) & (validation_speed < upper)
          if mask.any():
            bucket_squared_error[bucket_index] += (validation_error[mask] ** 2).sum(dim=0)
            bucket_values[bucket_index] += mask.sum()
          cruise_bucket_mask = mask & cruise_mask
          if cruise_bucket_mask.any():
            cruise_squared_error[bucket_index] += (
              validation_error[cruise_bucket_mask] ** 2
            ).sum(dim=0)
            cruise_values[bucket_index] += cruise_bucket_mask.sum()
    bucket_rmse = torch.sqrt(
      bucket_squared_error / bucket_values[:, None].clamp_min(1.0),
    ).cpu().numpy()
    return {
      "loss": validation_squared_error / validation_values,
      "bucket_normalized_rmse": bucket_rmse.tolist(),
      "cruise_bucket_normalized_rmse": torch.sqrt(
        cruise_squared_error / cruise_values[:, None].clamp_min(1.0),
      ).cpu().numpy().tolist(),
      "cruise_bucket_values": cruise_values.cpu().numpy().tolist(),
      "maneuver_8_15_normalized_rmse": torch.sqrt(
        maneuver_squared_error / maneuver_values.clamp_min(1.0),
      ).cpu().numpy().tolist(),
      "maneuver_8_15_values": float(maneuver_values.cpu()),
    }

  initial_validation = validation_metrics()
  best_loss = float(initial_validation["loss"])
  initial_validation_loss = best_loss
  initial_bucket_rmse = np.asarray(initial_validation["bucket_normalized_rmse"])
  initial_cruise_rmse = np.asarray(initial_validation["cruise_bucket_normalized_rmse"])
  initial_maneuver_rmse = np.asarray(initial_validation["maneuver_8_15_normalized_rmse"])
  print(
    f"{config.name} seed={seed} initial_validation={best_loss:.6f}",
    flush=True,
  )

  for epoch in range(1, epochs + 1):
    model.train()
    losses: list[float] = []
    for _ in range(steps_per_epoch):
      if residual_gate is not None:
        indexes = residual_stratified_indexes(
          train_windows.history,
          min(batch_size, len(train_windows)),
          residual_gate,
          residual_expert,
          rng,
        )
      else:
        indexes = speed_stratified_indexes(
          train_windows.history[:, 0, V_EGO_INDEX],
          min(batch_size, len(train_windows)),
          rng,
        )
      batch_history = torch.as_tensor(train_windows.history[indexes], device=device)
      batch_future = torch.as_tensor(train_windows.future_base[indexes], device=device)
      batch_targets = torch.as_tensor(train_windows.target_states[indexes], device=device)
      steps = min(rollout_train_steps, batch_targets.shape[1])
      predicted = rollout(model, batch_history, batch_future, stats_t, steps)
      scaled_error = (predicted - batch_targets[:, :steps]) / stats_t["state_std"]
      step_weights = torch.linspace(1.0, 0.5, steps, device=device)
      state_weights = speed_conditioned_state_weights(batch_future[:, :steps, V_EGO_INDEX])
      loss = (
        (
          F.smooth_l1_loss(scaled_error, torch.zeros_like(scaled_error), beta=0.20, reduction="none")
          * state_weights
        )
        .mean(dim=(0, 2))
        .mul(step_weights)
        .sum()
        / step_weights.sum()
      )
      preservation_loss = torch.zeros((), dtype=loss.dtype, device=device)
      if teacher is not None:
        with torch.no_grad():
          teacher_prediction = rollout(
            teacher, batch_history.clone(), batch_future, stats_t, steps,
          )
        teacher_error = (predicted - teacher_prediction) / stats_t["state_std"]
        speed = batch_future[:, :steps, V_EGO_INDEX]
        preservation_scale = torch.clamp((speed - 5.0) / 3.0, 0.0, 1.0)
        preservation_loss = (
          F.smooth_l1_loss(
            teacher_error,
            torch.zeros_like(teacher_error),
            beta=0.10,
            reduction="none",
          )
          * preservation_scale[..., None]
        ).sum() / (
          preservation_scale.sum().clamp_min(1.0) * teacher_error.shape[-1]
        )
        loss = loss + preservation_weight * preservation_loss
      optimizer.zero_grad(set_to_none=True)
      loss.backward()
      torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
      optimizer.step()
      losses.append(float(loss.detach().cpu()))

    current_validation = validation_metrics()
    current_validation_loss = float(current_validation["loss"])
    current_bucket_rmse = np.asarray(current_validation["bucket_normalized_rmse"])
    current_cruise_rmse = np.asarray(current_validation["cruise_bucket_normalized_rmse"])
    current_maneuver_rmse = np.asarray(current_validation["maneuver_8_15_normalized_rmse"])
    maneuver_weights = np.asarray(HIGH_SPEED_STATE_WEIGHTS)
    current_maneuver_score = float(np.average(
      current_maneuver_rmse, weights=maneuver_weights,
    ))
    cruise_max_ratio = float(np.max(
      current_cruise_rmse[2:]
      / np.maximum(initial_cruise_rmse[2:], 1e-6)
    ))
    preserved = True
    if initial_state is not None:
      for bucket_index, tolerance in (
        (2, max_mid_speed_regression),
        (3, max_high_speed_regression),
        (4, max_high_speed_regression),
      ):
        reference_rmse = (
          initial_cruise_rmse[bucket_index]
          if residual_gate is not None
          else initial_bucket_rmse[bucket_index]
        )
        candidate_rmse = (
          current_cruise_rmse[bucket_index]
          if residual_gate is not None
          else current_bucket_rmse[bucket_index]
        )
        preserved &= bool(np.all(
          candidate_rmse <= reference_rmse * (1.0 + tolerance) + 1e-4
        ))
      if residual_gate is not None:
        preserved &= bool(
          current_maneuver_score
          <= np.average(initial_maneuver_rmse, weights=maneuver_weights)
          * (1.0 + max_high_speed_regression)
          + 1e-4
        )
    train_loss = float(np.mean(losses))
    history_report.append({
      "epoch": epoch,
      "train_loss": train_loss,
      "validation_loss": current_validation_loss,
      "preserved": preserved,
      "maneuver_8_15_score": current_maneuver_score,
      "cruise_max_ratio": cruise_max_ratio,
    })
    print(
      f"{config.name} seed={seed} epoch={epoch:03d} " +
      f"train={train_loss:.6f} validation={current_validation_loss:.6f} " +
      f"maneuver={current_maneuver_score:.6f} cruise_ratio={cruise_max_ratio:.4f} " +
      f"preserved={preserved}",
      flush=True,
    )
    if preserved and current_validation_loss < best_loss - 1e-6:
      best_loss = current_validation_loss
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
    "initial_validation_loss": initial_validation_loss,
    "initial_validation_speed_buckets": initial_validation["bucket_normalized_rmse"],
    "initial_validation_cruise_speed_buckets": (
      initial_validation["cruise_bucket_normalized_rmse"]
    ),
    "initial_validation_maneuver_8_15": (
      initial_validation["maneuver_8_15_normalized_rmse"]
    ),
    "fit_seconds": perf_counter() - started,
    "rollout_train_steps": validation_steps,
    "rollout_train_seconds": validation_steps * config.sample_period_s,
    "history": history_report,
  }


def normalized_speed_metrics(normalized_error: np.ndarray, source_speed: np.ndarray,
                             state_std: np.ndarray) -> dict[str, Any]:
  buckets = speed_bucket_indexes(source_speed)
  metrics: dict[str, Any] = {}
  for index, (lower, upper) in enumerate(SPEED_BUCKETS_MPS):
    mask = buckets == index
    if not np.any(mask):
      continue
    normalized_rmse = np.sqrt(np.mean(normalized_error[mask] ** 2, axis=(0, 1)))
    absolute_rmse = normalized_rmse * state_std
    blend_speed = float(np.median(source_speed[mask]))
    low_blend = float(np.clip((8.0 - blend_speed) / 7.5, 0.0, 1.0))
    state_weights = (
      low_blend * np.asarray(LOW_SPEED_STATE_WEIGHTS)
      + (1.0 - low_blend) * np.asarray(HIGH_SPEED_STATE_WEIGHTS)
    )
    upper_label = "inf" if math.isinf(upper) else f"{upper:g}"
    metrics[f"{lower:g}-{upper_label}mps"] = {
      "windows": int(np.count_nonzero(mask)),
      "score": float(np.average(normalized_rmse, weights=state_weights)),
      "normalized_rmse": {
        name: float(normalized_rmse[state_index])
        for state_index, name in enumerate(plant_data.STATE_FEATURES)
      },
      "absolute_rmse": {
        name: float(absolute_rmse[state_index])
        for state_index, name in enumerate(plant_data.STATE_FEATURES)
      },
    }
  return metrics


def normalized_regime_metrics(normalized_error: np.ndarray, history: np.ndarray,
                              state_std: np.ndarray) -> dict[str, Any]:
  speed = history[:, 0, V_EGO_INDEX]
  angle = history[:, 0, STATE_INDEXES[1]]
  signed_rate = history[:, 0, STATE_INDEXES[SIGNED_STEERING_RATE_STATE_INDEX]]
  regimes = {
    "low_speed_below_5mps": speed < 5.0,
    "turn_8-15mps": (
      (speed >= 8.0) & (speed < 15.0)
      & ((np.abs(angle) > 2.0) | (np.abs(signed_rate) > 5.0))
    ),
    "unwind_8-15mps": (
      (speed >= 8.0) & (speed < 15.0)
      & (angle * signed_rate < 0.0) & (np.abs(signed_rate) > 5.0)
    ),
    "ordinary_cruise_above_5mps": (
      (speed >= 5.0) & (np.abs(angle) <= 2.0) & (np.abs(signed_rate) <= 5.0)
    ),
  }
  metrics: dict[str, Any] = {}
  for name, mask in regimes.items():
    if not np.any(mask):
      continue
    normalized_rmse = np.sqrt(np.mean(normalized_error[mask] ** 2, axis=(0, 1)))
    absolute_rmse = normalized_rmse * state_std
    metrics[name] = {
      "windows": int(np.count_nonzero(mask)),
      "normalized_rmse": {
        state_name: float(normalized_rmse[state_index])
        for state_index, state_name in enumerate(plant_data.STATE_FEATURES)
      },
      "absolute_rmse": {
        state_name: float(absolute_rmse[state_index])
        for state_index, state_name in enumerate(plant_data.STATE_FEATURES)
      },
    }
  return metrics


def evaluate_model(model: nn.Module, windows: WindowBatch, stats: dict[str, np.ndarray],
                   config: ModelConfig, max_windows: int | None, device: torch.device,
                   seed: int) -> dict[str, Any]:
  indexes = speed_stratified_evaluation_indexes(
    windows.history[:, 0, V_EGO_INDEX],
    min(len(windows), max_windows) if max_windows is not None else len(windows),
    np.random.default_rng(seed),
  )
  targets = windows.target_states[indexes]
  stats_t = tensor_stats(stats, device)
  predictions: list[np.ndarray] = []
  batch_size = min(
    evaluation_batch_size(config),
    128 if isinstance(model, SpeedGatedResidualPlant) else evaluation_batch_size(config),
  )
  with torch.no_grad():
    for start in range(0, len(indexes), batch_size):
      batch_indexes = indexes[start:start + batch_size]
      history = torch.as_tensor(windows.history[batch_indexes], device=device)
      future = torch.as_tensor(windows.future_base[batch_indexes], device=device)
      predictions.append(
        rollout(model, history, future, stats_t, targets.shape[1]).cpu().numpy(),
      )
      print(
        f"{config.name} evaluation={min(start + batch_size, len(indexes))}/{len(indexes)}",
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
  score_weights = np.asarray(HIGH_SPEED_STATE_WEIGHTS, dtype=np.float32)
  normalized_rmse = np.sqrt(np.mean(normalized ** 2, axis=(0, 1)))
  score = float(np.average(normalized_rmse, weights=score_weights))
  speed_metrics = normalized_speed_metrics(
    normalized,
    windows.history[indexes, 0, V_EGO_INDEX],
    stats["state_std"],
  )
  return {
    "windows": len(indexes),
    "score": score,
    "normalized_rmse": {
      name: float(normalized_rmse[index])
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "speed_buckets": speed_metrics,
    "regimes": normalized_regime_metrics(
      normalized, windows.history[indexes], stats["state_std"],
    ),
    "horizons": horizons,
  }


def evaluate_ensemble(models: list[nn.Module], windows: WindowBatch,
                      stats: dict[str, np.ndarray], config: ModelConfig,
                      max_windows: int | None, device: torch.device, seed: int) -> dict[str, Any]:
  indexes = speed_stratified_evaluation_indexes(
    windows.history[:, 0, V_EGO_INDEX],
    min(len(windows), max_windows) if max_windows is not None else len(windows),
    np.random.default_rng(seed),
  )
  targets = windows.target_states[indexes]
  stats_t = tensor_stats(stats, device)
  prediction_batches: list[np.ndarray] = []
  disagreement_batches: list[np.ndarray] = []
  batch_size = min(
    evaluation_batch_size(config),
    128 if any(isinstance(model, SpeedGatedResidualPlant) for model in models)
    else evaluation_batch_size(config),
  )
  with torch.no_grad():
    for start in range(0, len(indexes), batch_size):
      batch_indexes = indexes[start:start + batch_size]
      history = torch.as_tensor(windows.history[batch_indexes], device=device)
      future = torch.as_tensor(windows.future_base[batch_indexes], device=device)
      prediction, disagreement = ensemble_rollout(
        models, history, future, stats_t, targets.shape[1],
      )
      prediction_batches.append(prediction.cpu().numpy())
      disagreement_batches.append(disagreement.cpu().numpy())
      print(
        f"{config.name} ensemble_evaluation=" +
        f"{min(start + batch_size, len(indexes))}/{len(indexes)}",
        flush=True,
      )
  prediction = np.concatenate(prediction_batches)
  disagreement = np.concatenate(disagreement_batches)
  normalized = (prediction - targets) / stats["state_std"]
  normalized_rmse = np.sqrt(np.mean(normalized ** 2, axis=(0, 1)))
  normalized_disagreement = disagreement / stats["state_std"]
  score_weights = np.asarray(HIGH_SPEED_STATE_WEIGHTS, dtype=np.float32)
  return {
    "windows": len(indexes),
    "score": float(np.average(normalized_rmse, weights=score_weights)),
    "normalized_rmse": {
      name: float(normalized_rmse[index])
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "speed_buckets": normalized_speed_metrics(
      normalized,
      windows.history[indexes, 0, V_EGO_INDEX],
      stats["state_std"],
    ),
    "regimes": normalized_regime_metrics(
      normalized, windows.history[indexes], stats["state_std"],
    ),
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
    temporal_layers=args.temporal_layers,
    attention_heads=args.attention_heads,
    feedforward_size=args.feedforward_size,
    dropout=args.dropout,
  )


def config_from_mapping(values: dict[str, Any]) -> ModelConfig:
  normalized = dict(values)
  normalized["hidden_sizes"] = tuple(normalized["hidden_sizes"])
  return ModelConfig(**normalized)


def search_candidates(args: argparse.Namespace) -> tuple[ModelConfig, ...]:
  if args.candidate_file is not None:
    payload = json.loads(args.candidate_file.read_text(encoding="utf-8"))
    rows = payload["candidates"] if isinstance(payload, dict) else payload
    candidates = tuple(config_from_mapping(row) for row in rows)
  elif args.search_profile == "initial":
    candidates = INITIAL_CANDIDATES
  elif args.search_profile == "temporal":
    candidates = TEMPORAL_CANDIDATES
  elif args.search_profile == "architecture":
    candidates = ARCHITECTURE_CANDIDATES
  else:
    raise ValueError(f"Unsupported search profile: {args.search_profile}")
  if len(candidates) < 2:
    raise ValueError("Architecture search requires at least two candidates.")
  names = [candidate.name for candidate in candidates]
  if len(names) != len(set(names)):
    raise ValueError("Architecture search candidate names must be unique.")
  return candidates


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
  parser.add_argument("--max-route-driver-overlay", type=float, default=0.50)
  parser.add_argument("--pretraining-note")
  parser.add_argument("--initial-model", type=Path)
  parser.add_argument("--split-report", type=Path)
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
  parser.add_argument("--gated-residual", action="store_true")
  parser.add_argument(
    "--residual-expert",
    choices=("low_speed", "maneuver", "both"),
    default="both",
  )
  parser.add_argument("--low-speed-full-below-mps", type=float, default=3.0)
  parser.add_argument("--low-speed-off-above-mps", type=float, default=5.0)
  parser.add_argument("--maneuver-angle-on-deg", type=float, default=2.0)
  parser.add_argument("--maneuver-angle-full-deg", type=float, default=8.0)
  parser.add_argument("--maneuver-rate-on-deg-s", type=float, default=5.0)
  parser.add_argument("--maneuver-rate-full-deg-s", type=float, default=25.0)
  parser.add_argument("--maneuver-speed-on-below-mps", type=float, default=5.0)
  parser.add_argument("--maneuver-speed-full-below-mps", type=float, default=8.0)
  parser.add_argument("--maneuver-speed-full-above-mps", type=float, default=15.0)
  parser.add_argument("--maneuver-speed-off-above-mps", type=float, default=20.0)
  parser.add_argument("--residual-max-normalized-delta", type=float, default=0.5)
  parser.add_argument(
    "--low-speed-residual-state",
    action="append",
    choices=plant_data.STATE_FEATURES,
  )
  parser.add_argument(
    "--maneuver-residual-state",
    action="append",
    choices=plant_data.STATE_FEATURES,
  )
  parser.add_argument("--preservation-weight", type=float, default=1.0)
  parser.add_argument("--max-mid-speed-regression", type=float, default=0.03)
  parser.add_argument("--max-high-speed-regression", type=float, default=0.01)
  parser.add_argument("--random-state", type=int, default=23)
  parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
  return parser


def load_current(args: argparse.Namespace) -> tuple[list[plant_data.Trajectory], dict[str, Any]]:
  cache_path = args.trajectory_cache or args.output_dir / "current_trajectories.joblib"
  trajectories, inventory = load_trajectories(
    args.current_root, cache_path, args.workers, args.brand, args.car_fingerprint_contains,
  )
  retained, route_stats, excluded_routes = filter_high_overlay_routes(
    trajectories, args.max_route_driver_overlay,
  )
  print(
    f"excluded {len(excluded_routes)}/{len(route_stats)} routes above " +
    f"{args.max_route_driver_overlay:.0%} driver overlay",
    flush=True,
  )
  for route in excluded_routes:
    print(f"  exclude {route}: {route_stats[route]['driver_overlay_fraction']:.1%} overlay", flush=True)
  inventory["route_filter"] = {
    "max_driver_overlay_fraction": args.max_route_driver_overlay,
    "excluded_routes": excluded_routes,
    "retained_route_count": len({trajectory.route for trajectory in retained}),
  }
  return retained, inventory


def route_holdouts(args: argparse.Namespace) -> tuple[str, ...]:
  return tuple(args.holdout_route_prefix or DEFAULT_HOLDOUTS)


def filter_high_overlay_routes(
  trajectories: list[plant_data.Trajectory],
  max_driver_overlay_fraction: float,
) -> tuple[list[plant_data.Trajectory], dict[str, dict[str, float]], list[str]]:
  route_stats = plant_data.route_intervention_stats(trajectories)
  excluded_routes = sorted(
    route for route, stats in route_stats.items()
    if stats["driver_overlay_fraction"] > max_driver_overlay_fraction
  )
  retained = [
    trajectory for trajectory in trajectories
    if trajectory.route not in excluded_routes
  ]
  return retained, route_stats, excluded_routes


def pretraining_note(args: argparse.Namespace) -> str:
  return args.pretraining_note or "Pretraining was not performed by this command."


def search_report_path(args: argparse.Namespace) -> Path:
  label = args.candidate_file.stem if args.candidate_file is not None else args.search_profile
  return args.output_dir / f"{label}_search.json"


def effective_rollout_train_steps(requested_seconds: float, config: ModelConfig,
                                  rollout_steps: int) -> int:
  requested_steps = max(1, round(requested_seconds / config.sample_period_s))
  return min(rollout_steps, requested_steps)


def split_from_report(path: Path, usable_routes: set[str],
                      inventory: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
  report = json.loads(path.read_text(encoding="utf-8"))
  data = report.get("data", report)
  report_inventory = data.get("current", data.get("current_data"))
  if report_inventory is None:
    raise ValueError("Split report does not contain a data inventory.")
  for key in ("rlog_count", "rlog_bytes", "newest_mtime_ns"):
    if report_inventory.get(key) != inventory.get(key):
      raise ValueError(f"Split report data inventory differs at {key}.")
  training = set(data["train_routes"])
  validation = set(data["validation_routes"])
  holdout = set(data["holdout_routes"])
  if not training or not validation or not holdout:
    raise ValueError("Split report must contain non-empty train, validation, and holdout routes.")
  if training & validation or training & holdout or validation & holdout:
    raise ValueError("Split report route cohorts must be disjoint.")
  unavailable = (training | validation | holdout) - usable_routes
  if unavailable:
    raise ValueError(
      "Split report contains routes that are not eligible for this configuration: " +
      ", ".join(sorted(unavailable))
    )
  return training, validation, holdout


def run_search(args: argparse.Namespace) -> None:
  trajectories, inventory = load_current(args)
  candidates = search_candidates(args)
  candidate_routes = [
    eligible_routes(
      trajectories,
      candidate,
      max(1, round(args.rollout_seconds / candidate.sample_period_s)),
    )
    for candidate in candidates
  ]
  common_routes = set.intersection(*candidate_routes)
  if args.split_report is not None:
    train_routes, validation_routes, holdout_routes = split_from_report(
      args.split_report, common_routes, inventory,
    )
  else:
    split_trajectories = [
      trajectory for trajectory in trajectories
      if trajectory.route in common_routes
    ]
    train_routes, validation_routes, holdout_routes = split_routes(
      split_trajectories, args.validation_fraction, route_holdouts(args), args.random_state,
    )
  shared_train_sources = sampled_source_keys(
    common_source_keys(trajectories, train_routes, candidates, args.rollout_seconds),
    args.max_train_windows,
    args.random_state,
  )
  shared_validation_sources = sampled_source_keys(
    common_source_keys(trajectories, validation_routes, candidates, args.rollout_seconds),
    args.max_validation_windows,
    args.random_state + 1,
  )
  if not shared_train_sources or not shared_validation_sources:
    raise ValueError("No physical source windows are shared by every search candidate.")
  device = torch.device(args.device)
  results: list[dict[str, Any]] = []
  for candidate in candidates:
    rollout_steps = max(1, round(args.rollout_seconds / candidate.sample_period_s))
    train_windows = build_windows(
      trajectories, train_routes, candidate, rollout_steps,
      None, args.random_state, shared_train_sources,
    )
    validation_windows = build_windows(
      trajectories, validation_routes, candidate, rollout_steps,
      None, args.random_state + 1, shared_validation_sources,
      sample_with_replacement=False,
    )
    stats = normalization(train_windows)
    rollout_train_steps = effective_rollout_train_steps(
      args.rollout_train_seconds, candidate, rollout_steps,
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
    "search_profile": args.search_profile,
    "candidate_file": str(args.candidate_file) if args.candidate_file is not None else None,
    "split_report": str(args.split_report) if args.split_report is not None else None,
    "data": {
      "current": inventory,
      "pretraining_performed": False,
      "pretraining_note": pretraining_note(args),
      "train_routes": sorted(train_routes),
      "validation_routes": sorted(validation_routes),
      "holdout_routes": sorted(holdout_routes),
      "eligible_route_count": len(common_routes),
      "excluded_route_count": len({item.route for item in trajectories} - common_routes),
      "shared_train_source_count": len(shared_train_sources),
      "shared_validation_source_count": len(shared_validation_sources),
    },
    "selection_metric": "weighted normalized autoregressive rollout RMSE",
    "candidates": results,
    "selected": results[0]["config"],
  }
  args.output_dir.mkdir(parents=True, exist_ok=True)
  path = search_report_path(args)
  path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(f"selected {results[0]['config']['name']}", flush=True)
  print(f"report: {path}", flush=True)


def run_train(args: argparse.Namespace) -> None:
  trajectories, inventory = load_current(args)
  config = config_from_args(args)
  rollout_steps = max(1, round(args.rollout_seconds / config.sample_period_s))
  usable_routes = eligible_routes(trajectories, config, rollout_steps)
  if args.split_report is not None:
    train_routes, validation_routes, holdout_routes = split_from_report(
      args.split_report, usable_routes, inventory,
    )
  else:
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
    sample_with_replacement=False,
  )
  holdout_windows = build_windows(
    trajectories, holdout_routes, config, rollout_steps,
    args.max_holdout_windows, args.random_state + 2,
    sample_with_replacement=False,
  )
  validation_coverage: dict[str, int] | None = None
  if args.gated_residual:
    validation_coverage = residual_validation_coverage(
      validation_windows,
      args.residual_expert,
      ResidualGateConfig(
        low_speed_full_below_mps=args.low_speed_full_below_mps,
        low_speed_off_above_mps=args.low_speed_off_above_mps,
        maneuver_angle_on_deg=args.maneuver_angle_on_deg,
        maneuver_angle_full_deg=args.maneuver_angle_full_deg,
        maneuver_rate_on_deg_s=args.maneuver_rate_on_deg_s,
        maneuver_rate_full_deg_s=args.maneuver_rate_full_deg_s,
        maneuver_speed_on_below_mps=args.maneuver_speed_on_below_mps,
        maneuver_speed_full_below_mps=args.maneuver_speed_full_below_mps,
        maneuver_speed_full_above_mps=args.maneuver_speed_full_above_mps,
        maneuver_speed_off_above_mps=args.maneuver_speed_off_above_mps,
        residual_max_normalized_delta=args.residual_max_normalized_delta,
      ),
    )
  initial_payload: dict[str, Any] | None = None
  if args.initial_model is not None:
    initial_payload = torch.load(args.initial_model, map_location="cpu", weights_only=False)
  if args.gated_residual and initial_payload is None:
    raise ValueError("--gated-residual requires --initial-model.")
  stats = (
    initial_payload["normalization"]
    if initial_payload is not None
    else normalization(train_windows)
  )
  device = torch.device(args.device)
  rollout_train_steps = effective_rollout_train_steps(
    args.rollout_train_seconds, config, rollout_steps,
  )
  models: list[nn.Module] = []
  member_reports: list[dict[str, Any]] = []
  ensemble_seeds = args.ensemble_seed or [23, 41, 71]
  residual_gate = ResidualGateConfig(
    low_speed_full_below_mps=args.low_speed_full_below_mps,
    low_speed_off_above_mps=args.low_speed_off_above_mps,
    maneuver_angle_on_deg=args.maneuver_angle_on_deg,
    maneuver_angle_full_deg=args.maneuver_angle_full_deg,
    maneuver_rate_on_deg_s=args.maneuver_rate_on_deg_s,
    maneuver_rate_full_deg_s=args.maneuver_rate_full_deg_s,
    maneuver_speed_on_below_mps=args.maneuver_speed_on_below_mps,
    maneuver_speed_full_below_mps=args.maneuver_speed_full_below_mps,
    maneuver_speed_full_above_mps=args.maneuver_speed_full_above_mps,
    maneuver_speed_off_above_mps=args.maneuver_speed_off_above_mps,
    residual_max_normalized_delta=args.residual_max_normalized_delta,
    low_speed_state_mask=tuple(
      float(name in (args.low_speed_residual_state or plant_data.STATE_FEATURES))
      for name in plant_data.STATE_FEATURES
    ),
    maneuver_state_mask=tuple(
      float(name in (args.maneuver_residual_state or plant_data.STATE_FEATURES))
      for name in plant_data.STATE_FEATURES
    ),
  ) if args.gated_residual else None
  initial_states: list[dict[str, torch.Tensor]] = []
  if initial_payload is not None:
    initial_config = ModelConfig(**initial_payload["config"])
    initial_shape = asdict(initial_config)
    current_shape = asdict(config)
    initial_shape.pop("name")
    current_shape.pop("name")
    if initial_shape != current_shape:
      raise ValueError(
        f"Initial model config {initial_config.name} does not match {config.name}.",
      )
    initial_states = initial_payload["members"]
  for member_index, seed in enumerate(ensemble_seeds):
    model, fit = train_member(
      config, train_windows, validation_windows, stats,
      seed, args.epochs, args.patience, args.batch_size,
      args.learning_rate, rollout_train_steps, args.steps_per_epoch, device,
      initial_state=initial_states[member_index % len(initial_states)] if initial_states else None,
      residual_gate=residual_gate,
      residual_expert=args.residual_expert,
      preservation_weight=args.preservation_weight,
      max_mid_speed_regression=args.max_mid_speed_regression,
      max_high_speed_regression=args.max_high_speed_regression,
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
    "model_type": (
      "neural_speed_gated_residual_lateral_plant_ensemble"
      if residual_gate is not None
      else "neural_controller_independent_lateral_plant_ensemble"
    ),
    "config": asdict(config),
    "feature_names": list(plant_data.BASE_FEATURES),
    "state_feature_names": list(plant_data.STATE_FEATURES),
    "normalization": stats,
    "members": [state_dict_cpu(model) for model in models],
    "metadata": {
      "current_data": inventory,
      "pretraining_performed": args.initial_model is not None,
      "pretraining_note": (
        f"Fine-tuned from {args.initial_model}."
        if args.initial_model is not None
        else pretraining_note(args)
      ),
      "initial_model": str(args.initial_model) if args.initial_model is not None else None,
      "split_report": str(args.split_report) if args.split_report is not None else None,
      "train_routes": sorted(train_routes),
      "validation_routes": sorted(validation_routes),
      "holdout_routes": sorted(holdout_routes),
      "eligible_route_count": len(usable_routes),
      "excluded_route_count": len({item.route for item in trajectories} - usable_routes),
      "train_windows": len(train_windows),
      "validation_windows": len(validation_windows),
      "holdout_windows": len(holdout_windows),
      "residual_validation_coverage": validation_coverage,
      "member_reports": member_reports,
      "ensemble_validation": ensemble_validation,
      "ensemble_holdout": ensemble_holdout,
      "parameters_per_member": parameter_count(models[0]),
      "ensemble_parameters": sum(parameter_count(model) for model in models),
      "anti_exploitation": {
        "strategy": "ensemble mean with member disagreement exposed to downstream policy training",
        "selection_holdout": sorted(holdout_routes),
        "rollout_training_seconds": rollout_train_steps * config.sample_period_s,
        "rollout_training_steps": rollout_train_steps,
        "rollout_validation_seconds": args.rollout_seconds,
        "preservation_weight": args.preservation_weight,
        "residual_expert": args.residual_expert if residual_gate is not None else None,
        "max_mid_speed_regression": args.max_mid_speed_regression,
        "max_high_speed_regression": args.max_high_speed_regression,
      },
    },
  }
  if residual_gate is not None:
    artifact["residual_gate"] = asdict(residual_gate)
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
  if residual_gate is not None:
    report["residual_gate"] = artifact["residual_gate"]
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
  search.add_argument("--search-profile", choices=("initial", "temporal", "architecture"), default="initial")
  search.add_argument("--candidate-file", type=Path)
  search.set_defaults(func=run_search)
  train = subparsers.add_parser("train", parents=[common_parser()])
  train.add_argument("--name", default="selected_neural_plant")
  train.add_argument("--family", choices=("mlp", "gru", "tcn", "transformer"), default="gru")
  train.add_argument("--sample-step", type=int, default=2)
  train.add_argument("--history-steps", type=int, default=100)
  train.add_argument("--hidden-sizes", type=parse_hidden_sizes, default=(256,))
  train.add_argument("--gru-layers", type=int, default=2)
  train.add_argument("--temporal-layers", type=int, default=2)
  train.add_argument("--attention-heads", type=int, default=4)
  train.add_argument("--feedforward-size", type=int, default=256)
  train.add_argument("--dropout", type=float, default=0.10)
  train.add_argument("--ensemble-seed", type=int, action="append")
  train.set_defaults(func=run_train)
  args = parser.parse_args()
  args.max_train_windows = None if args.max_train_windows == 0 else args.max_train_windows
  args.max_validation_windows = None if args.max_validation_windows == 0 else args.max_validation_windows
  args.max_holdout_windows = None if args.max_holdout_windows == 0 else args.max_holdout_windows
  args.func(args)


if __name__ == "__main__":
  main()
