from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from comma_companion_importer.inventory import (
  InventoryConfig,
  build_route_inventory,
  canonical_json,
  load_inventory_config,
  manifest_sha256,
  parse_inventory_config,
  preview_inventories,
)
from comma_companion_importer.scanner import Artifact


def _artifact(
  tmp_path: Path,
  route_name: str,
  segment_number: int,
  filename: str,
  artifact_type: str,
  *,
  camera: str | None = None,
  root_name: str = "realdata",
) -> tuple[Artifact, str]:
  path = tmp_path / root_name / f"{route_name}--{segment_number}" / filename
  path.parent.mkdir(parents=True, exist_ok=True)
  content = f"{route_name}:{segment_number}:{filename}".encode()
  path.write_bytes(content)
  stat = path.stat()
  artifact = Artifact(
    source_path=path,
    device_id="smoke-comma",
    route_name=route_name,
    segment_number=segment_number,
    artifact_type=artifact_type,
    camera=camera,
    relative_path=(f"{root_name}/{route_name}--{segment_number}/{filename}"),
    size=stat.st_size,
    mtime_ns=stat.st_mtime_ns,
    source_identity=(f"{stat.st_dev}:{stat.st_ino}:{stat.st_ctime_ns}"),
    recorded_at=datetime.now(tz=UTC),
  )
  return artifact, hashlib.sha256(content).hexdigest()


def _config(*streams: dict[str, str]) -> InventoryConfig:
  return parse_inventory_config(
    {
      "inventory": {
        "expected_streams": list(streams),
      },
    },
  )


def test_agent_inventory_config_schema_and_canonical_json(
  tmp_path: Path,
) -> None:
  path = tmp_path / "agent-config.json"
  path.write_text(
    """
    {
      "server_url": "https://comma.example",
      "inventory": {
        "expected_streams": [
          {
            "root_name": "realdata",
            "artifact_type": "video",
            "camera": "road"
          },
          {
            "root_name": "realdata",
            "artifact_type": "rlog",
            "camera": ""
          }
        ]
      }
    }
    """,
    encoding="utf-8",
  )
  config = load_inventory_config(path)
  assert [stream.role for stream in config.expected_streams] == [
    "realdata|rlog|-",
    "realdata|video|road",
  ]
  canonical = canonical_json(
    {
      "z": 1,
      "special": "<&>\u2028\u2029",
    },
  )
  assert canonical == (b'{"special":"\\u003c\\u0026\\u003e' + b'\\u2028\\u2029","z":1}')
  assert hashlib.sha256(canonical).hexdigest() == ("28a9d312fa97f7b690f42a8a33380df5" + "748f9284074ae8c3631820d73526500e")


def test_segments_98_99_are_a_closed_but_partial_snapshot(
  tmp_path: Path,
) -> None:
  artifacts: list[Artifact] = []
  digests: dict[str, str] = {}
  route = "000000dc--fe7070223b"
  for segment in (98, 99):
    for filename, kind, camera in (
      ("fcamera.hevc", "video", "road"),
      ("rlog.zst", "rlog", None),
    ):
      artifact, digest = _artifact(
        tmp_path,
        route,
        segment,
        filename,
        kind,
        camera=camera,
      )
      artifacts.append(artifact)
      digests[str(artifact.source_path)] = digest
  config = _config(
    {"root_name": "realdata", "artifact_type": "rlog"},
    {
      "root_name": "realdata",
      "artifact_type": "video",
      "camera": "road",
    },
  )

  preview = preview_inventories(artifacts, config)[0]
  assert preview.state == "partial"
  assert preview.present_segments == (98, 99)
  assert preview.missing_segment_numbers == tuple(range(98))
  assert preview.missing_stream_count == 0

  candidate = build_route_inventory(
    device_id="smoke-comma",
    route_name=route,
    artifacts=artifacts,
    digests=digests,
    config=config,
    generation=1,
    previous_manifest_sha256=None,
    closed_at="2026-07-29T00:00:00.000000Z",
  )
  manifest = candidate.manifest
  assert manifest["route_closed"] is True
  assert manifest["state"] == "partial"
  assert manifest["missing_segment_numbers"] == list(range(98))
  assert [segment["number"] for segment in manifest["segments"]] == [
    98,
    99,
  ]
  assert (
    len(
      [file for segment in manifest["segments"] for file in segment["files"]],
    )
    == 4
  )
  assert candidate.manifest_sha256 == manifest_sha256(manifest)


