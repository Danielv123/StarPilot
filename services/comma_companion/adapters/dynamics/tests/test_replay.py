from __future__ import annotations

import copy
import math
from dataclasses import asdict
from typing import Any

import numpy as np
import pytest

from comma_companion_dynamics.contract import (
  BASE_FEATURES,
  HISTORY_STEPS,
  REFERENCE_CAR_FINGERPRINT,
  STATE_FEATURES,
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
  CURRENT_CONTROLLER_PROFILE_ID,
  EARLY_HISTORICAL_CONTROLLER_PROFILE_ID,
  HISTORICAL_CONTROLLER_PROFILE_ID,
  REVIEWED_CONTROLLER_PROFILES,
)
from comma_companion_dynamics.replay import (
  CAUSAL_INPUT_ALIGNMENT,
  CONTROLLER_I_TIMING,
  CONTROLLER_SELECTION_EVALUATOR_SOURCE_SHA256,
  HISTORICAL_FLM_EVALUATOR_SOURCE_SHA256,
  TELEMETRY_SCHEMA,
  TELEMETRY_SCHEMA_VERSION,
  TORQUE_CONTEXT_EVALUATOR,
  TORQUE_CONTEXT_EVALUATOR_IDS,
  TORQUE_CONTEXT_EVALUATOR_SOURCE_SHA256,
  replay,
)

FIXTURE_EXTRACTOR_SHA256 = "a" * 64
FIXTURE_EXTRACTOR_VERSION = "1.1.0"
FIXTURE_ROUTE_ORIGIN_NS = 1_000_000_000
FIXTURE_CONTROLLER_PROFILE = REVIEWED_CONTROLLER_PROFILES[HISTORICAL_CONTROLLER_PROFILE_ID]
FIXTURE_BASELINE_PARAMS = asdict(
  FIXTURE_CONTROLLER_PROFILE.parameters,
)


class FakePlant:
  member_count = 3
  history_steps = HISTORY_STEPS
  feature_names = BASE_FEATURES
  state_feature_names = STATE_FEATURES
  state_scale = np.ones(len(STATE_FEATURES), dtype=np.float64)

  def __init__(
    self,
    ood: dict[str, float] | None = None,
    fit_limit: float = 100.0,
    fit_warning_limit: float | None = None,
    causal_input_eligible: bool = True,
    max_asof_age_ms: float = 35.0,
    promoted_artifact_verified: Any = True,
    review_registry_match: Any = True,
  ):
    self.inputs: list[np.ndarray] = []
    self._ood = ood or {
      "max_abs_z": 1.0,
      "p99_abs_z": 1.0,
      "fraction_over_6": 0.0,
    }
    self._fit_limit = fit_limit
    self._fit_warning_limit = fit_warning_limit
    self._causal_input_eligible = causal_input_eligible
    self._max_asof_age_ms = max_asof_age_ms
    self._promoted_artifact_verified = promoted_artifact_verified
    self._review_registry_match = review_registry_match

  def predict_member_deltas(self, histories: np.ndarray) -> np.ndarray:
    return self.predict_scenario_deltas(histories[None, ...])[0]

  def predict_scenario_deltas(self, histories: np.ndarray) -> np.ndarray:
    self.inputs.append(histories[0].copy())
    applied = histories[:, :, 0, BASE_FEATURES.index("applied_torque")]
    result = np.zeros(
      (len(histories), self.member_count, len(STATE_FEATURES)),
      dtype=np.float64,
    )
    result[:, :, STATE_FEATURES.index("actual_lateral_accel")] = 0.01 * applied
    result[:, :, STATE_FEATURES.index("steering_angle_deg")] = 0.02 * applied
    result[:, :, STATE_FEATURES.index("signed_steering_rate_deg_s")] = -0.25 + np.arange(self.member_count) * 0.01
    result[:, :, STATE_FEATURES.index("steering_torque_eps")] = applied
    return result

  def ood_metrics(self, histories: np.ndarray) -> dict[str, float]:
    return self._ood

  def fit_envelope(self, horizon_s: float) -> dict[str, Any]:
    result = {
      "horizon_s": horizon_s,
      "metric": "fixture_limit",
      "limits": {name: self._fit_limit for name in STATE_FEATURES},
    }
    if self._fit_warning_limit is not None:
      result["warning_limits"] = {name: self._fit_warning_limit for name in STATE_FEATURES}
      result["blocker_limits"] = result["limits"]
    return result

  def provenance(self) -> dict[str, Any]:
    return {
      "artifact": "fixture",
      "member_count": self.member_count,
      "reviewed_member_count": self.member_count,
      "promoted_artifact_verified": (self._promoted_artifact_verified),
      "review_registry_match": self._review_registry_match,
      "review_manifest_verified": True,
      "review_manifest_sha256": "d" * 64,
      "training_alignment": (CAUSAL_INPUT_ALIGNMENT if self._causal_input_eligible else "legacy_file_order_noncausal"),
      "causal_training_eligible": self._causal_input_eligible,
      "causal_input_eligible": self._causal_input_eligible,
      "training_schema": TELEMETRY_SCHEMA,
      "training_schema_version": TELEMETRY_SCHEMA_VERSION,
      "training_extraction_version": 9,
      "training_extractor_sha256": FIXTURE_EXTRACTOR_SHA256,
      "training_contract_version": 10,
      "recursive_objective_horizon_s": 0.5,
      "trainer_schema": "starpilot.neural-lateral-plant",
      "trainer_schema_version": 8,
      "trainer_sha256": "b" * 64,
      "compatible_telemetry_extractor_version": (FIXTURE_EXTRACTOR_VERSION),
      "compatible_telemetry_extractor_sha256": FIXTURE_EXTRACTOR_SHA256,
      "max_asof_age_ms": self._max_asof_age_ms,
      "sampling": {
        **causal_sampling_contract(),
        "max_asof_age_ms": self._max_asof_age_ms,
      },
    }


