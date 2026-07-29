from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from comma_companion.app import create_app
from comma_companion.db import Database, isoformat
from comma_companion.media_sync import inspect_media_sync
from conftest import ADMIN_PASSWORD, ORIGIN


@pytest.fixture
def media_sync_client(settings) -> Iterator[TestClient]:
  application = create_app(settings)
  with TestClient(application, base_url=ORIGIN) as client:
    response = client.post(
      "/api/v1/auth/login",
      headers={"Origin": ORIGIN},
      json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    yield client


def _write(archive_root: Path, relative: str, content: bytes) -> str:
  path = archive_root / Path(*relative.split("/"))
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_bytes(content)
  return hashlib.sha256(content).hexdigest()


def _seed_exact_sync(
  client: TestClient,
  *,
  telemetry_frame_count: int = 2,
  telemetry_state: str = "complete",
  telemetry_segment_number: int = 0,
  segment_state: str = "complete",
  frame_quality_issue_count: int = 0,
  missing_camera_timestamp: bool = False,
  invalid_encoder_event: bool = False,
  log_type: str = "rlog",
  camera_range_end_offset_us: int = 0,
) -> dict[str, object]:
  database: Database = client.app.state.database
  archive_root: Path = client.app.state.settings.archive_root
  drive_id = uuid4().hex
  segment_id = uuid4().hex
  raw_id = uuid4().hex
  video_id = uuid4().hex
  frame_index_id = uuid4().hex
  now = isoformat()

  raw_content = f"source-hevc-{raw_id}".encode()
  raw_relative = f"objects/test/{raw_id}"
  raw_sha256 = _write(archive_root, raw_relative, raw_content)
  video_content = f"validated-av1-webm-{drive_id}".encode()
  video_relative = f"derived/device-one/route-{drive_id}/0/road.av1.webm"
  video_sha256 = _write(archive_root, video_relative, video_content)

  frame_index_relative = f"derived/device-one/route-{drive_id}/0/road.av1.frames.json"
  frame_document = {
    "schema_version": 1,
    "mapping_type": "encoded_frame_pts",
    "join_key": ["camera", "segment_num", "segment_frame_id"],
    "ordinal_basis": 0,
    "source_frame_key": "segment_frame_id",
    "camera": "road",
    "segment_num": 0,
    "source_artifact_id": raw_id,
    "video": {
      "path": str(archive_root / Path(*video_relative.split("/"))),
      "sha256": video_sha256,
    },
    "time_base": {
      "numerator": 1,
      "denominator": 1_000_000,
      "text": "1/1000000",
    },
    "frame_count": 2,
    "first_pts": 125_000,
    "last_end_pts": 225_000,
    "duration_inference_count": 0,
    "frames": [
      {
        "ordinal": 0,
        "segment_frame_id": 0,
        "pts": 125_000,
        "duration": 50_000,
        "pts_us": 125_000,
        "duration_us": 50_000,
        "keyframe": True,
      },
      {
        "ordinal": 1,
        "segment_frame_id": 1,
        "pts": 175_000,
        "duration": 50_000,
        "pts_us": 175_000,
        "duration_us": 50_000,
        "keyframe": False,
      },
    ],
  }
  frame_content = (
    json.dumps(
      frame_document,
      separators=(",", ":"),
      sort_keys=True,
    ).encode("utf-8")
    + b"\n"
  )
  frame_index_sha256 = _write(
    archive_root,
    frame_index_relative,
    frame_content,
  )

  timeline_origin_ns = 1_000_000_000
  telemetry_rows = []
  for ordinal in range(telemetry_frame_count):
    drive_t_us = 12_001_000 + (ordinal * 50_000)
    timestamp_eof_ns = timeline_origin_ns + (drive_t_us * 1_000)
    telemetry_rows.append(
      {
        "event_valid": not (invalid_encoder_event and ordinal == 0),
        "t_us": drive_t_us,
        "segment_num": telemetry_segment_number,
        "segment_frame_id": ordinal,
        "timestamp_sof_ns": ("0" if missing_camera_timestamp and ordinal == 0 else str(timestamp_eof_ns - 1_000_000)),
        "timestamp_eof_ns": str(timestamp_eof_ns),
      }
    )
  telemetry_record = {
    "record": "frame_chunk",
    "camera": "road",
    "chunk": 0,
    "rows": telemetry_rows,
  }
  telemetry_content = (
    json.dumps(
      telemetry_record,
      separators=(",", ":"),
      sort_keys=True,
    ).encode("utf-8")
    + b"\n"
  )
  telemetry_sha256 = hashlib.sha256(telemetry_content).hexdigest()
  telemetry_relative = f"telemetry/device-one/route-{drive_id}/v1/telemetry.{telemetry_sha256}.ndjson"
  _write(archive_root, telemetry_relative, telemetry_content)
  timeline_version = "c" * 64

  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO drives(
        id, device_id, route_name, telemetry_ready, created_at
      )
      VALUES (?, 'device-one', ?, ?, ?)
      """,
      (
        drive_id,
        f"route-{drive_id}",
        int(telemetry_state == "complete"),
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO route_inventories(
        id, device_id, drive_id, route_name, generation,
        manifest_sha256, rlog_source_fingerprint,
        manifest_size, materialized_row_count,
        state, route_closed, manifest_json, created_at
      ) VALUES (
        ?, 'device-one', ?, ?, 1, ?, 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
        2, 0, ?, 1, '{}', ?
      )
      """,
      (
        uuid4().hex,
        drive_id,
        f"route-{drive_id}",
        hashlib.sha256(drive_id.encode()).hexdigest(),
        ("complete" if telemetry_state == "complete" else "partial"),
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO segments(
        id, drive_id, number, start_t_us, duration_us, created_at
      ) VALUES (?, ?, 0, 12001000, 100000, ?)
      """,
      (segment_id, drive_id, now),
    )
    for digest, content, relative in (
      (raw_sha256, raw_content, raw_relative),
      (video_sha256, video_content, video_relative),
      (frame_index_sha256, frame_content, frame_index_relative),
    ):
      connection.execute(
        """
        INSERT INTO objects(sha256, size, storage_path, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (digest, len(content), relative, now),
      )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256, kind,
        camera, relative_path, storage_path, size, mime_type,
        frame_count, status, created_at
      ) VALUES (
        ?, 'device-one', ?, ?, ?, 'fcamera', 'road', ?, ?, ?,
        'video/hevc', 2, 'ready', ?
      )
      """,
      (
        raw_id,
        drive_id,
        segment_id,
        raw_sha256,
        raw_relative,
        raw_relative,
        len(raw_content),
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256, kind,
        camera, relative_path, storage_path, size, mime_type, codec,
        duration_us, frame_count, time_map_path, status,
        source_artifact_id, created_at
      ) VALUES (
        ?, 'device-one', ?, ?, ?, 'derived_video', 'road', ?, ?, ?,
        'video/webm', 'av1', 100000, 2, ?, 'ready', ?, ?
      )
      """,
      (
        video_id,
        drive_id,
        segment_id,
        video_sha256,
        video_relative,
        video_relative,
        len(video_content),
        frame_index_relative,
        raw_id,
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256, kind,
        camera, relative_path, storage_path, size, mime_type,
        duration_us, frame_count, status, source_artifact_id, created_at
      ) VALUES (
        ?, 'device-one', ?, ?, ?, 'video_frame_index', 'road',
        ?, ?, ?, 'application/json', 100000, 2, 'ready', ?, ?
      )
      """,
      (
        frame_index_id,
        drive_id,
        segment_id,
        frame_index_sha256,
        frame_index_relative,
        frame_index_relative,
        len(frame_content),
        video_id,
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO telemetry_indexes(
        drive_id, schema_version, state, ndjson_path, ndjson_sha256,
        manifest_json, signal_catalog_json, source_fingerprint,
        created_at, updated_at
      ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        drive_id,
        telemetry_state,
        telemetry_relative,
        telemetry_sha256,
        json.dumps(
          {
            "state": telemetry_state,
            "publication_ready": telemetry_state == "complete",
            "timeline_version": timeline_version,
            "timebase": {
              "unit": "us",
              "origin_log_mono_time_ns": str(timeline_origin_ns),
              "conversion": "floor((logMonoTime-origin)/1000)",
            },
            "completeness": {
              "segments": [
                {
                  "segment_num": 0,
                  "state": segment_state,
                  "log_type": log_type,
                  "camera_ranges_us": {
                    "road": [
                      telemetry_rows[0]["t_us"],
                      (telemetry_rows[-1]["t_us"] + camera_range_end_offset_us),
                    ],
                  },
                  "frame_quality_issue_count": (frame_quality_issue_count),
                }
              ],
            },
          }
        ),
        json.dumps({"record": "signal_catalog", "signals": []}),
        "d" * 64,
        now,
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO telemetry_frame_chunks(
        drive_id, camera, segment_number, chunk_index, ndjson_path,
        byte_offset, byte_length, record_sha256
      ) VALUES (?, 'road', 0, 0, ?, 0, ?, ?)
      """,
      (
        drive_id,
        telemetry_relative,
        len(telemetry_content),
        telemetry_sha256,
      ),
    )
  return {
    "drive_id": drive_id,
    "video_id": video_id,
    "video_sha256": video_sha256,
    "video_relative": video_relative,
    "frame_index_id": frame_index_id,
    "frame_index_sha256": frame_index_sha256,
    "frame_index_relative": frame_index_relative,
    "telemetry_sha256": telemetry_sha256,
    "timeline_version": timeline_version,
  }


