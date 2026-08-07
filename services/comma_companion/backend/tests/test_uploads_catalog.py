from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from comma_companion.app import create_app
from comma_companion.db import isoformat
from comma_companion.integrations import telemetry_source_fingerprint
from comma_companion.jobs import (
  cancel_job,
  claim_job,
  fail_job,
  request_cancellation,
)
from comma_companion.models import UploadCreate
from conftest import (
  DEVICE_ID,
  IMPORT_TOKEN,
  device_headers,
)


def _checksum(data: bytes) -> str:
  return "sha256 " + base64.b64encode(hashlib.sha256(data).digest()).decode()


def _stream_request(
  app,
  receive,
  *,
  headers: dict[str, str] | None = None,
) -> Request:
  return Request(
    {
      "type": "http",
      "asgi": {"version": "3.0", "spec_version": "2.3"},
      "http_version": "1.1",
      "method": "PATCH",
      "scheme": "https",
      "path": "/api/v1/uploads/test",
      "raw_path": b"/api/v1/uploads/test",
      "query_string": b"",
      "headers": [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()],
      "client": ("127.0.0.1", 12345),
      "server": ("comma.test", 443),
      "app": app,
    },
    receive,
  )


def _declaration(
  data: bytes,
  *,
  relative_path: str = "00000123--abcdef/0/fcamera.hevc",
  sha256: str | None = None,
) -> dict[str, object]:
  return {
    "device_id": DEVICE_ID,
    "route_name": "00000123--abcdef",
    "segment_number": 0,
    "artifact_type": "fcamera",
    "camera": "road",
    "relative_path": relative_path,
    "size": len(data),
    "mtime_ns": 1_774_358_400_000_000_000,
    "sha256": sha256 or hashlib.sha256(data).hexdigest(),
    "completion_evidence": ["no_lock", "offroad_grace"],
    "partial": False,
  }


def _declare_pending_upload(
  admin_client: TestClient,
  *,
  idempotency_key: str,
  relative_path: str,
  route_name: str,
  artifact_type: str,
  camera: str | None,
) -> dict[str, object]:
  data = idempotency_key.encode()
  payload = _declaration(data, relative_path=relative_path)
  payload.update(
    {
      "route_name": route_name,
      "artifact_type": artifact_type,
      "camera": camera,
    },
  )
  response = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key=idempotency_key),
    json=payload,
  )
  assert response.status_code == 201, response.text
  return response.json()


def _complete_log_upload(
  admin_client: TestClient,
  *,
  route_name: str,
  segment_number: int,
  data: bytes,
  idempotency_key: str,
  filename: str = "rlog.zst",
) -> dict[str, object]:
  declaration = _declaration(
    data,
    relative_path=f"{route_name}/{segment_number}/{filename}",
  )
  declaration.update(
    {
      "route_name": route_name,
      "segment_number": segment_number,
      "artifact_type": "rlog",
      "camera": None,
    },
  )
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key=idempotency_key),
    json=declaration,
  )
  assert created.status_code == 201, created.text
  completed = admin_client.patch(
    f"/api/v1/uploads/{created.json()['id']}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(data),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data,
  )
  assert completed.status_code == 200, completed.text
  return completed.json()


