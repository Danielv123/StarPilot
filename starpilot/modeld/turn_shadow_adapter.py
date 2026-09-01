"""Portable NumPy inference for the diagnostic personalized turn adapter."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np


EXPECTED_POINTS = 33
EXPECTED_FEATURES = 62
MAX_DISPLAY_RESIDUAL_M = 20.0


@dataclass(frozen=True)
class ShadowPrediction:
  valid: bool
  active: bool
  path_x: np.ndarray
  path_y: np.ndarray
  path_z: np.ndarray
  path_t: np.ndarray
  max_abs_residual: float
  status: str


def sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
      digest.update(chunk)
  return digest.hexdigest()


def _silu(value: np.ndarray) -> np.ndarray:
  clipped = np.clip(value, -60.0, 60.0)
  return value / (1.0 + np.exp(-clipped))


def model_turn(path_x: np.ndarray, path_y: np.ndarray) -> float:
  dx = np.gradient(path_x.astype(np.float64))
  dy = np.gradient(path_y.astype(np.float64))
  heading = np.unwrap(np.arctan2(dy, dx))
  return float(heading[20] - heading[0])


class TurnShadowAdapter:
  def __init__(self, artifact: str | Path):
    self.artifact = Path(artifact)
    with np.load(self.artifact, allow_pickle=False) as stored:
      self.arrays = {key: stored[key] for key in stored.files}
    if self.arrays["schema"].item() != "starpilot.turn-shadow-adapter" or int(self.arrays["schema_version"]) != 1:
      raise RuntimeError("unsupported turn-shadow adapter schema")
    self.feature_mean = self.arrays["feature_mean"].astype(np.float32)
    self.feature_std = self.arrays["feature_std"].astype(np.float32)
    self.path_indices = self.arrays["path_indices"].astype(np.int32)
    self.residual_scale = float(self.arrays["residual_scale"])
    self.checkpoint_sha256 = str(self.arrays["checkpoint_sha256"].item())
    self.artifact_sha256 = sha256(self.artifact)
    self._validate_shapes()

  def _validate_shapes(self) -> None:
    shapes = {
      "feature_mean": (EXPECTED_FEATURES,), "feature_std": (EXPECTED_FEATURES,),
      "layer0_weight": (192, EXPECTED_FEATURES), "layer0_bias": (192,),
      "layer1_weight": (192, 192), "layer1_bias": (192,),
      "layer2_weight": (6, 192), "layer2_bias": (6,),
      "bezier_basis": (EXPECTED_POINTS, 6), "path_indices": (17,),
    }
    for name, expected in shapes.items():
      if self.arrays[name].shape != expected:
        raise RuntimeError(f"{name} shape {self.arrays[name].shape} != {expected}")
    if not np.array_equal(self.path_indices, np.arange(0, EXPECTED_POINTS, 2)):
      raise RuntimeError("unexpected path feature indices")
    if np.any(~np.isfinite(self.feature_mean)) or np.any(~np.isfinite(self.feature_std)) or np.any(self.feature_std <= 0):
      raise RuntimeError("invalid feature normalization")

  def _network(self, features: np.ndarray) -> np.ndarray:
    value = (features - self.feature_mean) / self.feature_std
    value = _silu(self.arrays["layer0_weight"] @ value + self.arrays["layer0_bias"])
    value = _silu(self.arrays["layer1_weight"] @ value + self.arrays["layer1_bias"])
    return self.arrays["layer2_weight"] @ value + self.arrays["layer2_bias"]

  def predict(
    self,
    path_x: np.ndarray,
    path_y: np.ndarray,
    path_z: np.ndarray,
    path_t: np.ndarray,
    speed: float,
    left_blinker: bool,
    right_blinker: bool,
    desire_state: np.ndarray,
  ) -> ShadowPrediction:
    path_x = np.asarray(path_x, dtype=np.float32)
    path_y = np.asarray(path_y, dtype=np.float32)
    path_z = np.asarray(path_z, dtype=np.float32)
    path_t = np.asarray(path_t, dtype=np.float32)
    desire_state = np.asarray(desire_state, dtype=np.float32)
    if any(array.shape != (EXPECTED_POINTS,) for array in (path_x, path_y, path_z, path_t)) or desire_state.shape != (8,):
      return ShadowPrediction(False, False, path_x, path_y, path_z, path_t, 0.0, "shape")
    if not np.isfinite(speed) or any(np.any(~np.isfinite(array)) for array in (path_x, path_y, path_z, path_t, desire_state)):
      return ShadowPrediction(False, False, path_x, path_y, path_z, path_t, 0.0, "nonfinite")

    turn = model_turn(path_x, path_y)
    active = abs(turn) >= 0.12 or left_blinker or right_blinker or float(np.max(desire_state[1:])) > 0.10
    if not active:
      return ShadowPrediction(True, False, path_x.copy(), path_y.copy(), path_z.copy(), path_t.copy(), 0.0, "gate_off")

    features = np.concatenate((
      path_x[self.path_indices], path_y[self.path_indices], path_t[self.path_indices],
      np.asarray([speed, float(left_blinker), float(right_blinker)], dtype=np.float32), desire_state,
    )).astype(np.float32)
    controls = self._network(features)
    controls -= controls[0]
    lateral_residual = self.residual_scale * (self.arrays["bezier_basis"] @ controls)
    display_horizon = path_t <= 5.0
    if not np.any(display_horizon):
      return ShadowPrediction(False, True, path_x, path_y, path_z, path_t, 0.0, "horizon")
    max_abs_residual = float(np.max(np.abs(lateral_residual[display_horizon])))
    if np.any(~np.isfinite(lateral_residual)) or max_abs_residual > MAX_DISPLAY_RESIDUAL_M:
      return ShadowPrediction(False, True, path_x, path_y, path_z, path_t, max_abs_residual, "residual_bounds")
    return ShadowPrediction(
      True, True, path_x.copy(), path_y + lateral_residual, path_z.copy(), path_t.copy(), max_abs_residual, "active",
    )