def _request(client: TestClient) -> Request:
  return Request(
    {
      "type": "http",
      "app": client.app,
      "method": "GET",
      "path": "/",
      "root_path": "",
      "scheme": "https",
      "query_string": b"",
      "headers": [],
      "client": ("testclient", 50000),
      "server": ("comma.test", 443),
    }
  )


def test_media_sync_exact_join_pins_cache_and_inspection(
  media_sync_client: TestClient,
) -> None:
  seeded = _seed_exact_sync(media_sync_client)
  path = f"/api/v1/drives/{seeded['drive_id']}/media-sync"
  pinned = media_sync_client.get(
    path,
    params={
      "camera": "road",
      "segment": 0,
      "telemetry_sha256": seeded["telemetry_sha256"],
      "frame_index_sha256": seeded["frame_index_sha256"],
      "video_sha256": seeded["video_sha256"],
      "video_artifact_id": seeded["video_id"],
      "frame_index_artifact_id": seeded["frame_index_id"],
    },
  )
  assert pinned.status_code == 200, pinned.text
  assert pinned.json() == {
    "drive_id": seeded["drive_id"],
    "camera": "road",
    "segment_number": 0,
    "video_artifact_id": seeded["video_id"],
    "video_sha256": seeded["video_sha256"],
    "frame_index_artifact_id": seeded["frame_index_id"],
    "frame_index_sha256": seeded["frame_index_sha256"],
    "telemetry_sha256": seeded["telemetry_sha256"],
    "timeline_version": seeded["timeline_version"],
    "timeline_origin": "stable",
    "integrity_scope": "referenced_frame_rows",
    "mode": "exact",
    "coverage": {
      "start_t_us": 12_001_000,
      "end_t_us": 12_051_000,
      "first_pts_us": 125_000,
      "last_end_pts_us": 225_000,
    },
    "points": [
      {
        "segment_frame_id": 0,
        "pts_us": 125_000,
        "duration_us": 50_000,
        "drive_t_us": 12_001_000,
        "keyframe": True,
      },
      {
        "segment_frame_id": 1,
        "pts_us": 175_000,
        "duration_us": 50_000,
        "drive_t_us": 12_051_000,
        "keyframe": False,
      },
    ],
  }
  assert pinned.headers["cache-control"] == ("private, max-age=31536000, immutable")
  assert pinned.headers["etag"].startswith('"')

  unpinned = media_sync_client.get(
    path,
    params={"camera": "road", "segment": 0},
  )
  assert unpinned.status_code == 200
  assert unpinned.headers["cache-control"] == "no-store"
  assert unpinned.headers["etag"] == pinned.headers["etag"]

  manifest = media_sync_client.get(
    f"/api/v1/drives/{seeded['drive_id']}/media-manifest",
    params={"camera": "road"},
  )
  assert manifest.status_code == 200, manifest.text
  item = manifest.json()["items"][0]
  assert item["sync_mode"] == "exact"
  assert item["video_sha256"] == seeded["video_sha256"]
  assert item["timeline_origin"] == "stable"
  manifest_sync = media_sync_client.get(item["sync_url"])
  assert manifest_sync.status_code == 200, manifest_sync.text
  assert manifest_sync.headers["cache-control"] == ("private, max-age=31536000, immutable")

  inspection = inspect_media_sync(
    _request(media_sync_client),
    str(seeded["drive_id"]),
    "road",
    0,
    include_points=False,
  )
  assert inspection == {
    "ready": True,
    "reason": None,
    "video_artifact_id": seeded["video_id"],
    "video_sha256": seeded["video_sha256"],
    "frame_index_artifact_id": seeded["frame_index_id"],
    "frame_index_sha256": seeded["frame_index_sha256"],
    "telemetry_sha256": seeded["telemetry_sha256"],
    "timeline_version": seeded["timeline_version"],
    "timeline_origin": "stable",
    "integrity_scope": "referenced_frame_rows",
    "segment_window": {
      "start_t_us": 12_001_000,
      "duration_us": 100_000,
    },
  }


