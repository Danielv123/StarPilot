from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from .controller_profile import (
  C6_CONTROLLER_SOURCE_COMMIT,
  HISTORICAL_CONTROLLER_SOURCE_COMMIT,
  validated_controller_profile,
)


MODE = "approximate_closed_loop"
CAR_FINGERPRINT = "HYUNDAI_IONIQ_5"
TELEMETRY_EXTRACTOR_VERSION = "1.1.0"
TELEMETRY_EXTRACTOR_SHA256 = (
  "0a5e00783409697e208df4b325efbb6136049317d7caadf66363b6d0bfa27ca5"
)
ALIGNMENT = "timestamp_causal_recorded_history_asof"
CONTROLLER_I_TIMING = "post_update_asof_source_row"
DYNAMICS_SCHEMA = "comma-companion.dynamics-row"
DYNAMICS_SCHEMA_VERSION = 1
SAMPLE_PERIOD_US = 10_000
HISTORY_ROWS = 300
MIN_HORIZON_US = 100_000
MAX_HORIZON_US = 2_000_000
MAX_ASOF_AGE_MS = 35
MAX_ASOF_AGE_US = MAX_ASOF_AGE_MS * 1000
MAX_RECORD_BYTES = 16 * 1024 * 1024
SOURCE_AGE_PROOF_SCHEMA = "comma-companion.source-age-proof"
CONTROLLER_SELECTION_PROOF_SCHEMA = "comma-companion.controller-selection-proof"
EFFECTIVE_TORQUE_CONTEXT_PROOF_SCHEMA = "comma-companion.effective-torque-context-proof"
EFFECTIVE_TORQUE_CONTEXT_EVALUATOR = (
  "starpilot-torque-context-by-source-commit"
)
EFFECTIVE_TORQUE_CONTEXT_EVALUATOR_SHA256 = (
  "e2fd454ee0180589abfaa4cffd3fb3b7f8ebb018cee60131b2299afdeb934a24"
)
TORQUE_CONTEXT_EVALUATOR_IDS = {
  HISTORICAL_CONTROLLER_SOURCE_COMMIT: (
    "starpilot-torque-context-2747bf-v1"
  ),
  C6_CONTROLLER_SOURCE_COMMIT: (
    "starpilot-torque-context-6dd6c0-v1"
  ),
}
CONTROLLER_SELECTION_EVALUATOR = "starpilot-controlsd-lateral-selection"
CONTROLLER_SELECTION_EVALUATOR_SHA256 = (
  "bdb1b78a4ec79278f0bae3650ef1cd49cc9853e7f22d9d5ee49b94a99ca89bd2"
)
SOURCE_AGE_REQUIRED_SOURCES = {
  "car_state": {
    "field": "car_state_age_us",
    "source": "carState",
  },
  "car_control": {
    "field": "car_control_age_us",
    "source": "carControl",
  },
  "car_output": {
    "field": "car_output_age_us",
    "source": "carOutput.actuatorsOutput.torque",
  },
  "controls_state": {
    "field": "controls_state_age_us",
    "source": "controlsState",
  },
}
CONTROLLER_SELECTION_SOURCES = {
  "starpilotPlan.starpilotToggles",
  "versioned_initData_fallback",
}
EFFECTIVE_TORQUE_PARAMETER_SOURCES = {
  "car_params",
  "live_filtered",
  "resolved_custom",
}
CAUSAL_SAMPLING_CONTRACT = {
  "grid": "absolute_monotonic_time",
  "absolute_grid_field": "nominal_log_mono_time_ns",
  "absolute_grid_phase_ns": 0,
  "sample_period_ns": 10_000_000,
  "sample_rate_hz": 100.0,
  "first_tick_formula": ("ceil(first_valid_carState_logMonoTime_ns/sample_period_ns)" + "*sample_period_ns"),
  "alignment": ("latest_at_or_before_grid_time_zero_order_hold"),
  "source_selection": ("independent_per_source_max_valid_source_with_" + "logMonoTime_at_or_before_tick"),
  "invalid_event_policy": {
    "carState": "drop_without_invalidating_prior_valid_state",
    "carControl": "invalidate_until_next_valid",
    "controlsState": "invalidate_until_next_valid",
    "carOutput": "invalidate_until_next_valid",
  },
  "signed_steering_rate": ("causal_grid_difference_of_zoh_steering_angle"),
  "applied_torque_source": ("carOutput.actuatorsOutput.torque_only_no_fallback"),
  "source_age_equation": ("source_age_ms=(nominal_log_mono_time_ns-" + "source_log_mono_time_ns)/1e6"),
  "source_time_error_equation": ("source_time_error_ms=-source_age_ms"),
  "no_future_source": True,
  "max_asof_age_ms": 35.0,
  "required_asof_sources": [
    "carState",
    "carControl",
    "controlsState",
    "carOutput",
  ],
  "route_relative_time_formula": ("nominal_t_us=(nominal_log_mono_time_ns-" + "route_origin_log_mono_time_ns)//1000"),
  "route_relative_phase_policy": ("constant_nonzero_modulo_allowed_exact_10000us_steps"),
  "event_order": ["logMonoTime", "source_ordinal"],
}
NO_FLM_SOURCE_COMMITS = frozenset({
  HISTORICAL_CONTROLLER_SOURCE_COMMIT,
  C6_CONTROLLER_SOURCE_COMMIT,
})
HISTORICAL_FLM_EVALUATOR = "starpilot-flm-availability-by-source-commit"
HISTORICAL_FLM_EVALUATOR_SHA256 = (
  "db63993f0a9d32b083a9e8a4a17496e366fdd35a800c7fd88c39c2a3f4ad0a0e"
)


