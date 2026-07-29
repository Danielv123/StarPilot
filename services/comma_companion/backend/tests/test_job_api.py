import pytest

from comma_companion.jobs import claim_job, enqueue_job, fail_job
from conftest import ORIGIN


def test_admin_can_request_job_cancellation(admin_client) -> None:
  database = admin_client.app.state.database
  job = enqueue_job(
    database,
    "verify_artifact",
    {"artifact_id": "artifact-one"},
  )

  missing_origin = admin_client.post(f"/api/v1/jobs/{job.id}/cancel")
  assert missing_origin.status_code == 403
  assert missing_origin.json()["error"]["code"] == "origin_required"

  response = admin_client.post(
    f"/api/v1/jobs/{job.id}/cancel",
    headers={"Origin": ORIGIN},
  )
  assert response.status_code == 200, response.text
  assert response.json()["state"] == "canceled"

  repeated = admin_client.post(
    f"/api/v1/jobs/{job.id}/cancel",
    headers={"Origin": ORIGIN},
  )
  assert repeated.status_code == 200
  assert repeated.json()["state"] == "canceled"

  audit = database.query_all(
    """
    SELECT action, resource_id
    FROM audit_events
    WHERE action = 'job.cancel' AND resource_id = ?
    """,
    (job.id,),
  )
  assert len(audit) == 2


def test_cancel_unknown_job_returns_error(admin_client) -> None:
  response = admin_client.post(
    "/api/v1/jobs/missing/cancel",
    headers={"Origin": ORIGIN},
  )
  assert response.status_code == 404
  assert response.json()["error"]["code"] == "job_not_found"


def test_admin_can_page_and_filter_real_jobs(admin_client) -> None:
  database = admin_client.app.state.database
  first = enqueue_job(
    database,
    "verify_artifact",
    {"artifact_id": "artifact-one"},
  )
  second = enqueue_job(
    database,
    "extract_telemetry",
    {"drive_id": "drive-one"},
  )
  database.execute(
    "UPDATE jobs SET created_at = ? WHERE id = ?",
    ("2099-01-01T00:00:00.000000Z", second.id),
  )

  response = admin_client.get("/api/v1/jobs?limit=1")
  assert response.status_code == 200
  assert response.json()["total"] == 2
  assert response.json()["items"][0]["id"] == second.id
  assert response.json()["items"][0]["progress"] == 0.0

  filtered = admin_client.get(
    "/api/v1/jobs?type=verify_artifact&state=queued",
  )
  assert filtered.status_code == 200
  assert filtered.json()["total"] == 1
  assert filtered.json()["items"][0]["id"] == first.id

  drive = admin_client.get("/api/v1/jobs?drive_id=drive-one")
  assert drive.status_code == 200
  assert drive.json()["total"] == 1
  assert drive.json()["items"][0]["id"] == second.id


def test_job_search_matches_fields_and_resolved_associations(
  admin_client,
) -> None:
  database = admin_client.app.state.database
  now = "2026-07-29T10:00:00.000000Z"
  drive_id = "Drive-Search-Mixed"
  artifact_id = "Artifact-Search-Mixed"
  upload_id = "Upload-Search-Mixed"
  digest = "1" * 64
  database.execute(
    """
    INSERT INTO objects(sha256, size, storage_path, created_at)
    VALUES (?, 1, ?, ?)
    """,
    (digest, f"objects/{digest}", now),
  )
  database.execute(
    """
    INSERT INTO drives(id, device_id, route_name, created_at)
    VALUES (?, 'device-one', 'route-search', ?)
    """,
    (drive_id, now),
  )
  database.execute(
    """
    INSERT INTO artifacts(
      id, device_id, drive_id, object_sha256, kind, relative_path,
      storage_path, size, status, created_at
    )
    VALUES (?, 'device-one', ?, ?, 'rlog', 'route/0/rlog',
      'objects/source-rlog', 1, 'stored', ?)
    """,
    (artifact_id, drive_id, digest, now),
  )
  database.execute(
    """
    INSERT INTO uploads(
      id, device_id, relative_path, artifact_type, declared_size,
      offset, status, part_path, object_sha256, artifact_id,
      created_at, updated_at, completed_at
    )
    VALUES (?, 'device-one', 'route/0/rlog', 'rlog', 1,
      1, 'complete', 'uploads/search.part', ?, ?, ?, ?, ?)
    """,
    (upload_id, digest, artifact_id, now, now, now),
  )
  job = enqueue_job(
    database,
    "Extract_Telemetry_Search",
    {"artifact_id": artifact_id},
  )
  database.execute(
    "UPDATE jobs SET error = ? WHERE id = ?",
    ("Encoder EXPLODED during search", job.id),
  )

  for query in (
    f"  {job.id.upper()}  ",
    "extract_telemetry_search",
    "encoder exploded",
    artifact_id.lower(),
    upload_id.lower(),
    drive_id.lower(),
  ):
    response = admin_client.get("/api/v1/jobs", params={"q": query})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == job.id


