from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import mimetypes
import os
import secrets
import shutil
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from anyio import CancelScope
from fastapi import APIRouter, Request, Response, status
from starlette.concurrency import run_in_threadpool

from .auth import (
  ApiError,
  Principal,
  audit,
  request_ip,
  require_idempotency_key,
)
from .db import isoformat, utc_now
from .models import UploadCreate, UploadList, UploadSnapshot, UploadView


VIDEO_TYPES = {
  "fcamera",
  "ecamera",
  "dcamera",
  "qcamera",
  "road",
  "wideRoad",
  "driver",
  "video",
}
LOG_TYPES = {"rlog", "qlog"}
_VIDEO_TYPE_NAMES = frozenset(item.casefold() for item in VIDEO_TYPES)
_LOG_TYPE_NAMES = frozenset(item.casefold() for item in LOG_TYPES)
_UPLOAD_LOCKS = tuple(threading.RLock() for _ in range(257))
_PATCH_ADMISSION_INIT_LOCK = threading.Lock()


class _PatchAdmissionManager:
  def __init__(self) -> None:
    self._lock = threading.Lock()
    self._uploads: set[str] = set()
    self._by_device: dict[str, int] = defaultdict(int)
    self._global = 0

  def acquire(
    self,
    *,
    upload_id: str,
    device_id: str,
    per_device_limit: int,
    global_limit: int,
  ) -> None:
    with self._lock:
      if upload_id in self._uploads:
        raise ApiError(
          429,
          "upload_patch_in_flight",
          "Another PATCH is already in flight for this upload",
          headers={"Retry-After": "1"},
        )
      if self._by_device.get(device_id, 0) >= per_device_limit:
        raise ApiError(
          429,
          "upload_patch_capacity_exceeded",
          "This device has too many upload PATCH requests in flight",
          headers={"Retry-After": "1"},
        )
      if self._global >= global_limit:
        raise ApiError(
          503,
          "archive_backpressure",
          "Archive upload PATCH capacity is exhausted",
          headers={"Retry-After": "1"},
        )
      self._uploads.add(upload_id)
      self._by_device[device_id] += 1
      self._global += 1

  def release(self, *, upload_id: str, device_id: str) -> None:
    with self._lock:
      if upload_id not in self._uploads:
        return
      self._uploads.remove(upload_id)
      self._global -= 1
      remaining = self._by_device[device_id] - 1
      if remaining > 0:
        self._by_device[device_id] = remaining
      else:
        self._by_device.pop(device_id, None)


def _patch_admission_manager(request: Request) -> _PatchAdmissionManager:
  manager = getattr(request.app.state, "_upload_patch_admissions", None)
  if manager is not None:
    return manager
  with _PATCH_ADMISSION_INIT_LOCK:
    manager = getattr(request.app.state, "_upload_patch_admissions", None)
    if manager is None:
      manager = _PatchAdmissionManager()
      request.app.state._upload_patch_admissions = manager
  return manager


def _artifact_class(artifact_type: str) -> str:
  normalized = artifact_type.casefold()
  if normalized in _VIDEO_TYPE_NAMES:
    return "video"
  if normalized in _LOG_TYPE_NAMES:
    return "log"
  return "other"


def _artifact_size_limit(settings: Any, artifact_type: str) -> int:
  artifact_class = _artifact_class(artifact_type)
  typed_limit = {
    "video": settings.max_video_artifact_bytes,
    "log": settings.max_log_artifact_bytes,
    "other": settings.max_other_artifact_bytes,
  }[artifact_class]
  return min(settings.max_artifact_bytes, typed_limit)


def _archive_min_free_bytes(settings: Any, total_bytes: int) -> int:
  percent_floor = math.ceil(
    total_bytes * settings.archive_min_free_percent / 100.0,
  )
  return max(settings.archive_min_free_bytes, percent_floor)


def _archive_capacity_snapshot(settings: Any) -> tuple[int, int]:
  try:
    usage = shutil.disk_usage(settings.archive_root)
  except OSError as exc:
    raise ApiError(
      503,
      "archive_storage_unavailable",
      "Archive free space could not be verified",
      headers={"Retry-After": "60"},
    ) from exc
  return usage.total, usage.free


def _check_archive_capacity_snapshot(
  settings: Any,
  reserved_bytes: int,
  *,
  capacity_snapshot: tuple[int, int],
) -> None:
  total_bytes, free_bytes = capacity_snapshot
  minimum_free = _archive_min_free_bytes(settings, total_bytes)
  if reserved_bytes + minimum_free > free_bytes:
    raise ApiError(
      503,
      "archive_storage_pressure",
      "Archive free-space reserve would be exceeded",
      headers={"Retry-After": "60"},
    )


def _ensure_archive_capacity(
  settings: Any,
  reserved_bytes: int,
) -> None:
  _check_archive_capacity_snapshot(
    settings,
    reserved_bytes,
    capacity_snapshot=_archive_capacity_snapshot(settings),
  )


@contextmanager
def _upload_lock(upload_id: str) -> Iterator[None]:
  lock = _UPLOAD_LOCKS[hash(upload_id) % len(_UPLOAD_LOCKS)]
  with lock:
    yield


def _hash_file(path: Path) -> tuple[str, int]:
  digest = hashlib.sha256()
  size = 0
  with path.open("rb") as source:
    while chunk := source.read(1024 * 1024):
      digest.update(chunk)
      size += len(chunk)
  return digest.hexdigest(), size


def _upload_view(row: sqlite3.Row) -> dict[str, Any]:
  fields = set(row.keys())
  upload_id = row["id"]
  state = row["status"]
  return {
    "id": upload_id,
    "upload_id": upload_id,
    "file_id": row["file_id"],
    "device_id": row["device_id"],
    "relative_path": row["relative_path"],
    "route_name": row["route_name"],
    "segment_number": row["segment_number"],
    "artifact_type": row["artifact_type"],
    "camera": row["camera"],
    "completion_evidence": json.loads(row["completion_evidence_json"] or "[]"),
    "partial": bool(row["partial"]),
    "offset": row["offset"],
    "length": row["declared_size"],
    "status": state,
    "state": state,
    "durable": state == "complete",
    "declared_sha256": row["declared_sha256"],
    "sha256": row["object_sha256"],
    "artifact_id": row["artifact_id"],
    "error": row["error"],
    "created_at": row["created_at"],
    "updated_at": row["updated_at"],
    "completed_at": row["completed_at"],
    "bytes_per_second": float(row["bytes_per_second"]) if "bytes_per_second" in fields else 0,
  }


def _authorize_upload(
  request: Request,
  row: sqlite3.Row,
) -> Principal:
  authorization = request.headers.get("authorization", "")
  if authorization.lower().startswith("bearer "):
    principal = request.app.state.auth.authenticate_device(
      request,
      allow_importer=True,
    )
    if principal.actor_type == "device" and principal.actor_id != row["device_id"]:
      raise ApiError(403, "upload_access_denied", "Upload belongs to another device")
    return principal
  return request.app.state.auth.authenticate_admin(request)


def _object_relative_path(digest: str) -> Path:
  return Path("objects") / "sha256" / digest[:2] / digest[2:4] / digest


def _staging_path(request: Request, relative_value: str) -> Path:
  relative = Path(relative_value)
  if (
    relative.is_absolute()
    or any(part in {"", ".", ".."} for part in relative.parts)
  ):
    raise ApiError(
      500,
      "unsafe_upload_path",
      "Upload staging path is invalid",
    )
  path = (request.app.state.settings.archive_root / relative).resolve()
  root = request.app.state.settings.uploads_root.resolve()
  if not path.is_relative_to(root):
    raise ApiError(
      500,
      "unsafe_upload_path",
      "Upload staging path escapes the upload root",
    )
  return path


