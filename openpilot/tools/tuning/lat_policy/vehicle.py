"""Ioniq 5 steering geometry and actuator limits shared by the offline tools.

These mirror ``opendbc.car.vehicle_model.VehicleModel`` and the Hyundai CAN-FD
``CarControllerParams`` so training, simulation and the runtime controller agree
on what "lateral acceleration" and "applied torque" mean. They work on numpy
arrays and torch tensors alike.
"""

from __future__ import annotations

import math

GRAVITY = 9.81
MASS = 2084.0
WHEELBASE = 2.9700000286102295
CENTER_TO_FRONT = 1.187999963760376
CENTER_TO_REAR = WHEELBASE - CENTER_TO_FRONT
TIRE_STIFFNESS_FRONT = 178034.46875
TIRE_STIFFNESS_REAR = 187624.125
BASE_STEER_RATIO = 14.260000228881836
# VehicleModel slip factor at stiffnessFactor == 1; it scales with 1 / stiffnessFactor.
BASE_SLIP_FACTOR = MASS * (TIRE_STIFFNESS_FRONT * CENTER_TO_FRONT - TIRE_STIFFNESS_REAR * CENTER_TO_REAR) / (
  WHEELBASE**2 * TIRE_STIFFNESS_FRONT * TIRE_STIFFNESS_REAR
)

STEER_MAX = 409
# CAN-FD torque rate limits in CAN counts per 10 ms frame, as (below, at or above) 15 m/s.
STEER_DELTA_UP = (10, 2)
STEER_DELTA_DOWN = (8, 3)
LOW_SPEED_LIMIT_MPS = 15.0
STEER_DRIVER_ALLOWANCE = 100
STEER_DRIVER_MULTIPLIER = 2
STEER_DRIVER_FACTOR = 1


def lateral_accel(steering_angle_deg, speed, roll, steer_ratio, stiffness_factor):
  """Controller-convention lateral acceleration: ``-VM.calc_curvature(...) * v**2``.

  ``steering_angle_deg`` must already have the learned angle offset removed.
  """
  slip_factor = BASE_SLIP_FACTOR / stiffness_factor
  speed_squared = speed * speed
  curvature = (steering_angle_deg * (math.pi / 180.0) / steer_ratio) / ((1.0 - slip_factor * speed_squared) * WHEELBASE)
  curvature = curvature + GRAVITY * roll / (1.0 / slip_factor - speed_squared)
  return -curvature * speed_squared


def steering_angle_deg(lat_accel, speed, roll, steer_ratio, stiffness_factor):
  """Inverse of :func:`lateral_accel` for a fixed speed and roll."""
  slip_factor = BASE_SLIP_FACTOR / stiffness_factor
  speed_squared = speed * speed
  curvature = -lat_accel / speed_squared - GRAVITY * roll / (1.0 / slip_factor - speed_squared)
  return curvature * (1.0 - slip_factor * speed_squared) * WHEELBASE * steer_ratio * (180.0 / math.pi)
