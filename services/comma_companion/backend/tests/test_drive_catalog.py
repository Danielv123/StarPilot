from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from comma_companion.db import isoformat


def _object(connection, content: bytes, name: str) -> str:
  digest = hashlib.sha256(content).hexdigest()
  connection.execute(
    """
    INSERT INTO objects(sha256, size, storage_path, created_at)
    VALUES (?, ?, ?, ?)
    """,
    (digest, len(content), f"objects/{name}", isoformat()),
  )
  return digest


def _complete_inventory(
  connection,
  *,
  drive_id: str,
  route_name: str,
  video_path: str,
  video_sha256: str,
  video_size: int,
  rlog_path: str,
  rlog_sha256: str,
  rlog_size: int,
  now: str,
) -> str:
  inventory_id = f"inventory-{drive_id}"
  rlog_source_fingerprint = hashlib.sha256(
    json.dumps(
      [{"segment_number": 0, "sha256": rlog_sha256}],
      separators=(",", ":"),
      sort_keys=True,
    ).encode(),
  ).hexdigest()
  manifest = {
    "capability_source": "configured+route_union",
    "closure_evidence": ["no_lock", "offroad", "stable_duration"],
    "expected_streams": [
      {
        "role": "realdata|rlog|-",
        "root_name": "realdata",
        "artifact_type": "rlog",
        "camera": None,
      },
      {
        "role": "realdata|video|road",
        "root_name": "realdata",
        "artifact_type": "video",
        "camera": "road",
      },
    ],
    "missing_segment_numbers": [],
  }
  connection.execute(
    """
    INSERT INTO route_inventories(
      id, device_id, drive_id, route_name, generation,
      manifest_sha256, rlog_source_fingerprint, state,
      route_closed, manifest_json, manifest_size,
      materialized_row_count, created_at
    ) VALUES (
      ?, 'device-one', ?, ?, 1, ?, ?, 'complete', 1, ?, ?, 3, ?
    )
    """,
    (
      inventory_id,
      drive_id,
      route_name,
      hashlib.sha256(inventory_id.encode()).hexdigest(),
      rlog_source_fingerprint,
      json.dumps(manifest),
      len(json.dumps(manifest).encode("utf-8")),
      now,
    ),
  )
  connection.execute(
    """
    INSERT INTO route_inventory_segments(
      inventory_id, segment_number, missing
    ) VALUES (?, 0, 0)
    """,
    (inventory_id,),
  )
  connection.executemany(
    """
    INSERT INTO route_inventory_expected_files(
      inventory_id, location_key, segment_number, role, is_stream,
      status, relative_path, artifact_type, camera,
      declared_size, declared_mtime_ns, declared_sha256
    ) VALUES (?, 'segment:0', 0, ?, 1, 'present', ?, ?, ?, ?, 1, ?)
    """,
    [
      (
        inventory_id,
        "realdata|rlog|-",
        rlog_path,
        "rlog",
        None,
        rlog_size,
        rlog_sha256,
      ),
      (
        inventory_id,
        "realdata|video|road",
        video_path,
        "video",
        "road",
        video_size,
        video_sha256,
      ),
    ],
  )
  return rlog_source_fingerprint