def test_upload_search_matches_catalog_fields_case_insensitively(
  admin_client: TestClient,
) -> None:
  relative_path = _declare_pending_upload(
    admin_client,
    idempotency_key="search-relative-path",
    relative_path="alpha-folder/0/FrontClip.HEVC",
    route_name="route-alpha",
    artifact_type="fcamera",
    camera="road",
  )
  route = _declare_pending_upload(
    admin_client,
    idempotency_key="search-route",
    relative_path="bravo/0/data.bin",
    route_name="RouteBeacon",
    artifact_type="other",
    camera=None,
  )
  camera = _declare_pending_upload(
    admin_client,
    idempotency_key="search-camera",
    relative_path="charlie/0/data.bin",
    route_name="route-charlie",
    artifact_type="video",
    camera="CabinCam",
  )
  artifact_type = _declare_pending_upload(
    admin_client,
    idempotency_key="search-artifact-type",
    relative_path="delta/0/data.bin",
    route_name="route-delta",
    artifact_type="QLOG",
    camera=None,
  )

  expectations = (
    ("  FRONTCLIP.hevc  ", {relative_path["id"]}),
    ("ALPHA-FOLDER/0", {relative_path["id"]}),
    ("routebeacon", {route["id"]}),
    ("cabincam", {camera["id"]}),
    ("qlog", {artifact_type["id"]}),
    (
      "DEVICE-ONE",
      {
        relative_path["id"],
        route["id"],
        camera["id"],
        artifact_type["id"],
      },
    ),
  )
  for query, expected_ids in expectations:
    response = admin_client.get(
      "/api/v1/uploads",
      params={"q": query},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(expected_ids)
    assert {item["id"] for item in body["items"]} == expected_ids


def test_upload_search_combines_filters_and_counts_before_paging(
  admin_client: TestClient,
) -> None:
  matching = [
    _declare_pending_upload(
      admin_client,
      idempotency_key=f"search-page-{index}",
      relative_path=f"PageNeedle/{index}/data.bin",
      route_name=f"route-page-{index}",
      artifact_type="other",
      camera=None,
    )
    for index in range(3)
  ]
  nonmatching = _declare_pending_upload(
    admin_client,
    idempotency_key="search-page-unrelated",
    relative_path="unrelated/0/data.bin",
    route_name="route-unrelated",
    artifact_type="other",
    camera=None,
  )
  database = admin_client.app.state.database
  for upload in (*matching[:2], nonmatching):
    database.execute(
      "UPDATE uploads SET status = 'failed' WHERE id = ?",
      (upload["id"],),
    )

  response = admin_client.get(
    "/api/v1/uploads",
    params={
      "q": "  pAgEnEeDlE  ",
      "state": "failed",
      "device_id": DEVICE_ID,
      "limit": 1,
      "offset": 1,
    },
  )
  assert response.status_code == 200, response.text
  body = response.json()
  assert body["total"] == 2
  assert len(body["items"]) == 1
  assert body["items"][0]["id"] in {
    matching[0]["id"],
    matching[1]["id"],
  }

  empty_page = admin_client.get(
    "/api/v1/uploads",
    params={
      "q": "pageneedle",
      "state": "failed",
      "device_id": DEVICE_ID,
      "limit": 1,
      "offset": 10,
    },
  )
  assert empty_page.status_code == 200
  assert empty_page.json()["total"] == 2
  assert empty_page.json()["items"] == []


@pytest.mark.parametrize("query", ("", "   ", "x" * 257))
def test_upload_search_rejects_invalid_q(
  admin_client: TestClient,
  query: str,
) -> None:
  response = admin_client.get(
    "/api/v1/uploads",
    params={"q": query},
  )
  assert response.status_code == 422
  assert response.json()["error"] == {
    "code": "invalid_query",
    "message": "q must contain between 1 and 256 characters",
    "details": {},
  }


def test_agent_file_id_is_persisted_exposed_and_idempotency_bound(
  admin_client: TestClient,
) -> None:
  data = b"agent-file-id"
  uppercase_file_id = "AB" * 32
  normalized_file_id = uppercase_file_id.lower()
  declaration = _declaration(
    data,
    relative_path="file-id-route/0/rlog.zst",
  )
  declaration.update({
    "artifact_type": "rlog",
    "camera": None,
    "file_id": uppercase_file_id,
  })
  headers = device_headers(idempotency_key="agent-file-id")
  created = admin_client.post(
    "/api/v1/uploads",
    headers=headers,
    json=declaration,
  )
  assert created.status_code == 201, created.text
  upload = created.json()
  assert upload["file_id"] == normalized_file_id

  replay = admin_client.post(
    "/api/v1/uploads",
    headers=headers,
    json=declaration,
  )
  assert replay.status_code == 201, replay.text
  assert replay.json()["id"] == upload["id"]
  assert replay.json()["file_id"] == normalized_file_id

  changed_selector = dict(declaration)
  changed_selector["file_id"] = "cd" * 32
  conflict = admin_client.post(
    "/api/v1/uploads",
    headers=headers,
    json=changed_selector,
  )
  assert conflict.status_code == 409
  assert conflict.json()["error"]["code"] == "idempotency_key_reused"

  persisted = admin_client.app.state.database.query_one(
    "SELECT file_id FROM uploads WHERE id = ?",
    (upload["id"],),
  )
  assert persisted is not None
  assert persisted["file_id"] == normalized_file_id
  detail = admin_client.get(f"/api/v1/uploads/{upload['id']}")
  assert detail.status_code == 200
  assert detail.json()["file_id"] == normalized_file_id
  listing = admin_client.get(
    "/api/v1/uploads",
    params={"q": normalized_file_id},
  )
  assert listing.status_code == 200
  assert listing.json()["total"] == 1
  assert listing.json()["items"][0]["file_id"] == normalized_file_id


def test_legacy_declaration_hash_replays_without_file_id(
  admin_client: TestClient,
) -> None:
  declaration = _declaration(
    b"legacy-idempotency",
    relative_path="legacy-file-id/0/rlog.zst",
  )
  declaration.update({
    "artifact_type": "rlog",
    "camera": None,
  })
  validated = UploadCreate.model_validate(declaration)
  legacy_payload = validated.model_dump(mode="json")
  del legacy_payload["file_id"]
  legacy_hash = hashlib.sha256(
    json.dumps(
      {
        "operation": "upload.create",
        **legacy_payload,
        "device_id": DEVICE_ID,
      },
      separators=(",", ":"),
      sort_keys=True,
    ).encode(),
  ).hexdigest()
  upload_id = "legacy-file-id-replay"
  idempotency_key = "legacy-file-id-replay"
  now = isoformat()
  database = admin_client.app.state.database
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO idempotency_keys(
        actor_type, actor_id, key, request_hash, created_at
      ) VALUES ('device', ?, ?, ?, ?)
      """,
      (DEVICE_ID, idempotency_key, legacy_hash, now),
    )
    connection.execute(
      """
      INSERT INTO uploads(
        id, device_id, idempotency_key, relative_path, route_name,
        segment_number, artifact_type, camera,
        completion_evidence_json, partial,
        declared_size, declared_mtime_ns, declared_sha256,
        status, part_path, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?,
        'receiving', ?, ?, ?)
      """,
      (
        upload_id,
        DEVICE_ID,
        idempotency_key,
        validated.relative_path,
        validated.route_name,
        validated.segment_number,
        validated.artifact_type,
        validated.camera,
        json.dumps(validated.completion_evidence, separators=(",", ":")),
        validated.size,
        validated.mtime_ns,
        validated.sha256,
        f"uploads/{upload_id}.part",
        now,
        now,
      ),
    )

  replay = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key=idempotency_key),
    json=declaration,
  )
  assert replay.status_code == 201, replay.text
  assert replay.json()["id"] == upload_id
  assert replay.json()["file_id"] is None


@pytest.mark.parametrize(
  "file_id",
  (
    "",
    "a" * 63,
    "a" * 65,
    "g" * 64,
    ("a" * 32) + "-" + ("b" * 31),
  ),
)
def test_agent_file_id_rejects_non_sha256_identifiers(
  admin_client: TestClient,
  file_id: str,
) -> None:
  declaration = _declaration(b"invalid-file-id")
  declaration["file_id"] = file_id
  response = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(
      idempotency_key=f"invalid-file-id-{hashlib.sha256(file_id.encode()).hexdigest()}",
    ),
    json=declaration,
  )
  assert response.status_code == 422
  assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
  "evidence",
  ("final_segment_grace", "non_segment_grace"),
)
def test_agent_completion_grace_evidence_is_accepted(
  admin_client: TestClient,
  evidence: str,
) -> None:
  declaration = _declaration(
    evidence.encode(),
    relative_path=f"completion-evidence/0/{evidence}.bin",
  )
  declaration["completion_evidence"] = ["no_lock", "stable_duration", evidence]
  response = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key=f"completion-evidence-{evidence}"),
    json=declaration,
  )
  assert response.status_code == 201, response.text
  assert response.json()["completion_evidence"] == declaration["completion_evidence"]


def test_upload_snapshot_pending_bytes_counts_only_remaining_data(
  admin_client: TestClient,
) -> None:
  partial_data = b"0123456789"
  partial = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="snapshot-partial"),
    json=_declaration(
      partial_data,
      relative_path="snapshot-route/0/partial.bin",
    ),
  )
  assert partial.status_code == 201, partial.text
  first_chunk = partial_data[:4]
  patched = admin_client.patch(
    f"/api/v1/uploads/{partial.json()['id']}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(first_chunk),
      "Content-Type": "application/offset+octet-stream",
    },
    content=first_chunk,
  )
  assert patched.status_code == 200, patched.text
  assert patched.json()["offset"] == len(first_chunk)

  untouched_data = b"abcde"
  untouched = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="snapshot-untouched"),
    json=_declaration(
      untouched_data,
      relative_path="snapshot-route/0/untouched.bin",
    ),
  )
  assert untouched.status_code == 201, untouched.text

  response = admin_client.get("/api/v1/uploads/snapshot")
  assert response.status_code == 200, response.text
  snapshot = response.json()
  expected_remaining = len(partial_data) - len(first_chunk) + len(untouched_data)
  assert snapshot["active_uploads"] == 2
  assert snapshot["bytes_received"] == len(first_chunk)
  assert snapshot["bytes_expected"] == (len(partial_data) + len(untouched_data))
  assert snapshot["pending_bytes"] == expected_remaining
  assert snapshot["by_device"][DEVICE_ID] == {
    "active_uploads": 2,
    "bytes_received": len(first_chunk),
    "pending_bytes": expected_remaining,
  }


def test_resumable_upload_idempotency_finalization_and_catalog(
  admin_client: TestClient,
  settings,
) -> None:
  data = b"abcdef"
  declared = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="upload-one"),
    json=_declaration(data),
  )
  assert declared.status_code == 201, declared.text
  upload = declared.json()
  assert upload["id"] == upload["upload_id"]
  assert upload["offset"] == 0
  assert upload["length"] == len(data)
  assert upload["state"] == "receiving"
  assert upload["completion_evidence"] == ["no_lock", "offroad_grace"]

  replay = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="upload-one"),
    json=_declaration(data),
  )
  assert replay.status_code == 201
  assert replay.json()["upload_id"] == upload["upload_id"]

  head = admin_client.head(
    f"/api/v1/uploads/{upload['id']}",
    headers=device_headers(),
  )
  assert head.status_code == 204
  assert head.headers["upload-offset"] == "0"
  assert head.headers["upload-length"] == str(len(data))
  assert head.headers["upload-durable"] == "false"
  assert head.headers["upload-terminal"] == "false"

  first = data[:3]
  first_patch = admin_client.patch(
    f"/api/v1/uploads/{upload['id']}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Length": str(len(data)),
      "Upload-Checksum": _checksum(first),
      "Content-Type": "application/offset+octet-stream",
    },
    content=first,
  )
  assert first_patch.status_code == 200, first_patch.text
  assert first_patch.json()["offset"] == 3

  identical_retry = admin_client.patch(
    f"/api/v1/uploads/{upload['id']}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(first),
      "Content-Type": "application/offset+octet-stream",
    },
    content=first,
  )
  assert identical_retry.status_code == 200
  assert identical_retry.json()["offset"] == 3

  wrong_retry = admin_client.patch(
    f"/api/v1/uploads/{upload['id']}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(b"xyz"),
      "Content-Type": "application/offset+octet-stream",
    },
    content=b"xyz",
  )
  assert wrong_retry.status_code == 409
  assert wrong_retry.headers["upload-offset"] == "3"
  assert wrong_retry.json()["error"]["code"] == "upload_offset_mismatch"

  last = data[3:]
  complete = admin_client.patch(
    f"/api/v1/uploads/{upload['id']}",
    headers={
      **device_headers(),
      "Upload-Offset": "3",
      "Upload-Checksum": _checksum(last),
      "Content-Type": "application/offset+octet-stream",
    },
    content=last,
  )
  assert complete.status_code == 200, complete.text
  finalized = complete.json()
  assert finalized["state"] == "complete"
  assert finalized["durable"] is True
  assert finalized["sha256"] == hashlib.sha256(data).hexdigest()
  assert finalized["artifact_id"]
  completed_head = admin_client.head(
    f"/api/v1/uploads/{upload['id']}",
    headers=device_headers(),
  )
  assert completed_head.headers["upload-durable"] == "true"
  assert completed_head.headers["upload-terminal"] == "true"
  cancel_complete = admin_client.post(
    f"/api/v1/uploads/{upload['id']}/cancel",
    headers=device_headers(idempotency_key="cancel-complete"),
  )
  assert cancel_complete.status_code == 409
  assert cancel_complete.json()["error"]["code"] == "upload_complete"

  digest = hashlib.sha256(data).hexdigest()
  object_path = settings.archive_root / "objects" / "sha256" / digest[:2] / digest[2:4] / digest
  assert object_path.read_bytes() == data
  assert not (settings.uploads_root / f"{upload['id']}.part").exists()

  drive_list = admin_client.get("/api/v1/drives")
  assert drive_list.status_code == 200
  catalog = drive_list.json()
  assert catalog["total"] == 1
  assert catalog["limit"] == 100
  assert catalog["offset"] == 0
  assert len(catalog["items"]) == 1
  drive = catalog["items"][0]
  assert drive["route_name"] == "00000123--abcdef"
  assert drive["started_at"] == "2026-03-24T13:20:00Z"
  assert drive["readiness"] == "processing"

  detail = admin_client.get(f"/api/v1/drives/{drive['id']}")
  assert detail.status_code == 200
  artifact = detail.json()["segments"][0]["artifacts"][0]
  assert artifact["id"] == finalized["artifact_id"]
  assert artifact["status"] == "stored"
  assert "storage_path" not in artifact

  jobs = admin_client.app.state.database.query_all(
    "SELECT type, payload_json FROM jobs ORDER BY type",
  )
  assert [row["type"] for row in jobs] == ["transcode_video", "verify_artifact"]

  ranged = admin_client.get(
    f"/api/v1/artifacts/{artifact['id']}/content",
    headers={"Range": "bytes=1-3"},
  )
  assert ranged.status_code == 206
  assert ranged.content == b"bcd"
  assert ranged.headers["content-range"] == "bytes 1-3/6"
  assert ranged.headers["content-disposition"].startswith("attachment;")
  assert ranged.headers["content-security-policy"] == ("default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox")

  admin_client.app.state.database.execute(
    """
    UPDATE artifacts
    SET kind = 'derived_video', mime_type = 'video/webm'
    WHERE id = ?
    """,
    (artifact["id"],),
  )
  device_spoof = admin_client.get(
    f"/api/v1/artifacts/{artifact['id']}/content",
  )
  assert device_spoof.status_code == 200
  assert device_spoof.headers["content-disposition"].startswith("attachment;")

  snapshot = admin_client.get("/api/v1/uploads/snapshot")
  assert snapshot.status_code == 200
  assert snapshot.json()["completed_uploads"] == 1
  assert snapshot.json()["bytes_received"] == len(data)
  assert snapshot.json()["pending_bytes"] == 0

  actions = [item["action"] for item in admin_client.get("/api/v1/audit-events?limit=100").json()]
  assert "upload.create" in actions
  assert "upload.complete" in actions
  assert "upload.chunk" not in actions


def test_driver_camera_upload_catalogs_and_queues_av1_transcode(
  admin_client: TestClient,
) -> None:
  data = b"driver-facing-hevc"
  declaration = _declaration(
    data,
    relative_path="realdata/00000123--abcdef--0/dcamera.hevc",
  )
  declaration.update(
    {
      "artifact_type": "video",
      "camera": "driver",
    },
  )
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="driver-camera-upload"),
    json=declaration,
  )
  assert created.status_code == 201, created.text

  completed = admin_client.patch(
    f"/api/v1/uploads/{created.json()['id']}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(data),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data,
  )
  assert completed.status_code == 200, completed.text

  artifact = admin_client.app.state.database.query_one(
    """
    SELECT kind, camera, relative_path, status
    FROM artifacts
    WHERE id = ?
    """,
    (completed.json()["artifact_id"],),
  )
  assert dict(artifact) == {
    "kind": "video",
    "camera": "driver",
    "relative_path": "realdata/00000123--abcdef--0/dcamera.hevc",
    "status": "stored",
  }
  job = admin_client.app.state.database.query_one(
    """
    SELECT payload_json
    FROM jobs
    WHERE type = 'transcode_video'
    """,
  )
  assert json.loads(job["payload_json"]) == {
    "artifact_id": completed.json()["artifact_id"],
  }


def test_log_scheduler_fingerprints_only_latest_per_segment(
  admin_client: TestClient,
) -> None:
  route_name = "00000789--replacement"
  contents = (b"superseded-rlog", b"latest-rlog")

  for index, data in enumerate(contents):
    declaration = _declaration(
      data,
      relative_path=f"{route_name}/0/rlog-{index}.zst",
    )
    declaration.update({
      "route_name": route_name,
      "segment_number": 0,
      "artifact_type": "rlog",
      "camera": None,
    })
    created = admin_client.post(
      "/api/v1/uploads",
      headers=device_headers(
        idempotency_key=f"replacement-rlog-{index}",
      ),
      json=declaration,
    )
    assert created.status_code == 201, created.text
    completed = admin_client.patch(
      f"/api/v1/uploads/{created.json()['id']}",
      headers={
        **device_headers(),
        "Upload-Offset": "0",
        "Upload-Checksum": _checksum(data),
        "Content-Type": "application/offset+octet-stream",
      },
      content=data,
    )
    assert completed.status_code == 200, completed.text

  row = admin_client.app.state.database.query_one(
    """
    SELECT payload_json
    FROM jobs
    WHERE type = 'extract_telemetry'
    """,
  )
  assert row is not None
  payload = json.loads(row["payload_json"])
  latest_digest = hashlib.sha256(contents[-1]).hexdigest()
  old_digest = hashlib.sha256(contents[0]).hexdigest()
  assert payload["source_fingerprint"] == telemetry_source_fingerprint([{
    "segment_number": 0,
    "sha256": latest_digest,
  }])
  assert payload["source_fingerprint"] != telemetry_source_fingerprint([
    {
      "segment_number": 0,
      "sha256": old_digest,
    },
    {
      "segment_number": 0,
      "sha256": latest_digest,
    },
  ])


def test_new_rlog_invalidates_telemetry_and_queues_after_succeeded_extract(
  admin_client: TestClient,
) -> None:
  route_name = "00000790--telemetry-rebuild"
  first_data = b"first-complete-rlog"
  first = _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=0,
    data=first_data,
    idempotency_key="telemetry-rebuild-first",
  )
  database = admin_client.app.state.database
  drive = database.query_one(
    "SELECT id FROM drives WHERE device_id = ? AND route_name = ?",
    (DEVICE_ID, route_name),
  )
  assert drive is not None
  first_extract = database.query_one(
    """
    SELECT id
    FROM jobs
    WHERE type = 'extract_telemetry' AND dedupe_key = ?
    """,
    (f"drive:{drive['id']}",),
  )
  assert first_extract is not None
  database.execute(
    """
    UPDATE jobs
    SET state = 'succeeded', completed_at = ?, updated_at = ?
    WHERE id = ?
    """,
    (
      "2026-07-29T10:00:00Z",
      "2026-07-29T10:00:00Z",
      first_extract["id"],
    ),
  )

  source = database.query_one(
    "SELECT segment_id FROM artifacts WHERE id = ?",
    (first["artifact_id"],),
  )
  assert source is not None
  now = "2026-07-29T10:00:01Z"
  derived_id = "derived-video-before-new-rlog"
  sync_id = "media-sync-before-new-rlog"
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256, kind,
        camera, relative_path, storage_path, size, mime_type, codec,
        time_map_path, status, source_artifact_id, created_at
      ) VALUES (?, ?, ?, ?, ?, 'derived_video', 'road', ?, ?, ?,
        'video/mp4', 'av1', 'derived/road.sync.json', 'ready', ?, ?)
      """,
      (
        derived_id,
        DEVICE_ID,
        drive["id"],
        source["segment_id"],
        first["sha256"],
        f"{route_name}/0/road.av1.mp4",
        f"objects/{first['sha256']}",
        len(first_data),
        first["artifact_id"],
        now,
      ),
    )
    connection.execute(
      """
      INSERT INTO artifacts(
        id, device_id, drive_id, segment_id, object_sha256, kind,
        camera, relative_path, storage_path, size, mime_type,
        status, source_artifact_id, created_at
      ) VALUES (?, ?, ?, ?, ?, 'video_telemetry_sync', 'road', ?, ?, ?,
        'application/json', 'ready', ?, ?)
      """,
      (
        sync_id,
        DEVICE_ID,
        drive["id"],
        source["segment_id"],
        first["sha256"],
        f"{route_name}/0/road.sync.json",
        f"objects/{first['sha256']}",
        len(first_data),
        derived_id,
        now,
      ),
    )
    connection.execute(
      "UPDATE drives SET telemetry_ready = 1 WHERE id = ?",
      (drive["id"],),
    )

  second_data = b"second-complete-rlog"
  _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=1,
    data=second_data,
    idempotency_key="telemetry-rebuild-second",
  )

  drive_state = database.query_one(
    "SELECT telemetry_ready FROM drives WHERE id = ?",
    (drive["id"],),
  )
  assert drive_state["telemetry_ready"] == 0
  derived = database.query_one(
    "SELECT time_map_path FROM artifacts WHERE id = ?",
    (derived_id,),
  )
  assert derived["time_map_path"] is None
  sync = database.query_one(
    "SELECT status FROM artifacts WHERE id = ?",
    (sync_id,),
  )
  assert sync["status"] == "stale"

  extracts = database.query_all(
    """
    SELECT id, state, payload_json
    FROM jobs
    WHERE type = 'extract_telemetry' AND dedupe_key = ?
    ORDER BY created_at, id
    """,
    (f"drive:{drive['id']}",),
  )
  assert len(extracts) == 2
  assert extracts[0]["id"] == first_extract["id"]
  assert extracts[0]["state"] == "succeeded"
  assert extracts[1]["state"] == "queued"
  next_payload = json.loads(extracts[1]["payload_json"])
  assert next_payload["source_fingerprint"] == telemetry_source_fingerprint([
    {
      "segment_number": 0,
      "sha256": hashlib.sha256(first_data).hexdigest(),
    },
    {
      "segment_number": 1,
      "sha256": hashlib.sha256(second_data).hexdigest(),
    },
  ])


