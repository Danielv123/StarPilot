from __future__ import annotations

import getpass
import hashlib
import json
import math
import os
import secrets
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import uvicorn
from argon2 import PasswordHasher
from fastapi import APIRouter, FastAPI, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import (
  ApiError,
  AuthService,
  audit,
  request_ip,
  require_idempotency_key,
)
from .commands import router as commands_router
from .config import Settings
from .db import Database, ensure_storage_directories, isoformat, utc_now
from .devices import bootstrap_configured_devices, router as devices_router
from .inventory import (
  latest_inventory_view,
  router as inventory_router,
  segment_expected_streams,
)
from .jobs import WORKER_STALE_AFTER_SECONDS, request_cancellation
from .media_sync import inspect_media_sync, router as media_sync_router
from .models import (
  ArtifactView,
  AuditEventView,
  CompanionSettings,
  DashboardSnapshot,
  DriveCatalogPage,
  DriveDetail,
  JobList,
  JobView,
  LoginRequest,
  MediaManifest,
  SegmentView,
  SessionView,
  SimulationAccepted,
  SimulationCreate,
  SimulationView,
  SimulatorCapabilities,
)
from .request_limits import RequestBodyLimitMiddleware
from .simulation_eligibility import evaluate_simulation_eligibility
from .spa import mount_spa
from .telemetry import router as telemetry_router
from .uploads import build_upload_snapshot, router as uploads_router


def _error_content(
  code: str,
  message: str,
  details: Any | None = None,
) -> dict[str, Any]:
  return {
    "error": {
      "code": code,
      "message": message,
      "details": details if details is not None else {},
    },
  }


def _readiness(row: sqlite3.Row) -> str:
  if row["failed_artifact_count"] or row["failed_media_job_count"]:
    return "failed"
  if row["partial_artifact_count"]:
    return "partial"
  if row["inventory_id"] is None:
    return "processing" if row["artifact_count"] else "importing"
  if (
    row["inventory_state"] == "partial"
    or not bool(row["inventory_route_closed"])
    or row["inventory_missing_segment_count"]
    or row["inventory_missing_stream_count"]
  ):
    return "partial"
  if (
    row["inventory_declared_file_count"] == row["inventory_archived_file_count"]
    and row["inventory_ready_media_count"] >= row["inventory_expected_media_count"]
    and row["inventory_expected_rlog_count"] > 0
    and row["inventory_archived_rlog_count"] >= row["inventory_expected_rlog_count"]
    and row["inventory_rlog_source_fingerprint"] is not None
    and row["inventory_rlog_source_fingerprint"] == row["telemetry_source_fingerprint"]
    and bool(row["telemetry_ready"])
  ):
    return "ready"
  if row["artifact_count"]:
    return "processing"
  return "importing"


CATALOG_VIDEO_TYPES = frozenset(
  {
    "fcamera",
    "ecamera",
    "dcamera",
    "qcamera",
    "road",
    "wideRoad",
    "driver",
    "video",
  }
)
CATALOG_POSTER_TYPES = frozenset(
  {
    "poster",
    "video_poster",
    "derived_poster",
  }
)
CATALOG_THUMBNAIL_TYPES = frozenset(
  {
    "thumbnail",
    "video_thumbnail",
    "derived_thumbnail",
  }
)
CAMERA_IDS_BY_KIND = {
  "fcamera": "road",
  "road": "road",
  "ecamera": "wide",
  "wideRoad": "wide",
  "dcamera": "driver",
  "driver": "driver",
}
CAMERA_LABELS = {
  "road": "Road",
  "wide": "Wide",
  "driver": "Driver",
  "qcamera": "Q camera",
}


DRIVE_SELECT = """
  SELECT
    d.id, d.device_id, d.route_name, d.started_at, d.ended_at,
    d.duration_us, d.telemetry_ready, d.route_state, d.created_at,
    (
      SELECT COALESCE(SUM(MIN(upload.offset, upload.declared_size)), 0)
      FROM uploads upload
      WHERE upload.device_id = d.device_id
        AND upload.route_name = d.route_name
        AND upload.status != 'canceled'
    ) AS backup_bytes_received,
    (
      SELECT COALESCE(SUM(upload.declared_size), 0)
      FROM uploads upload
      WHERE upload.device_id = d.device_id
        AND upload.route_name = d.route_name
        AND upload.status != 'canceled'
    ) AS backup_bytes_expected,
    COUNT(DISTINCT s.id) AS segment_count,
    COUNT(DISTINCT a.id) AS artifact_count,
    COUNT(DISTINCT CASE WHEN a.status = 'failed' THEN a.id END)
      AS failed_artifact_count,
    COUNT(DISTINCT CASE WHEN a.status = 'partial' THEN a.id END)
      AS partial_artifact_count,
    COUNT(DISTINCT CASE
      WHEN a.status = 'ready'
        AND a.kind = 'derived_video'
        AND LOWER(a.codec) = 'av1'
      THEN a.id
    END) AS ready_media_count
    ,
    COUNT(DISTINCT CASE
      WHEN a.source_artifact_id IS NULL
        AND a.kind IN (
          'fcamera', 'ecamera', 'dcamera', 'qcamera',
          'road', 'wideRoad', 'driver', 'video'
        )
        AND a.segment_id IS NOT NULL
      THEN a.segment_id || ':' || COALESCE(a.camera, a.kind)
    END) AS expected_media_count,
    COUNT(DISTINCT CASE
      WHEN a.status = 'ready'
        AND a.kind = 'derived_video'
        AND LOWER(a.codec) = 'av1'
        AND a.segment_id IS NOT NULL
      THEN a.segment_id || ':' || COALESCE(a.camera, a.kind)
    END) AS ready_media_pair_count,
    (
      SELECT COUNT(DISTINCT pruned_source.id)
      FROM artifacts pruned_source
      JOIN objects pruned_object
        ON pruned_object.sha256 = pruned_source.object_sha256
      WHERE pruned_source.drive_id = d.id
        AND pruned_source.source_artifact_id IS NULL
        AND pruned_source.segment_id IS NOT NULL
        AND pruned_source.kind IN (
          'fcamera', 'ecamera', 'dcamera', 'qcamera',
          'road', 'wideRoad', 'driver', 'video'
        )
        AND pruned_object.storage_state = 'pruned'
        AND EXISTS (
          SELECT 1
          FROM artifacts pruned_derived
          WHERE pruned_derived.source_artifact_id = pruned_source.id
            AND pruned_derived.kind = 'derived_video'
            AND LOWER(pruned_derived.codec) = 'av1'
            AND pruned_derived.status = 'ready'
        )
    ) AS pruned_media_count,
    (
      SELECT COUNT(*)
      FROM segments ready_segment
      WHERE ready_segment.drive_id = d.id
        AND EXISTS (
          SELECT 1
          FROM artifacts expected
          WHERE expected.segment_id = ready_segment.id
            AND expected.source_artifact_id IS NULL
            AND expected.kind IN (
              'fcamera', 'ecamera', 'dcamera', 'qcamera',
              'road', 'wideRoad', 'driver', 'video'
            )
        )
        AND NOT EXISTS (
          SELECT 1
          FROM artifacts expected
          WHERE expected.segment_id = ready_segment.id
            AND expected.source_artifact_id IS NULL
            AND expected.kind IN (
              'fcamera', 'ecamera', 'dcamera', 'qcamera',
              'road', 'wideRoad', 'driver', 'video'
            )
            AND NOT EXISTS (
              SELECT 1
              FROM artifacts derived
              WHERE derived.segment_id = expected.segment_id
                AND COALESCE(derived.camera, derived.kind)
                  = COALESCE(expected.camera, expected.kind)
                AND derived.status = 'ready'
                AND derived.kind = 'derived_video'
                AND LOWER(derived.codec) = 'av1'
            )
        )
    ) AS ready_media_segment_count,
    (
      SELECT COUNT(DISTINCT
        failed_source.segment_id || ':' ||
        COALESCE(failed_source.camera, failed_source.kind)
      )
      FROM artifacts failed_source
      WHERE failed_source.drive_id = d.id
        AND failed_source.source_artifact_id IS NULL
        AND failed_source.segment_id IS NOT NULL
        AND failed_source.kind IN (
          'fcamera', 'ecamera', 'dcamera', 'qcamera',
          'road', 'wideRoad', 'driver', 'video'
        )
        AND NOT EXISTS (
          SELECT 1
          FROM artifacts recovered
          WHERE recovered.segment_id = failed_source.segment_id
            AND COALESCE(recovered.camera, recovered.kind)
              = COALESCE(failed_source.camera, failed_source.kind)
            AND recovered.kind = 'derived_video'
            AND LOWER(recovered.codec) = 'av1'
            AND recovered.status = 'ready'
        )
        AND (
          SELECT latest_job.state
          FROM jobs latest_job
          JOIN artifacts latest_source
            ON latest_source.id = json_extract(
              latest_job.payload_json,
              '$.artifact_id'
            )
          WHERE latest_job.type = 'transcode_video'
            AND latest_source.segment_id = failed_source.segment_id
            AND COALESCE(latest_source.camera, latest_source.kind)
              = COALESCE(failed_source.camera, failed_source.kind)
          ORDER BY latest_job.created_at DESC, latest_job.id DESC
          LIMIT 1
        ) = 'failed'
    ) AS failed_media_job_count,
    inventory.id AS inventory_id,
    inventory.state AS inventory_state,
    inventory.route_closed AS inventory_route_closed,
    inventory.rlog_source_fingerprint
      AS inventory_rlog_source_fingerprint,
    (
      SELECT telemetry.source_fingerprint
      FROM telemetry_indexes telemetry
      WHERE telemetry.drive_id = d.id
    ) AS telemetry_source_fingerprint,
    (
      SELECT COUNT(*)
      FROM route_inventory_segments inventory_segment
      WHERE inventory_segment.inventory_id = inventory.id
        AND inventory_segment.missing = 1
    ) AS inventory_missing_segment_count,
    (
      SELECT COUNT(*)
      FROM route_inventory_expected_files expected
      WHERE expected.inventory_id = inventory.id
        AND expected.is_stream = 1
        AND expected.status = 'missing'
    ) AS inventory_missing_stream_count,
    (
      SELECT COUNT(*)
      FROM route_inventory_expected_files declared
      WHERE declared.inventory_id = inventory.id
        AND declared.status = 'present'
    ) AS inventory_declared_file_count,
    (
      SELECT COUNT(*)
      FROM route_inventory_expected_files declared
      WHERE declared.inventory_id = inventory.id
        AND declared.status = 'present'
        AND EXISTS (
          SELECT 1
          FROM artifacts archived
          WHERE archived.drive_id = d.id
            AND archived.device_id = d.device_id
            AND archived.relative_path = declared.relative_path
            AND archived.object_sha256 = declared.declared_sha256
            AND archived.size = declared.declared_size
            AND (
              archived.kind = declared.artifact_type
              OR (
                declared.artifact_type = 'video'
                AND archived.kind IN (
                  'fcamera', 'ecamera', 'dcamera', 'qcamera',
                  'road', 'wideRoad', 'driver'
                )
              )
            )
            AND COALESCE(archived.camera, '') =
              COALESCE(declared.camera, '')
            AND archived.status IN ('stored', 'verified', 'ready')
        )
    ) AS inventory_archived_file_count,
    (
      SELECT COUNT(*)
      FROM route_inventory_expected_files expected
      WHERE expected.inventory_id = inventory.id
        AND expected.is_stream = 1
        AND expected.artifact_type = 'video'
    ) AS inventory_expected_media_count,
    (
      SELECT COUNT(*)
      FROM route_inventory_expected_files expected
      WHERE expected.inventory_id = inventory.id
        AND expected.is_stream = 1
        AND expected.artifact_type = 'video'
        AND expected.status = 'present'
        AND EXISTS (
          SELECT 1
          FROM artifacts source
          JOIN artifacts derived
            ON derived.source_artifact_id = source.id
          WHERE source.drive_id = d.id
            AND source.device_id = d.device_id
            AND source.relative_path = expected.relative_path
            AND source.object_sha256 = expected.declared_sha256
            AND source.size = expected.declared_size
            AND (
              source.kind = expected.artifact_type
              OR (
                expected.artifact_type = 'video'
                AND source.kind IN (
                  'fcamera', 'ecamera', 'dcamera', 'qcamera',
                  'road', 'wideRoad', 'driver'
                )
              )
            )
            AND COALESCE(source.camera, '') =
              COALESCE(expected.camera, '')
            AND source.status IN ('stored', 'verified', 'ready')
            AND derived.kind = 'derived_video'
            AND LOWER(derived.codec) = 'av1'
            AND derived.status = 'ready'
        )
    ) AS inventory_ready_media_count,
    (
      SELECT COUNT(*)
      FROM route_inventory_expected_files expected
      WHERE expected.inventory_id = inventory.id
        AND expected.is_stream = 1
        AND expected.artifact_type = 'rlog'
    ) AS inventory_expected_rlog_count,
    (
      SELECT COUNT(*)
      FROM route_inventory_expected_files expected
      WHERE expected.inventory_id = inventory.id
        AND expected.is_stream = 1
        AND expected.artifact_type = 'rlog'
        AND expected.status = 'present'
        AND EXISTS (
          SELECT 1
          FROM artifacts archived
          WHERE archived.drive_id = d.id
            AND archived.device_id = d.device_id
            AND archived.relative_path = expected.relative_path
            AND archived.object_sha256 = expected.declared_sha256
            AND archived.size = expected.declared_size
            AND archived.kind = 'rlog'
            AND archived.camera IS NULL
            AND archived.status IN ('stored', 'verified', 'ready')
        )
    ) AS inventory_archived_rlog_count,
    (
      SELECT COUNT(*)
      FROM route_inventory_segments inventory_segment
      WHERE inventory_segment.inventory_id = inventory.id
        AND inventory_segment.missing = 0
        AND NOT EXISTS (
          SELECT 1
          FROM route_inventory_expected_files expected
          WHERE expected.inventory_id = inventory.id
            AND expected.segment_number =
              inventory_segment.segment_number
            AND (
              expected.status = 'missing'
              OR NOT EXISTS (
                SELECT 1
                FROM artifacts archived
                WHERE archived.drive_id = d.id
                  AND archived.device_id = d.device_id
                  AND archived.relative_path = expected.relative_path
                  AND archived.object_sha256 = expected.declared_sha256
                  AND archived.size = expected.declared_size
                  AND (
                    archived.kind = expected.artifact_type
                    OR (
                      expected.artifact_type = 'video'
                      AND archived.kind IN (
                        'fcamera', 'ecamera', 'dcamera', 'qcamera',
                        'road', 'wideRoad', 'driver'
                      )
                    )
                  )
                  AND COALESCE(archived.camera, '') =
                    COALESCE(expected.camera, '')
                  AND archived.status IN ('stored', 'verified', 'ready')
              )
              OR (
                expected.is_stream = 1
                AND expected.artifact_type = 'video'
                AND NOT EXISTS (
                  SELECT 1
                  FROM artifacts media_source
                  JOIN artifacts media_derived
                    ON media_derived.source_artifact_id = media_source.id
                  WHERE media_source.drive_id = d.id
                    AND media_source.device_id = d.device_id
                    AND media_source.relative_path =
                      expected.relative_path
                    AND media_source.object_sha256 =
                      expected.declared_sha256
                    AND media_source.size = expected.declared_size
                    AND (
                      media_source.kind = expected.artifact_type
                      OR (
                        expected.artifact_type = 'video'
                        AND media_source.kind IN (
                          'fcamera', 'ecamera', 'dcamera', 'qcamera',
                          'road', 'wideRoad', 'driver'
                        )
                      )
                    )
                    AND COALESCE(media_source.camera, '') =
                      COALESCE(expected.camera, '')
                    AND media_source.status IN (
                      'stored', 'verified', 'ready'
                    )
                    AND media_derived.kind = 'derived_video'
                    AND LOWER(media_derived.codec) = 'av1'
                    AND media_derived.status = 'ready'
                )
              )
            )
        )
    ) AS inventory_ready_segment_count
  FROM drives d
  LEFT JOIN segments s ON s.drive_id = d.id
  LEFT JOIN artifacts a ON a.drive_id = d.id
  LEFT JOIN route_inventories inventory
    ON inventory.drive_id = d.id
    AND inventory.generation = (
      SELECT MAX(latest_inventory.generation)
      FROM route_inventories latest_inventory
      WHERE latest_inventory.drive_id = d.id
    )
"""

