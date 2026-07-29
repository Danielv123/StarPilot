from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from comma_companion.db import Database
from comma_companion.jobs import (
  JobContext,
  cancellation_requested,
  claim_job,
  complete_job,
  enqueue_job,
  fail_job,
  get_job,
  heartbeat_job,
  request_cancellation,
  run_worker,
)


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Database:
  result = Database(tmp_path / "companion.sqlite3")
  result.initialize()
  return result


def test_claim_is_priority_ordered_and_transactionally_exclusive(
  database: Database,
) -> None:
  low = enqueue_job(
    database,
    "low",
    {"value": 1},
    priority=0,
    now=NOW,
  )
  high = enqueue_job(
    database,
    "high",
    {"value": 2},
    priority=10,
    now=NOW + timedelta(seconds=1),
  )

  first = claim_job(database, "priority-worker", now=NOW + timedelta(seconds=2))
  assert first is not None
  assert first.id == high.id
  assert first.attempts == 1
  assert (
    complete_job(
      database,
      first.id,
      "priority-worker",
      now=NOW + timedelta(seconds=3),
    )
    is not None
  )

  other = enqueue_job(
    database,
    "other",
    {"value": 3},
    now=NOW + timedelta(seconds=4),
  )
  barrier = threading.Barrier(3)

  def claim(owner: str):
    barrier.wait()
    return claim_job(
      database,
      owner,
      lease_seconds=30,
      now=NOW + timedelta(seconds=5),
    )

  with ThreadPoolExecutor(max_workers=2) as executor:
    futures = [
      executor.submit(claim, "worker-a"),
      executor.submit(claim, "worker-b"),
    ]
    barrier.wait()
    claimed = [future.result(timeout=5) for future in futures]

  assert all(job is not None for job in claimed)
  assert {job.id for job in claimed if job is not None} == {low.id, other.id}
  assert len({job.lease_owner for job in claimed if job is not None}) == 2


def test_heartbeat_progress_and_expired_lease_recovery(
  database: Database,
) -> None:
  queued = enqueue_job(database, "work", {"input": "x"}, now=NOW)
  original = claim_job(
    database,
    "worker-one",
    lease_seconds=10,
    now=NOW,
  )
  assert original is not None
  assert original.id == queued.id

  heartbeat = heartbeat_job(
    database,
    queued.id,
    "worker-one",
    lease_seconds=10,
    progress=0.4,
    now=NOW + timedelta(seconds=5),
  )
  assert heartbeat is not None
  assert heartbeat.progress == pytest.approx(0.4)
  non_regressing = heartbeat_job(
    database,
    queued.id,
    "worker-one",
    lease_seconds=10,
    progress=0.2,
    now=NOW + timedelta(seconds=6),
  )
  assert non_regressing is not None
  assert non_regressing.progress == pytest.approx(0.4)
  assert (
    claim_job(
      database,
      "worker-two",
      now=NOW + timedelta(seconds=14),
    )
    is None
  )

  recovered = claim_job(
    database,
    "worker-two",
    lease_seconds=10,
    now=NOW + timedelta(seconds=17),
  )
  assert recovered is not None
  assert recovered.id == queued.id
  assert recovered.attempts == 2
  assert recovered.progress == 0
  assert (
    complete_job(
      database,
      queued.id,
      "worker-one",
      {"stale": True},
      now=NOW + timedelta(seconds=18),
    )
    is None
  )

  completed = complete_job(
    database,
    queued.id,
    "worker-two",
    {"ok": True},
    now=NOW + timedelta(seconds=18),
  )
  assert completed is not None
  assert completed.state == "succeeded"
  assert completed.progress == 1
  assert completed.result == {"ok": True}


