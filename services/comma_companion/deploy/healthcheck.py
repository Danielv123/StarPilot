#!/usr/bin/env python3
from __future__ import annotations

import errno
import fcntl
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path
from uuid import uuid4


def _write_probe(path: Path) -> tuple[bool, str | None]:
  probe = path / f".health-write-probe-{uuid4().hex}"
  try:
    with probe.open("xb") as stream:
      stream.write(b"health\n")
      stream.flush()
      os.fsync(stream.fileno())
    probe.unlink()
    return True, None
  except OSError as error:
    try:
      probe.unlink()
    except OSError:
      pass
    return False, f"{type(error).__name__}: {error}"


def _read_only_probe(path: Path) -> tuple[bool, str | None]:
  probe = path / f".health-read-only-probe-{uuid4().hex}"
  try:
    descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
  except OSError as error:
    if error.errno in {errno.EACCES, errno.EROFS}:
      return True, None
    return False, f"{type(error).__name__}: {error}"
  os.close(descriptor)
  try:
    probe.unlink()
  except OSError as error:
    return False, f"writable and cleanup failed: {type(error).__name__}: {error}"
  return False, "write unexpectedly succeeded"


def _child_process_is_running(command_name: bytes) -> bool:
  try:
    child_pids = Path("/proc/1/task/1/children").read_text(
      encoding="utf-8",
    ).split()
  except OSError:
    return False
  for pid in child_pids:
    try:
      command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
      continue
    if command_name in command:
      return True
  return False


def check_api() -> int:
  port = int(os.environ.get("PORT", "8000"))
  request = urllib.request.Request(
    f"http://127.0.0.1:{port}/api/v1/health",
    headers={"Accept": "application/json"},
  )
  try:
    with urllib.request.urlopen(request, timeout=5) as response:
      payload = json.load(response)
  except Exception as error:
    print(f"health request failed: {type(error).__name__}: {error}", file=sys.stderr)
    return 1

  if (
    payload.get("status") != "ok"
    or payload.get("service") != "comma-companion-api"
  ):
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), file=sys.stderr)
    return 1
  return 0


def check_worker() -> int:
  database_path = Path(
    os.environ.get(
      "COMPANION_DATABASE_PATH",
      "/var/lib/comma-companion/database/companion.sqlite3",
    ),
  )
  session_dir = Path(
    os.environ.get(
      "COMPANION_SESSION_DIR",
      "/var/lib/comma-companion/database/worker-runtime",
    ),
  )
  archive_root = Path(
    os.environ.get(
      "COMPANION_ARCHIVE_ROOT",
      "/archive/comma-companion",
    ),
  )
  sentinel = Path(
    os.environ.get(
      "COMPANION_ARCHIVE_SENTINEL",
      str(archive_root / ".comma-companion-archive"),
    ),
  )

  if not _child_process_is_running(b"comma-companion-worker"):
    print("durable worker process is not running under init", file=sys.stderr)
    return 1

  if not sentinel.is_file():
    print("worker archive sentinel is missing", file=sys.stderr)
    return 1
  try:
    for relative in (Path("."), Path("objects"), Path("uploads")):
      path = archive_root / relative
      read_only, error = _read_only_probe(path) if path.is_dir() else (
        False,
        "directory is missing",
      )
      if not read_only:
        print(f"worker raw path is not read-only: {path}: {error}", file=sys.stderr)
        return 1
    for relative in ("derived", "telemetry", "thumbnails"):
      path = archive_root / relative
      writable, error = _write_probe(path) if path.is_dir() else (
        False,
        "directory is missing",
      )
      if not writable:
        print(f"worker output path is not writable: {path}: {error}", file=sys.stderr)
        return 1
  except OSError as error:
    print(f"worker archive check failed: {type(error).__name__}: {error}", file=sys.stderr)
    return 1

  lock_path = session_dir / "worker.lock"
  try:
    with lock_path.open("r+", encoding="utf-8") as lock:
      try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
      except BlockingIOError:
        pass
      else:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        print("durable worker does not hold its singleton lock", file=sys.stderr)
        return 1
  except OSError as error:
    print(f"worker lock check failed: {type(error).__name__}: {error}", file=sys.stderr)
    return 1

  if not database_path.is_file():
    print(f"worker database is missing: {database_path}", file=sys.stderr)
    return 1
  try:
    with sqlite3.connect(
      f"file:{database_path.as_posix()}?mode=ro",
      uri=True,
      timeout=2,
    ) as connection:
      row = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
  except sqlite3.Error as error:
    print(f"worker database check failed: {type(error).__name__}: {error}", file=sys.stderr)
    return 1
  if row is None or not isinstance(row[0], int):
    print("worker database schema metadata is missing", file=sys.stderr)
    return 1
  return 0


