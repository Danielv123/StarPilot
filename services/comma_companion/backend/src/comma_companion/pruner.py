from __future__ import annotations

import logging
import os
import signal
import threading
from types import FrameType

from .config import Settings
from .db import Database, ensure_storage_directories
from .integrations import IntegrationHandlers
from .jobs import run_worker
from .worker import SingletonWorkerLock, WorkerAlreadyRunning


LOGGER = logging.getLogger("comma_companion.pruner")


def _install_signal_handlers(stop_event: threading.Event) -> None:
  def stop(_signal_number: int, _frame: FrameType | None) -> None:
    stop_event.set()

  signal.signal(signal.SIGINT, stop)
  signal.signal(signal.SIGTERM, stop)


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
    retain_raw_video=settings.retain_raw_video,
  )
  lock_path = settings.session_dir / "pruner.lock"
  try:
    with SingletonWorkerLock(lock_path):
      recovered = integrations.recover_pending_raw_video_prunes()
      if recovered:
        LOGGER.info("Recovered %d pending raw-video prune operations", len(recovered))
      LOGGER.info("Raw-video pruner started")
      run_worker(
        database,
        {"prune_raw_video": integrations.prune_raw_video},
        stop_event,
        lease_seconds=settings.job_lease_seconds,
        poll_interval=settings.job_poll_seconds,
        record_liveness=False,
      )
  except WorkerAlreadyRunning as exc:
    raise SystemExit(str(exc)) from exc
  finally:
    database.checkpoint()
  LOGGER.info("Raw-video pruner stopped")


if __name__ == "__main__":
  main()