@pytest.mark.parametrize("terminal_state", ("failed", "canceled"))
def test_exact_rlog_reupload_queues_after_terminal_extraction(
  admin_client: TestClient,
  terminal_state: str,
) -> None:
  route_name = f"00000790--exact-retry-{terminal_state}"
  data = f"exact-retry-{terminal_state}".encode()
  first = _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=0,
    data=data,
    idempotency_key=f"exact-retry-{terminal_state}-first",
  )
  database = admin_client.app.state.database
  drive = database.query_one(
    "SELECT id, telemetry_ready FROM drives WHERE route_name = ?",
    (route_name,),
  )
  assert drive is not None
  assert drive["telemetry_ready"] == 0
  original = database.query_one(
    """
    SELECT id
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND json_extract(payload_json, '$.drive_id') = ?
    """,
    (drive["id"],),
  )
  assert original is not None
  database.execute(
    """
    UPDATE jobs
    SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
      available_at = NULL, retryable = 0,
      completed_at = '2026-07-29T10:00:00Z',
      updated_at = '2026-07-29T10:00:00Z'
    WHERE id = ?
    """,
    (terminal_state, original["id"]),
  )

  repeated = _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=0,
    data=data,
    idempotency_key=f"exact-retry-{terminal_state}-second",
  )

  assert repeated["artifact_id"] == first["artifact_id"]
  extracts = database.query_all(
    """
    SELECT id, state, payload_json
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND json_extract(payload_json, '$.drive_id') = ?
    ORDER BY created_at, id
    """,
    (drive["id"],),
  )
  assert len(extracts) == 2
  assert extracts[0]["id"] == original["id"]
  assert extracts[0]["state"] == terminal_state
  assert extracts[1]["state"] == "queued"
  assert (
    json.loads(extracts[1]["payload_json"])["source_fingerprint"]
    == telemetry_source_fingerprint([{
      "segment_number": 0,
      "sha256": hashlib.sha256(data).hexdigest(),
    }])
  )