def test_successful_media_retry_clears_historical_failure(
  admin_client,
) -> None:
  database = admin_client.app.state.database
  drive_id = uuid4().hex
  segment_id = uuid4().hex
  source_id = uuid4().hex
  derived_id = uuid4().hex
  failed_job_id = uuid4().hex
  succeeded_job_id = uuid4().hex
  now = isoformat()
  with database.transaction(immediate=True) as connection:
    source_sha = _object(connection, b"raw", "raw")
    rlog_sha = _object(connection, b"log", "rlog")
    derived_sha = _object(connection, b"av1", "derived")
    connection.execute(
      """
      INSERT INTO drives(
        id, device_id, route_name, telemetry_ready, created_at
      ) VALUES (?, 'device-one', 'route-retry', 1, ?)
      """,
      (drive_id, now),
    )
    connection.execute(
      """
      INSERT INTO segments(id, drive_id, number, created_at)
      VALUES (?, ?, 0, ?)
      """,
      (segment_id, drive_id, now),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, camera, relative_path, storage_path, size,
        status, created_at
      ) VALUES (
        ?, 'device-one', ?, ?, ?, 'fcamera', 'road',
        'route-retry/0/fcamera.hevc', 'objects/raw', 3,
        'stored', ?
      )
      """,
      (source_id, drive_id, segment_id, source_sha, now),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, relative_path, storage_path, size, status, created_at
      ) VALUES (
        ?, 'device-one', ?, ?, ?, 'rlog',
        'route-retry/0/rlog', 'objects/rlog', 3, 'stored', ?
      )
      """,
      (uuid4().hex, drive_id, segment_id, rlog_sha, now),
    )
    rlog_source_fingerprint = _complete_inventory(
      connection,
      drive_id=drive_id,
      route_name="route-retry",
      video_path="route-retry/0/fcamera.hevc",
      video_sha256=source_sha,
      video_size=3,
      rlog_path="route-retry/0/rlog",
      rlog_sha256=rlog_sha,
      rlog_size=3,
      now=now,
    )
    connection.execute(
      """
      INSERT INTO telemetry_indexes(
        drive_id, schema_version, state, ndjson_path, ndjson_sha256,
        manifest_json, signal_catalog_json, source_fingerprint,
        created_at, updated_at
      ) VALUES (?, 1, 'complete', 'telemetry/retry.ndjson', ?, '{}', '[]', ?, ?, ?)
      """,
      (
        drive_id,
        "e" * 64,
        rlog_source_fingerprint,
        now,
        now,
      ),
    )
    payload = json.dumps(
      {"artifact_id": source_id},
      separators=(",", ":"),
      sort_keys=True,
    )
    connection.execute(
      """
      INSERT INTO jobs(
        id, type, state, payload_json, error, created_at, updated_at
      ) VALUES (
        ?, 'transcode_video', 'failed', ?, 'encoder failed',
        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
      )
      """,
      (failed_job_id, payload),
    )

  failed = admin_client.get(f"/api/v1/drives/{drive_id}")
  assert failed.status_code == 200
  assert failed.json()["readiness"] == "failed"
  assert failed.json()["failed_media"] == 1

  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, camera, relative_path, storage_path, size,
        mime_type, codec, status, source_artifact_id, created_at
      ) VALUES (
        ?, 'device-one', ?, ?, ?, 'derived_video', 'road',
        'derived/route-retry/0/road.webm', 'objects/derived', 3,
        'video/webm', 'av1', 'ready', ?, ?
      )
      """,
      (
        derived_id,
        drive_id,
        segment_id,
        derived_sha,
        source_id,
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO jobs(
        id, type, state, payload_json, progress,
        created_at, updated_at, completed_at
      ) VALUES (
        ?, 'transcode_video', 'succeeded', ?, 1,
        '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z',
        '2026-01-02T00:00:00Z'
      )
      """,
      (succeeded_job_id, payload),
    )

  recovered = admin_client.get(f"/api/v1/drives/{drive_id}")
  assert recovered.status_code == 200
  assert recovered.json()["readiness"] == "ready"
  assert recovered.json()["failed_media"] == 0
  assert recovered.json()["expected_media"] == 1
  assert recovered.json()["ready_media"] == 1
  assert recovered.json()["ready_segments"] == 1


def test_catalog_search_and_readiness_filter_precede_pagination(
  admin_client,
) -> None:
  database = admin_client.app.state.database
  with database.transaction(immediate=True) as connection:
    for index, (route_name, duration_us) in enumerate(
      (
        ("needle-newest", 3_000_000),
        ("unrelated-route", 9_000_000),
        ("needle-oldest", 1_000_000),
      )
    ):
      connection.execute(
        """
        INSERT INTO drives(
          id, device_id, route_name, started_at, duration_us, created_at
        ) VALUES (?, 'device-one', ?, ?, ?, ?)
        """,
        (
          f"drive-{index}",
          route_name,
          f"2026-07-2{9 - index}T12:00:00Z",
          duration_us,
          f"2026-07-2{9 - index}T12:00:00Z",
        ),
      )

  response = admin_client.get(
    "/api/v1/drives?q=needle&readiness=importing&limit=1&offset=1",
  )
  assert response.status_code == 200, response.text
  catalog = response.json()
  assert catalog["total"] == 2
  assert catalog["limit"] == 1
  assert catalog["offset"] == 1
  assert [item["route_name"] for item in catalog["items"]] == [
    "needle-oldest",
  ]
  assert catalog["summary"] == {
    "duration_us": 4_000_000,
    "stored_bytes": 0,
    "by_readiness": {
      "importing": 2,
      "processing": 0,
      "ready": 0,
      "partial": 0,
      "failed": 0,
    },
  }

  beyond_end = admin_client.get(
    "/api/v1/drives?q=needle&limit=1&offset=10",
  )
  assert beyond_end.status_code == 200
  assert beyond_end.json()["items"] == []
  assert beyond_end.json()["total"] == 2
  assert beyond_end.json()["summary"]["duration_us"] == 4_000_000


