from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comma_companion.app import create_app
from comma_companion.config import Settings


ORIGIN = "https://comma.test"
ADMIN_PASSWORD = "correct horse battery staple"
DEVICE_ID = "device-one"
DEVICE_TOKEN = "device-secret-token-that-is-long-and-random"
IMPORT_TOKEN = "import-secret-token-that-is-long-and-random"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
  return Settings(
    database_path=tmp_path / "local" / "companion.sqlite3",
    session_dir=tmp_path / "local" / "sessions",
    archive_root=tmp_path / "archive",
    session_secret="s" * 64,
    admin_username="admin",
    admin_password=ADMIN_PASSWORD,
    public_origin=ORIGIN,
    allowed_origins=(ORIGIN,),
    cookie_name="companion_session",
    cookie_secure=True,
    device_tokens={DEVICE_ID: DEVICE_TOKEN},
    import_token=IMPORT_TOKEN,
    max_chunk_bytes=1024,
    max_artifact_bytes=1024 * 1024,
    archive_min_free_bytes=0,
    archive_min_free_percent=0,
  )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
  with TestClient(
    create_app(settings),
    base_url=ORIGIN,
  ) as test_client:
    yield test_client


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
  response = client.post(
    "/api/v1/auth/login",
    headers={"Origin": ORIGIN},
    json={"username": "admin", "password": ADMIN_PASSWORD},
  )
  assert response.status_code == 200, response.text
  return client


def device_headers(
  *,
  token: str = DEVICE_TOKEN,
  idempotency_key: str | None = None,
) -> dict[str, str]:
  headers = {"Authorization": f"Bearer {token}"}
  if idempotency_key:
    headers["Idempotency-Key"] = idempotency_key
  return headers


def admin_mutation_headers(idempotency_key: str | None = None) -> dict[str, str]:
  headers = {"Origin": ORIGIN}
  if idempotency_key:
    headers["Idempotency-Key"] = idempotency_key
  return headers