def rows(
  count: int = 510,
) -> list[dict[str, float | int | bool | str]]:
  result = []
  for index in range(count):
    nominal_t_us = index * 10_000
    car_state_age_us = (index % 3) * 400
    result.append(
      {
        "t_us": nominal_t_us,
        "nominal_t_us": nominal_t_us,
        "nominal_log_mono_time_ns": str(
          FIXTURE_ROUTE_ORIGIN_NS + nominal_t_us * 1_000,
        ),
        "source_t_us": nominal_t_us - car_state_age_us,
        "source_time_error_us": -car_state_age_us,
        "car_state_age_us": car_state_age_us,
        "car_control_age_us": 1_000,
        "car_output_age_us": 2_000,
        "controls_state_age_us": 3_000,
        "live_parameters_age_us": 4_000,
        "live_parameters_event_valid": True,
        "live_torque_event_valid": True,
        "live_torque_alive": True,
        "live_torque_frequency_ok": True,
        "live_torque_used": False,
        "live_torque_cadence_policy": "causal_timestamp_history",
        "live_torque_cadence_policy_version": 1,
        "effective_torque_params_exact": True,
        "effective_torque_params_missing_fields": [],
        "effective_torque_params_source": {
          "factor": "car_params",
          "offset": "car_params",
          "friction": "car_params",
        },
        "effective_torque_params_source_age_us": {
          "factor": None,
          "offset": None,
          "friction": None,
        },
        "effective_torque_params_stateful": True,
        "effective_torque_params_state_machine_version": 1,
        "effective_torque_params_value_space": (CONTROLLER_PARAMS_VALUE_SPACE),
        "vehicle_lat_accel_factor_multiplier": (FIXTURE_CONTROLLER_PROFILE.parameters.base_lat_accel_factor_mult),
        "baseline_controller_profile_id": (FIXTURE_CONTROLLER_PROFILE.profile_id),
        "baseline_controller_params_sha256": (FIXTURE_CONTROLLER_PROFILE.params_sha256),
        "baseline_controller_source_starpilot_commit": (FIXTURE_CONTROLLER_PROFILE.source_starpilot_commit),
        "applied_torque_source": "carOutput.actuatorsOutput.torque",
        "controller_type": "conventional_torque",
        "controller_selection_source": ("starpilotPlan.starpilotToggles"),
        "controller_selection_stateful": True,
        "controller_selection_state_machine_version": 1,
        "resolved_toggles_sha256": "c" * 64,
        "continuous": True,
        "applied_torque": 0.0,
        "actual_lateral_accel": 0.05,
        "steering_angle_deg": 1.0,
        "steering_rate_deg": 2.0,
        "signed_steering_rate_deg_s": 2.0,
        "steering_torque_eps": 5.0,
        "v_ego": 12.0,
        "a_ego": 0.0,
        "desired_lateral_accel": 0.25,
        "future_feedforward_lateral_accel": 0.20,
        "future_feedforward_exact": True,
        "desired_lateral_jerk": 0.1,
        "controller_output": 0.0,
        "controller_i": 0.0,
        "lat_active": 1.0,
        "driver_overlay": 0.0,
        "saturated": 0.0,
        "steer_limited_by_safety": False,
        "integrator_frozen": False,
        "integrator_freeze_exact": True,
        "base_lat_accel_factor": BASE_LAT_ACCEL_FACTOR,
        "base_lat_accel_offset": 0.0,
        "base_friction": BASE_FRICTION,
        "steering_angle_deadzone_deg": 0.0,
      }
    )
  return result


def request(
  input_rows: list[dict[str, float | int | bool | str]] | None = None,
) -> dict[str, Any]:
  return {
    "car_fingerprint": REFERENCE_CAR_FINGERPRINT,
    "anchor_index": 300,
    "horizon_s": 0.05,
    "source_log_type": "rlog",
    "input_alignment": CAUSAL_INPUT_ALIGNMENT,
    "telemetry_provenance": {
      "schema": TELEMETRY_SCHEMA,
      "schema_version": TELEMETRY_SCHEMA_VERSION,
      "alignment": CAUSAL_INPUT_ALIGNMENT,
      "causal_input_eligible": True,
      "extractor_version": FIXTURE_EXTRACTOR_VERSION,
      "extractor_source_sha256": FIXTURE_EXTRACTOR_SHA256,
      "route_origin_log_mono_time_ns": str(FIXTURE_ROUTE_ORIGIN_NS),
    },
    "controller_i_timing": CONTROLLER_I_TIMING,
    "controller_provenance": {
      "controller_type": "conventional_torque",
      "controller_type_verified": True,
      "car_params": {
        "car_fingerprint": REFERENCE_CAR_FINGERPRINT,
        "lateral_tuning_type": "torque",
        "lateral_torque_tuning": {
          "lat_accel_factor": BASE_LAT_ACCEL_FACTOR,
          "lat_accel_offset": 0.0,
          "friction": BASE_FRICTION,
          "steering_angle_deadzone_deg": 0.0,
        },
      },
      "car_params_sha256": "f" * 64,
      "car_params_complete": True,
      "car_params_provenance": {
        "wire_sha256": "f" * 64,
        "summary_sha256": "e" * 64,
        "source_starpilot_commit": (FIXTURE_CONTROLLER_PROFILE.source_starpilot_commit),
      },
      "toggle_snapshot": {
        "controller_selection_source_types": [
          "starpilotPlan.starpilotToggles",
        ],
        "resolved_toggles_sha256": ["c" * 64],
        "flm_active": False,
        "flm_active_available": True,
        "flm_resolution": None,
        "trailer_load_kg": 0.0,
      },
      "tuning_snapshot": {
        "lat_accel_factor": BASE_LAT_ACCEL_FACTOR,
        "lat_accel_offset": 0.0,
        "friction": BASE_FRICTION,
        "steering_angle_deadzone_deg": 0.0,
      },
      "tuning_snapshot_complete": True,
      "tuning_provenance": {
        "controller_params_sha256": "a" * 64,
        "baseline_controller_profile": {
          "profile_id": FIXTURE_CONTROLLER_PROFILE.profile_id,
          "kernel_schema": CONTROLLER_KERNEL_SCHEMA,
          "kernel_schema_version": (CONTROLLER_KERNEL_SCHEMA_VERSION),
          "source_starpilot_commit": (FIXTURE_CONTROLLER_PROFILE.source_starpilot_commit),
          "baseline_controller_params": (copy.deepcopy(FIXTURE_BASELINE_PARAMS)),
          "baseline_controller_params_sha256": (FIXTURE_CONTROLLER_PROFILE.params_sha256),
          "effective_torque_params_value_space": (CONTROLLER_PARAMS_VALUE_SPACE),
          "vehicle_lat_accel_factor_multiplier": (FIXTURE_CONTROLLER_PROFILE.parameters.base_lat_accel_factor_mult),
          "evaluator": {
            "name": CONTROLLER_PROFILE_EVALUATOR,
            "version": 1,
            "source_commit": (FIXTURE_CONTROLLER_PROFILE.source_starpilot_commit),
            "source_sha256": (CONTROLLER_PROFILE_EVALUATOR_SOURCE_SHA256),
          },
        },
        "controller_selection_validation": {
          "schema": "comma-companion.controller-selection-proof",
          "schema_version": 1,
          "state": "verified",
          "checked_row_count": 510,
          "missing_row_count": 0,
          "invalid_row_count": 0,
          "evaluator": {
            "name": "starpilot-controlsd-lateral-selection",
            "version": 1,
            "source_sha256": (CONTROLLER_SELECTION_EVALUATOR_SOURCE_SHA256),
          },
        },
        "effective_torque_context_validation": {
          "schema": "comma-companion.effective-torque-context-proof",
          "schema_version": 1,
          "state": "verified",
          "checked_row_count": 510,
          "exact_row_count": 510,
          "inexact_row_count": 0,
          "missing_field_row_count": 0,
          "valid_row_count": 510,
          "invalid_row_count": 0,
          "stateful_invalid_row_count": 0,
          "context_not_bound_to_controls_row_count": 0,
          "source_after_controls_row_count": 0,
          "source_identity_invalid_count": 0,
          "factor_source_counts": {"car_params": 510},
          "offset_source_counts": {"car_params": 510},
          "friction_source_counts": {"car_params": 510},
          "evaluator": {
            "name": TORQUE_CONTEXT_EVALUATOR,
            "version": 1,
            "source_sha256": (TORQUE_CONTEXT_EVALUATOR_SOURCE_SHA256),
          },
        },
      },
    },
    "baseline_params": copy.deepcopy(FIXTURE_BASELINE_PARAMS),
    "rows": input_rows or rows(),
    "candidate_params": {"base_lat_accel_factor_mult": 1.12},
  }


