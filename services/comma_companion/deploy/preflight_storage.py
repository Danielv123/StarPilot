#!/usr/bin/env python3
from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import stat
import sys
from pathlib import Path
from uuid import uuid4


def _unescape_mount_field(value: str) -> str:
  return value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def _filesystem_type(path: Path) -> str:
  try:
    lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
  except OSError:
    return "unknown"
  resolved = str(path)
  best_match: tuple[int, str] | None = None
  for line in lines:
    fields = line.split()
    try:
      separator = fields.index("-")
      mountpoint = _unescape_mount_field(fields[4])
      filesystem = fields[separator + 1]
    except (IndexError, ValueError):
      continue
    if resolved == mountpoint or resolved.startswith(mountpoint.rstrip("/") + "/"):
      candidate = (len(mountpoint), filesystem)
      if best_match is None or candidate[0] > best_match[0]:
        best_match = candidate
  return best_match[1] if best_match else "unknown"


def _write_probe(path: Path) -> tuple[bool, str | None]:
  probe = path / f".storage-layout-probe-{uuid4().hex}"
  try:
    with probe.open("xb") as stream:
      stream.write(b"comma-companion-layout-probe\n")
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


def _hardlink_paths_alias(source: Path, link: Path) -> bool:
  try:
    if link.samefile(source):
      return True
  except OSError:
    pass

  source_stat = source.stat(follow_symlinks=False)
  link_stat = link.stat(follow_symlinks=False)
  if (
    not stat.S_ISREG(source_stat.st_mode)
    or not stat.S_ISREG(link_stat.st_mode)
    or source_stat.st_dev != link_stat.st_dev
    or source_stat.st_nlink < 2
    or link_stat.st_nlink < 2
    or source_stat.st_size != link_stat.st_size
    or source.read_bytes() != link.read_bytes()
  ):
    return False

  # Some CIFS mounts expose a different synthetic inode for each pathname even
  # when the SMB server created a real hardlink. Prove aliasing by extending the
  # disposable source and observing the same bytes through the target.
  marker = f"comma-companion-hardlink-alias-{uuid4().hex}\n".encode()
  with source.open("ab") as stream:
    stream.write(marker)
    stream.flush()
    os.fsync(stream.fileno())
  return link.read_bytes().endswith(marker)


def _hardlink_probe(path: Path) -> tuple[bool, str | None]:
  source = path / f".storage-hardlink-source-{uuid4().hex}"
  link = path / f".storage-hardlink-target-{uuid4().hex}"
  try:
    with source.open("xb") as stream:
      stream.write(b"comma-companion-hardlink-probe\n")
      stream.flush()
      os.fsync(stream.fileno())
    os.link(source, link)
    if not _hardlink_paths_alias(source, link):
      return False, "hardlink target does not alias its source"
    return True, None
  except OSError as error:
    return False, f"{type(error).__name__}: {error}"
  finally:
    for candidate in (link, source):
      try:
        candidate.unlink()
      except FileNotFoundError:
        pass


def _read_only_probe(path: Path) -> tuple[bool, str | None]:
  probe = path / f".storage-read-only-probe-{uuid4().hex}"
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


def _sqlite_lock_probe(path: Path) -> tuple[bool, str | None]:
  database = path / f".sqlite-lock-probe-{uuid4().hex}.sqlite3"
  first: sqlite3.Connection | None = None
  second: sqlite3.Connection | None = None
  try:
    first = sqlite3.connect(database, timeout=1)
    second = sqlite3.connect(database, timeout=0)
    first.execute("CREATE TABLE probe(value INTEGER)")
    first.commit()
    first.execute("BEGIN EXCLUSIVE")
    first.execute("INSERT INTO probe VALUES (1)")
    try:
      second.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
      if "locked" not in str(error).lower():
        return False, f"unexpected lock error: {error}"
    else:
      second.rollback()
      return False, "second writer acquired a lock during an exclusive transaction"
    first.rollback()
    return True, None
  except (OSError, sqlite3.Error) as error:
    return False, f"{type(error).__name__}: {error}"
  finally:
    if second is not None:
      second.close()
    if first is not None:
      first.close()
    for suffix in ("", "-journal", "-wal", "-shm"):
      try:
        Path(f"{database}{suffix}").unlink()
      except FileNotFoundError:
        pass


