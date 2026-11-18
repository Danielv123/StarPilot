from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from comma_companion_importer.scanner import Artifact


INVENTORY_SCHEMA = "comma-companion.route-inventory"
INVENTORY_SCHEMA_VERSION = 1
CAPABILITY_CONFIGURED = "configured+route_union"
CAPABILITY_UNCONFIGURED = "route_union_unconfigured"
STREAM_TYPES = frozenset({"video", "rlog", "qlog"})
COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ExpectedStream:
  root_name: str
  artifact_type: str
  camera: str | None = None

  @property
  def role(self) -> str:
    return stream_role(
      self.root_name,
      self.artifact_type,
      self.camera,
    )

  def as_manifest(self) -> dict[str, Any]:
    return {
      "artifact_type": self.artifact_type,
      "camera": self.camera,
      "role": self.role,
      "root_name": self.root_name,
    }


@dataclass(frozen=True, slots=True)
class InventoryConfig:
  expected_streams: tuple[ExpectedStream, ...]


@dataclass(frozen=True, slots=True)
class RouteInventoryCandidate:
  device_id: str
  route_name: str
  content_sha256: str
  manifest_sha256: str
  manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InventoryPreview:
  device_id: str
  route_name: str
  state: str
  present_segments: tuple[int, ...]
  missing_segment_numbers: tuple[int, ...]
  missing_stream_count: int
  expected_streams: tuple[ExpectedStream, ...]


def stream_role(
  root_name: str,
  artifact_type: str,
  camera: str | None,
) -> str:
  return f"{root_name}|{artifact_type}|{camera or '-'}"


def inventory_artifact_type(artifact_type: str) -> str:
  return artifact_type if artifact_type in STREAM_TYPES else "artifact"


def canonical_json(value: Any) -> bytes:
  encoded = json.dumps(
    value,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
  )
  encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
  return encoded.encode("utf-8")


def manifest_sha256(manifest: dict[str, Any]) -> str:
  return hashlib.sha256(canonical_json(manifest)).hexdigest()


def content_sha256(manifest: dict[str, Any]) -> str:
  content = copy.deepcopy(manifest)
  content["closed_at"] = "0001-01-01T00:00:00Z"
  content["generation"] = 0
  content["previous_manifest_sha256"] = None
  return manifest_sha256(content)


def utc_timestamp(value: datetime | None = None) -> str:
  resolved = value or datetime.now(tz=UTC)
  resolved = resolved.astimezone(UTC)
  return resolved.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_component(value: Any, field: str) -> str:
  if not isinstance(value, str):
    raise ValueError(f"{field} must be a string")
  normalized = value.strip()
  if not COMPONENT_RE.fullmatch(normalized):
    raise ValueError(
      f"{field} may contain only letters, digits, dot, underscore, and dash",
    )
  return normalized


def _expected_stream(value: Any, index: int) -> ExpectedStream:
  if not isinstance(value, dict):
    raise ValueError(
      f"inventory.expected_streams[{index}] must be an object",
    )
  unknown = set(value) - {"root_name", "artifact_type", "camera"}
  if unknown:
    raise ValueError(
      "inventory.expected_streams" + f"[{index}] contains unknown fields: {', '.join(sorted(unknown))}",
    )
  root_name = _validate_component(
    value.get("root_name"),
    f"inventory.expected_streams[{index}].root_name",
  )
  artifact_type = _validate_component(
    value.get("artifact_type"),
    f"inventory.expected_streams[{index}].artifact_type",
  ).lower()
  camera_value = value.get("camera")
  camera = None
  if camera_value not in {None, ""}:
    camera = _validate_component(
      camera_value,
      f"inventory.expected_streams[{index}].camera",
    ).lower()
  if artifact_type not in STREAM_TYPES:
    raise ValueError(
      "inventory.expected_streams" + f"[{index}].artifact_type must be video, rlog, or qlog",
    )
  if artifact_type == "video" and camera is None:
    raise ValueError(
      f"inventory.expected_streams[{index}].camera is required for video",
    )
  if artifact_type != "video" and camera is not None:
    raise ValueError(
      "inventory.expected_streams" + f"[{index}].camera must be empty for logs",
    )
  return ExpectedStream(root_name, artifact_type, camera)