def _install_object(
  archive_root: Path,
  part_path: Path,
  digest: str,
  expected_size: int,
) -> tuple[Path, str]:
  relative = _object_relative_path(digest)
  target = archive_root / relative
  target.parent.mkdir(parents=True, exist_ok=True)
  if target.exists():
    existing_digest, existing_size = _hash_file(target)
    if existing_digest != digest or existing_size != expected_size:
      raise ApiError(
        500,
        "object_store_collision",
        "Existing content-addressed object failed verification",
      )
    return target, relative.as_posix()

  temporary = target.with_name(f".{digest}.{uuid4().hex}.tmp")
  try:
    linked = False
    try:
      # Staging and object storage are deliberately under the same archive
      # root. A hard link avoids temporarily consuming a second artifact's
      # worth of storage while the catalog transaction is still pending.
      os.link(part_path, temporary)
      linked = True
    except OSError:
      # Some archive filesystems (notably some CIFS configurations) do not
      # support hard links. Preserve the verified-copy fallback for them.
      with part_path.open("rb") as source, temporary.open("xb") as destination:
        shutil.copyfileobj(source, destination, length=1024 * 1024)
        destination.flush()
        os.fsync(destination.fileno())
    if not linked:
      copied_digest, copied_size = _hash_file(temporary)
      if copied_digest != digest or copied_size != expected_size:
        raise ApiError(
          500,
          "object_copy_failed",
          "Staged content object failed verification",
        )
    os.replace(temporary, target)
    try:
      directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
      directory_fd = os.open(target.parent, directory_flags)
      try:
        os.fsync(directory_fd)
      finally:
        os.close(directory_fd)
    except OSError:
      # Windows and some network filesystems do not expose directory fsync.
      pass
  finally:
    temporary.unlink(missing_ok=True)
  return target, relative.as_posix()


def _schedule_telemetry_extraction(
  connection: sqlite3.Connection,
  *,
  drive_id: str,
  route_name: str,
  source_fingerprint: str,
  available_at: str,
  now_text: str,
) -> None:
  payload_json = json.dumps(
    {
      "drive_id": drive_id,
      "route_name": route_name,
      "source_fingerprint": source_fingerprint,
    },
    separators=(",", ":"),
    sort_keys=True,
  )
  connection.execute(
    """
    UPDATE jobs
    SET state = CASE
        WHEN cancel_requested_at IS NOT NULL THEN 'canceled'
        ELSE 'failed'
      END,
      lease_owner = NULL,
      lease_expires_at = NULL,
      available_at = NULL,
      retryable = CASE
        WHEN cancel_requested_at IS NOT NULL THEN NULL
        ELSE 0
      END,
      error = CASE
        WHEN cancel_requested_at IS NULL
          THEN COALESCE(error, 'maximum attempts exhausted')
        ELSE error
      END,
      completed_at = ?,
      updated_at = ?
    WHERE type = 'extract_telemetry'
      AND state = 'queued'
      AND json_extract(payload_json, '$.drive_id') = ?
      AND (
        cancel_requested_at IS NOT NULL
        OR attempts >= max_attempts
      )
    """,
    (now_text, now_text, drive_id),
  )
  queued = connection.execute(
    """
    SELECT
      id,
      json_extract(payload_json, '$.source_fingerprint')
        AS source_fingerprint
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND state = 'queued'
      AND cancel_requested_at IS NULL
      AND attempts < max_attempts
      AND lease_owner IS NULL
      AND json_extract(payload_json, '$.drive_id') = ?
    ORDER BY created_at DESC, id DESC
    LIMIT 1
    """,
    (drive_id,),
  ).fetchone()
  if queued is not None:
    if queued["source_fingerprint"] == source_fingerprint:
      return
    connection.execute(
      """
      UPDATE jobs
      SET payload_json = ?, available_at = ?, updated_at = ?
      WHERE id = ? AND state = 'queued'
      """,
      (payload_json, available_at, now_text, queued["id"]),
    )
    return

  in_flight = connection.execute(
    """
    SELECT
      id,
      cancel_requested_at,
      attempts,
      max_attempts,
      retryable,
      lease_owner,
      lease_expires_at,
      json_extract(payload_json, '$.source_fingerprint')
        AS source_fingerprint
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND state IN ('leased', 'running')
      AND json_extract(payload_json, '$.drive_id') = ?
    ORDER BY created_at DESC, id DESC
    LIMIT 1
    """,
    (drive_id,),
  ).fetchone()
  viable_in_flight = (
    in_flight is not None
    and in_flight["cancel_requested_at"] is None
    and in_flight["attempts"] < in_flight["max_attempts"]
    and in_flight["retryable"] != 0
    and in_flight["lease_owner"] is not None
    and in_flight["lease_expires_at"] is not None
    and connection.execute(
      "SELECT julianday(?) > julianday(?)",
      (in_flight["lease_expires_at"], now_text),
    ).fetchone()[0]
    == 1
  )
  if (
    viable_in_flight
    and in_flight["source_fingerprint"] == source_fingerprint
  ):
    return

  dedupe_key = (
    f"drive:{drive_id}"
    if in_flight is None
    else f"drive:{drive_id}:after:{in_flight['id']}"
  )
  connection.execute(
    """
    INSERT INTO jobs(
      id, type, state, payload_json, dedupe_key, available_at,
      created_at, updated_at
    ) VALUES (?, 'extract_telemetry', 'queued', ?, ?, ?, ?, ?)
    """,
    (
      uuid4().hex,
      payload_json,
      dedupe_key,
      available_at,
      now_text,
      now_text,
    ),
  )


def _current_telemetry_source_fingerprint(
  connection: sqlite3.Connection,
  drive_id: str,
) -> str:
  preferred_log_kind = (
    "rlog"
    if connection.execute(
      """
      SELECT 1
      FROM artifacts
      WHERE drive_id = ?
        AND kind = 'rlog'
        AND status != 'partial'
      LIMIT 1
      """,
      (drive_id,),
    ).fetchone()
    else "qlog"
  )
  source_rows = connection.execute(
    """
    SELECT
      s.number,
      a.id,
      a.created_at,
      a.object_sha256
    FROM artifacts a
    JOIN segments s ON s.id = a.segment_id
    WHERE a.drive_id = ?
      AND a.kind = ?
      AND a.status != 'partial'
    ORDER BY s.number, a.created_at, a.id
    """,
    (drive_id, preferred_log_kind),
  ).fetchall()
  selected_by_segment: dict[int, sqlite3.Row] = {}
  for item in source_rows:
    selected_by_segment[int(item["number"])] = item
  selected_source_rows = [
    selected_by_segment[number]
    for number in sorted(selected_by_segment)
  ]
  return hashlib.sha256(
    json.dumps(
      [
        {
          "segment_number": item["number"],
          "sha256": item["object_sha256"],
        }
        for item in selected_source_rows
      ],
      separators=(",", ":"),
      sort_keys=True,
    ).encode(),
  ).hexdigest()


def _schedule_current_telemetry_extraction(
  connection: sqlite3.Connection,
  *,
  drive_id: str,
  route_name: str,
  telemetry_debounce_seconds: int,
  now_text: str,
) -> None:
  _schedule_telemetry_extraction(
    connection,
    drive_id=drive_id,
    route_name=route_name,
    source_fingerprint=_current_telemetry_source_fingerprint(
      connection,
      drive_id,
    ),
    available_at=isoformat(
      utc_now() + timedelta(seconds=telemetry_debounce_seconds),
    ),
    now_text=now_text,
  )


