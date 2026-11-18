from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import pytest

from comma_companion_dynamics.contract import DynamicsContractError
from comma_companion_dynamics.controller import (
  CURRENT_CONTROLLER_PROFILE_ID,
  EARLY_HISTORICAL_CONTROLLER_PROFILE_ID,
  HISTORICAL_CONTROLLER_PROFILE_ID,
  MEASUREMENT_RATE_FILTER_RC,
  REVIEWED_CONTROLLER_PROFILES,
  ControllerContext,
  ControllerParameters,
  ControllerState,
  controller_step,
  initialize_controller_state,
  parameter_schema,
  parameters_from_patch,
  tune_terms,
)


def test_parameter_schema_covers_every_controller_field() -> None:
  schema = parameter_schema()
  defaults = ControllerParameters()
  assert {item["name"] for item in schema} == set(defaults.__dataclass_fields__)
  assert all("default" in item and "category" in item for item in schema)
  by_name = {item["name"]: item for item in schema}
  assert by_name["damping_gain"]["runtime_supported"]
  assert by_name["turn_exit_damping_gain"]["scope"] == "model_only"
  assert not by_name["steering_rate_feedback_gain"]["runtime_supported"]


def test_candidate_patch_is_applied_over_baseline() -> None:
  baseline = parameters_from_patch({"damping_gain": 0.03})
  candidate = parameters_from_patch({"turn_in_boost_left": 0.2}, baseline)
  assert candidate.damping_gain == 0.03
  assert candidate.turn_in_boost_left == 0.2


def _state(
  integral: float = 0.0,
  actual: float = 0.0,
  setpoint: float = 0.2,
) -> ControllerState:
  return ControllerState(
    integral=integral,
    previous_actual=actual,
    previous_steering_rate=0.0,
    previous_setpoint=setpoint,
  )


def _observation(**overrides: float | bool) -> dict[str, float | bool]:
  result: dict[str, float | bool] = {
    "desired_lateral_accel": 0.2,
    "future_feedforward_lateral_accel": 0.3,
    "desired_lateral_jerk": 0.0,
    "v_ego": 12.0,
    "actual_lateral_accel": 0.0,
    "signed_steering_rate_deg_s": 0.0,
    "lat_active": 1.0,
    "driver_overlay": 0.0,
    "steer_limited_by_safety": False,
    "integrator_frozen": False,
    "lateral_accel_deadzone": 0.0,
  }
  result.update(overrides)
  return result


@pytest.mark.parametrize(
  "desired,jerk,speed,error,expected_feedforward",
  [
    (0.25, 0.1, 12.0, 0.2, 0.4627077940194849),
    (-0.4, -0.2, 8.0, -0.3, -0.6745909523793285),
    (0.01, 0.0, 25.0, 0.05, 0.061408981201700526),
  ],
)
def test_controller_kernel_matches_reviewed_tuning_math(
  desired: float,
  jerk: float,
  speed: float,
  error: float,
  expected_feedforward: float,
) -> None:
  feedforward, factor = tune_terms(
    ControllerParameters(),
    desired,
    jerk,
    speed,
    error,
  )
  assert feedforward == pytest.approx(expected_feedforward, abs=1e-12)
  assert factor == pytest.approx(4.31518344, abs=1e-12)


def test_historical_controller_profile_is_hash_bound_and_neutralizes_new_logic() -> None:
  profile = REVIEWED_CONTROLLER_PROFILES[HISTORICAL_CONTROLLER_PROFILE_ID]
  values = asdict(profile.parameters)
  encoded = json.dumps(
    values,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
  ).encode()
  assert hashlib.sha256(encoded).hexdigest() == profile.params_sha256
  assert profile.parameters.base_lat_accel_factor_mult == 1.2101
  assert profile.parameters.friction_scale_mult == 0.729
  assert profile.parameters.hkg_friction_threshold is False
  assert profile.parameters.steady_high_lat_taper == 0.0
  assert profile.parameters.damping_gain == 0.0

  feedforward, factor = tune_terms(
    profile.parameters,
    desired=0.25,
    jerk=0.1,
    speed=12.0,
    error=0.2,
  )
  assert feedforward == pytest.approx(0.5111797396256095, abs=1e-12)
  assert factor == pytest.approx(3.8395613829, abs=1e-12)


def test_early_historical_controller_profile_is_separately_hash_bound() -> None:
  profile = REVIEWED_CONTROLLER_PROFILES[EARLY_HISTORICAL_CONTROLLER_PROFILE_ID]
  values = asdict(profile.parameters)
  encoded = json.dumps(
    values,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
  ).encode()
  assert hashlib.sha256(encoded).hexdigest() == profile.params_sha256
  assert profile.source_starpilot_commit == "6dd6c0a3d558842b91b903e1cddfaca576a69c25"
  assert profile.parameters.base_lat_accel_factor_mult == 1.2507
  assert profile.parameters.turn_in_boost_left == 0.1761
  assert profile.parameters.unwind_taper_right == 0.8885
  assert profile.parameters.friction_scale_mult == 1.0
  assert profile.parameters.center_taper_max == 0.2412
  assert profile.parameters.hkg_friction_threshold is False
  assert profile.parameters.damping_gain == 0.0

  feedforward, factor = tune_terms(
    profile.parameters,
    desired=0.25,
    jerk=0.1,
    speed=12.0,
    error=0.2,
  )
  assert feedforward == pytest.approx(0.6254148435807457, abs=1e-12)
  assert factor == pytest.approx(3.9683823003, abs=1e-12)