def bind_request_to_profile(
  replay_request: dict[str, Any],
  profile_id: str,
) -> None:
  profile = REVIEWED_CONTROLLER_PROFILES[profile_id]
  baseline_params = asdict(profile.parameters)
  replay_request["baseline_params"] = copy.deepcopy(baseline_params)
  provenance = replay_request["controller_provenance"]
  provenance["car_params_provenance"]["source_starpilot_commit"] = profile.source_starpilot_commit
  baseline_profile = provenance["tuning_provenance"]["baseline_controller_profile"]
  baseline_profile.update(
    {
      "profile_id": profile.profile_id,
      "source_starpilot_commit": profile.source_starpilot_commit,
      "baseline_controller_params": copy.deepcopy(baseline_params),
      "baseline_controller_params_sha256": profile.params_sha256,
      "vehicle_lat_accel_factor_multiplier": (profile.parameters.base_lat_accel_factor_mult),
    }
  )
  baseline_profile["evaluator"]["source_commit"] = profile.source_starpilot_commit
  for row in replay_request["rows"]:
    row["vehicle_lat_accel_factor_multiplier"] = profile.parameters.base_lat_accel_factor_mult
    row["baseline_controller_profile_id"] = profile.profile_id
    row["baseline_controller_params_sha256"] = profile.params_sha256
    row["baseline_controller_source_starpilot_commit"] = profile.source_starpilot_commit


def warning_codes(result: dict[str, Any]) -> set[str]:
  return {warning["code"] for warning in result["warnings"]}


def test_replay_is_deterministic_and_returns_explicit_trace_sets() -> None:
  first = replay(FakePlant(), request())
  second = replay(FakePlant(), request())
  assert first == second
  assert first["exact_baseline"] is False
  assert first["eligible"]
  assert first["window"]["history_steps"] == 300
  assert len(first["recorded"]["t_us"]) == 5
  assert first["baseline"]["signals"]["actual_lateral_accel"]["mean"] != []
  assert first["candidate"]["signals"]["actual_lateral_accel"]["mean"] != []
  assert first["comparison"]["candidate_minus_baseline"]["modeled_applied_torque"] != [0.0] * 5
  assert {
    "requested_output",
    "reused_actuator_limiter_gap",
    "modeled_applied_torque",
  } <= set(first["baseline"]["signals"])
  assert "applied_torque" not in first["baseline"]["signals"]


def test_rollout_derives_unsigned_rate_from_counterfactual_signed_rate() -> None:
  plant = FakePlant()
  replay(plant, request())
  assert len(plant.inputs) >= 2
  second_step = plant.inputs[1]
  unsigned = second_step[:, 0, BASE_FEATURES.index("steering_rate_deg")]
  signed = second_step[:, 0, BASE_FEATURES.index("signed_steering_rate_deg_s")]
  assert np.allclose(unsigned, np.abs(signed))


def test_plant_lag_zero_is_anchor_while_controller_warmup_is_pre_anchor() -> None:
  input_rows = rows()
  input_rows[299]["steering_angle_deg"] = 2.0
  input_rows[300]["steering_angle_deg"] = 7.0
  plant = FakePlant()
  replay(plant, request(input_rows))
  initial = plant.inputs[0]
  assert np.all(
    initial[:, 0, BASE_FEATURES.index("steering_angle_deg")] == 7.0,
  )
  assert np.all(
    initial[:, 1, BASE_FEATURES.index("steering_angle_deg")] == 2.0,
  )


def test_quality_conditions_block_eligibility_but_remain_visible() -> None:
  input_rows = rows()
  input_rows[300]["v_ego"] = 2.0
  input_rows[301]["lat_active"] = 0.0
  input_rows[302]["driver_overlay"] = 1.0
  input_rows[303]["saturated"] = 1.0
  input_rows[304]["nominal_t_us"] = int(input_rows[304]["nominal_t_us"]) + 10_000
  plant = FakePlant(
    {
      "max_abs_z": 13.0,
      "p99_abs_z": 7.0,
      "fraction_over_6": 0.02,
    }
  )
  result = replay(plant, request(input_rows))
  assert not result["eligible"]
  assert {
    "nominal_grid_invalid",
    "low_speed",
    "lateral_inactive",
    "driver_overlay",
    "logged_saturation",
    "out_of_distribution",
  } <= warning_codes(result)
  assert result["recorded"]["t_us"]


