from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from comma_companion.controller_profile import (
  C6_CONTROLLER_PARAMS,
  C6_CONTROLLER_PROFILE_ID,
  C6_CONTROLLER_PROFILE_SHA256,
  C6_CONTROLLER_SOURCE_COMMIT,
  CONTROLLER_KERNEL_SCHEMA,
  CONTROLLER_PARAMS_VALUE_SPACE,
  CONTROLLER_PROFILE_EVALUATOR,
  CONTROLLER_PROFILE_EVALUATOR_SHA256,
  CURRENT_CONTROLLER_PARAMS,
  CURRENT_CONTROLLER_PROFILE_ID,
  CURRENT_CONTROLLER_PROFILE_SHA256,
  CURRENT_CONTROLLER_SOURCE_COMMIT,
  HISTORICAL_CONTROLLER_PARAMS,
  HISTORICAL_CONTROLLER_PROFILE_ID,
  HISTORICAL_CONTROLLER_PROFILE_SHA256,
  HISTORICAL_CONTROLLER_SOURCE_COMMIT,
)
from comma_companion.simulation_eligibility import (
  ALIGNMENT,
  CAR_FINGERPRINT,
  CONTROLLER_SELECTION_EVALUATOR_SHA256,
  DYNAMICS_SCHEMA,
  EFFECTIVE_TORQUE_CONTEXT_EVALUATOR,
  EFFECTIVE_TORQUE_CONTEXT_EVALUATOR_SHA256,
  HISTORICAL_FLM_EVALUATOR_SHA256,
  MODE,
  TELEMETRY_EXTRACTOR_SHA256,
  TORQUE_CONTEXT_EVALUATOR_IDS,
  evaluate_simulation_eligibility,
)


DRIVE_ID = "drive-one"
ROUTE_NAME = "00000001--route"
MODEL_HASH = "a" * 64
TELEMETRY_HASH = "b" * 64
EXTRACTOR_HASH = TELEMETRY_EXTRACTOR_SHA256
TRAINING_EXTRACTOR_HASH = "d" * 64
TIMELINE_VERSION = "e" * 64
RLOG_SOURCE_FINGERPRINT = "f" * 64


def _historical_controller_profile() -> dict[str, Any]:
  return {
    "profile_id": HISTORICAL_CONTROLLER_PROFILE_ID,
    "kernel_schema": CONTROLLER_KERNEL_SCHEMA,
    "kernel_schema_version": 1,
    "source_starpilot_commit": HISTORICAL_CONTROLLER_SOURCE_COMMIT,
    "baseline_controller_params": copy.deepcopy(
      HISTORICAL_CONTROLLER_PARAMS,
    ),
    "baseline_controller_params_sha256": (HISTORICAL_CONTROLLER_PROFILE_SHA256),
    "effective_torque_params_value_space": (CONTROLLER_PARAMS_VALUE_SPACE),
    "vehicle_lat_accel_factor_multiplier": 1.2101,
    "evaluator": {
      "name": CONTROLLER_PROFILE_EVALUATOR,
      "version": 1,
      "source_commit": HISTORICAL_CONTROLLER_SOURCE_COMMIT,
      "source_sha256": CONTROLLER_PROFILE_EVALUATOR_SHA256,
    },
  }


def _current_controller_profile() -> dict[str, Any]:
  return {
    "profile_id": CURRENT_CONTROLLER_PROFILE_ID,
    "kernel_schema": CONTROLLER_KERNEL_SCHEMA,
    "kernel_schema_version": 1,
    "source_starpilot_commit": CURRENT_CONTROLLER_SOURCE_COMMIT,
    "baseline_controller_params": copy.deepcopy(
      CURRENT_CONTROLLER_PARAMS,
    ),
    "baseline_controller_params_sha256": (CURRENT_CONTROLLER_PROFILE_SHA256),
    "effective_torque_params_value_space": (CONTROLLER_PARAMS_VALUE_SPACE),
    "vehicle_lat_accel_factor_multiplier": 1.36,
    "evaluator": {
      "name": CONTROLLER_PROFILE_EVALUATOR,
      "version": 1,
      "source_commit": CURRENT_CONTROLLER_SOURCE_COMMIT,
      "source_sha256": CONTROLLER_PROFILE_EVALUATOR_SHA256,
    },
  }


