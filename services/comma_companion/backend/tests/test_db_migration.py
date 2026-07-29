from __future__ import annotations

import sqlite3
from pathlib import Path

from comma_companion.db import Database, SCHEMA_VERSION


def test_v1_database_is_migrated_before_v2_indexes(tmp_path: Path) -> None:
  path = tmp_path / "companion.sqlite3"
  connection = sqlite3.connect(path)
  connection.executescript(
    """
    CREATE TABLE schema_meta(version INTEGER NOT NULL);
    INSERT INTO schema_meta(version) VALUES (1);
    CREATE TABLE jobs (
      id TEXT PRIMARY KEY,
      type TEXT NOT NULL,
      state TEXT NOT NULL,
      priority INTEGER NOT NULL DEFAULT 0,
      payload_json TEXT NOT NULL,
      progress REAL NOT NULL DEFAULT 0,
      attempts INTEGER NOT NULL DEFAULT 0,
      max_attempts INTEGER NOT NULL DEFAULT 3,
      lease_owner TEXT,
      lease_expires_at TEXT,
      cancel_requested_at TEXT,
      error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      completed_at TEXT
    );
    """,
  )
  connection.close()

  database = Database(path)
  database.initialize()

  with database.connection() as migrated:
    version = migrated.execute(
      "SELECT version FROM schema_meta",
    ).fetchone()[0]
    columns = {row["name"] for row in migrated.execute("PRAGMA table_info(jobs)").fetchall()}
    telemetry_table = migrated.execute(
      """
      SELECT 1
      FROM sqlite_master
      WHERE type = 'table' AND name = 'telemetry_indexes'
      """,
    ).fetchone()
  assert version == SCHEMA_VERSION
  assert {"dedupe_key", "available_at", "result_json"} <= columns
  assert telemetry_table is not None


