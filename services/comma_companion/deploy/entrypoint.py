#!/usr/bin/env python3
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from uuid import uuid4


SECRET_VARIABLES = (
  "COMPANION_SESSION_SECRET",
  "COMPANION_ADMIN_PASSWORD_HASH",
  "COMPANION_DEVICE_TOKENS_JSON",
  "COMPANION_IMPORT_TOKEN",
)


def fail(message: str) -> None:
  print(f"comma-companion entrypoint: {message}", file=sys.stderr, flush=True)
  raise SystemExit(1)


def load_file_variables() -> None:
  for variable in SECRET_VARIABLES:
    file_variable = f"{variable}_FILE"
    path_value = os.environ.get(file_variable)
    if not path_value:
      continue
    path = Path(path_value)
    try:
      value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as error:
      fail(f"cannot read {file_variable}: {error}")
    if not value:
      fail(f"{file_variable} points to an empty file")
    os.environ[variable] = value
    os.environ.pop(file_variable, None)


def verify_writable_directory(label: str, path: Path) -> None:
  if not path.is_dir():
    fail(f"{label} directory does not exist: {path}")
  probe = path / f".entrypoint-write-probe-{uuid4().hex}"
  try:
    descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    probe.unlink()
  except OSError as error:
    try:
      probe.unlink()
    except OSError:
      pass
    fail(f"{label} directory is not writable: {path}: {error}")


def verify_read_only_directory(label: str, path: Path) -> None:
  if not path.is_dir():
    fail(f"{label} directory does not exist: {path}")
  probe = path / f".entrypoint-read-only-probe-{uuid4().hex}"
  try:
    descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
  except OSError as error:
    if error.errno in {errno.EACCES, errno.EROFS}:
      return
    fail(f"{label} read-only check failed: {path}: {error}")
  else:
    os.close(descriptor)
    try:
      probe.unlink()
    except OSError as error:
      fail(f"{label} was writable and probe cleanup failed: {probe}: {error}")
    fail(f"{label} must be mounted read-only: {path}")


def validate_runtime(*, role: str) -> None:
  if os.environ.get("COMPANION_ADMIN_PASSWORD"):
    fail("COMPANION_ADMIN_PASSWORD is disabled; configure an Argon2id hash file")

  if role == "api":
    required = (
      "COMPANION_SESSION_SECRET",
      "COMPANION_ADMIN_PASSWORD_HASH",
      "COMPANION_DEVICE_TOKENS_JSON",
      "COMPANION_IMPORT_TOKEN",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
      fail(f"missing required secret values: {', '.join(missing)}")

  for variable in ("COMPANION_DATABASE_PATH", "COMPANION_ARCHIVE_ROOT"):
    if not os.environ.get(variable):
      fail(f"{variable} must be configured")

  state_root = Path(os.environ["COMPANION_DATABASE_PATH"]).parent
  archive_root = Path(os.environ["COMPANION_ARCHIVE_ROOT"])
  verify_writable_directory("database state", state_root)
  if role == "api":
    verify_writable_directory("API archive", archive_root)
  elif role == "worker":
    for relative in (Path("."), Path("objects"), Path("uploads")):
      verify_read_only_directory(
        f"worker archive {relative}",
        archive_root / relative,
      )
    for relative in ("derived", "telemetry", "thumbnails"):
      verify_writable_directory(
        f"worker archive {relative}",
        archive_root / relative,
      )
  else:
    fail(f"unsupported runtime role: {role}")

  sentinel_value = os.environ.get("COMPANION_ARCHIVE_SENTINEL")
  if sentinel_value and not Path(sentinel_value).is_file():
    fail(f"archive sentinel is missing; refusing local fallback: {sentinel_value}")


def reject_worker_secrets() -> None:
  leaked = [
    name
    for variable in SECRET_VARIABLES
    for name in (variable, f"{variable}_FILE")
    if os.environ.get(name)
  ]
  if leaked:
    fail(
      "worker containers must not receive authentication secrets: "
      + ", ".join(leaked),
    )


def main() -> int:
  arguments = sys.argv[1:] or ["api"]
  if arguments == ["api"]:
    load_file_variables()
    validate_runtime(role="api")
    os.execvp("comma-companion-api", ["comma-companion-api"])
  if arguments == ["worker"]:
    reject_worker_secrets()
    validate_runtime(role="worker")
    os.execvp("comma-companion-worker", ["comma-companion-worker"])
  os.execvp(arguments[0], arguments)


if __name__ == "__main__":
  raise SystemExit(main())