def _c6_controller_profile() -> dict[str, Any]:
  return {
    "profile_id": C6_CONTROLLER_PROFILE_ID,
    "kernel_schema": CONTROLLER_KERNEL_SCHEMA,
    "kernel_schema_version": 1,
    "source_starpilot_commit": C6_CONTROLLER_SOURCE_COMMIT,
    "baseline_controller_params": copy.deepcopy(
      C6_CONTROLLER_PARAMS,
    ),
    "baseline_controller_params_sha256": C6_CONTROLLER_PROFILE_SHA256,
    "effective_torque_params_value_space": CONTROLLER_PARAMS_VALUE_SPACE,
    "vehicle_lat_accel_factor_multiplier": 1.2507,
    "evaluator": {
      "name": CONTROLLER_PROFILE_EVALUATOR,
      "version": 1,
      "source_commit": C6_CONTROLLER_SOURCE_COMMIT,
      "source_sha256": CONTROLLER_PROFILE_EVALUATOR_SHA256,
    },
  }


@pytest.fixture
def connection() -> sqlite3.Connection:
  result = sqlite3.connect(":memory:")
  result.executescript(
    """
    CREATE TABLE drives (
      id TEXT PRIMARY KEY,
      route_name TEXT NOT NULL,
      telemetry_ready INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE telemetry_indexes (
      drive_id TEXT PRIMARY KEY,
      state TEXT NOT NULL,
      ndjson_path TEXT NOT NULL,
      ndjson_sha256 TEXT NOT NULL,
      manifest_json TEXT NOT NULL,
      source_fingerprint TEXT NOT NULL
    );
    CREATE TABLE route_inventories (
      id TEXT PRIMARY KEY,
      drive_id TEXT NOT NULL,
      generation INTEGER NOT NULL,
      state TEXT NOT NULL,
      route_closed INTEGER NOT NULL,
      rlog_source_fingerprint TEXT
    );
    CREATE TABLE telemetry_dynamics_chunks (
      drive_id TEXT NOT NULL,
      chunk_index INTEGER NOT NULL,
      start_t_us INTEGER NOT NULL,
      end_t_us INTEGER NOT NULL,
      ndjson_path TEXT NOT NULL,
      byte_offset INTEGER NOT NULL,
      byte_length INTEGER NOT NULL,
      record_sha256 TEXT NOT NULL
    );
    CREATE TABLE model_registry (
      sha256 TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      enabled INTEGER NOT NULL,
      mode TEXT NOT NULL,
      metadata_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    """,
  )
  _seed_eligible(result)
  try:
    yield result
  finally:
    result.close()