def test_v2_inline_telemetry_is_invalidated_without_losing_archive(
  tmp_path: Path,
) -> None:
  path = tmp_path / "companion.sqlite3"
  connection = sqlite3.connect(path)
  connection.executescript(
    """
    PRAGMA foreign_keys = ON;
    CREATE TABLE schema_meta(version INTEGER NOT NULL);
    INSERT INTO schema_meta(version) VALUES (2);

    CREATE TABLE devices (
      id TEXT PRIMARY KEY,
      display_name TEXT NOT NULL,
      token_hash TEXT NOT NULL UNIQUE,
      enrolled_at TEXT NOT NULL,
      disabled_at TEXT
    );
    CREATE TABLE objects (
      sha256 TEXT PRIMARY KEY,
      size INTEGER NOT NULL,
      storage_path TEXT NOT NULL UNIQUE,
      created_at TEXT NOT NULL
    );
    CREATE TABLE drives (
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
    CREATE TABLE segments (
      id TEXT PRIMARY KEY,
      drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
      number INTEGER NOT NULL,
      started_at TEXT,
      start_t_us INTEGER,
      duration_us INTEGER,
      created_at TEXT NOT NULL,
      UNIQUE(drive_id, number)
    );
    CREATE TABLE artifacts (
      id TEXT PRIMARY KEY,
      device_id TEXT NOT NULL REFERENCES devices(id),
      drive_id TEXT REFERENCES drives(id) ON DELETE CASCADE,
      segment_id TEXT REFERENCES segments(id) ON DELETE CASCADE,
      object_sha256 TEXT NOT NULL REFERENCES objects(sha256),
      kind TEXT NOT NULL,
      camera TEXT,
      relative_path TEXT NOT NULL,
      storage_path TEXT NOT NULL,
      size INTEGER NOT NULL,
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
    CREATE TABLE telemetry_indexes (
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
    CREATE TABLE telemetry_series_chunks (
      drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
      signal_id TEXT NOT NULL,
      tier TEXT NOT NULL,
      chunk_index INTEGER NOT NULL,
      kind TEXT NOT NULL,
      unit TEXT,
      start_t_us INTEGER NOT NULL,
      end_t_us INTEGER NOT NULL,
      data_json TEXT NOT NULL,
      PRIMARY KEY(drive_id, signal_id, tier, chunk_index)
    );
    CREATE TABLE telemetry_frame_chunks (
      drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
      camera TEXT NOT NULL,
      segment_number INTEGER NOT NULL,
      chunk_index INTEGER NOT NULL,
      data_json TEXT NOT NULL,
      PRIMARY KEY(drive_id, camera, segment_number, chunk_index)
    );
    CREATE TABLE telemetry_dynamics_chunks (
      drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
      chunk_index INTEGER NOT NULL,
      start_t_us INTEGER NOT NULL,
      end_t_us INTEGER NOT NULL,
      data_json TEXT NOT NULL,
      PRIMARY KEY(drive_id, chunk_index)
    );

    INSERT INTO devices(
      id, display_name, token_hash, enrolled_at
    ) VALUES (
      'device-one', 'comma', 'token-hash', '2026-07-29T10:00:00Z'
    );
    INSERT INTO objects(
      sha256, size, storage_path, created_at
    ) VALUES (
      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      4,
      'objects/sha256/aa/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      '2026-07-29T10:00:00Z'
    );
    INSERT INTO drives(
      id, device_id, route_name, telemetry_ready, route_state, created_at
    ) VALUES (
      'drive-one',
      'device-one',
      '00000001--abc123def0',
      1,
      'complete',
      '2026-07-29T10:00:00Z'
    );
    INSERT INTO segments(
      id, drive_id, number, start_t_us, duration_us, created_at
    ) VALUES (
      'segment-zero',
      'drive-one',
      0,
      0,
      10000,
      '2026-07-29T10:00:00Z'
    );
    INSERT INTO artifacts(
      id, device_id, drive_id, segment_id, object_sha256,
      kind, relative_path, storage_path, size, status, created_at
    ) VALUES (
      'raw-rlog',
      'device-one',
      'drive-one',
      'segment-zero',
      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      'rlog',
      'realdata/00000001--abc123def0--0/rlog',
      'objects/sha256/aa/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      4,
      'ready',
      '2026-07-29T10:00:00Z'
    );
    INSERT INTO telemetry_indexes(
      drive_id, schema_version, state, ndjson_path, ndjson_sha256,
      manifest_json, signal_catalog_json, source_fingerprint,
      created_at, updated_at
    ) VALUES (
      'drive-one',
      1,
      'complete',
      'telemetry/device-one/00000001--abc123def0/v1/telemetry.ndjson',
      'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      '{"publication_ready":true}',
      '[]',
      'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
      '2026-07-29T10:00:00Z',
      '2026-07-29T10:00:00Z'
    );
    INSERT INTO telemetry_series_chunks VALUES (
      'drive-one',
      'vehicle.speed',
      'full',
      0,
      'numeric',
      'm/s',
      0,
      10000,
      '{"record":"series_chunk","v":[1.0,2.0]}'
    );
    INSERT INTO telemetry_frame_chunks VALUES (
      'drive-one',
      'road',
      0,
      0,
      '{"record":"frame_chunk","rows":[]}'
    );
    INSERT INTO telemetry_dynamics_chunks VALUES (
      'drive-one',
      0,
      0,
      10000,
      '{"record":"dynamics_chunk","rows":[]}'
    );
    """,
  )
  connection.close()

  database = Database(path)
  database.initialize()

  reference_columns = {
    "ndjson_path",
    "byte_offset",
    "byte_length",
    "record_sha256",
  }
  with database.connection() as migrated:
    version = migrated.execute(
      "SELECT version FROM schema_meta",
    ).fetchone()[0]
    chunk_columns = {}
    for table in (
      "telemetry_series_chunks",
      "telemetry_frame_chunks",
      "telemetry_dynamics_chunks",
    ):
      chunk_columns[table] = {
        row["name"]
        for row in migrated.execute(
          f"PRAGMA table_info({table})",
        ).fetchall()
      }
      assert (
        migrated.execute(
          f"SELECT COUNT(*) FROM {table}",
        ).fetchone()[0]
        == 0
      )
    telemetry = migrated.execute(
      """
      SELECT state, ndjson_path, ndjson_sha256
      FROM telemetry_indexes
      WHERE drive_id = 'drive-one'
      """,
    ).fetchone()
    drive = migrated.execute(
      """
      SELECT telemetry_ready, route_state
      FROM drives
      WHERE id = 'drive-one'
      """,
    ).fetchone()
    artifact = migrated.execute(
      """
      SELECT kind, object_sha256, storage_path, size, status
      FROM artifacts
      WHERE id = 'raw-rlog'
      """,
    ).fetchone()
    archived_object = migrated.execute(
      """
      SELECT size, storage_path
      FROM objects
      WHERE sha256 =
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
      """,
    ).fetchone()
    foreign_key_errors = migrated.execute(
      "PRAGMA foreign_key_check",
    ).fetchall()

  assert version == SCHEMA_VERSION
  for columns in chunk_columns.values():
    assert reference_columns <= columns
    assert "data_json" not in columns
  assert telemetry["state"] == "stale"
  assert telemetry["ndjson_path"].endswith("/telemetry.ndjson")
  assert telemetry["ndjson_sha256"] == "b" * 64
  assert drive["telemetry_ready"] == 0
  assert drive["route_state"] == "complete"
  expected_storage_path = "objects/sha256/aa/aa/" + "a" * 64
  assert dict(artifact) == {
    "kind": "rlog",
    "object_sha256": "a" * 64,
    "storage_path": expected_storage_path,
    "size": 4,
    "status": "ready",
  }
  assert dict(archived_object) == {
    "size": 4,
    "storage_path": expected_storage_path,
  }
  assert foreign_key_errors == []


