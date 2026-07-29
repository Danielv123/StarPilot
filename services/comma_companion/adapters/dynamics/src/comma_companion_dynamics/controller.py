from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from typing import Any

from comma_companion_dynamics.contract import DynamicsContractError

BASE_LAT_ACCEL_FACTOR = 3.172929
BASE_FRICTION = 0.096019
JERK_GAIN = 0.22
KI = 0.35
MAX_LAT_JERK_UP = 2.5
MEASUREMENT_RATE_FILTER_RC = 1.0 / (2.0 * math.pi * (MAX_LAT_JERK_UP - 0.5))
LOW_SPEED_RESET_THRESHOLD = 0.3
STEER_RELEASE_I_DECAY = 0.8
UNWIND_D_DES_THRESHOLD = -1.0
UNWIND_LAT_ACCEL_NEAR_ZERO = 0.3
KP_SPEEDS = (1.0, 1.5, 2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 30.0)
KP_VALUES = (250.0, 120.0, 65.0, 30.0, 11.5, 5.5, 3.5, 2.0, 0.6)
LOW_SPEED_X = (0.0, 10.0, 20.0, 30.0)
LOW_SPEED_Y = (12.0, 10.5, 8.0, 5.0)


@dataclass(frozen=True)
class ParameterSpec:
  label: str
  category: str
  minimum: float | None = None
  maximum: float | None = None
  step: float | None = None
  advanced: bool = False
  scope: str = "runtime"


@dataclass(frozen=True)
class ControllerParameters:
  base_lat_accel_factor_mult: float = 1.36
  ff_reduction_left: float = 0.2625
  ff_reduction_right: float = 0.415
  turn_in_boost_left: float = 0.135
  turn_in_boost_right: float = 0.02
  unwind_taper_left: float = 1.15
  unwind_taper_right: float = 1.39
  turn_in_threshold_reduction_left: float = 0.125
  turn_in_threshold_reduction_right: float = 0.085
  unwind_threshold_increase_left: float = 0.28
  unwind_threshold_increase_right: float = 0.30
  turn_in_friction_boost_left: float = 0.02
  turn_in_friction_boost_right: float = 0.01
  unwind_friction_reduction_left: float = 0.42
  unwind_friction_reduction_right: float = 0.44
  friction_scale_mult: float = 1.0
  center_taper_max: float = 0.17
  center_taper_lat: float = 0.16
  center_taper_lat_width: float = 0.04
  center_taper_speed: float = 15.0
  center_taper_speed_width: float = 2.2
  sustained_turn_in_ff_boost_left: float = 0.0
  sustained_turn_in_ff_boost_right: float = 0.0
  sustained_turn_in_ff_speed: float = 13.5
  sustained_turn_in_ff_speed_width: float = 1.8
  sustained_turn_in_ff_lat_start: float = 1.10
  sustained_turn_in_ff_lat_end: float = 3.60
  sustained_turn_in_ff_lat_width: float = 0.30
  steady_high_lat_taper: float = 0.015
  steady_high_lat_start: float = 0.35
  steady_high_lat_width: float = 0.12
  steady_jerk_width: float = 0.08
  hkg_friction_threshold: bool = True
  damping_gain: float = 0.02
  turn_exit_damping_gain: float = 0.02
  turn_exit_damping_gain_right: float = 0.02
  reversal_damping_gain: float = 0.0175
  reversal_hold_seconds: float = 0.60
  steering_rate_feedback_gain: float = 0.0


CONTROLLER_KERNEL_SCHEMA = "comma-companion.ioniq5-torque-kernel"
CONTROLLER_KERNEL_SCHEMA_VERSION = 1
CONTROLLER_PARAMS_VALUE_SPACE = "raw_carparams_live_custom_pre_vehicle_multiplier"
CONTROLLER_PROFILE_EVALUATOR = "starpilot-ioniq5-controller-profile-by-source-commit"
EARLY_HISTORICAL_CONTROLLER_SOURCE_COMMIT = "6dd6c0a3d558842b91b903e1cddfaca576a69c25"
EARLY_HISTORICAL_CONTROLLER_PROFILE_ID = "starpilot-ioniq5-torque-6dd6c0a3d558-v1"
EARLY_HISTORICAL_CONTROLLER_PROFILE_SHA256 = "f02fff8adef34f524607bc0f200ad486523850a2ec576c36d664d9b369175ce8"
HISTORICAL_CONTROLLER_SOURCE_COMMIT = "2747bf037c0f284500457f1befb4f52415e3285a"
HISTORICAL_CONTROLLER_PROFILE_ID = "starpilot-ioniq5-torque-2747bf037c0f-v1"
HISTORICAL_CONTROLLER_PROFILE_SHA256 = "f8dc55e57772cd4850e37db43e494b4103384acc325627b0674a793952dd7a12"
CURRENT_CONTROLLER_SOURCE_COMMIT = "19f8c767ec0d3b6fc1000aa1effbeec625ddd753"
CURRENT_CONTROLLER_PROFILE_ID = "starpilot-ioniq5-torque-19f8c767ec0d-v1"
CURRENT_CONTROLLER_PROFILE_SHA256 = "b88e9997a04b67b8acb979ec3e5b988a2c3caf081e988f14764e56e0149a9537"


