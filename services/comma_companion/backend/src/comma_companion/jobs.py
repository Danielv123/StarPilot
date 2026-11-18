from __future__ import annotations

import json
import math
import os
import socket
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .db import Database, isoformat, utc_now


ACTIVE_STATES = frozenset({"leased", "running"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled"})
WORKER_STALE_AFTER_SECONDS = 15


class JobError(RuntimeError):
  pass


class JobLeaseLost(JobError):
  pass


class JobCancelled(JobError):
  pass


class PermanentJobError(JobError):
  pass


@dataclass(frozen=True, slots=True)
class Job:
  id: str
  type: str
  state: str
  priority: int
  dedupe_key: str | None
  payload: dict[str, Any]
  progress: float
  attempts: int
  max_attempts: int
  lease_owner: str | None
  lease_expires_at: str | None
  available_at: str | None
  cancel_requested_at: str | None
  retryable: bool | None
  error: str | None
  result: dict[str, Any]
  created_at: str
  updated_at: str
  completed_at: str | None

  @classmethod
  def from_row(cls, row: Mapping[str, Any]) -> Job:
    payload = json.loads(row["payload_json"])
    result = json.loads(row["result_json"])
    if not isinstance(payload, dict):
      raise ValueError("job payload_json must contain a JSON object")
    if not isinstance(result, dict):
      raise ValueError("job result_json must contain a JSON object")
    return cls(
      id=row["id"],
      type=row["type"],
      state=row["state"],
      priority=row["priority"],
      dedupe_key=row["dedupe_key"],
      payload=payload,
      progress=float(row["progress"]),
      attempts=row["attempts"],
      max_attempts=row["max_attempts"],
      lease_owner=row["lease_owner"],
      lease_expires_at=row["lease_expires_at"],
      available_at=row["available_at"],
      cancel_requested_at=row["cancel_requested_at"],
      retryable=(None if row["retryable"] is None else bool(row["retryable"])),
      error=row["error"],
      result=result,
      created_at=row["created_at"],
      updated_at=row["updated_at"],
      completed_at=row["completed_at"],
    )


def _now(value: datetime | None) -> datetime:
  current = value or utc_now()
  if current.tzinfo is None or current.utcoffset() is None:
    raise ValueError("job timestamps must be timezone-aware")
  return current.astimezone(UTC)


def _seconds(value: float, name: str) -> float:
  converted = float(value)
  if not math.isfinite(converted) or converted < 0:
    raise ValueError(f"{name} must be a finite, non-negative number")
  return converted


def _lease_seconds(value: float) -> float:
  converted = _seconds(value, "lease_seconds")
  if converted == 0:
    raise ValueError("lease_seconds must be positive")
  return converted


def _progress(value: float) -> float:
  converted = float(value)
  if not math.isfinite(converted) or converted < 0 or converted > 1:
    raise ValueError("progress must be between 0 and 1")
  return converted


def _json_object(value: Mapping[str, Any] | None) -> tuple[dict[str, Any], str]:
  result = dict(value or {})
  encoded = json.dumps(result, separators=(",", ":"), sort_keys=True)
  return result, encoded


def get_job(database: Database, job_id: str) -> Job | None:
  row = database.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
  return Job.from_row(row) if row is not None else None


def record_worker_heartbeat(
  database: Database,
  worker_id: str,
  *,
  now: datetime | None = None,
) -> str:
  if not worker_id:
    raise ValueError("worker_id must not be empty")
  last_seen_at = isoformat(_now(now))
  database.execute(
    """
    INSERT INTO worker_heartbeats(worker_id, last_seen_at)
    VALUES (?, ?)
    ON CONFLICT(worker_id) DO UPDATE SET
      last_seen_at = excluded.last_seen_at
    """,
    (worker_id, last_seen_at),
  )
  return last_seen_at


def enqueue_job(
  database: Database,
  job_type: str,
  payload: Mapping[str, Any],
  *,
  priority: int = 0,
  max_attempts: int = 3,
  dedupe_key: str | None = None,
  available_at: datetime | None = None,
  job_id: str | None = None,
  now: datetime | None = None,
) -> Job:
  if not job_type:
    raise ValueError("job_type must not be empty")
  if max_attempts < 1:
    raise ValueError("max_attempts must be at least one")
  _, payload_json = _json_object(payload)
  created_at = _now(now)
  created = isoformat(created_at)
  available = isoformat(_now(available_at)) if available_at is not None else created
  identifier = job_id or uuid4().hex
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO jobs(
        id, type, state, priority, payload_json, dedupe_key,
        max_attempts, available_at, created_at, updated_at
      ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        identifier,
        job_type,
        priority,
        payload_json,
        dedupe_key,
        max_attempts,
        available,
        created,
        created,
      ),
    )
    row = connection.execute(
      "SELECT * FROM jobs WHERE id = ?",
      (identifier,),
    ).fetchone()
  assert row is not None
  return Job.from_row(row)


def claim_job(
  database: Database,
  worker_id: str,
  *,
  lease_seconds: float = 60,
  now: datetime | None = None,
  allowed_types: Sequence[str] | None = None,
) -> Job | None:
  if not worker_id:
    raise ValueError("worker_id must not be empty")
  lease_duration = _lease_seconds(lease_seconds)
  claimed_at = _now(now)
  now_text = isoformat(claimed_at)
  lease_expires_at = isoformat(claimed_at + timedelta(seconds=lease_duration))
  type_clause = ""
  type_parameters: tuple[str, ...] = ()
  if allowed_types is not None:
    type_parameters = tuple(dict.fromkeys(allowed_types))
    if not type_parameters:
      raise ValueError("allowed_types must not be empty")
    placeholders = ",".join("?" for _ in type_parameters)
    type_clause = f"AND candidate_job.type IN ({placeholders})"
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      UPDATE jobs
      SET state = 'canceled',
        lease_owner = NULL,
        lease_expires_at = NULL,
        available_at = NULL,
        completed_at = ?,
        updated_at = ?
      WHERE cancel_requested_at IS NOT NULL
        AND (
          state = 'queued'
          OR (
            state IN ('leased', 'running')
            AND lease_owner IS NOT NULL
            AND (
              lease_expires_at IS NULL
              OR julianday(lease_expires_at) <= julianday(?)
            )
          )
        )
      """,
      (now_text, now_text, now_text),
    )
    connection.execute(
      """
      UPDATE jobs
      SET state = 'failed',
        lease_owner = NULL,
        lease_expires_at = NULL,
        available_at = NULL,
        retryable = 0,
        error = COALESCE(
          error,
          'superseded telemetry generation after lease expiry'
        ),
        completed_at = ?,
        updated_at = ?
      WHERE type = 'extract_telemetry'
        AND cancel_requested_at IS NULL
        AND state IN ('leased', 'running')
        AND lease_owner IS NOT NULL
        AND (
          lease_expires_at IS NULL
          OR julianday(lease_expires_at) <= julianday(?)
        )
        AND EXISTS (
          SELECT 1
          FROM jobs successor
          WHERE successor.id != jobs.id
            AND successor.type = 'extract_telemetry'
            AND successor.state = 'queued'
            AND successor.cancel_requested_at IS NULL
            AND successor.attempts < successor.max_attempts
            AND successor.lease_owner IS NULL
            AND json_extract(
              successor.payload_json,
              '$.drive_id'
            ) = json_extract(
              jobs.payload_json,
              '$.drive_id'
            )
        )
      """,
      (now_text, now_text, now_text),
    )
    connection.execute(
      """
      UPDATE jobs
      SET state = 'failed',
        lease_owner = NULL,
        lease_expires_at = NULL,
        available_at = NULL,
        retryable = 1,
        error = COALESCE(error, 'maximum attempts exhausted'),
        completed_at = ?,
        updated_at = ?
      WHERE cancel_requested_at IS NULL
        AND attempts >= max_attempts
        AND (
          state = 'queued'
          OR (
            state IN ('leased', 'running')
            AND lease_owner IS NOT NULL
            AND (
              lease_expires_at IS NULL
              OR julianday(lease_expires_at) <= julianday(?)
            )
          )
        )
      """,
      (now_text, now_text, now_text),
    )
    candidate = connection.execute(
      f"""
      SELECT candidate_job.id
      FROM jobs candidate_job
      WHERE candidate_job.cancel_requested_at IS NULL
        AND candidate_job.attempts < candidate_job.max_attempts
        {type_clause}
        AND (
          (
            candidate_job.state = 'queued'
            AND candidate_job.lease_owner IS NULL
            AND (
              candidate_job.available_at IS NULL
              OR julianday(candidate_job.available_at) <= julianday(?)
            )
          )
          OR (
            candidate_job.state IN ('leased', 'running')
            AND candidate_job.lease_owner IS NOT NULL
            AND (
              candidate_job.lease_expires_at IS NULL
              OR julianday(candidate_job.lease_expires_at) <= julianday(?)
            )
          )
        )
        AND (
          candidate_job.state != 'queued'
          OR candidate_job.type != 'extract_telemetry'
          OR json_extract(
            candidate_job.payload_json,
            '$.drive_id'
          ) IS NULL
          OR NOT EXISTS (
            SELECT 1
            FROM jobs active_generation
            WHERE active_generation.id != candidate_job.id
              AND active_generation.type = 'extract_telemetry'
              AND active_generation.state IN ('leased', 'running')
              AND json_extract(
                active_generation.payload_json,
                '$.drive_id'
              ) = json_extract(
                candidate_job.payload_json,
                '$.drive_id'
              )
          )
        )
      ORDER BY candidate_job.priority DESC,
        candidate_job.created_at,
        candidate_job.id
      LIMIT 1
      """,
      (*type_parameters, now_text, now_text),
    ).fetchone()
    if candidate is None:
      return None
    connection.execute(
      """
      UPDATE jobs
      SET state = 'running',
        progress = 0,
        attempts = attempts + 1,
        lease_owner = ?,
        lease_expires_at = ?,
        available_at = NULL,
        retryable = NULL,
        completed_at = NULL,
        updated_at = ?
      WHERE id = ?
      """,
      (worker_id, lease_expires_at, now_text, candidate["id"]),
    )
    row = connection.execute(
      "SELECT * FROM jobs WHERE id = ?",
      (candidate["id"],),
    ).fetchone()
  assert row is not None
  return Job.from_row(row)


def heartbeat_job(
  database: Database,
  job_id: str,
  worker_id: str,
  *,
  lease_seconds: float = 60,
  progress: float | None = None,
  now: datetime | None = None,
) -> Job | None:
  lease_duration = _lease_seconds(lease_seconds)
  fraction = _progress(progress) if progress is not None else None
  heartbeat_at = _now(now)
  now_text = isoformat(heartbeat_at)
  lease_expires_at = isoformat(heartbeat_at + timedelta(seconds=lease_duration))
  with database.transaction(immediate=True) as connection:
    cursor = connection.execute(
      """
      UPDATE jobs
      SET progress = CASE
          WHEN ? IS NULL THEN progress
          WHEN ? > progress THEN ?
          ELSE progress
        END,
        lease_expires_at = ?,
        updated_at = ?
      WHERE id = ?
        AND lease_owner = ?
        AND state IN ('leased', 'running')
        AND lease_expires_at IS NOT NULL
        AND julianday(lease_expires_at) > julianday(?)
      """,
      (
        fraction,
        fraction,
        fraction,
        lease_expires_at,
        now_text,
        job_id,
        worker_id,
        now_text,
      ),
    )
    if cursor.rowcount != 1:
      return None
    row = connection.execute(
      "SELECT * FROM jobs WHERE id = ?",
      (job_id,),
    ).fetchone()
  assert row is not None
  return Job.from_row(row)


def complete_job(
  database: Database,
  job_id: str,
  worker_id: str,
  result: Mapping[str, Any] | None = None,
  *,
  now: datetime | None = None,
) -> Job | None:
  _, result_json = _json_object(result)
  completed_at = isoformat(_now(now))
  with database.transaction(immediate=True) as connection:
    row = connection.execute(
      """
      SELECT *
      FROM jobs
      WHERE id = ?
        AND lease_owner = ?
        AND state IN ('leased', 'running')
        AND lease_expires_at IS NOT NULL
        AND julianday(lease_expires_at) > julianday(?)
      """,
      (job_id, worker_id, completed_at),
    ).fetchone()
    if row is None:
      return None
    state = "canceled" if row["cancel_requested_at"] is not None else "succeeded"
    progress = row["progress"] if state == "canceled" else 1
    connection.execute(
      """
      UPDATE jobs
      SET state = ?,
        progress = ?,
        lease_owner = NULL,
        lease_expires_at = NULL,
        available_at = NULL,
        retryable = NULL,
        error = NULL,
        result_json = ?,
        completed_at = ?,
        updated_at = ?
      WHERE id = ?
      """,
      (
        state,
        progress,
        result_json if state == "succeeded" else row["result_json"],
        completed_at,
        completed_at,
        job_id,
      ),
    )
    updated = connection.execute(
      "SELECT * FROM jobs WHERE id = ?",
      (job_id,),
    ).fetchone()
  assert updated is not None
  return Job.from_row(updated)


def fail_job(
  database: Database,
  job_id: str,
  worker_id: str,
  error: str,
  *,
  retryable: bool = True,
  retry_delay_seconds: float = 0,
  now: datetime | None = None,
) -> Job | None:
  delay = _seconds(retry_delay_seconds, "retry_delay_seconds")
  failed_at = _now(now)
  now_text = isoformat(failed_at)
  message = str(error)
  with database.transaction(immediate=True) as connection:
    row = connection.execute(
      """
      SELECT *
      FROM jobs
      WHERE id = ?
        AND lease_owner = ?
        AND state IN ('leased', 'running')
        AND lease_expires_at IS NOT NULL
        AND julianday(lease_expires_at) > julianday(?)
      """,
      (job_id, worker_id, now_text),
    ).fetchone()
    if row is None:
      return None
    canceled = row["cancel_requested_at"] is not None
    exhausted = row["attempts"] >= row["max_attempts"]
    superseded = False
    if row["type"] == "extract_telemetry":
      superseded = connection.execute(
        """
        SELECT 1
        FROM jobs successor
        WHERE successor.id != ?
          AND successor.type = 'extract_telemetry'
          AND successor.state = 'queued'
          AND successor.cancel_requested_at IS NULL
          AND successor.attempts < successor.max_attempts
          AND successor.lease_owner IS NULL
          AND json_extract(
            successor.payload_json,
            '$.drive_id'
          ) = json_extract(?, '$.drive_id')
        LIMIT 1
        """,
        (job_id, row["payload_json"]),
      ).fetchone() is not None
    if canceled:
      state = "canceled"
    elif retryable and not exhausted and not superseded:
      state = "queued"
    else:
      state = "failed"
    available_at = isoformat(failed_at + timedelta(seconds=delay)) if state == "queued" else None
    completed_at = now_text if state in TERMINAL_STATES else None
    retryable_value = (
      int(retryable and not superseded)
      if state == "failed"
      else None
    )
    connection.execute(
      """
      UPDATE jobs
      SET state = ?,
        lease_owner = NULL,
        lease_expires_at = NULL,
        available_at = ?,
        retryable = ?,
        error = ?,
        completed_at = ?,
        updated_at = ?
      WHERE id = ?
      """,
      (
        state,
        available_at,
        retryable_value,
        message,
        completed_at,
        now_text,
        job_id,
      ),
    )
    updated = connection.execute(
      "SELECT * FROM jobs WHERE id = ?",
      (job_id,),
    ).fetchone()
  assert updated is not None
  return Job.from_row(updated)


def request_cancellation(
  database: Database,
  job_id: str,
  *,
  now: datetime | None = None,
) -> Job | None:
  requested_at = isoformat(_now(now))
  with database.transaction(immediate=True) as connection:
    row = connection.execute(
      "SELECT * FROM jobs WHERE id = ?",
      (job_id,),
    ).fetchone()
    if row is None:
      return None
    if row["state"] in TERMINAL_STATES:
      return Job.from_row(row)
    if row["state"] == "queued":
      connection.execute(
        """
        UPDATE jobs
        SET state = 'canceled',
          cancel_requested_at = COALESCE(cancel_requested_at, ?),
          lease_owner = NULL,
          lease_expires_at = NULL,
          available_at = NULL,
          completed_at = ?,
          updated_at = ?
        WHERE id = ?
        """,
        (requested_at, requested_at, requested_at, job_id),
      )
    else:
      connection.execute(
        """
        UPDATE jobs
        SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
          updated_at = ?
        WHERE id = ?
        """,
        (requested_at, requested_at, job_id),
      )
    updated = connection.execute(
      "SELECT * FROM jobs WHERE id = ?",
      (job_id,),
    ).fetchone()
  assert updated is not None
  return Job.from_row(updated)


def cancellation_requested(database: Database, job_id: str) -> bool:
  row = database.query_one(
    """
    SELECT state, cancel_requested_at
    FROM jobs
    WHERE id = ?
    """,
    (job_id,),
  )
  return row is not None and (row["cancel_requested_at"] is not None or row["state"] == "canceled")


def cancel_job(
  database: Database,
  job_id: str,
  worker_id: str,
  *,
  now: datetime | None = None,
) -> Job | None:
  canceled_at = isoformat(_now(now))
  with database.transaction(immediate=True) as connection:
    cursor = connection.execute(
      """
      UPDATE jobs
      SET state = 'canceled',
        cancel_requested_at = COALESCE(cancel_requested_at, ?),
        lease_owner = NULL,
        lease_expires_at = NULL,
        available_at = NULL,
        completed_at = ?,
        updated_at = ?
      WHERE id = ?
        AND lease_owner = ?
        AND state IN ('leased', 'running')
        AND lease_expires_at IS NOT NULL
        AND julianday(lease_expires_at) > julianday(?)
      """,
      (
        canceled_at,
        canceled_at,
        canceled_at,
        job_id,
        worker_id,
        canceled_at,
      ),
    )
    if cursor.rowcount != 1:
      return None
    row = connection.execute(
      "SELECT * FROM jobs WHERE id = ?",
      (job_id,),
    ).fetchone()
  assert row is not None
  return Job.from_row(row)


class JobContext:
  def __init__(
    self,
    database: Database,
    job: Job,
    worker_id: str,
    *,
    lease_seconds: float,
    cancel_path: Path,
  ):
    self.database = database
    self.job = job
    self.job_id = job.id
    self.job_type = job.type
    self.worker_id = worker_id
    self.lease_seconds = _lease_seconds(lease_seconds)
    self.cancel_path = cancel_path
    self.cancel_event = threading.Event()
    self._cancel_requested = threading.Event()
    self._lease_lost = threading.Event()

  def heartbeat(self, progress: float | None = None) -> None:
    record_worker_heartbeat(self.database, self.worker_id)
    updated = heartbeat_job(
      self.database,
      self.job_id,
      self.worker_id,
      lease_seconds=self.lease_seconds,
      progress=progress,
    )
    if updated is None:
      self._mark_lease_lost()
      raise JobLeaseLost(f"lease lost for job {self.job_id}")
    self.job = updated

  def progress(self, fraction: float) -> None:
    self.heartbeat(progress=fraction)

  def cancellation_requested(self) -> bool:
    if not self._cancel_requested.is_set() and cancellation_requested(
      self.database,
      self.job_id,
    ):
      self._mark_cancel_requested()
    return self._cancel_requested.is_set()

  def raise_if_cancelled(self) -> None:
    if self._lease_lost.is_set():
      raise JobLeaseLost(f"lease lost for job {self.job_id}")
    if self.cancellation_requested():
      raise JobCancelled(f"cancellation requested for job {self.job_id}")

  @property
  def lease_lost(self) -> bool:
    return self._lease_lost.is_set()

  def _touch_cancel_path(self) -> None:
    try:
      self.cancel_path.touch(exist_ok=True)
    except OSError:
      pass

  def _mark_cancel_requested(self) -> None:
    self._cancel_requested.set()
    self._touch_cancel_path()
    self.cancel_event.set()

  def _mark_lease_lost(self) -> None:
    self._lease_lost.set()
    self._touch_cancel_path()
    self.cancel_event.set()


JobHandler = Callable[
  [JobContext, dict[str, Any]],
  Mapping[str, Any] | None,
]


def _monitor_job(
  context: JobContext,
  finished: threading.Event,
  heartbeat_interval: float,
) -> None:
  while not finished.wait(heartbeat_interval):
    try:
      context.heartbeat()
    except Exception:
      context._mark_lease_lost()
      return
    context.cancellation_requested()


def _worker_id() -> str:
  return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"


def _error_text(error: BaseException) -> str:
  message = str(error).strip()
  rendered = f"{type(error).__name__}: {message}" if message else type(error).__name__
  return rendered[-16_384:]


def run_worker(
  database: Database,
  handlers: Mapping[str, JobHandler],
  stop_event: threading.Event,
  *,
  worker_id: str | None = None,
  lease_seconds: float = 60,
  poll_interval: float = 1,
  heartbeat_interval: float | None = None,
  retry_backoff_seconds: float = 1,
  max_retry_backoff_seconds: float = 60,
  record_liveness: bool = True,
) -> None:
  lease_duration = _lease_seconds(lease_seconds)
  poll_delay = _seconds(poll_interval, "poll_interval")
  retry_base = _seconds(retry_backoff_seconds, "retry_backoff_seconds")
  retry_cap = _seconds(max_retry_backoff_seconds, "max_retry_backoff_seconds")
  if retry_cap < retry_base:
    raise ValueError("max_retry_backoff_seconds must not be less than retry_backoff_seconds")
  heartbeat_delay = min(lease_duration / 3, 5) if heartbeat_interval is None else _seconds(heartbeat_interval, "heartbeat_interval")
  if heartbeat_delay <= 0 or heartbeat_delay >= lease_duration:
    raise ValueError("heartbeat_interval must be positive and shorter than lease_seconds")
  owner = worker_id or _worker_id()

  while not stop_event.is_set():
    if record_liveness:
      record_worker_heartbeat(database, owner)
    job = claim_job(
      database,
      owner,
      lease_seconds=lease_duration,
      allowed_types=tuple(handlers),
    )
    if job is None:
      stop_event.wait(poll_delay)
      continue
    handler = handlers.get(job.type)
    if handler is None:
      fail_job(
        database,
        job.id,
        owner,
        f"no handler registered for job type {job.type!r}",
        retryable=False,
      )
      continue

    with tempfile.TemporaryDirectory(prefix="comma-companion-job-") as temporary:
      context = JobContext(
        database,
        job,
        owner,
        lease_seconds=lease_duration,
        cancel_path=Path(temporary) / "cancel",
      )
      finished = threading.Event()
      monitor = threading.Thread(
        target=_monitor_job,
        args=(context, finished, heartbeat_delay),
        name=f"job-heartbeat-{job.id[:12]}",
        daemon=True,
      )
      monitor.start()
      result: Mapping[str, Any] | None = None
      failure: BaseException | None = None
      retryable = True
      try:
        context.raise_if_cancelled()
        result = handler(context, job.payload)
        if result is not None and not isinstance(result, Mapping):
          raise PermanentJobError("job handlers must return a mapping or None")
        if result is not None:
          _json_object(result)
        context.raise_if_cancelled()
      except JobCancelled as exc:
        failure = exc
        retryable = False
      except JobLeaseLost:
        context._mark_lease_lost()
      except PermanentJobError as exc:
        failure = exc
        retryable = False
      except Exception as exc:
        failure = exc
        retryable = bool(getattr(exc, "retryable", True))
        retryable = bool(getattr(exc, "retryable", True))
      finally:
        finished.set()
        monitor.join()

      if context.lease_lost:
        continue
      if context.cancellation_requested():
        cancel_job(database, job.id, owner)
      elif failure is not None:
        exponent = min(max(job.attempts - 1, 0), 30)
        retry_delay = min(retry_cap, retry_base * (2**exponent))
        fail_job(
          database,
          job.id,
          owner,
          _error_text(failure),
          retryable=retryable,
          retry_delay_seconds=retry_delay,
        )
      else:
        complete_job(database, job.id, owner, result)