def test_v3_failed_jobs_migrate_with_retry_fail_closed(
  tmp_path: Path,
) -> None:
  path = tmp_path / "companion.sqlite3"
  connection = sqlite3.connect(path)
  connection.executescript(
    """
    CREATE TABLE schema_meta(version INTEGER NOT NULL);
    INSERT INTO schema_meta(version) VALUES (3);
    CREATE TABLE jobs (
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
      error TEXT,
      result_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      completed_at TEXT
    );
    INSERT INTO jobs(
      id, type, state, payload_json, error,
      created_at, updated_at, completed_at
    ) VALUES (
      'old-failure', 'verify_artifact', 'failed', '{}', 'unknown failure',
      '2026-07-29T10:00:00Z', '2026-07-29T10:00:00Z',
      '2026-07-29T10:00:00Z'
    );
    INSERT INTO jobs(
      id, type, state, payload_json, created_at, updated_at
    ) VALUES (
      'old-queued', 'verify_artifact', 'queued', '{}',
      '2026-07-29T10:00:00Z', '2026-07-29T10:00:00Z'
    );
    """,
  )
  connection.close()

  database = Database(path)
  database.initialize()

  with database.connection() as migrated:
    version = migrated.execute(
      "SELECT version FROM schema_meta",
    ).fetchone()[0]
    rows = migrated.execute(
      "SELECT id, retryable FROM jobs ORDER BY id",
    ).fetchall()

  assert version == SCHEMA_VERSION
  assert [tuple(row) for row in rows] == [
    ("old-failure", 0),
    ("old-queued", None),
  ]


def test_v4_migrates_immutable_route_inventory_tables(
  tmp_path: Path,
) -> None:
  path = tmp_path / "companion.sqlite3"
  database = Database(path)
  database.initialize()
  with database.transaction(immediate=True) as connection:
    connection.execute("DROP TABLE route_inventory_expected_files")
    connection.execute("DROP TABLE route_inventory_segments")
    connection.execute("DROP TABLE route_inventories")
    connection.execute("UPDATE schema_meta SET version = 4")

  database.initialize()

  with database.connection() as migrated:
    version = migrated.execute(
      "SELECT version FROM schema_meta",
    ).fetchone()[0]
    inventory_columns = {
      row["name"]
      for row in migrated.execute(
        "PRAGMA table_info(route_inventories)",
      )
    }
    file_columns = {
      row["name"]
      for row in migrated.execute(
        "PRAGMA table_info(route_inventory_expected_files)",
      )
    }
    foreign_key_errors = migrated.execute(
      "PRAGMA foreign_key_check",
    ).fetchall()

  assert version == SCHEMA_VERSION
  assert {
    "device_id",
    "drive_id",
    "generation",
    "manifest_sha256",
    "previous_manifest_sha256",
    "rlog_source_fingerprint",
    "manifest_size",
    "materialized_row_count",
    "manifest_json",
  } <= inventory_columns
  assert {
    "location_key",
    "segment_number",
    "role",
    "is_stream",
    "status",
    "relative_path",
    "declared_sha256",
  } <= file_columns
  assert foreign_key_errors == []