def worker_layout_preflight() -> int:
  root = Path(
    os.environ.get(
      "COMPANION_ARCHIVE_ROOT",
      "/archive/comma-companion",
    ),
  ).resolve()
  database_root = (
    Path(
      os.environ.get(
        "COMPANION_DATABASE_PATH",
        "/var/lib/comma-companion/database/companion.sqlite3",
      ),
    )
    .resolve()
    .parent
  )
  sentinel = Path(
    os.environ.get(
      "COMPANION_ARCHIVE_SENTINEL",
      str(root / ".comma-companion-archive"),
    ),
  )
  result: dict[str, object] = {
    "root": str(root),
    "filesystem": _filesystem_type(root),
    "sentinel": sentinel.is_file(),
    "raw_read_only": {},
    "outputs_writable": {},
    "outputs_hardlink": {},
    "database_writable": False,
    "database_filesystem": _filesystem_type(database_root),
    "database_locking": False,
    "session_mount_absent": True,
  }
  require_cifs = os.environ.get(
    "COMPANION_PREFLIGHT_REQUIRE_CIFS",
    "1",
  ).strip().lower() not in {"0", "false", "no", "off"}
  result["cifs_required"] = require_cifs
  success = bool(result["sentinel"])
  if require_cifs:
    success = success and result["filesystem"] in {"cifs", "smb3"}
  for relative in (Path("."), Path("objects"), Path("uploads")):
    path = root / relative
    read_only, error = _read_only_probe(path)
    result["raw_read_only"][str(relative)] = {
      "ok": read_only,
      "error": error,
    }
    success = success and read_only
  for relative in ("derived", "telemetry", "thumbnails"):
    writable, error = _write_probe(root / relative)
    result["outputs_writable"][relative] = {
      "ok": writable,
      "error": error,
    }
    hardlink, hardlink_error = _hardlink_probe(root / relative)
    result["outputs_hardlink"][relative] = {
      "ok": hardlink,
      "error": hardlink_error,
    }
    success = success and writable and hardlink
  database_writable, database_error = _write_probe(database_root)
  result["database_writable"] = database_writable
  result["database_error"] = database_error
  success = success and database_writable
  database_locking, locking_error = _sqlite_lock_probe(database_root)
  result["database_locking"] = database_locking
  result["database_locking_error"] = locking_error
  success = success and database_locking
  remote_filesystems = {
    "9p",
    "ceph",
    "cifs",
    "fuse.sshfs",
    "glusterfs",
    "lustre",
    "nfs",
    "nfs4",
    "smb3",
    "sshfs",
    "virtiofs",
  }
  database_is_local = result["database_filesystem"] not in remote_filesystems and result["database_filesystem"] != "unknown"
  result["database_is_local"] = database_is_local
  success = success and database_is_local

  session_path = Path("/var/lib/comma-companion/sessions")
  try:
    result["session_mount_absent"] = not any(session_path.iterdir())
  except FileNotFoundError:
    result["session_mount_absent"] = True
  except OSError as error:
    result["session_error"] = f"{type(error).__name__}: {error}"
    result["session_mount_absent"] = False
  success = success and bool(result["session_mount_absent"])
  print(json.dumps(result, indent=2, sort_keys=True))
  return 0 if success else 1


def _storage_ok(result: dict[str, object], *, require_cifs: bool) -> bool:
  success = bool(
    result["atomic_replace"]
    and result["file_fsync"]
    and result["hardlink"]
    and result["state_writable"]
    and result["state_is_local"]
    and result["sqlite_locking"]
    and "fatal_error" not in result
    and "cleanup_error" not in result
  )
  if require_cifs:
    success = success and bool(result["archive_is_cifs"]) and not bool(result.get("state_and_archive_share_device", True))
  return success