def test_job_search_combines_filters_and_counts_before_paging(
  admin_client,
) -> None:
  database = admin_client.app.state.database
  matching = [
    enqueue_job(
      database,
      "catalog_search",
      {"drive_id": "Drive-Page-Needle"},
    )
    for _ in range(2)
  ]
  enqueue_job(
    database,
    "catalog_search",
    {"drive_id": "drive-unrelated"},
  )
  enqueue_job(
    database,
    "other_search",
    {"drive_id": "Drive-Page-Needle"},
  )

  response = admin_client.get(
    "/api/v1/jobs",
    params={
      "q": "  page-NEEDLE  ",
      "state": "queued",
      "type": "catalog_search",
      "drive_id": "Drive-Page-Needle",
      "limit": 1,
      "offset": 1,
    },
  )
  assert response.status_code == 200, response.text
  body = response.json()
  assert body["total"] == 2
  assert len(body["items"]) == 1
  assert body["items"][0]["id"] in {job.id for job in matching}

  empty_page = admin_client.get(
    "/api/v1/jobs",
    params={
      "q": "page-needle",
      "state": "queued",
      "type": "catalog_search",
      "drive_id": "Drive-Page-Needle",
      "limit": 1,
      "offset": 10,
    },
  )
  assert empty_page.status_code == 200
  assert empty_page.json()["total"] == 2
  assert empty_page.json()["items"] == []


def test_job_search_treats_sql_wildcards_as_literal_text(admin_client) -> None:
  database = admin_client.app.state.database
  literal = enqueue_job(database, "search_100%_complete", {})
  enqueue_job(database, "search_100x_complete", {})

  response = admin_client.get(
    "/api/v1/jobs",
    params={"q": "100%_COMPLETE"},
  )
  assert response.status_code == 200
  assert response.json()["total"] == 1
  assert response.json()["items"][0]["id"] == literal.id


@pytest.mark.parametrize("query", ("", "   ", "x" * 257))
def test_job_search_rejects_invalid_q(admin_client, query: str) -> None:
  response = admin_client.get("/api/v1/jobs", params={"q": query})
  assert response.status_code == 422
  assert response.json()["error"] == {
    "code": "invalid_query",
    "message": "q must contain between 1 and 256 characters",
    "details": {},
  }


def test_admin_can_retry_retryable_failure_idempotently(admin_client) -> None:
  database = admin_client.app.state.database
  original = enqueue_job(
    database,
    "verify_artifact",
    {"artifact_id": "artifact-one"},
    max_attempts=1,
  )
  claimed = claim_job(database, "worker-one")
  assert claimed is not None
  assert claimed.id == original.id
  failed = fail_job(
    database,
    original.id,
    "worker-one",
    "temporary storage failure",
    retryable=True,
  )
  assert failed is not None
  assert failed.state == "failed"
  assert failed.retryable is True

  missing_origin = admin_client.post(
    f"/api/v1/jobs/{original.id}/retry",
    headers={"Idempotency-Key": "retry-one"},
  )
  assert missing_origin.status_code == 403

  response = admin_client.post(
    f"/api/v1/jobs/{original.id}/retry",
    headers={
      "Origin": ORIGIN,
      "Idempotency-Key": "retry-one",
    },
  )
  assert response.status_code == 202, response.text
  body = response.json()
  assert body["id"] != original.id
  assert body["retry_of_job_id"] == original.id
  assert body["state"] == "queued"
  assert body["attempts"] == 0
  assert body["retryable"] is None

  repeated = admin_client.post(
    f"/api/v1/jobs/{original.id}/retry",
    headers={
      "Origin": ORIGIN,
      "Idempotency-Key": "retry-one",
    },
  )
  assert repeated.status_code == 202
  assert repeated.json()["id"] == body["id"]

  duplicate = admin_client.post(
    f"/api/v1/jobs/{original.id}/retry",
    headers={
      "Origin": ORIGIN,
      "Idempotency-Key": "retry-two",
    },
  )
  assert duplicate.status_code == 409
  assert duplicate.json()["error"]["code"] == "job_retry_already_active"

  audit = database.query_all(
    """
    SELECT action, resource_id, details_json
    FROM audit_events
    WHERE action = 'job.retry'
    """,
  )
  assert len(audit) == 1
  assert audit[0]["resource_id"] == body["id"]


def test_admin_cannot_retry_permanent_failure(admin_client) -> None:
  database = admin_client.app.state.database
  original = enqueue_job(
    database,
    "verify_artifact",
    {"artifact_id": "artifact-one"},
    max_attempts=1,
  )
  claimed = claim_job(database, "worker-one")
  assert claimed is not None
  failed = fail_job(
    database,
    original.id,
    "worker-one",
    "invalid artifact",
    retryable=False,
  )
  assert failed is not None
  assert failed.retryable is False

  response = admin_client.post(
    f"/api/v1/jobs/{original.id}/retry",
    headers={
      "Origin": ORIGIN,
      "Idempotency-Key": "permanent-retry",
    },
  )
  assert response.status_code == 409
  assert response.json()["error"]["code"] == "job_not_retryable"