READINESS_SQL = """
  CASE
    WHEN failed_artifact_count > 0 OR failed_media_job_count > 0
      THEN 'failed'
    WHEN partial_artifact_count > 0
      THEN 'partial'
    WHEN inventory_id IS NULL
      THEN CASE
        WHEN artifact_count > 0 THEN 'processing'
        ELSE 'importing'
      END
    WHEN inventory_state = 'partial'
      OR inventory_route_closed = 0
      OR inventory_missing_segment_count > 0
      OR inventory_missing_stream_count > 0
      THEN 'partial'
    WHEN inventory_declared_file_count = inventory_archived_file_count
      AND inventory_ready_media_count >= inventory_expected_media_count
      AND inventory_expected_rlog_count > 0
      AND inventory_archived_rlog_count >= inventory_expected_rlog_count
      AND inventory_rlog_source_fingerprint IS NOT NULL
      AND inventory_rlog_source_fingerprint = telemetry_source_fingerprint
      AND telemetry_ready = 1
      THEN 'ready'
    WHEN artifact_count > 0
      THEN 'processing'
    ELSE 'importing'
  END
"""


def _camera_id(row: sqlite3.Row) -> str:
  return row["camera"] or CAMERA_IDS_BY_KIND.get(row["kind"], row["kind"])


def _camera_label(camera_id: str) -> str:
  return CAMERA_LABELS.get(camera_id, camera_id)


def _catalog_coordinate(value: Any) -> str | None:
  if not isinstance(value, str) or value.count(",") != 1:
    return None
  latitude_text, longitude_text = value.split(",")
  try:
    latitude = float(latitude_text)
    longitude = float(longitude_text)
  except ValueError:
    return None
  if (
    not math.isfinite(latitude)
    or not math.isfinite(longitude)
    or not -90.0 <= latitude <= 90.0
    or not -180.0 <= longitude <= 180.0
    or f"{latitude:.7f},{longitude:.7f}" != value
  ):
    return None
  return value


def _route_summary_metadata(
  manifest: dict[str, Any],
) -> dict[str, Any]:
  result = {
    "distance_m": None,
    "location_start": None,
    "location_end": None,
  }
  if manifest.get("state") != "complete" or manifest.get("publication_ready") is not True:
    return result
  summary = manifest.get("route_summary")
  if not isinstance(summary, dict):
    return result
  provenance = summary.get("provenance")
  if not isinstance(provenance, dict):
    return result
  included = provenance.get("included_interval_count")
  excluded = provenance.get("excluded_interval_count")
  counts_valid = (
    isinstance(included, int)
    and not isinstance(included, bool)
    and included >= 0
    and isinstance(excluded, int)
    and not isinstance(excluded, bool)
    and excluded >= 0
  )
  distance = summary.get("distance_m")
  if (
    counts_valid
    and included > 0
    and isinstance(distance, (int, float))
    and not isinstance(distance, bool)
    and math.isfinite(float(distance))
    and float(distance) >= 0.0
    and provenance.get("distance_method") == "trapezoidal_absolute_vEgo_monotonic_dt_le_250ms"
  ):
    result["distance_m"] = float(distance)
  location_start = _catalog_coordinate(
    summary.get("location_start"),
  )
  location_end = _catalog_coordinate(summary.get("location_end"))
  if (
    counts_valid
    and location_start is not None
    and location_end is not None
    and provenance.get("location_method") == "first_last_valid_gps_fix_lat_lon_decimal_degrees_7"
  ):
    result["location_start"] = location_start
    result["location_end"] = location_end
  return result


def _drive_metadata(
  database: Database,
  drive_ids: list[str],
) -> dict[str, dict[str, Any]]:
  metadata = {
    drive_id: {
      "cameras": {},
      "vehicle": None,
      "distance_m": None,
      "location_start": None,
      "location_end": None,
      "raw_objects": {},
      "derived_objects": {},
      "all_objects": {},
      "poster_url": None,
      "thumbnail_url": None,
      "poster_choice": None,
      "thumbnail_choice": None,
    }
    for drive_id in drive_ids
  }
  if not drive_ids:
    return metadata
  placeholders = ",".join("?" for _ in drive_ids)
  artifact_rows = database.query_all(
    f"""
    SELECT
      artifacts.drive_id, artifacts.id, kind, camera, status, codec, width, height, fps,
      mime_type, source_artifact_id, object_sha256, artifacts.size,
      artifacts.created_at,
      objects.storage_state
    FROM artifacts
    JOIN objects ON objects.sha256 = artifacts.object_sha256
    WHERE artifacts.drive_id IN ({placeholders})
    ORDER BY artifacts.created_at, artifacts.id
    """,
    drive_ids,
  )
  for row in artifact_rows:
    item = metadata[row["drive_id"]]
    size = int(row["size"])
    if row["storage_state"] == "present":
      item["all_objects"][row["object_sha256"]] = size
      object_group = item["derived_objects"] if row["source_artifact_id"] is not None else item["raw_objects"]
      object_group[row["object_sha256"]] = size

    is_source_video = row["source_artifact_id"] is None and row["kind"] in CATALOG_VIDEO_TYPES
    is_ready_video = row["kind"] == "derived_video" and row["status"] == "ready" and isinstance(row["codec"], str) and row["codec"].lower() == "av1"
    if is_source_video or is_ready_video:
      camera_id = _camera_id(row)
      camera = item["cameras"].setdefault(
        camera_id,
        {
          "id": camera_id,
          "label": _camera_label(camera_id),
          "available": False,
          "codec": None,
          "width": None,
          "height": None,
          "fps": None,
        },
      )
      if is_ready_video:
        camera.update(
          {
            "available": True,
            "codec": row["codec"],
            "width": row["width"],
            "height": row["height"],
            "fps": row["fps"],
          }
        )

    image_kind: str | None = None
    if row["kind"] in CATALOG_POSTER_TYPES:
      image_kind = "poster"
    elif row["kind"] in CATALOG_THUMBNAIL_TYPES:
      image_kind = "thumbnail"
    if image_kind is not None and row["status"] == "ready" and isinstance(row["mime_type"], str) and row["mime_type"].lower().startswith("image/"):
      camera_id = _camera_id(row)
      choice = (
        int(camera_id == "road"),
        row["created_at"],
        row["id"],
      )
      choice_key = f"{image_kind}_choice"
      if item[choice_key] is None or choice > item[choice_key]:
        item[choice_key] = choice
        item[f"{image_kind}_url"] = f"/api/v1/artifacts/{row['id']}/content"

  inventory_rows = database.query_all(
    f"""
    SELECT inventory.drive_id, inventory.manifest_json
    FROM route_inventories inventory
    WHERE inventory.drive_id IN ({placeholders})
      AND inventory.generation = (
        SELECT MAX(latest.generation)
        FROM route_inventories latest
        WHERE latest.drive_id = inventory.drive_id
      )
    """,
    drive_ids,
  )
  for row in inventory_rows:
    try:
      manifest = json.loads(row["manifest_json"])
    except (json.JSONDecodeError, TypeError):
      continue
    expected_streams = manifest.get("expected_streams") if isinstance(manifest, dict) else None
    if not isinstance(expected_streams, list):
      continue
    for expected in expected_streams:
      if not isinstance(expected, dict) or expected.get("artifact_type") != "video" or not isinstance(expected.get("camera"), str):
        continue
      camera_id = expected["camera"]
      metadata[row["drive_id"]]["cameras"].setdefault(
        camera_id,
        {
          "id": camera_id,
          "label": _camera_label(camera_id),
          "available": False,
          "codec": None,
          "width": None,
          "height": None,
          "fps": None,
        },
      )

  manifest_rows = database.query_all(
    f"""
    SELECT drive_id, manifest_json
    FROM telemetry_indexes
    WHERE drive_id IN ({placeholders})
    """,
    drive_ids,
  )
  for row in manifest_rows:
    try:
      manifest = json.loads(row["manifest_json"])
    except (json.JSONDecodeError, TypeError):
      continue
    vehicle = manifest.get("vehicle") if isinstance(manifest, dict) else None
    fingerprint = vehicle.get("car_fingerprint") if isinstance(vehicle, dict) else None
    if isinstance(fingerprint, str) and fingerprint.strip():
      metadata[row["drive_id"]]["vehicle"] = fingerprint.strip()
    metadata[row["drive_id"]].update(
      _route_summary_metadata(manifest),
    )

  for item in metadata.values():
    item["cameras"] = [item["cameras"][camera_id] for camera_id in sorted(item["cameras"])]
    item["raw_bytes"] = sum(item.pop("raw_objects").values())
    item["derived_bytes"] = sum(item.pop("derived_objects").values())
    item["stored_bytes"] = sum(item.pop("all_objects").values())
    item.pop("poster_choice")
    item.pop("thumbnail_choice")
  return metadata


