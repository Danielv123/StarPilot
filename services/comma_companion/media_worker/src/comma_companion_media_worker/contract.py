from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CAMERAS = {"road", "wide", "driver", "qcamera", "unknown"}
INPUT_FORMATS = {"raw_hevc", "mpegts"}


class ContractError(ValueError):
  pass


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
  if not isinstance(value, Mapping):
    raise ContractError(f"{field_name} must be an object")
  return value


def _required_string(value: Any, field_name: str) -> str:
  if not isinstance(value, str) or not value.strip():
    raise ContractError(f"{field_name} must be a non-empty string")
  return value


def _optional_string(value: Any, field_name: str) -> str | None:
  if value is None:
    return None
  return _required_string(value, field_name)


def _absolute_path(value: Any, field_name: str) -> str:
  result = _required_string(value, field_name)
  if not Path(result).is_absolute():
    raise ContractError(f"{field_name} must be an absolute local path")
  return result


def _optional_absolute_path(value: Any, field_name: str) -> str | None:
  if value is None:
    return None
  return _absolute_path(value, field_name)


def _integer(value: Any, field_name: str, minimum: int, maximum: int | None = None) -> int:
  if isinstance(value, bool) or not isinstance(value, int):
    raise ContractError(f"{field_name} must be an integer")
  if value < minimum or (maximum is not None and value > maximum):
    range_text = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
    raise ContractError(f"{field_name} must be {range_text}")
  return value


def _number(value: Any, field_name: str, minimum: float, maximum: float | None = None) -> float:
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise ContractError(f"{field_name} must be a number")
  result = float(value)
  if result < minimum or (maximum is not None and result > maximum):
    range_text = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
    raise ContractError(f"{field_name} must be {range_text}")
  return result


def _boolean(value: Any, field_name: str) -> bool:
  if not isinstance(value, bool):
    raise ContractError(f"{field_name} must be a boolean")
  return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], field_name: str) -> None:
  unknown = sorted(set(value) - allowed)
  if unknown:
    raise ContractError(f"{field_name} contains unknown fields: {', '.join(unknown)}")


@dataclass(frozen=True)
class InputArtifact:
  path: str
  camera: str
  kind: str
  input_format: str
  segment_num: int
  artifact_id: str | None = None
  expected_sha256: str | None = None

  @classmethod
  def from_dict(cls, value: Any) -> InputArtifact:
    source = _mapping(value, "input")
    _reject_unknown(source, {"path", "camera", "kind", "input_format", "artifact_id", "expected_sha256", "segment_num"}, "input")
    camera = _required_string(source.get("camera"), "input.camera")
    if camera not in CAMERAS:
      raise ContractError(f"input.camera must be one of: {', '.join(sorted(CAMERAS))}")
    input_format = _required_string(source.get("input_format"), "input.input_format")
    if input_format not in INPUT_FORMATS:
      raise ContractError(f"input.input_format must be one of: {', '.join(sorted(INPUT_FORMATS))}")
    expected_sha256 = _optional_string(source.get("expected_sha256"), "input.expected_sha256")
    if expected_sha256 is not None:
      expected_sha256 = expected_sha256.lower()
      if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise ContractError("input.expected_sha256 must be a lowercase or uppercase SHA-256 hex digest")
    return cls(
      path=_absolute_path(source.get("path"), "input.path"),
      camera=camera,
      kind=_required_string(source.get("kind"), "input.kind"),
      input_format=input_format,
      segment_num=_integer(source.get("segment_num"), "input.segment_num", 0),
      artifact_id=_optional_string(source.get("artifact_id"), "input.artifact_id"),
      expected_sha256=expected_sha256,
    )


@dataclass(frozen=True)
class OutputPaths:
  video_path: str
  metadata_path: str
  poster_path: str | None = None
  thumbnails_dir: str | None = None
  frame_index_path: str | None = None

  @classmethod
  def from_dict(cls, value: Any) -> OutputPaths:
    outputs = _mapping(value, "outputs")
    _reject_unknown(outputs, {"video_path", "metadata_path", "poster_path", "thumbnails_dir", "frame_index_path"}, "outputs")
    video_path = _required_string(outputs.get("video_path"), "outputs.video_path")
    if Path(video_path).suffix.lower() != ".webm":
      raise ContractError("outputs.video_path must end in .webm")
    return cls(
      video_path=_absolute_path(video_path, "outputs.video_path"),
      metadata_path=_absolute_path(outputs.get("metadata_path"), "outputs.metadata_path"),
      poster_path=_optional_absolute_path(outputs.get("poster_path"), "outputs.poster_path"),
      thumbnails_dir=_optional_absolute_path(outputs.get("thumbnails_dir"), "outputs.thumbnails_dir"),
      frame_index_path=_optional_absolute_path(outputs.get("frame_index_path"), "outputs.frame_index_path"),
    )


