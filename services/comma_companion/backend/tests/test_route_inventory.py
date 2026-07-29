from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from fastapi.testclient import TestClient

from comma_companion.app import create_app
from comma_companion.inventory import MAX_INVENTORY_RECORDS_PER_DEVICE
from conftest import DEVICE_ID, DEVICE_TOKEN, IMPORT_TOKEN


ROUTE_NAME = "routeabc"


def _file(
  relative_path: str,
  artifact_type: str,
  sha_character: str,
  *,
  camera: str | None = None,
  size: int = 10,
) -> dict[str, Any]:
  return {
    "artifact_type": artifact_type,
    "camera": camera,
    "mtime_ns": 1_774_358_400_000_000_000,
    "relative_path": relative_path,
    "sha256": sha_character * 64,
    "size": size,
  }


def _stream(
  role: str,
  *,
  file: dict[str, Any] | None,
) -> dict[str, Any]:
  if file is None:
    return {
      "mtime_ns": None,
      "relative_path": None,
      "role": role,
      "sha256": None,
      "size": None,
      "status": "missing",
    }
  return {
    "mtime_ns": file["mtime_ns"],
    "relative_path": file["relative_path"],
    "role": role,
    "sha256": file["sha256"],
    "size": file["size"],
    "status": "present",
  }


def _manifest(
  *,
  missing_driver: bool = False,
  generation: int = 1,
  previous: str | None = None,
) -> dict[str, Any]:
  road = _file(
    f"realdata/{ROUTE_NAME}--0/fcamera.hevc",
    "video",
    "a",
    camera="road",
  )
  rlog = _file(
    f"realdata/{ROUTE_NAME}--0/rlog",
    "rlog",
    "b",
    size=20,
  )
  expected = [
    {
      "artifact_type": "rlog",
      "camera": None,
      "role": "realdata|rlog|-",
      "root_name": "realdata",
    },
  ]
  streams = [
    _stream("realdata|rlog|-", file=rlog),
  ]
  if missing_driver:
    expected.extend(
      [
        {
          "artifact_type": "video",
          "camera": "driver",
          "role": "realdata|video|driver",
          "root_name": "realdata",
        },
      ],
    )
    streams.extend(
      [
        _stream("realdata|video|driver", file=None),
      ],
    )
  expected.extend(
    [
      {
        "artifact_type": "video",
        "camera": "road",
        "role": "realdata|video|road",
        "root_name": "realdata",
      },
    ],
  )
  streams.extend(
    [
      _stream("realdata|video|road", file=road),
    ],
  )
  evidence = (
    [
      "missing_expected_streams",
      "no_lock",
      "offroad",
      "stable_duration",
    ]
    if missing_driver
    else [
      "no_lock",
      "offroad",
      "stable_duration",
    ]
  )
  return {
    "capability_source": "configured+route_union",
    "closed_at": "2026-07-29T01:02:03.123456789Z",
    "closure_evidence": evidence,
    "expected_streams": expected,
    "generation": generation,
    "missing_segment_numbers": [],
    "previous_manifest_sha256": previous,
    "root_names": ["realdata"],
    "route_closed": True,
    "route_files": [],
    "route_name": ROUTE_NAME,
    "schema": "comma-companion.route-inventory",
    "schema_version": 1,
    "segments": [
      {
        "files": [road, rlog],
        "number": 0,
        "streams": streams,
      },
    ],
    "state": "partial" if missing_driver else "complete",
  }


def _digest(manifest: dict[str, Any]) -> str:
  canonical = json.dumps(
    manifest,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
  ).encode()
  return hashlib.sha256(canonical).hexdigest()


def _declare(
  client: TestClient,
  manifest: dict[str, Any],
  *,
  token: str = DEVICE_TOKEN,
  device_id: str | None = None,
  digest: str | None = None,
) -> Any:
  manifest_sha256 = digest or _digest(manifest)
  body: dict[str, Any] = {
    "manifest_sha256": manifest_sha256,
    "manifest": manifest,
  }
  if device_id is not None:
    body["device_id"] = device_id
  return client.post(
    "/api/v1/route-inventories",
    headers={
      "Authorization": f"Bearer {token}",
      "Idempotency-Key": f"route-inventory:{manifest_sha256}",
    },
    json=body,
  )


