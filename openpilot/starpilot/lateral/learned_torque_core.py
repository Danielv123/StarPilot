"""Learned steering torque law for the Ioniq 5, evaluated with numpy only.

A small network, trained offline against a plant model learned from this car's
recorded drives, maps the tracking state to a change in steering torque. The
law is in velocity form: every step it moves the previous torque by a bounded
amount, so the command is continuous by construction and cannot step.

Everything here uses the torque controller's sign convention (positive torque
gives positive lateral acceleration) and has no openpilot imports, so the
offline trainer and simulator run this exact code.
"""

from __future__ import annotations

from collections import deque
import math

import numpy as np

CORE_VERSION = 1
DT = 0.01
OBSERVATION_SIZE = 18
CURVATURE_BUFFER_STEPS = 100
HISTORY_STEPS = 32
# The planner issues each curvature for one lateral delay ahead. The network is
# trained for delays in this range; a learned delay outside it is clamped.
MIN_DELAY_STEPS = 20
MAX_DELAY_STEPS = 50
PREVIEW_POINTS = 4

GRAVITY = 9.81
STEER_MAX = 409.0
STEER_DRIVER_ALLOWANCE = 100.0
STEER_DRIVER_MULTIPLIER = 2.0
LOW_SPEED_RATE_LIMIT_MPS = 15.0
# CAN-FD torque rate limits per 10 ms frame, normalised by STEER_MAX.
RATE_UP_LOW, RATE_DOWN_LOW = 10.0 / STEER_MAX, 8.0 / STEER_MAX
RATE_UP_HIGH, RATE_DOWN_HIGH = 2.0 / STEER_MAX, 3.0 / STEER_MAX


def delay_steps(delay: float) -> int:
  # The epsilon keeps a delay that is a whole number of frames from truncating to one fewer.
  return int(min(max(delay / DT + 1e-6, MIN_DELAY_STEPS), MAX_DELAY_STEPS))