def _drive_view(
  row: sqlite3.Row,
  metadata: dict[str, Any] | None = None,
  *,
  raw_video_pruning_required: bool = False,
) -> dict[str, Any]:
  catalog = metadata or {}
  inventory_present = row["inventory_id"] is not None
  expected_media = row["inventory_expected_media_count"] if inventory_present else row["expected_media_count"]
  ready_media = row["inventory_ready_media_count"] if inventory_present else row["ready_media_pair_count"]
  return {
    "id": row["id"],
    "device_id": row["device_id"],
    "route_name": row["route_name"],
    "started_at": row["started_at"],
    "ended_at": row["ended_at"],
    "duration_us": row["duration_us"],
    "segment_count": row["segment_count"],
    "ready_segments": (row["inventory_ready_segment_count"] if inventory_present else row["ready_media_segment_count"]),
    "expected_media": expected_media,
    "ready_media": ready_media,
    "missing_media": max(
      0,
      expected_media - ready_media,
    ),
    "failed_media": row["failed_media_job_count"],
    "pruned_media": row["pruned_media_count"],
    "raw_video_pruning_required": raw_video_pruning_required,
    "backup_bytes_received": row["backup_bytes_received"],
    "backup_bytes_expected": row["backup_bytes_expected"],
    "artifact_count": row["artifact_count"],
    "telemetry_ready": bool(row["telemetry_ready"]),
    "readiness": _readiness(row),
    "cameras": catalog.get("cameras", []),
    "vehicle": catalog.get("vehicle"),
    "distance_m": catalog.get("distance_m"),
    "location_start": catalog.get("location_start"),
    "location_end": catalog.get("location_end"),
    "raw_bytes": catalog.get("raw_bytes", 0),
    "derived_bytes": catalog.get("derived_bytes", 0),
    "stored_bytes": catalog.get("stored_bytes", 0),
    "poster_url": catalog.get("poster_url"),
    "thumbnail_url": catalog.get("thumbnail_url"),
    "created_at": row["created_at"],
  }


def _drive_catalog_sql(where: str, *, readiness: bool) -> str:
  computed = f"""
    SELECT catalog.*, {READINESS_SQL} AS computed_readiness
    FROM (
      {DRIVE_SELECT}
      {where}
      GROUP BY d.id
    ) AS catalog
  """
  if not readiness:
    return computed
  return f"SELECT * FROM ({computed}) WHERE computed_readiness = ?"


def _artifact_view(row: sqlite3.Row) -> dict[str, Any]:
  return {
    "id": row["id"],
    "device_id": row["device_id"],
    "drive_id": row["drive_id"],
    "segment_id": row["segment_id"],
    "kind": row["kind"],
    "camera": row["camera"],
    "relative_path": row["relative_path"],
    "sha256": row["object_sha256"],
    "size": row["size"],
    "mime_type": row["mime_type"],
    "codec": row["codec"],
    "duration_us": row["duration_us"],
    "status": row["status"],
    "source_artifact_id": row["source_artifact_id"],
    "created_at": row["created_at"],
  }


def _segment_view(
  row: sqlite3.Row,
  artifacts: list[sqlite3.Row],
  expected_streams: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
  return {
    "id": row["id"],
    "drive_id": row["drive_id"],
    "number": row["number"],
    "started_at": row["started_at"],
    "start_t_us": row["start_t_us"],
    "duration_us": row["duration_us"],
    "artifacts": [_artifact_view(item) for item in artifacts],
    "expected_streams": expected_streams or [],
  }


def _job_view(row: sqlite3.Row) -> dict[str, Any]:
  fields = set(row.keys())
  payload = json.loads(row["payload_json"])
  return {
    "id": row["id"],
    "type": row["type"],
    "upload_id": (row["upload_id_association"] if "upload_id_association" in fields else payload.get("upload_id")),
    "drive_id": (row["drive_id_association"] if "drive_id_association" in fields else payload.get("drive_id")),
    "artifact_id": (row["artifact_id_association"] if "artifact_id_association" in fields else payload.get("artifact_id")),
    "retry_of_job_id": payload.get("retry_of_job_id"),
    "state": row["state"],
    "progress": row["progress"],
    "attempts": row["attempts"],
    "max_attempts": row["max_attempts"],
    "retryable": (None if row["retryable"] is None else bool(row["retryable"])),
    "error": row["error"],
    "result": json.loads(row["result_json"] or "{}"),
    "available_at": row["available_at"],
    "cancel_requested_at": row["cancel_requested_at"],
    "created_at": row["created_at"],
    "updated_at": row["updated_at"],
    "completed_at": row["completed_at"],
  }


JOB_SELECT = """
  SELECT
    j.*,
    json_extract(j.payload_json, '$.artifact_id')
      AS artifact_id_association,
    COALESCE(
      json_extract(j.payload_json, '$.upload_id'),
      (
        SELECT association_upload.id
        FROM uploads association_upload
        WHERE association_upload.artifact_id = json_extract(
          j.payload_json,
          '$.artifact_id'
        )
        ORDER BY association_upload.completed_at DESC,
          association_upload.id DESC
        LIMIT 1
      )
    ) AS upload_id_association,
    COALESCE(
      json_extract(j.payload_json, '$.drive_id'),
      (
        SELECT association_artifact.drive_id
        FROM artifacts association_artifact
        WHERE association_artifact.id = json_extract(
          j.payload_json,
          '$.artifact_id'
        )
      )
    ) AS drive_id_association
  FROM jobs j
"""


def _validate_parameters(
  schema: Any,
  parameters: dict[str, Any],
) -> None:
  if not isinstance(schema, list):
    raise ApiError(
      500,
      "invalid_model_schema",
      "Enabled model has an invalid parameter schema",
    )
  definitions = {item.get("name"): item for item in schema if isinstance(item, dict) and isinstance(item.get("name"), str)}
  unknown = sorted(set(parameters) - set(definitions))
  if unknown:
    raise ApiError(
      422,
      "unknown_simulation_parameter",
      "Simulation contains unknown parameters",
      details={"parameters": unknown},
    )
  errors: list[dict[str, Any]] = []
  for name, value in parameters.items():
    definition = definitions[name]
    value_type = definition.get("type")
    valid_type = (
      isinstance(value, bool)
      if value_type == "boolean"
      else isinstance(value, int) and not isinstance(value, bool)
      if value_type == "integer"
      else isinstance(value, (int, float)) and not isinstance(value, bool)
    )
    if not valid_type:
      errors.append({"parameter": name, "reason": "type"})
      continue
    if isinstance(value, (int, float)) and not isinstance(value, bool):
      minimum = definition.get("minimum")
      maximum = definition.get("maximum")
      if isinstance(minimum, (int, float)) and value < minimum:
        errors.append({"parameter": name, "reason": "minimum", "minimum": minimum})
      if isinstance(maximum, (int, float)) and value > maximum:
        errors.append({"parameter": name, "reason": "maximum", "maximum": maximum})
  if errors:
    raise ApiError(
      422,
      "invalid_simulation_parameters",
      "Simulation parameters are outside the model schema",
      details={"errors": errors},
    )


def _public_parameter_schema(schema: Any) -> dict[str, Any]:
  if not isinstance(schema, list):
    raise ApiError(
      500,
      "invalid_model_schema",
      "Registered model has an invalid parameter schema",
    )
  properties: dict[str, Any] = {}
  for item in schema:
    if (
      not isinstance(item, dict)
      or not isinstance(item.get("name"), str)
      or item["name"] in properties
      or item.get("type") not in {"number", "integer", "boolean"}
    ):
      raise ApiError(
        500,
        "invalid_model_schema",
        "Registered model has an invalid parameter schema",
      )
    definition = {
      key: item[key]
      for key in (
        "type",
        "minimum",
        "maximum",
        "default",
        "description",
      )
      if key in item
    }
    if "label" in item:
      definition["title"] = item["label"]
    if "step" in item:
      definition["multipleOf"] = item["step"]
    extensions = {
      "runtime_supported": "x-runtime-supported",
      "scope": "x-scope",
      "units": "x-units",
      "category": "x-group",
      "advanced": "x-advanced",
    }
    for key, public_key in extensions.items():
      if key in item:
        definition[public_key] = item[key]
    properties[item["name"]] = definition
  return {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": properties,
    "additionalProperties": False,
  }


api = APIRouter(prefix="/api/v1")


@api.get("/health", tags=["system"])
async def health() -> dict[str, Any]:
  """Cheap public process liveness with no database or archive I/O."""
  return {
    "status": "ok",
    "service": "comma-companion-api",
    "time": isoformat(),
  }


def _archive_health(request: Request) -> dict[str, bool]:
  archive_root = request.app.state.settings.archive_root
  available = archive_root.is_dir()
  return {
    "available": available,
    "writable": available and os.access(archive_root, os.W_OK),
  }


@api.get("/readiness", tags=["system"])
def readiness(request: Request, response: Response) -> dict[str, Any]:
  """Authenticated dependency diagnostics for operators."""
  request.app.state.auth.authenticate_admin(request)
  database_health = request.app.state.database.health()
  archive = _archive_health(request)
  ready = database_health["ok"] and archive["available"] and archive["writable"]
  if not ready:
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
  return {
    "status": "ready" if ready else "degraded",
    "database": database_health,
    "archive": archive,
    "admin_configured": request.app.state.auth.admin_configured,
    "time": isoformat(),
  }


@api.post("/auth/login", response_model=SessionView, tags=["authentication"])
def login(
  request: Request,
  response: Response,
  payload: LoginRequest,
) -> dict[str, Any]:
  auth = request.app.state.auth
  auth.require_origin(request)
  key = request_ip(request) or "unknown"
  username_valid = secrets.compare_digest(
    payload.username,
    request.app.state.settings.admin_username,
  )
  password_valid = auth.verify_password_admitted(
    payload.password,
    key,
    additional_valid=username_valid,
  )
  if not username_valid or not password_valid:
    audit(
      request.app.state.database,
      actor_type="anonymous",
      actor_id=None,
      action="auth.login_failed",
      details={"username": payload.username[:128]},
      ip_address=request_ip(request),
    )
    raise ApiError(401, "invalid_credentials", "Username or password is invalid")
  signed, session = auth.create_session(
    ip_address=request_ip(request),
    user_agent=request.headers.get("user-agent"),
  )
  response.set_cookie(
    request.app.state.settings.cookie_name,
    signed,
    max_age=request.app.state.settings.session_ttl_seconds,
    secure=request.app.state.settings.cookie_secure,
    httponly=True,
    samesite="strict",
    path="/",
  )
  response.headers["Cache-Control"] = "no-store"
  return session


@api.get("/auth/session", response_model=SessionView, tags=["authentication"])
@api.get("/auth/me", response_model=SessionView, tags=["authentication"])
def current_session(request: Request) -> dict[str, Any]:
  principal = request.app.state.auth.authenticate_admin(request)
  return request.app.state.auth.session_view(principal)


@api.post("/auth/confirm", response_model=SessionView, tags=["authentication"])
def confirm_password(request: Request, payload: LoginRequest) -> dict[str, Any]:
  auth = request.app.state.auth
  auth.require_origin(request)
  principal = auth.authenticate_admin(request)
  username_valid = secrets.compare_digest(
    payload.username,
    request.app.state.settings.admin_username,
  )
  auth.confirm_password(
    principal,
    payload.password,
    request_ip(request),
    username_valid=username_valid,
  )
  return auth.session_view(principal)


@api.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["authentication"])
def logout(request: Request, response: Response) -> Response:
  auth = request.app.state.auth
  auth.require_origin(request)
  principal = auth.authenticate_admin(request)
  auth.revoke_session(principal, request_ip(request))
  response.delete_cookie(
    request.app.state.settings.cookie_name,
    path="/",
    secure=request.app.state.settings.cookie_secure,
    httponly=True,
    samesite="strict",
  )
  response.status_code = status.HTTP_204_NO_CONTENT
  return response