def test_nominal_timestamps_must_be_on_the_absolute_logmonotime_grid() -> None:
  shifted = rows()
  for row in shifted:
    row["nominal_log_mono_time_ns"] = str(
      int(row["nominal_log_mono_time_ns"]) + 1_000_000,
    )
  result = replay(FakePlant(), request(shifted))
  assert not result["eligible"]
  assert "nominal_grid_invalid" in warning_codes(result)

  numeric = rows()
  numeric[250]["nominal_log_mono_time_ns"] = int(
    numeric[250]["nominal_log_mono_time_ns"],
  )
  numeric_result = replay(FakePlant(), request(numeric))
  assert not numeric_result["eligible"]
  assert "nominal_grid_invalid" in warning_codes(numeric_result)


def test_route_relative_nominal_time_may_have_a_constant_nonzero_phase() -> None:
  input_rows = rows()
  origin_ns = FIXTURE_ROUTE_ORIGIN_NS + 3_000
  for row in input_rows:
    nominal_ns = int(row["nominal_log_mono_time_ns"])
    row["nominal_t_us"] = (nominal_ns - origin_ns) // 1_000
    row["t_us"] = row["nominal_t_us"]
    row["source_t_us"] = int(row["nominal_t_us"]) - int(
      row["car_state_age_us"],
    )
  replay_request = request(input_rows)
  replay_request["telemetry_provenance"]["route_origin_log_mono_time_ns"] = str(origin_ns)
  result = replay(FakePlant(), replay_request)
  assert result["eligible"]
  assert result["window"]["anchor_t_us"] % 10_000 == 9_997


def test_causal_zoh_source_timestamps_are_allowed_but_discontinuity_blocks() -> None:
  jittered = rows()
  jittered[250]["source_t_us"] = jittered[249]["source_t_us"]
  jittered[251]["source_t_us"] = int(jittered[249]["source_t_us"]) + 20_000
  for index in (250, 251):
    jittered[index]["car_state_age_us"] = int(
      jittered[index]["nominal_t_us"],
    ) - int(jittered[index]["source_t_us"])
    jittered[index]["source_time_error_us"] = -int(
      jittered[index]["car_state_age_us"],
    )
  assert "source_telemetry_gap" not in warning_codes(
    replay(FakePlant(), request(jittered)),
  )

  gapped = rows()
  gapped[250]["continuous"] = False
  result = replay(FakePlant(), request(gapped))
  assert not result["eligible"]
  assert "source_telemetry_gap" in warning_codes(result)


def test_qlog_or_unspecified_log_type_can_never_be_eligible() -> None:
  for source_log_type in (None, "qlog"):
    replay_request = request()
    replay_request["source_log_type"] = source_log_type
    result = replay(FakePlant(), replay_request)
    assert not result["eligible"]
    assert "source_log_type_ineligible" in warning_codes(result)


def test_future_or_unproven_service_join_blocks_causal_claim() -> None:
  missing = rows()
  for row in missing:
    row.pop("live_parameters_age_us")
  missing_result = replay(FakePlant(), request(missing))
  assert "source_age_provenance_missing" in warning_codes(missing_result)

  future = rows()
  future[250]["controls_state_age_us"] = -1
  future_result = replay(FakePlant(), request(future))
  assert not future_result["eligible"]
  assert "noncausal_source_join" in warning_codes(future_result)

  future_car_state = rows()
  future_car_state[250]["source_t_us"] = (
    int(
      future_car_state[250]["nominal_t_us"],
    )
    + 1
  )
  future_car_state[250]["source_time_error_us"] = 1
  future_car_state[250]["car_state_age_us"] = -1
  source_result = replay(FakePlant(), request(future_car_state))
  assert not source_result["eligible"]
  assert "source_time_inconsistent" in warning_codes(source_result)


def test_artifact_asof_age_limit_allows_boundary_and_blocks_stale_source() -> None:
  boundary = rows()
  boundary[250]["car_state_age_us"] = 35_000
  boundary[250]["source_time_error_us"] = -35_000
  boundary[250]["source_t_us"] = (
    int(
      boundary[250]["nominal_t_us"],
    )
    - 35_000
  )
  boundary_result = replay(FakePlant(), request(boundary))
  assert boundary_result["eligible"]
  assert "source_age_limit_exceeded" not in warning_codes(boundary_result)

  for field in (
    "car_state_age_us",
    "car_control_age_us",
    "controls_state_age_us",
    "car_output_age_us",
  ):
    stale = rows()
    stale[250][field] = 35_001
    if field == "car_state_age_us":
      stale[250]["source_time_error_us"] = -35_001
      stale[250]["source_t_us"] = (
        int(
          stale[250]["nominal_t_us"],
        )
        - 35_001
      )
    result = replay(FakePlant(), request(stale))
    assert not result["eligible"]
    warning = next(item for item in result["warnings"] if item["code"] == "source_age_limit_exceeded")
    assert warning["max_asof_age_ms"] == 35.0
    assert warning["fields"] == [field]

  fractional = rows()
  fractional[250]["car_output_age_us"] = 35_000.9
  fractional_result = replay(
    FakePlant(),
    request(fractional),
  )
  assert not fractional_result["eligible"]
  assert "source_age_provenance_missing" in warning_codes(
    fractional_result,
  )


def test_requested_carcontrol_torque_is_never_an_applied_plant_fallback() -> None:
  input_rows = rows()
  input_rows[300]["applied_torque_source"] = "carControl.actuators.torque"
  result = replay(FakePlant(), request(input_rows))
  assert not result["eligible"]
  warning = next(item for item in result["warnings"] if item["code"] == "applied_torque_source_ineligible")
  assert warning["expected"] == "carOutput.actuatorsOutput.torque"