@pytest.mark.parametrize(
  "successor_fingerprint",
  ("1" * 64, "2" * 64),
)
def test_expired_telemetry_generation_yields_to_queued_successor(
  database: Database,
  successor_fingerprint: str,
) -> None:
  drive_id = "drive-generation"
  original = enqueue_job(
    database,
    "extract_telemetry",
    {
      "drive_id": drive_id,
      "route_name": "route-generation",
      "source_fingerprint": "1" * 64,
    },
    dedupe_key=f"drive:{drive_id}",
    now=NOW,
  )
  claimed = claim_job(
    database,
    "generation-worker",
    lease_seconds=10,
    now=NOW,
  )
  assert claimed is not None
  assert claimed.id == original.id
  successor = enqueue_job(
    database,
    "extract_telemetry",
    {
      "drive_id": drive_id,
      "route_name": "route-generation",
      "source_fingerprint": successor_fingerprint,
    },
    dedupe_key=f"drive:{drive_id}:after:{original.id}",
    now=NOW + timedelta(seconds=1),
  )

  assert (
    claim_job(
      database,
      "other-worker",
      now=NOW + timedelta(seconds=9),
    )
    is None
  )
  next_generation = claim_job(
    database,
    "other-worker",
    lease_seconds=10,
    now=NOW + timedelta(seconds=11),
  )
  assert next_generation is not None
  assert next_generation.id == successor.id
  superseded = get_job(database, original.id)
  assert superseded is not None
  assert superseded.state == "failed"
  assert superseded.retryable is False
  assert superseded.payload["source_fingerprint"] == "1" * 64
  assert superseded.error == (
    "superseded telemetry generation after lease expiry"
  )


def test_retry_not_before_and_max_attempts_are_durable(
  database: Database,
) -> None:
  queued = enqueue_job(
    database,
    "flaky",
    {},
    max_attempts=2,
    now=NOW,
  )
  first = claim_job(database, "worker", lease_seconds=30, now=NOW)
  assert first is not None
  retry = fail_job(
    database,
    queued.id,
    "worker",
    "temporary failure",
    retry_delay_seconds=5,
    now=NOW + timedelta(seconds=1),
  )
  assert retry is not None
  assert retry.id == queued.id
  assert retry.state == "queued"
  assert retry.attempts == 1
  assert retry.lease_owner is None
  assert retry.lease_expires_at is None
  assert retry.available_at == "2026-07-29T12:00:06Z"
  assert (
    claim_job(
      database,
      "worker",
      now=NOW + timedelta(seconds=5),
    )
    is None
  )

  second = claim_job(
    database,
    "worker",
    lease_seconds=30,
    now=NOW + timedelta(seconds=6),
  )
  assert second is not None
  assert second.id == queued.id
  assert second.attempts == 2
  assert second.available_at is None
  exhausted = fail_job(
    database,
    queued.id,
    "worker",
    "failed again",
    retryable=True,
    retry_delay_seconds=5,
    now=NOW + timedelta(seconds=7),
  )
  assert exhausted is not None
  assert exhausted.state == "failed"
  assert exhausted.error == "failed again"
  assert exhausted.completed_at is not None
  assert exhausted.available_at is None
  assert exhausted.lease_expires_at is None
  assert (
    claim_job(
      database,
      "another-worker",
      now=NOW + timedelta(minutes=1),
    )
    is None
  )


def test_cancellation_handles_queued_and_running_jobs(
  database: Database,
) -> None:
  queued = enqueue_job(database, "queued", {}, now=NOW)
  canceled = request_cancellation(
    database,
    queued.id,
    now=NOW + timedelta(seconds=1),
  )
  assert canceled is not None
  assert canceled.state == "canceled"
  assert canceled.cancel_requested_at is not None
  assert canceled.completed_at is not None

  running = enqueue_job(
    database,
    "running",
    {},
    now=NOW + timedelta(seconds=2),
  )
  claimed = claim_job(
    database,
    "worker",
    lease_seconds=30,
    now=NOW + timedelta(seconds=3),
  )
  assert claimed is not None
  assert claimed.id == running.id
  requested = request_cancellation(
    database,
    running.id,
    now=NOW + timedelta(seconds=4),
  )
  assert requested is not None
  assert requested.state == "running"
  assert cancellation_requested(database, running.id)

  terminal = complete_job(
    database,
    running.id,
    "worker",
    {"ignored": True},
    now=NOW + timedelta(seconds=5),
  )
  assert terminal is not None
  assert terminal.state == "canceled"
  assert terminal.result == {}
  assert (
    claim_job(
      database,
      "another-worker",
      now=NOW + timedelta(minutes=1),
    )
    is None
  )