def parse_inventory_config(document: Any) -> InventoryConfig:
  if not isinstance(document, dict):
    raise ValueError("inventory config must be a JSON object")
  inventory = document.get("inventory", document)
  if not isinstance(inventory, dict):
    raise ValueError("inventory config's inventory field must be an object")
  unknown = set(inventory) - {"expected_streams"}
  if document is inventory and unknown:
    raise ValueError(
      "inventory config contains unknown fields: " + ", ".join(sorted(unknown)),
    )
  raw_streams = inventory.get("expected_streams")
  if not isinstance(raw_streams, list):
    raise ValueError("inventory.expected_streams must be an array")
  streams = [_expected_stream(value, index) for index, value in enumerate(raw_streams)]
  streams.sort(key=lambda item: item.role)
  roles = [item.role for item in streams]
  if len(roles) != len(set(roles)):
    raise ValueError("inventory.expected_streams contains a duplicate role")
  return InventoryConfig(tuple(streams))


def load_inventory_config(path: Path) -> InventoryConfig:
  resolved = path.expanduser().resolve()
  try:
    document = json.loads(resolved.read_text(encoding="utf-8"))
  except OSError as error:
    raise ValueError(f"cannot read inventory config {resolved}: {error}") from error
  except json.JSONDecodeError as error:
    raise ValueError(
      f"inventory config {resolved} is not valid JSON: {error}",
    ) from error
  return parse_inventory_config(document)


def _root_name(artifact: Artifact) -> str:
  root_name, separator, _rest = artifact.relative_path.partition("/")
  if not separator or not COMPONENT_RE.fullmatch(root_name):
    raise ValueError(
      f"artifact has an invalid logical root: {artifact.relative_path}",
    )
  return root_name


def _inventory_file(
  artifact: Artifact,
  digest: str,
) -> dict[str, Any]:
  if not SHA256_RE.fullmatch(digest):
    raise ValueError(
      f"artifact SHA-256 is invalid: {artifact.relative_path}",
    )
  return {
    "artifact_type": inventory_artifact_type(
      artifact.artifact_type,
    ),
    "camera": (artifact.camera if artifact.artifact_type == "video" else None),
    "mtime_ns": artifact.mtime_ns,
    "relative_path": artifact.relative_path,
    "sha256": digest,
    "size": artifact.size,
  }


def _route_groups(
  artifacts: list[Artifact],
) -> dict[tuple[str, str], list[Artifact]]:
  groups: dict[tuple[str, str], list[Artifact]] = {}
  for artifact in artifacts:
    if artifact.route_name is None or artifact.segment_number is None:
      continue
    groups.setdefault(
      (artifact.device_id, artifact.route_name),
      [],
    ).append(artifact)
  return groups


def _route_shape(
  artifacts: list[Artifact],
  config: InventoryConfig,
) -> tuple[
  list[ExpectedStream],
  list[int],
  dict[int, list[Artifact]],
  int,
  bool,
  set[str],
]:
  by_segment: dict[int, list[Artifact]] = {}
  observed: dict[str, ExpectedStream] = {}
  active_roots: set[str] = set()
  for artifact in artifacts:
    assert artifact.segment_number is not None
    by_segment.setdefault(artifact.segment_number, []).append(artifact)
    active_roots.add(_root_name(artifact))
    if artifact.artifact_type in STREAM_TYPES:
      stream = ExpectedStream(
        _root_name(artifact),
        artifact.artifact_type,
        artifact.camera,
      )
      observed[stream.role] = stream
  expected = {stream.role: stream for stream in config.expected_streams if stream.root_name in active_roots}
  expected.update(observed)
  expected_streams = [expected[role] for role in sorted(expected)]
  present_numbers = sorted(by_segment)
  if not present_numbers:
    raise ValueError("route inventory has no captured segment")
  missing_numbers = sorted(
    set(range(present_numbers[-1] + 1)) - set(present_numbers),
  )
  missing_stream_count = 0
  for segment_artifacts in by_segment.values():
    present_roles = {
      stream_role(
        _root_name(artifact),
        artifact.artifact_type,
        artifact.camera,
      )
      for artifact in segment_artifacts
      if artifact.artifact_type in STREAM_TYPES
    }
    missing_stream_count += sum(stream.role not in present_roles for stream in expected_streams)
  configured_by_root = {root_name: [stream for stream in config.expected_streams if stream.root_name == root_name] for root_name in active_roots}
  explicitly_configured = all(
    configured_by_root[root_name] and any(stream.artifact_type == "rlog" for stream in configured_by_root[root_name]) for root_name in active_roots
  )
  return (
    expected_streams,
    missing_numbers,
    by_segment,
    missing_stream_count,
    explicitly_configured,
    active_roots,
  )


