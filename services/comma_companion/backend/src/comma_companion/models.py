from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
  AliasChoices,
  BaseModel,
  ConfigDict,
  Field,
  field_validator,
  model_validator,
)


class StrictModel(BaseModel):
  model_config = ConfigDict(
    extra="forbid",
    populate_by_name=True,
    allow_inf_nan=False,
  )


class LoginRequest(StrictModel):
  username: str = Field(default="admin", min_length=1, max_length=128)
  password: str = Field(min_length=1, max_length=1024)


class SessionView(StrictModel):
  authenticated: bool
  username: str
  expires_at: datetime
  step_up_expires_at: datetime | None = None


class DeviceCreate(StrictModel):
  device_id: str | None = Field(default=None, min_length=1, max_length=128)
  display_name: str = Field(min_length=1, max_length=128)


class DeviceView(StrictModel):
  id: str
  display_name: str
  enrolled_at: datetime
  last_seen_at: datetime | None = None
  online: bool
  offroad: bool | None = None
  agent_version: str | None = None
  software_version: str | None = None
  network_type: str | None = None
  state: str | None = None
  capabilities: list[str] = Field(default_factory=list)
  metrics: dict[str, Any] = Field(default_factory=dict)


class DeviceEnrollmentView(StrictModel):
  device: DeviceView
  token: str


class HeartbeatRequest(StrictModel):
  agent_version: str | None = Field(default=None, max_length=128)
  timestamp: datetime | None = None
  state: str | None = Field(default=None, max_length=64)
  capabilities: list[str] = Field(default_factory=list, max_length=128)
  metrics: dict[str, Any] = Field(default_factory=dict)
  offroad: bool | None = None
  network_type: str | None = Field(default=None, max_length=64)
  software_version: str | None = Field(default=None, max_length=128)


class EmptyCommandArgs(StrictModel):
  pass


class UploadTargetArgs(StrictModel):
  file_id: str | None = Field(default=None, min_length=1, max_length=256)
  upload_id: str | None = Field(default=None, min_length=1, max_length=256)
  scope: Literal["all"] | None = None

  @model_validator(mode="after")
  def require_target(self) -> UploadTargetArgs:
    if not any((self.file_id, self.upload_id, self.scope)):
      raise ValueError("one of file_id, upload_id, or scope is required")
    return self


class PowerArgs(StrictModel):
  reason: str = Field(min_length=1, max_length=256)


class StatusCommand(StrictModel):
  type: Literal["status"]
  args: EmptyCommandArgs = Field(default_factory=EmptyCommandArgs)
  expires_in_seconds: int = Field(default=120, ge=10, le=3600)


class RescanCommand(StrictModel):
  type: Literal["rescan"]
  args: EmptyCommandArgs = Field(default_factory=EmptyCommandArgs)
  expires_in_seconds: int = Field(default=3600, ge=10, le=24 * 60 * 60)


class PauseCommand(StrictModel):
  type: Literal["pause"]
  args: EmptyCommandArgs = Field(default_factory=EmptyCommandArgs)
  expires_in_seconds: int = Field(default=3600, ge=10, le=24 * 60 * 60)


class ResumeCommand(StrictModel):
  type: Literal["resume"]
  args: EmptyCommandArgs = Field(default_factory=EmptyCommandArgs)
  expires_in_seconds: int = Field(default=3600, ge=10, le=24 * 60 * 60)


class RetryUploadCommand(StrictModel):
  type: Literal["retry_upload"]
  args: UploadTargetArgs
  expires_in_seconds: int = Field(default=3600, ge=10, le=24 * 60 * 60)


class CancelUploadCommand(StrictModel):
  type: Literal["cancel_upload"]
  args: UploadTargetArgs
  expires_in_seconds: int = Field(default=3600, ge=10, le=24 * 60 * 60)


class RestartAgentCommand(StrictModel):
  type: Literal["restart_agent"]
  args: EmptyCommandArgs = Field(default_factory=EmptyCommandArgs)
  expires_in_seconds: int = Field(default=300, ge=10, le=3600)


class RestartStarpilotCommand(StrictModel):
  type: Literal["restart_starpilot"]
  args: EmptyCommandArgs = Field(default_factory=EmptyCommandArgs)
  expires_in_seconds: int = Field(default=300, ge=10, le=3600)


class RebootDeviceCommand(StrictModel):
  type: Literal["reboot_device"]
  args: PowerArgs
  expires_in_seconds: int = Field(default=300, ge=10, le=3600)


class ShutdownDeviceCommand(StrictModel):
  type: Literal["shutdown_device"]
  args: PowerArgs
  expires_in_seconds: int = Field(default=300, ge=10, le=3600)