def _dashboard(request: Request) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  database = request.app.state.database
  generated_at = utc_now()
  online_after = isoformat(
    generated_at - timedelta(seconds=request.app.state.settings.online_window_seconds),
  )
  counts = database.query_one(
    """
    SELECT
      (SELECT COUNT(*) FROM devices WHERE disabled_at IS NULL) AS devices_total,
      (
        SELECT COUNT(*)
        FROM device_live_state live
        JOIN devices active_device ON active_device.id = live.device_id
        WHERE active_device.disabled_at IS NULL
          AND live.last_seen_at >= ?
      ) AS devices_online,
      (SELECT COUNT(*) FROM drives) AS drives_total,
      (SELECT COUNT(*) FROM segments) AS segments_total,
      (SELECT COUNT(*) FROM artifacts) AS artifacts_total,
      (
        SELECT COALESCE(SUM(stored.size), 0)
        FROM (
          SELECT artifacts.object_sha256, MAX(artifacts.size) AS size
          FROM artifacts
          JOIN objects ON objects.sha256 = artifacts.object_sha256
          WHERE source_artifact_id IS NULL
            AND objects.storage_state = 'present'
          GROUP BY artifacts.object_sha256
        ) AS stored
      ) AS raw_bytes,
      (
        SELECT COALESCE(SUM(stored.size), 0)
        FROM (
          SELECT artifacts.object_sha256, MAX(artifacts.size) AS size
          FROM artifacts
          JOIN objects ON objects.sha256 = artifacts.object_sha256
          WHERE source_artifact_id IS NOT NULL
            AND objects.storage_state = 'present'
          GROUP BY artifacts.object_sha256
        ) AS stored
      ) AS derived_bytes,
      (
        SELECT COALESCE(SUM(stored.size), 0)
        FROM (
          SELECT artifacts.object_sha256, MAX(artifacts.size) AS size
          FROM artifacts
          JOIN objects ON objects.sha256 = artifacts.object_sha256
          WHERE objects.storage_state = 'present'
          GROUP BY artifacts.object_sha256
        ) AS stored
      ) AS cataloged_bytes
    """,
    (online_after,),
  )
  readiness_rows = database.query_all(
    f"""
    SELECT computed_readiness AS state, COUNT(*) AS count
    FROM ({_drive_catalog_sql("", readiness=False)})
    GROUP BY computed_readiness
    """,
  )
  command_rows = database.query_all(
    "SELECT state, COUNT(*) AS count FROM commands GROUP BY state",
  )
  job_rows = database.query_all(
    "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state",
  )
  transcode_work = database.query_one(
    """
    SELECT
      COUNT(*) AS jobs_remaining,
      COALESCE(SUM(1.0 - MIN(1.0, MAX(0.0, progress))), 0.0)
        AS work_remaining
    FROM jobs
    WHERE type = 'transcode_video'
      AND state IN ('queued', 'leased', 'running')
    """,
  )
  recent_transcodes = database.query_all(
    """
    SELECT completed_at
    FROM jobs
    WHERE type = 'transcode_video'
      AND state = 'succeeded'
      AND completed_at IS NOT NULL
    ORDER BY completed_at DESC
    LIMIT 21
    """,
  )
  worker_online_after = isoformat(
    generated_at - timedelta(seconds=WORKER_STALE_AFTER_SECONDS),
  )
  worker = database.query_one(
    """
    SELECT
      latest.last_seen,
      CASE
        WHEN latest.last_seen IS NOT NULL
          AND julianday(latest.last_seen) >= julianday(?)
        THEN 1
        ELSE 0
      END AS online
    FROM (
      SELECT MAX(last_seen_at) AS last_seen
      FROM worker_heartbeats
    ) AS latest
    """,
    (worker_online_after,),
  )
  assert counts is not None
  assert worker is not None
  assert transcode_work is not None
  completion_times = sorted(
    datetime.fromisoformat(row["completed_at"])
    for row in recent_transcodes
  )
  completion_gaps = [
    (current - previous).total_seconds()
    for previous, current in zip(
      completion_times,
      completion_times[1:],
      strict=False,
    )
    if 1 <= (current - previous).total_seconds() <= 600
  ]
  seconds_per_transcode = (
    float(median(completion_gaps))
    if len(completion_gaps) >= 3
    else None
  )
  transcode_jobs_remaining = int(transcode_work["jobs_remaining"])
  if transcode_jobs_remaining == 0:
    transcode_eta_seconds: int | None = 0
  elif seconds_per_transcode is None:
    transcode_eta_seconds = None
  else:
    transcode_eta_seconds = math.ceil(
      float(transcode_work["work_remaining"]) * seconds_per_transcode,
    )
  worker_online = bool(worker["online"])
  drives_by_readiness = dict.fromkeys(
    ("importing", "processing", "ready", "partial", "failed"),
    0,
  )
  drives_by_readiness.update({row["state"]: row["count"] for row in readiness_rows})
  try:
    storage = shutil.disk_usage(request.app.state.settings.archive_root)
  except OSError:
    storage = None
  return {
    "generated_at": generated_at,
    "devices_total": counts["devices_total"],
    "devices_online": counts["devices_online"],
    "drives_total": counts["drives_total"],
    "drives_ready": drives_by_readiness["ready"],
    "drives_by_readiness": drives_by_readiness,
    "segments_total": counts["segments_total"],
    "artifacts_total": counts["artifacts_total"],
    "raw_bytes": counts["raw_bytes"],
    "derived_bytes": counts["derived_bytes"],
    "storage_cataloged_bytes": counts["cataloged_bytes"],
    "storage_capacity_bytes": storage.total if storage else None,
    "storage_used_bytes": storage.used if storage else None,
    "storage_free_bytes": storage.free if storage else None,
    "upload": build_upload_snapshot(request),
    "commands_by_state": {row["state"]: row["count"] for row in command_rows},
    "jobs_by_state": {row["state"]: row["count"] for row in job_rows},
    "worker": {
      "last_seen": worker["last_seen"],
      "stale": worker["last_seen"] is not None and not worker_online,
      "online": worker_online,
      "transcode_jobs_remaining": transcode_jobs_remaining,
      "transcode_seconds_per_job": seconds_per_transcode,
      "eta_seconds": transcode_eta_seconds,
    },
    "archive": _archive_health(request),
  }


@api.get("/dashboard", response_model=DashboardSnapshot, tags=["dashboard"])
@api.get("/overview", response_model=DashboardSnapshot, tags=["dashboard"])
def dashboard(request: Request) -> dict[str, Any]:
  return _dashboard(request)


def _settings_view(request: Request) -> dict[str, Any]:
  rows = request.app.state.database.query_all(
    "SELECT key, value_json FROM runtime_settings",
  )
  stored = {row["key"]: json.loads(row["value_json"]) for row in rows}
  return {
    "archive_path": str(request.app.state.settings.archive_root),
    "raw_log_retention_enabled": True,
    "raw_video_retention_enabled": request.app.state.settings.retain_raw_video,
    "transcode_codec": "av1",
    "transcode_crf": stored.get(
      "transcode_crf",
      request.app.state.settings.transcode_crf,
    ),
    "worker_concurrency": 1,
    "metered_uploads_allowed": stored.get("metered_uploads_allowed", False),
    "timezone": stored.get("timezone", "Europe/Oslo"),
  }


@api.get("/settings", response_model=CompanionSettings, tags=["settings"])
def get_settings(request: Request) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  return _settings_view(request)


@api.patch("/settings", response_model=CompanionSettings, tags=["settings"])
def update_settings(
  request: Request,
  payload: CompanionSettings,
) -> dict[str, Any]:
  auth = request.app.state.auth
  auth.require_origin(request)
  principal = auth.authenticate_admin(request)
  idempotency_key = require_idempotency_key(request)
  current = _settings_view(request)
  immutable = {
    "archive_path",
    "raw_log_retention_enabled",
    "raw_video_retention_enabled",
    "transcode_codec",
    "worker_concurrency",
    "metered_uploads_allowed",
    "timezone",
  }
  changed_immutable = sorted(key for key in immutable if payload.model_dump()[key] != current[key])
  if changed_immutable:
    raise ApiError(
      422,
      "immutable_setting",
      "This setting is fixed by the server deployment",
      details={"settings": changed_immutable},
    )
  values = {
    "transcode_crf": payload.transcode_crf,
  }
  canonical = json.dumps(
    {"operation": "settings.update", **values},
    separators=(",", ":"),
    sort_keys=True,
  )
  request_hash = hashlib.sha256(canonical.encode()).hexdigest()
  now_text = isoformat()
  with request.app.state.database.transaction(immediate=True) as connection:
    prior = connection.execute(
      """
      SELECT request_hash
      FROM idempotency_keys
      WHERE actor_type = 'admin' AND actor_id = 'admin' AND key = ?
      """,
      (idempotency_key,),
    ).fetchone()
    if prior is not None:
      if not secrets.compare_digest(prior["request_hash"], request_hash):
        raise ApiError(
          409,
          "idempotency_key_reused",
          "Idempotency key was already used with a different request",
        )
      return _settings_view(request)
    connection.execute(
      """
      INSERT INTO idempotency_keys(
        actor_type, actor_id, key, request_hash, status_code,
        response_json, created_at
      ) VALUES ('admin', 'admin', ?, ?, 200, '{}', ?)
      """,
      (idempotency_key, request_hash, now_text),
    )
    for key, value in values.items():
      connection.execute(
        """
        INSERT INTO runtime_settings(key, value_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
          value_json = excluded.value_json,
          updated_at = excluded.updated_at
        """,
        (key, json.dumps(value), now_text),
      )
    audit(
      connection,
      actor_type=principal.actor_type,
      actor_id=principal.actor_id,
      action="settings.update",
      resource_type="settings",
      details={"keys": sorted(values)},
      ip_address=request_ip(request),
    )
  return _settings_view(request)


