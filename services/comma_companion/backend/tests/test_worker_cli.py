from pathlib import Path

import pytest

from comma_companion.worker import (
  SingletonWorkerLock,
  WorkerAlreadyRunning,
  _command,
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