def _catalog_artifact(
  connection: sqlite3.Connection,
  row: sqlite3.Row,
  *,
  digest: str,
  storage_path: str,
  now_text: str,
  telemetry_debounce_seconds: int,
) -> str:
  drive_id: str | None = None
  segment_id: str | None = None
  provisional_started_at = row["declared_mtime"]
  if provisional_started_at is None and row["declared_mtime_ns"] is not None:
    provisional_started_at = isoformat(datetime.fromtimestamp(
      row["declared_mtime_ns"] / 1_000_000_000,
      UTC,
    ))
  if row["route_name"]:
    drive = connection.execute(
      "SELECT id FROM drives WHERE device_id = ? AND route_name = ?",
      (row["device_id"], row["route_name"]),
    ).fetchone()
    if drive is None:
      drive_id = uuid4().hex
      connection.execute(
        """
        INSERT INTO drives(
          id, device_id, route_name, started_at, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
          drive_id,
          row["device_id"],
          row["route_name"],
          provisional_started_at,
          now_text,
        ),
      )
    else:
      drive_id = drive["id"]
    if row["segment_number"] is not None:
      segment = connection.execute(
        "SELECT id FROM segments WHERE drive_id = ? AND number = ?",
        (drive_id, row["segment_number"]),
      ).fetchone()
      if segment is None:
        segment_id = uuid4().hex
        connection.execute(
          """
          INSERT INTO segments(
            id, drive_id, number, started_at, created_at
          ) VALUES (?, ?, ?, ?, ?)
          """,
          (
            segment_id,
            drive_id,
            row["segment_number"],
            provisional_started_at,
            now_text,
          ),
        )
      else:
        segment_id = segment["id"]

  prior = connection.execute(
    """
    SELECT id, drive_id
    FROM artifacts
    WHERE device_id = ? AND relative_path = ? AND object_sha256 = ?
    """,
    (row["device_id"], row["relative_path"], digest),
  ).fetchone()
  if prior is not None:
    prior_drive_id = prior["drive_id"] or drive_id
    if (
      prior_drive_id is not None
      and not row["partial"]
      and row["artifact_type"] in LOG_TYPES
    ):
      drive_state = connection.execute(
        """
        SELECT route_name, telemetry_ready
        FROM drives
        WHERE id = ?
        """,
        (prior_drive_id,),
      ).fetchone()
      if (
        drive_state is not None
        and drive_state["telemetry_ready"] != 1
      ):
        _schedule_current_telemetry_extraction(
          connection,
          drive_id=prior_drive_id,
          route_name=drive_state["route_name"],
          telemetry_debounce_seconds=telemetry_debounce_seconds,
          now_text=now_text,
        )
    return prior["id"]

  artifact_id = uuid4().hex
  mime_type = row["mime_type"] or mimetypes.guess_type(row["relative_path"])[0]
  connection.execute(
    """
    INSERT INTO artifacts(
      id, device_id, drive_id, segment_id, object_sha256,
      kind, camera, relative_path, storage_path, size,
      mime_type, status, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
      artifact_id,
      row["device_id"],
      drive_id,
      segment_id,
      digest,
      row["artifact_type"],
      row["camera"],
      row["relative_path"],
      storage_path,
      row["declared_size"],
      mime_type,
      "partial" if row["partial"] else "stored",
      now_text,
    ),
  )
  if drive_id:
    connection.execute(
      """
      UPDATE drives
      SET artifact_generation = artifact_generation + 1, updated_at = ?
      WHERE id = ?
      """,
      (now_text, drive_id),
    )

  jobs = [("verify_artifact", {"artifact_id": artifact_id})]
  if not row["partial"] and row["artifact_type"] in VIDEO_TYPES:
    jobs.append(("transcode_video", {"artifact_id": artifact_id}))
  if not row["partial"] and row["artifact_type"] in LOG_TYPES and drive_id:
    connection.execute(
      """
      UPDATE drives
      SET telemetry_ready = 0, updated_at = ?
      WHERE id = ?
      """,
      (now_text, drive_id),
    )
    connection.execute(
      """
      UPDATE artifacts
      SET status = 'stale'
      WHERE drive_id = ?
        AND kind = 'video_telemetry_sync'
        AND status = 'ready'
      """,
      (drive_id,),
    )
    connection.execute(
      """
      UPDATE artifacts
      SET time_map_path = NULL
      WHERE drive_id = ? AND kind = 'derived_video'
      """,
      (drive_id,),
    )
    _schedule_current_telemetry_extraction(
      connection,
      drive_id=drive_id,
      route_name=row["route_name"],
      telemetry_debounce_seconds=telemetry_debounce_seconds,
      now_text=now_text,
    )
  for job_type, job_payload in jobs:
    dedupe_key = (
      f"drive:{drive_id}"
      if job_type == "extract_telemetry"
      else f"artifact:{artifact_id}"
    )
    duplicate = connection.execute(
      """
      SELECT id
      FROM jobs
      WHERE type = ?
        AND dedupe_key = ?
        AND (
          state IN ('queued', 'leased', 'running')
          OR (? != 'extract_telemetry' AND state = 'succeeded')
        )
      LIMIT 1
      """,
      (
        job_type,
        dedupe_key,
        job_type,
      ),
    ).fetchone()
    if duplicate is not None:
      continue
    job_id = uuid4().hex
    connection.execute(
      """
      INSERT INTO jobs(
        id, type, state, payload_json, dedupe_key, available_at,
        created_at, updated_at
      ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
      """,
      (
        job_id,
        job_type,
        json.dumps(job_payload, separators=(",", ":"), sort_keys=True),
        dedupe_key,
        (
          isoformat(utc_now() + timedelta(seconds=telemetry_debounce_seconds))
          if job_type == "extract_telemetry"
          else now_text
        ),
        now_text,
        now_text,
      ),
    )
  return artifact_id


def _finalize_upload(request: Request, upload_id: str) -> dict[str, Any]:
  with _upload_lock(upload_id):
    return _finalize_upload_locked(request, upload_id)


def _fail_finalizing_upload(
  request: Request,
  row: sqlite3.Row,
  *,
  message: str,
  reason: str,
) -> None:
  now_text = isoformat()
  database = request.app.state.database
  with database.transaction(immediate=True) as connection:
    current = connection.execute(
      "SELECT * FROM uploads WHERE id = ?",
      (row["id"],),
    ).fetchone()
    if current is None:
      raise ApiError(404, "upload_not_found", "Upload was not found")
    if current["status"] != "finalizing":
      raise ApiError(
        409,
        "upload_state_changed",
        "Upload state changed while finalizing",
        details={"state": current["status"]},
      )
    cursor = connection.execute(
      """
      UPDATE uploads
      SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
      WHERE id = ? AND status = 'finalizing'
      """,
      (message, now_text, now_text, row["id"]),
    )
    if cursor.rowcount != 1:
      raise ApiError(
        409,
        "upload_state_changed",
        "Upload state changed while finalizing",
      )
    audit(
      connection,
      actor_type="device",
      actor_id=row["device_id"],
      action="upload.failed",
      resource_type="upload",
      resource_id=row["id"],
      details={"reason": reason},
      ip_address=request_ip(request),
    )


def _release_finalization_claim(
  request: Request,
  upload_id: str,
  *,
  error: str,
) -> None:
  request.app.state.database.execute(
    """
    UPDATE uploads
    SET status = 'receiving', error = ?, updated_at = ?
    WHERE id = ? AND status = 'finalizing'
    """,
    (error, isoformat(), upload_id),
  )