@pytest.mark.parametrize(
  "active_state",
  ("leased", "running"),
)
@pytest.mark.parametrize(
  "active_condition",
  ("cancel_pending", "last_attempt"),
)
def test_same_fingerprint_artifact_queues_when_active_cannot_retry(
  admin_client: TestClient,
  active_state: str,
  active_condition: str,
) -> None:
  route_name = (
    f"00000790--same-fingerprint-{active_state}-{active_condition}"
  )
  data = f"same-fingerprint-{active_state}-{active_condition}".encode()
  _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=0,
    data=data,
    idempotency_key=(
      f"same-fingerprint-{active_state}-{active_condition}-first"
    ),
  )
  database = admin_client.app.state.database
  drive = database.query_one(
    "SELECT id FROM drives WHERE route_name = ?",
    (route_name,),
  )
  assert drive is not None
  active = database.query_one(
    """
    SELECT id, payload_json, max_attempts
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND json_extract(payload_json, '$.drive_id') = ?
    """,
    (drive["id"],),
  )
  assert active is not None
  attempts = (
    active["max_attempts"]
    if active_condition == "last_attempt"
    else 1
  )
  database.execute(
    """
    UPDATE jobs
    SET state = ?, attempts = ?,
      lease_owner = 'generation-worker',
      lease_expires_at = '2099-01-01T00:00:00Z',
      available_at = NULL
    WHERE id = ?
    """,
    (active_state, attempts, active["id"]),
  )
  if active_condition == "cancel_pending":
    cancellation = request_cancellation(
      database,
      active["id"],
    )
    assert cancellation is not None
    assert cancellation.cancel_requested_at is not None

  _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=0,
    data=data,
    idempotency_key=(
      f"same-fingerprint-{active_state}-{active_condition}-second"
    ),
    filename="rlog-copy.zst",
  )

  extracts = database.query_all(
    """
    SELECT id, state, payload_json, cancel_requested_at
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND json_extract(payload_json, '$.drive_id') = ?
    ORDER BY created_at, id
    """,
    (drive["id"],),
  )
  assert len(extracts) == 2
  assert extracts[0]["id"] == active["id"]
  assert extracts[0]["state"] == active_state
  assert extracts[0]["payload_json"] == active["payload_json"]
  if active_condition == "cancel_pending":
    assert extracts[0]["cancel_requested_at"] is not None
  assert extracts[1]["state"] == "queued"
  assert (
    json.loads(extracts[1]["payload_json"])["source_fingerprint"]
    == json.loads(active["payload_json"])["source_fingerprint"]
  )


@pytest.mark.parametrize(
  ("active_state", "terminal_state"),
  (
    ("leased", "failed"),
    ("running", "canceled"),
  ),
)
def test_new_rlog_queues_one_successor_without_mutating_inflight_generation(
  admin_client: TestClient,
  active_state: str,
  terminal_state: str,
) -> None:
  route_name = f"00000791--{active_state}-follow-up"
  first_data = f"{active_state}-first-rlog".encode()
  _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=0,
    data=first_data,
    idempotency_key=f"{active_state}-follow-up-first",
  )
  database = admin_client.app.state.database
  drive = database.query_one(
    "SELECT id FROM drives WHERE device_id = ? AND route_name = ?",
    (DEVICE_ID, route_name),
  )
  assert drive is not None
  running = database.query_one(
    """
    SELECT id, payload_json
    FROM jobs
    WHERE type = 'extract_telemetry' AND dedupe_key = ?
    """,
    (f"drive:{drive['id']}",),
  )
  assert running is not None
  requested_payload = running["payload_json"]
  database.execute(
    """
    UPDATE jobs
    SET state = ?, attempts = 1, lease_owner = 'generation-worker',
      lease_expires_at = '2099-01-01T00:00:00Z',
      available_at = NULL
    WHERE id = ?
    """,
    (active_state, running["id"]),
  )

  second_data = f"{active_state}-second-rlog".encode()
  _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=1,
    data=second_data,
    idempotency_key=f"{active_state}-follow-up-second",
    filename="rlog-new.zst",
  )
  _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=1,
    data=second_data,
    idempotency_key=f"{active_state}-follow-up-second-duplicate",
    filename="rlog-new-duplicate.zst",
  )

  extracts = database.query_all(
    """
    SELECT id, state, payload_json
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND json_extract(payload_json, '$.drive_id') = ?
    ORDER BY created_at, id
    """,
    (drive["id"],),
  )
  assert len(extracts) == 2
  assert extracts[0]["id"] == running["id"]
  assert extracts[0]["state"] == active_state
  assert extracts[0]["payload_json"] == requested_payload
  assert (
    json.loads(requested_payload)["source_fingerprint"]
    == telemetry_source_fingerprint([{
      "segment_number": 0,
      "sha256": hashlib.sha256(first_data).hexdigest(),
    }])
  )
  successor_id = extracts[1]["id"]
  assert extracts[1]["state"] == "queued"
  successor_payload = json.loads(extracts[1]["payload_json"])
  assert successor_payload["source_fingerprint"] == telemetry_source_fingerprint([
    {
      "segment_number": 0,
      "sha256": hashlib.sha256(first_data).hexdigest(),
    },
    {
      "segment_number": 1,
      "sha256": hashlib.sha256(second_data).hexdigest(),
    },
  ])
  successor = database.query_one(
    "SELECT dedupe_key FROM jobs WHERE id = ?",
    (successor_id,),
  )
  assert successor["dedupe_key"] == (
    f"drive:{drive['id']}:after:{running['id']}"
  )

  database.execute(
    """
    UPDATE jobs
    SET state = 'succeeded', completed_at = updated_at
    WHERE type != 'extract_telemetry' AND state = 'queued'
    """,
  )
  database.execute(
    "UPDATE jobs SET available_at = '2000-01-01T00:00:00Z' WHERE id = ?",
    (successor_id,),
  )
  assert claim_job(database, "other-worker") is None

  if terminal_state == "failed":
    terminal = fail_job(
      database,
      running["id"],
      "generation-worker",
      "generation failed",
      retryable=True,
    )
    assert terminal is not None
    assert terminal.state == "failed"
    assert terminal.retryable is False
  else:
    terminal = cancel_job(
      database,
      running["id"],
      "generation-worker",
    )
    assert terminal is not None
    assert terminal.state == "canceled"

  preserved = database.query_one(
    "SELECT payload_json FROM jobs WHERE id = ?",
    (running["id"],),
  )
  assert preserved["payload_json"] == requested_payload
  queued = database.query_all(
    """
    SELECT id, state
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND state = 'queued'
      AND json_extract(payload_json, '$.drive_id') = ?
    """,
    (drive["id"],),
  )
  assert [row["id"] for row in queued] == [successor_id]
  claimed = claim_job(database, "successor-worker")
  assert claimed is not None
  assert claimed.id == successor_id