def test_inventory_acceptance_replay_and_absent_camera_contract(
  admin_client: TestClient,
) -> None:
  manifest = _manifest(missing_driver=True)
  accepted = _declare(admin_client, manifest)
  assert accepted.status_code == 201, accepted.text
  assert accepted.json() == {
    "manifest_sha256": _digest(manifest),
    "generation": 1,
    "state": "accepted",
  }

  replay = _declare(admin_client, manifest)
  assert replay.status_code == 200
  assert replay.json() == accepted.json()

  database = admin_client.app.state.database
  drive = database.query_one(
    "SELECT id FROM drives WHERE device_id = ? AND route_name = ?",
    (DEVICE_ID, ROUTE_NAME),
  )
  assert drive is not None
  detail = admin_client.get(f"/api/v1/drives/{drive['id']}")
  assert detail.status_code == 200, detail.text
  payload = detail.json()
  assert payload["readiness"] == "partial"
  assert payload["route_inventory"]["state"] == "partial"
  assert payload["route_inventory"]["capability_source"] == "configured+route_union"
  assert "missing_expected_streams" in (payload["route_inventory"]["closure_evidence"])
  assert payload["route_inventory"]["missing_file_count"] == 3
  assert payload["cameras"] == [
    {
      "id": "driver",
      "label": "Driver",
      "available": False,
      "codec": None,
      "width": None,
      "height": None,
      "fps": None,
    },
    {
      "id": "road",
      "label": "Road",
      "available": False,
      "codec": None,
      "width": None,
      "height": None,
      "fps": None,
    },
  ]
  assert payload["segments"][0]["started_at"] is None
  assert [
    (
      item["role"],
      item["manifest_status"],
      item["archive_status"],
    )
    for item in payload["segments"][0]["expected_streams"]
  ] == [
    ("realdata|rlog|-", "present", "missing"),
    ("realdata|video|driver", "missing", "missing"),
    ("realdata|video|road", "present", "missing"),
  ]


def test_inventory_generation_chain_can_regress_readiness(
  admin_client: TestClient,
) -> None:
  first = _manifest()
  first_digest = _digest(first)
  assert _declare(admin_client, first).status_code == 201

  second = _manifest(
    missing_driver=True,
    generation=2,
    previous=first_digest,
  )
  second_response = _declare(admin_client, second)
  assert second_response.status_code == 201, second_response.text

  fork = copy.deepcopy(second)
  fork["closed_at"] = "2026-07-29T01:02:04Z"
  conflict = _declare(admin_client, fork)
  assert conflict.status_code == 409
  assert conflict.json()["error"]["code"] == "route_inventory_generation_conflict"

  drive = admin_client.app.state.database.query_one(
    "SELECT id FROM drives WHERE device_id = ? AND route_name = ?",
    (DEVICE_ID, ROUTE_NAME),
  )
  assert drive is not None
  detail = admin_client.get(f"/api/v1/drives/{drive['id']}").json()
  assert detail["route_inventory"]["generation"] == 2
  assert detail["readiness"] == "partial"