@api.get("/drives", response_model=DriveCatalogPage, tags=["catalog"])
def list_drives(
  request: Request,
  q: str | None = None,
  device_id: str | None = None,
  readiness: str | None = None,
  limit: int = 100,
  offset: int = 0,
) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  if limit < 1 or limit > 500:
    raise ApiError(422, "invalid_limit", "limit must be between 1 and 500")
  if offset < 0:
    raise ApiError(422, "invalid_offset", "offset must not be negative")
  if readiness and readiness not in {"importing", "processing", "ready", "partial", "failed"}:
    raise ApiError(422, "invalid_readiness", "Drive readiness is invalid")
  clauses: list[str] = []
  parameters: list[Any] = []
  if q:
    query = q.strip()
    if not query or len(query) > 256:
      raise ApiError(
        422,
        "invalid_query",
        "q must contain between 1 and 256 characters",
      )
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    clauses.append(
      """
      (
        d.route_name LIKE ? ESCAPE '\\'
        OR d.device_id LIKE ? ESCAPE '\\'
        OR EXISTS (
          SELECT 1
          FROM devices search_device
          WHERE search_device.id = d.device_id
            AND search_device.display_name LIKE ? ESCAPE '\\'
        )
        OR EXISTS (
          SELECT 1
          FROM telemetry_indexes search_telemetry
          WHERE search_telemetry.drive_id = d.id
            AND (
              json_extract(
                search_telemetry.manifest_json,
                '$.vehicle.car_fingerprint'
              ) LIKE ? ESCAPE '\\'
              OR json_extract(
                search_telemetry.manifest_json,
                '$.provenance.source_starpilot_commit'
              ) LIKE ? ESCAPE '\\'
            )
        )
      )
      """,
    )
    parameters.extend((pattern, pattern, pattern, pattern, pattern))
  if device_id:
    clauses.append("d.device_id = ?")
    parameters.append(device_id)
  where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
  catalog_sql = _drive_catalog_sql(
    where,
    readiness=readiness is not None,
  )
  catalog_parameters = [
    *parameters,
    *([readiness] if readiness else []),
  ]
  database = request.app.state.database
  summary = database.query_one(
    f"""
    WITH filtered AS (
      {catalog_sql}
    ),
    unique_objects AS (
      SELECT artifact.object_sha256, MAX(artifact.size) AS size
      FROM artifacts artifact
      JOIN filtered drive ON drive.id = artifact.drive_id
      GROUP BY artifact.object_sha256
    )
    SELECT
      COUNT(*) AS total,
      COALESCE(SUM(duration_us), 0) AS duration_us,
      COALESCE(SUM(
        CASE WHEN computed_readiness = 'importing' THEN 1 ELSE 0 END
      ), 0)
        AS importing_count,
      COALESCE(SUM(
        CASE WHEN computed_readiness = 'processing' THEN 1 ELSE 0 END
      ), 0)
        AS processing_count,
      COALESCE(SUM(
        CASE WHEN computed_readiness = 'ready' THEN 1 ELSE 0 END
      ), 0)
        AS ready_count,
      COALESCE(SUM(
        CASE WHEN computed_readiness = 'partial' THEN 1 ELSE 0 END
      ), 0)
        AS partial_count,
      COALESCE(SUM(
        CASE WHEN computed_readiness = 'failed' THEN 1 ELSE 0 END
      ), 0)
        AS failed_count,
      (SELECT COALESCE(SUM(size), 0) FROM unique_objects) AS stored_bytes
    FROM filtered
    """,
    catalog_parameters,
  )
  assert summary is not None
  rows = database.query_all(
    f"""
    {catalog_sql}
    ORDER BY COALESCE(started_at, created_at) DESC, id DESC
    LIMIT ? OFFSET ?
    """,
    [*catalog_parameters, limit, offset],
  )
  metadata = _drive_metadata(
    database,
    [row["id"] for row in rows],
  )
  return {
    "items": [
      _drive_view(
        row,
        metadata.get(row["id"]),
        raw_video_pruning_required=not request.app.state.settings.retain_raw_video,
      )
      for row in rows
    ],
    "total": summary["total"],
    "limit": limit,
    "offset": offset,
    "summary": {
      "duration_us": summary["duration_us"],
      "stored_bytes": summary["stored_bytes"],
      "by_readiness": {
        "importing": summary["importing_count"],
        "processing": summary["processing_count"],
        "ready": summary["ready_count"],
        "partial": summary["partial_count"],
        "failed": summary["failed_count"],
      },
    },
  }


def _get_drive_row(request: Request, drive_id: str) -> sqlite3.Row:
  row = request.app.state.database.query_one(
    f"{DRIVE_SELECT} WHERE d.id = ? GROUP BY d.id",
    (drive_id,),
  )
  if row is None:
    raise ApiError(404, "drive_not_found", "Drive was not found")
  return row


def _get_drive_detail(request: Request, drive_id: str) -> dict[str, Any]:
  row = _get_drive_row(request, drive_id)
  segment_rows = request.app.state.database.query_all(
    "SELECT * FROM segments WHERE drive_id = ? ORDER BY number",
    (drive_id,),
  )
  artifact_rows = request.app.state.database.query_all(
    """
    SELECT *
    FROM artifacts
    WHERE drive_id = ?
    ORDER BY segment_id, kind, camera, created_at
    """,
    (drive_id,),
  )
  by_segment: dict[str, list[sqlite3.Row]] = {}
  for artifact in artifact_rows:
    if artifact["segment_id"]:
      by_segment.setdefault(artifact["segment_id"], []).append(artifact)
  metadata = _drive_metadata(
    request.app.state.database,
    [drive_id],
  )
  detail = _drive_view(
    row,
    metadata.get(drive_id),
    raw_video_pruning_required=not request.app.state.settings.retain_raw_video,
  )
  route_inventory = latest_inventory_view(
    request.app.state.database,
    drive_id,
  )
  expected_by_segment = (
    segment_expected_streams(
      request.app.state.database,
      drive_id,
      route_inventory["manifest_sha256"],
    )
    if route_inventory is not None
    else {}
  )
  detail["segments"] = [
    _segment_view(
      segment,
      by_segment.get(segment["id"], []),
      expected_by_segment.get(segment["number"], []),
    )
    for segment in segment_rows
  ]
  detail["route_inventory"] = route_inventory
  telemetry = request.app.state.database.query_one(
    """
    SELECT
      state, schema_version, ndjson_sha256, source_fingerprint,
      manifest_json
    FROM telemetry_indexes
    WHERE drive_id = ?
    """,
    (drive_id,),
  )
  detail["telemetry_generation"] = _telemetry_generation_view(telemetry) if telemetry is not None else None
  with request.app.state.database.connection() as connection:
    connection.execute("BEGIN")
    try:
      eligibility = evaluate_simulation_eligibility(
        connection,
        drive_id,
      )
    finally:
      connection.rollback()
  detail["simulation_eligible"] = eligibility["eligible"]
  detail["simulation_eligibility_reasons"] = eligibility["reasons"]
  return detail


def _telemetry_generation_view(
  row: sqlite3.Row,
) -> dict[str, Any]:
  try:
    manifest = json.loads(row["manifest_json"])
  except (TypeError, json.JSONDecodeError) as exc:
    raise ApiError(
      500,
      "telemetry_index_invalid",
      "Telemetry generation metadata is invalid",
    ) from exc
  if not isinstance(manifest, dict):
    raise ApiError(
      500,
      "telemetry_index_invalid",
      "Telemetry generation metadata is invalid",
    )
  provenance = manifest.get("provenance")
  if not isinstance(provenance, dict):
    provenance = {}
  source_rlogs: list[dict[str, Any]] = []
  source_objects = provenance.get("source_objects")
  if isinstance(source_objects, list):
    for source in source_objects:
      if not isinstance(source, dict) or source.get("log_type") != "rlog":
        continue
      source_rlogs.append(
        {
          key: source[key]
          for key in (
            "segment_num",
            "log_type",
            "sha256",
            "size_bytes",
            "compression",
          )
          if key in source
        }
      )
  bounds = manifest.get("range")
  if not isinstance(bounds, dict):
    bounds = {}
  vehicle = manifest.get("vehicle")
  route_software = manifest.get("route_software")
  completeness = manifest.get("completeness")
  return {
    "state": row["state"],
    "schema_version": row["schema_version"],
    "ndjson_sha256": row["ndjson_sha256"],
    "source_fingerprint": row["source_fingerprint"],
    "timeline_version": manifest.get("timeline_version"),
    "publication_ready": manifest.get("publication_ready") is True,
    "start_t_us": bounds.get("start_us"),
    "end_t_us": bounds.get("end_us"),
    "extractor": provenance.get("extractor"),
    "extractor_version": provenance.get("extractor_version"),
    "source_starpilot_commit": provenance.get(
      "source_starpilot_commit",
    ),
    "source_rlogs": source_rlogs,
    "vehicle": vehicle if isinstance(vehicle, dict) else {},
    "route_software": (route_software if isinstance(route_software, dict) else {}),
    "completeness": (completeness if isinstance(completeness, dict) else {}),
  }


@api.get("/drives/{drive_id}", response_model=DriveDetail, tags=["catalog"])
def get_drive(request: Request, drive_id: str) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  return _get_drive_detail(request, drive_id)


@api.get(
  "/drives/{drive_id}/segments",
  response_model=list[SegmentView],
  tags=["catalog"],
)
def drive_segments(request: Request, drive_id: str) -> list[dict[str, Any]]:
  request.app.state.auth.authenticate_admin(request)
  return _get_drive_detail(request, drive_id)["segments"]


@api.get("/segments/{segment_id}", response_model=SegmentView, tags=["catalog"])
def get_segment(request: Request, segment_id: str) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  segment = request.app.state.database.query_one(
    "SELECT * FROM segments WHERE id = ?",
    (segment_id,),
  )
  if segment is None:
    raise ApiError(404, "segment_not_found", "Segment was not found")
  artifacts = request.app.state.database.query_all(
    "SELECT * FROM artifacts WHERE segment_id = ? ORDER BY kind, camera, created_at",
    (segment_id,),
  )
  route_inventory = latest_inventory_view(
    request.app.state.database,
    segment["drive_id"],
  )
  expected = (
    segment_expected_streams(
      request.app.state.database,
      segment["drive_id"],
      route_inventory["manifest_sha256"],
    )
    if route_inventory is not None
    else {}
  )
  return _segment_view(
    segment,
    artifacts,
    expected.get(segment["number"], []),
  )


