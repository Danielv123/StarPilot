import threading
from pathlib import Path

import pytest

import comma_companion.worker as worker_module
from comma_companion.worker import (
  SingletonWorkerLock,
  WorkerAlreadyRunning,
  _command,
  _run_worker_pool,
)


def test_command_splits_configured_adapter_invocation() -> None:
  assert _command(
    'python -m adapter --model "/app/model file.pt"',
    "TEST_COMMAND",
  ) == (
    "python",
    "-m",
    "adapter",
    "--model",
    "/app/model file.pt",
  )
  with pytest.raises(ValueError, match="must not be empty"):
    _command("   ", "TEST_COMMAND")
  with pytest.raises(ValueError, match="not a valid command"):
    _command('"unterminated', "TEST_COMMAND")


def test_singleton_worker_lock_rejects_concurrent_holder(
  tmp_path: Path,
) -> None:
  lock_path = tmp_path / "state" / "worker.lock"
  with SingletonWorkerLock(lock_path):
    with pytest.raises(WorkerAlreadyRunning):
      with SingletonWorkerLock(lock_path):
        pass
  with SingletonWorkerLock(lock_path):
    assert lock_path.is_file()


def test_worker_pool_runs_configured_jobs_in_parallel(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  stop_event = threading.Event()
  rendezvous = threading.Barrier(2)
  worker_threads: list[str] = []

  def fake_run_worker(*_args, **_kwargs) -> None:
    worker_threads.append(threading.current_thread().name)
    rendezvous.wait(timeout=2)
    stop_event.set()

  monkeypatch.setattr(worker_module, "run_worker", fake_run_worker)
  _run_worker_pool(
    object(),  # type: ignore[arg-type]
    {},
    stop_event,
    concurrency=2,
    lease_seconds=120,
    poll_interval=1,
  )

  assert sorted(worker_threads) == [
    "archive-worker-1",
    "archive-worker-2",
  ]
