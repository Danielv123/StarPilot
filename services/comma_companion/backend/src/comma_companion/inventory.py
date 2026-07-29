from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Request, Response, status
from pydantic import Field, field_validator, model_validator

from .auth import ApiError, audit, request_ip, require_idempotency_key
from .db import Database, isoformat
from .models import StrictModel


INVENTORY_SCHEMA = "comma-companion.route-inventory"
INVENTORY_SCHEMA_VERSION = 1
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP_PATTERN = re.compile(
  r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$",
)
COMPONENT_PATTERN = re.compile(r"^[^|/\\\x00-\x1f\x7f]+$")
MAX_INVENTORY_ROWS = 20_000
MAX_INVENTORY_GENERATIONS_PER_ROUTE = 1024
MAX_INVENTORY_ROUTES_PER_DEVICE = 25_000
MAX_INVENTORY_RECORDS_PER_DEVICE = 100_000
MAX_INVENTORY_BYTES_PER_DEVICE = 512 * 1024 * 1024
MAX_INVENTORY_MATERIALIZED_ROWS_PER_DEVICE = 2_000_000
INVENTORY_RATE_WINDOW_SECONDS = 60.0
INVENTORY_DEVICE_REQUESTS_PER_WINDOW = 60
INVENTORY_IMPORT_REQUESTS_PER_WINDOW = 600
_inventory_rate_lock = threading.Lock()
_inventory_rate_attempts: dict[str, deque[float]] = defaultdict(deque)


def _validate_component(value: str, field: str) -> str:
  if not COMPONENT_PATTERN.fullmatch(value) or any(not (character.isascii() and (character.isalnum() or character in "._-")) for character in value):
    raise ValueError(
      f"{field} must not contain pipes, slashes, or control characters",
    )
  return value


def _validate_relative_path(value: str) -> str:
  if value.startswith(("/", "\\")) or "\\" in value or "\x00" in value:
    raise ValueError("relative_path must be a canonical relative POSIX path")
  if any(part in {"", ".", ".."} for part in value.split("/")):
    raise ValueError("relative_path contains an invalid component")
  if len(value.encode("utf-8")) > 4096:
    raise ValueError("relative_path is too long")
  return value


def _stream_role(
  root_name: str,
  artifact_type: str,
  camera: str | None,
) -> str:
  return f"{root_name}|{artifact_type}|{camera or '-'}"


class InventoryFile(StrictModel):
  artifact_type: str = Field(min_length=1, max_length=64)
  camera: str | None = Field(default=None, min_length=1, max_length=32)
  mtime_ns: int = Field(gt=0)
  relative_path: str = Field(min_length=1, max_length=4096)
  sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
  size: int = Field(ge=0, le=1 << 40)

  @field_validator("artifact_type")
  @classmethod
  def validate_artifact_type(cls, value: str) -> str:
    return _validate_component(value, "artifact_type")

  @field_validator("camera")
  @classmethod
  def validate_camera(cls, value: str | None) -> str | None:
    return _validate_component(value, "camera") if value is not None else None

  @field_validator("relative_path")
  @classmethod
  def validate_path(cls, value: str) -> str:
    return _validate_relative_path(value)

  @model_validator(mode="after")
  def validate_identity(self) -> InventoryFile:
    if self.artifact_type == "video":
      if self.camera is None:
        raise ValueError("video inventory files require camera")
    elif self.artifact_type in {"rlog", "qlog", "artifact"}:
      if self.camera is not None:
        raise ValueError("log and non-stream inventory files require null camera")
    else:
      raise ValueError(
        "artifact_type must be video, rlog, qlog, or artifact",
      )
    return self


class InventoryExpectedStream(StrictModel):
  artifact_type: Literal["video", "rlog", "qlog"]
  camera: str | None = Field(default=None, min_length=1, max_length=32)
  role: str = Field(min_length=3, max_length=256)
  root_name: str = Field(min_length=1, max_length=64)

  @field_validator("camera")
  @classmethod
  def validate_camera(cls, value: str | None) -> str | None:
    return _validate_component(value, "camera") if value is not None else None

  @field_validator("root_name")
  @classmethod
  def validate_root_name(cls, value: str) -> str:
    return _validate_component(value, "root_name")

  @model_validator(mode="after")
  def validate_role(self) -> InventoryExpectedStream:
    if self.artifact_type == "video" and self.camera is None:
      raise ValueError("video expected streams require camera")
    if self.artifact_type != "video" and self.camera is not None:
      raise ValueError("log expected streams must not specify camera")
    expected = _stream_role(
      self.root_name,
      self.artifact_type,
      self.camera,
    )
    if self.role != expected:
      raise ValueError(f"role must be {expected!r}")
    return self