CommandCreate = Annotated[
  StatusCommand
  | RescanCommand
  | PauseCommand
  | ResumeCommand
  | RetryUploadCommand
  | CancelUploadCommand
  | RestartAgentCommand
  | RestartStarpilotCommand
  | RebootDeviceCommand
  | ShutdownDeviceCommand,
  Field(discriminator="type"),
]


class CommandState(StrEnum):
  QUEUED = "queued"
  DELIVERED = "delivered"
  RUNNING = "running"
  SUCCEEDED = "succeeded"
  FAILED = "failed"
  REJECTED = "rejected"
  EXPIRED = "expired"
  CANCELED = "canceled"


class CommandView(StrictModel):
  id: str
  device_id: str
  type: str
  args: dict[str, Any]
  state: CommandState
  requires_offroad: bool
  issued_at: datetime
  expires_at: datetime
  delivered_at: datetime | None = None
  started_at: datetime | None = None
  finished_at: datetime | None = None
  message: str | None = None
  error: str | None = None


class CommandResultRequest(StrictModel):
  state: Literal["running", "succeeded", "failed", "rejected"]
  started_at: datetime | None = None
  finished_at: datetime | None = None
  message: str | None = Field(default=None, max_length=4096)
  error: str | None = Field(default=None, max_length=16_384)
  result: dict[str, Any] = Field(default_factory=dict)

  @model_validator(mode="after")
  def validate_result(self) -> CommandResultRequest:
    if self.state == "failed" and not self.error:
      raise ValueError("failed command results require error")
    if self.state in {"succeeded", "failed", "rejected"} and self.finished_at is None:
      self.finished_at = datetime.now().astimezone()
    return self


class HeartbeatResponse(StrictModel):
  server_time: datetime
  commands: list[CommandView]


class UploadCreate(StrictModel):
  device_id: str | None = Field(default=None, min_length=1, max_length=128)
  file_id: str | None = Field(
    default=None,
    pattern=r"^[0-9a-fA-F]{64}$",
  )
  route_name: str | None = Field(default=None, min_length=1, max_length=256)
  segment_number: int | None = Field(default=None, ge=0, le=1_000_000)
  artifact_type: str = Field(default="unknown", min_length=1, max_length=64)
  camera: str | None = Field(default=None, max_length=32)
  relative_path: str = Field(min_length=1, max_length=4096)
  size: int = Field(
    ge=0,
    validation_alias=AliasChoices("size", "size_bytes"),
  )
  mtime_ns: int | None = Field(default=None, ge=0)
  mtime: datetime | None = None
  sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
  mime_type: str | None = Field(default=None, max_length=255)
  completion_evidence: list[str] = Field(default_factory=list, max_length=32)
  partial: bool = False

  @field_validator("relative_path")
  @classmethod
  def validate_relative_path(cls, value: str) -> str:
    normalized = value.replace("\\", "/")
    if value.startswith(("/", "\\")) or "\x00" in value:
      raise ValueError("relative_path must be relative")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
      raise ValueError("relative_path contains an invalid component")
    return normalized

  @field_validator("file_id", "sha256")
  @classmethod
  def normalize_hex_identifier(cls, value: str | None) -> str | None:
    return value.lower() if value else None

  @field_validator("completion_evidence")
  @classmethod
  def validate_completion_evidence(cls, value: list[str]) -> list[str]:
    allowed = {
      "no_lock",
      "stable_duration",
      "newer_segment",
      "offroad_grace",
      "historical_import",
      "manual",
    }
    if any(item not in allowed for item in value):
      raise ValueError("completion_evidence contains an unknown value")
    return value


class UploadState(StrEnum):
  RECEIVING = "receiving"
  FINALIZING = "finalizing"
  COMPLETE = "complete"
  FAILED = "failed"
  CANCELED = "canceled"


class UploadView(StrictModel):
  id: str
  upload_id: str
  file_id: str | None = None
  device_id: str
  relative_path: str
  route_name: str | None = None
  segment_number: int | None = None
  artifact_type: str
  camera: str | None = None
  completion_evidence: list[str] = Field(default_factory=list)
  partial: bool = False
  offset: int
  length: int
  status: UploadState
  state: UploadState
  durable: bool
  declared_sha256: str | None = None
  sha256: str | None = None
  artifact_id: str | None = None
  error: str | None = None
  created_at: datetime
  updated_at: datetime
  completed_at: datetime | None = None
  bytes_per_second: float = 0


class UploadList(StrictModel):
  items: list[UploadView]
  total: int


class UploadSnapshot(StrictModel):
  active_uploads: int
  failed_uploads: int
  completed_uploads: int
  bytes_received: int
  bytes_expected: int
  pending_bytes: int
  bytes_per_second_60s: float
  by_device: dict[str, dict[str, int | float]]