def test_live_controller_sources_use_separate_validity_and_cadence_gates() -> None:
  boundary = rows()
  boundary[250]["live_parameters_age_us"] = 250_000
  assert "live_parameters_invalid" not in warning_codes(
    replay(FakePlant(), request(boundary)),
  )

  stale_parameters = rows()
  stale_parameters[250]["live_parameters_age_us"] = 250_001
  parameters_result = replay(FakePlant(), request(stale_parameters))
  assert not parameters_result["eligible"]
  assert "live_parameters_invalid" in warning_codes(parameters_result)

  selected_torque = rows()
  selected_torque[250].update(
    {
      "live_torque_valid": True,
      "live_torque_in_use": True,
      "live_torque_used": True,
      "live_torque_age_us": 1_000_000,
      "live_lat_accel_factor": 3.0,
      "live_lat_accel_offset": -0.1,
      "live_friction": 0.08,
      "effective_torque_params_source": {
        "factor": "live_filtered",
        "offset": "live_filtered",
        "friction": "live_filtered",
      },
      "effective_torque_params_source_age_us": {
        "factor": 1_000_000,
        "offset": 1_000_000,
        "friction": 1_000_000,
      },
    }
  )
  assert "live_torque_diagnostics_invalid" not in warning_codes(
    replay(FakePlant(), request(selected_torque)),
  )

  invalid_torque = copy.deepcopy(selected_torque)
  invalid_torque[250]["live_torque_valid"] = False
  invalid_torque[250]["live_torque_frequency_ok"] = False
  torque_result = replay(FakePlant(), request(invalid_torque))
  assert not torque_result["eligible"]
  assert "live_torque_diagnostics_invalid" in warning_codes(torque_result)

  inexact_context = rows()
  inexact_context[250]["effective_torque_params_exact"] = False
  inexact_context[250]["effective_torque_params_missing_fields"] = [
    "custom_friction_selection",
  ]
  context_result = replay(FakePlant(), request(inexact_context))
  assert not context_result["eligible"]
  assert "effective_torque_params_inexact" in warning_codes(context_result)

  impossible_base_selection = copy.deepcopy(selected_torque)
  impossible_base_selection[250].update(
    {
      "base_lat_accel_factor": BASE_LAT_ACCEL_FACTOR,
      "base_lat_accel_offset": 0.0,
      "base_friction": BASE_FRICTION,
      "effective_torque_params_source": {
        "factor": "car_params",
        "offset": "car_params",
        "friction": "car_params",
      },
      "effective_torque_params_source_age_us": {
        "factor": None,
        "offset": None,
        "friction": None,
      },
      "effective_torque_params_stateful": True,
      "effective_torque_params_state_machine_version": 1,
      "effective_torque_params_value_space": (CONTROLLER_PARAMS_VALUE_SPACE),
      "vehicle_lat_accel_factor_multiplier": (FIXTURE_CONTROLLER_PROFILE.parameters.base_lat_accel_factor_mult),
    }
  )
  for name in (
    "live_lat_accel_factor",
    "live_lat_accel_offset",
    "live_friction",
  ):
    impossible_base_selection[250].pop(name)
  impossible_result = replay(
    FakePlant(),
    request(impossible_base_selection),
  )
  assert not impossible_result["eligible"]
  assert "effective_torque_params_inexact" in warning_codes(
    impossible_result,
  )

  malformed_missing_fields = rows()
  malformed_missing_fields[250]["effective_torque_params_missing_fields"] = "effective_friction"
  malformed_result = replay(
    FakePlant(),
    request(malformed_missing_fields),
  )
  assert not malformed_result["eligible"]
  assert "effective_torque_params_inexact" in warning_codes(
    malformed_result,
  )


def test_wrong_car_and_insufficient_history_are_hard_contract_errors() -> None:
  wrong_car = request()
  wrong_car["car_fingerprint"] = "OTHER"
  with pytest.raises(DynamicsContractError, match="Ioniq 5") as error:
    replay(FakePlant(), wrong_car)
  assert error.value.code == "wrong_car"

  no_history = request()
  no_history["anchor_index"] = 100
  with pytest.raises(DynamicsContractError) as error:
    replay(FakePlant(), no_history)
  assert error.value.code == "insufficient_history"


def test_subsample_positive_horizon_rounds_to_one_model_step() -> None:
  replay_request = request()
  replay_request["horizon_s"] = 0.001
  result = replay(FakePlant(), replay_request)
  assert result["window"]["horizon_steps"] == 1
  assert result["window"]["horizon_s"] == 0.01


def test_missing_unsigned_rate_uses_visible_fallback_but_blocks() -> None:
  input_rows = copy.deepcopy(rows())
  for row in input_rows:
    row.pop("steering_rate_deg")
    row["signed_steering_rate_deg_s"] = -3.0
  plant = FakePlant()
  result = replay(plant, request(input_rows))
  initial = plant.inputs[0]
  assert np.all(initial[:, 0, BASE_FEATURES.index("steering_rate_deg")] == 3.0)
  assert not result["eligible"]
  assert "raw_unsigned_steering_rate_missing" in warning_codes(result)


def test_missing_context_timing_and_provenance_are_explicit_blockers() -> None:
  input_rows = rows()
  for row in input_rows:
    for name in (
      "base_lat_accel_factor",
      "base_lat_accel_offset",
      "base_friction",
      "steering_angle_deadzone_deg",
    ):
      row.pop(name)
  replay_request = request(input_rows)
  replay_request.pop("controller_i_timing")
  replay_request.pop("controller_provenance")
  result = replay(FakePlant(), replay_request)
  assert {
    "controller_context_missing",
    "controller_i_timing_unverified",
    "controller_provenance_missing",
  } <= warning_codes(result)


def test_active_unmodeled_runtime_override_blocks_baseline_fidelity() -> None:
  replay_request = request()
  replay_request["controller_provenance"]["toggle_snapshot"]["flm_active"] = True
  result = replay(FakePlant(), replay_request)
  assert not result["eligible"]
  assert "unsupported_runtime_overrides" in warning_codes(result)

  negative_trailer = request()
  negative_trailer["controller_provenance"]["toggle_snapshot"]["trailer_load_kg"] = -1
  negative_result = replay(FakePlant(), negative_trailer)
  assert not negative_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    negative_result,
  )