def test_nonstream_files_use_agent_artifact_identity_and_do_not_hide(
  tmp_path: Path,
) -> None:
  artifacts: list[Artifact] = []
  digests: dict[str, str] = {}
  route = "000000c6--d12fa4d148"
  for segment in range(4):
    artifact, digest = _artifact(
      tmp_path,
      route,
      segment,
      "rlog.zst",
      "rlog",
    )
    artifacts.append(artifact)
    digests[str(artifact.source_path)] = digest
  metadata, metadata_digest = _artifact(
    tmp_path,
    route,
    0,
    "route_metadata.json",
    "metadata",
  )
  artifacts.append(metadata)
  digests[str(metadata.source_path)] = metadata_digest
  config = _config(
    {"root_name": "realdata", "artifact_type": "rlog"},
  )

  candidate = build_route_inventory(
    device_id="smoke-comma",
    route_name=route,
    artifacts=artifacts,
    digests=digests,
    config=config,
    generation=1,
    previous_manifest_sha256=None,
    closed_at="2026-07-29T00:00:00.000000Z",
  )
  manifest = candidate.manifest
  assert manifest["state"] == "complete"
  assert manifest["missing_segment_numbers"] == []
  assert [segment["number"] for segment in manifest["segments"]] == [
    0,
    1,
    2,
    3,
  ]
  assert [len(segment["files"]) for segment in manifest["segments"]] == [
    2,
    1,
    1,
    1,
  ]
  assert manifest["segments"][0]["files"][0] == {
    "artifact_type": "rlog",
    "camera": None,
    "mtime_ns": artifacts[0].mtime_ns,
    "relative_path": artifacts[0].relative_path,
    "sha256": digests[str(artifacts[0].source_path)],
    "size": artifacts[0].size,
  }
  assert manifest["segments"][0]["files"][1] == {
    "artifact_type": "artifact",
    "camera": None,
    "mtime_ns": metadata.mtime_ns,
    "relative_path": metadata.relative_path,
    "sha256": metadata_digest,
    "size": metadata.size,
  }


def test_configured_alternative_roots_are_scoped_to_active_route_roots(
  tmp_path: Path,
) -> None:
  artifact, digest = _artifact(
    tmp_path,
    "route",
    0,
    "rlog.zst",
    "rlog",
    root_name="realdata_HD",
  )
  incomplete_config = _config(
    {"root_name": "realdata", "artifact_type": "rlog"},
  )
  partial = build_route_inventory(
    device_id="smoke-comma",
    route_name="route",
    artifacts=[artifact],
    digests={str(artifact.source_path): digest},
    config=incomplete_config,
    generation=1,
    previous_manifest_sha256=None,
    closed_at="2026-07-29T00:00:00.000000Z",
  ).manifest
  assert partial["root_names"] == ["realdata_HD"]
  assert partial["expected_streams"] == [
    {
      "artifact_type": "rlog",
      "camera": None,
      "role": "realdata_HD|rlog|-",
      "root_name": "realdata_HD",
    },
  ]
  assert partial["capability_source"] == "route_union_unconfigured"
  assert partial["state"] == "partial"
  assert "expected_streams_unconfigured" in partial["closure_evidence"]

  complete_config = _config(
    {"root_name": "realdata", "artifact_type": "rlog"},
    {"root_name": "realdata_HD", "artifact_type": "rlog"},
  )
  complete = build_route_inventory(
    device_id="smoke-comma",
    route_name="route",
    artifacts=[artifact],
    digests={str(artifact.source_path): digest},
    config=complete_config,
    generation=1,
    previous_manifest_sha256=None,
    closed_at="2026-07-29T00:00:00.000000Z",
  ).manifest
  assert complete["root_names"] == ["realdata_HD"]
  assert complete["state"] == "complete"


def test_multiple_active_alternative_log_roots_are_explicitly_partial(
  tmp_path: Path,
) -> None:
  artifacts: list[Artifact] = []
  digests: dict[str, str] = {}
  for root_name in ("realdata", "realdata_HD"):
    artifact, digest = _artifact(
      tmp_path,
      "route",
      0,
      "rlog.zst",
      "rlog",
      root_name=root_name,
    )
    artifacts.append(artifact)
    digests[str(artifact.source_path)] = digest
  config = _config(
    {"root_name": "realdata", "artifact_type": "rlog"},
    {"root_name": "realdata_HD", "artifact_type": "rlog"},
  )
  manifest = build_route_inventory(
    device_id="smoke-comma",
    route_name="route",
    artifacts=artifacts,
    digests=digests,
    config=config,
    generation=1,
    previous_manifest_sha256=None,
    closed_at="2026-07-29T00:00:00.000000Z",
  ).manifest
  assert manifest["root_names"] == ["realdata", "realdata_HD"]
  assert manifest["state"] == "partial"
  assert "multiple_active_log_roots" in manifest["closure_evidence"]
  assert (
    len(
      [stream for stream in manifest["expected_streams"] if stream["artifact_type"] == "rlog"],
    )
    == 2
  )


def test_absent_explicit_stream_is_visible_and_partial(
  tmp_path: Path,
) -> None:
  artifact, digest = _artifact(
    tmp_path,
    "route",
    0,
    "rlog.zst",
    "rlog",
  )
  config = _config(
    {"root_name": "realdata", "artifact_type": "rlog"},
    {
      "root_name": "realdata",
      "artifact_type": "video",
      "camera": "road",
    },
  )
  candidate = build_route_inventory(
    device_id="smoke-comma",
    route_name="route",
    artifacts=[artifact],
    digests={str(artifact.source_path): digest},
    config=config,
    generation=1,
    previous_manifest_sha256=None,
    closed_at="2026-07-29T00:00:00.000000Z",
  )
  assert candidate.manifest["state"] == "partial"
  assert "missing_expected_streams" in (candidate.manifest["closure_evidence"])
  assert candidate.manifest["segments"][0]["streams"] == [
    {
      "mtime_ns": artifact.mtime_ns,
      "relative_path": artifact.relative_path,
      "role": "realdata|rlog|-",
      "sha256": digest,
      "size": artifact.size,
      "status": "present",
    },
    {
      "mtime_ns": None,
      "relative_path": None,
      "role": "realdata|video|road",
      "sha256": None,
      "size": None,
      "status": "missing",
    },
  ]