class ArtifactView(StrictModel):
  id: str
  device_id: str
  drive_id: str | None = None
  segment_id: str | None = None
  kind: str
  camera: str | None = None
  relative_path: str
  sha256: str
  size: int
  mime_type: str | None = None
  codec: str | None = None
  duration_us: int | None = None
  status: str
  source_artifact_id: str | None = None
  created_at: datetime


class RouteInventoryExpectedStreamView(StrictModel):
  role: str
  root_name: str
  artifact_type: str
  camera: str | None = None


class RouteInventoryView(StrictModel):
  manifest_sha256: str
  generation: int
  state: Literal["complete", "partial"]
  route_closed: bool
  capability_source: Literal[
    "configured+route_union",
    "route_union_unconfigured",
  ]
  closure_evidence: list[str] = Field(default_factory=list)
  expected_streams: list[RouteInventoryExpectedStreamView] = Field(
    default_factory=list,
  )
  missing_segment_numbers: list[int] = Field(default_factory=list)
  declared_file_count: int
  archived_file_count: int
  missing_file_count: int


class SegmentExpectedStreamView(StrictModel):
  role: str
  artifact_type: str
  camera: str | None = None
  manifest_status: Literal["present", "missing"]
  relative_path: str | None = None
  archive_status: Literal["missing", "stored", "verified", "ready"]
  media_status: Literal["not_required", "pending", "ready", "failed"]


class SegmentView(StrictModel):
  id: str
  drive_id: str
  number: int
  started_at: datetime | None = None
  start_t_us: int | None = None
  duration_us: int | None = None
  artifacts: list[ArtifactView] = Field(default_factory=list)
  expected_streams: list[SegmentExpectedStreamView] = Field(
    default_factory=list,
  )


class DriveCameraView(StrictModel):
  id: str
  label: str
  available: bool
  codec: str | None = None
  width: int | None = None
  height: int | None = None
  fps: float | None = None


class DriveView(StrictModel):
  id: str
  device_id: str
  route_name: str
  started_at: datetime | None = None
  ended_at: datetime | None = None
  duration_us: int | None = None
  segment_count: int
  ready_segments: int
  expected_media: int
  ready_media: int
  missing_media: int
  failed_media: int
  pruned_media: int
  raw_video_pruning_required: bool
  backup_bytes_received: int
  backup_bytes_expected: int
  artifact_count: int
  telemetry_ready: bool
  readiness: Literal["importing", "processing", "ready", "partial", "failed"]
  cameras: list[DriveCameraView] = Field(default_factory=list)
  vehicle: str | None = None
  distance_m: float | None = None
  location_start: str | None = None
  location_end: str | None = None
  raw_bytes: int = 0
  derived_bytes: int = 0
  stored_bytes: int = 0
  poster_url: str | None = None
  thumbnail_url: str | None = None
  created_at: datetime


class DriveCatalogSummary(StrictModel):
  duration_us: int
  stored_bytes: int
  by_readiness: dict[str, int] = Field(default_factory=dict)


class DriveCatalogPage(StrictModel):
  items: list[DriveView]
  total: int
  limit: int
  offset: int
  summary: DriveCatalogSummary


class TelemetryGenerationView(StrictModel):
  state: str
  schema_version: int
  ndjson_sha256: str
  source_fingerprint: str
  timeline_version: str | None = None
  publication_ready: bool
  start_t_us: int | None = None
  end_t_us: int | None = None
  extractor: str | None = None
  extractor_version: str | None = None
  source_starpilot_commit: str | None = None
  source_rlogs: list[dict[str, Any]] = Field(default_factory=list)
  vehicle: dict[str, Any] = Field(default_factory=dict)
  route_software: dict[str, Any] = Field(default_factory=dict)
  completeness: dict[str, Any] = Field(default_factory=dict)


class DriveDetail(DriveView):
  segments: list[SegmentView] = Field(default_factory=list)
  route_inventory: RouteInventoryView | None = None
  telemetry_generation: TelemetryGenerationView | None = None
  simulation_eligible: bool = False
  simulation_eligibility_reasons: list[dict[str, Any]] = Field(
    default_factory=list,
  )


class AuditEventView(StrictModel):
  id: int
  actor_type: str
  actor_id: str | None = None
  action: str
  resource_type: str | None = None
  resource_id: str | None = None
  details: dict[str, Any] = Field(default_factory=dict)
  ip_address: str | None = None
  created_at: datetime


class WorkerLiveness(StrictModel):
  last_seen: datetime | None = None
  stale: bool
  online: bool
  transcode_jobs_remaining: int
  transcode_seconds_per_job: float | None = None
  eta_seconds: int | None = None


