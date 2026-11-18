from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from comma_companion.db import isoformat


def test_media_manifest_uses_authoritative_segment_time_and_range(
  admin_client: TestClient,
  settings,
) -> None:
  database = admin_client.app.state.database
  drive_id = uuid4().hex
  segment_id = uuid4().hex
  source_id = uuid4().hex
  derived_id = uuid4().hex
  content = b"\x1aE\xdf\xa3fake-av1-webm"
  digest = hashlib.sha256(content).hexdigest()
  storage_path = (
    Path("derived")
    / "device-one"
    / "route-one"
    / "0"
    / "road.av1.webm"
  )
  absolute_path = settings.archive_root / storage_path
  absolute_path.parent.mkdir(parents=True, exist_ok=True)
  absolute_path.write_bytes(content)
  now = isoformat()
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO drives(id, device_id, route_name, created_at)
      VALUES (?, 'device-one', 'route-one', ?)
      """,
      (drive_id, now),
    )
    connection.execute(
      """
      INSERT INTO segments(
        id, drive_id, number, start_t_us, duration_us, created_at
      ) VALUES (?, ?, 0, 1234567, 60000000, ?)
      """,
      (segment_id, drive_id, now),
    )
    connection.execute(
      """
      INSERT INTO objects(sha256, size, storage_path, created_at)
      VALUES (?, ?, ?, ?)
      """,
      (digest, len(content), storage_path.as_posix(), now),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, camera, relative_path, storage_path, size,
        mime_type, status, created_at
      ) VALUES (?, 'device-one', ?, ?, ?, 'fcamera', 'road',
        'route-one/0/fcamera.hevc', ?, ?, 'video/hevc', 'stored', ?)
      """,
      (
        source_id,
        drive_id,
        segment_id,
        digest,
        storage_path.as_posix(),
        len(content),
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, camera, relative_path, storage_path, size,
        mime_type, codec, duration_us, status, source_artifact_id,
        created_at
      ) VALUES (?, 'device-one', ?, ?, ?, 'derived_video', 'road',
        'route-one/0/road.av1.webm', ?, ?, 'video/webm', 'av1',
        60000000, 'ready', ?, ?)
      """,
      (
        derived_id,
        drive_id,
        segment_id,
        digest,
        storage_path.as_posix(),
        len(content),
        source_id,
        now,
      ),
    )

  manifest = admin_client.get(
    f"/api/v1/drives/{drive_id}/media-manifest?camera=road",
  )
  assert manifest.status_code == 200
  assert manifest.json() == {
    "drive_id": drive_id,
    "camera": "road",
    "synchronized": False,
    "items": [{
      "segment_number": 0,
      "start_t_us": 1_234_567,
      "duration_us": 60_000_000,
      "artifact_id": derived_id,
      "url": f"/api/v1/artifacts/{derived_id}/content",
      "mime_type": "video/webm",
      "codec": "av1",
      "fps": None,
      "sync_mode": "approximate",
      "sync_url": None,
      "frame_index_artifact_id": None,
      "frame_index_url": None,
      "sync_reason": "video_invalid",
      "video_sha256": None,
      "timeline_origin": None,
    }],
  }

  suffix = admin_client.get(
    f"/api/v1/artifacts/{derived_id}/content",
    headers={"Range": "bytes=-4"},
  )
  assert suffix.status_code == 206
  assert suffix.content == content[-4:]
  assert suffix.headers["accept-ranges"] == "bytes"
  assert suffix.headers["content-disposition"].startswith("inline;")
  assert suffix.headers["content-security-policy"] == ("default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox")

  drive_stream = admin_client.get(
    f"/api/v1/drives/{drive_id}/media?camera=road&segment=0",
    headers={"Range": "bytes=0-3"},
  )
  assert drive_stream.status_code == 206
  assert drive_stream.content == content[:4]
  assert drive_stream.headers["content-disposition"].startswith("inline;")


def test_dashboard_health_openapi_and_extension_errors(
  admin_client: TestClient,
) -> None:
  health = admin_client.get("/api/v1/health")
  assert health.status_code == 200
  assert health.json()["status"] == "ok"
  assert health.json()["service"] == "comma-companion-api"
  assert set(health.json()) == {"status", "service", "time"}

  readiness = admin_client.get("/api/v1/readiness")
  assert readiness.status_code == 200
  assert readiness.json()["status"] == "ready"
  assert readiness.json()["database"]["journal_mode"] == "wal"
  assert readiness.json()["archive"] == {
    "available": True,
    "writable": True,
  }

  dashboard = admin_client.get("/api/v1/dashboard")
  assert dashboard.status_code == 200
  assert dashboard.json()["devices_total"] == 1
  assert "bytes_per_second_60s" in dashboard.json()["upload"]
  assert dashboard.json()["archive"] == readiness.json()["archive"]

  openapi = admin_client.get("/api/openapi.json")
  assert openapi.status_code == 200
  paths = openapi.json()["paths"]
  assert "/api/v1/drives/{drive_id}/series" in paths
  assert "/api/v1/simulator/capabilities" in paths
  assert "/api/v1/drives/{drive_id}/simulations" in paths
  assert "/api/v1/jobs/{job_id}" in paths

  capabilities = admin_client.get("/api/v1/simulator/capabilities")
  assert capabilities.status_code == 200
  assert capabilities.json()["available"] is False
  assert capabilities.json()["modes"] == ["approximate_closed_loop"]


def test_dashboard_worker_eta_uses_recent_transcode_throughput(
  admin_client: TestClient,
) -> None:
  database = admin_client.app.state.database
  with database.transaction(immediate=True) as connection:
    for index, completed_at in enumerate(
      (
        "2026-07-29T10:00:00Z",
        "2026-07-29T10:01:00Z",
        "2026-07-29T10:02:00Z",
        "2026-07-29T10:03:00Z",
      )
    ):
      connection.execute(
        """
        INSERT INTO jobs(
          id, type, state, payload_json, progress,
          created_at, updated_at, completed_at
        ) VALUES (?, 'transcode_video', 'succeeded', '{}', 1, ?, ?, ?)
        """,
        (f"completed-{index}", completed_at, completed_at, completed_at),
      )
    connection.execute(
      """
      INSERT INTO jobs(
        id, type, state, payload_json, progress, created_at, updated_at
      ) VALUES
        ('running-half', 'transcode_video', 'running', '{}', 0.5,
         '2026-07-29T10:04:00Z', '2026-07-29T10:04:00Z'),
        ('queued-full', 'transcode_video', 'queued', '{}', 0,
         '2026-07-29T10:04:00Z', '2026-07-29T10:04:00Z')
      """,
    )

  response = admin_client.get("/api/v1/dashboard")
  assert response.status_code == 200, response.text
  worker = response.json()["worker"]
  assert worker["transcode_jobs_remaining"] == 2
  assert worker["transcode_seconds_per_job"] == 60.0
  assert worker["eta_seconds"] == 90


def test_dashboard_archive_health_matches_degraded_readiness(
  admin_client: TestClient,
) -> None:
  current_settings = admin_client.app.state.settings
  admin_client.app.state.settings = replace(
    current_settings,
    archive_root=current_settings.archive_root / "missing",
  )

  readiness = admin_client.get("/api/v1/readiness")
  assert readiness.status_code == 503
  assert readiness.json()["archive"] == {
    "available": False,
    "writable": False,
  }

  dashboard = admin_client.get("/api/v1/dashboard")
  assert dashboard.status_code == 200
  assert dashboard.json()["archive"] == readiness.json()["archive"]
