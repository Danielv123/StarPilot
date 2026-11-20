"""The learned Ioniq 5 steering law: selection, wiring, limits and fallback."""

import json
import math
import random
from types import SimpleNamespace

import numpy as np
import pytest
from opendbc.car.car_helpers import interfaces
from opendbc.car.hyundai.values import CAR, CarControllerParams
from opendbc.car.lateral import apply_driver_steer_torque_limits
from opendbc.car.vehicle_model import VehicleModel
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.starpilot.lateral import learned_torque_core as core
from openpilot.starpilot.lateral import learned_torque_policy as learned
from openpilot.starpilot.lateral.controller_selection import (
  ControllerMode, default_selection, parse_document, replace_mode, selection_from_bytes,
)
from openpilot.starpilot.lateral.hkg_vehicle_policy import HKGVehicleTorquePolicy
from openpilot.starpilot.lateral.torque_extension import selected_policy
from openpilot.starpilot.ui.controller_feature import CHOICES, LEARNED_CHOICE, controller_choices

DT = 0.01


def cp_for(vehicle=CAR.HYUNDAI_IONIQ_5):
  return interfaces[vehicle].get_non_essential_params(vehicle)


def build(vehicle=CAR.HYUNDAI_IONIQ_5, *, learned_policy=True, mode=ControllerMode.STARPILOT):
  cp = cp_for(vehicle)
  controller = LatControlTorque(cp.as_reader(), interfaces[vehicle](cp), DT, controller_mode=mode, learned=learned_policy)
  return cp, controller


def car_state(speed=20.0, angle=0.0, pressed=False, driver_torque=0.0):
  return SimpleNamespace(vEgo=speed, steeringAngleDeg=angle, steeringPressed=pressed, steeringTorque=driver_torque)


def drive(controller, cp, *, speed, curvature, frames, active=True, pressed=False, driver_torque=0.0):
  vm = VehicleModel(cp)
  params = SimpleNamespace(angleOffsetDeg=0.0, roll=0.0)
  outputs = []
  for _ in range(frames):
    steer, _, log = controller.update(active, car_state(speed, pressed=pressed, driver_torque=driver_torque), vm, params,
                                      False, curvature, False, 0.33)
    outputs.append((steer, log))
  return outputs


def random_weights(seed=0, scale=0.5):
  rng = np.random.default_rng(seed)
  return {
    'version': np.asarray(core.CORE_VERSION), 'max_slew': np.asarray(12.0 / core.STEER_MAX, dtype=np.float32),
    'w0': rng.normal(0, scale, (core.OBSERVATION_SIZE, 16)).astype(np.float32), 'b0': np.zeros(16, dtype=np.float32),
    'w1': rng.normal(0, scale, (16, 16)).astype(np.float32), 'b1': np.zeros(16, dtype=np.float32),
    'w2': rng.normal(0, scale, (16, 1)).astype(np.float32), 'b2': np.zeros(1, dtype=np.float32),
  }


def test_saved_choice_roundtrip_and_scope():
  cp = cp_for()
  assert not default_selection(cp).learned
  raw = replace_mode(None, cp, ControllerMode.STARPILOT, learned=True)
  assert parse_document(raw)['vehicles']['HYUNDAI_IONIQ_5'] == {'brand': 'hyundai', 'mode': 'starpilot', 'policy': 'learned'}
  selected = selection_from_bytes(cp, raw)
  assert (selected.mode, selected.learned, selected.source) == (ControllerMode.STARPILOT, True, 'saved')
  # Choosing either ordinary controller drops the learned policy again.
  for mode in (ControllerMode.STARPILOT, ControllerMode.STANDARD):
    plain = replace_mode(raw, cp, mode)
    assert 'policy' not in parse_document(plain)['vehicles']['HYUNDAI_IONIQ_5']
    assert not selection_from_bytes(cp, plain).learned
  with pytest.raises(ValueError):
    replace_mode(None, cp, ControllerMode.STANDARD, learned=True)
  with pytest.raises(ValueError):
    replace_mode(None, cp_for(CAR.KIA_EV6), ControllerMode.STARPILOT, learned=True)


