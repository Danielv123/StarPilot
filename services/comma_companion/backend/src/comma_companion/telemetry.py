from __future__ import annotations

import json
import hashlib
import hmac
import math
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal

from fastapi import APIRouter, Query, Request

from .auth import ApiError
from .db import Database
from .models import SeriesResponse


TIER_ORDER = ("full", "100ms", "500ms", "2s", "10s")
READY_INDEX_STATES = {"complete", "partial"}
VALID_KINDS = {"continuous", "step", "event"}
MAX_SIGNALS = 64
MAX_SIGNAL_ID_LENGTH = 256
MAX_RECORD_BYTES = 16 * 1024 * 1024
SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = (2**63) - 1
SHA256_LENGTH = 64

router = APIRouter(prefix="/drives", tags=["telemetry"])


@dataclass(frozen=True, slots=True)
class _Point:
  t_us: int
  value: float | bool | str | None
  chunk_index: int
  point_index: int


@dataclass(frozen=True, slots=True)
class _SignalMetadata:
  kind: Literal["continuous", "step", "event"]
  unit: str | None


@dataclass(slots=True)
class _WindowPoints:
  points: list[_Point]
  before: _Point | None
  after: _Point | None
  overflow: bool = False


class TelemetryRecordLoader:
  def __init__(self, archive_root: Path, ndjson_path: str):
    if not isinstance(ndjson_path, str) or not ndjson_path or "\\" in ndjson_path:
      raise _invalid_index()
    relative = PurePosixPath(ndjson_path)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != ndjson_path:
      raise _invalid_index()
    resolved_archive_root = archive_root.resolve()
    expected_telemetry_root = resolved_archive_root / "telemetry"
    telemetry_root = expected_telemetry_root.resolve()
    if telemetry_root != expected_telemetry_root:
      raise _invalid_index()
    absolute = (resolved_archive_root / Path(*relative.parts)).resolve()
    if absolute == telemetry_root or not absolute.is_relative_to(telemetry_root):
      raise _invalid_index()
    self._ndjson_path = ndjson_path
    self._absolute = absolute
    self._stream: BinaryIO | None = None

  def __enter__(self) -> TelemetryRecordLoader:
    return self

  def __exit__(self, *_: Any) -> None:
    if self._stream is not None:
      self._stream.close()
      self._stream = None

  def _open(self) -> BinaryIO:
    if self._stream is None:
      try:
        stream = self._absolute.open("rb")
      except OSError as exc:
        raise _invalid_index() from exc
      try:
        regular = stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
      except OSError as exc:
        stream.close()
        raise _invalid_index() from exc
      if not regular:
        stream.close()
        raise _invalid_index()
      self._stream = stream
    return self._stream

  def load(self, row: sqlite3.Row) -> tuple[Any, bool]:
    keys = set(row.keys())
    if "data_json" in keys and row["data_json"] is not None:
      raw = row["data_json"]
      if not isinstance(raw, str) or not raw:
        raise _invalid_index()
      try:
        return _strict_json_loads(raw), False
      except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _invalid_index() from exc

    required = {
      "ndjson_path",
      "byte_offset",
      "byte_length",
      "record_sha256",
    }
    if not required.issubset(keys):
      raise _invalid_index()
    path = row["ndjson_path"]
    offset = row["byte_offset"]
    length = row["byte_length"]
    digest = row["record_sha256"]
    if path != self._ndjson_path:
      raise _invalid_index()
    if (
      not isinstance(offset, int)
      or isinstance(offset, bool)
      or not 0 <= offset <= SQLITE_INT_MAX
      or not isinstance(length, int)
      or isinstance(length, bool)
      or not 1 <= length <= MAX_RECORD_BYTES
      or not isinstance(digest, str)
      or len(digest) != 64
      or digest != digest.lower()
      or any(character not in "0123456789abcdef" for character in digest)
    ):
      raise _invalid_index()
    stream = self._open()
    try:
      stream.seek(offset)
      payload_bytes = stream.read(length)
    except OSError as exc:
      raise _invalid_index() from exc
    if (
      len(payload_bytes) != length
      or not payload_bytes.startswith(b"{")
      or not payload_bytes.endswith(b"}\n")
      or b"\n" in payload_bytes[:-1]
      or b"\r" in payload_bytes
      or not hmac.compare_digest(
        hashlib.sha256(payload_bytes).hexdigest(),
        digest,
      )
    ):
      raise _invalid_index()
    try:
      payload_text = payload_bytes[:-1].decode("utf-8")
      return _strict_json_loads(payload_text), True
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
      raise _invalid_index() from exc