def _manifest() -> dict[str, Any]:
  return {
    "record": "manifest",
    "schema": "comma-companion.telemetry-manifest",
    "schema_version": 1,
    "route_id": ROUTE_NAME,
    "state": "complete",
    "publication_ready": True,
    "timeline_version": TIMELINE_VERSION,
    "timebase": {
      "origin_log_mono_time_ns": "1000000000",
    },
    "vehicle": {
      "car_fingerprint": CAR_FINGERPRINT,
      "lateral_tuning_type": "torque",
      "steer_control_type": "torque",
    },
    "route_software": {
      "controller_params": {
        "LateralTune": {
          "text": "1",
          "sha256": "2" * 64,
          "size_bytes": 1,
        },
        "NNFF": {
          "text": "0",
          "sha256": "3" * 64,
          "size_bytes": 1,
        },
        "NNFFLite": {
          "text": "0",
          "sha256": "4" * 64,
          "size_bytes": 1,
        },
        "NNFFModelName": {
          "text": "HYUNDAI IONIQ 5",
          "sha256": "9" * 64,
          "size_bytes": 16,
        },
      },
    },
    "completeness": {
      "contiguous_from_segment_zero": True,
      "route_start_observed": True,
      "route_end_observed": True,
      "boundary_chain_valid": True,
      "segments": [
        {
          "segment_num": 0,
          "state": "complete",
          "log_type": "rlog",
        }
      ],
    },
    "dynamics": {
      "schema": DYNAMICS_SCHEMA,
      "schema_version": 1,
      "state": "available",
      "alignment": ALIGNMENT,
      "sample_period_us": 10_000,
      "controller_i_timing": "post_update_asof_source_row",
      "row_count": 501,
      "causal_input_eligible": True,
      "telemetry_provenance": {
        "schema": DYNAMICS_SCHEMA,
        "schema_version": 1,
        "alignment": ALIGNMENT,
        "causal_input_eligible": True,
        "extractor_version": "1.1.0",
        "extractor_source_sha256": EXTRACTOR_HASH,
        "max_asof_age_ms": 35,
        "route_origin_log_mono_time_ns": "1000000000",
      },
      "source_age_validation": {
        "schema": "comma-companion.source-age-proof",
        "schema_version": 1,
        "state": "verified",
        "alignment": ("latest_at_or_before_grid_time_zero_order_hold"),
        "maximum_age_us": 35_000,
        "comparison": "0 <= age_us <= maximum_age_us",
        "car_state_time_relation": ("source_time_error_us == -car_state_age_us"),
        "checked_row_count": 501,
        "valid_row_count": 501,
        "missing_required_age_count": 0,
        "negative_required_age_count": 0,
        "over_maximum_age_count": 0,
        "future_car_state_count": 0,
        "source_time_error_mismatch_count": 0,
        "required_sources": {
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
        },
      },
      "controller_selection_validation": {
        "schema": "comma-companion.controller-selection-proof",
        "schema_version": 1,
        "state": "verified",
        "evaluator": {
          "name": "starpilot-controlsd-lateral-selection",
          "version": 1,
          "source_sha256": CONTROLLER_SELECTION_EVALUATOR_SHA256,
        },
        "checked_row_count": 501,
        "resolved_row_count": 501,
        "missing_row_count": 0,
        "invalid_row_count": 0,
        "controller_types": ["conventional_torque"],
        "conventional_torque_row_count": 501,
        "nnff_row_count": 0,
        "nnff_lite_row_count": 0,
        "unsupported_row_count": 0,
        "source_types": [
          "starpilotPlan.starpilotToggles",
        ],
        "snapshot_hashes": ["8" * 64],
      },
      "effective_torque_context_validation": {
        "schema": ("comma-companion.effective-torque-context-proof"),
        "schema_version": 1,
        "state": "verified",
        "evaluator": {
          "name": EFFECTIVE_TORQUE_CONTEXT_EVALUATOR,
          "version": 1,
          "source_sha256": (
            EFFECTIVE_TORQUE_CONTEXT_EVALUATOR_SHA256
          ),
        },
        "checked_row_count": 501,
        "exact_row_count": 501,
        "valid_row_count": 501,
        "inexact_row_count": 0,
        "missing_field_row_count": 0,
        "invalid_row_count": 0,
        "stateful_invalid_row_count": 0,
        "context_not_bound_to_controls_row_count": 0,
        "source_after_controls_row_count": 0,
        "source_identity_invalid_count": 0,
        "factor_source_counts": {"car_params": 501},
        "offset_source_counts": {"car_params": 501},
        "friction_source_counts": {"car_params": 501},
      },
      "controller_provenance": {
        "car_params_wire_sha256": "5" * 64,
        "controller_params_sha256": "6" * 64,
        "baseline_controller_profile": (_historical_controller_profile()),
        "flm_active": False,
        "flm_active_available": True,
        "trailer_load_kg": 0.0,
        "trailer_load_available": True,
        "resolved_toggle_snapshots": [
          {
            "sha256": "8" * 64,
            "source": "starpilotPlan.starpilotToggles",
            "valid": True,
            "values": {
              "nnff": False,
              "nnff_lite": False,
              "nnff_model_name": "HYUNDAI IONIQ 5",
            },
            "segment_num": 0,
            "source_ordinal": 10,
            "log_mono_time_ns": "1000000000",
          }
        ],
      },
    },
    "provenance": {
      "extractor": "comma-companion-rlog",
      "extractor_version": "1.1.0",
      "extractor_source_sha256": EXTRACTOR_HASH,
      "source_starpilot_commit": (HISTORICAL_CONTROLLER_SOURCE_COMMIT),
      "source_objects": [
        {
          "segment_num": 0,
          "log_type": "rlog",
          "sha256": "7" * 64,
        }
      ],
    },
  }