def _exact_int(value: Any, expected: int) -> bool:
  return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _strict_json_object(value: Any) -> dict[str, Any] | None:
  if not isinstance(value, str):
    return None
  try:
    parsed = json.loads(
      value,
      parse_constant=lambda constant: (_ for _ in ()).throw(
        ValueError(f"invalid JSON number {constant}"),
      ),
    )
  except (TypeError, ValueError, json.JSONDecodeError):
    return None
  return parsed if isinstance(parsed, dict) else None


def _row(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
  value = cursor.fetchone()
  if value is None:
    return None
  names = [column[0] for column in cursor.description or ()]
  if isinstance(value, sqlite3.Row):
    return {name: value[name] for name in names}
  return dict(zip(names, value, strict=True))


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
  names = [column[0] for column in cursor.description or ()]
  result = []
  for value in cursor.fetchall():
    if isinstance(value, sqlite3.Row):
      result.append({name: value[name] for name in names})
    else:
      result.append(dict(zip(names, value, strict=True)))
  return result


def _sha256(value: Any) -> bool:
  return isinstance(value, str) and len(value) == 64 and value == value.lower() and all(character in "0123456789abcdef" for character in value)


def _archive_relative_telemetry_path(value: Any) -> bool:
  if not isinstance(value, str) or not value or "\\" in value:
    return False
  path = PurePosixPath(value)
  return not path.is_absolute() and len(path.parts) > 1 and path.parts[0] == "telemetry" and all(part not in {"", ".", ".."} for part in path.parts)


def _snapshot_boolean(
  snapshot: Any,
  key: str,
) -> bool | None:
  if not isinstance(snapshot, Mapping):
    return None
  item = snapshot.get(key)
  text = item.get("text") if isinstance(item, Mapping) else None
  if not isinstance(text, str):
    return None
  normalized = text.strip().lower()
  if normalized in {"0", "false", "off", "no", ""}:
    return False
  if normalized in {"1", "true", "on", "yes"}:
    return True
  return None


def _snapshot_text(
  snapshot: Any,
  key: str,
) -> str | None:
  if not isinstance(snapshot, Mapping):
    return None
  item = snapshot.get(key)
  text = item.get("text") if isinstance(item, Mapping) else None
  return text.strip() if isinstance(text, str) else None


def _finite_number(value: Any) -> bool:
  return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _source_age_proof_valid(
  proof: Any,
  row_count: Any,
) -> bool:
  return bool(
    isinstance(proof, Mapping)
    and proof.get("schema") == SOURCE_AGE_PROOF_SCHEMA
    and _exact_int(proof.get("schema_version"), 1)
    and proof.get("state") == "verified"
    and proof.get("alignment") == "latest_at_or_before_grid_time_zero_order_hold"
    and proof.get("maximum_age_us") == MAX_ASOF_AGE_US
    and proof.get("comparison") == "0 <= age_us <= maximum_age_us"
    and proof.get("car_state_time_relation") == "source_time_error_us == -car_state_age_us"
    and isinstance(row_count, int)
    and not isinstance(row_count, bool)
    and row_count > HISTORY_ROWS
    and proof.get("checked_row_count") == row_count
    and proof.get("valid_row_count") == row_count
    and _exact_int(proof.get("missing_required_age_count"), 0)
    and _exact_int(proof.get("negative_required_age_count"), 0)
    and _exact_int(proof.get("over_maximum_age_count"), 0)
    and _exact_int(proof.get("future_car_state_count"), 0)
    and _exact_int(proof.get("source_time_error_mismatch_count"), 0)
    and proof.get("required_sources") == SOURCE_AGE_REQUIRED_SOURCES
  )


def _effective_torque_context_proof_valid(
  proof: Any,
  row_count: Any,
  source_commit: Any,
) -> bool:
  evaluator = proof.get("evaluator") if isinstance(proof, Mapping) else None
  if (
    not isinstance(proof, Mapping)
    or proof.get("schema") != EFFECTIVE_TORQUE_CONTEXT_PROOF_SCHEMA
    or not _exact_int(proof.get("schema_version"), 1)
    or proof.get("state") != "verified"
    or not isinstance(row_count, int)
    or isinstance(row_count, bool)
    or row_count <= HISTORY_ROWS
    or proof.get("checked_row_count") != row_count
    or proof.get("exact_row_count") != row_count
    or proof.get("valid_row_count") != row_count
    or not _exact_int(proof.get("inexact_row_count"), 0)
    or not _exact_int(proof.get("missing_field_row_count"), 0)
    or not _exact_int(proof.get("invalid_row_count"), 0)
    or not _exact_int(proof.get("stateful_invalid_row_count"), 0)
    or not _exact_int(
      proof.get("context_not_bound_to_controls_row_count"),
      0,
    )
    or not _exact_int(proof.get("source_after_controls_row_count"), 0)
    or not _exact_int(proof.get("source_identity_invalid_count"), 0)
    or not isinstance(evaluator, Mapping)
    or evaluator.get("name")
    != EFFECTIVE_TORQUE_CONTEXT_EVALUATOR
    or not _exact_int(evaluator.get("version"), 1)
    or evaluator.get("source_sha256")
    != EFFECTIVE_TORQUE_CONTEXT_EVALUATOR_SHA256
  ):
    return False
  evaluator_source_commit = evaluator.get("source_commit")
  evaluator_id = evaluator.get("evaluator_id")
  if evaluator_source_commit is not None or evaluator_id is not None:
    if (
      evaluator_source_commit != source_commit
      or TORQUE_CONTEXT_EVALUATOR_IDS.get(source_commit)
      != evaluator_id
    ):
      return False
  for field in (
    "factor_source_counts",
    "offset_source_counts",
    "friction_source_counts",
  ):
    counts = proof.get(field)
    if (
      not isinstance(counts, Mapping)
      or not counts
      or not set(counts).issubset(
        EFFECTIVE_TORQUE_PARAMETER_SOURCES,
      )
      or any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in counts.values())
      or sum(counts.values()) != row_count
    ):
      return False
  return True