def preview_offsets(steps: int) -> list[int]:
  """How many frames ago each preview target was issued, from the present setpoint to the newest."""
  return [steps - (index * steps) // PREVIEW_POINTS for index in range(PREVIEW_POINTS + 1)]


def torque_range(previous: float, driver_torque: float, speed: float) -> tuple[float, float]:
  """The torque the car will accept this frame, mirroring ``apply_driver_steer_torque_limits``.

  ``driver_torque`` is in native units and in this module's sign convention.
  """
  if speed < LOW_SPEED_RATE_LIMIT_MPS:
    up, down = RATE_UP_LOW, RATE_DOWN_LOW
  else:
    up, down = RATE_UP_HIGH, RATE_DOWN_HIGH
  driver_high = 1.0 + (STEER_DRIVER_ALLOWANCE + driver_torque) * STEER_DRIVER_MULTIPLIER / STEER_MAX
  driver_low = -1.0 + (-STEER_DRIVER_ALLOWANCE + driver_torque) * STEER_DRIVER_MULTIPLIER / STEER_MAX
  high = max(min(1.0, driver_high), 0.0)
  low = min(max(-1.0, driver_low), 0.0)
  if previous > 0.0:
    rate_low, rate_high = max(previous - down, -up), previous + up
  else:
    rate_low, rate_high = previous - up, min(previous + down, up)
  # The rate limit is applied last, so it wins over the driver window.
  return min(max(low, rate_low), rate_high), max(min(high, rate_high), rate_low)


def observation_terms(targets, y_now, y_10, y_30, u_1, u_6, u_16, u_31, speed, roll, delay, error_mean,
                      angle_now, angle_10, xp=np):
  """The network inputs, as a list of scalars or equally shaped arrays.

  ``targets`` are the preview lateral accelerations from the present setpoint
  to the newest request. ``xp`` is numpy at runtime and torch in training.
  """
  setpoint = targets[0]
  inverse_speed = xp.clip(5.0 / xp.clip(speed, 0.5, None), None, 2.0)
  return [
    xp.clip(setpoint - y_now, -3.0, 3.0),
    setpoint * 0.5,
    targets[1] - setpoint,
    targets[2] - setpoint,
    targets[3] - setpoint,
    targets[4] - setpoint,
    (y_now - y_10) * (1.0 / (10 * DT) / 3.0),
    (y_10 - y_30) * (1.0 / (20 * DT) / 3.0),
    u_1,
    (u_1 - u_6) * 5.0,
    (u_6 - u_16) * 5.0,
    (u_16 - u_31) * 5.0,
    speed * (1.0 / 20.0),
    inverse_speed,
    roll * GRAVITY,
    delay * (1.0 / 0.3),
    xp.clip(error_mean, -1.5, 1.5) * 2.0,
    xp.clip((angle_now - angle_10) * (1.0 / (10 * DT) / 30.0), -3.0, 3.0),
  ]


class LearnedTorqueCore:
  def __init__(self, weights):
    if int(weights["version"]) != CORE_VERSION:
      raise ValueError("Learned torque weights do not match this controller version")
    self.weights = [np.asarray(weights[f"w{index}"], dtype=np.float64) for index in range(3)]
    self.biases = [np.asarray(weights[f"b{index}"], dtype=np.float64) for index in range(3)]
    if (self.weights[0].shape[0] != OBSERVATION_SIZE or self.weights[2].shape[1] != 1 or
        not all(np.isfinite(array).all() for array in self.weights + self.biases)):
      raise ValueError("Learned torque weights are malformed")
    self.max_slew = float(weights["max_slew"])
    if not 0.0 < self.max_slew <= 0.05:
      raise ValueError("Learned torque slew limit is out of range")
    self.reset()

  def reset(self, y: float = 0.0, angle: float = 0.0, curvature: float = 0.0):
    self.curvature = deque([curvature] * CURVATURE_BUFFER_STEPS, maxlen=CURVATURE_BUFFER_STEPS)
    self.y = deque([y] * HISTORY_STEPS, maxlen=HISTORY_STEPS)
    self.angle = deque([angle] * HISTORY_STEPS, maxlen=HISTORY_STEPS)
    self.u = deque([0.0] * HISTORY_STEPS, maxlen=HISTORY_STEPS)
    self.error = deque([0.0] * HISTORY_STEPS, maxlen=HISTORY_STEPS)
    self.torque = 0.0
    self.setpoint = 0.0
    self.newest_target = 0.0

  def observe(self, y: float, angle: float, curvature: float, speed: float, roll: float, delay: float) -> np.ndarray:
    """Record this frame's measurements and return the network inputs."""
    self.curvature.append(curvature)
    self.y.append(y)
    self.angle.append(angle)
    steps = delay_steps(delay)
    speed_squared = speed * speed
    targets = [self.curvature[-1 - offset] * speed_squared for offset in preview_offsets(steps)]
    self.setpoint = targets[0]
    self.newest_target = targets[-1]
    self.error.append(self.setpoint - y)
    error_mean = sum(self.error[index] for index in range(-30, 0)) / 30.0
    return np.asarray(observation_terms(
      targets, y, self.y[-11], self.y[-31], self.u[-1], self.u[-6], self.u[-16], self.u[-31],
      speed, roll, steps * DT, error_mean, angle, self.angle[-11],
    ), dtype=np.float64)

  def increment(self, observation: np.ndarray) -> float:
    hidden = np.tanh(observation @ self.weights[0] + self.biases[0])
    hidden = np.tanh(hidden @ self.weights[1] + self.biases[1])
    return self.max_slew * math.tanh(float((hidden @ self.weights[2] + self.biases[2])[0]))

  def propose(self, y: float, angle: float, curvature: float, speed: float, roll: float, delay: float,
              driver_torque: float, pressed: bool) -> float:
    """Return the torque this law wants now. Call :meth:`commit` with what was actually applied."""
    observation = self.observe(y, angle, curvature, speed, roll, delay)
    low, high = torque_range(self.torque, driver_torque, speed)
    torque = min(max(self.torque + self.increment(observation), low), high)
    if pressed:
      # Never build torque against the driver; only let it relax toward zero.
      torque = min(max(torque, min(self.torque, 0.0)), max(self.torque, 0.0))
    return torque

  def commit(self, applied: float):
    self.torque = float(applied)
    self.u.append(self.torque)

  def idle(self, y: float, angle: float, curvature: float):
    """Track the car while nothing is being steered, so engagement starts from real history."""
    self.curvature.append(curvature)
    self.y.append(y)
    self.angle.append(angle)
    self.error.append(0.0)
    self.commit(0.0)
