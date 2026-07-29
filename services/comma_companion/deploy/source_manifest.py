#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import os
import posixpath
import stat
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = SERVICE_ROOT.parents[1]
SOURCE_ROOTS = (
  SERVICE_ROOT / "backend",
  SERVICE_ROOT / "media_worker",
  SERVICE_ROOT / "adapters" / "dynamics",
  SERVICE_ROOT / "adapters" / "rlog",
  SERVICE_ROOT / "web",
  SERVICE_ROOT / "deploy",
)
SOURCE_FILES = (
  SERVICE_ROOT / "Dockerfile",
  SERVICE_ROOT / "docker-compose.yml",
  SERVICE_ROOT / ".dockerignore",
  SERVICE_ROOT / ".gitignore",
  SERVICE_ROOT / ".env.example",
  SERVICE_ROOT / "README.md",
  REPOSITORY_ROOT / "cereal" / "__init__.py",
  REPOSITORY_ROOT / "cereal" / "log.capnp",
  REPOSITORY_ROOT / "cereal" / "custom.capnp",
  REPOSITORY_ROOT / "cereal" / "legacy.capnp",
  REPOSITORY_ROOT / "opendbc_repo" / "opendbc" / "car" / "car.capnp",
)
EXCLUDED_DIRECTORIES = {
  ".git",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  ".venv",
  "__pycache__",
  "coverage",
  "dist",
  "node_modules",
  "secrets",
}


@dataclass(frozen=True, slots=True)
class ManifestEntry:
  path: str
  kind: str
  mode: int
  size: int
  sha256: str
  content: bytes


def _excluded(path: Path) -> bool:
  return (
    path.name in EXCLUDED_DIRECTORIES
    or path.name == ".env"
    or path.name.startswith(".env.")
    and path.name != ".env.example"
    or path.suffix in {".pyc", ".pyo", ".tsbuildinfo"}
    or path.name.endswith(".egg-info")
  )


def _entry(path: Path) -> ManifestEntry:
  relative = path.relative_to(REPOSITORY_ROOT).as_posix()
  if path.is_symlink():
    content = os.readlink(path).encode("utf-8")
    kind = "symlink"
  elif path.is_dir():
    content = b""
    kind = "directory"
  else:
    content = path.read_bytes()
    kind = "file"
  mode = _logical_mode(kind, content)
  return ManifestEntry(
    path=relative,
    kind=kind,
    mode=mode,
    size=len(content),
    sha256=hashlib.sha256(content).hexdigest(),
    content=content,
  )


def _logical_mode(kind: str, content: bytes) -> int:
  if kind == "directory":
    return 0o755
  if kind == "symlink":
    return 0o777
  if kind != "file":
    raise ValueError(f"unsupported source-manifest entry kind: {kind}")
  return 0o755 if content.startswith(b"#!") else 0o644


def _walk(root: Path) -> list[Path]:
  paths: list[Path] = [root]
  for directory, names, filenames in os.walk(root, followlinks=False):
    parent = Path(directory)
    retained_names: list[str] = []
    for name in sorted(names):
      path = parent / name
      if _excluded(path):
        continue
      if path.is_symlink():
        paths.append(path)
      else:
        paths.append(path)
        retained_names.append(name)
    names[:] = retained_names
    for name in sorted(filenames):
      path = parent / name
      if not _excluded(path):
        paths.append(path)
  return paths


def manifest_entries() -> tuple[ManifestEntry, ...]:
  candidates = list(SOURCE_FILES)
  candidates.extend(_walk(REPOSITORY_ROOT / "cereal" / "include"))
  for root in SOURCE_ROOTS:
    candidates.extend(_walk(root))
  missing = [path for path in candidates if not path.exists() and not path.is_symlink()]
  if missing:
    joined = ", ".join(str(path) for path in missing)
    raise FileNotFoundError(f"source bundle inputs are missing: {joined}")
  unique = {path.relative_to(REPOSITORY_ROOT).as_posix(): path for path in candidates}
  return tuple(_entry(unique[name]) for name in sorted(unique))


def source_sha256(entries: tuple[ManifestEntry, ...] | None = None) -> str:
  digest = hashlib.sha256()
  for entry in entries or manifest_entries():
    digest.update(entry.kind.encode("ascii"))
    digest.update(b"\0")
    digest.update(entry.path.encode("utf-8"))
    digest.update(b"\0")
    digest.update(f"{entry.mode:04o}".encode("ascii"))
    digest.update(b"\0")
    digest.update(str(entry.size).encode("ascii"))
    digest.update(b"\0")
    digest.update(entry.content)
    digest.update(b"\0")
  return digest.hexdigest()