def test_controller_provenance_proofs_are_strict_and_scoped() -> None:
  missing_snapshot = request()
  missing_snapshot["controller_provenance"].pop("toggle_snapshot")
  missing_snapshot["controller_provenance"].pop("tuning_snapshot")
  missing_result = replay(FakePlant(), missing_snapshot)
  assert not missing_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    missing_result,
  )

  numeric_flm = request()
  numeric_flm["controller_provenance"]["toggle_snapshot"]["flm_active"] = 1
  numeric_result = replay(FakePlant(), numeric_flm)
  assert not numeric_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    numeric_result,
  )

  fallback_without_resolution = request()
  fallback_without_resolution["controller_provenance"]["toggle_snapshot"]["controller_selection_source_types"] = [
    "versioned_initData_fallback",
  ]
  fallback_result = replay(
    FakePlant(),
    fallback_without_resolution,
  )
  assert not fallback_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    fallback_result,
  )

  unscoped = request()
  unscoped["controller_provenance"]["tuning_provenance"]["controller_selection_validation"]["checked_row_count"] = 0
  unscoped_result = replay(FakePlant(), unscoped)
  assert not unscoped_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    unscoped_result,
  )

  wrong_car_params = request()
  wrong_car_params["controller_provenance"]["car_params"]["car_fingerprint"] = "OTHER"
  wrong_car_params_result = replay(
    FakePlant(),
    wrong_car_params,
  )
  assert not wrong_car_params_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    wrong_car_params_result,
  )

  missing_row_binding = request()
  missing_row_binding["rows"][250].pop(
    "resolved_toggles_sha256",
  )
  row_binding_result = replay(
    FakePlant(),
    missing_row_binding,
  )
  assert not row_binding_result["eligible"]
  assert "controller_row_provenance_invalid" in warning_codes(
    row_binding_result,
  )


def test_row_causality_proofs_must_be_explicit_and_consistent() -> None:
  missing_continuity = rows()
  missing_continuity[250].pop("continuous")
  missing_continuity_result = replay(
    FakePlant(),
    request(missing_continuity),
  )
  assert not missing_continuity_result["eligible"]
  assert "source_telemetry_gap" in warning_codes(
    missing_continuity_result,
  )

  inconsistent_source = rows()
  inconsistent_source[250]["source_t_us"] = int(inconsistent_source[250]["source_t_us"]) - 1
  inconsistent_result = replay(
    FakePlant(),
    request(inconsistent_source),
  )
  assert not inconsistent_result["eligible"]
  assert "source_time_inconsistent" in warning_codes(
    inconsistent_result,
  )


def test_flm_state_requires_recorded_or_versioned_historical_proof() -> None:
  unknown = request()
  unknown["controller_provenance"]["toggle_snapshot"]["flm_active_available"] = False
  unknown_result = replay(FakePlant(), unknown)
  assert not unknown_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    unknown_result,
  )

  historical = request()
  historical["controller_provenance"]["toggle_snapshot"].update(
    {
      "controller_selection_source_types": [
        "versioned_initData_fallback",
      ],
      "flm_resolution": {
        "state": "verified",
        "source": "versioned_source_commit_evaluator",
        "flm_active": False,
        "evaluator": {
          "name": "starpilot-flm-availability-by-source-commit",
          "version": 1,
          "source_commit": "2747bf037c0f284500457f1befb4f52415e3285a",
          "source_sha256": HISTORICAL_FLM_EVALUATOR_SOURCE_SHA256,
        },
      },
      "init_data_fallback_evaluator": {
        "state": "available",
        "name": TORQUE_CONTEXT_EVALUATOR,
        "version": 1,
        "evaluator_id": (TORQUE_CONTEXT_EVALUATOR_IDS["2747bf037c0f284500457f1befb4f52415e3285a"]),
        "source_commit": "2747bf037c0f284500457f1befb4f52415e3285a",
        "source_sha256": TORQUE_CONTEXT_EVALUATOR_SOURCE_SHA256,
      },
    }
  )
  historical["controller_provenance"]["car_params_provenance"]["source_starpilot_commit"] = (
    "2747bf037c0f284500457f1befb4f52415e3285a"
  )
  for row in historical["rows"]:
    row["controller_selection_source"] = "versioned_initData_fallback"
  assert replay(FakePlant(), historical)["eligible"]

  early = request()
  bind_request_to_profile(
    early,
    EARLY_HISTORICAL_CONTROLLER_PROFILE_ID,
  )
  early["controller_provenance"]["toggle_snapshot"].update(
    {
      "controller_selection_source_types": [
        "versioned_initData_fallback",
      ],
      "flm_resolution": {
        "state": "verified",
        "source": "versioned_source_commit_evaluator",
        "flm_active": False,
        "evaluator": {
          "name": "starpilot-flm-availability-by-source-commit",
          "version": 1,
          "source_commit": ("6dd6c0a3d558842b91b903e1cddfaca576a69c25"),
          "source_sha256": HISTORICAL_FLM_EVALUATOR_SOURCE_SHA256,
        },
      },
      "init_data_fallback_evaluator": {
        "state": "available",
        "name": TORQUE_CONTEXT_EVALUATOR,
        "version": 1,
        "evaluator_id": (TORQUE_CONTEXT_EVALUATOR_IDS["6dd6c0a3d558842b91b903e1cddfaca576a69c25"]),
        "source_commit": ("6dd6c0a3d558842b91b903e1cddfaca576a69c25"),
        "source_sha256": TORQUE_CONTEXT_EVALUATOR_SOURCE_SHA256,
      },
    }
  )
  for row in early["rows"]:
    row["controller_selection_source"] = "versioned_initData_fallback"
  assert replay(FakePlant(), early)["eligible"]

  crossed = copy.deepcopy(early)
  crossed["controller_provenance"]["toggle_snapshot"]["flm_resolution"]["evaluator"]["source_commit"] = (
    "2747bf037c0f284500457f1befb4f52415e3285a"
  )
  crossed_result = replay(FakePlant(), crossed)
  assert not crossed_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(crossed_result)

  tampered = copy.deepcopy(early)
  tampered["controller_provenance"]["toggle_snapshot"]["flm_resolution"]["evaluator"]["source_sha256"] = "0" * 64
  tampered_result = replay(FakePlant(), tampered)
  assert not tampered_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    tampered_result,
  )

  fallback_evaluator_tamper = copy.deepcopy(early)
  fallback_evaluator_tamper["controller_provenance"]["toggle_snapshot"]["init_data_fallback_evaluator"][
    "source_sha256"
  ] = "0" * 64
  fallback_evaluator_tamper_result = replay(
    FakePlant(),
    fallback_evaluator_tamper,
  )
  assert not fallback_evaluator_tamper_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    fallback_evaluator_tamper_result,
  )


@pytest.mark.parametrize(
  ("field", "value"),
  (
    ("schema_version", True),
    ("missing_row_count", False),
    ("invalid_row_count", False),
  ),
)
def test_controller_selection_proof_integer_fields_reject_booleans(
  field: str,
  value: bool,
) -> None:
  replay_request = request()
  proof = replay_request["controller_provenance"]["tuning_provenance"]["controller_selection_validation"]
  proof[field] = value
  result = replay(FakePlant(), replay_request)
  assert not result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(result)