def test_fingerprint_flip_back_coalesces_and_active_failure_yields(
  admin_client: TestClient,
) -> None:
  route_name = "00000792--fingerprint-flip-back"
  first_data = b"fingerprint-one"
  second_data = b"fingerprint-two"
  first = _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=0,
    data=first_data,
    idempotency_key="fingerprint-flip-first",
    filename="rlog-f1.zst",
  )
  database = admin_client.app.state.database
  drive = database.query_one(
    "SELECT id FROM drives WHERE route_name = ?",
    (route_name,),
  )
  assert drive is not None
  active = database.query_one(
    """
    SELECT id, payload_json
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND json_extract(payload_json, '$.drive_id') = ?
    """,
    (drive["id"],),
  )
  assert active is not None
  active_payload = active["payload_json"]
  database.execute(
    """
    UPDATE jobs
    SET state = 'running', attempts = 1,
      lease_owner = 'generation-worker',
      lease_expires_at = '2099-01-01T00:00:00Z',
      available_at = NULL
    WHERE id = ?
    """,
    (active["id"],),
  )

  second = _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=0,
    data=second_data,
    idempotency_key="fingerprint-flip-second",
    filename="rlog-f2.zst",
  )
  successor = database.query_one(
    """
    SELECT id, payload_json
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND state = 'queued'
      AND json_extract(payload_json, '$.drive_id') = ?
    """,
    (drive["id"],),
  )
  assert successor is not None
  assert (
    json.loads(successor["payload_json"])["source_fingerprint"]
    == telemetry_source_fingerprint([{
      "segment_number": 0,
      "sha256": hashlib.sha256(second_data).hexdigest(),
    }])
  )
  database.execute(
    """
    UPDATE artifacts
    SET created_at = CASE id
      WHEN ? THEN '2000-01-01T00:00:00Z'
      WHEN ? THEN '2000-01-01T00:00:01Z'
      ELSE created_at
    END
    WHERE id IN (?, ?)
    """,
    (
      first["artifact_id"],
      second["artifact_id"],
      first["artifact_id"],
      second["artifact_id"],
    ),
  )

  _complete_log_upload(
    admin_client,
    route_name=route_name,
    segment_number=0,
    data=first_data,
    idempotency_key="fingerprint-flip-third",
    filename="rlog-f1-return.zst",
  )

  extracts = database.query_all(
    """
    SELECT id, state, payload_json
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND json_extract(payload_json, '$.drive_id') = ?
    ORDER BY created_at, id
    """,
    (drive["id"],),
  )
  assert len(extracts) == 2
  assert extracts[0]["id"] == active["id"]
  assert extracts[0]["payload_json"] == active_payload
  assert extracts[1]["id"] == successor["id"]
  assert extracts[1]["state"] == "queued"
  assert (
    json.loads(extracts[1]["payload_json"])["source_fingerprint"]
    == json.loads(active_payload)["source_fingerprint"]
  )

  terminal = fail_job(
    database,
    active["id"],
    "generation-worker",
    "active generation failed",
    retryable=True,
  )
  assert terminal is not None
  assert terminal.state == "failed"
  assert terminal.retryable is False
  queued = database.query_all(
    """
    SELECT id
    FROM jobs
    WHERE type = 'extract_telemetry'
      AND state = 'queued'
      AND json_extract(payload_json, '$.drive_id') = ?
    """,
    (drive["id"],),
  )
  assert [row["id"] for row in queued] == [successor["id"]]
  database.execute(
    """
    UPDATE jobs
    SET state = 'succeeded', completed_at = updated_at
    WHERE type != 'extract_telemetry' AND state = 'queued'
    """,
  )
  database.execute(
    "UPDATE jobs SET available_at = '2000-01-01T00:00:00Z' WHERE id = ?",
    (successor["id"],),
  )
  claimed = claim_job(database, "successor-worker")
  assert claimed is not None
  assert claimed.id == successor["id"]


def test_declared_sha_mismatch_fails_without_object(
  admin_client: TestClient,
  settings,
) -> None:
  data = b"does-not-match"
  declaration = _declaration(
    data,
    relative_path="00000123--abcdef/1/rlog.zst",
    sha256="0" * 64,
  )
  declaration["artifact_type"] = "rlog"
  declaration["camera"] = None
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="bad-digest"),
    json=declaration,
  )
  assert created.status_code == 201
  upload_id = created.json()["id"]
  failed = admin_client.patch(
    f"/api/v1/uploads/{upload_id}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(data),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data,
  )
  assert failed.status_code == 422
  assert failed.json()["error"]["code"] == "sha256_mismatch"
  head = admin_client.head(
    f"/api/v1/uploads/{upload_id}",
    headers=device_headers(),
  )
  assert head.headers["upload-state"] == "failed"
  assert head.headers["upload-terminal"] == "true"
  assert head.headers["upload-retry-action"] == "redeclare"
  assert list((settings.archive_root / "objects" / "sha256").rglob("0" * 64)) == []


def test_cancel_closes_server_session_and_retry_redeclares(
  admin_client: TestClient,
  settings,
) -> None:
  data = b"cancel-me"
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="upload-create:file:0"),
    json=_declaration(data),
  )
  assert created.status_code == 201
  upload_id = created.json()["id"]
  first = data[:3]
  patched = admin_client.patch(
    f"/api/v1/uploads/{upload_id}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(first),
      "Content-Type": "application/offset+octet-stream",
    },
    content=first,
  )
  assert patched.status_code == 200

  cancel_headers = device_headers(
    idempotency_key=f"upload-cancel:file:{upload_id}",
  )
  canceled = admin_client.post(
    f"/api/v1/uploads/{upload_id}/cancel",
    headers=cancel_headers,
  )
  assert canceled.status_code == 200, canceled.text
  assert canceled.json()["state"] == "canceled"
  assert canceled.json()["durable"] is False
  assert not (settings.uploads_root / f"{upload_id}.part").exists()

  replay = admin_client.post(
    f"/api/v1/uploads/{upload_id}/cancel",
    headers=cancel_headers,
  )
  assert replay.status_code == 200
  assert replay.json()["state"] == "canceled"

  old_generation = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="upload-create:file:0"),
    json=_declaration(data),
  )
  assert old_generation.status_code == 201
  assert old_generation.json()["id"] == upload_id
  assert old_generation.json()["state"] == "canceled"

  head = admin_client.head(
    f"/api/v1/uploads/{upload_id}",
    headers=device_headers(),
  )
  assert head.status_code == 204
  assert head.headers["upload-state"] == "canceled"
  assert head.headers["upload-terminal"] == "true"
  assert head.headers["upload-retry-action"] == "redeclare"

  rejected = admin_client.patch(
    f"/api/v1/uploads/{upload_id}",
    headers={
      **device_headers(),
      "Upload-Offset": str(len(first)),
      "Upload-Checksum": _checksum(data[len(first) :]),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data[len(first) :],
  )
  assert rejected.status_code == 409
  assert rejected.json()["error"]["code"] == "upload_terminal"
  assert rejected.headers["upload-state"] == "canceled"

  retried = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="upload-create:file:1"),
    json=_declaration(data),
  )
  assert retried.status_code == 201
  assert retried.json()["id"] != upload_id
  assert retried.json()["state"] == "receiving"


def test_terminal_state_wins_paused_finalization_without_cataloging(
  admin_client: TestClient,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  data = b"finalization-race"
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="race-create"),
    json=_declaration(data),
  )
  upload_id = created.json()["id"]
  installed = threading.Event()
  release = threading.Event()
  original_install = uploads_module._install_object

  def paused_install(*args, **kwargs):
    result = original_install(*args, **kwargs)
    installed.set()
    assert release.wait(5)
    return result

  monkeypatch.setattr(uploads_module, "_install_object", paused_install)

  def finalize():
    return admin_client.patch(
      f"/api/v1/uploads/{upload_id}",
      headers={
        **device_headers(),
        "Upload-Offset": "0",
        "Upload-Checksum": _checksum(data),
        "Content-Type": "application/offset+octet-stream",
      },
      content=data,
    )

  with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(finalize)
    assert installed.wait(5)
    database = admin_client.app.state.database
    now = isoformat()
    database.execute(
      """
      UPDATE uploads
      SET status = 'canceled', error = 'Canceled during finalization',
        completed_at = ?, updated_at = ?
      WHERE id = ? AND status = 'finalizing'
      """,
      (now, now, upload_id),
    )
    release.set()
    finalized = future.result(timeout=5)

  assert finalized.status_code == 409
  assert finalized.json()["error"]["code"] == "upload_terminal"
  assert (
    database.query_one(
      "SELECT artifact_id FROM uploads WHERE id = ?",
      (upload_id,),
    )["artifact_id"]
    is None
  )
  assert (
    database.query_one(
      "SELECT COUNT(*) AS count FROM artifacts WHERE device_id = ?",
      (DEVICE_ID,),
    )["count"]
    == 0
  )


def test_device_cannot_cancel_another_devices_upload(
  admin_client: TestClient,
) -> None:
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="owned-upload"),
    json=_declaration(b"owned"),
  )
  upload_id = created.json()["id"]
  database = admin_client.app.state.database
  other_token = "another-device-token-that-is-long-and-random"
  database.execute(
    """
    INSERT INTO devices(id, display_name, token_hash, enrolled_at)
    VALUES (?, ?, ?, ?)
    """,
    (
      "device-two",
      "Device two",
      admin_client.app.state.auth.hash_token(other_token),
      isoformat(),
    ),
  )
  denied = admin_client.post(
    f"/api/v1/uploads/{upload_id}/cancel",
    headers={
      "Authorization": f"Bearer {other_token}",
      "Idempotency-Key": "cross-device-cancel",
    },
  )
  assert denied.status_code == 403
  assert denied.json()["error"]["code"] == "upload_access_denied"


def test_empty_media_is_rejected_before_catalog_or_jobs(
  admin_client: TestClient,
) -> None:
  response = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="empty-media"),
    json=_declaration(b""),
  )
  assert response.status_code == 422
  assert response.json()["error"]["code"] == "empty_artifact"
  database = admin_client.app.state.database
  assert (
    database.query_one(
      "SELECT COUNT(*) AS count FROM uploads",
    )["count"]
    == 0
  )
  assert (
    database.query_one(
      "SELECT COUNT(*) AS count FROM jobs",
    )["count"]
    == 0
  )