def test_v7_inventory_usage_is_backfilled_exactly(
  tmp_path: Path,
) -> None:
  path = tmp_path / "companion.sqlite3"
  database = Database(path)
  database.initialize()
  manifest_json = '{"expected_streams":[],"segments":[]}'
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO devices(
        id, display_name, token_hash, enrolled_at
      ) VALUES ('device-one', 'comma', 'token-hash', '2026-07-29T10:00:00Z')
      """,
    )
    connection.execute(
      """
      INSERT INTO drives(
        id, device_id, route_name, created_at
      ) VALUES (
        'drive-one', 'device-one', 'route-one', '2026-07-29T10:00:00Z'
      )
      """,
    )
    connection.execute(
      """
      INSERT INTO segments(
        id, drive_id, number, created_at
      ) VALUES (
        'segment-zero', 'drive-one', 0, '2026-07-29T10:00:00Z'
      )
      """,
    )
    connection.execute(
      """
      INSERT INTO route_inventories(
        id, device_id, drive_id, route_name, generation,
        manifest_sha256, manifest_size, materialized_row_count,
        state, route_closed, manifest_json, created_at
      ) VALUES (
        'inventory-one', 'device-one', 'drive-one', 'route-one', 1,
        ?, 0, 0, 'partial', 1, ?, '2026-07-29T10:00:00Z'
      )
      """,
      ("a" * 64, manifest_json),
    )
    connection.execute(
      """
      INSERT INTO route_inventory_segments(
        inventory_id, segment_number, missing
      ) VALUES ('inventory-one', 0, 1)
      """,
    )
    connection.execute(
      """
      INSERT INTO route_inventory_expected_files(
        inventory_id, location_key, segment_number, role,
        is_stream, status, artifact_type
      ) VALUES (
        'inventory-one', 'segment:0', 0, 'realdata|rlog|-',
        1, 'missing', 'rlog'
      )
      """,
    )
    connection.execute("DROP TABLE route_inventory_usage")
    connection.execute("UPDATE schema_meta SET version = 7")

  database.initialize()

  inventory = database.query_one(
    """
    SELECT manifest_size, materialized_row_count
    FROM route_inventories
    WHERE id = 'inventory-one'
    """,
  )
  usage = database.query_one(
    """
    SELECT route_count, inventory_count, manifest_bytes, materialized_rows
    FROM route_inventory_usage
    WHERE device_id = 'device-one'
    """,
  )
  assert inventory is not None
  assert usage is not None
  assert dict(inventory) == {
    "manifest_size": len(manifest_json.encode()),
    "materialized_row_count": 2,
  }
  assert dict(usage) == {
    "route_count": 1,
    "inventory_count": 1,
    "manifest_bytes": len(manifest_json.encode()),
    "materialized_rows": 3,
  }


def test_v8_uploads_gain_nullable_agent_file_id(
  tmp_path: Path,
) -> None:
  path = tmp_path / "companion.sqlite3"
  connection = sqlite3.connect(path)
  connection.executescript(
    """
    CREATE TABLE schema_meta(version INTEGER NOT NULL);
    INSERT INTO schema_meta(version) VALUES (8);
    CREATE TABLE devices (
      id TEXT PRIMARY KEY,
      display_name TEXT NOT NULL,
      token_hash TEXT NOT NULL UNIQUE,
      enrolled_at TEXT NOT NULL,
      disabled_at TEXT
    );
    CREATE TABLE uploads (
      id TEXT PRIMARY KEY,
      device_id TEXT NOT NULL REFERENCES devices(id),
      idempotency_key TEXT,
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
      object_sha256 TEXT,
      artifact_id TEXT,
      error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      completed_at TEXT,
      UNIQUE(device_id, idempotency_key)
    );
    INSERT INTO devices(
      id, display_name, token_hash, enrolled_at
    ) VALUES (
      'device-one', 'comma', 'token-hash', '2026-07-29T10:00:00Z'
    );
    INSERT INTO uploads(
      id, device_id, idempotency_key, relative_path,
      artifact_type, declared_size, status, part_path,
      created_at, updated_at
    ) VALUES (
      'legacy-upload', 'device-one', 'legacy-key', 'route/0/rlog',
      'rlog', 4, 'receiving', 'uploads/legacy-upload.part',
      '2026-07-29T10:00:00Z', '2026-07-29T10:00:00Z'
    );
    """,
  )
  connection.close()

  database = Database(path)
  database.initialize()

  with database.connection() as migrated:
    version = migrated.execute(
      "SELECT version FROM schema_meta",
    ).fetchone()[0]
    columns = {
      row["name"]
      for row in migrated.execute(
        "PRAGMA table_info(uploads)",
      )
    }
    legacy = migrated.execute(
      "SELECT file_id FROM uploads WHERE id = 'legacy-upload'",
    ).fetchone()
    index = migrated.execute(
      """
      SELECT sql
      FROM sqlite_master
      WHERE type = 'index' AND name = 'idx_uploads_device_file'
      """,
    ).fetchone()

  assert version == SCHEMA_VERSION
  assert "file_id" in columns
  assert legacy["file_id"] is None
  assert index is not None
  assert "WHERE file_id IS NOT NULL" in index["sql"]