def test_controller_selection_evaluator_version_rejects_boolean() -> None:
  replay_request = request()
  proof = replay_request["controller_provenance"]["tuning_provenance"]["controller_selection_validation"]
  proof["evaluator"]["version"] = True
  result = replay(FakePlant(), replay_request)
  assert not result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(result)


def test_controller_evaluator_source_hashes_are_pinned() -> None:
  profile_hash = request()
  profile_hash["controller_provenance"]["tuning_provenance"]["baseline_controller_profile"]["evaluator"][
    "source_sha256"
  ] = "0" * 64
  profile_result = replay(FakePlant(), profile_hash)
  assert not profile_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    profile_result,
  )

  selection_hash = request()
  selection_hash["controller_provenance"]["tuning_provenance"]["controller_selection_validation"]["evaluator"][
    "source_sha256"
  ] = "0" * 64
  selection_result = replay(FakePlant(), selection_hash)
  assert not selection_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    selection_result,
  )

  torque_context_hash = request()
  torque_context_hash["controller_provenance"]["tuning_provenance"]["effective_torque_context_validation"]["evaluator"][
    "source_sha256"
  ] = "0" * 64
  torque_context_result = replay(
    FakePlant(),
    torque_context_hash,
  )
  assert not torque_context_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    torque_context_result,
  )


def test_effective_torque_context_proof_is_exact_and_complete() -> None:
  missing = request()
  missing["controller_provenance"]["tuning_provenance"].pop(
    "effective_torque_context_validation",
  )
  missing_result = replay(FakePlant(), missing)
  assert not missing_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    missing_result,
  )

  invalid_count = request()
  invalid_count["controller_provenance"]["tuning_provenance"]["effective_torque_context_validation"][
    "source_identity_invalid_count"
  ] = 1
  invalid_count_result = replay(FakePlant(), invalid_count)
  assert not invalid_count_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    invalid_count_result,
  )

  source_total = request()
  source_total["controller_provenance"]["tuning_provenance"]["effective_torque_context_validation"][
    "factor_source_counts"
  ]["car_params"] = 509
  source_total_result = replay(FakePlant(), source_total)
  assert not source_total_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    source_total_result,
  )


def test_historical_baseline_profile_is_exactly_bound_to_request() -> None:
  missing_parameter = request()
  missing_parameter["baseline_params"].pop("base_lat_accel_factor_mult")
  missing_result = replay(FakePlant(), missing_parameter)
  assert not missing_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    missing_result,
  )

  wrong_multiplier = request()
  wrong_multiplier["controller_provenance"]["tuning_provenance"]["baseline_controller_profile"][
    "vehicle_lat_accel_factor_multiplier"
  ] = 1.36
  multiplier_result = replay(FakePlant(), wrong_multiplier)
  assert not multiplier_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    multiplier_result,
  )


def test_reviewed_profiles_cannot_be_cross_selected() -> None:
  early = request()
  bind_request_to_profile(early, EARLY_HISTORICAL_CONTROLLER_PROFILE_ID)
  assert replay(FakePlant(), early)["eligible"]

  current = request()
  bind_request_to_profile(current, CURRENT_CONTROLLER_PROFILE_ID)
  assert replay(FakePlant(), current)["eligible"]

  crossed_request = copy.deepcopy(current)
  crossed_request["controller_provenance"]["tuning_provenance"]["baseline_controller_profile"]["profile_id"] = (
    HISTORICAL_CONTROLLER_PROFILE_ID
  )
  crossed_result = replay(FakePlant(), crossed_request)
  assert not crossed_result["eligible"]
  assert "controller_provenance_incomplete" in warning_codes(
    crossed_result,
  )

  crossed_row = copy.deepcopy(current)
  crossed_row["rows"][250]["baseline_controller_profile_id"] = HISTORICAL_CONTROLLER_PROFILE_ID
  row_result = replay(FakePlant(), crossed_row)
  assert not row_result["eligible"]
  assert "effective_torque_params_inexact" in warning_codes(row_result)


@pytest.mark.parametrize(
  ("field", "value"),
  (
    ("baseline_controller_profile_id", "unreviewed-controller-profile"),
    ("baseline_controller_params_sha256", "0" * 64),
    ("baseline_controller_source_starpilot_commit", "0" * 40),
  ),
)
def test_every_row_is_bound_to_the_reviewed_baseline_profile(
  field: str,
  value: str,
) -> None:
  input_rows = rows()
  input_rows[250][field] = value
  result = replay(FakePlant(), request(input_rows))
  assert not result["eligible"]
  assert "effective_torque_params_inexact" in warning_codes(result)


def test_stateful_controller_and_effective_context_proofs_are_required() -> None:
  selection_rows = rows()
  selection_rows[250]["controller_selection_stateful"] = False
  selection_result = replay(
    FakePlant(),
    request(selection_rows),
  )
  assert not selection_result["eligible"]
  assert "controller_row_provenance_invalid" in warning_codes(
    selection_result,
  )

  post_controls_rows = rows()
  post_controls_rows[250].update(
    {
      "effective_lat_accel_factor": 3.0,
      "effective_lat_accel_offset": -0.1,
      "effective_friction": 0.08,
      "effective_torque_params_source": {
        "factor": "live_filtered",
        "offset": "live_filtered",
        "friction": "live_filtered",
      },
      "effective_torque_params_source_age_us": {
        "factor": 2_000,
        "offset": 2_000,
        "friction": 2_000,
      },
    }
  )
  post_controls_result = replay(
    FakePlant(),
    request(post_controls_rows),
  )
  assert not post_controls_result["eligible"]
  assert "effective_torque_params_inexact" in warning_codes(
    post_controls_result,
  )

  retained_rows = copy.deepcopy(post_controls_rows)
  retained_rows[250]["effective_torque_params_source_age_us"] = {
    "factor": 4_000,
    "offset": 4_000,
    "friction": 4_000,
  }
  retained_result = replay(FakePlant(), request(retained_rows))
  assert retained_result["eligible"]


