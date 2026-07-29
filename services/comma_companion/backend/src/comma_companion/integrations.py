from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import queue
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from uuid import uuid4

from .controller_profile import (
  C6_CONTROLLER_SOURCE_COMMIT,
  HISTORICAL_CONTROLLER_SOURCE_COMMIT,
  validated_controller_profile,
)
from .object_lock import ObjectLock


CANONICAL_JSON_SEPARATORS = (",", ":")
MEDIA_SCHEMA_VERSION = 1
MEDIA_GENERATION_SCHEMA_VERSION = 1
MEDIA_BITRATE_POLICY_PROFILE = {
  "policy_version": 1,
  "policy": "strictly_lower_total_average_bitrate",
  "initial_target_ratio": {
    "numerator": 4,
    "denominator": 5,
  },
  "fallback_target_ratio": {
    "numerator": 3,
    "denominator": 5,
  },
  "container_reserve_ratio": {
    "numerator": 1,
    "denominator": 20,
  },
  "audio_reserve_ratio": {
    "numerator": 5,
    "denominator": 4,
  },
  "minimum_container_reserve_bps": 8_000,
  "minimum_video_maxrate_bps": 1_000,
}
TELEMETRY_SCHEMA_VERSION = 1
MEDIA_TIMEOUT_SECONDS = 14_400
TELEMETRY_TIMEOUT_SECONDS = 7_200
SIMULATION_TIMEOUT_SECONDS = 180
PROCESS_OUTPUT_LIMIT_BYTES = 64 * 1024 * 1024
DYNAMICS_REQUEST_LIMIT_BYTES = 8 * 1024 * 1024
TELEMETRY_RECORD_LIMIT_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_MEDIA_SOURCE_BYTES = 64 * 1024 * 1024 * 1024
RAW_VIDEO_KINDS = frozenset(
  {
    "video",
    "fcamera",
    "ecamera",
    "dcamera",
    "qcamera",
    "road",
    "wideRoad",
    "driver",
  }
)
MAX_TELEMETRY_ROUTE_SEGMENTS = 512
MAX_TELEMETRY_SOURCE_BYTES_PER_SEGMENT = 64 * 1024 * 1024
MAX_TELEMETRY_SOURCE_BYTES_PER_ROUTE = 2 * 1024 * 1024 * 1024
SIMULATION_SAMPLE_PERIOD_US = 10_000
SIMULATION_HISTORY_ROWS = 300
SIMULATION_MAX_SAMPLE_AGE_US = 100_000
SIMULATION_MAX_ASOF_AGE_MS = 35
SIMULATION_MAX_ASOF_AGE_US = SIMULATION_MAX_ASOF_AGE_MS * 1000
SOURCE_AGE_PROOF_SCHEMA = "comma-companion.source-age-proof"
CONTROLLER_SELECTION_PROOF_SCHEMA = "comma-companion.controller-selection-proof"
EFFECTIVE_TORQUE_CONTEXT_PROOF_SCHEMA = "comma-companion.effective-torque-context-proof"
EFFECTIVE_TORQUE_CONTEXT_EVALUATOR = (
  "starpilot-torque-context-by-source-commit"
)
EFFECTIVE_TORQUE_CONTEXT_EVALUATOR_SHA256 = (
  "e2fd454ee0180589abfaa4cffd3fb3b7f8ebb018cee60131b2299afdeb934a24"
)
TORQUE_CONTEXT_EVALUATOR_IDS = {
  HISTORICAL_CONTROLLER_SOURCE_COMMIT: (
    "starpilot-torque-context-2747bf-v1"
  ),
  C6_CONTROLLER_SOURCE_COMMIT: (
    "starpilot-torque-context-6dd6c0-v1"
  ),
}
CONTROLLER_SELECTION_EVALUATOR = "starpilot-controlsd-lateral-selection"
CONTROLLER_SELECTION_EVALUATOR_SHA256 = "bdb1b78a4ec79278f0bae3650ef1cd49cc9853e7f22d9d5ee49b94a99ca89bd2"
SOURCE_AGE_REQUIRED_SOURCES = {
  "car_state": {
    "field": "car_state_age_us",
    "source": "carState",
  },
  "car_control": {
    "field": "car_control_age_us",
    "source": "carControl",
  },
  "car_output": {
    "field": "car_output_age_us",
    "source": "carOutput.actuatorsOutput.torque",
  },
  "controls_state": {
    "field": "controls_state_age_us",
    "source": "controlsState",
  },
}
CONTROLLER_SELECTION_SOURCES = {
  "starpilotPlan.starpilotToggles",
  "versioned_initData_fallback",
}
EFFECTIVE_TORQUE_PARAMETER_SOURCES = {
  "car_params",
  "live_filtered",
  "resolved_custom",
}
CAUSAL_SAMPLING_CONTRACT = {
  "grid": "absolute_monotonic_time",
  "absolute_grid_field": "nominal_log_mono_time_ns",
  "absolute_grid_phase_ns": 0,
  "sample_period_ns": 10_000_000,
  "sample_rate_hz": 100.0,
  "first_tick_formula": ("ceil(first_valid_carState_logMonoTime_ns/sample_period_ns)*sample_period_ns"),
  "alignment": ("latest_at_or_before_grid_time_zero_order_hold"),
  "source_selection": ("independent_per_source_max_valid_source_with_logMonoTime_at_or_before_tick"),
  "invalid_event_policy": {
    "carState": "drop_without_invalidating_prior_valid_state",
    "carControl": "invalidate_until_next_valid",
    "controlsState": "invalidate_until_next_valid",
    "carOutput": "invalidate_until_next_valid",
  },
  "signed_steering_rate": ("causal_grid_difference_of_zoh_steering_angle"),
  "applied_torque_source": ("carOutput.actuatorsOutput.torque_only_no_fallback"),
  "source_age_equation": ("source_age_ms=(nominal_log_mono_time_ns-source_log_mono_time_ns)/1e6"),
  "source_time_error_equation": ("source_time_error_ms=-source_age_ms"),
  "no_future_source": True,
  "max_asof_age_ms": 35.0,
  "required_asof_sources": [
    "carState",
    "carControl",
    "controlsState",
    "carOutput",
  ],
  "route_relative_time_formula": ("nominal_t_us=(nominal_log_mono_time_ns-route_origin_log_mono_time_ns)//1000"),
  "route_relative_phase_policy": ("constant_nonzero_modulo_allowed_exact_10000us_steps"),
  "event_order": ["logMonoTime", "source_ordinal"],
}
NO_FLM_SOURCE_COMMITS = frozenset({
  HISTORICAL_CONTROLLER_SOURCE_COMMIT,
  C6_CONTROLLER_SOURCE_COMMIT,
})
HISTORICAL_FLM_EVALUATOR = "starpilot-flm-availability-by-source-commit"
HISTORICAL_FLM_EVALUATOR_SHA256 = (
  "db63993f0a9d32b083a9e8a4a17496e366fdd35a800c7fd88c39c2a3f4ad0a0e"
)
REFERENCE_CAR_FINGERPRINT = "HYUNDAI_IONIQ_5"
REFERENCE_TELEMETRY_EXTRACTOR_VERSION = "1.1.0"
REFERENCE_TELEMETRY_EXTRACTOR_SHA256 = (
  "0a5e00783409697e208df4b325efbb6136049317d7caadf66363b6d0bfa27ca5"
)
REFERENCE_DYNAMICS_MODEL_SHA256 = "fb1b8b951fdff19ff5f9349470415d003b61ee5655fff996365b429d93f6dc45"


def _exact_int(value: Any, expected: int) -> bool:
  return isinstance(value, int) and not isinstance(value, bool) and value == expected


TELEMETRY_TABLES = {
  "telemetry_indexes",
  "telemetry_series_chunks",
  "telemetry_markers",
  "telemetry_frame_chunks",
  "telemetry_dynamics_chunks",
}
TELEMETRY_REFERENCE_COLUMNS = {
  "telemetry_series_chunks": {
    "ndjson_path",
    "byte_offset",
    "byte_length",
    "record_sha256",
  },
  "telemetry_frame_chunks": {
    "ndjson_path",
    "byte_offset",
    "byte_length",
    "record_sha256",
  },
  "telemetry_dynamics_chunks": {
    "ndjson_path",
    "byte_offset",
    "byte_length",
    "record_sha256",
  },
}

