"""Learned Ioniq 5 steering torque controller.

The StarPilot vehicle policy keeps running underneath and still owns three
jobs: it steers alone below walking-to-jogging speeds, where no drive data
exists to learn from; it is blended in across a short speed band so the
handover is seamless; and it takes over for the rest of the drive if the
learned law ever fails. Above the band the learned law steers.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from opendbc.car.hyundai.values import CAR
from openpilot.common.swaglog import cloudlog
from openpilot.starpilot.lateral.hkg_torque_policy import supported_torque_cp
from openpilot.starpilot.lateral.learned_torque_core import DT, LearnedTorqueCore, delay_steps

VEHICLES = frozenset((CAR.HYUNDAI_IONIQ_5,))
WEIGHTS = Path(__file__).with_name('learned_torque_ioniq5.npz')
# Below the first speed the StarPilot policy steers alone, above the second the learned law does.
BLEND_SPEEDS = (3.0, 6.0)
# Fraction of the gap closed per frame while the StarPilot policy's integrator shadows the learned torque.
FOLLOW_RATE = 0.05
LOG_VERSION = 3


def supported_cp(cp) -> bool:
  return supported_torque_cp(cp, VEHICLES)


def available(cp) -> bool:
  return supported_cp(cp) and WEIGHTS.is_file()


def load_core(path: Path | None = None) -> LearnedTorqueCore:
  with np.load(WEIGHTS if path is None else path, allow_pickle=False) as weights:
    return LearnedTorqueCore({name: weights[name] for name in weights.files})


class LearnedTorquePolicy:
  def __init__(self, base, parent, core: LearnedTorqueCore):
    self.base = base
    self.parent = parent
    self.core = core
    self.FACTOR_MULT = base.FACTOR_MULT
    self.failed = False
    self.sat_time = 0.0

  def __getattr__(self, name):
    # Everything the rest of the stack reads off the vehicle policy still resolves.
    if name == 'base':
      raise AttributeError(name)
    return getattr(self.base, name)

  def update(self, active, cs, vm, params, safety_limited, curvature, curvature_limited, delay):
    result = self.base.update(active, cs, vm, params, safety_limited, curvature, curvature_limited, delay)
    if self.failed:
      return result
    try:
      return self.steer(result, active, cs, vm, params, safety_limited, curvature, curvature_limited, delay)
    except Exception:
      cloudlog.exception('learned torque controller failed; the StarPilot controller steers for the rest of this drive')
      self.failed = True
      return result

  def steer(self, result, active, cs, vm, params, safety_limited, curvature, curvature_limited, delay):
    parent = self.parent
    speed = float(cs.vEgo)
    angle = float(cs.steeringAngleDeg)
    measurement = -vm.calc_curvature(math.radians(angle - params.angleOffsetDeg), speed, params.roll) * speed**2
    if not active:
      self.core.idle(measurement, angle, curvature)
      self.sat_time = 0.0
      return result

    base_torque = -float(result[0])
    previous = self.core.torque
    learned = self.core.propose(measurement, angle, curvature, speed, float(params.roll), delay,
                                -float(cs.steeringTorque), bool(cs.steeringPressed))
    weight = float(np.interp(speed, BLEND_SPEEDS, (0.0, 1.0)))
    torque = weight * learned + (1.0 - weight) * base_torque
    if not math.isfinite(torque):
      raise ValueError('non-finite learned torque')
    torque = min(max(torque, -parent.steer_max), parent.steer_max)
    self.core.commit(torque)

    if weight >= 1.0:
      # Keep the idle policy's integrator on the applied torque, so the low-speed
      # blend and a mid-drive fallback both start from where the wheel already is.
      gap = parent.lateral_accel_from_torque(torque - base_torque, parent.torque_params)
      parent.pid.i = float(np.clip(parent.pid.i + FOLLOW_RATE * gap, parent.pid.neg_limit, parent.pid.pos_limit))

    at_limit = parent.steer_max - abs(torque) < 1e-3
    if (at_limit or curvature_limited) and speed > parent.sat_check_min_speed and not safety_limited and not cs.steeringPressed:
      self.sat_time += DT
    else:
      self.sat_time -= DT
    self.sat_time = min(max(self.sat_time, 0.0), parent.sat_limit)

    log = result[2]
    log.version = LOG_VERSION
    log.error = float(self.core.setpoint - measurement)
    log.actualLateralAccel = float(measurement)
    log.desiredLateralAccel = float(self.core.setpoint)
    log.desiredLateralJerk = float((self.core.newest_target - self.core.setpoint) / (delay_steps(delay) * DT))
    # The PID slots carry this controller's own terms: the learned torque, the
    # shadowing StarPilot torque, the applied torque rate and the blend weight.
    log.p = float(learned)
    log.i = float(base_torque)
    log.d = float((torque - previous) / DT)
    log.f = weight
    log.output = float(-torque)
    log.saturated = bool(self.sat_time > parent.sat_limit - 1e-3)
    return -torque, 0.0, log
