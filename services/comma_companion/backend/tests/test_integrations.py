from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
from pathlib import Path
from typing import Any

import pytest

from comma_companion.controller_profile import (
  CONTROLLER_KERNEL_SCHEMA,
  CONTROLLER_PARAMS_VALUE_SPACE,
  CONTROLLER_PROFILE_EVALUATOR,
  CONTROLLER_PROFILE_EVALUATOR_SHA256,
  HISTORICAL_CONTROLLER_PARAMS,
  HISTORICAL_CONTROLLER_PROFILE_ID,
  HISTORICAL_CONTROLLER_PROFILE_SHA256,
  HISTORICAL_CONTROLLER_SOURCE_COMMIT,
)
from comma_companion.db import Database
from comma_companion.integrations import (
  CONTROLLER_SELECTION_EVALUATOR_SHA256,
  EFFECTIVE_TORQUE_CONTEXT_EVALUATOR,
  EFFECTIVE_TORQUE_CONTEXT_EVALUATOR_SHA256,
  MEDIA_BITRATE_POLICY_PROFILE,
  MEDIA_GENERATION_SCHEMA_VERSION,
  MEDIA_SCHEMA_VERSION,
  IntegrationError,
  IntegrationHandlers,
  HISTORICAL_FLM_EVALUATOR_SHA256,
  ProcessResult,
  REFERENCE_DYNAMICS_MODEL_SHA256,
  REFERENCE_TELEMETRY_EXTRACTOR_SHA256,
  REQUIRED_TELEMETRY_SCHEMA_SQL,
  canonical_json,
  telemetry_source_fingerprint,
)


NOW = "2026-07-29T10:00:00Z"
DEVICE_ID = "device-one"
DRIVE_ID = "drive-one"
SEGMENT_ID = "segment-zero"
ROUTE_NAME = "00000001--abc123def0"


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


class FakeContext:
  def __init__(self, job_id: str):
    self.job_id = job_id
    self.cancel_event = threading.Event()
    self.cancel_path: Path | None = None
    self.progress_values: list[float] = []

  def progress(self, fraction: float) -> None:
    self.progress_values.append(fraction)

  def cancellation_requested(self) -> bool:
    return self.cancel_event.is_set()

  def raise_if_cancelled(self) -> None:
    if self.cancel_event.is_set():
      raise RuntimeError("cancelled")


@pytest.fixture
def database(tmp_path: Path) -> Database:
  result = Database(tmp_path / "local" / "companion.sqlite3")
  result.initialize()
  with result.connection() as connection:
    connection.executescript(REQUIRED_TELEMETRY_SCHEMA_SQL)
  return result


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
  root = tmp_path / "archive"
  for name in ("objects", "derived", "telemetry", "thumbnails"):
    (root / name).mkdir(parents=True)
  return root


def _seed_artifact(
  database: Database,
  archive_root: Path,
  *,
  artifact_id: str,
  kind: str,
  relative_path: str,
  content: bytes,
  camera: str | None = None,
) -> tuple[str, str]:
  digest = hashlib.sha256(content).hexdigest()
  storage_path = Path("objects") / "sha256" / digest[:2] / digest[2:4] / digest
  object_path = archive_root / storage_path
  object_path.parent.mkdir(parents=True, exist_ok=True)
  object_path.write_bytes(content)
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT OR IGNORE INTO devices(
        id, display_name, token_hash, enrolled_at
      ) VALUES (?, ?, ?, ?)
      """,
      (DEVICE_ID, "Test device", "token-hash", NOW),
    )
    connection.execute(
      """
      INSERT OR IGNORE INTO drives(
        id, device_id, route_name, created_at
      ) VALUES (?, ?, ?, ?)
      """,
      (DRIVE_ID, DEVICE_ID, ROUTE_NAME, NOW),
    )
    connection.execute(
      """
      INSERT OR IGNORE INTO segments(
        id, drive_id, number, created_at
      ) VALUES (?, ?, 0, ?)
      """,
      (SEGMENT_ID, DRIVE_ID, NOW),
    )
    connection.execute(
      """
      INSERT OR IGNORE INTO objects(
        sha256, size, storage_path, created_at
      ) VALUES (?, ?, ?, ?)
      """,
      (digest, len(content), storage_path.as_posix(), NOW),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, camera, relative_path, storage_path, size,
        status, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stored', ?)
      """,
      (
        artifact_id,
        DEVICE_ID,
        DRIVE_ID,
        SEGMENT_ID,
        digest,
        kind,
        camera,
        relative_path,
        storage_path.as_posix(),
        len(content),
        NOW,
      ),
    )
  return digest, storage_path.as_posix()