def test_superseding_rlog_generation_invalidates_old_telemetry(
  admin_client: TestClient,
) -> None:
  first = _manifest()
  first_digest = _digest(first)
  assert _declare(admin_client, first).status_code == 201
  database = admin_client.app.state.database
  inventory = database.query_one(
    """
    SELECT drive_id, rlog_source_fingerprint
    FROM route_inventories
    WHERE device_id = ? AND route_name = ?
    """,
    (DEVICE_ID, ROUTE_NAME),
  )
  assert inventory is not None
  now = "2026-07-29T01:03:00Z"
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO telemetry_indexes(
        drive_id, schema_version, state, ndjson_path, ndjson_sha256,
        manifest_json, signal_catalog_json, source_fingerprint,
        created_at, updated_at
      ) VALUES (?, 1, 'complete', 'telemetry/old.ndjson', ?, '{}', '[]', ?, ?, ?)
      """,
      (
        inventory["drive_id"],
        "d" * 64,
        inventory["rlog_source_fingerprint"],
        now,
        now,
      ),
    )
    connection.execute(
      "UPDATE drives SET telemetry_ready = 1 WHERE id = ?",
      (inventory["drive_id"],),
    )

  second = _manifest(generation=2, previous=first_digest)
  rlog_file = second["segments"][0]["files"][1]
  rlog_stream = second["segments"][0]["streams"][0]
  rlog_file["sha256"] = "c" * 64
  rlog_stream["sha256"] = rlog_file["sha256"]
  superseded = _declare(admin_client, second)
  assert superseded.status_code == 201, superseded.text

  state = database.query_one(
    """
    SELECT d.telemetry_ready, t.source_fingerprint,
      latest.rlog_source_fingerprint
    FROM drives d
    JOIN telemetry_indexes t ON t.drive_id = d.id
    JOIN route_inventories latest ON latest.drive_id = d.id
    WHERE d.id = ? AND latest.generation = 2
    """,
    (inventory["drive_id"],),
  )
  assert state is not None
  assert state["telemetry_ready"] == 0
  assert state["source_fingerprint"] != state["rlog_source_fingerprint"]


def test_matching_inventory_releases_preindexed_telemetry(
  admin_client: TestClient,
) -> None:
  first = _manifest()
  first_digest = _digest(first)
  assert _declare(admin_client, first).status_code == 201
  second = _manifest(generation=2, previous=first_digest)
  rlog_file = second["segments"][0]["files"][1]
  rlog_stream = second["segments"][0]["streams"][0]
  rlog_file["sha256"] = "c" * 64
  rlog_stream["sha256"] = rlog_file["sha256"]
  source_fingerprint = hashlib.sha256(
    json.dumps(
      [{"segment_number": 0, "sha256": rlog_file["sha256"]}],
      separators=(",", ":"),
      sort_keys=True,
    ).encode(),
  ).hexdigest()
  database = admin_client.app.state.database
  drive = database.query_one(
    "SELECT id FROM drives WHERE device_id = ? AND route_name = ?",
    (DEVICE_ID, ROUTE_NAME),
  )
  assert drive is not None
  now = "2026-07-29T01:03:00Z"
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO telemetry_indexes(
        drive_id, schema_version, state, ndjson_path, ndjson_sha256,
        manifest_json, signal_catalog_json, source_fingerprint,
        created_at, updated_at
      ) VALUES (
        ?, 1, 'complete', 'telemetry/new.ndjson', ?,
        ?, '[]', ?, ?, ?
      )
      """,
      (
        drive["id"],
        "d" * 64,
        json.dumps(
          {
            "publication_ready": True,
            "state": "complete",
          }
        ),
        source_fingerprint,
        now,
        now,
      ),
    )

  accepted = _declare(admin_client, second)
  assert accepted.status_code == 201, accepted.text
  state = database.query_one(
    """
    SELECT d.telemetry_ready, telemetry.source_fingerprint,
      inventory.rlog_source_fingerprint
    FROM drives d
    JOIN telemetry_indexes telemetry ON telemetry.drive_id = d.id
    JOIN route_inventories inventory ON inventory.drive_id = d.id
    WHERE d.id = ? AND inventory.generation = 2
    """,
    (drive["id"],),
  )
  assert state is not None
  assert state["telemetry_ready"] == 1
  assert state["source_fingerprint"] == source_fingerprint
  assert state["rlog_source_fingerprint"] == source_fingerprint


def test_inventory_rejects_digest_traversal_and_invalid_idempotency(
  admin_client: TestClient,
) -> None:
  manifest = _manifest()
  bad_digest = _declare(admin_client, manifest, digest="f" * 64)
  assert bad_digest.status_code == 422
  assert bad_digest.json()["error"]["code"] == "manifest_digest_mismatch"

  traversal = _manifest()
  traversal["segments"][0]["files"][0]["relative_path"] = "realdata/../escape"
  traversal["segments"][0]["streams"][-1]["relative_path"] = "realdata/../escape"
  rejected = _declare(admin_client, traversal)
  assert rejected.status_code == 422

  manifest_sha256 = _digest(manifest)
  wrong_key = admin_client.post(
    "/api/v1/route-inventories",
    headers={
      "Authorization": f"Bearer {DEVICE_TOKEN}",
      "Idempotency-Key": "route-inventory:" + "0" * 64,
    },
    json={
      "manifest_sha256": manifest_sha256,
      "manifest": manifest,
    },
  )
  assert wrong_key.status_code == 422
  assert wrong_key.json()["error"]["code"] == "invalid_idempotency_key"