def test_upload_admission_caps_and_reclaims_stale_parts(
  admin_client: TestClient,
) -> None:
  original_settings = admin_client.app.state.settings
  admin_client.app.state.settings = replace(
    original_settings,
    max_active_uploads_per_device=1,
    upload_stale_seconds=60,
  )
  first = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="capacity-first"),
    json=_declaration(b"first"),
  )
  assert first.status_code == 201
  first_id = first.json()["id"]

  capped = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="capacity-second"),
    json=_declaration(b"second"),
  )
  assert capped.status_code == 429
  assert capped.json()["error"]["code"] == "upload_capacity_exceeded"

  database = admin_client.app.state.database
  stale = database.query_one(
    "SELECT part_path FROM uploads WHERE id = ?",
    (first_id,),
  )
  stale_path = original_settings.archive_root / stale["part_path"]
  assert stale_path.is_file()
  database.execute(
    "UPDATE uploads SET updated_at = ? WHERE id = ?",
    ("2000-01-01T00:00:00Z", first_id),
  )

  admitted = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="capacity-after-expiry"),
    json=_declaration(b"replacement"),
  )
  assert admitted.status_code == 201
  expired = database.query_one(
    "SELECT status, error FROM uploads WHERE id = ?",
    (first_id,),
  )
  assert expired["status"] == "failed"
  assert expired["error"] == "Upload expired before completion"
  assert not stale_path.exists()


def test_chunk_copy_is_offloaded_and_outside_write_transaction(
  admin_client: TestClient,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  data = b"threaded-cifs-copy"
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="threaded-copy"),
    json=_declaration(data),
  )
  upload_id = created.json()["id"]
  database = admin_client.app.state.database
  original_append = uploads_module._append_chunk_file
  observed: dict[str, object] = {}

  def checked_append(*args, **kwargs):
    observed["thread"] = threading.current_thread().name
    with database.transaction(immediate=True) as connection:
      observed["write_transaction_available"] = connection.execute("SELECT 1").fetchone()[0] == 1
    return original_append(*args, **kwargs)

  monkeypatch.setattr(
    uploads_module,
    "_append_chunk_file",
    checked_append,
  )
  response = admin_client.patch(
    f"/api/v1/uploads/{upload_id}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(data),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data,
  )
  assert response.status_code == 200
  assert response.json()["state"] == "complete"
  assert observed["write_transaction_available"] is True
  assert observed["thread"] == "AnyIO worker thread"


def test_full_declared_size_remains_reserved_after_partial_receipt(
  admin_client: TestClient,
) -> None:
  admin_client.app.state.settings = replace(
    admin_client.app.state.settings,
    max_pending_upload_bytes_per_device=9,
  )
  data = b"abcdefgh"
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="full-reservation-first"),
    json=_declaration(data),
  )
  upload_id = created.json()["id"]
  partial = admin_client.patch(
    f"/api/v1/uploads/{upload_id}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(data[:7]),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data[:7],
  )
  assert partial.status_code == 200
  assert partial.json()["offset"] == 7

  capped = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="full-reservation-second"),
    json=_declaration(b"xy", relative_path="route/0/second.bin"),
  )
  assert capped.status_code == 429
  assert capped.json()["error"]["code"] == "upload_capacity_exceeded"

  canceled = admin_client.post(
    f"/api/v1/uploads/{upload_id}/cancel",
    headers=device_headers(idempotency_key="full-reservation-cancel"),
  )
  assert canceled.status_code == 200
  admitted = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="full-reservation-second"),
    json=_declaration(b"xy", relative_path="route/0/second.bin"),
  )
  assert admitted.status_code == 201


def test_global_reservation_uses_full_size_across_devices(
  admin_client: TestClient,
) -> None:
  other_device = "reservation-device-two"
  other_token = "reservation-device-token-that-is-long-and-random"
  admin_client.app.state.database.execute(
    """
    INSERT INTO devices(id, display_name, token_hash, enrolled_at)
    VALUES (?, ?, ?, ?)
    """,
    (
      other_device,
      "Reservation device two",
      admin_client.app.state.auth.hash_token(other_token),
      isoformat(),
    ),
  )
  admin_client.app.state.settings = replace(
    admin_client.app.state.settings,
    max_pending_upload_bytes_per_device=9,
    max_pending_upload_bytes_global=9,
  )
  data = b"abcdefgh"
  first = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="global-reservation-first"),
    json=_declaration(data),
  )
  partial = admin_client.patch(
    f"/api/v1/uploads/{first.json()['id']}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(data[:7]),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data[:7],
  )
  assert partial.status_code == 200

  second_declaration = _declaration(
    b"xy",
    relative_path="route/0/global-reservation-second.bin",
  )
  second_declaration["device_id"] = other_device
  rejected = admin_client.post(
    "/api/v1/uploads",
    headers={
      "Authorization": f"Bearer {other_token}",
      "Idempotency-Key": "global-reservation-second",
    },
    json=second_declaration,
  )
  assert rejected.status_code == 503
  assert rejected.json()["error"]["code"] == "archive_backpressure"


def test_full_size_reservation_is_held_while_finalizing(
  admin_client: TestClient,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  data = b"reservation-during-finalization"
  admin_client.app.state.settings = replace(
    admin_client.app.state.settings,
    max_pending_upload_bytes_per_device=len(data),
  )
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="finalizing-reservation"),
    json=_declaration(data),
  )
  upload_id = created.json()["id"]
  installed = threading.Event()
  release = threading.Event()
  original_install = uploads_module._install_object

  def paused_install(*args, **kwargs):
    result = original_install(*args, **kwargs)
    installed.set()
    assert release.wait(5)
    return result

  monkeypatch.setattr(uploads_module, "_install_object", paused_install)

  def finalize():
    return admin_client.patch(
      f"/api/v1/uploads/{upload_id}",
      headers={
        **device_headers(),
        "Upload-Offset": "0",
        "Upload-Checksum": _checksum(data),
        "Content-Type": "application/offset+octet-stream",
      },
      content=data,
    )

  with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(finalize)
    assert installed.wait(5)
    row = admin_client.app.state.database.query_one(
      "SELECT status, offset, declared_size FROM uploads WHERE id = ?",
      (upload_id,),
    )
    assert dict(row) == {
      "status": "finalizing",
      "offset": len(data),
      "declared_size": len(data),
    }
    capped = admin_client.post(
      "/api/v1/uploads",
      headers=device_headers(idempotency_key="during-finalization-second"),
      json=_declaration(b"x", relative_path="route/0/during.bin"),
    )
    assert capped.status_code == 429
    release.set()
    result = future.result(timeout=5)
  assert result.status_code == 200
  assert result.json()["state"] == "complete"


def test_database_finalization_claim_allows_only_one_installer(
  admin_client: TestClient,
  settings,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  data = b"single-finalizer"
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="single-finalizer"),
    json=_declaration(data),
  )
  upload_id = created.json()["id"]
  part_path = settings.uploads_root / f"{upload_id}.part"
  part_path.write_bytes(data)
  admin_client.app.state.database.execute(
    "UPDATE uploads SET offset = ?, updated_at = ? WHERE id = ?",
    (len(data), isoformat(), upload_id),
  )

  installed = threading.Event()
  release = threading.Event()
  install_calls = 0
  original_install = uploads_module._install_object

  def paused_install(*args, **kwargs):
    nonlocal install_calls
    install_calls += 1
    result = original_install(*args, **kwargs)
    installed.set()
    assert release.wait(5)
    return result

  monkeypatch.setattr(uploads_module, "_install_object", paused_install)

  async def unused_receive():
    return {"type": "http.disconnect"}

  first_request = _stream_request(admin_client.app, unused_receive)
  second_request = _stream_request(admin_client.app, unused_receive)
  with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(
      uploads_module._finalize_upload_locked,
      first_request,
      upload_id,
    )
    assert installed.wait(5)
    concurrent = uploads_module._finalize_upload_locked(
      second_request,
      upload_id,
    )
    assert concurrent["state"] == "finalizing"
    assert install_calls == 1
    release.set()
    completed = future.result(timeout=5)
  assert completed["state"] == "complete"