class InventoryStream(StrictModel):
  mtime_ns: int | None = Field(default=None, ge=0)
  relative_path: str | None = Field(default=None, min_length=1, max_length=4096)
  role: str = Field(min_length=3, max_length=256)
  sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
  size: int | None = Field(default=None, ge=0, le=1 << 40)
  status: Literal["present", "missing"]

  @field_validator("relative_path")
  @classmethod
  def validate_path(cls, value: str | None) -> str | None:
    return _validate_relative_path(value) if value is not None else None

  @model_validator(mode="after")
  def validate_file_fields(self) -> InventoryStream:
    fields = (
      self.mtime_ns,
      self.relative_path,
      self.sha256,
      self.size,
    )
    if self.status == "present" and any(value is None for value in fields):
      raise ValueError("present streams require every immutable file field")
    if self.status == "missing" and any(value is not None for value in fields):
      raise ValueError("missing streams must not contain file fields")
    return self


class InventorySegment(StrictModel):
  files: list[InventoryFile] = Field(default_factory=list, max_length=512)
  number: int = Field(ge=0, le=1_000_000)
  streams: list[InventoryStream] = Field(default_factory=list, max_length=64)


class RouteInventoryManifest(StrictModel):
  capability_source: Literal[
    "configured+route_union",
    "route_union_unconfigured",
  ]
  closed_at: str = Field(min_length=20, max_length=40)
  closure_evidence: list[str] = Field(min_length=1, max_length=32)
  expected_streams: list[InventoryExpectedStream] = Field(
    default_factory=list,
    max_length=64,
  )
  generation: int = Field(ge=1, le=1_000_000)
  missing_segment_numbers: list[int] = Field(
    default_factory=list,
    max_length=4096,
  )
  previous_manifest_sha256: str | None = Field(
    default=None,
    pattern=r"^[0-9a-f]{64}$",
  )
  root_names: list[str] = Field(min_length=1, max_length=32)
  route_closed: Literal[True]
  route_files: list[InventoryFile] = Field(default_factory=list, max_length=4096)
  route_name: str = Field(min_length=1, max_length=256)
  schema_: Literal["comma-companion.route-inventory"] = Field(
    alias="schema",
    serialization_alias="schema",
  )
  schema_version: Literal[1]
  segments: list[InventorySegment] = Field(default_factory=list, max_length=4096)
  state: Literal["complete", "partial"]

  @field_validator("closed_at")
  @classmethod
  def validate_closed_at(cls, value: str) -> str:
    if not UTC_TIMESTAMP_PATTERN.fullmatch(value):
      raise ValueError("closed_at must be a canonical UTC RFC3339 timestamp")
    return value

  @field_validator("closure_evidence")
  @classmethod
  def validate_closure_evidence(cls, value: list[str]) -> list[str]:
    for item in value:
      if not item or len(item) > 128 or not COMPONENT_PATTERN.fullmatch(item):
        raise ValueError("closure_evidence contains an invalid value")
    if value != sorted(set(value)):
      raise ValueError("closure_evidence must be sorted and unique")
    return value

  @field_validator("root_names")
  @classmethod
  def validate_root_names(cls, value: list[str]) -> list[str]:
    for item in value:
      _validate_component(item, "root_name")
    if value != sorted(set(value)):
      raise ValueError("root_names must be sorted and unique")
    return value

  @field_validator("route_name")
  @classmethod
  def validate_route_name(cls, value: str) -> str:
    if value.strip() != value or "/" in value or "\\" in value or "\x00" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
      raise ValueError("route_name is not a canonical route component")
    return value

  @field_validator("missing_segment_numbers")
  @classmethod
  def validate_missing_numbers(cls, value: list[int]) -> list[int]:
    if any(number < 0 or number > 1_000_000 for number in value):
      raise ValueError("missing_segment_numbers contains an invalid number")
    if value != sorted(set(value)):
      raise ValueError("missing_segment_numbers must be sorted and unique")
    return value

  @model_validator(mode="after")
  def validate_manifest(self) -> RouteInventoryManifest:
    roles = [stream.role for stream in self.expected_streams]
    if roles != sorted(set(roles)):
      raise ValueError("expected_streams must be sorted by unique role")
    roots = set(self.root_names)
    if any(stream.root_name not in roots for stream in self.expected_streams):
      raise ValueError("expected stream references an undeclared root")

    segment_numbers = [segment.number for segment in self.segments]
    if segment_numbers != sorted(set(segment_numbers)):
      raise ValueError("segments must be sorted by unique number")
    if set(segment_numbers) & set(self.missing_segment_numbers):
      raise ValueError("present and missing segment numbers overlap")
    if not segment_numbers:
      raise ValueError("route inventory must contain a captured segment")
    max_segment = segment_numbers[-1]
    if any(number > max_segment for number in self.missing_segment_numbers):
      raise ValueError("missing segment number exceeds the captured range")
    known_numbers = set(segment_numbers) | set(self.missing_segment_numbers)
    if known_numbers != set(range(max_segment + 1)):
      raise ValueError(
        "every segment through the captured range must be present or missing",
      )

    all_paths: set[str] = set()
    self._validate_files(self.route_files, roots, all_paths)
    expected_by_role = {stream.role: stream for stream in self.expected_streams}
    for segment in self.segments:
      self._validate_files(segment.files, roots, all_paths)
      file_paths = {item.relative_path: item for item in segment.files}
      stream_roles = [stream.role for stream in segment.streams]
      if stream_roles != roles:
        raise ValueError(
          f"segment {segment.number} must contain every expected role in order",
        )
      for item in segment.files:
        parsed = _parse_segment_path(item.relative_path)
        if parsed != (self.route_name, segment.number):
          raise ValueError(
            f"segment {segment.number} contains a file from another route or segment",
          )
      for stream in segment.streams:
        expected = expected_by_role[stream.role]
        if stream.status == "missing":
          continue
        assert stream.relative_path is not None
        actual = file_paths.get(stream.relative_path)
        if (
          actual is None
          or actual.size != stream.size
          or actual.mtime_ns != stream.mtime_ns
          or actual.sha256 != stream.sha256
          or actual.artifact_type != expected.artifact_type
          or actual.camera != expected.camera
          or actual.relative_path.split("/", 1)[0] != expected.root_name
        ):
          raise ValueError(
            f"present stream {stream.role!r} does not match its actual file",
          )

    has_missing_stream = any(stream.status == "missing" for segment in self.segments for stream in segment.streams)
    if self.generation == 1 and self.previous_manifest_sha256 is not None:
      raise ValueError("generation 1 must not have a previous manifest")
    if self.generation > 1 and self.previous_manifest_sha256 is None:
      raise ValueError("later generations require a previous manifest")
    if self.state == "complete":
      rlog_stream_count = sum(stream.artifact_type == "rlog" for stream in self.expected_streams)
      if not self.expected_streams or not self.segments or self.missing_segment_numbers or has_missing_stream or rlog_stream_count != 1:
        raise ValueError(
          "complete inventory requires exactly one rlog stream, present streams, and no gaps",
        )
      if self.capability_source != "configured+route_union":
        raise ValueError(
          "complete inventory requires explicitly configured capabilities",
        )
    if self.capability_source == "route_union_unconfigured":
      if self.state != "partial" or "expected_streams_unconfigured" not in self.closure_evidence:
        raise ValueError(
          "an unconfigured inventory must be partial and identify its evidence",
        )
    if (self.missing_segment_numbers or has_missing_stream) and self.state != "partial":
      raise ValueError("an inventory with missing content must be partial")
    return self

  @staticmethod
  def _validate_files(
    files: list[InventoryFile],
    roots: set[str],
    all_paths: set[str],
  ) -> None:
    paths = [item.relative_path for item in files]
    if paths != sorted(paths):
      raise ValueError("inventory files must be sorted by relative_path")
    for item in files:
      root_name = item.relative_path.split("/", 1)[0]
      if root_name not in roots:
        raise ValueError("inventory file references an undeclared root")
      if item.relative_path in all_paths:
        raise ValueError("inventory file paths must be unique route-wide")
      all_paths.add(item.relative_path)


