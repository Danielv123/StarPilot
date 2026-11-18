from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from comma_companion_dynamics.contract import (
  BASE_FEATURES,
  DEFAULT_HORIZON_S,
  HISTORY_STEPS,
  LIVE_PARAMETERS_MAX_AGE_MS,
  LIVE_TORQUE_CADENCE_POLICY,
  LIVE_TORQUE_CADENCE_POLICY_VERSION,
  LIVE_TORQUE_MAX_AGE_MS,
  MAX_ASOF_AGE_MS,
  MAX_HORIZON_S,
  MODE,
  RECURSIVE_OBJECTIVE_HORIZON_S,
  REFERENCE_CAR_FINGERPRINT,
  SAMPLE_PERIOD_NS,
  SAMPLE_PERIOD_S,
  SAMPLE_PERIOD_US,
  STATE_FEATURES,
  TRAINER_SCHEMA,
  TRAINER_SCHEMA_VERSION,
  TRAINING_CONTRACT_VERSION,
  TRAINING_EXTRACTION_VERSION,
  DynamicsContractError,
  causal_sampling_contract,
)
from comma_companion_dynamics.controller import (
  BASE_FRICTION,
  BASE_LAT_ACCEL_FACTOR,
  CONTROLLER_KERNEL_SCHEMA,
  CONTROLLER_KERNEL_SCHEMA_VERSION,
  CONTROLLER_PARAMS_VALUE_SPACE,
  CONTROLLER_PROFILE_EVALUATOR,
  CONTROLLER_PROFILE_EVALUATOR_SOURCE_SHA256,
  PARAMETER_SPECS,
  REVIEWED_CONTROLLER_PROFILES,
  ControllerContext,
  ControllerParameters,
  controller_step,
  initialize_controller_state,
  model_only_parameters,
  parameters_from_patch,
)
from comma_companion_dynamics.plant import Plant

CAUSAL_INPUT_ALIGNMENT = "timestamp_causal_recorded_history_asof"
TELEMETRY_SCHEMA = "comma-companion.dynamics-row"
TELEMETRY_SCHEMA_VERSION = 1
CONTROLLER_I_TIMING = "post_update_asof_source_row"
APPLIED_TORQUE_SOURCE = "carOutput.actuatorsOutput.torque"
CONTROLLER_SELECTION_SOURCES = {
  "starpilotPlan.starpilotToggles",
  "versioned_initData_fallback",
}
HISTORICAL_FLM_EVALUATOR = "starpilot-flm-availability-by-source-commit"
HISTORICAL_FLM_EVALUATOR_SOURCE_SHA256 = "db63993f0a9d32b083a9e8a4a17496e366fdd35a800c7fd88c39c2a3f4ad0a0e"
CONTROLLER_SELECTION_EVALUATOR_SOURCE_SHA256 = "bdb1b78a4ec79278f0bae3650ef1cd49cc9853e7f22d9d5ee49b94a99ca89bd2"
EFFECTIVE_TORQUE_CONTEXT_PROOF_SCHEMA = "comma-companion.effective-torque-context-proof"
TORQUE_CONTEXT_EVALUATOR = "starpilot-torque-context-by-source-commit"
TORQUE_CONTEXT_EVALUATOR_SOURCE_SHA256 = "e2fd454ee0180589abfaa4cffd3fb3b7f8ebb018cee60131b2299afdeb934a24"
TORQUE_CONTEXT_EVALUATOR_IDS = {
  "2747bf037c0f284500457f1befb4f52415e3285a": "starpilot-torque-context-2747bf-v1",
  "6dd6c0a3d558842b91b903e1cddfaca576a69c25": "starpilot-torque-context-6dd6c0-v1",
}
EFFECTIVE_TORQUE_PARAMETER_SOURCES = {
  "car_params",
  "live_filtered",
  "resolved_custom",
}
HISTORICAL_NO_FLM_SOURCE_COMMITS = frozenset(
  {
    "6dd6c0a3d558842b91b903e1cddfaca576a69c25",
    "2747bf037c0f284500457f1befb4f52415e3285a",
  }
)
LIMITER_GAP_WARNING = 0.10
LIMITER_GAP_BLOCKER = 0.20
LIMITER_JUMP_WARNING = 0.08
LIMITER_JUMP_BLOCKER = 0.15

REQUIRED_NUMERIC_ROW_FIELDS = (
  "applied_torque",
  "actual_lateral_accel",
  "steering_angle_deg",
  "signed_steering_rate_deg_s",
  "steering_torque_eps",
  "v_ego",
  "a_ego",
  "desired_lateral_accel",
  "desired_lateral_jerk",
  "controller_output",
  "controller_i",
  "lat_active",
  "driver_overlay",
  "saturated",
)


def _optional_float(
  raw: Mapping[str, Any],
  names: Sequence[str],
  index: int,
) -> tuple[float | None, str | None]:
  for name in names:
    if name not in raw or raw[name] is None:
      continue
    try:
      value = float(raw[name])
    except (TypeError, ValueError) as exc:
      raise DynamicsContractError(
        "invalid_row",
        f"{name} must be numeric when supplied.",
        {"row_index": index, "field": name},
      ) from exc
    if not math.isfinite(value):
      raise DynamicsContractError(
        "nonfinite_row",
        f"{name} must be finite when supplied.",
        {"row_index": index, "field": name},
      )
    return value, name
  return None, None


def _optional_bool(raw: Mapping[str, Any], name: str, default: bool = False) -> bool:
  value = raw.get(name, default)
  if isinstance(value, bool):
    return value
  if isinstance(value, (int, float)) and math.isfinite(float(value)):
    return float(value) > 0.5
  return default


def _optional_int(
  raw: Mapping[str, Any],
  name: str,
  *,
  allow_decimal_string: bool = False,
) -> int | None:
  value = raw.get(name)
  if value is None:
    return None
  if isinstance(value, int) and not isinstance(value, bool):
    return value
  if allow_decimal_string and isinstance(value, str) and value.isdigit():
    return int(value)
  return None


def _is_exact_int(value: Any, expected: int) -> bool:
  return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _canonical_mapping_sha256(value: Any) -> str | None:
  if not isinstance(value, Mapping):
    return None
  try:
    encoded = json.dumps(
      dict(value),
      sort_keys=True,
      separators=(",", ":"),
      ensure_ascii=False,
      allow_nan=False,
    ).encode("utf-8")
  except (TypeError, ValueError):
    return None
  return hashlib.sha256(encoded).hexdigest()


def _declared_reviewed_controller_profile(
  request: Mapping[str, Any],
) -> Any:
  controller = request.get("controller_provenance")
  tuning = controller.get("tuning_provenance") if isinstance(controller, Mapping) else None
  profile = tuning.get("baseline_controller_profile") if isinstance(tuning, Mapping) else None
  profile_id = profile.get("profile_id") if isinstance(profile, Mapping) else None
  return REVIEWED_CONTROLLER_PROFILES.get(profile_id) if isinstance(profile_id, str) else None


def _effective_source_ages(
  raw: Mapping[str, Any],
) -> tuple[tuple[int | None, int | None, int | None], bool]:
  values = raw.get("effective_torque_params_source_age_us")
  names = ("factor", "offset", "friction")
  if not isinstance(values, Mapping) or set(values) != set(names):
    return (None, None, None), False
  result: list[int | None] = []
  for name in names:
    value = values[name]
    if value is None:
      result.append(None)
    elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
      result.append(value)
    else:
      return (None, None, None), False
  return (result[0], result[1], result[2]), True


def _string_tuple(
  raw: Mapping[str, Any],
  name: str,
) -> tuple[tuple[str, ...], bool]:
  values = raw.get(name)
  if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
    return (), False
  return tuple(values), True


def _effective_torque_sources(
  raw: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None]:
  values = raw.get("effective_torque_params_source")
  if not isinstance(values, Mapping):
    return None, None, None
  return (
    values.get("factor") if isinstance(values.get("factor"), str) else None,
    values.get("offset") if isinstance(values.get("offset"), str) else None,
    values.get("friction") if isinstance(values.get("friction"), str) else None,
  )


def _effective_source_matches_value(
  declared: str | None,
  actual: str | None,
) -> bool:
  if actual is None:
    return False
  if actual.startswith("live_"):
    return declared == "live_filtered"
  if actual.startswith(("base_", "torque_")):
    return declared == "car_params"
  # Direct effective fields are already the output of the versioned resolver;
  # their separate source map carries the ownership proof.
  return actual.startswith(("effective_", "lateral_accel", "friction"))


def _context_value(
  raw: Mapping[str, Any],
  index: int,
  direct_names: Sequence[str],
  live_names: Sequence[str],
  base_names: Sequence[str],
) -> tuple[float | None, str | None]:
  direct, source = _optional_float(raw, direct_names, index)
  if direct is not None:
    return direct, source
  live_torque_used = raw.get("live_torque_used") is True
  if live_torque_used:
    live, source = _optional_float(raw, live_names, index)
    if live is not None:
      return live, source
  return _optional_float(raw, base_names, index)