def main() -> int:
  if sys.argv[1:] == ["--worker-layout"]:
    return worker_layout_preflight()
  if sys.argv[1:]:
    print("usage: preflight_storage.py [--worker-layout]", file=sys.stderr)
    return 2

  root = Path(
    os.environ.get(
      "COMPANION_ARCHIVE_ROOT",
      "/archive/comma-companion",
    ),
  ).resolve()
  sentinel = Path(
    os.environ.get(
      "COMPANION_ARCHIVE_SENTINEL",
      str(root / ".comma-companion-archive"),
    ),
  )
  if not root.is_dir() or not sentinel.is_file():
    print("archive root or sentinel is missing", file=sys.stderr)
    return 1

  probe = root / f".storage-preflight-{uuid4().hex}"
  source = probe / "source"
  published = probe / "published"
  hardlink = probe / "hardlink"
  symlink = probe / "symlink"
  filesystem = _filesystem_type(root)
  require_cifs = os.environ.get(
    "COMPANION_PREFLIGHT_REQUIRE_CIFS",
    "1",
  ).strip().lower() not in {"0", "false", "no", "off"}
  state_root = (
    Path(
      os.environ.get(
        "COMPANION_DATABASE_PATH",
        "/var/lib/comma-companion/database/companion.sqlite3",
      ),
    )
    .resolve()
    .parent
  )
  result: dict[str, object] = {
    "root": str(root),
    "filesystem": filesystem,
    "archive_is_cifs": filesystem in {"cifs", "smb3"},
    "cifs_required": require_cifs,
    "state_root": str(state_root),
    "state_filesystem": _filesystem_type(state_root),
    "state_writable": False,
    "sqlite_locking": False,
    "atomic_replace": False,
    "file_fsync": False,
    "directory_fsync": False,
    "hardlink": False,
    "symlink": False,
  }
  remote_filesystems = {
    "9p",
    "ceph",
    "cifs",
    "fuse.sshfs",
    "glusterfs",
    "lustre",
    "nfs",
    "nfs4",
    "smb3",
    "sshfs",
    "virtiofs",
  }
  result["state_is_local"] = result["state_filesystem"] not in remote_filesystems and result["state_filesystem"] != "unknown"

  try:
    state_writable, state_write_error = _write_probe(state_root)
    result["state_writable"] = state_writable
    result["state_write_error"] = state_write_error
    sqlite_locking, sqlite_locking_error = _sqlite_lock_probe(state_root)
    result["sqlite_locking"] = sqlite_locking
    result["sqlite_locking_error"] = sqlite_locking_error
    probe.mkdir(mode=0o700)
    with source.open("wb") as stream:
      stream.write(b"comma-companion-storage-preflight\n")
      stream.flush()
      os.fsync(stream.fileno())
      result["file_fsync"] = True
    os.replace(source, published)
    result["atomic_replace"] = published.read_bytes() == b"comma-companion-storage-preflight\n"

    try:
      directory_fd = os.open(probe, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
      try:
        os.fsync(directory_fd)
        result["directory_fsync"] = True
      finally:
        os.close(directory_fd)
    except OSError as error:
      result["directory_fsync_error"] = f"{type(error).__name__}: {error}"

    try:
      os.link(published, hardlink)
      result["hardlink"] = _hardlink_paths_alias(published, hardlink)
    except OSError as error:
      result["hardlink_error"] = f"{type(error).__name__}: {error}"

    try:
      os.symlink(published.name, symlink)
      result["symlink"] = symlink.read_bytes() == published.read_bytes()
    except OSError as error:
      result["symlink_error"] = f"{type(error).__name__}: {error}"

    usage = shutil.disk_usage(root)
    result["capacity_bytes"] = {
      "total": usage.total,
      "used": usage.used,
      "free": usage.free,
    }
    result["archive_device"] = root.stat().st_dev
    result["state_device"] = state_root.stat().st_dev
    result["state_and_archive_share_device"] = result["archive_device"] == result["state_device"]
  except OSError as error:
    result["fatal_error"] = f"{type(error).__name__}: {error}"
  finally:
    for path in (symlink, hardlink, source, published):
      try:
        path.unlink()
      except FileNotFoundError:
        pass
    try:
      probe.rmdir()
    except FileNotFoundError:
      pass
    except OSError as error:
      result["cleanup_error"] = f"{type(error).__name__}: {error}"

  print(json.dumps(result, indent=2, sort_keys=True))
  return 0 if _storage_ok(result, require_cifs=require_cifs) else 1


if __name__ == "__main__":
  raise SystemExit(main())