def check_pruner() -> int:
  database_path = Path(
    os.environ.get(
      "COMPANION_DATABASE_PATH",
      "/var/lib/comma-companion/database/companion.sqlite3",
    ),
  )
  session_dir = Path(
    os.environ.get(
      "COMPANION_SESSION_DIR",
      "/var/lib/comma-companion/database/pruner-runtime",
    ),
  )
  archive_root = Path(
    os.environ.get(
      "COMPANION_ARCHIVE_ROOT",
      "/archive/comma-companion",
    ),
  )
  sentinel = Path(
    os.environ.get(
      "COMPANION_ARCHIVE_SENTINEL",
      str(archive_root / ".comma-companion-archive"),
    ),
  )

  if not _child_process_is_running(b"comma-companion-pruner"):
    print("raw-video pruner process is not running under init", file=sys.stderr)
    return 1
  if not sentinel.is_file():
    print("pruner archive sentinel is missing", file=sys.stderr)
    return 1
  try:
    for relative in (Path("."), Path("objects"), Path("uploads"), Path("derived")):
      path = archive_root / relative
      read_only, error = _read_only_probe(path) if path.is_dir() else (
        False,
        "directory is missing",
      )
      if not read_only:
        print(f"pruner path is not read-only: {path}: {error}", file=sys.stderr)
        return 1
    object_store = archive_root / "objects" / "sha256"
    writable, error = _write_probe(object_store) if object_store.is_dir() else (
      False,
      "directory is missing",
    )
    if not writable:
      print(f"pruner object store is not writable: {object_store}: {error}", file=sys.stderr)
      return 1
  except OSError as error:
    print(f"pruner archive check failed: {type(error).__name__}: {error}", file=sys.stderr)
    return 1

  lock_path = session_dir / "pruner.lock"
  try:
    with lock_path.open("r+", encoding="utf-8") as lock:
      try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
      except BlockingIOError:
        pass
      else:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        print("raw-video pruner does not hold its singleton lock", file=sys.stderr)
        return 1
  except OSError as error:
    print(f"pruner lock check failed: {type(error).__name__}: {error}", file=sys.stderr)
    return 1

  if not database_path.is_file():
    print(f"pruner database is missing: {database_path}", file=sys.stderr)
    return 1
  try:
    with sqlite3.connect(
      f"file:{database_path.as_posix()}?mode=ro",
      uri=True,
      timeout=2,
    ) as connection:
      row = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
  except sqlite3.Error as error:
    print(f"pruner database check failed: {type(error).__name__}: {error}", file=sys.stderr)
    return 1
  if row is None or not isinstance(row[0], int):
    print("pruner database schema metadata is missing", file=sys.stderr)
    return 1
  return 0


def main() -> int:
  arguments = sys.argv[1:]
  if not arguments or arguments == ["api"]:
    return check_api()
  if arguments == ["worker"]:
    return check_worker()
  if arguments == ["pruner"]:
    return check_pruner()
  print("usage: healthcheck.py [api|worker|pruner]", file=sys.stderr)
  return 2


if __name__ == "__main__":
  raise SystemExit(main())
