from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from comma_companion_importer.manifest import Manifest
from comma_companion_importer.scanner import Artifact


def _artifact(path: Path) -> Artifact:
  stat = path.stat()
  return Artifact(
    source_path=path,
    device_id="device",
    route_name="route",
    segment_number=3,
    artifact_type="rlog",
    camera=None,
    relative_path="route--3/rlog",
    size=stat.st_size,
    mtime_ns=stat.st_mtime_ns,
    source_identity=f"{stat.st_dev}:{stat.st_ino}:{stat.st_ctime_ns}",
    recorded_at=datetime.now(tz=UTC),
  )


def test_manifest_resumes_and_resets_changed_sources(tmp_path: Path) -> None:
  source = tmp_path / "rlog"
  source.write_bytes(b"first")
  manifest_path = tmp_path / "state" / "import.sqlite3"

  with Manifest(manifest_path) as manifest:
    artifact = _artifact(source)
    manifest.upsert(artifact, "https://comma.example")
    manifest.set_hash(source, "a" * 64)
    manifest.set_upload(source, "upload-one", 3)
    resumed = manifest.upsert(artifact, "https://comma.example")
    assert resumed.upload_id == "upload-one"
    assert resumed.offset == 3
    assert resumed.sha256 == "a" * 64

    source.write_bytes(b"second")
    changed_ns = artifact.mtime_ns + 1_000_000_000
    os.utime(source, ns=(changed_ns, changed_ns))
    reset = manifest.upsert(_artifact(source), "https://comma.example")
    assert reset.upload_id is None
    assert reset.offset == 0
    assert reset.sha256 is None
    assert reset.status == "pending"


def test_manifest_resets_when_server_or_declaration_changes(tmp_path: Path) -> None:
  source = tmp_path / "rlog"
  source.write_bytes(b"log")

  with Manifest(tmp_path / "manifest.sqlite3") as manifest:
    artifact = _artifact(source)
    manifest.upsert(artifact, "https://one.example")
    manifest.set_hash(source, "b" * 64)
    manifest.set_upload(source, "upload-one", artifact.size)
    manifest.complete(source, artifact.size)

    changed_device = replace(artifact, device_id="correct-device")
    reset_device = manifest.upsert(changed_device, "https://one.example")
    assert reset_device.status == "pending"
    assert reset_device.upload_id is None
    assert reset_device.sha256 == "b" * 64

    manifest.set_hash(source, "b" * 64)
    manifest.set_upload(source, "upload-two", artifact.size)
    manifest.complete(source, artifact.size)
    reset_server = manifest.upsert(changed_device, "https://two.example")
    assert reset_server.status == "pending"
    assert reset_server.upload_id is None