def _controller_selection_proof_state(
  manifest: Mapping[str, Any],
  dynamics: Mapping[str, Any],
) -> tuple[str, dict[str, Any] | None]:
  proof = dynamics.get("controller_selection_validation")
  recorded = dynamics.get("controller_provenance")
  provenance = manifest.get("provenance")
  row_count = dynamics.get("row_count")
  evaluator = proof.get("evaluator") if isinstance(proof, Mapping) else None
  source_types = proof.get("source_types") if isinstance(proof, Mapping) else None
  snapshot_hashes = proof.get("snapshot_hashes") if isinstance(proof, Mapping) else None
  controller_types = proof.get("controller_types") if isinstance(proof, Mapping) else None
  controller_counts = (
    (
      proof.get("conventional_torque_row_count"),
      proof.get("nnff_row_count"),
      proof.get("nnff_lite_row_count"),
      proof.get("unsupported_row_count"),
    )
    if isinstance(proof, Mapping)
    else ()
  )
  valid = (
    isinstance(proof, Mapping)
    and proof.get("schema") == CONTROLLER_SELECTION_PROOF_SCHEMA
    and _exact_int(proof.get("schema_version"), 1)
    and proof.get("state") == "verified"
    and isinstance(evaluator, Mapping)
    and evaluator.get("name") == CONTROLLER_SELECTION_EVALUATOR
    and _exact_int(evaluator.get("version"), 1)
    and evaluator.get("source_sha256")
    == CONTROLLER_SELECTION_EVALUATOR_SHA256
    and isinstance(row_count, int)
    and not isinstance(row_count, bool)
    and row_count > HISTORY_ROWS
    and proof.get("checked_row_count") == row_count
    and proof.get("resolved_row_count") == row_count
    and _exact_int(proof.get("missing_row_count"), 0)
    and _exact_int(proof.get("invalid_row_count"), 0)
    and isinstance(controller_types, list)
    and bool(controller_types)
    and all(isinstance(value, str) for value in controller_types)
    and controller_types == sorted(set(controller_types))
    and set(controller_types).issubset(
      {
        "conventional_torque",
        "nnff",
        "nnff_lite",
        "unsupported",
      }
    )
    and len(controller_counts) == 4
    and all(isinstance(count, int) and not isinstance(count, bool) and count >= 0 for count in controller_counts)
    and sum(controller_counts) == row_count
    and isinstance(source_types, list)
    and bool(source_types)
    and all(isinstance(value, str) for value in source_types)
    and source_types == sorted(set(source_types))
    and set(source_types).issubset(CONTROLLER_SELECTION_SOURCES)
    and isinstance(snapshot_hashes, list)
    and bool(snapshot_hashes)
    and all(isinstance(value, str) for value in snapshot_hashes)
    and snapshot_hashes == sorted(set(snapshot_hashes))
    and all(_sha256(value) for value in snapshot_hashes)
    and isinstance(recorded, Mapping)
  )
  if not valid:
    return "unverified", None
  normalized = {
    "proof": dict(proof),
    "recorded": dict(recorded),
  }
  if (
    controller_types != ["conventional_torque"]
    or proof.get("conventional_torque_row_count") != row_count
    or proof.get("nnff_row_count") != 0
    or proof.get("nnff_lite_row_count") != 0
    or proof.get("unsupported_row_count") != 0
  ):
    return "unsupported", normalized

  resolved_snapshots = recorded.get("resolved_toggle_snapshots")
  if "starpilotPlan.starpilotToggles" in source_types:
    if not (
      isinstance(resolved_snapshots, list)
      and bool(resolved_snapshots)
      and all(
        isinstance(snapshot, Mapping)
        and snapshot.get("source") == "starpilotPlan.starpilotToggles"
        and snapshot.get("valid") is True
        and _sha256(snapshot.get("sha256"))
        and snapshot.get("sha256") in snapshot_hashes
        and isinstance(snapshot.get("values"), Mapping)
        and snapshot["values"].get("nnff") is False
        and snapshot["values"].get("nnff_lite") is False
        and (
          snapshot["values"].get("nnff_model_name") is None
          or isinstance(
            snapshot["values"].get("nnff_model_name"),
            str,
          )
        )
        and isinstance(snapshot.get("segment_num"), int)
        and not isinstance(snapshot.get("segment_num"), bool)
        and snapshot["segment_num"] >= 0
        and isinstance(snapshot.get("source_ordinal"), int)
        and not isinstance(snapshot.get("source_ordinal"), bool)
        and snapshot["source_ordinal"] >= 0
        and isinstance(snapshot.get("log_mono_time_ns"), str)
        and snapshot["log_mono_time_ns"].isdigit()
        for snapshot in resolved_snapshots
      )
    ):
      return "unverified", None

  if "versioned_initData_fallback" in source_types:
    fallback = recorded.get("init_data_fallback_evaluator")
    source_commit = provenance.get("source_starpilot_commit") if isinstance(provenance, Mapping) else None
    if not (
      isinstance(fallback, Mapping)
      and fallback.get("state") == "available"
      and fallback.get("name")
      == EFFECTIVE_TORQUE_CONTEXT_EVALUATOR
      and _exact_int(fallback.get("version"), 1)
      and fallback.get("source_commit") == source_commit
      and fallback.get("evaluator_id")
      == TORQUE_CONTEXT_EVALUATOR_IDS.get(source_commit)
      and fallback.get("source_sha256")
      == EFFECTIVE_TORQUE_CONTEXT_EVALUATOR_SHA256
    ):
      return "unverified", None
  return "valid", normalized


