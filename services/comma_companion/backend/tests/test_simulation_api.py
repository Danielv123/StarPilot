from __future__ import annotations

import json

import comma_companion.app as app_module
from comma_companion.db import isoformat
from conftest import ORIGIN


MODEL_HASH = "a" * 64
TELEMETRY_HASH = "b" * 64
TIMELINE_VERSION = "c" * 64


def _seed_model_and_drive(database) -> None:
  schema = [{
    "name": "friction",
    "type": "number",
    "minimum": 0.0,
    "maximum": 1.0,
    "default": 0.1,
    "label": "Friction",
    "category": "Controller",
    "step": 0.01,
    "advanced": False,
    "runtime_supported": True,
    "scope": "controller",
    "units": "normalized",
  }]
  metadata = {
    "baseline_params": {"friction": 0.9},
    "causal_training_eligible": True,
    "capabilities": {"available": True},
  }
  now = isoformat()
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO drives(id, device_id, route_name, created_at)
      VALUES ('drive-one', 'device-one', 'route-one', ?)
      """,
      (now,),
    )
    connection.execute(
      """
      INSERT INTO model_registry(
        sha256, name, enabled, mode, parameter_schema_json,
        metadata_json, created_at
      ) VALUES (?, 'Causal model', 1, 'approximate_closed_loop', ?, ?, ?)
      """,
      (
        MODEL_HASH,
        json.dumps(schema, separators=(",", ":"), sort_keys=True),
        json.dumps(metadata, separators=(",", ":"), sort_keys=True),
        now,
      ),
    )


def _request(**overrides):
  return {
    "t_us": 4_000_000,
    "horizon_us": 1_000_000,
    "model_hash": MODEL_HASH,
    "telemetry_sha256": TELEMETRY_HASH,
    "timeline_version": TIMELINE_VERSION,
    "mode": "approximate_closed_loop",
    "parameters": {"friction": 0.2},
    **overrides,
  }


def test_simulation_requires_and_persists_exact_generation_snapshot(
  admin_client,
  monkeypatch,
) -> None:
  database = admin_client.app.state.database
  _seed_model_and_drive(database)

  def eligible(*_args, **_kwargs):
    return {
      "eligible": True,
      "reasons": [],
      "telemetry_generation": {
        "ndjson_sha256": TELEMETRY_HASH,
        "timeline_version": TIMELINE_VERSION,
      },
      "model": {"sha256": MODEL_HASH},
      "baseline_params": {"friction": 0.1},
    }

  monkeypatch.setattr(
    app_module,
    "evaluate_simulation_eligibility",
    eligible,
  )

  missing_pin = admin_client.post(
    "/api/v1/drives/drive-one/simulations",
    headers={"Origin": ORIGIN, "Idempotency-Key": "missing-pin"},
    json={
      key: value
      for key, value in _request().items()
      if key != "timeline_version"
    },
  )
  assert missing_pin.status_code == 422

  stale = admin_client.post(
    "/api/v1/drives/drive-one/simulations",
    headers={"Origin": ORIGIN, "Idempotency-Key": "stale-pin"},
    json=_request(telemetry_sha256="d" * 64),
  )
  assert stale.status_code == 409
  assert stale.json()["error"]["code"] == "telemetry_generation_changed"

  accepted = admin_client.post(
    "/api/v1/drives/drive-one/simulations",
    headers={"Origin": ORIGIN, "Idempotency-Key": "exact-generation"},
    json=_request(),
  )
  assert accepted.status_code == 202, accepted.text
  job_id = accepted.json()["job_id"]
  simulation_id = accepted.json()["id"]
  job = database.query_one(
    "SELECT payload_json FROM jobs WHERE id = ?",
    (job_id,),
  )
  payload = json.loads(job["payload_json"])
  assert payload["baseline_params"] == {"friction": 0.1}
  assert payload["telemetry_sha256"] == TELEMETRY_HASH
  assert payload["timeline_version"] == TIMELINE_VERSION

  stored = database.query_one(
    """
    SELECT baseline_parameters_json, telemetry_sha256
    FROM simulation_requests
    WHERE id = ?
    """,
    (simulation_id,),
  )
  assert json.loads(stored["baseline_parameters_json"]) == {
    "friction": 0.1,
  }
  assert stored["telemetry_sha256"] == TELEMETRY_HASH

  view = admin_client.get(f"/api/v1/simulations/{simulation_id}")
  assert view.status_code == 200
  assert view.json()["baseline_parameters"] == {"friction": 0.1}
  assert view.json()["telemetry_sha256"] == TELEMETRY_HASH
  assert view.json()["timeline_version"] == TIMELINE_VERSION


def test_simulation_preflight_returns_structured_concrete_reason(
  admin_client,
  monkeypatch,
) -> None:
  database = admin_client.app.state.database
  _seed_model_and_drive(database)

  monkeypatch.setattr(
    app_module,
    "evaluate_simulation_eligibility",
    lambda *_args, **_kwargs: {
      "eligible": False,
      "reasons": [{
        "code": "full_rlog_required",
        "message": "Simulation requires full rlog telemetry.",
        "details": {"log_type": "qlog"},
      }],
      "telemetry_generation": {
        "ndjson_sha256": TELEMETRY_HASH,
        "timeline_version": TIMELINE_VERSION,
      },
    },
  )
  response = admin_client.post(
    "/api/v1/drives/drive-one/simulations",
    headers={"Origin": ORIGIN, "Idempotency-Key": "qlog-blocked"},
    json=_request(),
  )
  assert response.status_code == 409
  assert response.json()["error"]["code"] == "full_rlog_required"
  assert response.json()["error"]["details"]["reasons"] == [{
    "code": "full_rlog_required",
    "message": "Simulation requires full rlog telemetry.",
    "details": {"log_type": "qlog"},
  }]


def test_capabilities_project_adapter_list_to_json_schema(
  admin_client,
) -> None:
  _seed_model_and_drive(admin_client.app.state.database)
  response = admin_client.get("/api/v1/simulator/capabilities")
  assert response.status_code == 200, response.text
  schema = response.json()["parameter_schema"]
  assert schema["type"] == "object"
  assert schema["additionalProperties"] is False
  assert schema["properties"]["friction"] == {
    "type": "number",
    "minimum": 0.0,
    "maximum": 1.0,
    "default": 0.1,
    "title": "Friction",
    "multipleOf": 0.01,
    "x-runtime-supported": True,
    "x-scope": "controller",
    "x-units": "normalized",
    "x-group": "Controller",
    "x-advanced": False,
  }