def _model_metadata() -> dict[str, Any]:
  return {
    "causal_training_eligible": True,
    "model": {
      "sha256": MODEL_HASH,
      "promoted_artifact_verified": True,
      "review_registry_match": True,
      "causal_training_eligible": True,
      "training_alignment": ALIGNMENT,
      "training_schema": DYNAMICS_SCHEMA,
      "training_schema_version": 1,
      "training_extraction_version": 9,
      "trainer_schema": "starpilot.neural-lateral-plant",
      "trainer_schema_version": 7,
      "training_extractor_sha256": TRAINING_EXTRACTOR_HASH,
      "trainer_sha256": "0" * 64,
      "compatible_telemetry_extractor_sha256": EXTRACTOR_HASH,
      "compatible_telemetry_extractor_version": "1.1.0",
      "max_asof_age_ms": 35,
      "sampling": {
        "grid": "absolute_monotonic_time",
        "absolute_grid_field": "nominal_log_mono_time_ns",
        "absolute_grid_phase_ns": 0,
        "sample_period_ns": 10_000_000,
        "sample_rate_hz": 100.0,
        "first_tick_formula": ("ceil(first_valid_carState_logMonoTime_ns/" + "sample_period_ns)*sample_period_ns"),
        "alignment": ("latest_at_or_before_grid_time_zero_order_hold"),
        "source_selection": ("independent_per_source_max_valid_source_with_" + "logMonoTime_at_or_before_tick"),
        "invalid_event_policy": {
          "carState": ("drop_without_invalidating_prior_valid_state"),
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
      },
    },
    "adapter": {
      "capabilities": {
        "causal_replay_eligible": True,
      },
    },
  }


def _seed_eligible(connection: sqlite3.Connection) -> None:
  ndjson_path = f"telemetry/device-one/{ROUTE_NAME}/v1/" + f"telemetry.{TELEMETRY_HASH}.ndjson"
  connection.execute(
    """
    INSERT INTO drives(
      id, route_name, telemetry_ready
    ) VALUES (?, ?, 1)
    """,
    (DRIVE_ID, ROUTE_NAME),
  )
  connection.execute(
    """
    INSERT INTO route_inventories(
      id, drive_id, generation, state, route_closed,
      rlog_source_fingerprint
    ) VALUES ('inventory-one', ?, 1, 'complete', 1, ?)
    """,
    (DRIVE_ID, RLOG_SOURCE_FINGERPRINT),
  )
  connection.execute(
    """
    INSERT INTO telemetry_indexes(
      drive_id, state, ndjson_path, ndjson_sha256,
      manifest_json, source_fingerprint
    ) VALUES (?, 'complete', ?, ?, ?, ?)
    """,
    (
      DRIVE_ID,
      ndjson_path,
      TELEMETRY_HASH,
      json.dumps(_manifest(), separators=(",", ":"), sort_keys=True),
      RLOG_SOURCE_FINGERPRINT,
    ),
  )
  connection.execute(
    """
    INSERT INTO telemetry_dynamics_chunks(
      drive_id, chunk_index, start_t_us, end_t_us,
      ndjson_path, byte_offset, byte_length, record_sha256
    ) VALUES (?, 0, 0, 5000000, ?, 100, 4096, ?)
    """,
    (DRIVE_ID, ndjson_path, "1" * 64),
  )
  connection.execute(
    """
    INSERT INTO model_registry(
      sha256, name, enabled, mode, metadata_json, created_at
    ) VALUES (?, 'Ioniq causal plant', 1, ?, ?, '2026-07-29T00:00:00Z')
    """,
    (
      MODEL_HASH,
      MODE,
      json.dumps(
        _model_metadata(),
        separators=(",", ":"),
        sort_keys=True,
      ),
    ),
  )


def _change_manifest(
  connection: sqlite3.Connection,
  mutate: Callable[[dict[str, Any]], None],
) -> None:
  raw = connection.execute(
    "SELECT manifest_json FROM telemetry_indexes WHERE drive_id = ?",
    (DRIVE_ID,),
  ).fetchone()[0]
  manifest = json.loads(raw)
  mutate(manifest)
  connection.execute(
    "UPDATE telemetry_indexes SET manifest_json = ? WHERE drive_id = ?",
    (
      json.dumps(manifest, separators=(",", ":"), sort_keys=True),
      DRIVE_ID,
    ),
  )


def _codes(result: dict[str, Any]) -> list[str]:
  return [reason["code"] for reason in result["reasons"]]


def test_drive_and_point_eligibility_return_pinnable_generation(
  connection: sqlite3.Connection,
) -> None:
  drive = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )
  point = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
    model_hash=MODEL_HASH,
    mode=MODE,
    t_us=3_000_000,
    horizon_us=1_000_000,
  )

  assert drive["eligible"] is True
  assert drive["reasons"] == []
  assert drive["telemetry_generation"] == {
    "ndjson_sha256": TELEMETRY_HASH,
    "timeline_version": TIMELINE_VERSION,
  }
  assert drive["model"]["sha256"] == MODEL_HASH
  assert drive["baseline_params"] == HISTORICAL_CONTROLLER_PARAMS
  assert drive["baseline_controller_profile"] == (_historical_controller_profile())
  assert point["eligible"] is True
  assert point["telemetry_generation"] == drive["telemetry_generation"]


