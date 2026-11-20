"""Learned steering plant: a GRU ensemble that advances the wheel angle 10 ms.

Each member carries its own hidden state and is evaluated in one batched pass,
so tensors are shaped ``[members, batch, ...]``. The model sees the applied
torque and the vehicle state and returns the change in steering wheel angle;
lateral acceleration is then recovered with the same geometry the controller
uses, so the plant and the controller can never disagree about it.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import torch
from torch import nn

try:
  from . import vehicle
except ImportError:  # running as a script
  sys.path.insert(0, str(Path(__file__).resolve().parent))
  import vehicle

DT = 0.01
# Fixed input scales keep the exported weights independent of any dataset statistics.
ANGLE_SCALE = 20.0  # deg
RATE_SCALE = 30.0  # deg/s
LAT_ACCEL_SCALE = 2.0  # m/s^2
SPEED_SCALE = 20.0  # m/s
ACCEL_SCALE = 2.0  # m/s^2
ROLL_ACCEL_SCALE = 1.0  # m/s^2
DELTA_SCALE = 0.1  # deg per step, i.e. 10 deg/s
FEATURES = 8


def features(u, angle_rel, rate, y, v, a, roll):
  """Stack the per-step plant inputs. ``rate`` is in deg/s."""
  return torch.stack((
    u,
    angle_rel / ANGLE_SCALE,
    rate / RATE_SCALE,
    y / LAT_ACCEL_SCALE,
    v / SPEED_SCALE,
    torch.clamp(5.0 / torch.clamp(v, min=0.5), max=2.0),
    a / ACCEL_SCALE,
    roll * (vehicle.GRAVITY / ROLL_ACCEL_SCALE),
  ), dim=-1)


class EnsemblePlant(nn.Module):
  def __init__(self, members: int = 5, hidden: int = 128):
    super().__init__()
    self.members = members
    self.hidden = hidden
    scale = 1.0 / math.sqrt(hidden)
    self.w_ih = nn.Parameter(torch.empty(members, FEATURES, 3 * hidden).uniform_(-scale, scale))
    self.w_hh = nn.Parameter(torch.empty(members, hidden, 3 * hidden).uniform_(-scale, scale))
    self.b_ih = nn.Parameter(torch.zeros(members, 1, 3 * hidden))
    self.b_hh = nn.Parameter(torch.zeros(members, 1, 3 * hidden))
    self.w_1 = nn.Parameter(torch.empty(members, hidden + FEATURES, hidden).uniform_(-scale, scale))
    self.b_1 = nn.Parameter(torch.zeros(members, 1, hidden))
    self.w_2 = nn.Parameter(torch.empty(members, hidden, 1).uniform_(-scale, scale) * 0.1)
    self.b_2 = nn.Parameter(torch.zeros(members, 1, 1))

  def initial_state(self, batch: int, device=None) -> torch.Tensor:
    return torch.zeros(self.members, batch, self.hidden, device=device or self.w_ih.device, dtype=self.w_ih.dtype)

  def step(self, x: torch.Tensor, h: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Advance one step. ``x`` is ``[members, batch, FEATURES]``; returns (delta angle in deg, next state)."""
    gates_x = torch.baddbmm(self.b_ih, x, self.w_ih)
    gates_h = torch.baddbmm(self.b_hh, h, self.w_hh)
    x_r, x_z, x_n = gates_x.chunk(3, dim=-1)
    h_r, h_z, h_n = gates_h.chunk(3, dim=-1)
    reset = torch.sigmoid(x_r + h_r)
    update = torch.sigmoid(x_z + h_z)
    candidate = torch.tanh(x_n + reset * h_n)
    h = (1.0 - update) * candidate + update * h
    hidden = torch.nn.functional.silu(torch.baddbmm(self.b_1, torch.cat((h, x), dim=-1), self.w_1))
    delta = torch.baddbmm(self.b_2, hidden, self.w_2).squeeze(-1) * DELTA_SCALE
    return delta, h

  def export(self) -> dict:
    """Weights as float32 numpy arrays for the numpy simulator."""
    return {name: value.detach().cpu().numpy().astype("float32") for name, value in self.named_parameters()}


def lateral_accel(angle, offset, v, roll, steer_ratio, stiffness):
  return vehicle.lateral_accel(angle - offset, v, roll, steer_ratio, stiffness)


def rollout(model: EnsemblePlant, batch: dict, burn_in: int, *, detach_burn_in: bool = True):
  """Teacher-force ``burn_in`` steps, then predict the rest of the window open loop.

  ``batch`` holds ``[members, batch, steps]`` tensors. Returns the predicted
  angle for every step after the first, aligned with ``batch["angle"][..., 1:]``.
  """
  angle_true = batch["angle"]
  steps = angle_true.shape[-1]
  h = model.initial_state(angle_true.shape[1], angle_true.device)
  angle = angle_true[..., 0]
  previous = angle_true[..., 0]
  predictions = []
  for t in range(steps - 1):
    if t < burn_in:
      angle = angle_true[..., t]
      previous = angle_true[..., t - 1] if t > 0 else angle
    rate = (angle - previous) / DT
    y = lateral_accel(angle, batch["offset"][..., t], batch["v"][..., t], batch["roll"][..., t],
                      batch["steer_ratio"][..., t], batch["stiffness"][..., t])
    x = features(batch["u"][..., t], angle - batch["offset"][..., t], rate, y, batch["v"][..., t], batch["a"][..., t], batch["roll"][..., t])
    delta, h = model.step(x, h)
    if detach_burn_in and t == burn_in - 1:
      h = h.detach()
    previous = angle
    angle = angle + delta
    predictions.append(angle)
  return torch.stack(predictions, dim=-1)
