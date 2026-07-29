from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


ROUTE_DATE_RE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})--(\d{2})-(\d{2})-(\d{2})(?!\d)")
COMPRESSED_SUFFIXES = (".bz2", ".zst", ".gz", ".xz")
MEDIA_EXTENSIONS = (".hevc", ".h265", ".mp4", ".ts", ".mkv")
REALDATA_NAMESPACES = {
  "realdata": "realdata",
  "_hd": "realdata_HD",
  "realdata_hd": "realdata_HD",
  "_konik": "realdata_konik",
  "realdata_konik": "realdata_konik",
}
CAMERA_ALIASES = {
  "camera": "road",
  "fcamera": "road",
  "road": "road",
  "roadcamera": "road",
  "ecamera": "wide",
  "wide": "wide",
  "widecamera": "wide",
  "dcamera": "driver",
  "driver": "driver",
  "drivercamera": "driver",
  "qcamera": "qcamera",
}


@dataclass(frozen=True, slots=True)
class Artifact:
  source_path: Path
  device_id: str
  route_name: str | None
  segment_number: int | None
  artifact_type: str
  camera: str | None
  relative_path: str
  size: int
  mtime_ns: int
  source_identity: str
  recorded_at: datetime


@dataclass(frozen=True, slots=True)
class ScanFilter:
  cameras: frozenset[str] = frozenset({"road", "wide", "driver", "qcamera"})
  logs: frozenset[str] = frozenset({"rlog", "qlog"})
  include_other: bool = True
  routes: tuple[str, ...] = ()
  exclude_routes: tuple[str, ...] = ()
  since: datetime | None = None
  until: datetime | None = None
  include_artifacts: frozenset[str] = frozenset()
  exclude_artifacts: frozenset[str] = frozenset()

  def accepts(self, artifact: Artifact) -> bool:
    if self.routes and (artifact.route_name is None or not any(fnmatch.fnmatchcase(artifact.route_name, pattern) for pattern in self.routes)):
      return False
    if artifact.route_name is not None and any(fnmatch.fnmatchcase(artifact.route_name, pattern) for pattern in self.exclude_routes):
      return False
    if self.since is not None and artifact.recorded_at < self.since:
      return False
    if self.until is not None and artifact.recorded_at >= self.until:
      return False
    if self.include_artifacts and artifact.artifact_type not in self.include_artifacts:
      return False
    if artifact.artifact_type in self.exclude_artifacts:
      return False
    if artifact.artifact_type == "video":
      return artifact.camera in self.cameras
    if artifact.artifact_type in {"rlog", "qlog"}:
      return artifact.artifact_type in self.logs
    return self.include_other


def _strip_compression(filename: str) -> str:
  lowered = filename.lower()
  for suffix in COMPRESSED_SUFFIXES:
    if lowered.endswith(suffix):
      return lowered[: -len(suffix)]
  return lowered


def classify_artifact(filename: str) -> tuple[str, str | None]:
  base = _strip_compression(Path(filename).name)
  if base in {"rlog", "qlog"}:
    return base, None

  stem = base
  for suffix in MEDIA_EXTENSIONS:
    if stem.endswith(suffix):
      stem = stem[: -len(suffix)]
      break
  camera = CAMERA_ALIASES.get(stem)
  if camera is not None:
    return "video", camera

  plain_stem = Path(base).stem.replace("-", "_").lower()
  if plain_stem in {"bootlog", "boot"}:
    return "bootlog", None
  if plain_stem in {"crash", "error", "error_log"} or plain_stem.startswith("crash"):
    return "crash", None
  if plain_stem in {"initdata", "init_data", "metadata", "route_metadata"}:
    return "metadata", None
  if plain_stem in {"stats", "statlog"}:
    return "stats", None
  if plain_stem in {"user_flag", "userflag"}:
    return "user_flag", None
  return "other", None


def _parse_segment_dir(name: str) -> tuple[str, int] | None:
  route, separator, segment = name.rpartition("--")
  if separator and route and segment.isdigit():
    return route, int(segment)
  return None


def _parse_flat_filename(name: str) -> tuple[str, int, str] | None:
  route_and_segment, separator, artifact_name = name.rpartition("--")
  if not separator or not artifact_name:
    return None
  parsed = _parse_segment_dir(route_and_segment)
  if parsed is None:
    return None
  route, segment = parsed
  return route, segment, artifact_name