def _invalid_index() -> ApiError:
  return ApiError(
    500,
    "telemetry_index_invalid",
    "Telemetry index data is invalid",
  )


def _reject_json_constant(value: str) -> None:
  raise ValueError(f"non-finite JSON constant: {value}")


def _strict_json_loads(value: str) -> Any:
  return json.loads(value, parse_constant=_reject_json_constant)


def _valid_sha256(value: Any) -> bool:
  return isinstance(value, str) and len(value) == SHA256_LENGTH and value == value.lower() and all(character in "0123456789abcdef" for character in value)


def _generation_not_ready() -> ApiError:
  return ApiError(
    409,
    "telemetry_not_ready",
    "A valid telemetry generation is not ready for this drive",
  )


def _telemetry_generation(
  index: sqlite3.Row,
) -> tuple[str, str, Literal["stable", "provisional"]]:
  ndjson_sha256 = index["ndjson_sha256"]
  if not _valid_sha256(ndjson_sha256):
    raise _generation_not_ready()
  try:
    manifest = _strict_json_loads(index["manifest_json"])
  except (TypeError, ValueError, json.JSONDecodeError) as exc:
    raise _generation_not_ready() from exc
  if not isinstance(manifest, dict):
    raise _generation_not_ready()
  timeline_version = manifest.get("timeline_version")
  if not _valid_sha256(timeline_version):
    raise _generation_not_ready()

  state = index["state"]
  manifest_state = manifest.get("state")
  publication_ready = manifest.get("publication_ready")
  complete = state == "complete" and manifest_state == "complete" and publication_ready is True
  partial = state == "partial" and manifest_state == "partial" and publication_ready is False
  if not complete and not partial:
    raise _generation_not_ready()
  return (
    ndjson_sha256,
    timeline_version,
    "stable" if complete else "provisional",
  )


def _parse_signals(value: str) -> list[str]:
  result: list[str] = []
  seen: set[str] = set()
  for raw_signal in value.split(","):
    signal = raw_signal.strip()
    if signal and signal not in seen:
      seen.add(signal)
      result.append(signal)
  if not result:
    raise ApiError(
      422,
      "invalid_signals",
      "signals must contain at least one signal ID",
    )
  if len(result) > MAX_SIGNALS:
    raise ApiError(
      422,
      "invalid_signals",
      f"signals must contain at most {MAX_SIGNALS} unique signal IDs",
      details={"maximum": MAX_SIGNALS},
    )
  oversized = [signal for signal in result if len(signal) > MAX_SIGNAL_ID_LENGTH]
  if oversized:
    raise ApiError(
      422,
      "invalid_signals",
      f"signal IDs must not exceed {MAX_SIGNAL_ID_LENGTH} characters",
      details={"maximum_length": MAX_SIGNAL_ID_LENGTH},
    )
  return result


def _catalog_kind(item: dict[str, Any]) -> Literal["continuous", "step", "event"]:
  explicit = item.get("kind")
  if explicit in VALID_KINDS:
    return explicit
  interpolation = item.get("interpolation")
  value_type = item.get("value_type")
  if interpolation == "event" or value_type == "event":
    return "event"
  if interpolation == "step" or value_type in {"bool", "enum", "text", "string"}:
    return "step"
  return "continuous"


