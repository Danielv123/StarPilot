from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from comma_companion.db import Database, isoformat
from comma_companion.integrations import IntegrationHandlers


def test_telemetry_publication_invalidation_rolls_back_together(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  database = Database(tmp_path / "local" / "companion.sqlite3")
  database.initialize()
  archive_root = tmp_path / "archive"
  for directory in ("derived", "objects", "telemetry"):
    (archive_root / directory).mkdir(parents=True)
  now = isoformat()
  derived_sha256 = "1" * 64
  sync_sha256 = "2" * 64
  sync_path = "derived/device-one/route-one/0/road.sync.json"
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO devices(id, display_name, token_hash, enrolled_at)
      VALUES ('device-one', 'Test device', 'token-hash', ?)
      """,
      (now,),
    )
    connection.execute(
      """
      INSERT INTO drives(
        id, device_id, route_name, telemetry_ready, created_at
      ) VALUES ('drive-one', 'device-one', 'route-one', 1, ?)
      """,
      (now,),
    )
    connection.execute(
      """
      INSERT INTO segments(id, drive_id, number, created_at)
      VALUES ('segment-zero', 'drive-one', 0, ?)
      """,
      (now,),
    )
    connection.executemany(
      """
      INSERT INTO objects(sha256, size, storage_path, created_at)
      VALUES (?, 1, ?, ?)
      """,
      (
        (
          derived_sha256,
          "derived/device-one/route-one/0/road.av1.webm",
          now,
        ),
        (sync_sha256, sync_path, now),
      ),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256, kind,
        camera, relative_path, storage_path, size, time_map_path,
        status, created_at
      ) VALUES (
        'derived-one', 'device-one', 'drive-one', 'segment-zero', ?,
        'derived_video', 'road',
        'derived/device-one/route-one/0/road.av1.webm',
        'derived/device-one/route-one/0/road.av1.webm',
        1, ?, 'ready', ?
      )
      """,
      (derived_sha256, sync_path, now),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256, kind,
        camera, relative_path, storage_path, size, status,
        source_artifact_id, created_at
      ) VALUES (
        'sync-one', 'device-one', 'drive-one', 'segment-zero', ?,
        'video_telemetry_sync', 'road', ?, ?, 1, 'ready',
        'derived-one', ?
      )
      """,
      (sync_sha256, sync_path, sync_path, now),
    )

  handlers = IntegrationHandlers(database, archive_root)
  invalidate_media_sync = handlers._invalidate_drive_media_sync

  def fail_after_media_sync_invalidation(
    drive_id: str,
    **kwargs: Any,
  ) -> None:
    invalidate_media_sync(drive_id, **kwargs)
    raise RuntimeError("injected failure")

  monkeypatch.setattr(
    handlers,
    "_invalidate_drive_media_sync",
    fail_after_media_sync_invalidation,
  )

  with pytest.raises(RuntimeError, match="injected failure"):
    handlers._invalidate_drive_telemetry_publication("drive-one")

  drive = database.query_one(
    "SELECT telemetry_ready FROM drives WHERE id = 'drive-one'",
  )
  derived = database.query_one(
    "SELECT time_map_path FROM artifacts WHERE id = 'derived-one'",
  )
  sync = database.query_one(
    "SELECT status FROM artifacts WHERE id = 'sync-one'",
  )
  assert drive is not None and drive["telemetry_ready"] == 1
  assert derived is not None and derived["time_map_path"] == sync_path
  assert sync is not None and sync["status"] == "ready"