def test_current_controller_profile_is_separately_hash_bound() -> None:
  profile = REVIEWED_CONTROLLER_PROFILES[CURRENT_CONTROLLER_PROFILE_ID]
  values = asdict(profile.parameters)
  encoded = json.dumps(
    values,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
  ).encode()
  assert hashlib.sha256(encoded).hexdigest() == profile.params_sha256
  assert profile.source_starpilot_commit == "19f8c767ec0d3b6fc1000aa1effbeec625ddd753"
  assert profile.params_sha256 != REVIEWED_CONTROLLER_PROFILES[HISTORICAL_CONTROLLER_PROFILE_ID].params_sha256
  assert profile.parameters == ControllerParameters()


def test_feedforward_uses_future_acceleration_not_setpoint() -> None:
  parameters = ControllerParameters()
  context = ControllerContext(
    lateral_accel_factor=3.0,
    lateral_accel_offset=0.1,
    friction=0.08,
  )
  first, _ = tune_terms(
    parameters,
    desired=0.25,
    jerk=0.1,
    speed=12.0,
    error=0.2,
    future_feedforward_lateral_accel=0.1,
    context=context,
  )
  second, _ = tune_terms(
    parameters,
    desired=0.25,
    jerk=0.1,
    speed=12.0,
    error=0.2,
    future_feedforward_lateral_accel=0.5,
    context=context,
  )
  assert second > first
  assert second - first > 0.1


def test_final_future_feedforward_does_not_apply_offset_twice() -> None:
  parameters = ControllerParameters()
  with_offset_context = ControllerContext(lateral_accel_offset=0.2)
  final_feedforward, _ = tune_terms(
    parameters,
    desired=0.25,
    jerk=0.1,
    speed=12.0,
    error=0.2,
    future_feedforward_lateral_accel=0.5,
    context=with_offset_context,
    feedforward_includes_offset=True,
  )
  same_final_without_context_offset, _ = tune_terms(
    parameters,
    desired=0.25,
    jerk=0.1,
    speed=12.0,
    error=0.2,
    future_feedforward_lateral_accel=0.5,
    context=ControllerContext(lateral_accel_offset=0.0),
    feedforward_includes_offset=True,
  )
  gravity_adjusted_fallback, _ = tune_terms(
    parameters,
    desired=0.25,
    jerk=0.1,
    speed=12.0,
    error=0.2,
    future_feedforward_lateral_accel=0.5,
    context=with_offset_context,
    feedforward_includes_offset=False,
  )
  assert final_feedforward == pytest.approx(same_final_without_context_offset)
  assert gravity_adjusted_fallback < final_feedforward


def test_history_warms_runtime_first_order_measurement_rate_filter() -> None:
  history = []
  for index in range(300):
    history.append(
      {
        "controller_i": 0.01,
        "actual_lateral_accel": index * 0.01,
        "signed_steering_rate_deg_s": 1.0,
        "desired_lateral_accel": 0.2,
        "desired_lateral_jerk": 0.0,
        "v_ego": 12.0,
        "lat_active": 1.0,
        "driver_overlay": 0.0,
      }
    )
  state = initialize_controller_state(
    ControllerParameters(),
    history,
    0.01,
  )
  alpha = 0.01 / (MEASUREMENT_RATE_FILTER_RC + 0.01)
  expected = 0.0
  for index in range(300):
    raw = 0.0 if index == 0 else 1.0
    expected = (1.0 - alpha) * expected + alpha * raw
  assert state.measurement_rate_filter_x == pytest.approx(expected, abs=1e-12)
  assert state.integral == 0.01

  step = controller_step(
    ControllerParameters(),
    state,
    _observation(actual_lateral_accel=3.0),
    0.01,
  )
  expected = (1.0 - alpha) * expected + alpha * 1.0
  assert step.raw_measurement_rate == pytest.approx(1.0)
  assert step.filtered_measurement_rate == pytest.approx(expected)
  assert step.damping == pytest.approx(-0.02 * expected)


def test_integrator_freeze_and_pid_anti_windup_match_runtime_ordering() -> None:
  frozen_state = _state(integral=0.2)
  frozen = controller_step(
    ControllerParameters(),
    frozen_state,
    _observation(integrator_frozen=True),
    0.01,
  )
  assert frozen.integrator_frozen
  assert frozen.integral == pytest.approx(0.2)

  windup_state = _state(integral=0.1)
  limited = controller_step(
    ControllerParameters(),
    windup_state,
    _observation(desired_lateral_accel=3.0),
    0.01,
    ControllerContext(steer_max=0.1),
  )
  assert limited.anti_windup_limited
  assert limited.integral == pytest.approx(0.1)
  assert abs(limited.requested_output) == pytest.approx(0.1)


def test_runtime_damping_defaults_are_not_optimizer_surrogates() -> None:
  parameters = ControllerParameters()
  assert parameters.damping_gain == 0.02
  assert parameters.reversal_damping_gain == 0.0175
  assert parameters.reversal_hold_seconds == 0.60


@pytest.mark.parametrize(
  "patch,code",
  [
    ({"not_a_parameter": 1.0}, "unknown_parameter"),
    ({"damping_gain": 1.0}, "parameter_out_of_range"),
    ({"hkg_friction_threshold": 1}, "invalid_parameter"),
    ({"damping_gain": float("nan")}, "invalid_parameter"),
  ],
)
def test_invalid_parameter_patch_is_rejected(patch: dict, code: str) -> None:
  with pytest.raises(DynamicsContractError) as error:
    parameters_from_patch(patch)
  assert error.value.code == code