@dataclass(frozen=True)
class ReviewedControllerProfile:
  profile_id: str
  source_starpilot_commit: str
  params_sha256: str
  parameters: ControllerParameters


REVIEWED_CONTROLLER_PROFILES: dict[str, ReviewedControllerProfile] = {
  EARLY_HISTORICAL_CONTROLLER_PROFILE_ID: ReviewedControllerProfile(
    profile_id=EARLY_HISTORICAL_CONTROLLER_PROFILE_ID,
    source_starpilot_commit=EARLY_HISTORICAL_CONTROLLER_SOURCE_COMMIT,
    params_sha256=EARLY_HISTORICAL_CONTROLLER_PROFILE_SHA256,
    parameters=ControllerParameters(
      base_lat_accel_factor_mult=1.2507,
      ff_reduction_left=0.12,
      ff_reduction_right=0.22,
      turn_in_boost_left=0.1761,
      turn_in_boost_right=0.06,
      unwind_taper_left=0.76,
      unwind_taper_right=0.8885,
      turn_in_threshold_reduction_left=0.08,
      turn_in_threshold_reduction_right=0.05,
      unwind_threshold_increase_left=0.36,
      unwind_threshold_increase_right=0.38,
      turn_in_friction_boost_left=0.04,
      turn_in_friction_boost_right=0.03,
      unwind_friction_reduction_left=0.34,
      unwind_friction_reduction_right=0.34,
      friction_scale_mult=1.0,
      center_taper_max=0.2412,
      center_taper_lat=0.12,
      center_taper_lat_width=0.03,
      center_taper_speed=16.0,
      center_taper_speed_width=2.5,
      sustained_turn_in_ff_boost_left=0.0,
      sustained_turn_in_ff_boost_right=0.0,
      sustained_turn_in_ff_speed=13.5,
      sustained_turn_in_ff_speed_width=1.8,
      sustained_turn_in_ff_lat_start=1.1,
      sustained_turn_in_ff_lat_end=3.6,
      sustained_turn_in_ff_lat_width=0.3,
      steady_high_lat_taper=0.0,
      steady_high_lat_start=0.35,
      steady_high_lat_width=0.12,
      steady_jerk_width=0.08,
      hkg_friction_threshold=False,
      damping_gain=0.0,
      turn_exit_damping_gain=0.0,
      turn_exit_damping_gain_right=0.0,
      reversal_damping_gain=0.0,
      reversal_hold_seconds=0.6,
      steering_rate_feedback_gain=0.0,
    ),
  ),
  HISTORICAL_CONTROLLER_PROFILE_ID: ReviewedControllerProfile(
    profile_id=HISTORICAL_CONTROLLER_PROFILE_ID,
    source_starpilot_commit=HISTORICAL_CONTROLLER_SOURCE_COMMIT,
    params_sha256=HISTORICAL_CONTROLLER_PROFILE_SHA256,
    parameters=ControllerParameters(
      base_lat_accel_factor_mult=1.2101,
      ff_reduction_left=0.12,
      ff_reduction_right=0.22,
      turn_in_boost_left=0.14,
      turn_in_boost_right=0.06,
      unwind_taper_left=0.76,
      unwind_taper_right=0.85,
      turn_in_threshold_reduction_left=0.08,
      turn_in_threshold_reduction_right=0.05,
      unwind_threshold_increase_left=0.36,
      unwind_threshold_increase_right=0.38,
      turn_in_friction_boost_left=0.04,
      turn_in_friction_boost_right=0.03,
      unwind_friction_reduction_left=0.34,
      unwind_friction_reduction_right=0.34,
      friction_scale_mult=0.729,
      center_taper_max=0.24,
      center_taper_lat=0.12,
      center_taper_lat_width=0.03,
      center_taper_speed=16.0,
      center_taper_speed_width=2.5,
      sustained_turn_in_ff_boost_left=0.0,
      sustained_turn_in_ff_boost_right=0.0,
      sustained_turn_in_ff_speed=13.5,
      sustained_turn_in_ff_speed_width=1.8,
      sustained_turn_in_ff_lat_start=1.1,
      sustained_turn_in_ff_lat_end=3.6,
      sustained_turn_in_ff_lat_width=0.3,
      steady_high_lat_taper=0.0,
      steady_high_lat_start=0.35,
      steady_high_lat_width=0.12,
      steady_jerk_width=0.08,
      hkg_friction_threshold=False,
      damping_gain=0.0,
      turn_exit_damping_gain=0.0,
      turn_exit_damping_gain_right=0.0,
      reversal_damping_gain=0.0,
      reversal_hold_seconds=0.6,
      steering_rate_feedback_gain=0.0,
    ),
  ),
  CURRENT_CONTROLLER_PROFILE_ID: ReviewedControllerProfile(
    profile_id=CURRENT_CONTROLLER_PROFILE_ID,
    source_starpilot_commit=CURRENT_CONTROLLER_SOURCE_COMMIT,
    params_sha256=CURRENT_CONTROLLER_PROFILE_SHA256,
    parameters=ControllerParameters(),
  ),
}
CONTROLLER_PROFILE_EVALUATOR_SOURCE_SHA256 = hashlib.sha256(
  json.dumps(
    {
      "name": CONTROLLER_PROFILE_EVALUATOR,
      "version": 1,
      "car_fingerprint": "HYUNDAI_IONIQ_5",
      "profiles_by_source_commit": {
        profile.source_starpilot_commit: {
          "profile_id": profile.profile_id,
          "baseline_controller_params": asdict(profile.parameters),
          "baseline_controller_params_sha256": profile.params_sha256,
        }
        for profile in REVIEWED_CONTROLLER_PROFILES.values()
      },
    },
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
  ).encode(),
).hexdigest()