def test_worker_registry_retries_then_completes_with_context(
  database: Database,
) -> None:
  queued = enqueue_job(
    database,
    "flaky",
    {"answer": 42},
    max_attempts=3,
  )
  stop_event = threading.Event()
  calls: list[int] = []

  def handler(
    context: JobContext,
    payload: dict[str, object],
  ) -> dict[str, object]:
    assert context.job_id == queued.id
    assert context.job_type == "flaky"
    assert payload == {"answer": 42}
    calls.append(context.job.attempts)
    if len(calls) < 3:
      raise RuntimeError("try again")
    assert not context.cancel_path.exists()
    context.progress(0.75)
    stop_event.set()
    return {"attempt": len(calls)}

  run_worker(
    database,
    {"flaky": handler},
    stop_event,
    worker_id="test-worker",
    lease_seconds=1,
    heartbeat_interval=0.05,
    poll_interval=0,
    retry_backoff_seconds=0,
    max_retry_backoff_seconds=0,
  )

  completed = get_job(database, queued.id)
  assert completed is not None
  assert calls == [1, 2, 3]
  assert completed.state == "succeeded"
  assert completed.attempts == 3
  assert completed.progress == 1
  assert completed.result == {"attempt": 3}


def test_worker_honors_handler_retryable_marker(
  database: Database,
) -> None:
  class ContractFailure(RuntimeError):
    retryable = False

  queued = enqueue_job(database, "invalid", {}, max_attempts=3)
  stop_event = threading.Event()

  def handler(
    context: JobContext,
    payload: dict[str, object],
  ) -> dict[str, object]:
    stop_event.set()
    raise ContractFailure("invalid artifact contract")

  run_worker(
    database,
    {"invalid": handler},
    stop_event,
    worker_id="test-worker",
    lease_seconds=1,
    heartbeat_interval=0.05,
    poll_interval=0,
    retry_backoff_seconds=0,
    max_retry_backoff_seconds=0,
  )

  failed = get_job(database, queued.id)
  assert failed is not None
  assert failed.state == "failed"
  assert failed.attempts == 1
  assert failed.error == "ContractFailure: invalid artifact contract"


def test_worker_monitor_turns_database_cancellation_into_process_signals(
  database: Database,
) -> None:
  queued = enqueue_job(database, "cancelable", {})
  stop_event = threading.Event()
  handler_started = threading.Event()
  saw_cancel_file = threading.Event()

  def handler(
    context: JobContext,
    payload: dict[str, object],
  ) -> dict[str, object]:
    assert payload == {}
    handler_started.set()
    assert context.cancel_event.wait(2)
    if context.cancel_path.exists():
      saw_cancel_file.set()
    context.raise_if_cancelled()
    raise AssertionError("raise_if_cancelled should stop the handler")

  worker = threading.Thread(
    target=run_worker,
    args=(database, {"cancelable": handler}, stop_event),
    kwargs={
      "worker_id": "cancel-worker",
      "lease_seconds": 1,
      "heartbeat_interval": 0.02,
      "poll_interval": 0.01,
    },
  )
  worker.start()
  assert handler_started.wait(2)
  requested = request_cancellation(database, queued.id)
  assert requested is not None
  stop_event.set()
  worker.join(3)

  assert not worker.is_alive()
  assert saw_cancel_file.is_set()
  canceled = get_job(database, queued.id)
  assert canceled is not None
  assert canceled.state == "canceled"


def test_worker_honors_structured_nonretryable_exception(
  database: Database,
) -> None:
  queued = enqueue_job(database, "permanent", {}, max_attempts=3)
  stop_event = threading.Event()

  class StructuredError(RuntimeError):
    retryable = False

  def handler(
    _context: JobContext,
    _payload: dict[str, object],
  ) -> None:
    stop_event.set()
    raise StructuredError("invalid artifact contract")

  run_worker(
    database,
    {"permanent": handler},
    stop_event,
    worker_id="test-worker",
    lease_seconds=1,
    heartbeat_interval=0.05,
    poll_interval=0,
  )

  failed = get_job(database, queued.id)
  assert failed is not None
  assert failed.state == "failed"
  assert failed.attempts == 1
  assert failed.error == "StructuredError: invalid artifact contract"