@pytest.mark.parametrize('choice', [
  {'brand': 'hyundai', 'mode': 'standard', 'policy': 'learned'},
  {'brand': 'hyundai', 'mode': 'starpilot', 'policy': 'other'},
  {'brand': 'hyundai', 'mode': 'starpilot', 'policy': 'learned', 'extra': 1},
])
def test_invalid_learned_documents_fall_back_to_the_default(choice):
  raw = json.dumps({'version': 1, 'vehicles': {'HYUNDAI_IONIQ_5': choice}}).encode()
  with pytest.raises(ValueError):
    parse_document(raw)
  selected = selection_from_bytes(cp_for(), raw)
  assert (selected.source, selected.learned, selected.mode) == ('invalid', False, ControllerMode.STARPILOT)


def test_learned_policy_is_rejected_for_other_vehicles():
  raw = json.dumps({'version': 1, 'vehicles': {'KIA_EV6': {'brand': 'hyundai', 'mode': 'starpilot', 'policy': 'learned'}}}).encode()
  with pytest.raises(ValueError):
    parse_document(raw)


def test_settings_offer_the_learned_controller_only_where_it_exists():
  assert controller_choices(cp_for()) == (*CHOICES, LEARNED_CHOICE)
  assert controller_choices(cp_for(CAR.KIA_EV6)) == CHOICES
  assert controller_choices(cp_for(CAR.HYUNDAI_IONIQ_6)) == CHOICES


def test_wiring_keeps_the_vehicle_policy_underneath():
  cp, controller = build()
  policy = selected_policy(controller)
  assert isinstance(policy, learned.LearnedTorquePolicy)
  assert isinstance(policy.base, HKGVehicleTorquePolicy)
  # The StarPilot tune still shapes the torque parameters and stays reachable through the wrapper.
  assert controller.torque_params.latAccelFactor == pytest.approx(cp.lateralTuning.torque.latAccelFactor * policy.base.FACTOR_MULT)
  assert policy.FACTOR_MULT == policy.base.FACTOR_MULT
  assert policy.ioniq_5_damping is policy.base.ioniq_5_damping
  _, ordinary = build(learned_policy=False)
  assert type(selected_policy(ordinary)) is HKGVehicleTorquePolicy
  _, other = build(CAR.KIA_EV6)
  assert type(selected_policy(other)) is HKGVehicleTorquePolicy
  _, standard = build(mode=ControllerMode.STANDARD)
  assert standard.starpilot_extension is None


def test_missing_or_corrupt_weights_leave_the_starpilot_controller(tmp_path, monkeypatch):
  monkeypatch.setattr(learned, 'WEIGHTS', tmp_path / 'absent.npz')
  assert not learned.available(cp_for())
  assert controller_choices(cp_for()) == CHOICES
  bad = tmp_path / 'bad.npz'
  weights = random_weights()
  weights['version'] = np.asarray(core.CORE_VERSION + 1)
  np.savez(bad, **weights)
  with pytest.raises(ValueError):
    learned.load_core(bad)
  monkeypatch.setattr(learned, 'load_core', lambda: (_ for _ in ()).throw(ValueError('corrupt')))
  _, controller = build()
  assert type(selected_policy(controller)) is HKGVehicleTorquePolicy


def test_torque_range_matches_the_car_controller():
  cp = cp_for().as_reader()
  rng = random.Random(0)
  for _ in range(20000):
    speed = rng.choice((3.0, 14.99, 15.0, 30.0))
    limits = CarControllerParams(cp, speed)
    last = rng.randint(-409, 409)
    requested = rng.randint(-450, 450)
    driver = rng.uniform(-600, 600)
    expected = apply_driver_steer_torque_limits(requested, last, driver, limits)
    low, high = core.torque_range(last / core.STEER_MAX, driver, speed)
    mine = min(max(requested / core.STEER_MAX, low), high) * core.STEER_MAX
    assert abs(mine - expected) <= 0.5 + 1e-6, (speed, last, requested, driver, mine, expected)


def test_core_moves_within_the_rate_limits_and_never_builds_against_the_driver():
  controller = core.LearnedTorqueCore(random_weights(scale=2.0))
  rng = random.Random(1)
  for frame in range(4000):
    speed = 10.0 if frame < 2000 else 25.0
    pressed = 1500 <= frame < 1800
    previous = controller.torque
    wanted = controller.propose(rng.uniform(-2, 2), rng.uniform(-30, 30), rng.uniform(-0.01, 0.01), speed, 0.0, 0.33, 0.0, pressed)
    up, down = (core.RATE_UP_LOW, core.RATE_DOWN_LOW) if speed < 15 else (core.RATE_UP_HIGH, core.RATE_DOWN_HIGH)
    assert abs(wanted) <= 1.0
    assert abs(wanted) - abs(previous) <= up + 1e-9
    assert abs(wanted - previous) <= max(up, down) + 1e-9
    if pressed:
      assert abs(wanted) <= abs(previous) + 1e-12
      assert wanted * previous >= 0.0
    controller.commit(wanted)