PARAMETER_SPECS: dict[str, ParameterSpec] = {
  "base_lat_accel_factor_mult": ParameterSpec("Lateral factor", "feedforward", 1.0, 1.6, 0.01),
  "ff_reduction_left": ParameterSpec("FF reduction left", "feedforward", 0.0, 0.6, 0.01),
  "ff_reduction_right": ParameterSpec("FF reduction right", "feedforward", 0.0, 0.6, 0.01),
  "turn_in_boost_left": ParameterSpec("Turn-in boost left", "phase", 0.0, 0.7, 0.01),
  "turn_in_boost_right": ParameterSpec("Turn-in boost right", "phase", 0.0, 0.7, 0.01),
  "unwind_taper_left": ParameterSpec("Unwind taper left", "phase", 0.0, 1.6, 0.01),
  "unwind_taper_right": ParameterSpec("Unwind taper right", "phase", 0.0, 1.6, 0.01),
  "turn_in_threshold_reduction_left": ParameterSpec("Threshold reduction left", "friction", 0.0, 0.4, 0.01, True),
  "turn_in_threshold_reduction_right": ParameterSpec("Threshold reduction right", "friction", 0.0, 0.4, 0.01, True),
  "unwind_threshold_increase_left": ParameterSpec("Threshold increase left", "friction", 0.0, 1.0, 0.01, True),
  "unwind_threshold_increase_right": ParameterSpec("Threshold increase right", "friction", 0.0, 1.0, 0.01, True),
  "turn_in_friction_boost_left": ParameterSpec("Friction boost left", "friction", 0.0, 0.2, 0.005, True),
  "turn_in_friction_boost_right": ParameterSpec("Friction boost right", "friction", 0.0, 0.2, 0.005, True),
  "unwind_friction_reduction_left": ParameterSpec("Friction reduction left", "friction", 0.0, 0.8, 0.01, True),
  "unwind_friction_reduction_right": ParameterSpec("Friction reduction right", "friction", 0.0, 0.8, 0.01, True),
  "friction_scale_mult": ParameterSpec("Friction scale", "friction", 0.5, 1.5, 0.01, True, "model_only"),
  "center_taper_max": ParameterSpec("Center taper", "center", 0.0, 0.5, 0.01),
  "center_taper_lat": ParameterSpec("Center lateral threshold", "center", 0.02, 0.5, 0.01, True),
  "center_taper_lat_width": ParameterSpec("Center lateral width", "center", 0.01, 0.2, 0.005, True),
  "center_taper_speed": ParameterSpec("Center speed", "center", 5.0, 30.0, 0.5, True),
  "center_taper_speed_width": ParameterSpec("Center speed width", "center", 0.5, 8.0, 0.1, True),
  "sustained_turn_in_ff_boost_left": ParameterSpec("Sustained boost left", "phase", 0.0, 0.5, 0.01, True),
  "sustained_turn_in_ff_boost_right": ParameterSpec("Sustained boost right", "phase", 0.0, 0.5, 0.01, True),
  "sustained_turn_in_ff_speed": ParameterSpec("Sustained speed", "phase", 5.0, 30.0, 0.5, True),
  "sustained_turn_in_ff_speed_width": ParameterSpec("Sustained speed width", "phase", 0.5, 8.0, 0.1, True),
  "sustained_turn_in_ff_lat_start": ParameterSpec("Sustained lateral start", "phase", 0.2, 3.0, 0.05, True),
  "sustained_turn_in_ff_lat_end": ParameterSpec("Sustained lateral end", "phase", 1.0, 6.0, 0.05, True),
  "sustained_turn_in_ff_lat_width": ParameterSpec("Sustained lateral width", "phase", 0.05, 1.0, 0.01, True),
  "steady_high_lat_taper": ParameterSpec("Steady high-lateral taper", "phase", 0.0, 0.2, 0.005, True),
  "steady_high_lat_start": ParameterSpec("Steady lateral start", "phase", 0.1, 1.5, 0.01, True),
  "steady_high_lat_width": ParameterSpec("Steady lateral width", "phase", 0.02, 0.5, 0.01, True),
  "steady_jerk_width": ParameterSpec("Steady jerk width", "phase", 0.01, 0.5, 0.01, True),
  "hkg_friction_threshold": ParameterSpec(
    "Hyundai friction threshold",
    "friction",
    advanced=True,
    scope="model_only",
  ),
  "damping_gain": ParameterSpec("Damping", "damping", 0.0, 0.1, 0.0025),
  "turn_exit_damping_gain": ParameterSpec(
    "Turn-exit damping left",
    "damping",
    0.0,
    0.1,
    0.0025,
    scope="model_only",
  ),
  "turn_exit_damping_gain_right": ParameterSpec(
    "Turn-exit damping right",
    "damping",
    0.0,
    0.1,
    0.0025,
    scope="model_only",
  ),
  "reversal_damping_gain": ParameterSpec("Reversal damping", "damping", 0.0, 0.1, 0.0025),
  "reversal_hold_seconds": ParameterSpec("Reversal hold", "damping", 0.05, 1.5, 0.05),
  "steering_rate_feedback_gain": ParameterSpec(
    "Steering-rate feedback",
    "damping",
    -0.05,
    0.05,
    0.0025,
    True,
    "model_only",
  ),
}