@dataclass(frozen=True)
class TelemetryRow:
  nominal_t_us: int
  nominal_log_mono_time_ns: int | None
  nominal_log_mono_time_decimal_string: bool
  source_t_us: int
  source_time_error_us: int
  car_state_age_us: int | None
  car_control_age_us: int | None
  car_output_age_us: int | None
  controls_state_age_us: int | None
  live_torque_age_us: int | None
  live_parameters_age_us: int | None
  live_parameters_event_valid: bool
  live_torque_event_valid: bool
  live_torque_alive: bool
  live_torque_frequency_ok: bool
  live_torque_cadence_policy: str | None
  live_torque_cadence_policy_version: int | None
  applied_torque_source: str | None
  controller_type: str | None
  controller_selection_source: str | None
  controller_selection_stateful: bool
  controller_selection_state_machine_version: int | None
  resolved_toggles_sha256: str | None
  continuous: bool
  nominal_time_present: bool
  raw_unsigned_rate_present: bool
  future_feedforward_present: bool
  future_feedforward_exact: bool
  feedforward_includes_offset: bool
  gravity_adjusted_future_lateral_accel: float | None
  live_torque_valid: bool
  live_torque_in_use: bool
  live_torque_used: bool
  live_torque_used_valid: bool
  live_torque_context_complete: bool
  effective_torque_params_exact: bool
  effective_torque_params_missing_fields: tuple[str, ...]
  effective_torque_params_missing_fields_valid: bool
  effective_torque_params_source: tuple[
    str | None,
    str | None,
    str | None,
  ]
  effective_torque_params_source_age_us: tuple[
    int | None,
    int | None,
    int | None,
  ]
  effective_torque_params_source_age_valid: bool
  effective_torque_params_source_matches_values: bool
  effective_torque_params_stateful: bool
  effective_torque_params_state_machine_version: int | None
  effective_torque_params_value_space: str | None
  vehicle_lat_accel_factor_multiplier: float | None
  baseline_controller_profile_id: str | None
  baseline_controller_params_sha256: str | None
  baseline_controller_source_starpilot_commit: str | None
  applied_torque: float
  actual_lateral_accel: float
  steering_angle_deg: float
  steering_rate_deg: float
  signed_steering_rate_deg_s: float
  steering_torque_eps: float
  v_ego: float
  a_ego: float
  desired_lateral_accel: float
  future_feedforward_lateral_accel: float
  desired_lateral_jerk: float
  controller_output: float
  controller_i: float
  lat_active: float
  driver_overlay: float
  saturated: float
  lateral_accel_factor: float | None
  lateral_accel_offset: float | None
  friction: float | None
  steering_angle_deadzone_deg: float | None
  lateral_accel_deadzone: float | None
  steer_max: float | None
  steer_limited_by_safety: bool
  integrator_frozen: bool
  integrator_freeze_exact: bool
  context_sources: tuple[str, ...]

  @classmethod
  def from_mapping(cls, raw: Any, index: int) -> TelemetryRow:
    if not isinstance(raw, Mapping):
      raise DynamicsContractError(
        "invalid_row",
        "Each telemetry row must be a JSON object.",
        {"row_index": index},
      )
    missing = [name for name in REQUIRED_NUMERIC_ROW_FIELDS if name not in raw]
    if missing:
      raise DynamicsContractError(
        "missing_row_fields",
        "A telemetry row is missing required fields.",
        {"row_index": index, "fields": missing},
      )
    nominal_time_present = "nominal_t_us" in raw
    raw_nominal_log_mono_time_ns = raw.get(
      "nominal_log_mono_time_ns",
    )
    nominal_log_mono_time_decimal_string = (
      isinstance(raw_nominal_log_mono_time_ns, str) and raw_nominal_log_mono_time_ns.isdigit()
    )
    nominal_raw = raw.get("nominal_t_us", raw.get("t_us"))
    if nominal_raw is None:
      raise DynamicsContractError(
        "missing_row_fields",
        "A telemetry row must contain nominal_t_us.",
        {"row_index": index, "fields": ["nominal_t_us"]},
      )
    missing_timestamps = [name for name in ("source_t_us", "source_time_error_us") if name not in raw]
    if missing_timestamps:
      raise DynamicsContractError(
        "missing_row_fields",
        "A telemetry row is missing causal source timestamps.",
        {
          "row_index": index,
          "fields": missing_timestamps,
        },
      )
    raw_source_t_us = raw.get("source_t_us", nominal_raw)
    raw_source_time_error_us = raw.get(
      "source_time_error_us",
      (
        raw_source_t_us - nominal_raw
        if isinstance(raw_source_t_us, int)
        and not isinstance(raw_source_t_us, bool)
        and isinstance(nominal_raw, int)
        and not isinstance(nominal_raw, bool)
        else None
      ),
    )
    if not all(
      isinstance(value, int) and not isinstance(value, bool)
      for value in (
        nominal_raw,
        raw_source_t_us,
        raw_source_time_error_us,
      )
    ):
      raise DynamicsContractError(
        "invalid_row",
        "Telemetry timestamps must be integers.",
        {"row_index": index},
      )
    nominal_t_us = nominal_raw
    source_t_us = raw_source_t_us
    source_time_error_us = raw_source_time_error_us

    values: dict[str, float] = {}
    for name in REQUIRED_NUMERIC_ROW_FIELDS:
      try:
        value = float(raw[name])
      except (TypeError, ValueError) as exc:
        raise DynamicsContractError(
          "invalid_row",
          f"{name} must be numeric.",
          {"row_index": index, "field": name},
        ) from exc
      if not math.isfinite(value):
        raise DynamicsContractError(
          "nonfinite_row",
          f"{name} must be finite.",
          {"row_index": index, "field": name},
        )
      values[name] = value

    raw_unsigned_rate_present = "steering_rate_deg" in raw and raw["steering_rate_deg"] is not None
    steering_rate, _ = _optional_float(raw, ("steering_rate_deg",), index)
    if steering_rate is None:
      steering_rate = abs(values["signed_steering_rate_deg_s"])

    feedforward, feedforward_source = _optional_float(
      raw,
      (
        "future_feedforward_lateral_accel",
        "gravity_adjusted_future_lateral_accel",
      ),
      index,
    )
    future_feedforward_present = feedforward_source == "future_feedforward_lateral_accel"
    feedforward_includes_offset = future_feedforward_present
    if feedforward is None:
      feedforward = values["desired_lateral_accel"]
    gravity_adjusted, _ = _optional_float(
      raw,
      ("gravity_adjusted_future_lateral_accel",),
      index,
    )

    lateral_accel_factor, factor_source = _context_value(
      raw,
      index,
      (
        "lateral_accel_factor",
        "effective_lateral_accel_factor",
        "effective_lat_accel_factor",
      ),
      ("live_lateral_accel_factor", "live_lat_accel_factor"),
      (
        "torque_lateral_accel_factor",
        "base_lateral_accel_factor",
        "base_lat_accel_factor",
      ),
    )
    lateral_accel_offset, offset_source = _context_value(
      raw,
      index,
      (
        "lateral_accel_offset",
        "effective_lateral_accel_offset",
        "effective_lat_accel_offset",
      ),
      ("live_lateral_accel_offset", "live_lat_accel_offset"),
      (
        "torque_lateral_accel_offset",
        "base_lateral_accel_offset",
        "base_lat_accel_offset",
      ),
    )
    friction, friction_source = _context_value(
      raw,
      index,
      ("friction", "effective_friction"),
      ("live_friction",),
      ("torque_friction", "base_friction"),
    )
    steering_deadzone, steering_deadzone_source = _optional_float(
      raw,
      (
        "steering_angle_deadzone_deg",
        "torque_steering_angle_deadzone_deg",
      ),
      index,
    )
    lateral_deadzone, lateral_deadzone_source = _optional_float(
      raw,
      ("lateral_accel_deadzone",),
      index,
    )
    steer_max, steer_max_source = _optional_float(
      raw,
      ("steer_max",),
      index,
    )
    invalid_nonnegative = {
      "lateral_accel_factor": lateral_accel_factor,
      "friction": friction,
      "steering_angle_deadzone_deg": steering_deadzone,
      "lateral_accel_deadzone": lateral_deadzone,
      "steer_max": steer_max,
    }
    invalid = [
      name
      for name, value in invalid_nonnegative.items()
      if value is not None and (value < 0.0 or (name in {"lateral_accel_factor", "steer_max"} and value == 0.0))
    ]
    if invalid:
      raise DynamicsContractError(
        "invalid_row",
        "Controller context values are outside their physical range.",
        {"row_index": index, "fields": invalid},
      )
    context_sources = tuple(
      source
      for source in (
        factor_source,
        offset_source,
        friction_source,
        steering_deadzone_source,
        lateral_deadzone_source,
        steer_max_source,
        feedforward_source,
      )
      if source is not None
    )
    live_torque_used_raw = raw.get("live_torque_used")
    live_torque_used = live_torque_used_raw is True
    live_torque_used_valid = isinstance(
      live_torque_used_raw,
      bool,
    )
    effective_torque_params_source = _effective_torque_sources(raw)
    effective_torque_params_source_matches_values = all(
      _effective_source_matches_value(declared, actual)
      for declared, actual in zip(
        effective_torque_params_source,
        (factor_source, offset_source, friction_source),
      )
    )
    live_torque_context_complete = not live_torque_used or (
      factor_source
      in {
        "lateral_accel_factor",
        "effective_lateral_accel_factor",
        "effective_lat_accel_factor",
        "live_lateral_accel_factor",
        "live_lat_accel_factor",
      }
      and offset_source
      in {
        "lateral_accel_offset",
        "effective_lateral_accel_offset",
        "effective_lat_accel_offset",
        "live_lateral_accel_offset",
        "live_lat_accel_offset",
      }
      and friction_source
      in {
        "friction",
        "effective_friction",
        "live_friction",
      }
    )
    (
      effective_torque_params_missing_fields,
      effective_torque_params_missing_fields_valid,
    ) = _string_tuple(
      raw,
      "effective_torque_params_missing_fields",
    )
    (
      effective_torque_params_source_age_us,
      effective_torque_params_source_age_valid,
    ) = _effective_source_ages(raw)
    raw_vehicle_multiplier = raw.get(
      "vehicle_lat_accel_factor_multiplier",
    )
    vehicle_multiplier = (
      float(raw_vehicle_multiplier)
      if isinstance(raw_vehicle_multiplier, (int, float))
      and not isinstance(raw_vehicle_multiplier, bool)
      and math.isfinite(float(raw_vehicle_multiplier))
      and float(raw_vehicle_multiplier) > 0.0
      else None
    )
    return cls(
      nominal_t_us=nominal_t_us,
      nominal_log_mono_time_ns=_optional_int(
        raw,
        "nominal_log_mono_time_ns",
        allow_decimal_string=True,
      ),
      nominal_log_mono_time_decimal_string=(nominal_log_mono_time_decimal_string),
      source_t_us=source_t_us,
      source_time_error_us=source_time_error_us,
      car_state_age_us=_optional_int(raw, "car_state_age_us"),
      car_control_age_us=_optional_int(raw, "car_control_age_us"),
      car_output_age_us=_optional_int(raw, "car_output_age_us"),
      controls_state_age_us=_optional_int(raw, "controls_state_age_us"),
      live_torque_age_us=_optional_int(raw, "live_torque_age_us"),
      live_parameters_age_us=_optional_int(raw, "live_parameters_age_us"),
      live_parameters_event_valid=(raw.get("live_parameters_event_valid") is True),
      live_torque_event_valid=(raw.get("live_torque_event_valid") is True),
      live_torque_alive=raw.get("live_torque_alive") is True,
      live_torque_frequency_ok=(raw.get("live_torque_frequency_ok") is True),
      live_torque_cadence_policy=(
        raw.get("live_torque_cadence_policy") if isinstance(raw.get("live_torque_cadence_policy"), str) else None
      ),
      live_torque_cadence_policy_version=_optional_int(
        raw,
        "live_torque_cadence_policy_version",
      ),
      applied_torque_source=(
        str(raw["applied_torque_source"]) if raw.get("applied_torque_source") is not None else None
      ),
      controller_type=(raw.get("controller_type") if isinstance(raw.get("controller_type"), str) else None),
      controller_selection_source=(
        raw.get("controller_selection_source")
        if isinstance(
          raw.get("controller_selection_source"),
          str,
        )
        else None
      ),
      controller_selection_stateful=(raw.get("controller_selection_stateful") is True),
      controller_selection_state_machine_version=_optional_int(
        raw,
        "controller_selection_state_machine_version",
      ),
      resolved_toggles_sha256=(
        raw.get("resolved_toggles_sha256") if isinstance(raw.get("resolved_toggles_sha256"), str) else None
      ),
      continuous=raw.get("continuous") is True,
      nominal_time_present=nominal_time_present,
      raw_unsigned_rate_present=raw_unsigned_rate_present,
      future_feedforward_present=future_feedforward_present,
      future_feedforward_exact=(raw.get("future_feedforward_exact") is True),
      feedforward_includes_offset=feedforward_includes_offset,
      gravity_adjusted_future_lateral_accel=gravity_adjusted,
      live_torque_valid=raw.get("live_torque_valid") is True,
      live_torque_in_use=raw.get("live_torque_in_use") is True,
      live_torque_used=live_torque_used,
      live_torque_used_valid=live_torque_used_valid,
      live_torque_context_complete=live_torque_context_complete,
      effective_torque_params_exact=(raw.get("effective_torque_params_exact") is True),
      effective_torque_params_missing_fields=(effective_torque_params_missing_fields),
      effective_torque_params_missing_fields_valid=(effective_torque_params_missing_fields_valid),
      effective_torque_params_source=effective_torque_params_source,
      effective_torque_params_source_age_us=(effective_torque_params_source_age_us),
      effective_torque_params_source_age_valid=(effective_torque_params_source_age_valid),
      effective_torque_params_source_matches_values=(effective_torque_params_source_matches_values),
      effective_torque_params_stateful=(raw.get("effective_torque_params_stateful") is True),
      effective_torque_params_state_machine_version=_optional_int(
        raw,
        "effective_torque_params_state_machine_version",
      ),
      effective_torque_params_value_space=(
        raw.get("effective_torque_params_value_space")
        if isinstance(
          raw.get("effective_torque_params_value_space"),
          str,
        )
        else None
      ),
      vehicle_lat_accel_factor_multiplier=vehicle_multiplier,
      baseline_controller_profile_id=(
        raw.get("baseline_controller_profile_id")
        if isinstance(
          raw.get("baseline_controller_profile_id"),
          str,
        )
        else None
      ),
      baseline_controller_params_sha256=(
        raw.get("baseline_controller_params_sha256")
        if isinstance(
          raw.get("baseline_controller_params_sha256"),
          str,
        )
        else None
      ),
      baseline_controller_source_starpilot_commit=(
        raw.get("baseline_controller_source_starpilot_commit")
        if isinstance(
          raw.get("baseline_controller_source_starpilot_commit"),
          str,
        )
        else None
      ),
      steering_rate_deg=steering_rate,
      future_feedforward_lateral_accel=feedforward,
      lateral_accel_factor=lateral_accel_factor,
      lateral_accel_offset=lateral_accel_offset,
      friction=friction,
      steering_angle_deadzone_deg=steering_deadzone,
      lateral_accel_deadzone=lateral_deadzone,
      steer_max=steer_max,
      steer_limited_by_safety=_optional_bool(raw, "steer_limited_by_safety"),
      integrator_frozen=_optional_bool(raw, "integrator_frozen"),
      integrator_freeze_exact=(raw.get("integrator_freeze_exact") is True),
      context_sources=context_sources,
      **values,
    )

  @property
  def t_us(self) -> int:
    """Compatibility accessor for existing response helpers."""
    return self.nominal_t_us

  def features(self) -> np.ndarray:
    return np.asarray([getattr(self, name) for name in BASE_FEATURES], dtype=np.float64)

  def controller_observation(
    self,
    state_values: np.ndarray | None = None,
  ) -> dict[str, float | bool]:
    values: dict[str, float | bool] = {
      "desired_lateral_accel": self.desired_lateral_accel,
      "future_feedforward_lateral_accel": self.future_feedforward_lateral_accel,
      "desired_lateral_jerk": self.desired_lateral_jerk,
      "v_ego": self.v_ego,
      "actual_lateral_accel": self.actual_lateral_accel,
      "signed_steering_rate_deg_s": self.signed_steering_rate_deg_s,
      "lateral_accel_deadzone": self.lateral_accel_deadzone or 0.0,
      "feedforward_includes_offset": self.feedforward_includes_offset,
      "lat_active": self.lat_active,
      "driver_overlay": self.driver_overlay,
      "steer_limited_by_safety": self.steer_limited_by_safety,
      "integrator_frozen": self.integrator_frozen,
    }
    if state_values is not None:
      values["actual_lateral_accel"] = float(
        state_values[STATE_FEATURES.index("actual_lateral_accel")],
      )
      values["signed_steering_rate_deg_s"] = float(
        state_values[STATE_FEATURES.index("signed_steering_rate_deg_s")],
      )
    return values

  def as_controller_mapping(self) -> dict[str, float | bool]:
    return {
      "controller_i": self.controller_i,
      "actual_lateral_accel": self.actual_lateral_accel,
      "signed_steering_rate_deg_s": self.signed_steering_rate_deg_s,
      "desired_lateral_accel": self.desired_lateral_accel,
      "desired_lateral_jerk": self.desired_lateral_jerk,
      "v_ego": self.v_ego,
      "lat_active": self.lat_active,
      "driver_overlay": self.driver_overlay,
    }