def _finalize_upload_locked(
  request: Request,
  upload_id: str,
) -> dict[str, Any]:
  database = request.app.state.database
  settings = request.app.state.settings
  claim_time = isoformat()
  with database.transaction(immediate=True) as connection:
    initial = connection.execute(
      "SELECT * FROM uploads WHERE id = ?",
      (upload_id,),
    ).fetchone()
    if initial is None:
      raise ApiError(404, "upload_not_found", "Upload was not found")
    if initial["status"] == "complete":
      return _upload_view(initial)
    if initial["status"] in {"failed", "canceled"}:
      raise ApiError(
        409,
        "upload_terminal",
        "Upload is terminal and must be redeclared",
        details={
          "state": initial["status"],
          "error": initial["error"],
          "retry_action": "redeclare",
        },
      )
    if initial["status"] == "finalizing":
      return _upload_view(initial)
    if initial["offset"] != initial["declared_size"]:
      return _upload_view(initial)

    device_finalizers = connection.execute(
      """
      SELECT COUNT(*) AS count
      FROM uploads
      WHERE device_id = ? AND status = 'finalizing'
      """,
      (initial["device_id"],),
    ).fetchone()
    if (
      device_finalizers["count"]
      >= settings.max_inflight_upload_patches_per_device
    ):
      raise ApiError(
        429,
        "upload_finalization_capacity_exceeded",
        "This device has too many uploads finalizing",
        headers={"Retry-After": "1"},
      )
    global_finalizers = connection.execute(
      "SELECT COUNT(*) AS count FROM uploads WHERE status = 'finalizing'",
    ).fetchone()
    if (
      global_finalizers["count"]
      >= settings.max_inflight_upload_patches_global
    ):
      raise ApiError(
        503,
        "archive_backpressure",
        "Archive finalization capacity is exhausted",
        headers={"Retry-After": "1"},
      )
    cursor = connection.execute(
      """
      UPDATE uploads
      SET status = 'finalizing', updated_at = ?
      WHERE id = ? AND status = 'receiving' AND offset = declared_size
      """,
      (claim_time, upload_id),
    )
    if cursor.rowcount != 1:
      raise ApiError(
        409,
        "upload_state_changed",
        "Upload state changed before finalization",
      )
    initial = connection.execute(
      "SELECT * FROM uploads WHERE id = ?",
      (upload_id,),
    ).fetchone()
  assert initial is not None

  part_path = _staging_path(request, initial["part_path"])
  if not part_path.is_file():
    _fail_finalizing_upload(
      request,
      initial,
      message="Upload staging file is missing",
      reason="part_missing",
    )
    raise ApiError(500, "upload_part_missing", "Upload staging file is missing")
  try:
    digest, actual_size = _hash_file(part_path)
  except OSError as exc:
    _release_finalization_claim(
      request,
      upload_id,
      error="Finalization deferred because archive storage was unavailable",
    )
    raise ApiError(
      503,
      "archive_storage_unavailable",
      "Archive content could not be read for finalization",
      headers={"Retry-After": "60"},
    ) from exc
  if actual_size != initial["declared_size"]:
    _fail_finalizing_upload(
      request,
      initial,
      message="Upload staging file size does not match its catalog offset",
      reason="part_size_mismatch",
    )
    part_path.unlink(missing_ok=True)
    raise ApiError(
      500,
      "upload_part_size_mismatch",
      "Upload staging file size does not match its catalog offset",
    )
  if initial["declared_sha256"] and digest != initial["declared_sha256"]:
    message = (
      f"SHA-256 mismatch: declared {initial['declared_sha256']}, "
      + f"computed {digest}"
    )
    _fail_finalizing_upload(
      request,
      initial,
      message=message,
      reason="sha256_mismatch",
    )
    part_path.unlink(missing_ok=True)
    raise ApiError(
      422,
      "sha256_mismatch",
      "Uploaded content did not match the declared SHA-256",
      details={"declared": initial["declared_sha256"], "computed": digest},
    )

  try:
    _, storage_path = _install_object(
      settings.archive_root,
      part_path,
      digest,
      actual_size,
    )
  except ApiError:
    _release_finalization_claim(
      request,
      upload_id,
      error="Finalization deferred because object installation failed",
    )
    raise
  except OSError as exc:
    _release_finalization_claim(
      request,
      upload_id,
      error="Finalization deferred because archive storage was unavailable",
    )
    raise ApiError(
      503,
      "archive_storage_unavailable",
      "Archive content could not be installed",
      headers={"Retry-After": "60"},
    ) from exc
  now_text = isoformat()
  with database.transaction(immediate=True) as connection:
    row = connection.execute(
      "SELECT * FROM uploads WHERE id = ?",
      (upload_id,),
    ).fetchone()
    if row is None:
      raise ApiError(404, "upload_not_found", "Upload was not found")
    if row["status"] == "complete":
      completed = row
    elif row["status"] == "finalizing":
      connection.execute(
        """
        INSERT INTO objects(sha256, size, storage_path, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(sha256) DO NOTHING
        """,
        (digest, actual_size, storage_path, now_text),
      )
      artifact_id = _catalog_artifact(
        connection,
        row,
        digest=digest,
        storage_path=storage_path,
        now_text=now_text,
        telemetry_debounce_seconds=(
          request.app.state.settings.telemetry_debounce_seconds
        ),
      )
      cursor = connection.execute(
        """
        UPDATE uploads
        SET status = 'complete', object_sha256 = ?, artifact_id = ?,
          completed_at = ?, updated_at = ?, error = NULL
        WHERE id = ? AND status = 'finalizing'
        """,
        (digest, artifact_id, now_text, now_text, upload_id),
      )
      if cursor.rowcount != 1:
        raise ApiError(
          409,
          "upload_state_changed",
          "Upload state changed while finalizing",
        )
      audit(
        connection,
        actor_type="device",
        actor_id=row["device_id"],
        action="upload.complete",
        resource_type="upload",
        resource_id=upload_id,
        details={
          "artifact_id": artifact_id,
          "sha256": digest,
          "size": actual_size,
        },
        ip_address=request_ip(request),
      )
      completed = connection.execute(
        "SELECT * FROM uploads WHERE id = ?",
        (upload_id,),
      ).fetchone()
    else:
      raise ApiError(
        409,
        "upload_terminal",
        "Upload is terminal and must be redeclared",
        details={
          "state": row["status"],
          "error": row["error"],
          "retry_action": "redeclare",
        },
      )
  part_path.unlink(missing_ok=True)
  assert completed is not None
  return _upload_view(completed)


router = APIRouter(prefix="/uploads", tags=["uploads"])


def _expire_stale_uploads(request: Request) -> None:
  database = request.app.state.database
  cutoff = isoformat(
    utc_now()
    - timedelta(
      seconds=request.app.state.settings.upload_stale_seconds,
    ),
  )
  now_text = isoformat()
  stale = database.query_all(
    """
    SELECT id, part_path, device_id, offset, declared_size
    FROM uploads
    WHERE status IN ('receiving', 'finalizing') AND updated_at < ?
    """,
    (cutoff,),
  )
  for candidate in stale:
    expired = False
    with _upload_lock(candidate["id"]):
      with database.transaction(immediate=True) as connection:
        cursor = connection.execute(
          """
          UPDATE uploads
          SET status = 'failed',
            error = 'Upload expired before completion',
            completed_at = ?, updated_at = ?
          WHERE id = ? AND status IN ('receiving', 'finalizing')
            AND updated_at < ?
          """,
          (now_text, now_text, candidate["id"], cutoff),
        )
        if cursor.rowcount == 1:
          expired = True
          audit(
            connection,
            actor_type="system",
            actor_id=None,
            action="upload.expire",
            resource_type="upload",
            resource_id=candidate["id"],
            details={
              "device_id": candidate["device_id"],
              "offset": candidate["offset"],
              "declared_size": candidate["declared_size"],
            },
          )
      if not expired:
        continue
      try:
        _staging_path(request, candidate["part_path"]).unlink(
          missing_ok=True,
        )
      except OSError:
        # A future maintenance pass can reclaim a terminal orphan.
        pass
  reclaim_before = isoformat(utc_now() - timedelta(seconds=60))
  terminal_orphans = database.query_all(
    """
    SELECT part_path
    FROM uploads
    WHERE status IN ('complete', 'failed', 'canceled')
      AND updated_at < ?
    ORDER BY updated_at
    LIMIT 100
    """,
    (reclaim_before,),
  )
  for orphan in terminal_orphans:
    try:
      _staging_path(request, orphan["part_path"]).unlink(
        missing_ok=True,
      )
    except OSError:
      pass