def test_media_sync_and_playback_use_canonical_object_path(
  media_sync_client: TestClient,
) -> None:
  seeded = _seed_exact_sync(media_sync_client)
  database: Database = media_sync_client.app.state.database
  archive_root: Path = media_sync_client.app.state.settings.archive_root
  original_relative = str(seeded["video_relative"])
  original_path = archive_root / Path(*original_relative.split("/"))
  content = original_path.read_bytes()
  digest = str(seeded["video_sha256"])
  canonical_relative = (
    f"objects/sha256/{digest[:2]}/{digest[2:4]}/{digest}"
  )
  canonical_path = archive_root / canonical_relative
  canonical_path.parent.mkdir(parents=True, exist_ok=True)
  original_path.replace(canonical_path)
  with database.transaction(immediate=True) as connection:
    connection.execute(
      "UPDATE objects SET storage_path = ? WHERE sha256 = ?",
      (canonical_relative, digest),
    )
    connection.execute(
      "UPDATE artifacts SET storage_path = ? WHERE id = ?",
      (canonical_relative, seeded["video_id"]),
    )

  response = media_sync_client.get(
    f"/api/v1/drives/{seeded['drive_id']}/media-sync",
    params={"camera": "road", "segment": 0},
  )
  assert response.status_code == 200, response.text
  playback = media_sync_client.get(
    f"/api/v1/artifacts/{seeded['video_id']}/content",
  )
  assert playback.status_code == 200, playback.text
  assert playback.content == content
  assert not original_path.exists()