def test_drive_telemetry_readiness_marker_is_required(
  connection: sqlite3.Connection,
) -> None:
  connection.execute(
    "UPDATE drives SET telemetry_ready = 0 WHERE id = ?",
    (DRIVE_ID,),
  )

  result = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )

  assert result["eligible"] is False
  assert _codes(result) == ["telemetry_not_ready"]


def test_valid_shape_wrong_extractor_hash_is_not_causal_telemetry(
  connection: sqlite3.Connection,
) -> None:
  def replace_extractor_hash(manifest: dict[str, Any]) -> None:
    manifest["dynamics"]["telemetry_provenance"][
      "extractor_source_sha256"
    ] = "0" * 64
    manifest["provenance"]["extractor_source_sha256"] = "0" * 64

  _change_manifest(connection, replace_extractor_hash)

  result = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )

  assert result["eligible"] is False
  assert "causal_telemetry_required" in _codes(result)


def test_parsed_generation_must_be_declared_by_latest_inventory(
  connection: sqlite3.Connection,
) -> None:
  connection.execute(
    """
    UPDATE telemetry_indexes
    SET source_fingerprint = ?
    WHERE drive_id = ?
    """,
    ("0" * 64, DRIVE_ID),
  )

  result = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )

  assert result["eligible"] is False
  assert _codes(result) == ["immutable_generation_unverified"]


@pytest.mark.parametrize(
  ("state", "route_closed", "source_fingerprint"),
  [
    ("complete", 1, "0" * 64),
    ("partial", 1, RLOG_SOURCE_FINGERPRINT),
    ("complete", 0, RLOG_SOURCE_FINGERPRINT),
  ],
)
def test_superseding_inventory_generation_must_be_immutable_and_match(
  connection: sqlite3.Connection,
  state: str,
  route_closed: int,
  source_fingerprint: str,
) -> None:
  connection.execute(
    """
    INSERT INTO route_inventories(
      id, drive_id, generation, state, route_closed,
      rlog_source_fingerprint
    ) VALUES ('inventory-two', ?, 2, ?, ?, ?)
    """,
    (
      DRIVE_ID,
      state,
      route_closed,
      source_fingerprint,
    ),
  )

  result = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )

  assert result["eligible"] is False
  assert _codes(result) == ["immutable_generation_unverified"]


def test_profile_selection_accepts_all_reviewed_and_rejects_cross_selection(
  connection: sqlite3.Connection,
) -> None:
  def select_c6(manifest: dict[str, Any]) -> None:
    manifest["provenance"]["source_starpilot_commit"] = (
      C6_CONTROLLER_SOURCE_COMMIT
    )
    manifest["dynamics"]["controller_provenance"][
      "baseline_controller_profile"
    ] = _c6_controller_profile()

  _change_manifest(connection, select_c6)
  c6 = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )

  assert c6["eligible"] is True
  assert c6["baseline_params"] == C6_CONTROLLER_PARAMS
  assert c6["baseline_controller_profile"] == _c6_controller_profile()

  def select_current(manifest: dict[str, Any]) -> None:
    manifest["provenance"]["source_starpilot_commit"] = CURRENT_CONTROLLER_SOURCE_COMMIT
    manifest["dynamics"]["controller_provenance"]["baseline_controller_profile"] = _current_controller_profile()

  _change_manifest(connection, select_current)
  current = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )

  assert current["eligible"] is True
  assert current["baseline_params"] == CURRENT_CONTROLLER_PARAMS
  assert current["baseline_controller_profile"] == (_current_controller_profile())

  _change_manifest(
    connection,
    lambda manifest: manifest["dynamics"]["controller_provenance"].update(
      {
        "baseline_controller_profile": (_historical_controller_profile()),
      }
    ),
  )
  crossed = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )

  assert crossed["eligible"] is False
  assert "controller_provenance_incomplete" in _codes(crossed)