# Kept here so an older database produces an actionable worker result instead
# of silently installing files which the API cannot query.
REQUIRED_TELEMETRY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS telemetry_indexes (
  drive_id TEXT PRIMARY KEY REFERENCES drives(id) ON DELETE CASCADE,
  schema_version INTEGER NOT NULL,
  state TEXT NOT NULL,
  ndjson_path TEXT NOT NULL,
  ndjson_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  signal_catalog_json TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_series_chunks (
  drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  signal_id TEXT NOT NULL,
  tier TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  kind TEXT NOT NULL,
  unit TEXT,
  start_t_us INTEGER NOT NULL,
  end_t_us INTEGER NOT NULL,
  ndjson_path TEXT NOT NULL,
  byte_offset INTEGER NOT NULL,
  byte_length INTEGER NOT NULL,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(drive_id, signal_id, tier, chunk_index)
);

CREATE TABLE IF NOT EXISTS telemetry_markers (
  drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  marker_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  start_t_us INTEGER NOT NULL,
  end_t_us INTEGER NOT NULL,
  severity TEXT,
  label TEXT,
  data_json TEXT NOT NULL,
  PRIMARY KEY(drive_id, marker_id)
);

CREATE TABLE IF NOT EXISTS telemetry_frame_chunks (
  drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  camera TEXT NOT NULL,
  segment_number INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL,
  ndjson_path TEXT NOT NULL,
  byte_offset INTEGER NOT NULL,
  byte_length INTEGER NOT NULL,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(drive_id, camera, segment_number, chunk_index)
);

CREATE TABLE IF NOT EXISTS telemetry_dynamics_chunks (
  drive_id TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  start_t_us INTEGER NOT NULL,
  end_t_us INTEGER NOT NULL,
  ndjson_path TEXT NOT NULL,
  byte_offset INTEGER NOT NULL,
  byte_length INTEGER NOT NULL,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(drive_id, chunk_index)
);
""".strip()


class JobContext(Protocol):
  job_id: str

  def progress(self, fraction: float) -> None: ...

  def cancellation_requested(self) -> bool: ...

  def raise_if_cancelled(self) -> None: ...


class IntegrationError(RuntimeError):
  def __init__(
    self,
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
    retryable: bool = False,
  ):
    super().__init__(f"{code}: {message}")
    self.code = code
    self.message = message
    self.details = dict(details or {})
    self.retryable = retryable

  def as_dict(self) -> dict[str, Any]:
    return {
      "code": self.code,
      "message": self.message,
      "details": self.details,
      "retryable": self.retryable,
    }


class ProcessCancelled(RuntimeError):
  pass


@dataclass(frozen=True)
class ProcessResult:
  returncode: int
  stdout: str
  stderr: str


ProcessRunner = Callable[..., ProcessResult]


@dataclass(frozen=True)
class TelemetryDocument:
  header: dict[str, Any]
  signal_catalog: dict[str, Any]
  manifest: dict[str, Any]
  stream_end: dict[str, Any]
  sha256: str
  size_bytes: int
  record_count: int


@dataclass(frozen=True)
class TelemetrySourceSet:
  log_type: str
  selected: tuple[sqlite3.Row, ...]
  all_rows: tuple[sqlite3.Row, ...]
  fingerprint: str


@dataclass(frozen=True)
class TelemetryRecordReference:
  byte_offset: int
  byte_length: int
  sha256: str


class _NoopContext:
  job_id = "model-registry-refresh"

  def __init__(self) -> None:
    self.cancel_event = threading.Event()

  def progress(self, _fraction: float) -> None:
    pass

  def cancellation_requested(self) -> bool:
    return False

  def raise_if_cancelled(self) -> None:
    pass


def canonical_json(value: Any) -> str:
  return json.dumps(
    value,
    allow_nan=False,
    ensure_ascii=False,
    separators=CANONICAL_JSON_SEPARATORS,
    sort_keys=True,
  )


def _reject_nonfinite_constant(value: str) -> None:
  raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def strict_json_loads(value: str | bytes) -> Any:
  return json.loads(value, parse_constant=_reject_nonfinite_constant)


def telemetry_source_fingerprint(
  sources: Sequence[Mapping[str, Any] | tuple[int, str]],
) -> str:
  entries: list[dict[str, Any]] = []
  for source in sources:
    if isinstance(source, Mapping):
      segment_number = int(source["segment_number"])
      digest = str(source["sha256"]).lower()
    else:
      segment_number, digest = source
      segment_number = int(segment_number)
      digest = str(digest).lower()
    entries.append(
      {
        "segment_number": segment_number,
        "sha256": digest,
      }
    )
  payload = canonical_json(entries).encode("utf-8")
  return hashlib.sha256(payload).hexdigest()


def _now_text() -> str:
  return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _path_component(value: Any) -> str:
  text = str(value)
  encoded = quote(text, safe="-_~")
  if not encoded or encoded in {".", ".."}:
    raise IntegrationError(
      "invalid_catalog_path",
      "A catalog identifier cannot be represented as an archive path component.",
    )
  return encoded


def _safe_route_directory_name(route_name: str) -> str:
  if not route_name or route_name in {".", ".."} or "/" in route_name or "\\" in route_name or "\x00" in route_name:
    raise IntegrationError(
      "invalid_route_name",
      "The stored route name is not a single filesystem component.",
      details={"route_name": route_name},
    )
  return route_name


def _context_progress(context: Any, fraction: float) -> None:
  callback = getattr(context, "progress", None)
  if callable(callback):
    callback(min(1.0, max(0.0, float(fraction))))


def _context_cancelled(context: Any) -> bool:
  callback = getattr(context, "cancellation_requested", None)
  if callable(callback):
    return bool(callback())
  event = getattr(context, "cancel_event", None)
  return bool(event is not None and event.is_set())


def _raise_if_cancelled(context: Any) -> None:
  callback = getattr(context, "raise_if_cancelled", None)
  if callable(callback):
    callback()
  if _context_cancelled(context):
    raise IntegrationError(
      "cancelled",
      "The integration job was cancelled.",
      retryable=True,
    )


def _terminate_process(process: subprocess.Popen[str]) -> None:
  if process.poll() is not None:
    return
  process.terminate()
  try:
    process.wait(timeout=3)
  except subprocess.TimeoutExpired:
    process.kill()
    process.wait(timeout=3)


def run_subprocess(
  command: Sequence[str],
  *,
  input_text: str | None,
  timeout_seconds: float,
  on_stdout_line: Callable[[str], None] | None,
  cancel_event: threading.Event | None,
  cwd: Path | None = None,
  output_limit_bytes: int = PROCESS_OUTPUT_LIMIT_BYTES,
) -> ProcessResult:
  if timeout_seconds <= 0:
    raise ValueError("timeout_seconds must be positive")
  try:
    process = subprocess.Popen(
      list(command),
      cwd=cwd,
      stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      encoding="utf-8",
      errors="strict",
      shell=False,
      bufsize=1,
    )
  except OSError as error:
    raise IntegrationError(
      "tool_unavailable",
      "Could not start an integration subprocess.",
      details={
        "command": command[0] if command else None,
        "error_type": type(error).__name__,
      },
    ) from error

  output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

  def read_stream(name: str, stream: Any) -> None:
    try:
      for line in stream:
        output_queue.put((name, line))
    finally:
      output_queue.put((name, None))

  assert process.stdout is not None
  assert process.stderr is not None
  readers = [
    threading.Thread(
      target=read_stream,
      args=("stdout", process.stdout),
      daemon=True,
    ),
    threading.Thread(
      target=read_stream,
      args=("stderr", process.stderr),
      daemon=True,
    ),
  ]
  for reader in readers:
    reader.start()

  try:
    if input_text is not None:
      assert process.stdin is not None
      try:
        process.stdin.write(input_text)
        process.stdin.flush()
      except BrokenPipeError:
        pass
      finally:
        process.stdin.close()

    deadline = time.monotonic() + timeout_seconds
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    output_bytes = 0
    closed_streams: set[str] = set()
    while process.poll() is None or len(closed_streams) < 2 or not output_queue.empty():
      if cancel_event is not None and cancel_event.is_set():
        _terminate_process(process)
        raise ProcessCancelled("integration subprocess was cancelled")
      if time.monotonic() >= deadline:
        _terminate_process(process)
        raise IntegrationError(
          "subprocess_timeout",
          "An integration subprocess exceeded its bounded timeout.",
          details={"timeout_seconds": timeout_seconds},
          retryable=True,
        )
      try:
        name, line = output_queue.get(timeout=0.1)
      except queue.Empty:
        continue
      if line is None:
        closed_streams.add(name)
        continue
      output_bytes += len(line.encode("utf-8"))
      if output_bytes > output_limit_bytes:
        _terminate_process(process)
        raise IntegrationError(
          "subprocess_output_too_large",
          "An integration subprocess exceeded the captured output limit.",
          details={"limit_bytes": output_limit_bytes},
        )
      if name == "stdout":
        stdout_parts.append(line)
        if on_stdout_line is not None:
          on_stdout_line(line)
      else:
        stderr_parts.append(line)

    return ProcessResult(
      returncode=process.wait(),
      stdout="".join(stdout_parts),
      stderr="".join(stderr_parts),
    )
  except BaseException:
    _terminate_process(process)
    raise
  finally:
    for reader in readers:
      reader.join(timeout=1)
    process.stdout.close()
    process.stderr.close()


class IntegrationHandlers:
  def __init__(
    self,
    database: Any,
    archive_root: Path,
    *,
    media_command: Sequence[str] | None = None,
    rlog_command: Sequence[str] | None = None,
    dynamics_command: Sequence[str] | None = None,
    process_runner: ProcessRunner | None = None,
    media_timeout_seconds: int = MEDIA_TIMEOUT_SECONDS,
    telemetry_timeout_seconds: int = TELEMETRY_TIMEOUT_SECONDS,
    simulation_timeout_seconds: int = SIMULATION_TIMEOUT_SECONDS,
    transcode_crf: int = 38,
    transcode_preset: int = 10,
    retain_raw_video: bool = True,
    max_media_source_bytes: int = DEFAULT_MAX_MEDIA_SOURCE_BYTES,
    working_directory: Path | None = None,
  ):
    self.database = database
    self.archive_root = Path(archive_root).resolve()
    self.media_command = tuple(media_command or (sys.executable, "-m", "comma_companion_media_worker"))
    self.rlog_command = tuple(
      rlog_command
      or (
        sys.executable,
        "-m",
        "services.comma_companion.adapters.rlog",
      )
    )
    self.dynamics_command = tuple(dynamics_command or ("comma-companion-dynamics",))
    self.process_runner = process_runner or run_subprocess
    self.media_timeout_seconds = self._bounded_timeout(
      media_timeout_seconds,
      "media_timeout_seconds",
      24 * 60 * 60,
    )
    self.telemetry_timeout_seconds = self._bounded_timeout(
      telemetry_timeout_seconds,
      "telemetry_timeout_seconds",
      24 * 60 * 60,
    )
    self.simulation_timeout_seconds = self._bounded_timeout(
      simulation_timeout_seconds,
      "simulation_timeout_seconds",
      15 * 60,
    )
    self.transcode_crf = self._bounded_integer(
      transcode_crf,
      "transcode_crf",
      0,
      63,
    )
    self.transcode_preset = self._bounded_integer(
      transcode_preset,
      "transcode_preset",
      0,
      13,
    )
    if not isinstance(retain_raw_video, bool):
      raise ValueError("retain_raw_video must be a boolean")
    self.retain_raw_video = retain_raw_video
    self.max_media_source_bytes = self._bounded_integer(
      max_media_source_bytes,
      "max_media_source_bytes",
      1,
      1024 * 1024 * 1024 * 1024,
    )
    self.working_directory = Path(working_directory).resolve() if working_directory is not None else None
    self.handlers: Mapping[
      str,
      Callable[[JobContext, dict[str, Any]], dict[str, Any]],
    ] = {
      "verify_artifact": self.verify_artifact,
      "transcode_video": self.transcode_video,
      "extract_telemetry": self.extract_telemetry,
      "build_media_sync": self.build_media_sync,
      "simulate_counterfactual": self.simulate_counterfactual,
    }

  @staticmethod
  def _bounded_timeout(value: Any, field: str, maximum: int) -> int:
    if isinstance(value, bool):
      raise ValueError(f"{field} must be an integer")
    result = int(value)
    if result < 1 or result > maximum:
      raise ValueError(f"{field} must be between 1 and {maximum}")
    return result

  @staticmethod
  def _bounded_integer(
    value: Any,
    field: str,
    minimum: int,
    maximum: int,
  ) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
      raise ValueError(f"{field} must be an integer")
    if value < minimum or value > maximum:
      raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value

  def _runtime_transcode_crf(self) -> int:
    if "runtime_settings" not in self._existing_tables():
      return self.transcode_crf
    row = self.database.query_one(
      "SELECT value_json FROM runtime_settings WHERE key = 'transcode_crf'",
    )
    if row is None:
      return self.transcode_crf
    try:
      value = strict_json_loads(row["value_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
      return self.transcode_crf
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 63:
      return value
    return self.transcode_crf

  def _run(
    self,
    context: Any,
    command: Sequence[str],
    *,
    input_text: str | None,
    timeout_seconds: float,
    on_stdout_line: Callable[[str], None] | None = None,
    output_limit_bytes: int = PROCESS_OUTPUT_LIMIT_BYTES,
  ) -> ProcessResult:
    _raise_if_cancelled(context)
    cancel_event = getattr(context, "cancel_event", None)
    try:
      result = self.process_runner(
        tuple(str(part) for part in command),
        input_text=input_text,
        timeout_seconds=timeout_seconds,
        on_stdout_line=on_stdout_line,
        cancel_event=cancel_event,
        cwd=self.working_directory,
        output_limit_bytes=output_limit_bytes,
      )
    except ProcessCancelled:
      _raise_if_cancelled(context)
      raise IntegrationError(
        "cancelled",
        "The integration subprocess was cancelled.",
        retryable=True,
      ) from None
    _raise_if_cancelled(context)
    if not isinstance(result, ProcessResult):
      result = ProcessResult(
        returncode=int(result.returncode),
        stdout=(result.stdout.decode("utf-8") if isinstance(result.stdout, bytes) else str(result.stdout or "")),
        stderr=(result.stderr.decode("utf-8") if isinstance(result.stderr, bytes) else str(result.stderr or "")),
      )
    return result

  def _archive_path(
    self,
    storage_path: str,
    *,
    required_root: Path | None = None,
    must_exist: bool = True,
  ) -> Path:
    relative = Path(storage_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
      raise IntegrationError(
        "unsafe_storage_path",
        "The catalog contains an unsafe archive-relative path.",
        details={"storage_path": storage_path},
      )
    resolved = (self.archive_root / relative).resolve()
    root = (required_root or self.archive_root).resolve()
    if not resolved.is_relative_to(root):
      raise IntegrationError(
        "unsafe_storage_path",
        "The catalog path escapes its authorized archive root.",
        details={"storage_path": storage_path},
      )
    if must_exist and not resolved.is_file():
      raise IntegrationError(
        "artifact_missing",
        "The cataloged archive object is missing.",
        details={"storage_path": storage_path},
        retryable=True,
      )
    return resolved

  def _artifact(self, artifact_id: str) -> sqlite3.Row:
    row = self.database.query_one(
      """
      SELECT
        a.*,
        o.size AS object_size,
        o.storage_path AS object_storage_path,
        o.storage_state,
        d.route_name,
        s.number AS segment_number
      FROM artifacts a
      JOIN objects o ON o.sha256 = a.object_sha256
      LEFT JOIN drives d ON d.id = a.drive_id
      LEFT JOIN segments s ON s.id = a.segment_id
      WHERE a.id = ?
      """,
      (artifact_id,),
    )
    if row is None:
      raise IntegrationError(
        "artifact_not_found",
        "The integration job references an unknown artifact.",
        details={"artifact_id": artifact_id},
      )
    return row

  def _source_object_path(self, row: sqlite3.Row) -> Path:
    if row["storage_state"] != "present":
      raise IntegrationError(
        "artifact_content_pruned",
        "The original camera object has already been pruned.",
        details={"artifact_id": row["id"]},
      )
    object_root = self.archive_root / "objects"
    path = self._archive_path(
      row["object_storage_path"],
      required_root=object_root,
    )
    if row["storage_path"] != row["object_storage_path"]:
      raise IntegrationError(
        "artifact_catalog_mismatch",
        "A source artifact does not point to its immutable object.",
        details={"artifact_id": row["id"]},
      )
    return path

  @staticmethod
  def _hash_file(
    path: Path,
    *,
    progress: Callable[[int], None] | None = None,
  ) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
      while chunk := source.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
        if progress is not None:
          progress(size)
    return digest.hexdigest(), size

  def verify_artifact(
    self,
    context: JobContext,
    payload: dict[str, Any],
  ) -> dict[str, Any]:
    artifact_id = self._required_payload_string(payload, "artifact_id")
    row = self._artifact(artifact_id)
    path = self._source_object_path(row)
    expected_size = int(row["object_size"])

    def progress(bytes_read: int) -> None:
      _raise_if_cancelled(context)
      if expected_size:
        _context_progress(context, bytes_read / expected_size)

    digest, size = self._hash_file(path, progress=progress)
    if digest != row["object_sha256"] or size != expected_size or size != row["size"]:
      raise IntegrationError(
        "artifact_integrity_failed",
        "The immutable artifact failed SHA-256 or size verification.",
        details={
          "artifact_id": artifact_id,
          "expected_sha256": row["object_sha256"],
          "actual_sha256": digest,
          "expected_size": expected_size,
          "actual_size": size,
        },
      )
    if row["status"] not in {"partial", "ready"}:
      self.database.execute(
        "UPDATE artifacts SET status = 'verified' WHERE id = ?",
        (artifact_id,),
      )
    _context_progress(context, 1.0)
    return {
      "status": "verified",
      "artifact_id": artifact_id,
      "sha256": digest,
      "size_bytes": size,
      "storage_path": row["object_storage_path"],
    }

  @staticmethod
  def _required_payload_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
      raise IntegrationError(
        "invalid_job_payload",
        f"Job payload field {field!r} must be a non-empty string.",
      )
    return value

  @staticmethod
  def _camera(row: sqlite3.Row) -> str:
    camera = row["camera"]
    if camera in {"road", "wide", "driver", "qcamera", "unknown"}:
      return camera
    return {
      "fcamera": "road",
      "road": "road",
      "ecamera": "wide",
      "wideRoad": "wide",
      "dcamera": "driver",
      "driver": "driver",
      "qcamera": "qcamera",
    }.get(row["kind"], "unknown")

  @staticmethod
  def _media_input_format(row: sqlite3.Row) -> str:
    relative_path = row["relative_path"]
    if not isinstance(relative_path, str) or not relative_path:
      raise IntegrationError(
        "unsupported_media_source",
        "The media artifact has no canonical relative filename.",
      )
    filename = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    camera = IntegrationHandlers._camera(row)
    formats = {
      ("fcamera", "road", "fcamera.hevc"): "raw_hevc",
      ("ecamera", "wide", "ecamera.hevc"): "raw_hevc",
      ("dcamera", "driver", "dcamera.hevc"): "raw_hevc",
      ("qcamera", "qcamera", "qcamera.hevc"): "raw_hevc",
      ("qcamera", "qcamera", "qcamera.ts"): "mpegts",
      ("video", "road", "fcamera.hevc"): "raw_hevc",
      ("video", "wide", "ecamera.hevc"): "raw_hevc",
      ("video", "driver", "dcamera.hevc"): "raw_hevc",
      ("video", "qcamera", "qcamera.hevc"): "raw_hevc",
      ("video", "qcamera", "qcamera.ts"): "mpegts",
    }
    input_format = formats.get((row["kind"], camera, filename))
    if input_format is None:
      raise IntegrationError(
        "unsupported_media_source",
        "The artifact kind and canonical filename are not an approved camera input pair.",
        details={
          "kind": row["kind"],
          "camera": camera,
          "filename": filename,
        },
      )
    return input_format

  def _media_paths(
    self,
    row: sqlite3.Row,
    generation_fingerprint: str,
  ) -> tuple[Path, Path, Path, Path, Path, str]:
    if (
      len(generation_fingerprint) != 64
      or any(character not in "0123456789abcdef" for character in generation_fingerprint)
    ):
      raise IntegrationError(
        "invalid_media_generation",
        "The derived media generation fingerprint is invalid.",
      )
    device = _path_component(row["device_id"])
    route = _path_component(row["route_name"] or "_unassigned")
    segment = str(int(row["segment_number"])) if row["segment_number"] is not None else _path_component(row["id"])
    camera = self._camera(row)
    directory = (
      self.archive_root
      / "derived"
      / device
      / route
      / segment
      / generation_fingerprint
    ).resolve()
    derived_root = (self.archive_root / "derived").resolve()
    if not directory.is_relative_to(derived_root):
      raise IntegrationError(
        "unsafe_output_path",
        "The derived media path escapes its authorized archive root.",
      )
    video = directory / f"{camera}.av1.webm"
    metadata = directory / f"{camera}.av1.json"
    poster = directory / f"{camera}.av1.poster.jpg"
    thumbnails = directory / f"{camera}.av1.thumbnails"
    frame_index = directory / f"{camera}.av1.frames.json"
    relative = video.relative_to(self.archive_root).as_posix()
    return video, metadata, poster, thumbnails, frame_index, relative

  def _media_encode_profile(self) -> dict[str, Any]:
    return {
      "encoder": "libsvtav1",
      "preset": self.transcode_preset,
      "crf": self._runtime_transcode_crf(),
      "logical_processors": 2,
      "pixel_format": "yuv420p",
      "raw_hevc_frame_rate": 20,
      "keyframe_interval_seconds": 4,
      "thumbnail_count": 6,
      "thumbnail_width": 480,
      "preserve_audio": True,
      "audio_bitrate_kbps": 32,
      "overwrite": False,
    }

  def _media_generation_fingerprint(
    self,
    row: sqlite3.Row,
    encode_profile: Mapping[str, Any],
  ) -> str:
    source_digest = str(row["object_sha256"]).lower()
    if (
      len(source_digest) != 64
      or any(character not in "0123456789abcdef" for character in source_digest)
    ):
      raise IntegrationError(
        "invalid_media_generation",
        "The source object has no valid digest for media generation.",
      )
    contract = {
      "schema": "comma-companion.media-generation",
      "schema_version": MEDIA_GENERATION_SCHEMA_VERSION,
      "media_job_schema_version": MEDIA_SCHEMA_VERSION,
      "source": {
        "object_sha256": source_digest,
        "input_format": self._media_input_format(row),
        "kind": row["kind"],
        "camera": self._camera(row),
      },
      "encode": dict(encode_profile),
      "bitrate_policy": MEDIA_BITRATE_POLICY_PROFILE,
    }
    return hashlib.sha256(canonical_json(contract).encode("utf-8")).hexdigest()

  def _time_mapping(self, drive_id: str | None) -> dict[str, Any] | None:
    if drive_id is None or not self._tables_present({"telemetry_indexes"}):
      return None
    row = self.database.query_one(
      """
      SELECT ndjson_path, ndjson_sha256, schema_version
      FROM telemetry_indexes
      WHERE drive_id = ?
      """,
      (drive_id,),
    )
    if row is None:
      return None
    path = self._archive_path(
      row["ndjson_path"],
      required_root=self.archive_root / "telemetry",
    )
    return {
      "path": str(path),
      "sha256": row["ndjson_sha256"],
      "version": f"telemetry-v{row['schema_version']}",
    }

  def transcode_video(
    self,
    context: JobContext,
    payload: dict[str, Any],
  ) -> dict[str, Any]:
    artifact_id = self._required_payload_string(payload, "artifact_id")
    source = self._artifact(artifact_id)
    source_path = self._source_object_path(source)
    input_format = self._media_input_format(source)
    actual_source_size = source_path.stat().st_size
    if actual_source_size != source["object_size"] or actual_source_size != source["size"]:
      raise IntegrationError(
        "media_source_size_mismatch",
        "The media source size does not match its immutable catalog entry.",
        details={
          "artifact_id": artifact_id,
          "catalog_size": source["object_size"],
          "actual_size": actual_source_size,
        },
      )
    if actual_source_size > self.max_media_source_bytes:
      raise IntegrationError(
        "media_source_too_large",
        "The media source exceeds the configured per-artifact transcode cap.",
        details={
          "artifact_id": artifact_id,
          "size_bytes": actual_source_size,
          "limit_bytes": self.max_media_source_bytes,
        },
      )
    if source["status"] == "partial":
      raise IntegrationError(
        "partial_video",
        "A partial camera artifact cannot be transcoded.",
        details={"artifact_id": artifact_id},
      )
    if source["segment_id"] is None or source["segment_number"] is None:
      raise IntegrationError(
        "video_segment_required",
        "A video must belong to a numbered segment before it can be synchronized.",
        details={"artifact_id": artifact_id},
      )
    encode_profile = self._media_encode_profile()
    generation_fingerprint = self._media_generation_fingerprint(
      source,
      encode_profile,
    )
    (
      video,
      metadata,
      poster,
      thumbnails,
      frame_index,
      relative_path,
    ) = self._media_paths(source, generation_fingerprint)
    for output in (video, metadata, poster, thumbnails, frame_index):
      if not output.resolve().is_relative_to((self.archive_root / "derived").resolve()):
        raise IntegrationError(
          "unsafe_output_path",
          "A media output escapes the derived archive root.",
        )
    video.parent.mkdir(parents=True, exist_ok=True)
    cancel_path = getattr(context, "cancel_path", None)
    job: dict[str, Any] = {
      "schema_version": MEDIA_SCHEMA_VERSION,
      "job_id": context.job_id,
      "input": {
        "path": str(source_path),
        "artifact_id": artifact_id,
        "camera": self._camera(source),
        "kind": source["kind"],
        "input_format": input_format,
        "expected_sha256": source["object_sha256"],
      },
      "outputs": {
        "video_path": str(video),
        "metadata_path": str(metadata),
        "poster_path": str(poster),
        "thumbnails_dir": str(thumbnails),
        "frame_index_path": str(frame_index),
      },
      "encode": encode_profile,
      "limits": {
        "timeout_seconds": self.media_timeout_seconds,
        "minimum_free_bytes": 2 * 1024**3,
        "working_space_multiplier": 1.25,
      },
      "retain_raw": True,
    }
    job["input"]["segment_num"] = int(source["segment_number"])
    if cancel_path is not None:
      job["limits"]["cancel_file"] = str(Path(cancel_path).resolve())
    time_mapping = self._time_mapping(source["drive_id"])
    if time_mapping is not None:
      job["time_mapping"] = time_mapping

    events: list[dict[str, Any]] = []

    def progress_line(line: str) -> None:
      if not line.strip():
        return
      try:
        event = strict_json_loads(line)
      except (ValueError, json.JSONDecodeError):
        return
      if not isinstance(event, dict):
        return
      events.append(event)
      fraction = event.get("fraction")
      if event.get("event") == "progress" and isinstance(fraction, (int, float)) and not isinstance(fraction, bool) and math.isfinite(float(fraction)):
        _context_progress(context, min(0.95, float(fraction) * 0.95))

    result = self._run(
      context,
      (*self.media_command, "encode", "-"),
      input_text=canonical_json(job) + "\n",
      timeout_seconds=self.media_timeout_seconds + 30,
      on_stdout_line=progress_line,
    )
    parsed_events = self._strict_ndjson(result.stdout, "media worker stdout")
    if result.returncode != 0:
      error = self._structured_process_error(result.stderr)
      raise IntegrationError(
        error.get("code", "media_worker_failed"),
        error.get("message", "The media worker failed."),
        details={
          "returncode": result.returncode,
          "error": error,
        },
        retryable=bool(error.get("retryable", False)),
      )
    terminal = next(
      (item for item in reversed(parsed_events) if item.get("event") == "result" and isinstance(item.get("result"), dict)),
      None,
    )
    if terminal is None:
      raise IntegrationError(
        "media_result_missing",
        "The media worker exited successfully without a terminal result.",
      )
    worker_result = terminal["result"]
    validated = self._validate_media_result(
      worker_result,
      source,
      video,
      metadata,
      poster,
      thumbnails,
      frame_index,
      expected_job_id=context.job_id,
      expected_encode=job["encode"],
    )
    validated["encoder"]["generation"] = {
      "schema": "comma-companion.media-generation",
      "schema_version": MEDIA_GENERATION_SCHEMA_VERSION,
      "fingerprint": generation_fingerprint,
      "source_object_sha256": source["object_sha256"],
      "bitrate_policy": MEDIA_BITRATE_POLICY_PROFILE,
    }
    (
      derived_id,
      frame_index_id,
      poster_id,
      thumbnail_ids,
      catalog_paths,
    ) = self._catalog_derived_video(
      source,
      relative_path,
      validated,
    )
    media_sync = self._attempt_media_sync(context, derived_id)
    if self.retain_raw_video:
      raw_source = {"status": "retained", "reason": "deployment_policy"}
    else:
      self._schedule_raw_video_prune(artifact_id)
      raw_source = {"status": "prune_queued"}
    _context_progress(context, 1.0)
    return {
      "status": "ready",
      "artifact_id": derived_id,
      "source_artifact_id": artifact_id,
      "storage_path": catalog_paths["video"],
      "relative_path": relative_path,
      "generation_fingerprint": generation_fingerprint,
      "sha256": validated["sha256"],
      "size_bytes": validated["size_bytes"],
      "codec": validated["codec"],
      "mime_type": validated["mime_type"],
      "duration_us": validated["duration_us"],
      "frame_index_artifact_id": frame_index_id,
      "frame_index_path": catalog_paths["frame_index"],
      "frame_index_sha256": validated["frame_index"]["sha256"],
      "poster_artifact_id": poster_id,
      "poster_path": catalog_paths["poster"],
      "poster_sha256": validated["poster"]["sha256"],
      "thumbnail_artifact_ids": thumbnail_ids,
      "thumbnails": [
        {
          "artifact_id": artifact_id,
          "storage_path": storage_path,
          "relative_path": thumbnail["storage_path"],
          "sha256": thumbnail["sha256"],
          "timestamp_seconds": thumbnail["timestamp_seconds"],
        }
        for artifact_id, thumbnail, storage_path in zip(
          thumbnail_ids,
          validated["thumbnails"],
          catalog_paths["thumbnails"],
          strict=True,
        )
      ],
      "media_sync": media_sync,
      "raw_source": raw_source,
      "worker_status": worker_result["status"],
      "worker": worker_result,
    }

  def _schedule_raw_video_prune(self, artifact_id: str) -> None:
    now = _now_text()
    with self.database.transaction(immediate=True) as connection:
      connection.execute(
        """
        INSERT OR IGNORE INTO jobs(
          id, type, state, payload_json, dedupe_key, available_at,
          created_at, updated_at
        ) VALUES (?, 'prune_raw_video', 'queued', ?, ?, ?, ?, ?)
        """,
        (
          uuid4().hex,
          canonical_json({"artifact_id": artifact_id}),
          f"artifact:{artifact_id}",
          now,
          now,
          now,
        ),
      )

  def _raw_video_prune_candidate(
    self,
    connection: sqlite3.Connection,
    object_sha256: str,
  ) -> tuple[sqlite3.Row, list[sqlite3.Row]] | None:
    object_row = connection.execute(
      """
      SELECT sha256, size, storage_path, storage_state, pruned_at
      FROM objects
      WHERE sha256 = ?
      """,
      (object_sha256,),
    ).fetchone()
    if object_row is None:
      return None
    references = connection.execute(
      """
      SELECT id, kind, camera, source_artifact_id, storage_path
      FROM artifacts
      WHERE object_sha256 = ?
      ORDER BY id
      """,
      (object_sha256,),
    ).fetchall()
    if not references:
      return None
    for reference in references:
      if (
        reference["source_artifact_id"] is not None
        or reference["kind"] not in RAW_VIDEO_KINDS
      ):
        return None
      derivative = connection.execute(
        """
        SELECT derived.id
        FROM artifacts derived
        JOIN objects derived_object
          ON derived_object.sha256 = derived.object_sha256
        WHERE derived.source_artifact_id = ?
          AND derived.kind = 'derived_video'
          AND derived.status = 'ready'
          AND LOWER(derived.codec) = 'av1'
          AND derived_object.storage_state = 'present'
        LIMIT 1
        """,
        (reference["id"],),
      ).fetchone()
      if derivative is None:
        return None
    return object_row, references

  def _prune_raw_video_object(self, artifact_id: str) -> dict[str, Any]:
    if self.retain_raw_video:
      return {"status": "retained", "reason": "deployment_policy"}
    source = self.database.query_one(
      """
      SELECT id, object_sha256
      FROM artifacts
      WHERE id = ? AND source_artifact_id IS NULL
      """,
      (artifact_id,),
    )
    if source is None:
      return {"status": "retained", "reason": "not_source_artifact"}
    lock_root = Path(self.database.path).parent / "object-locks"
    with ObjectLock(lock_root, source["object_sha256"]):
      return self._prune_raw_video_object_locked(artifact_id)

  def _prune_raw_video_object_locked(
    self,
    artifact_id: str,
  ) -> dict[str, Any]:
    with self.database.transaction(immediate=True) as connection:
      source = connection.execute(
        """
        SELECT id, object_sha256
        FROM artifacts
        WHERE id = ? AND source_artifact_id IS NULL
        """,
        (artifact_id,),
      ).fetchone()
      if source is None:
        return {"status": "retained", "reason": "not_source_artifact"}
      candidate = self._raw_video_prune_candidate(
        connection,
        source["object_sha256"],
      )
      if candidate is None:
        return {"status": "retained", "reason": "shared_or_unverified"}
      object_row, references = candidate
      if object_row["storage_state"] == "pruned":
        return {
          "status": "pruned",
          "sha256": object_row["sha256"],
          "size_bytes": object_row["size"],
          "already_pruned": True,
        }
      connection.execute(
        """
        UPDATE objects
        SET storage_state = 'prune_pending', pruned_at = NULL
        WHERE sha256 = ? AND storage_state != 'pruned'
        """,
        (object_row["sha256"],),
      )

    path = self._archive_path(
      object_row["storage_path"],
      required_root=self.archive_root / "objects" / "sha256",
      must_exist=False,
    )
    already_missing = not path.exists()
    try:
      path.unlink(missing_ok=True)
    except OSError as exc:
      self.database.execute(
        """
        UPDATE objects
        SET storage_state = 'present'
        WHERE sha256 = ? AND storage_state = 'prune_pending'
        """,
        (object_row["sha256"],),
      )
      raise IntegrationError(
        "raw_video_prune_failed",
        "The verified source video could not be removed from object storage.",
        details={"sha256": object_row["sha256"]},
        retryable=True,
      ) from exc

    pruned_at = _now_text()
    with self.database.transaction(immediate=True) as connection:
      connection.execute(
        """
        UPDATE objects
        SET storage_state = 'pruned', pruned_at = ?
        WHERE sha256 = ? AND storage_state = 'prune_pending'
        """,
        (pruned_at, object_row["sha256"]),
      )
      connection.executemany(
        """
        UPDATE artifacts
        SET status = 'raw_video_pruned'
        WHERE id = ? AND source_artifact_id IS NULL
        """,
        [(reference["id"],) for reference in references],
      )
      connection.execute(
        """
        INSERT INTO audit_events(
          actor_type, actor_id, action, resource_type, resource_id,
          details_json, created_at
        ) VALUES ('worker', NULL, 'raw_video.pruned', 'object', ?, ?, ?)
        """,
        (
          object_row["sha256"],
          canonical_json(
            {
              "artifact_ids": [reference["id"] for reference in references],
              "size_bytes": object_row["size"],
              "already_missing": already_missing,
            }
          ),
          pruned_at,
        ),
      )
    return {
      "status": "pruned",
      "sha256": object_row["sha256"],
      "size_bytes": object_row["size"],
      "already_missing": already_missing,
    }

  def prune_raw_video(
    self,
    context: JobContext,
    payload: dict[str, Any],
  ) -> dict[str, Any]:
    artifact_id = self._required_payload_string(payload, "artifact_id")
    result = self._prune_raw_video_object(artifact_id)
    _context_progress(context, 1.0)
    return result

  def recover_pending_raw_video_prunes(self) -> list[dict[str, Any]]:
    rows = self.database.query_all(
      """
      SELECT MIN(a.id) AS id
      FROM objects o
      JOIN artifacts a ON a.object_sha256 = o.sha256
      WHERE o.storage_state = 'prune_pending'
        AND a.source_artifact_id IS NULL
      GROUP BY o.sha256
      ORDER BY o.sha256
      """,
    )
    return [self._prune_raw_video_object(row["id"]) for row in rows]

  @staticmethod
  def _strict_ndjson(value: str, description: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(value.splitlines(), start=1):
      if not line.strip():
        continue
      try:
        record = strict_json_loads(line)
      except (ValueError, json.JSONDecodeError) as error:
        raise IntegrationError(
          "invalid_subprocess_json",
          f"{description} contains invalid JSON.",
          details={"line": line_number},
        ) from error
      if not isinstance(record, dict):
        raise IntegrationError(
          "invalid_subprocess_json",
          f"{description} must contain one JSON object per line.",
          details={"line": line_number},
        )
      records.append(record)
    return records

  @staticmethod
  def _structured_process_error(stderr: str) -> dict[str, Any]:
    try:
      records = IntegrationHandlers._strict_ndjson(
        stderr,
        "integration subprocess stderr",
      )
    except IntegrationError:
      return {
        "code": "subprocess_failed",
        "message": "The integration subprocess failed without a structured error.",
      }
    for record in reversed(records):
      error = record.get("error")
      if isinstance(error, dict):
        return error
    return {
      "code": "subprocess_failed",
      "message": "The integration subprocess failed without a structured error.",
    }

  def _validate_media_result(
    self,
    result: Mapping[str, Any],
    source: sqlite3.Row,
    expected_video: Path,
    expected_metadata: Path,
    expected_poster: Path,
    expected_thumbnails: Path,
    expected_frame_index: Path,
    *,
    expected_job_id: str,
    expected_encode: Mapping[str, Any],
  ) -> dict[str, Any]:
    if not _exact_int(result.get("schema_version"), MEDIA_SCHEMA_VERSION):
      raise IntegrationError(
        "media_contract_mismatch",
        "The media worker returned an unsupported schema version.",
      )
    if result.get("job_id") != expected_job_id or result.get("status") not in {
      "complete",
      "already_complete",
    }:
      raise IntegrationError(
        "media_not_complete",
        "The media worker did not report a completed output.",
      )
    input_result = result.get("input")
    output = result.get("output")
    if not isinstance(input_result, Mapping) or not isinstance(output, Mapping):
      raise IntegrationError(
        "media_contract_mismatch",
        "The media worker result is missing input or output metadata.",
      )
    if (
      input_result.get("artifact_id") != source["id"]
      or str(input_result.get("sha256", "")).lower() != source["object_sha256"]
      or input_result.get("camera") != self._camera(source)
      or input_result.get("kind") != source["kind"]
      or input_result.get("input_format") != self._media_input_format(source)
      or input_result.get("segment_num") != source["segment_number"]
    ):
      raise IntegrationError(
        "media_input_mismatch",
        "The media worker result does not describe the requested source artifact.",
      )
    input_probe = input_result.get("probe")
    input_size = input_result.get("size_bytes")
    returned_encode = result.get("encode")
    if (
      not isinstance(input_probe, Mapping)
      or not _exact_int(input_size, source["size"])
      or not _exact_int(input_probe.get("size_bytes"), source["size"])
      or not isinstance(returned_encode, Mapping)
      or dict(returned_encode) != dict(expected_encode)
    ):
      raise IntegrationError(
        "media_bitrate_policy_invalid",
        "The media worker did not bind its bitrate measurements to the submitted source and encode settings.",
      )
    raw_frame_rate = expected_encode.get("raw_hevc_frame_rate")
    if isinstance(raw_frame_rate, bool) or not isinstance(raw_frame_rate, int) or raw_frame_rate <= 0:
      raise IntegrationError(
        "media_bitrate_policy_invalid",
        "The submitted media encode settings have no valid raw HEVC frame rate.",
      )
    input_format = self._media_input_format(source)
    if input_format == "raw_hevc":
      frame_count = input_probe.get("frame_count")
      if (
        input_probe.get("raw_elementary_stream") is not True
        or input_probe.get("codec_name") != "hevc"
        or isinstance(frame_count, bool)
        or not isinstance(frame_count, int)
        or frame_count <= 0
        or not _exact_int(
          input_result.get("raw_hevc_frame_rate_applied"),
          raw_frame_rate,
        )
      ):
        raise IntegrationError(
          "media_bitrate_policy_invalid",
          "The raw HEVC source probe cannot support an independent bitrate calculation.",
        )
      duration_numerator = frame_count * 1_000_000
      input_duration_us = (duration_numerator * 2 + raw_frame_rate) // (raw_frame_rate * 2)
    elif input_format == "mpegts":
      input_duration = input_probe.get("duration_seconds")
      if (
        input_probe.get("raw_elementary_stream") is not False
        or input_result.get("raw_hevc_frame_rate_applied") is not None
        or isinstance(input_duration, bool)
        or not isinstance(input_duration, (int, float))
        or not math.isfinite(float(input_duration))
        or float(input_duration) <= 0
      ):
        raise IntegrationError(
          "media_bitrate_policy_invalid",
          "The MPEG-TS source probe cannot support an independent bitrate calculation.",
        )
      input_duration_us = round(float(input_duration) * 1_000_000)
    else:
      raise IntegrationError(
        "media_bitrate_policy_invalid",
        "The source format has no version 1 bitrate-policy calculation.",
      )
    if input_duration_us <= 0:
      raise IntegrationError(
        "media_bitrate_policy_invalid",
        "The source probe produced a non-positive bitrate duration.",
      )
    input_bitrate_bps = (source["size"] * 8 * 1_000_000 + input_duration_us - 1) // input_duration_us
    video = output.get("video")
    if not isinstance(video, Mapping):
      raise IntegrationError(
        "media_contract_mismatch",
        "The media worker result is missing video metadata.",
      )
    if Path(str(video.get("path", ""))).resolve() != expected_video.resolve():
      raise IntegrationError(
        "media_output_mismatch",
        "The media worker returned an unexpected output path.",
      )
    if video.get("mime_type") != "video/webm":
      raise IntegrationError(
        "media_output_invalid",
        "The completed media output is not WebM.",
      )
    probe = video.get("probe")
    if not isinstance(probe, Mapping) or probe.get("codec_name") != "av1":
      raise IntegrationError(
        "media_output_invalid",
        "The completed media output did not validate as AV1.",
      )
    if video.get("decoded") is not True or video.get("cues_front_loaded") is not True:
      raise IntegrationError(
        "media_output_invalid",
        "The completed media output failed decode or seek-cue validation.",
      )
    duration = probe.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(float(duration)) or float(duration) <= 0:
      raise IntegrationError(
        "media_output_invalid",
        "The completed media output has no positive finite duration.",
      )
    digest = str(video.get("sha256", "")).lower()
    size_value = video.get("size_bytes")
    if (
      len(digest) != 64
      or any(character not in "0123456789abcdef" for character in digest)
      or isinstance(size_value, bool)
      or not isinstance(size_value, int)
      or size_value <= 0
    ):
      raise IntegrationError(
        "media_output_invalid",
        "The media worker returned invalid output integrity metadata.",
      )
    if result.get("raw_retained") is not True:
      raise IntegrationError(
        "raw_retention_violation",
        "The media worker did not confirm retention of the raw source.",
      )
    actual_digest, actual_size = self._hash_file(expected_video)
    if actual_digest != digest or actual_size != size_value:
      raise IntegrationError(
        "media_output_integrity_failed",
        "The published media output does not match worker integrity metadata.",
        details={
          "expected_sha256": digest,
          "actual_sha256": actual_digest,
          "expected_size": size_value,
          "actual_size": actual_size,
        },
      )
    if actual_size >= source["size"]:
      raise IntegrationError(
        "media_bitrate_policy_invalid",
        "The published media output is not smaller than its source artifact.",
        details={
          "source_size": source["size"],
          "output_size": actual_size,
        },
      )
    output_duration_us = round(float(duration) * 1_000_000)
    if output_duration_us <= 0 or not _exact_int(
      probe.get("size_bytes"),
      actual_size,
    ):
      raise IntegrationError(
        "media_bitrate_policy_invalid",
        "The output probe does not bind its bitrate duration and size to the published file.",
      )
    output_bitrate_bps = (actual_size * 8 * 1_000_000 + output_duration_us - 1) // output_duration_us
    audio_codec_name = input_probe.get("audio_codec_name")
    preserve_audio = expected_encode.get("preserve_audio")
    audio_bitrate_kbps = expected_encode.get("audio_bitrate_kbps")
    if (
      (audio_codec_name is not None and (not isinstance(audio_codec_name, str) or not audio_codec_name))
      or not isinstance(preserve_audio, bool)
      or isinstance(audio_bitrate_kbps, bool)
      or not isinstance(audio_bitrate_kbps, int)
      or audio_bitrate_kbps <= 0
    ):
      raise IntegrationError(
        "media_bitrate_policy_invalid",
        "The source audio probe or submitted audio settings are invalid.",
      )
    bitrate_policy = result.get("bitrate_policy")
    policy_keys = {
      "policy_version",
      "policy",
      "initial_target_ratio",
      "fallback_target_ratio",
      "selected_budget",
      "output_duration_us",
      "output_total_bitrate_bps",
      "output_to_input_ratio",
      "bitrate_reduced",
      "size_reduced",
      "accepted",
    }
    selected_budget = bitrate_policy.get("selected_budget") if isinstance(bitrate_policy, Mapping) else None
    budget_keys = {
      "attempt",
      "target_ratio",
      "input_duration_us",
      "input_total_bitrate_bps",
      "target_total_bitrate_bps",
      "reserved_audio_bitrate_bps",
      "reserved_container_bitrate_bps",
      "video_maxrate_bps",
    }

    def exact_ratio(
      value: Any,
      numerator: int,
      denominator: int,
    ) -> bool:
      if not isinstance(value, Mapping) or set(value) != {
        "numerator",
        "denominator",
        "decimal",
      }:
        return False
      decimal = value.get("decimal")
      return (
        _exact_int(value.get("numerator"), numerator)
        and _exact_int(value.get("denominator"), denominator)
        and not isinstance(decimal, bool)
        and isinstance(decimal, (int, float))
        and math.isfinite(float(decimal))
        and float(decimal) == numerator / denominator
      )

    if (
      not isinstance(bitrate_policy, Mapping)
      or set(bitrate_policy) != policy_keys
      or not _exact_int(bitrate_policy.get("policy_version"), 1)
      or bitrate_policy.get("policy") != "strictly_lower_total_average_bitrate"
      or bitrate_policy.get("accepted") is not True
      or bitrate_policy.get("bitrate_reduced") is not True
      or bitrate_policy.get("size_reduced") is not True
      or not exact_ratio(
        bitrate_policy.get("initial_target_ratio"),
        4,
        5,
      )
      or not exact_ratio(
        bitrate_policy.get("fallback_target_ratio"),
        3,
        5,
      )
      or not isinstance(selected_budget, Mapping)
      or set(selected_budget) != budget_keys
    ):
      raise IntegrationError(
        "media_bitrate_policy_invalid",
        "The media worker did not satisfy the exact version 1 lower-bitrate policy contract.",
      )
    attempt = selected_budget.get("attempt")
    if not _exact_int(attempt, 1) and not _exact_int(attempt, 2):
      raise IntegrationError(
        "media_bitrate_policy_invalid",
        "The media worker returned an invalid bitrate-policy attempt.",
      )
    target_numerator, target_denominator = (4, 5) if attempt == 1 else (3, 5)
    target_total_bitrate_bps = (input_bitrate_bps * target_numerator) // target_denominator
    reserved_container_bitrate_bps = max(
      8_000,
      (target_total_bitrate_bps + 19) // 20,
    )
    preserves_source_audio = input_format == "mpegts" and preserve_audio and audio_codec_name is not None
    reserved_audio_bitrate_bps = (audio_bitrate_kbps * 1_000 * 5 + 3) // 4 if preserves_source_audio else 0
    video_maxrate_bps = target_total_bitrate_bps - reserved_audio_bitrate_bps - reserved_container_bitrate_bps
    common_divisor = math.gcd(
      output_bitrate_bps,
      input_bitrate_bps,
    )
    if (
      not exact_ratio(
        selected_budget.get("target_ratio"),
        target_numerator,
        target_denominator,
      )
      or not _exact_int(
        selected_budget.get("input_duration_us"),
        input_duration_us,
      )
      or not _exact_int(
        selected_budget.get("input_total_bitrate_bps"),
        input_bitrate_bps,
      )
      or not _exact_int(
        selected_budget.get("target_total_bitrate_bps"),
        target_total_bitrate_bps,
      )
      or not _exact_int(
        selected_budget.get("reserved_audio_bitrate_bps"),
        reserved_audio_bitrate_bps,
      )
      or not _exact_int(
        selected_budget.get("reserved_container_bitrate_bps"),
        reserved_container_bitrate_bps,
      )
      or not _exact_int(
        selected_budget.get("video_maxrate_bps"),
        video_maxrate_bps,
      )
      or video_maxrate_bps < 1_000
      or not _exact_int(
        bitrate_policy.get("output_duration_us"),
        output_duration_us,
      )
      or not _exact_int(
        bitrate_policy.get("output_total_bitrate_bps"),
        output_bitrate_bps,
      )
      or output_bitrate_bps >= input_bitrate_bps
      or not exact_ratio(
        bitrate_policy.get("output_to_input_ratio"),
        output_bitrate_bps // common_divisor,
        input_bitrate_bps // common_divisor,
      )
    ):
      raise IntegrationError(
        "media_bitrate_policy_invalid",
        "The media worker bitrate measurements or budget do not match the independently recomputed policy.",
      )
    poster = output.get("poster")
    thumbnails = output.get("thumbnails")
    expected_thumbnail_paths = [expected_thumbnails / f"{index:03d}.jpg" for index in range(6)]
    if not isinstance(poster, Mapping) or not isinstance(thumbnails, list) or len(thumbnails) != len(expected_thumbnail_paths):
      raise IntegrationError(
        "media_artwork_missing",
        "The media worker result is missing its poster or thumbnail set.",
      )

    def validate_artwork(
      record: Mapping[str, Any],
      expected_path: Path,
      role: str,
      ordinal: int | None,
    ) -> dict[str, Any]:
      digest_value = record.get("sha256")
      size_bytes = record.get("size_bytes")
      timestamp_seconds = record.get("timestamp_seconds")
      if (
        Path(str(record.get("path", ""))).resolve() != expected_path.resolve()
        or record.get("mime_type") != "image/jpeg"
        or not self._sha256_digest(digest_value)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
        or isinstance(timestamp_seconds, bool)
        or not isinstance(timestamp_seconds, (int, float))
        or not math.isfinite(float(timestamp_seconds))
        or float(timestamp_seconds) < 0
        or float(timestamp_seconds) > float(duration)
        or not expected_path.is_file()
      ):
        raise IntegrationError(
          "media_artwork_invalid",
          "The media worker returned invalid poster or thumbnail metadata.",
          details={"role": role, "ordinal": ordinal},
        )
      actual_artwork_digest, actual_artwork_size = self._hash_file(expected_path)
      if actual_artwork_digest != digest_value or actual_artwork_size != size_bytes:
        raise IntegrationError(
          "media_artwork_integrity_failed",
          "A published poster or thumbnail does not match worker integrity metadata.",
          details={
            "role": role,
            "ordinal": ordinal,
            "expected_sha256": digest_value,
            "actual_sha256": actual_artwork_digest,
            "expected_size": size_bytes,
            "actual_size": actual_artwork_size,
          },
        )
      return {
        **dict(record),
        "sha256": digest_value,
        "size_bytes": size_bytes,
        "timestamp_seconds": float(timestamp_seconds),
        "storage_path": expected_path.relative_to(
          self.archive_root,
        ).as_posix(),
        "role": role,
        "ordinal": ordinal,
      }

    validated_poster = validate_artwork(
      poster,
      expected_poster,
      "poster",
      None,
    )
    validated_thumbnails = [
      validate_artwork(
        thumbnail,
        expected_path,
        "thumbnail",
        ordinal,
      )
      for ordinal, (thumbnail, expected_path) in enumerate(
        zip(
          thumbnails,
          expected_thumbnail_paths,
          strict=True,
        ),
      )
      if isinstance(thumbnail, Mapping)
    ]
    if len(validated_thumbnails) != len(expected_thumbnail_paths) or any(
      left["timestamp_seconds"] > right["timestamp_seconds"]
      for left, right in zip(
        validated_thumbnails,
        validated_thumbnails[1:],
        strict=False,
      )
    ):
      raise IntegrationError(
        "media_artwork_invalid",
        "The media thumbnail set is incomplete or not time-ordered.",
      )
    frame_index = output.get("frame_index")
    if not isinstance(frame_index, Mapping):
      raise IntegrationError(
        "media_frame_index_missing",
        "The media worker result is missing its frame synchronization index.",
      )
    if Path(str(frame_index.get("path", ""))).resolve() != expected_frame_index.resolve():
      raise IntegrationError(
        "media_frame_index_mismatch",
        "The media worker returned an unexpected frame-index path.",
      )
    frame_index_digest = str(frame_index.get("sha256", "")).lower()
    frame_index_size = frame_index.get("size_bytes")
    frame_index_count = frame_index.get("frame_count")
    probe_frame_count = probe.get("frame_count")
    if (
      frame_index.get("mime_type") != "application/json"
      or not _exact_int(frame_index.get("schema_version"), 1)
      or len(frame_index_digest) != 64
      or any(character not in "0123456789abcdef" for character in frame_index_digest)
      or isinstance(frame_index_size, bool)
      or not isinstance(frame_index_size, int)
      or frame_index_size <= 0
      or isinstance(frame_index_count, bool)
      or not isinstance(frame_index_count, int)
      or frame_index_count <= 0
      or isinstance(probe_frame_count, bool)
      or not isinstance(probe_frame_count, int)
      or frame_index_count != probe_frame_count
      or not isinstance(frame_index.get("time_base"), Mapping)
      or frame_index.get("join_key") != ["camera", "segment_num", "segment_frame_id"]
      or frame_index.get("ordinal_basis") != 0
      or frame_index.get("source_frame_key") != "segment_frame_id"
      or frame_index.get("camera") != self._camera(source)
      or frame_index.get("segment_num") != source["segment_number"]
      or frame_index.get("source_artifact_id") != source["id"]
    ):
      raise IntegrationError(
        "media_frame_index_invalid",
        "The media frame index violates its version 1 result contract.",
      )
    actual_frame_digest, actual_frame_size = self._hash_file(
      expected_frame_index,
    )
    if actual_frame_digest != frame_index_digest or actual_frame_size != frame_index_size:
      raise IntegrationError(
        "media_frame_index_integrity_failed",
        "The published frame index does not match worker integrity metadata.",
      )
    try:
      frame_index_document = strict_json_loads(
        expected_frame_index.read_text(encoding="utf-8"),
      )
    except (OSError, ValueError, json.JSONDecodeError) as error:
      raise IntegrationError(
        "media_frame_index_invalid",
        "The published frame index is not canonical JSON.",
      ) from error
    if not isinstance(frame_index_document, Mapping):
      raise IntegrationError(
        "media_frame_index_invalid",
        "The published frame index must be a JSON object.",
      )
    frames = frame_index_document.get("frames")
    video_identity = frame_index_document.get("video")
    if (
      not _exact_int(frame_index_document.get("schema_version"), 1)
      or frame_index_document.get("mapping_type") != "encoded_frame_pts"
      or frame_index_document.get("join_key") != ["camera", "segment_num", "segment_frame_id"]
      or frame_index_document.get("ordinal_basis") != 0
      or frame_index_document.get("source_frame_key") != "segment_frame_id"
      or frame_index_document.get("camera") != self._camera(source)
      or frame_index_document.get("segment_num") != source["segment_number"]
      or frame_index_document.get("source_artifact_id") != source["id"]
      or frame_index_document.get("frame_count") != frame_index_count
      or frame_index_document.get("time_base") != frame_index.get("time_base")
      or not isinstance(video_identity, Mapping)
      or Path(str(video_identity.get("path", ""))).resolve() != expected_video.resolve()
      or video_identity.get("sha256") != digest
      or not isinstance(frames, list)
      or len(frames) != frame_index_count
      or any(
        not isinstance(frame, Mapping)
        or frame.get("ordinal") != ordinal
        or frame.get("segment_frame_id") != ordinal
        or isinstance(frame.get("pts"), bool)
        or not isinstance(frame.get("pts"), int)
        or isinstance(frame.get("duration"), bool)
        or not isinstance(frame.get("duration"), int)
        or frame["duration"] <= 0
        or not isinstance(frame.get("pts_us"), int)
        or not isinstance(frame.get("duration_us"), int)
        or frame["duration_us"] <= 0
        or not isinstance(frame.get("keyframe"), bool)
        for ordinal, frame in enumerate(frames)
      )
    ):
      raise IntegrationError(
        "media_frame_index_invalid",
        "The published frame index identity or ordinal mapping is invalid.",
      )
    if not expected_metadata.is_file():
      raise IntegrationError(
        "media_completion_marker_missing",
        "The media worker completion metadata is missing.",
      )
    try:
      metadata_result = strict_json_loads(
        expected_metadata.read_text(encoding="utf-8"),
      )
    except (OSError, ValueError, json.JSONDecodeError) as error:
      raise IntegrationError(
        "media_completion_marker_invalid",
        "The media worker completion metadata is invalid.",
      ) from error
    metadata_output = metadata_result.get("output") if isinstance(metadata_result, Mapping) else None
    metadata_thumbnails = metadata_output.get("thumbnails") if isinstance(metadata_output, Mapping) else None
    metadata_video = metadata_output.get("video") if isinstance(metadata_output, Mapping) else None
    metadata_frame_index = metadata_output.get("frame_index") if isinstance(metadata_output, Mapping) else None
    metadata_poster = metadata_output.get("poster") if isinstance(metadata_output, Mapping) else None
    metadata_bitrate_policy = metadata_result.get("bitrate_policy") if isinstance(metadata_result, Mapping) else None
    expected_metadata_result = dict(result)
    expected_metadata_result["status"] = "complete" if result.get("status") == "already_complete" else result.get("status")
    if (
      not isinstance(metadata_result, Mapping)
      or not isinstance(metadata_output, Mapping)
      or not isinstance(metadata_video, Mapping)
      or not isinstance(metadata_frame_index, Mapping)
      or not isinstance(metadata_poster, Mapping)
      or metadata_result.get("job_id") != result.get("job_id")
      or metadata_result.get("status") != ("complete" if result.get("status") == "already_complete" else result.get("status"))
      or dict(metadata_result) != expected_metadata_result
      or metadata_bitrate_policy != bitrate_policy
      or metadata_video.get("sha256") != digest
      or metadata_frame_index.get("sha256") != frame_index_digest
      or metadata_poster.get("sha256") != validated_poster["sha256"]
      or [item.get("sha256") if isinstance(item, Mapping) else None for item in (metadata_thumbnails if isinstance(metadata_thumbnails, list) else [])]
      != [item["sha256"] for item in validated_thumbnails]
    ):
      raise IntegrationError(
        "media_completion_marker_invalid",
        "The media completion marker does not match the terminal result.",
      )
    return {
      "sha256": digest,
      "size_bytes": size_value,
      "codec": "av1",
      "mime_type": "video/webm",
      "duration_us": round(float(duration) * 1_000_000),
      "width": probe.get("width"),
      "height": probe.get("height"),
      "fps": probe.get("average_frame_rate"),
      "frame_count": probe_frame_count,
      "pixel_format": probe.get("pixel_format"),
      "audio_codec": probe.get("audio_codec_name"),
      "metadata": dict(result),
      "validation": {
        "decoded": video.get("decoded"),
        "cues_front_loaded": video.get("cues_front_loaded"),
        "probe": dict(probe),
      },
      "encoder": {
        "encode": result.get("encode", {}),
        "tools": result.get("tools", {}),
      },
      "frame_index": {
        **dict(frame_index),
        "sha256": frame_index_digest,
        "size_bytes": frame_index_size,
        "storage_path": expected_frame_index.relative_to(
          self.archive_root,
        ).as_posix(),
        "document": dict(frame_index_document),
      },
      "poster": validated_poster,
      "thumbnails": validated_thumbnails,
    }

  def _catalog_derived_video(
    self,
    source: sqlite3.Row,
    storage_path: str,
    output: Mapping[str, Any],
  ) -> tuple[str, str, str, list[str], dict[str, Any]]:
    now = _now_text()
    frame_index = output["frame_index"]
    poster = output["poster"]
    thumbnails = output["thumbnails"]
    frame_storage_path = frame_index["storage_path"]
    object_candidates = [
      (output["sha256"], output["size_bytes"], storage_path),
      (
        frame_index["sha256"],
        frame_index["size_bytes"],
        frame_storage_path,
      ),
      (
        poster["sha256"],
        poster["size_bytes"],
        poster["storage_path"],
      ),
      *[
        (
          thumbnail["sha256"],
          thumbnail["size_bytes"],
          thumbnail["storage_path"],
        )
        for thumbnail in thumbnails
      ],
    ]
    canonical_paths: dict[str, str] = {}
    with self.database.transaction(immediate=True) as connection:
      current = connection.execute(
        """
        SELECT id, device_id, drive_id, segment_id, camera
        FROM artifacts
        WHERE id = ?
        """,
        (source["id"],),
      ).fetchone()
      if current is None:
        raise IntegrationError(
          "artifact_not_found",
          "The source artifact disappeared before media cataloging.",
        )
      for digest, size, path in object_candidates:
        if (
          not isinstance(path, str)
          or not path
          or not isinstance(size, int)
          or isinstance(size, bool)
          or size <= 0
          or not isinstance(digest, str)
          or len(digest) != 64
          or any(character not in "0123456789abcdef" for character in digest)
        ):
          raise IntegrationError(
            "media_output_integrity_failed",
            "A validated media output has invalid catalog identity.",
          )
        proposed_file = self._archive_path(
          path,
          required_root=self.archive_root / "derived",
        )
        path_owner = connection.execute(
          """
          SELECT sha256, size
          FROM objects
          WHERE storage_path = ?
          """,
          (path,),
        ).fetchone()
        if path_owner is not None and (
          path_owner["sha256"] != digest
          or path_owner["size"] != size
        ):
          raise IntegrationError(
            "object_store_collision",
            "A derived media path is already bound to a different immutable object.",
            details={"storage_path": path},
          )
        object_row = connection.execute(
          "SELECT size, storage_path FROM objects WHERE sha256 = ?",
          (digest,),
        ).fetchone()
        if object_row is not None and object_row["size"] != size:
          raise IntegrationError(
            "object_store_collision",
            "An existing object row has the output digest but a different size.",
          )
        if object_row is None:
          connection.execute(
            """
            INSERT INTO objects(sha256, size, storage_path, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (digest, size, path, now),
          )
          canonical_path = path
        else:
          canonical_path = object_row["storage_path"]
          canonical_file = self._archive_path(
            canonical_path,
            required_root=self.archive_root,
          )
          if canonical_file != proposed_file:
            canonical_digest, canonical_size = self._hash_file(canonical_file)
            if canonical_digest != digest or canonical_size != size:
              raise IntegrationError(
                "object_store_integrity_failed",
                "The canonical derived object no longer matches its immutable catalog entry.",
                details={
                  "storage_path": canonical_path,
                  "expected_sha256": digest,
                  "actual_sha256": canonical_digest,
                  "expected_size": size,
                  "actual_size": canonical_size,
                },
              )
        canonical_paths[path] = canonical_path
      video_storage_path = canonical_paths[storage_path]
      frame_object_path = canonical_paths[frame_storage_path]
      poster_object_path = canonical_paths[poster["storage_path"]]
      thumbnail_object_paths = [
        canonical_paths[thumbnail["storage_path"]]
        for thumbnail in thumbnails
      ]
      existing = connection.execute(
        """
        SELECT id
        FROM artifacts
        WHERE source_artifact_id = ?
          AND kind = 'derived_video'
          AND object_sha256 = ?
          AND relative_path = ?
          AND status = 'ready'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (source["id"], output["sha256"], storage_path),
      ).fetchone()
      if existing is not None:
        artifact_id = existing["id"]
      else:
        artifact_id = uuid4().hex
        connection.execute(
          """
          INSERT INTO artifacts(
            id, device_id, drive_id, segment_id, object_sha256,
            kind, camera, relative_path, storage_path, size,
            mime_type, codec, duration_us, width, height, fps,
            frame_count, pixel_format, audio_codec, metadata_json,
            validation_json, encoder_json, time_map_path, status,
            source_artifact_id, created_at
          ) VALUES (
            ?, ?, ?, ?, ?, 'derived_video', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?
          )
          """,
          (
            artifact_id,
            current["device_id"],
            current["drive_id"],
            current["segment_id"],
            output["sha256"],
            self._camera(source),
            storage_path,
            video_storage_path,
            output["size_bytes"],
            output["mime_type"],
            output["codec"],
            output["duration_us"],
            output["width"],
            output["height"],
            output["fps"],
            output["frame_count"],
            output["pixel_format"],
            output["audio_codec"],
            canonical_json(output["metadata"]),
            canonical_json(output["validation"]),
            canonical_json(output["encoder"]),
            None,
            source["id"],
            now,
          ),
        )
      existing_frame_index = connection.execute(
        """
        SELECT id
        FROM artifacts
        WHERE source_artifact_id = ?
          AND kind = 'video_frame_index'
          AND object_sha256 = ?
          AND relative_path = ?
          AND status = 'ready'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (
          artifact_id,
          frame_index["sha256"],
          frame_storage_path,
        ),
      ).fetchone()
      if existing_frame_index is not None:
        frame_index_id = existing_frame_index["id"]
      else:
        frame_index_id = uuid4().hex
        frame_metadata = {key: value for key, value in frame_index.items() if key != "document"}
        connection.execute(
          """
          INSERT INTO artifacts(
            id, device_id, drive_id, segment_id, object_sha256,
            kind, camera, relative_path, storage_path, size,
            mime_type, duration_us, frame_count, metadata_json,
            validation_json, status, source_artifact_id, created_at
          ) VALUES (
            ?, ?, ?, ?, ?, 'video_frame_index', ?, ?, ?, ?, ?,
            ?, ?, ?, ?, 'ready', ?, ?
          )
          """,
          (
            frame_index_id,
            current["device_id"],
            current["drive_id"],
            current["segment_id"],
            frame_index["sha256"],
            self._camera(source),
            frame_storage_path,
            frame_object_path,
            frame_index["size_bytes"],
            frame_index["mime_type"],
            output["duration_us"],
            frame_index["frame_count"],
            canonical_json(frame_metadata),
            canonical_json(
              {
                "sha256": frame_index["sha256"],
                "schema_version": frame_index["schema_version"],
                "time_base": frame_index["time_base"],
              }
            ),
            artifact_id,
            now,
          ),
        )
      connection.execute(
        """
        UPDATE artifacts
        SET status = 'stale'
        WHERE source_artifact_id = ?
          AND kind = 'video_frame_index'
          AND id != ?
          AND status = 'ready'
        """,
        (artifact_id, frame_index_id),
      )
      poster_id = ""
      thumbnail_ids: list[str] = []
      image_specs = [
        ("poster", poster),
        *(("thumbnail", thumbnail) for thumbnail in thumbnails),
      ]
      for image_index, (image_kind, image) in enumerate(image_specs):
        image_object_path = (
          poster_object_path
          if image_kind == "poster"
          else thumbnail_object_paths[image_index - 1]
        )
        existing_image = connection.execute(
          """
          SELECT id
          FROM artifacts
          WHERE source_artifact_id = ?
            AND kind = ?
            AND object_sha256 = ?
            AND relative_path = ?
            AND status = 'ready'
          ORDER BY created_at DESC, id DESC
          LIMIT 1
          """,
          (
            artifact_id,
              image_kind,
              image["sha256"],
              image["storage_path"],
          ),
        ).fetchone()
        if existing_image is not None:
          image_id = existing_image["id"]
        else:
          image_id = uuid4().hex
          connection.execute(
            """
            INSERT INTO artifacts(
              id, device_id, drive_id, segment_id, object_sha256,
              kind, camera, relative_path, storage_path, size,
              mime_type, metadata_json, validation_json, status,
              source_artifact_id, created_at
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'image/jpeg',
              ?, ?, 'ready', ?, ?
            )
            """,
            (
              image_id,
              current["device_id"],
              current["drive_id"],
              current["segment_id"],
              image["sha256"],
              image_kind,
              self._camera(source),
              image["storage_path"],
              image_object_path,
              image["size_bytes"],
              canonical_json(dict(image)),
              canonical_json(
                {
                  "sha256": image["sha256"],
                  "size_bytes": image["size_bytes"],
                  "timestamp_seconds": image["timestamp_seconds"],
                  "role": image["role"],
                  "ordinal": image["ordinal"],
                }
              ),
              artifact_id,
              now,
            ),
          )
        if image_kind == "poster":
          poster_id = image_id
        else:
          thumbnail_ids.append(image_id)
      if not poster_id or len(thumbnail_ids) != len(thumbnails):
        raise IntegrationError(
          "media_artwork_catalog_failed",
          "The validated poster or thumbnail set was not fully cataloged.",
        )
      connection.execute(
        """
        UPDATE artifacts
        SET status = 'stale'
        WHERE source_artifact_id = ?
          AND kind = 'poster'
          AND id != ?
          AND status = 'ready'
        """,
        (artifact_id, poster_id),
      )
      thumbnail_placeholders = ",".join("?" for _ in thumbnail_ids)
      connection.execute(
        f"""
        UPDATE artifacts
        SET status = 'stale'
        WHERE source_artifact_id = ?
          AND kind = 'thumbnail'
          AND id NOT IN ({thumbnail_placeholders})
          AND status = 'ready'
        """,
        (artifact_id, *thumbnail_ids),
      )
      connection.execute(
        """
        UPDATE artifacts
        SET time_map_path = (
          SELECT storage_path
          FROM artifacts
          WHERE source_artifact_id = ?
            AND kind = 'video_telemetry_sync'
            AND status = 'ready'
          ORDER BY created_at DESC, id DESC
          LIMIT 1
        )
        WHERE id = ?
        """,
        (artifact_id, artifact_id),
      )
      if current["segment_id"] is not None:
        connection.execute(
          """
          UPDATE segments
          SET duration_us = COALESCE(duration_us, ?)
          WHERE id = ?
          """,
          (output["duration_us"], current["segment_id"]),
        )
    return (
      artifact_id,
      frame_index_id,
      poster_id,
      thumbnail_ids,
      {
        "video": video_storage_path,
        "frame_index": frame_object_path,
        "poster": poster_object_path,
        "thumbnails": thumbnail_object_paths,
      },
    )

  def build_media_sync(
    self,
    context: JobContext,
    payload: dict[str, Any],
  ) -> dict[str, Any]:
    artifact_id = self._required_payload_string(payload, "artifact_id")
    video = self._artifact(artifact_id)
    if video["kind"] != "derived_video" or video["status"] != "ready":
      raise IntegrationError(
        "derived_video_required",
        "Media synchronization requires a ready derived video artifact.",
        details={"artifact_id": artifact_id},
      )
    if video["drive_id"] is None or video["segment_id"] is None or video["segment_number"] is None:
      raise IntegrationError(
        "video_segment_required",
        "The derived video is not attached to a numbered drive segment.",
      )
    generation = self._media_sync_generation(video["drive_id"])
    if not self._media_sync_generation_matches(generation):
      raise IntegrationError(
        "telemetry_not_ready",
        "Media synchronization requires telemetry bound to the latest immutable route inventory.",
        retryable=True,
      )
    camera = self._camera(video)
    video_path = self._archive_path(
      video["object_storage_path"],
      required_root=self.archive_root,
    )
    video_sha256, video_size = self._hash_file(video_path)
    if (
      video["storage_path"] != video["object_storage_path"]
      or video_sha256 != video["object_sha256"]
      or video_size != video["object_size"]
      or video_size != video["size"]
    ):
      raise IntegrationError(
        "media_sync_source_invalid",
        "The derived video failed immutable source verification.",
      )
    frame_rows = self.database.query_all(
      """
      SELECT id
      FROM artifacts
      WHERE source_artifact_id = ?
        AND kind = 'video_frame_index'
        AND status = 'ready'
      ORDER BY created_at DESC, id DESC
      """,
      (artifact_id,),
    )
    if len(frame_rows) != 1:
      raise IntegrationError(
        "media_frame_index_not_ready",
        "The derived video must have exactly one ready frame-index sidecar.",
        details={"count": len(frame_rows)},
        retryable=not frame_rows,
      )
    frame_artifact = self._artifact(frame_rows[0]["id"])
    frame_path = self._archive_path(
      frame_artifact["object_storage_path"],
      required_root=self.archive_root,
    )
    frame_sha256, frame_size = self._hash_file(frame_path)
    if (
      frame_artifact["storage_path"] != frame_artifact["object_storage_path"]
      or frame_sha256 != frame_artifact["object_sha256"]
      or frame_size != frame_artifact["object_size"]
      or frame_size != frame_artifact["size"]
    ):
      raise IntegrationError(
        "media_sync_source_invalid",
        "The encoded-frame index failed immutable source verification.",
      )
    try:
      frame_document = strict_json_loads(
        frame_path.read_text(encoding="utf-8"),
      )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
      raise IntegrationError(
        "media_frame_index_invalid",
        "The encoded-frame index contains invalid JSON.",
      ) from error
    media_frames = self._media_sync_frames(
      frame_document,
      video,
      camera,
    )
    telemetry = self.database.query_one(
      """
      SELECT *
      FROM telemetry_indexes
      WHERE drive_id = ?
      """,
      (video["drive_id"],),
    )
    if telemetry is None or telemetry["state"] != "complete":
      raise IntegrationError(
        "telemetry_not_ready",
        "A complete indexed telemetry generation is required for media synchronization.",
        retryable=True,
      )
    try:
      manifest = strict_json_loads(telemetry["manifest_json"])
    except (ValueError, json.JSONDecodeError) as error:
      raise IntegrationError(
        "telemetry_index_corrupt",
        "The telemetry manifest contains invalid JSON.",
      ) from error
    if not isinstance(manifest, Mapping):
      raise IntegrationError(
        "telemetry_index_corrupt",
        "The telemetry manifest is not a JSON object.",
      )
    if not isinstance(manifest, Mapping) or manifest.get("publication_ready") is not True:
      raise IntegrationError(
        "telemetry_not_ready",
        "The telemetry generation is not publication-ready.",
        retryable=True,
      )
    telemetry_path = self._archive_path(
      telemetry["ndjson_path"],
      required_root=self.archive_root / "telemetry",
    )
    telemetry_sha256, _ = self._hash_file(telemetry_path)
    if telemetry_sha256 != telemetry["ndjson_sha256"]:
      raise IntegrationError(
        "telemetry_integrity_failed",
        "The telemetry generation failed immutable source verification.",
      )
    telemetry_frames, record_hashes = self._telemetry_sync_frames(
      video["drive_id"],
      camera,
      int(video["segment_number"]),
      telemetry_path,
      telemetry["ndjson_path"],
    )
    media_ids = [frame["segment_frame_id"] for frame in media_frames]
    telemetry_ids = [frame["segment_frame_id"] for frame in telemetry_frames]
    if len(media_frames) != len(telemetry_frames) or media_ids != telemetry_ids:
      raise IntegrationError(
        "media_telemetry_join_mismatch",
        "Encoded frames do not have a one-to-one telemetry frame join.",
        details={
          "camera": camera,
          "segment_number": int(video["segment_number"]),
          "media_frame_count": len(media_frames),
          "telemetry_frame_count": len(telemetry_frames),
          "first_media_ids": media_ids[:10],
          "first_telemetry_ids": telemetry_ids[:10],
        },
      )
    timeline_version = manifest.get("timeline_version")
    if not isinstance(timeline_version, str) or not timeline_version:
      raise IntegrationError(
        "telemetry_index_corrupt",
        "The telemetry manifest has no timeline version.",
      )
    frames = [
      {
        "ordinal": ordinal,
        "segment_frame_id": media_frame["segment_frame_id"],
        "pts_us": media_frame["pts_us"],
        "duration_us": media_frame["duration_us"],
        "drive_t_us": telemetry_frame["t_us"],
        "keyframe": media_frame["keyframe"],
      }
      for ordinal, (media_frame, telemetry_frame) in enumerate(
        zip(media_frames, telemetry_frames, strict=True),
      )
    ]
    sync_document = {
      "schema": "comma-companion.video-telemetry-sync",
      "schema_version": 1,
      "join_key": ["camera", "segment_num", "segment_frame_id"],
      "ordinal_basis": 0,
      "camera": camera,
      "segment_num": int(video["segment_number"]),
      "frame_count": len(frames),
      "timeline_version": timeline_version,
      "sources": {
        "video_artifact_id": artifact_id,
        "video_sha256": video_sha256,
        "frame_index_artifact_id": frame_artifact["id"],
        "frame_index_sha256": frame_sha256,
        "telemetry_ndjson_path": telemetry["ndjson_path"],
        "telemetry_ndjson_sha256": telemetry_sha256,
        "telemetry_record_sha256": record_hashes,
      },
      "frames": frames,
    }
    encoded = (canonical_json(sync_document) + "\n").encode("utf-8")
    sync_sha256 = hashlib.sha256(encoded).hexdigest()
    sync_path = video_path.with_name(
      f"{camera}.video-telemetry-sync.{sync_sha256}.json",
    )
    if sync_path.exists():
      existing_sha256, existing_size = self._hash_file(sync_path)
      if existing_sha256 != sync_sha256 or existing_size != len(encoded):
        raise IntegrationError(
          "media_sync_output_collision",
          "An existing media synchronization artifact failed integrity verification.",
        )
    else:
      self._atomic_write_json(sync_path, sync_document)
    storage_path = sync_path.relative_to(self.archive_root).as_posix()
    now = _now_text()
    with self.database.transaction(immediate=True) as connection:
      current_generation = self._media_sync_generation(
        video["drive_id"],
        connection=connection,
      )
      if (
        not self._media_sync_generation_matches(
          current_generation,
        )
        or current_generation["ndjson_sha256"] != telemetry_sha256
      ):
        raise IntegrationError(
          "stale_generation",
          "Telemetry or route inventory changed while the media synchronization artifact was built.",
          retryable=True,
        )
      current_video = connection.execute(
        """
        SELECT status, object_sha256
        FROM artifacts
        WHERE id = ?
        """,
        (artifact_id,),
      ).fetchone()
      if current_video is None or current_video["status"] != "ready" or current_video["object_sha256"] != video_sha256:
        raise IntegrationError(
          "stale_generation",
          "The derived video changed while its synchronization artifact was built.",
          retryable=True,
        )
      object_row = connection.execute(
        "SELECT size, storage_path FROM objects WHERE sha256 = ?",
        (sync_sha256,),
      ).fetchone()
      if object_row is not None and (object_row["size"] != len(encoded) or object_row["storage_path"] != storage_path):
        raise IntegrationError(
          "object_store_collision",
          "An existing object row conflicts with the media sync artifact.",
        )
      connection.execute(
        """
        INSERT INTO objects(sha256, size, storage_path, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(sha256) DO NOTHING
        """,
        (sync_sha256, len(encoded), storage_path, now),
      )
      connection.execute(
        """
        UPDATE artifacts
        SET status = 'stale'
        WHERE source_artifact_id = ?
          AND kind = 'video_telemetry_sync'
          AND object_sha256 != ?
          AND status = 'ready'
        """,
        (artifact_id, sync_sha256),
      )
      existing = connection.execute(
        """
        SELECT id
        FROM artifacts
        WHERE source_artifact_id = ?
          AND kind = 'video_telemetry_sync'
          AND object_sha256 = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (artifact_id, sync_sha256),
      ).fetchone()
      if existing is not None:
        sync_artifact_id = existing["id"]
        connection.execute(
          "UPDATE artifacts SET status = 'ready' WHERE id = ?",
          (sync_artifact_id,),
        )
      else:
        sync_artifact_id = uuid4().hex
        connection.execute(
          """
          INSERT INTO artifacts(
            id, device_id, drive_id, segment_id, object_sha256,
            kind, camera, relative_path, storage_path, size,
            mime_type, duration_us, frame_count, metadata_json,
            validation_json, status, source_artifact_id, created_at
          ) VALUES (
            ?, ?, ?, ?, ?, 'video_telemetry_sync', ?, ?, ?, ?,
            'application/json', ?, ?, ?, ?, 'ready', ?, ?
          )
          """,
          (
            sync_artifact_id,
            video["device_id"],
            video["drive_id"],
            video["segment_id"],
            sync_sha256,
            camera,
            storage_path,
            storage_path,
            len(encoded),
            video["duration_us"],
            len(frames),
            canonical_json(sync_document["sources"]),
            canonical_json(
              {
                "join_key": sync_document["join_key"],
                "frame_count": len(frames),
                "timeline_version": timeline_version,
              }
            ),
            artifact_id,
            now,
          ),
        )
      connection.execute(
        "UPDATE artifacts SET time_map_path = ? WHERE id = ?",
        (storage_path, artifact_id),
      )
    _context_progress(context, 1.0)
    return {
      "status": "ready",
      "artifact_id": sync_artifact_id,
      "source_artifact_id": artifact_id,
      "storage_path": storage_path,
      "sha256": sync_sha256,
      "size_bytes": len(encoded),
      "frame_count": len(frames),
      "timeline_version": timeline_version,
    }

  def _media_sync_generation(
    self,
    drive_id: str,
    *,
    connection: sqlite3.Connection | None = None,
  ) -> sqlite3.Row | None:
    sql = """
      SELECT
        drive.telemetry_ready,
        telemetry.state AS telemetry_state,
        telemetry.ndjson_sha256,
        telemetry.source_fingerprint AS telemetry_source_fingerprint,
        inventory.state AS inventory_state,
        inventory.route_closed AS inventory_route_closed,
        inventory.rlog_source_fingerprint
      FROM drives drive
      LEFT JOIN telemetry_indexes telemetry
        ON telemetry.drive_id = drive.id
      LEFT JOIN route_inventories inventory
        ON inventory.id = (
          SELECT latest.id
          FROM route_inventories latest
          WHERE latest.drive_id = drive.id
          ORDER BY latest.generation DESC
          LIMIT 1
        )
      WHERE drive.id = ?
    """
    if connection is not None:
      return connection.execute(sql, (drive_id,)).fetchone()
    return self.database.query_one(sql, (drive_id,))

  @staticmethod
  def _media_sync_generation_matches(
    generation: sqlite3.Row | None,
  ) -> bool:
    return (
      generation is not None
      and generation["telemetry_ready"] == 1
      and generation["telemetry_state"] == "complete"
      and generation["inventory_state"] == "complete"
      and generation["inventory_route_closed"] == 1
      and isinstance(
        generation["telemetry_source_fingerprint"],
        str,
      )
      and isinstance(generation["rlog_source_fingerprint"], str)
      and secrets.compare_digest(
        generation["telemetry_source_fingerprint"],
        generation["rlog_source_fingerprint"],
      )
    )

  def _attempt_media_sync(
    self,
    context: JobContext,
    artifact_id: str,
  ) -> dict[str, Any]:
    try:
      return self.build_media_sync(
        context,
        {"artifact_id": artifact_id},
      )
    except IntegrationError as error:
      return {
        "status": "not_synchronized",
        "error": error.as_dict(),
      }

  def _media_sync_frames(
    self,
    document: Any,
    video: sqlite3.Row,
    camera: str,
  ) -> list[dict[str, Any]]:
    frames = document.get("frames") if isinstance(document, Mapping) else None
    if (
      not isinstance(document, Mapping)
      or document.get("schema_version") != 1
      or document.get("mapping_type") != "encoded_frame_pts"
      or document.get("join_key") != ["camera", "segment_num", "segment_frame_id"]
      or document.get("ordinal_basis") != 0
      or document.get("source_frame_key") != "segment_frame_id"
      or document.get("camera") != camera
      or document.get("segment_num") != video["segment_number"]
      or document.get("source_artifact_id") != video["source_artifact_id"]
      or not isinstance(document.get("video"), Mapping)
      or document["video"].get("sha256") != video["object_sha256"]
      or not isinstance(frames, list)
      or document.get("frame_count") != len(frames)
      or video["frame_count"] != len(frames)
    ):
      raise IntegrationError(
        "media_frame_index_invalid",
        "The encoded-frame index does not match its derived video.",
      )
    result: list[dict[str, Any]] = []
    for ordinal, frame in enumerate(frames):
      if (
        not isinstance(frame, Mapping)
        or frame.get("ordinal") != ordinal
        or isinstance(frame.get("segment_frame_id"), bool)
        or not isinstance(frame.get("segment_frame_id"), int)
        or isinstance(frame.get("pts_us"), bool)
        or not isinstance(frame.get("pts_us"), int)
        or frame["pts_us"] < 0
        or isinstance(frame.get("duration_us"), bool)
        or not isinstance(frame.get("duration_us"), int)
        or frame["duration_us"] <= 0
        or not isinstance(frame.get("keyframe"), bool)
      ):
        raise IntegrationError(
          "media_frame_index_invalid",
          "The encoded-frame index contains an invalid frame mapping.",
          details={"ordinal": ordinal},
        )
      result.append(dict(frame))
    identifiers = [frame["segment_frame_id"] for frame in result]
    if any(
      left >= right
      for left, right in zip(
        identifiers,
        identifiers[1:],
        strict=False,
      )
    ):
      raise IntegrationError(
        "media_frame_index_invalid",
        "Encoded segment frame IDs are not strictly increasing.",
      )
    return result

  def _telemetry_sync_frames(
    self,
    drive_id: str,
    camera: str,
    segment_number: int,
    telemetry_path: Path,
    ndjson_relative: str,
  ) -> tuple[list[dict[str, Any]], list[str]]:
    rows = self.database.query_all(
      """
      SELECT
        camera, segment_number, chunk_index, ndjson_path,
        byte_offset, byte_length, record_sha256
      FROM telemetry_frame_chunks
      WHERE drive_id = ?
        AND camera = ?
        AND segment_number = ?
      ORDER BY chunk_index
      """,
      (drive_id, camera, segment_number),
    )
    frames: list[dict[str, Any]] = []
    record_hashes: list[str] = []
    with telemetry_path.open("rb") as stream:
      for row in rows:
        record = self._read_indexed_telemetry_record(
          stream,
          row,
          expected_path=ndjson_relative,
          expected_record="frame_chunk",
          error_code="telemetry_index_corrupt",
        )
        try:
          self._validate_frame_chunk(record, 0)
        except IntegrationError as error:
          raise IntegrationError(
            "telemetry_index_corrupt",
            "A stored telemetry frame chunk violates its contract.",
          ) from error
        if record.get("camera") != camera or record.get("chunk") != row["chunk_index"]:
          raise IntegrationError(
            "telemetry_index_corrupt",
            "A telemetry frame record does not match its compact index.",
          )
        record_hashes.append(row["record_sha256"])
        for frame in record["rows"]:
          if frame["segment_num"] != segment_number:
            continue
          if isinstance(frame.get("segment_frame_id"), bool) or not isinstance(frame.get("segment_frame_id"), int) or frame["segment_frame_id"] < 0:
            raise IntegrationError(
              "telemetry_frame_join_unavailable",
              "Telemetry lacks an authoritative segment_frame_id join key.",
            )
          frames.append(dict(frame))
    identifiers = [frame["segment_frame_id"] for frame in frames]
    if any(
      left >= right
      for left, right in zip(
        identifiers,
        identifiers[1:],
        strict=False,
      )
    ):
      raise IntegrationError(
        "telemetry_frame_join_unavailable",
        "Telemetry segment frame IDs are not strictly increasing.",
      )
    return frames, list(dict.fromkeys(record_hashes))

  def _invalidate_drive_media_sync(
    self,
    drive_id: str,
    *,
    connection: sqlite3.Connection | None = None,
  ) -> None:
    def invalidate(target: sqlite3.Connection) -> None:
      target.execute(
        """
        UPDATE artifacts
        SET status = 'stale'
        WHERE drive_id = ?
          AND kind = 'video_telemetry_sync'
          AND status = 'ready'
        """,
        (drive_id,),
      )
      target.execute(
        """
        UPDATE artifacts
        SET time_map_path = NULL
        WHERE drive_id = ?
          AND kind = 'derived_video'
        """,
        (drive_id,),
      )

    if connection is not None:
      invalidate(connection)
      return
    with self.database.transaction(immediate=True) as transaction:
      invalidate(transaction)

  def _invalidate_drive_telemetry_publication(
    self,
    drive_id: str,
  ) -> None:
    with self.database.transaction(immediate=True) as connection:
      self._set_drive_telemetry_ready(
        drive_id,
        False,
        connection=connection,
      )
      self._invalidate_drive_media_sync(
        drive_id,
        connection=connection,
      )

  def _build_drive_media_sync(
    self,
    context: JobContext,
    drive_id: str,
  ) -> list[dict[str, Any]]:
    rows = self.database.query_all(
      """
      SELECT id
      FROM artifacts
      WHERE drive_id = ?
        AND kind = 'derived_video'
        AND status = 'ready'
      ORDER BY created_at, id
      """,
      (drive_id,),
    )
    return [self._attempt_media_sync(context, row["id"]) for row in rows]

  def extract_telemetry(
    self,
    context: JobContext,
    payload: dict[str, Any],
  ) -> dict[str, Any]:
    drive_id = self._required_payload_string(payload, "drive_id")
    route_name = self._required_payload_string(payload, "route_name")
    drive = self.database.query_one(
      "SELECT id, device_id, route_name FROM drives WHERE id = ?",
      (drive_id,),
    )
    if drive is None:
      raise IntegrationError(
        "drive_not_found",
        "The telemetry job references an unknown drive.",
        details={"drive_id": drive_id},
      )
    if drive["route_name"] != route_name:
      raise IntegrationError(
        "drive_route_mismatch",
        "The telemetry job route does not match the drive catalog.",
      )
    _safe_route_directory_name(route_name)
    self._invalidate_drive_telemetry_publication(drive_id)
    sources = self._telemetry_sources(drive_id)
    requested_fingerprint = payload.get("source_fingerprint")
    if requested_fingerprint is not None and (not isinstance(requested_fingerprint, str) or len(requested_fingerprint) != 64):
      raise IntegrationError(
        "invalid_job_payload",
        "source_fingerprint must be a SHA-256 digest.",
      )

    staging_parent = self.archive_root / "telemetry" / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
      tempfile.mkdtemp(
        prefix=f"extract-{_path_component(context.job_id)}-",
        dir=staging_parent,
      )
    ).resolve()
    if not staging.is_relative_to(staging_parent.resolve()):
      raise IntegrationError(
        "unsafe_staging_path",
        "Could not create an authorized telemetry staging directory.",
      )
    try:
      input_root = staging / "input"
      input_root.mkdir()
      self._stage_rlogs(input_root, route_name, sources)
      raw_output = staging / "adapter.ndjson"
      canonical_output = staging / "telemetry.ndjson"
      _context_progress(context, 0.05)
      command = (
        *self.rlog_command,
        str(input_root),
        "--route-id",
        route_name,
        "--log-type",
        sources.log_type,
        "--format",
        "ndjson",
        "--output",
        str(raw_output),
      )
      process = self._run(
        context,
        command,
        input_text=None,
        timeout_seconds=self.telemetry_timeout_seconds,
      )
      if process.returncode != 0:
        raise IntegrationError(
          "telemetry_adapter_failed",
          "The rlog telemetry adapter failed.",
          details={
            "returncode": process.returncode,
            "stderr": process.stderr[-16_384:],
          },
          retryable=process.returncode == 1,
        )
      if process.stdout.strip():
        raise IntegrationError(
          "telemetry_adapter_protocol_error",
          "The rlog adapter wrote unexpected stdout while using --output.",
        )
      if not raw_output.is_file():
        raise IntegrationError(
          "telemetry_adapter_protocol_error",
          "The rlog adapter did not create its requested output.",
        )
      document = self._canonicalize_telemetry(
        raw_output,
        canonical_output,
        expected_route=route_name,
      )
      _context_progress(context, 0.72)
      installed = self._install_telemetry_files(
        drive,
        route_name,
        sources,
        document,
        canonical_output,
      )
      _context_progress(context, 0.8)
      missing_tables = sorted(
        TELEMETRY_TABLES - self._existing_tables(),
      )
      missing_reference_columns = self._missing_reference_columns()
      if missing_tables or missing_reference_columns:
        self._update_segment_timing(drive_id, document.manifest)
        result: dict[str, Any] = {
          "status": "stored_unindexed",
          "drive_id": drive_id,
          "source_fingerprint": sources.fingerprint,
          "ndjson_path": installed["ndjson_path"],
          "ndjson_sha256": document.sha256,
          "manifest": document.manifest,
          "migration_required": {
            "missing_tables": missing_tables,
            "missing_reference_columns": missing_reference_columns,
            "sql": REQUIRED_TELEMETRY_SCHEMA_SQL,
          },
        }
      else:
        counts = self._index_telemetry(
          drive,
          sources,
          document,
          Path(installed["ndjson_absolute"]),
          installed["ndjson_path"],
        )
        result = {
          "status": document.manifest["state"],
          "drive_id": drive_id,
          "source_fingerprint": sources.fingerprint,
          "ndjson_path": installed["ndjson_path"],
          "ndjson_sha256": document.sha256,
          "record_count": document.record_count,
          "manifest": document.manifest,
          "index_counts": counts,
        }
        result["media_sync"] = self._build_drive_media_sync(
          context,
          drive_id,
        )
      self._atomic_write_json(
        Path(installed["index_path"]),
        installed["index"],
      )
      latest_sources = self._telemetry_sources(drive_id)
      comparison_fingerprint = requested_fingerprint if isinstance(requested_fingerprint, str) else sources.fingerprint
      if latest_sources.fingerprint != comparison_fingerprint:
        self._invalidate_drive_telemetry_publication(drive_id)
        follow_up = self._enqueue_telemetry_follow_up(
          context,
          drive_id,
          route_name,
          latest_sources.fingerprint,
        )
        result["follow_up"] = follow_up
      _context_progress(context, 1.0)
      return result
    finally:
      if staging.exists() and staging.is_relative_to(staging_parent.resolve()):
        shutil.rmtree(staging)

  def _telemetry_sources(
    self,
    drive_id: str,
    *,
    connection: sqlite3.Connection | None = None,
  ) -> TelemetrySourceSet:
    def rows_for(kind: str) -> list[sqlite3.Row]:
      sql = """
        SELECT
          a.id,
          a.relative_path,
          a.storage_path,
          a.object_sha256 AS sha256,
          a.created_at,
          o.storage_path AS object_storage_path,
          o.size AS object_size,
          s.number AS segment_number
        FROM artifacts a
        JOIN objects o ON o.sha256 = a.object_sha256
        JOIN segments s ON s.id = a.segment_id
        WHERE a.drive_id = ?
          AND a.kind = ?
          AND a.status != 'partial'
        ORDER BY s.number, a.id
        """
      if connection is not None:
        return connection.execute(sql, (drive_id, kind)).fetchall()
      return self.database.query_all(sql, (drive_id, kind))

    rows = rows_for("rlog")
    log_type = "rlog"
    if not rows:
      rows = rows_for("qlog")
      log_type = "qlog"
    if not rows:
      raise IntegrationError(
        "telemetry_source_missing",
        "No complete rlog or qlog artifacts are available for the drive.",
        details={"drive_id": drive_id},
        retryable=True,
      )
    selected_by_segment: dict[int, sqlite3.Row] = {}
    for row in sorted(
      rows,
      key=lambda item: (
        int(item["segment_number"]),
        str(item["created_at"]),
        str(item["id"]),
      ),
    ):
      selected_by_segment[int(row["segment_number"])] = row
    selected = tuple(selected_by_segment[number] for number in sorted(selected_by_segment))
    fingerprint = telemetry_source_fingerprint(
      [
        {
          "segment_number": row["segment_number"],
          "sha256": row["sha256"],
        }
        for row in selected
      ]
    )
    return TelemetrySourceSet(
      log_type=log_type,
      selected=selected,
      all_rows=tuple(rows),
      fingerprint=fingerprint,
    )

  def _stage_rlogs(
    self,
    input_root: Path,
    route_name: str,
    sources: TelemetrySourceSet,
  ) -> None:
    object_root = self.archive_root / "objects"
    source_sizes = [row["object_size"] for row in sources.selected]
    if (
      len(source_sizes) > MAX_TELEMETRY_ROUTE_SEGMENTS
      or any(isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > MAX_TELEMETRY_SOURCE_BYTES_PER_SEGMENT for size in source_sizes)
      or sum(source_sizes) > MAX_TELEMETRY_SOURCE_BYTES_PER_ROUTE
    ):
      raise IntegrationError(
        "telemetry_source_too_large",
        "The selected telemetry source set exceeds bounded extractor staging limits.",
        details={
          "segment_count": len(source_sizes),
          "route_size_bytes": sum(size for size in source_sizes if isinstance(size, int) and not isinstance(size, bool)),
          "max_segments": MAX_TELEMETRY_ROUTE_SEGMENTS,
          "max_segment_bytes": (MAX_TELEMETRY_SOURCE_BYTES_PER_SEGMENT),
          "max_route_bytes": MAX_TELEMETRY_SOURCE_BYTES_PER_ROUTE,
        },
      )
    for row in sources.selected:
      source = self._archive_path(
        row["object_storage_path"],
        required_root=object_root,
      )
      digest, size = self._hash_file(source)
      if digest != row["sha256"] or size != row["object_size"]:
        raise IntegrationError(
          "artifact_integrity_failed",
          "A telemetry source failed immutable-object verification.",
          details={"artifact_id": row["id"]},
        )
      relative_path = str(row["relative_path"]).lower()
      suffix = ".zst" if relative_path.endswith(".zst") else ".bz2" if relative_path.endswith(".bz2") else ""
      segment_directory = input_root / f"{route_name}--{int(row['segment_number'])}"
      segment_directory.mkdir()
      target = segment_directory / f"{sources.log_type}{suffix}"
      # Never expose the immutable object through a writable hard-link alias.
      # The adapter runs in the worker process and the archive is mounted
      # read-write there, so only an independent staging copy provides this
      # boundary without relying on a second read-only bind mount.
      shutil.copyfile(source, target)
      staged_digest, staged_size = self._hash_file(target)
      if staged_digest != digest or staged_size != size:
        raise IntegrationError(
          "artifact_integrity_failed",
          "A staged telemetry source failed copy verification.",
          details={"artifact_id": row["id"]},
        )

  def _canonicalize_telemetry(
    self,
    source_path: Path,
    destination_path: Path,
    *,
    expected_route: str,
  ) -> TelemetryDocument:
    header: dict[str, Any] | None = None
    catalog: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    stream_end: dict[str, Any] | None = None
    record_count = 0
    digest = hashlib.sha256()
    size = 0
    with (
      source_path.open("r", encoding="utf-8") as source,
      destination_path.open("x", encoding="utf-8", newline="\n") as destination,
    ):
      for line_number, line in enumerate(source, start=1):
        if not line.strip():
          raise IntegrationError(
            "invalid_telemetry_stream",
            "Telemetry NDJSON contains a blank record.",
            details={"line": line_number},
          )
        try:
          record = strict_json_loads(line)
        except (ValueError, json.JSONDecodeError) as error:
          raise IntegrationError(
            "invalid_telemetry_stream",
            "Telemetry NDJSON contains invalid JSON.",
            details={"line": line_number},
          ) from error
        if not isinstance(record, dict) or not isinstance(record.get("record"), str):
          raise IntegrationError(
            "invalid_telemetry_stream",
            "Every telemetry line must be a record object.",
            details={"line": line_number},
          )
        kind = record["record"]
        if record_count == 0:
          if (
            kind != "stream_header"
            or record.get("schema") != "comma-companion.telemetry"
            or not _exact_int(
              record.get("schema_version"),
              TELEMETRY_SCHEMA_VERSION,
            )
            or record.get("route_id") != expected_route
          ):
            raise IntegrationError(
              "telemetry_contract_mismatch",
              "The telemetry stream header is invalid or for another route.",
            )
          header = record
        elif record_count == 1:
          if kind != "signal_catalog" or not isinstance(record.get("signals"), list):
            raise IntegrationError(
              "telemetry_contract_mismatch",
              "The second telemetry record must be the signal catalog.",
            )
          catalog = record
        elif manifest is not None and kind != "stream_end":
          raise IntegrationError(
            "invalid_telemetry_stream",
            "Only stream_end may follow the telemetry manifest.",
            details={"line": line_number},
          )
        elif kind == "manifest":
          if (
            manifest is not None
            or record.get("schema") != "comma-companion.telemetry-manifest"
            or not _exact_int(
              record.get("schema_version"),
              TELEMETRY_SCHEMA_VERSION,
            )
            or record.get("route_id") != expected_route
          ):
            raise IntegrationError(
              "telemetry_contract_mismatch",
              "The telemetry manifest is invalid or duplicated.",
            )
          manifest = record
        elif kind == "stream_end":
          if manifest is None or stream_end is not None:
            raise IntegrationError(
              "invalid_telemetry_stream",
              "stream_end is missing its authoritative manifest or is duplicated.",
            )
          stream_end = record
        elif kind == "series_chunk":
          self._validate_series_chunk(record, line_number)
        elif kind == "frame_chunk":
          self._validate_frame_chunk(record, line_number)
        elif kind == "dynamics_chunk":
          self._validate_dynamics_chunk(record, line_number)
        elif kind == "marker":
          self._validate_marker(record, line_number)
        canonical = canonical_json(record) + "\n"
        encoded = canonical.encode("utf-8")
        destination.write(canonical)
        digest.update(encoded)
        size += len(encoded)
        record_count += 1
      destination.flush()
      os.fsync(destination.fileno())
    if header is None or catalog is None or manifest is None or stream_end is None:
      raise IntegrationError(
        "incomplete_telemetry_stream",
        "The telemetry stream is missing a required contract record.",
      )
    if stream_end.get("status") != manifest.get("state"):
      raise IntegrationError(
        "telemetry_contract_mismatch",
        "The telemetry manifest and stream_end states disagree.",
      )
    return TelemetryDocument(
      header=header,
      signal_catalog=catalog,
      manifest=manifest,
      stream_end=stream_end,
      sha256=digest.hexdigest(),
      size_bytes=size,
      record_count=record_count,
    )

  @staticmethod
  def _validate_series_chunk(record: Mapping[str, Any], line: int) -> None:
    times = record.get("t_us")
    values = record.get("v")
    if (
      not isinstance(record.get("signal"), str)
      or not isinstance(record.get("tier"), str)
      or isinstance(record.get("chunk"), bool)
      or not isinstance(record.get("chunk"), int)
      or not isinstance(times, list)
      or not isinstance(values, list)
      or len(times) != len(values)
      or not times
      or any(isinstance(value, bool) or not isinstance(value, int) for value in times)
      or any(left > right for left, right in zip(times, times[1:], strict=False))
    ):
      raise IntegrationError(
        "invalid_telemetry_series",
        "A telemetry series chunk violates the version 1 contract.",
        details={"line": line},
      )

  @staticmethod
  def _validate_frame_chunk(record: Mapping[str, Any], line: int) -> None:
    rows = record.get("rows")
    if (
      record.get("camera") not in {"road", "wide", "driver", "qcamera"}
      or isinstance(record.get("chunk"), bool)
      or not isinstance(record.get("chunk"), int)
      or not isinstance(rows, list)
      or any(
        not isinstance(row, dict)
        or isinstance(row.get("segment_num"), bool)
        or not isinstance(row.get("segment_num"), int)
        or isinstance(row.get("t_us"), bool)
        or not isinstance(row.get("t_us"), int)
        for row in rows
      )
    ):
      raise IntegrationError(
        "invalid_telemetry_frames",
        "A telemetry frame chunk violates the version 1 contract.",
        details={"line": line},
      )

  @staticmethod
  def _validate_marker(record: Mapping[str, Any], line: int) -> None:
    if (
      not isinstance(record.get("id"), str)
      or not isinstance(record.get("kind"), str)
      or isinstance(record.get("start_us"), bool)
      or not isinstance(record.get("start_us"), int)
      or (record.get("end_us") is not None and (isinstance(record.get("end_us"), bool) or not isinstance(record.get("end_us"), int)))
    ):
      raise IntegrationError(
        "invalid_telemetry_marker",
        "A telemetry marker violates the version 1 contract.",
        details={"line": line},
      )

  @staticmethod
  def _validate_dynamics_chunk(
    record: Mapping[str, Any],
    line: int,
  ) -> None:
    rows = record.get("rows")
    integer_fields = (
      "nominal_t_us",
      "source_t_us",
      "source_time_error_us",
      "car_state_age_us",
      "car_control_age_us",
      "controls_state_age_us",
    )
    if (
      record.get("schema") != "comma-companion.dynamics-row"
      or isinstance(record.get("chunk"), bool)
      or not isinstance(record.get("chunk"), int)
      or not isinstance(rows, list)
      or not rows
      or any(
        not isinstance(row, dict)
        or any(isinstance(row.get(field), bool) or not isinstance(row.get(field), int) for field in integer_fields)
        or not isinstance(
          row.get("nominal_log_mono_time_ns"),
          str,
        )
        or not row["nominal_log_mono_time_ns"].isdigit()
        or row.get("t_us") != row.get("nominal_t_us")
        or row.get("car_state_age_us") < 0
        or row.get("source_time_error_us") != -row.get("car_state_age_us")
        or row.get("controller_i_timing") != "post_update_asof_source_row"
        or row.get("applied_torque_source") != "carOutput.actuatorsOutput.torque"
        or not isinstance(row.get("continuous"), bool)
        or isinstance(row.get("steering_rate_deg"), bool)
        or not isinstance(row.get("steering_rate_deg"), (int, float))
        or isinstance(row.get("signed_steering_rate_deg_s"), bool)
        or not isinstance(
          row.get("signed_steering_rate_deg_s"),
          (int, float),
        )
        or (row.get("car_output_age_us") is not None and (isinstance(row.get("car_output_age_us"), bool) or not isinstance(row.get("car_output_age_us"), int)))
        for row in rows
      )
      or any(left["nominal_t_us"] >= right["nominal_t_us"] for left, right in zip(rows, rows[1:], strict=False))
      or any(
        right["nominal_t_us"] - left["nominal_t_us"] != SIMULATION_SAMPLE_PERIOD_US or right.get("continuous") is not True
        for left, right in zip(rows, rows[1:], strict=False)
      )
    ):
      raise IntegrationError(
        "invalid_dynamics_chunk",
        "A dynamics chunk violates the native rlog version 1 contract.",
        details={"line": line},
      )

  def _install_telemetry_files(
    self,
    drive: sqlite3.Row,
    route_name: str,
    sources: TelemetrySourceSet,
    document: TelemetryDocument,
    canonical_output: Path,
  ) -> dict[str, Any]:
    final_directory = (self.archive_root / "telemetry" / _path_component(drive["device_id"]) / _path_component(route_name) / "v1").resolve()
    telemetry_root = (self.archive_root / "telemetry").resolve()
    if not final_directory.is_relative_to(telemetry_root):
      raise IntegrationError(
        "unsafe_output_path",
        "The telemetry output escapes its authorized archive root.",
      )
    final_directory.mkdir(parents=True, exist_ok=True)
    ndjson = final_directory / f"telemetry.{document.sha256}.ndjson"
    if ndjson.exists():
      existing_digest, existing_size = self._hash_file(ndjson)
      if existing_digest != document.sha256 or existing_size != document.size_bytes:
        raise IntegrationError(
          "telemetry_output_collision",
          "An existing telemetry revision failed integrity verification.",
        )
      canonical_output.unlink()
    else:
      os.replace(canonical_output, ndjson)
    manifest_path = final_directory / f"manifest.{document.sha256}.json"
    catalog_path = final_directory / f"signal-catalog.{document.sha256}.json"
    self._atomic_write_json(manifest_path, document.manifest)
    self._atomic_write_json(catalog_path, document.signal_catalog)
    ndjson_relative = ndjson.relative_to(self.archive_root).as_posix()
    index = {
      "schema": "comma-companion.telemetry-index",
      "schema_version": TELEMETRY_SCHEMA_VERSION,
      "state": document.manifest["state"],
      "drive_id": drive["id"],
      "device_id": drive["device_id"],
      "route_name": route_name,
      "source_fingerprint": sources.fingerprint,
      "source_artifacts": [
        {
          "artifact_id": row["id"],
          "segment_number": row["segment_number"],
          "sha256": row["sha256"],
        }
        for row in sources.all_rows
      ],
      "ndjson_path": ndjson_relative,
      "ndjson_sha256": document.sha256,
      "ndjson_size_bytes": document.size_bytes,
      "manifest_path": manifest_path.relative_to(self.archive_root).as_posix(),
      "signal_catalog_path": catalog_path.relative_to(self.archive_root).as_posix(),
      "updated_at": _now_text(),
    }
    return {
      "ndjson_path": ndjson_relative,
      "ndjson_absolute": str(ndjson),
      "index_path": str(final_directory / "index.json"),
      "index": index,
    }

  @staticmethod
  def _atomic_write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
      with temporary.open("x", encoding="utf-8", newline="\n") as output:
        output.write(canonical_json(value))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
      os.replace(temporary, path)
    finally:
      temporary.unlink(missing_ok=True)

  def _existing_tables(self) -> set[str]:
    rows = self.database.query_all(
      "SELECT name FROM sqlite_master WHERE type = 'table'",
    )
    return {row["name"] for row in rows}

  def _tables_present(self, names: set[str]) -> bool:
    return names <= self._existing_tables()

  def _update_segment_timing(
    self,
    drive_id: str,
    manifest: Mapping[str, Any],
    *,
    connection: sqlite3.Connection | None = None,
  ) -> None:
    completeness = manifest.get("completeness")
    reports = completeness.get("segments", []) if isinstance(completeness, Mapping) else []

    def update(target: sqlite3.Connection) -> None:
      for report in reports:
        if not isinstance(report, Mapping):
          continue
        number = report.get("segment_num")
        if isinstance(number, bool) or not isinstance(number, int):
          continue
        start_t_us = report.get("start_t_us")
        if isinstance(start_t_us, bool) or not isinstance(start_t_us, int):
          start_t_us = None
        duration_us = self._segment_duration(report, start_t_us)
        target.execute(
          """
          UPDATE segments
          SET start_t_us = ?, duration_us = ?
          WHERE drive_id = ? AND number = ?
          """,
          (start_t_us, duration_us, drive_id, number),
        )
      range_value = manifest.get("range")
      if isinstance(range_value, Mapping):
        start_us = range_value.get("start_us")
        end_us = range_value.get("end_us")
        if isinstance(start_us, int) and not isinstance(start_us, bool) and isinstance(end_us, int) and not isinstance(end_us, bool) and end_us >= start_us:
          duration_us = end_us - start_us
          timebase = manifest.get("timebase")
          utc_start = timebase.get("utc_start_us") if isinstance(timebase, Mapping) else None
          started_at = None
          ended_at = None
          try:
            if utc_start is not None:
              utc_value = int(utc_start)
              started = datetime.fromtimestamp(utc_value / 1_000_000, UTC)
              ended = datetime.fromtimestamp(
                (utc_value + duration_us) / 1_000_000,
                UTC,
              )
              started_at = started.isoformat().replace("+00:00", "Z")
              ended_at = ended.isoformat().replace("+00:00", "Z")
          except (OverflowError, TypeError, ValueError):
            pass
          target.execute(
            """
            UPDATE drives
            SET duration_us = ?,
              started_at = COALESCE(?, started_at),
              ended_at = COALESCE(?, ended_at)
            WHERE id = ?
            """,
            (duration_us, started_at, ended_at, drive_id),
          )

    if connection is not None:
      update(connection)
    else:
      with self.database.transaction(immediate=True) as transaction:
        update(transaction)

  @staticmethod
  def _segment_duration(
    report: Mapping[str, Any],
    start_t_us: int | None,
  ) -> int | None:
    camera_ranges = report.get("camera_ranges_us")
    end_candidates: list[int] = []
    if isinstance(camera_ranges, Mapping):
      for bounds in camera_ranges.values():
        if isinstance(bounds, list) and len(bounds) == 2 and isinstance(bounds[1], int) and not isinstance(bounds[1], bool):
          end_candidates.append(bounds[1])
    range_value = report.get("range_us")
    if isinstance(range_value, list) and len(range_value) == 2 and all(isinstance(value, int) and not isinstance(value, bool) for value in range_value):
      if start_t_us is None:
        return max(0, range_value[1] - range_value[0])
      end_candidates.append(range_value[1])
    if start_t_us is None or not end_candidates:
      return None
    return max(0, max(end_candidates) - start_t_us)

  def _index_telemetry(
    self,
    drive: sqlite3.Row,
    sources: TelemetrySourceSet,
    document: TelemetryDocument,
    ndjson: Path,
    ndjson_relative: str,
  ) -> dict[str, int]:
    signals = {signal["id"]: signal for signal in document.signal_catalog["signals"] if isinstance(signal, dict) and isinstance(signal.get("id"), str)}
    counts = {
      "series_chunks": 0,
      "markers": 0,
      "frame_chunks": 0,
      "dynamics_chunks": 0,
    }
    now = _now_text()
    with self.database.transaction(immediate=True) as connection:
      for table in (
        "telemetry_series_chunks",
        "telemetry_markers",
        "telemetry_frame_chunks",
        "telemetry_dynamics_chunks",
      ):
        connection.execute(f"DELETE FROM {table} WHERE drive_id = ?", (drive["id"],))
      connection.execute(
        """
        UPDATE artifacts
        SET status = 'stale'
        WHERE drive_id = ?
          AND kind = 'video_telemetry_sync'
          AND status = 'ready'
        """,
        (drive["id"],),
      )
      connection.execute(
        """
        UPDATE artifacts
        SET time_map_path = NULL
        WHERE drive_id = ? AND kind = 'derived_video'
        """,
        (drive["id"],),
      )
      index_digest = hashlib.sha256()
      indexed_size = 0
      with ndjson.open("rb") as stream:
        while True:
          byte_offset = stream.tell()
          encoded = stream.readline(TELEMETRY_RECORD_LIMIT_BYTES + 1)
          if not encoded:
            break
          if len(encoded) > TELEMETRY_RECORD_LIMIT_BYTES or not encoded.endswith(b"\n") or b"\n" in encoded[:-1]:
            raise IntegrationError(
              "telemetry_record_too_large",
              "A canonical telemetry record is not a bounded NDJSON line.",
              details={
                "byte_offset": byte_offset,
                "limit_bytes": TELEMETRY_RECORD_LIMIT_BYTES,
              },
            )
          try:
            record = strict_json_loads(encoded.decode("utf-8"))
          except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise IntegrationError(
              "telemetry_index_corrupt",
              "The installed canonical telemetry stream is invalid.",
              details={"byte_offset": byte_offset},
            ) from error
          reference = TelemetryRecordReference(
            byte_offset=byte_offset,
            byte_length=len(encoded),
            sha256=hashlib.sha256(encoded).hexdigest(),
          )
          index_digest.update(encoded)
          indexed_size += len(encoded)
          kind = record["record"]
          if kind == "series_chunk":
            signal = signals.get(record["signal"], {})
            interpolation = signal.get("interpolation")
            series_kind = "continuous" if interpolation == "linear" else "step"
            connection.execute(
              """
              INSERT INTO telemetry_series_chunks(
                drive_id, signal_id, tier, chunk_index, kind, unit,
                start_t_us, end_t_us, ndjson_path, byte_offset,
                byte_length, record_sha256
              ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
              """,
              (
                drive["id"],
                record["signal"],
                record["tier"],
                record["chunk"],
                series_kind,
                signal.get("unit"),
                record["t_us"][0],
                record["t_us"][-1],
                ndjson_relative,
                reference.byte_offset,
                reference.byte_length,
                reference.sha256,
              ),
            )
            counts["series_chunks"] += 1
          elif kind == "marker":
            connection.execute(
              """
              INSERT INTO telemetry_markers(
                drive_id, marker_id, kind, start_t_us, end_t_us,
                severity, label, data_json
              ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
              """,
              (
                drive["id"],
                record["id"],
                record["kind"],
                record["start_us"],
                (record["start_us"] if record.get("end_us") is None else record["end_us"]),
                record.get("severity"),
                record.get("label"),
                canonical_json(record),
              ),
            )
            counts["markers"] += 1
          elif kind == "frame_chunk":
            by_segment: dict[int, list[dict[str, Any]]] = {}
            for row in record["rows"]:
              by_segment.setdefault(row["segment_num"], []).append(row)
            for segment_number in sorted(by_segment):
              connection.execute(
                """
                INSERT INTO telemetry_frame_chunks(
                  drive_id, camera, segment_number, chunk_index,
                  ndjson_path, byte_offset, byte_length, record_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                  drive["id"],
                  record["camera"],
                  segment_number,
                  record["chunk"],
                  ndjson_relative,
                  reference.byte_offset,
                  reference.byte_length,
                  reference.sha256,
                ),
              )
              counts["frame_chunks"] += 1
          elif kind == "dynamics_chunk":
            rows = record["rows"]
            connection.execute(
              """
              INSERT INTO telemetry_dynamics_chunks(
                drive_id, chunk_index, start_t_us, end_t_us,
                ndjson_path, byte_offset, byte_length, record_sha256
              ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
              """,
              (
                drive["id"],
                record["chunk"],
                rows[0]["nominal_t_us"],
                rows[-1]["nominal_t_us"],
                ndjson_relative,
                reference.byte_offset,
                reference.byte_length,
                reference.sha256,
              ),
            )
            counts["dynamics_chunks"] += 1
      if index_digest.hexdigest() != document.sha256 or indexed_size != document.size_bytes:
        raise IntegrationError(
          "telemetry_integrity_failed",
          "The installed telemetry generation changed before indexing completed.",
        )
      prior = connection.execute(
        "SELECT created_at FROM telemetry_indexes WHERE drive_id = ?",
        (drive["id"],),
      ).fetchone()
      created_at = prior["created_at"] if prior is not None else now
      connection.execute(
        """
        INSERT INTO telemetry_indexes(
          drive_id, schema_version, state, ndjson_path, ndjson_sha256,
          manifest_json, signal_catalog_json, source_fingerprint,
          created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(drive_id) DO UPDATE SET
          schema_version = excluded.schema_version,
          state = excluded.state,
          ndjson_path = excluded.ndjson_path,
          ndjson_sha256 = excluded.ndjson_sha256,
          manifest_json = excluded.manifest_json,
          signal_catalog_json = excluded.signal_catalog_json,
          source_fingerprint = excluded.source_fingerprint,
          updated_at = excluded.updated_at
        """,
        (
          drive["id"],
          TELEMETRY_SCHEMA_VERSION,
          document.manifest["state"],
          ndjson_relative,
          document.sha256,
          canonical_json(document.manifest),
          canonical_json(document.signal_catalog),
          sources.fingerprint,
          created_at,
          now,
        ),
      )
      self._update_segment_timing(
        drive["id"],
        document.manifest,
        connection=connection,
      )
      latest_inventory = connection.execute(
        """
        SELECT state, route_closed, rlog_source_fingerprint
        FROM route_inventories
        WHERE drive_id = ?
        ORDER BY generation DESC
        LIMIT 1
        """,
        (drive["id"],),
      ).fetchone()
      inventory_matches = (
        latest_inventory is not None
        and latest_inventory["state"] == "complete"
        and latest_inventory["route_closed"] == 1
        and isinstance(
          latest_inventory["rlog_source_fingerprint"],
          str,
        )
        and secrets.compare_digest(
          latest_inventory["rlog_source_fingerprint"],
          sources.fingerprint,
        )
      )
      try:
        current_sources = self._telemetry_sources(
          drive["id"],
          connection=connection,
        )
      except IntegrationError:
        current_sources_match = False
      else:
        current_sources_match = secrets.compare_digest(
          current_sources.fingerprint,
          sources.fingerprint,
        )
      self._set_drive_telemetry_ready(
        drive["id"],
        inventory_matches and current_sources_match,
        manifest=document.manifest,
        connection=connection,
      )
    return counts

  def _enqueue_telemetry_follow_up(
    self,
    context: JobContext,
    drive_id: str,
    route_name: str,
    source_fingerprint: str,
  ) -> dict[str, Any]:
    columns = self._table_columns("jobs")
    if not {"available_at", "dedupe_key"} <= columns:
      return {
        "queued": False,
        "reason": "jobs_schema_missing_debounce_columns",
        "source_fingerprint": source_fingerprint,
      }
    payload = {
      "drive_id": drive_id,
      "route_name": route_name,
      "source_fingerprint": source_fingerprint,
    }
    payload_json = canonical_json(payload)
    now = _now_text()
    with self.database.transaction(immediate=True) as connection:
      queued = connection.execute(
        """
        SELECT id
        FROM jobs
        WHERE type = 'extract_telemetry'
          AND id != ?
          AND state = 'queued'
          AND json_extract(payload_json, '$.drive_id') = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (context.job_id, drive_id),
      ).fetchone()
      if queued is not None:
        connection.execute(
          """
          UPDATE jobs
          SET payload_json = ?, available_at = ?, updated_at = ?
          WHERE id = ?
          """,
          (payload_json, now, now, queued["id"]),
        )
        return {
          "queued": True,
          "job_id": queued["id"],
          "updated": True,
          "source_fingerprint": source_fingerprint,
        }
      job_id = uuid4().hex
      dedupe_key = f"drive:{drive_id}:after:{context.job_id}"
      connection.execute(
        """
        INSERT INTO jobs(
          id, type, state, payload_json, dedupe_key, available_at,
          created_at, updated_at
        ) VALUES (?, 'extract_telemetry', 'queued', ?, ?, ?, ?, ?)
        """,
        (
          job_id,
          payload_json,
          dedupe_key,
          now,
          now,
          now,
        ),
      )
    return {
      "queued": True,
      "job_id": job_id,
      "updated": False,
      "source_fingerprint": source_fingerprint,
    }

  def _table_columns(self, table: str) -> set[str]:
    if table not in self._existing_tables():
      return set()
    rows = self.database.query_all(f"PRAGMA table_info({table})")
    return {row["name"] for row in rows}

  def _missing_reference_columns(self) -> dict[str, list[str]]:
    missing: dict[str, list[str]] = {}
    for table, required in TELEMETRY_REFERENCE_COLUMNS.items():
      if table not in self._existing_tables():
        continue
      absent = sorted(required - self._table_columns(table))
      if absent:
        missing[table] = absent
    return missing

  def _set_drive_telemetry_ready(
    self,
    drive_id: str,
    ready: bool,
    *,
    manifest: Mapping[str, Any] | None = None,
    connection: sqlite3.Connection | None = None,
  ) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(drives)").fetchall()} if connection is not None else self._table_columns("drives")
    if "telemetry_ready" not in columns:
      return
    route_state = None
    if manifest is not None:
      manifest_state = manifest.get("state")
      if manifest_state in {"open", "partial", "complete"}:
        route_state = manifest_state
      else:
        route_state = "partial"
      ready = ready and manifest.get("publication_ready") is True and manifest_state == "complete"
    assignments = ["telemetry_ready = ?"]
    values: list[Any] = [int(ready)]
    if route_state is not None and "route_state" in columns:
      assignments.append("route_state = ?")
      values.append(route_state)
    if "updated_at" in columns:
      assignments.append("updated_at = ?")
      values.append(_now_text())
    values.append(drive_id)
    sql = f"UPDATE drives SET {', '.join(assignments)} WHERE id = ?"
    if connection is not None:
      connection.execute(sql, values)
    else:
      self.database.execute(sql, tuple(values))

  def refresh_model_registry(
    self,
    context: JobContext | None = None,
  ) -> dict[str, Any]:
    active_context: Any = context or _NoopContext()
    requests = [
      {
        "id": "model-info",
        "method": "model_info",
        "params": {},
      },
      {
        "id": "parameter-schema",
        "method": "parameter_schema",
        "params": {},
      },
    ]
    process = self._run(
      active_context,
      self.dynamics_command,
      input_text="".join(canonical_json(item) + "\n" for item in requests),
      timeout_seconds=self.simulation_timeout_seconds,
      output_limit_bytes=DYNAMICS_REQUEST_LIMIT_BYTES,
    )
    if process.returncode != 0:
      raise IntegrationError(
        "dynamics_adapter_failed",
        "The dynamics adapter could not describe its model.",
        details={
          "returncode": process.returncode,
          "stderr": process.stderr[-16_384:],
        },
        retryable=True,
      )
    responses = self._strict_ndjson(
      process.stdout,
      "dynamics adapter stdout",
    )
    by_id = {response.get("id"): response for response in responses if isinstance(response.get("id"), str)}
    if set(by_id) != {"model-info", "parameter-schema"}:
      raise IntegrationError(
        "dynamics_protocol_error",
        "The dynamics adapter did not return both registry responses.",
      )
    results: dict[str, Mapping[str, Any]] = {}
    for identifier, response in by_id.items():
      if response.get("ok") is not True or not isinstance(
        response.get("result"),
        Mapping,
      ):
        error = response.get("error")
        raise IntegrationError(
          (str(error.get("code")) if isinstance(error, Mapping) and error.get("code") else "dynamics_registry_rejected"),
          (str(error.get("message")) if isinstance(error, Mapping) and error.get("message") else "The dynamics adapter rejected a registry request."),
        )
      results[identifier] = response["result"]
    info = results["model-info"]
    schema_result = results["parameter-schema"]
    model = info.get("model")
    schema = schema_result.get("parameter_schema")
    input_contract = info.get("input_contract")
    if (
      not _exact_int(info.get("protocol_version"), 1)
      or info.get("mode") != "approximate_closed_loop"
      or info.get("target_car_fingerprint") != REFERENCE_CAR_FINGERPRINT
      or info.get("history_steps") != SIMULATION_HISTORY_ROWS
      or info.get("sample_period_s") != 0.01
      or not isinstance(model, Mapping)
      or str(model.get("sha256", "")).lower() != REFERENCE_DYNAMICS_MODEL_SHA256
      or model.get("promoted_artifact_verified") is not True
      or model.get("review_registry_match") is not True
      or not isinstance(input_contract, Mapping)
      or input_contract.get("source_log_type") != "rlog"
      or input_contract.get("telemetry_schema") != "comma-companion.dynamics-row"
      or not _exact_int(
        input_contract.get("telemetry_schema_version"),
        1,
      )
      or input_contract.get("alignment") != "timestamp_causal_recorded_history_asof"
      or input_contract.get("max_asof_age_ms") != SIMULATION_MAX_ASOF_AGE_MS
      or input_contract.get("controller_i_timing") != "post_update_asof_source_row"
      or not isinstance(schema, list)
      or schema != info.get("parameter_schema")
    ):
      raise IntegrationError(
        "dynamics_registry_contract_mismatch",
        "The dynamics adapter does not match the reviewed Ioniq 5 model contract.",
      )
    baseline_params = self._validate_parameter_schema(schema)
    capabilities = info.get("capabilities")
    if (
      not isinstance(capabilities, Mapping)
      or capabilities.get("counterfactual_replay") is not True
      or not isinstance(capabilities.get("causal_replay_eligible"), bool)
      or capabilities.get("apply_to_car") is not False
      or capabilities["causal_replay_eligible"] is not (model.get("causal_training_eligible") is True)
    ):
      raise IntegrationError(
        "dynamics_registry_contract_mismatch",
        "The dynamics adapter exposes an unsafe or incomplete capability set.",
      )
    causal_ready = (
      self._causal_model_provenance(model)
      and model.get("causal_training_eligible") is True
      and model.get("training_alignment") == "timestamp_causal_recorded_history_asof"
      and model.get("training_schema") == "comma-companion.dynamics-row"
      and _exact_int(model.get("training_schema_version"), 1)
      and self._sha256_digest(
        model.get("training_extractor_sha256"),
      )
      and model.get("compatible_telemetry_extractor_sha256")
      == REFERENCE_TELEMETRY_EXTRACTOR_SHA256
      and model.get("compatible_telemetry_extractor_version") == REFERENCE_TELEMETRY_EXTRACTOR_VERSION
      and model.get("max_asof_age_ms") == SIMULATION_MAX_ASOF_AGE_MS
      and capabilities["causal_replay_eligible"] is True
    )
    metadata = {
      "baseline_params": baseline_params,
      "parameter_schema_format": "adapter_parameter_list_v1",
      "model": dict(model),
      "adapter": dict(info),
      "capabilities": {
        "available": causal_ready,
        "counterfactual_replay": causal_ready,
        "apply_to_car": False,
      },
      "causal_training_eligible": causal_ready,
      "blocked_reason": (None if causal_ready else "blocked_pending_causal_retrain"),
      "refreshed_at": _now_text(),
    }
    now = _now_text()
    with self.database.transaction(immediate=True) as connection:
      connection.execute(
        "UPDATE model_registry SET enabled = 0 WHERE sha256 != ?",
        (REFERENCE_DYNAMICS_MODEL_SHA256,),
      )
      connection.execute(
        """
        INSERT INTO model_registry(
          sha256, name, enabled, mode, parameter_schema_json,
          metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sha256) DO UPDATE SET
          name = excluded.name,
          enabled = excluded.enabled,
          mode = excluded.mode,
          parameter_schema_json = excluded.parameter_schema_json,
          metadata_json = excluded.metadata_json
        """,
        (
          REFERENCE_DYNAMICS_MODEL_SHA256,
          "Ioniq 5 neural lateral plant",
          int(causal_ready),
          "approximate_closed_loop",
          canonical_json(schema),
          canonical_json(metadata),
          now,
        ),
      )
    _context_progress(active_context, 1.0)
    return {
      "status": ("ready" if causal_ready else "blocked_pending_causal_retrain"),
      "model_hash": REFERENCE_DYNAMICS_MODEL_SHA256,
      "mode": "approximate_closed_loop",
      "car_fingerprint": REFERENCE_CAR_FINGERPRINT,
      "parameter_schema": schema,
      "baseline_params": baseline_params,
      "model": dict(model),
      "capabilities": metadata["capabilities"],
    }

  @staticmethod
  def _validate_parameter_schema(
    schema: list[Any],
  ) -> dict[str, Any]:
    baseline: dict[str, Any] = {}
    for index, item in enumerate(schema):
      if not isinstance(item, Mapping):
        raise IntegrationError(
          "dynamics_parameter_schema_invalid",
          "A dynamics parameter definition is not an object.",
          details={"index": index},
        )
      name = item.get("name")
      value_type = item.get("type")
      default = item.get("default")
      if not isinstance(name, str) or not name or name in baseline or value_type not in {"number", "integer", "boolean"}:
        raise IntegrationError(
          "dynamics_parameter_schema_invalid",
          "A dynamics parameter definition has an invalid name or type.",
          details={"index": index},
        )
      valid_default = (
        isinstance(default, bool)
        if value_type == "boolean"
        else isinstance(default, int) and not isinstance(default, bool)
        if value_type == "integer"
        else isinstance(default, (int, float)) and not isinstance(default, bool)
      )
      if not valid_default or (isinstance(default, (int, float)) and not isinstance(default, bool) and not math.isfinite(float(default))):
        raise IntegrationError(
          "dynamics_parameter_schema_invalid",
          "A dynamics parameter has an invalid default value.",
          details={"parameter": name},
        )
      minimum = item.get("minimum")
      maximum = item.get("maximum")
      if (minimum is not None and isinstance(default, (int, float)) and default < minimum) or (
        maximum is not None and isinstance(default, (int, float)) and default > maximum
      ):
        raise IntegrationError(
          "dynamics_parameter_schema_invalid",
          "A dynamics parameter default is outside its declared range.",
          details={"parameter": name},
        )
      baseline[name] = default
    if not baseline:
      raise IntegrationError(
        "dynamics_parameter_schema_invalid",
        "The dynamics adapter returned an empty parameter schema.",
      )
    return baseline

  def _causal_model_provenance(
    self,
    model: Mapping[str, Any],
  ) -> bool:
    return (
      model.get("promoted_artifact_verified") is True
      and model.get("review_registry_match") is True
      and model.get("causal_training_eligible") is True
      and model.get("training_alignment") == "timestamp_causal_recorded_history_asof"
      and model.get("training_schema") == "comma-companion.dynamics-row"
      and _exact_int(model.get("training_schema_version"), 1)
      and _exact_int(model.get("training_extraction_version"), 9)
      and model.get("trainer_schema") == "starpilot.neural-lateral-plant"
      and _exact_int(model.get("trainer_schema_version"), 7)
      and self._sha256_digest(
        model.get("training_extractor_sha256"),
      )
      and self._sha256_digest(model.get("trainer_sha256"))
      and model.get("compatible_telemetry_extractor_sha256")
      == REFERENCE_TELEMETRY_EXTRACTOR_SHA256
      and model.get("max_asof_age_ms") == SIMULATION_MAX_ASOF_AGE_MS
      and model.get("sampling") == CAUSAL_SAMPLING_CONTRACT
    )

  def simulate_counterfactual(
    self,
    context: JobContext,
    payload: dict[str, Any],
  ) -> dict[str, Any]:
    drive_id = self._required_payload_string(payload, "drive_id")
    model_hash = self._required_payload_string(payload, "model_hash").lower()
    mode = payload.get("mode")
    if mode != "approximate_closed_loop":
      raise IntegrationError(
        "unsupported_simulation_mode",
        "Only approximate_closed_loop simulation is supported.",
      )
    t_us = payload.get("t_us")
    horizon_us = payload.get("horizon_us")
    if (
      isinstance(t_us, bool)
      or not isinstance(t_us, int)
      or t_us < 0
      or isinstance(horizon_us, bool)
      or not isinstance(horizon_us, int)
      or horizon_us < 100_000
      or horizon_us > 2_000_000
    ):
      raise IntegrationError(
        "invalid_job_payload",
        "Simulation t_us or horizon_us is outside the supported range.",
      )
    candidate_params = payload.get(
      "candidate_params",
      payload.get("parameters"),
    )
    if not isinstance(candidate_params, dict):
      raise IntegrationError(
        "invalid_job_payload",
        "Simulation candidate_params must be a JSON object.",
      )
    model = self.database.query_one(
      """
      SELECT sha256, enabled, mode, metadata_json
      FROM model_registry
      WHERE sha256 = ?
      """,
      (model_hash,),
    )
    if model is None or model["mode"] != mode:
      raise IntegrationError(
        "model_not_allowed",
        "The requested model hash and mode are not enabled.",
      )
    try:
      model_metadata = strict_json_loads(model["metadata_json"] or "{}")
    except (ValueError, json.JSONDecodeError) as error:
      raise IntegrationError(
        "model_registry_invalid",
        "The enabled model has invalid metadata JSON.",
      ) from error
    registered_model = model_metadata.get("model") if isinstance(model_metadata, Mapping) else None
    registered_adapter = model_metadata.get("adapter") if isinstance(model_metadata, Mapping) else None
    registered_capabilities = registered_adapter.get("capabilities") if isinstance(registered_adapter, Mapping) else None
    if (
      model["enabled"] != 1
      or model_hash != REFERENCE_DYNAMICS_MODEL_SHA256
      or not isinstance(registered_model, Mapping)
      or not self._causal_model_provenance(registered_model)
      or registered_model.get("sha256") != model_hash
      or registered_model.get("promoted_artifact_verified") is not True
      or registered_model.get("review_registry_match") is not True
      or registered_model.get("causal_training_eligible") is not True
      or registered_model.get("training_alignment") != "timestamp_causal_recorded_history_asof"
      or registered_model.get("training_schema") != "comma-companion.dynamics-row"
      or not _exact_int(
        registered_model.get("training_schema_version"),
        1,
      )
      or not self._sha256_digest(
        registered_model.get("training_extractor_sha256"),
      )
      or not self._sha256_digest(
        registered_model.get(
          "compatible_telemetry_extractor_sha256",
        ),
      )
      or registered_model.get("max_asof_age_ms") != SIMULATION_MAX_ASOF_AGE_MS
      or not isinstance(registered_capabilities, Mapping)
      or registered_capabilities.get("causal_replay_eligible") is not True
      or model_metadata.get("causal_training_eligible") is not True
    ):
      raise IntegrationError(
        "causal_model_required",
        "Counterfactual replay is unavailable until a reviewed timestamp-causal model is promoted.",
        details={
          "model_hash": model_hash,
          "status": "blocked_pending_causal_retrain",
        },
      )
    missing_reference_columns = self._missing_reference_columns()
    if not self._tables_present(TELEMETRY_TABLES) or missing_reference_columns:
      raise IntegrationError(
        "telemetry_schema_required",
        "Counterfactual simulation requires the telemetry index migration.",
        details={
          "missing_reference_columns": missing_reference_columns,
          "sql": REQUIRED_TELEMETRY_SCHEMA_SQL,
        },
      )
    telemetry = self.database.query_one(
      """
      SELECT *
      FROM telemetry_indexes
      WHERE drive_id = ?
      """,
      (drive_id,),
    )
    if telemetry is None:
      raise IntegrationError(
        "telemetry_not_ready",
        "Counterfactual simulation requires indexed telemetry.",
        retryable=True,
      )
    drive_state = self.database.query_one(
      "SELECT telemetry_ready FROM drives WHERE id = ?",
      (drive_id,),
    )
    latest_inventory = self.database.query_one(
      """
      SELECT
        generation, state, route_closed,
        rlog_source_fingerprint
      FROM route_inventories
      WHERE drive_id = ?
      ORDER BY generation DESC
      LIMIT 1
      """,
      (drive_id,),
    )
    inventory_fingerprint = latest_inventory["rlog_source_fingerprint"] if latest_inventory is not None else None
    current_generation_valid = (
      drive_state is not None
      and drive_state["telemetry_ready"] == 1
      and latest_inventory is not None
      and latest_inventory["state"] == "complete"
      and latest_inventory["route_closed"] == 1
      and self._sha256_digest(inventory_fingerprint)
      and self._sha256_digest(telemetry["source_fingerprint"])
      and secrets.compare_digest(
        inventory_fingerprint,
        telemetry["source_fingerprint"],
      )
    )
    if not current_generation_valid:
      raise IntegrationError(
        "stale_generation",
        "The pinned telemetry generation was superseded by the latest route inventory.",
        details={
          "telemetry_ready": (drive_state["telemetry_ready"] if drive_state is not None else None),
          "telemetry_source_fingerprint": (telemetry["source_fingerprint"]),
          "latest_inventory_generation": (latest_inventory["generation"] if latest_inventory is not None else None),
          "latest_inventory_state": (latest_inventory["state"] if latest_inventory is not None else None),
          "latest_inventory_route_closed": (bool(latest_inventory["route_closed"]) if latest_inventory is not None else None),
          "latest_inventory_rlog_source_fingerprint": (inventory_fingerprint),
        },
      )
    telemetry_sha256 = self._required_payload_string(
      payload,
      "telemetry_sha256",
    ).lower()
    if len(telemetry_sha256) != 64 or any(character not in "0123456789abcdef" for character in telemetry_sha256):
      raise IntegrationError(
        "invalid_job_payload",
        "telemetry_sha256 must be a lowercase or uppercase SHA-256 digest.",
      )
    if telemetry["ndjson_sha256"] != telemetry_sha256:
      raise IntegrationError(
        "stale_generation",
        "The simulation telemetry generation is no longer current.",
        details={
          "requested": telemetry_sha256,
          "current": telemetry["ndjson_sha256"],
        },
      )
    telemetry_path = self._archive_path(
      telemetry["ndjson_path"],
      required_root=self.archive_root / "telemetry",
    )
    actual_telemetry_sha256, _ = self._hash_file(telemetry_path)
    if actual_telemetry_sha256 != telemetry_sha256:
      raise IntegrationError(
        "telemetry_integrity_failed",
        "The pinned telemetry generation failed SHA-256 verification.",
      )
    try:
      manifest = strict_json_loads(telemetry["manifest_json"])
    except (ValueError, json.JSONDecodeError) as error:
      raise IntegrationError(
        "telemetry_index_corrupt",
        "The telemetry manifest contains invalid JSON.",
      ) from error
    if not isinstance(manifest, Mapping):
      raise IntegrationError(
        "telemetry_index_corrupt",
        "The telemetry manifest is not a JSON object.",
      )
    timeline_version = self._required_payload_string(
      payload,
      "timeline_version",
    ).lower()
    current_timeline_version = manifest.get("timeline_version")
    if not self._sha256_digest(timeline_version) or timeline_version != current_timeline_version:
      raise IntegrationError(
        "stale_generation",
        "The simulation timeline generation is no longer current.",
        details={
          "requested": timeline_version,
          "current": current_timeline_version,
        },
      )
    vehicle = manifest.get("vehicle") if isinstance(manifest, Mapping) else None
    car_fingerprint = vehicle.get("car_fingerprint") if isinstance(vehicle, Mapping) else None
    if car_fingerprint != REFERENCE_CAR_FINGERPRINT:
      raise IntegrationError(
        "wrong_car",
        "The selected drive is not from the model's trained Ioniq 5 fingerprint.",
        details={
          "expected": REFERENCE_CAR_FINGERPRINT,
          "actual": car_fingerprint,
        },
      )
    full_rlog = self._require_full_rlog(manifest, telemetry_path)
    telemetry_provenance = full_rlog["telemetry_provenance"]
    if (
      registered_model["training_schema"] != telemetry_provenance["schema"]
      or registered_model["training_schema_version"] != telemetry_provenance["schema_version"]
      or registered_model["training_alignment"] != telemetry_provenance["alignment"]
      or registered_model["compatible_telemetry_extractor_sha256"] != telemetry_provenance["extractor_source_sha256"]
      or registered_model["compatible_telemetry_extractor_version"] != telemetry_provenance["extractor_version"]
    ):
      raise IntegrationError(
        "causal_model_required",
        "The promoted model training provenance does not match this telemetry generation.",
        details={
          "model_training_schema": registered_model.get(
            "training_schema",
          ),
          "telemetry_schema": telemetry_provenance["schema"],
          "model_compatible_telemetry_extractor_sha256": (
            registered_model.get(
              "compatible_telemetry_extractor_sha256",
            )
          ),
          "telemetry_extractor_sha256": telemetry_provenance["extractor_source_sha256"],
          "model_compatible_telemetry_extractor_version": (
            registered_model.get(
              "compatible_telemetry_extractor_version",
            )
          ),
          "telemetry_extractor_version": telemetry_provenance["extractor_version"],
        },
      )
    baseline_profile = validated_controller_profile(
      manifest,
    )
    if baseline_profile is None:
      raise IntegrationError(
        "controller_provenance_incomplete",
        "Telemetry has no reviewed controller baseline for this route generation.",
      )
    route_baseline = baseline_profile["baseline_controller_params"]
    baseline_params = payload.get("baseline_params")
    if not isinstance(baseline_params, Mapping):
      raise IntegrationError(
        "invalid_job_payload",
        "Simulation baseline_params must contain the pinned route controller profile.",
      )
    if dict(baseline_params) != dict(route_baseline):
      raise IntegrationError(
        "controller_baseline_mismatch",
        "Simulation baseline_params do not match the selected telemetry generation.",
      )
    rows, anchor_t_us = self._simulation_rows(
      drive_id,
      t_us,
      horizon_us,
      telemetry_path=telemetry_path,
      ndjson_relative=telemetry["ndjson_path"],
      route_origin_log_mono_time_ns=telemetry_provenance["route_origin_log_mono_time_ns"],
      controller_profile=baseline_profile,
    )
    controller_provenance = self._controller_provenance(manifest)
    request = {
      "id": context.job_id,
      "method": "replay",
      "params": {
        "mode": mode,
        "car_fingerprint": car_fingerprint,
        "anchor_t_us": anchor_t_us,
        "horizon_s": horizon_us / 1_000_000,
        "source_log_type": "rlog",
        "input_alignment": "timestamp_causal_recorded_history_asof",
        "max_asof_age_ms": SIMULATION_MAX_ASOF_AGE_MS,
        "telemetry_provenance": full_rlog["telemetry_provenance"],
        "controller_i_timing": "post_update_asof_source_row",
        "controller_provenance": controller_provenance,
        "baseline_params": dict(baseline_params),
        "candidate_params": candidate_params,
        "rows": rows,
      },
    }
    input_text = canonical_json(request) + "\n"
    if len(input_text.encode("utf-8")) > DYNAMICS_REQUEST_LIMIT_BYTES:
      raise IntegrationError(
        "dynamics_request_too_large",
        "The canonical dynamics request exceeds the adapter limit.",
      )
    _context_progress(context, 0.35)
    process = self._run(
      context,
      self.dynamics_command,
      input_text=input_text,
      timeout_seconds=self.simulation_timeout_seconds,
      output_limit_bytes=DYNAMICS_REQUEST_LIMIT_BYTES,
    )
    if process.returncode != 0:
      raise IntegrationError(
        "dynamics_adapter_failed",
        "The dynamics adapter process failed.",
        details={
          "returncode": process.returncode,
          "stderr": process.stderr[-16_384:],
        },
        retryable=True,
      )
    responses = self._strict_ndjson(
      process.stdout,
      "dynamics adapter stdout",
    )
    if len(responses) != 1:
      raise IntegrationError(
        "dynamics_protocol_error",
        "The dynamics adapter must return exactly one response.",
      )
    response = responses[0]
    if response.get("id") != context.job_id:
      raise IntegrationError(
        "dynamics_protocol_error",
        "The dynamics adapter response ID does not match the request.",
      )
    if response.get("ok") is not True:
      error = response.get("error")
      if not isinstance(error, Mapping):
        error = {}
      raise IntegrationError(
        str(error.get("code", "dynamics_replay_failed")),
        str(error.get("message", "The dynamics replay was rejected.")),
        details=(error.get("details") if isinstance(error.get("details"), Mapping) else {}),
      )
    adapter_result = response.get("result")
    if not isinstance(adapter_result, Mapping):
      raise IntegrationError(
        "dynamics_protocol_error",
        "The dynamics adapter response has no result object.",
      )
    model_result = adapter_result.get("model")
    replay = adapter_result.get("replay")
    if not isinstance(model_result, Mapping) or str(model_result.get("sha256", "")).lower() != model_hash or not isinstance(replay, Mapping):
      raise IntegrationError(
        "dynamics_model_mismatch",
        "The dynamics adapter did not use the requested model artifact.",
      )
    persisted = dict(replay)
    persisted["provenance"] = {
      "model": dict(model_result),
      "model_hash": model_hash,
      "drive_id": drive_id,
      "telemetry_ndjson_sha256": telemetry["ndjson_sha256"],
      "telemetry_schema_version": telemetry["schema_version"],
      "timeline_version": timeline_version,
      "car_fingerprint": car_fingerprint,
      "extractor": (manifest.get("provenance", {}) if isinstance(manifest, Mapping) else {}),
      "candidate_parameters": candidate_params,
      "baseline_parameters": dict(baseline_params),
      "source_log_type": "rlog",
      "input_alignment": "timestamp_causal_recorded_history_asof",
      "max_asof_age_ms": SIMULATION_MAX_ASOF_AGE_MS,
      "telemetry_provenance": full_rlog["telemetry_provenance"],
      "controller_i_timing": "post_update_asof_source_row",
      "controller_provenance": controller_provenance,
      "full_rlog": full_rlog,
      "requested_t_us": t_us,
      "anchor_t_us": anchor_t_us,
      "horizon_us": horizon_us,
    }
    _context_progress(context, 1.0)
    return persisted

  def _simulation_rows(
    self,
    drive_id: str,
    requested_t_us: int,
    horizon_us: int,
    *,
    telemetry_path: Path,
    ndjson_relative: str,
    route_origin_log_mono_time_ns: str,
    controller_profile: Mapping[str, Any],
  ) -> tuple[list[dict[str, Any]], int]:
    horizon_steps = max(1, round(horizon_us / SIMULATION_SAMPLE_PERIOD_US))
    chunk_rows = self.database.query_all(
      """
      SELECT
        chunk_index, start_t_us, end_t_us, ndjson_path,
        byte_offset, byte_length, record_sha256
      FROM telemetry_dynamics_chunks
      WHERE drive_id = ?
        AND end_t_us >= ?
        AND start_t_us <= ?
      ORDER BY chunk_index
      """,
      (
        drive_id,
        max(0, requested_t_us - 5_000_000),
        requested_t_us + horizon_us + 1_000_000,
      ),
    )
    native_rows: list[dict[str, Any]] = []
    with telemetry_path.open("rb") as stream:
      for chunk_row in chunk_rows:
        record = self._read_indexed_telemetry_record(
          stream,
          chunk_row,
          expected_path=ndjson_relative,
          expected_record="dynamics_chunk",
        )
        try:
          self._validate_dynamics_chunk(record, 0)
        except IntegrationError as error:
          raise IntegrationError(
            "dynamics_index_corrupt",
            "A stored native dynamics chunk violates its version 1 contract.",
            details=error.details,
          ) from error
        rows = record["rows"]
        if (
          record.get("chunk") != chunk_row["chunk_index"]
          or rows[0]["nominal_t_us"] != chunk_row["start_t_us"]
          or rows[-1]["nominal_t_us"] != chunk_row["end_t_us"]
        ):
          raise IntegrationError(
            "dynamics_index_corrupt",
            "A native dynamics record does not match its compact index.",
          )
        native_rows.extend(rows)
    if not native_rows:
      raise IntegrationError(
        "full_rlog_required",
        "No native full-rlog dynamics rows are available for this drive.",
      )
    native_rows.sort(key=lambda row: row["nominal_t_us"])
    nominal_times = [row["nominal_t_us"] for row in native_rows]
    if any(
      left >= right
      for left, right in zip(
        nominal_times,
        nominal_times[1:],
        strict=False,
      )
    ):
      raise IntegrationError(
        "dynamics_index_corrupt",
        "Native dynamics rows contain duplicate or non-monotonic nominal time.",
      )
    if any(
      right - left != SIMULATION_SAMPLE_PERIOD_US
      for left, right in zip(
        nominal_times,
        nominal_times[1:],
        strict=False,
      )
    ):
      raise IntegrationError(
        "causal_telemetry_required",
        "Native dynamics rows are not an exact continuous 100 Hz timeline.",
      )
    anchor_index = (
      bisect.bisect_right(
        nominal_times,
        requested_t_us,
      )
      - 1
    )
    if anchor_index < SIMULATION_HISTORY_ROWS:
      raise IntegrationError(
        "insufficient_history",
        "The selected point has fewer than 300 native rows strictly before the anchor.",
        details={
          "required_rows": SIMULATION_HISTORY_ROWS,
          "available_rows": max(0, anchor_index),
        },
      )
    if anchor_index + horizon_steps >= len(native_rows):
      raise IntegrationError(
        "insufficient_future",
        "The selected native dynamics point lacks the requested future horizon.",
        details={
          "required_future_rows": horizon_steps,
          "available_future_rows": max(
            0,
            len(native_rows) - anchor_index - 1,
          ),
        },
      )
    selected = native_rows[anchor_index - SIMULATION_HISTORY_ROWS : anchor_index + horizon_steps + 1]
    expected_count = SIMULATION_HISTORY_ROWS + 1 + horizon_steps
    if len(selected) != expected_count:
      raise IntegrationError(
        "dynamics_index_corrupt",
        "The native replay window has an unexpected row count.",
      )
    self._validate_causal_simulation_rows(
      selected,
      route_origin_log_mono_time_ns=(route_origin_log_mono_time_ns),
      controller_profile=controller_profile,
    )
    return selected, native_rows[anchor_index]["nominal_t_us"]

  def _read_indexed_telemetry_record(
    self,
    stream: Any,
    row: Any,
    *,
    expected_path: str,
    expected_record: str,
    error_code: str = "dynamics_index_corrupt",
  ) -> dict[str, Any]:
    byte_offset = row["byte_offset"]
    byte_length = row["byte_length"]
    record_sha256 = row["record_sha256"]
    if (
      row["ndjson_path"] != expected_path
      or isinstance(byte_offset, bool)
      or not isinstance(byte_offset, int)
      or byte_offset < 0
      or isinstance(byte_length, bool)
      or not isinstance(byte_length, int)
      or byte_length < 2
      or byte_length > TELEMETRY_RECORD_LIMIT_BYTES
      or not isinstance(record_sha256, str)
      or len(record_sha256) != 64
      or any(character not in "0123456789abcdef" for character in record_sha256)
    ):
      raise IntegrationError(
        error_code,
        "A native dynamics record has an invalid immutable byte reference.",
      )
    stream.seek(byte_offset)
    encoded = stream.read(byte_length)
    if len(encoded) != byte_length or not encoded.endswith(b"\n") or b"\n" in encoded[:-1] or hashlib.sha256(encoded).hexdigest() != record_sha256:
      raise IntegrationError(
        error_code,
        "A native dynamics record failed bounded byte-range verification.",
      )
    try:
      record = strict_json_loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
      raise IntegrationError(
        error_code,
        "A native dynamics record contains invalid canonical JSON.",
      ) from error
    if not isinstance(record, dict) or record.get("record") != expected_record:
      raise IntegrationError(
        error_code,
        "A native dynamics byte reference points to the wrong record type.",
      )
    return record

  @staticmethod
  def _validate_causal_simulation_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    route_origin_log_mono_time_ns: str | None = None,
    controller_profile: Mapping[str, Any] | None = None,
  ) -> None:
    missing: set[str] = set()
    negative: set[str] = set()
    too_old: set[str] = set()
    future_car_state = False
    absolute_grid_invalid = not (isinstance(route_origin_log_mono_time_ns, str) and route_origin_log_mono_time_ns.isdigit())
    route_origin_ns = int(route_origin_log_mono_time_ns) if not absolute_grid_invalid else None
    discontinuous = False
    signed_steering_rate_invalid = False
    previous_grid_ns: int | None = None
    previous_grid_angle_deg: float | None = None
    expected_profile_id = controller_profile.get("profile_id") if isinstance(controller_profile, Mapping) else None
    expected_profile_sha256 = (
      controller_profile.get(
        "baseline_controller_params_sha256",
      )
      if isinstance(controller_profile, Mapping)
      else None
    )
    expected_source_commit = controller_profile.get("source_starpilot_commit") if isinstance(controller_profile, Mapping) else None
    expected_value_space = (
      controller_profile.get(
        "effective_torque_params_value_space",
      )
      if isinstance(controller_profile, Mapping)
      else None
    )
    expected_multiplier = (
      controller_profile.get(
        "vehicle_lat_accel_factor_multiplier",
      )
      if isinstance(controller_profile, Mapping)
      else None
    )
    for row in rows:
      if row.get("continuous") is not True:
        discontinuous = True
      applicable = {
        "car_state_age_us": row.get("car_state_age_us"),
        "car_control_age_us": row.get("car_control_age_us"),
        "car_output_age_us": row.get("car_output_age_us"),
        "controls_state_age_us": row.get("controls_state_age_us"),
      }
      applied_source = row.get("applied_torque_source")
      if applied_source != "carOutput.actuatorsOutput.torque":
        missing.add("applied_torque_source")
      if row.get("controller_type") != "conventional_torque":
        missing.add("controller_type")
      if row.get("controller_selection_source") not in CONTROLLER_SELECTION_SOURCES:
        missing.add("controller_selection_source")
      if row.get("controller_selection_stateful") is not True or not _exact_int(
        row.get(
          "controller_selection_state_machine_version",
        ),
        1,
      ):
        missing.add("controller_selection_state")
      if not IntegrationHandlers._sha256_digest(
        row.get("resolved_toggles_sha256"),
      ):
        missing.add("resolved_toggles_sha256")
      if row.get("effective_torque_params_exact") is not True:
        missing.add("effective_torque_params_exact")
      if row.get("effective_torque_params_missing_fields") != []:
        missing.add("effective_torque_params_missing_fields")
      if row.get("effective_torque_params_stateful") is not True or not _exact_int(
        row.get(
          "effective_torque_params_state_machine_version",
        ),
        1,
      ):
        missing.add("effective_torque_params_state")
      if not isinstance(row.get("live_torque_used"), bool):
        missing.add("live_torque_used")
      torque_parameter_sources = row.get(
        "effective_torque_params_source",
      )
      torque_parameter_ages = row.get(
        "effective_torque_params_source_age_us",
      )
      if (
        not isinstance(torque_parameter_sources, Mapping)
        or set(torque_parameter_sources) != {"factor", "offset", "friction"}
        or any(source not in EFFECTIVE_TORQUE_PARAMETER_SOURCES for source in torque_parameter_sources.values())
      ):
        missing.add("effective_torque_params_source")
      elif row.get("live_torque_used") is True and all(source == "car_params" for source in torque_parameter_sources.values()):
        missing.add("effective_torque_params_source")
      if not isinstance(torque_parameter_ages, Mapping) or set(torque_parameter_ages) != {"factor", "offset", "friction"}:
        missing.add("effective_torque_params_source_age_us")
      elif isinstance(torque_parameter_sources, Mapping):
        controls_state_age_us = row.get(
          "controls_state_age_us",
        )
        for part, source in torque_parameter_sources.items():
          age = torque_parameter_ages.get(part)
          if age is not None and (isinstance(age, bool) or not isinstance(age, int) or age < 0):
            missing.add(
              "effective_torque_params_source_age_us",
            )
          if source != "car_params" and (
            isinstance(age, bool)
            or not isinstance(age, int)
            or isinstance(controls_state_age_us, bool)
            or not isinstance(controls_state_age_us, int)
            or age < controls_state_age_us
          ):
            missing.add(
              "effective_torque_params_source_age_us",
            )
      if controller_profile is not None:
        multiplier = row.get(
          "vehicle_lat_accel_factor_multiplier",
        )
        if (
          row.get("effective_torque_params_value_space") != expected_value_space
          or row.get("baseline_controller_profile_id") != expected_profile_id
          or row.get("baseline_controller_params_sha256") != expected_profile_sha256
          or row.get(
            "baseline_controller_source_starpilot_commit",
          )
          != expected_source_commit
          or isinstance(expected_multiplier, bool)
          or not isinstance(
            expected_multiplier,
            (int, float),
          )
          or isinstance(multiplier, bool)
          or not isinstance(multiplier, (int, float))
          or not math.isfinite(float(multiplier))
          or float(multiplier) != float(expected_multiplier)
        ):
          missing.add("baseline_controller_profile")
      nominal_log_mono_time_ns = row.get(
        "nominal_log_mono_time_ns",
      )
      if not (isinstance(nominal_log_mono_time_ns, str) and nominal_log_mono_time_ns.isdigit()):
        absolute_grid_invalid = True
      else:
        nominal_ns = int(nominal_log_mono_time_ns)
        if (
          nominal_ns % (SIMULATION_SAMPLE_PERIOD_US * 1000) != 0 or route_origin_ns is None or (nominal_ns - route_origin_ns) // 1000 != row.get("nominal_t_us")
        ):
          absolute_grid_invalid = True
        steering_angle_deg = row.get("steering_angle_deg")
        signed_steering_rate_deg_s = row.get(
          "signed_steering_rate_deg_s",
        )
        if (
          isinstance(steering_angle_deg, bool)
          or not isinstance(steering_angle_deg, (int, float))
          or not math.isfinite(float(steering_angle_deg))
          or isinstance(signed_steering_rate_deg_s, bool)
          or not isinstance(
            signed_steering_rate_deg_s,
            (int, float),
          )
          or not math.isfinite(
            float(signed_steering_rate_deg_s),
          )
        ):
          missing.add("signed_steering_rate_deg_s")
          previous_grid_angle_deg = None
        else:
          if previous_grid_ns is not None:
            if nominal_ns - previous_grid_ns != SIMULATION_SAMPLE_PERIOD_US * 1000:
              absolute_grid_invalid = True
            elif previous_grid_angle_deg is not None:
              expected_signed_rate = (float(steering_angle_deg) - previous_grid_angle_deg) / (SIMULATION_SAMPLE_PERIOD_US / 1_000_000)
              if not math.isclose(
                float(signed_steering_rate_deg_s),
                expected_signed_rate,
                rel_tol=1e-9,
                abs_tol=1e-6,
              ):
                signed_steering_rate_invalid = True
          previous_grid_angle_deg = float(
            steering_angle_deg,
          )
        previous_grid_ns = nominal_ns
      if (
        isinstance(row.get("car_state_age_us"), int)
        and not isinstance(row.get("car_state_age_us"), bool)
        and row.get("source_time_error_us") != -row["car_state_age_us"]
      ):
        future_car_state = True
      for field, value in applicable.items():
        if isinstance(value, bool) or not isinstance(value, int):
          missing.add(field)
        elif value < 0:
          negative.add(field)
        elif value > SIMULATION_MAX_ASOF_AGE_MS * 1000:
          too_old.add(field)
    if missing or negative or too_old or future_car_state or absolute_grid_invalid or discontinuous or signed_steering_rate_invalid:
      raise IntegrationError(
        "causal_telemetry_required",
        "The replay window is not fully timestamp-causal and continuous.",
        details={
          "missing_source_ages": sorted(missing),
          "negative_source_ages": sorted(negative),
          "over_max_asof_source_ages": sorted(too_old),
          "max_asof_age_ms": SIMULATION_MAX_ASOF_AGE_MS,
          "future_car_state": future_car_state,
          "absolute_grid_invalid": absolute_grid_invalid,
          "discontinuous": discontinuous,
          "signed_steering_rate_invalid": (signed_steering_rate_invalid),
        },
      )

  @staticmethod
  def _source_age_proof_valid(
    proof: Any,
    row_count: Any,
  ) -> bool:
    return bool(
      isinstance(proof, Mapping)
      and proof.get("schema") == SOURCE_AGE_PROOF_SCHEMA
      and _exact_int(proof.get("schema_version"), 1)
      and proof.get("state") == "verified"
      and proof.get("alignment") == "latest_at_or_before_grid_time_zero_order_hold"
      and proof.get("maximum_age_us") == SIMULATION_MAX_ASOF_AGE_US
      and proof.get("comparison") == "0 <= age_us <= maximum_age_us"
      and proof.get("car_state_time_relation") == "source_time_error_us == -car_state_age_us"
      and isinstance(row_count, int)
      and not isinstance(row_count, bool)
      and row_count > SIMULATION_HISTORY_ROWS
      and proof.get("checked_row_count") == row_count
      and proof.get("valid_row_count") == row_count
      and _exact_int(proof.get("missing_required_age_count"), 0)
      and _exact_int(proof.get("negative_required_age_count"), 0)
      and _exact_int(proof.get("over_maximum_age_count"), 0)
      and _exact_int(proof.get("future_car_state_count"), 0)
      and _exact_int(
        proof.get("source_time_error_mismatch_count"),
        0,
      )
      and proof.get("required_sources") == SOURCE_AGE_REQUIRED_SOURCES
    )

  @staticmethod
  def _effective_torque_context_proof_valid(
    proof: Any,
    row_count: Any,
    source_commit: Any,
  ) -> bool:
    evaluator = proof.get("evaluator") if isinstance(proof, Mapping) else None
    if (
      not isinstance(proof, Mapping)
      or proof.get("schema") != EFFECTIVE_TORQUE_CONTEXT_PROOF_SCHEMA
      or not _exact_int(proof.get("schema_version"), 1)
      or proof.get("state") != "verified"
      or not isinstance(row_count, int)
      or isinstance(row_count, bool)
      or row_count <= SIMULATION_HISTORY_ROWS
      or proof.get("checked_row_count") != row_count
      or proof.get("exact_row_count") != row_count
      or proof.get("valid_row_count") != row_count
      or not _exact_int(proof.get("inexact_row_count"), 0)
      or not _exact_int(proof.get("missing_field_row_count"), 0)
      or not _exact_int(proof.get("invalid_row_count"), 0)
      or not _exact_int(proof.get("stateful_invalid_row_count"), 0)
      or not _exact_int(
        proof.get("context_not_bound_to_controls_row_count"),
        0,
      )
      or not _exact_int(
        proof.get("source_after_controls_row_count"),
        0,
      )
      or not _exact_int(proof.get("source_identity_invalid_count"), 0)
      or not isinstance(evaluator, Mapping)
      or evaluator.get("name")
      != EFFECTIVE_TORQUE_CONTEXT_EVALUATOR
      or not _exact_int(evaluator.get("version"), 1)
      or evaluator.get("source_sha256")
      != EFFECTIVE_TORQUE_CONTEXT_EVALUATOR_SHA256
    ):
      return False
    evaluator_source_commit = evaluator.get("source_commit")
    evaluator_id = evaluator.get("evaluator_id")
    if evaluator_source_commit is not None or evaluator_id is not None:
      if (
        evaluator_source_commit != source_commit
        or TORQUE_CONTEXT_EVALUATOR_IDS.get(source_commit)
        != evaluator_id
      ):
        return False
    for field in (
      "factor_source_counts",
      "offset_source_counts",
      "friction_source_counts",
    ):
      counts = proof.get(field)
      if (
        not isinstance(counts, Mapping)
        or not counts
        or not set(counts).issubset(
          EFFECTIVE_TORQUE_PARAMETER_SOURCES,
        )
        or any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in counts.values())
        or sum(counts.values()) != row_count
      ):
        return False
    return True

  @staticmethod
  def _require_full_rlog(
    manifest: Mapping[str, Any],
    telemetry_path: Path,
  ) -> dict[str, Any]:
    try:
      with telemetry_path.open("r", encoding="utf-8") as stream:
        header = strict_json_loads(stream.readline())
    except (OSError, ValueError, json.JSONDecodeError) as error:
      raise IntegrationError(
        "full_rlog_required",
        "The pinned telemetry generation has no valid source header.",
      ) from error
    completeness = manifest.get("completeness")
    dynamics = manifest.get("dynamics")
    provenance = manifest.get("provenance")
    timebase = manifest.get("timebase")
    telemetry_provenance = dynamics.get("telemetry_provenance") if isinstance(dynamics, Mapping) else None
    source_age_validation = dynamics.get("source_age_validation") if isinstance(dynamics, Mapping) else None
    torque_context_validation = dynamics.get("effective_torque_context_validation") if isinstance(dynamics, Mapping) else None
    source_objects = provenance.get("source_objects") if isinstance(provenance, Mapping) else None
    segments = completeness.get("segments") if isinstance(completeness, Mapping) else None
    header_timebase = header.get("timebase") if isinstance(header, Mapping) else None
    route_origin_log_mono_time_ns = (
      telemetry_provenance.get(
        "route_origin_log_mono_time_ns",
      )
      if isinstance(telemetry_provenance, Mapping)
      else None
    )
    valid = (
      isinstance(header, Mapping)
      and header.get("log_type") == "rlog"
      and manifest.get("state") == "complete"
      and manifest.get("publication_ready") is True
      and isinstance(completeness, Mapping)
      and completeness.get("contiguous_from_segment_zero") is True
      and completeness.get("route_start_observed") is True
      and completeness.get("route_end_observed") is True
      and completeness.get("boundary_chain_valid") is True
      and isinstance(segments, list)
      and bool(segments)
      and all(isinstance(segment, Mapping) and segment.get("state") == "complete" and segment.get("log_type") == "rlog" for segment in segments)
      and isinstance(dynamics, Mapping)
      and dynamics.get("state") == "available"
      and dynamics.get("alignment") == "timestamp_causal_recorded_history_asof"
      and dynamics.get("causal_input_eligible") is True
      and dynamics.get("controller_i_timing") == "post_update_asof_source_row"
      and dynamics.get("sample_period_us") == SIMULATION_SAMPLE_PERIOD_US
      and isinstance(dynamics.get("row_count"), int)
      and dynamics["row_count"] > SIMULATION_HISTORY_ROWS
      and isinstance(telemetry_provenance, Mapping)
      and telemetry_provenance.get("schema") == "comma-companion.dynamics-row"
      and _exact_int(telemetry_provenance.get("schema_version"), 1)
      and telemetry_provenance.get("alignment") == "timestamp_causal_recorded_history_asof"
      and telemetry_provenance.get("causal_input_eligible") is True
      and telemetry_provenance.get("extractor_version") == REFERENCE_TELEMETRY_EXTRACTOR_VERSION
      and telemetry_provenance.get("max_asof_age_ms") == SIMULATION_MAX_ASOF_AGE_MS
      and isinstance(route_origin_log_mono_time_ns, str)
      and route_origin_log_mono_time_ns.isdigit()
      and isinstance(timebase, Mapping)
      and timebase.get("origin_log_mono_time_ns") == route_origin_log_mono_time_ns
      and isinstance(header_timebase, Mapping)
      and header_timebase.get("origin_log_mono_time_ns") == route_origin_log_mono_time_ns
      and isinstance(
        telemetry_provenance.get("extractor_source_sha256"),
        str,
      )
      and telemetry_provenance["extractor_source_sha256"]
      == REFERENCE_TELEMETRY_EXTRACTOR_SHA256
      and isinstance(provenance, Mapping)
      and provenance.get("extractor") == "comma-companion-rlog"
      and provenance.get("extractor_version") == telemetry_provenance["extractor_version"]
      and provenance.get("extractor_source_sha256") == telemetry_provenance["extractor_source_sha256"]
      and header.get("extractor_version") == telemetry_provenance["extractor_version"]
      and isinstance(source_objects, list)
      and bool(source_objects)
      and all(
        isinstance(source, Mapping) and source.get("log_type") == "rlog" and IntegrationHandlers._sha256_digest(source.get("sha256"))
        for source in source_objects
      )
    )
    if not valid:
      raise IntegrationError(
        "full_rlog_required",
        "Dynamics replay requires a complete, provenance-verified full-rlog generation with native causal rows.",
      )
    if not IntegrationHandlers._source_age_proof_valid(
      source_age_validation,
      dynamics["row_count"],
    ):
      raise IntegrationError(
        "causal_source_ages_unverified",
        "The full-rlog generation does not prove bounded, causal as-of selection for every required plant source.",
      )
    if not IntegrationHandlers._effective_torque_context_proof_valid(
      torque_context_validation,
      dynamics["row_count"],
      provenance.get("source_starpilot_commit"),
    ):
      raise IntegrationError(
        "controller_provenance_incomplete",
        "The full-rlog generation does not prove exact factor, offset, and friction ownership for every dynamics row.",
      )
    return {
      "log_type": "rlog",
      "timeline_version": manifest.get("timeline_version"),
      "publication_ready": True,
      "dynamics": dict(dynamics),
      "telemetry_provenance": dict(telemetry_provenance),
      "source_objects": source_objects,
    }

  @staticmethod
  def _controller_selection_proof(
    manifest: Mapping[str, Any],
  ) -> dict[str, Any]:
    dynamics = manifest.get("dynamics")
    provenance = manifest.get("provenance")
    if not isinstance(dynamics, Mapping):
      raise IntegrationError(
        "controller_provenance_incomplete",
        "The full-rlog manifest has no dynamics controller proof.",
      )
    proof = dynamics.get("controller_selection_validation")
    recorded = dynamics.get("controller_provenance")
    row_count = dynamics.get("row_count")
    evaluator = proof.get("evaluator") if isinstance(proof, Mapping) else None
    source_types = proof.get("source_types") if isinstance(proof, Mapping) else None
    snapshot_hashes = proof.get("snapshot_hashes") if isinstance(proof, Mapping) else None
    controller_types = proof.get("controller_types") if isinstance(proof, Mapping) else None
    controller_counts = (
      (
        proof.get("conventional_torque_row_count"),
        proof.get("nnff_row_count"),
        proof.get("nnff_lite_row_count"),
        proof.get("unsupported_row_count"),
      )
      if isinstance(proof, Mapping)
      else ()
    )
    valid = (
      isinstance(proof, Mapping)
      and proof.get("schema") == CONTROLLER_SELECTION_PROOF_SCHEMA
      and _exact_int(proof.get("schema_version"), 1)
      and proof.get("state") == "verified"
      and isinstance(evaluator, Mapping)
      and evaluator.get("name") == CONTROLLER_SELECTION_EVALUATOR
      and _exact_int(evaluator.get("version"), 1)
      and evaluator.get("source_sha256") == CONTROLLER_SELECTION_EVALUATOR_SHA256
      and isinstance(row_count, int)
      and not isinstance(row_count, bool)
      and row_count > SIMULATION_HISTORY_ROWS
      and proof.get("checked_row_count") == row_count
      and proof.get("resolved_row_count") == row_count
      and _exact_int(proof.get("missing_row_count"), 0)
      and _exact_int(proof.get("invalid_row_count"), 0)
      and isinstance(controller_types, list)
      and bool(controller_types)
      and all(isinstance(value, str) for value in controller_types)
      and controller_types == sorted(set(controller_types))
      and set(controller_types).issubset(
        {
          "conventional_torque",
          "nnff",
          "nnff_lite",
          "unsupported",
        }
      )
      and len(controller_counts) == 4
      and all(isinstance(count, int) and not isinstance(count, bool) and count >= 0 for count in controller_counts)
      and sum(controller_counts) == row_count
      and isinstance(source_types, list)
      and bool(source_types)
      and all(isinstance(value, str) for value in source_types)
      and source_types == sorted(set(source_types))
      and set(source_types).issubset(CONTROLLER_SELECTION_SOURCES)
      and isinstance(snapshot_hashes, list)
      and bool(snapshot_hashes)
      and all(isinstance(value, str) for value in snapshot_hashes)
      and snapshot_hashes == sorted(set(snapshot_hashes))
      and all(IntegrationHandlers._sha256_digest(value) for value in snapshot_hashes)
      and isinstance(recorded, Mapping)
    )
    if not valid:
      raise IntegrationError(
        "controller_provenance_incomplete",
        "The route lacks an exact, route-wide controller-selection proof.",
      )
    if (
      controller_types != ["conventional_torque"]
      or proof.get("conventional_torque_row_count") != row_count
      or proof.get("nnff_row_count") != 0
      or proof.get("nnff_lite_row_count") != 0
      or proof.get("unsupported_row_count") != 0
    ):
      raise IntegrationError(
        "unsupported_controller_type",
        "Counterfactual replay currently supports only route-wide conventional torque control.",
        details={
          "controller_types": controller_types,
          "conventional_torque_row_count": proof.get(
            "conventional_torque_row_count",
          ),
          "nnff_row_count": proof.get("nnff_row_count"),
          "nnff_lite_row_count": proof.get(
            "nnff_lite_row_count",
          ),
          "unsupported_row_count": proof.get(
            "unsupported_row_count",
          ),
        },
      )

    resolved_snapshots = recorded.get("resolved_toggle_snapshots")
    if "starpilotPlan.starpilotToggles" in source_types:
      snapshots_valid = (
        isinstance(resolved_snapshots, list)
        and bool(resolved_snapshots)
        and all(
          isinstance(snapshot, Mapping)
          and snapshot.get("source") == "starpilotPlan.starpilotToggles"
          and snapshot.get("valid") is True
          and IntegrationHandlers._sha256_digest(
            snapshot.get("sha256"),
          )
          and snapshot.get("sha256") in snapshot_hashes
          and isinstance(snapshot.get("values"), Mapping)
          and snapshot["values"].get("nnff") is False
          and snapshot["values"].get("nnff_lite") is False
          and (
            snapshot["values"].get("nnff_model_name") is None
            or isinstance(
              snapshot["values"].get("nnff_model_name"),
              str,
            )
          )
          and isinstance(snapshot.get("segment_num"), int)
          and not isinstance(snapshot.get("segment_num"), bool)
          and snapshot["segment_num"] >= 0
          and isinstance(snapshot.get("source_ordinal"), int)
          and not isinstance(snapshot.get("source_ordinal"), bool)
          and snapshot["source_ordinal"] >= 0
          and isinstance(snapshot.get("log_mono_time_ns"), str)
          and snapshot["log_mono_time_ns"].isdigit()
          for snapshot in resolved_snapshots
        )
      )
      if not snapshots_valid:
        raise IntegrationError(
          "controller_provenance_incomplete",
          "Recorded starpilotPlan controller snapshots are incomplete or inconsistent with the route proof.",
        )

    fallback = recorded.get("init_data_fallback_evaluator")
    if "versioned_initData_fallback" in source_types:
      source_commit = provenance.get("source_starpilot_commit") if isinstance(provenance, Mapping) else None
      fallback_valid = (
        isinstance(fallback, Mapping)
        and fallback.get("state") == "available"
        and fallback.get("name")
        == EFFECTIVE_TORQUE_CONTEXT_EVALUATOR
        and _exact_int(fallback.get("version"), 1)
        and fallback.get("source_commit") == source_commit
        and fallback.get("evaluator_id")
        == TORQUE_CONTEXT_EVALUATOR_IDS.get(source_commit)
        and fallback.get("source_sha256")
        == EFFECTIVE_TORQUE_CONTEXT_EVALUATOR_SHA256
      )
      if not fallback_valid:
        raise IntegrationError(
          "controller_provenance_incomplete",
          "initData fallback rows lack their exact versioned evaluator provenance.",
        )

    return {
      "proof": dict(proof),
      "source_types": list(source_types),
      "snapshot_hashes": list(snapshot_hashes),
      "resolved_toggle_snapshots": (list(resolved_snapshots) if isinstance(resolved_snapshots, list) else []),
      "init_data_fallback_evaluator": (dict(fallback) if isinstance(fallback, Mapping) else None),
    }

  @staticmethod
  def _resolved_flm_state(
    recorded_controller: Mapping[str, Any],
    provenance: Mapping[str, Any],
  ) -> tuple[bool | None, dict[str, Any] | None]:
    if recorded_controller.get("flm_active_available") is True and isinstance(recorded_controller.get("flm_active"), bool):
      return bool(recorded_controller["flm_active"]), None
    resolution = recorded_controller.get("flm_resolution")
    evaluator = resolution.get("evaluator") if isinstance(resolution, Mapping) else None
    source_commit = provenance.get("source_starpilot_commit")
    valid_historical_resolution = (
      isinstance(resolution, Mapping)
      and resolution.get("state") == "verified"
      and resolution.get("flm_active") is False
      and resolution.get("source") == "versioned_source_commit_evaluator"
      and isinstance(evaluator, Mapping)
      and evaluator.get("name") == HISTORICAL_FLM_EVALUATOR
      and _exact_int(evaluator.get("version"), 1)
      and source_commit in NO_FLM_SOURCE_COMMITS
      and evaluator.get("source_commit") == source_commit
      and evaluator.get("source_sha256")
      == HISTORICAL_FLM_EVALUATOR_SHA256
    )
    if valid_historical_resolution:
      return False, dict(resolution)
    return None, None

  @staticmethod
  def _controller_provenance(
    manifest: Mapping[str, Any],
  ) -> dict[str, Any]:
    vehicle = manifest.get("vehicle")
    route_software = manifest.get("route_software")
    provenance = manifest.get("provenance")
    dynamics = manifest.get("dynamics")
    recorded_controller = (
      dynamics.get("controller_provenance") if isinstance(dynamics, Mapping) and isinstance(dynamics.get("controller_provenance"), Mapping) else {}
    )
    if not isinstance(vehicle, Mapping) or not isinstance(provenance, Mapping):
      raise IntegrationError(
        "full_rlog_required",
        "The full-rlog manifest lacks controller and CarParams provenance.",
      )
    selection = IntegrationHandlers._controller_selection_proof(
      manifest,
    )
    baseline_profile = validated_controller_profile(
      manifest,
    )
    if baseline_profile is None:
      raise IntegrationError(
        "controller_provenance_incomplete",
        "The full-rlog manifest has no reviewed route controller baseline.",
      )
    torque_context_validation = (
      dynamics.get("effective_torque_context_validation")
      if isinstance(dynamics, Mapping)
      else None
    )
    if not IntegrationHandlers._effective_torque_context_proof_valid(
      torque_context_validation,
      dynamics.get("row_count") if isinstance(dynamics, Mapping) else None,
      provenance.get("source_starpilot_commit"),
    ):
      raise IntegrationError(
        "controller_provenance_incomplete",
        "The full-rlog manifest has no exact effective-torque context proof.",
      )
    lateral_tuning_type = vehicle.get("lateral_tuning_type")
    raw_controller_params = (
      route_software.get("controller_params") if isinstance(route_software, Mapping) and isinstance(route_software.get("controller_params"), Mapping) else {}
    )
    if lateral_tuning_type != "torque":
      raise IntegrationError(
        "unsupported_controller_type",
        "Counterfactual replay currently models only torque-tuned routes.",
        details={
          "lateral_tuning_type": lateral_tuning_type,
          "steer_control_type": vehicle.get("steer_control_type"),
        },
      )
    controller_type = "conventional_torque"
    tuning_snapshot = vehicle.get("lateral_torque_tuning") if isinstance(vehicle.get("lateral_torque_tuning"), Mapping) else {}
    car_params_sha256 = provenance.get("car_params_wire_sha256") or provenance.get("car_params_summary_sha256")
    if not controller_type or not car_params_sha256:
      raise IntegrationError(
        "full_rlog_required",
        "The full-rlog manifest cannot identify the recorded controller or CarParams.",
      )
    car_params_complete = all(
      IntegrationHandlers._sha256_digest(provenance.get(field))
      for field in (
        "car_params_wire_sha256",
        "car_params_summary_sha256",
      )
    )
    flm_active, flm_resolution = IntegrationHandlers._resolved_flm_state(
      recorded_controller,
      provenance,
    )
    if not isinstance(flm_active, bool):
      raise IntegrationError(
        "controller_provenance_incomplete",
        "The route has no verified FLM activation state.",
      )
    tuning_snapshot_complete = (
      IntegrationHandlers._sha256_digest(
        provenance.get("controller_params_sha256"),
      )
      and bool(tuning_snapshot)
      and all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) for value in tuning_snapshot.values())
      and isinstance(flm_active, bool)
      and recorded_controller.get("trailer_load_available") is True
      and isinstance(
        recorded_controller.get("trailer_load_kg"),
        (int, float),
      )
      and not isinstance(
        recorded_controller.get("trailer_load_kg"),
        bool,
      )
      and math.isfinite(
        float(recorded_controller["trailer_load_kg"]),
      )
    )
    return {
      "controller_type": controller_type,
      "controller_type_verified": True,
      "car_params": dict(vehicle),
      "car_params_sha256": car_params_sha256,
      "car_params_complete": car_params_complete,
      "car_params_provenance": {
        "wire_sha256": provenance.get("car_params_wire_sha256"),
        "summary_sha256": provenance.get("car_params_summary_sha256"),
        "source_starpilot_commit": provenance.get(
          "source_starpilot_commit",
        ),
      },
      "toggle_snapshot": {
        "controller_selection_source_types": selection["source_types"],
        "resolved_toggles_sha256": selection["snapshot_hashes"],
        "resolved_toggle_snapshots": selection["resolved_toggle_snapshots"],
        "init_data_fallback_evaluator": selection["init_data_fallback_evaluator"],
        "flm_active": flm_active,
        "flm_active_available": isinstance(flm_active, bool),
        "flm_resolution": flm_resolution,
        "trailer_load_kg": recorded_controller.get(
          "trailer_load_kg",
        ),
        "raw_controller_params": dict(raw_controller_params),
      },
      "tuning_snapshot": dict(tuning_snapshot),
      "tuning_snapshot_complete": bool(tuning_snapshot_complete),
      "tuning_provenance": {
        "controller_selection_validation": selection["proof"],
        "effective_torque_context_validation": dict(
          torque_context_validation,
        ),
        "controller_params_sha256": provenance.get(
          "controller_params_sha256",
        ),
        "baseline_controller_profile": baseline_profile,
        "baseline_exact_claim_allowed": provenance.get(
          "baseline_exact_claim_allowed",
        ),
        "baseline_limitations": provenance.get(
          "baseline_limitations",
          [],
        ),
      },
    }

  @staticmethod
  def _snapshot_boolean(
    snapshot: Mapping[str, Any],
    key: str,
  ) -> bool | None:
    item = snapshot.get(key)
    text = item.get("text") if isinstance(item, Mapping) else None
    if not isinstance(text, str):
      return None
    normalized = text.strip().lower()
    if normalized in {"0", "false", "off", "no", ""}:
      return False
    if normalized in {"1", "true", "on", "yes"}:
      return True
    return None

  @staticmethod
  def _snapshot_text(
    snapshot: Mapping[str, Any],
    key: str,
  ) -> str | None:
    item = snapshot.get(key)
    text = item.get("text") if isinstance(item, Mapping) else None
    return text.strip() if isinstance(text, str) else None

  @staticmethod
  def _sha256_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and value == value.lower() and all(character in "0123456789abcdef" for character in value)
