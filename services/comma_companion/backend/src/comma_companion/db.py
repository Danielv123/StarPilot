from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 9

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  enrolled_at TEXT NOT NULL,
  disabled_at TEXT
);

CREATE TABLE IF NOT EXISTS device_live_state (
  device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  last_seen_at TEXT NOT NULL,
  device_timestamp TEXT,
  state TEXT,
  offroad INTEGER,
  agent_version TEXT,
  software_version TEXT,
  network_type TEXT,
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  metrics_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS admin_sessions (
  id_hash TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  step_up_expires_at TEXT,
  revoked_at TEXT,
  ip_address TEXT,
  user_agent TEXT
);

CREATE TABLE IF NOT EXISTS objects (
  sha256 TEXT PRIMARY KEY,
  size INTEGER NOT NULL CHECK(size >= 0),
  storage_path TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drives (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id),
  route_name TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  duration_us INTEGER,
  artifact_generation INTEGER NOT NULL DEFAULT 0,
  telemetry_ready INTEGER NOT NULL DEFAULT 0,
  route_state TEXT NOT NULL DEFAULT 'open',
  updated_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(device_id, route_name)
);

CREATE TABLE IF NOT EXISTS segments (
  id TEXT PRIMARY KEY,
  drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  number INTEGER NOT NULL CHECK(number >= 0),
  started_at TEXT,
  start_t_us INTEGER,
  duration_us INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE(drive_id, number)
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id),
  drive_id TEXT REFERENCES drives(id) ON DELETE CASCADE,
  segment_id TEXT REFERENCES segments(id) ON DELETE CASCADE,
  object_sha256 TEXT NOT NULL REFERENCES objects(sha256),
  kind TEXT NOT NULL,
  camera TEXT,
  relative_path TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  size INTEGER NOT NULL CHECK(size >= 0),
  mime_type TEXT,
  codec TEXT,
  duration_us INTEGER,
  width INTEGER,
  height INTEGER,
  fps REAL,
  frame_count INTEGER,
  pixel_format TEXT,
  audio_codec TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  validation_json TEXT NOT NULL DEFAULT '{}',
  encoder_json TEXT NOT NULL DEFAULT '{}',
  time_map_path TEXT,
  status TEXT NOT NULL,
  source_artifact_id TEXT REFERENCES artifacts(id),
  created_at TEXT NOT NULL,
  UNIQUE(device_id, relative_path, object_sha256)
);

CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id),
  idempotency_key TEXT,
  file_id TEXT,
  relative_path TEXT NOT NULL,
  route_name TEXT,
  segment_number INTEGER,
  artifact_type TEXT NOT NULL,
  camera TEXT,
  mime_type TEXT,
  completion_evidence_json TEXT NOT NULL DEFAULT '[]',
  partial INTEGER NOT NULL DEFAULT 0,
  declared_size INTEGER NOT NULL CHECK(declared_size >= 0),
  declared_mtime_ns INTEGER,
  declared_mtime TEXT,
  declared_sha256 TEXT,
  offset INTEGER NOT NULL DEFAULT 0 CHECK(offset >= 0),
  status TEXT NOT NULL,
  part_path TEXT NOT NULL,
  object_sha256 TEXT REFERENCES objects(sha256),
  artifact_id TEXT REFERENCES artifacts(id),
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(device_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS route_inventories (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id),
  drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  route_name TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation >= 1),
  manifest_sha256 TEXT NOT NULL,
  previous_manifest_sha256 TEXT,
  rlog_source_fingerprint TEXT,
  manifest_size INTEGER NOT NULL CHECK(manifest_size >= 0),
  materialized_row_count INTEGER NOT NULL
    CHECK(materialized_row_count >= 0),
  state TEXT NOT NULL,
  route_closed INTEGER NOT NULL CHECK(route_closed IN (0, 1)),
  manifest_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(device_id, route_name, generation),
  UNIQUE(device_id, route_name, manifest_sha256)
);

CREATE TABLE IF NOT EXISTS route_inventory_usage (
  device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  route_count INTEGER NOT NULL CHECK(route_count >= 0),
  inventory_count INTEGER NOT NULL CHECK(inventory_count >= 0),
  manifest_bytes INTEGER NOT NULL CHECK(manifest_bytes >= 0),
  materialized_rows INTEGER NOT NULL CHECK(materialized_rows >= 0)
);