def _warning(
  code: str,
  severity: str,
  message: str,
  selected: Sequence[TelemetryRow] | None = None,
  **details: Any,
) -> dict[str, Any]:
  result: dict[str, Any] = {
    "code": code,
    "severity": severity,
    "message": message,
    **details,
  }
  if selected:
    result.update(
      {
        "sample_count": len(selected),
        "first_t_us": selected[0].nominal_t_us,
        "last_t_us": selected[-1].nominal_t_us,
      }
    )
  return result


def _parse_rows(raw_rows: Any) -> list[TelemetryRow]:
  if not isinstance(raw_rows, list):
    raise DynamicsContractError("invalid_rows", "rows must be a JSON array.")
  if len(raw_rows) > 10_000:
    raise DynamicsContractError(
      "too_many_rows",
      "A replay request may contain at most 10,000 rows.",
      {"row_count": len(raw_rows)},
    )
  return [TelemetryRow.from_mapping(raw, index) for index, raw in enumerate(raw_rows)]


def _resolve_anchor(request: Mapping[str, Any], rows: Sequence[TelemetryRow]) -> int:
  anchor_index_raw = request.get("anchor_index")
  anchor_t_us_raw = request.get("anchor_t_us", request.get("anchor_nominal_t_us"))
  if anchor_index_raw is None and anchor_t_us_raw is None:
    return HISTORY_STEPS
  if anchor_index_raw is not None:
    try:
      anchor_index = int(anchor_index_raw)
    except (TypeError, ValueError) as exc:
      raise DynamicsContractError("invalid_anchor", "anchor_index must be an integer.") from exc
    if anchor_t_us_raw is not None:
      try:
        anchor_t_us = int(anchor_t_us_raw)
      except (TypeError, ValueError) as exc:
        raise DynamicsContractError("invalid_anchor", "anchor_t_us must be an integer.") from exc
      if not (0 <= anchor_index < len(rows)) or rows[anchor_index].nominal_t_us != anchor_t_us:
        raise DynamicsContractError(
          "anchor_mismatch",
          "anchor_index and anchor_t_us do not identify the same nominal row.",
        )
    return anchor_index
  try:
    anchor_t_us = int(anchor_t_us_raw)
  except (TypeError, ValueError) as exc:
    raise DynamicsContractError("invalid_anchor", "anchor_t_us must be an integer.") from exc
  matches = [index for index, row in enumerate(rows) if row.nominal_t_us == anchor_t_us]
  if not matches:
    raise DynamicsContractError(
      "anchor_not_found",
      "anchor_t_us does not match a supplied nominal telemetry row.",
      {"anchor_t_us": anchor_t_us},
    )
  return matches[0]


def _horizon_steps(request: Mapping[str, Any]) -> tuple[float, int]:
  raw = request.get("horizon_s", DEFAULT_HORIZON_S)
  if isinstance(raw, bool):
    raise DynamicsContractError("invalid_horizon", "horizon_s must be numeric.")
  try:
    horizon = float(raw)
  except (TypeError, ValueError) as exc:
    raise DynamicsContractError("invalid_horizon", "horizon_s must be numeric.") from exc
  if not math.isfinite(horizon) or horizon <= 0.0 or horizon > MAX_HORIZON_S:
    raise DynamicsContractError(
      "invalid_horizon",
      f"horizon_s must be greater than zero and no more than {MAX_HORIZON_S:.1f}.",
      {"maximum": MAX_HORIZON_S, "value": raw},
    )
  steps = max(1, round(horizon / SAMPLE_PERIOD_S))
  return steps * SAMPLE_PERIOD_S, steps


def _request_context(request: Mapping[str, Any]) -> Mapping[str, Any]:
  raw = request.get("controller_context", {})
  if raw is None:
    return {}
  if not isinstance(raw, Mapping):
    raise DynamicsContractError(
      "invalid_controller_context",
      "controller_context must be a JSON object.",
    )
  return raw


def _float_from_context(
  row_value: float | None,
  request_context: Mapping[str, Any],
  name: str,
  fallback: float,
) -> float:
  if row_value is not None:
    return row_value
  raw = request_context.get(name)
  if raw is None:
    return fallback
  try:
    value = float(raw)
  except (TypeError, ValueError) as exc:
    raise DynamicsContractError(
      "invalid_controller_context",
      f"controller_context.{name} must be numeric.",
    ) from exc
  if not math.isfinite(value):
    raise DynamicsContractError(
      "invalid_controller_context",
      f"controller_context.{name} must be finite.",
    )
  return value


def _controller_context(
  row: TelemetryRow,
  request_context: Mapping[str, Any],
) -> ControllerContext:
  context = ControllerContext(
    lateral_accel_factor=_float_from_context(
      row.lateral_accel_factor,
      request_context,
      "lateral_accel_factor",
      BASE_LAT_ACCEL_FACTOR,
    ),
    lateral_accel_offset=_float_from_context(
      row.lateral_accel_offset,
      request_context,
      "lateral_accel_offset",
      0.0,
    ),
    friction=_float_from_context(
      row.friction,
      request_context,
      "friction",
      BASE_FRICTION,
    ),
    steering_angle_deadzone_deg=_float_from_context(
      row.steering_angle_deadzone_deg,
      request_context,
      "steering_angle_deadzone_deg",
      0.0,
    ),
    steer_max=_float_from_context(
      row.steer_max,
      request_context,
      "steer_max",
      1.0,
    ),
  )
  if (
    context.lateral_accel_factor <= 0.0
    or context.friction < 0.0
    or context.steering_angle_deadzone_deg < 0.0
    or context.steer_max <= 0.0
  ):
    raise DynamicsContractError(
      "invalid_controller_context",
      "Controller context values are outside their physical range.",
    )
  return context


def _context_missing(
  rows: Sequence[TelemetryRow],
  request_context: Mapping[str, Any],
) -> list[str]:
  fields = {
    "lateral_accel_factor": "lateral_accel_factor",
    "lateral_accel_offset": "lateral_accel_offset",
    "friction": "friction",
    "steering_angle_deadzone_deg": "steering_angle_deadzone_deg",
  }
  missing = []
  for attribute, request_name in fields.items():
    if request_name in request_context:
      continue
    if any(getattr(row, attribute) is None for row in rows):
      missing.append(request_name)
  deadzone_missing = any(
    row.lateral_accel_deadzone is None
    and (row.steering_angle_deadzone_deg or request_context.get("steering_angle_deadzone_deg", 0.0)) != 0.0
    for row in rows
  )
  if deadzone_missing and "lateral_accel_deadzone" not in request_context:
    missing.append("lateral_accel_deadzone")
  return sorted(set(missing))


