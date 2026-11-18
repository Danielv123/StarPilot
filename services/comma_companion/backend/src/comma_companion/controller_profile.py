from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any


HISTORICAL_CONTROLLER_SOURCE_COMMIT = (
  "2747bf037c0f284500457f1befb4f52415e3285a"
)
HISTORICAL_CONTROLLER_PROFILE_ID = (
  "starpilot-ioniq5-torque-2747bf037c0f-v1"
)
HISTORICAL_CONTROLLER_PROFILE_SHA256 = (
  "f8dc55e57772cd4850e37db43e494b4103384acc325627b0674a793952dd7a12"
)
C6_CONTROLLER_SOURCE_COMMIT = (
  "6dd6c0a3d558842b91b903e1cddfaca576a69c25"
)
C6_CONTROLLER_PROFILE_ID = (
  "starpilot-ioniq5-torque-6dd6c0a3d558-v1"
)
C6_CONTROLLER_PROFILE_SHA256 = (
  "f02fff8adef34f524607bc0f200ad486523850a2ec576c36d664d9b369175ce8"
)
CURRENT_CONTROLLER_SOURCE_COMMIT = (
  "19f8c767ec0d3b6fc1000aa1effbeec625ddd753"
)
CURRENT_CONTROLLER_PROFILE_ID = (
  "starpilot-ioniq5-torque-19f8c767ec0d-v1"
)
CURRENT_CONTROLLER_PROFILE_SHA256 = (
  "b88e9997a04b67b8acb979ec3e5b988a2c3caf081e988f14764e56e0149a9537"
)
CONTROLLER_KERNEL_SCHEMA = "comma-companion.ioniq5-torque-kernel"
CONTROLLER_KERNEL_SCHEMA_VERSION = 1
CONTROLLER_PARAMS_VALUE_SPACE = (
  "raw_carparams_live_custom_pre_vehicle_multiplier"
)
CONTROLLER_PROFILE_EVALUATOR = (
  "starpilot-ioniq5-controller-profile-by-source-commit"
)
CONTROLLER_PROFILE_EVALUATOR_SHA256 = (
  "e3aff430236874e7f3255605fffa831874509a3b8b44a27770cd787228339060"
)
HISTORICAL_CONTROLLER_PARAMS: dict[str, float | bool] = {
  "base_lat_accel_factor_mult": 1.2101,
  "ff_reduction_left": 0.12,
  "ff_reduction_right": 0.22,
  "turn_in_boost_left": 0.14,
  "turn_in_boost_right": 0.06,
  "unwind_taper_left": 0.76,
  "unwind_taper_right": 0.85,
  "turn_in_threshold_reduction_left": 0.08,
  "turn_in_threshold_reduction_right": 0.05,
  "unwind_threshold_increase_left": 0.36,
  "unwind_threshold_increase_right": 0.38,
  "turn_in_friction_boost_left": 0.04,
  "turn_in_friction_boost_right": 0.03,
  "unwind_friction_reduction_left": 0.34,
  "unwind_friction_reduction_right": 0.34,
  "friction_scale_mult": 0.729,
  "center_taper_max": 0.24,
  "center_taper_lat": 0.12,
  "center_taper_lat_width": 0.03,
  "center_taper_speed": 16.0,
  "center_taper_speed_width": 2.5,
  "sustained_turn_in_ff_boost_left": 0.0,
  "sustained_turn_in_ff_boost_right": 0.0,
  "sustained_turn_in_ff_speed": 13.5,
  "sustained_turn_in_ff_speed_width": 1.8,
  "sustained_turn_in_ff_lat_start": 1.1,
  "sustained_turn_in_ff_lat_end": 3.6,
  "sustained_turn_in_ff_lat_width": 0.3,
  "steady_high_lat_taper": 0.0,
  "steady_high_lat_start": 0.35,
  "steady_high_lat_width": 0.12,
  "steady_jerk_width": 0.08,
  "hkg_friction_threshold": False,
  "damping_gain": 0.0,
  "turn_exit_damping_gain": 0.0,
  "turn_exit_damping_gain_right": 0.0,
  "reversal_damping_gain": 0.0,
  "reversal_hold_seconds": 0.6,
  "steering_rate_feedback_gain": 0.0,
}
C6_CONTROLLER_PARAMS: dict[str, float | bool] = {
  **HISTORICAL_CONTROLLER_PARAMS,
  "base_lat_accel_factor_mult": 1.2507,
  "turn_in_boost_left": 0.1761,
  "unwind_taper_right": 0.8885,
  "friction_scale_mult": 1.0,
  "center_taper_max": 0.2412,
}
CURRENT_CONTROLLER_PARAMS: dict[str, float | bool] = {
  "base_lat_accel_factor_mult": 1.36,
  "ff_reduction_left": 0.2625,
  "ff_reduction_right": 0.415,
  "turn_in_boost_left": 0.135,
  "turn_in_boost_right": 0.02,
  "unwind_taper_left": 1.15,
  "unwind_taper_right": 1.39,
  "turn_in_threshold_reduction_left": 0.125,
  "turn_in_threshold_reduction_right": 0.085,
  "unwind_threshold_increase_left": 0.28,
  "unwind_threshold_increase_right": 0.30,
  "turn_in_friction_boost_left": 0.02,
  "turn_in_friction_boost_right": 0.01,
  "unwind_friction_reduction_left": 0.42,
  "unwind_friction_reduction_right": 0.44,
  "friction_scale_mult": 1.0,
  "center_taper_max": 0.17,
  "center_taper_lat": 0.16,
  "center_taper_lat_width": 0.04,
  "center_taper_speed": 15.0,
  "center_taper_speed_width": 2.2,
  "sustained_turn_in_ff_boost_left": 0.0,
  "sustained_turn_in_ff_boost_right": 0.0,
  "sustained_turn_in_ff_speed": 13.5,
  "sustained_turn_in_ff_speed_width": 1.8,
  "sustained_turn_in_ff_lat_start": 1.1,
  "sustained_turn_in_ff_lat_end": 3.6,
  "sustained_turn_in_ff_lat_width": 0.3,
  "steady_high_lat_taper": 0.015,
  "steady_high_lat_start": 0.35,
  "steady_high_lat_width": 0.12,
  "steady_jerk_width": 0.08,
  "hkg_friction_threshold": True,
  "damping_gain": 0.02,
  "turn_exit_damping_gain": 0.02,
  "turn_exit_damping_gain_right": 0.02,
  "reversal_damping_gain": 0.0175,
  "reversal_hold_seconds": 0.60,
  "steering_rate_feedback_gain": 0.0,
}
REVIEWED_CONTROLLER_PROFILES: dict[
  str,
  tuple[str, str, dict[str, float | bool]],
] = {
  HISTORICAL_CONTROLLER_PROFILE_ID: (
    HISTORICAL_CONTROLLER_SOURCE_COMMIT,
    HISTORICAL_CONTROLLER_PROFILE_SHA256,
    HISTORICAL_CONTROLLER_PARAMS,
  ),
  C6_CONTROLLER_PROFILE_ID: (
    C6_CONTROLLER_SOURCE_COMMIT,
    C6_CONTROLLER_PROFILE_SHA256,
    C6_CONTROLLER_PARAMS,
  ),
  CURRENT_CONTROLLER_PROFILE_ID: (
    CURRENT_CONTROLLER_SOURCE_COMMIT,
    CURRENT_CONTROLLER_PROFILE_SHA256,
    CURRENT_CONTROLLER_PARAMS,
  ),
}


