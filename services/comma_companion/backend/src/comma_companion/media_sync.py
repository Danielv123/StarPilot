from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response

from .auth import ApiError
from .db import Database
from .telemetry import SQLITE_INT_MAX, TelemetryRecordLoader


FRAME_INDEX_SCHEMA_VERSION = 1
MAX_FRAME_INDEX_BYTES = 64 * 1024 * 1024
JS_SAFE_INT_MAX = (2**53) - 1
VALID_CAMERAS = {"road", "wide", "driver", "qcamera", "unknown"}
# Media sync rehashes every referenced frame record. The full NDJSON digest is
# an ingestion provenance pin; full replay eligibility verifies the whole file.
INTEGRITY_SCOPE = "referenced_frame_rows"
FRAME_INDEX_KEYS = {
  "schema_version",
  "mapping_type",
  "join_key",
  "ordinal_basis",
  "source_frame_key",
  "camera",
  "segment_num",
  "source_artifact_id",
  "video",
  "time_base",
  "frame_count",
  "first_pts",
  "last_end_pts",
  "duration_inference_count",
  "frames",
}
FRAME_KEYS = {
  "ordinal",
  "segment_frame_id",
  "pts",
  "duration",
  "pts_us",
  "duration_us",
  "keyframe",
}

router = APIRouter(prefix="/drives", tags=["media"])


class _SyncNotReady(Exception):
  def __init__(self, reason: str):
    super().__init__(reason)
    self.reason = reason


@dataclass(frozen=True, slots=True)
class _TargetSegmentProof:
  camera_start_t_us: int
  camera_end_t_us: int
  timeline_origin_ns: int


def _not_ready(reason: str) -> None:
  raise _SyncNotReady(reason)


def _valid_sha256(value: Any) -> bool:
  return isinstance(value, str) and len(value) == 64 and value == value.lower() and all(character in "0123456789abcdef" for character in value)


def _strict_json_loads(value: str) -> Any:
  def reject_constant(constant: str) -> None:
    raise ValueError(f"non-finite JSON constant: {constant}")

  return json.loads(value, parse_constant=reject_constant)


def _safe_int(value: Any, *, minimum: int = -JS_SAFE_INT_MAX) -> bool:
  return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= JS_SAFE_INT_MAX


def _decimal_ns(value: Any, *, positive: bool) -> int | None:
  if not isinstance(value, str) or not value or not value.isascii() or not value.isdecimal():
    return None
  parsed = int(value)
  minimum = 1 if positive else 0
  return parsed if minimum <= parsed <= (2**64) - 1 else None