class RouteInventoryEnvelope(StrictModel):
  device_id: str | None = Field(default=None, min_length=1, max_length=128)
  manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
  manifest: RouteInventoryManifest


class RouteInventoryAcceptance(StrictModel):
  manifest_sha256: str
  generation: int
  state: Literal["accepted"]


class RouteInventoryLatest(StrictModel):
  device_id: str
  route_name: str
  generation: int
  manifest_sha256: str
  manifest: RouteInventoryManifest


def _parse_segment_path(relative_path: str) -> tuple[str, int] | None:
  parts = relative_path.split("/")[1:]
  for part in parts:
    route, separator, number_text = part.rpartition("--")
    if separator and route and number_text.isdecimal():
      return route, int(number_text)
  if len(parts) >= 3 and parts[-2].isdecimal():
    return "--".join(parts[:-2]), int(parts[-2])
  return None


def _go_canonical_json(value: Any) -> bytes:
  encoded = json.dumps(
    value,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
  )
  encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
  return encoded.encode("utf-8")


def _manifest_digest(manifest: RouteInventoryManifest) -> tuple[str, str]:
  canonical = _go_canonical_json(
    manifest.model_dump(mode="json", by_alias=True),
  )
  return canonical.decode("utf-8"), hashlib.sha256(canonical).hexdigest()