def _seed_complete_inventory(
  database: Database,
  source_fingerprint: str,
) -> None:
  manifest_json = "{}"
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO route_inventories(
        id, device_id, drive_id, route_name, generation,
        manifest_sha256, rlog_source_fingerprint,
        manifest_size, materialized_row_count,
        state, route_closed, manifest_json, created_at
      ) VALUES (
        'inventory-one', ?, ?, ?, 1, ?, ?, ?, 0,
        'complete', 1, ?, ?
      )
      """,
      (
        DEVICE_ID,
        DRIVE_ID,
        ROUTE_NAME,
        "a" * 64,
        source_fingerprint,
        len(manifest_json.encode()),
        manifest_json,
        NOW,
      ),
    )


def _seed_ready_media_sync_inputs(
  database: Database,
  archive_root: Path,
) -> str:
  _seed_artifact(
    database,
    archive_root,
    artifact_id="artifact-video-source",
    kind="fcamera",
    camera="road",
    relative_path=f"realdata/{ROUTE_NAME}--0/fcamera.hevc",
    content=b"raw-video",
  )
  derived_id = "artifact-video-derived"
  video_content = b"derived-video"
  video_sha256 = hashlib.sha256(video_content).hexdigest()
  video_storage = Path("derived") / DEVICE_ID / ROUTE_NAME / "0" / "road.webm"
  video_path = archive_root / video_storage
  video_path.parent.mkdir(parents=True, exist_ok=True)
  video_path.write_bytes(video_content)
  frame_document = {
    "schema_version": 1,
    "mapping_type": "encoded_frame_pts",
    "join_key": ["camera", "segment_num", "segment_frame_id"],
    "ordinal_basis": 0,
    "source_frame_key": "segment_frame_id",
    "camera": "road",
    "segment_num": 0,
    "source_artifact_id": "artifact-video-source",
    "video": {"sha256": video_sha256},
    "frame_count": 1,
    "frames": [
      {
        "ordinal": 0,
        "segment_frame_id": 0,
        "pts_us": 0,
        "duration_us": 10_000,
        "keyframe": True,
      },
    ],
  }
  frame_content = (canonical_json(frame_document) + "\n").encode()
  frame_sha256 = hashlib.sha256(frame_content).hexdigest()
  frame_storage = video_storage.with_name("road.frames.json")
  (archive_root / frame_storage).write_bytes(frame_content)
  with database.transaction(immediate=True) as connection:
    connection.executemany(
      """
      INSERT INTO objects(sha256, size, storage_path, created_at)
      VALUES (?, ?, ?, ?)
      """,
      [
        (
          video_sha256,
          len(video_content),
          video_storage.as_posix(),
          NOW,
        ),
        (
          frame_sha256,
          len(frame_content),
          frame_storage.as_posix(),
          NOW,
        ),
      ],
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, camera, relative_path, storage_path, size,
        mime_type, codec, duration_us, frame_count, status,
        source_artifact_id, created_at
      ) VALUES (
        ?, ?, ?, ?, ?, 'derived_video', 'road', ?, ?, ?,
        'video/webm', 'av1', 10000, 1, 'ready', ?, ?
      )
      """,
      (
        derived_id,
        DEVICE_ID,
        DRIVE_ID,
        SEGMENT_ID,
        video_sha256,
        video_storage.as_posix(),
        video_storage.as_posix(),
        len(video_content),
        "artifact-video-source",
        NOW,
      ),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, camera, relative_path, storage_path, size,
        mime_type, status, source_artifact_id, created_at
      ) VALUES (
        'artifact-frame-index', ?, ?, ?, ?,
        'video_frame_index', 'road', ?, ?, ?,
        'application/json', 'ready', ?, ?
      )
      """,
      (
        DEVICE_ID,
        DRIVE_ID,
        SEGMENT_ID,
        frame_sha256,
        frame_storage.as_posix(),
        frame_storage.as_posix(),
        len(frame_content),
        derived_id,
        NOW,
      ),
    )
  return derived_id


def _telemetry_records(
  signals: dict[str, list[Any]],
  *,
  times: list[int],
  dynamics_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
  catalog = []
  series = []
  for index, (signal, values) in enumerate(signals.items()):
    value_type = "bool" if values and isinstance(values[0], bool) else "float"
    interpolation = "transitions" if value_type == "bool" else "linear"
    catalog.append(
      {
        "id": signal,
        "value_type": value_type,
        "unit": None,
        "source": "fixture",
        "interpolation": interpolation,
        "downsample": "numeric_envelope",
      }
    )
    series.append(
      {
        "record": "series_chunk",
        "signal": signal,
        "tier": "full",
        "chunk": index,
        "t_us": times,
        "v": values,
      }
    )
  manifest = {
    "record": "manifest",
    "schema": "comma-companion.telemetry-manifest",
    "schema_version": 1,
    "route_id": ROUTE_NAME,
    "source_route": ROUTE_NAME,
    "state": "complete",
    "publication_ready": True,
    "timeline_version": "c" * 64,
    "timebase": {
      "unit": "us",
      "origin_log_mono_time_ns": "1000000000",
      "conversion": "floor((logMonoTime-origin)/1000)",
      "utc_start_us": "1800000000000000",
      "utc_anchors": [],
    },
    "range": {
      "start_us": times[0],
      "end_us": times[-1],
    },
    "tiers": [{"id": "full", "width_us": None}],
    "signals": catalog,
    "vehicle": {
      "car_fingerprint": "HYUNDAI_IONIQ_5",
      "brand": "hyundai",
      "steer_control_type": "torque",
      "lateral_tuning_type": "torque",
      "lateral_torque_tuning": {
        "lat_accel_factor": 2.5,
        "lat_accel_offset": 0.0,
        "friction": 0.1,
        "steering_angle_deadzone_deg": 0.0,
      },
    },
    "route_software": {
      "controller_params": {
        "LateralTune": {
          "sha256": "c" * 64,
          "size_bytes": 1,
          "text": "1",
        },
        "NNFF": {
          "sha256": "d" * 64,
          "size_bytes": 1,
          "text": "0",
        },
        "NNFFLite": {
          "sha256": "e" * 64,
          "size_bytes": 1,
          "text": "0",
        },
        "NNFFModelName": {
          "sha256": "f" * 64,
          "size_bytes": 16,
          "text": "HYUNDAI IONIQ 5",
        },
      },
    },
    "frame_counts": {"road": 1},
    "completeness": {
      "supplied_segment_count": 1,
      "parsed_segment_numbers": [0],
      "missing_segment_numbers": [],
      "failed_segment_numbers": [],
      "contiguous_from_segment_zero": True,
      "route_start_observed": True,
      "route_end_observed": True,
      "boundary_chain_valid": True,
      "segments": [
        {
          "segment_num": 0,
          "directory_name": f"{ROUTE_NAME}--0",
          "state": "complete",
          "range_us": [times[0], times[-1]],
          "start_t_us": times[0],
          "start_time_source": "road.encode_index.timestamp_eof",
          "camera_ranges_us": {"road": [times[0], times[-1]]},
          "rlog_sha256": "b" * 64,
          "source_size_bytes": 4,
          "compression": "none",
          "event_count": len(times),
          "message_counts": {},
          "log_type": "rlog",
          "boundary": {
            "start_valid": True,
            "start": {"type": "startOfRoute"},
            "terminal_type": "endOfRoute",
          },
        }
      ],
    },
    "dynamics": {
      "schema": "comma-companion.dynamics-row",
      "schema_version": 1,
      "state": "available",
      "alignment": "timestamp_causal_recorded_history_asof",
      "sample_period_us": 10_000,
      "controller_i_timing": "post_update_asof_source_row",
      "row_count": len(dynamics_rows or []),
      "nonzero_logged_jerk_row_count": 0,
      "feedforward_eligible_row_count": len(dynamics_rows or []),
      "drop_counts": {},
      "causal_input_eligible": bool(dynamics_rows),
      "required_model_training_alignment": ("timestamp_causal_recorded_history_asof"),
      "telemetry_provenance": {
        "schema": "comma-companion.dynamics-row",
        "schema_version": 1,
        "alignment": "timestamp_causal_recorded_history_asof",
        "causal_input_eligible": bool(dynamics_rows),
        "extractor_version": "1.1.0",
        "extractor_source_sha256": (
          REFERENCE_TELEMETRY_EXTRACTOR_SHA256
        ),
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
        "checked_row_count": len(dynamics_rows or []),
        "valid_row_count": len(dynamics_rows or []),
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
        "checked_row_count": len(dynamics_rows or []),
        "resolved_row_count": len(dynamics_rows or []),
        "missing_row_count": 0,
        "invalid_row_count": 0,
        "controller_types": ["conventional_torque"],
        "conventional_torque_row_count": len(
          dynamics_rows or [],
        ),
        "nnff_row_count": 0,
        "nnff_lite_row_count": 0,
        "unsupported_row_count": 0,
        "source_types": [
          "starpilotPlan.starpilotToggles",
        ],
        "snapshot_hashes": ["a" * 64],
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
        "checked_row_count": len(dynamics_rows or []),
        "exact_row_count": len(dynamics_rows or []),
        "valid_row_count": len(dynamics_rows or []),
        "inexact_row_count": 0,
        "missing_field_row_count": 0,
        "invalid_row_count": 0,
        "stateful_invalid_row_count": 0,
        "context_not_bound_to_controls_row_count": 0,
        "source_after_controls_row_count": 0,
        "source_identity_invalid_count": 0,
        "factor_source_counts": {
          "car_params": len(dynamics_rows or []),
        },
        "offset_source_counts": {
          "car_params": len(dynamics_rows or []),
        },
        "friction_source_counts": {
          "car_params": len(dynamics_rows or []),
        },
      },
      "controller_provenance": {
        "lateral_tuning_type": "torque",
        "steer_control_type": "torque",
        "car_params_wire_sha256": "e" * 64,
        "controller_params_sha256": "1" * 64,
        "baseline_controller_profile": (_historical_controller_profile()),
        "flm_active": False,
        "flm_active_available": True,
        "trailer_load_kg": 0.0,
        "trailer_load_available": True,
        "resolved_toggle_snapshots": [
          {
            "sha256": "a" * 64,
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
        "baseline_exact_claim_allowed": False,
        "limitations": ["fixture"],
      },
    },
    "provenance": {
      "extractor": "comma-companion-rlog",
      "extractor_version": "1.1.0",
      "starpilot_commit": "fixture",
      "source_starpilot_commit": (HISTORICAL_CONTROLLER_SOURCE_COMMIT),
      "extractor_source_sha256": (
        REFERENCE_TELEMETRY_EXTRACTOR_SHA256
      ),
      "car_params_wire_sha256": "e" * 64,
      "car_params_summary_sha256": "f" * 64,
      "controller_params_sha256": "1" * 64,
      "baseline_exact_claim_allowed": False,
      "baseline_limitations": ["fixture"],
      "source_objects": [
        {
          "segment_num": 0,
          "sha256": "b" * 64,
          "log_type": "rlog",
          "size_bytes": 4,
          "compression": "none",
          "file_name": "rlog",
        }
      ],
    },
    "warnings": [],
  }
  return [
    {
      "record": "stream_header",
      "schema": "comma-companion.telemetry",
      "schema_version": 1,
      "extractor_version": "1.1.0",
      "route_id": ROUTE_NAME,
      "log_type": "rlog",
      "timebase": {
        "unit": "us",
        "origin_log_mono_time_ns": "1000000000",
        "conversion": "floor((logMonoTime-origin)/1000)",
      },
      "tiers": [{"id": "full", "width_us": None}],
    },
    {
      "record": "signal_catalog",
      "signals": catalog,
    },
    {
      "record": "dynamics_catalog",
      "schema": "comma-companion.dynamics-row",
      "schema_version": 1,
      "sample_period_us": 10_000,
      "alignment": "timestamp_causal_recorded_history_asof",
      "controller_i_timing": "post_update_asof_source_row",
      "availability": "available",
      "columns": [],
    },
    *series,
    *(
      [
        {
          "record": "dynamics_chunk",
          "schema": "comma-companion.dynamics-row",
          "chunk": 0,
          "rows": dynamics_rows,
        }
      ]
      if dynamics_rows
      else []
    ),
    {
      "record": "frame_chunk",
      "camera": "road",
      "chunk": 0,
      "rows": [
        {
          "t_us": times[0],
          "segment_num": 0,
        }
      ],
    },
    {
      "record": "marker",
      "id": "marker-0",
      "kind": "segment_boundary",
      "start_us": times[0],
      "end_us": None,
      "severity": "info",
      "label": "Segment 0",
      "attributes": {},
    },
    manifest,
    {
      "record": "stream_end",
      "status": "complete",
      "counts": {},
    },
  ]


def _native_dynamics_rows(times: list[int]) -> list[dict[str, Any]]:
  return [
    {
      "t_us": t_us,
      "nominal_t_us": t_us,
      "source_t_us": t_us,
      "source_time_error_us": 0,
      "nominal_log_mono_time_ns": str(1_000_000_000 + t_us * 1000),
      "log_mono_time_ns": str(1_000_000_000 + t_us * 1000),
      "segment_num": 0,
      "source_ordinal": index,
      "continuous": True,
      "quality_flags": [],
      "car_state_age_us": 0,
      "car_control_age_us": 0,
      "car_output_age_us": 0,
      "controls_state_age_us": 0,
      "live_torque_age_us": None,
      "live_parameters_age_us": 0,
      "applied_torque": 0.2,
      "applied_torque_source": "carOutput.actuatorsOutput.torque",
      "controller_selection_source": ("starpilotPlan.starpilotToggles"),
      "controller_selection_stateful": True,
      "controller_selection_state_machine_version": 1,
      "controller_type": "conventional_torque",
      "resolved_toggles_sha256": "a" * 64,
      "actual_lateral_accel": 0.1,
      "steering_angle_deg": 1.0,
      "steering_rate_deg": 33.0,
      "signed_steering_rate_deg_s": 0.0,
      "steering_torque_eps": 0.05,
      "v_ego": 10.0,
      "a_ego": 0.0,
      "desired_curvature": 0.001,
      "controls_desired_curvature": 0.001,
      "desired_lateral_accel": 0.12,
      "future_feedforward_lateral_accel": 0.11,
      "future_feedforward_eligible": True,
      "future_feedforward_exact": True,
      "desired_lateral_jerk": 0.01,
      "controller_output": 0.18,
      "controller_i": 0.02,
      "lat_active": True,
      "driver_overlay": True,
      "saturated": False,
      "steer_limited_by_safety": False,
      "integrator_frozen": False,
      "integrator_freeze_exact": False,
      "live_torque_valid": False,
      "live_torque_in_use": False,
      "live_torque_used": False,
      "live_lat_accel_factor": 2.5,
      "live_lat_accel_offset": 0.0,
      "live_friction": 0.1,
      "base_lat_accel_factor": 2.5,
      "base_lat_accel_offset": 0.0,
      "base_friction": 0.1,
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
      "vehicle_lat_accel_factor_multiplier": 1.2101,
      "baseline_controller_profile_id": (HISTORICAL_CONTROLLER_PROFILE_ID),
      "baseline_controller_params_sha256": (HISTORICAL_CONTROLLER_PROFILE_SHA256),
      "baseline_controller_source_starpilot_commit": (HISTORICAL_CONTROLLER_SOURCE_COMMIT),
      "steering_angle_deadzone_deg": 0.0,
      "roll": 0.0,
      "controller_i_timing": "post_update_asof_source_row",
    }
    for index, t_us in enumerate(times)
  ]


@pytest.mark.parametrize(
  ("car_state_age_us", "source_time_error_us"),
  [
    (-1, 1),
    (0, 1),
  ],
)
def test_dynamics_chunk_rejects_future_car_state_leakage(
  car_state_age_us: int,
  source_time_error_us: int,
) -> None:
  row = _native_dynamics_rows([0])[0]
  row["car_state_age_us"] = car_state_age_us
  row["source_time_error_us"] = source_time_error_us

  with pytest.raises(IntegrationError, match="invalid_dynamics_chunk"):
    IntegrationHandlers._validate_dynamics_chunk(
      {
        "schema": "comma-companion.dynamics-row",
        "chunk": 0,
        "rows": [row],
      },
      1,
    )
  with pytest.raises(IntegrationError, match="causal_telemetry_required"):
    IntegrationHandlers._validate_causal_simulation_rows([row])


def test_causal_rows_reject_inconsistent_grid_steering_rate() -> None:
  rows = _native_dynamics_rows([0, 10_000])
  rows[1]["steering_angle_deg"] = 1.9
  rows[1]["signed_steering_rate_deg_s"] = 90.0

  IntegrationHandlers._validate_causal_simulation_rows(
    rows,
    route_origin_log_mono_time_ns="1000000000",
  )

  rows[1]["signed_steering_rate_deg_s"] = 45.0
  with pytest.raises(IntegrationError) as failure:
    IntegrationHandlers._validate_causal_simulation_rows(
      rows,
      route_origin_log_mono_time_ns="1000000000",
    )

  assert failure.value.code == "causal_telemetry_required"
  assert failure.value.details["signed_steering_rate_invalid"] is True


def test_causal_rows_bind_stateful_effective_context_to_profile() -> None:
  rows = _native_dynamics_rows([0, 10_000])
  for row in rows:
    row["effective_torque_params_source"]["factor"] = "live_filtered"
    row["effective_torque_params_source_age_us"]["factor"] = 0
    row["live_torque_used"] = False

  IntegrationHandlers._validate_causal_simulation_rows(
    rows,
    route_origin_log_mono_time_ns="1000000000",
    controller_profile=_historical_controller_profile(),
  )

  invalid_state = copy.deepcopy(rows)
  invalid_state[0]["controller_selection_state_machine_version"] = True
  with pytest.raises(
    IntegrationError,
    match="causal_telemetry_required",
  ):
    IntegrationHandlers._validate_causal_simulation_rows(
      invalid_state,
      route_origin_log_mono_time_ns="1000000000",
      controller_profile=_historical_controller_profile(),
    )

  crossed_profile = copy.deepcopy(rows)
  crossed_profile[0]["baseline_controller_profile_id"] = "starpilot-ioniq5-torque-19f8c767ec0d-v1"
  with pytest.raises(
    IntegrationError,
    match="causal_telemetry_required",
  ):
    IntegrationHandlers._validate_causal_simulation_rows(
      crossed_profile,
      route_origin_log_mono_time_ns="1000000000",
      controller_profile=_historical_controller_profile(),
    )

  source_before_controls = copy.deepcopy(rows)
  source_before_controls[0]["controls_state_age_us"] = 5
  source_before_controls[0]["effective_torque_params_source_age_us"]["factor"] = 4
  with pytest.raises(
    IntegrationError,
    match="causal_telemetry_required",
  ):
    IntegrationHandlers._validate_causal_simulation_rows(
      source_before_controls,
      route_origin_log_mono_time_ns="1000000000",
      controller_profile=_historical_controller_profile(),
    )


def test_dynamics_chunk_cannot_span_a_continuity_gap() -> None:
  rows = _native_dynamics_rows([0, 20_000])
  rows[1]["continuous"] = False

  with pytest.raises(IntegrationError) as failure:
    IntegrationHandlers._validate_dynamics_chunk(
      {
        "schema": "comma-companion.dynamics-row",
        "chunk": 0,
        "rows": rows,
      },
      1,
    )

  assert failure.value.code == "invalid_dynamics_chunk"


def test_staged_rlog_is_not_a_writable_alias_of_raw_object(
  database: Database,
  archive_root: Path,
  tmp_path: Path,
) -> None:
  content = b"immutable-rlog"
  _, storage_path = _seed_artifact(
    database,
    archive_root,
    artifact_id="rlog-stage-source",
    kind="rlog",
    relative_path="rlog",
    content=content,
  )
  handlers = IntegrationHandlers(database, archive_root)
  sources = handlers._telemetry_sources(DRIVE_ID)
  input_root = tmp_path / "input"
  input_root.mkdir()

  handlers._stage_rlogs(input_root, ROUTE_NAME, sources)

  source = archive_root / storage_path
  staged = input_root / f"{ROUTE_NAME}--0" / "rlog"
  assert not source.samefile(staged)
  staged.write_bytes(b"mutated staging copy")
  assert source.read_bytes() == content


def test_telemetry_source_fingerprint_uses_selected_latest_per_segment(
  database: Database,
  archive_root: Path,
) -> None:
  old_digest, _ = _seed_artifact(
    database,
    archive_root,
    artifact_id="rlog-generation-a",
    kind="rlog",
    relative_path="rlog-old",
    content=b"superseded-rlog",
  )
  latest_digest, _ = _seed_artifact(
    database,
    archive_root,
    artifact_id="rlog-generation-b",
    kind="rlog",
    relative_path="rlog",
    content=b"latest-rlog",
  )

  sources = IntegrationHandlers(
    database,
    archive_root,
  )._telemetry_sources(DRIVE_ID)

  assert len(sources.all_rows) == 2
  assert [row["id"] for row in sources.selected] == [
    "rlog-generation-b",
  ]
  assert sources.fingerprint == telemetry_source_fingerprint(
    [
      {
        "segment_number": 0,
        "sha256": latest_digest,
      }
    ]
  )
  assert sources.fingerprint != telemetry_source_fingerprint(
    [
      {
        "segment_number": 0,
        "sha256": old_digest,
      },
      {
        "segment_number": 0,
        "sha256": latest_digest,
      },
    ]
  )


def test_stage_rlogs_rejects_oversize_source_before_copy(
  database: Database,
  archive_root: Path,
  tmp_path: Path,
) -> None:
  digest, _ = _seed_artifact(
    database,
    archive_root,
    artifact_id="oversize-rlog",
    kind="rlog",
    relative_path="rlog",
    content=b"small",
  )
  database.execute(
    "UPDATE objects SET size = ? WHERE sha256 = ?",
    (64 * 1024 * 1024 + 1, digest),
  )
  handlers = IntegrationHandlers(database, archive_root)
  sources = handlers._telemetry_sources(DRIVE_ID)
  input_root = tmp_path / "input"
  input_root.mkdir()

  with pytest.raises(
    IntegrationError,
    match="telemetry_source_too_large",
  ):
    handlers._stage_rlogs(input_root, ROUTE_NAME, sources)

  assert list(input_root.iterdir()) == []


def test_controller_provenance_accepts_versioned_historical_no_flm() -> None:
  rows = _native_dynamics_rows([index * 10_000 for index in range(301)])
  records = _telemetry_records(
    {},
    times=[row["nominal_t_us"] for row in rows],
    dynamics_rows=rows,
  )
  manifest = next(record for record in records if record["record"] == "manifest")
  old_commit = "2747bf037c0f284500457f1befb4f52415e3285a"
  manifest["provenance"]["source_starpilot_commit"] = old_commit
  controller = manifest["dynamics"]["controller_provenance"]
  controller["flm_active"] = None
  controller["flm_active_available"] = False
  controller["flm_resolution"] = {
    "state": "verified",
    "flm_active": False,
    "source": "versioned_source_commit_evaluator",
    "evaluator": {
      "name": "starpilot-flm-availability-by-source-commit",
      "version": 1,
      "source_commit": old_commit,
      "source_sha256": HISTORICAL_FLM_EVALUATOR_SHA256,
    },
  }

  result = IntegrationHandlers._controller_provenance(manifest)

  assert result["toggle_snapshot"]["flm_active"] is False
  assert result["toggle_snapshot"]["flm_active_available"] is True
  assert result["toggle_snapshot"]["flm_resolution"] == (controller["flm_resolution"])
  assert result["tuning_snapshot_complete"] is True

  controller["flm_resolution"]["evaluator"]["source_sha256"] = "0" * 64
  with pytest.raises(
    IntegrationError,
    match="controller_provenance_incomplete",
  ):
    IntegrationHandlers._controller_provenance(manifest)


def test_full_rlog_rejects_valid_shape_wrong_extractor_hash(
  tmp_path: Path,
) -> None:
  rows = _native_dynamics_rows(
    [index * 10_000 for index in range(301)],
  )
  records = _telemetry_records(
    {},
    times=[row["nominal_t_us"] for row in rows],
    dynamics_rows=rows,
  )
  manifest = next(
    record
    for record in records
    if record["record"] == "manifest"
  )
  telemetry_path = tmp_path / "telemetry.ndjson"
  telemetry_path.write_text(
    "".join(
      canonical_json(record) + "\n"
      for record in records
    ),
    encoding="utf-8",
  )

  IntegrationHandlers._require_full_rlog(
    manifest,
    telemetry_path,
  )

  wrong_extractor = copy.deepcopy(manifest)
  wrong_extractor["dynamics"]["telemetry_provenance"][
    "extractor_source_sha256"
  ] = "0" * 64
  wrong_extractor["provenance"][
    "extractor_source_sha256"
  ] = "0" * 64
  with pytest.raises(
    IntegrationError,
    match="full_rlog_required",
  ):
    IntegrationHandlers._require_full_rlog(
      wrong_extractor,
      telemetry_path,
    )


def _read_indexed_record(
  ndjson: Path,
  reference: Any,
) -> tuple[bytes, dict[str, Any]]:
  with ndjson.open("rb") as stream:
    stream.seek(reference["byte_offset"])
    encoded_record = stream.read(reference["byte_length"])
  assert encoded_record.endswith(b"\n")
  assert len(encoded_record) == reference["byte_length"]
  assert hashlib.sha256(encoded_record).hexdigest() == reference["record_sha256"]
  return encoded_record, json.loads(encoded_record)


def test_verify_artifact_hashes_cataloged_object(
  database: Database,
  archive_root: Path,
) -> None:
  digest, storage_path = _seed_artifact(
    database,
    archive_root,
    artifact_id="artifact-video",
    kind="fcamera",
    relative_path=f"realdata/{ROUTE_NAME}--0/fcamera.hevc",
    camera="road",
    content=b"raw-hevc",
  )
  handlers = IntegrationHandlers(database, archive_root)
  context = FakeContext("verify-job")

  result = handlers.verify_artifact(
    context,
    {"artifact_id": "artifact-video"},
  )

  assert result == {
    "status": "verified",
    "artifact_id": "artifact-video",
    "sha256": digest,
    "size_bytes": 8,
    "storage_path": storage_path,
  }
  assert context.progress_values[-1] == 1.0
  row = database.query_one(
    "SELECT status FROM artifacts WHERE id = 'artifact-video'",
  )
  assert row["status"] == "verified"

  (archive_root / storage_path).write_bytes(b"tampered")
  with pytest.raises(IntegrationError, match="artifact_integrity_failed"):
    handlers.verify_artifact(
      FakeContext("verify-tampered"),
      {"artifact_id": "artifact-video"},
    )


def test_agent_video_kind_and_canonical_camera_pair_is_approved(
  database: Database,
  archive_root: Path,
) -> None:
  _seed_artifact(
    database,
    archive_root,
    artifact_id="agent-road-video",
    kind="video",
    relative_path=f"realdata/{ROUTE_NAME}--0/fcamera.hevc",
    camera="road",
    content=b"raw-hevc",
  )
  handlers = IntegrationHandlers(database, archive_root)

  assert handlers._media_input_format(
    handlers._artifact("agent-road-video"),
  ) == "raw_hevc"


def test_verified_av1_prunes_only_raw_video(
  database: Database,
  archive_root: Path,
) -> None:
  source_digest, source_storage_path = _seed_artifact(
    database,
    archive_root,
    artifact_id="agent-road-video",
    kind="video",
    relative_path=f"realdata/{ROUTE_NAME}--0/fcamera.hevc",
    camera="road",
    content=b"raw-hevc-video",
  )
  log_digest, log_storage_path = _seed_artifact(
    database,
    archive_root,
    artifact_id="route-rlog",
    kind="rlog",
    relative_path=f"realdata/{ROUTE_NAME}--0/rlog.zst",
    camera=None,
    content=b"immutable-rlog",
  )
  derived_content = b"smaller-av1"
  derived_digest = hashlib.sha256(derived_content).hexdigest()
  derived_storage_path = "derived/device/route/0/road/video.webm"
  derived_path = archive_root / derived_storage_path
  derived_path.parent.mkdir(parents=True, exist_ok=True)
  derived_path.write_bytes(derived_content)
  database.execute(
    """
    INSERT INTO objects(sha256, size, storage_path, created_at)
    VALUES (?, ?, ?, ?)
    """,
    (derived_digest, len(derived_content), derived_storage_path, NOW),
  )
  source = database.query_one(
    "SELECT device_id, drive_id, segment_id FROM artifacts WHERE id = ?",
    ("agent-road-video",),
  )
  assert source is not None
  database.execute(
    """
    INSERT INTO artifacts(
      id, device_id, drive_id, segment_id, object_sha256,
      kind, camera, relative_path, storage_path, size,
      mime_type, codec, status, source_artifact_id, created_at
    ) VALUES (
      'agent-road-video-av1', ?, ?, ?, ?,
      'derived_video', 'road', ?, ?, ?,
      'video/webm', 'av1', 'ready', 'agent-road-video', ?
    )
    """,
    (
      source["device_id"],
      source["drive_id"],
      source["segment_id"],
      derived_digest,
      derived_storage_path,
      derived_storage_path,
      len(derived_content),
      NOW,
    ),
  )
  handlers = IntegrationHandlers(
    database,
    archive_root,
    retain_raw_video=False,
  )

  result = handlers._prune_raw_video_object("agent-road-video")

  assert result["status"] == "pruned"
  assert not (archive_root / source_storage_path).exists()
  assert (archive_root / derived_storage_path).read_bytes() == derived_content
  assert (archive_root / log_storage_path).read_bytes() == b"immutable-rlog"
  source_object = database.query_one(
    "SELECT storage_state, pruned_at FROM objects WHERE sha256 = ?",
    (source_digest,),
  )
  log_object = database.query_one(
    "SELECT storage_state, pruned_at FROM objects WHERE sha256 = ?",
    (log_digest,),
  )
  source_artifact = database.query_one(
    "SELECT status FROM artifacts WHERE id = 'agent-road-video'",
  )
  assert source_object is not None
  assert source_object["storage_state"] == "pruned"
  assert source_object["pruned_at"] is not None
  assert source_artifact is not None
  assert source_artifact["status"] == "raw_video_pruned"
  assert log_object is not None
  assert log_object["storage_state"] == "present"
  assert log_object["pruned_at"] is None


def test_raw_video_object_shared_with_log_is_never_pruned(
  database: Database,
  archive_root: Path,
) -> None:
  source_digest, source_storage_path = _seed_artifact(
    database,
    archive_root,
    artifact_id="shared-video",
    kind="video",
    relative_path=f"realdata/{ROUTE_NAME}--0/fcamera.hevc",
    camera="road",
    content=b"shared-content",
  )
  source = database.query_one(
    "SELECT device_id, drive_id, segment_id, size FROM artifacts WHERE id = ?",
    ("shared-video",),
  )
  assert source is not None
  database.execute(
    """
    INSERT INTO artifacts(
      id, device_id, drive_id, segment_id, object_sha256,
      kind, relative_path, storage_path, size, status, created_at
    ) VALUES (
      'shared-rlog', ?, ?, ?, ?,
      'rlog', ?, ?, ?, 'stored', ?
    )
    """,
    (
      source["device_id"],
      source["drive_id"],
      source["segment_id"],
      source_digest,
      f"realdata/{ROUTE_NAME}--0/rlog.zst",
      source_storage_path,
      source["size"],
      NOW,
    ),
  )
  handlers = IntegrationHandlers(
    database,
    archive_root,
    retain_raw_video=False,
  )

  result = handlers._prune_raw_video_object("shared-video")

  assert result == {"status": "retained", "reason": "shared_or_unverified"}
  assert (archive_root / source_storage_path).read_bytes() == b"shared-content"


def test_transcode_rejects_unapproved_source_before_subprocess(
  database: Database,
  archive_root: Path,
) -> None:
  _seed_artifact(
    database,
    archive_root,
    artifact_id="bad-video",
    kind="fcamera",
    relative_path=f"realdata/{ROUTE_NAME}--0/fcamera.ts",
    camera="road",
    content=b"bad",
  )

  def forbidden_runner(
    _command: tuple[str, ...],
    **_kwargs: Any,
  ) -> ProcessResult:
    raise AssertionError("subprocess must not run")

  handlers = IntegrationHandlers(
    database,
    archive_root,
    process_runner=forbidden_runner,
  )
  with pytest.raises(IntegrationError, match="unsupported_media_source"):
    handlers.transcode_video(
      FakeContext("bad-format"),
      {"artifact_id": "bad-video"},
    )

  _seed_artifact(
    database,
    archive_root,
    artifact_id="large-video",
    kind="fcamera",
    relative_path=f"realdata/{ROUTE_NAME}--0/fcamera.hevc",
    camera="road",
    content=b"four",
  )
  capped_handlers = IntegrationHandlers(
    database,
    archive_root,
    process_runner=forbidden_runner,
    max_media_source_bytes=3,
  )
  with pytest.raises(IntegrationError, match="media_source_too_large"):
    capped_handlers.transcode_video(
      FakeContext("too-large"),
      {"artifact_id": "large-video"},
    )


def test_media_generation_paths_bind_source_profile_and_policy(
  database: Database,
  archive_root: Path,
) -> None:
  _seed_artifact(
    database,
    archive_root,
    artifact_id="generation-video",
    kind="fcamera",
    relative_path=f"realdata/{ROUTE_NAME}--0/fcamera.hevc",
    camera="road",
    content=b"generation-source",
  )
  handlers = IntegrationHandlers(database, archive_root)
  source = handlers._artifact("generation-video")
  encode_profile = handlers._media_encode_profile()

  fingerprint = handlers._media_generation_fingerprint(
    source,
    encode_profile,
  )
  reordered_profile = dict(reversed(list(encode_profile.items())))
  assert (
    handlers._media_generation_fingerprint(source, reordered_profile)
    == fingerprint
  )
  expected_contract = {
    "schema": "comma-companion.media-generation",
    "schema_version": MEDIA_GENERATION_SCHEMA_VERSION,
    "media_job_schema_version": MEDIA_SCHEMA_VERSION,
    "source": {
      "object_sha256": source["object_sha256"],
      "input_format": "raw_hevc",
      "kind": "fcamera",
      "camera": "road",
    },
    "encode": encode_profile,
    "bitrate_policy": MEDIA_BITRATE_POLICY_PROFILE,
  }
  assert fingerprint == hashlib.sha256(
    canonical_json(expected_contract).encode("utf-8"),
  ).hexdigest()

  changed_profile = {
    **encode_profile,
    "crf": encode_profile["crf"] + 1,
  }
  assert (
    handlers._media_generation_fingerprint(source, changed_profile)
    != fingerprint
  )
  changed_source = {
    **dict(source),
    "object_sha256": "f" * 64,
  }
  assert (
    handlers._media_generation_fingerprint(
      changed_source,
      encode_profile,
    )
    != fingerprint
  )
  paths = handlers._media_paths(source, fingerprint)
  assert paths[0].parent.name == fingerprint
  assert paths[-1] == (
    f"derived/{DEVICE_ID}/{ROUTE_NAME}/0/{fingerprint}/road.av1.webm"
  )


def test_catalog_canonicalizes_duplicate_derived_bytes(
  database: Database,
  archive_root: Path,
) -> None:
  _seed_artifact(
    database,
    archive_root,
    artifact_id="catalog-source",
    kind="fcamera",
    relative_path=f"realdata/{ROUTE_NAME}--0/fcamera.hevc",
    camera="road",
    content=b"catalog-source",
  )
  video_content = b"identical-derived-video"
  _, uploaded_video_path = _seed_artifact(
    database,
    archive_root,
    artifact_id="matching-uploaded-object",
    kind="rlog",
    relative_path=f"realdata/{ROUTE_NAME}--0/rlog.duplicate-video",
    content=video_content,
  )
  handlers = IntegrationHandlers(database, archive_root)
  source = handlers._artifact("catalog-source")

  def install(relative_path: str, content: bytes) -> tuple[str, int]:
    path = archive_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest(), len(content)

  def bundle(generation: str) -> tuple[str, dict[str, Any]]:
    base = f"derived/{DEVICE_ID}/{ROUTE_NAME}/0/{generation}"
    video_path = f"{base}/road.av1.webm"
    frame_path = f"{base}/road.av1.frames.json"
    poster_path = f"{base}/road.av1.poster.jpg"
    thumbnail_path = f"{base}/road.av1.thumbnails/000.jpg"
    video_sha256, video_size = install(video_path, video_content)
    frame_sha256, frame_size = install(
      frame_path,
      b'{"frame_count":1}\n',
    )
    poster_sha256, poster_size = install(poster_path, b"same-poster")
    thumbnail_sha256, thumbnail_size = install(
      thumbnail_path,
      b"same-thumbnail",
    )
    return video_path, {
      "sha256": video_sha256,
      "size_bytes": video_size,
      "codec": "av1",
      "mime_type": "video/webm",
      "duration_us": 50_000,
      "width": 1928,
      "height": 1208,
      "fps": 20.0,
      "frame_count": 1,
      "pixel_format": "yuv420p",
      "audio_codec": None,
      "metadata": {},
      "validation": {},
      "encoder": {},
      "frame_index": {
        "sha256": frame_sha256,
        "size_bytes": frame_size,
        "storage_path": frame_path,
        "mime_type": "application/json",
        "frame_count": 1,
        "schema_version": 1,
        "time_base": {
          "numerator": 1,
          "denominator": 1000,
          "text": "1/1000",
        },
        "document": {},
      },
      "poster": {
        "sha256": poster_sha256,
        "size_bytes": poster_size,
        "storage_path": poster_path,
        "timestamp_seconds": 0.0,
        "role": "poster",
        "ordinal": None,
      },
      "thumbnails": [
        {
          "sha256": thumbnail_sha256,
          "size_bytes": thumbnail_size,
          "storage_path": thumbnail_path,
          "timestamp_seconds": 0.025,
          "role": "thumbnail",
          "ordinal": 0,
        },
      ],
    }

  first_path, first_output = bundle("generation-a")
  first = handlers._catalog_derived_video(
    source,
    first_path,
    first_output,
  )
  second_path, second_output = bundle("generation-b")
  second = handlers._catalog_derived_video(
    source,
    second_path,
    second_output,
  )
  repeated = handlers._catalog_derived_video(
    source,
    second_path,
    second_output,
  )

  assert first[:4] != second[:4]
  assert repeated[:4] == second[:4]
  assert first[4]["video"] == uploaded_video_path
  assert second[4]["video"] == uploaded_video_path
  all_ids = [
    first[0],
    first[1],
    first[2],
    *first[3],
    second[0],
    second[1],
    second[2],
    *second[3],
  ]
  placeholders = ",".join("?" for _ in all_ids)
  rows = database.query_all(
    f"""
    SELECT
      a.id,
      a.kind,
      a.relative_path,
      a.storage_path,
      o.storage_path AS object_storage_path
    FROM artifacts a
    JOIN objects o ON o.sha256 = a.object_sha256
    WHERE a.id IN ({placeholders})
    """,
    tuple(all_ids),
  )
  assert len(rows) == 8
  assert all(
    row["storage_path"] == row["object_storage_path"]
    for row in rows
  )
  second_rows = {
    row["kind"]: row
    for row in rows
    if row["id"] in {second[0], second[1], second[2], *second[3]}
  }
  assert all(
    "/generation-b/" in row["relative_path"]
    for row in second_rows.values()
  )
  assert all(
    "/generation-b/" not in row["storage_path"]
    for row in second_rows.values()
  )
  output_digests = {
    second_output["sha256"],
    second_output["frame_index"]["sha256"],
    second_output["poster"]["sha256"],
    second_output["thumbnails"][0]["sha256"],
  }
  digest_placeholders = ",".join("?" for _ in output_digests)
  assert database.query_one(
    f"""
    SELECT COUNT(*) AS count
    FROM objects
    WHERE sha256 IN ({digest_placeholders})
    """,
    tuple(output_digests),
  )["count"] == 4


def test_transcode_catalogs_only_validated_worker_output(
  database: Database,
  archive_root: Path,
) -> None:
  source_content = b"raw-hevc-source" * 8_192
  _seed_artifact(
    database,
    archive_root,
    artifact_id="artifact-video",
    kind="fcamera",
    relative_path=f"realdata/{ROUTE_NAME}--0/fcamera.hevc",
    camera="road",
    content=source_content,
  )
  database.execute(
    """
    INSERT INTO runtime_settings(key, value_json, updated_at)
    VALUES ('transcode_crf', '27', ?)
    """,
    (NOW,),
  )
  codec = {"name": "h264"}
  corrupt_completion = {"enabled": False}
  corrupt_bitrate_completion = {"enabled": False}
  repeat_thumbnail_timestamp = {"enabled": False}
  tamper_artwork = {"enabled": False}
  output_content = {"value": b"webm"}
  bitrate_attempt = {"value": 1}
  bitrate_policy_transform: dict[str, Any] = {"value": None}
  media_result_transform: dict[str, Any] = {"value": None}
  source_contents = {"artifact-video": source_content}

  def replace_policy_field(
    policy: dict[str, Any],
    field: str,
    value: Any,
  ) -> dict[str, Any]:
    return {**policy, field: value}

  def replace_budget_field(
    policy: dict[str, Any],
    field: str,
    value: Any,
  ) -> dict[str, Any]:
    return {
      **policy,
      "selected_budget": {
        **policy["selected_budget"],
        field: value,
      },
    }

  def replace_target_ratio(
    policy: dict[str, Any],
    numerator: int,
    denominator: int,
  ) -> dict[str, Any]:
    return replace_budget_field(
      policy,
      "target_ratio",
      {
        "numerator": numerator,
        "denominator": denominator,
        "decimal": numerator / denominator,
      },
    )

  def replace_ratio_field(
    policy: dict[str, Any],
    ratio_name: str,
    field: str,
    value: Any,
  ) -> dict[str, Any]:
    return {
      **policy,
      ratio_name: {
        **policy[ratio_name],
        field: value,
      },
    }

  def replace_input_field(
    result: dict[str, Any],
    field: str,
    value: Any,
  ) -> dict[str, Any]:
    return {
      **result,
      "input": {
        **result["input"],
        field: value,
      },
    }

  def replace_input_probe_field(
    result: dict[str, Any],
    field: str,
    value: Any,
  ) -> dict[str, Any]:
    return {
      **result,
      "input": {
        **result["input"],
        "probe": {
          **result["input"]["probe"],
          field: value,
        },
      },
    }

  def replace_output_probe_field(
    result: dict[str, Any],
    field: str,
    value: Any,
  ) -> dict[str, Any]:
    return {
      **result,
      "output": {
        **result["output"],
        "video": {
          **result["output"]["video"],
          "probe": {
            **result["output"]["video"]["probe"],
            field: value,
          },
        },
      },
    }

  def replace_encode_field(
    result: dict[str, Any],
    field: str,
    value: Any,
  ) -> dict[str, Any]:
    return {
      **result,
      "encode": {
        **result["encode"],
        field: value,
      },
    }

  def media_runner(
    command: tuple[str, ...],
    **kwargs: Any,
  ) -> ProcessResult:
    assert command[-2:] == ("encode", "-")
    job = json.loads(kwargs["input_text"])
    assert job["encode"]["crf"] == 27
    assert job["input"]["input_format"] in {"raw_hevc", "mpegts"}
    source_content_for_job = source_contents[job["input"]["artifact_id"]]
    is_mpegts = job["input"]["input_format"] == "mpegts"
    output_path = Path(job["outputs"]["video_path"])
    metadata_path = Path(job["outputs"]["metadata_path"])
    poster_path = Path(job["outputs"]["poster_path"])
    thumbnails_dir = Path(job["outputs"]["thumbnails_dir"])
    frame_index_path = Path(job["outputs"]["frame_index_path"])
    encoded_content = output_content["value"]
    output_path.write_bytes(encoded_content)
    output_digest = hashlib.sha256(encoded_content).hexdigest()
    poster_content = b"poster-jpeg"
    poster_path.write_bytes(b"tampered-poster" if tamper_artwork["enabled"] else poster_content)
    poster_digest = hashlib.sha256(poster_content).hexdigest()
    thumbnails_dir.mkdir(exist_ok=True)
    thumbnail_records = []
    for index in range(job["encode"]["thumbnail_count"]):
      thumbnail_path = thumbnails_dir / f"{index:03d}.jpg"
      thumbnail_content = f"thumbnail-{index}".encode()
      thumbnail_path.write_bytes(thumbnail_content)
      thumbnail_records.append(
        {
          "path": str(thumbnail_path),
          "mime_type": "image/jpeg",
          "sha256": hashlib.sha256(
            thumbnail_content,
          ).hexdigest(),
          "size_bytes": len(thumbnail_content),
          "timestamp_seconds": round(
            2.5 * (index + 0.5) / job["encode"]["thumbnail_count"],
            6,
          ),
        }
      )
    if repeat_thumbnail_timestamp["enabled"]:
      thumbnail_records[-1]["timestamp_seconds"] = thumbnail_records[-2]["timestamp_seconds"]
    time_base = {
      "numerator": 1,
      "denominator": 1000,
      "text": "1/1000",
    }
    frame_document = {
      "schema_version": 1,
      "mapping_type": "encoded_frame_pts",
      "join_key": ["camera", "segment_num", "segment_frame_id"],
      "ordinal_basis": 0,
      "source_frame_key": "segment_frame_id",
      "camera": job["input"]["camera"],
      "segment_num": 0,
      "source_artifact_id": job["input"]["artifact_id"],
      "video": {
        "path": str(output_path),
        "sha256": output_digest,
      },
      "time_base": time_base,
      "frame_count": 2,
      "first_pts": 0,
      "last_end_pts": 100,
      "duration_inference_count": 0,
      "frames": [
        {
          "ordinal": 0,
          "segment_frame_id": 0,
          "pts": 0,
          "duration": 50,
          "pts_us": 0,
          "duration_us": 50_000,
          "keyframe": True,
        },
        {
          "ordinal": 1,
          "segment_frame_id": 1,
          "pts": 50,
          "duration": 50,
          "pts_us": 50_000,
          "duration_us": 50_000,
          "keyframe": False,
        },
      ],
    }
    frame_index_path.write_text(
      canonical_json(frame_document),
      encoding="utf-8",
    )
    frame_digest = hashlib.sha256(
      canonical_json(frame_document).encode(),
    ).hexdigest()
    attempt = bitrate_attempt["value"]
    target_numerator, target_denominator = (4, 5) if attempt == 1 else (3, 5)
    input_duration_us = 2_500_000
    input_total_bitrate_bps = (len(source_content_for_job) * 8 * 1_000_000 + input_duration_us - 1) // input_duration_us
    target_total_bitrate_bps = (input_total_bitrate_bps * target_numerator) // target_denominator
    reserved_container_bitrate_bps = max(
      8_000,
      (target_total_bitrate_bps + 19) // 20,
    )
    reserved_audio_bitrate_bps = 40_000 if is_mpegts else 0
    video_maxrate_bps = target_total_bitrate_bps - reserved_audio_bitrate_bps - reserved_container_bitrate_bps
    output_duration_us = 2_500_000
    output_total_bitrate_bps = (len(encoded_content) * 8 * 1_000_000 + output_duration_us - 1) // output_duration_us
    ratio_divisor = math.gcd(
      output_total_bitrate_bps,
      input_total_bitrate_bps,
    )
    bitrate_policy: Any = {
      "policy_version": 1,
      "policy": "strictly_lower_total_average_bitrate",
      "initial_target_ratio": {
        "numerator": 4,
        "denominator": 5,
        "decimal": 0.8,
      },
      "fallback_target_ratio": {
        "numerator": 3,
        "denominator": 5,
        "decimal": 0.6,
      },
      "selected_budget": {
        "attempt": attempt,
        "target_ratio": {
          "numerator": target_numerator,
          "denominator": target_denominator,
          "decimal": target_numerator / target_denominator,
        },
        "input_duration_us": input_duration_us,
        "input_total_bitrate_bps": input_total_bitrate_bps,
        "target_total_bitrate_bps": target_total_bitrate_bps,
        "reserved_audio_bitrate_bps": reserved_audio_bitrate_bps,
        "reserved_container_bitrate_bps": (reserved_container_bitrate_bps),
        "video_maxrate_bps": video_maxrate_bps,
      },
      "output_duration_us": output_duration_us,
      "output_total_bitrate_bps": output_total_bitrate_bps,
      "output_to_input_ratio": {
        "numerator": output_total_bitrate_bps // ratio_divisor,
        "denominator": input_total_bitrate_bps // ratio_divisor,
        "decimal": (output_total_bitrate_bps / input_total_bitrate_bps),
      },
      "bitrate_reduced": True,
      "size_reduced": True,
      "accepted": True,
    }
    transform = bitrate_policy_transform["value"]
    if transform is not None:
      bitrate_policy = transform(bitrate_policy)
    result = {
      "schema_version": 1,
      "job_id": job["job_id"],
      "status": "complete",
      "raw_retained": True,
      "bitrate_policy": bitrate_policy,
      "input": {
        "artifact_id": job["input"]["artifact_id"],
        "camera": job["input"]["camera"],
        "kind": job["input"]["kind"],
        "input_format": job["input"]["input_format"],
        "segment_num": job["input"]["segment_num"],
        "sha256": job["input"]["expected_sha256"],
        "size_bytes": len(source_content_for_job),
        "raw_hevc_frame_rate_applied": None if is_mpegts else 20,
        "probe": {
          "codec_name": "hevc",
          "duration_seconds": 2.5 if is_mpegts else None,
          "frame_count": 50,
          "size_bytes": len(source_content_for_job),
          "raw_elementary_stream": not is_mpegts,
          "audio_codec_name": "aac" if is_mpegts else None,
        },
      },
      "output": {
        "video": {
          "path": str(output_path),
          "mime_type": "video/webm",
          "sha256": output_digest,
          "size_bytes": len(encoded_content),
          "decoded": True,
          "cues_front_loaded": True,
          "probe": {
            "codec_name": codec["name"],
            "duration_seconds": 2.5,
            "width": 1928,
            "height": 1208,
            "average_frame_rate": 20.0,
            "frame_count": 2,
            "size_bytes": len(encoded_content),
            "pixel_format": "yuv420p",
            "audio_codec_name": None,
          },
        },
        "poster": {
          "path": str(poster_path),
          "mime_type": "image/jpeg",
          "sha256": poster_digest,
          "size_bytes": len(poster_content),
          "timestamp_seconds": 0.25,
        },
        "thumbnails": thumbnail_records,
        "frame_index": {
          "path": str(frame_index_path),
          "mime_type": "application/json",
          "sha256": frame_digest,
          "size_bytes": len(canonical_json(frame_document).encode()),
          "schema_version": 1,
          "frame_count": 2,
          "time_base": time_base,
          "join_key": ["camera", "segment_num", "segment_frame_id"],
          "ordinal_basis": 0,
          "source_frame_key": "segment_frame_id",
          "camera": job["input"]["camera"],
          "segment_num": 0,
          "source_artifact_id": job["input"]["artifact_id"],
        },
      },
      "encode": job["encode"],
      "tools": {"ffmpeg": "fixture", "ffprobe": "fixture"},
    }
    result_transform = media_result_transform["value"]
    if result_transform is not None:
      result = result_transform(result)
    completion_result = json.loads(canonical_json(result))
    if corrupt_completion["enabled"]:
      completion_result["output"]["poster"] = []
    if corrupt_bitrate_completion["enabled"]:
      completion_result["bitrate_policy"]["accepted"] = False
    metadata_path.write_text(
      canonical_json(completion_result),
      encoding="utf-8",
    )
    return ProcessResult(
      0,
      canonical_json(
        {
          "event": "result",
          "job_id": job["job_id"],
          "result": result,
        }
      )
      + "\n",
      "",
    )

  handlers = IntegrationHandlers(
    database,
    archive_root,
    process_runner=media_runner,
  )
  with pytest.raises(IntegrationError, match="media_output_invalid"):
    handlers.transcode_video(
      FakeContext("media-job"),
      {"artifact_id": "artifact-video"},
    )
  assert (
    database.query_one(
      "SELECT id FROM artifacts WHERE kind = 'derived_video'",
    )
    is None
  )

  codec["name"] = "av1"
  invalid_bitrate_policies = [
    lambda _policy: [],
    lambda policy: replace_policy_field(
      policy,
      "policy_version",
      True,
    ),
    lambda policy: replace_policy_field(
      policy,
      "accepted",
      False,
    ),
    lambda policy: replace_policy_field(
      policy,
      "bitrate_reduced",
      False,
    ),
    lambda policy: replace_policy_field(
      policy,
      "size_reduced",
      False,
    ),
    lambda policy: replace_policy_field(
      policy,
      "policy",
      "worker_claim_only",
    ),
    lambda policy: {
      **policy,
      "invented": True,
    },
    lambda policy: replace_ratio_field(
      policy,
      "initial_target_ratio",
      "decimal",
      0.81,
    ),
    lambda policy: replace_ratio_field(
      policy,
      "fallback_target_ratio",
      "numerator",
      4,
    ),
    lambda policy: replace_budget_field(
      policy,
      "attempt",
      True,
    ),
    lambda policy: replace_budget_field(
      policy,
      "input_total_bitrate_bps",
      True,
    ),
    lambda policy: replace_policy_field(
      policy,
      "output_total_bitrate_bps",
      0,
    ),
    lambda policy: replace_policy_field(
      policy,
      "output_total_bitrate_bps",
      1_000,
    ),
    lambda policy: replace_budget_field(
      policy,
      "input_duration_us",
      0,
    ),
    lambda policy: replace_budget_field(
      policy,
      "input_duration_us",
      policy["selected_budget"]["input_duration_us"] + 1,
    ),
    lambda policy: replace_budget_field(
      policy,
      "input_total_bitrate_bps",
      policy["selected_budget"]["input_total_bitrate_bps"] + 1,
    ),
    lambda policy: replace_budget_field(
      policy,
      "target_total_bitrate_bps",
      policy["selected_budget"]["target_total_bitrate_bps"] + 1,
    ),
    lambda policy: replace_budget_field(
      policy,
      "reserved_audio_bitrate_bps",
      1,
    ),
    lambda policy: replace_budget_field(
      policy,
      "reserved_container_bitrate_bps",
      policy["selected_budget"]["reserved_container_bitrate_bps"] + 1,
    ),
    lambda policy: replace_budget_field(
      policy,
      "video_maxrate_bps",
      policy["selected_budget"]["video_maxrate_bps"] + 1,
    ),
    lambda policy: {
      **policy,
      "selected_budget": {
        **policy["selected_budget"],
        "invented": 1,
      },
    },
    lambda policy: replace_policy_field(
      policy,
      "output_duration_us",
      True,
    ),
    lambda policy: replace_policy_field(
      policy,
      "output_duration_us",
      policy["output_duration_us"] + 1,
    ),
    lambda policy: replace_target_ratio(
      policy,
      3,
      5,
    ),
    lambda policy: replace_budget_field(
      policy,
      "attempt",
      2,
    ),
    lambda policy: replace_policy_field(
      policy,
      "output_to_input_ratio",
      {
        "numerator": 2,
        "denominator": 3,
        "decimal": 2 / 3,
      },
    ),
  ]
  for transform in invalid_bitrate_policies:
    bitrate_policy_transform["value"] = transform
    with pytest.raises(
      IntegrationError,
      match="media_bitrate_policy_invalid",
    ):
      handlers.transcode_video(
        FakeContext("media-job"),
        {"artifact_id": "artifact-video"},
      )
  bitrate_policy_transform["value"] = None
  invalid_media_results = [
    lambda result: replace_input_field(
      result,
      "size_bytes",
      result["input"]["size_bytes"] + 1,
    ),
    lambda result: replace_input_probe_field(
      result,
      "size_bytes",
      result["input"]["probe"]["size_bytes"] + 1,
    ),
    lambda result: replace_input_probe_field(
      result,
      "frame_count",
      result["input"]["probe"]["frame_count"] - 1,
    ),
    lambda result: replace_input_field(
      result,
      "raw_hevc_frame_rate_applied",
      21,
    ),
    lambda result: replace_encode_field(
      result,
      "audio_bitrate_kbps",
      31,
    ),
    lambda result: replace_output_probe_field(
      result,
      "duration_seconds",
      2.4,
    ),
    lambda result: replace_output_probe_field(
      result,
      "size_bytes",
      result["output"]["video"]["probe"]["size_bytes"] + 1,
    ),
  ]
  for transform in invalid_media_results:
    media_result_transform["value"] = transform
    with pytest.raises(
      IntegrationError,
      match="media_bitrate_policy_invalid",
    ):
      handlers.transcode_video(
        FakeContext("media-job"),
        {"artifact_id": "artifact-video"},
      )
  media_result_transform["value"] = None
  output_content["value"] = source_content
  with pytest.raises(
    IntegrationError,
    match="media_bitrate_policy_invalid",
  ):
    handlers.transcode_video(
      FakeContext("media-job"),
      {"artifact_id": "artifact-video"},
    )
  output_content["value"] = b"webm"
  assert (
    database.query_one(
      "SELECT id FROM artifacts WHERE kind = 'derived_video'",
    )
    is None
  )

  corrupt_bitrate_completion["enabled"] = True
  with pytest.raises(
    IntegrationError,
    match="media_completion_marker_invalid",
  ):
    handlers.transcode_video(
      FakeContext("media-job"),
      {"artifact_id": "artifact-video"},
    )
  corrupt_bitrate_completion["enabled"] = False

  corrupt_completion["enabled"] = True
  with pytest.raises(
    IntegrationError,
    match="media_completion_marker_invalid",
  ):
    handlers.transcode_video(
      FakeContext("media-job"),
      {"artifact_id": "artifact-video"},
    )
  assert (
    database.query_one(
      "SELECT id FROM artifacts WHERE kind = 'derived_video'",
    )
    is None
  )

  corrupt_completion["enabled"] = False
  tamper_artwork["enabled"] = True
  with pytest.raises(
    IntegrationError,
    match="media_artwork_integrity_failed",
  ):
    handlers.transcode_video(
      FakeContext("media-job"),
      {"artifact_id": "artifact-video"},
    )
  assert (
    database.query_one(
      "SELECT id FROM artifacts WHERE kind = 'derived_video'",
    )
    is None
  )

  tamper_artwork["enabled"] = False
  repeat_thumbnail_timestamp["enabled"] = True
  bitrate_attempt["value"] = 2
  result = handlers.transcode_video(
    FakeContext("media-job"),
    {"artifact_id": "artifact-video"},
  )
  assert result["worker"]["bitrate_policy"]["selected_budget"]["target_ratio"] == {
    "numerator": 3,
    "denominator": 5,
    "decimal": 0.6,
  }
  derived = database.query_one(
    "SELECT * FROM artifacts WHERE id = ?",
    (result["artifact_id"],),
  )
  assert derived["kind"] == "derived_video"
  assert derived["status"] == "ready"
  assert derived["source_artifact_id"] == "artifact-video"
  assert derived["codec"] == "av1"
  assert derived["mime_type"] == "video/webm"
  assert derived["duration_us"] == 2_500_000
  assert derived["width"] == 1928
  assert derived["height"] == 1208
  assert derived["fps"] == 20.0
  assert derived["frame_count"] == 2
  assert derived["time_map_path"] is None
  assert result["media_sync"]["status"] == "not_synchronized"
  frame_index = database.query_one(
    "SELECT * FROM artifacts WHERE id = ?",
    (result["frame_index_artifact_id"],),
  )
  assert frame_index["kind"] == "video_frame_index"
  assert frame_index["source_artifact_id"] == derived["id"]
  assert frame_index["status"] == "ready"
  poster = database.query_one(
    "SELECT * FROM artifacts WHERE id = ?",
    (result["poster_artifact_id"],),
  )
  assert poster["kind"] == "poster"
  assert poster["source_artifact_id"] == derived["id"]
  assert poster["status"] == "ready"
  assert poster["mime_type"] == "image/jpeg"
  assert poster["camera"] == "road"
  assert poster["storage_path"] == result["poster_path"]
  thumbnails = database.query_all(
    """
    SELECT *
    FROM artifacts
    WHERE source_artifact_id = ?
      AND kind = 'thumbnail'
      AND status = 'ready'
    ORDER BY storage_path
    """,
    (derived["id"],),
  )
  assert len(thumbnails) == 6
  assert {row["id"] for row in thumbnails} == set(result["thumbnail_artifact_ids"])
  assert all(row["mime_type"] == "image/jpeg" and row["camera"] == "road" for row in thumbnails)
  image_objects = database.query_all(
    """
    SELECT sha256, size
    FROM objects
    WHERE sha256 IN (
      SELECT object_sha256
      FROM artifacts
      WHERE source_artifact_id = ?
        AND kind IN ('poster', 'thumbnail')
        AND status = 'ready'
    )
    """,
    (derived["id"],),
  )
  assert len(image_objects) == 7

  bitrate_attempt["value"] = 1
  repeated = handlers.transcode_video(
    FakeContext("media-job"),
    {"artifact_id": "artifact-video"},
  )
  assert repeated["worker"]["bitrate_policy"]["selected_budget"]["target_ratio"] == {
    "numerator": 4,
    "denominator": 5,
    "decimal": 0.8,
  }
  assert repeated["artifact_id"] == result["artifact_id"]
  assert repeated["frame_index_artifact_id"] == result["frame_index_artifact_id"]
  assert repeated["poster_artifact_id"] == result["poster_artifact_id"]
  assert repeated["thumbnail_artifact_ids"] == result["thumbnail_artifact_ids"]
  assert (
    database.query_one(
      """
    SELECT COUNT(*) AS count
    FROM artifacts
    WHERE source_artifact_id = ?
      AND kind IN ('poster', 'thumbnail')
      AND status = 'ready'
    """,
      (derived["id"],),
    )["count"]
    == 7
  )

  rlog_digest, _ = _seed_artifact(
    database,
    archive_root,
    artifact_id="artifact-rlog",
    kind="rlog",
    relative_path=f"realdata/{ROUTE_NAME}--0/rlog",
    content=b"rlog",
  )
  telemetry_records = _telemetry_records(
    {},
    times=[0, 50_000],
  )
  telemetry_frame_chunk = next(record for record in telemetry_records if record["record"] == "frame_chunk")
  telemetry_frame_chunk["rows"] = [
    {
      "t_us": 0,
      "segment_num": 0,
      "segment_frame_id": 0,
    },
    {
      "t_us": 50_000,
      "segment_num": 0,
      "segment_frame_id": 1,
    },
  ]

  def rlog_runner(
    command: tuple[str, ...],
    **_kwargs: Any,
  ) -> ProcessResult:
    output_path = Path(command[command.index("--output") + 1])
    output_path.write_text(
      "".join(canonical_json(record) + "\n" for record in telemetry_records),
      encoding="utf-8",
    )
    return ProcessResult(0, "", "")

  telemetry_handlers = IntegrationHandlers(
    database,
    archive_root,
    process_runner=rlog_runner,
  )
  source_fingerprint = telemetry_source_fingerprint(
    [
      {
        "segment_number": 0,
        "sha256": rlog_digest,
      }
    ]
  )
  _seed_complete_inventory(database, source_fingerprint)
  extraction = telemetry_handlers.extract_telemetry(
    FakeContext("telemetry-sync-job"),
    {
      "drive_id": DRIVE_ID,
      "route_name": ROUTE_NAME,
      "source_fingerprint": source_fingerprint,
    },
  )
  assert extraction["media_sync"][0]["status"] == "ready"
  synchronized = database.query_one(
    "SELECT * FROM artifacts WHERE id = ?",
    (extraction["media_sync"][0]["artifact_id"],),
  )
  assert synchronized["kind"] == "video_telemetry_sync"
  derived = database.query_one(
    "SELECT * FROM artifacts WHERE id = ?",
    (result["artifact_id"],),
  )
  assert derived["time_map_path"] == synchronized["storage_path"]
  sync_document = json.loads(
    (archive_root / synchronized["storage_path"]).read_text(
      encoding="utf-8",
    ),
  )
  assert sync_document["sources"]["video_sha256"] == result["sha256"]
  assert sync_document["sources"]["telemetry_ndjson_sha256"] == extraction["ndjson_sha256"]
  assert sync_document["frames"] == [
    {
      "ordinal": 0,
      "segment_frame_id": 0,
      "pts_us": 0,
      "duration_us": 50_000,
      "drive_t_us": 0,
      "keyframe": True,
    },
    {
      "ordinal": 1,
      "segment_frame_id": 1,
      "pts_us": 50_000,
      "duration_us": 50_000,
      "drive_t_us": 50_000,
      "keyframe": False,
    },
  ]

  mpegts_content = b"mpegts-source-with-audio" * 8_192
  source_contents["artifact-video-mpegts"] = mpegts_content
  _seed_artifact(
    database,
    archive_root,
    artifact_id="artifact-video-mpegts",
    kind="qcamera",
    relative_path=f"realdata/{ROUTE_NAME}--0/qcamera.ts",
    camera="qcamera",
    content=mpegts_content,
  )
  bitrate_policy_transform["value"] = lambda policy: replace_budget_field(
    policy,
    "reserved_audio_bitrate_bps",
    0,
  )
  with pytest.raises(
    IntegrationError,
    match="media_bitrate_policy_invalid",
  ):
    handlers.transcode_video(
      FakeContext("media-mpegts-job"),
      {"artifact_id": "artifact-video-mpegts"},
    )
  bitrate_policy_transform["value"] = None
  mpegts_result = handlers.transcode_video(
    FakeContext("media-mpegts-job"),
    {"artifact_id": "artifact-video-mpegts"},
  )
  mpegts_budget = mpegts_result["worker"]["bitrate_policy"]["selected_budget"]
  assert mpegts_budget["reserved_audio_bitrate_bps"] == 40_000
  assert mpegts_budget["video_maxrate_bps"] == (mpegts_budget["target_total_bitrate_bps"] - 40_000 - mpegts_budget["reserved_container_bitrate_bps"])


def test_extract_telemetry_installs_and_indexes_canonical_stream(
  database: Database,
  archive_root: Path,
) -> None:
  digest, _ = _seed_artifact(
    database,
    archive_root,
    artifact_id="artifact-rlog",
    kind="rlog",
    relative_path=f"realdata/{ROUTE_NAME}--0/rlog.zst",
    content=b"rlog",
  )
  records = _telemetry_records(
    {"vehicle.speed": [5.0, 5.1]},
    times=[100, 900_000],
    dynamics_rows=_native_dynamics_rows([100, 10_100]),
  )

  def rlog_runner(
    command: tuple[str, ...],
    **_kwargs: Any,
  ) -> ProcessResult:
    output_path = Path(command[command.index("--output") + 1])
    output_path.write_text(
      "".join(canonical_json(record) + "\n" for record in records),
      encoding="utf-8",
    )
    return ProcessResult(0, "", "")

  handlers = IntegrationHandlers(
    database,
    archive_root,
    process_runner=rlog_runner,
  )
  source_fingerprint = telemetry_source_fingerprint(
    [
      {
        "segment_number": 0,
        "sha256": digest,
      },
    ]
  )
  _seed_complete_inventory(database, source_fingerprint)
  result = handlers.extract_telemetry(
    FakeContext("telemetry-job"),
    {
      "drive_id": DRIVE_ID,
      "route_name": ROUTE_NAME,
      "source_fingerprint": source_fingerprint,
    },
  )

  assert result["status"] == "complete"
  assert result["source_fingerprint"] == source_fingerprint
  ndjson = archive_root / result["ndjson_path"]
  assert ndjson.is_file()
  assert ndjson.parent == (archive_root / "telemetry" / DEVICE_ID / ROUTE_NAME / "v1")
  index = json.loads((ndjson.parent / "index.json").read_text(encoding="utf-8"))
  assert index["ndjson_sha256"] == result["ndjson_sha256"]
  indexed = database.query_one(
    "SELECT * FROM telemetry_indexes WHERE drive_id = ?",
    (DRIVE_ID,),
  )
  assert indexed["ndjson_path"] == result["ndjson_path"]
  assert indexed["source_fingerprint"] == source_fingerprint
  series_reference = database.query_one(
    """
    SELECT ndjson_path, byte_offset, byte_length, record_sha256
    FROM telemetry_series_chunks
    WHERE drive_id = ? AND signal_id = 'vehicle.speed'
    """,
    (DRIVE_ID,),
  )
  frame_reference = database.query_one(
    """
    SELECT ndjson_path, byte_offset, byte_length, record_sha256
    FROM telemetry_frame_chunks
    WHERE drive_id = ? AND camera = 'road'
    """,
    (DRIVE_ID,),
  )
  dynamics_reference = database.query_one(
    """
    SELECT ndjson_path, byte_offset, byte_length, record_sha256
    FROM telemetry_dynamics_chunks
    WHERE drive_id = ?
    """,
    (DRIVE_ID,),
  )
  expected_records = {
    record["record"]: (canonical_json(record) + "\n").encode()
    for record in records
    if record["record"]
    in {
      "series_chunk",
      "frame_chunk",
      "dynamics_chunk",
    }
  }
  for reference, record_type in (
    (series_reference, "series_chunk"),
    (frame_reference, "frame_chunk"),
    (dynamics_reference, "dynamics_chunk"),
  ):
    assert reference is not None
    assert reference["ndjson_path"] == result["ndjson_path"]
    encoded_record, decoded_record = _read_indexed_record(ndjson, reference)
    assert encoded_record == expected_records[record_type]
    assert decoded_record["record"] == record_type
  _, series_record = _read_indexed_record(ndjson, series_reference)
  assert series_record["v"] == [5.0, 5.1]
  segment = database.query_one(
    "SELECT start_t_us, duration_us FROM segments WHERE id = ?",
    (SEGMENT_ID,),
  )
  assert segment["start_t_us"] == 100
  assert segment["duration_us"] == 899_900
  drive = database.query_one(
    "SELECT telemetry_ready, route_state FROM drives WHERE id = ?",
    (DRIVE_ID,),
  )
  assert drive["telemetry_ready"] == 1
  assert drive["route_state"] == "complete"


def test_extract_telemetry_cannot_publish_ahead_of_latest_inventory(
  database: Database,
  archive_root: Path,
) -> None:
  old_digest, _ = _seed_artifact(
    database,
    archive_root,
    artifact_id="artifact-rlog-a",
    kind="rlog",
    relative_path=f"realdata/{ROUTE_NAME}--0/rlog",
    content=b"old-rlog",
  )
  old_fingerprint = telemetry_source_fingerprint(
    [
      {
        "segment_number": 0,
        "sha256": old_digest,
      }
    ]
  )
  _seed_complete_inventory(database, old_fingerprint)
  new_digest, _ = _seed_artifact(
    database,
    archive_root,
    artifact_id="artifact-rlog-z",
    kind="rlog",
    relative_path=f"realdata/{ROUTE_NAME}--0/rlog",
    content=b"new-rlog",
  )
  new_fingerprint = telemetry_source_fingerprint(
    [
      {
        "segment_number": 0,
        "sha256": new_digest,
      }
    ]
  )
  derived_id = _seed_ready_media_sync_inputs(
    database,
    archive_root,
  )
  records = _telemetry_records({}, times=[0, 10_000])
  frame_chunk = next(record for record in records if record["record"] == "frame_chunk")
  frame_chunk["rows"] = [
    {
      "t_us": 0,
      "segment_num": 0,
      "segment_frame_id": 0,
    },
  ]
  injected_race = False

  def rlog_runner(
    command: tuple[str, ...],
    **_kwargs: Any,
  ) -> ProcessResult:
    nonlocal injected_race
    if not injected_race:
      injected_race = True
      sync_content = b"{}\n"
      sync_sha256 = hashlib.sha256(sync_content).hexdigest()
      sync_storage = Path("derived") / DEVICE_ID / ROUTE_NAME / "0" / "race-sync.json"
      sync_path = archive_root / sync_storage
      sync_path.parent.mkdir(parents=True, exist_ok=True)
      sync_path.write_bytes(sync_content)
      with database.transaction(immediate=True) as connection:
        connection.execute(
          """
          INSERT INTO objects(
            sha256, size, storage_path, created_at
          ) VALUES (?, ?, ?, ?)
          """,
          (
            sync_sha256,
            len(sync_content),
            sync_storage.as_posix(),
            NOW,
          ),
        )
        connection.execute(
          """
          INSERT INTO artifacts(
            id, device_id, drive_id, segment_id, object_sha256,
            kind, camera, relative_path, storage_path, size,
            mime_type, status, source_artifact_id, created_at
          ) VALUES (
            'race-ready-sync', ?, ?, ?, ?,
            'video_telemetry_sync', 'road', ?, ?, ?,
            'application/json', 'ready', ?, ?
          )
          """,
          (
            DEVICE_ID,
            DRIVE_ID,
            SEGMENT_ID,
            sync_sha256,
            sync_storage.as_posix(),
            sync_storage.as_posix(),
            len(sync_content),
            derived_id,
            NOW,
          ),
        )
        connection.execute(
          "UPDATE artifacts SET time_map_path = ? WHERE id = ?",
          (sync_storage.as_posix(), derived_id),
        )
    output_path = Path(command[command.index("--output") + 1])
    output_path.write_text(
      "".join(canonical_json(record) + "\n" for record in records),
      encoding="utf-8",
    )
    return ProcessResult(0, "", "")

  result = IntegrationHandlers(
    database,
    archive_root,
    process_runner=rlog_runner,
  ).extract_telemetry(
    FakeContext("telemetry-new-generation"),
    {
      "drive_id": DRIVE_ID,
      "route_name": ROUTE_NAME,
      "source_fingerprint": new_fingerprint,
    },
  )

  state = database.query_one(
    """
    SELECT d.telemetry_ready, t.source_fingerprint,
      inventory.rlog_source_fingerprint
    FROM drives d
    JOIN telemetry_indexes t ON t.drive_id = d.id
    JOIN route_inventories inventory ON inventory.drive_id = d.id
    WHERE d.id = ?
    """,
    (DRIVE_ID,),
  )
  assert result["source_fingerprint"] == new_fingerprint
  assert state is not None
  assert state["telemetry_ready"] == 0
  assert state["source_fingerprint"] == new_fingerprint
  assert state["rlog_source_fingerprint"] == old_fingerprint
  assert result["media_sync"] == [
    {
      "status": "not_synchronized",
      "error": {
        "code": "telemetry_not_ready",
        "message": "Media synchronization requires telemetry bound to the latest immutable route inventory.",
        "details": {},
        "retryable": True,
      },
    },
  ]
  assert (
    database.query_one(
      """
    SELECT COUNT(*) AS count
    FROM artifacts
    WHERE drive_id = ?
      AND kind = 'video_telemetry_sync'
      AND status = 'ready'
    """,
      (DRIVE_ID,),
    )["count"]
    == 0
  )
  assert (
    database.query_one(
      "SELECT time_map_path FROM artifacts WHERE id = ?",
      (derived_id,),
    )["time_map_path"]
    is None
  )


def test_extract_telemetry_does_not_publish_after_new_source_arrives(
  database: Database,
  archive_root: Path,
) -> None:
  captured_digest, _ = _seed_artifact(
    database,
    archive_root,
    artifact_id="artifact-rlog-a",
    kind="rlog",
    relative_path=f"realdata/{ROUTE_NAME}--0/rlog",
    content=b"captured-rlog",
  )
  captured_fingerprint = telemetry_source_fingerprint(
    [
      {
        "segment_number": 0,
        "sha256": captured_digest,
      }
    ]
  )
  _seed_complete_inventory(database, captured_fingerprint)
  derived_id = _seed_ready_media_sync_inputs(
    database,
    archive_root,
  )
  records = _telemetry_records({}, times=[0, 10_000])
  frame_chunk = next(
    record
    for record in records
    if record["record"] == "frame_chunk"
  )
  frame_chunk["rows"] = [
    {
      "t_us": 0,
      "segment_num": 0,
      "segment_frame_id": 0,
    },
  ]
  replacement_digest: str | None = None

  def rlog_runner(
    command: tuple[str, ...],
    **_kwargs: Any,
  ) -> ProcessResult:
    nonlocal replacement_digest
    replacement_digest, _ = _seed_artifact(
      database,
      archive_root,
      artifact_id="artifact-rlog-z",
      kind="rlog",
      relative_path=f"realdata/{ROUTE_NAME}--0/rlog",
      content=b"replacement-rlog",
    )
    output_path = Path(command[command.index("--output") + 1])
    output_path.write_text(
      "".join(
        canonical_json(record) + "\n"
        for record in records
      ),
      encoding="utf-8",
    )
    return ProcessResult(0, "", "")

  result = IntegrationHandlers(
    database,
    archive_root,
    process_runner=rlog_runner,
  ).extract_telemetry(
    FakeContext("telemetry-source-race"),
    {
      "drive_id": DRIVE_ID,
      "route_name": ROUTE_NAME,
      "source_fingerprint": captured_fingerprint,
    },
  )

  assert replacement_digest is not None
  replacement_fingerprint = telemetry_source_fingerprint(
    [
      {
        "segment_number": 0,
        "sha256": replacement_digest,
      }
    ]
  )
  assert result["source_fingerprint"] == captured_fingerprint
  assert result["media_sync"] == [
    {
      "status": "not_synchronized",
      "error": {
        "code": "telemetry_not_ready",
        "message": "Media synchronization requires telemetry bound to the latest immutable route inventory.",
        "details": {},
        "retryable": True,
      },
    },
  ]
  assert result["follow_up"]["source_fingerprint"] == replacement_fingerprint
  state = database.query_one(
    """
    SELECT d.telemetry_ready, t.source_fingerprint
    FROM drives d
    JOIN telemetry_indexes t ON t.drive_id = d.id
    WHERE d.id = ?
    """,
    (DRIVE_ID,),
  )
  assert state is not None
  assert state["telemetry_ready"] == 0
  assert state["source_fingerprint"] == captured_fingerprint
  assert (
    database.query_one(
      """
      SELECT COUNT(*) AS count
      FROM artifacts
      WHERE drive_id = ?
        AND kind = 'video_telemetry_sync'
        AND status = 'ready'
      """,
      (DRIVE_ID,),
    )["count"]
    == 0
  )
  assert (
    database.query_one(
      "SELECT time_map_path FROM artifacts WHERE id = ?",
      (derived_id,),
    )["time_map_path"]
    is None
  )


def test_model_registry_keeps_legacy_artifact_unavailable(
  database: Database,
  archive_root: Path,
) -> None:
  parameter_schema = [
    {
      "name": "friction",
      "type": "number",
      "default": 0.1,
      "minimum": 0.0,
      "maximum": 1.0,
    }
  ]

  def dynamics_runner(
    _command: tuple[str, ...],
    **kwargs: Any,
  ) -> ProcessResult:
    requests = [json.loads(line) for line in kwargs["input_text"].splitlines()]
    assert [request["method"] for request in requests] == [
      "model_info",
      "parameter_schema",
    ]
    info = {
      "protocol_version": 1,
      "mode": "approximate_closed_loop",
      "target_car_fingerprint": "HYUNDAI_IONIQ_5",
      "history_steps": 300,
      "sample_period_s": 0.01,
      "model": {
        "sha256": REFERENCE_DYNAMICS_MODEL_SHA256,
        "promoted_artifact_verified": True,
        "review_registry_match": True,
        "training_alignment": "legacy_file_order_noncausal",
        "causal_training_eligible": False,
      },
      "input_contract": {
        "source_log_type": "rlog",
        "telemetry_schema": "comma-companion.dynamics-row",
        "telemetry_schema_version": 1,
        "alignment": "timestamp_causal_recorded_history_asof",
        "max_asof_age_ms": 35,
        "controller_i_timing": "post_update_asof_source_row",
      },
      "parameter_schema": parameter_schema,
      "capabilities": {
        "counterfactual_replay": True,
        "causal_replay_eligible": False,
        "apply_to_car": False,
      },
    }
    responses = [
      {
        "id": requests[0]["id"],
        "ok": True,
        "result": info,
      },
      {
        "id": requests[1]["id"],
        "ok": True,
        "result": {
          "protocol_version": 1,
          "parameter_schema": parameter_schema,
        },
      },
    ]
    return ProcessResult(
      0,
      "".join(canonical_json(response) + "\n" for response in responses),
      "",
    )

  handlers = IntegrationHandlers(
    database,
    archive_root,
    process_runner=dynamics_runner,
  )
  result = handlers.refresh_model_registry()

  assert result["status"] == "blocked_pending_causal_retrain"
  assert result["capabilities"]["available"] is False
  registered = database.query_one(
    "SELECT enabled, metadata_json FROM model_registry WHERE sha256 = ?",
    (REFERENCE_DYNAMICS_MODEL_SHA256,),
  )
  assert registered["enabled"] == 0
  metadata = json.loads(registered["metadata_json"])
  assert metadata["causal_training_eligible"] is False
  assert metadata["blocked_reason"] == "blocked_pending_causal_retrain"


def test_simulation_uses_pinned_aligned_telemetry_and_inner_replay(
  database: Database,
  archive_root: Path,
) -> None:
  digest, _ = _seed_artifact(
    database,
    archive_root,
    artifact_id="artifact-rlog",
    kind="rlog",
    relative_path=f"realdata/{ROUTE_NAME}--0/rlog",
    content=b"rlog",
  )
  times = [index * 10_000 for index in range(400)]
  native_rows = _native_dynamics_rows(times)
  records = _telemetry_records(
    {},
    times=times,
    dynamics_rows=native_rows,
  )
  captured_request: dict[str, Any] = {}
  model_hash = REFERENCE_DYNAMICS_MODEL_SHA256

  def integration_runner(
    command: tuple[str, ...],
    **kwargs: Any,
  ) -> ProcessResult:
    if "--output" in command:
      output_path = Path(command[command.index("--output") + 1])
      output_path.write_text(
        "".join(canonical_json(record) + "\n" for record in records),
        encoding="utf-8",
      )
      return ProcessResult(0, "", "")
    request = json.loads(kwargs["input_text"])
    captured_request.update(request)
    response = {
      "id": request["id"],
      "ok": True,
      "result": {
        "protocol_version": 1,
        "model": {
          "sha256": model_hash,
          "promoted_artifact_verified": True,
          "causal_training_eligible": True,
        },
        "replay": {
          "mode": "approximate_closed_loop",
          "eligible": True,
          "recorded": {"signals": {}},
          "baseline": {"signals": {}},
          "candidate": {"signals": {}},
          "comparison": {},
          "warnings": [],
        },
      },
    }
    return ProcessResult(0, canonical_json(response) + "\n", "")

  handlers = IntegrationHandlers(
    database,
    archive_root,
    process_runner=integration_runner,
  )
  source_fingerprint = telemetry_source_fingerprint(
    [
      {
        "segment_number": 0,
        "sha256": digest,
      },
    ]
  )
  _seed_complete_inventory(database, source_fingerprint)
  extraction = handlers.extract_telemetry(
    FakeContext("telemetry-job"),
    {
      "drive_id": DRIVE_ID,
      "route_name": ROUTE_NAME,
      "source_fingerprint": source_fingerprint,
    },
  )
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO model_registry(
        sha256, name, enabled, mode, parameter_schema_json,
        metadata_json, created_at
      ) VALUES (?, 'Ioniq plant', 1, 'approximate_closed_loop', '{}', ?, ?)
      """,
      (
        model_hash,
        canonical_json(
          {
            "baseline_params": {"friction": 0.9},
            "model": {
              "sha256": model_hash,
              "promoted_artifact_verified": True,
              "review_registry_match": True,
              "causal_training_eligible": True,
              "training_alignment": ("timestamp_causal_recorded_history_asof"),
              "training_schema": "comma-companion.dynamics-row",
              "training_schema_version": 1,
              "training_extraction_version": 9,
              "trainer_schema": "starpilot.neural-lateral-plant",
              "trainer_schema_version": 7,
              "training_extractor_sha256": "8" * 64,
              "trainer_sha256": "7" * 64,
              "compatible_telemetry_extractor_sha256": (
                REFERENCE_TELEMETRY_EXTRACTOR_SHA256
              ),
              "compatible_telemetry_extractor_version": "1.1.0",
              "max_asof_age_ms": 35,
              "sampling": {
                "grid": "absolute_monotonic_time",
                "absolute_grid_field": ("nominal_log_mono_time_ns"),
                "absolute_grid_phase_ns": 0,
                "sample_period_ns": 10_000_000,
                "sample_rate_hz": 100.0,
                "first_tick_formula": ("ceil(first_valid_carState_logMonoTime_ns/sample_period_ns)*sample_period_ns"),
                "alignment": ("latest_at_or_before_grid_time_zero_order_hold"),
                "source_selection": ("independent_per_source_max_valid_source_with_logMonoTime_at_or_before_tick"),
                "invalid_event_policy": {
                  "carState": ("drop_without_invalidating_prior_valid_state"),
                  "carControl": "invalidate_until_next_valid",
                  "controlsState": "invalidate_until_next_valid",
                  "carOutput": "invalidate_until_next_valid",
                },
                "signed_steering_rate": ("causal_grid_difference_of_zoh_steering_angle"),
                "applied_torque_source": ("carOutput.actuatorsOutput.torque_only_no_fallback"),
                "source_age_equation": ("source_age_ms=(nominal_log_mono_time_ns-source_log_mono_time_ns)/1e6"),
                "source_time_error_equation": ("source_time_error_ms=-source_age_ms"),
                "no_future_source": True,
                "max_asof_age_ms": 35.0,
                "required_asof_sources": [
                  "carState",
                  "carControl",
                  "controlsState",
                  "carOutput",
                ],
                "route_relative_time_formula": ("nominal_t_us=(nominal_log_mono_time_ns-route_origin_log_mono_time_ns)//1000"),
                "route_relative_phase_policy": ("constant_nonzero_modulo_allowed_exact_10000us_steps"),
                "event_order": ["logMonoTime", "source_ordinal"],
              },
            },
            "adapter": {
              "capabilities": {
                "causal_replay_eligible": True,
              },
            },
            "causal_training_eligible": True,
          }
        ),
        NOW,
      ),
    )

  result = handlers.simulate_counterfactual(
    FakeContext("simulation-job"),
    {
      "simulation_id": "simulation-one",
      "drive_id": DRIVE_ID,
      "t_us": 3_000_000,
      "horizon_us": 100_000,
      "model_hash": model_hash,
      "telemetry_sha256": extraction["ndjson_sha256"],
      "timeline_version": extraction["manifest"]["timeline_version"],
      "mode": "approximate_closed_loop",
      "baseline_params": copy.deepcopy(
        HISTORICAL_CONTROLLER_PARAMS,
      ),
      "candidate_params": {"friction": 0.2},
    },
  )

  request_rows = captured_request["params"]["rows"]
  assert len(request_rows) == 311
  assert captured_request["params"]["anchor_t_us"] == 3_000_000
  assert request_rows[0]["steering_angle_deg"] == pytest.approx(1.0)
  assert request_rows[0]["steering_rate_deg"] == pytest.approx(33.0)
  assert request_rows[0]["signed_steering_rate_deg_s"] == pytest.approx(0.0)
  assert request_rows[0]["driver_overlay"] is True
  assert captured_request["params"]["input_alignment"] == ("timestamp_causal_recorded_history_asof")
  assert captured_request["params"]["max_asof_age_ms"] == 35
  telemetry_manifest = next(record for record in records if record["record"] == "manifest")
  assert captured_request["params"]["telemetry_provenance"] == telemetry_manifest["dynamics"]["telemetry_provenance"]
  assert captured_request["params"]["controller_provenance"]["controller_type"] == "conventional_torque"
  assert captured_request["params"]["controller_provenance"]["controller_type_verified"] is True
  assert captured_request["params"]["controller_provenance"]["tuning_snapshot_complete"] is True
  assert captured_request["params"]["controller_provenance"]["toggle_snapshot"]["flm_active"] is False
  assert captured_request["params"]["controller_provenance"]["toggle_snapshot"]["trailer_load_kg"] == 0.0
  assert captured_request["params"]["baseline_params"] == HISTORICAL_CONTROLLER_PARAMS
  assert captured_request["params"]["controller_provenance"]["tuning_provenance"]["baseline_controller_profile"] == _historical_controller_profile()
  assert captured_request["params"]["controller_provenance"]["tuning_provenance"]["effective_torque_context_validation"] == (
    telemetry_manifest["dynamics"][
      "effective_torque_context_validation"
    ]
  )
  assert result["eligible"] is True
  assert "replay" not in result
  assert result["provenance"]["model_hash"] == model_hash
  assert result["provenance"]["telemetry_ndjson_sha256"] == extraction["ndjson_sha256"]
  assert result["provenance"]["timeline_version"] == extraction["manifest"]["timeline_version"]

  mismatched_baseline = copy.deepcopy(HISTORICAL_CONTROLLER_PARAMS)
  mismatched_baseline["friction_scale_mult"] = 0.8
  with pytest.raises(
    IntegrationError,
    match="controller_baseline_mismatch",
  ):
    handlers.simulate_counterfactual(
      FakeContext("mismatched-baseline"),
      {
        "simulation_id": "simulation-two",
        "drive_id": DRIVE_ID,
        "t_us": 3_000_000,
        "horizon_us": 100_000,
        "model_hash": model_hash,
        "telemetry_sha256": extraction["ndjson_sha256"],
        "timeline_version": extraction["manifest"]["timeline_version"],
        "mode": "approximate_closed_loop",
        "baseline_params": mismatched_baseline,
        "candidate_params": {"friction": 0.2},
      },
    )

  with pytest.raises(IntegrationError, match="stale_generation"):
    handlers.simulate_counterfactual(
      FakeContext("stale-timeline"),
      {
        "drive_id": DRIVE_ID,
        "t_us": 3_000_000,
        "horizon_us": 100_000,
        "model_hash": model_hash,
        "telemetry_sha256": extraction["ndjson_sha256"],
        "timeline_version": "0" * 64,
        "mode": "approximate_closed_loop",
        "candidate_params": {},
      },
    )

  with pytest.raises(IntegrationError, match="stale_generation"):
    handlers.simulate_counterfactual(
      FakeContext("stale-simulation"),
      {
        "drive_id": DRIVE_ID,
        "t_us": 3_000_000,
        "horizon_us": 100_000,
        "model_hash": model_hash,
        "telemetry_sha256": "f" * 64,
        "mode": "approximate_closed_loop",
        "parameters": {},
      },
    )

  superseding_fingerprint = "d" * 64
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      UPDATE drives
      SET telemetry_ready = 0
      WHERE id = ?
      """,
      (DRIVE_ID,),
    )
    connection.execute(
      """
      INSERT INTO route_inventories(
        id, device_id, drive_id, route_name, generation,
        manifest_sha256, rlog_source_fingerprint,
        manifest_size, materialized_row_count,
        state, route_closed, manifest_json, created_at
      ) VALUES (
        'inventory-two', ?, ?, ?, 2, ?, ?, 2, 0,
        'complete', 1, '{}', ?
      )
      """,
      (
        DEVICE_ID,
        DRIVE_ID,
        ROUTE_NAME,
        "e" * 64,
        superseding_fingerprint,
        NOW,
      ),
    )

  with pytest.raises(
    IntegrationError,
    match="stale_generation",
  ) as superseded:
    handlers.simulate_counterfactual(
      FakeContext("superseded-after-queue"),
      {
        "simulation_id": "simulation-three",
        "drive_id": DRIVE_ID,
        "t_us": 3_000_000,
        "horizon_us": 100_000,
        "model_hash": model_hash,
        "telemetry_sha256": extraction["ndjson_sha256"],
        "timeline_version": extraction["manifest"]["timeline_version"],
        "mode": "approximate_closed_loop",
        "baseline_params": copy.deepcopy(
          HISTORICAL_CONTROLLER_PARAMS,
        ),
        "candidate_params": {"friction": 0.2},
      },
    )
  assert superseded.value.details["latest_inventory_rlog_source_fingerprint"] == superseding_fingerprint
