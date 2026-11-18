from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import sqlite3
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer

from .config import Settings
from .db import Database, isoformat, utc_now


class ApiError(Exception):
  def __init__(
    self,
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
  ):
    super().__init__(message)
    self.status_code = status_code
    self.code = code
    self.message = message
    self.details = details or {}
    self.headers = headers or {}


@dataclass(frozen=True, slots=True)
class Principal:
  actor_type: str
  actor_id: str
  session_id_hash: str | None = None


def request_ip(request: Request) -> str | None:
  # Uvicorn normalizes request.client only for explicitly trusted proxies.
  # Never trust a directly supplied forwarding header here.
  return request.client.host if request.client else None


def _request_is_allowed_importer_client(
  request: Request,
  allowed_clients: tuple[str, ...],
) -> bool:
  host = request_ip(request)
  if host is None:
    return False
  try:
    address = ipaddress.ip_address(host.split("%", 1)[0])
    allowed = {
      str(ipaddress.ip_address(value))
      for value in allowed_clients
    }
  except ValueError:
    return False
  if str(address) in allowed:
    return True
  mapped = getattr(address, "ipv4_mapped", None)
  return bool(mapped is not None and str(mapped) in allowed)


def audit(
  target: Database | sqlite3.Connection,
  *,
  actor_type: str,
  actor_id: str | None,
  action: str,
  resource_type: str | None = None,
  resource_id: str | None = None,
  details: dict[str, Any] | None = None,
  ip_address: str | None = None,
) -> None:
  parameters = (
    actor_type,
    actor_id,
    action,
    resource_type,
    resource_id,
    json.dumps(details or {}, separators=(",", ":"), sort_keys=True),
    ip_address,
    isoformat(),
  )
  sql = """
    INSERT INTO audit_events(
      actor_type, actor_id, action, resource_type, resource_id,
      details_json, ip_address, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
  """
  if isinstance(target, Database):
    target.execute(sql, parameters)
  else:
    target.execute(sql, parameters)