def _catalog_metadata(raw_catalog: str) -> dict[str, _SignalMetadata]:
  try:
    payload = _strict_json_loads(raw_catalog)
  except (TypeError, ValueError, json.JSONDecodeError) as exc:
    raise _invalid_index() from exc
  if isinstance(payload, dict):
    items = payload.get("signals", [])
  elif isinstance(payload, list):
    items = payload
  else:
    raise _invalid_index()
  if not isinstance(items, list):
    raise _invalid_index()

  result: dict[str, _SignalMetadata] = {}
  for item in items:
    if not isinstance(item, dict):
      raise _invalid_index()
    signal_id = item.get("id", item.get("signal"))
    unit = item.get("unit")
    if not isinstance(signal_id, str) or not signal_id:
      raise _invalid_index()
    if unit is not None and not isinstance(unit, str):
      raise _invalid_index()
    metadata = _SignalMetadata(kind=_catalog_kind(item), unit=unit)
    existing = result.get(signal_id)
    if existing is not None and existing != metadata:
      raise _invalid_index()
    result[signal_id] = metadata
  return result


def _chunk_metadata(
  connection: sqlite3.Connection,
  drive_id: str,
  signal_ids: list[str],
) -> dict[str, dict[str, _SignalMetadata]]:
  placeholders = ",".join("?" for _ in signal_ids)
  rows = connection.execute(
    f"""
    SELECT signal_id, tier, kind, unit
    FROM telemetry_series_chunks
    WHERE drive_id = ? AND signal_id IN ({placeholders})
    GROUP BY signal_id, tier, kind, unit
    ORDER BY signal_id, tier, kind, unit
    """,
    (drive_id, *signal_ids),
  ).fetchall()
  result: dict[str, dict[str, _SignalMetadata]] = {}
  for row in rows:
    tier = row["tier"]
    if tier not in TIER_ORDER:
      continue
    kind = row["kind"]
    unit = row["unit"]
    if kind not in VALID_KINDS:
      raise _invalid_index()
    if unit is not None and not isinstance(unit, str):
      raise _invalid_index()
    metadata = _SignalMetadata(kind=kind, unit=unit)
    tiers = result.setdefault(row["signal_id"], {})
    existing = tiers.get(tier)
    if existing is not None and existing != metadata:
      raise _invalid_index()
    tiers[tier] = metadata

  for tiers in result.values():
    if len(set(tiers.values())) > 1:
      raise _invalid_index()
  return result


def _normalize_value(value: Any) -> float | bool | str | None:
  if value is None or isinstance(value, (bool, str)):
    return value
  if isinstance(value, (int, float)) and not isinstance(value, bool):
    try:
      normalized = float(value)
    except (OverflowError, ValueError) as exc:
      raise _invalid_index() from exc
    if math.isfinite(normalized):
      return normalized
  raise _invalid_index()