def test_catalog_metadata_and_dashboard_aggregates_are_global(
  admin_client,
  settings,
) -> None:
  database = admin_client.app.state.database
  now = isoformat()
  ready_drive_id = "drive-ready"
  source_id = "source-road"
  derived_id = "derived-road"
  poster_id = "poster-road"
  thumbnail_id = "thumbnail-not-ready"
  paths = {
    "raw": b"raw",
    "rlog": b"log",
    "derived": b"av1!",
    "poster": b"poster",
    "thumbnail": b"thumb",
  }
  for name, content in paths.items():
    path = settings.archive_root / "objects" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

  with database.transaction(immediate=True) as connection:
    digests = {name: _object(connection, content, name) for name, content in paths.items()}
    connection.execute(
      """
      INSERT INTO drives(
        id, device_id, route_name, started_at, duration_us,
        telemetry_ready, created_at
      ) VALUES (
        ?, 'device-one', 'ready-route', '2026-07-28T12:00:00Z',
        12_000_000, 1, ?
      )
      """,
      (ready_drive_id, now),
    )
    connection.execute(
      """
      INSERT INTO drives(
        id, device_id, route_name, started_at, duration_us, created_at
      ) VALUES (
        'drive-newest-importing', 'device-one', 'newest-importing',
        '2026-07-29T12:00:00Z', 5_000_000, ?
      )
      """,
      (now,),
    )
    connection.execute(
      """
      INSERT INTO segments(id, drive_id, number, created_at)
      VALUES ('segment-ready', ?, 0, ?)
      """,
      (ready_drive_id, now),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, camera, relative_path, storage_path, size,
        status, created_at
      ) VALUES (
        ?, 'device-one', ?, 'segment-ready', ?,
        'fcamera', 'road', 'ready-route/0/fcamera.hevc',
        'objects/raw', 3, 'stored', ?
      )
      """,
      (source_id, ready_drive_id, digests["raw"], now),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, relative_path, storage_path, size, status, created_at
      ) VALUES (
        'source-rlog', 'device-one', ?, 'segment-ready', ?,
        'rlog', 'ready-route/0/rlog', 'objects/rlog', 3, 'stored', ?
      )
      """,
      (ready_drive_id, digests["rlog"], now),
    )
    rlog_source_fingerprint = _complete_inventory(
      connection,
      drive_id=ready_drive_id,
      route_name="ready-route",
      video_path="ready-route/0/fcamera.hevc",
      video_sha256=digests["raw"],
      video_size=3,
      rlog_path="ready-route/0/rlog",
      rlog_sha256=digests["rlog"],
      rlog_size=3,
      now=now,
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, camera, relative_path, storage_path, size,
        mime_type, codec, width, height, fps, status,
        source_artifact_id, created_at
      ) VALUES (
        ?, 'device-one', ?, 'segment-ready', ?,
        'derived_video', 'road', 'derived/ready/road.webm',
        'objects/derived', 4, 'video/webm', 'av1',
        1928, 1208, 20, 'ready', ?, ?
      )
      """,
      (
        derived_id,
        ready_drive_id,
        digests["derived"],
        source_id,
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, camera, relative_path, storage_path, size,
        mime_type, status, source_artifact_id, created_at
      ) VALUES (
        ?, 'device-one', ?, 'segment-ready', ?,
        'poster', 'road', 'derived/ready/road.poster.jpg',
        'objects/poster', 6, 'image/jpeg', 'ready', ?, ?
      )
      """,
      (
        poster_id,
        ready_drive_id,
        digests["poster"],
        derived_id,
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256,
        kind, camera, relative_path, storage_path, size,
        mime_type, status, source_artifact_id, created_at
      ) VALUES (
        ?, 'device-one', ?, 'segment-ready', ?,
        'thumbnail', 'road', 'derived/ready/road.thumb.jpg',
        'objects/thumbnail', 5, 'image/jpeg', 'stored', ?, ?
      )
      """,
      (
        thumbnail_id,
        ready_drive_id,
        digests["thumbnail"],
        derived_id,
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO telemetry_indexes(
        drive_id, schema_version, state, ndjson_path, ndjson_sha256,
        manifest_json, signal_catalog_json, source_fingerprint,
        created_at, updated_at
      ) VALUES (?, 1, 'complete', 'telemetry/ready.ndjson', ?, ?, '[]', ?, ?, ?)
      """,
      (
        ready_drive_id,
        "a" * 64,
        json.dumps(
          {
            "state": "complete",
            "publication_ready": True,
            "vehicle": {
              "car_fingerprint": "HYUNDAI_IONIQ_5",
            },
            "route_summary": {
              "distance_m": 1234.567,
              "location_start": "59.9101234,10.7509877",
              "location_end": "59.9200000,10.7600001",
              "provenance": {
                "distance_method": ("trapezoidal_absolute_vEgo_monotonic_dt_le_250ms"),
                "location_method": ("first_last_valid_gps_fix_lat_lon_decimal_degrees_7"),
                "included_interval_count": 120,
                "excluded_interval_count": 2,
              },
            },
          }
        ),
        rlog_source_fingerprint,
        now,
        now,
      ),
    )

  first_page = admin_client.get("/api/v1/drives?limit=1")
  assert first_page.status_code == 200, first_page.text
  page = first_page.json()
  assert page["total"] == 2
  assert page["items"][0]["route_name"] == "newest-importing"
  assert page["summary"]["by_readiness"]["ready"] == 1
  assert page["summary"]["by_readiness"]["importing"] == 1
  assert page["summary"]["duration_us"] == 17_000_000
  assert page["summary"]["stored_bytes"] == 21

  ready_response = admin_client.get(
    "/api/v1/drives?readiness=ready&limit=1",
  )
  assert ready_response.status_code == 200, ready_response.text
  ready = ready_response.json()["items"][0]
  assert ready["vehicle"] == "HYUNDAI_IONIQ_5"
  assert ready["distance_m"] == 1234.567
  assert ready["location_start"] == "59.9101234,10.7509877"
  assert ready["location_end"] == "59.9200000,10.7600001"
  assert ready["raw_bytes"] == 6
  assert ready["derived_bytes"] == 15
  assert ready["stored_bytes"] == 21
  assert ready["cameras"] == [
    {
      "id": "road",
      "label": "Road",
      "available": True,
      "codec": "av1",
      "width": 1928,
      "height": 1208,
      "fps": 20.0,
    }
  ]
  assert ready["poster_url"] == (f"/api/v1/artifacts/{poster_id}/content")
  assert ready["thumbnail_url"] is None
  poster = admin_client.get(ready["poster_url"])
  assert poster.status_code == 200
  assert poster.content == paths["poster"]

  dashboard = admin_client.get("/api/v1/dashboard")
  assert dashboard.status_code == 200, dashboard.text
  snapshot = dashboard.json()
  assert snapshot["drives_total"] == 2
  assert snapshot["drives_ready"] == 1
  assert snapshot["drives_by_readiness"]["ready"] == 1
  assert snapshot["drives_by_readiness"]["importing"] == 1
  assert snapshot["raw_bytes"] == 6
  assert snapshot["derived_bytes"] == 15
  assert snapshot["storage_cataloged_bytes"] == 21
  assert snapshot["storage_capacity_bytes"] > 0
  assert snapshot["storage_used_bytes"] >= 0
  assert snapshot["storage_free_bytes"] >= 0