def _rlog_source_fingerprint(
  manifest: RouteInventoryManifest,
) -> str | None:
  expected = {stream.role: stream for stream in manifest.expected_streams}
  entries = [
    {
      "segment_number": segment.number,
      "sha256": stream.sha256,
    }
    for segment in manifest.segments
    for stream in segment.streams
    if (stream.status == "present" and expected[stream.role].artifact_type == "rlog")
  ]
  entries.sort(key=lambda item: (item["segment_number"], item["sha256"]))
  if not entries:
    return None
  canonical = _go_canonical_json(entries)
  return hashlib.sha256(canonical).hexdigest()


def _acceptance(row: sqlite3.Row) -> dict[str, Any]:
  return {
    "manifest_sha256": row["manifest_sha256"],
    "generation": row["generation"],
    "state": "accepted",
  }


def _insert_inventory_files(
  connection: sqlite3.Connection,
  *,
  inventory_id: str,
  manifest: RouteInventoryManifest,
) -> None:
  rows: list[tuple[Any, ...]] = []
  for item in manifest.route_files:
    rows.append(
      (
        inventory_id,
        "route",
        None,
        f"@file:{item.relative_path}",
        0,
        "present",
        item.relative_path,
        item.artifact_type,
        item.camera,
        item.size,
        item.mtime_ns,
        item.sha256,
      ),
    )
  expected_by_role = {stream.role: stream for stream in manifest.expected_streams}
  for segment in manifest.segments:
    location = f"segment:{segment.number}"
    role_by_path = {stream.relative_path: stream.role for stream in segment.streams if stream.status == "present"}
    for item in segment.files:
      role = role_by_path.get(
        item.relative_path,
        f"@file:{item.relative_path}",
      )
      rows.append(
        (
          inventory_id,
          location,
          segment.number,
          role,
          int(item.relative_path in role_by_path),
          "present",
          item.relative_path,
          item.artifact_type,
          item.camera,
          item.size,
          item.mtime_ns,
          item.sha256,
        ),
      )
    for stream in segment.streams:
      if stream.status == "present":
        continue
      expected = expected_by_role[stream.role]
      rows.append(
        (
          inventory_id,
          location,
          segment.number,
          stream.role,
          1,
          "missing",
          None,
          expected.artifact_type,
          expected.camera,
          None,
          None,
          None,
        ),
      )
  connection.executemany(
    """
    INSERT INTO route_inventory_expected_files(
      inventory_id, location_key, segment_number, role, is_stream,
      status, relative_path, artifact_type, camera,
      declared_size, declared_mtime_ns, declared_sha256
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    rows,
  )


def _validate_manifest_artifact_limits(
  request: Request,
  manifest: RouteInventoryManifest,
) -> None:
  settings = request.app.state.settings
  limits = {
    "video": min(
      settings.max_artifact_bytes,
      settings.max_video_artifact_bytes,
    ),
    "rlog": min(
      settings.max_artifact_bytes,
      settings.max_log_artifact_bytes,
    ),
    "qlog": min(
      settings.max_artifact_bytes,
      settings.max_log_artifact_bytes,
    ),
    "artifact": min(
      settings.max_artifact_bytes,
      settings.max_other_artifact_bytes,
    ),
  }
  files = [
    *manifest.route_files,
    *(item for segment in manifest.segments for item in segment.files),
  ]
  for item in files:
    limit = limits[item.artifact_type]
    if item.size > limit:
      raise ApiError(
        413,
        "artifact_too_large",
        "Route inventory file exceeds the configured artifact limit",
        details={
          "relative_path": item.relative_path,
          "artifact_type": item.artifact_type,
          "maximum_size": limit,
        },
      )
    if item.artifact_type in {"video", "rlog", "qlog"} and item.size == 0:
      raise ApiError(
        422,
        "empty_artifact",
        "Video and log inventory files must not be empty",
        details={"relative_path": item.relative_path},
      )


def _inventory_row_count(manifest: RouteInventoryManifest) -> int:
  segment_count = len(manifest.segments) + len(manifest.missing_segment_numbers)
  return (
    len(manifest.route_files)
    + 2 * segment_count
    + len(manifest.missing_segment_numbers) * len(manifest.expected_streams)
    + sum(len(segment.files) + sum(stream.status == "missing" for stream in segment.streams) for segment in manifest.segments)
  )


def _inventory_expected_file_row_count(
  manifest: RouteInventoryManifest,
) -> int:
  return len(manifest.route_files) + sum(len(segment.files) + sum(stream.status == "missing" for stream in segment.streams) for segment in manifest.segments)


def _admit_inventory_request(
  *,
  actor_type: str,
  device_id: str,
) -> None:
  now = time.monotonic()
  cutoff = now - INVENTORY_RATE_WINDOW_SECONDS
  key = f"{actor_type}:{device_id}"
  limit = INVENTORY_IMPORT_REQUESTS_PER_WINDOW if actor_type == "importer" else INVENTORY_DEVICE_REQUESTS_PER_WINDOW
  with _inventory_rate_lock:
    attempts = _inventory_rate_attempts[key]
    while attempts and attempts[0] <= cutoff:
      attempts.popleft()
    if len(attempts) >= limit:
      retry_after = max(
        1,
        int(attempts[0] + INVENTORY_RATE_WINDOW_SECONDS - now) + 1,
      )
      raise ApiError(
        429,
        "route_inventory_rate_limited",
        "Route inventory admission is temporarily rate limited",
        headers={"Retry-After": str(retry_after)},
      )
    attempts.append(now)
    if len(_inventory_rate_attempts) > MAX_INVENTORY_ROUTES_PER_DEVICE:
      for candidate in list(_inventory_rate_attempts):
        queue = _inventory_rate_attempts[candidate]
        while queue and queue[0] <= cutoff:
          queue.popleft()
        if not queue:
          _inventory_rate_attempts.pop(candidate, None)


router = APIRouter(prefix="/route-inventories", tags=["route-inventories"])


@router.get("/latest", response_model=RouteInventoryLatest)
def get_latest_route_inventory(
  request: Request,
  device_id: str | None = None,
  route_name: str | None = None,
) -> dict[str, Any]:
  principal = request.app.state.auth.authenticate_device(
    request,
    allow_importer=True,
  )
  if not route_name or len(route_name) > 256:
    raise ApiError(
      422,
      "route_name_required",
      "route_name must identify one route",
    )
  if principal.actor_type == "device":
    if device_id is not None and device_id != principal.actor_id:
      raise ApiError(
        403,
        "device_mismatch",
        "Cannot inspect another device inventory",
      )
    resolved_device_id = principal.actor_id
  else:
    if not device_id:
      raise ApiError(
        422,
        "device_id_required",
        "Historical inventory lookup must specify device_id",
      )
    resolved_device_id = device_id
  active_device = request.app.state.database.query_one(
    "SELECT id FROM devices WHERE id = ? AND disabled_at IS NULL",
    (resolved_device_id,),
  )
  if active_device is None:
    raise ApiError(404, "device_not_found", "Device was not found")
  row = request.app.state.database.query_one(
    """
    SELECT
      device_id, route_name, generation, manifest_sha256, manifest_json
    FROM route_inventories
    WHERE device_id = ? AND route_name = ?
    ORDER BY generation DESC
    LIMIT 1
    """,
    (resolved_device_id, route_name),
  )
  if row is None:
    raise ApiError(
      404,
      "route_inventory_not_found",
      "No route inventory has been accepted for this route",
    )
  try:
    manifest = json.loads(row["manifest_json"])
  except (json.JSONDecodeError, TypeError) as exc:
    raise ApiError(
      500,
      "route_inventory_invalid",
      "Stored route inventory metadata is invalid",
    ) from exc
  return {
    "device_id": row["device_id"],
    "route_name": row["route_name"],
    "generation": row["generation"],
    "manifest_sha256": row["manifest_sha256"],
    "manifest": manifest,
  }


@router.post(
  "",
  response_model=RouteInventoryAcceptance,
  status_code=status.HTTP_201_CREATED,
)
def declare_route_inventory(
  request: Request,
  response: Response,
  payload: RouteInventoryEnvelope,
) -> dict[str, Any]:
  principal = request.app.state.auth.authenticate_device(
    request,
    allow_importer=True,
  )
  if principal.actor_type == "device":
    if payload.device_id is not None and payload.device_id != principal.actor_id:
      raise ApiError(
        403,
        "device_mismatch",
        "Cannot declare an inventory for another device",
      )
    device_id = principal.actor_id
  else:
    if payload.device_id is None:
      raise ApiError(
        422,
        "device_id_required",
        "Historical inventories must specify device_id",
      )
    device_id = payload.device_id
  _admit_inventory_request(
    actor_type=principal.actor_type,
    device_id=device_id,
  )
  expanded_rows = _inventory_row_count(payload.manifest)
  if expanded_rows > MAX_INVENTORY_ROWS:
    raise ApiError(
      413,
      "route_inventory_too_large",
      "Route inventory expands beyond the server row limit",
      details={
        "rows": expanded_rows,
        "maximum_rows": MAX_INVENTORY_ROWS,
      },
    )
  expected_key = f"route-inventory:{payload.manifest_sha256}"
  if require_idempotency_key(request) != expected_key:
    raise ApiError(
      422,
      "invalid_idempotency_key",
      "Idempotency-Key must identify the exact route inventory digest",
      details={"expected": expected_key},
    )
  manifest_json, actual_digest = _manifest_digest(payload.manifest)
  if not secrets.compare_digest(actual_digest, payload.manifest_sha256):
    raise ApiError(
      422,
      "manifest_digest_mismatch",
      "manifest_sha256 does not match the canonical manifest",
      details={
        "declared_sha256": payload.manifest_sha256,
        "actual_sha256": actual_digest,
      },
    )
  _validate_manifest_artifact_limits(request, payload.manifest)

  database: Database = request.app.state.database
  now_text = isoformat()
  manifest = payload.manifest
  rlog_source_fingerprint = _rlog_source_fingerprint(manifest)
  manifest_size = len(manifest_json.encode("utf-8"))
  segment_count = len(manifest.segments) + len(manifest.missing_segment_numbers)
  expected_file_count = _inventory_expected_file_row_count(manifest)
  with database.transaction(immediate=True) as connection:
    device = connection.execute(
      "SELECT id FROM devices WHERE id = ? AND disabled_at IS NULL",
      (device_id,),
    ).fetchone()
    if device is None:
      raise ApiError(404, "device_not_found", "Device was not found")
    replay = connection.execute(
      """
      SELECT manifest_sha256, generation
      FROM route_inventories
      WHERE device_id = ? AND route_name = ? AND manifest_sha256 = ?
      """,
      (device_id, manifest.route_name, payload.manifest_sha256),
    ).fetchone()
    if replay is not None:
      response.status_code = status.HTTP_200_OK
      return _acceptance(replay)

    latest = connection.execute(
      """
      SELECT manifest_sha256, generation
      FROM route_inventories
      WHERE device_id = ? AND route_name = ?
      ORDER BY generation DESC
      LIMIT 1
      """,
      (device_id, manifest.route_name),
    ).fetchone()
    if latest is None:
      chain_valid = manifest.generation == 1 and manifest.previous_manifest_sha256 is None
    else:
      chain_valid = manifest.generation == latest["generation"] + 1 and manifest.previous_manifest_sha256 == latest["manifest_sha256"]
    if not chain_valid:
      raise ApiError(
        409,
        "route_inventory_generation_conflict",
        "Route inventory does not extend the latest immutable generation",
        details={
          "latest_generation": (latest["generation"] if latest is not None else None),
          "latest_manifest_sha256": (latest["manifest_sha256"] if latest is not None else None),
        },
      )

    drive = connection.execute(
      "SELECT id FROM drives WHERE device_id = ? AND route_name = ?",
      (device_id, manifest.route_name),
    ).fetchone()
    if drive is None:
      existing_segment_count = 0
    else:
      existing_segment_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM segments
        WHERE drive_id = ? AND number BETWEEN 0 AND ?
        """,
        (drive["id"], manifest.segments[-1].number),
      ).fetchone()[0]
    new_placeholder_count = segment_count - existing_segment_count
    materialized_row_count = segment_count + expected_file_count + new_placeholder_count
    connection.execute(
      """
      INSERT OR IGNORE INTO route_inventory_usage(
        device_id, route_count, inventory_count,
        manifest_bytes, materialized_rows
      ) VALUES (?, 0, 0, 0, 0)
      """,
      (device_id,),
    )
    quotas = connection.execute(
      """
      SELECT
        route_count, inventory_count, manifest_bytes, materialized_rows
      FROM route_inventory_usage
      WHERE device_id = ?
      """,
      (device_id,),
    ).fetchone()
    assert quotas is not None
    quota_reason: str | None = None
    if latest is not None and latest["generation"] >= MAX_INVENTORY_GENERATIONS_PER_ROUTE:
      quota_reason = "route_generation_limit"
    elif latest is None and quotas["route_count"] >= MAX_INVENTORY_ROUTES_PER_DEVICE:
      quota_reason = "device_route_limit"
    elif quotas["inventory_count"] >= MAX_INVENTORY_RECORDS_PER_DEVICE:
      quota_reason = "device_inventory_limit"
    elif quotas["manifest_bytes"] + manifest_size > MAX_INVENTORY_BYTES_PER_DEVICE:
      quota_reason = "device_manifest_bytes_limit"
    elif quotas["materialized_rows"] + materialized_row_count > MAX_INVENTORY_MATERIALIZED_ROWS_PER_DEVICE:
      quota_reason = "device_materialized_rows_limit"
    if quota_reason is not None:
      raise ApiError(
        status.HTTP_507_INSUFFICIENT_STORAGE,
        "route_inventory_quota_exceeded",
        "Route inventory storage quota is exhausted",
        details={"reason": quota_reason},
      )
    if drive is None:
      drive_id = uuid4().hex
      connection.execute(
        """
        INSERT INTO drives(
          id, device_id, route_name, route_state, updated_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
          drive_id,
          device_id,
          manifest.route_name,
          manifest.state,
          now_text,
          now_text,
        ),
      )
    else:
      drive_id = drive["id"]
      connection.execute(
        """
        UPDATE drives
        SET route_state = ?, updated_at = ?
        WHERE id = ?
        """,
        (manifest.state, now_text, drive_id),
      )

    inventory_id = uuid4().hex
    connection.execute(
      """
      INSERT INTO route_inventories(
        id, device_id, drive_id, route_name, generation,
        manifest_sha256, previous_manifest_sha256,
        rlog_source_fingerprint, manifest_size,
        materialized_row_count, state,
        route_closed, manifest_json, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        inventory_id,
        device_id,
        drive_id,
        manifest.route_name,
        manifest.generation,
        payload.manifest_sha256,
        manifest.previous_manifest_sha256,
        rlog_source_fingerprint,
        manifest_size,
        materialized_row_count,
        manifest.state,
        int(manifest.route_closed),
        manifest_json,
        now_text,
      ),
    )
    telemetry = connection.execute(
      """
      SELECT state, manifest_json, source_fingerprint
      FROM telemetry_indexes
      WHERE drive_id = ?
      """,
      (drive_id,),
    ).fetchone()
    try:
      telemetry_manifest = json.loads(telemetry["manifest_json"]) if telemetry is not None else None
    except (json.JSONDecodeError, TypeError):
      telemetry_manifest = None
    telemetry_matches = (
      telemetry is not None
      and telemetry["state"] == "complete"
      and isinstance(telemetry_manifest, dict)
      and telemetry_manifest.get("state") == "complete"
      and telemetry_manifest.get("publication_ready") is True
      and manifest.state == "complete"
      and rlog_source_fingerprint is not None
      and isinstance(telemetry["source_fingerprint"], str)
      and secrets.compare_digest(
        telemetry["source_fingerprint"],
        rlog_source_fingerprint,
      )
    )
    if telemetry_matches:
      connection.execute(
        """
        UPDATE drives
        SET telemetry_ready = 1, updated_at = ?
        WHERE id = ?
        """,
        (now_text, drive_id),
      )
    else:
      connection.execute(
        "UPDATE drives SET telemetry_ready = 0 WHERE id = ?",
        (drive_id,),
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
    segment_numbers = [(segment.number, 0) for segment in manifest.segments] + [(number, 1) for number in manifest.missing_segment_numbers]
    connection.executemany(
      """
      INSERT INTO route_inventory_segments(
        inventory_id, segment_number, missing
      ) VALUES (?, ?, ?)
      """,
      [(inventory_id, number, missing) for number, missing in segment_numbers],
    )
    for number, _missing in segment_numbers:
      existing_segment = connection.execute(
        "SELECT id FROM segments WHERE drive_id = ? AND number = ?",
        (drive_id, number),
      ).fetchone()
      if existing_segment is None:
        connection.execute(
          """
          INSERT INTO segments(id, drive_id, number, created_at)
          VALUES (?, ?, ?, ?)
          """,
          (uuid4().hex, drive_id, number, now_text),
        )
    _insert_inventory_files(
      connection,
      inventory_id=inventory_id,
      manifest=manifest,
    )
    connection.execute(
      """
      UPDATE route_inventory_usage
      SET route_count = route_count + ?,
        inventory_count = inventory_count + 1,
        manifest_bytes = manifest_bytes + ?,
        materialized_rows = materialized_rows + ?
      WHERE device_id = ?
      """,
      (
        int(latest is None),
        manifest_size,
        materialized_row_count,
        device_id,
      ),
    )
    audit(
      connection,
      actor_type=principal.actor_type,
      actor_id=principal.actor_id,
      action="route_inventory.accept",
      resource_type="route_inventory",
      resource_id=inventory_id,
      details={
        "route_name": manifest.route_name,
        "device_id": device_id,
        "generation": manifest.generation,
        "manifest_sha256": payload.manifest_sha256,
        "state": manifest.state,
      },
      ip_address=request_ip(request),
    )
    accepted = connection.execute(
      """
      SELECT manifest_sha256, generation
      FROM route_inventories
      WHERE id = ?
      """,
      (inventory_id,),
    ).fetchone()
  assert accepted is not None
  return _acceptance(accepted)


def latest_inventory_view(
  database: Database,
  drive_id: str,
) -> dict[str, Any] | None:
  row = database.query_one(
    """
    SELECT
      ri.id, ri.manifest_sha256, ri.generation, ri.state,
      ri.route_closed, ri.manifest_json,
      COUNT(DISTINCT CASE
        WHEN declared.status = 'present' THEN
          declared.location_key || ':' || declared.role
      END) AS declared_file_count,
      COUNT(DISTINCT CASE
        WHEN declared.status = 'present' AND EXISTS (
          SELECT 1
          FROM artifacts archived
          WHERE archived.drive_id = ri.drive_id
            AND archived.device_id = ri.device_id
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
        ) THEN declared.location_key || ':' || declared.role
      END) AS archived_file_count,
      COUNT(DISTINCT CASE
        WHEN declared.status = 'missing' OR (
          declared.status = 'present' AND NOT EXISTS (
            SELECT 1
            FROM artifacts archived
            WHERE archived.drive_id = ri.drive_id
              AND archived.device_id = ri.device_id
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
        ) THEN declared.location_key || ':' || declared.role
      END) AS missing_file_count
    FROM route_inventories ri
    LEFT JOIN route_inventory_expected_files declared
      ON declared.inventory_id = ri.id
    WHERE ri.drive_id = ?
      AND ri.generation = (
        SELECT MAX(latest.generation)
        FROM route_inventories latest
        WHERE latest.drive_id = ri.drive_id
      )
    GROUP BY ri.id
    """,
    (drive_id,),
  )
  if row is None:
    return None
  try:
    manifest = json.loads(row["manifest_json"])
  except (json.JSONDecodeError, TypeError) as exc:
    raise ApiError(
      500,
      "route_inventory_invalid",
      "Stored route inventory metadata is invalid",
    ) from exc
  implicit_missing_files = len(manifest["missing_segment_numbers"]) * len(manifest["expected_streams"])
  return {
    "manifest_sha256": row["manifest_sha256"],
    "generation": row["generation"],
    "state": row["state"],
    "route_closed": bool(row["route_closed"]),
    "capability_source": manifest["capability_source"],
    "closure_evidence": manifest["closure_evidence"],
    "expected_streams": manifest["expected_streams"],
    "missing_segment_numbers": manifest["missing_segment_numbers"],
    "declared_file_count": row["declared_file_count"],
    "archived_file_count": row["archived_file_count"],
    "missing_file_count": (row["missing_file_count"] + implicit_missing_files),
  }


def segment_expected_streams(
  database: Database,
  drive_id: str,
  manifest_sha256: str,
) -> dict[int, list[dict[str, Any]]]:
  inventory = database.query_one(
    """
    SELECT id, manifest_json
    FROM route_inventories
    WHERE drive_id = ? AND manifest_sha256 = ?
    """,
    (drive_id, manifest_sha256),
  )
  if inventory is None:
    return {}
  try:
    manifest = json.loads(inventory["manifest_json"])
  except (json.JSONDecodeError, TypeError) as exc:
    raise ApiError(
      500,
      "route_inventory_invalid",
      "Stored route inventory metadata is invalid",
    ) from exc
  rows = database.query_all(
    """
    SELECT
      expected.segment_number, expected.role, expected.artifact_type,
      expected.camera, expected.status, expected.relative_path,
      source.id AS source_id, source.status AS source_status,
      (
        SELECT derived.status
        FROM artifacts derived
        WHERE derived.source_artifact_id = source.id
          AND derived.kind = 'derived_video'
          AND LOWER(derived.codec) = 'av1'
        ORDER BY
          CASE derived.status
            WHEN 'ready' THEN 4
            WHEN 'verified' THEN 3
            WHEN 'stored' THEN 2
            WHEN 'failed' THEN 1
            ELSE 0
          END DESC,
          derived.created_at DESC,
          derived.id DESC
        LIMIT 1
      ) AS media_status
    FROM route_inventories inventory
    JOIN route_inventory_expected_files expected
      ON expected.inventory_id = inventory.id
      AND expected.is_stream = 1
      AND expected.segment_number IS NOT NULL
    LEFT JOIN artifacts source
      ON source.drive_id = inventory.drive_id
      AND source.device_id = inventory.device_id
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
      AND COALESCE(source.camera, '') = COALESCE(expected.camera, '')
      AND source.status IN ('stored', 'verified', 'ready')
    WHERE inventory.id = ?
    ORDER BY expected.segment_number, expected.role
    """,
    (inventory["id"],),
  )
  result: dict[int, list[dict[str, Any]]] = {}
  for row in rows:
    source_status = row["source_status"]
    archive_status = source_status if source_status in {"stored", "verified", "ready"} else "missing"
    if row["artifact_type"] != "video":
      media_status = "not_required"
    elif row["media_status"] == "ready":
      media_status = "ready"
    elif row["media_status"] == "failed":
      media_status = "failed"
    else:
      media_status = "pending"
    result.setdefault(row["segment_number"], []).append(
      {
        "role": row["role"],
        "artifact_type": row["artifact_type"],
        "camera": row["camera"],
        "manifest_status": row["status"],
        "relative_path": row["relative_path"],
        "archive_status": archive_status,
        "media_status": media_status,
      },
    )
  for number in manifest["missing_segment_numbers"]:
    result[number] = [
      {
        "role": expected["role"],
        "artifact_type": expected["artifact_type"],
        "camera": expected["camera"],
        "manifest_status": "missing",
        "relative_path": None,
        "archive_status": "missing",
        "media_status": ("pending" if expected["artifact_type"] == "video" else "not_required"),
      }
      for expected in manifest["expected_streams"]
    ]
  return result
