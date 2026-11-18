from __future__ import annotations

from typing import Any

PROTOCOL_VERSION = 1
MODE = "approximate_closed_loop"
REFERENCE_CAR_FINGERPRINT = "HYUNDAI_IONIQ_5"
SAMPLE_PERIOD_S = 0.01
SAMPLE_PERIOD_US = 10_000
SAMPLE_PERIOD_NS = 10_000_000
HISTORY_STEPS = 300
DEFAULT_HORIZON_S = 1.0
MAX_HORIZON_S = 2.0
MAX_ASOF_AGE_MS = 35.0
LIVE_PARAMETERS_MAX_AGE_MS = 250.0
LIVE_TORQUE_MAX_AGE_MS = 1_000.0
LIVE_TORQUE_CADENCE_POLICY = "causal_timestamp_history"
LIVE_TORQUE_CADENCE_POLICY_VERSION = 1
SAMPLING_GRID = "absolute_monotonic_time"
SAMPLING_ALIGNMENT = "latest_at_or_before_grid_time_zero_order_hold"
SAMPLING_EVENT_ORDER = ("logMonoTime", "source_ordinal")
TRAINING_EXTRACTION_VERSION = 9
TRAINING_CONTRACT_VERSION = 10
TRAINER_SCHEMA = "starpilot.neural-lateral-plant"
TRAINER_SCHEMA_VERSION = 8
RECURSIVE_OBJECTIVE_HORIZON_S = 0.5


def causal_sampling_contract() -> dict[str, Any]:
  return {
    "grid": SAMPLING_GRID,
    "absolute_grid_field": "nominal_log_mono_time_ns",
    "absolute_grid_phase_ns": 0,
    "sample_period_ns": SAMPLE_PERIOD_NS,
    "sample_rate_hz": 1.0 / SAMPLE_PERIOD_S,
    "first_tick_formula": ("ceil(first_valid_carState_logMonoTime_ns/sample_period_ns)*sample_period_ns"),
    "alignment": SAMPLING_ALIGNMENT,
    "source_selection": ("independent_per_source_max_valid_source_with_logMonoTime_at_or_before_tick"),
    "invalid_event_policy": {
      "carState": "drop_without_invalidating_prior_valid_state",
      "carControl": "invalidate_until_next_valid",
      "controlsState": "invalidate_until_next_valid",
      "carOutput": "invalidate_until_next_valid",
    },
    "signed_steering_rate": ("causal_grid_difference_of_zoh_steering_angle"),
    "applied_torque_source": ("carOutput.actuatorsOutput.torque_only_no_fallback"),
    "source_age_equation": ("source_age_ms=(nominal_log_mono_time_ns-source_log_mono_time_ns)/1e6"),
    "source_time_error_equation": ("source_time_error_ms=-source_age_ms"),
    "no_future_source": True,
    "max_asof_age_ms": MAX_ASOF_AGE_MS,
    "required_asof_sources": [
      "carState",
      "carControl",
      "controlsState",
      "carOutput",
    ],
    "route_relative_time_formula": ("nominal_t_us=(nominal_log_mono_time_ns-route_origin_log_mono_time_ns)//1000"),
    "route_relative_phase_policy": ("constant_nonzero_modulo_allowed_exact_10000us_steps"),
    "event_order": list(SAMPLING_EVENT_ORDER),
  }


BASE_FEATURES = (
  "applied_torque",
  "actual_lateral_accel",
  "steering_angle_deg",
  "steering_rate_deg",
  "signed_steering_rate_deg_s",
  "steering_torque_eps",
  "v_ego",
  "a_ego",
)
STATE_FEATURES = (
  "actual_lateral_accel",
  "steering_angle_deg",
  "signed_steering_rate_deg_s",
  "steering_torque_eps",
)


class DynamicsContractError(ValueError):
  def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
    super().__init__(message)
    self.code = code
    self.message = message
    self.details = details or {}

  def as_dict(self) -> dict[str, Any]:
    return {
      "code": self.code,
      "message": self.message,
      "details": self.details,
    }
