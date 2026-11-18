from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request, status

from .auth import ApiError, audit, request_ip, require_idempotency_key
from .db import isoformat, utc_now
from .models import CommandCreate, CommandResultRequest, CommandView


REQUIRES_OFFROAD = {
  "status": False,
  "rescan": False,
  "pause": False,
  "resume": False,
  "retry_upload": False,
  "cancel_upload": False,
  "restart_agent": False,
  "restart_starpilot": True,
  "reboot_device": True,
  "shutdown_device": True,
}

REQUIRES_STEP_UP = {"restart_starpilot", "reboot_device", "shutdown_device"}
TERMINAL_STATES = {"succeeded", "failed", "rejected", "expired", "canceled"}
COMMAND_CAPABILITIES = {
  command_type: f"command_{command_type}"
  for command_type in REQUIRES_OFFROAD
}


def command_view(row: sqlite3.Row) -> dict[str, Any]:
  return {
    "id": row["id"],
    "device_id": row["device_id"],
    "type": row["type"],
    "args": json.loads(row["args_json"]),
    "state": row["state"],
    "requires_offroad": bool(row["requires_offroad"]),
    "issued_at": row["issued_at"],
    "expires_at": row["expires_at"],
    "delivered_at": row["delivered_at"],
    "started_at": row["started_at"],
    "finished_at": row["finished_at"],
    "message": row["message"],
    "error": row["error"],
  }


router = APIRouter(prefix="/devices/{device_id}/commands", tags=["commands"])


@router.post("", response_model=CommandView, status_code=status.HTTP_202_ACCEPTED)
def create_command(
  request: Request,
  device_id: str,
  payload: CommandCreate,
) -> dict[str, Any]:
  auth = request.app.state.auth
  auth.require_origin(request)
  principal = auth.authenticate_admin(request)
  idempotency_key = require_idempotency_key(request)
  if payload.type in REQUIRES_STEP_UP:
    auth.require_step_up(principal)

  canonical = json.dumps(
    {
      "operation": "command.create",
      "device_id": device_id,
      **payload.model_dump(mode="json"),
    },
    separators=(",", ":"),
    sort_keys=True,
  )
  request_hash = hashlib.sha256(canonical.encode()).hexdigest()
  database = request.app.state.database
  now = utc_now()
  command_id = uuid4().hex
  with database.transaction(immediate=True) as connection:
    device = connection.execute(
      """
      SELECT d.id, s.capabilities_json
      FROM devices d
      LEFT JOIN device_live_state s ON s.device_id = d.id
      WHERE d.id = ? AND d.disabled_at IS NULL
      """,
      (device_id,),
    ).fetchone()
    if device is None:
      raise ApiError(404, "device_not_found", "Device was not found")
    prior_key = connection.execute(
      """
      SELECT request_hash
      FROM idempotency_keys
      WHERE actor_type = 'admin' AND actor_id = 'admin' AND key = ?
      """,
      (idempotency_key,),
    ).fetchone()
    prior = connection.execute(
      """
      SELECT c.*, i.request_hash
      FROM commands c
      JOIN idempotency_keys i
        ON i.actor_type = 'admin'
        AND i.actor_id = 'admin'
        AND i.key = c.idempotency_key
      WHERE c.device_id = ? AND c.idempotency_key = ?
      """,
      (device_id, idempotency_key),
    ).fetchone()
    if prior_key is not None:
      if (
        prior is None
        or not secrets_compare(prior_key["request_hash"], request_hash)
      ):
        raise ApiError(
          409,
          "idempotency_key_reused",
          "Idempotency key was already used with a different request",
        )
      return command_view(prior)
    try:
      capabilities = json.loads(
        device["capabilities_json"] or "[]",
      )
    except json.JSONDecodeError:
      capabilities = []
    required_capability = COMMAND_CAPABILITIES[payload.type]
    if (
      not isinstance(capabilities, list)
      or required_capability not in capabilities
    ):
      raise ApiError(
        409,
        "command_not_supported",
        "The device did not advertise support for this command",
        details={
          "command_type": payload.type,
          "required_capability": required_capability,
        },
      )

    expires = now + timedelta(seconds=payload.expires_in_seconds)
    args_json = json.dumps(
      payload.args.model_dump(mode="json"),
      separators=(",", ":"),
      sort_keys=True,
    )
    connection.execute(
      """
      INSERT INTO idempotency_keys(
        actor_type, actor_id, key, request_hash, created_at
      ) VALUES ('admin', 'admin', ?, ?, ?)
      """,
      (idempotency_key, request_hash, isoformat(now)),
    )
    connection.execute(
      """
      INSERT INTO commands(
        id, device_id, idempotency_key, type, args_json, state,
        requires_offroad, issued_at, expires_at
      ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)
      """,
      (
        command_id,
        device_id,
        idempotency_key,
        payload.type,
        args_json,
        int(REQUIRES_OFFROAD[payload.type]),
        isoformat(now),
        isoformat(expires),
      ),
    )
    audit(
      connection,
      actor_type=principal.actor_type,
      actor_id=principal.actor_id,
      action="command.create",
      resource_type="command",
      resource_id=command_id,
      details={
        "device_id": device_id,
        "type": payload.type,
        "requires_offroad": REQUIRES_OFFROAD[payload.type],
        "idempotency_key": idempotency_key,
      },
      ip_address=request_ip(request),
    )
    row = connection.execute(
      "SELECT * FROM commands WHERE id = ?",
      (command_id,),
    ).fetchone()
  assert row is not None
  return command_view(row)