def preview_inventories(
  artifacts: list[Artifact],
  config: InventoryConfig,
) -> list[InventoryPreview]:
  previews: list[InventoryPreview] = []
  for (device_id, route_name), route_artifacts in sorted(_route_groups(artifacts).items()):
    (
      expected,
      missing_numbers,
      by_segment,
      missing_stream_count,
      explicitly_configured,
      _active_roots,
    ) = _route_shape(
      route_artifacts,
      config,
    )
    complete = explicitly_configured and len(_active_roots) == 1 and not missing_numbers and missing_stream_count == 0
    previews.append(
      InventoryPreview(
        device_id=device_id,
        route_name=route_name,
        state="complete" if complete else "partial",
        present_segments=tuple(sorted(by_segment)),
        missing_segment_numbers=tuple(missing_numbers),
        missing_stream_count=missing_stream_count,
        expected_streams=tuple(expected),
      ),
    )
  return previews


def build_route_inventory(
  *,
  device_id: str,
  route_name: str,
  artifacts: list[Artifact],
  digests: dict[str, str],
  config: InventoryConfig,
  generation: int,
  previous_manifest_sha256: str | None,
  closed_at: str,
) -> RouteInventoryCandidate:
  (
    expected,
    missing_numbers,
    by_segment,
    missing_stream_count,
    explicitly_configured,
    active_roots,
  ) = _route_shape(
    artifacts,
    config,
  )
  expected_by_role = {stream.role: stream for stream in expected}
  segments: list[dict[str, Any]] = []
  for number in sorted(by_segment):
    segment_artifacts = sorted(
      by_segment[number],
      key=lambda item: item.relative_path,
    )
    files = [
      _inventory_file(
        artifact,
        digests[str(artifact.source_path)],
      )
      for artifact in segment_artifacts
    ]
    files_by_role: dict[str, tuple[Artifact, dict[str, Any]]] = {}
    for artifact, file in zip(segment_artifacts, files, strict=True):
      if artifact.artifact_type not in STREAM_TYPES:
        continue
      role = stream_role(
        _root_name(artifact),
        artifact.artifact_type,
        artifact.camera,
      )
      files_by_role.setdefault(role, (artifact, file))
    streams: list[dict[str, Any]] = []
    for role in sorted(expected_by_role):
      present = files_by_role.get(role)
      if present is None:
        streams.append(
          {
            "mtime_ns": None,
            "relative_path": None,
            "role": role,
            "sha256": None,
            "size": None,
            "status": "missing",
          },
        )
      else:
        _artifact, file = present
        streams.append(
          {
            "mtime_ns": file["mtime_ns"],
            "relative_path": file["relative_path"],
            "role": role,
            "sha256": file["sha256"],
            "size": file["size"],
            "status": "present",
          },
        )
    segments.append(
      {
        "files": files,
        "number": number,
        "streams": streams,
      },
    )

  complete = explicitly_configured and len(active_roots) == 1 and not missing_numbers and missing_stream_count == 0
  evidence = {
    "historical_static_snapshot",
    "source_scan_complete",
  }
  if not explicitly_configured:
    evidence.add("expected_streams_unconfigured")
  if missing_numbers:
    evidence.add("missing_segment_numbers")
  if missing_stream_count:
    evidence.add("missing_expected_streams")
  if len(active_roots) > 1:
    evidence.add("multiple_active_log_roots")
  manifest = {
    "capability_source": (CAPABILITY_CONFIGURED if explicitly_configured else CAPABILITY_UNCONFIGURED),
    "closed_at": closed_at,
    "closure_evidence": sorted(evidence),
    "expected_streams": [stream.as_manifest() for stream in expected],
    "generation": generation,
    "missing_segment_numbers": missing_numbers,
    "previous_manifest_sha256": previous_manifest_sha256,
    "root_names": sorted(active_roots),
    "route_closed": True,
    "route_files": [],
    "route_name": route_name,
    "schema": INVENTORY_SCHEMA,
    "schema_version": INVENTORY_SCHEMA_VERSION,
    "segments": segments,
    "state": "complete" if complete else "partial",
  }
  return RouteInventoryCandidate(
    device_id=device_id,
    route_name=route_name,
    content_sha256=content_sha256(manifest),
    manifest_sha256=manifest_sha256(manifest),
    manifest=manifest,
  )


def route_artifact_groups(
  artifacts: list[Artifact],
) -> dict[tuple[str, str], list[Artifact]]:
  return _route_groups(artifacts)