def test_requested_window_ignores_unrelated_early_gap(
  connection: sqlite3.Connection,
) -> None:
  row_count = 22_509

  def install_real_route_shape(
    manifest: dict[str, Any],
  ) -> None:
    dynamics = manifest["dynamics"]
    dynamics["row_count"] = row_count
    source_age = dynamics["source_age_validation"]
    source_age["checked_row_count"] = row_count
    source_age["valid_row_count"] = row_count
    controller = dynamics["controller_selection_validation"]
    controller["checked_row_count"] = row_count
    controller["resolved_row_count"] = row_count
    controller["conventional_torque_row_count"] = row_count
    effective = dynamics["effective_torque_context_validation"]
    effective["checked_row_count"] = row_count
    effective["exact_row_count"] = row_count
    effective["valid_row_count"] = row_count
    for field in (
      "factor_source_counts",
      "offset_source_counts",
      "friction_source_counts",
    ):
      effective[field] = {"car_params": row_count}

  _change_manifest(connection, install_real_route_shape)
  ndjson_path = connection.execute(
    """
    SELECT ndjson_path
    FROM telemetry_indexes
    WHERE drive_id = ?
    """,
    (DRIVE_ID,),
  ).fetchone()[0]
  connection.execute(
    "DELETE FROM telemetry_dynamics_chunks WHERE drive_id = ?",
    (DRIVE_ID,),
  )
  connection.executemany(
    """
    INSERT INTO telemetry_dynamics_chunks(
      drive_id, chunk_index, start_t_us, end_t_us,
      ndjson_path, byte_offset, byte_length, record_sha256
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
      (
        DRIVE_ID,
        0,
        5_031_739,
        6_151_739,
        ndjson_path,
        100,
        4096,
        "1" * 64,
      ),
      (
        DRIVE_ID,
        1,
        6_741_739,
        230_691_739,
        ndjson_path,
        5000,
        4096,
        "2" * 64,
      ),
    ),
  )

  clean_later_window = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
    model_hash=MODEL_HASH,
    mode=MODE,
    t_us=89_177_278,
    horizon_us=1_000_000,
  )
  point_in_early_gap = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
    model_hash=MODEL_HASH,
    mode=MODE,
    t_us=6_500_000,
    horizon_us=1_000_000,
  )

  assert clean_later_window["eligible"] is True
  assert clean_later_window["reasons"] == []
  assert point_in_early_gap["eligible"] is False
  assert "dynamics_gap_at_point" in _codes(point_in_early_gap)


def test_drive_level_excludes_playhead_history_gate(
  connection: sqlite3.Connection,
) -> None:
  drive = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )
  early_point = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
    t_us=1_000_000,
    horizon_us=1_000_000,
  )
  late_point = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
    t_us=4_500_000,
    horizon_us=1_000_000,
  )

  assert drive["eligible"] is True
  assert _codes(early_point) == ["insufficient_history"]
  assert _codes(late_point) == ["insufficient_future"]


@pytest.mark.parametrize(
  ("mutate", "expected_code"),
  [
    (
      lambda manifest: manifest["dynamics"].pop(
        "source_age_validation",
      ),
      "causal_source_ages_unverified",
    ),
    (
      lambda manifest: manifest["dynamics"]["source_age_validation"].update({"schema_version": True}),
      "causal_source_ages_unverified",
    ),
    (
      lambda manifest: manifest["completeness"]["segments"][0].update(
        {"log_type": "qlog"},
      ),
      "full_rlog_required",
    ),
    (
      lambda manifest: manifest["dynamics"].pop(
        "controller_selection_validation",
      ),
      "controller_identity_unverified",
    ),
    (
      lambda manifest: manifest["dynamics"]["controller_selection_validation"]["evaluator"].update({"version": True}),
      "controller_identity_unverified",
    ),
    (
      lambda manifest: manifest["dynamics"]["controller_selection_validation"]["evaluator"].update(
        {"source_sha256": "0" * 64}
      ),
      "controller_identity_unverified",
    ),
    (
      lambda manifest: manifest["dynamics"].pop(
        "effective_torque_context_validation",
      ),
      "controller_provenance_incomplete",
    ),
    (
      lambda manifest: manifest["dynamics"]["effective_torque_context_validation"].update({"schema_version": True}),
      "controller_provenance_incomplete",
    ),
    (
      lambda manifest: manifest["dynamics"]["effective_torque_context_validation"]["evaluator"].update(
        {"source_sha256": "0" * 64}
      ),
      "controller_provenance_incomplete",
    ),
    (
      lambda manifest: manifest["dynamics"]["effective_torque_context_validation"].update(
        {"stateful_invalid_row_count": 1}
      ),
      "controller_provenance_incomplete",
    ),
    (
      lambda manifest: manifest["dynamics"]["controller_provenance"].pop("baseline_controller_profile"),
      "controller_provenance_incomplete",
    ),
    (
      lambda manifest: manifest["dynamics"]["controller_provenance"]["baseline_controller_profile"]["baseline_controller_params"].update(
        {"friction_scale_mult": 0.8}
      ),
      "controller_provenance_incomplete",
    ),
    (
      lambda manifest: manifest["dynamics"]["controller_provenance"]["baseline_controller_profile"].update(
        {
          "kernel_schema_version": True,
        }
      ),
      "controller_provenance_incomplete",
    ),
    (
      lambda manifest: manifest["dynamics"]["controller_provenance"]["baseline_controller_profile"]["evaluator"].update(
        {
          "version": True,
        }
      ),
      "controller_provenance_incomplete",
    ),
    (
      lambda manifest: manifest["dynamics"]["controller_provenance"]["baseline_controller_profile"]["evaluator"].update(
        {
          "source_sha256": "0" * 64,
        }
      ),
      "controller_provenance_incomplete",
    ),
    (
      lambda manifest: manifest["dynamics"]["controller_selection_validation"].update(
        {
          "controller_types": ["nnff"],
          "conventional_torque_row_count": 0,
          "nnff_row_count": 501,
        }
      ),
      "unsupported_controller_type",
    ),
  ],
)
def test_telemetry_and_controller_gates_fail_closed(
  connection: sqlite3.Connection,
  mutate: Callable[[dict[str, Any]], None],
  expected_code: str,
) -> None:
  _change_manifest(connection, mutate)

  result = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )

  assert result["eligible"] is False
  assert expected_code in _codes(result)


def test_model_extractor_compatibility_is_exact(
  connection: sqlite3.Connection,
) -> None:
  metadata = _model_metadata()
  metadata["model"]["compatible_telemetry_extractor_sha256"] = "8" * 64
  connection.execute(
    "UPDATE model_registry SET metadata_json = ? WHERE sha256 = ?",
    (
      json.dumps(metadata, separators=(",", ":"), sort_keys=True),
      MODEL_HASH,
    ),
  )

  result = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
    model_hash=MODEL_HASH,
  )

  assert result["eligible"] is False
  assert "model_telemetry_provenance_mismatch" in _codes(result)


def test_boolean_model_schema_version_is_not_integer_one(
  connection: sqlite3.Connection,
) -> None:
  metadata = _model_metadata()
  metadata["model"]["training_schema_version"] = True
  connection.execute(
    "UPDATE model_registry SET metadata_json = ? WHERE sha256 = ?",
    (
      json.dumps(metadata, separators=(",", ":"), sort_keys=True),
      MODEL_HASH,
    ),
  )

  result = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
    model_hash=MODEL_HASH,
  )

  assert result["eligible"] is False
  assert "causal_model_required" in _codes(result)


def test_lateral_tune_parent_disables_stale_nnff_child_toggle(
  connection: sqlite3.Connection,
) -> None:
  def disable_parent(manifest: dict[str, Any]) -> None:
    params = manifest["route_software"]["controller_params"]
    params["LateralTune"]["text"] = "0"
    params["NNFF"]["text"] = "1"
    params["NNFFLite"]["text"] = "1"

  _change_manifest(connection, disable_parent)

  result = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )

  assert result["eligible"] is True
  assert "unsupported_controller_type" not in _codes(result)


def test_historical_no_flm_commit_uses_versioned_false_proof(
  connection: sqlite3.Connection,
) -> None:
  old_commit = "2747bf037c0f284500457f1befb4f52415e3285a"

  def use_historical_flm_proof(
    manifest: dict[str, Any],
  ) -> None:
    manifest["provenance"]["source_starpilot_commit"] = old_commit
    controller = manifest["dynamics"]["controller_provenance"]
    controller["flm_active"] = None
    controller["flm_active_available"] = False
    controller["flm_resolution"] = {
      "state": "verified",
      "flm_active": False,
      "source": "versioned_source_commit_evaluator",
      "evaluator": {
        "name": ("starpilot-flm-availability-by-source-commit"),
        "version": 1,
        "source_commit": old_commit,
        "source_sha256": HISTORICAL_FLM_EVALUATOR_SHA256,
      },
    }

  _change_manifest(connection, use_historical_flm_proof)

  result = evaluate_simulation_eligibility(connection, DRIVE_ID)

  assert result["eligible"] is True
  assert "controller_provenance_incomplete" not in _codes(result)

  _change_manifest(
    connection,
    lambda manifest: manifest["dynamics"]["controller_provenance"]["flm_resolution"]["evaluator"].update(
      {
        "source_sha256": "0" * 64,
      }
    ),
  )
  rejected = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )
  assert rejected["eligible"] is False
  assert "controller_provenance_incomplete" in _codes(rejected)

  _change_manifest(connection, use_historical_flm_proof)
  _change_manifest(
    connection,
    lambda manifest: manifest["dynamics"]["controller_provenance"]["flm_resolution"]["evaluator"].update(
      {
        "source_commit": "f" * 40,
      }
    ),
  )
  rejected_commit = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
  )
  assert rejected_commit["eligible"] is False
  assert "controller_provenance_incomplete" in _codes(rejected_commit)


def test_c6_no_flm_commit_uses_distinct_profile_and_shared_false_proof(
  connection: sqlite3.Connection,
) -> None:
  def use_c6_flm_proof(manifest: dict[str, Any]) -> None:
    manifest["provenance"]["source_starpilot_commit"] = (
      C6_CONTROLLER_SOURCE_COMMIT
    )
    controller = manifest["dynamics"]["controller_provenance"]
    controller["baseline_controller_profile"] = _c6_controller_profile()
    manifest["dynamics"]["controller_selection_validation"][
      "source_types"
    ] = ["versioned_initData_fallback"]
    controller["init_data_fallback_evaluator"] = {
      "state": "available",
      "name": EFFECTIVE_TORQUE_CONTEXT_EVALUATOR,
      "version": 1,
      "evaluator_id": TORQUE_CONTEXT_EVALUATOR_IDS[
        C6_CONTROLLER_SOURCE_COMMIT
      ],
      "source_commit": C6_CONTROLLER_SOURCE_COMMIT,
      "source_sha256": EFFECTIVE_TORQUE_CONTEXT_EVALUATOR_SHA256,
    }
    controller["flm_active"] = None
    controller["flm_active_available"] = False
    controller["flm_resolution"] = {
      "state": "verified",
      "flm_active": False,
      "source": "versioned_source_commit_evaluator",
      "evaluator": {
        "name": "starpilot-flm-availability-by-source-commit",
        "version": 1,
        "source_commit": C6_CONTROLLER_SOURCE_COMMIT,
        "source_sha256": HISTORICAL_FLM_EVALUATOR_SHA256,
      },
    }

  _change_manifest(connection, use_c6_flm_proof)

  result = evaluate_simulation_eligibility(connection, DRIVE_ID)

  assert result["eligible"] is True
  assert result["baseline_params"] == C6_CONTROLLER_PARAMS


def test_init_data_fallback_requires_exact_commit_bound_evaluator(
  connection: sqlite3.Connection,
) -> None:
  def select_fallback(manifest: dict[str, Any]) -> None:
    manifest["dynamics"]["controller_selection_validation"][
      "source_types"
    ] = ["versioned_initData_fallback"]
    manifest["dynamics"]["controller_provenance"][
      "init_data_fallback_evaluator"
    ] = {
      "state": "available",
      "name": EFFECTIVE_TORQUE_CONTEXT_EVALUATOR,
      "version": 1,
      "evaluator_id": TORQUE_CONTEXT_EVALUATOR_IDS[
        HISTORICAL_CONTROLLER_SOURCE_COMMIT
      ],
      "source_commit": HISTORICAL_CONTROLLER_SOURCE_COMMIT,
      "source_sha256": EFFECTIVE_TORQUE_CONTEXT_EVALUATOR_SHA256,
    }

  _change_manifest(connection, select_fallback)
  accepted = evaluate_simulation_eligibility(connection, DRIVE_ID)
  assert accepted["eligible"] is True

  _change_manifest(
    connection,
    lambda manifest: manifest["dynamics"]["controller_provenance"][
      "init_data_fallback_evaluator"
    ].update({"source_sha256": "0" * 64}),
  )
  rejected = evaluate_simulation_eligibility(connection, DRIVE_ID)
  assert rejected["eligible"] is False
  assert "controller_identity_unverified" in _codes(rejected)


def test_invalid_generation_and_point_use_stable_reason_codes(
  connection: sqlite3.Connection,
) -> None:
  connection.execute(
    """
    UPDATE telemetry_indexes
    SET ndjson_path = '../escape.ndjson'
    WHERE drive_id = ?
    """,
    (DRIVE_ID,),
  )

  result = evaluate_simulation_eligibility(
    connection,
    DRIVE_ID,
    t_us=3_000_000,
  )

  assert result["eligible"] is False
  assert _codes(result) == [
    "immutable_generation_unverified",
    "dynamics_index_unavailable",
    "invalid_point_request",
  ]
