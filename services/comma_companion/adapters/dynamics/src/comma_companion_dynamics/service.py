from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from comma_companion_dynamics.contract import (
  DEFAULT_HORIZON_S,
  HISTORY_STEPS,
  LIVE_PARAMETERS_MAX_AGE_MS,
  LIVE_TORQUE_CADENCE_POLICY,
  LIVE_TORQUE_CADENCE_POLICY_VERSION,
  LIVE_TORQUE_MAX_AGE_MS,
  MAX_ASOF_AGE_MS,
  MAX_HORIZON_S,
  MODE,
  PROTOCOL_VERSION,
  RECURSIVE_OBJECTIVE_HORIZON_S,
  REFERENCE_CAR_FINGERPRINT,
  SAMPLE_PERIOD_S,
  TRAINER_SCHEMA,
  TRAINER_SCHEMA_VERSION,
  TRAINING_CONTRACT_VERSION,
  TRAINING_EXTRACTION_VERSION,
  DynamicsContractError,
  causal_sampling_contract,
)
from comma_companion_dynamics.controller import parameter_schema
from comma_companion_dynamics.plant import Plant, TorchPlantEnsemble, default_model_path
from comma_companion_dynamics.replay import (
  APPLIED_TORQUE_SOURCE,
  CAUSAL_INPUT_ALIGNMENT,
  CONTROLLER_I_TIMING,
  TELEMETRY_SCHEMA,
  TELEMETRY_SCHEMA_VERSION,
  replay,
)


def _sha256_digest(value: Any) -> bool:
  return (
    isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)
  )


class DynamicsService:
  def __init__(
    self,
    plant: Plant | None = None,
    model_path: Path | None = None,
    device: str = "cpu",
    require_reference_hash: bool | None = None,
  ):
    self._plant = plant
    self._model_path = model_path or default_model_path()
    self._device = device
    if require_reference_hash is None:
      require_reference_hash = (
        os.environ.get(
          "COMMA_DYNAMICS_ALLOW_UNVERIFIED_MODEL",
        )
        != "1"
      )
    self._require_reference_hash = require_reference_hash

  @property
  def plant(self) -> Plant:
    if self._plant is None:
      self._plant = TorchPlantEnsemble(
        self._model_path,
        self._device,
        self._require_reference_hash,
      )
    return self._plant

  def model_info(self) -> dict[str, Any]:
    model = self.plant.provenance()
    causal_replay_eligible = (
      model.get("promoted_artifact_verified") is True
      and model.get("review_registry_match") is True
      and model.get("review_manifest_verified") is True
      and _sha256_digest(model.get("review_manifest_sha256"))
      and isinstance(model.get("member_count"), int)
      and not isinstance(model.get("member_count"), bool)
      and model["member_count"] >= 2
      and model.get("reviewed_member_count") == model.get("member_count")
      and model.get("causal_training_eligible") is True
      and model.get("training_alignment") == CAUSAL_INPUT_ALIGNMENT
      and model.get("training_schema") == TELEMETRY_SCHEMA
      and model.get("training_schema_version") == TELEMETRY_SCHEMA_VERSION
      and model.get("training_extraction_version") == TRAINING_EXTRACTION_VERSION
      and _sha256_digest(model.get("training_extractor_sha256"))
      and model.get("training_contract_version") == TRAINING_CONTRACT_VERSION
      and model.get("recursive_objective_horizon_s") == RECURSIVE_OBJECTIVE_HORIZON_S
      and model.get("trainer_schema") == TRAINER_SCHEMA
      and model.get("trainer_schema_version") == TRAINER_SCHEMA_VERSION
      and _sha256_digest(model.get("trainer_sha256"))
      and isinstance(
        model.get("compatible_telemetry_extractor_version"),
        str,
      )
      and bool(model.get("compatible_telemetry_extractor_version"))
      and _sha256_digest(
        model.get("compatible_telemetry_extractor_sha256"),
      )
      and model.get("max_asof_age_ms") == MAX_ASOF_AGE_MS
      and model.get("sampling") == causal_sampling_contract()
    )
    return {
      "protocol_version": PROTOCOL_VERSION,
      "mode": MODE,
      "target_car_fingerprint": REFERENCE_CAR_FINGERPRINT,
      "sample_period_s": SAMPLE_PERIOD_S,
      "history_steps": HISTORY_STEPS,
      "history_s": HISTORY_STEPS * SAMPLE_PERIOD_S,
      "default_horizon_s": DEFAULT_HORIZON_S,
      "max_horizon_s": MAX_HORIZON_S,
      "model": model,
      "parameter_schema": parameter_schema(),
      "input_contract": {
        "source_log_type": "rlog",
        "applied_torque_source": APPLIED_TORQUE_SOURCE,
        "telemetry_schema": TELEMETRY_SCHEMA,
        "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
        "alignment": CAUSAL_INPUT_ALIGNMENT,
        "timebase": "nominal_log_mono_time_ns_absolute_100_hz",
        "route_relative_time": (
          "nominal_t_us=floor((nominal_log_mono_time_ns-telemetry_provenance.route_origin_log_mono_time_ns)/1000)"
        ),
        "source_time": "source_t_us_with_nonnegative_asof_service_ages",
        "max_asof_age_ms": MAX_ASOF_AGE_MS,
        "live_parameters_max_age_ms": LIVE_PARAMETERS_MAX_AGE_MS,
        "live_torque_max_age_ms": LIVE_TORQUE_MAX_AGE_MS,
        "live_torque_cadence_policy": LIVE_TORQUE_CADENCE_POLICY,
        "live_torque_cadence_policy_version": LIVE_TORQUE_CADENCE_POLICY_VERSION,
        "sampling": causal_sampling_contract(),
        "controller_i_timing": CONTROLLER_I_TIMING,
      },
      "capabilities": {
        "counterfactual_replay": True,
        "exact_baseline": False,
        "causal_replay_eligible": causal_replay_eligible,
        "apply_to_car": False,
      },
    }

  def handle(self, request: Any) -> dict[str, Any]:
    if not isinstance(request, Mapping):
      raise DynamicsContractError("invalid_request", "Each request must be a JSON object.")
    method = request.get("method")
    params = request.get("params", {})
    if method == "ping":
      return {
        "protocol_version": PROTOCOL_VERSION,
        "status": "ok",
      }
    if method in ("model_info", "parameter_schema"):
      info = self.model_info()
      return (
        info
        if method == "model_info"
        else {
          "protocol_version": PROTOCOL_VERSION,
          "parameter_schema": info["parameter_schema"],
        }
      )
    if method == "replay":
      return {
        "protocol_version": PROTOCOL_VERSION,
        "model": self.plant.provenance(),
        "replay": replay(self.plant, params),
      }
    raise DynamicsContractError(
      "unknown_method",
      "Unknown dynamics adapter method.",
      {"method": method},
    )
