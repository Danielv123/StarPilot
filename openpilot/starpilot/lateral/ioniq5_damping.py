"""Ioniq 5 measured-response damping for the StarPilot torque controller.

Ported from the Starpilot-20inch-ioniq5 fork. The PID derivative gain is fixed
at IONIQ_5_DAMPING_GAIN; this scales the error rate so the effective gain is
raised at highway speed and briefly softened after a wheel-rate reversal on
turn exit.
"""

import numpy as np

from openpilot.starpilot.lateral import hkg_vehicle_shaping as shaping


class Ioniq5Damping:
  def __init__(self, dt):
    self.dt = dt
    self.previous_steering_angle_deg = None
    self.previous_signed_steering_rate_deg_s = 0.0
    self.turn_exit_timer = 0.0
    self.reversal_damping_timer = 0.0

  def reset(self, steering_angle_deg):
    self.previous_steering_angle_deg = steering_angle_deg
    self.previous_signed_steering_rate_deg_s = 0.0
    self.turn_exit_timer = 0.0
    self.reversal_damping_timer = 0.0

  def steering_rate(self, steering_angle_deg):
    rate = (0.0 if self.previous_steering_angle_deg is None
            else (steering_angle_deg - self.previous_steering_angle_deg) / self.dt)
    self.previous_steering_angle_deg = steering_angle_deg
    return rate

  def error_rate(self, measurement_rate, setpoint, desired_lateral_jerk, v_ego,
                 signed_steering_rate_deg_s, steering_pressed):
    if steering_pressed:
      self.turn_exit_timer = 0.0
      self.reversal_damping_timer = 0.0
      self.previous_signed_steering_rate_deg_s = signed_steering_rate_deg_s
      return -measurement_rate

    speed_in_range = shaping.IONIQ_5_TURN_EXIT_MIN_SPEED <= v_ego < shaping.IONIQ_5_TURN_EXIT_MAX_SPEED
    unwind = (
      speed_in_range
      and abs(setpoint) >= shaping.IONIQ_5_TURN_EXIT_MIN_LAT_ACCEL
      and setpoint * desired_lateral_jerk < shaping.IONIQ_5_TURN_EXIT_JERK_PRODUCT_THRESHOLD
    )
    self.turn_exit_timer = max(self.turn_exit_timer - self.dt, 0.0)
    if unwind:
      self.turn_exit_timer = shaping.IONIQ_5_TURN_EXIT_HOLD_SECONDS
    turn_exit = (
      speed_in_range
      and (
        unwind
        or (
          self.turn_exit_timer > 0.0
          and abs(setpoint) < shaping.IONIQ_5_TURN_EXIT_NEAR_CENTER_LAT_ACCEL
        )
      )
    )
    rate_reversal = (
      turn_exit
      and signed_steering_rate_deg_s * self.previous_signed_steering_rate_deg_s < 0.0
      and min(
        abs(signed_steering_rate_deg_s),
        abs(self.previous_signed_steering_rate_deg_s),
      ) >= shaping.IONIQ_5_REVERSAL_MIN_STEERING_RATE
    )
    self.reversal_damping_timer = max(self.reversal_damping_timer - self.dt, 0.0)
    if rate_reversal:
      self.reversal_damping_timer = shaping.IONIQ_5_REVERSAL_DAMPING_HOLD_SECONDS
    self.previous_signed_steering_rate_deg_s = signed_steering_rate_deg_s

    if turn_exit and self.reversal_damping_timer > 0.0:
      return -measurement_rate * shaping.IONIQ_5_REVERSAL_DAMPING_GAIN / shaping.IONIQ_5_DAMPING_GAIN
    if turn_exit:
      return -measurement_rate
    damping_gain = np.interp(
      v_ego,
      [shaping.IONIQ_5_HIGHWAY_DAMPING_START_SPEED, shaping.IONIQ_5_HIGHWAY_DAMPING_FULL_SPEED],
      [shaping.IONIQ_5_DAMPING_GAIN, shaping.IONIQ_5_HIGHWAY_DAMPING_GAIN],
    )
    return -measurement_rate * damping_gain / shaping.IONIQ_5_DAMPING_GAIN