@router.post("", response_model=UploadView, status_code=status.HTTP_201_CREATED)
def create_upload(request: Request, payload: UploadCreate) -> dict[str, Any]:
  principal = request.app.state.auth.authenticate_device(
    request,
    allow_importer=True,
  )
  idempotency_key = require_idempotency_key(request)
  if principal.actor_type == "device":
    if payload.device_id and payload.device_id != principal.actor_id:
      raise ApiError(403, "device_mismatch", "Cannot declare an upload for another device")
    device_id = principal.actor_id
  else:
    if not payload.device_id:
      raise ApiError(
        422,
        "device_id_required",
        "Historical imports must specify device_id",
      )
    if payload.file_id is not None:
      raise ApiError(
        403,
        "importer_file_id_not_allowed",
        "Historical imports cannot claim an agent-local file identifier",
      )
    device_id = payload.device_id
  settings = request.app.state.settings
  artifact_limit = _artifact_size_limit(settings, payload.artifact_type)
  if payload.size > artifact_limit:
    raise ApiError(
      413,
      "artifact_too_large",
      "Declared artifact exceeds the configured limit for its type",
      details={
        "artifact_class": _artifact_class(payload.artifact_type),
        "maximum_size": artifact_limit,
      },
    )
  if (
    payload.size == 0
    and _artifact_class(payload.artifact_type) in {"video", "log"}
  ):
    raise ApiError(
      422,
      "empty_artifact",
      "Video and log artifacts must not be empty",
    )
  _expire_stale_uploads(request)

  canonical_payload = payload.model_dump(mode="json")
  if canonical_payload["file_id"] is None:
    # Version 8 declarations had no file_id member. Keep null/omitted legacy
    # replays byte-for-byte compatible while binding every non-null selector.
    del canonical_payload["file_id"]
  canonical = json.dumps(
    {
      "operation": "upload.create",
      **canonical_payload,
      "device_id": device_id,
    },
    separators=(",", ":"),
    sort_keys=True,
  )
  request_hash = hashlib.sha256(canonical.encode()).hexdigest()
  database = request.app.state.database
  upload_id = uuid4().hex
  part_relative = (Path("uploads") / f"{upload_id}.part").as_posix()
  part_path = settings.archive_root / part_relative
  now_text = isoformat()
  capacity_snapshot = _archive_capacity_snapshot(settings)
  part_path.parent.mkdir(parents=True, exist_ok=True)
  with part_path.open("xb"):
    pass
  part_adopted = False
  try:
    with database.transaction(immediate=True) as connection:
      device = connection.execute(
        "SELECT id FROM devices WHERE id = ? AND disabled_at IS NULL",
        (device_id,),
      ).fetchone()
      if device is None:
        raise ApiError(404, "device_not_found", "Device was not found")
      prior_key = connection.execute(
        """
        SELECT request_hash
        FROM idempotency_keys
        WHERE actor_type = ? AND actor_id = ? AND key = ?
        """,
        (principal.actor_type, principal.actor_id, idempotency_key),
      ).fetchone()
      prior = connection.execute(
        """
        SELECT u.*, i.request_hash
        FROM uploads u
        JOIN idempotency_keys i
          ON i.actor_type = ?
          AND i.actor_id = ?
          AND i.key = u.idempotency_key
        WHERE u.device_id = ? AND u.idempotency_key = ?
        """,
        (
          principal.actor_type,
          principal.actor_id,
          device_id,
          idempotency_key,
        ),
      ).fetchone()
      if prior_key is not None:
        if (
          prior is None
          or not secrets.compare_digest(prior_key["request_hash"], request_hash)
        ):
          raise ApiError(
            409,
            "idempotency_key_reused",
            "Idempotency key was already used with a different upload declaration",
          )
        return _upload_view(prior)

      capacity = connection.execute(
        """
        SELECT
          COUNT(*) AS active_uploads,
          COALESCE(SUM(declared_size), 0) AS reserved_bytes
        FROM uploads
        WHERE device_id = ? AND status IN ('receiving', 'finalizing')
        """,
        (device_id,),
      ).fetchone()
      if (
        capacity["active_uploads"]
        >= settings.max_active_uploads_per_device
      ):
        raise ApiError(
          429,
          "upload_capacity_exceeded",
          "This device has too many active uploads",
          headers={"Retry-After": "60"},
        )
      if (
        capacity["reserved_bytes"] + payload.size
        > settings.max_pending_upload_bytes_per_device
      ):
        raise ApiError(
          429,
          "upload_capacity_exceeded",
          "This device has too many reserved upload bytes",
          headers={"Retry-After": "60"},
        )
      global_capacity = connection.execute(
        """
        SELECT
          COUNT(*) AS active_uploads,
          COALESCE(SUM(declared_size), 0) AS reserved_bytes
        FROM uploads
        WHERE status IN ('receiving', 'finalizing')
        """,
      ).fetchone()
      if (
        global_capacity["active_uploads"]
        >= settings.max_active_uploads_global
        or global_capacity["reserved_bytes"] + payload.size
        > settings.max_pending_upload_bytes_global
      ):
        raise ApiError(
          503,
          "archive_backpressure",
          "Archive upload capacity is exhausted",
          headers={"Retry-After": "60"},
        )
      _check_archive_capacity_snapshot(
        settings,
        global_capacity["reserved_bytes"] + payload.size,
        capacity_snapshot=capacity_snapshot,
      )
      active_jobs = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM jobs
        WHERE state IN ('queued', 'leased', 'running')
        """,
      ).fetchone()
      if (
        active_jobs["count"] + 3
        > settings.max_active_jobs
      ):
        raise ApiError(
          503,
          "archive_backpressure",
          "Archive processing is at capacity",
          headers={"Retry-After": "60"},
        )

      connection.execute(
        """
        INSERT INTO idempotency_keys(
          actor_type, actor_id, key, request_hash, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
          principal.actor_type,
          principal.actor_id,
          idempotency_key,
          request_hash,
          now_text,
        ),
      )
      connection.execute(
        """
        INSERT INTO uploads(
          id, device_id, idempotency_key, file_id, relative_path, route_name,
          segment_number, artifact_type, camera, mime_type,
          completion_evidence_json, partial,
          declared_size, declared_mtime_ns, declared_mtime,
          declared_sha256, offset, status, part_path, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'receiving', ?, ?, ?)
        """,
        (
          upload_id,
          device_id,
          idempotency_key,
          payload.file_id,
          payload.relative_path,
          payload.route_name,
          payload.segment_number,
          payload.artifact_type,
          payload.camera,
          payload.mime_type,
          json.dumps(payload.completion_evidence, separators=(",", ":")),
          int(payload.partial),
          payload.size,
          payload.mtime_ns,
          isoformat(payload.mtime) if payload.mtime else None,
          payload.sha256,
          part_relative,
          now_text,
          now_text,
        ),
      )
      audit(
        connection,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        action="upload.create",
        resource_type="upload",
        resource_id=upload_id,
        details={
          "device_id": device_id,
          "file_id": payload.file_id,
          "relative_path": payload.relative_path,
          "size": payload.size,
        },
        ip_address=request_ip(request),
      )
      row = connection.execute(
        "SELECT * FROM uploads WHERE id = ?",
        (upload_id,),
      ).fetchone()
    part_adopted = True
  finally:
    if not part_adopted:
      part_path.unlink(missing_ok=True)
  assert row is not None
  if payload.size == 0:
    return _finalize_upload(request, upload_id)
  return _upload_view(row)


@router.get("/snapshot", response_model=UploadSnapshot)
def upload_snapshot(request: Request) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  return build_upload_snapshot(request)


def build_upload_snapshot(request: Request) -> dict[str, Any]:
  database = request.app.state.database
  since = isoformat(utc_now() - timedelta(seconds=60))
  summary = database.query_one(
    """
    SELECT
      SUM(CASE WHEN status IN ('receiving', 'finalizing') THEN 1 ELSE 0 END) AS active,
      SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
      SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS complete,
      COALESCE(SUM(offset), 0) AS received,
      COALESCE(SUM(declared_size), 0) AS expected,
      COALESCE(SUM(
        CASE
          WHEN status IN ('receiving', 'finalizing')
            AND declared_size > offset
            THEN declared_size - offset
          ELSE 0
        END
      ), 0) AS pending
    FROM uploads
    """,
  )
  recent = database.query_one(
    """
    SELECT COALESCE(SUM(size), 0) AS bytes
    FROM upload_chunks
    WHERE received_at >= ?
    """,
    (since,),
  )
  device_rows = database.query_all(
    """
    SELECT
      device_id,
      SUM(
        CASE WHEN status IN ('receiving', 'finalizing') THEN 1 ELSE 0 END
      ) AS active_uploads,
      COALESCE(SUM(offset), 0) AS bytes_received,
      COALESCE(SUM(
        CASE
          WHEN status IN ('receiving', 'finalizing')
            AND declared_size > offset
            THEN declared_size - offset
          ELSE 0
        END
      ), 0) AS pending_bytes
    FROM uploads
    GROUP BY device_id
    """,
  )
  by_device = {
    row["device_id"]: {
      "active_uploads": row["active_uploads"],
      "bytes_received": row["bytes_received"],
      "pending_bytes": row["pending_bytes"],
    }
    for row in device_rows
  }
  return {
    "active_uploads": summary["active"] or 0,
    "failed_uploads": summary["failed"] or 0,
    "completed_uploads": summary["complete"] or 0,
    "bytes_received": summary["received"],
    "bytes_expected": summary["expected"],
    "pending_bytes": summary["pending"],
    "bytes_per_second_60s": (recent["bytes"] or 0) / 60.0,
    "by_device": by_device,
  }