def _safe_archive_entry(entry: ManifestEntry) -> None:
  if (
    not entry.path
    or entry.path.startswith("/")
    or posixpath.normpath(entry.path).startswith("../")
    or posixpath.normpath(entry.path) != entry.path
  ):
    raise ValueError(f"unsafe source archive path: {entry.path!r}")
  if entry.kind == "symlink":
    target = entry.content.decode("utf-8")
    resolved = posixpath.normpath(
      posixpath.join(posixpath.dirname(entry.path), target),
    )
    if (
      not target
      or target.startswith("/")
      or resolved == ".."
      or resolved.startswith("../")
    ):
        raise ValueError(f"source archive symlink escapes the repository: {entry.path!r} -> {target!r}")


def _tar_info(entry: ManifestEntry) -> tarfile.TarInfo:
  _safe_archive_entry(entry)
  information = tarfile.TarInfo(entry.path)
  information.uid = 0
  information.gid = 0
  information.uname = "root"
  information.gname = "root"
  information.mtime = 0
  information.mode = entry.mode
  if entry.kind == "directory":
    information.type = tarfile.DIRTYPE
    information.size = 0
  elif entry.kind == "symlink":
    information.type = tarfile.SYMTYPE
    information.linkname = entry.content.decode("utf-8")
    information.size = 0
  elif entry.kind == "file":
    information.type = tarfile.REGTYPE
    information.size = entry.size
  else:
    raise ValueError(f"unsupported source archive entry kind: {entry.kind}")
  return information


def write_archive(
  entries: tuple[ManifestEntry, ...],
  destination: Path,
) -> str:
  destination = destination.resolve()
  if destination.suffix != ".tar":
    raise ValueError("deterministic source archive must use a .tar filename")
  destination.parent.mkdir(parents=False, exist_ok=True)
  descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{destination.name}.",
    suffix=".tmp",
    dir=destination.parent,
  )
  os.close(descriptor)
  temporary = Path(temporary_name)
  try:
    with temporary.open("wb") as stream:
      with tarfile.open(
        fileobj=stream,
        mode="w",
        format=tarfile.PAX_FORMAT,
      ) as archive:
        for entry in entries:
          information = _tar_info(entry)
          data = (
            io.BytesIO(entry.content)
            if entry.kind == "file"
            else None
          )
          archive.addfile(information, data)
      stream.flush()
      os.fsync(stream.fileno())
    os.chmod(temporary, 0o644)
    archive_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
    if destination.exists() or destination.is_symlink():
      metadata = destination.lstat()
      if (
        destination.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or hashlib.sha256(destination.read_bytes()).hexdigest() != archive_hash
      ):
        raise FileExistsError(
          f"refusing to replace a different source archive: {destination}",
        )
      temporary.unlink()
      return archive_hash
    os.replace(temporary, destination)
    if os.name != "nt":
      directory_descriptor = os.open(destination.parent, os.O_RDONLY)
      try:
        os.fsync(directory_descriptor)
      finally:
        os.close(directory_descriptor)
    return archive_hash
  finally:
    temporary.unlink(missing_ok=True)


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Hash the deterministic Comma Companion deployment source bundle.",
  )
  parser.add_argument(
    "--manifest",
    action="store_true",
    help="print the per-file manifest before the aggregate bundle digest",
  )
  parser.add_argument(
    "--archive",
    type=Path,
      help="write a deterministic uncompressed .tar of the exact manifest entries for transfer to the Linux build host",
  )
  arguments = parser.parse_args()
  entries = manifest_entries()
  if arguments.archive is not None:
    archive_hash = write_archive(entries, arguments.archive)
    print(
      f"source-archive-sha256\t{archive_hash}\t{arguments.archive.resolve()}",
      file=sys.stderr,
    )
  if arguments.manifest:
    for entry in entries:
      print(f"{entry.kind}\t{entry.mode:04o}\t{entry.sha256}\t{entry.size}\t{entry.path}")
    print(f"bundle-sha256\t{source_sha256(entries)}")
  else:
    print(source_sha256(entries))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
