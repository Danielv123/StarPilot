from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request, status

from .auth import (
  ApiError,
  Principal,
  audit,
  request_ip,
  require_idempotency_key,
)
from .db import isoformat, utc_now
from .models import (
  DeviceCreate,
  DeviceEnrollmentView,
  DeviceView,
  HeartbeatRequest,
  HeartbeatResponse,
)


DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _device_view(
  row: sqlite3.Row,
  *,
  online_window_seconds: int,
) -> dict[str, Any]:
  last_seen = row["last_seen_at"]
  online = False
  if last_seen:
    from datetime import datetime

    seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
    online = seen >= utc_now() - timedelta(seconds=online_window_seconds)
  return {
    "id": row["id"],
    "display_name": row["display_name"],
    "enrolled_at": row["enrolled_at"],
    "last_seen_at": last_seen,
    "online": online,
    "offroad": None if row["offroad"] is None else bool(row["offroad"]),
    "agent_version": row["agent_version"],
    "software_version": row["software_version"],
    "network_type": row["network_type"],
    "state": row["state"],
    "capabilities": json.loads(row["capabilities_json"] or "[]"),
    "metrics": json.loads(row["metrics_json"] or "{}"),
  }


DEVICE_SELECT = """
  SELECT
    d.id, d.display_name, d.enrolled_at,
    s.last_seen_at, s.offroad, s.agent_version, s.software_version,
    s.network_type, s.state, s.capabilities_json, s.metrics_json
  FROM devices d
  LEFT JOIN device_live_state s ON s.device_id = d.id
"""


def bootstrap_configured_devices(request_or_app: Any) -> None:
  """Enroll configured credentials without changing administrative disable state."""
  state = request_or_app.state
  settings = state.settings
  auth = state.auth
  database = state.database
  now = isoformat()
  with database.transaction(immediate=True) as connection:
    for device_id, token in settings.device_tokens.items():
      if not DEVICE_ID_PATTERN.fullmatch(device_id):
        raise ValueError(f"invalid configured device ID: {device_id!r}")
      connection.execute(
        """
        INSERT INTO devices(id, display_name, token_hash, enrolled_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          token_hash = excluded.token_hash
        """,
        (device_id, device_id, auth.hash_token(token), now),
      )


def _admin(request: Request, *, mutation: bool = False) -> Principal:
  auth = request.app.state.auth
  if mutation:
    auth.require_origin(request)
  return auth.authenticate_admin(request)


router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("", response_model=DeviceEnrollmentView, status_code=status.HTTP_201_CREATED)
def enroll_device(request: Request, payload: DeviceCreate) -> dict[str, Any]:
  principal = _admin(request, mutation=True)
  request.app.state.auth.require_step_up(principal)
  idempotency_key = require_idempotency_key(request)
  device_id = payload.device_id or uuid4().hex
  if not DEVICE_ID_PATTERN.fullmatch(device_id):
    raise ApiError(
      422,
      "invalid_device_id",
      "Device ID must contain only letters, numbers, dots, underscores, and hyphens",
    )
  token = secrets.token_urlsafe(32)
  token_hash = request.app.state.auth.hash_token(token)
  database = request.app.state.database
  now = isoformat()
  with database.transaction(immediate=True) as connection:
    existing_idempotency = connection.execute(
      """
      SELECT resource_id, details_json
      FROM audit_events
      WHERE actor_type = 'admin'
        AND action = 'device.enroll'
        AND json_extract(details_json, '$.idempotency_key') = ?
      ORDER BY id DESC LIMIT 1
      """,
      (idempotency_key,),
    ).fetchone()
    if existing_idempotency is not None:
      raise ApiError(
        409,
        "idempotency_replay_not_available",
        "The device was already enrolled; its bearer token cannot be shown again",
        details={"device_id": existing_idempotency["resource_id"]},
      )
    try:
      connection.execute(
        """
        INSERT INTO devices(id, display_name, token_hash, enrolled_at)
        VALUES (?, ?, ?, ?)
        """,
        (device_id, payload.display_name, token_hash, now),
      )
    except sqlite3.IntegrityError as exc:
      raise ApiError(409, "device_exists", "Device ID is already enrolled") from exc
    audit(
      connection,
      actor_type=principal.actor_type,
      actor_id=principal.actor_id,
      action="device.enroll",
      resource_type="device",
      resource_id=device_id,
      details={"idempotency_key": idempotency_key},
      ip_address=request_ip(request),
    )
  row = database.query_one(f"{DEVICE_SELECT} WHERE d.id = ?", (device_id,))
  assert row is not None
  return {
    "device": _device_view(
      row,
      online_window_seconds=request.app.state.settings.online_window_seconds,
    ),
    "token": token,
  }


