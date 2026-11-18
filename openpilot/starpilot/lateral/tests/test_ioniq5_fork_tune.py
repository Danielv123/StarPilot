"""Ioniq 5 20-inch tune ported from the Starpilot-20inch-ioniq5 fork."""

from types import SimpleNamespace
import math

import pytest
from opendbc.car.car_helpers import interfaces
from opendbc.car.hyundai.values import CAR
from opendbc.car.vehicle_model import VehicleModel
from openpilot.cereal import log
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.starpilot.lateral import hkg_vehicle_shaping as shaping
from openpilot.starpilot.lateral.controller_selection import ControllerMode
from openpilot.starpilot.lateral.ioniq5_damping import Ioniq5Damping
from openpilot.starpilot.lateral.torque_shaping import JERK_GAIN

DT = 0.01


def build(vehicle=CAR.HYUNDAI_IONIQ_5, mode=ControllerMode.STARPILOT):
  cp = interfaces[vehicle].get_non_essential_params(vehicle)
  ci = interfaces[vehicle](cp)
  return cp, LatControlTorque(cp.as_reader(), ci, DT, controller_mode=mode)


def test_controller_uses_fork_factor_and_damping_gain():
  cp, controller = build()
  assert shaping.IONIQ_5_BASE_LAT_ACCEL_FACTOR_MULT == 1.36
  assert controller.torque_params.latAccelFactor == pytest.approx(cp.lateralTuning.torque.latAccelFactor * 1.36)
  assert controller.pid._k_d[1] == [shaping.IONIQ_5_DAMPING_GAIN]
  policy = controller.starpilot_extension.policy
  assert policy.friction_jerk_gain() == shaping.IONIQ_5_FRICTION_JERK_GAIN
  assert isinstance(policy.ioniq_5_damping, Ioniq5Damping)


def test_other_hkg_profiles_and_standard_mode_are_unchanged():
  _, ev6 = build(CAR.KIA_EV6)
  policy = ev6.starpilot_extension.policy
  assert policy.ioniq_5_damping is None
  assert policy.friction_jerk_gain() == JERK_GAIN
  assert max(ev6.pid._k_d[1]) == 0
  cs = SimpleNamespace(vEgo=20.0, steeringAngleDeg=3.0, steeringPressed=False)
  assert policy.damping_error_rate(1.5, 0.5, -1.0, cs) == -1.5
  _, standard = build(mode=ControllerMode.STANDARD)
  assert standard.starpilot_extension is None
  assert max(standard.pid._k_d[1]) == 0


def test_ff_scale_shape():
  steady_left = shaping.get_ioniq_5_ff_scale(0.7, 0.0, 12.0)
  steady_right = shaping.get_ioniq_5_ff_scale(-0.7, 0.0, 12.0)
  gentle_turn_in_left = shaping.get_ioniq_5_ff_scale(0.7, 0.16, 12.0)
  assert steady_right < steady_left < 1.0
  assert steady_left < gentle_turn_in_left
  # A sustained sharp unwind keeps feedforward authority on both sides.
  for side in (1.0, -1.0):
    assert shaping.get_ioniq_5_ff_scale(side * 0.7, -side * 2.0, 5.0) > 0.1


def test_reduces_damping_only_after_turn_exit_rate_reversal():
  damping = Ioniq5Damping(DT)
  rate = 2.0
  assert damping.error_rate(rate, 0.5, -1.0, 11.0, 2.0, False) == pytest.approx(-rate)
  assert damping.error_rate(rate, 0.5, -1.0, 11.0, -2.0, False) == pytest.approx(
    -rate * shaping.IONIQ_5_REVERSAL_DAMPING_GAIN / shaping.IONIQ_5_DAMPING_GAIN)
  for _ in range(round(shaping.IONIQ_5_REVERSAL_DAMPING_HOLD_SECONDS / DT) + 1):
    recovered = damping.error_rate(rate, 0.2, 0.0, 11.0, -2.0, False)
  assert recovered == pytest.approx(-rate)


def test_increases_damping_at_highway_speed_and_resets_on_driver_input():
  damping = Ioniq5Damping(DT)
  rate = 2.0
  highway = damping.error_rate(rate, 0.5, 0.0, shaping.IONIQ_5_HIGHWAY_DAMPING_FULL_SPEED, 0.0, False)
  assert highway == pytest.approx(-rate * shaping.IONIQ_5_HIGHWAY_DAMPING_GAIN / shaping.IONIQ_5_DAMPING_GAIN)
  damping.error_rate(rate, 0.5, -1.0, 11.0, 2.0, False)
  damping.error_rate(rate, 0.5, -1.0, 11.0, -2.0, False)
  assert damping.reversal_damping_timer > 0.0
  assert damping.error_rate(rate, 0.5, -1.0, 11.0, 2.0, True) == -rate
  assert damping.turn_exit_timer == damping.reversal_damping_timer == 0.0


def test_steering_rate_tracks_angle_and_reset():
  damping = Ioniq5Damping(DT)
  assert damping.steering_rate(4.0) == 0.0
  assert damping.steering_rate(4.5) == pytest.approx(50.0)
  damping.reset(10.0)
  assert damping.steering_rate(10.0) == 0.0


def test_controller_applies_tuned_friction_jerk_gain(monkeypatch):
  cp, controller = build()
  policy = controller.starpilot_extension.policy
  monkeypatch.setattr(policy.jerk_filter, 'update', lambda _raw: 2.0)
  monkeypatch.setattr(shaping, 'get_ioniq_5_friction_jerk_deadzone', lambda *_: 0.0)
  captured = {}

  def friction(error, deadzone, threshold, cs, setpoint):
    captured['error'] = error
    return 0.0

  monkeypatch.setattr(controller, 'friction', friction)
  cs = SimpleNamespace(vEgo=20.0, steeringAngleDeg=0.0, steeringPressed=False)
  params = log.VehicleParameters.new_message(angleOffsetDeg=0.0, roll=0.0)
  # 2.0 m/s^2 setpoint keeps the shared center-chatter jerk deadzone at zero.
  _, _, state = controller.update(True, cs, VehicleModel(cp), params, False, 0.005, False, 0.2)
  assert state.active
  assert captured['error'] - state.error == pytest.approx(2.0 * shaping.IONIQ_5_FRICTION_JERK_GAIN, abs=1e-6)


@pytest.mark.parametrize('direction', [-1.0, 1.0])
def test_controller_output_is_finite_and_bounded_through_turn_and_exit(direction):
  cp, controller = build()
  vm = VehicleModel(cp)
  params = log.VehicleParameters.new_message(angleOffsetDeg=0.0, roll=0.0)
  cs = SimpleNamespace(vEgo=11.0, steeringAngleDeg=0.0, steeringPressed=False)
  controller.update(False, cs, vm, params, False, 0.0, False, 0.28)
  for step in range(400):
    curvature = direction * 0.004 * max(0.0, 1.0 - abs(step - 150) / 150)
    cs.steeringAngleDeg = -direction * 12.0 * max(0.0, 1.0 - abs(step - 170) / 150) + math.sin(step / 3.0)
    output, _, state = controller.update(True, cs, vm, params, False, curvature, False, 0.28)
    assert math.isfinite(output) and abs(output) <= controller.steer_max + 1e-6
    assert math.isfinite(state.d)
  assert state.active