def parameters_from_patch(
  patch: Mapping[str, Any] | None,
  base: ControllerParameters | None = None,
) -> ControllerParameters:
  result = asdict(base or ControllerParameters())
  if patch is None:
    return ControllerParameters(**result)
  if not isinstance(patch, Mapping):
    raise DynamicsContractError("invalid_parameters", "Tuning parameters must be a JSON object.")
  unknown = sorted(set(patch) - set(PARAMETER_SPECS))
  if unknown:
    raise DynamicsContractError(
      "unknown_parameter",
      "Unknown tuning parameter.",
      {"parameters": unknown},
    )
  for name, raw in patch.items():
    default = result[name]
    spec = PARAMETER_SPECS[name]
    if isinstance(default, bool):
      if not isinstance(raw, bool):
        raise DynamicsContractError(
          "invalid_parameter",
          f"{name} must be a boolean.",
          {"parameter": name},
        )
      result[name] = raw
      continue
    if isinstance(raw, bool):
      raise DynamicsContractError(
        "invalid_parameter",
        f"{name} must be numeric.",
        {"parameter": name},
      )
    try:
      value = float(raw)
    except (TypeError, ValueError) as exc:
      raise DynamicsContractError(
        "invalid_parameter",
        f"{name} must be numeric.",
        {"parameter": name},
      ) from exc
    if not math.isfinite(value):
      raise DynamicsContractError(
        "invalid_parameter",
        f"{name} must be finite.",
        {"parameter": name},
      )
    if spec.minimum is not None and value < spec.minimum:
      raise DynamicsContractError(
        "parameter_out_of_range",
        f"{name} is below its supported replay range.",
        {"parameter": name, "minimum": spec.minimum, "value": value},
      )
    if spec.maximum is not None and value > spec.maximum:
      raise DynamicsContractError(
        "parameter_out_of_range",
        f"{name} is above its supported replay range.",
        {"parameter": name, "maximum": spec.maximum, "value": value},
      )
    result[name] = value
  parameters = ControllerParameters(**result)
  if parameters.sustained_turn_in_ff_lat_end <= parameters.sustained_turn_in_ff_lat_start:
    raise DynamicsContractError(
      "invalid_parameter_combination",
      "sustained_turn_in_ff_lat_end must exceed sustained_turn_in_ff_lat_start.",
    )
  return parameters


