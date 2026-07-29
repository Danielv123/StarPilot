from __future__ import annotations

import hashlib
import os
import tarfile
from pathlib import Path

from pytest import MonkeyPatch

import deploy.source_manifest as source_manifest
from deploy.source_manifest import (
  ManifestEntry,
  _logical_mode,
  source_sha256,
  write_archive,
)


def test_entry_ignores_host_permission_bits(
  tmp_path: Path,
  monkeypatch: MonkeyPatch,
) -> None:
  monkeypatch.setattr(source_manifest, "REPOSITORY_ROOT", tmp_path)
  candidate = tmp_path / "script"
  candidate.write_bytes(b"#!/bin/sh\nexit 0\n")
  os.chmod(candidate, 0o600)
  first = source_manifest._entry(candidate)
  os.chmod(candidate, 0o777)
  second = source_manifest._entry(candidate)
  assert first.mode == second.mode == 0o755
  assert source_sha256((first,)) == source_sha256((second,))


def entry(
  path: str,
  kind: str,
  content: bytes,
) -> ManifestEntry:
  return ManifestEntry(
    path=path,
    kind=kind,
    mode=_logical_mode(kind, content),
    size=len(content),
    sha256=hashlib.sha256(content).hexdigest(),
    content=content,
  )


def test_logical_modes_do_not_depend_on_host_lstat() -> None:
  assert _logical_mode("directory", b"") == 0o755
  assert _logical_mode("symlink", b"../target") == 0o777
  assert _logical_mode("file", b"ordinary\n") == 0o644
  assert _logical_mode("file", b"#!/bin/sh\nexit 0\n") == 0o755
  assert _logical_mode("file", b"#!/usr/bin/env python3\r\n") == 0o755


def test_source_hash_uses_logical_modes() -> None:
  content = b"same bytes\n"
  windows_shaped = ManifestEntry(
    path="services/comma_companion/example.txt",
    kind="file",
    mode=_logical_mode("file", content),
    size=len(content),
    sha256=hashlib.sha256(content).hexdigest(),
    content=content,
  )
  linux_shaped = ManifestEntry(
    path=windows_shaped.path,
    kind=windows_shaped.kind,
    mode=0o644,
    size=windows_shaped.size,
    sha256=windows_shaped.sha256,
    content=content,
  )
  assert source_sha256((windows_shaped,)) == source_sha256((linux_shaped,))


def test_deterministic_archive_preserves_logical_metadata(
  tmp_path: Path,
) -> None:
  entries = (
    entry("services/comma_companion", "directory", b""),
    entry(
      "services/comma_companion/run.sh",
      "file",
      b"#!/bin/sh\nexit 0\n",
    ),
    entry(
      "services/comma_companion/config.txt",
      "file",
      b"value\n",
    ),
    entry(
      "services/comma_companion/config-link",
      "symlink",
      b"config.txt",
    ),
  )
  first = tmp_path / "first.tar"
  second = tmp_path / "second.tar"
  first_hash = write_archive(entries, first)
  second_hash = write_archive(entries, second)
  assert first.read_bytes() == second.read_bytes()
  assert first_hash == second_hash

  with tarfile.open(first, "r") as archive:
    members = {member.name: member for member in archive.getmembers()}
    assert members["services/comma_companion"].isdir()
    assert members["services/comma_companion"].mode == 0o755
    assert members["services/comma_companion/run.sh"].mode == 0o755
    assert members["services/comma_companion/config.txt"].mode == 0o644
    link = members["services/comma_companion/config-link"]
    assert link.issym()
    assert link.mode == 0o777
    assert link.linkname == "config.txt"
    assert all(member.mtime == 0 for member in members.values())