def test_inexact_feedforward_and_integrator_freeze_warn_without_blocking_approximate_replay() -> None:
  input_rows = rows()
  input_rows[300]["future_feedforward_exact"] = False
  input_rows[300]["integrator_freeze_exact"] = False
  result = replay(FakePlant(), request(input_rows))
  assert result["eligible"]
  warnings = {warning["code"]: warning for warning in result["warnings"]}
  assert warnings["future_feedforward_inexact"]["severity"] == "warning"
  assert warnings["integrator_freeze_inexact"]["severity"] == "warning"


def test_legacy_model_alignment_is_always_blocked_for_causal_rows() -> None:
  result = replay(
    FakePlant(causal_input_eligible=False),
    request(),
  )
  assert not result["eligible"]
  assert "model_training_alignment_mismatch" in warning_codes(result)


def test_replay_requires_promoted_reviewed_model_flags_with_strict_booleans() -> None:
  for plant in (
    FakePlant(promoted_artifact_verified=False),
    FakePlant(review_registry_match=False),
    FakePlant(promoted_artifact_verified=1),
    FakePlant(review_registry_match=1),
  ):
    result = replay(plant, request())
    assert not result["eligible"]
    assert "model_training_alignment_mismatch" in warning_codes(
      result,
    )


def test_telemetry_provenance_must_be_causal_and_match_model_extractor() -> None:
  replay_request = request()
  replay_request["telemetry_provenance"] = {
    **replay_request["telemetry_provenance"],
    "causal_input_eligible": False,
  }
  telemetry_result = replay(FakePlant(), replay_request)
  assert not telemetry_result["eligible"]
  assert "telemetry_provenance_mismatch" in warning_codes(telemetry_result)

  model_result = replay(
    FakePlant(),
    {
      **request(),
      "telemetry_provenance": {
        **request()["telemetry_provenance"],
        "extractor_source_sha256": "b" * 64,
      },
    },
  )
  assert not model_result["eligible"]
  assert "model_training_alignment_mismatch" in warning_codes(model_result)

  version_result = replay(
    FakePlant(),
    {
      **request(),
      "telemetry_provenance": {
        **request()["telemetry_provenance"],
        "extractor_version": "1.1.1",
      },
    },
  )
  assert not version_result["eligible"]
  version_warning = next(
    item for item in version_result["warnings"] if item["code"] == "model_training_alignment_mismatch"
  )
  assert version_warning["mismatches"]["compatible_telemetry_extractor_version"] == {
    "expected": "1.1.1",
    "actual": FIXTURE_EXTRACTOR_VERSION,
  }

  age_contract_result = replay(
    FakePlant(max_asof_age_ms=40.0),
    request(),
  )
  assert not age_contract_result["eligible"]
  warning = next(
    item for item in age_contract_result["warnings"] if item["code"] == "model_training_alignment_mismatch"
  )
  assert warning["mismatches"]["max_asof_age_ms"] == {
    "expected": 35.0,
    "actual": 40.0,
  }


def test_bad_baseline_fit_is_gated_by_reviewed_envelope() -> None:
  result = replay(FakePlant(fit_limit=1e-8), request())
  assert not result["eligible"]
  assert "baseline_fit_outside_reviewed_envelope" in warning_codes(result)
  assert result["quality"]["baseline_fit_envelope"]["exceeded"]


def test_nonfinite_or_incomplete_model_outputs_fail_closed() -> None:
  with pytest.raises(DynamicsContractError) as ood_error:
    replay(
      FakePlant(
        ood={
          "max_abs_z": math.nan,
          "p99_abs_z": 1.0,
          "fraction_over_6": 0.0,
        }
      ),
      request(),
    )
  assert ood_error.value.code == "nonfinite_model_output"

  class NonfiniteDeltaPlant(FakePlant):
    def predict_scenario_deltas(
      self,
      histories: np.ndarray,
    ) -> np.ndarray:
      result = super().predict_scenario_deltas(histories)
      result[0, 0, 0] = math.nan
      return result

  with pytest.raises(DynamicsContractError) as delta_error:
    replay(NonfiniteDeltaPlant(), request())
  assert delta_error.value.code == "nonfinite_model_output"

  class PartialEnvelopePlant(FakePlant):
    def fit_envelope(self, horizon_s: float) -> dict[str, Any]:
      return {
        "horizon_s": horizon_s,
        "metric": "partial",
        "limits": {"actual_lateral_accel": 1.0},
      }

  partial_result = replay(PartialEnvelopePlant(), request())
  assert not partial_result["eligible"]
  assert "baseline_fit_envelope_invalid" in warning_codes(
    partial_result,
  )

  invalid_scale = FakePlant()
  invalid_scale.state_scale = np.asarray(
    [1.0, 1.0, math.nan, 1.0],
  )
  with pytest.raises(DynamicsContractError) as scale_error:
    replay(invalid_scale, request())
  assert scale_error.value.code == "model_contract_mismatch"


def test_marginal_baseline_fit_is_downgraded_without_false_block() -> None:
  result = replay(
    FakePlant(fit_limit=100.0, fit_warning_limit=1e-8),
    request(),
  )
  assert result["eligible"]
  warning = next(item for item in result["warnings"] if item["code"] == "baseline_fit_degraded")
  assert warning["severity"] == "warning"
  assert result["quality"]["baseline_fit_envelope"]["degraded"]


def test_large_or_discontinuous_limiter_gap_warns_then_blocks() -> None:
  warning_rows = rows()
  warning_rows[300]["applied_torque"] = 0.11
  warning_result = replay(FakePlant(), request(warning_rows))
  limiter_warning = next(item for item in warning_result["warnings"] if item["code"] == "actuator_limiter_gap")
  assert limiter_warning["severity"] == "warning"

  blocker_rows = rows()
  blocker_rows[300]["applied_torque"] = 0.21
  blocker_result = replay(FakePlant(), request(blocker_rows))
  assert not blocker_result["eligible"]
  limiter_blocker = next(item for item in blocker_result["warnings"] if item["code"] == "actuator_limiter_gap")
  assert limiter_blocker["severity"] == "blocker"


def test_model_only_parameter_is_labeled_without_hiding_trace() -> None:
  replay_request = request()
  replay_request["candidate_params"] = {"turn_exit_damping_gain": 0.03}
  result = replay(FakePlant(), replay_request)
  warning = next(item for item in result["warnings"] if item["code"] == "model_only_parameters")
  assert warning["severity"] == "warning"
  assert warning["parameters"] == ["turn_exit_damping_gain"]