def test_global_finalizer_gate_defers_additional_installers(
  admin_client: TestClient,
  settings,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  admin_client.app.state.settings = replace(
    admin_client.app.state.settings,
    max_inflight_upload_patches_per_device=1,
    max_inflight_upload_patches_global=1,
  )
  other_device = "finalizer-device-two"
  other_token = "finalizer-device-token-that-is-long-and-random"
  admin_client.app.state.database.execute(
    """
    INSERT INTO devices(id, display_name, token_hash, enrolled_at)
    VALUES (?, ?, ?, ?)
    """,
    (
      other_device,
      "Finalizer device two",
      admin_client.app.state.auth.hash_token(other_token),
      isoformat(),
    ),
  )
  uploads: list[str] = []
  for index, data in enumerate((b"first-finalizer", b"second-finalizer")):
    declaration = _declaration(
      data,
      relative_path=f"route/0/finalizer-gate-{index}.bin",
    )
    headers = device_headers(idempotency_key=f"finalizer-gate-{index}")
    if index == 1:
      declaration["device_id"] = other_device
      headers = {
        "Authorization": f"Bearer {other_token}",
        "Idempotency-Key": f"finalizer-gate-{index}",
      }
    created = admin_client.post(
      "/api/v1/uploads",
      headers=headers,
      json=declaration,
    )
    upload_id = created.json()["id"]
    (settings.uploads_root / f"{upload_id}.part").write_bytes(data)
    admin_client.app.state.database.execute(
      "UPDATE uploads SET offset = ?, updated_at = ? WHERE id = ?",
      (len(data), isoformat(), upload_id),
    )
    uploads.append(upload_id)

  installed = threading.Event()
  release = threading.Event()
  original_install = uploads_module._install_object

  def paused_install(*args, **kwargs):
    result = original_install(*args, **kwargs)
    installed.set()
    assert release.wait(5)
    return result

  monkeypatch.setattr(uploads_module, "_install_object", paused_install)

  async def unused_receive():
    return {"type": "http.disconnect"}

  with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(
      uploads_module._finalize_upload_locked,
      _stream_request(admin_client.app, unused_receive),
      uploads[0],
    )
    assert installed.wait(5)
    with pytest.raises(uploads_module.ApiError) as error:
      uploads_module._finalize_upload_locked(
        _stream_request(admin_client.app, unused_receive),
        uploads[1],
      )
    assert error.value.status_code == 503
    assert error.value.code == "archive_backpressure"
    assert admin_client.app.state.database.query_one(
      "SELECT status FROM uploads WHERE id = ?",
      (uploads[1],),
    )["status"] == "receiving"
    release.set()
    assert future.result(timeout=5)["state"] == "complete"

  monkeypatch.setattr(uploads_module, "_install_object", original_install)
  retried = uploads_module._finalize_upload_locked(
    _stream_request(admin_client.app, unused_receive),
    uploads[1],
  )
  assert retried["state"] == "complete"


@pytest.mark.parametrize(
  ("artifact_type", "setting_name", "limit"),
  [
    ("video", "max_video_artifact_bytes", 3),
    ("rlog", "max_log_artifact_bytes", 4),
    ("artifact", "max_other_artifact_bytes", 5),
  ],
)
def test_type_specific_artifact_limits_are_inclusive(
  admin_client: TestClient,
  artifact_type: str,
  setting_name: str,
  limit: int,
) -> None:
  admin_client.app.state.settings = replace(
    admin_client.app.state.settings,
    **{setting_name: limit},
  )
  oversized_data = b"x" * (limit + 1)
  oversized = _declaration(
    oversized_data,
    relative_path=f"route/0/{artifact_type}-oversized.bin",
  )
  oversized["artifact_type"] = artifact_type
  oversized["camera"] = "road" if artifact_type == "video" else None
  rejected = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key=f"typed-{artifact_type}-oversized"),
    json=oversized,
  )
  assert rejected.status_code == 413
  assert rejected.json()["error"]["code"] == "artifact_too_large"

  boundary_data = b"y" * limit
  boundary = _declaration(
    boundary_data,
    relative_path=f"route/0/{artifact_type}-boundary.bin",
  )
  boundary["artifact_type"] = artifact_type
  boundary["camera"] = "road" if artifact_type == "video" else None
  admitted = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key=f"typed-{artifact_type}-boundary"),
    json=boundary,
  )
  assert admitted.status_code == 201


def test_patch_rechecks_type_specific_limit_before_receiving(
  admin_client: TestClient,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  data = b"abcdef"
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="patch-typed-limit"),
    json=_declaration(data),
  )
  admin_client.app.state.settings = replace(
    admin_client.app.state.settings,
    max_video_artifact_bytes=len(data) - 1,
  )

  async def forbidden_receive(*_args, **_kwargs):
    raise AssertionError("request body must not be received")

  monkeypatch.setattr(uploads_module, "_receive_chunk", forbidden_receive)
  rejected = admin_client.patch(
    f"/api/v1/uploads/{created.json()['id']}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(data),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data,
  )
  assert rejected.status_code == 413
  assert rejected.json()["error"]["code"] == "artifact_too_large"


def test_archive_minimum_free_bytes_and_percent_are_enforced_at_create(
  admin_client: TestClient,
  settings,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  free_bytes = {"value": 19}

  def disk_usage(_path):
    return SimpleNamespace(total=100, used=81, free=free_bytes["value"])

  monkeypatch.setattr(uploads_module.shutil, "disk_usage", disk_usage)
  admin_client.app.state.settings = replace(
    admin_client.app.state.settings,
    archive_min_free_bytes=5,
    archive_min_free_percent=10,
  )
  data = b"0123456789"
  rejected = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="disk-floor-rejected"),
    json=_declaration(data),
  )
  assert rejected.status_code == 503
  assert rejected.json()["error"]["code"] == "archive_storage_pressure"
  assert list(settings.uploads_root.glob("*.part")) == []

  free_bytes["value"] = 20
  admitted = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="disk-floor-boundary"),
    json=_declaration(data, relative_path="route/0/disk-boundary.bin"),
  )
  assert admitted.status_code == 201


def test_patch_rechecks_disk_floor_before_staging_body(
  admin_client: TestClient,
  settings,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  free_bytes = {"value": 100}

  def disk_usage(_path):
    return SimpleNamespace(total=100, used=0, free=free_bytes["value"])

  monkeypatch.setattr(uploads_module.shutil, "disk_usage", disk_usage)
  admin_client.app.state.settings = replace(
    admin_client.app.state.settings,
    archive_min_free_bytes=5,
    archive_min_free_percent=10,
  )
  data = b"abcdef"
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="patch-disk-floor"),
    json=_declaration(data),
  )
  upload_id = created.json()["id"]
  free_bytes["value"] = 15

  async def forbidden_receive(*_args, **_kwargs):
    raise AssertionError("request body must not be received")

  monkeypatch.setattr(uploads_module, "_receive_chunk", forbidden_receive)
  rejected = admin_client.patch(
    f"/api/v1/uploads/{upload_id}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(data),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data,
  )
  assert rejected.status_code == 503
  assert rejected.json()["error"]["code"] == "archive_storage_pressure"
  assert (settings.uploads_root / f"{upload_id}.part").read_bytes() == b""
  assert list(settings.uploads_root.glob(".*.chunk")) == []


def test_receive_chunk_streams_each_asgi_message_through_threadpool(
  admin_client: TestClient,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  messages = [
    {"type": "http.request", "body": b"ab", "more_body": True},
    {"type": "http.request", "body": b"cd", "more_body": True},
    {"type": "http.request", "body": b"ef", "more_body": False},
  ]

  async def receive():
    return messages.pop(0)

  writes: list[tuple[bytes, str]] = []
  original_write = uploads_module._ChunkSink.write

  def observed_write(sink, data):
    writes.append((bytes(data), threading.current_thread().name))
    return original_write(sink, data)

  monkeypatch.setattr(uploads_module._ChunkSink, "write", observed_write)
  request = _stream_request(admin_client.app, receive)
  chunk_path, size, digest = asyncio.run(
    uploads_module._receive_chunk(
      request,
      "streamed",
      max_body_bytes=6,
      declared_length=6,
    ),
  )
  try:
    assert chunk_path.read_bytes() == b"abcdef"
    assert size == 6
    assert digest == hashlib.sha256(b"abcdef").hexdigest()
    assert [data for data, _ in writes] == [b"ab", b"cd", b"ef"]
    assert {thread for _, thread in writes} == {"AnyIO worker thread"}
  finally:
    chunk_path.unlink(missing_ok=True)


def test_patch_cancellation_cleans_temp_and_releases_admission(
  admin_client: TestClient,
  settings,
) -> None:
  import comma_companion.uploads as uploads_module

  data = b"abcd"
  created = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="canceled-stream"),
    json=_declaration(data),
  )
  upload_id = created.json()["id"]
  receive_count = 0

  async def receive():
    nonlocal receive_count
    receive_count += 1
    if receive_count == 1:
      return {"type": "http.request", "body": b"ab", "more_body": True}
    raise asyncio.CancelledError

  request = _stream_request(
    admin_client.app,
    receive,
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Content-Type": "application/offset+octet-stream",
    },
  )
  with pytest.raises(asyncio.CancelledError):
    asyncio.run(uploads_module.patch_upload(request, upload_id))
  assert list(settings.uploads_root.glob(".*.chunk")) == []
  assert (settings.uploads_root / f"{upload_id}.part").read_bytes() == b""

  completed = admin_client.patch(
    f"/api/v1/uploads/{upload_id}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(data),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data,
  )
  assert completed.status_code == 200
  assert completed.json()["state"] == "complete"