@api.get("/artifacts", response_model=list[ArtifactView], tags=["catalog"])
def list_artifacts(
  request: Request,
  drive_id: str | None = None,
  segment_id: str | None = None,
  kind: str | None = None,
  limit: int = 200,
) -> list[dict[str, Any]]:
  request.app.state.auth.authenticate_admin(request)
  if limit < 1 or limit > 1000:
    raise ApiError(422, "invalid_limit", "limit must be between 1 and 1000")
  clauses: list[str] = []
  parameters: list[Any] = []
  for column, value in (
    ("drive_id", drive_id),
    ("segment_id", segment_id),
    ("kind", kind),
  ):
    if value:
      clauses.append(f"{column} = ?")
      parameters.append(value)
  where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
  rows = request.app.state.database.query_all(
    f"SELECT * FROM artifacts {where} ORDER BY created_at DESC LIMIT ?",
    [*parameters, limit],
  )
  return [_artifact_view(row) for row in rows]


@api.get("/artifacts/{artifact_id}", response_model=ArtifactView, tags=["catalog"])
def get_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  row = request.app.state.database.query_one(
    "SELECT * FROM artifacts WHERE id = ?",
    (artifact_id,),
  )
  if row is None:
    raise ApiError(404, "artifact_not_found", "Artifact was not found")
  return _artifact_view(row)


def _safe_artifact_path(request: Request, row: sqlite3.Row) -> Path:
  archive_root = request.app.state.settings.archive_root.resolve()
  storage_path = Path(row["storage_path"])
  if storage_path.is_absolute():
    raise ApiError(500, "invalid_storage_path", "Artifact storage path is invalid")
  path = (archive_root / storage_path).resolve()
  if archive_root not in path.parents:
    raise ApiError(500, "invalid_storage_path", "Artifact storage path is invalid")
  if not path.is_file():
    raise ApiError(404, "artifact_content_missing", "Artifact content is missing")
  return path