def _route_timestamp(route_name: str | None, fallback_mtime_ns: int) -> datetime:
  match = ROUTE_DATE_RE.search(route_name) if route_name is not None else None
  if match is not None:
    try:
      return datetime(*(int(part) for part in match.groups()), tzinfo=UTC)
    except ValueError:
      pass
  return datetime.fromtimestamp(fallback_mtime_ns / 1_000_000_000, tz=UTC)


def _infer_device_id(root: Path, source_path: Path, override: str | None) -> str:
  if override:
    return override

  relative_parts = source_path.relative_to(root).parts
  for index, part in enumerate(relative_parts):
    if part.lower() in REALDATA_NAMESPACES:
      if index > 0:
        return relative_parts[index - 1]
      if root.name:
        return root.name

  if root.name.lower() in REALDATA_NAMESPACES and root.parent.name:
    return root.parent.name
  return "historical-import"


def _root_namespace(root: Path, source_path: Path) -> str:
  for part in source_path.relative_to(root).parts[:-1]:
    if namespace := REALDATA_NAMESPACES.get(part.lower()):
      return namespace
  return REALDATA_NAMESPACES.get(root.name.lower(), "realdata")


def _locate_route_segment(root: Path, source_path: Path) -> tuple[str, int, str] | None:
  flattened = _parse_flat_filename(source_path.name)
  if flattened is not None:
    return flattened

  current = source_path.parent
  while current != root.parent:
    parsed = _parse_segment_dir(current.name)
    if parsed is not None:
      route, segment = parsed
      return route, segment, source_path.name
    if current == root:
      break
    current = current.parent

  parent = source_path.parent
  if parent.name.isdigit() and parent.parent != parent:
    route = parent.parent.name
    if route and route.lower() not in REALDATA_NAMESPACES:
      return route, int(parent.name), source_path.name
  return None


def _is_boot_artifact(root: Path, source_path: Path) -> bool:
  return any(part.lower() == "boot" for part in source_path.relative_to(root).parts[:-1])


def scan(source_root: Path, scan_filter: ScanFilter, device_id: str | None = None) -> list[Artifact]:
  root = source_root.expanduser().resolve()
  if not root.is_dir():
    raise ValueError(f"source root is not a directory: {root}")

  artifacts: list[Artifact] = []
  walk_errors: list[OSError] = []
  for directory, directory_names, filenames in os.walk(root, followlinks=False, onerror=walk_errors.append):
    directory_names[:] = sorted(name for name in directory_names if not Path(directory, name).is_symlink())
    for filename in sorted(filenames):
      path = Path(directory, filename)
      if path.is_symlink() or not path.is_file():
        continue
      stat = path.stat()
      namespace = _root_namespace(root, path)
      route_segment = _locate_route_segment(root, path)
      if route_segment is None:
        if not _is_boot_artifact(root, path):
          continue
        route_name = None
        segment_number = None
        artifact_filename = path.name
        artifact_type, camera = "bootlog", None
        relative_path = f"{namespace}/boot/{artifact_filename}"
      else:
        route_name, segment_number, artifact_filename = route_segment
        artifact_type, camera = classify_artifact(artifact_filename)
        relative_path = f"{namespace}/{route_name}--{segment_number}/{artifact_filename}"
      artifact = Artifact(
        source_path=path,
        device_id=_infer_device_id(root, path, device_id),
        route_name=route_name,
        segment_number=segment_number,
        artifact_type=artifact_type,
        camera=camera,
        relative_path=relative_path,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        source_identity=f"{stat.st_dev}:{stat.st_ino}:{stat.st_ctime_ns}",
        recorded_at=_route_timestamp(route_name, stat.st_mtime_ns),
      )
      if scan_filter.accepts(artifact):
        artifacts.append(artifact)
  if walk_errors:
    first = walk_errors[0]
    raise OSError(f"scan was incomplete due to {len(walk_errors)} directory error(s); first error: {first}")
  return sorted(
    artifacts,
    key=lambda item: (
      item.device_id,
      item.route_name or "",
      item.segment_number if item.segment_number is not None else -1,
      item.relative_path,
    ),
  )
