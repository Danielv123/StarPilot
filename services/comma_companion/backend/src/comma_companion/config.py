from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlparse


def _bool_env(name: str, default: bool) -> bool:
  value = os.getenv(name)
  if value is None:
    return default
  return value.strip().lower() in {"1", "true", "yes", "on"}


def _origins(value: str | None, public_origin: str) -> tuple[str, ...]:
  if not value:
    return (public_origin.rstrip("/"),)
  return tuple(item.strip().rstrip("/") for item in value.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
  database_path: Path = Path("/var/lib/comma-companion/companion.sqlite3")
  session_dir: Path = Path("/var/lib/comma-companion/sessions")
  archive_root: Path = Path("/archive/comma-companion")
  session_secret: str = field(default_factory=lambda: secrets.token_urlsafe(48))
  admin_username: str = "admin"
  admin_password_hash: str | None = None
  admin_password: str | None = None
  public_origin: str = "https://comma.danielv.no"
  allowed_origins: tuple[str, ...] = ("https://comma.danielv.no",)
  cookie_name: str = "__Host-comma_companion_session"
  cookie_secure: bool = True
  session_ttl_seconds: int = 12 * 60 * 60
  step_up_ttl_seconds: int = 5 * 60
  password_hash_concurrency: int = 2
  device_tokens: dict[str, str] = field(default_factory=dict)
  import_token: str | None = None
  import_allowed_clients: tuple[str, ...] = ("127.0.0.1", "::1")
  max_chunk_bytes: int = 16 * 1024 * 1024
  max_artifact_bytes: int = 64 * 1024 * 1024 * 1024
  max_video_artifact_bytes: int = 1024 * 1024 * 1024
  max_log_artifact_bytes: int = 64 * 1024 * 1024
  max_other_artifact_bytes: int = 256 * 1024 * 1024
  max_json_body_bytes: int = 1024 * 1024
  max_active_uploads_per_device: int = 4
  max_pending_upload_bytes_per_device: int = 128 * 1024 * 1024 * 1024
  max_active_uploads_global: int = 32
  max_pending_upload_bytes_global: int = 512 * 1024 * 1024 * 1024
  max_inflight_upload_patches_per_device: int = 2
  max_inflight_upload_patches_global: int = 8
  archive_min_free_bytes: int = 10 * 1024 * 1024 * 1024
  archive_min_free_percent: float = 5.0
  max_active_jobs: int = 10_000
  upload_stale_seconds: int = 24 * 60 * 60
  online_window_seconds: int = 90
  command_poll_limit: int = 20
  telemetry_debounce_seconds: int = 120
  web_root: Path | None = None
  media_worker_command: str = "comma-companion-media"
  rlog_adapter_command: str = "comma-companion-rlog"
  dynamics_adapter_command: str = "comma-companion-dynamics"
  job_lease_seconds: int = 300
  job_poll_seconds: float = 1.0
  media_timeout_seconds: int = 4 * 60 * 60
  transcode_crf: int = 38
  transcode_preset: int = 10
  retain_raw_video: bool = False

  @classmethod
  def from_env(cls) -> Settings:
    public_origin = os.getenv("COMPANION_PUBLIC_ORIGIN", "https://comma.danielv.no").rstrip("/")
    raw_tokens = os.getenv("COMPANION_DEVICE_TOKENS_JSON", "{}")
    try:
      parsed_tokens = json.loads(raw_tokens)
    except json.JSONDecodeError as exc:
      raise ValueError("COMPANION_DEVICE_TOKENS_JSON must be valid JSON") from exc
    if not isinstance(parsed_tokens, dict) or not all(
      isinstance(key, str) and isinstance(value, str)
      for key, value in parsed_tokens.items()
    ):
      raise ValueError("COMPANION_DEVICE_TOKENS_JSON must map device IDs to tokens")

    session_secret = os.getenv("COMPANION_SESSION_SECRET")
    admin_password_hash = os.getenv("COMPANION_ADMIN_PASSWORD_HASH")
    admin_password = os.getenv("COMPANION_ADMIN_PASSWORD")
    import_token = os.getenv("COMPANION_IMPORT_TOKEN")
    if not session_secret and (
      admin_password_hash
      or admin_password
      or parsed_tokens
      or import_token
    ):
      raise ValueError(
        "COMPANION_SESSION_SECRET is required when authentication is configured",
      )

    settings = cls(
      database_path=Path(os.getenv(
        "COMPANION_DATABASE_PATH",
        "/var/lib/comma-companion/companion.sqlite3",
      )),
      session_dir=Path(os.getenv(
        "COMPANION_SESSION_DIR",
        "/var/lib/comma-companion/sessions",
      )),
      archive_root=Path(os.getenv(
        "COMPANION_ARCHIVE_ROOT",
        "/archive/comma-companion",
      )),
      session_secret=session_secret or secrets.token_urlsafe(48),
      admin_username=os.getenv("COMPANION_ADMIN_USERNAME", "admin"),
      admin_password_hash=admin_password_hash,
      admin_password=admin_password,
      public_origin=public_origin,
      allowed_origins=_origins(os.getenv("COMPANION_ALLOWED_ORIGINS"), public_origin),
      cookie_name=os.getenv(
        "COMPANION_COOKIE_NAME",
        "__Host-comma_companion_session",
      ),
      cookie_secure=_bool_env("COMPANION_COOKIE_SECURE", True),
      session_ttl_seconds=int(os.getenv(
        "COMPANION_SESSION_TTL_SECONDS",
        str(12 * 60 * 60),
      )),
      step_up_ttl_seconds=int(os.getenv(
        "COMPANION_STEP_UP_TTL_SECONDS",
        str(5 * 60),
      )),
      password_hash_concurrency=int(os.getenv(
        "COMPANION_PASSWORD_HASH_CONCURRENCY",
        "2",
      )),
      device_tokens=dict(parsed_tokens),
      import_token=import_token,
      import_allowed_clients=tuple(
        item.strip()
        for item in os.getenv(
          "COMPANION_IMPORT_ALLOWED_CLIENTS",
          "127.0.0.1,::1",
        ).split(",")
        if item.strip()
      ),
      max_chunk_bytes=int(os.getenv(
        "COMPANION_MAX_CHUNK_BYTES",
        str(16 * 1024 * 1024),
      )),
      max_artifact_bytes=int(os.getenv(
        "COMPANION_MAX_ARTIFACT_BYTES",
        str(64 * 1024 * 1024 * 1024),
      )),
      max_video_artifact_bytes=int(os.getenv(
        "COMPANION_MAX_VIDEO_ARTIFACT_BYTES",
        str(1024 * 1024 * 1024),
      )),
      max_log_artifact_bytes=int(os.getenv(
        "COMPANION_MAX_LOG_ARTIFACT_BYTES",
        str(64 * 1024 * 1024),
      )),
      max_other_artifact_bytes=int(os.getenv(
        "COMPANION_MAX_OTHER_ARTIFACT_BYTES",
        str(256 * 1024 * 1024),
      )),
      max_json_body_bytes=int(os.getenv(
        "COMPANION_MAX_JSON_BODY_BYTES",
        str(1024 * 1024),
      )),
      max_active_uploads_per_device=int(os.getenv(
        "COMPANION_MAX_ACTIVE_UPLOADS_PER_DEVICE",
        "4",
      )),
      max_pending_upload_bytes_per_device=int(os.getenv(
        "COMPANION_MAX_PENDING_UPLOAD_BYTES_PER_DEVICE",
        str(128 * 1024 * 1024 * 1024),
      )),
      max_active_uploads_global=int(os.getenv(
        "COMPANION_MAX_ACTIVE_UPLOADS_GLOBAL",
        "32",
      )),
      max_pending_upload_bytes_global=int(os.getenv(
        "COMPANION_MAX_PENDING_UPLOAD_BYTES_GLOBAL",
        str(512 * 1024 * 1024 * 1024),
      )),
      max_inflight_upload_patches_per_device=int(os.getenv(
        "COMPANION_MAX_INFLIGHT_UPLOAD_PATCHES_PER_DEVICE",
        "2",
      )),
      max_inflight_upload_patches_global=int(os.getenv(
        "COMPANION_MAX_INFLIGHT_UPLOAD_PATCHES_GLOBAL",
        "8",
      )),
      archive_min_free_bytes=int(os.getenv(
        "COMPANION_ARCHIVE_MIN_FREE_BYTES",
        str(10 * 1024 * 1024 * 1024),
      )),
      archive_min_free_percent=float(os.getenv(
        "COMPANION_ARCHIVE_MIN_FREE_PERCENT",
        "5",
      )),
      max_active_jobs=int(os.getenv(
        "COMPANION_MAX_ACTIVE_JOBS",
        "10000",
      )),
      upload_stale_seconds=int(os.getenv(
        "COMPANION_UPLOAD_STALE_SECONDS",
        str(24 * 60 * 60),
      )),
      online_window_seconds=int(os.getenv(
        "COMPANION_ONLINE_WINDOW_SECONDS",
        "90",
      )),
      command_poll_limit=int(os.getenv(
        "COMPANION_COMMAND_POLL_LIMIT",
        "20",
      )),
      telemetry_debounce_seconds=int(os.getenv(
        "COMPANION_TELEMETRY_DEBOUNCE_SECONDS",
        "120",
      )),
      web_root=(
        Path(os.environ["COMPANION_WEB_ROOT"])
        if os.getenv("COMPANION_WEB_ROOT")
        else None
      ),
      media_worker_command=os.getenv(
        "COMPANION_MEDIA_WORKER_COMMAND",
        "comma-companion-media",
      ),
      rlog_adapter_command=os.getenv(
        "COMPANION_RLOG_ADAPTER_COMMAND",
        "comma-companion-rlog",
      ),
      dynamics_adapter_command=os.getenv(
        "COMPANION_DYNAMICS_ADAPTER_COMMAND",
        "comma-companion-dynamics",
      ),
      job_lease_seconds=int(os.getenv(
        "COMPANION_JOB_LEASE_SECONDS",
        "300",
      )),
      job_poll_seconds=float(os.getenv(
        "COMPANION_JOB_POLL_SECONDS",
        "1",
      )),
      media_timeout_seconds=int(os.getenv(
        "COMPANION_MEDIA_TIMEOUT_SECONDS",
        str(4 * 60 * 60),
      )),
      transcode_crf=int(os.getenv("COMPANION_TRANSCODE_CRF", "38")),
      transcode_preset=int(os.getenv("COMPANION_TRANSCODE_PRESET", "10")),
      retain_raw_video=_bool_env("COMPANION_RETAIN_RAW_VIDEO", False),
    )
    settings.validate()
    return settings

  @property
  def objects_root(self) -> Path:
    return self.archive_root / "objects" / "sha256"

  @property
  def uploads_root(self) -> Path:
    return self.archive_root / "uploads"

  def validate(self) -> None:
    if len(self.session_secret) < 32:
      raise ValueError("COMPANION_SESSION_SECRET must contain at least 32 characters")
    if self.max_chunk_bytes <= 0:
      raise ValueError("COMPANION_MAX_CHUNK_BYTES must be positive")
    if self.max_artifact_bytes <= 0:
      raise ValueError("COMPANION_MAX_ARTIFACT_BYTES must be positive")
    for name, value in (
      ("COMPANION_MAX_VIDEO_ARTIFACT_BYTES", self.max_video_artifact_bytes),
      ("COMPANION_MAX_LOG_ARTIFACT_BYTES", self.max_log_artifact_bytes),
      ("COMPANION_MAX_OTHER_ARTIFACT_BYTES", self.max_other_artifact_bytes),
    ):
      if value <= 0:
        raise ValueError(f"{name} must be positive")
    if self.max_chunk_bytes > self.max_artifact_bytes:
      raise ValueError(
        "COMPANION_MAX_CHUNK_BYTES must not exceed COMPANION_MAX_ARTIFACT_BYTES",
      )
    if self.max_json_body_bytes <= 0:
      raise ValueError("COMPANION_MAX_JSON_BODY_BYTES must be positive")
    if not 1 <= self.max_active_uploads_per_device <= 128:
      raise ValueError(
        "COMPANION_MAX_ACTIVE_UPLOADS_PER_DEVICE must be between 1 and 128",
      )
    maximum_effective_artifact_bytes = max(
      min(self.max_artifact_bytes, self.max_video_artifact_bytes),
      min(self.max_artifact_bytes, self.max_log_artifact_bytes),
      min(self.max_artifact_bytes, self.max_other_artifact_bytes),
    )
    if (
      self.max_pending_upload_bytes_per_device
      < maximum_effective_artifact_bytes
    ):
      raise ValueError(
        "COMPANION_MAX_PENDING_UPLOAD_BYTES_PER_DEVICE must admit the largest effective artifact limit",
      )
    if (
      not 1 <= self.max_active_uploads_global <= 4096
      or
      self.max_active_uploads_global
      < self.max_active_uploads_per_device
    ):
      raise ValueError(
        "COMPANION_MAX_ACTIVE_UPLOADS_GLOBAL must be at least the per-device active upload limit",
      )
    if (
      self.max_pending_upload_bytes_global
      < self.max_pending_upload_bytes_per_device
    ):
      raise ValueError(
        "COMPANION_MAX_PENDING_UPLOAD_BYTES_GLOBAL must be at least the per-device pending byte limit",
      )
    if not 1 <= self.max_inflight_upload_patches_per_device <= 128:
      raise ValueError(
        "COMPANION_MAX_INFLIGHT_UPLOAD_PATCHES_PER_DEVICE must be between 1 and 128",
      )
    if (
      not 1 <= self.max_inflight_upload_patches_global <= 4096
      or self.max_inflight_upload_patches_global
      < self.max_inflight_upload_patches_per_device
    ):
      raise ValueError(
        "COMPANION_MAX_INFLIGHT_UPLOAD_PATCHES_GLOBAL must be at least the per-device in-flight PATCH limit",
      )
    if self.archive_min_free_bytes < 0:
      raise ValueError(
        "COMPANION_ARCHIVE_MIN_FREE_BYTES must not be negative",
      )
    if not 0 <= self.archive_min_free_percent < 100:
      raise ValueError(
        "COMPANION_ARCHIVE_MIN_FREE_PERCENT must be between 0 and 100",
      )
    if not 1 <= self.max_active_jobs <= 1_000_000:
      raise ValueError(
        "COMPANION_MAX_ACTIVE_JOBS must be between 1 and 1000000",
      )
    if self.upload_stale_seconds < 60:
      raise ValueError(
        "COMPANION_UPLOAD_STALE_SECONDS must be at least 60",
      )
    if self.session_ttl_seconds <= 0 or self.step_up_ttl_seconds <= 0:
      raise ValueError("session and step-up TTL values must be positive")
    if self.step_up_ttl_seconds > self.session_ttl_seconds:
      raise ValueError(
        "COMPANION_STEP_UP_TTL_SECONDS must not exceed the session TTL",
      )
    if not 1 <= self.password_hash_concurrency <= 4:
      raise ValueError(
        "COMPANION_PASSWORD_HASH_CONCURRENCY must be between 1 and 4",
      )
    if not self.import_allowed_clients:
      raise ValueError(
        "COMPANION_IMPORT_ALLOWED_CLIENTS must contain at least one IP address",
      )
    try:
      normalized_import_clients = tuple(
        str(ip_address(value))
        for value in self.import_allowed_clients
      )
    except ValueError as exc:
      raise ValueError(
        "COMPANION_IMPORT_ALLOWED_CLIENTS must contain exact IP addresses",
      ) from exc
    if len(set(normalized_import_clients)) != len(normalized_import_clients):
      raise ValueError(
        "COMPANION_IMPORT_ALLOWED_CLIENTS must not contain duplicate IP addresses",
      )
    if self.online_window_seconds <= 0:
      raise ValueError("COMPANION_ONLINE_WINDOW_SECONDS must be positive")
    if not 1 <= self.command_poll_limit <= 100:
      raise ValueError("COMPANION_COMMAND_POLL_LIMIT must be between 1 and 100")
    if self.telemetry_debounce_seconds < 0:
      raise ValueError(
        "COMPANION_TELEMETRY_DEBOUNCE_SECONDS must not be negative",
      )
    if self.job_lease_seconds <= 0:
      raise ValueError("COMPANION_JOB_LEASE_SECONDS must be positive")
    if self.job_poll_seconds <= 0:
      raise ValueError("COMPANION_JOB_POLL_SECONDS must be positive")
    if self.media_timeout_seconds <= 0:
      raise ValueError("COMPANION_MEDIA_TIMEOUT_SECONDS must be positive")
    if not 0 <= self.transcode_crf <= 63:
      raise ValueError("COMPANION_TRANSCODE_CRF must be between 0 and 63")
    if not 0 <= self.transcode_preset <= 13:
      raise ValueError("COMPANION_TRANSCODE_PRESET must be between 0 and 13")
    for name, command in (
      ("COMPANION_MEDIA_WORKER_COMMAND", self.media_worker_command),
      ("COMPANION_RLOG_ADAPTER_COMMAND", self.rlog_adapter_command),
      ("COMPANION_DYNAMICS_ADAPTER_COMMAND", self.dynamics_adapter_command),
    ):
      if not command.strip():
        raise ValueError(f"{name} must not be empty")
    parsed = urlparse(self.public_origin)
    if (
      parsed.scheme not in {"http", "https"}
      or not parsed.netloc
      or parsed.path not in {"", "/"}
      or parsed.params
      or parsed.query
      or parsed.fragment
    ):
      raise ValueError(
        "COMPANION_PUBLIC_ORIGIN must be an absolute HTTP(S) origin without a path",
      )
    for origin in self.allowed_origins:
      allowed = urlparse(origin)
      if (
        allowed.scheme not in {"http", "https"}
        or not allowed.netloc
        or allowed.path not in {"", "/"}
        or allowed.params
        or allowed.query
        or allowed.fragment
      ):
        raise ValueError(
          "COMPANION_ALLOWED_ORIGINS must contain absolute HTTP(S) origins",
        )
    archive = self.archive_root.resolve()
    database = self.database_path.resolve()
    if database == archive or archive in database.parents:
      raise ValueError("SQLite database must be outside the archive/SMB root")