@router.get("", response_model=list[DeviceView])
def list_devices(request: Request) -> list[dict[str, Any]]:
  _admin(request)
  rows = request.app.state.database.query_all(
    f"{DEVICE_SELECT} WHERE d.disabled_at IS NULL ORDER BY d.display_name, d.id",
  )
  return [
    _device_view(
      row,
      online_window_seconds=request.app.state.settings.online_window_seconds,
    )
    for row in rows
  ]


@router.get("/{device_id}", response_model=DeviceView)
def get_device(request: Request, device_id: str) -> dict[str, Any]:
  _admin(request)
  row = request.app.state.database.query_one(
    f"{DEVICE_SELECT} WHERE d.id = ? AND d.disabled_at IS NULL",
    (device_id,),
  )
  if row is None:
    raise ApiError(404, "device_not_found", "Device was not found")
  return _device_view(
    row,
    online_window_seconds=request.app.state.settings.online_window_seconds,
  )


@router.post("/{device_id}/heartbeat", response_model=HeartbeatResponse)
def heartbeat(
  request: Request,
  device_id: str,
  payload: HeartbeatRequest,
) -> dict[str, Any]:
  request.app.state.auth.authenticate_device(
    request,
    expected_device_id=device_id,
  )
  database = request.app.state.database
  now = utc_now()
  now_text = isoformat(now)
  metrics = dict(payload.metrics)
  aliases = {
    "upload_bytes_per_second": "upload_bps",
    "storage_free_bytes": "free_space_bytes",
    "metered": "network_metered",
  }
  for source, target in aliases.items():
    if source in metrics and target not in metrics:
      metrics[target] = metrics[source]
  try:
    metrics_json = json.dumps(
      metrics,
      allow_nan=False,
      separators=(",", ":"),
      sort_keys=True,
    )
  except (TypeError, ValueError) as exc:
    raise ApiError(422, "invalid_metrics", "Heartbeat metrics are not valid JSON") from exc
  if len(metrics_json.encode()) > 256 * 1024:
    raise ApiError(413, "metrics_too_large", "Heartbeat metrics are too large")
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO device_live_state(
        device_id, last_seen_at, device_timestamp, state, offroad,
        agent_version, software_version, network_type,
        capabilities_json, metrics_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(device_id) DO UPDATE SET
        last_seen_at = excluded.last_seen_at,
        device_timestamp = excluded.device_timestamp,
        state = excluded.state,
        offroad = excluded.offroad,
        agent_version = excluded.agent_version,
        software_version = excluded.software_version,
        network_type = excluded.network_type,
        capabilities_json = excluded.capabilities_json,
        metrics_json = excluded.metrics_json
      """,
      (
        device_id,
        now_text,
        isoformat(payload.timestamp) if payload.timestamp else None,
        payload.state,
        None if payload.offroad is None else int(payload.offroad),
        payload.agent_version,
        payload.software_version,
        payload.network_type,
        json.dumps(payload.capabilities, separators=(",", ":")),
        metrics_json,
      ),
    )
    connection.execute(
      """
      UPDATE commands
      SET state = 'expired', finished_at = ?
      WHERE device_id = ?
        AND state IN ('queued', 'delivered')
        AND expires_at <= ?
      """,
      (now_text, device_id, now_text),
    )
    rows = connection.execute(
      """
      SELECT *
      FROM commands
      WHERE device_id = ?
        AND state IN ('queued', 'delivered')
        AND expires_at > ?
        AND (requires_offroad = 0 OR ? = 1)
        AND EXISTS (
          SELECT 1
          FROM json_each(?) capability
          WHERE capability.value = 'command_' || commands.type
        )
      ORDER BY issued_at, id
      LIMIT ?
      """,
      (
        device_id,
        now_text,
        int(payload.offroad is True),
        json.dumps(payload.capabilities, separators=(",", ":")),
        request.app.state.settings.command_poll_limit,
      ),
    ).fetchall()
    delivered_ids = [row["id"] for row in rows if row["state"] == "queued"]
    if delivered_ids:
      placeholders = ",".join("?" for _ in delivered_ids)
      connection.execute(
        f"""
        UPDATE commands
        SET state = 'delivered', delivered_at = COALESCE(delivered_at, ?)
        WHERE id IN ({placeholders})
        """,
        (now_text, *delivered_ids),
      )
  from .commands import command_view

  commands = []
  for row in rows:
    item = command_view(row)
    if item["state"] == "queued":
      item["state"] = "delivered"
      item["delivered_at"] = now_text
    commands.append(item)
  return {"server_time": now, "commands": commands}
