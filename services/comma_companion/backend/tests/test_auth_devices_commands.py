from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import (
  ADMIN_PASSWORD,
  DEVICE_ID,
  ORIGIN,
  admin_mutation_headers,
  device_headers,
)
from comma_companion.db import isoformat
from comma_companion.devices import bootstrap_configured_devices


def test_secure_admin_session_and_origin(client: TestClient) -> None:
  unauthenticated = client.get("/api/v1/auth/session")
  assert unauthenticated.status_code == 401
  assert unauthenticated.json()["error"]["code"] == "authentication_required"

  missing_origin = client.post(
    "/api/v1/auth/login",
    json={"username": "admin", "password": ADMIN_PASSWORD},
  )
  assert missing_origin.status_code == 403
  assert missing_origin.json()["error"]["code"] == "origin_required"

  wrong_password = client.post(
    "/api/v1/auth/login",
    headers={"Origin": ORIGIN},
    json={"username": "admin", "password": "incorrect"},
  )
  assert wrong_password.status_code == 401
  assert wrong_password.json()["error"]["code"] == "invalid_credentials"

  logged_in = client.post(
    "/api/v1/auth/login",
    headers={"Origin": ORIGIN},
    json={"username": "admin", "password": ADMIN_PASSWORD},
  )
  assert logged_in.status_code == 200
  cookie = logged_in.headers["set-cookie"].lower()
  assert "httponly" in cookie
  assert "secure" in cookie
  assert "samesite=strict" in cookie
  assert client.get("/api/v1/auth/me").json()["authenticated"] is True

  invalid_origin = client.post(
    "/api/v1/auth/logout",
    headers={"Origin": "https://attacker.invalid"},
  )
  assert invalid_origin.status_code == 403

  logged_out = client.post(
    "/api/v1/auth/logout",
    headers={"Origin": ORIGIN},
  )
  assert logged_out.status_code == 204
  assert "max-age=0" in logged_out.headers["set-cookie"].lower()
  assert client.get("/api/v1/auth/session").status_code == 401


def test_command_step_up_offroad_delivery_and_results(
  admin_client: TestClient,
) -> None:
  command_body = {
    "type": "reboot_device",
    "args": {"reason": "apply a verified system update"},
    "expires_in_seconds": 300,
  }
  without_step_up = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/commands",
    headers=admin_mutation_headers("command-reboot"),
    json=command_body,
  )
  assert without_step_up.status_code == 403
  assert without_step_up.json()["error"]["code"] == "password_confirmation_required"

  confirmation = admin_client.post(
    "/api/v1/auth/confirm",
    headers={"Origin": ORIGIN},
    json={"username": "admin", "password": ADMIN_PASSWORD},
  )
  assert confirmation.status_code == 200
  assert confirmation.json()["step_up_expires_at"] is not None

  advertised = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/heartbeat",
    headers=device_headers(),
    json={
      "offroad": False,
      "capabilities": ["command_reboot_device"],
    },
  )
  assert advertised.status_code == 200

  created = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/commands",
    headers=admin_mutation_headers("command-reboot"),
    json=command_body,
  )
  assert created.status_code == 202
  command = created.json()
  assert command["state"] == "queued"
  assert command["requires_offroad"] is True

  replay = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/commands",
    headers=admin_mutation_headers("command-reboot"),
    json=command_body,
  )
  assert replay.status_code == 202
  assert replay.json()["id"] == command["id"]

  changed_replay = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/commands",
    headers=admin_mutation_headers("command-reboot"),
    json={
      **command_body,
      "args": {"reason": "a different operation"},
    },
  )
  assert changed_replay.status_code == 409
  assert changed_replay.json()["error"]["code"] == "idempotency_key_reused"

  onroad = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/heartbeat",
    headers=device_headers(),
    json={
      "agent_version": "1.0.0",
      "state": "online",
      "offroad": False,
      "capabilities": [
        "resumable_uploads",
        "command_reboot_device",
      ],
      "metrics": {"upload_bps": 1000},
    },
  )
  assert onroad.status_code == 200
  assert onroad.json()["commands"] == []

  offroad = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/heartbeat",
    headers=device_headers(),
    json={
      "agent_version": "1.0.0",
      "state": "online",
      "offroad": True,
      "capabilities": [
        "resumable_uploads",
        "command_reboot_device",
      ],
      "metrics": {"upload_bps": 1000},
    },
  )
  assert offroad.status_code == 200
  assert [item["id"] for item in offroad.json()["commands"]] == [command["id"]]
  assert offroad.json()["commands"][0]["state"] == "delivered"

  running = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/commands/{command['id']}/result",
    headers=device_headers(idempotency_key="result-running"),
    json={"state": "running", "message": "reboot scheduled"},
  )
  assert running.status_code == 200
  assert running.json()["state"] == "running"

  succeeded = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/commands/{command['id']}/result",
    headers=device_headers(idempotency_key="result-finished"),
    json={"state": "succeeded", "message": "reboot requested"},
  )
  assert succeeded.status_code == 200
  assert succeeded.json()["state"] == "succeeded"
  assert succeeded.json()["finished_at"] is not None

  activity = admin_client.get("/api/v1/audit-events?limit=200")
  assert activity.status_code == 200
  actions = [item["action"] for item in activity.json()]
  assert "command.create" in actions
  assert "command.result" in actions
  assert "device.heartbeat" not in actions