def test_multiple_active_log_roots_are_accepted_only_as_partial(
  admin_client: TestClient,
) -> None:
  manifest = _manifest()
  alternate_rlog = _file(
    f"konik/{ROUTE_NAME}--0/rlog",
    "rlog",
    "c",
    size=20,
  )
  manifest["root_names"] = ["konik", "realdata"]
  manifest["expected_streams"].insert(
    0,
    {
      "artifact_type": "rlog",
      "camera": None,
      "role": "konik|rlog|-",
      "root_name": "konik",
    },
  )
  manifest["segments"][0]["files"].insert(0, alternate_rlog)
  manifest["segments"][0]["streams"].insert(
    0,
    _stream("konik|rlog|-", file=alternate_rlog),
  )
  manifest["state"] = "partial"
  manifest["closure_evidence"] = [
    "multiple_active_log_roots",
    "no_lock",
    "offroad",
    "stable_duration",
  ]

  accepted = _declare(admin_client, manifest)
  assert accepted.status_code == 201, accepted.text
  drive = admin_client.app.state.database.query_one(
    "SELECT id FROM drives WHERE device_id = ? AND route_name = ?",
    (DEVICE_ID, ROUTE_NAME),
  )
  assert drive is not None
  detail = admin_client.get(f"/api/v1/drives/{drive['id']}")
  assert detail.status_code == 200
  assert detail.json()["route_inventory"]["state"] == "partial"
  assert "multiple_active_log_roots" in (detail.json()["route_inventory"]["closure_evidence"])

  invalid_complete = copy.deepcopy(manifest)
  invalid_complete["state"] = "complete"
  invalid_complete["route_name"] = "different-route"
  for item in invalid_complete["segments"][0]["files"]:
    item["relative_path"] = item["relative_path"].replace(
      ROUTE_NAME,
      "different-route",
    )
  for stream in invalid_complete["segments"][0]["streams"]:
    if stream["relative_path"] is not None:
      stream["relative_path"] = stream["relative_path"].replace(
        ROUTE_NAME,
        "different-route",
      )
  rejected = _declare(admin_client, invalid_complete)
  assert rejected.status_code == 422


def test_missing_segment_is_materialized_without_amplified_manifest_rows(
  admin_client: TestClient,
) -> None:
  manifest = _manifest()
  manifest["state"] = "partial"
  manifest["closure_evidence"] = [
    "missing_segment_numbers",
    "no_lock",
    "offroad",
    "stable_duration",
  ]
  manifest["missing_segment_numbers"] = [0]
  segment = manifest["segments"][0]
  segment["number"] = 1
  for file in segment["files"]:
    file["relative_path"] = file["relative_path"].replace("--0/", "--1/")
  for stream in segment["streams"]:
    stream["relative_path"] = stream["relative_path"].replace(
      "--0/",
      "--1/",
    )

  accepted = _declare(admin_client, manifest)
  assert accepted.status_code == 201, accepted.text
  drive = admin_client.app.state.database.query_one(
    "SELECT id FROM drives WHERE device_id = ? AND route_name = ?",
    (DEVICE_ID, ROUTE_NAME),
  )
  assert drive is not None
  detail = admin_client.get(f"/api/v1/drives/{drive['id']}").json()
  assert [segment["number"] for segment in detail["segments"]] == [0, 1]
  assert detail["segments"][0]["started_at"] is None
  assert {item["manifest_status"] for item in detail["segments"][0]["expected_streams"]} == {"missing"}
  assert detail["route_inventory"]["missing_segment_numbers"] == [0]
  assert detail["route_inventory"]["missing_file_count"] == 4
  inventory = admin_client.app.state.database.query_one(
    """
    SELECT manifest_size, materialized_row_count
    FROM route_inventories
    WHERE device_id = ? AND route_name = ?
    """,
    (DEVICE_ID, ROUTE_NAME),
  )
  usage = admin_client.app.state.database.query_one(
    """
    SELECT route_count, inventory_count, manifest_bytes, materialized_rows
    FROM route_inventory_usage
    WHERE device_id = ?
    """,
    (DEVICE_ID,),
  )
  assert inventory is not None
  assert usage is not None
  assert inventory["manifest_size"] == len(
    json.dumps(
      manifest,
      allow_nan=False,
      ensure_ascii=False,
      separators=(",", ":"),
      sort_keys=True,
    ).encode(),
  )
  assert inventory["materialized_row_count"] == 6
  assert dict(usage) == {
    "route_count": 1,
    "inventory_count": 1,
    "manifest_bytes": inventory["manifest_size"],
    "materialized_rows": 6,
  }