CREATE TABLE IF NOT EXISTS route_inventory_segments (
  inventory_id TEXT NOT NULL
    REFERENCES route_inventories(id) ON DELETE CASCADE,
  segment_number INTEGER NOT NULL CHECK(segment_number >= 0),
  missing INTEGER NOT NULL CHECK(missing IN (0, 1)),
  PRIMARY KEY(inventory_id, segment_number)
);

CREATE TABLE IF NOT EXISTS route_inventory_expected_files (
  inventory_id TEXT NOT NULL
    REFERENCES route_inventories(id) ON DELETE CASCADE,
  location_key TEXT NOT NULL,
  segment_number INTEGER,
  role TEXT NOT NULL,
  is_stream INTEGER NOT NULL CHECK(is_stream IN (0, 1)),
  status TEXT NOT NULL CHECK(status IN ('present', 'missing')),
  relative_path TEXT,
  artifact_type TEXT NOT NULL,
  camera TEXT,
  declared_size INTEGER,
  declared_mtime_ns INTEGER,
  declared_sha256 TEXT,
  PRIMARY KEY(inventory_id, location_key, role)
);

CREATE TABLE IF NOT EXISTS upload_chunks (
  upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
  chunk_offset INTEGER NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY(upload_id, chunk_offset)
);