def parameter_schema() -> list[dict[str, Any]]:
  defaults = asdict(ControllerParameters())
  result = []
  for field in fields(ControllerParameters):
    spec = PARAMETER_SPECS[field.name]
    item: dict[str, Any] = {
      "name": field.name,
      "label": spec.label,
      "category": spec.category,
      "type": "boolean" if isinstance(defaults[field.name], bool) else "number",
      "default": defaults[field.name],
      "advanced": spec.advanced,
      "scope": spec.scope,
      "runtime_supported": spec.scope == "runtime",
    }
    if spec.minimum is not None:
      item["minimum"] = spec.minimum
    if spec.maximum is not None:
      item["maximum"] = spec.maximum
    if spec.step is not None:
      item["step"] = spec.step
    result.append(item)
  return result


def model_only_parameters(patch: Mapping[str, Any] | None) -> list[str]:
  if not isinstance(patch, Mapping):
    return []
  return sorted(name for name in patch if name in PARAMETER_SPECS and PARAMETER_SPECS[name].scope == "model_only")


@dataclass(frozen=True)
class ControllerContext:
  lateral_accel_factor: float = BASE_LAT_ACCEL_FACTOR
  lateral_accel_offset: float = 0.0
  friction: float = BASE_FRICTION
  steering_angle_deadzone_deg: float = 0.0
  steer_max: float = 1.0


@dataclass
class ControllerState:
  integral: float
  previous_actual: float
  previous_steering_rate: float
  previous_setpoint: float
  measurement_rate_filter_x: float = 0.0
  turn_exit_remaining_s: float = 0.0
  reversal_remaining_s: float = 0.0
  previous_driver_overlay: bool = False


@dataclass(frozen=True)
class ControllerStep:
  requested_output: float
  output_lateral_accel: float
  proportional: float
  integral: float
  damping: float
  feedforward: float
  factor: float
  raw_measurement_rate: float
  filtered_measurement_rate: float
  integrator_frozen: bool
  anti_windup_limited: bool
  turn_exit: bool
  reversal_recovery: bool

  @property
  def output(self) -> float:
    """Compatibility alias; the explicit API name is requested_output."""
    return self.requested_output


def _interpolate(value: float, x: Sequence[float], y: Sequence[float]) -> float:
  if value <= x[0]:
    return y[0]
  if value >= x[-1]:
    return y[-1]
  right = bisect.bisect_right(x, value)
  left = right - 1
  fraction = (value - x[left]) / (x[right] - x[left])
  return y[left] + fraction * (y[right] - y[left])


def _sigmoid(value: float) -> float:
  if value >= 0.0:
    return 1.0 / (1.0 + math.exp(-value))
  exponential = math.exp(value)
  return exponential / (1.0 + exponential)


def _side(desired: float, left: float, right: float) -> float:
  return left if desired >= 0.0 else right