def _file_chunks(
  path: Path,
  start: int,
  length: int,
  chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
  remaining = length
  with path.open("rb") as source:
    source.seek(start)
    while remaining > 0:
      chunk = source.read(min(chunk_size, remaining))
      if not chunk:
        break
      remaining -= len(chunk)
      yield chunk


_ARTIFACT_CONTENT_SECURITY_POLICY = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox"
_INLINE_ARTIFACT_MEDIA_TYPES = {
  "derived_video": "video/webm",
  "poster": "image/jpeg",
  "thumbnail": "image/jpeg",
  "video_frame_index": "application/json",
  "video_telemetry_sync": "application/json",
}


def _artifact_content_disposition(row: sqlite3.Row) -> str:
  media_type = str(row["mime_type"] or "").partition(";")[0].strip().lower()
  inline = row["source_artifact_id"] is not None and _INLINE_ARTIFACT_MEDIA_TYPES.get(row["kind"]) == media_type
  disposition = "inline" if inline else "attachment"
  raw_filename = str(row["relative_path"] or row["id"]).replace("\\", "/")
  raw_filename = raw_filename.rsplit("/", 1)[-1]
  filename = "".join(character if character.isascii() and (character.isalnum() or character in "._-") else "_" for character in raw_filename[:160])
  if not filename or filename in {".", ".."}:
    filename = f"artifact-{row['id']}"
  return f'{disposition}; filename="{filename}"'


def _media_response(request: Request, row: sqlite3.Row) -> StreamingResponse:
  path = _safe_artifact_path(request, row)
  size = path.stat().st_size
  media_type = row["mime_type"] or "application/octet-stream"
  range_header = request.headers.get("range")
  headers = {
    "Accept-Ranges": "bytes",
    "Cache-Control": "private, no-store",
    "Content-Disposition": _artifact_content_disposition(row),
    "Content-Security-Policy": _ARTIFACT_CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
  }
  if not range_header:
    headers["Content-Length"] = str(size)
    return StreamingResponse(
      _file_chunks(path, 0, size),
      media_type=media_type,
      headers=headers,
    )
  if not range_header.startswith("bytes=") or "," in range_header:
    raise ApiError(
      416,
      "invalid_range",
      "Only one byte range is supported",
      headers={"Content-Range": f"bytes */{size}"},
    )
  value = range_header.removeprefix("bytes=")
  first, separator, last = value.partition("-")
  if not separator:
    raise ApiError(
      416,
      "invalid_range",
      "Byte range is invalid",
      headers={"Content-Range": f"bytes */{size}"},
    )
  try:
    if first:
      start = int(first)
      end = int(last) if last else size - 1
    else:
      suffix_length = int(last)
      if suffix_length <= 0:
        raise ValueError
      start = max(0, size - suffix_length)
      end = size - 1
  except ValueError as exc:
    raise ApiError(
      416,
      "invalid_range",
      "Byte range is invalid",
      headers={"Content-Range": f"bytes */{size}"},
    ) from exc
  if start < 0 or start >= size or end < start:
    raise ApiError(
      416,
      "range_not_satisfiable",
      "Byte range is outside the artifact",
      headers={"Content-Range": f"bytes */{size}"},
    )
  end = min(end, size - 1)
  length = end - start + 1
  headers["Content-Range"] = f"bytes {start}-{end}/{size}"
  headers["Content-Length"] = str(length)
  return StreamingResponse(
    _file_chunks(path, start, length),
    status_code=status.HTTP_206_PARTIAL_CONTENT,
    media_type=media_type,
    headers=headers,
  )


@api.get("/artifacts/{artifact_id}/content", tags=["media"])
def artifact_content(request: Request, artifact_id: str) -> StreamingResponse:
  request.app.state.auth.authenticate_admin(request)
  row = request.app.state.database.query_one(
    "SELECT * FROM artifacts WHERE id = ?",
    (artifact_id,),
  )
  if row is None:
    raise ApiError(404, "artifact_not_found", "Artifact was not found")
  if row["status"] not in {"stored", "verified", "ready"}:
    raise ApiError(409, "artifact_not_ready", "Artifact content is not ready")
  return _media_response(request, row)


@api.get("/drives/{drive_id}/media", tags=["media"])
def drive_media(
  request: Request,
  drive_id: str,
  camera: str = "road",
  segment: int | None = Query(default=None, ge=0),
) -> StreamingResponse:
  request.app.state.auth.authenticate_admin(request)
  parameters: list[Any] = [drive_id, camera]
  segment_clause = ""
  if segment is not None:
    segment_clause = "AND s.number = ?"
    parameters.append(segment)
  row = request.app.state.database.query_one(
    f"""
    SELECT a.*
    FROM artifacts a
    JOIN segments s ON s.id = a.segment_id
    WHERE a.drive_id = ?
      AND a.camera = ?
      AND a.status = 'ready'
      AND (a.codec LIKE '%av1%' OR a.kind IN ('derived_video', 'video_av1'))
      {segment_clause}
    ORDER BY s.number, a.created_at DESC
    LIMIT 1
    """,
    parameters,
  )
  if row is None:
    raise ApiError(
      409,
      "media_not_ready",
      "No validated AV1 backup is ready for this drive and camera",
    )
  return _media_response(request, row)


@api.get(
  "/drives/{drive_id}/media-manifest",
  response_model=MediaManifest,
  tags=["media"],
)
def media_manifest(
  request: Request,
  drive_id: str,
  camera: str = "road",
) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  _get_drive_row(request, drive_id)
  rows = request.app.state.database.query_all(
    """
    SELECT
      a.id AS artifact_id,
      s.number AS segment_number,
      s.start_t_us,
      COALESCE(a.duration_us, s.duration_us) AS duration_us,
      a.mime_type,
      a.codec,
      a.fps
    FROM artifacts a
    JOIN segments s ON s.id = a.segment_id
    WHERE a.drive_id = ?
      AND a.camera = ?
      AND a.status = 'ready'
      AND a.kind = 'derived_video'
      AND LOWER(a.codec) = 'av1'
    ORDER BY s.number, a.created_at DESC, a.id DESC
    """,
    (drive_id, camera),
  )
  deduplicated: list[dict[str, Any]] = []
  seen_segments: set[int] = set()
  for row in rows:
    if row["segment_number"] in seen_segments:
      continue
    seen_segments.add(row["segment_number"])
    sync = inspect_media_sync(
      request,
      drive_id,
      camera,
      row["segment_number"],
      include_points=False,
    )
    exact = sync["ready"] and sync["video_artifact_id"] == row["artifact_id"]
    segment_window = sync.get("segment_window") if exact and isinstance(sync.get("segment_window"), dict) else None
    item = {
      "segment_number": row["segment_number"],
      "start_t_us": (segment_window.get("start_t_us") if segment_window is not None else row["start_t_us"]),
      "duration_us": (segment_window.get("duration_us") if segment_window is not None else row["duration_us"]),
      "artifact_id": row["artifact_id"],
      "url": f"/api/v1/artifacts/{row['artifact_id']}/content",
      "mime_type": row["mime_type"],
      "codec": row["codec"],
      "fps": row["fps"],
      "sync_mode": "exact" if exact else "approximate",
      "sync_url": None,
      "frame_index_artifact_id": None,
      "frame_index_url": None,
      "sync_reason": (None if exact else "video_generation_changed" if sync["ready"] else sync["reason"]),
      "video_sha256": sync.get("video_sha256") if exact else None,
      "timeline_origin": sync.get("timeline_origin") if exact else None,
    }
    if exact:
      query = urlencode(
        {
          "camera": camera,
          "segment": row["segment_number"],
          "telemetry_sha256": sync["telemetry_sha256"],
          "frame_index_sha256": sync["frame_index_sha256"],
          "frame_index_artifact_id": sync["frame_index_artifact_id"],
          "video_sha256": sync["video_sha256"],
          "video_artifact_id": sync["video_artifact_id"],
        }
      )
      item["sync_url"] = f"/api/v1/drives/{drive_id}/media-sync?{query}"
      item["frame_index_artifact_id"] = sync["frame_index_artifact_id"]
      item["frame_index_url"] = f"/api/v1/artifacts/{sync['frame_index_artifact_id']}/content"
    deduplicated.append(item)
  return {
    "drive_id": drive_id,
    "camera": camera,
    "synchronized": bool(deduplicated) and all(item["sync_mode"] == "exact" for item in deduplicated),
    "items": deduplicated,
  }


@api.get(
  "/simulator/capabilities",
  response_model=SimulatorCapabilities,
  tags=["simulator"],
)
def simulator_capabilities(request: Request) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  rows = request.app.state.database.query_all(
    """
    SELECT
      sha256, name, enabled, mode,
      parameter_schema_json, metadata_json
    FROM model_registry
    ORDER BY enabled DESC, name, sha256
    """,
  )
  models = []
  eligible_schemas: list[Any] = []
  for row in rows:
    metadata = json.loads(row["metadata_json"])
    eligible = (
      row["enabled"] == 1
      and isinstance(metadata, dict)
      and metadata.get("causal_training_eligible") is True
      and isinstance(metadata.get("capabilities"), dict)
      and metadata["capabilities"].get("available") is True
    )
    models.append(
      {
        "sha256": row["sha256"],
        "name": row["name"],
        "mode": row["mode"],
        "enabled": bool(row["enabled"]),
        "eligible": eligible,
        "metadata": metadata,
      }
    )
    if eligible:
      eligible_schemas.append(
        json.loads(row["parameter_schema_json"]),
      )
  public_schemas = [_public_parameter_schema(schema) for schema in eligible_schemas]
  parameter_schema = (
    public_schemas[0]
    if len(public_schemas) == 1
    else {"oneOf": public_schemas}
    if public_schemas
    else {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "type": "object",
      "properties": {},
      "additionalProperties": False,
    }
  )
  return {
    "available": any(model["eligible"] for model in models),
    "modes": ["approximate_closed_loop"],
    "models": models,
    "history_required_us": 3_000_000,
    "default_horizon_us": 1_000_000,
    "maximum_horizon_us": 2_000_000,
    "parameter_schema": parameter_schema,
  }


@api.get("/models", tags=["simulator"])
def list_models(request: Request) -> list[dict[str, Any]]:
  return simulator_capabilities(request)["models"]


@api.get("/models/{model_hash}/parameters", tags=["simulator"])
def model_parameters(request: Request, model_hash: str) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  row = request.app.state.database.query_one(
    """
    SELECT parameter_schema_json
    FROM model_registry
    WHERE sha256 = ? AND enabled = 1
    """,
    (model_hash.lower(),),
  )
  if row is None:
    raise ApiError(404, "model_not_found", "Enabled model was not found")
  return _public_parameter_schema(
    json.loads(row["parameter_schema_json"]),
  )


@api.post(
  "/drives/{drive_id}/simulations",
  response_model=SimulationAccepted,
  status_code=status.HTTP_202_ACCEPTED,
  tags=["simulator"],
)
def create_simulation(
  request: Request,
  drive_id: str,
  payload: SimulationCreate,
) -> dict[str, Any]:
  auth = request.app.state.auth
  auth.require_origin(request)
  principal = auth.authenticate_admin(request)
  idempotency_key = require_idempotency_key(request)
  canonical = json.dumps(
    {
      "operation": "simulation.create",
      "drive_id": drive_id,
      **payload.model_dump(mode="json"),
    },
    separators=(",", ":"),
    sort_keys=True,
  )
  request_hash = hashlib.sha256(canonical.encode()).hexdigest()
  database = request.app.state.database
  now_text = isoformat()
  with database.transaction(immediate=True) as connection:
    prior_key = connection.execute(
      """
      SELECT request_hash
      FROM idempotency_keys
      WHERE actor_type = 'admin' AND actor_id = 'admin' AND key = ?
      """,
      (idempotency_key,),
    ).fetchone()
    prior = connection.execute(
      """
      SELECT s.id, s.job_id, i.request_hash
      FROM simulation_requests s
      JOIN idempotency_keys i
        ON i.actor_type = 'admin'
        AND i.actor_id = 'admin'
        AND i.key = s.idempotency_key
      WHERE s.idempotency_key = ?
      """,
      (idempotency_key,),
    ).fetchone()
    if prior_key is not None:
      if prior is None or not secrets.compare_digest(prior_key["request_hash"], request_hash):
        raise ApiError(
          409,
          "idempotency_key_reused",
          "Idempotency key was already used with a different simulation",
        )
      return {"id": prior["id"], "job_id": prior["job_id"], "state": "queued"}
    drive = connection.execute(
      "SELECT id FROM drives WHERE id = ?",
      (drive_id,),
    ).fetchone()
    if drive is None:
      raise ApiError(404, "drive_not_found", "Drive was not found")
    model = connection.execute(
      """
      SELECT
        sha256, enabled, mode,
        parameter_schema_json, metadata_json
      FROM model_registry
      WHERE sha256 = ? AND mode = ?
      """,
      (payload.model_hash, payload.mode),
    ).fetchone()
    if model is None:
      raise ApiError(
        422,
        "model_not_allowed",
        "Requested model hash and mode are not registered",
      )
    parameter_schema = json.loads(model["parameter_schema_json"])
    _validate_parameters(parameter_schema, payload.parameters)
    eligibility = evaluate_simulation_eligibility(
      connection,
      drive_id,
      model_hash=payload.model_hash,
      mode=payload.mode,
      t_us=payload.t_us,
      horizon_us=payload.horizon_us,
    )
    if not eligibility["eligible"]:
      first_reason = eligibility["reasons"][0]
      raise ApiError(
        409,
        first_reason["code"],
        first_reason["message"],
        details={
          **first_reason.get("details", {}),
          "reasons": eligibility["reasons"],
          "telemetry_generation": eligibility.get(
            "telemetry_generation",
          ),
        },
      )
    telemetry_generation = eligibility["telemetry_generation"]
    requested_generation = {
      "ndjson_sha256": payload.telemetry_sha256,
      "timeline_version": payload.timeline_version,
    }
    if requested_generation != telemetry_generation:
      raise ApiError(
        409,
        "telemetry_generation_changed",
        "The selected telemetry generation is no longer current",
        details={
          "requested": requested_generation,
          "current": telemetry_generation,
        },
      )
    baseline_params = eligibility.get("baseline_params")
    if not isinstance(baseline_params, dict):
      raise ApiError(
        500,
        "simulation_eligibility_invalid",
        "Simulation eligibility omitted its pinned route baseline",
      )
    _validate_parameters(parameter_schema, baseline_params)
    simulation_id = uuid4().hex
    job_id = uuid4().hex
    job_payload = {
      "simulation_id": simulation_id,
      "drive_id": drive_id,
      "t_us": payload.t_us,
      "horizon_us": payload.horizon_us,
      "model_hash": payload.model_hash,
      "mode": payload.mode,
      "baseline_params": baseline_params,
      "candidate_params": payload.parameters,
      "telemetry_sha256": telemetry_generation["ndjson_sha256"],
      "timeline_version": telemetry_generation["timeline_version"],
    }
    connection.execute(
      """
      INSERT INTO idempotency_keys(
        actor_type, actor_id, key, request_hash, created_at
      ) VALUES ('admin', 'admin', ?, ?, ?)
      """,
      (idempotency_key, request_hash, now_text),
    )
    connection.execute(
      """
      INSERT INTO jobs(
        id, type, state, payload_json, created_at, updated_at
      ) VALUES (?, 'simulate_counterfactual', 'queued', ?, ?, ?)
      """,
      (
        job_id,
        json.dumps(job_payload, separators=(",", ":"), sort_keys=True),
        now_text,
        now_text,
      ),
    )
    connection.execute(
      """
      INSERT INTO simulation_requests(
        id, drive_id, job_id, t_us, horizon_us, model_hash,
        mode, parameters_json, baseline_parameters_json, telemetry_sha256,
        idempotency_key, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        simulation_id,
        drive_id,
        job_id,
        payload.t_us,
        payload.horizon_us,
        payload.model_hash,
        payload.mode,
        json.dumps(payload.parameters, separators=(",", ":"), sort_keys=True),
        json.dumps(baseline_params, separators=(",", ":"), sort_keys=True),
        telemetry_generation["ndjson_sha256"],
        idempotency_key,
        now_text,
      ),
    )
    audit(
      connection,
      actor_type=principal.actor_type,
      actor_id=principal.actor_id,
      action="simulation.create",
      resource_type="simulation",
      resource_id=simulation_id,
      details={
        "drive_id": drive_id,
        "job_id": job_id,
        "model_hash": payload.model_hash,
        "mode": payload.mode,
        "telemetry_sha256": telemetry_generation["ndjson_sha256"],
        "timeline_version": telemetry_generation["timeline_version"],
      },
      ip_address=request_ip(request),
    )
  return {"id": simulation_id, "job_id": job_id, "state": "queued"}


@api.get("/jobs", response_model=JobList, tags=["jobs"])
def list_jobs(
  request: Request,
  q: str | None = None,
  state: str | None = None,
  job_type: str | None = Query(default=None, alias="type"),
  upload_id: str | None = None,
  drive_id: str | None = None,
  limit: int = Query(default=100, ge=1, le=500),
  offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  valid_states = {
    "queued",
    "leased",
    "running",
    "succeeded",
    "failed",
    "canceled",
  }
  if state is not None and state not in valid_states:
    raise ApiError(422, "invalid_job_state", "Job state is invalid")
  if job_type is not None and (not job_type or len(job_type) > 128):
    raise ApiError(422, "invalid_job_type", "Job type is invalid")
  clauses: list[str] = []
  parameters: list[Any] = []
  if q is not None:
    query = q.strip()
    if not query or len(query) > 256:
      raise ApiError(
        422,
        "invalid_query",
        "q must contain between 1 and 256 characters",
      )
    escaped = query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    clauses.append(
      """
      (
        LOWER(j.id) LIKE ? ESCAPE '\\'
        OR LOWER(j.type) LIKE ? ESCAPE '\\'
        OR LOWER(COALESCE(j.error, '')) LIKE ? ESCAPE '\\'
        OR LOWER(COALESCE(
          CAST(json_extract(j.payload_json, '$.artifact_id') AS TEXT),
          ''
        )) LIKE ? ESCAPE '\\'
        OR LOWER(COALESCE(
          CAST(json_extract(j.payload_json, '$.upload_id') AS TEXT),
          (
            SELECT search_upload.id
            FROM uploads search_upload
            WHERE search_upload.artifact_id = json_extract(
              j.payload_json,
              '$.artifact_id'
            )
            ORDER BY search_upload.completed_at DESC,
              search_upload.id DESC
            LIMIT 1
          ),
          ''
        )) LIKE ? ESCAPE '\\'
        OR LOWER(COALESCE(
          CAST(json_extract(j.payload_json, '$.drive_id') AS TEXT),
          (
            SELECT search_artifact.drive_id
            FROM artifacts search_artifact
            WHERE search_artifact.id = json_extract(
              j.payload_json,
              '$.artifact_id'
            )
          ),
          ''
        )) LIKE ? ESCAPE '\\'
      )
      """,
    )
    parameters.extend((pattern, pattern, pattern, pattern, pattern, pattern))
  if state is not None:
    clauses.append("j.state = ?")
    parameters.append(state)
  if job_type is not None:
    clauses.append("j.type = ?")
    parameters.append(job_type)
  if drive_id is not None:
    clauses.append(
      """
      (
        json_extract(j.payload_json, '$.drive_id') = ?
        OR EXISTS (
          SELECT 1
          FROM artifacts drive_artifact
          WHERE drive_artifact.id = json_extract(
            j.payload_json,
            '$.artifact_id'
          )
            AND drive_artifact.drive_id = ?
        )
      )
      """,
    )
    parameters.extend((drive_id, drive_id))
  if upload_id is not None:
    clauses.append(
      """
      EXISTS (
        SELECT 1
        FROM uploads job_upload
        WHERE job_upload.id = ?
          AND (
            json_extract(j.payload_json, '$.upload_id') = job_upload.id
            OR json_extract(j.payload_json, '$.artifact_id')
              = job_upload.artifact_id
          )
      )
      """,
    )
    parameters.append(upload_id)
  where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
  database = request.app.state.database
  total = database.query_one(
    f"SELECT COUNT(*) AS count FROM jobs j {where}",
    parameters,
  )
  rows = database.query_all(
    f"""
    {JOB_SELECT}
    {where}
    ORDER BY j.created_at DESC, j.id DESC
    LIMIT ? OFFSET ?
    """,
    [*parameters, limit, offset],
  )
  return {
    "items": [_job_view(row) for row in rows],
    "total": total["count"] if total is not None else 0,
  }


@api.get("/jobs/{job_id}", response_model=JobView, tags=["jobs"])
def get_job(request: Request, job_id: str) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  row = request.app.state.database.query_one(
    f"{JOB_SELECT} WHERE j.id = ?",
    (job_id,),
  )
  if row is None:
    raise ApiError(404, "job_not_found", "Job was not found")
  return _job_view(row)


@api.post(
  "/jobs/{job_id}/cancel",
  response_model=JobView,
  tags=["jobs"],
)
def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
  auth = request.app.state.auth
  auth.require_origin(request)
  principal = auth.authenticate_admin(request)
  database = request.app.state.database
  job = request_cancellation(database, job_id)
  if job is None:
    raise ApiError(404, "job_not_found", "Job was not found")
  row = database.query_one(
    f"{JOB_SELECT} WHERE j.id = ?",
    (job_id,),
  )
  assert row is not None
  with database.transaction(immediate=True) as connection:
    audit(
      connection,
      actor_type=principal.actor_type,
      actor_id=principal.actor_id,
      action="job.cancel",
      resource_type="job",
      resource_id=job_id,
      details={"state": row["state"]},
      ip_address=request_ip(request),
    )
  return _job_view(row)


@api.post(
  "/jobs/{job_id}/retry",
  response_model=JobView,
  status_code=status.HTTP_202_ACCEPTED,
  tags=["jobs"],
)
def retry_job(request: Request, job_id: str) -> dict[str, Any]:
  auth = request.app.state.auth
  auth.require_origin(request)
  principal = auth.authenticate_admin(request)
  idempotency_key = require_idempotency_key(request)
  canonical = json.dumps(
    {"operation": "job.retry", "job_id": job_id},
    separators=(",", ":"),
    sort_keys=True,
  )
  request_hash = hashlib.sha256(canonical.encode()).hexdigest()
  database = request.app.state.database
  now_text = isoformat()
  retry_job_id: str | None = None

  with database.transaction(immediate=True) as connection:
    prior = connection.execute(
      """
      SELECT request_hash, response_json
      FROM idempotency_keys
      WHERE actor_type = ? AND actor_id = ? AND key = ?
      """,
      (
        principal.actor_type,
        principal.actor_id,
        idempotency_key,
      ),
    ).fetchone()
    if prior is not None:
      if not secrets.compare_digest(prior["request_hash"], request_hash):
        raise ApiError(
          409,
          "idempotency_key_reused",
          "Idempotency key was already used with a different request",
        )
      try:
        prior_response = json.loads(prior["response_json"] or "{}")
        retry_job_id = prior_response["job_id"]
      except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ApiError(
          500,
          "idempotency_record_invalid",
          "The stored retry response is invalid",
        ) from exc
      if not isinstance(retry_job_id, str):
        raise ApiError(
          500,
          "idempotency_record_invalid",
          "The stored retry response is invalid",
        )
    else:
      original = connection.execute(
        "SELECT * FROM jobs WHERE id = ?",
        (job_id,),
      ).fetchone()
      if original is None:
        raise ApiError(404, "job_not_found", "Job was not found")
      if original["state"] != "failed":
        raise ApiError(
          409,
          "job_not_failed",
          "Only a failed job can be retried",
          details={"state": original["state"]},
        )
      if original["retryable"] != 1:
        raise ApiError(
          409,
          "job_not_retryable",
          "This job failed permanently and cannot be retried safely",
        )

      active_retry = connection.execute(
        """
        SELECT id
        FROM jobs
        WHERE json_extract(payload_json, '$.retry_of_job_id') = ?
          AND state IN ('queued', 'leased', 'running')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (job_id,),
      ).fetchone()
      if active_retry is not None:
        raise ApiError(
          409,
          "job_retry_already_active",
          "A retry of this job is already active",
          details={"job_id": active_retry["id"]},
        )

      payload = json.loads(original["payload_json"])
      if not isinstance(payload, dict):
        raise ApiError(
          500,
          "job_payload_invalid",
          "The stored job payload is invalid",
        )
      payload["retry_of_job_id"] = job_id
      payload["retry_root_job_id"] = payload.get(
        "retry_root_job_id",
        job_id,
      )
      retry_job_id = uuid4().hex
      connection.execute(
        """
        INSERT INTO jobs(
          id, type, state, priority, payload_json, dedupe_key,
          max_attempts, available_at, created_at, updated_at
        ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
          retry_job_id,
          original["type"],
          original["priority"],
          json.dumps(payload, separators=(",", ":"), sort_keys=True),
          original["dedupe_key"],
          original["max_attempts"],
          now_text,
          now_text,
          now_text,
        ),
      )
      if original["type"] == "simulate_counterfactual":
        updated = connection.execute(
          """
          UPDATE simulation_requests
          SET job_id = ?
          WHERE job_id = ?
          """,
          (retry_job_id, job_id),
        )
        if updated.rowcount != 1:
          raise ApiError(
            409,
            "job_retry_superseded",
            "This simulation job is no longer the active attempt",
          )
      response_json = json.dumps(
        {"job_id": retry_job_id},
        separators=(",", ":"),
        sort_keys=True,
      )
      connection.execute(
        """
        INSERT INTO idempotency_keys(
          actor_type, actor_id, key, request_hash, status_code,
          response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
          principal.actor_type,
          principal.actor_id,
          idempotency_key,
          request_hash,
          status.HTTP_202_ACCEPTED,
          response_json,
          now_text,
        ),
      )
      audit(
        connection,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        action="job.retry",
        resource_type="job",
        resource_id=retry_job_id,
        details={
          "retry_of_job_id": job_id,
          "type": original["type"],
        },
        ip_address=request_ip(request),
      )

  assert retry_job_id is not None
  row = database.query_one(
    f"{JOB_SELECT} WHERE j.id = ?",
    (retry_job_id,),
  )
  if row is None:
    raise ApiError(
      500,
      "job_retry_missing",
      "The queued retry could not be loaded",
    )
  return _job_view(row)


@api.get(
  "/simulations/{simulation_id}",
  response_model=SimulationView,
  tags=["simulator"],
)
def get_simulation(request: Request, simulation_id: str) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  row = request.app.state.database.query_one(
    """
    SELECT
      s.*, j.state, j.progress, j.result_json, j.error,
      j.payload_json AS job_payload_json
    FROM simulation_requests s
    JOIN jobs j ON j.id = s.job_id
    WHERE s.id = ?
    """,
    (simulation_id,),
  )
  if row is None:
    raise ApiError(404, "simulation_not_found", "Simulation was not found")
  try:
    job_payload = json.loads(row["job_payload_json"])
  except json.JSONDecodeError:
    job_payload = {}
  return {
    "id": row["id"],
    "drive_id": row["drive_id"],
    "job_id": row["job_id"],
    "t_us": row["t_us"],
    "horizon_us": row["horizon_us"],
    "model_hash": row["model_hash"],
    "mode": row["mode"],
    "parameters": json.loads(row["parameters_json"]),
    "baseline_parameters": json.loads(
      row["baseline_parameters_json"],
    ),
    "telemetry_sha256": row["telemetry_sha256"],
    "timeline_version": (job_payload.get("timeline_version") if isinstance(job_payload, dict) else None),
    "state": row["state"],
    "progress": row["progress"],
    "result": json.loads(row["result_json"] or "{}"),
    "error": row["error"],
    "created_at": row["created_at"],
  }


def _audit_events(
  request: Request,
  limit: int,
  before_id: int | None,
) -> list[dict[str, Any]]:
  request.app.state.auth.authenticate_admin(request)
  if limit < 1 or limit > 500:
    raise ApiError(422, "invalid_limit", "limit must be between 1 and 500")
  where = "WHERE id < ?" if before_id is not None else ""
  parameters: list[Any] = [before_id] if before_id is not None else []
  parameters.append(limit)
  rows = request.app.state.database.query_all(
    f"""
    SELECT *
    FROM audit_events
    {where}
    ORDER BY id DESC
    LIMIT ?
    """,
    parameters,
  )
  return [
    {
      "id": row["id"],
      "actor_type": row["actor_type"],
      "actor_id": row["actor_id"],
      "action": row["action"],
      "resource_type": row["resource_type"],
      "resource_id": row["resource_id"],
      "details": json.loads(row["details_json"]),
      "ip_address": row["ip_address"],
      "created_at": row["created_at"],
    }
    for row in rows
  ]


@api.get("/audit-events", response_model=list[AuditEventView], tags=["audit"])
@api.get("/activity", response_model=list[AuditEventView], tags=["audit"])
def audit_events(
  request: Request,
  limit: int = 100,
  before_id: int | None = None,
) -> list[dict[str, Any]]:
  return _audit_events(request, limit, before_id)


def create_app(settings: Settings | None = None) -> FastAPI:
  resolved = settings or Settings.from_env()
  resolved.validate()
  database = Database(resolved.database_path)
  auth_service = AuthService(resolved, database)

  @asynccontextmanager
  async def lifespan(application: FastAPI):
    ensure_storage_directories(resolved.archive_root, resolved.session_dir)
    database.initialize()
    application.state.database = database
    application.state.settings = resolved
    application.state.auth = auth_service
    bootstrap_configured_devices(application)
    database.execute(
      """
      UPDATE admin_sessions
      SET revoked_at = COALESCE(revoked_at, ?)
      WHERE expires_at <= ? AND revoked_at IS NULL
      """,
      (isoformat(), isoformat()),
    )
    yield
    database.checkpoint()

  application = FastAPI(
    title="Comma Companion API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
  )
  application.state.database = database
  application.state.settings = resolved
  application.state.auth = auth_service
  application.add_middleware(
    CORSMiddleware,
    allow_origins=list(resolved.allowed_origins),
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "PATCH"],
    allow_headers=[
      "Authorization",
      "Content-Type",
      "Idempotency-Key",
      "Upload-Checksum",
      "Upload-Length",
      "Upload-Offset",
    ],
    expose_headers=[
      "Content-Length",
      "Content-Range",
      "Upload-Length",
      "Upload-Offset",
      "Upload-Durable",
      "Upload-Retry-Action",
      "Upload-SHA256",
      "Upload-State",
      "Upload-Terminal",
    ],
  )

  @application.exception_handler(ApiError)
  async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
      status_code=exc.status_code,
      content=_error_content(exc.code, exc.message, exc.details),
      headers=exc.headers,
    )

  @application.exception_handler(RequestValidationError)
  async def validation_error_handler(
    _: Request,
    exc: RequestValidationError,
  ) -> JSONResponse:
    return JSONResponse(
      status_code=422,
      content=_error_content(
        "validation_error",
        "Request validation failed",
        jsonable_encoder(exc.errors()),
      ),
    )

  @application.middleware("http")
  async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
      "Content-Security-Policy",
      "default-src 'none'; frame-ancestors 'none'",
    )
    if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
      response.headers["Cache-Control"] = "no-store"
    return response

  application.add_middleware(
    RequestBodyLimitMiddleware,
    json_max_bytes=resolved.max_json_body_bytes,
    upload_patch_max_bytes=resolved.max_chunk_bytes,
  )

  application.include_router(api)
  application.include_router(devices_router, prefix="/api/v1")
  application.include_router(commands_router, prefix="/api/v1")
  application.include_router(uploads_router, prefix="/api/v1")
  application.include_router(inventory_router, prefix="/api/v1")
  application.include_router(telemetry_router, prefix="/api/v1")
  application.include_router(media_sync_router, prefix="/api/v1")
  if resolved.web_root is not None:
    mount_spa(application, resolved.web_root)
  return application


app = create_app()


def hash_password_cli() -> None:
  if sys.stdin.isatty():
    password = getpass.getpass("Administrator password: ")
  else:
    password = sys.stdin.readline().rstrip("\r\n")
  if not password:
    raise SystemExit("password must not be empty")
  print(
    PasswordHasher(
      time_cost=3,
      memory_cost=65_536,
      parallelism=2,
      hash_len=32,
      salt_len=16,
    ).hash(password)
  )


def run() -> None:
  uvicorn.run(
    "comma_companion.app:app",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8000")),
    proxy_headers=True,
    forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
  )