def _read_markers(
  connection: sqlite3.Connection,
  drive_id: str,
  start_us: int,
  end_us: int,
  max_markers: int,
) -> tuple[list[dict[str, Any]], bool]:
  rows = connection.execute(
    """
    SELECT *
    FROM telemetry_markers
    WHERE drive_id = ?
      AND end_t_us >= ?
      AND start_t_us <= ?
    ORDER BY start_t_us, end_t_us, marker_id
    LIMIT ?
    """,
    (drive_id, start_us, end_us, max_markers + 1),
  ).fetchall()
  truncated = len(rows) > max_markers
  result: list[dict[str, Any]] = []
  for row in rows[:max_markers]:
    try:
      record = _strict_json_loads(row["data_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
      raise _invalid_index() from exc
    attributes = (
      record.get("attributes", {})
      if isinstance(record, dict)
      else None
    )
    record_end_us = (
      record.get("start_us")
      if isinstance(record, dict) and record.get("end_us") is None
      else record.get("end_us")
      if isinstance(record, dict)
      else None
    )
    if (
      not isinstance(record, dict)
      or record.get("record") != "marker"
      or record.get("id") != row["marker_id"]
      or record.get("kind") != row["kind"]
      or record.get("start_us") != row["start_t_us"]
      or record_end_us != row["end_t_us"]
      or record.get("severity") != row["severity"]
      or record.get("label") != row["label"]
      or not isinstance(row["marker_id"], str)
      or not row["marker_id"]
      or not isinstance(row["kind"], str)
      or not row["kind"]
      or not isinstance(row["start_t_us"], int)
      or isinstance(row["start_t_us"], bool)
      or not isinstance(row["end_t_us"], int)
      or isinstance(row["end_t_us"], bool)
      or not SQLITE_INT_MIN <= row["start_t_us"] <= row["end_t_us"] <= SQLITE_INT_MAX
      or (row["severity"] is not None and not isinstance(row["severity"], str))
      or (row["label"] is not None and not isinstance(row["label"], str))
      or not isinstance(attributes, dict)
    ):
      raise _invalid_index()
    result.append(
      {
        "id": row["marker_id"],
        "kind": row["kind"],
        "start_t_us": row["start_t_us"],
        "end_t_us": row["end_t_us"],
        "severity": row["severity"],
        "label": row["label"],
        "attributes": attributes,
      }
    )
  return result, truncated


def _parse_chunk(
  row: sqlite3.Row,
  signal_id: str,
  tier: str,
  records: TelemetryRecordLoader,
) -> list[_Point]:
  payload, referenced = records.load(row)
  if not isinstance(payload, dict):
    raise _invalid_index()
  if (referenced and payload.get("record") != "series_chunk") or (not referenced and payload.get("record", "series_chunk") != "series_chunk"):
    raise _invalid_index()
  if (referenced and payload.get("signal") != signal_id) or (not referenced and payload.get("signal", signal_id) != signal_id):
    raise _invalid_index()
  if (referenced and payload.get("tier") != tier) or (not referenced and payload.get("tier", tier) != tier):
    raise _invalid_index()
  chunk_index = row["chunk_index"]
  if (
    not isinstance(chunk_index, int)
    or isinstance(chunk_index, bool)
    or chunk_index < 0
    or (referenced and payload.get("chunk") != chunk_index)
    or (not referenced and payload.get("chunk", chunk_index) != chunk_index)
  ):
    raise _invalid_index()
  times = payload.get("t_us")
  values = payload.get("v")
  if not isinstance(times, list) or not isinstance(values, list) or not times or len(times) != len(values):
    raise _invalid_index()

  result: list[_Point] = []
  prior_t_us: int | None = None
  for point_index, (t_us, value) in enumerate(zip(times, values, strict=True)):
    if not isinstance(t_us, int) or isinstance(t_us, bool) or not SQLITE_INT_MIN <= t_us <= SQLITE_INT_MAX or (prior_t_us is not None and t_us < prior_t_us):
      raise _invalid_index()
    result.append(
      _Point(
        t_us=t_us,
        value=_normalize_value(value),
        chunk_index=chunk_index,
        point_index=point_index,
      )
    )
    prior_t_us = t_us
  if row["start_t_us"] != times[0] or row["end_t_us"] != times[-1]:
    raise _invalid_index()
  return result


def _value_key(value: float | bool | str | None) -> tuple[str, Any]:
  return type(value).__name__, value


def _point_order(point: _Point) -> tuple[int, int, int]:
  return point.t_us, point.chunk_index, point.point_index


def _load_window(
  connection: sqlite3.Connection,
  records: TelemetryRecordLoader,
  drive_id: str,
  signal_id: str,
  tier: str,
  start_us: int,
  end_us: int,
  *,
  limit: int | None,
) -> _WindowPoints:
  points: list[_Point] = []
  seen: set[tuple[int, tuple[str, Any]]] = set()
  before: _Point | None = None
  after: _Point | None = None
  rows = connection.execute(
    """
    SELECT *
    FROM telemetry_series_chunks
    WHERE drive_id = ?
      AND signal_id = ?
      AND tier = ?
      AND end_t_us >= ?
      AND start_t_us <= ?
    ORDER BY start_t_us, chunk_index
    """,
    (drive_id, signal_id, tier, start_us, end_us),
  )
  for row in rows:
    for point in _parse_chunk(row, signal_id, tier, records):
      if point.t_us < start_us:
        if before is None or _point_order(point) > _point_order(before):
          before = point
        continue
      if point.t_us > end_us:
        if after is None or _point_order(point) < _point_order(after):
          after = point
        continue
      key = point.t_us, _value_key(point.value)
      if key in seen:
        continue
      seen.add(key)
      points.append(point)
      if limit is not None and len(points) > limit:
        return _WindowPoints([], before, after, overflow=True)
  points.sort(key=_point_order)
  return _WindowPoints(points, before, after)


def _adjacent_neighbor(
  connection: sqlite3.Connection,
  records: TelemetryRecordLoader,
  drive_id: str,
  signal_id: str,
  tier: str,
  boundary_us: int,
  *,
  before: bool,
) -> _Point | None:
  comparison = "end_t_us < ?" if before else "start_t_us > ?"
  ordering = "end_t_us DESC, start_t_us DESC, chunk_index DESC" if before else "start_t_us, end_t_us, chunk_index"
  rows = connection.execute(
    f"""
    SELECT *
    FROM telemetry_series_chunks
    WHERE drive_id = ?
      AND signal_id = ?
      AND tier = ?
      AND {comparison}
    ORDER BY {ordering}
    """,
    (drive_id, signal_id, tier, boundary_us),
  )
  for row in rows:
    candidates = []
    for point in _parse_chunk(row, signal_id, tier, records):
      if (before and point.t_us < boundary_us) or (not before and point.t_us > boundary_us):
        candidates.append(point)
    if candidates:
      return max(candidates, key=_point_order) if before else min(candidates, key=_point_order)
  return None


def _linear_deviation(
  point: _Point,
  first: _Point,
  last: _Point,
) -> float:
  if not isinstance(point.value, float) or not isinstance(first.value, float) or not isinstance(last.value, float):
    return 0.0
  duration = last.t_us - first.t_us
  if duration <= 0:
    expected = first.value
  else:
    fraction = (point.t_us - first.t_us) / duration
    expected = first.value + ((last.value - first.value) * fraction)
  return abs(point.value - expected)


def _thin_continuous(points: list[_Point], max_points: int) -> list[_Point]:
  if len(points) <= max_points:
    return points
  if max_points == 2:
    return [points[0], points[-1]]
  if not all(isinstance(point.value, float) for point in points):
    indexes = {round(index * (len(points) - 1) / (max_points - 1)) for index in range(max_points)}
    return [points[index] for index in sorted(indexes)]

  interior = points[1:-1]
  slots = max_points - 2
  pair_buckets = slots // 2
  single_buckets = slots % 2
  bucket_count = pair_buckets + single_buckets
  selected_indexes = {0, len(points) - 1}
  for bucket_index in range(bucket_count):
    start = (bucket_index * len(interior)) // bucket_count
    end = ((bucket_index + 1) * len(interior)) // bucket_count
    bucket_indexes = list(range(start + 1, end + 1))
    if not bucket_indexes:
      continue
    if bucket_index < pair_buckets:
      minimum_index = min(
        bucket_indexes,
        key=lambda index: (points[index].value, _point_order(points[index])),
      )
      maximum_index = max(
        bucket_indexes,
        key=lambda index: (
          points[index].value,
          tuple(-item for item in _point_order(points[index])),
        ),
      )
      selected_indexes.add(minimum_index)
      selected_indexes.add(maximum_index)
    else:
      standout_index = max(
        bucket_indexes,
        key=lambda index: (
          _linear_deviation(points[index], points[0], points[-1]),
          tuple(-item for item in _point_order(points[index])),
        ),
      )
      selected_indexes.add(standout_index)

  if len(selected_indexes) < max_points:
    remaining = sorted(
      (index for index in range(1, len(points) - 1) if index not in selected_indexes),
      key=lambda index: (
        -_linear_deviation(points[index], points[0], points[-1]),
        index,
      ),
    )
    selected_indexes.update(remaining[: max_points - len(selected_indexes)])
  return [points[index] for index in sorted(selected_indexes)]


def _evenly_spaced(points: list[_Point], max_points: int) -> list[_Point]:
  if len(points) <= max_points:
    return points
  indexes = [(slot * (len(points) - 1)) // (max_points - 1) for slot in range(max_points)]
  return [points[index] for index in indexes]


def _thin_step(points: list[_Point], max_points: int) -> list[_Point]:
  if len(points) <= max_points:
    return points
  transitions = [points[0]]
  for point in points[1:]:
    if _value_key(point.value) != _value_key(transitions[-1].value):
      transitions.append(point)
  if transitions[-1] is not points[-1]:
    transitions.append(points[-1])
  return _evenly_spaced(transitions, max_points)


def _thin_points(
  points: list[_Point],
  kind: Literal["continuous", "step", "event"],
  max_points: int,
) -> list[_Point]:
  if kind == "continuous":
    return _thin_continuous(points, max_points)
  if kind == "step":
    return _thin_step(points, max_points)
  return _evenly_spaced(points, max_points)


def _with_neighbors(
  connection: sqlite3.Connection,
  records: TelemetryRecordLoader,
  drive_id: str,
  signal_id: str,
  tier: str,
  metadata: _SignalMetadata,
  window: _WindowPoints,
  start_us: int,
  end_us: int,
) -> list[_Point]:
  points = list(window.points)
  if metadata.kind in {"continuous", "step"}:
    before = window.before or _adjacent_neighbor(
      connection,
      records,
      drive_id,
      signal_id,
      tier,
      start_us,
      before=True,
    )
    if before is not None:
      points.insert(0, before)
  if metadata.kind == "continuous":
    after = window.after or _adjacent_neighbor(
      connection,
      records,
      drive_id,
      signal_id,
      tier,
      end_us,
      before=False,
    )
    if after is not None:
      points.append(after)
  return points


@router.get("/{drive_id}/series", response_model=SeriesResponse)
def drive_series(
  request: Request,
  drive_id: str,
  signals: str,
  start_us: int = Query(default=0),
  end_us: int | None = Query(default=None),
  max_points: int = Query(default=5000, ge=2, le=100_000),
  max_markers: int = Query(default=1000, ge=1, le=10_000),
  telemetry_sha256: str | None = Query(
    default=None,
    pattern=r"^[0-9a-f]{64}$",
  ),
  timeline_version: str | None = Query(
    default=None,
    pattern=r"^[0-9a-f]{64}$",
  ),
) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  if not SQLITE_INT_MIN <= start_us <= SQLITE_INT_MAX or (end_us is not None and not SQLITE_INT_MIN <= end_us <= SQLITE_INT_MAX):
    raise ApiError(
      422,
      "invalid_time_range",
      "start_us and end_us must fit signed 64-bit microseconds",
    )
  signal_ids = _parse_signals(signals)
  database: Database = request.app.state.database
  with database.connection() as connection:
    connection.execute("BEGIN")
    try:
      drive = connection.execute(
        "SELECT id FROM drives WHERE id = ?",
        (drive_id,),
      ).fetchone()
      if drive is None:
        raise ApiError(404, "drive_not_found", "Drive was not found")
      index = connection.execute(
        """
        SELECT state, signal_catalog_json, ndjson_path, ndjson_sha256,
               manifest_json
        FROM telemetry_indexes
        WHERE drive_id = ?
        """,
        (drive_id,),
      ).fetchone()
      if index is None or index["state"] not in READY_INDEX_STATES:
        raise ApiError(
          409,
          "telemetry_not_ready",
          "Telemetry has not been indexed for this drive",
        )
      (
        current_sha256,
        current_timeline_version,
        timeline_origin,
      ) = _telemetry_generation(index)
      if (telemetry_sha256 is not None and telemetry_sha256 != current_sha256) or (
        timeline_version is not None and timeline_version != current_timeline_version
      ):
        raise ApiError(
          409,
          "telemetry_generation_changed",
          "The requested telemetry generation is no longer current",
        )
      with TelemetryRecordLoader(
        request.app.state.settings.archive_root,
        index["ndjson_path"],
      ) as records:
        return _read_drive_series(
          connection,
          records,
          index,
          drive_id,
          signal_ids,
          start_us,
          end_us,
          max_points,
          max_markers,
          current_sha256,
          current_timeline_version,
          timeline_origin,
        )
    finally:
      connection.rollback()


def _read_drive_series(
  connection: sqlite3.Connection,
  records: TelemetryRecordLoader,
  index: sqlite3.Row,
  drive_id: str,
  signal_ids: list[str],
  start_us: int,
  end_us: int | None,
  max_points: int,
  max_markers: int,
  ndjson_sha256: str,
  timeline_version: str,
  timeline_origin: Literal["stable", "provisional"],
) -> dict[str, Any]:
  catalog = _catalog_metadata(index["signal_catalog_json"])
  chunks = _chunk_metadata(connection, drive_id, signal_ids)
  unknown = [signal_id for signal_id in signal_ids if signal_id not in catalog]
  if unknown:
    raise ApiError(
      422,
      "unknown_signals",
      "One or more requested signals are not indexed",
      details={"signals": unknown},
    )
  for signal_id, tiers in chunks.items():
    if any(metadata != catalog[signal_id] for metadata in tiers.values()):
      raise _invalid_index()

  if end_us is None:
    bounds = connection.execute(
      """
      SELECT MAX(end_t_us) AS end_t_us
      FROM (
        SELECT end_t_us
        FROM telemetry_series_chunks
        WHERE drive_id = ?
        UNION ALL
        SELECT end_t_us
        FROM telemetry_markers
        WHERE drive_id = ?
      )
      """,
      (drive_id, drive_id),
    ).fetchone()
    resolved_end_us = bounds["end_t_us"] if bounds is not None and bounds["end_t_us"] is not None else start_us
  else:
    resolved_end_us = end_us
  if resolved_end_us < start_us:
    raise ApiError(
      422,
      "invalid_time_range",
      "end_us must be greater than or equal to start_us",
    )

  response_signals: list[dict[str, Any]] = []
  for signal_id in signal_ids:
    tier_metadata = chunks.get(signal_id, {})
    if not tier_metadata:
      metadata = catalog[signal_id]
      response_signals.append(
        {
          "signal": signal_id,
          "unit": metadata.unit,
          "kind": metadata.kind,
          "points": [],
        }
      )
      continue

    selected_tier: str | None = None
    selected_points: list[_Point] | None = None
    for tier in TIER_ORDER:
      if tier not in tier_metadata:
        continue
      candidate = _load_window(
        connection,
        records,
        drive_id,
        signal_id,
        tier,
        start_us,
        resolved_end_us,
        limit=max_points,
      )
      if candidate.overflow:
        continue
      candidate_points = _with_neighbors(
        connection,
        records,
        drive_id,
        signal_id,
        tier,
        tier_metadata[tier],
        candidate,
        start_us,
        resolved_end_us,
      )
      if len(candidate_points) <= max_points:
        selected_tier = tier
        selected_points = candidate_points
        break
    if selected_tier is None:
      selected_tier = next(tier for tier in reversed(TIER_ORDER) if tier in tier_metadata)
      selected_window = _load_window(
        connection,
        records,
        drive_id,
        signal_id,
        selected_tier,
        start_us,
        resolved_end_us,
        limit=None,
      )
      selected_points = _with_neighbors(
        connection,
        records,
        drive_id,
        signal_id,
        selected_tier,
        tier_metadata[selected_tier],
        selected_window,
        start_us,
        resolved_end_us,
      )
    metadata = tier_metadata[selected_tier]
    assert selected_points is not None
    points = _thin_points(selected_points, metadata.kind, max_points)
    response_signals.append(
      {
        "signal": signal_id,
        "unit": metadata.unit,
        "kind": metadata.kind,
        "points": [{"t_us": point.t_us, "value": point.value} for point in points],
      }
    )

  markers, markers_truncated = _read_markers(
    connection,
    drive_id,
    start_us,
    resolved_end_us,
    max_markers,
  )
  return {
    "drive_id": drive_id,
    "ndjson_sha256": ndjson_sha256,
    "timeline_version": timeline_version,
    "timeline_origin": timeline_origin,
    "start_t_us": start_us,
    "end_t_us": resolved_end_us,
    "signals": response_signals,
    "markers": markers,
    "markers_truncated": markers_truncated,
  }