def test_terminal_and_unknown_offsets_reject_before_body_receive(
  admin_client: TestClient,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  data = b"offset-check"
  terminal = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="prebody-terminal"),
    json=_declaration(data),
  )
  terminal_id = terminal.json()["id"]
  canceled = admin_client.post(
    f"/api/v1/uploads/{terminal_id}/cancel",
    headers=device_headers(idempotency_key="prebody-terminal-cancel"),
  )
  assert canceled.status_code == 200
  receiving = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="prebody-offset"),
    json=_declaration(
      data,
      relative_path="route/0/prebody-offset.bin",
    ),
  )

  async def forbidden_receive(*_args, **_kwargs):
    raise AssertionError("request body must not be received")

  monkeypatch.setattr(uploads_module, "_receive_chunk", forbidden_receive)
  terminal_patch = admin_client.patch(
    f"/api/v1/uploads/{terminal_id}",
    headers={
      **device_headers(),
      "Upload-Offset": "0",
      "Upload-Checksum": _checksum(data),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data,
  )
  assert terminal_patch.status_code == 409
  assert terminal_patch.json()["error"]["code"] == "upload_terminal"

  wrong_offset = admin_client.patch(
    f"/api/v1/uploads/{receiving.json()['id']}",
    headers={
      **device_headers(),
      "Upload-Offset": "1",
      "Upload-Checksum": _checksum(data),
      "Content-Type": "application/offset+octet-stream",
    },
    content=data,
  )
  assert wrong_offset.status_code == 409
  assert wrong_offset.json()["error"]["code"] == "upload_offset_mismatch"


def test_per_device_inflight_patch_limit_rejects_before_receive(
  admin_client: TestClient,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  admin_client.app.state.settings = replace(
    admin_client.app.state.settings,
    max_inflight_upload_patches_per_device=1,
  )
  uploads = []
  for index, data in enumerate((b"a", b"b")):
    response = admin_client.post(
      "/api/v1/uploads",
      headers=device_headers(idempotency_key=f"inflight-device-{index}"),
      json=_declaration(
        data,
        relative_path=f"route/0/inflight-device-{index}.bin",
      ),
    )
    uploads.append((response.json()["id"], data))

  entered = threading.Event()
  release = threading.Event()
  calls = 0
  original_receive = uploads_module._receive_chunk

  async def paused_receive(*args, **kwargs):
    nonlocal calls
    calls += 1
    entered.set()
    assert await uploads_module.run_in_threadpool(release.wait, 5)
    return await original_receive(*args, **kwargs)

  monkeypatch.setattr(uploads_module, "_receive_chunk", paused_receive)

  def first_patch():
    upload_id, data = uploads[0]
    return admin_client.patch(
      f"/api/v1/uploads/{upload_id}",
      headers={
        **device_headers(),
        "Upload-Offset": "0",
        "Upload-Checksum": _checksum(data),
        "Content-Type": "application/offset+octet-stream",
      },
      content=data,
    )

  with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(first_patch)
    assert entered.wait(5)
    second_id, second_data = uploads[1]
    rejected = admin_client.patch(
      f"/api/v1/uploads/{second_id}",
      headers={
        **device_headers(),
        "Upload-Offset": "0",
        "Upload-Checksum": _checksum(second_data),
        "Content-Type": "application/offset+octet-stream",
      },
      content=second_data,
    )
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "upload_patch_capacity_exceeded"
    assert calls == 1
    release.set()
    completed = future.result(timeout=5)
  assert completed.status_code == 200


def test_global_inflight_patch_limit_rejects_before_receive(
  admin_client: TestClient,
  monkeypatch,
) -> None:
  import comma_companion.uploads as uploads_module

  other_device = "device-global-two"
  other_token = "global-limit-device-token-that-is-long-and-random"
  admin_client.app.state.database.execute(
    """
    INSERT INTO devices(id, display_name, token_hash, enrolled_at)
    VALUES (?, ?, ?, ?)
    """,
    (
      other_device,
      "Global limit device",
      admin_client.app.state.auth.hash_token(other_token),
      isoformat(),
    ),
  )
  admin_client.app.state.settings = replace(
    admin_client.app.state.settings,
    max_inflight_upload_patches_per_device=1,
    max_inflight_upload_patches_global=1,
  )
  first_data = b"a"
  first = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="inflight-global-first"),
    json=_declaration(
      first_data,
      relative_path="route/0/inflight-global-first.bin",
    ),
  )
  second_data = b"b"
  second_declaration = _declaration(
    second_data,
    relative_path="route/0/inflight-global-second.bin",
  )
  second_declaration["device_id"] = other_device
  second = admin_client.post(
    "/api/v1/uploads",
    headers={
      "Authorization": f"Bearer {other_token}",
      "Idempotency-Key": "inflight-global-second",
    },
    json=second_declaration,
  )

  entered = threading.Event()
  release = threading.Event()
  calls = 0
  original_receive = uploads_module._receive_chunk

  async def paused_receive(*args, **kwargs):
    nonlocal calls
    calls += 1
    entered.set()
    assert await uploads_module.run_in_threadpool(release.wait, 5)
    return await original_receive(*args, **kwargs)

  monkeypatch.setattr(uploads_module, "_receive_chunk", paused_receive)

  def first_patch():
    return admin_client.patch(
      f"/api/v1/uploads/{first.json()['id']}",
      headers={
        **device_headers(),
        "Upload-Offset": "0",
        "Upload-Checksum": _checksum(first_data),
        "Content-Type": "application/offset+octet-stream",
      },
      content=first_data,
    )

  with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(first_patch)
    assert entered.wait(5)
    rejected = admin_client.patch(
      f"/api/v1/uploads/{second.json()['id']}",
      headers={
        "Authorization": f"Bearer {other_token}",
        "Upload-Offset": "0",
        "Upload-Checksum": _checksum(second_data),
        "Content-Type": "application/offset+octet-stream",
      },
      content=second_data,
    )
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "archive_backpressure"
    assert calls == 1
    release.set()
    completed = future.result(timeout=5)
  assert completed.status_code == 200


def test_import_token_is_rejected_from_non_loopback_client(
  admin_client: TestClient,
) -> None:
  data = b"historical-rlog"
  declaration = _declaration(
    data,
    relative_path="00000456--history/2/rlog.zst",
  )
  declaration["route_name"] = "00000456--history"
  declaration["segment_number"] = 2
  declaration["artifact_type"] = "rlog"
  declaration["camera"] = None
  created = admin_client.post(
    "/api/v1/uploads",
    headers={
      "Authorization": f"Bearer {IMPORT_TOKEN}",
      "Idempotency-Key": "historical-upload",
      "X-Forwarded-For": "127.0.0.1",
    },
    json=declaration,
  )
  assert created.status_code == 403
  assert created.json()["error"]["code"] == "importer_source_not_allowed"
  assert (
    admin_client.app.state.database.query_one(
      "SELECT COUNT(*) AS count FROM uploads",
    )["count"]
    == 0
  )


def test_import_token_can_upload_for_enrolled_device_over_loopback(
  settings,
) -> None:
  data = b"historical-rlog"
  declaration = _declaration(
    data,
    relative_path="00000456--history/2/rlog.zst",
  )
  declaration["route_name"] = "00000456--history"
  declaration["segment_number"] = 2
  declaration["artifact_type"] = "rlog"
  declaration["camera"] = None
  with TestClient(
    create_app(settings),
    base_url="http://127.0.0.1:18000",
    client=("127.0.0.1", 50000),
  ) as loopback_client:
    claimed_agent_file = dict(declaration)
    claimed_agent_file["file_id"] = "a" * 64
    rejected = loopback_client.post(
      "/api/v1/uploads",
      headers={
        "Authorization": f"Bearer {IMPORT_TOKEN}",
        "Idempotency-Key": "historical-upload-agent-file-id",
      },
      json=claimed_agent_file,
    )
    created = loopback_client.post(
      "/api/v1/uploads",
      headers={
        "Authorization": f"Bearer {IMPORT_TOKEN}",
        "Idempotency-Key": "historical-upload",
      },
      json=declaration,
    )
  assert rejected.status_code == 403
  assert rejected.json()["error"]["code"] == "importer_file_id_not_allowed"
  assert created.status_code == 201, created.text
  assert created.json()["device_id"] == DEVICE_ID
  assert created.json()["file_id"] is None


def test_import_token_accepts_only_exact_configured_dnat_gateway(
  settings,
) -> None:
  gateway = "172.30.0.1"
  gateway_settings = replace(
    settings,
    import_allowed_clients=(gateway,),
  )
  declaration = _declaration(
    b"gateway-import",
    relative_path="00000456--history/3/rlog.zst",
  )
  declaration["artifact_type"] = "rlog"
  declaration["camera"] = None
  with TestClient(
    create_app(gateway_settings),
    base_url="http://127.0.0.1:18000",
    client=(gateway, 50000),
  ) as gateway_client:
    allowed = gateway_client.post(
      "/api/v1/uploads",
      headers={
        "Authorization": f"Bearer {IMPORT_TOKEN}",
        "Idempotency-Key": "dnat-gateway-upload",
      },
      json=declaration,
    )
  assert allowed.status_code == 201, allowed.text

  with TestClient(
    create_app(gateway_settings),
    base_url="http://127.0.0.1:18000",
    client=("172.30.0.2", 50000),
  ) as other_client:
    denied = other_client.post(
      "/api/v1/uploads",
      headers={
        "Authorization": f"Bearer {IMPORT_TOKEN}",
        "Idempotency-Key": "other-bridge-client",
        "X-Forwarded-For": gateway,
      },
      json=declaration,
    )
  assert denied.status_code == 403
  assert denied.json()["error"]["code"] == "importer_source_not_allowed"


def test_upload_path_traversal_has_consistent_error(
  admin_client: TestClient,
) -> None:
  declaration = _declaration(b"x", relative_path="../secret")
  response = admin_client.post(
    "/api/v1/uploads",
    headers=device_headers(idempotency_key="traversal"),
    json=declaration,
  )
  assert response.status_code == 422
  assert response.json()["error"]["code"] == "validation_error"