@router.get("", response_model=UploadList)
def list_uploads(
  request: Request,
  q: str | None = None,
  state: str | None = None,
  device_id: str | None = None,
  limit: int = 100,
  offset: int = 0,
) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  if limit < 1 or limit > 500:
    raise ApiError(422, "invalid_limit", "limit must be between 1 and 500")
  if offset < 0:
    raise ApiError(422, "invalid_offset", "offset must not be negative")
  if state and state not in {
    "receiving",
    "finalizing",
    "complete",
    "failed",
    "canceled",
  }:
    raise ApiError(422, "invalid_upload_state", "Upload state is invalid")
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
    escaped = (
      query.lower()
      .replace("\\", "\\\\")
      .replace("%", "\\%")
      .replace("_", "\\_")
    )
    pattern = f"%{escaped}%"
    clauses.append(
      """
      (
        LOWER(u.relative_path) LIKE ? ESCAPE '\\'
        OR LOWER(COALESCE(u.file_id, '')) LIKE ? ESCAPE '\\'
        OR LOWER(COALESCE(u.route_name, '')) LIKE ? ESCAPE '\\'
        OR LOWER(u.device_id) LIKE ? ESCAPE '\\'
        OR LOWER(COALESCE(u.camera, '')) LIKE ? ESCAPE '\\'
        OR LOWER(u.artifact_type) LIKE ? ESCAPE '\\'
      )
      """,
    )
    parameters.extend((pattern, pattern, pattern, pattern, pattern, pattern))
  if state:
    clauses.append("u.status = ?")
    parameters.append(state)
  if device_id:
    clauses.append("u.device_id = ?")
    parameters.append(device_id)
  where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
  total = request.app.state.database.query_one(
    f"SELECT COUNT(*) AS count FROM uploads u {where}",
    parameters,
  )
  rows = request.app.state.database.query_all(
    f"""
    SELECT u.*, COALESCE((
      SELECT SUM(c.size) / 60.0
      FROM upload_chunks c
      WHERE c.upload_id = u.id
        AND c.received_at >= ?
    ), 0) AS bytes_per_second
    FROM uploads u
    {where}
    ORDER BY u.updated_at DESC, u.id DESC
    LIMIT ? OFFSET ?
    """,
    [isoformat(utc_now() - timedelta(seconds=60)), *parameters, limit, offset],
  )
  return {
    "items": [_upload_view(row) for row in rows],
    "total": total["count"] if total else 0,
  }


@router.get("/{upload_id}", response_model=UploadView)
def get_upload(request: Request, upload_id: str) -> dict[str, Any]:
  row = request.app.state.database.query_one(
    """
    SELECT u.*, COALESCE((
      SELECT SUM(c.size) / 60.0
      FROM upload_chunks c
      WHERE c.upload_id = u.id
        AND c.received_at >= ?
    ), 0) AS bytes_per_second
    FROM uploads u
    WHERE u.id = ?
    """,
    (isoformat(utc_now() - timedelta(seconds=60)), upload_id),
  )
  if row is None:
    raise ApiError(404, "upload_not_found", "Upload was not found")
  _authorize_upload(request, row)
  return _upload_view(row)