def tune_terms(
  parameters: ControllerParameters,
  desired: float,
  jerk: float,
  speed: float,
  error: float,
  future_feedforward_lateral_accel: float | None = None,
  context: ControllerContext | None = None,
  lateral_accel_deadzone: float = 0.0,
  feedforward_includes_offset: bool = True,
) -> tuple[float, float]:
  context = context or ControllerContext()
  if future_feedforward_lateral_accel is None:
    future_feedforward_lateral_accel = desired
  absolute_lateral = abs(desired)
  low_speed = 1.0 / (1.0 + (max(speed, 0.0) / 12.5) ** 2)
  phase = math.tanh((desired * jerk) / 0.10)
  turn_in = max(phase, 0.0)
  unwind = max(-phase, 0.0)
  envelope = _sigmoid((absolute_lateral - 0.10) / 0.05) * _sigmoid((1.20 - absolute_lateral) / 0.30) * low_speed
  base_reduction = (
    _side(
      desired,
      parameters.ff_reduction_left,
      parameters.ff_reduction_right,
    )
    * envelope
  )
  turn_boost = 1.0 + _side(
    desired,
    parameters.turn_in_boost_left,
    parameters.turn_in_boost_right,
  ) * turn_in * (0.35 + 0.65 * low_speed)
  unwind_taper = 1.0 - _side(
    desired,
    parameters.unwind_taper_left,
    parameters.unwind_taper_right,
  ) * unwind * (0.35 + 0.65 * low_speed)
  sustained = 0.0
  if desired * jerk > 0.0:
    sustained_speed = _sigmoid(
      (max(speed, 0.0) - parameters.sustained_turn_in_ff_speed) / parameters.sustained_turn_in_ff_speed_width,
    )
    sustained_onset = _sigmoid(
      (absolute_lateral - parameters.sustained_turn_in_ff_lat_start) / parameters.sustained_turn_in_ff_lat_width,
    )
    sustained_cutoff = _sigmoid(
      (parameters.sustained_turn_in_ff_lat_end - absolute_lateral) / parameters.sustained_turn_in_ff_lat_width,
    )
    sustained = (
      _side(
        desired,
        parameters.sustained_turn_in_ff_boost_left,
        parameters.sustained_turn_in_ff_boost_right,
      )
      * sustained_speed
      * sustained_onset
      * sustained_cutoff
    )
  feedforward_scale = (1.0 + sustained) * (1.0 - base_reduction) * turn_boost * max(unwind_taper, 0.0)
  steady_weight = math.exp(-((jerk / parameters.steady_jerk_width) ** 2))
  steady_high_lateral_weight = _sigmoid(
    (absolute_lateral - parameters.steady_high_lat_start) / parameters.steady_high_lat_width,
  )
  feedforward_scale *= 1.0 - parameters.steady_high_lat_taper * steady_weight * steady_high_lateral_weight
  if desired == 0.0:
    # Runtime get_ioniq_5_ff_scale has an explicit neutral-setpoint fast path.
    feedforward_scale = 1.0

  center_speed = _sigmoid(
    (speed - parameters.center_taper_speed) / parameters.center_taper_speed_width,
  )
  center_lateral = _sigmoid(
    (parameters.center_taper_lat - absolute_lateral) / parameters.center_taper_lat_width,
  )
  center_taper = 1.0 - parameters.center_taper_max * center_speed * center_lateral
  if parameters.hkg_friction_threshold:
    base_threshold = 0.39
  else:
    base_threshold = _interpolate(
      speed,
      (1.0 * 0.44704, 20.0 * 0.44704, 75.0 * 0.44704),
      (0.16, 0.19, 0.27),
    )
  threshold_scale = (
    1.0
    - _side(
      desired,
      parameters.turn_in_threshold_reduction_left,
      parameters.turn_in_threshold_reduction_right,
    )
    * envelope
    * turn_in
  )
  threshold_scale += (
    _side(
      desired,
      parameters.unwind_threshold_increase_left,
      parameters.unwind_threshold_increase_right,
    )
    * envelope
    * unwind
  )
  threshold = base_threshold * min(max(threshold_scale, 0.86), 1.18)

  friction_scale = (
    1.0
    + _side(
      desired,
      parameters.turn_in_friction_boost_left,
      parameters.turn_in_friction_boost_right,
    )
    * envelope
    * turn_in
  )
  friction_scale -= (
    _side(
      desired,
      parameters.unwind_friction_reduction_left,
      parameters.unwind_friction_reduction_right,
    )
    * envelope
    * unwind
  )
  friction_scale = min(max(friction_scale, 0.86), 1.04)
  friction_scale = 1.0 + (friction_scale - 1.0) * center_taper
  friction_scale *= parameters.friction_scale_mult

  lateral_factor = context.lateral_accel_factor * parameters.base_lat_accel_factor_mult
  friction_error = error + JERK_GAIN * jerk
  if -lateral_accel_deadzone < friction_error < lateral_accel_deadzone:
    friction_error = 0.0
  friction_input = min(max(friction_error / threshold, -1.0), 1.0)
  friction = friction_input * context.friction * lateral_factor
  feedforward_input = future_feedforward_lateral_accel
  if not feedforward_includes_offset:
    roll_offset_fade = _interpolate(speed, (0.5, 2.5), (0.0, 1.0))
    feedforward_input -= context.lateral_accel_offset * roll_offset_fade
  feedforward = feedforward_input * feedforward_scale * center_taper + friction_scale * friction
  return feedforward, lateral_factor