def test_media_manifest_exact_window_comes_from_sync_snapshot(
  media_sync_client: TestClient,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  import comma_companion.app as app_module

  seeded = _seed_exact_sync(media_sync_client)
  database: Database = media_sync_client.app.state.database
  original_inspect = app_module.inspect_media_sync
  reindexed_start_t_us = 13_001_000
  called = False

  def reindex_then_inspect(*args, **kwargs):
    nonlocal called
    if not called:
      called = True
      database.execute(
        """
        UPDATE segments
        SET start_t_us = ?, duration_us = ?
        WHERE drive_id = ? AND number = 0
        """,
        (reindexed_start_t_us, 200_000, seeded["drive_id"]),
      )
    return original_inspect(*args, **kwargs)

  monkeypatch.setattr(
    app_module,
    "inspect_media_sync",
    reindex_then_inspect,
  )
  response = media_sync_client.get(
    f"/api/v1/drives/{seeded['drive_id']}/media-manifest",
    params={"camera": "road"},
  )

  assert response.status_code == 200, response.text
  item = response.json()["items"][0]
  assert item["sync_mode"] == "exact"
  assert item["start_t_us"] == reindexed_start_t_us
  # The transcoded artifact duration remains the authoritative media length.
  assert item["duration_us"] == 100_000


def test_media_sync_rejects_stale_pins_and_incomplete_join(
  media_sync_client: TestClient,
) -> None:
  seeded = _seed_exact_sync(media_sync_client, telemetry_frame_count=1)
  path = f"/api/v1/drives/{seeded['drive_id']}/media-sync"

  incomplete = media_sync_client.get(
    path,
    params={"camera": "road", "segment": 0},
  )
  assert incomplete.status_code == 409
  assert incomplete.json()["error"] == {
    "code": "media_sync_not_ready",
    "message": "Exact media synchronization is not ready",
    "details": {"reason": "frame_join_mismatch"},
  }
  inspection = inspect_media_sync(
    _request(media_sync_client),
    str(seeded["drive_id"]),
    "road",
    0,
    include_points=False,
  )
  assert inspection == {
    "ready": False,
    "reason": "frame_join_mismatch",
  }

  valid = _seed_exact_sync(media_sync_client)
  stale = media_sync_client.get(
    f"/api/v1/drives/{valid['drive_id']}/media-sync",
    params={
      "camera": "road",
      "segment": 0,
      "telemetry_sha256": "0" * 64,
      "frame_index_sha256": valid["frame_index_sha256"],
    },
  )
  assert stale.status_code == 409
  assert stale.json()["error"]["details"] == {
    "reason": "telemetry_generation_changed",
  }

  stale_video = media_sync_client.get(
    f"/api/v1/drives/{valid['drive_id']}/media-sync",
    params={
      "camera": "road",
      "segment": 0,
      "video_artifact_id": "superseded-video",
    },
  )
  assert stale_video.status_code == 409
  assert stale_video.json()["error"]["details"] == {
    "reason": "video_generation_changed",
  }


def test_media_sync_rejects_partial_unbound_generation(
  media_sync_client: TestClient,
) -> None:
  seeded = _seed_exact_sync(
    media_sync_client,
    telemetry_state="partial",
  )
  response = media_sync_client.get(
    f"/api/v1/drives/{seeded['drive_id']}/media-sync",
    params={
      "camera": "road",
      "segment": 0,
      "telemetry_sha256": seeded["telemetry_sha256"],
      "frame_index_sha256": seeded["frame_index_sha256"],
      "video_sha256": seeded["video_sha256"],
      "video_artifact_id": seeded["video_id"],
      "frame_index_artifact_id": seeded["frame_index_id"],
    },
  )
  assert response.status_code == 409
  assert response.json()["error"]["details"] == {
    "reason": "telemetry_inventory_mismatch",
  }

  manifest = media_sync_client.get(
    f"/api/v1/drives/{seeded['drive_id']}/media-manifest",
    params={"camera": "road"},
  )
  assert manifest.status_code == 200, manifest.text
  item = manifest.json()["items"][0]
  assert item["sync_mode"] == "approximate"
  assert item["sync_reason"] == "telemetry_inventory_mismatch"
  assert item["sync_url"] is None

  inspection = inspect_media_sync(
    _request(media_sync_client),
    str(seeded["drive_id"]),
    "road",
    0,
    include_points=False,
  )
  assert inspection == {
    "ready": False,
    "reason": "telemetry_inventory_mismatch",
  }


def test_media_sync_rejects_latest_inventory_generation_mismatch(
  media_sync_client: TestClient,
) -> None:
  seeded = _seed_exact_sync(media_sync_client)
  database: Database = media_sync_client.app.state.database
  with database.transaction(immediate=True) as connection:
    connection.execute(
      "UPDATE drives SET telemetry_ready = 0 WHERE id = ?",
      (seeded["drive_id"],),
    )
    connection.execute(
      """
      UPDATE route_inventories
      SET rlog_source_fingerprint = ?
      WHERE drive_id = ?
      """,
      ("e" * 64, seeded["drive_id"]),
    )

  response = media_sync_client.get(
    f"/api/v1/drives/{seeded['drive_id']}/media-sync",
    params={"camera": "road", "segment": 0},
  )
  assert response.status_code == 409
  assert response.json()["error"]["details"] == {
    "reason": "telemetry_inventory_mismatch",
  }
  manifest = media_sync_client.get(
    f"/api/v1/drives/{seeded['drive_id']}/media-manifest",
    params={"camera": "road"},
  )
  assert manifest.status_code == 200, manifest.text
  item = manifest.json()["items"][0]
  assert item["sync_mode"] == "approximate"
  assert item["sync_reason"] == "telemetry_inventory_mismatch"
  assert item["sync_url"] is None
  inspection = inspect_media_sync(
    _request(media_sync_client),
    str(seeded["drive_id"]),
    "road",
    0,
    include_points=False,
  )
  assert inspection == {
    "ready": False,
    "reason": "telemetry_inventory_mismatch",
  }


@pytest.mark.parametrize(
  ("seed_options", "reason"),
  [
    (
      {"telemetry_segment_number": 1},
      "telemetry_frame_map_missing",
    ),
    (
      {"segment_state": "partial"},
      "target_segment_incomplete",
    ),
    (
      {"frame_quality_issue_count": 1},
      "target_segment_incomplete",
    ),
    (
      {"missing_camera_timestamp": True},
      "telemetry_frame_map_invalid",
    ),
    (
      {"invalid_encoder_event": True},
      "telemetry_frame_map_invalid",
    ),
    (
      {"camera_range_end_offset_us": 50_000},
      "telemetry_frame_map_invalid",
    ),
  ],
)
def test_media_sync_requires_complete_target_segment_camera_proof(
  media_sync_client: TestClient,
  seed_options: dict[str, object],
  reason: str,
) -> None:
  seeded = _seed_exact_sync(
    media_sync_client,
    **seed_options,
  )
  response = media_sync_client.get(
    f"/api/v1/drives/{seeded['drive_id']}/media-sync",
    params={"camera": "road", "segment": 0},
  )
  assert response.status_code == 409
  assert response.json()["error"]["details"] == {"reason": reason}

  inspection = inspect_media_sync(
    _request(media_sync_client),
    str(seeded["drive_id"]),
    "road",
    0,
    include_points=False,
  )
  assert inspection == {"ready": False, "reason": reason}


def test_media_sync_allows_qlog_camera_proof(
  media_sync_client: TestClient,
) -> None:
  seeded = _seed_exact_sync(media_sync_client, log_type="qlog")
  response = media_sync_client.get(
    f"/api/v1/drives/{seeded['drive_id']}/media-sync",
    params={"camera": "road", "segment": 0},
  )
  assert response.status_code == 200, response.text
  assert response.json()["mode"] == "exact"


def test_media_sync_rejects_same_size_video_tampering(
  media_sync_client: TestClient,
) -> None:
  seeded = _seed_exact_sync(media_sync_client)
  archive_root = media_sync_client.app.state.settings.archive_root
  video_path = archive_root / Path(
    *str(seeded["video_relative"]).split("/"),
  )
  content = video_path.read_bytes()
  video_path.write_bytes(bytes([content[0] ^ 1]) + content[1:])

  response = media_sync_client.get(
    f"/api/v1/drives/{seeded['drive_id']}/media-sync",
    params={"camera": "road", "segment": 0},
  )
  assert response.status_code == 409
  assert response.json()["error"]["details"] == {
    "reason": "video_invalid",
  }

  inspection = inspect_media_sync(
    _request(media_sync_client),
    str(seeded["drive_id"]),
    "road",
    0,
    include_points=False,
  )
  assert inspection == {"ready": False, "reason": "video_invalid"}


def test_media_sync_rejects_hash_valid_schema_invalid_frame_index(
  media_sync_client: TestClient,
) -> None:
  seeded = _seed_exact_sync(media_sync_client)
  archive_root = media_sync_client.app.state.settings.archive_root
  original_relative = str(seeded["frame_index_relative"])
  original_path = archive_root / Path(*original_relative.split("/"))
  document = json.loads(original_path.read_text(encoding="utf-8"))
  document["ordinal_basis"] = False
  invalid_content = json.dumps(document, separators=(",", ":"), sort_keys=True).encode() + b"\n"
  invalid_relative = original_relative.replace(
    ".frames.json",
    ".invalid.frames.json",
  )
  invalid_sha256 = _write(
    archive_root,
    invalid_relative,
    invalid_content,
  )
  database: Database = media_sync_client.app.state.database
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO objects(sha256, size, storage_path, created_at)
      VALUES (?, ?, ?, ?)
      """,
      (
        invalid_sha256,
        len(invalid_content),
        invalid_relative,
        isoformat(),
      ),
    )
    connection.execute(
      """
      UPDATE artifacts
      SET object_sha256 = ?, relative_path = ?, storage_path = ?, size = ?
      WHERE id = ?
      """,
      (
        invalid_sha256,
        invalid_relative,
        invalid_relative,
        len(invalid_content),
        seeded["frame_index_id"],
      ),
    )

  response = media_sync_client.get(
    f"/api/v1/drives/{seeded['drive_id']}/media-sync",
    params={"camera": "road", "segment": 0},
  )
  assert response.status_code == 409
  assert response.json()["error"]["details"] == {
    "reason": "frame_index_invalid",
  }