def _controller_provenance_warnings(
  request: Mapping[str, Any],
  rows: Sequence[TelemetryRow],
) -> list[dict[str, Any]]:
  raw = request.get("controller_provenance")
  if not isinstance(raw, Mapping):
    return [
      _warning(
        "controller_provenance_missing",
        "blocker",
        "Controller type, full CarParams provenance, and toggle/tuning snapshot are required for baseline fidelity.",
      )
    ]
  missing = []
  if not raw.get("controller_type"):
    missing.append("controller_type")
  if raw.get("controller_type_verified") is not True:
    missing.append("controller_type_verified")
  car_params = raw.get("car_params")
  car_params_provenance = raw.get("car_params_provenance")
  if not isinstance(car_params, Mapping):
    missing.append("car_params")
  else:
    if car_params.get("car_fingerprint") != request.get(
      "car_fingerprint",
    ):
      missing.append("car_params.car_fingerprint")
    if car_params.get("lateral_tuning_type") != "torque":
      missing.append("car_params.lateral_tuning_type")
  car_params_sha256 = raw.get("car_params_sha256")
  if not _sha256_digest(car_params_sha256):
    missing.append("car_params_sha256")
  if not isinstance(car_params_provenance, Mapping):
    missing.append("car_params_provenance")
  else:
    for field in ("wire_sha256", "summary_sha256"):
      if not _sha256_digest(car_params_provenance.get(field)):
        missing.append(f"car_params_provenance.{field}")
    if _sha256_digest(car_params_sha256) and car_params_sha256 != car_params_provenance.get("wire_sha256"):
      missing.append("car_params_sha256_wire_binding")
    if not car_params_provenance.get("source_starpilot_commit"):
      missing.append(
        "car_params_provenance.source_starpilot_commit",
      )
  if raw.get("car_params_complete") is not True:
    missing.append("car_params_complete")
  if not isinstance(raw.get("toggle_snapshot"), Mapping):
    missing.append("toggle_snapshot")
  tuning_values = raw.get("tuning_snapshot")
  if not isinstance(tuning_values, Mapping):
    missing.append("tuning_snapshot")
  else:
    required_tuning_fields = {
      "lat_accel_factor",
      "lat_accel_offset",
      "friction",
      "steering_angle_deadzone_deg",
    }
    if not required_tuning_fields.issubset(tuning_values):
      missing.append("tuning_snapshot.required_fields")
    elif any(
      not isinstance(tuning_values[field], (int, float))
      or isinstance(tuning_values[field], bool)
      or not math.isfinite(float(tuning_values[field]))
      for field in required_tuning_fields
    ):
      missing.append("tuning_snapshot.finite_values")
    elif (
      float(tuning_values["lat_accel_factor"]) <= 0.0
      or float(tuning_values["friction"]) < 0.0
      or float(tuning_values["steering_angle_deadzone_deg"]) < 0.0
    ):
      missing.append("tuning_snapshot.physical_range")
    if isinstance(car_params, Mapping) and car_params.get("lateral_torque_tuning") != tuning_values:
      missing.append("tuning_snapshot.car_params_binding")
  if raw.get("tuning_snapshot_complete") is not True:
    missing.append("tuning_snapshot_complete")
  tuning_snapshot = raw.get("toggle_snapshot", raw.get("tuning_snapshot"))
  unsupported_overrides: dict[str, Any] = {}
  selection_sources: Any = None
  resolved_hashes: Any = None
  if isinstance(tuning_snapshot, Mapping):
    if tuning_snapshot.get("flm_active") is not False:
      if tuning_snapshot.get("flm_active") is True:
        unsupported_overrides["flm_active"] = True
      else:
        missing.append("toggle_snapshot.flm_active")
    if tuning_snapshot.get("flm_active_available") is not True:
      missing.append("toggle_snapshot.flm_active_available")
    selection_sources = tuning_snapshot.get(
      "controller_selection_source_types",
    )
    if (
      not isinstance(selection_sources, list)
      or not selection_sources
      or any(source not in CONTROLLER_SELECTION_SOURCES for source in selection_sources)
    ):
      missing.append(
        "toggle_snapshot.controller_selection_source_types",
      )
    resolved_hashes = tuning_snapshot.get(
      "resolved_toggles_sha256",
    )
    if (
      not isinstance(resolved_hashes, list)
      or not resolved_hashes
      or any(not _sha256_digest(value) for value in resolved_hashes)
    ):
      missing.append("toggle_snapshot.resolved_toggles_sha256")
    flm_resolution = tuning_snapshot.get("flm_resolution")
    fallback_selected = isinstance(selection_sources, list) and "versioned_initData_fallback" in selection_sources
    fallback_evaluator = tuning_snapshot.get(
      "init_data_fallback_evaluator",
    )
    if flm_resolution is not None or fallback_selected:
      evaluator = flm_resolution.get("evaluator") if isinstance(flm_resolution, Mapping) else None
      evaluator_source_commit = evaluator.get("source_commit") if isinstance(evaluator, Mapping) else None
      if not (
        isinstance(flm_resolution, Mapping)
        and flm_resolution.get("state") == "verified"
        and flm_resolution.get("source") == "versioned_source_commit_evaluator"
        and flm_resolution.get("flm_active") is False
        and isinstance(evaluator, Mapping)
        and evaluator.get("name") == HISTORICAL_FLM_EVALUATOR
        and _is_exact_int(evaluator.get("version"), 1)
        and evaluator_source_commit in HISTORICAL_NO_FLM_SOURCE_COMMITS
        and evaluator.get("source_sha256") == HISTORICAL_FLM_EVALUATOR_SOURCE_SHA256
        and isinstance(car_params_provenance, Mapping)
        and car_params_provenance.get("source_starpilot_commit") == evaluator_source_commit
      ):
        missing.append("toggle_snapshot.flm_resolution")
    if fallback_selected:
      fallback_source_commit = (
        fallback_evaluator.get("source_commit") if isinstance(fallback_evaluator, Mapping) else None
      )
      if not (
        isinstance(fallback_evaluator, Mapping)
        and fallback_evaluator.get("state") == "available"
        and fallback_evaluator.get("name") == TORQUE_CONTEXT_EVALUATOR
        and _is_exact_int(fallback_evaluator.get("version"), 1)
        and fallback_source_commit in TORQUE_CONTEXT_EVALUATOR_IDS
        and fallback_evaluator.get("evaluator_id") == TORQUE_CONTEXT_EVALUATOR_IDS[fallback_source_commit]
        and fallback_evaluator.get("source_sha256") == TORQUE_CONTEXT_EVALUATOR_SOURCE_SHA256
        and isinstance(car_params_provenance, Mapping)
        and car_params_provenance.get("source_starpilot_commit") == fallback_source_commit
      ):
        missing.append(
          "toggle_snapshot.init_data_fallback_evaluator",
        )
    if "trailer_load_kg" not in tuning_snapshot:
      missing.append("toggle_snapshot.trailer_load_kg")
    else:
      try:
        trailer_load_kg = float(tuning_snapshot["trailer_load_kg"])
      except (TypeError, ValueError):
        missing.append("toggle_snapshot.trailer_load_kg")
      else:
        if not math.isfinite(trailer_load_kg) or trailer_load_kg < 0.0:
          missing.append("toggle_snapshot.trailer_load_kg")
        elif trailer_load_kg > 0.0:
          unsupported_overrides["trailer_load_kg"] = trailer_load_kg
  tuning_provenance = raw.get("tuning_provenance")
  if not isinstance(tuning_provenance, Mapping) or not _sha256_digest(
    tuning_provenance.get("controller_params_sha256"),
  ):
    missing.append(
      "tuning_provenance.controller_params_sha256",
    )
  baseline_profile = (
    tuning_provenance.get("baseline_controller_profile") if isinstance(tuning_provenance, Mapping) else None
  )
  profile_id = baseline_profile.get("profile_id") if isinstance(baseline_profile, Mapping) else None
  reviewed_profile = REVIEWED_CONTROLLER_PROFILES.get(profile_id) if isinstance(profile_id, str) else None
  baseline_profile_params = (
    baseline_profile.get("baseline_controller_params") if isinstance(baseline_profile, Mapping) else None
  )
  baseline_profile_sha256 = (
    baseline_profile.get("baseline_controller_params_sha256") if isinstance(baseline_profile, Mapping) else None
  )
  baseline_profile_evaluator = baseline_profile.get("evaluator") if isinstance(baseline_profile, Mapping) else None
  profile_source_commit = (
    baseline_profile.get("source_starpilot_commit") if isinstance(baseline_profile, Mapping) else None
  )
  route_source_commit = (
    car_params_provenance.get("source_starpilot_commit") if isinstance(car_params_provenance, Mapping) else None
  )
  baseline_patch = request.get("baseline_params")
  profile_multiplier = (
    baseline_profile.get("vehicle_lat_accel_factor_multiplier") if isinstance(baseline_profile, Mapping) else None
  )
  if not (
    isinstance(baseline_profile, Mapping)
    and reviewed_profile is not None
    and baseline_profile.get("kernel_schema") == CONTROLLER_KERNEL_SCHEMA
    and _is_exact_int(
      baseline_profile.get("kernel_schema_version"),
      CONTROLLER_KERNEL_SCHEMA_VERSION,
    )
    and profile_source_commit == route_source_commit
    and profile_source_commit == reviewed_profile.source_starpilot_commit
    and isinstance(baseline_profile_params, Mapping)
    and set(baseline_profile_params) == set(PARAMETER_SPECS)
    and dict(baseline_profile_params) == asdict(reviewed_profile.parameters)
    and baseline_profile_sha256 == reviewed_profile.params_sha256
    and _canonical_mapping_sha256(baseline_profile_params) == reviewed_profile.params_sha256
    and isinstance(baseline_patch, Mapping)
    and set(baseline_patch) == set(PARAMETER_SPECS)
    and dict(baseline_patch) == dict(baseline_profile_params)
    and _canonical_mapping_sha256(baseline_patch) == reviewed_profile.params_sha256
    and baseline_profile.get("effective_torque_params_value_space") == CONTROLLER_PARAMS_VALUE_SPACE
    and isinstance(profile_multiplier, (int, float))
    and not isinstance(profile_multiplier, bool)
    and math.isfinite(float(profile_multiplier))
    and float(profile_multiplier) == reviewed_profile.parameters.base_lat_accel_factor_mult
    and isinstance(baseline_profile_evaluator, Mapping)
    and baseline_profile_evaluator.get("name") == CONTROLLER_PROFILE_EVALUATOR
    and _is_exact_int(baseline_profile_evaluator.get("version"), 1)
    and baseline_profile_evaluator.get("source_commit") == profile_source_commit
    and baseline_profile_evaluator.get("source_sha256") == CONTROLLER_PROFILE_EVALUATOR_SOURCE_SHA256
  ):
    missing.append(
      "tuning_provenance.baseline_controller_profile",
    )
  selection_proof = (
    tuning_provenance.get("controller_selection_validation") if isinstance(tuning_provenance, Mapping) else None
  )
  selection_evaluator = selection_proof.get("evaluator") if isinstance(selection_proof, Mapping) else None
  checked_row_count = selection_proof.get("checked_row_count") if isinstance(selection_proof, Mapping) else None
  request_row_count = len(request["rows"]) if isinstance(request.get("rows"), list) else 0
  if not (
    isinstance(selection_proof, Mapping)
    and selection_proof.get("schema") == "comma-companion.controller-selection-proof"
    and _is_exact_int(selection_proof.get("schema_version"), 1)
    and selection_proof.get("state") == "verified"
    and _is_exact_int(selection_proof.get("missing_row_count"), 0)
    and _is_exact_int(selection_proof.get("invalid_row_count"), 0)
    and isinstance(checked_row_count, int)
    and not isinstance(checked_row_count, bool)
    and checked_row_count >= request_row_count
    and checked_row_count > 0
    and isinstance(selection_evaluator, Mapping)
    and selection_evaluator.get("name") == "starpilot-controlsd-lateral-selection"
    and _is_exact_int(selection_evaluator.get("version"), 1)
    and selection_evaluator.get("source_sha256") == CONTROLLER_SELECTION_EVALUATOR_SOURCE_SHA256
  ):
    missing.append(
      "tuning_provenance.controller_selection_validation",
    )
  torque_context_proof = (
    tuning_provenance.get(
      "effective_torque_context_validation",
    )
    if isinstance(tuning_provenance, Mapping)
    else None
  )
  torque_context_evaluator = (
    torque_context_proof.get("evaluator") if isinstance(torque_context_proof, Mapping) else None
  )
  torque_checked_row_count = (
    torque_context_proof.get("checked_row_count") if isinstance(torque_context_proof, Mapping) else None
  )
  torque_source_counts_valid = True
  for field in (
    "factor_source_counts",
    "offset_source_counts",
    "friction_source_counts",
  ):
    source_counts = torque_context_proof.get(field) if isinstance(torque_context_proof, Mapping) else None
    if not (
      isinstance(source_counts, Mapping)
      and bool(source_counts)
      and set(source_counts).issubset(
        EFFECTIVE_TORQUE_PARAMETER_SOURCES,
      )
      and all(isinstance(count, int) and not isinstance(count, bool) and count >= 0 for count in source_counts.values())
      and isinstance(torque_checked_row_count, int)
      and not isinstance(torque_checked_row_count, bool)
      and sum(source_counts.values()) == torque_checked_row_count
    ):
      torque_source_counts_valid = False
      break
  if not (
    isinstance(torque_context_proof, Mapping)
    and torque_context_proof.get("schema") == EFFECTIVE_TORQUE_CONTEXT_PROOF_SCHEMA
    and _is_exact_int(
      torque_context_proof.get("schema_version"),
      1,
    )
    and torque_context_proof.get("state") == "verified"
    and isinstance(torque_checked_row_count, int)
    and not isinstance(torque_checked_row_count, bool)
    and torque_checked_row_count >= request_row_count
    and torque_checked_row_count > 0
    and torque_context_proof.get("exact_row_count") == torque_checked_row_count
    and torque_context_proof.get("valid_row_count") == torque_checked_row_count
    and _is_exact_int(
      torque_context_proof.get("inexact_row_count"),
      0,
    )
    and _is_exact_int(
      torque_context_proof.get("missing_field_row_count"),
      0,
    )
    and _is_exact_int(
      torque_context_proof.get("invalid_row_count"),
      0,
    )
    and _is_exact_int(
      torque_context_proof.get("stateful_invalid_row_count"),
      0,
    )
    and _is_exact_int(
      torque_context_proof.get(
        "context_not_bound_to_controls_row_count",
      ),
      0,
    )
    and _is_exact_int(
      torque_context_proof.get(
        "source_after_controls_row_count",
      ),
      0,
    )
    and _is_exact_int(
      torque_context_proof.get(
        "source_identity_invalid_count",
      ),
      0,
    )
    and torque_source_counts_valid
    and isinstance(torque_context_evaluator, Mapping)
    and torque_context_evaluator.get("name") == TORQUE_CONTEXT_EVALUATOR
    and _is_exact_int(
      torque_context_evaluator.get("version"),
      1,
    )
    and torque_context_evaluator.get("source_sha256") == TORQUE_CONTEXT_EVALUATOR_SOURCE_SHA256
  ):
    missing.append(
      "tuning_provenance.effective_torque_context_validation",
    )
  warnings = []
  if missing:
    warnings.append(
      _warning(
        "controller_provenance_incomplete",
        "blocker",
        "The baseline controller snapshot is incomplete.",
        missing_fields=missing,
      )
    )
  controller_type = str(raw.get("controller_type", "")).lower()
  if controller_type and controller_type not in {
    "torque",
    "conventional_torque",
    "latcontrol_torque",
  }:
    warnings.append(
      _warning(
        "unsupported_controller_type",
        "blocker",
        "This replay kernel models only the conventional torque controller.",
        controller_type=raw.get("controller_type"),
      )
    )
  if unsupported_overrides:
    warnings.append(
      _warning(
        "unsupported_runtime_overrides",
        "blocker",
        "Active FLM or trailer-load shaping is not modeled by this replay kernel.",
        overrides=unsupported_overrides,
      )
    )
  invalid_rows = [
    row
    for row in rows
    if (
      row.controller_type
      not in {
        "torque",
        "conventional_torque",
        "latcontrol_torque",
      }
      or row.controller_selection_source not in CONTROLLER_SELECTION_SOURCES
      or not row.controller_selection_stateful
      or row.controller_selection_state_machine_version != 1
      or not _sha256_digest(row.resolved_toggles_sha256)
    )
  ]
  declared_sources = set(selection_sources) if isinstance(selection_sources, list) else set()
  declared_hashes = set(resolved_hashes) if isinstance(resolved_hashes, list) else set()
  if any(
    row.controller_selection_source not in declared_sources or row.resolved_toggles_sha256 not in declared_hashes
    for row in rows
  ):
    invalid_rows = list(rows)
  if invalid_rows:
    warnings.append(
      _warning(
        "controller_row_provenance_invalid",
        "blocker",
        "Every selected row must bind the conventional controller to an allowed resolved toggle snapshot.",
        invalid_rows,
      )
    )
  return warnings