def _update_phase_context(
  parameters: ControllerParameters,
  state: ControllerState,
  desired: float,
  jerk: float,
  speed: float,
  steering_rate: float,
  driver_overlay: bool,
  dt: float,
) -> tuple[bool, bool, float]:
  if driver_overlay:
    state.turn_exit_remaining_s = 0.0
    state.reversal_remaining_s = 0.0
    state.previous_steering_rate = steering_rate
    return False, False, parameters.damping_gain

  unwind = 8.0 <= speed < 15.0 and abs(desired) >= 0.12 and desired * jerk < -0.01
  state.turn_exit_remaining_s = max(state.turn_exit_remaining_s - dt, 0.0)
  if unwind:
    state.turn_exit_remaining_s = 0.75
  turn_exit = 8.0 <= speed < 15.0 and (unwind or (state.turn_exit_remaining_s > 0.0 and abs(desired) < 0.35))
  rate_reversal = (
    turn_exit
    and steering_rate * state.previous_steering_rate < 0.0
    and min(abs(steering_rate), abs(state.previous_steering_rate)) >= 0.5
  )
  state.reversal_remaining_s = max(state.reversal_remaining_s - dt, 0.0)
  if rate_reversal:
    state.reversal_remaining_s = parameters.reversal_hold_seconds
  reversal_recovery = turn_exit and state.reversal_remaining_s > 0.0
  if reversal_recovery:
    damping_gain = parameters.reversal_damping_gain
  else:
    damping_gain = parameters.damping_gain
  state.previous_steering_rate = steering_rate
  return turn_exit, reversal_recovery, damping_gain


def initialize_controller_state(
  parameters: ControllerParameters,
  history_rows: Sequence[Mapping[str, Any]],
  dt: float,
) -> ControllerState:
  if len(history_rows) < 2:
    raise DynamicsContractError(
      "insufficient_history",
      "At least two rows are required to initialize controller context.",
    )
  first = history_rows[0]
  state = ControllerState(
    integral=0.0,
    previous_actual=float(first["actual_lateral_accel"]),
    previous_steering_rate=0.0,
    previous_setpoint=float(first["desired_lateral_accel"]),
  )
  alpha = dt / (MEASUREMENT_RATE_FILTER_RC + dt)
  for row in history_rows:
    actual = float(row["actual_lateral_accel"])
    steering_rate = float(row["signed_steering_rate_deg_s"])
    desired = float(row["desired_lateral_accel"])
    driver_overlay = float(row.get("driver_overlay", 0.0)) > 0.5
    if float(row.get("lat_active", 1.0)) < 0.5:
      state.integral = 0.0
      state.previous_actual = actual
      state.previous_steering_rate = 0.0
      state.previous_setpoint = desired
      state.measurement_rate_filter_x = 0.0
      state.turn_exit_remaining_s = 0.0
      state.reversal_remaining_s = 0.0
      state.previous_driver_overlay = driver_overlay
      continue
    raw_measurement_rate = (actual - state.previous_actual) / dt
    state.measurement_rate_filter_x = (1.0 - alpha) * state.measurement_rate_filter_x + alpha * raw_measurement_rate
    _update_phase_context(
      parameters,
      state,
      desired,
      float(row["desired_lateral_jerk"]),
      float(row["v_ego"]),
      steering_rate,
      driver_overlay,
      dt,
    )
    state.integral = float(row["controller_i"])
    state.previous_actual = actual
    state.previous_setpoint = desired
    state.previous_driver_overlay = driver_overlay
  return state


