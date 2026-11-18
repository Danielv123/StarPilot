from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from comma_companion_importer.scanner import ScanFilter, classify_artifact, scan
import pytest


def _write(path: Path, payload: bytes = b"x") -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_bytes(payload)
  return path


def test_classifies_current_legacy_and_compressed_names() -> None:
  assert classify_artifact("rlog.zst") == ("rlog", None)
  assert classify_artifact("qlog.bz2") == ("qlog", None)
  assert classify_artifact("fcamera.hevc") == ("video", "road")
  assert classify_artifact("camera.h265") == ("video", "road")
  assert classify_artifact("ecamera.hevc") == ("video", "wide")
  assert classify_artifact("dcamera.mp4") == ("video", "driver")
  assert classify_artifact("qcamera.ts") == ("video", "qcamera")
  assert classify_artifact("route_metadata.json") == ("metadata", None)
  assert classify_artifact("unrecognized.bin") == ("other", None)


def test_scans_supported_layouts_and_builds_canonical_paths(tmp_path: Path) -> None:
  _write(tmp_path / "device-a" / "realdata" / "route-a--7" / "rlog.zst", b"rlog")
  _write(tmp_path / "device-a" / "realdata" / "route-a--7" / "fcamera.hevc", b"video")
  _write(tmp_path / "device-b" / "_HD" / "route-b" / "8" / "qlog.bz2", b"qlog")
  _write(tmp_path / "device-c" / "_konik" / "route-c--9--dcamera.hevc", b"driver")
  _write(tmp_path / "device-c" / "_konik" / "boot" / "boot-id.zst", b"boot")
  _write(tmp_path / "outside.txt", b"ignore")

  artifacts = scan(tmp_path, ScanFilter())

  assert [(item.device_id, item.route_name, item.segment_number, item.relative_path) for item in artifacts] == [
    ("device-a", "route-a", 7, "realdata/route-a--7/fcamera.hevc"),
    ("device-a", "route-a", 7, "realdata/route-a--7/rlog.zst"),
    ("device-b", "route-b", 8, "realdata_HD/route-b--8/qlog.bz2"),
    ("device-c", None, None, "realdata_konik/boot/boot-id.zst"),
    ("device-c", "route-c", 9, "realdata_konik/route-c--9/dcamera.hevc"),
  ]


def test_filters_routes_dates_cameras_logs_and_other(tmp_path: Path) -> None:
  segment = tmp_path / "device-a" / "realdata" / "drive-2026-07-04--10-30-00--2"
  _write(segment / "rlog")
  _write(segment / "qlog")
  _write(segment / "fcamera.hevc")
  _write(segment / "dcamera.hevc")
  _write(segment / "metadata.json")

  scan_filter = ScanFilter(
    cameras=frozenset({"road"}),
    logs=frozenset({"rlog"}),
    include_other=False,
    routes=("drive-*",),
    since=datetime(2026, 7, 4, tzinfo=UTC),
    until=datetime(2026, 7, 5, tzinfo=UTC),
  )
  artifacts = scan(tmp_path, scan_filter)

  assert [(item.artifact_type, item.camera) for item in artifacts] == [
    ("video", "road"),
    ("rlog", None),
  ]


def test_falls_back_to_mtime_for_opaque_route_date(tmp_path: Path) -> None:
  path = _write(tmp_path / "realdata" / "opaque-route--0" / "rlog")
  timestamp = datetime(2026, 7, 10, 12, tzinfo=UTC).timestamp()
  os.utime(path, (timestamp, timestamp))

  included = scan(
    tmp_path / "realdata",
    ScanFilter(
      since=datetime(2026, 7, 10, tzinfo=UTC),
      until=datetime(2026, 7, 11, tzinfo=UTC),
    ),
    "override-device",
  )
  excluded = scan(
    tmp_path / "realdata",
    ScanFilter(since=datetime(2026, 7, 11, tzinfo=UTC)),
    "override-device",
  )

  assert len(included) == 1
  assert included[0].device_id == "override-device"
  assert excluded == []


def test_infers_device_when_source_is_device_directory(tmp_path: Path) -> None:
  device_root = tmp_path / "10.30.1.75"
  _write(device_root / "realdata" / "route--0" / "rlog")

  artifacts = scan(device_root, ScanFilter())

  assert len(artifacts) == 1
  assert artifacts[0].device_id == "10.30.1.75"


def test_scan_fails_instead_of_silently_omitting_unreadable_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
  def failed_walk(_root: Path, *, followlinks: bool, onerror: object) -> list[object]:
    assert not followlinks
    assert callable(onerror)
    onerror(PermissionError("denied"))
    return []

  monkeypatch.setattr(os, "walk", failed_walk)

  with pytest.raises(OSError, match="scan was incomplete"):
    scan(tmp_path, ScanFilter())