class ArchiveHealth(StrictModel):
  available: bool
  writable: bool


class DashboardSnapshot(StrictModel):
  generated_at: datetime
  devices_total: int
  devices_online: int
  drives_total: int
  drives_ready: int
  drives_by_readiness: dict[str, int]
  segments_total: int
  artifacts_total: int
  raw_bytes: int
  derived_bytes: int
  storage_cataloged_bytes: int
  storage_capacity_bytes: int | None = None
  storage_used_bytes: int | None = None
  storage_free_bytes: int | None = None
  upload: UploadSnapshot
  commands_by_state: dict[str, int]
  jobs_by_state: dict[str, int]
  worker: WorkerLiveness
  archive: ArchiveHealth


class CompanionSettings(StrictModel):
  archive_path: str
  raw_log_retention_enabled: Literal[True]
  raw_video_retention_enabled: bool
  transcode_codec: Literal["av1"]
  transcode_crf: int = Field(ge=0, le=63)
  worker_concurrency: Literal[1]
  metered_uploads_allowed: bool
  timezone: str = Field(min_length=1, max_length=128)


class JobView(StrictModel):
  id: str
  type: str
  upload_id: str | None = None
  drive_id: str | None = None
  artifact_id: str | None = None
  retry_of_job_id: str | None = None
  state: str
  progress: float
  attempts: int
  max_attempts: int
  retryable: bool | None = None
  error: str | None = None
  result: dict[str, Any] = Field(default_factory=dict)
  available_at: datetime | None = None
  cancel_requested_at: datetime | None = None
  created_at: datetime
  updated_at: datetime
  completed_at: datetime | None = None


class JobList(StrictModel):
  items: list[JobView]
  total: int


class SeriesPoint(StrictModel):
  t_us: int
  value: float | bool | str | None
  minimum: float | None = None
  maximum: float | None = None


class SignalSeries(StrictModel):
  signal: str
  unit: str | None = None
  kind: Literal["continuous", "step", "event"]
  points: list[SeriesPoint]


class TelemetryMarker(StrictModel):
  id: str
  kind: str
  start_t_us: int
  end_t_us: int
  severity: str | None = None
  label: str | None = None
  attributes: dict[str, Any] = Field(default_factory=dict)


class SeriesResponse(StrictModel):
  drive_id: str
  ndjson_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
  timeline_version: str = Field(pattern=r"^[0-9a-f]{64}$")
  timeline_origin: Literal["stable", "provisional"]
  start_t_us: int
  end_t_us: int
  signals: list[SignalSeries]
  markers: list[TelemetryMarker] = Field(default_factory=list)
  markers_truncated: bool = False


class SimulationCreate(StrictModel):
  t_us: int = Field(ge=0)
  horizon_us: int = Field(default=1_000_000, ge=100_000, le=2_000_000)
  model_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
  telemetry_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
  timeline_version: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
  mode: Literal["approximate_closed_loop"] = "approximate_closed_loop"
  parameters: dict[str, float | int | bool]

  @field_validator(
    "model_hash",
    "telemetry_sha256",
    "timeline_version",
  )
  @classmethod
  def normalize_sha256(cls, value: str) -> str:
    return value.lower()


class SimulationAccepted(StrictModel):
  id: str
  job_id: str
  state: Literal["queued"]


class SimulationView(StrictModel):
  id: str
  drive_id: str
  job_id: str
  t_us: int
  horizon_us: int
  model_hash: str
  mode: str
  parameters: dict[str, Any]
  baseline_parameters: dict[str, Any] = Field(default_factory=dict)
  telemetry_sha256: str
  timeline_version: str | None = None
  state: str
  progress: float
  result: dict[str, Any] = Field(default_factory=dict)
  error: str | None = None
  created_at: datetime


class SimulatorCapabilities(StrictModel):
  available: bool
  modes: list[str]
  models: list[dict[str, Any]]
  history_required_us: int
  default_horizon_us: int
  maximum_horizon_us: int
  parameter_schema: dict[str, Any]


class MediaManifestEntry(StrictModel):
  segment_number: int
  start_t_us: int | None
  duration_us: int | None
  artifact_id: str
  url: str
  mime_type: str | None = None
  codec: str | None = None
  fps: float | None = None
  sync_mode: Literal["exact", "approximate"]
  sync_url: str | None = None
  frame_index_artifact_id: str | None = None
  frame_index_url: str | None = None
  sync_reason: str | None = None
  video_sha256: str | None = None
  timeline_origin: Literal["stable", "provisional"] | None = None


class MediaManifest(StrictModel):
  drive_id: str
  camera: str
  synchronized: bool
  items: list[MediaManifestEntry]