def _resolved_flm_state(
  controller_provenance: Mapping[str, Any],
  provenance: Any,
) -> bool | None:
  if controller_provenance.get("flm_active_available") is True and isinstance(controller_provenance.get("flm_active"), bool):
    return bool(controller_provenance["flm_active"])
  resolution = controller_provenance.get("flm_resolution")
  evaluator = resolution.get("evaluator") if isinstance(resolution, Mapping) else None
  source_commit = (
    provenance.get("source_starpilot_commit")
    if isinstance(provenance, Mapping)
    else None
  )
  if (
    isinstance(provenance, Mapping)
    and isinstance(resolution, Mapping)
    and resolution.get("state") == "verified"
    and resolution.get("flm_active") is False
    and resolution.get("source") == "versioned_source_commit_evaluator"
    and isinstance(evaluator, Mapping)
    and evaluator.get("name") == HISTORICAL_FLM_EVALUATOR
    and _exact_int(evaluator.get("version"), 1)
    and source_commit in NO_FLM_SOURCE_COMMITS
    and evaluator.get("source_commit") == source_commit
    and evaluator.get("source_sha256")
    == HISTORICAL_FLM_EVALUATOR_SHA256
  ):
    return False
  return None


def evaluate_simulation_eligibility(
  connection: sqlite3.Connection,
  drive_id: str,
  *,
  model_hash: str | None = None,
  mode: str | None = None,
  t_us: int | None = None,
  horizon_us: int | None = None,
) -> dict[str, Any]:
  reasons: list[dict[str, Any]] = []
  seen_codes: set[str] = set()

  def reject(
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
  ) -> None:
    if code in seen_codes:
      return
    seen_codes.add(code)
    reason: dict[str, Any] = {
      "code": code,
      "message": message,
    }
    if details:
      reason["details"] = dict(details)
    reasons.append(reason)

  result: dict[str, Any] = {
    "eligible": False,
    "reasons": reasons,
  }
  if not isinstance(drive_id, str) or not drive_id:
    reject("invalid_drive_id", "drive_id must be a non-empty string.")
    return result

  try:
    drive = _row(
      connection.execute(
        """
      SELECT id, route_name, telemetry_ready
      FROM drives
      WHERE id = ?
      """,
        (drive_id,),
      )
    )
  except sqlite3.Error as error:
    reject(
      "eligibility_schema_unavailable",
      "The simulation eligibility schema is unavailable.",
      {"error": type(error).__name__},
    )
    return result
  if drive is None:
    reject("drive_not_found", "The selected drive does not exist.")
    return result

  if drive["telemetry_ready"] != 1:
    reject(
      "telemetry_not_ready",
      "The drive telemetry readiness marker is not set.",
    )

  try:
    telemetry = _row(
      connection.execute(
        """
      SELECT
        state, ndjson_path, ndjson_sha256, manifest_json,
        source_fingerprint
      FROM telemetry_indexes
      WHERE drive_id = ?
      """,
        (drive_id,),
      )
    )
    latest_inventory = _row(
      connection.execute(
        """
      SELECT
        generation, state, route_closed,
        rlog_source_fingerprint
      FROM route_inventories
      WHERE drive_id = ?
      ORDER BY generation DESC
      LIMIT 1
      """,
        (drive_id,),
      )
    )
  except sqlite3.Error as error:
    reject(
      "eligibility_schema_unavailable",
      "The simulation eligibility schema is unavailable.",
      {"error": type(error).__name__},
    )
    return result
  manifest = _strict_json_object(telemetry["manifest_json"]) if telemetry is not None else None
  if telemetry is None:
    reject(
      "telemetry_not_ready",
      "The drive has no indexed telemetry generation.",
    )
  elif manifest is None:
    reject(
      "telemetry_index_corrupt",
      "The indexed telemetry manifest is invalid.",
    )

  dynamics: Mapping[str, Any] = {}
  telemetry_provenance: Mapping[str, Any] = {}
  telemetry_extractor_sha256: str | None = None
  timeline_version: str | None = None
  if telemetry is not None and manifest is not None:
    timeline_version = manifest.get("timeline_version")
    inventory_binding_valid = (
      latest_inventory is not None
      and latest_inventory["state"] == "complete"
      and latest_inventory["route_closed"] == 1
      and _sha256(
        latest_inventory["rlog_source_fingerprint"],
      )
      and latest_inventory["rlog_source_fingerprint"] == telemetry["source_fingerprint"]
    )
    immutable_catalog = (
      telemetry["state"] == "complete"
      and manifest.get("state") == "complete"
      and manifest.get("publication_ready") is True
      and _archive_relative_telemetry_path(telemetry["ndjson_path"])
      and _sha256(telemetry["ndjson_sha256"])
      and _sha256(telemetry["source_fingerprint"])
      and _sha256(timeline_version)
      and manifest.get("route_id", drive["route_name"]) == drive["route_name"]
      and inventory_binding_valid
    )
    if telemetry["state"] != "complete":
      reject(
        "telemetry_not_ready",
        "The indexed telemetry generation is not complete.",
        {"state": telemetry["state"]},
      )
    if manifest.get("state") != "complete" or manifest.get("publication_ready") is not True:
      reject(
        "telemetry_not_publication_ready",
        "The telemetry manifest is not complete and publication-ready.",
      )
    if not immutable_catalog:
      reject(
        "immutable_generation_unverified",
        "The telemetry generation lacks valid immutable catalog provenance.",
      )
    if _sha256(telemetry["ndjson_sha256"]) and _sha256(timeline_version):
      result["telemetry_generation"] = {
        "ndjson_sha256": telemetry["ndjson_sha256"].lower(),
        "timeline_version": str(timeline_version).lower(),
      }

    completeness = manifest.get("completeness")
    segments = completeness.get("segments") if isinstance(completeness, Mapping) else None
    provenance = manifest.get("provenance")
    source_objects = provenance.get("source_objects") if isinstance(provenance, Mapping) else None
    full_rlog = (
      isinstance(completeness, Mapping)
      and completeness.get("contiguous_from_segment_zero") is True
      and completeness.get("route_start_observed") is True
      and completeness.get("route_end_observed") is True
      and completeness.get("boundary_chain_valid") is True
      and isinstance(segments, list)
      and bool(segments)
      and all(isinstance(segment, Mapping) and segment.get("state") == "complete" and segment.get("log_type") == "rlog" for segment in segments)
      and isinstance(provenance, Mapping)
      and provenance.get("extractor") == "comma-companion-rlog"
      and isinstance(source_objects, list)
      and bool(source_objects)
      and all(isinstance(source, Mapping) and source.get("log_type") == "rlog" and _sha256(source.get("sha256")) for source in source_objects)
    )
    if not full_rlog:
      reject(
        "full_rlog_required",
        "Simulation requires a complete contiguous full-rlog route.",
      )

    raw_dynamics = manifest.get("dynamics")
    if isinstance(raw_dynamics, Mapping):
      dynamics = raw_dynamics
    raw_telemetry_provenance = dynamics.get("telemetry_provenance")
    if isinstance(raw_telemetry_provenance, Mapping):
      telemetry_provenance = raw_telemetry_provenance
    provenance = manifest.get("provenance")
    timebase = manifest.get("timebase")
    telemetry_extractor_sha256 = telemetry_provenance.get("extractor_source_sha256") if isinstance(telemetry_provenance, Mapping) else None
    causal_contract = (
      dynamics.get("state") == "available"
      and dynamics.get("alignment") == ALIGNMENT
      and dynamics.get("causal_input_eligible") is True
      and dynamics.get("controller_i_timing") == CONTROLLER_I_TIMING
      and dynamics.get("sample_period_us") == SAMPLE_PERIOD_US
      and telemetry_provenance.get("schema") == DYNAMICS_SCHEMA
      and _exact_int(
        telemetry_provenance.get("schema_version"),
        DYNAMICS_SCHEMA_VERSION,
      )
      and telemetry_provenance.get("alignment") == ALIGNMENT
      and telemetry_provenance.get("causal_input_eligible") is True
      and telemetry_provenance.get("extractor_version") == TELEMETRY_EXTRACTOR_VERSION
      and telemetry_provenance.get("max_asof_age_ms") == MAX_ASOF_AGE_MS
      and isinstance(
        telemetry_provenance.get(
          "route_origin_log_mono_time_ns",
        ),
        str,
      )
      and telemetry_provenance["route_origin_log_mono_time_ns"].isdigit()
      and isinstance(timebase, Mapping)
      and timebase.get("origin_log_mono_time_ns") == telemetry_provenance["route_origin_log_mono_time_ns"]
      and telemetry_extractor_sha256 == TELEMETRY_EXTRACTOR_SHA256
      and isinstance(provenance, Mapping)
      and provenance.get("extractor_version") == telemetry_provenance["extractor_version"]
      and provenance.get("extractor_source_sha256") == telemetry_extractor_sha256
    )
    if not causal_contract:
      reject(
        "causal_telemetry_required",
        "Telemetry lacks the exact timestamp-causal 100 Hz provenance contract.",
      )

    source_age_validation = dynamics.get("source_age_validation")
    row_count = dynamics.get("row_count")
    source_ages_verified = _source_age_proof_valid(
      source_age_validation,
      row_count,
    )
    if not source_ages_verified:
      reject(
        "causal_source_ages_unverified",
        "Telemetry has no complete proof of nonnegative, bounded as-of source ages.",
      )
    if not _effective_torque_context_proof_valid(
      dynamics.get("effective_torque_context_validation"),
      row_count,
      (
        provenance.get("source_starpilot_commit")
        if isinstance(provenance, Mapping)
        else None
      ),
    ):
      reject(
        "controller_provenance_incomplete",
        "Telemetry does not prove exact factor, offset, and friction ownership for every dynamics row.",
      )

    vehicle = manifest.get("vehicle")
    if not isinstance(vehicle, Mapping) or vehicle.get("car_fingerprint") != CAR_FINGERPRINT:
      reject(
        "wrong_car",
        "The dynamics artifact supports only the trained Ioniq 5 fingerprint.",
        {
          "expected": CAR_FINGERPRINT,
          "actual": (vehicle.get("car_fingerprint") if isinstance(vehicle, Mapping) else None),
        },
      )
    selection_state, selection = _controller_selection_proof_state(
      manifest,
      dynamics,
    )
    if selection_state == "unverified":
      reject(
        "controller_identity_unverified",
        "The route has no complete, versioned proof of the resolved runtime controller.",
      )
    elif selection_state == "unsupported":
      reject(
        "unsupported_controller_type",
        "Only route-wide conventional torque control is supported.",
        {
          "controller_selection_validation": (selection["proof"] if isinstance(selection, Mapping) else None),
        },
      )
    if not isinstance(vehicle, Mapping) or vehicle.get("lateral_tuning_type") != "torque":
      reject(
        "unsupported_controller_type",
        "Only conventional torque-controller routes are supported.",
      )
    controller_provenance = dynamics.get("controller_provenance")
    flm_active = _resolved_flm_state(controller_provenance, provenance) if isinstance(controller_provenance, Mapping) else None
    controller_complete = (
      isinstance(controller_provenance, Mapping)
      and _sha256(controller_provenance.get("car_params_wire_sha256"))
      and _sha256(controller_provenance.get("controller_params_sha256"))
      and isinstance(flm_active, bool)
      and controller_provenance.get("trailer_load_available") is True
      and _finite_number(controller_provenance.get("trailer_load_kg"))
    )
    if not controller_complete:
      reject(
        "controller_provenance_incomplete",
        "The recorded controller and runtime tuning snapshot are incomplete.",
      )
    elif flm_active is True or float(controller_provenance["trailer_load_kg"]) > 0.0:
      reject(
        "unsupported_runtime_overrides",
        "Active FLM or trailer-load shaping is not modeled by replay.",
        {
          "flm_active": flm_active,
          "trailer_load_kg": controller_provenance.get(
            "trailer_load_kg",
          ),
        },
      )
    baseline_profile = validated_controller_profile(
      manifest,
    )
    if baseline_profile is None:
      reject(
        "controller_provenance_incomplete",
        "Telemetry has no reviewed controller baseline for this route generation.",
      )
    else:
      result["baseline_controller_profile"] = baseline_profile
      result["baseline_params"] = dict(
        baseline_profile["baseline_controller_params"],
      )

  requested_mode = mode or MODE
  if requested_mode != MODE:
    reject(
      "unsupported_simulation_mode",
      f"Only {MODE} is supported.",
      {"mode": requested_mode},
    )
  if model_hash is not None and not _sha256(model_hash):
    reject(
      "invalid_model_hash",
      "model_hash must be a SHA-256 digest.",
    )
    model_row = None
  elif model_hash is not None:
    model_row = _row(
      connection.execute(
        """
      SELECT sha256, name, enabled, mode, metadata_json
      FROM model_registry
      WHERE sha256 = ?
      """,
        (model_hash.lower(),),
      )
    )
  else:
    model_row = _row(
      connection.execute(
        """
      SELECT sha256, name, enabled, mode, metadata_json
      FROM model_registry
      WHERE mode = ?
      ORDER BY enabled DESC, created_at DESC, sha256
      LIMIT 1
      """,
        (requested_mode,),
      )
    )
  model_metadata = _strict_json_object(model_row["metadata_json"]) if model_row is not None else None
  registered_model = model_metadata.get("model") if isinstance(model_metadata, Mapping) and isinstance(model_metadata.get("model"), Mapping) else None
  if model_row is None:
    reject(
      "causal_model_required",
      "No reviewed timestamp-causal dynamics model is registered.",
    )
  elif model_metadata is None or registered_model is None:
    reject(
      "model_registry_invalid",
      "The selected model registry entry has invalid provenance.",
    )
  else:
    result["model"] = {
      "sha256": model_row["sha256"],
      "name": model_row["name"],
      "mode": model_row["mode"],
      "enabled": model_row["enabled"] == 1,
      "max_asof_age_ms": registered_model.get("max_asof_age_ms"),
    }
    adapter = model_metadata.get("adapter")
    capabilities = adapter.get("capabilities") if isinstance(adapter, Mapping) and isinstance(adapter.get("capabilities"), Mapping) else None
    causal_model = (
      model_row["enabled"] == 1
      and model_row["mode"] == MODE
      and registered_model.get("sha256") == model_row["sha256"]
      and registered_model.get("promoted_artifact_verified") is True
      and registered_model.get("review_registry_match") is True
      and registered_model.get("causal_training_eligible") is True
      and registered_model.get("training_alignment") == ALIGNMENT
      and registered_model.get("training_schema") == DYNAMICS_SCHEMA
      and _exact_int(
        registered_model.get("training_schema_version"),
        DYNAMICS_SCHEMA_VERSION,
      )
      and _exact_int(
        registered_model.get("training_extraction_version"),
        9,
      )
      and registered_model.get("trainer_schema") == "starpilot.neural-lateral-plant"
      and _exact_int(
        registered_model.get("trainer_schema_version"),
        7,
      )
      and _sha256(registered_model.get("training_extractor_sha256"))
      and _sha256(registered_model.get("trainer_sha256"))
      and _sha256(
        registered_model.get(
          "compatible_telemetry_extractor_sha256",
        ),
      )
      and isinstance(
        registered_model.get(
          "compatible_telemetry_extractor_version",
        ),
        str,
      )
      and bool(
        registered_model.get(
          "compatible_telemetry_extractor_version",
        ),
      )
      and registered_model.get("max_asof_age_ms") == MAX_ASOF_AGE_MS
      and registered_model.get("sampling") == CAUSAL_SAMPLING_CONTRACT
      and model_metadata.get("causal_training_eligible") is True
      and isinstance(capabilities, Mapping)
      and capabilities.get("causal_replay_eligible") is True
    )
    if not causal_model:
      reject(
        "causal_model_required",
        "The selected model is not a reviewed, enabled timestamp-causal artifact.",
        {"model_hash": model_row["sha256"]},
      )
    elif telemetry_extractor_sha256 == TELEMETRY_EXTRACTOR_SHA256 and (
      registered_model.get(
        "compatible_telemetry_extractor_sha256",
      )
      != telemetry_extractor_sha256
      or registered_model.get(
        "compatible_telemetry_extractor_version",
      )
      != telemetry_provenance.get("extractor_version")
    ):
      reject(
        "model_telemetry_provenance_mismatch",
        "The model is not reviewed for this telemetry extractor generation.",
        {
          "model_compatible_extractor_sha256": registered_model.get(
            "compatible_telemetry_extractor_sha256",
          ),
          "telemetry_extractor_sha256": telemetry_extractor_sha256,
          "model_compatible_extractor_version": (
            registered_model.get(
              "compatible_telemetry_extractor_version",
            )
          ),
          "telemetry_extractor_version": (telemetry_provenance.get("extractor_version")),
        },
      )

  try:
    chunks = _rows(
      connection.execute(
        """
      SELECT
        chunk_index, start_t_us, end_t_us, ndjson_path,
        byte_offset, byte_length, record_sha256
      FROM telemetry_dynamics_chunks
      WHERE drive_id = ?
      ORDER BY chunk_index
      """,
        (drive_id,),
      )
    )
  except sqlite3.Error:
    chunks = []
  range_start: int | None = None
  range_end: int | None = None
  indexed_row_capacity = 0
  dynamics_index_valid = bool(chunks)
  prior_end: int | None = None
  grid_phase_us: int | None = None
  continuity_groups: list[list[int]] = []
  expected_path = telemetry["ndjson_path"] if telemetry is not None else None
  for expected_chunk_index, chunk in enumerate(chunks):
    start = chunk["start_t_us"]
    end = chunk["end_t_us"]
    length = chunk["byte_length"]
    valid_chunk = (
      chunk["chunk_index"] == expected_chunk_index
      and isinstance(start, int)
      and not isinstance(start, bool)
      and isinstance(end, int)
      and not isinstance(end, bool)
      and end >= start
      and (end - start) % SAMPLE_PERIOD_US == 0
      and (prior_end is None or start > prior_end)
      and chunk["ndjson_path"] == expected_path
      and isinstance(chunk["byte_offset"], int)
      and not isinstance(chunk["byte_offset"], bool)
      and chunk["byte_offset"] >= 0
      and isinstance(length, int)
      and not isinstance(length, bool)
      and 1 <= length <= MAX_RECORD_BYTES
      and _sha256(chunk["record_sha256"])
    )
    if not valid_chunk:
      dynamics_index_valid = False
      continue
    chunk_phase_us = start % SAMPLE_PERIOD_US
    if grid_phase_us is None:
      grid_phase_us = chunk_phase_us
    elif chunk_phase_us != grid_phase_us:
      dynamics_index_valid = False
    if range_start is None:
      range_start = start
    range_end = end
    indexed_row_capacity += (end - start) // SAMPLE_PERIOD_US + 1
    if continuity_groups and start == continuity_groups[-1][1] + SAMPLE_PERIOD_US:
      continuity_groups[-1][1] = end
    else:
      continuity_groups.append([start, end])
    prior_end = end
  manifest_row_count = dynamics.get("row_count") if isinstance(dynamics, Mapping) else None
  if (
    not dynamics_index_valid
    or not isinstance(manifest_row_count, int)
    or isinstance(manifest_row_count, bool)
    or manifest_row_count <= 0
    or manifest_row_count != indexed_row_capacity
    or range_start is None
    or range_end is None
  ):
    reject(
      "dynamics_index_unavailable",
      "The compact native dynamics index is missing or inconsistent.",
    )

  point_requested = t_us is not None or horizon_us is not None
  if point_requested:
    point_valid = (
      isinstance(t_us, int)
      and not isinstance(t_us, bool)
      and t_us >= 0
      and isinstance(horizon_us, int)
      and not isinstance(horizon_us, bool)
      and MIN_HORIZON_US <= horizon_us <= MAX_HORIZON_US
    )
    if not point_valid:
      reject(
        "invalid_point_request",
        "Point eligibility requires a nonnegative t_us and supported horizon_us.",
        {
          "minimum_horizon_us": MIN_HORIZON_US,
          "maximum_horizon_us": MAX_HORIZON_US,
        },
      )
    elif dynamics_index_valid and continuity_groups:
      selected_group = next(
        (group for group in continuity_groups if group[0] <= t_us < group[1] + SAMPLE_PERIOD_US),
        None,
      )
      if selected_group is None:
        reject(
          "dynamics_gap_at_point",
          "The selected point is outside a continuous native dynamics group.",
          {"t_us": t_us},
        )
      else:
        group_start, group_end = selected_group
        anchor_t_us = group_start + ((t_us - group_start) // SAMPLE_PERIOD_US) * SAMPLE_PERIOD_US
        available_history_rows = max(
          0,
          (anchor_t_us - group_start) // SAMPLE_PERIOD_US,
        )
        if available_history_rows < HISTORY_ROWS:
          reject(
            "insufficient_history",
            "The selected point has fewer than 3 seconds of native history.",
            {
              "required_rows": HISTORY_ROWS,
              "available_rows": available_history_rows,
            },
          )
        horizon_steps = max(
          1,
          round(horizon_us / SAMPLE_PERIOD_US),
        )
        available_future_rows = max(
          0,
          (group_end - anchor_t_us) // SAMPLE_PERIOD_US,
        )
        if available_future_rows < horizon_steps:
          reject(
            "insufficient_future",
            "The selected point lacks the requested native future horizon.",
            {
              "required_rows": horizon_steps,
              "available_rows": available_future_rows,
            },
          )
  elif dynamics_index_valid and continuity_groups:
    minimum_future_rows = max(
      1,
      round(MIN_HORIZON_US / SAMPLE_PERIOD_US),
    )
    if not any((end - start) // SAMPLE_PERIOD_US >= HISTORY_ROWS + minimum_future_rows for start, end in continuity_groups):
      reject(
        "no_eligible_continuity_group",
        "No native dynamics group contains both warmup history and a supported future horizon.",
      )

  result["eligible"] = not reasons
  return result


simulation_eligibility = evaluate_simulation_eligibility
