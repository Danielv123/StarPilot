from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import (
  DEVICE_ID,
  admin_mutation_headers,
  device_headers,
)


def _create_status_command(admin_client: TestClient) -> dict[str, object]:
  advertised = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/heartbeat",
    headers=device_headers(),
    json={
      "offroad": False,
      "capabilities": ["command_status"],
    },
  )
  assert advertised.status_code == 200
  created = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/commands",
    headers=admin_mutation_headers("command-status-lookup"),
    json={"type": "status", "args": {}},
  )
  assert created.status_code == 202
  return created.json()


def test_admin_gets_command_by_device_and_id(
  admin_client: TestClient,
) -> None:
  command = _create_status_command(admin_client)

  response = admin_client.get(
    f"/api/v1/devices/{DEVICE_ID}/commands/{command['id']}",
  )

  assert response.status_code == 200
  assert response.json() == command


def test_command_lookup_requires_admin_session(client: TestClient) -> None:
  response = client.get(
    f"/api/v1/devices/{DEVICE_ID}/commands/not-a-command",
  )

  assert response.status_code == 401
  assert response.json()["error"]["code"] == "authentication_required"


def test_command_lookup_does_not_cross_device_boundary(
  admin_client: TestClient,
) -> None:
  command = _create_status_command(admin_client)

  response = admin_client.get(
    f"/api/v1/devices/another-device/commands/{command['id']}",
  )

  assert response.status_code == 404
  assert response.json()["error"]["code"] == "command_not_found"


def test_unknown_command_returns_command_not_found(
  admin_client: TestClient,
) -> None:
  response = admin_client.get(
    f"/api/v1/devices/{DEVICE_ID}/commands/not-a-command",
  )

  assert response.status_code == 404
  assert response.json()["error"]["code"] == "command_not_found"