def test_device_token_is_bound_to_device(admin_client: TestClient) -> None:
  confirmation = admin_client.post(
    "/api/v1/auth/confirm",
    headers={"Origin": ORIGIN},
    json={"password": ADMIN_PASSWORD},
  )
  assert confirmation.status_code == 200
  enrollment = admin_client.post(
    "/api/v1/devices",
    headers=admin_mutation_headers("enroll-second"),
    json={"device_id": "device-two", "display_name": "Second comma"},
  )
  assert enrollment.status_code == 201
  token = enrollment.json()["token"]
  mismatch = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/heartbeat",
    headers=device_headers(token=token),
    json={"offroad": True},
  )
  assert mismatch.status_code == 403
  assert mismatch.json()["error"]["code"] == "device_mismatch"


def test_device_enrollment_requires_password_confirmation(
  admin_client: TestClient,
) -> None:
  response = admin_client.post(
    "/api/v1/devices",
    headers=admin_mutation_headers("enroll-without-step-up"),
    json={"device_id": "device-two", "display_name": "Second comma"},
  )
  assert response.status_code == 403
  assert response.json()["error"]["code"] == "password_confirmation_required"


def test_disabled_configured_device_stays_revoked_across_bootstrap(
  admin_client: TestClient,
) -> None:
  database = admin_client.app.state.database
  initial = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/heartbeat",
    headers=device_headers(),
    json={"state": "online", "offroad": True},
  )
  assert initial.status_code == 200
  live_state = database.query_one(
    "SELECT last_seen_at FROM device_live_state WHERE device_id = ?",
    (DEVICE_ID,),
  )
  assert live_state is not None

  disabled_at = isoformat()
  database.execute(
    "UPDATE devices SET disabled_at = ? WHERE id = ?",
    (disabled_at, DEVICE_ID),
  )
  rejected = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/heartbeat",
    headers=device_headers(),
    json={"state": "online", "offroad": True},
  )
  assert rejected.status_code == 401
  assert rejected.json()["error"]["code"] == "invalid_device_token"

  rotated_token = "rotated-device-secret-that-stays-disabled"
  admin_client.app.state.settings.device_tokens[DEVICE_ID] = rotated_token
  bootstrap_configured_devices(admin_client.app)
  device = database.query_one(
    "SELECT disabled_at, token_hash FROM devices WHERE id = ?",
    (DEVICE_ID,),
  )
  assert device is not None
  assert device["disabled_at"] == disabled_at
  assert device["token_hash"] == admin_client.app.state.auth.hash_token(
    rotated_token,
  )

  still_rejected = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/heartbeat",
    headers=device_headers(token=rotated_token),
    json={"state": "online", "offroad": True},
  )
  assert still_rejected.status_code == 401
  assert still_rejected.json()["error"]["code"] == "invalid_device_token"
  unchanged_live_state = database.query_one(
    "SELECT last_seen_at FROM device_live_state WHERE device_id = ?",
    (DEVICE_ID,),
  )
  assert unchanged_live_state is not None
  assert unchanged_live_state["last_seen_at"] == live_state["last_seen_at"]
  assert admin_client.get(f"/api/v1/devices/{DEVICE_ID}").status_code == 404


def test_server_rejects_unadvertised_typed_command(
  admin_client: TestClient,
) -> None:
  response = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/commands",
    headers=admin_mutation_headers("unsupported-status"),
    json={"type": "status", "args": {}},
  )
  assert response.status_code == 409
  assert response.json()["error"]["code"] == "command_not_supported"
  assert response.json()["error"]["details"] == {
    "command_type": "status",
    "required_capability": "command_status",
  }


def test_unsafe_command_shapes_are_rejected(admin_client: TestClient) -> None:
  rejected = admin_client.post(
    f"/api/v1/devices/{DEVICE_ID}/commands",
    headers=admin_mutation_headers("unsafe-command"),
    json={
      "type": "upload_files",
      "args": {"paths": ["/data/params/d/ControlsReady"]},
    },
  )
  assert rejected.status_code == 422
  assert rejected.json()["error"]["code"] == "validation_error"