@router.head("/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
def head_upload(request: Request, upload_id: str) -> Response:
  row = request.app.state.database.query_one(
    "SELECT * FROM uploads WHERE id = ?",
    (upload_id,),
  )
  if row is None:
    raise ApiError(404, "upload_not_found", "Upload was not found")
  _authorize_upload(request, row)
  if (
    row["status"] == "receiving"
    and row["offset"] == row["declared_size"]
  ):
    _finalize_upload(request, upload_id)
    row = request.app.state.database.query_one(
      "SELECT * FROM uploads WHERE id = ?",
      (upload_id,),
    )
    assert row is not None
  headers = {
    "Upload-Offset": str(row["offset"]),
    "Upload-Length": str(row["declared_size"]),
    "Upload-State": row["status"],
    "Upload-Durable": "true" if row["status"] == "complete" else "false",
    "Upload-Terminal": (
      "true"
      if row["status"] in {"complete", "failed", "canceled"}
      else "false"
    ),
    "Cache-Control": "no-store",
  }
  if row["status"] in {"failed", "canceled"}:
    headers["Upload-Retry-Action"] = "redeclare"
  if row["object_sha256"]:
    headers["Upload-SHA256"] = row["object_sha256"]
  return Response(status_code=status.HTTP_204_NO_CONTENT, headers=headers)


@router.post("/{upload_id}/cancel", response_model=UploadView)
def cancel_upload(request: Request, upload_id: str) -> dict[str, Any]:
  with _upload_lock(upload_id):
    return _cancel_upload_locked(request, upload_id)


def _cancel_upload_locked(
  request: Request,
  upload_id: str,
) -> dict[str, Any]:
  database = request.app.state.database
  initial = database.query_one(
    "SELECT * FROM uploads WHERE id = ?",
    (upload_id,),
  )
  if initial is None:
    raise ApiError(404, "upload_not_found", "Upload was not found")
  principal = _authorize_upload(request, initial)
  if principal.actor_type == "admin":
    request.app.state.auth.require_origin(request)
  idempotency_key = require_idempotency_key(request)
  canonical = json.dumps(
    {
      "operation": "upload.cancel",
      "upload_id": upload_id,
    },
    separators=(",", ":"),
    sort_keys=True,
  )
  request_hash = hashlib.sha256(canonical.encode()).hexdigest()
  now_text = isoformat()
  with database.transaction(immediate=True) as connection:
    row = connection.execute(
      "SELECT * FROM uploads WHERE id = ?",
      (upload_id,),
    ).fetchone()
    if row is None:
      raise ApiError(404, "upload_not_found", "Upload was not found")
    prior = connection.execute(
      """
      SELECT request_hash
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
          "Idempotency key was already used for another operation",
        )
    else:
      if row["status"] == "complete":
        raise ApiError(
          409,
          "upload_complete",
          "A durable completed upload cannot be canceled",
        )
      if row["status"] == "finalizing":
        raise ApiError(
          409,
          "upload_finalizing",
          "An upload cannot be canceled after finalization has started",
          headers={
            "Upload-Offset": str(row["offset"]),
            "Upload-State": "finalizing",
          },
        )
      connection.execute(
        """
        INSERT INTO idempotency_keys(
          actor_type, actor_id, key, request_hash, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
          principal.actor_type,
          principal.actor_id,
          idempotency_key,
          request_hash,
          now_text,
        ),
      )
      connection.execute(
        """
        UPDATE uploads
        SET status = 'canceled',
          error = COALESCE(error, 'Canceled by uploader'),
          completed_at = COALESCE(completed_at, ?),
          updated_at = ?
        WHERE id = ?
        """,
        (now_text, now_text, upload_id),
      )
      audit(
        connection,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        action="upload.cancel",
        resource_type="upload",
        resource_id=upload_id,
        details={
          "previous_state": row["status"],
          "offset": row["offset"],
          "declared_size": row["declared_size"],
        },
        ip_address=request_ip(request),
      )
    canceled = connection.execute(
      "SELECT * FROM uploads WHERE id = ?",
      (upload_id,),
    ).fetchone()
  assert canceled is not None
  _staging_path(request, canceled["part_path"]).unlink(missing_ok=True)
  return _upload_view(canceled)


class _ChunkSink:
  def __init__(self, path: Path):
    self.path = path
    self._destination = path.open("xb")
    self._digest = hashlib.sha256()
    self._size = 0
    self._closed = False

  def write(self, data: bytes) -> None:
    if self._closed:
      raise RuntimeError("upload chunk sink is closed")
    remaining = memoryview(data)
    while remaining:
      written = self._destination.write(remaining)
      if written is None or written <= 0:
        raise OSError("upload chunk write made no progress")
      self._digest.update(remaining[:written])
      self._size += written
      remaining = remaining[written:]

  def finish(self) -> tuple[int, str]:
    if self._closed:
      raise RuntimeError("upload chunk sink is closed")
    try:
      self._destination.flush()
      os.fsync(self._destination.fileno())
    finally:
      self._destination.close()
      self._closed = True
    return self._size, self._digest.hexdigest()

  def abort(self) -> None:
    if not self._closed:
      try:
        self._destination.close()
      finally:
        self._closed = True
    self.path.unlink(missing_ok=True)


def _content_length(request: Request) -> int | None:
  value = request.headers.get("content-length")
  if value is None:
    return None
  try:
    length = int(value)
  except ValueError as exc:
    raise ApiError(
      400,
      "invalid_content_length",
      "Content-Length is invalid",
    ) from exc
  if length < 0:
    raise ApiError(
      400,
      "invalid_content_length",
      "Content-Length is invalid",
    )
  if length > request.app.state.settings.max_chunk_bytes:
    raise ApiError(413, "chunk_too_large", "Upload chunk is too large")
  return length


def _expected_chunk_checksum(request: Request) -> str | None:
  header = request.headers.get("upload-checksum")
  if not header:
    return None
  algorithm, separator, encoded = header.partition(" ")
  if not separator or algorithm.lower() != "sha256":
    raise ApiError(
      400,
      "unsupported_upload_checksum",
      "Upload-Checksum must use SHA-256",
    )
  try:
    decoded = base64.b64decode(encoded, validate=True)
  except (ValueError, binascii.Error) as exc:
    raise ApiError(
      400,
      "invalid_upload_checksum",
      "Upload-Checksum is invalid",
    ) from exc
  if len(decoded) != hashlib.sha256().digest_size:
    raise ApiError(
      400,
      "invalid_upload_checksum",
      "Upload-Checksum is invalid",
    )
  return decoded.hex()


async def _abort_chunk_sink(
  chunk_path: Path,
  sink: _ChunkSink | None,
) -> None:
  def cleanup() -> None:
    if sink is not None:
      sink.abort()
    else:
      chunk_path.unlink(missing_ok=True)

  # Client disconnect and task cancellation must not strand a large temp file.
  with CancelScope(shield=True):
    await run_in_threadpool(cleanup)


async def _receive_chunk(
  request: Request,
  upload_id: str,
  *,
  max_body_bytes: int | None = None,
  declared_length: int | None = None,
) -> tuple[Path, int, str]:
  if declared_length is None:
    declared_length = _content_length(request)
  body_limit = request.app.state.settings.max_chunk_bytes
  if max_body_bytes is not None:
    body_limit = min(body_limit, max_body_bytes)
  if declared_length is not None and declared_length > body_limit:
    raise ApiError(
      413,
      "upload_exceeds_length",
      "Chunk would exceed the admitted upload length",
    )
  chunk_path = request.app.state.settings.uploads_root / (
    f".{upload_id}.{uuid4().hex}.chunk"
  )
  sink: _ChunkSink | None = None
  size = 0
  try:
    sink = await run_in_threadpool(_ChunkSink, chunk_path)
    async for data in request.stream():
      size += len(data)
      if size > body_limit:
        raise ApiError(
          413,
          "upload_exceeds_length",
          "Chunk would exceed the admitted upload length",
        )
      if data:
        await run_in_threadpool(sink.write, data)
    size, digest = await run_in_threadpool(sink.finish)
  except BaseException:
    await _abort_chunk_sink(chunk_path, sink)
    raise
  if declared_length is not None and size != declared_length:
    await _abort_chunk_sink(chunk_path, sink)
    raise ApiError(
      400,
      "content_length_mismatch",
      "Chunk length did not match Content-Length",
    )
  return chunk_path, size, digest


def _verify_chunk_checksum(
  request: Request,
  digest: str,
  expected: str | None = None,
) -> None:
  if expected is None:
    expected = _expected_chunk_checksum(request)
  if expected is None:
    return
  if not secrets.compare_digest(expected, digest):
    raise ApiError(
      460,
      "chunk_checksum_mismatch",
      "Upload chunk did not match Upload-Checksum",
    )


def _preflight_patch_admission(
  request: Request,
  upload_id: str,
  chunk_offset: int,
  content_length: int | None,
  expected_checksum: str | None,
) -> int:
  database = request.app.state.database
  settings = request.app.state.settings
  with database.transaction() as connection:
    row = connection.execute(
      "SELECT * FROM uploads WHERE id = ?",
      (upload_id,),
    ).fetchone()
    if row is None:
      raise ApiError(404, "upload_not_found", "Upload was not found")
    if row["status"] in {"failed", "canceled"}:
      raise ApiError(
        409,
        "upload_terminal",
        "Upload is terminal and must be redeclared",
        details={
          "state": row["status"],
          "error": row["error"],
          "retry_action": "redeclare",
        },
        headers={
          "Upload-Offset": str(row["offset"]),
          "Upload-State": row["status"],
          "Upload-Terminal": "true",
          "Upload-Retry-Action": "redeclare",
        },
      )
    if row["status"] == "complete":
      raise ApiError(
        409,
        "upload_complete",
        "Upload is already complete",
        headers={
          "Upload-Offset": str(row["offset"]),
          "Upload-State": "complete",
          "Upload-Terminal": "true",
        },
      )
    if row["status"] != "receiving":
      raise ApiError(
        429,
        "upload_finalizing",
        "Upload finalization is already in progress",
        headers={
          "Retry-After": "1",
          "Upload-Offset": str(row["offset"]),
          "Upload-State": row["status"],
        },
      )

    artifact_limit = _artifact_size_limit(settings, row["artifact_type"])
    if row["declared_size"] > artifact_limit:
      raise ApiError(
        413,
        "artifact_too_large",
        "Declared artifact exceeds the configured limit for its type",
      )

    device_capacity = connection.execute(
      """
      SELECT
        COUNT(*) AS active_uploads,
        COALESCE(SUM(declared_size), 0) AS reserved_bytes
      FROM uploads
      WHERE device_id = ? AND status IN ('receiving', 'finalizing')
      """,
      (row["device_id"],),
    ).fetchone()
    if (
      device_capacity["active_uploads"] > settings.max_active_uploads_per_device
      or device_capacity["reserved_bytes"]
      > settings.max_pending_upload_bytes_per_device
    ):
      raise ApiError(
        429,
        "upload_capacity_exceeded",
        "This device's upload reservation exceeds current capacity",
        headers={"Retry-After": "60"},
      )

    global_capacity = connection.execute(
      """
      SELECT
        COUNT(*) AS active_uploads,
        COALESCE(SUM(declared_size), 0) AS reserved_bytes
      FROM uploads
      WHERE status IN ('receiving', 'finalizing')
      """,
    ).fetchone()
    if (
      global_capacity["active_uploads"] > settings.max_active_uploads_global
      or global_capacity["reserved_bytes"]
      > settings.max_pending_upload_bytes_global
    ):
      raise ApiError(
        503,
        "archive_backpressure",
        "Archive upload reservation exceeds current capacity",
        headers={"Retry-After": "60"},
      )

    if chunk_offset == row["offset"]:
      remaining = row["declared_size"] - row["offset"]
      if remaining <= 0:
        raise ApiError(
          409,
          "upload_finalization_pending",
          "Upload content is complete and awaiting finalization",
          headers={"Upload-Offset": str(row["offset"])},
        )
      body_limit = min(settings.max_chunk_bytes, remaining)
      if content_length == 0:
        raise ApiError(
          400,
          "empty_upload_chunk",
          "Upload chunk must not be empty",
        )
      if content_length is not None and content_length > body_limit:
        raise ApiError(
          413,
          "upload_exceeds_length",
          "Chunk would exceed the declared upload length",
          headers={"Upload-Offset": str(row["offset"])},
        )
    else:
      prior = connection.execute(
        """
        SELECT size, sha256
        FROM upload_chunks
        WHERE upload_id = ? AND chunk_offset = ?
        """,
        (upload_id, chunk_offset),
      ).fetchone()
      if (
        prior is None
        or (content_length is not None and content_length != prior["size"])
        or (
          expected_checksum is not None
          and not secrets.compare_digest(expected_checksum, prior["sha256"])
        )
      ):
        raise ApiError(
          409,
          "upload_offset_mismatch",
          "Upload-Offset does not match the server offset",
          details={"expected_offset": row["offset"]},
          headers={"Upload-Offset": str(row["offset"])},
        )
      body_limit = prior["size"]

    part_path = _staging_path(request, row["part_path"])
    reserved_bytes = global_capacity["reserved_bytes"]

  if not part_path.is_file():
    raise ApiError(
      500,
      "upload_part_missing",
      "Upload staging file is missing",
    )
  _ensure_archive_capacity(settings, reserved_bytes)
  return body_limit


def _append_chunk_file(
  part_path: Path,
  chunk_path: Path,
  chunk_offset: int,
) -> None:
  with part_path.open("r+b") as destination, chunk_path.open("rb") as source:
    destination.seek(0, os.SEEK_END)
    actual_part_size = destination.tell()
    if actual_part_size < chunk_offset:
      raise ApiError(
        500,
        "upload_part_truncated",
        "Upload staging file is shorter than the catalog offset",
      )
    if actual_part_size > chunk_offset:
      destination.truncate(chunk_offset)
    destination.seek(chunk_offset)
    shutil.copyfileobj(source, destination, length=1024 * 1024)
    destination.flush()
    os.fsync(destination.fileno())


def _commit_upload_chunk(
  request: Request,
  upload_id: str,
  chunk_offset: int,
  chunk_path: Path,
  chunk_size: int,
  chunk_digest: str,
) -> tuple[dict[str, Any], bool]:
  database = request.app.state.database
  with _upload_lock(upload_id):
    row = database.query_one(
      "SELECT * FROM uploads WHERE id = ?",
      (upload_id,),
    )
    if row is None:
      raise ApiError(404, "upload_not_found", "Upload was not found")
    if row["status"] in {"failed", "canceled"}:
      raise ApiError(
        409,
        "upload_terminal",
        "Upload is terminal and must be redeclared",
        details={
          "state": row["status"],
          "error": row["error"],
          "retry_action": "redeclare",
        },
        headers={
          "Upload-Offset": str(row["offset"]),
          "Upload-State": row["status"],
          "Upload-Terminal": "true",
          "Upload-Retry-Action": "redeclare",
        },
      )
    if row["status"] == "finalizing":
      raise ApiError(
        429,
        "upload_finalizing",
        "Upload finalization is already in progress",
        headers={
          "Retry-After": "1",
          "Upload-Offset": str(row["offset"]),
          "Upload-State": "finalizing",
        },
      )
    if chunk_offset != row["offset"]:
      prior = database.query_one(
        """
        SELECT size, sha256
        FROM upload_chunks
        WHERE upload_id = ? AND chunk_offset = ?
        """,
        (upload_id, chunk_offset),
      )
      if (
        prior is not None
        and prior["size"] == chunk_size
        and secrets.compare_digest(prior["sha256"], chunk_digest)
      ):
        return _upload_view(row), False
      raise ApiError(
        409,
        "upload_offset_mismatch",
        "Upload-Offset does not match the server offset",
        details={"expected_offset": row["offset"]},
        headers={"Upload-Offset": str(row["offset"])},
      )
    if row["status"] == "complete":
      raise ApiError(
        409,
        "upload_complete",
        "Upload is already complete",
        headers={"Upload-Offset": str(row["offset"])},
      )
    next_offset = chunk_offset + chunk_size
    if next_offset > row["declared_size"]:
      raise ApiError(
        413,
        "upload_exceeds_length",
        "Chunk would exceed the declared upload length",
        headers={"Upload-Offset": str(row["offset"])},
      )
    part_path = _staging_path(request, row["part_path"])
    if not part_path.exists():
      raise ApiError(
        500,
        "upload_part_missing",
        "Upload staging file is missing",
      )

    # CIFS copy and durability happen outside every SQLite transaction.
    _append_chunk_file(part_path, chunk_path, chunk_offset)
    now_text = isoformat()
    with database.transaction(immediate=True) as connection:
      current = connection.execute(
        "SELECT * FROM uploads WHERE id = ?",
        (upload_id,),
      ).fetchone()
      if current is None:
        raise ApiError(404, "upload_not_found", "Upload was not found")
      if current["status"] != "receiving":
        raise ApiError(
          409,
          "upload_state_changed",
          "Upload state changed while committing the chunk",
          details={"state": current["status"]},
        )
      if current["offset"] != chunk_offset:
        raise ApiError(
          409,
          "upload_offset_mismatch",
          "Upload-Offset changed while committing the chunk",
          details={"expected_offset": current["offset"]},
          headers={"Upload-Offset": str(current["offset"])},
        )
      connection.execute(
        """
        INSERT INTO upload_chunks(
          upload_id, chunk_offset, size, sha256, received_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (upload_id, chunk_offset, chunk_size, chunk_digest, now_text),
      )
      cursor = connection.execute(
        """
        UPDATE uploads
        SET offset = ?, updated_at = ?
        WHERE id = ? AND status = 'receiving' AND offset = ?
        """,
        (next_offset, now_text, upload_id, chunk_offset),
      )
      if cursor.rowcount != 1:
        raise ApiError(
          409,
          "upload_state_changed",
          "Upload state changed while committing the chunk",
        )
      updated = connection.execute(
        "SELECT * FROM uploads WHERE id = ?",
        (upload_id,),
      ).fetchone()
    assert updated is not None
    return (
      _upload_view(updated),
      updated["offset"] == updated["declared_size"],
    )


@router.patch("/{upload_id}", response_model=UploadView)
async def patch_upload(request: Request, upload_id: str) -> dict[str, Any]:
  initial = request.app.state.database.query_one(
    "SELECT * FROM uploads WHERE id = ?",
    (upload_id,),
  )
  if initial is None:
    raise ApiError(404, "upload_not_found", "Upload was not found")
  _authorize_upload(request, initial)
  content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
  if content_type not in {
    "application/offset+octet-stream",
    "application/octet-stream",
  }:
    raise ApiError(
      415,
      "unsupported_media_type",
      "Upload chunks must use application/offset+octet-stream",
    )
  raw_offset = request.headers.get("upload-offset")
  if raw_offset is None:
    raise ApiError(400, "upload_offset_required", "Upload-Offset header is required")
  try:
    chunk_offset = int(raw_offset)
  except ValueError as exc:
    raise ApiError(400, "invalid_upload_offset", "Upload-Offset is invalid") from exc
  if chunk_offset < 0:
    raise ApiError(400, "invalid_upload_offset", "Upload-Offset is invalid")
  upload_length = request.headers.get("upload-length")
  if upload_length is not None:
    try:
      header_length = int(upload_length)
    except ValueError as exc:
      raise ApiError(400, "invalid_upload_length", "Upload-Length is invalid") from exc
    if header_length != initial["declared_size"]:
      raise ApiError(
        409,
        "upload_length_mismatch",
        "Upload-Length does not match the declared length",
      )

  content_length = _content_length(request)
  expected_checksum = _expected_chunk_checksum(request)
  settings = request.app.state.settings
  admissions = _patch_admission_manager(request)
  admissions.acquire(
    upload_id=upload_id,
    device_id=initial["device_id"],
    per_device_limit=settings.max_inflight_upload_patches_per_device,
    global_limit=settings.max_inflight_upload_patches_global,
  )
  chunk_path: Path | None = None
  try:
    body_limit = await run_in_threadpool(
      _preflight_patch_admission,
      request,
      upload_id,
      chunk_offset,
      content_length,
      expected_checksum,
    )
    chunk_path, chunk_size, chunk_digest = await _receive_chunk(
      request,
      upload_id,
      max_body_bytes=body_limit,
      declared_length=content_length,
    )
    if chunk_size == 0:
      raise ApiError(400, "empty_upload_chunk", "Upload chunk must not be empty")
    _verify_chunk_checksum(request, chunk_digest, expected_checksum)
    updated, should_finalize = await run_in_threadpool(
      _commit_upload_chunk,
      request,
      upload_id,
      chunk_offset,
      chunk_path,
      chunk_size,
      chunk_digest,
    )
    if should_finalize:
      return await run_in_threadpool(
        _finalize_upload,
        request,
        upload_id,
      )
    return updated
  finally:
    try:
      if chunk_path is not None:
        await _abort_chunk_sink(chunk_path, None)
    finally:
      admissions.release(
        upload_id=upload_id,
        device_id=initial["device_id"],
      )