def _exact_int(value: Any, expected: int) -> bool:
  return (
    isinstance(value, int)
    and not isinstance(value, bool)
    and value == expected
  )


def _sha256(value: Any) -> bool:
  return (
    isinstance(value, str)
    and len(value) == 64
    and value == value.lower()
    and all(character in "0123456789abcdef" for character in value)
  )


def _canonical_mapping_sha256(value: Mapping[str, Any]) -> str | None:
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


def validated_controller_profile(
  manifest: Mapping[str, Any],
) -> dict[str, Any] | None:
  dynamics = manifest.get("dynamics")
  recorded = (
    dynamics.get("controller_provenance")
    if isinstance(dynamics, Mapping)
    else None
  )
  profile = (
    recorded.get("baseline_controller_profile")
    if isinstance(recorded, Mapping)
    else None
  )
  provenance = manifest.get("provenance")
  vehicle = manifest.get("vehicle")
  if (
    not isinstance(profile, Mapping)
    or not isinstance(provenance, Mapping)
    or not isinstance(vehicle, Mapping)
  ):
    return None
  parameters = profile.get("baseline_controller_params")
  evaluator = profile.get("evaluator")
  multiplier = profile.get("vehicle_lat_accel_factor_multiplier")
  profile_id = profile.get("profile_id")
  reviewed = (
    REVIEWED_CONTROLLER_PROFILES.get(profile_id)
    if isinstance(profile_id, str)
    else None
  )
  if reviewed is None:
    return None
  expected_commit, expected_sha256, expected_parameters = reviewed
  if (
    vehicle.get("car_fingerprint") != "HYUNDAI_IONIQ_5"
    or profile.get("kernel_schema") != CONTROLLER_KERNEL_SCHEMA
    or not _exact_int(
      profile.get("kernel_schema_version"),
      CONTROLLER_KERNEL_SCHEMA_VERSION,
    )
    or profile.get("source_starpilot_commit") != expected_commit
    or provenance.get("source_starpilot_commit") != expected_commit
    or not isinstance(parameters, Mapping)
    or dict(parameters) != expected_parameters
    or profile.get("baseline_controller_params_sha256")
    != expected_sha256
    or _canonical_mapping_sha256(parameters) != expected_sha256
    or profile.get("effective_torque_params_value_space")
    != CONTROLLER_PARAMS_VALUE_SPACE
    or not isinstance(multiplier, (int, float))
    or isinstance(multiplier, bool)
    or not math.isfinite(float(multiplier))
    or float(multiplier)
    != float(expected_parameters["base_lat_accel_factor_mult"])
    or not isinstance(evaluator, Mapping)
    or evaluator.get("name") != CONTROLLER_PROFILE_EVALUATOR
    or not _exact_int(evaluator.get("version"), 1)
    or evaluator.get("source_commit") != expected_commit
    or evaluator.get("source_sha256")
    != CONTROLLER_PROFILE_EVALUATOR_SHA256
  ):
    return None
  return copy.deepcopy(dict(profile))


def selected_baseline_params(
  manifest: Mapping[str, Any],
) -> dict[str, Any] | None:
  profile = validated_controller_profile(manifest)
  if profile is None:
    return None
  parameters = profile["baseline_controller_params"]
  return copy.deepcopy(dict(parameters))