def test_low_speed_is_left_to_the_starpilot_policy():
  cp, controller = build()
  _, twin = build(learned_policy=False)
  slow = drive(controller, cp, speed=2.0, curvature=0.02, frames=200)
  reference = drive(twin, cp, speed=2.0, curvature=0.02, frames=200)
  assert [steer for steer, _ in slow] == pytest.approx([steer for steer, _ in reference])
  assert slow[-1][1].f == 0.0 and slow[-1][1].version == learned.LOG_VERSION


def test_learned_law_steers_at_speed_and_logs_its_terms():
  cp, controller = build()
  outputs = drive(controller, cp, speed=20.0, curvature=0.003, frames=300)
  torques = np.array([-steer for steer, _ in outputs])
  log = outputs[-1][1]
  assert log.active and log.version == learned.LOG_VERSION and log.f == 1.0
  assert log.output == pytest.approx(outputs[-1][0])
  assert log.desiredLateralAccel == pytest.approx(0.003 * 20.0**2)
  # A left-positive request must be answered with torque in the same sense, without a step.
  assert torques[-1] > 0.02
  assert np.abs(np.diff(torques)).max() <= core.RATE_DOWN_HIGH + 1e-9
  assert all(math.isfinite(value) for value in torques)


def test_standby_integrator_follows_the_applied_torque_without_winding_up():
  cp, controller = build()
  policy = selected_policy(controller)
  fast = drive(controller, cp, speed=20.0, curvature=0.003, frames=60)
  log = fast[-1][1]
  # At speed nothing clamps the StarPilot output, so its standby torque trails the
  # still-ramping learned one by no more than the follow lag.
  assert 0.2 < log.p < 0.5
  assert log.i == pytest.approx(log.p, abs=0.15)
  # At roundabout speed the StarPilot policy clamps its own output well below what a
  # sharp turn needs, so its torque never reaches the learned one. The integrator
  # must settle on its target and stay there rather than keep ramping on that gap.
  drive(controller, cp, speed=7.0, curvature=0.03, frames=600)
  pid = controller.pid
  settled = pid.i
  wanted = controller.lateral_accel_from_torque(policy.core.torque, controller.torque_params) - (pid.p + pid.d + pid.f)
  assert settled == pytest.approx(wanted, abs=0.25)
  assert pid.neg_limit < settled < pid.pos_limit
  slow = drive(controller, cp, speed=7.0, curvature=0.03, frames=300)
  assert abs(slow[-1][1].i) < abs(slow[-1][1].p)
  assert pid.i == pytest.approx(settled, abs=0.05)


def test_disengaged_frames_reset_to_zero_torque():
  cp, controller = build()
  drive(controller, cp, speed=20.0, curvature=0.003, frames=200)
  idle = drive(controller, cp, speed=20.0, curvature=0.003, frames=5, active=False)
  assert all(steer == 0.0 and not log.active for steer, log in idle)
  assert selected_policy(controller).core.torque == 0.0
  resumed = drive(controller, cp, speed=20.0, curvature=0.003, frames=2)
  assert abs(resumed[0][0]) <= core.RATE_UP_HIGH + 1e-9


def test_a_failing_learned_law_hands_over_for_the_rest_of_the_drive():
  cp, controller = build()
  _, twin = build(learned_policy=False)
  policy = selected_policy(controller)
  drive(controller, cp, speed=20.0, curvature=0.002, frames=50)
  drive(twin, cp, speed=20.0, curvature=0.002, frames=50)
  policy.core.increment = lambda observation: float('nan')
  failed = drive(controller, cp, speed=20.0, curvature=0.002, frames=3)
  assert policy.failed
  assert all(math.isfinite(steer) for steer, _ in failed)
  assert failed[-1][1].version == 2
  policy.core.increment = lambda observation: 0.0
  assert drive(controller, cp, speed=20.0, curvature=0.002, frames=1)[0][1].version == 2