def controller_step(
  parameters: ControllerParameters,
  state: ControllerState,
  observation: Mapping[str, Any],
  dt: float,
  context: ControllerContext | None = None,
) -> ControllerStep:
  context = context or ControllerContext()
  desired = float(observation["desired_lateral_accel"])
  jerk = float(observation["desired_lateral_jerk"])
  speed = float(observation["v_ego"])
  actual = float(observation["actual_lateral_accel"])
  steering_rate = float(observation["signed_steering_rate_deg_s"])
  driver_overlay = float(observation.get("driver_overlay", 0.0)) > 0.5
  factor = context.lateral_accel_factor * parameters.base_lat_accel_factor_mult
  if float(observation.get("lat_active", 1.0)) < 0.5:
    state.integral = 0.0
    state.previous_actual = actual
    state.previous_steering_rate = 0.0
    state.previous_setpoint = desired
    state.measurement_rate_filter_x = 0.0
    state.turn_exit_remaining_s = 0.0
    state.reversal_remaining_s = 0.0
    state.previous_driver_overlay = driver_overlay
    return ControllerStep(
      requested_output=0.0,
      output_lateral_accel=0.0,
      proportional=0.0,
      integral=0.0,
      damping=0.0,
      feedforward=0.0,
      factor=factor,
      raw_measurement_rate=0.0,
      filtered_measurement_rate=0.0,
      integrator_frozen=True,
      anti_windup_limited=False,
      turn_exit=False,
      reversal_recovery=False,
    )

  if state.previous_driver_overlay and not driver_overlay:
    state.integral *= STEER_RELEASE_I_DECAY
  kp = _interpolate(speed, KP_SPEEDS, KP_VALUES)
  low_speed_factor = (_interpolate(speed, LOW_SPEED_X, LOW_SPEED_Y) / max(speed, 0.3)) ** 2
  error = desired - actual
  error_low_speed = error * (1.0 + low_speed_factor / max(kp, 1e-3))
  proportional = kp * error_low_speed
  raw_measurement_rate = (actual - state.previous_actual) / dt
  alpha = dt / (MEASUREMENT_RATE_FILTER_RC + dt)
  state.measurement_rate_filter_x = (1.0 - alpha) * state.measurement_rate_filter_x + alpha * raw_measurement_rate
  filtered_measurement_rate = min(
    max(state.measurement_rate_filter_x, -MAX_LAT_JERK_UP),
    MAX_LAT_JERK_UP,
  )
  turn_exit, reversal_recovery, damping_gain = _update_phase_context(
    parameters,
    state,
    desired,
    jerk,
    speed,
    steering_rate,
    driver_overlay,
    dt,
  )
  damping = -damping_gain * filtered_measurement_rate
  steering_rate_feedback = parameters.steering_rate_feedback_gain * steering_rate if reversal_recovery else 0.0
  feedforward, factor = tune_terms(
    parameters,
    desired,
    jerk,
    speed,
    error_low_speed,
    float(observation.get("future_feedforward_lateral_accel", desired)),
    context,
    float(observation.get("lateral_accel_deadzone", 0.0)),
    bool(observation.get("feedforward_includes_offset", True)),
  )
  desired_rate = (desired - state.previous_setpoint) / dt
  unwind_detected = desired_rate < UNWIND_D_DES_THRESHOLD and abs(desired) < UNWIND_LAT_ACCEL_NEAR_ZERO
  low_speed_reset = speed < LOW_SPEED_RESET_THRESHOLD
  if low_speed_reset:
    state.integral = 0.0
  integrator_frozen = (
    bool(observation.get("integrator_frozen", False))
    or bool(observation.get("steer_limited_by_safety", False))
    or driver_overlay
    or low_speed_reset
    or unwind_detected
  )
  derivative = damping + steering_rate_feedback
  anti_windup_limited = False
  if not integrator_frozen:
    candidate_integral = state.integral + KI * dt * error_low_speed
    positive_limit = context.steer_max * factor
    negative_limit = -context.steer_max * factor
    test_control = proportional + candidate_integral + derivative + feedforward
    upper_bound = state.integral if test_control > positive_limit else positive_limit
    lower_bound = state.integral if test_control < negative_limit else negative_limit
    bounded_integral = min(max(candidate_integral, lower_bound), upper_bound)
    anti_windup_limited = not math.isclose(
      bounded_integral,
      candidate_integral,
      rel_tol=0.0,
      abs_tol=1e-15,
    )
    state.integral = bounded_integral
  output_lateral_accel = proportional + state.integral + derivative + feedforward
  output_lateral_accel = min(
    max(output_lateral_accel, -context.steer_max * factor),
    context.steer_max * factor,
  )
  requested_output = -output_lateral_accel / factor
  state.previous_actual = actual
  state.previous_setpoint = desired
  state.previous_driver_overlay = driver_overlay
  return ControllerStep(
    requested_output=requested_output,
    output_lateral_accel=output_lateral_accel,
    proportional=proportional,
    integral=state.integral,
    damping=damping,
    feedforward=feedforward,
    factor=factor,
    raw_measurement_rate=raw_measurement_rate,
    filtered_measurement_rate=filtered_measurement_rate,
    integrator_frozen=integrator_frozen,
    anti_windup_limited=anti_windup_limited,
    turn_exit=turn_exit,
    reversal_recovery=reversal_recovery,
  )