CREATE TABLE IF NOT EXISTS commands (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id),
  idempotency_key TEXT NOT NULL,
  type TEXT NOT NULL,
  args_json TEXT NOT NULL,
  state TEXT NOT NULL,
  requires_offroad INTEGER NOT NULL DEFAULT 0,
  issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  delivered_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  message TEXT,
  error TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(device_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  state TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL,
  dedupe_key TEXT,
  progress REAL NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  lease_owner TEXT,
  lease_expires_at TEXT,
  available_at TEXT,
  cancel_requested_at TEXT,
  retryable INTEGER CHECK(retryable IN (0, 1)),
  error TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_registry (
  sha256 TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'approximate_closed_loop',
  parameter_schema_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS simulation_requests (
  id TEXT PRIMARY KEY,
  drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
  t_us INTEGER NOT NULL CHECK(t_us >= 0),
  horizon_us INTEGER NOT NULL CHECK(horizon_us > 0),
  model_hash TEXT NOT NULL REFERENCES model_registry(sha256),
  mode TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  baseline_parameters_json TEXT NOT NULL DEFAULT '{}',
  telemetry_sha256 TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_indexes (
  drive_id TEXT PRIMARY KEY REFERENCES drives(id) ON DELETE CASCADE,
  schema_version INTEGER NOT NULL,
  state TEXT NOT NULL,
  ndjson_path TEXT NOT NULL,
  ndjson_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  signal_catalog_json TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_series_chunks (
  drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  signal_id TEXT NOT NULL,
  tier TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  kind TEXT NOT NULL,
  unit TEXT,
  start_t_us INTEGER NOT NULL,
  end_t_us INTEGER NOT NULL,
  ndjson_path TEXT NOT NULL,
  byte_offset INTEGER NOT NULL CHECK(byte_offset >= 0),
  byte_length INTEGER NOT NULL CHECK(byte_length > 0),
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(drive_id, signal_id, tier, chunk_index)
);

CREATE TABLE IF NOT EXISTS telemetry_markers (
  drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  marker_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  start_t_us INTEGER NOT NULL,
  end_t_us INTEGER NOT NULL,
  severity TEXT,
  label TEXT,
  data_json TEXT NOT NULL,
  PRIMARY KEY(drive_id, marker_id)
);

CREATE TABLE IF NOT EXISTS telemetry_frame_chunks (
  drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  camera TEXT NOT NULL,
  segment_number INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL,
  ndjson_path TEXT NOT NULL,
  byte_offset INTEGER NOT NULL CHECK(byte_offset >= 0),
  byte_length INTEGER NOT NULL CHECK(byte_length > 0),
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(drive_id, camera, segment_number, chunk_index)
);

CREATE TABLE IF NOT EXISTS telemetry_dynamics_chunks (
  drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  start_t_us INTEGER NOT NULL,
  end_t_us INTEGER NOT NULL,
  ndjson_path TEXT NOT NULL,
  byte_offset INTEGER NOT NULL CHECK(byte_offset >= 0),
  byte_length INTEGER NOT NULL CHECK(byte_length > 0),
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(drive_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS runtime_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status_code INTEGER,
  response_json TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(actor_type, actor_id, key)
);

CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_type TEXT NOT NULL,
  actor_id TEXT,
  action TEXT NOT NULL,
  resource_type TEXT,
  resource_id TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  ip_address TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_device_state_seen
ON device_live_state(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_uploads_device_status
ON uploads(device_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_uploads_device_file
ON uploads(device_id, file_id, created_at DESC)
WHERE file_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_upload_chunks_received
ON upload_chunks(received_at);
CREATE INDEX IF NOT EXISTS idx_drives_device_started
ON drives(device_id, started_at DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_segments_drive_number
ON segments(drive_id, number);
CREATE INDEX IF NOT EXISTS idx_artifacts_drive_segment
ON artifacts(drive_id, segment_id, kind);
CREATE INDEX IF NOT EXISTS idx_commands_device_state
ON commands(device_id, state, issued_at);
CREATE INDEX IF NOT EXISTS idx_jobs_state_priority
ON jobs(state, available_at, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_worker_heartbeats_seen
ON worker_heartbeats(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_route_inventories_latest
ON route_inventories(device_id, route_name, generation DESC);
CREATE INDEX IF NOT EXISTS idx_route_inventories_drive_latest
ON route_inventories(drive_id, generation DESC);
CREATE INDEX IF NOT EXISTS idx_route_inventory_expected_path
ON route_inventory_expected_files(relative_path, declared_sha256);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_dedupe
ON jobs(type, dedupe_key)
WHERE dedupe_key IS NOT NULL AND state IN ('queued', 'leased', 'running');
CREATE INDEX IF NOT EXISTS idx_series_window
ON telemetry_series_chunks(drive_id, signal_id, tier, start_t_us, end_t_us);
CREATE INDEX IF NOT EXISTS idx_markers_window
ON telemetry_markers(drive_id, start_t_us, end_t_us);
CREATE INDEX IF NOT EXISTS idx_dynamics_window
ON telemetry_dynamics_chunks(drive_id, start_t_us, end_t_us);
CREATE INDEX IF NOT EXISTS idx_audit_created
ON audit_events(created_at DESC);
"""


def utc_now() -> datetime:
  return datetime.now(UTC)


def isoformat(value: datetime | None = None) -> str:
  return (value or utc_now()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
  return (
    connection.execute(
      "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
      (table,),
    ).fetchone()
    is not None
  )


def _ensure_column(
  connection: sqlite3.Connection,
  table: str,
  column: str,
  declaration: str,
) -> None:
  columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
  if column not in columns:
    connection.execute(
      f"ALTER TABLE {table} ADD COLUMN {column} {declaration}",
    )


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
  additions = {
    "drives": {
      "artifact_generation": "INTEGER NOT NULL DEFAULT 0",
      "telemetry_ready": "INTEGER NOT NULL DEFAULT 0",
      "route_state": "TEXT NOT NULL DEFAULT 'open'",
      "updated_at": "TEXT",
    },
    "segments": {
      "start_t_us": "INTEGER",
    },
    "artifacts": {
      "width": "INTEGER",
      "height": "INTEGER",
      "fps": "REAL",
      "frame_count": "INTEGER",
      "pixel_format": "TEXT",
      "audio_codec": "TEXT",
      "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
      "validation_json": "TEXT NOT NULL DEFAULT '{}'",
      "encoder_json": "TEXT NOT NULL DEFAULT '{}'",
      "time_map_path": "TEXT",
    },
    "uploads": {
      "completion_evidence_json": "TEXT NOT NULL DEFAULT '[]'",
      "partial": "INTEGER NOT NULL DEFAULT 0",
    },
    "commands": {
      "result_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "jobs": {
      "dedupe_key": "TEXT",
      "available_at": "TEXT",
      "result_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "simulation_requests": {
      "baseline_parameters_json": "TEXT NOT NULL DEFAULT '{}'",
      "telemetry_sha256": "TEXT",
    },
  }
  connection.execute("BEGIN IMMEDIATE")
  try:
    for table, columns in additions.items():
      if not _table_exists(connection, table):
        continue
      for column, declaration in columns.items():
        _ensure_column(connection, table, column, declaration)
    connection.commit()
  except BaseException:
    connection.rollback()
    raise


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
  connection.execute("BEGIN IMMEDIATE")
  try:
    # Version 2 duplicated large NDJSON records in SQLite. These tables are
    # derived indexes, so invalidate and rebuild them as bounded references to
    # the immutable archive generation rather than copying the payload again.
    connection.execute("DROP TABLE IF EXISTS telemetry_series_chunks_v3")
    connection.execute(
      """
      CREATE TABLE telemetry_series_chunks_v3 (
        drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
        signal_id TEXT NOT NULL,
        tier TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        kind TEXT NOT NULL,
        unit TEXT,
        start_t_us INTEGER NOT NULL,
        end_t_us INTEGER NOT NULL,
        ndjson_path TEXT NOT NULL,
        byte_offset INTEGER NOT NULL CHECK(byte_offset >= 0),
        byte_length INTEGER NOT NULL CHECK(byte_length > 0),
        record_sha256 TEXT NOT NULL,
        PRIMARY KEY(drive_id, signal_id, tier, chunk_index)
      )
      """,
    )
    connection.execute("DROP TABLE IF EXISTS telemetry_frame_chunks_v3")
    connection.execute(
      """
      CREATE TABLE telemetry_frame_chunks_v3 (
        drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
        camera TEXT NOT NULL,
        segment_number INTEGER NOT NULL,
        chunk_index INTEGER NOT NULL,
        ndjson_path TEXT NOT NULL,
        byte_offset INTEGER NOT NULL CHECK(byte_offset >= 0),
        byte_length INTEGER NOT NULL CHECK(byte_length > 0),
        record_sha256 TEXT NOT NULL,
        PRIMARY KEY(drive_id, camera, segment_number, chunk_index)
      )
      """,
    )
    connection.execute("DROP TABLE IF EXISTS telemetry_dynamics_chunks_v3")
    connection.execute(
      """
      CREATE TABLE telemetry_dynamics_chunks_v3 (
        drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL,
        start_t_us INTEGER NOT NULL,
        end_t_us INTEGER NOT NULL,
        ndjson_path TEXT NOT NULL,
        byte_offset INTEGER NOT NULL CHECK(byte_offset >= 0),
        byte_length INTEGER NOT NULL CHECK(byte_length > 0),
        record_sha256 TEXT NOT NULL,
        PRIMARY KEY(drive_id, chunk_index)
      )
      """,
    )
    connection.execute("DROP TABLE IF EXISTS telemetry_series_chunks")
    connection.execute(
      "ALTER TABLE telemetry_series_chunks_v3 RENAME TO telemetry_series_chunks",
    )
    connection.execute("DROP TABLE IF EXISTS telemetry_frame_chunks")
    connection.execute(
      "ALTER TABLE telemetry_frame_chunks_v3 RENAME TO telemetry_frame_chunks",
    )
    connection.execute("DROP TABLE IF EXISTS telemetry_dynamics_chunks")
    connection.execute(
      "ALTER TABLE telemetry_dynamics_chunks_v3 RENAME TO telemetry_dynamics_chunks",
    )
    if _table_exists(connection, "telemetry_indexes"):
      connection.execute(
        "UPDATE telemetry_indexes SET state = 'stale'",
      )
    if _table_exists(connection, "drives"):
      connection.execute("UPDATE drives SET telemetry_ready = 0")
    connection.commit()
  except BaseException:
    connection.rollback()
    raise


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
  connection.execute("BEGIN IMMEDIATE")
  try:
    if _table_exists(connection, "jobs"):
      _ensure_column(connection, "jobs", "retryable", "INTEGER")
      # Historical terminal failures did not record whether their error was
      # permanent. Fail closed rather than offering an unsafe manual retry.
      connection.execute(
        """
        UPDATE jobs
        SET retryable = 0
        WHERE state = 'failed' AND retryable IS NULL
        """,
      )
    connection.commit()
  except BaseException:
    connection.rollback()
    raise


def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
  connection.execute("BEGIN IMMEDIATE")
  try:
    connection.execute(
      """
      CREATE TABLE route_inventories (
        id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL REFERENCES devices(id),
        drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
        route_name TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK(generation >= 1),
        manifest_sha256 TEXT NOT NULL,
        previous_manifest_sha256 TEXT,
        state TEXT NOT NULL,
        route_closed INTEGER NOT NULL CHECK(route_closed IN (0, 1)),
        manifest_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(device_id, route_name, generation),
        UNIQUE(device_id, route_name, manifest_sha256)
      )
      """,
    )
    connection.execute(
      """
      CREATE TABLE route_inventory_segments (
        inventory_id TEXT NOT NULL
          REFERENCES route_inventories(id) ON DELETE CASCADE,
        segment_number INTEGER NOT NULL CHECK(segment_number >= 0),
        missing INTEGER NOT NULL CHECK(missing IN (0, 1)),
        PRIMARY KEY(inventory_id, segment_number)
      )
      """,
    )
    connection.execute(
      """
      CREATE TABLE route_inventory_expected_files (
        inventory_id TEXT NOT NULL
          REFERENCES route_inventories(id) ON DELETE CASCADE,
        location_key TEXT NOT NULL,
        segment_number INTEGER,
        role TEXT NOT NULL,
        is_stream INTEGER NOT NULL CHECK(is_stream IN (0, 1)),
        status TEXT NOT NULL CHECK(status IN ('present', 'missing')),
        relative_path TEXT,
        artifact_type TEXT NOT NULL,
        camera TEXT,
        declared_size INTEGER,
        declared_mtime_ns INTEGER,
        declared_sha256 TEXT,
        PRIMARY KEY(inventory_id, location_key, role)
      )
      """,
    )
    connection.execute(
      """
      CREATE INDEX idx_route_inventories_latest
      ON route_inventories(device_id, route_name, generation DESC)
      """,
    )
    connection.execute(
      """
      CREATE INDEX idx_route_inventory_expected_path
      ON route_inventory_expected_files(relative_path, declared_sha256)
      """,
    )
    connection.commit()
  except BaseException:
    connection.rollback()
    raise


def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
  connection.execute("BEGIN IMMEDIATE")
  try:
    connection.execute(
      """
      CREATE TABLE IF NOT EXISTS worker_heartbeats (
        worker_id TEXT PRIMARY KEY,
        last_seen_at TEXT NOT NULL
      )
      """,
    )
    connection.execute(
      """
      CREATE INDEX IF NOT EXISTS idx_worker_heartbeats_seen
      ON worker_heartbeats(last_seen_at DESC)
      """,
    )
    connection.commit()
  except BaseException:
    connection.rollback()
    raise


def _manifest_rlog_fingerprint(manifest_json: str) -> str | None:
  try:
    manifest = json.loads(manifest_json)
    expected = {item["role"]: item for item in manifest["expected_streams"]}
    entries = [
      {
        "segment_number": int(segment["number"]),
        "sha256": str(stream["sha256"]).lower(),
      }
      for segment in manifest["segments"]
      for stream in segment["streams"]
      if (stream["status"] == "present" and expected[stream["role"]]["artifact_type"] == "rlog")
    ]
  except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    return None
  entries.sort(key=lambda item: (item["segment_number"], item["sha256"]))
  if not entries:
    return None
  canonical = json.dumps(
    entries,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
  ).encode()
  return hashlib.sha256(canonical).hexdigest()


def _migrate_v6_to_v7(connection: sqlite3.Connection) -> None:
  connection.execute("BEGIN IMMEDIATE")
  try:
    if _table_exists(connection, "route_inventories"):
      _ensure_column(
        connection,
        "route_inventories",
        "rlog_source_fingerprint",
        "TEXT",
      )
      rows = connection.execute(
        """
        SELECT id, manifest_json
        FROM route_inventories
        WHERE rlog_source_fingerprint IS NULL
        """,
      ).fetchall()
      connection.executemany(
        """
        UPDATE route_inventories
        SET rlog_source_fingerprint = ?
        WHERE id = ?
        """,
        [(_manifest_rlog_fingerprint(row["manifest_json"]), row["id"]) for row in rows],
      )
      connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_route_inventories_drive_latest
        ON route_inventories(drive_id, generation DESC)
        """,
      )
    connection.commit()
  except BaseException:
    connection.rollback()
    raise


def _migrate_v7_to_v8(connection: sqlite3.Connection) -> None:
  connection.execute("BEGIN IMMEDIATE")
  try:
    if _table_exists(connection, "route_inventories"):
      _ensure_column(
        connection,
        "route_inventories",
        "manifest_size",
        "INTEGER NOT NULL DEFAULT 0",
      )
      _ensure_column(
        connection,
        "route_inventories",
        "materialized_row_count",
        "INTEGER NOT NULL DEFAULT 0",
      )
      connection.execute(
        """
        UPDATE route_inventories
        SET manifest_size = LENGTH(CAST(manifest_json AS BLOB))
        """,
      )
      if _table_exists(connection, "route_inventory_segments") and _table_exists(connection, "route_inventory_expected_files"):
        connection.execute(
          """
          UPDATE route_inventories
          SET materialized_row_count =
            (
              SELECT COUNT(*)
              FROM route_inventory_segments segment
              WHERE segment.inventory_id = route_inventories.id
            )
            + (
              SELECT COUNT(*)
              FROM route_inventory_expected_files expected
              WHERE expected.inventory_id = route_inventories.id
            )
          """,
        )
    connection.execute(
      """
      CREATE TABLE IF NOT EXISTS route_inventory_usage (
        device_id TEXT PRIMARY KEY
          REFERENCES devices(id) ON DELETE CASCADE,
        route_count INTEGER NOT NULL CHECK(route_count >= 0),
        inventory_count INTEGER NOT NULL CHECK(inventory_count >= 0),
        manifest_bytes INTEGER NOT NULL CHECK(manifest_bytes >= 0),
        materialized_rows INTEGER NOT NULL CHECK(materialized_rows >= 0)
      )
      """,
    )
    if _table_exists(connection, "route_inventories") and _table_exists(connection, "devices"):
      connection.execute(
        """
        INSERT INTO route_inventory_usage(
          device_id, route_count, inventory_count,
          manifest_bytes, materialized_rows
        )
        SELECT
          inventory.device_id,
          COUNT(DISTINCT inventory.route_name),
          COUNT(*),
          COALESCE(SUM(inventory.manifest_size), 0),
          COALESCE(SUM(inventory.materialized_row_count), 0)
        FROM route_inventories inventory
        GROUP BY inventory.device_id
        ON CONFLICT(device_id) DO UPDATE SET
          route_count = excluded.route_count,
          inventory_count = excluded.inventory_count,
          manifest_bytes = excluded.manifest_bytes,
          materialized_rows = excluded.materialized_rows
        """,
      )
      if _table_exists(connection, "segments") and _table_exists(connection, "drives"):
        connection.execute(
          """
          UPDATE route_inventory_usage
          SET materialized_rows = materialized_rows + (
            SELECT COUNT(DISTINCT placeholder.id)
            FROM segments placeholder
            JOIN drives inventory_drive
              ON inventory_drive.id = placeholder.drive_id
            WHERE inventory_drive.device_id =
                route_inventory_usage.device_id
              AND EXISTS (
                SELECT 1
                FROM route_inventories known_inventory
                WHERE known_inventory.drive_id = inventory_drive.id
              )
          )
          """,
        )
    connection.commit()
  except BaseException:
    connection.rollback()
    raise


def _migrate_v8_to_v9(connection: sqlite3.Connection) -> None:
  connection.execute("BEGIN IMMEDIATE")
  try:
    if _table_exists(connection, "uploads"):
      _ensure_column(
        connection,
        "uploads",
        "file_id",
        "TEXT",
      )
      connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_uploads_device_file
        ON uploads(device_id, file_id, created_at DESC)
        WHERE file_id IS NOT NULL
        """,
      )
    connection.commit()
  except BaseException:
    connection.rollback()
    raise


class Database:
  def __init__(self, path: Path):
    self.path = path
    self._schema_lock = threading.Lock()

  def connect(self) -> sqlite3.Connection:
    connection = sqlite3.connect(
      self.path,
      timeout=30,
      isolation_level=None,
      check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection

  def initialize(self) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    with self._schema_lock:
      connection = self.connect()
      try:
        connection.execute("PRAGMA journal_mode=WAL")
        row = None
        if _table_exists(connection, "schema_meta"):
          row = connection.execute(
            "SELECT version FROM schema_meta LIMIT 1",
          ).fetchone()
        existing_version = row["version"] if row is not None else None
        if existing_version == 1:
          _migrate_v1_to_v2(connection)
          existing_version = 2
        if existing_version == 2:
          _migrate_v2_to_v3(connection)
          existing_version = 3
        if existing_version == 3:
          _migrate_v3_to_v4(connection)
          existing_version = 4
        if existing_version == 4:
          _migrate_v4_to_v5(connection)
          existing_version = 5
        if existing_version == 5:
          _migrate_v5_to_v6(connection)
          existing_version = 6
        if existing_version == 6:
          _migrate_v6_to_v7(connection)
          existing_version = 7
        if existing_version == 7:
          _migrate_v7_to_v8(connection)
          existing_version = 8
        if existing_version == 8:
          _migrate_v8_to_v9(connection)
          existing_version = 9
        if existing_version is not None and existing_version != SCHEMA_VERSION:
          raise RuntimeError(
            f"database schema {existing_version} is not supported " + f"(expected {SCHEMA_VERSION})",
          )
        connection.executescript(SCHEMA)
        if row is None:
          connection.execute(
            "INSERT INTO schema_meta(version) VALUES (?)",
            (SCHEMA_VERSION,),
          )
        elif row["version"] != SCHEMA_VERSION:
          connection.execute(
            "UPDATE schema_meta SET version = ?",
            (SCHEMA_VERSION,),
          )
      finally:
        connection.close()

  @contextmanager
  def connection(self) -> Iterator[sqlite3.Connection]:
    connection = self.connect()
    try:
      yield connection
    finally:
      connection.close()

  @contextmanager
  def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection = self.connect()
    try:
      connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
      yield connection
      connection.commit()
    except BaseException:
      connection.rollback()
      raise
    finally:
      connection.close()

  def query_one(
    self,
    sql: str,
    parameters: Sequence[Any] = (),
  ) -> sqlite3.Row | None:
    with self.connection() as connection:
      return connection.execute(sql, parameters).fetchone()

  def query_all(
    self,
    sql: str,
    parameters: Sequence[Any] = (),
  ) -> list[sqlite3.Row]:
    with self.connection() as connection:
      return list(connection.execute(sql, parameters).fetchall())

  def execute(
    self,
    sql: str,
    parameters: Sequence[Any] = (),
  ) -> int:
    with self.transaction(immediate=True) as connection:
      cursor = connection.execute(sql, parameters)
      return cursor.rowcount

  def health(self) -> dict[str, Any]:
    with self.connection() as connection:
      result = connection.execute("SELECT 1").fetchone()
      mode = connection.execute("PRAGMA journal_mode").fetchone()
      version = connection.execute(
        "SELECT version FROM schema_meta LIMIT 1",
      ).fetchone()
    return {
      "ok": result is not None and result[0] == 1,
      "journal_mode": mode[0] if mode else None,
      "schema_version": version[0] if version else None,
    }

  def checkpoint(self) -> None:
    with self.connection() as connection:
      connection.execute("PRAGMA wal_checkpoint(PASSIVE)")


def ensure_storage_directories(
  archive_root: Path,
  session_dir: Path,
) -> None:
  for path in (
    archive_root / "objects" / "sha256",
    archive_root / "uploads",
    archive_root / "derived",
    archive_root / "telemetry",
    archive_root / "thumbnails",
    session_dir,
  ):
    path.mkdir(parents=True, exist_ok=True)