class AuthService:
  def __init__(self, settings: Settings, database: Database):
    self.settings = settings
    self.database = database
    self.password_hasher = PasswordHasher(
      time_cost=3,
      memory_cost=65_536,
      parallelism=2,
      hash_len=32,
      salt_len=16,
    )
    self.serializer = URLSafeTimedSerializer(
      settings.session_secret,
      salt="comma-companion-admin-session-v1",
    )
    self._admin_hash = settings.admin_password_hash
    if not self._admin_hash and settings.admin_password:
      self._admin_hash = self.password_hasher.hash(settings.admin_password)
    self._attempts: dict[str, deque[datetime]] = defaultdict(deque)
    self._global_attempts: deque[datetime] = deque()
    self._inflight_attempts: dict[str, int] = defaultdict(int)
    self._attempt_lock = threading.Lock()
    self._password_slots = threading.BoundedSemaphore(
      settings.password_hash_concurrency,
    )

  @property
  def admin_configured(self) -> bool:
    return bool(self._admin_hash)

  def hash_token(self, token: str) -> str:
    return hmac.new(
      self.settings.session_secret.encode(),
      token.encode(),
      hashlib.sha256,
    ).hexdigest()

  def verify_password(self, password: str) -> bool:
    if not self._admin_hash:
      raise ApiError(
        503,
        "admin_not_configured",
        "Administrator login is not configured",
      )
    try:
      return self.password_hasher.verify(self._admin_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
      return False

  def _reserve_password_attempt(self, key: str) -> None:
    if not self._password_slots.acquire(blocking=False):
      raise ApiError(
        429,
        "password_verification_busy",
        "Password verification is temporarily busy",
        headers={"Retry-After": "1"},
      )
    now = utc_now()
    cutoff = now - timedelta(minutes=15)
    try:
      with self._attempt_lock:
        attempts = self._attempts.get(key)
        if attempts is None:
          attempts = deque()
        while attempts and attempts[0] < cutoff:
          attempts.popleft()
        if not attempts:
          self._attempts.pop(key, None)
        while self._global_attempts and self._global_attempts[0] < cutoff:
          self._global_attempts.popleft()
        inflight = self._inflight_attempts.get(key, 0)
        if len(attempts) + inflight >= 10:
          retry_after = max(
            1,
            int(
              (
                attempts[0]
                + timedelta(minutes=15)
                - now
              ).total_seconds()
            ),
          )
          raise ApiError(
            429,
            "login_rate_limited",
            "Too many failed login attempts",
            headers={"Retry-After": str(retry_after)},
          )
        if len(self._global_attempts) >= 200:
          retry_after = max(
            1,
            int(
              (
                self._global_attempts[0]
                + timedelta(minutes=15)
                - now
              ).total_seconds()
            ),
          )
          raise ApiError(
            429,
            "login_rate_limited",
            "Login is temporarily rate limited",
            headers={"Retry-After": str(retry_after)},
          )
        self._inflight_attempts[key] += 1
        self._global_attempts.append(now)
    except BaseException:
      self._password_slots.release()
      raise

  def verify_password_admitted(
    self,
    password: str,
    key: str,
    *,
    additional_valid: bool = True,
  ) -> bool:
    self._reserve_password_attempt(key)
    password_valid = False
    try:
      password_valid = self.verify_password(password)
      return password_valid
    finally:
      with self._attempt_lock:
        self._inflight_attempts[key] -= 1
        if self._inflight_attempts[key] <= 0:
          self._inflight_attempts.pop(key, None)
        if not password_valid or not additional_valid:
          self._attempts[key].append(utc_now())
          if len(self._attempts) > 10_000:
            nonempty = {
              item: attempts
              for item, attempts in self._attempts.items()
              if attempts
            }
            oldest_key = min(
              nonempty,
              key=lambda item: nonempty[item][-1],
            )
            if oldest_key != key:
              self._attempts.pop(oldest_key, None)
      self._password_slots.release()

  def create_session(
    self,
    *,
    ip_address: str | None,
    user_agent: str | None,
  ) -> tuple[str, dict[str, Any]]:
    raw_id = secrets.token_urlsafe(32)
    id_hash = self.hash_token(raw_id)
    now = utc_now()
    expires = now + timedelta(seconds=self.settings.session_ttl_seconds)
    with self.database.transaction(immediate=True) as connection:
      connection.execute(
        """
        INSERT INTO admin_sessions(
          id_hash, created_at, expires_at, last_seen_at, ip_address, user_agent
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
          id_hash,
          isoformat(now),
          isoformat(expires),
          isoformat(now),
          ip_address,
          (user_agent or "")[:1024],
        ),
      )
      audit(
        connection,
        actor_type="admin",
        actor_id="admin",
        action="auth.login",
        resource_type="session",
        resource_id=id_hash[:16],
        ip_address=ip_address,
      )
    signed = self.serializer.dumps({"sid": raw_id})
    return signed, {
      "authenticated": True,
      "username": self.settings.admin_username,
      "expires_at": expires,
      "step_up_expires_at": None,
    }

  def authenticate_admin(self, request: Request) -> Principal:
    signed = request.cookies.get(self.settings.cookie_name)
    if not signed:
      raise ApiError(401, "authentication_required", "Administrator login required")
    try:
      payload = self.serializer.loads(
        signed,
        max_age=self.settings.session_ttl_seconds,
      )
    except SignatureExpired as exc:
      raise ApiError(401, "session_expired", "Administrator session expired") from exc
    except BadData as exc:
      raise ApiError(401, "invalid_session", "Administrator session is invalid") from exc
    raw_id = payload.get("sid") if isinstance(payload, dict) else None
    if not isinstance(raw_id, str):
      raise ApiError(401, "invalid_session", "Administrator session is invalid")
    id_hash = self.hash_token(raw_id)
    row = self.database.query_one(
      """
      SELECT expires_at, revoked_at
      FROM admin_sessions
      WHERE id_hash = ?
      """,
      (id_hash,),
    )
    if row is None or row["revoked_at"] is not None:
      raise ApiError(401, "invalid_session", "Administrator session is invalid")
    expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if expires <= utc_now():
      raise ApiError(401, "session_expired", "Administrator session expired")
    self.database.execute(
      "UPDATE admin_sessions SET last_seen_at = ? WHERE id_hash = ?",
      (isoformat(), id_hash),
    )
    return Principal("admin", "admin", id_hash)

  def session_view(self, principal: Principal) -> dict[str, Any]:
    if principal.session_id_hash is None:
      raise ApiError(401, "authentication_required", "Administrator login required")
    row = self.database.query_one(
      """
      SELECT expires_at, step_up_expires_at
      FROM admin_sessions
      WHERE id_hash = ? AND revoked_at IS NULL
      """,
      (principal.session_id_hash,),
    )
    if row is None:
      raise ApiError(401, "invalid_session", "Administrator session is invalid")
    return {
      "authenticated": True,
      "username": self.settings.admin_username,
      "expires_at": row["expires_at"],
      "step_up_expires_at": row["step_up_expires_at"],
    }

  def revoke_session(self, principal: Principal, ip_address: str | None) -> None:
    if principal.session_id_hash is None:
      return
    with self.database.transaction(immediate=True) as connection:
      connection.execute(
        """
        UPDATE admin_sessions
        SET revoked_at = ?
        WHERE id_hash = ? AND revoked_at IS NULL
        """,
        (isoformat(), principal.session_id_hash),
      )
      audit(
        connection,
        actor_type="admin",
        actor_id="admin",
        action="auth.logout",
        resource_type="session",
        resource_id=principal.session_id_hash[:16],
        ip_address=ip_address,
      )

  def confirm_password(
    self,
    principal: Principal,
    password: str,
    ip_address: str | None,
    *,
    username_valid: bool = True,
  ) -> datetime:
    if principal.session_id_hash is None:
      raise ApiError(401, "authentication_required", "Administrator login required")
    key = f"confirm:{principal.session_id_hash}:{ip_address or 'unknown'}"
    password_valid = self.verify_password_admitted(
      password,
      key,
      additional_valid=username_valid,
    )
    if not username_valid or not password_valid:
      audit(
        self.database,
        actor_type="admin",
        actor_id="admin",
        action="auth.step_up_failed",
        resource_type="session",
        resource_id=principal.session_id_hash[:16],
        ip_address=ip_address,
      )
      raise ApiError(401, "invalid_password", "Password confirmation failed")
    expires = utc_now() + timedelta(seconds=self.settings.step_up_ttl_seconds)
    with self.database.transaction(immediate=True) as connection:
      connection.execute(
        "UPDATE admin_sessions SET step_up_expires_at = ? WHERE id_hash = ?",
        (isoformat(expires), principal.session_id_hash),
      )
      audit(
        connection,
        actor_type="admin",
        actor_id="admin",
        action="auth.step_up",
        resource_type="session",
        resource_id=principal.session_id_hash[:16],
        ip_address=ip_address,
      )
    return expires

  def require_step_up(self, principal: Principal) -> None:
    if principal.session_id_hash is None:
      raise ApiError(401, "authentication_required", "Administrator login required")
    row = self.database.query_one(
      "SELECT step_up_expires_at FROM admin_sessions WHERE id_hash = ?",
      (principal.session_id_hash,),
    )
    if row is None or row["step_up_expires_at"] is None:
      raise ApiError(
        403,
        "password_confirmation_required",
        "A fresh password confirmation is required",
      )
    expires = datetime.fromisoformat(
      row["step_up_expires_at"].replace("Z", "+00:00"),
    )
    if expires <= utc_now():
      raise ApiError(
        403,
        "password_confirmation_required",
        "A fresh password confirmation is required",
      )

  def authenticate_device(
    self,
    request: Request,
    *,
    expected_device_id: str | None = None,
    allow_importer: bool = False,
  ) -> Principal:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
      raise ApiError(
        401,
        "device_authentication_required",
        "A device bearer token is required",
        headers={"WWW-Authenticate": "Bearer"},
      )
    if allow_importer and self.settings.import_token and secrets.compare_digest(
      token,
      self.settings.import_token,
    ):
      if not _request_is_allowed_importer_client(
        request,
        self.settings.import_allowed_clients,
      ):
        raise ApiError(
          403,
          "importer_source_not_allowed",
          "The historical importer token is restricted to trusted local clients",
        )
      return Principal("importer", "historical-importer")
    token_hash = self.hash_token(token)
    row = self.database.query_one(
      "SELECT id FROM devices WHERE token_hash = ? AND disabled_at IS NULL",
      (token_hash,),
    )
    if row is None:
      raise ApiError(
        401,
        "invalid_device_token",
        "Device bearer token is invalid",
        headers={"WWW-Authenticate": "Bearer"},
      )
    device_id = row["id"]
    if expected_device_id is not None and device_id != expected_device_id:
      raise ApiError(
        403,
        "device_mismatch",
        "Bearer token does not belong to this device",
      )
    return Principal("device", device_id)

  def require_origin(self, request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
      raise ApiError(403, "origin_required", "Origin header is required")
    normalized = origin.rstrip("/")
    if normalized not in self.settings.allowed_origins:
      raise ApiError(403, "invalid_origin", "Origin is not allowed")


def require_idempotency_key(request: Request) -> str:
  key = request.headers.get("idempotency-key", "").strip()
  if not key:
    raise ApiError(
      400,
      "idempotency_key_required",
      "Idempotency-Key header is required",
    )
  if len(key) > 255:
    raise ApiError(
      400,
      "invalid_idempotency_key",
      "Idempotency-Key must not exceed 255 characters",
    )
  return key