def test_inventory_enforces_persisted_per_device_quota(
  admin_client: TestClient,
) -> None:
  first = _manifest()
  first_digest = _digest(first)
  assert _declare(admin_client, first).status_code == 201
  database = admin_client.app.state.database
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      UPDATE route_inventory_usage
      SET inventory_count = ?
      WHERE device_id = ?
      """,
      (MAX_INVENTORY_RECORDS_PER_DEVICE, DEVICE_ID),
    )

  second = _manifest(generation=2, previous=first_digest)
  rejected = _declare(admin_client, second)
  assert rejected.status_code == 507
  assert rejected.json()["error"] == {
    "code": "route_inventory_quota_exceeded",
    "message": "Route inventory storage quota is exhausted",
    "details": {"reason": "device_inventory_limit"},
  }
  generations = database.query_one(
    """
    SELECT COUNT(*) AS count
    FROM route_inventories
    WHERE device_id = ? AND route_name = ?
    """,
    (DEVICE_ID, ROUTE_NAME),
  )
  assert generations is not None
  assert generations["count"] == 1


def test_inventory_uses_the_same_typed_artifact_caps_as_uploads(
  admin_client: TestClient,
) -> None:
  manifest = _manifest()
  video_file = manifest["segments"][0]["files"][0]
  video_stream = manifest["segments"][0]["streams"][-1]
  video_file["size"] = 2 * 1024 * 1024
  video_stream["size"] = video_file["size"]
  rejected = _declare(admin_client, manifest)
  assert rejected.status_code == 413
  assert rejected.json()["error"]["code"] == "artifact_too_large"


def test_inventory_caps_implicit_missing_segment_view_expansion(
  admin_client: TestClient,
) -> None:
  roles = [
    {
      "artifact_type": "video",
      "camera": f"camera{index:02d}",
      "role": f"realdata|video|camera{index:02d}",
      "root_name": "realdata",
    }
    for index in range(64)
  ]
  manifest = {
    "capability_source": "configured+route_union",
    "closed_at": "2026-07-29T01:02:03Z",
    "closure_evidence": [
      "missing_expected_streams",
      "missing_segment_numbers",
      "no_lock",
      "offroad",
      "stable_duration",
    ],
    "expected_streams": roles,
    "generation": 1,
    "missing_segment_numbers": list(range(4096)),
    "previous_manifest_sha256": None,
    "root_names": ["realdata"],
    "route_closed": True,
    "route_files": [],
    "route_name": "large-gap-route",
    "schema": "comma-companion.route-inventory",
    "schema_version": 1,
    "segments": [
      {
        "files": [],
        "number": 4096,
        "streams": [_stream(role["role"], file=None) for role in roles],
      },
    ],
    "state": "partial",
  }

  rejected = _declare(admin_client, manifest)
  assert rejected.status_code == 413
  assert rejected.json()["error"] == {
    "code": "route_inventory_too_large",
    "message": "Route inventory expands beyond the server row limit",
    "details": {
      "rows": 270_402,
      "maximum_rows": 20_000,
    },
  }


def test_loopback_importer_can_declare_inventory_for_enrolled_device(
  settings,
) -> None:
  with TestClient(
    create_app(settings),
    base_url="https://comma.test",
    client=("127.0.0.1", 43000),
  ) as importer:
    response = _declare(
      importer,
      _manifest(),
      token=IMPORT_TOKEN,
      device_id=DEVICE_ID,
    )
    latest = importer.get(
      "/api/v1/route-inventories/latest",
      headers={"Authorization": f"Bearer {IMPORT_TOKEN}"},
      params={"device_id": DEVICE_ID, "route_name": ROUTE_NAME},
    )
  assert response.status_code == 201, response.text
  assert latest.status_code == 200, latest.text
  assert latest.json()["manifest_sha256"] == response.json()["manifest_sha256"]
  assert latest.json()["manifest"] == _manifest()