@dataclass(frozen=True)
class EncodeOptions:
  encoder: str = "libsvtav1"
  preset: int = 10
  crf: int = 38
  logical_processors: int = 2
  pixel_format: str = "yuv420p"
  raw_hevc_frame_rate: int = 20
  keyframe_interval_seconds: int = 4
  thumbnail_count: int = 6
  thumbnail_width: int = 480
  preserve_audio: bool = True
  audio_bitrate_kbps: int = 32
  overwrite: bool = False

  @classmethod
  def from_dict(cls, value: Any) -> EncodeOptions:
    source = _mapping(value, "encode")
    allowed = {
      "encoder",
      "preset",
      "crf",
      "logical_processors",
      "pixel_format",
      "raw_hevc_frame_rate",
      "keyframe_interval_seconds",
      "thumbnail_count",
      "thumbnail_width",
      "preserve_audio",
      "audio_bitrate_kbps",
      "overwrite",
    }
    _reject_unknown(source, allowed, "encode")
    encoder = _required_string(source.get("encoder", "libsvtav1"), "encode.encoder")
    if encoder != "libsvtav1":
      raise ContractError("encode.encoder must be libsvtav1")
    pixel_format = _required_string(source.get("pixel_format", "yuv420p"), "encode.pixel_format")
    if pixel_format != "yuv420p":
      raise ContractError("encode.pixel_format must be yuv420p for browser compatibility")
    overwrite = _boolean(source.get("overwrite", False), "encode.overwrite")
    if overwrite:
      raise ContractError("encode.overwrite must be false for crash-safe publication")
    return cls(
      encoder=encoder,
      preset=_integer(source.get("preset", 10), "encode.preset", 0, 13),
      crf=_integer(source.get("crf", 38), "encode.crf", 0, 63),
      logical_processors=_integer(source.get("logical_processors", 2), "encode.logical_processors", 1, 256),
      pixel_format=pixel_format,
      raw_hevc_frame_rate=_integer(source.get("raw_hevc_frame_rate", 20), "encode.raw_hevc_frame_rate", 1, 240),
      keyframe_interval_seconds=_integer(source.get("keyframe_interval_seconds", 4), "encode.keyframe_interval_seconds", 1, 60),
      thumbnail_count=_integer(source.get("thumbnail_count", 6), "encode.thumbnail_count", 0, 100),
      thumbnail_width=_integer(source.get("thumbnail_width", 480), "encode.thumbnail_width", 64, 4096),
      preserve_audio=_boolean(source.get("preserve_audio", True), "encode.preserve_audio"),
      audio_bitrate_kbps=_integer(source.get("audio_bitrate_kbps", 32), "encode.audio_bitrate_kbps", 16, 128),
      overwrite=overwrite,
    )


@dataclass(frozen=True)
class Limits:
  timeout_seconds: int = 14_400
  minimum_free_bytes: int = 2 * 1024**3
  working_space_multiplier: float = 1.25
  cancel_file: str | None = None

  @classmethod
  def from_dict(cls, value: Any) -> Limits:
    source = _mapping(value, "limits")
    _reject_unknown(source, {"timeout_seconds", "minimum_free_bytes", "working_space_multiplier", "cancel_file"}, "limits")
    return cls(
      timeout_seconds=_integer(source.get("timeout_seconds", 14_400), "limits.timeout_seconds", 1),
      minimum_free_bytes=_integer(source.get("minimum_free_bytes", 2 * 1024**3), "limits.minimum_free_bytes", 0),
      working_space_multiplier=_number(source.get("working_space_multiplier", 1.25), "limits.working_space_multiplier", 1.0, 10.0),
      cancel_file=_optional_string(source.get("cancel_file"), "limits.cancel_file"),
    )


@dataclass(frozen=True)
class TimeMappingReference:
  path: str | None = None
  artifact_id: str | None = None
  sha256: str | None = None
  version: str | None = None

  @classmethod
  def from_dict(cls, value: Any) -> TimeMappingReference | None:
    if value is None:
      return None
    source = _mapping(value, "time_mapping")
    _reject_unknown(source, {"path", "artifact_id", "sha256", "version"}, "time_mapping")
    result = cls(
      path=_optional_string(source.get("path"), "time_mapping.path"),
      artifact_id=_optional_string(source.get("artifact_id"), "time_mapping.artifact_id"),
      sha256=_optional_string(source.get("sha256"), "time_mapping.sha256"),
      version=_optional_string(source.get("version"), "time_mapping.version"),
    )
    if not any(asdict(result).values()):
      raise ContractError("time_mapping must contain at least one reference field")
    if result.sha256 is not None:
      digest = result.sha256.lower()
      if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ContractError("time_mapping.sha256 must be a SHA-256 hex digest")
      return cls(path=result.path, artifact_id=result.artifact_id, sha256=digest, version=result.version)
    return result


@dataclass(frozen=True)
class EncodeJob:
  job_id: str
  input: InputArtifact
  outputs: OutputPaths
  encode: EncodeOptions = field(default_factory=EncodeOptions)
  limits: Limits = field(default_factory=Limits)
  time_mapping: TimeMappingReference | None = None
  retain_raw: bool = True
  schema_version: int = SCHEMA_VERSION

  @classmethod
  def from_dict(cls, value: Any) -> EncodeJob:
    source = _mapping(value, "job")
    _reject_unknown(
      source,
      {"schema_version", "job_id", "input", "outputs", "encode", "limits", "time_mapping", "retain_raw"},
      "job",
    )
    schema_version = _integer(source.get("schema_version"), "schema_version", 1)
    if schema_version != SCHEMA_VERSION:
      raise ContractError(f"unsupported schema_version {schema_version}; expected {SCHEMA_VERSION}")
    return cls(
      schema_version=schema_version,
      job_id=_required_string(source.get("job_id"), "job_id"),
      input=InputArtifact.from_dict(source.get("input")),
      outputs=OutputPaths.from_dict(source.get("outputs")),
      encode=EncodeOptions.from_dict(source.get("encode", {})),
      limits=Limits.from_dict(source.get("limits", {})),
      time_mapping=TimeMappingReference.from_dict(source.get("time_mapping")),
      retain_raw=_boolean(source.get("retain_raw", True), "retain_raw"),
    )

  def as_dict(self) -> dict[str, Any]:
    return asdict(self)