def _limiter_gap_quality(rows: Sequence[TelemetryRow]) -> dict[str, float]:
  gaps = np.asarray(
    [row.applied_torque - row.controller_output for row in rows],
    dtype=np.float64,
  )
  jumps = np.abs(np.diff(gaps))
  return {
    "mean": float(np.mean(gaps)),
    "p95_abs": float(np.percentile(np.abs(gaps), 95)),
    "max_abs": float(np.max(np.abs(gaps))),
    "max_step_change": float(np.max(jumps)) if len(jumps) else 0.0,
  }


def _input_warnings(
  rows: Sequence[TelemetryRow],
  request: Mapping[str, Any],
  request_context: Mapping[str, Any],
  max_asof_age_ms: float,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
  warnings: list[dict[str, Any]] = []
  source_log_type = request.get("source_log_type")
  if source_log_type != "rlog":
    warnings.append(
      _warning(
        "source_log_type_ineligible",
        "blocker",
        "Dynamics replay requires native rlog data; qlog is approximately 10 Hz and cannot be repeated or interpolated into eligible 100 Hz rows.",
        expected="rlog",
        actual=source_log_type,
      )
    )
  missing_source_age_fields: set[str] = set()
  future_join_rows: list[TelemetryRow] = []
  stale_join_rows: list[TelemetryRow] = []
  stale_join_fields: set[str] = set()
  max_asof_age_us = round(max_asof_age_ms * 1_000.0)
  max_observed_age_us = 0
  for row in rows:
    freshness_limited_fields = {
      "car_state_age_us",
      "car_control_age_us",
      "controls_state_age_us",
    }
    applicable = {
      "car_state_age_us": row.car_state_age_us,
      "car_control_age_us": row.car_control_age_us,
      "controls_state_age_us": row.controls_state_age_us,
      "live_parameters_age_us": row.live_parameters_age_us,
    }
    applied_source = "".join(
      character for character in (row.applied_torque_source or "").lower() if character.isalnum()
    )
    if applied_source.startswith("caroutput"):
      applicable["car_output_age_us"] = row.car_output_age_us
      freshness_limited_fields.add("car_output_age_us")
    elif not applied_source.startswith("carcontrol"):
      missing_source_age_fields.add("applied_torque_source")
    if row.live_torque_used:
      applicable["live_torque_age_us"] = row.live_torque_age_us
    for name, value in applicable.items():
      if value is None:
        missing_source_age_fields.add(name)
      elif value < 0:
        future_join_rows.append(row)
      else:
        max_observed_age_us = max(max_observed_age_us, value)
        if name in freshness_limited_fields and value > max_asof_age_us:
          stale_join_rows.append(row)
          stale_join_fields.add(name)
  if missing_source_age_fields:
    warnings.append(
      _warning(
        "source_age_provenance_missing",
        "blocker",
        "Timestamp-causal service joins require explicit source ages and applied-torque source provenance.",
        missing_fields=sorted(missing_source_age_fields),
      )
    )
  if future_join_rows:
    warnings.append(
      _warning(
        "noncausal_source_join",
        "blocker",
        "At least one source service is timestamped after its carState row.",
        future_join_rows,
      )
    )
  wrong_applied_source = [row for row in rows if row.applied_torque_source != APPLIED_TORQUE_SOURCE]
  if wrong_applied_source:
    warnings.append(
      _warning(
        "applied_torque_source_ineligible",
        "blocker",
        "Plant replay requires measured carOutput actuator torque; requested carControl torque is never an applied-input fallback.",
        wrong_applied_source,
        expected=APPLIED_TORQUE_SOURCE,
        actual=sorted({row.applied_torque_source for row in wrong_applied_source}, key=str),
      )
    )
  if stale_join_rows:
    unique = {row.nominal_t_us: row for row in stale_join_rows}
    warnings.append(
      _warning(
        "source_age_limit_exceeded",
        "blocker",
        "At least one causal latest-as-of source is older than the plant artifact's reviewed age limit.",
        list(unique.values()),
        max_asof_age_ms=max_asof_age_ms,
        max_observed_age_us=max_observed_age_us,
        fields=sorted(stale_join_fields),
      )
    )
  live_parameters_invalid = [
    row
    for row in rows
    if (
      not row.live_parameters_event_valid
      or row.live_parameters_age_us is None
      or row.live_parameters_age_us > round(LIVE_PARAMETERS_MAX_AGE_MS * 1_000.0)
    )
  ]
  if live_parameters_invalid:
    warnings.append(
      _warning(
        "live_parameters_invalid",
        "blocker",
        "liveParameters must have a valid Event and remain within its controller-diagnostic cadence limit.",
        live_parameters_invalid,
        max_age_ms=LIVE_PARAMETERS_MAX_AGE_MS,
      )
    )
  live_torque_invalid = [
    row
    for row in rows
    if row.live_torque_used
    and (
      not row.live_torque_event_valid
      or not row.live_torque_alive
      or not row.live_torque_frequency_ok
      or row.live_torque_age_us is None
      or row.live_torque_age_us > round(LIVE_TORQUE_MAX_AGE_MS * 1_000.0)
      or row.live_torque_cadence_policy != LIVE_TORQUE_CADENCE_POLICY
      or row.live_torque_cadence_policy_version != LIVE_TORQUE_CADENCE_POLICY_VERSION
      or not row.live_torque_context_complete
    )
  ]
  if live_torque_invalid:
    warnings.append(
      _warning(
        "live_torque_diagnostics_invalid",
        "blocker",
        "Selected live torque parameters require separate payload validity, Event validity, and causal cadence proof.",
        live_torque_invalid,
        max_age_ms=LIVE_TORQUE_MAX_AGE_MS,
        cadence_policy=LIVE_TORQUE_CADENCE_POLICY,
        cadence_policy_version=LIVE_TORQUE_CADENCE_POLICY_VERSION,
      )
    )
  reviewed_profile = _declared_reviewed_controller_profile(
    request,
  )
  inexact_torque_context = [
    row
    for row in rows
    if (
      not row.effective_torque_params_exact
      or not row.live_torque_used_valid
      or not row.effective_torque_params_missing_fields_valid
      or bool(row.effective_torque_params_missing_fields)
      or not row.effective_torque_params_source_matches_values
      or not row.effective_torque_params_source_age_valid
      or not row.effective_torque_params_stateful
      or row.effective_torque_params_state_machine_version != 1
      or row.effective_torque_params_value_space != CONTROLLER_PARAMS_VALUE_SPACE
      or reviewed_profile is None
      or row.vehicle_lat_accel_factor_multiplier != reviewed_profile.parameters.base_lat_accel_factor_mult
      or row.baseline_controller_profile_id != reviewed_profile.profile_id
      or row.baseline_controller_params_sha256 != reviewed_profile.params_sha256
      or row.baseline_controller_source_starpilot_commit != reviewed_profile.source_starpilot_commit
      or any(
        source
        not in {
          "car_params",
          "live_filtered",
          "resolved_custom",
        }
        for source in row.effective_torque_params_source
      )
      or (row.live_torque_used and all(source == "car_params" for source in row.effective_torque_params_source))
      or any(
        (age is None or row.controls_state_age_us is None or age < row.controls_state_age_us)
        for source, age in zip(
          row.effective_torque_params_source,
          row.effective_torque_params_source_age_us,
        )
        if source != "car_params"
      )
    )
  ]
  if inexact_torque_context:
    warnings.append(
      _warning(
        "effective_torque_params_inexact",
        "blocker",
        "The effective factor, offset, and friction ownership must be resolved exactly for controller replay.",
        inexact_torque_context,
        missing_fields=sorted(
          {field for row in inexact_torque_context for field in row.effective_torque_params_missing_fields}
        ),
        allowed_sources=[
          "car_params",
          "live_filtered",
          "resolved_custom",
        ],
      )
    )
  if any(not row.nominal_time_present for row in rows):
    warnings.append(
      _warning(
        "nominal_time_missing",
        "blocker",
        "nominal_t_us is required; legacy t_us aliases are visible but not eligible.",
      )
    )
  nominal = np.asarray([row.nominal_t_us for row in rows], dtype=np.int64)
  bad_nominal_steps = np.flatnonzero(np.diff(nominal) != SAMPLE_PERIOD_US)
  raw_telemetry_provenance = request.get("telemetry_provenance")
  raw_origin_ns = (
    raw_telemetry_provenance.get("route_origin_log_mono_time_ns")
    if isinstance(raw_telemetry_provenance, Mapping)
    else None
  )
  route_origin_ns = int(raw_origin_ns) if isinstance(raw_origin_ns, str) and raw_origin_ns.isdigit() else None
  missing_nominal_ns = [
    row for row in rows if row.nominal_log_mono_time_ns is None or not row.nominal_log_mono_time_decimal_string
  ]
  nominal_ns = np.asarray(
    [row.nominal_log_mono_time_ns or 0 for row in rows],
    dtype=np.int64,
  )
  bad_nominal_ns_steps = np.flatnonzero(np.diff(nominal_ns) != SAMPLE_PERIOD_NS)
  off_absolute_grid = np.flatnonzero(nominal_ns % SAMPLE_PERIOD_NS != 0)
  inconsistent_conversion = (
    [
      row
      for row in rows
      if row.nominal_log_mono_time_ns is not None
      and (row.nominal_log_mono_time_ns - route_origin_ns) // 1_000 != row.nominal_t_us
    ]
    if route_origin_ns is not None
    else []
  )
  if (
    len(bad_nominal_steps)
    or missing_nominal_ns
    or len(bad_nominal_ns_steps)
    or len(off_absolute_grid)
    or inconsistent_conversion
  ):
    affected = [rows[min(index + 1, len(rows) - 1)] for index in bad_nominal_steps]
    affected.extend(missing_nominal_ns)
    affected.extend(rows[min(index + 1, len(rows) - 1)] for index in bad_nominal_ns_steps)
    affected.extend(rows[index] for index in off_absolute_grid)
    affected.extend(inconsistent_conversion)
    unique = {row.nominal_t_us: row for row in affected}
    warnings.append(
      _warning(
        "nominal_grid_invalid",
        "blocker",
        "The replay tensor must use the absolute logMonoTime 10 ms grid and manifest-pinned route-relative conversion.",
        list(unique.values()),
      )
    )
  inconsistent_source_time = [
    row
    for row in rows
    if (
      row.source_time_error_us > 0
      or row.car_state_age_us is None
      or row.source_time_error_us != -row.car_state_age_us
      or row.source_t_us != row.nominal_t_us - row.car_state_age_us
      or row.source_t_us > row.nominal_t_us
    )
  ]
  if inconsistent_source_time:
    warnings.append(
      _warning(
        "source_time_inconsistent",
        "blocker",
        "carState must be the causal latest-as-of source: source_time_error_us=-car_state_age_us<=0.",
        inconsistent_source_time,
      )
    )
  discontinuous = [row for row in rows if not row.continuous]
  if discontinuous:
    unique = {row.nominal_t_us: row for row in discontinuous}
    warnings.append(
      _warning(
        "source_telemetry_gap",
        "blocker",
        "The source timeline is marked discontinuous.",
        list(unique.values()),
      )
    )
  if any(not row.raw_unsigned_rate_present for row in rows):
    warnings.append(
      _warning(
        "raw_unsigned_steering_rate_missing",
        "blocker",
        "Raw unsigned steering_rate_deg is required for model eligibility; the derived signed magnitude is display-only fallback.",
      )
    )
  if any(not row.future_feedforward_present for row in rows):
    warnings.append(
      _warning(
        "future_feedforward_missing",
        "blocker",
        "The gravity-adjusted future feedforward acceleration must be distinct from the controller setpoint.",
      )
    )
  if any(not row.future_feedforward_exact for row in rows):
    warnings.append(
      _warning(
        "future_feedforward_inexact",
        "warning",
        "Unlogged controller overrides make the reconstructed future feedforward acceleration approximate.",
      )
    )
  if any(not row.integrator_freeze_exact for row in rows):
    warnings.append(
      _warning(
        "integrator_freeze_inexact",
        "warning",
        "Safety limiting and internal unwind state are insufficiently logged to reconstruct every integrator-freeze decision exactly.",
      )
    )
  missing_context = _context_missing(rows, request_context)
  if missing_context:
    warnings.append(
      _warning(
        "controller_context_missing",
        "blocker",
        "Live/base torque context is incomplete; historical constants are used only to keep the trace visible.",
        missing_fields=missing_context,
      )
    )
  timing = request.get("controller_i_timing")
  if timing != CONTROLLER_I_TIMING:
    warnings.append(
      _warning(
        "controller_i_timing_unverified",
        "blocker",
        "controller_i must be the post-update integral logged at each row.",
        expected=CONTROLLER_I_TIMING,
        actual=timing,
      )
    )
  warnings.extend(
    _controller_provenance_warnings(request, rows),
  )

  low_speed = [row for row in rows if row.v_ego < 3.0]
  if low_speed:
    warnings.append(
      _warning(
        "low_speed",
        "blocker",
        "Replay is outside the validated speed gate below 3 m/s.",
        low_speed,
      )
    )
  inactive = [row for row in rows if row.lat_active < 0.5]
  if inactive:
    warnings.append(
      _warning(
        "lateral_inactive",
        "blocker",
        "Lateral control was inactive during part of the selected window.",
        inactive,
      )
    )
  overlay = [row for row in rows if row.driver_overlay > 0.5]
  if overlay:
    warnings.append(
      _warning(
        "driver_overlay",
        "blocker",
        "Driver steering torque overlays the controller during the selected window.",
        overlay,
      )
    )
  saturated = [row for row in rows if row.saturated > 0.5 or abs(row.applied_torque) >= 0.999]
  if saturated:
    warnings.append(
      _warning(
        "logged_saturation",
        "blocker",
        "The logged controller or actuator was saturated during the selected window.",
        saturated,
      )
    )

  limiter_quality = _limiter_gap_quality(rows)
  limiter_severity = None
  if limiter_quality["max_abs"] >= LIMITER_GAP_BLOCKER or limiter_quality["max_step_change"] >= LIMITER_JUMP_BLOCKER:
    limiter_severity = "blocker"
  elif limiter_quality["max_abs"] >= LIMITER_GAP_WARNING or limiter_quality["max_step_change"] >= LIMITER_JUMP_WARNING:
    limiter_severity = "warning"
  if limiter_severity:
    warnings.append(
      _warning(
        "actuator_limiter_gap",
        limiter_severity,
        "The reused recorded actuator-limiter gap is large or discontinuous.",
        metrics=limiter_quality,
        warning_thresholds={
          "max_abs": LIMITER_GAP_WARNING,
          "max_step_change": LIMITER_JUMP_WARNING,
        },
        blocker_thresholds={
          "max_abs": LIMITER_GAP_BLOCKER,
          "max_step_change": LIMITER_JUMP_BLOCKER,
        },
      )
    )
  return warnings, limiter_quality


def _disagreement(
  standard_deviation: np.ndarray,
  state_scale: np.ndarray,
) -> dict[str, Any]:
  normalized = standard_deviation / np.maximum(state_scale[None, :], 1e-9)
  return {
    "mean_normalized": float(np.mean(normalized)),
    "p95_normalized": float(np.percentile(normalized, 95)),
    "by_state": {
      name: {
        "mean_normalized": float(np.mean(normalized[:, index])),
        "p95_normalized": float(np.percentile(normalized[:, index], 95)),
      }
      for index, name in enumerate(STATE_FEATURES)
    },
  }


def _trace(mean: np.ndarray, std: np.ndarray) -> dict[str, list[float]]:
  return {
    "mean": mean.tolist(),
    "std": std.tolist(),
  }


def _format_rollout(
  plant: Plant,
  future_rows: Sequence[TelemetryRow],
  parameters: ControllerParameters,
  requested_values: np.ndarray,
  limiter_gap_values: np.ndarray,
  modeled_applied_values: np.ndarray,
  state_values: np.ndarray,
) -> dict[str, Any]:
  mean_state = np.mean(state_values, axis=1)
  std_state = np.std(state_values, axis=1)
  target_rows = future_rows[1:]
  return {
    "parameters": asdict(parameters),
    "t_us": [row.nominal_t_us for row in target_rows],
    "source_t_us": [row.source_t_us for row in target_rows],
    "command_source_t_us": [row.nominal_t_us for row in future_rows[:-1]],
    "signals": {
      **{name: _trace(mean_state[:, index], std_state[:, index]) for index, name in enumerate(STATE_FEATURES)},
      "requested_output": _trace(
        np.mean(requested_values, axis=1),
        np.std(requested_values, axis=1),
      ),
      "reused_actuator_limiter_gap": _trace(
        np.mean(limiter_gap_values, axis=1),
        np.std(limiter_gap_values, axis=1),
      ),
      "modeled_applied_torque": _trace(
        np.mean(modeled_applied_values, axis=1),
        np.std(modeled_applied_values, axis=1),
      ),
    },
    "disagreement": _disagreement(std_state, plant.state_scale),
  }


def _rollouts(
  plant: Plant,
  history_rows: Sequence[TelemetryRow],
  future_rows: Sequence[TelemetryRow],
  parameter_sets: Sequence[ControllerParameters],
  request_context: Mapping[str, Any],
) -> list[tuple[dict[str, Any], dict[str, float], bool]]:
  scenario_count = len(parameter_sets)
  if not scenario_count:
    return []
  # The controller needs 300 rows strictly before the anchor to recover its
  # filter/integrator state. The plant's lag-zero row is the anchor itself,
  # because its predicted delta advances anchor -> anchor + 1.
  plant_history_rows = (*history_rows[1:], future_rows[0])
  history = np.stack([row.features() for row in reversed(plant_history_rows)])
  histories = np.repeat(history[None, None, :, :], scenario_count, axis=0)
  histories = np.repeat(histories, plant.member_count, axis=1)
  controller_history = [row.as_controller_mapping() for row in history_rows]
  controller_states = [
    [initialize_controller_state(parameters, controller_history, SAMPLE_PERIOD_S) for _ in range(plant.member_count)]
    for parameters in parameter_sets
  ]
  state_indexes = [BASE_FEATURES.index(name) for name in STATE_FEATURES]
  applied_torque_index = BASE_FEATURES.index("applied_torque")
  unsigned_rate_index = BASE_FEATURES.index("steering_rate_deg")
  signed_rate_state_index = STATE_FEATURES.index("signed_steering_rate_deg_s")
  requested_commands: list[np.ndarray] = []
  limiter_gaps: list[np.ndarray] = []
  modeled_applied_commands: list[np.ndarray] = []
  predictions: list[np.ndarray] = []
  ood = [{"max_abs_z": 0.0, "p99_abs_z": 0.0, "fraction_over_6": 0.0} for _ in parameter_sets]
  clipped = [False for _ in parameter_sets]
  for step, source in enumerate(future_rows[:-1]):
    scenario_requested = np.empty((scenario_count, plant.member_count), dtype=np.float64)
    scenario_gap = np.empty((scenario_count, plant.member_count), dtype=np.float64)
    scenario_applied = np.empty((scenario_count, plant.member_count), dtype=np.float64)
    context = _controller_context(source, request_context)
    for scenario, parameters in enumerate(parameter_sets):
      for member in range(plant.member_count):
        current_state = histories[scenario, member, 0, state_indexes]
        control = controller_step(
          parameters,
          controller_states[scenario][member],
          source.controller_observation(current_state),
          SAMPLE_PERIOD_S,
          context,
        )
        limiter_gap = source.applied_torque - source.controller_output
        modeled_applied = min(
          max(control.requested_output + limiter_gap, -context.steer_max),
          context.steer_max,
        )
        clipped[scenario] = clipped[scenario] or abs(modeled_applied) >= context.steer_max - 0.001
        scenario_requested[scenario, member] = control.requested_output
        scenario_gap[scenario, member] = limiter_gap
        scenario_applied[scenario, member] = modeled_applied
        histories[scenario, member, 0, applied_torque_index] = modeled_applied
      current_ood = plant.ood_metrics(histories[scenario])
      for name, previous in ood[scenario].items():
        try:
          current_value = float(current_ood[name])
        except (KeyError, TypeError, ValueError) as exc:
          raise DynamicsContractError(
            "invalid_model_output",
            "The plant returned incomplete OOD metrics.",
            {"metric": name},
          ) from exc
        if not math.isfinite(current_value) or current_value < 0.0:
          raise DynamicsContractError(
            "nonfinite_model_output",
            "The plant returned an invalid OOD metric.",
            {"metric": name, "value": current_value},
          )
        ood[scenario][name] = max(previous, current_value)
    delta = np.asarray(
      plant.predict_scenario_deltas(histories),
      dtype=np.float64,
    )
    expected_delta_shape = (
      scenario_count,
      plant.member_count,
      len(STATE_FEATURES),
    )
    if delta.shape != expected_delta_shape:
      raise DynamicsContractError(
        "invalid_model_output",
        "The plant returned state deltas with the wrong shape.",
        {
          "expected": list(expected_delta_shape),
          "actual": list(delta.shape),
        },
      )
    if not np.isfinite(delta).all():
      raise DynamicsContractError(
        "nonfinite_model_output",
        "The plant returned non-finite state deltas.",
      )
    current_state = np.take(histories[:, :, 0, :], state_indexes, axis=2)
    next_state = current_state + delta
    requested_commands.append(scenario_requested)
    limiter_gaps.append(scenario_gap)
    modeled_applied_commands.append(scenario_applied)
    predictions.append(next_state)
    next_row = future_rows[step + 1]
    next_base = histories[:, :, 0].copy()
    next_base[:, :, BASE_FEATURES.index("v_ego")] = next_row.v_ego
    next_base[:, :, BASE_FEATURES.index("a_ego")] = next_row.a_ego
    for state_index, feature_index in enumerate(state_indexes):
      next_base[:, :, feature_index] = next_state[:, :, state_index]
    next_base[:, :, unsigned_rate_index] = np.abs(
      next_state[:, :, signed_rate_state_index],
    )
    histories[:, :, 1:] = histories[:, :, :-1].copy()
    histories[:, :, 0] = next_base

  requested_values = np.stack(requested_commands)
  limiter_gap_values = np.stack(limiter_gaps)
  modeled_applied_values = np.stack(modeled_applied_commands)
  state_values = np.stack(predictions)
  return [
    (
      _format_rollout(
        plant,
        future_rows,
        parameters,
        requested_values[:, scenario],
        limiter_gap_values[:, scenario],
        modeled_applied_values[:, scenario],
        state_values[:, scenario],
      ),
      ood[scenario],
      clipped[scenario],
    )
    for scenario, parameters in enumerate(parameter_sets)
  ]


def _recorded(
  rows: Sequence[TelemetryRow],
  request_context: Mapping[str, Any],
) -> dict[str, Any]:
  limiter_gap = [row.applied_torque - row.controller_output for row in rows]
  contexts = [_controller_context(row, request_context) for row in rows]
  return {
    "t_us": [row.nominal_t_us for row in rows],
    "nominal_log_mono_time_ns": [
      str(row.nominal_log_mono_time_ns) if row.nominal_log_mono_time_ns is not None else None for row in rows
    ],
    "source_t_us": [row.source_t_us for row in rows],
    "source_time_error_us": [row.source_time_error_us for row in rows],
    "continuous": [row.continuous for row in rows],
    "source_ages_us": {
      "car_state": [row.car_state_age_us for row in rows],
      "car_control": [row.car_control_age_us for row in rows],
      "car_output": [row.car_output_age_us for row in rows],
      "controls_state": [row.controls_state_age_us for row in rows],
      "live_torque": [row.live_torque_age_us for row in rows],
      "live_parameters": [row.live_parameters_age_us for row in rows],
    },
    "source_validity": {
      "live_parameters_event_valid": [row.live_parameters_event_valid for row in rows],
      "live_torque_payload_valid": [row.live_torque_valid for row in rows],
      "live_torque_in_use": [row.live_torque_in_use for row in rows],
      "live_torque_used": [row.live_torque_used for row in rows],
      "live_torque_event_valid": [row.live_torque_event_valid for row in rows],
      "live_torque_alive": [row.live_torque_alive for row in rows],
      "live_torque_frequency_ok": [row.live_torque_frequency_ok for row in rows],
      "live_torque_cadence_policy": [row.live_torque_cadence_policy for row in rows],
      "live_torque_cadence_policy_version": [row.live_torque_cadence_policy_version for row in rows],
      "effective_torque_params_exact": [row.effective_torque_params_exact for row in rows],
      "effective_torque_params_missing_fields": [list(row.effective_torque_params_missing_fields) for row in rows],
      "effective_torque_params_source": [
        {
          "factor": row.effective_torque_params_source[0],
          "offset": row.effective_torque_params_source[1],
          "friction": row.effective_torque_params_source[2],
        }
        for row in rows
      ],
      "effective_torque_params_source_age_us": [
        {
          "factor": row.effective_torque_params_source_age_us[0],
          "offset": row.effective_torque_params_source_age_us[1],
          "friction": row.effective_torque_params_source_age_us[2],
        }
        for row in rows
      ],
      "effective_torque_params_stateful": [row.effective_torque_params_stateful for row in rows],
      "effective_torque_params_state_machine_version": [
        row.effective_torque_params_state_machine_version for row in rows
      ],
      "effective_torque_params_value_space": [row.effective_torque_params_value_space for row in rows],
      "vehicle_lat_accel_factor_multiplier": [row.vehicle_lat_accel_factor_multiplier for row in rows],
      "baseline_controller_profile_id": [row.baseline_controller_profile_id for row in rows],
      "baseline_controller_params_sha256": [row.baseline_controller_params_sha256 for row in rows],
      "baseline_controller_source_starpilot_commit": [row.baseline_controller_source_starpilot_commit for row in rows],
    },
    "applied_torque_source": [row.applied_torque_source for row in rows],
    "controller_provenance": {
      "controller_type": [row.controller_type for row in rows],
      "controller_selection_source": [row.controller_selection_source for row in rows],
      "controller_selection_stateful": [row.controller_selection_stateful for row in rows],
      "controller_selection_state_machine_version": [row.controller_selection_state_machine_version for row in rows],
      "resolved_toggles_sha256": [row.resolved_toggles_sha256 for row in rows],
    },
    "signals": {
      "desired_lateral_accel": [row.desired_lateral_accel for row in rows],
      "future_feedforward_lateral_accel": [row.future_feedforward_lateral_accel for row in rows],
      "gravity_adjusted_future_lateral_accel": [row.gravity_adjusted_future_lateral_accel for row in rows],
      "actual_lateral_accel": [row.actual_lateral_accel for row in rows],
      "steering_angle_deg": [row.steering_angle_deg for row in rows],
      "steering_rate_deg": [row.steering_rate_deg for row in rows],
      "signed_steering_rate_deg_s": [row.signed_steering_rate_deg_s for row in rows],
      "steering_torque_eps": [row.steering_torque_eps for row in rows],
      "requested_output": [row.controller_output for row in rows],
      "controller_i": [row.controller_i for row in rows],
      "reused_actuator_limiter_gap": limiter_gap,
      "applied_torque": [row.applied_torque for row in rows],
      "v_ego": [row.v_ego for row in rows],
      "base_lateral_accel_factor": [context.lateral_accel_factor for context in contexts],
      "lateral_accel_offset": [context.lateral_accel_offset for context in contexts],
      "friction": [context.friction for context in contexts],
      "steering_angle_deadzone_deg": [context.steering_angle_deadzone_deg for context in contexts],
      "lateral_accel_deadzone": [row.lateral_accel_deadzone for row in rows],
      "live_torque_valid": [row.live_torque_valid for row in rows],
      "live_torque_in_use": [row.live_torque_in_use for row in rows],
      "future_feedforward_exact": [row.future_feedforward_exact for row in rows],
      "integrator_freeze_exact": [row.integrator_freeze_exact for row in rows],
    },
  }


def _comparison(
  baseline: Mapping[str, Any],
  candidate: Mapping[str, Any],
) -> dict[str, Any]:
  signals = {}
  for name in (
    *STATE_FEATURES,
    "requested_output",
    "reused_actuator_limiter_gap",
    "modeled_applied_torque",
  ):
    base = np.asarray(baseline["signals"][name]["mean"], dtype=np.float64)
    changed = np.asarray(candidate["signals"][name]["mean"], dtype=np.float64)
    signals[name] = (changed - base).tolist()
  return {"candidate_minus_baseline": signals}


def _fit_metrics(
  recorded: Mapping[str, Any],
  rollout: Mapping[str, Any],
) -> dict[str, Any]:
  metrics = {}
  for name in STATE_FEATURES:
    actual = np.asarray(recorded["signals"][name], dtype=np.float64)
    predicted = np.asarray(rollout["signals"][name]["mean"], dtype=np.float64)
    error = predicted - actual
    metrics[name] = {
      "rmse": float(np.sqrt(np.mean(error**2))),
      "mae": float(np.mean(np.abs(error))),
      "bias": float(np.mean(error)),
      "p95_abs_error": float(np.percentile(np.abs(error), 95)),
    }
  return metrics


def _fit_envelope_warning(
  plant: Plant,
  horizon_s: float,
  fit_metrics: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
  envelope = plant.fit_envelope(horizon_s)
  if not envelope:
    return (
      _warning(
        "baseline_fit_envelope_missing",
        "blocker",
        "The model has no reviewed validation envelope for this horizon.",
      ),
      {},
    )
  try:
    reviewed_horizon_s = float(envelope["horizon_s"])
  except (KeyError, TypeError, ValueError):
    reviewed_horizon_s = math.nan
  warning_limits = envelope.get("warning_limits", {})
  blocker_limits = envelope.get(
    "blocker_limits",
    envelope.get("limits", {}),
  )
  valid_limits = (
    isinstance(warning_limits, Mapping)
    and isinstance(blocker_limits, Mapping)
    and set(blocker_limits) == set(STATE_FEATURES)
    and set(warning_limits).issubset(STATE_FEATURES)
    and all(
      isinstance(value, (int, float))
      and not isinstance(value, bool)
      and math.isfinite(float(value))
      and float(value) >= 0.0
      for value in (
        *blocker_limits.values(),
        *warning_limits.values(),
      )
    )
    and math.isfinite(reviewed_horizon_s)
    and reviewed_horizon_s + 1e-9 >= horizon_s
    and isinstance(envelope.get("metric"), str)
    and bool(envelope["metric"])
  )
  if not valid_limits:
    return (
      _warning(
        "baseline_fit_envelope_invalid",
        "blocker",
        "The reviewed model fit envelope is incomplete or does not cover this horizon.",
      ),
      {},
    )
  exceeded = {
    name: {
      "rmse": float(fit_metrics[name]["rmse"]),
      "conservative_hard_limit": float(limit),
    }
    for name, limit in blocker_limits.items()
    if name in fit_metrics and float(fit_metrics[name]["rmse"]) > float(limit)
  }
  degraded = {
    name: {
      "rmse": float(fit_metrics[name]["rmse"]),
      "reviewed_validation_rmse": float(limit),
    }
    for name, limit in warning_limits.items()
    if (name in fit_metrics and float(fit_metrics[name]["rmse"]) > float(limit) and name not in exceeded)
  }
  quality = {
    "reviewed_horizon_s": reviewed_horizon_s,
    "metric": envelope["metric"],
    "warning_limits": warning_limits,
    "blocker_limits": blocker_limits,
    "limits": blocker_limits,
    "degraded": degraded,
    "exceeded": exceeded,
  }
  if exceeded:
    return (
      _warning(
        "baseline_fit_outside_reviewed_envelope",
        "blocker",
        "Baseline fit is worse than the conservative reviewed validation envelope.",
        **quality,
      ),
      quality,
    )
  if degraded:
    return (
      _warning(
        "baseline_fit_degraded",
        "warning",
        "Baseline fit is worse than reviewed validation RMSE but remains inside the conservative hard envelope.",
        **quality,
      ),
      quality,
    )
  return None, quality


def _telemetry_provenance_warnings(
  request: Mapping[str, Any],
  input_alignment: str,
) -> tuple[list[dict[str, Any]], Mapping[str, Any]]:
  raw = request.get("telemetry_provenance")
  if not isinstance(raw, Mapping):
    return (
      [
        _warning(
          "telemetry_provenance_missing",
          "blocker",
          "The dynamics-row schema, causal eligibility, and extractor source hash are required.",
        )
      ],
      {},
    )
  expected = {
    "schema": TELEMETRY_SCHEMA,
    "schema_version": TELEMETRY_SCHEMA_VERSION,
    "alignment": CAUSAL_INPUT_ALIGNMENT,
  }
  mismatches = {
    name: {
      "expected": value,
      "actual": raw.get(name),
    }
    for name, value in expected.items()
    if raw.get(name) != value
  }
  if raw.get("causal_input_eligible") is not True:
    mismatches["causal_input_eligible"] = {
      "expected": True,
      "actual": raw.get("causal_input_eligible"),
    }
  extractor_source_sha256 = raw.get("extractor_source_sha256")
  if not _sha256_digest(extractor_source_sha256):
    mismatches["extractor_source_sha256"] = {
      "expected": "64-character SHA-256",
      "actual": extractor_source_sha256,
    }
  extractor_version = raw.get("extractor_version")
  if not isinstance(extractor_version, str) or not extractor_version:
    mismatches["extractor_version"] = {
      "expected": "non-empty version string",
      "actual": extractor_version,
    }
  route_origin = raw.get("route_origin_log_mono_time_ns")
  if not isinstance(route_origin, str) or not route_origin.isdigit():
    mismatches["route_origin_log_mono_time_ns"] = {
      "expected": "decimal string",
      "actual": route_origin,
    }
  if raw.get("alignment") != input_alignment:
    mismatches["request_alignment"] = {
      "expected": raw.get("alignment"),
      "actual": input_alignment,
    }
  if mismatches:
    return (
      [
        _warning(
          "telemetry_provenance_mismatch",
          "blocker",
          "Telemetry provenance does not satisfy the timestamp-causal dynamics-row contract.",
          mismatches=mismatches,
        )
      ],
      raw,
    )
  return [], raw


def _sha256_digest(value: Any) -> bool:
  return (
    isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)
  )


def _model_integrity_warnings(
  plant: Plant,
  input_alignment: str,
  telemetry_provenance: Mapping[str, Any],
) -> list[dict[str, Any]]:
  provenance = plant.provenance()
  warnings = []
  if input_alignment != CAUSAL_INPUT_ALIGNMENT:
    warnings.append(
      _warning(
        "input_alignment_unverified",
        "blocker",
        "Only causal recorded-order as-of telemetry is accepted by this API.",
        expected=CAUSAL_INPUT_ALIGNMENT,
        actual=input_alignment,
      )
    )
  telemetry_extractor = telemetry_provenance.get("extractor_source_sha256")
  mismatches = {}
  expected_model_values = {
    "promoted_artifact_verified": True,
    "review_registry_match": True,
    "review_manifest_verified": True,
    "member_count": plant.member_count,
    "reviewed_member_count": plant.member_count,
    "causal_training_eligible": True,
    "training_alignment": input_alignment,
    "training_schema": telemetry_provenance.get("schema"),
    "training_schema_version": telemetry_provenance.get("schema_version"),
    "training_extraction_version": TRAINING_EXTRACTION_VERSION,
    "training_contract_version": TRAINING_CONTRACT_VERSION,
    "recursive_objective_horizon_s": (RECURSIVE_OBJECTIVE_HORIZON_S),
    "trainer_schema": TRAINER_SCHEMA,
    "trainer_schema_version": TRAINER_SCHEMA_VERSION,
    "compatible_telemetry_extractor_version": (telemetry_provenance.get("extractor_version")),
    "compatible_telemetry_extractor_sha256": telemetry_extractor,
    "max_asof_age_ms": MAX_ASOF_AGE_MS,
    "sampling": causal_sampling_contract(),
  }
  for name, expected in expected_model_values.items():
    actual = provenance.get(name)
    matches = actual is expected if isinstance(expected, bool) else actual == expected
    if not matches:
      mismatches[name] = {
        "expected": expected,
        "actual": actual,
      }
  for name in (
    "review_manifest_sha256",
    "training_extractor_sha256",
    "trainer_sha256",
  ):
    if not _sha256_digest(provenance.get(name)):
      mismatches[name] = {
        "expected": "64-character SHA-256",
        "actual": provenance.get(name),
      }
  if mismatches:
    warnings.append(
      _warning(
        "model_training_alignment_mismatch",
        "blocker",
        "The loaded plant is not registered for this exact timestamp-causal extractor and schema.",
        training_alignment=provenance.get("training_alignment", "unknown"),
        input_alignment=input_alignment,
        mismatches=mismatches,
      )
    )
  return warnings


def _validate_plant_runtime_contract(plant: Plant) -> None:
  problems: dict[str, Any] = {}
  if not isinstance(plant.member_count, int) or isinstance(plant.member_count, bool) or plant.member_count < 2:
    problems["member_count"] = plant.member_count
  if plant.history_steps != HISTORY_STEPS:
    problems["history_steps"] = {
      "expected": HISTORY_STEPS,
      "actual": plant.history_steps,
    }
  if tuple(plant.feature_names) != BASE_FEATURES:
    problems["feature_names"] = {
      "expected": list(BASE_FEATURES),
      "actual": list(plant.feature_names),
    }
  if tuple(plant.state_feature_names) != STATE_FEATURES:
    problems["state_feature_names"] = {
      "expected": list(STATE_FEATURES),
      "actual": list(plant.state_feature_names),
    }
  try:
    state_scale = np.asarray(
      plant.state_scale,
      dtype=np.float64,
    )
  except (TypeError, ValueError):
    state_scale = np.empty(0, dtype=np.float64)
  if state_scale.shape != (len(STATE_FEATURES),) or not np.isfinite(state_scale).all() or np.any(state_scale <= 0.0):
    problems["state_scale"] = state_scale.tolist() if state_scale.ndim == 1 else {"shape": list(state_scale.shape)}
  if problems:
    raise DynamicsContractError(
      "model_contract_mismatch",
      "The loaded plant does not satisfy the replay runtime contract.",
      problems,
    )


def replay(plant: Plant, request: Mapping[str, Any]) -> dict[str, Any]:
  if not isinstance(request, Mapping):
    raise DynamicsContractError("invalid_request", "Replay params must be a JSON object.")
  mode = request.get("mode", MODE)
  if mode != MODE:
    raise DynamicsContractError(
      "unsupported_mode",
      f"Only {MODE} is available.",
      {"mode": mode},
    )
  fingerprint = request.get("car_fingerprint")
  if fingerprint != REFERENCE_CAR_FINGERPRINT:
    raise DynamicsContractError(
      "wrong_car",
      "This plant is valid only for the trained Ioniq 5 fingerprint.",
      {"expected": REFERENCE_CAR_FINGERPRINT, "actual": fingerprint},
    )
  _validate_plant_runtime_contract(plant)
  rows = _parse_rows(request.get("rows"))
  anchor = _resolve_anchor(request, rows)
  horizon_s, steps = _horizon_steps(request)
  if anchor < HISTORY_STEPS:
    raise DynamicsContractError(
      "insufficient_history",
      "Replay requires 3.0 seconds (300 rows) of history before the anchor.",
      {"required_rows": HISTORY_STEPS, "available_rows": anchor},
    )
  if anchor + steps >= len(rows):
    raise DynamicsContractError(
      "insufficient_future",
      "The supplied rows do not cover the requested replay horizon.",
      {
        "required_last_index": anchor + steps,
        "available_last_index": len(rows) - 1,
      },
    )
  history_rows = rows[anchor - HISTORY_STEPS : anchor]
  future_rows = rows[anchor : anchor + steps + 1]
  selected_rows = rows[anchor - HISTORY_STEPS : anchor + steps + 1]
  request_context = _request_context(request)
  model_provenance = plant.provenance()
  raw_max_asof_age_ms = model_provenance.get("max_asof_age_ms")
  max_asof_age_ms = (
    float(raw_max_asof_age_ms)
    if isinstance(raw_max_asof_age_ms, (int, float))
    and not isinstance(raw_max_asof_age_ms, bool)
    and math.isfinite(float(raw_max_asof_age_ms))
    and float(raw_max_asof_age_ms) > 0.0
    else MAX_ASOF_AGE_MS
  )
  warnings, limiter_quality = _input_warnings(
    selected_rows,
    request,
    request_context,
    max_asof_age_ms,
  )
  input_alignment = str(request.get("input_alignment", CAUSAL_INPUT_ALIGNMENT))
  telemetry_warnings, telemetry_provenance = _telemetry_provenance_warnings(
    request,
    input_alignment,
  )
  warnings.extend(telemetry_warnings)
  warnings.extend(
    _model_integrity_warnings(
      plant,
      input_alignment,
      telemetry_provenance,
    )
  )

  baseline_patch = request.get("baseline_params")
  candidate_patch = request.get("candidate_params")
  baseline_parameters = parameters_from_patch(baseline_patch)
  candidate_parameters = parameters_from_patch(candidate_patch, baseline_parameters)
  # The baseline is the route's reviewed kernel profile. Some profile fields
  # are model-only relative to the corresponding runtime surface, so only user
  # candidate edits get this label.
  model_only = model_only_parameters(candidate_patch)
  if model_only:
    warnings.append(
      _warning(
        "model_only_parameters",
        "warning",
        "These optimizer-only knobs are not implemented by the on-device runtime.",
        parameters=model_only,
      )
    )

  (
    (baseline, baseline_ood, baseline_clipped),
    (candidate, candidate_ood, candidate_clipped),
  ) = _rollouts(
    plant,
    history_rows,
    future_rows,
    (baseline_parameters, candidate_parameters),
    request_context,
  )
  worst_ood = {name: max(float(baseline_ood[name]), float(candidate_ood[name])) for name in baseline_ood}
  if worst_ood["p99_abs_z"] > 6.0 or worst_ood["max_abs_z"] > 12.0 or worst_ood["fraction_over_6"] > 0.01:
    warnings.append(
      _warning(
        "out_of_distribution",
        "blocker",
        "The model inputs exceed the training-normalization guardrails.",
        metrics=worst_ood,
      )
    )
  if baseline_clipped or candidate_clipped:
    warnings.append(
      _warning(
        "predicted_saturation",
        "blocker",
        "A baseline or candidate modeled actuator command reached the replay saturation limit.",
      )
    )
  disagreement_p95 = max(
    baseline["disagreement"]["p95_normalized"],
    candidate["disagreement"]["p95_normalized"],
  )
  if disagreement_p95 > 0.5:
    warnings.append(
      _warning(
        "model_disagreement",
        "blocker",
        "Plant ensemble disagreement is above the replay confidence gate.",
        p95_normalized=disagreement_p95,
      )
    )
  recorded = _recorded(future_rows[1:], request_context)
  baseline["fit_to_recorded"] = _fit_metrics(recorded, baseline)
  candidate["fit_to_recorded"] = _fit_metrics(recorded, candidate)
  fit_warning, fit_quality = _fit_envelope_warning(
    plant,
    horizon_s,
    baseline["fit_to_recorded"],
  )
  if fit_warning:
    warnings.append(fit_warning)

  controller_provenance = request.get(
    "controller_provenance",
  )
  tuning_provenance = (
    controller_provenance.get("tuning_provenance") if isinstance(controller_provenance, Mapping) else None
  )
  baseline_profile = (
    tuning_provenance.get("baseline_controller_profile") if isinstance(tuning_provenance, Mapping) else None
  )
  controller_context_provenance = {
    "kernel_schema": CONTROLLER_KERNEL_SCHEMA,
    "kernel_schema_version": CONTROLLER_KERNEL_SCHEMA_VERSION,
    "baseline_profile_id": (baseline_profile.get("profile_id") if isinstance(baseline_profile, Mapping) else None),
    "semantics": {
      "lateral_accel_factor": "base/pre-Ioniq-multiplier value",
      "gravity_adjusted_future_lateral_accel": "desired-curvature acceleration minus roll compensation",
      "future_feedforward_lateral_accel": "gravity-adjusted future acceleration after lateral-accel offset",
      "controller_i": CONTROLLER_I_TIMING,
    },
    "request": dict(request_context),
    "row_sources": sorted({source for row in selected_rows for source in row.context_sources}),
    "controller_provenance": controller_provenance,
  }
  return {
    "mode": MODE,
    "exact_baseline": False,
    "eligible": not any(item["severity"] == "blocker" for item in warnings),
    "warnings": warnings,
    "limitations": [
      "This is an approximate closed-loop counterfactual, not a bit-exact replay of the on-device controller.",
      "The recorded actuator-limiter gap is reused explicitly; modeled_applied_torque is not measured actuator output.",
      "Only an explicitly reviewed and promoted timestamp-causal plant can make a result eligible.",
      "Results are read-only and cannot be applied to the car.",
    ],
    "input_alignment": input_alignment,
    "telemetry_provenance": dict(telemetry_provenance),
    "max_asof_age_ms": max_asof_age_ms,
    "controller_context": controller_context_provenance,
    "window": {
      "anchor_index": anchor,
      "anchor_t_us": rows[anchor].nominal_t_us,
      "anchor_nominal_log_mono_time_ns": (
        str(rows[anchor].nominal_log_mono_time_ns) if rows[anchor].nominal_log_mono_time_ns is not None else None
      ),
      "anchor_source_t_us": rows[anchor].source_t_us,
      "history_start_t_us": history_rows[0].nominal_t_us,
      "history_end_t_us": history_rows[-1].nominal_t_us,
      "history_steps": HISTORY_STEPS,
      "sample_period_s": SAMPLE_PERIOD_S,
      "horizon_s": horizon_s,
      "horizon_steps": steps,
    },
    "recorded": recorded,
    "baseline": baseline,
    "candidate": candidate,
    "comparison": _comparison(baseline, candidate),
    "quality": {
      "worst_ood": worst_ood,
      "max_ensemble_disagreement_p95_normalized": disagreement_p95,
      "actuator_limiter_gap": limiter_quality,
      "baseline_fit_envelope": fit_quality,
    },
  }
