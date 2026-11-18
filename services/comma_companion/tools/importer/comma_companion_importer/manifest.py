from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from comma_companion_importer.scanner import Artifact


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  source_path TEXT PRIMARY KEY,
  server_scope TEXT NOT NULL,
  device_id TEXT NOT NULL,
  route_name TEXT,
  segment_number INTEGER,
  artifact_type TEXT NOT NULL,
  camera TEXT,
  relative_path TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  source_identity TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  sha256 TEXT,
  upload_id TEXT,
  offset INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS files_upload_id_idx ON files(upload_id);
CREATE INDEX IF NOT EXISTS files_status_idx ON files(status);
CREATE TABLE IF NOT EXISTS route_inventories (
  server_scope TEXT NOT NULL,
  device_id TEXT NOT NULL,
  route_name TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  generation INTEGER NOT NULL,
  previous_manifest_sha256 TEXT,
  manifest_json TEXT NOT NULL,
  accepted_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(server_scope, device_id, route_name, manifest_sha256)
);
CREATE INDEX IF NOT EXISTS route_inventories_content_idx
ON route_inventories(
  server_scope, device_id, route_name, content_sha256,
  generation, previous_manifest_sha256
);
CREATE INDEX IF NOT EXISTS route_inventories_accepted_idx
ON route_inventories(
  server_scope, device_id, route_name, accepted_at, manifest_sha256
);
"""


@dataclass(frozen=True, slots=True)
class ManifestEntry:
  source_path: str
  sha256: str | None
  upload_id: str | None
  offset: int
  status: str
  attempts: int
  last_error: str | None


@dataclass(frozen=True, slots=True)
class InventoryEntry:
  server_scope: str
  device_id: str
  route_name: str
  content_sha256: str
  manifest_sha256: str
  generation: int
  previous_manifest_sha256: str | None
  manifest_json: str
  accepted_at: str | None
  attempts: int
  last_error: str | None


class Manifest:
  def __init__(self, path: Path):
    self.path = path.expanduser().resolve()
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self._connection = sqlite3.connect(self.path, check_same_thread=False)
    self._connection.row_factory = sqlite3.Row
    self._lock = threading.Lock()
    with self._lock:
      self._connection.execute("PRAGMA journal_mode=WAL")
      self._connection.execute("PRAGMA synchronous=FULL")
      self._connection.executescript(SCHEMA)
      columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(files)")}
      if "server_scope" not in columns:
        self._connection.execute("ALTER TABLE files ADD COLUMN server_scope TEXT NOT NULL DEFAULT ''")
      if "recorded_at" not in columns:
        self._connection.execute("ALTER TABLE files ADD COLUMN recorded_at TEXT NOT NULL DEFAULT ''")
      if "source_identity" not in columns:
        self._connection.execute("ALTER TABLE files ADD COLUMN source_identity TEXT NOT NULL DEFAULT ''")
      self._connection.commit()

  def close(self) -> None:
    with self._lock:
      self._connection.close()

  def __enter__(self) -> Manifest:
    return self

  def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
    self.close()

  @staticmethod
  def _now() -> str:
    return datetime.now(tz=UTC).isoformat()

  def upsert(self, artifact: Artifact, server_scope: str) -> ManifestEntry:
    source_path = str(artifact.source_path)
    with self._lock:
      existing = self._connection.execute(
        """
        SELECT
          server_scope, device_id, route_name, segment_number, artifact_type,
          camera, relative_path, size, mtime_ns, source_identity, recorded_at
        FROM files
        WHERE source_path = ?
        """,
        (source_path,),
      ).fetchone()
      declaration = (
        server_scope,
        artifact.device_id,
        artifact.route_name,
        artifact.segment_number,
        artifact.artifact_type,
        artifact.camera,
        artifact.relative_path,
        artifact.size,
        artifact.mtime_ns,
        artifact.source_identity,
        artifact.recorded_at.isoformat(),
      )
      changed = existing is not None and tuple(existing) != declaration
      content_changed = existing is not None and (
        existing["size"] != artifact.size or existing["mtime_ns"] != artifact.mtime_ns or existing["source_identity"] != artifact.source_identity
      )
      self._connection.execute(
        """
        INSERT INTO files(
          source_path, server_scope, device_id, route_name, segment_number, artifact_type,
          camera, relative_path, size, mtime_ns, source_identity, recorded_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
          server_scope = excluded.server_scope,
          device_id = excluded.device_id,
          route_name = excluded.route_name,
          segment_number = excluded.segment_number,
          artifact_type = excluded.artifact_type,
          camera = excluded.camera,
          relative_path = excluded.relative_path,
          size = excluded.size,
          mtime_ns = excluded.mtime_ns,
          source_identity = excluded.source_identity,
          recorded_at = excluded.recorded_at,
          updated_at = excluded.updated_at
        """,
        (
          source_path,
          server_scope,
          artifact.device_id,
          artifact.route_name,
          artifact.segment_number,
          artifact.artifact_type,
          artifact.camera,
          artifact.relative_path,
          artifact.size,
          artifact.mtime_ns,
          artifact.source_identity,
          artifact.recorded_at.isoformat(),
          self._now(),
        ),
      )
      if changed:
        self._connection.execute(
          """
          UPDATE files
          SET sha256 = CASE WHEN ? THEN NULL ELSE sha256 END,
              upload_id = NULL, offset = 0, status = 'pending',
              attempts = 0, last_error = NULL, updated_at = ?
          WHERE source_path = ?
          """,
          (content_changed, self._now(), source_path),
        )
      self._connection.commit()
      return self._get_locked(source_path)

  def reset_upload(self, source_path: Path | str) -> None:
    with self._lock:
      self._connection.execute(
        """
        UPDATE files
        SET upload_id = NULL, offset = 0, status = 'pending',
            last_error = NULL, updated_at = ?
        WHERE source_path = ?
        """,
        (self._now(), str(source_path)),
      )
      self._connection.commit()

  def get(self, source_path: Path | str) -> ManifestEntry:
    with self._lock:
      return self._get_locked(str(source_path))

  def _get_locked(self, source_path: str) -> ManifestEntry:
    row = self._connection.execute(
      "SELECT source_path, sha256, upload_id, offset, status, attempts, last_error FROM files WHERE source_path = ?",
      (source_path,),
    ).fetchone()
    if row is None:
      raise KeyError(source_path)
    return ManifestEntry(**dict(row))

  def set_hash(self, source_path: Path | str, digest: str) -> None:
    with self._lock:
      self._connection.execute(
        "UPDATE files SET sha256 = ?, updated_at = ? WHERE source_path = ?",
        (digest, self._now(), str(source_path)),
      )
      self._connection.commit()

  def set_upload(self, source_path: Path | str, upload_id: str, offset: int, status: str = "uploading") -> None:
    with self._lock:
      self._connection.execute(
        """
        UPDATE files
        SET upload_id = ?, offset = ?, status = ?, attempts = attempts + 1,
            last_error = NULL, updated_at = ?
        WHERE source_path = ?
        """,
        (upload_id, offset, status, self._now(), str(source_path)),
      )
      self._connection.commit()

  def set_offset(self, source_path: Path | str, offset: int) -> None:
    with self._lock:
      self._connection.execute(
        "UPDATE files SET offset = ?, status = 'uploading', updated_at = ? WHERE source_path = ?",
        (offset, self._now(), str(source_path)),
      )
      self._connection.commit()

  def complete(self, source_path: Path | str, offset: int) -> None:
    with self._lock:
      self._connection.execute(
        """
        UPDATE files
        SET offset = ?, status = 'complete', last_error = NULL, updated_at = ?
        WHERE source_path = ?
        """,
        (offset, self._now(), str(source_path)),
      )
      self._connection.commit()

  def fail(self, source_path: Path | str, error: str) -> None:
    with self._lock:
      self._connection.execute(
        "UPDATE files SET status = 'error', last_error = ?, updated_at = ? WHERE source_path = ?",
        (error[:2000], self._now(), str(source_path)),
      )
      self._connection.commit()

  @staticmethod
  def _inventory_entry(row: sqlite3.Row | None) -> InventoryEntry | None:
    if row is None:
      return None
    return InventoryEntry(**dict(row))

  def reusable_inventory(
    self,
    *,
    server_scope: str,
    device_id: str,
    route_name: str,
    content_sha256: str,
    generation: int,
    previous_manifest_sha256: str | None,
  ) -> InventoryEntry | None:
    with self._lock:
      row = self._connection.execute(
        """
        SELECT
          server_scope, device_id, route_name, content_sha256,
          manifest_sha256, generation, previous_manifest_sha256,
          manifest_json, accepted_at, attempts, last_error
        FROM route_inventories
        WHERE server_scope = ?
          AND device_id = ?
          AND route_name = ?
          AND content_sha256 = ?
          AND generation = ?
          AND previous_manifest_sha256 IS ?
        ORDER BY
          CASE WHEN accepted_at IS NOT NULL THEN 1 ELSE 0 END DESC,
          updated_at DESC,
          manifest_sha256 DESC
        LIMIT 1
        """,
        (
          server_scope,
          device_id,
          route_name,
          content_sha256,
          generation,
          previous_manifest_sha256,
        ),
      ).fetchone()
      return self._inventory_entry(row)

  def accepted_inventory(
    self,
    *,
    server_scope: str,
    device_id: str,
    route_name: str,
    manifest_sha256: str,
  ) -> InventoryEntry | None:
    with self._lock:
      row = self._connection.execute(
        """
        SELECT
          server_scope, device_id, route_name, content_sha256,
          manifest_sha256, generation, previous_manifest_sha256,
          manifest_json, accepted_at, attempts, last_error
        FROM route_inventories
        WHERE server_scope = ?
          AND device_id = ?
          AND route_name = ?
          AND manifest_sha256 = ?
          AND accepted_at IS NOT NULL
        LIMIT 1
        """,
        (
          server_scope,
          device_id,
          route_name,
          manifest_sha256,
        ),
      ).fetchone()
      return self._inventory_entry(row)

  def save_inventory(
    self,
    *,
    server_scope: str,
    device_id: str,
    route_name: str,
    content_sha256: str,
    manifest_sha256: str,
    generation: int,
    previous_manifest_sha256: str | None,
    manifest_json: str,
    accepted: bool = False,
  ) -> InventoryEntry:
    now = self._now()
    with self._lock:
      self._connection.execute(
        """
        INSERT INTO route_inventories(
          server_scope, device_id, route_name, content_sha256,
          manifest_sha256, generation, previous_manifest_sha256,
          manifest_json, accepted_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(
          server_scope, device_id, route_name, manifest_sha256
        ) DO UPDATE SET
          content_sha256 = excluded.content_sha256,
          generation = excluded.generation,
          previous_manifest_sha256 =
            excluded.previous_manifest_sha256,
          manifest_json = excluded.manifest_json,
          accepted_at = CASE
            WHEN route_inventories.accepted_at IS NOT NULL
              THEN route_inventories.accepted_at
            ELSE excluded.accepted_at
          END,
          last_error = NULL,
          updated_at = excluded.updated_at
        """,
        (
          server_scope,
          device_id,
          route_name,
          content_sha256,
          manifest_sha256,
          generation,
          previous_manifest_sha256,
          manifest_json,
          now if accepted else None,
          now,
          now,
        ),
      )
      self._connection.commit()
      row = self._connection.execute(
        """
        SELECT
          server_scope, device_id, route_name, content_sha256,
          manifest_sha256, generation, previous_manifest_sha256,
          manifest_json, accepted_at, attempts, last_error
        FROM route_inventories
        WHERE server_scope = ?
          AND device_id = ?
          AND route_name = ?
          AND manifest_sha256 = ?
        """,
        (
          server_scope,
          device_id,
          route_name,
          manifest_sha256,
        ),
      ).fetchone()
      entry = self._inventory_entry(row)
      assert entry is not None
      return entry

  def accept_inventory(
    self,
    *,
    server_scope: str,
    device_id: str,
    route_name: str,
    manifest_sha256: str,
  ) -> None:
    with self._lock:
      cursor = self._connection.execute(
        """
        UPDATE route_inventories
        SET accepted_at = COALESCE(accepted_at, ?),
            attempts = 0, last_error = NULL, updated_at = ?
        WHERE server_scope = ?
          AND device_id = ?
          AND route_name = ?
          AND manifest_sha256 = ?
        """,
        (
          self._now(),
          self._now(),
          server_scope,
          device_id,
          route_name,
          manifest_sha256,
        ),
      )
      if cursor.rowcount != 1:
        raise KeyError(manifest_sha256)
      self._connection.commit()

  def fail_inventory(
    self,
    *,
    server_scope: str,
    device_id: str,
    route_name: str,
    manifest_sha256: str,
    error: str,
  ) -> None:
    with self._lock:
      self._connection.execute(
        """
        UPDATE route_inventories
        SET attempts = attempts + 1, last_error = ?, updated_at = ?
        WHERE server_scope = ?
          AND device_id = ?
          AND route_name = ?
          AND manifest_sha256 = ?
        """,
        (
          error[:2000],
          self._now(),
          server_scope,
          device_id,
          route_name,
          manifest_sha256,
        ),
      )
      self._connection.commit()