def _ticks_to_microseconds(
  value: int,
  numerator: int,
  denominator: int,
) -> int:
  scaled = value * numerator * 1_000_000
  if scaled >= 0:
    return ((scaled * 2) + denominator) // (denominator * 2)
  positive = -scaled
  return -(((positive * 2) + denominator) // (denominator * 2))


def _resolved_archive_file(
  archive_root: Path,
  storage_path: Any,
  *,
  required_root: str | None,
) -> Path:
  if not isinstance(storage_path, str) or not storage_path or "\\" in storage_path:
    _not_ready("artifact_path_invalid")
  relative = PurePosixPath(storage_path)
  if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != storage_path:
    _not_ready("artifact_path_invalid")
  try:
    resolved_archive = archive_root.resolve()
    expected_root = (
      resolved_archive
      if required_root is None
      else resolved_archive / required_root
    )
    resolved_root = expected_root.resolve()
  except (OSError, RuntimeError, ValueError):
    _not_ready("artifact_path_invalid")
  if resolved_root != expected_root:
    _not_ready("artifact_path_invalid")
  try:
    absolute = (resolved_archive / Path(*relative.parts)).resolve()
  except (OSError, RuntimeError, ValueError):
    _not_ready("artifact_path_invalid")
  if absolute == resolved_root or not absolute.is_relative_to(resolved_root):
    _not_ready("artifact_path_invalid")
  try:
    with absolute.open("rb") as stream:
      if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        _not_ready("artifact_content_missing")
  except (OSError, RuntimeError, ValueError):
    _not_ready("artifact_content_missing")
  return absolute


def _read_frame_index(
  archive_root: Path,
  frame_artifact: sqlite3.Row,
) -> tuple[dict[str, Any], str]:
  digest = frame_artifact["object_sha256"]
  size = frame_artifact["size"]
  if (
    not _valid_sha256(digest)
    or not isinstance(size, int)
    or isinstance(size, bool)
    or not 1 <= size <= MAX_FRAME_INDEX_BYTES
    or frame_artifact["object_size"] != size
    or frame_artifact["object_storage_path"] != frame_artifact["storage_path"]
    or frame_artifact["mime_type"] != "application/json"
  ):
    _not_ready("frame_index_invalid")
  path = _resolved_archive_file(
    archive_root,
    frame_artifact["storage_path"],
    required_root=None,
  )
  try:
    content = path.read_bytes()
  except OSError:
    _not_ready("frame_index_invalid")
  if (
    len(content) != size
    or not content.startswith(b"{")
    or not content.endswith(b"}\n")
    or b"\n" in content[:-1]
    or b"\r" in content
    or hashlib.sha256(content).hexdigest() != digest
  ):
    _not_ready("frame_index_invalid")
  try:
    document = _strict_json_loads(content[:-1].decode("utf-8"))
  except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
    _not_ready("frame_index_invalid")
  if not isinstance(document, dict):
    _not_ready("frame_index_invalid")
  return document, digest


def _validate_video(
  archive_root: Path,
  video: sqlite3.Row,
) -> tuple[str, Path]:
  digest = video["object_sha256"]
  size = video["size"]
  frame_count = video["frame_count"]
  if (
    not _valid_sha256(digest)
    or not isinstance(size, int)
    or isinstance(size, bool)
    or size <= 0
    or video["object_size"] != size
    or video["object_storage_path"] != video["storage_path"]
    or video["kind"] != "derived_video"
    or video["status"] != "ready"
    or str(video["codec"]).lower() != "av1"
    or not isinstance(frame_count, int)
    or isinstance(frame_count, bool)
    or frame_count <= 0
  ):
    _not_ready("video_invalid")
  path = _resolved_archive_file(
    archive_root,
    video["storage_path"],
    required_root=None,
  )
  try:
    with path.open("rb") as stream:
      file_size = os.fstat(stream.fileno()).st_size
      file_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    if file_size != size or file_sha256 != digest:
      _not_ready("video_invalid")
  except (OSError, RuntimeError, ValueError):
    _not_ready("video_invalid")
  return digest, path


def _validate_frame_document(
  document: dict[str, Any],
  frame_artifact: sqlite3.Row,
  video: sqlite3.Row,
  video_sha256: str,
  camera: str,
  segment: int,
) -> list[dict[str, Any]]:
  frames = document.get("frames")
  video_identity = document.get("video")
  time_base = document.get("time_base")
  if (
    set(document) != FRAME_INDEX_KEYS
    or not _safe_int(document.get("schema_version"), minimum=1)
    or document["schema_version"] != FRAME_INDEX_SCHEMA_VERSION
    or document.get("mapping_type") != "encoded_frame_pts"
    or document.get("join_key") != ["camera", "segment_num", "segment_frame_id"]
    or not _safe_int(document.get("ordinal_basis"), minimum=0)
    or document["ordinal_basis"] != 0
    or document.get("source_frame_key") != "segment_frame_id"
    or document.get("camera") not in VALID_CAMERAS
    or document.get("camera") != camera
    or not _safe_int(document.get("segment_num"), minimum=0)
    or document["segment_num"] != segment
    or document.get("source_artifact_id") != video["source_artifact_id"]
    or not _safe_int(document.get("frame_count"), minimum=1)
    or document.get("frame_count") != video["frame_count"]
    or document.get("frame_count") != frame_artifact["frame_count"]
    or not isinstance(video_identity, Mapping)
    or set(video_identity) != {"path", "sha256"}
    or not isinstance(video_identity.get("path"), str)
    or not video_identity["path"]
    or video_identity.get("sha256") != video_sha256
    or not isinstance(time_base, Mapping)
    or set(time_base) != {"numerator", "denominator", "text"}
    or not _safe_int(time_base.get("numerator"), minimum=1)
    or not _safe_int(time_base.get("denominator"), minimum=1)
    or time_base.get("text") != f"{time_base.get('numerator')}/{time_base.get('denominator')}"
    or not isinstance(frames, list)
    or not frames
    or len(frames) != document.get("frame_count")
    or not _safe_int(document.get("first_pts"))
    or not _safe_int(document.get("last_end_pts"))
    or not _safe_int(document.get("duration_inference_count"), minimum=0)
    or document["duration_inference_count"] > document["frame_count"]
  ):
    _not_ready("frame_index_invalid")
  try:
    embedded_video_path = Path(video_identity["path"])
    # This is producer identity metadata, not an authorized read path. The
    # artifact/object join above supplies the verified canonical video path.
    if not embedded_video_path.is_absolute():
      _not_ready("frame_index_invalid")
  except (OSError, RuntimeError, ValueError):
    _not_ready("frame_index_invalid")

  numerator = time_base["numerator"]
  denominator = time_base["denominator"]
  reduced_numerator = numerator
  reduced_denominator = denominator
  while reduced_denominator:
    reduced_numerator, reduced_denominator = (
      reduced_denominator,
      reduced_numerator % reduced_denominator,
    )
  if reduced_numerator != 1:
    _not_ready("frame_index_invalid")

  validated: list[dict[str, Any]] = []
  prior_pts_us: int | None = None
  prior_end_pts: int | None = None
  for ordinal, frame in enumerate(frames):
    if (
      not isinstance(frame, dict)
      or set(frame) != FRAME_KEYS
      or not _safe_int(frame.get("ordinal"), minimum=0)
      or frame["ordinal"] != ordinal
      or not _safe_int(frame.get("segment_frame_id"), minimum=0)
      or frame["segment_frame_id"] != ordinal
      or not _safe_int(frame.get("pts"))
      or not _safe_int(frame.get("duration"), minimum=1)
      or not _safe_int(frame.get("pts_us"))
      or not _safe_int(frame.get("duration_us"), minimum=1)
      or not isinstance(frame.get("keyframe"), bool)
      or frame["pts_us"] != _ticks_to_microseconds(frame["pts"], numerator, denominator)
      or frame["duration_us"] != _ticks_to_microseconds(frame["duration"], numerator, denominator)
      or not _safe_int(frame["pts_us"] + frame["duration_us"])
      or (prior_pts_us is not None and frame["pts_us"] <= prior_pts_us)
      or (prior_end_pts is not None and frame["pts"] < prior_end_pts)
    ):
      _not_ready("frame_index_invalid")
    validated.append(frame)
    prior_pts_us = frame["pts_us"]
    prior_end_pts = frame["pts"] + frame["duration"]
  first = validated[0]
  last = validated[-1]
  if first["keyframe"] is not True or document["first_pts"] != first["pts"] or document["last_end_pts"] != last["pts"] + last["duration"]:
    _not_ready("frame_index_invalid")
  return validated


def _target_segment_proof(
  manifest: Mapping[str, Any],
  camera: str,
  segment: int,
) -> _TargetSegmentProof:
  completeness = manifest.get("completeness")
  reports = completeness.get("segments") if isinstance(completeness, Mapping) else None
  if not isinstance(reports, list):
    _not_ready("target_segment_incomplete")

  target: Mapping[str, Any] | None = None
  seen_segments: set[int] = set()
  for report in reports:
    if not isinstance(report, Mapping):
      _not_ready("target_segment_incomplete")
    segment_number = report.get("segment_num")
    if not _safe_int(segment_number, minimum=0) or segment_number in seen_segments:
      _not_ready("target_segment_incomplete")
    seen_segments.add(segment_number)
    if segment_number == segment:
      target = report
  if target is None:
    _not_ready("target_segment_incomplete")

  issue_count = target.get("frame_quality_issue_count")
  camera_ranges = target.get("camera_ranges_us")
  bounds = camera_ranges.get(camera) if isinstance(camera_ranges, Mapping) else None
  if (
    target.get("state") != "complete"
    or not _safe_int(issue_count, minimum=0)
    or issue_count != 0
    or target.get("log_type") not in {"rlog", "qlog"}
    or not isinstance(bounds, list)
    or len(bounds) != 2
    or not _safe_int(bounds[0])
    or not _safe_int(bounds[1])
    or bounds[1] < bounds[0]
  ):
    _not_ready("target_segment_incomplete")

  timebase = manifest.get("timebase")
  timeline_origin_ns = (
    _decimal_ns(
      timebase.get("origin_log_mono_time_ns"),
      positive=False,
    )
    if isinstance(timebase, Mapping)
    else None
  )
  if timeline_origin_ns is None or timebase.get("unit") != "us" or timebase.get("conversion") != "floor((logMonoTime-origin)/1000)":
    _not_ready("target_segment_incomplete")
  return _TargetSegmentProof(
    camera_start_t_us=bounds[0],
    camera_end_t_us=bounds[1],
    timeline_origin_ns=timeline_origin_ns,
  )


def _telemetry_generation(
  connection: sqlite3.Connection,
  drive_id: str,
  camera: str,
  segment: int,
) -> tuple[
  sqlite3.Row,
  str,
  str,
  Literal["stable", "provisional"],
  _TargetSegmentProof,
]:
  index = connection.execute(
    """
    SELECT
      telemetry.schema_version, telemetry.state,
      telemetry.ndjson_path, telemetry.ndjson_sha256,
      telemetry.manifest_json, telemetry.source_fingerprint,
      drive.telemetry_ready,
      inventory.state AS inventory_state,
      inventory.route_closed AS inventory_route_closed,
      inventory.rlog_source_fingerprint
    FROM telemetry_indexes telemetry
    JOIN drives drive ON drive.id = telemetry.drive_id
    LEFT JOIN route_inventories inventory
      ON inventory.id = (
        SELECT latest.id
        FROM route_inventories latest
        WHERE latest.drive_id = telemetry.drive_id
        ORDER BY latest.generation DESC
        LIMIT 1
      )
    WHERE telemetry.drive_id = ?
    """,
    (drive_id,),
  ).fetchone()
  if index is None or index["schema_version"] != 1 or index["state"] not in {"complete", "partial"} or not _valid_sha256(index["ndjson_sha256"]):
    _not_ready("telemetry_not_ready")
  if (
    index["telemetry_ready"] != 1
    or index["inventory_state"] != "complete"
    or index["inventory_route_closed"] != 1
    or not _valid_sha256(index["source_fingerprint"])
    or not _valid_sha256(index["rlog_source_fingerprint"])
    or not secrets.compare_digest(
      index["source_fingerprint"],
      index["rlog_source_fingerprint"],
    )
  ):
    _not_ready("telemetry_inventory_mismatch")
  try:
    manifest = _strict_json_loads(index["manifest_json"])
  except (TypeError, ValueError, json.JSONDecodeError):
    _not_ready("telemetry_not_ready")
  if not isinstance(manifest, dict):
    _not_ready("telemetry_not_ready")
  timeline_version = manifest.get("timeline_version")
  if not _valid_sha256(timeline_version):
    _not_ready("telemetry_not_ready")
  state = index["state"]
  manifest_state = manifest.get("state")
  publication_ready = manifest.get("publication_ready")
  stable = state == "complete" and manifest_state == "complete" and publication_ready is True
  provisional = state == "partial" and manifest_state == "partial" and publication_ready is False
  if not stable and not provisional:
    _not_ready("telemetry_not_ready")
  segment_proof = _target_segment_proof(
    manifest,
    camera,
    segment,
  )
  return (
    index,
    index["ndjson_sha256"],
    timeline_version,
    "stable" if stable else "provisional",
    segment_proof,
  )


def _telemetry_frames(
  connection: sqlite3.Connection,
  archive_root: Path,
  index: sqlite3.Row,
  drive_id: str,
  camera: str,
  segment: int,
  segment_proof: _TargetSegmentProof,
) -> list[dict[str, Any]]:
  rows = connection.execute(
    """
    SELECT *
    FROM telemetry_frame_chunks
    WHERE drive_id = ? AND camera = ? AND segment_number = ?
    ORDER BY chunk_index
    """,
    (drive_id, camera, segment),
  ).fetchall()
  if not rows:
    _not_ready("telemetry_frame_map_missing")

  result: list[dict[str, Any]] = []
  try:
    with TelemetryRecordLoader(
      archive_root,
      index["ndjson_path"],
    ) as records:
      for stored in rows:
        document, referenced = records.load(stored)
        if (
          not referenced
          or not _safe_int(stored["chunk_index"], minimum=0)
          or not isinstance(document, dict)
          or document.get("record") != "frame_chunk"
          or document.get("camera") != camera
          or not _safe_int(document.get("chunk"), minimum=0)
          or document.get("chunk") != stored["chunk_index"]
          or not isinstance(document.get("rows"), list)
        ):
          _not_ready("telemetry_frame_map_invalid")
        for frame in document["rows"]:
          if not isinstance(frame, dict):
            _not_ready("telemetry_frame_map_invalid")
          segment_number = frame.get("segment_num")
          if not _safe_int(segment_number, minimum=0):
            _not_ready("telemetry_frame_map_invalid")
          if segment_number != segment:
            continue
          segment_frame_id = frame.get("segment_frame_id")
          drive_t_us = frame.get("t_us")
          timestamp_sof_ns = _decimal_ns(
            frame.get("timestamp_sof_ns"),
            positive=True,
          )
          timestamp_eof_ns = _decimal_ns(
            frame.get("timestamp_eof_ns"),
            positive=True,
          )
          if (
            frame.get("event_valid") is not True
            or not _safe_int(segment_frame_id, minimum=0)
            or not _safe_int(drive_t_us)
            or timestamp_sof_ns is None
            or timestamp_eof_ns is None
            or timestamp_eof_ns < timestamp_sof_ns
            or drive_t_us != (timestamp_eof_ns - segment_proof.timeline_origin_ns) // 1_000
            or drive_t_us < segment_proof.camera_start_t_us
            or drive_t_us > segment_proof.camera_end_t_us
          ):
            _not_ready("telemetry_frame_map_invalid")
          result.append(
            {
              "segment_frame_id": segment_frame_id,
              "drive_t_us": drive_t_us,
            }
          )
  except (ApiError, OSError, RuntimeError, ValueError):
    _not_ready("telemetry_frame_map_invalid")
  if not result:
    _not_ready("telemetry_frame_map_missing")
  for ordinal, frame in enumerate(result):
    if frame["segment_frame_id"] != ordinal or (ordinal > 0 and frame["drive_t_us"] <= result[ordinal - 1]["drive_t_us"]):
      _not_ready("telemetry_frame_map_invalid")
  if result[0]["drive_t_us"] != segment_proof.camera_start_t_us or result[-1]["drive_t_us"] != segment_proof.camera_end_t_us:
    _not_ready("telemetry_frame_map_invalid")
  return result


def _inspect_media_sync(
  connection: sqlite3.Connection,
  archive_root: Path,
  drive_id: str,
  camera: str,
  segment: int,
  include_points: bool,
) -> dict[str, Any]:
  drive = connection.execute(
    "SELECT id FROM drives WHERE id = ?",
    (drive_id,),
  ).fetchone()
  if drive is None:
    _not_ready("drive_not_found")
  video = connection.execute(
    """
    SELECT
      a.*,
      s.start_t_us AS segment_start_t_us,
      COALESCE(a.duration_us, s.duration_us) AS segment_duration_us,
      o.size AS object_size,
      o.storage_path AS object_storage_path
    FROM artifacts a
    JOIN segments s
      ON s.id = a.segment_id
      AND s.drive_id = a.drive_id
    JOIN objects o ON o.sha256 = a.object_sha256
    WHERE a.drive_id = ?
      AND a.camera = ?
      AND s.number = ?
      AND a.kind = 'derived_video'
      AND a.status = 'ready'
      AND LOWER(a.codec) = 'av1'
    ORDER BY a.created_at DESC, a.id DESC
    LIMIT 1
    """,
    (drive_id, camera, segment),
  ).fetchone()
  if video is None:
    _not_ready("video_not_ready")
  video_sha256, _ = _validate_video(archive_root, video)

  frame_artifact = connection.execute(
    """
    SELECT
      a.*,
      o.size AS object_size,
      o.storage_path AS object_storage_path
    FROM artifacts a
    JOIN objects o ON o.sha256 = a.object_sha256
    WHERE a.drive_id = ?
      AND a.segment_id = ?
      AND a.camera = ?
      AND a.kind = 'video_frame_index'
      AND a.status = 'ready'
      AND a.source_artifact_id = ?
    ORDER BY a.created_at DESC, a.id DESC
    LIMIT 1
    """,
    (drive_id, video["segment_id"], camera, video["id"]),
  ).fetchone()
  if frame_artifact is None:
    _not_ready("frame_index_not_ready")
  frame_document, frame_index_sha256 = _read_frame_index(
    archive_root,
    frame_artifact,
  )
  video_frames = _validate_frame_document(
    frame_document,
    frame_artifact,
    video,
    video_sha256,
    camera,
    segment,
  )
  (
    telemetry_index,
    telemetry_sha256,
    timeline_version,
    timeline_origin,
    segment_proof,
  ) = _telemetry_generation(
    connection,
    drive_id,
    camera,
    segment,
  )
  telemetry_frames = _telemetry_frames(
    connection,
    archive_root,
    telemetry_index,
    drive_id,
    camera,
    segment,
    segment_proof,
  )
  if len(video_frames) != len(telemetry_frames):
    _not_ready("frame_join_mismatch")

  points: list[dict[str, Any]] = []
  for ordinal, (video_frame, telemetry_frame) in enumerate(zip(video_frames, telemetry_frames, strict=True)):
    if video_frame["segment_frame_id"] != ordinal or telemetry_frame["segment_frame_id"] != ordinal:
      _not_ready("frame_join_mismatch")
    points.append(
      {
        "segment_frame_id": ordinal,
        "pts_us": video_frame["pts_us"],
        "duration_us": video_frame["duration_us"],
        "drive_t_us": telemetry_frame["drive_t_us"],
        "keyframe": video_frame["keyframe"],
      }
    )

  base = {
    "ready": True,
    "reason": None,
    "video_artifact_id": video["id"],
    "video_sha256": video_sha256,
    "frame_index_artifact_id": frame_artifact["id"],
    "frame_index_sha256": frame_index_sha256,
    "telemetry_sha256": telemetry_sha256,
    "timeline_version": timeline_version,
    "timeline_origin": timeline_origin,
    "integrity_scope": INTEGRITY_SCOPE,
  }
  if not include_points:
    return {
      **base,
      # The media manifest is assembled outside this read transaction. Return
      # its selection window from this same telemetry/video snapshot so a
      # concurrent reindex cannot pair an old segment origin with new exact
      # generation pins.
      "segment_window": {
        "start_t_us": video["segment_start_t_us"],
        "duration_us": video["segment_duration_us"],
      },
    }
  first = points[0]
  last = points[-1]
  return {
    **base,
    "drive_id": drive_id,
    "camera": camera,
    "segment_number": segment,
    "mode": "exact",
    "coverage": {
      "start_t_us": first["drive_t_us"],
      "end_t_us": last["drive_t_us"],
      "first_pts_us": first["pts_us"],
      "last_end_pts_us": last["pts_us"] + last["duration_us"],
    },
    "points": points,
  }


def inspect_media_sync(
  request: Request,
  drive_id: str,
  camera: str,
  segment: int,
  include_points: bool,
) -> dict[str, Any]:
  if (
    not isinstance(camera, str)
    or not camera
    or len(camera) > 32
    or not isinstance(segment, int)
    or isinstance(segment, bool)
    or segment < 0
    or segment > SQLITE_INT_MAX
  ):
    if include_points:
      raise ApiError(
        409,
        "media_sync_not_ready",
        "Exact media synchronization is not ready",
        details={"reason": "invalid_request"},
      )
    return {"ready": False, "reason": "invalid_request"}
  database: Database = request.app.state.database
  try:
    with database.connection() as connection:
      connection.execute("BEGIN")
      try:
        return _inspect_media_sync(
          connection,
          request.app.state.settings.archive_root,
          drive_id,
          camera,
          segment,
          include_points,
        )
      finally:
        connection.rollback()
  except _SyncNotReady as exc:
    if include_points:
      raise ApiError(
        409,
        "media_sync_not_ready",
        "Exact media synchronization is not ready",
        details={"reason": exc.reason},
      ) from exc
    return {"ready": False, "reason": exc.reason}


def _etag(
  video_artifact_id: str,
  video_sha256: str,
  frame_index_artifact_id: str,
  frame_index_sha256: str,
  telemetry_sha256: str,
  timeline_version: str,
  timeline_origin: Literal["stable", "provisional"],
) -> str:
  canonical = json.dumps(
    {
      "frame_index_sha256": frame_index_sha256,
      "frame_index_artifact_id": frame_index_artifact_id,
      "integrity_scope": INTEGRITY_SCOPE,
      "telemetry_sha256": telemetry_sha256,
      "timeline_origin": timeline_origin,
      "timeline_version": timeline_version,
      "video_artifact_id": video_artifact_id,
      "video_sha256": video_sha256,
    },
    separators=(",", ":"),
    sort_keys=True,
  ).encode("utf-8")
  return f'"{hashlib.sha256(canonical).hexdigest()}"'


@router.get("/{drive_id}/media-sync")
def media_sync(
  request: Request,
  response: Response,
  drive_id: str,
  camera: str,
  segment: int = Query(ge=0),
  telemetry_sha256: str | None = Query(
    default=None,
    pattern=r"^[0-9a-f]{64}$",
  ),
  frame_index_sha256: str | None = Query(
    default=None,
    pattern=r"^[0-9a-f]{64}$",
  ),
  video_sha256: str | None = Query(
    default=None,
    pattern=r"^[0-9a-f]{64}$",
  ),
  video_artifact_id: str | None = Query(
    default=None,
    min_length=1,
    max_length=256,
  ),
  frame_index_artifact_id: str | None = Query(
    default=None,
    min_length=1,
    max_length=256,
  ),
) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  inspection = inspect_media_sync(
    request,
    drive_id,
    camera,
    segment,
    include_points=True,
  )
  if telemetry_sha256 is not None and telemetry_sha256 != inspection["telemetry_sha256"]:
    raise ApiError(
      409,
      "media_sync_not_ready",
      "Exact media synchronization generation changed",
      details={"reason": "telemetry_generation_changed"},
    )
  if frame_index_sha256 is not None and frame_index_sha256 != inspection["frame_index_sha256"]:
    raise ApiError(
      409,
      "media_sync_not_ready",
      "Exact media synchronization generation changed",
      details={"reason": "frame_index_generation_changed"},
    )
  if video_sha256 is not None and video_sha256 != inspection["video_sha256"]:
    raise ApiError(
      409,
      "media_sync_not_ready",
      "Exact media synchronization generation changed",
      details={"reason": "video_generation_changed"},
    )
  if video_artifact_id is not None and video_artifact_id != inspection["video_artifact_id"]:
    raise ApiError(
      409,
      "media_sync_not_ready",
      "Exact media synchronization generation changed",
      details={"reason": "video_generation_changed"},
    )
  if frame_index_artifact_id is not None and frame_index_artifact_id != inspection["frame_index_artifact_id"]:
    raise ApiError(
      409,
      "media_sync_not_ready",
      "Exact media synchronization generation changed",
      details={"reason": "frame_index_generation_changed"},
    )
  response.headers["ETag"] = _etag(
    inspection["video_artifact_id"],
    inspection["video_sha256"],
    inspection["frame_index_artifact_id"],
    inspection["frame_index_sha256"],
    inspection["telemetry_sha256"],
    inspection["timeline_version"],
    inspection["timeline_origin"],
  )
  if (
    inspection["timeline_origin"] == "stable"
    and telemetry_sha256 is not None
    and frame_index_sha256 is not None
    and video_sha256 is not None
    and video_artifact_id is not None
    and frame_index_artifact_id is not None
  ):
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
  else:
    response.headers["Cache-Control"] = "no-store"
  return {key: value for key, value in inspection.items() if key not in {"ready", "reason"}}
