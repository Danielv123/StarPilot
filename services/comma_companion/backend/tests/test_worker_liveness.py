from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import comma_companion.jobs as jobs_module
from comma_companion.db import Database, SCHEMA_VERSION, isoformat, utc_now
from comma_companion.jobs import (
  WORKER_STALE_AFTER_SECONDS,
  JobContext,
  claim_job,
  enqueue_job,
  run_worker,
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
  result = Database(tmp_path / "companion.sqlite3")
  result.initialize()
  return result


def test_idle_worker_poll_records_durable_heartbeat(
  database: Database,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  stop_event = threading.Event()
  claim_called = threading.Event()

  def no_job(*_args: object, **_kwargs: object) -> None:
    claim_called.set()
    stop_event.set()
    return None

  monkeypatch.setattr(jobs_module, "claim_job", no_job)
  run_worker(
    database,
    {},
    stop_event,
    worker_id="idle-worker",
    poll_interval=0,
  )

  assert claim_called.is_set()
  row = database.query_one(
    "SELECT worker_id, last_seen_at FROM worker_heartbeats",
  )
  assert row is not None
  assert row["worker_id"] == "idle-worker"
  assert row["last_seen_at"] is not None


def test_active_job_heartbeat_refreshes_worker_liveness(
  database: Database,
  tmp_path: Path,
) -> None:
  queued = enqueue_job(database, "active", {})
  claimed = claim_job(database, "active-worker", lease_seconds=30)
  assert claimed is not None
  assert claimed.id == queued.id
  assert database.query_one(
    "SELECT 1 FROM worker_heartbeats WHERE worker_id = 'active-worker'",
  ) is None

  context = JobContext(
    database,
    claimed,
    "active-worker",
    lease_seconds=30,
    cancel_path=tmp_path / "cancel",
  )
  context.heartbeat()

  row = database.query_one(
    """
    SELECT worker_id, last_seen_at
    FROM worker_heartbeats
    WHERE worker_id = 'active-worker'
    """,
  )
  assert row is not None
  assert row["worker_id"] == "active-worker"
  assert row["last_seen_at"] is not None


def test_dashboard_worker_liveness_has_exact_absent_stale_online_states(
  admin_client: TestClient,
) -> None:
  database = admin_client.app.state.database

  absent = admin_client.get("/api/v1/dashboard")
  assert absent.status_code == 200
  assert absent.json()["worker"] == {
    "last_seen": None,
    "stale": False,
    "online": False,
  }

  stale_seen = utc_now() - timedelta(
    seconds=WORKER_STALE_AFTER_SECONDS + 5,
  )
  stale_seen_text = isoformat(stale_seen)
  database.execute(
    """
    INSERT INTO worker_heartbeats(worker_id, last_seen_at)
    VALUES ('stale-worker', ?)
    """,
    (stale_seen_text,),
  )
  stale = admin_client.get("/api/v1/dashboard")
  assert stale.status_code == 200
  assert stale.json()["worker"] == {
    "last_seen": stale_seen_text,
    "stale": True,
    "online": False,
  }

  fresh_seen_text = isoformat()
  database.execute(
    """
    INSERT INTO worker_heartbeats(worker_id, last_seen_at)
    VALUES ('fresh-worker', ?)
    """,
    (fresh_seen_text,),
  )
  online = admin_client.get("/api/v1/dashboard")
  assert online.status_code == 200
  assert online.json()["worker"] == {
    "last_seen": fresh_seen_text,
    "stale": False,
    "online": True,
  }


def test_v5_database_migrates_worker_heartbeat_table(
  tmp_path: Path,
) -> None:
  path = tmp_path / "companion.sqlite3"
  connection = sqlite3.connect(path)
  connection.executescript(
    """
    CREATE TABLE schema_meta(version INTEGER NOT NULL);
    INSERT INTO schema_meta(version) VALUES (5);
    """,
  )
  connection.close()

  database = Database(path)
  database.initialize()

  with database.connection() as migrated:
    version = migrated.execute(
      "SELECT version FROM schema_meta",
    ).fetchone()[0]
    columns = {
      row["name"]
      for row in migrated.execute(
        "PRAGMA table_info(worker_heartbeats)",
      ).fetchall()
    }
    indexes = {
      row["name"]
      for row in migrated.execute(
        "PRAGMA index_list(worker_heartbeats)",
      ).fetchall()
    }

  assert version == SCHEMA_VERSION
  assert columns == {"worker_id", "last_seen_at"}
  assert "idx_worker_heartbeats_seen" in indexes
