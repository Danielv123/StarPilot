from __future__ import annotations

import logging
import os
import shlex
import signal
import threading
from pathlib import Path
from types import FrameType

from .config import Settings
from .db import Database, ensure_storage_directories
from .integrations import IntegrationError, IntegrationHandlers
from .jobs import run_worker


LOGGER = logging.getLogger("comma_companion.worker")


class WorkerAlreadyRunning(RuntimeError):
  pass


class SingletonWorkerLock:
  def __init__(self, path: Path):
    self.path = path
    self._descriptor: int | None = None

  def __enter__(self) -> SingletonWorkerLock:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
      self.path,
      os.O_CREAT | os.O_RDWR,
      0o600,
    )
    try:
      if os.name == "nt":
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
          os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
      else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
      os.close(descriptor)
      raise WorkerAlreadyRunning(
        f"another worker holds {self.path}",
      ) from exc
    self._descriptor = descriptor
    return self

  def __exit__(self, *_args: object) -> None:
    descriptor = self._descriptor
    self._descriptor = None
    if descriptor is None:
      return
    try:
      if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
      else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
      os.close(descriptor)


def _command(value: str, setting_name: str) -> tuple[str, ...]:
  try:
    parts = tuple(shlex.split(value, posix=True))
  except ValueError as exc:
    raise ValueError(f"{setting_name} is not a valid command") from exc
  if not parts:
    raise ValueError(f"{setting_name} must not be empty")
  return parts


def _install_signal_handlers(stop_event: threading.Event) -> None:
  def stop(_signal_number: int, _frame: FrameType | None) -> None:
    stop_event.set()

  signal.signal(signal.SIGINT, stop)
  signal.signal(signal.SIGTERM, stop)


def _refresh_model_registry(
  database: Database,
  integrations: IntegrationHandlers,
) -> None:
  try:
    result = integrations.refresh_model_registry()
  except IntegrationError:
    database.execute("UPDATE model_registry SET enabled = 0")
    LOGGER.exception(
      "Dynamics registry refresh failed; simulation is disabled while archive jobs continue",
    )
    return
  if result["status"] == "ready":
    LOGGER.info(
      "Enabled dynamics model %s in %s mode",
      result["model_hash"],
      result["mode"],
    )
  else:
    LOGGER.warning(
      "Dynamics model %s is unavailable: %s",
      result["model_hash"],
      result["status"],
    )


def main() -> None:
  logging.basicConfig(
    level=os.getenv("COMPANION_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
  )
  settings = Settings.from_env()
  ensure_storage_directories(settings.archive_root, settings.session_dir)
  database = Database(settings.database_path)
  database.initialize()
  stop_event = threading.Event()
  _install_signal_handlers(stop_event)
  integrations = IntegrationHandlers(
    database,
    settings.archive_root,
    media_command=_command(
      settings.media_worker_command,
      "COMPANION_MEDIA_WORKER_COMMAND",
    ),
    rlog_command=_command(
      settings.rlog_adapter_command,
      "COMPANION_RLOG_ADAPTER_COMMAND",
    ),
    dynamics_command=_command(
      settings.dynamics_adapter_command,
      "COMPANION_DYNAMICS_ADAPTER_COMMAND",
    ),
    media_timeout_seconds=settings.media_timeout_seconds,
    max_media_source_bytes=settings.max_artifact_bytes,
    transcode_crf=settings.transcode_crf,
    transcode_preset=settings.transcode_preset,
    retain_raw_video=settings.retain_raw_video,
  )
  lock_path = settings.session_dir / "worker.lock"
  try:
    with SingletonWorkerLock(lock_path):
      _refresh_model_registry(database, integrations)
      LOGGER.info("Worker started")
      run_worker(
        database,
        integrations.handlers,
        stop_event,
        lease_seconds=settings.job_lease_seconds,
        poll_interval=settings.job_poll_seconds,
      )
  except WorkerAlreadyRunning as exc:
    raise SystemExit(str(exc)) from exc
  finally:
    database.checkpoint()
  LOGGER.info("Worker stopped")


if __name__ == "__main__":
  main()