def secrets_compare(left: str, right: str) -> bool:
  import secrets

  return secrets.compare_digest(left, right)


@router.get("", response_model=list[CommandView])
def list_commands(
  request: Request,
  device_id: str,
  state: str | None = None,
  limit: int = 100,
) -> list[dict[str, Any]]:
  request.app.state.auth.authenticate_admin(request)
  if limit < 1 or limit > 500:
    raise ApiError(422, "invalid_limit", "limit must be between 1 and 500")
  parameters: list[Any] = [device_id]
  where = "WHERE device_id = ?"
  if state:
    if state not in {
      "queued",
      "delivered",
      "running",
      "succeeded",
      "failed",
      "rejected",
      "expired",
      "canceled",
    }:
      raise ApiError(422, "invalid_command_state", "Command state is invalid")
    where += " AND state = ?"
    parameters.append(state)
  parameters.append(limit)
  rows = request.app.state.database.query_all(
    f"""
    SELECT *
    FROM commands
    {where}
    ORDER BY issued_at DESC, id DESC
    LIMIT ?
    """,
    parameters,
  )
  return [command_view(row) for row in rows]


@router.get("/{command_id}", response_model=CommandView)
def get_command(
  request: Request,
  device_id: str,
  command_id: str,
) -> dict[str, Any]:
  request.app.state.auth.authenticate_admin(request)
  row = request.app.state.database.query_one(
    """
    SELECT *
    FROM commands
    WHERE id = ? AND device_id = ?
    """,
    (command_id, device_id),
  )
  if row is None:
    raise ApiError(404, "command_not_found", "Command was not found")
  return command_view(row)


@router.post("/{command_id}/result", response_model=CommandView)
def command_result(
  request: Request,
  device_id: str,
  command_id: str,
  payload: CommandResultRequest,
) -> dict[str, Any]:
  principal = request.app.state.auth.authenticate_device(
    request,
    expected_device_id=device_id,
  )
  idempotency_key = require_idempotency_key(request)
  canonical = json.dumps(
    {
      "operation": "command.result",
      "device_id": device_id,
      "command_id": command_id,
      **payload.model_dump(mode="json"),
    },
    separators=(",", ":"),
    sort_keys=True,
  )
  result_json = json.dumps(
    payload.result,
    allow_nan=False,
    separators=(",", ":"),
    sort_keys=True,
  )
  if len(result_json.encode()) > 256 * 1024:
    raise ApiError(413, "command_result_too_large", "Command result is too large")
  request_hash = hashlib.sha256(canonical.encode()).hexdigest()
  database = request.app.state.database
  now_text = isoformat()
  with database.transaction(immediate=True) as connection:
    row = connection.execute(
      "SELECT * FROM commands WHERE id = ? AND device_id = ?",
      (command_id, device_id),
    ).fetchone()
    if row is None:
      raise ApiError(404, "command_not_found", "Command was not found")
    prior = connection.execute(
      """
      SELECT request_hash
      FROM idempotency_keys
      WHERE actor_type = 'device' AND actor_id = ? AND key = ?
      """,
      (device_id, idempotency_key),
    ).fetchone()
    if prior is not None:
      if not secrets_compare(prior["request_hash"], request_hash):
        raise ApiError(
          409,
          "idempotency_key_reused",
          "Idempotency key was already used with a different result",
        )
      return command_view(row)
    if row["state"] in TERMINAL_STATES:
      raise ApiError(
        409,
        "command_already_finished",
        "Command already has a terminal result",
        details={"state": row["state"]},
      )
    if payload.state == "running" and row["state"] not in {"queued", "delivered", "running"}:
      raise ApiError(409, "invalid_command_transition", "Command cannot enter running state")

    started_at = (
      isoformat(payload.started_at)
      if payload.started_at
      else row["started_at"] or (now_text if payload.state == "running" else None)
    )
    finished_at = (
      isoformat(payload.finished_at)
      if payload.finished_at
      else (now_text if payload.state in TERMINAL_STATES else None)
    )
    connection.execute(
      """
      UPDATE commands
      SET state = ?, started_at = COALESCE(?, started_at),
        finished_at = ?, message = ?, error = ?, result_json = ?
      WHERE id = ?
      """,
      (
        payload.state,
        started_at,
        finished_at,
        payload.message,
        payload.error,
        result_json,
        command_id,
      ),
    )
    connection.execute(
      """
      INSERT INTO idempotency_keys(
        actor_type, actor_id, key, request_hash, status_code,
        response_json, created_at
      ) VALUES ('device', ?, ?, ?, 200, '{}', ?)
      """,
      (device_id, idempotency_key, request_hash, now_text),
    )
    audit(
      connection,
      actor_type=principal.actor_type,
      actor_id=principal.actor_id,
      action="command.result",
      resource_type="command",
      resource_id=command_id,
      details={"state": payload.state},
      ip_address=request_ip(request),
    )
    updated = connection.execute(
      "SELECT * FROM commands WHERE id = ?",
      (command_id,),
    ).fetchone()
  assert updated is not None
  return command_view(updated)
