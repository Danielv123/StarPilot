from __future__ import annotations

import collections
import copy
import hashlib
import itertools
import json
import math
import os
import queue
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from .contract import EncodeJob

ProgressCallback = Callable[[dict[str, Any]], None]
FRAME_INDEX_SCHEMA_VERSION = 1
PUBLISH_TRANSACTION_SCHEMA_VERSION = 1
LOCAL_PROTOCOL_WHITELIST = "file,pipe"
PROBE_SIZE_BYTES = 8 * 1024 * 1024
ANALYZE_DURATION_US = 5_000_000
MAX_PROBE_PACKETS = 4_096
MAX_INPUT_BYTES = 1024**3
MAX_OUTPUT_BYTES = 1024**3
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 256 * 1024 * 1024
MAX_AUXILIARY_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_WIDTH = 4_096
MAX_HEIGHT = 2_160
MAX_PIXELS = MAX_WIDTH * MAX_HEIGHT
MAX_FRAMES = 10_000
MAX_DURATION_SECONDS = 180.0
MAX_FRAME_RATE = 60.0
MAX_VIDEO_STREAMS = 1
MAX_AUDIO_STREAMS = 1
MAX_AUDIO_CHANNELS = 2
MAX_AUDIO_SAMPLE_RATE = 48_000
MIN_AUDIO_SAMPLE_RATE = 8_000
MAX_STREAM_DURATION_SKEW_SECONDS = 1.0
HARD_PROBE_TIMEOUT_SECONDS = 120.0
MIN_AUDIO_FRAME_DURATION_SECONDS = 0.0025
MAX_AUDIO_FRAMES = math.ceil(MAX_DURATION_SECONDS / MIN_AUDIO_FRAME_DURATION_SECONDS)
MAX_SCAN_PACKETS = MAX_FRAMES + MAX_AUDIO_FRAMES + 1_024
MAX_SINGLE_ALLOCATION_BYTES = 512 * 1024 * 1024
MAX_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_LINE_CHARS = 64 * 1024
MAX_CAPTURE_QUEUE_LINES = 256
MAX_PUBLISH_TRANSACTION_BYTES = 1024 * 1024
BITRATE_POLICY_VERSION = 1
BITRATE_TARGET_RATIO = Fraction(4, 5)
BITRATE_FALLBACK_RATIO = Fraction(3, 5)
BITRATE_CONTAINER_RESERVE_RATIO = Fraction(1, 20)
BITRATE_AUDIO_RESERVE_RATIO = Fraction(5, 4)
MIN_CONTAINER_RESERVE_BPS = 8_000
MIN_VIDEO_MAXRATE_BPS = 1_000
DEMUXERS = {
  "raw_hevc": "hevc",
  "mpegts": "mpegts",
  "webm": "matroska",
}
CODEC_WHITELISTS = {
  "raw_hevc": "hevc",
  "mpegts": "h264,aac,opus",
  "webm": "av1,libdav1d,opus",
}
MAX_STREAMS_BY_FORMAT = {
  "raw_hevc": 1,
  "mpegts": 2,
  "webm": 2,
}


class MediaWorkerError(RuntimeError):
  code = "media_worker_error"


class ToolUnavailableError(MediaWorkerError):
  code = "tool_unavailable"


class ProbeError(MediaWorkerError):
  code = "probe_failed"


class CancelledError(MediaWorkerError):
  code = "cancelled"


class TimeoutError(MediaWorkerError):
  code = "timeout"


class DiskSpaceError(MediaWorkerError):
  code = "insufficient_disk_space"


class OutputConflictError(MediaWorkerError):
  code = "output_conflict"


class OutputValidationError(MediaWorkerError):
  code = "output_validation_failed"


class BitrateReductionError(OutputValidationError):
  code = "bitrate_reduction_failed"


class InputIntegrityError(MediaWorkerError):
  code = "input_integrity_failed"


class InputValidationError(MediaWorkerError):
  code = "input_validation_failed"


@dataclass(frozen=True)
class ProbeInfo:
  path: str
  format_name: str
  codec_name: str
  codec_type: str
  width: int
  height: int
  pixel_format: str | None
  average_frame_rate: float | None
  reported_frame_rate: float | None
  duration_seconds: float | None
  frame_count: int | None
  size_bytes: int
  raw_elementary_stream: bool
  audio_codec_name: str | None = None
  audio_channels: int | None = None
  audio_sample_rate: int | None = None
  time_base: str | None = None
  video_stream_count: int = 1
  audio_stream_count: int = 0
  other_stream_count: int = 0
  video_duration_seconds: float | None = None
  audio_duration_seconds: float | None = None
  container_duration_seconds: float | None = None
  audio_time_base: str | None = None
  max_frame_width: int | None = None
  max_frame_height: int | None = None
  scanned_audio_channels_max: int | None = None
  scanned_audio_sample_rate_min: int | None = None
  scanned_audio_sample_rate_max: int | None = None
  scan_packet_count: int | None = None

  def as_dict(self) -> dict[str, Any]:
    return asdict(self)

  def effective_duration(self, raw_hevc_frame_rate: int = 20) -> float | None:
    if self.raw_elementary_stream and self.codec_name == "hevc" and self.frame_count:
      return self.frame_count / raw_hevc_frame_rate
    return self.duration_seconds


@dataclass(frozen=True)
class BitrateBudget:
  attempt: int
  total_ratio: Fraction
  input_duration_us: int
  input_total_bitrate_bps: int
  target_total_bitrate_bps: int
  reserved_audio_bitrate_bps: int
  reserved_container_bitrate_bps: int
  video_maxrate_bps: int

  def as_dict(self) -> dict[str, Any]:
    return {
      "attempt": self.attempt,
      "target_ratio": {
        "numerator": self.total_ratio.numerator,
        "denominator": self.total_ratio.denominator,
        "decimal": float(self.total_ratio),
      },
      "input_duration_us": self.input_duration_us,
      "input_total_bitrate_bps": self.input_total_bitrate_bps,
      "target_total_bitrate_bps": self.target_total_bitrate_bps,
      "reserved_audio_bitrate_bps": self.reserved_audio_bitrate_bps,
      "reserved_container_bitrate_bps": self.reserved_container_bitrate_bps,
      "video_maxrate_bps": self.video_maxrate_bps,
    }


@dataclass(frozen=True)
class ValidationResult:
  probe: ProbeInfo
  cues_front_loaded: bool
  decoded: bool

  def as_dict(self) -> dict[str, Any]:
    return {
      "probe": self.probe.as_dict(),
      "cues_front_loaded": self.cues_front_loaded,
      "decoded": self.decoded,
    }


@dataclass(frozen=True)
class ToolVersions:
  ffmpeg: str
  ffprobe: str

  def as_dict(self) -> dict[str, str]:
    return asdict(self)


@dataclass(frozen=True)
class FrameScan:
  video_frame_count: int
  video_duration_seconds: float | None
  audio_duration_seconds: float | None
  max_frame_width: int
  max_frame_height: int
  audio_channels_max: int | None
  audio_sample_rate_min: int | None
  audio_sample_rate_max: int | None
  packet_count: int


@dataclass(frozen=True)
class _FileIdentity:
  device: int
  inode: int
  size_bytes: int
  mtime_ns: int


def _utc_now() -> str:
  return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_fraction(value: Any) -> float | None:
  if value in (None, "", "N/A", "0/0"):
    return None
  try:
    if isinstance(value, str) and "/" in value:
      numerator, denominator = value.split("/", 1)
      denominator_value = float(denominator)
      return float(numerator) / denominator_value if denominator_value else None
    return float(value)
  except (TypeError, ValueError, ZeroDivisionError):
    return None


def _parse_int(value: Any) -> int | None:
  if value in (None, "", "N/A"):
    return None
  try:
    return int(value)
  except (TypeError, ValueError):
    return None


def _parse_float(value: Any) -> float | None:
  if value in (None, "", "N/A"):
    return None
  try:
    result = float(value)
    return result if math.isfinite(result) else None
  except (TypeError, ValueError):
    return None


def _time_base(value: Any) -> Fraction:
  if not isinstance(value, str):
    raise OutputValidationError("video stream has no exact time base")
  try:
    result = Fraction(value)
  except (ValueError, ZeroDivisionError) as exc:
    raise OutputValidationError(f"video stream has an invalid time base: {value}") from exc
  if result <= 0:
    raise OutputValidationError(f"video stream has a non-positive time base: {value}")
  return result


def _ticks_to_microseconds(value: int, time_base: Fraction) -> int:
  return _round_fraction(value * time_base * 1_000_000)


def _round_fraction(value: Fraction) -> int:
  scaled = value
  if scaled >= 0:
    return (scaled.numerator * 2 + scaled.denominator) // (scaled.denominator * 2)
  positive = -scaled
  return -((positive.numerator * 2 + positive.denominator) // (positive.denominator * 2))


def _ceil_fraction(value: Fraction) -> int:
  return -(-value.numerator // value.denominator)


def _effective_duration_us(info: ProbeInfo, raw_hevc_frame_rate: int) -> int | None:
  if info.raw_elementary_stream and info.codec_name == "hevc" and info.frame_count:
    duration_us = _round_fraction(Fraction(info.frame_count * 1_000_000, raw_hevc_frame_rate))
  else:
    duration = info.effective_duration(raw_hevc_frame_rate)
    duration_us = round(duration * 1_000_000) if duration is not None and math.isfinite(duration) else 0
  return duration_us if duration_us > 0 else None


def _average_bitrate_bps(size_bytes: int, duration_us: int) -> int:
  if size_bytes <= 0 or duration_us <= 0:
    raise ValueError("size and duration must be positive")
  return _ceil_fraction(Fraction(size_bytes * 8 * 1_000_000, duration_us))


def _nearest_existing_parent(path: Path) -> Path:
  candidate = path
  while not candidate.exists():
    if candidate.parent == candidate:
      raise DiskSpaceError(f"no existing parent found for {path}")
    candidate = candidate.parent
  return candidate


def _stage_path(target: Path) -> Path:
  return target.with_name(f".{target.name}.{uuid.uuid4().hex}.part{target.suffix}")


def _is_stage_path_for(stage: Path, target: Path) -> bool:
  if not stage.is_absolute() or stage.parent != target.parent:
    return False
  prefix = f".{target.name}."
  suffix = f".part{target.suffix}"
  if not stage.name.startswith(prefix) or not stage.name.endswith(suffix):
    return False
  token = stage.name[len(prefix) : -len(suffix)]
  return len(token) == 32 and all(character in "0123456789abcdef" for character in token)


def _fsync_file(path: Path) -> None:
  with path.open("r+b") as stream:
    os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
  try:
    descriptor = os.open(path, os.O_RDONLY)
  except OSError:
    return
  try:
    os.fsync(descriptor)
  except OSError:
    pass
  finally:
    os.close(descriptor)


def _write_json_durable(path: Path, payload: dict[str, Any]) -> None:
  serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
  with path.open("x", encoding="utf-8", newline="\n") as stream:
    stream.write(serialized)
    stream.flush()
    os.fsync(stream.fileno())


def _file_identity(file_stat: os.stat_result) -> _FileIdentity:
  return _FileIdentity(
    device=file_stat.st_dev,
    inode=file_stat.st_ino,
    size_bytes=file_stat.st_size,
    mtime_ns=file_stat.st_mtime_ns,
  )


def _stat_matches_identity(file_stat: os.stat_result, identity: _FileIdentity) -> bool:
  return stat.S_ISREG(file_stat.st_mode) and _file_identity(file_stat) == identity


def _unlink_verified_identity(path: Path, identity: _FileIdentity, *, description: str) -> None:
  try:
    current = path.stat(follow_symlinks=False)
  except OSError as exc:
    raise OutputConflictError(f"{description} changed after verification: {path}") from exc
  if not _stat_matches_identity(current, identity):
    raise OutputConflictError(f"{description} changed after verification: {path}")
  path.unlink()


def _sha256(path: Path, *, deadline: float, cancelled: Callable[[], bool]) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    while chunk := stream.read(4 * 1024 * 1024):
      if cancelled():
        raise CancelledError("job cancelled while hashing")
      if time.monotonic() >= deadline:
        raise TimeoutError("job timed out while hashing")
      digest.update(chunk)
  return digest.hexdigest()


def _webm_cues_front_loaded(path: Path, scan_bytes: int = 16 * 1024 * 1024) -> bool:
  cues_id = b"\x1c\x53\xbb\x6b"
  cluster_id = b"\x1f\x43\xb6\x75"
  with path.open("rb") as stream:
    prefix = stream.read(scan_bytes)
  cues_offset = prefix.find(cues_id)
  cluster_offset = prefix.find(cluster_id)
  return cues_offset >= 0 and cluster_offset >= 0 and cues_offset < cluster_offset


def _checked_file_size(path: Path) -> int:
  if not path.is_file():
    raise ProbeError(f"input is not a regular file: {path}")
  try:
    size = path.stat().st_size
  except OSError as exc:
    raise ProbeError(f"input could not be inspected: {path}: {exc}") from exc
  if size <= 0:
    raise ProbeError(f"input is empty: {path}")
  if size > MAX_INPUT_BYTES:
    raise ProbeError(f"input is {size} bytes; the worker limit is {MAX_INPUT_BYTES} bytes")
  return size


def _input_guard_args(input_format: str) -> list[str]:
  try:
    demuxer = DEMUXERS[input_format]
    codec_whitelist = CODEC_WHITELISTS[input_format]
    max_streams = MAX_STREAMS_BY_FORMAT[input_format]
  except KeyError as exc:
    raise ProbeError(f"unsupported forced input format: {input_format}") from exc
  return [
    "-protocol_whitelist",
    LOCAL_PROTOCOL_WHITELIST,
    "-f",
    demuxer,
    "-codec_whitelist",
    codec_whitelist,
    "-max_pixels",
    str(MAX_PIXELS),
    "-max_streams",
    str(max_streams),
    "-max_alloc",
    str(MAX_SINGLE_ALLOCATION_BYTES),
    "-probesize",
    str(PROBE_SIZE_BYTES),
    "-analyzeduration",
    str(ANALYZE_DURATION_US),
    "-max_probe_packets",
    str(MAX_PROBE_PACKETS),
  ]


class MediaWorker:
  def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
    self.ffmpeg = ffmpeg
    self.ffprobe = ffprobe

  def tool_versions(self) -> ToolVersions:
    return ToolVersions(ffmpeg=self._version_line(self.ffmpeg), ffprobe=self._version_line(self.ffprobe))

  def _version_line(self, binary: str) -> str:
    try:
      completed = subprocess.run(
        [binary, "-version"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
      )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
      raise ToolUnavailableError(f"{binary} is unavailable: {exc}") from exc
    if completed.returncode != 0:
      raise ToolUnavailableError(f"{binary} -version failed: {completed.stderr.strip()}")
    return (completed.stdout.splitlines() or [binary])[0].strip()

  def ensure_encoder(self, *, require_opus: bool = False) -> None:
    try:
      completed = subprocess.run(
        [self.ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=20,
        check=False,
      )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
      raise ToolUnavailableError(f"{self.ffmpeg} is unavailable: {exc}") from exc
    if completed.returncode != 0 or "libsvtav1" not in completed.stdout:
      raise ToolUnavailableError("ffmpeg does not provide the required libsvtav1 encoder")
    if require_opus and "libopus" not in completed.stdout:
      raise ToolUnavailableError("ffmpeg does not provide the required libopus encoder for qcamera audio")

  def _run_capture(
    self,
    command: list[str],
    *,
    phase: str,
    deadline: float,
    cancelled: Callable[[], bool],
  ) -> subprocess.CompletedProcess[str]:
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
      process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name != "nt",
        creationflags=creation_flags,
      )
    except FileNotFoundError as exc:
      raise ToolUnavailableError(f"executable is unavailable: {command[0]}") from exc

    chunks: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=MAX_CAPTURE_QUEUE_LINES)

    def drain(name: str, stream: Any) -> None:
      try:
        while chunk := stream.read(MAX_CAPTURE_LINE_CHARS):
          chunks.put((name, chunk))
      finally:
        chunks.put((name, None))

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
      threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
      threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
      reader.start()

    stopped_for: str | None = None
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    captured_bytes = 0
    closed_streams = 0
    while closed_streams < 2 or process.poll() is None:
      if stopped_for is None and cancelled():
        stopped_for = "cancelled"
        self._stop_process(process)
      if stopped_for is None and time.monotonic() >= deadline:
        stopped_for = "timeout"
        self._stop_process(process)
      try:
        stream_name, chunk = chunks.get(timeout=0.1)
      except queue.Empty:
        continue
      if chunk is None:
        closed_streams += 1
        continue
      if stopped_for is not None:
        continue
      captured_bytes += len(chunk.encode("utf-8", errors="replace"))
      if captured_bytes > MAX_CAPTURE_BYTES:
        stopped_for = "capture_limit"
        self._stop_process(process)
        continue
      (stdout_parts if stream_name == "stdout" else stderr_parts).append(chunk)

    try:
      return_code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
      self._stop_process(process, force=True)
      return_code = process.wait(timeout=5)
    for reader in readers:
      reader.join(timeout=1)

    if stopped_for == "cancelled":
      raise CancelledError(f"{phase} cancelled")
    if stopped_for == "timeout":
      raise TimeoutError(f"{phase} exceeded the hard probe timeout")
    if stopped_for == "capture_limit":
      raise ProbeError(f"{phase} emitted more than the {MAX_CAPTURE_BYTES}-byte capture limit")

    return subprocess.CompletedProcess(command, return_code, "".join(stdout_parts), "".join(stderr_parts))

  def _validate_header_topology(
    self,
    input_format: str,
    format_name: str,
    video_streams: list[dict[str, Any]],
    audio_streams: list[dict[str, Any]],
    other_streams: list[dict[str, Any]],
  ) -> None:
    if len(video_streams) != MAX_VIDEO_STREAMS:
      raise InputValidationError(f"expected exactly one video stream, found {len(video_streams)}")
    if other_streams:
      raise InputValidationError(f"unexpected non-audio/video streams: {len(other_streams)}")

    format_tokens = {item.strip() for item in format_name.split(",")}
    video = video_streams[0]
    video_codec = str(video.get("codec_name", ""))
    width = _parse_int(video.get("width")) or 0
    height = _parse_int(video.get("height")) or 0
    if width <= 0 or height <= 0:
      raise InputValidationError("input video dimensions are missing or non-positive")
    if width > MAX_WIDTH or height > MAX_HEIGHT or width * height > MAX_PIXELS:
      raise InputValidationError(f"input dimensions {width}x{height} exceed {MAX_WIDTH}x{MAX_HEIGHT}/{MAX_PIXELS} pixels")

    if input_format == "raw_hevc":
      if format_tokens != {"hevc"} or video_codec != "hevc":
        raise InputValidationError(f"raw_hevc requires an HEVC elementary stream, got {format_name}/{video_codec}")
      if audio_streams:
        raise InputValidationError("raw_hevc input must not contain audio")
      return

    if input_format == "mpegts":
      if "mpegts" not in format_tokens or video_codec != "h264":
        raise InputValidationError(f"mpegts requires H.264 video, got {format_name}/{video_codec}")
    elif input_format == "webm":
      if "webm" not in format_tokens or video_codec != "av1":
        raise InputValidationError(f"webm validation requires AV1 video, got {format_name}/{video_codec}")
    else:
      raise ProbeError(f"unsupported forced input format: {input_format}")

    if len(audio_streams) > MAX_AUDIO_STREAMS:
      raise InputValidationError(f"{input_format} has {len(audio_streams)} audio streams; at most one is allowed")
    if not audio_streams:
      return
    audio = audio_streams[0]
    expected_audio_codecs = {"aac", "opus"} if input_format == "mpegts" else {"opus"}
    audio_codec = str(audio.get("codec_name", ""))
    if audio_codec not in expected_audio_codecs:
      raise InputValidationError(f"{input_format} audio codec is not allowed: {audio_codec}")
    channels = _parse_int(audio.get("channels"))
    sample_rate = _parse_int(audio.get("sample_rate"))
    if channels is None or not 1 <= channels <= MAX_AUDIO_CHANNELS:
      raise InputValidationError(f"{input_format} audio channel count is not allowed: {channels}")
    if sample_rate is None or not MIN_AUDIO_SAMPLE_RATE <= sample_rate <= MAX_AUDIO_SAMPLE_RATE:
      raise InputValidationError(f"{input_format} audio sample rate is not allowed: {sample_rate}")

  def _scan_decoded_frames(
    self,
    target: Path,
    *,
    input_format: str,
    video_stream: dict[str, Any],
    audio_stream: dict[str, Any] | None,
    deadline: float,
    cancelled: Callable[[], bool],
  ) -> FrameScan:
    def stream_index(stream: dict[str, Any], label: str) -> int:
      result = _parse_int(stream.get("index"))
      if result is None or result < 0:
        raise InputValidationError(f"{label} stream has no valid index")
      return result

    def stream_time_base(stream: dict[str, Any], label: str) -> Fraction:
      value = stream.get("time_base")
      if not isinstance(value, str):
        raise InputValidationError(f"{label} stream has no exact time base")
      try:
        result = Fraction(value)
      except (ValueError, ZeroDivisionError) as exc:
        raise InputValidationError(f"{label} stream has an invalid time base: {value}") from exc
      if result <= 0:
        raise InputValidationError(f"{label} stream has a non-positive time base: {value}")
      return result

    video_index = stream_index(video_stream, "video")
    video_time_base = stream_time_base(video_stream, "video")
    audio_index = stream_index(audio_stream, "audio") if audio_stream is not None else None
    audio_time_base = stream_time_base(audio_stream, "audio") if audio_stream is not None else None
    audio_codec = str(audio_stream.get("codec_name", "")) if audio_stream is not None else None
    audio_declared_sample_rate = _parse_int(audio_stream.get("sample_rate")) if audio_stream is not None else None
    command = [
      self.ffprobe,
      "-v",
      "error",
      *_input_guard_args(input_format),
      "-show_packets",
      "-show_frames",
      "-show_entries",
      ("packet=stream_index:frame=media_type,stream_index,pts,best_effort_timestamp,duration,pkt_duration,width,height,channels,nb_samples"),
      "-of",
      "compact=p=1:nk=0",
      "-read_intervals",
      f"%+#{MAX_SCAN_PACKETS + 1}",
      "-i",
      str(target),
    ]
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
      process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=os.name != "nt",
        creationflags=creation_flags,
      )
    except FileNotFoundError as exc:
      raise ToolUnavailableError(f"{self.ffprobe} is unavailable") from exc

    lines: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=MAX_CAPTURE_QUEUE_LINES)
    stderr_tail: collections.deque[str] = collections.deque(maxlen=200)

    def drain(name: str, stream: Any) -> None:
      try:
        while line := stream.readline(MAX_CAPTURE_LINE_CHARS + 1):
          if len(line) > MAX_CAPTURE_LINE_CHARS:
            lines.put(("overflow", line[:MAX_CAPTURE_LINE_CHARS]))
          else:
            lines.put((name, line.rstrip("\r\n")))
      finally:
        lines.put((name, None))

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
      threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
      threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
      reader.start()

    packet_count = 0
    video_frame_count = 0
    audio_frame_count = 0
    max_frame_width = 0
    max_frame_height = 0
    audio_channels_max: int | None = None
    audio_sample_rates: list[int] = []
    video_duration_sum = Fraction(0)
    audio_duration_sum = Fraction(0)
    video_pts_bounds: tuple[int, int] | None = None
    audio_pts_bounds: tuple[int, int] | None = None
    closed_streams = 0
    stopped_for: str | None = None
    violation: MediaWorkerError | None = None

    def update_bounds(bounds: tuple[int, int] | None, pts: int | None, duration: int | None) -> tuple[int, int] | None:
      if pts is None or duration is None or duration <= 0:
        return bounds
      end = pts + duration
      if bounds is None:
        return (pts, end)
      return (min(bounds[0], pts), max(bounds[1], end))

    while closed_streams < 2 or process.poll() is None:
      if stopped_for is None and violation is None and cancelled():
        stopped_for = "cancelled"
        self._stop_process(process)
      if stopped_for is None and violation is None and time.monotonic() >= deadline:
        stopped_for = "timeout"
        self._stop_process(process)
      try:
        stream_name, line = lines.get(timeout=0.1)
      except queue.Empty:
        continue
      if line is None:
        closed_streams += 1
        continue
      if stream_name == "overflow" and violation is None:
        violation = ProbeError(f"ffprobe emitted a line longer than {MAX_CAPTURE_LINE_CHARS} characters")
        self._stop_process(process)
        continue
      if stopped_for is not None or violation is not None:
        continue
      if stream_name == "stderr":
        stderr_tail.append(line[-4_000:])
        continue
      try:
        if line.startswith("packet|"):
          packet_count += 1
          if packet_count > MAX_SCAN_PACKETS:
            raise InputValidationError(f"input packet count exceeds the bounded scan limit of {MAX_SCAN_PACKETS}")
          continue
        if not line.startswith("frame|"):
          continue
        values = {}
        for field in line.split("|")[1:]:
          if "=" in field:
            key, value = field.split("=", 1)
            values[key] = value
        media_type = values.get("media_type")
        index = _parse_int(values.get("stream_index"))
        duration = _parse_int(values.get("duration"))
        if duration is None:
          duration = _parse_int(values.get("pkt_duration"))
        pts = _parse_int(values.get("pts"))
        if pts is None:
          pts = _parse_int(values.get("best_effort_timestamp"))
        if media_type == "video":
          if index != video_index:
            raise InputValidationError(f"decoded unexpected video stream index: {index}")
          video_frame_count += 1
          if video_frame_count > MAX_FRAMES:
            raise InputValidationError(f"input frame count exceeds the limit of {MAX_FRAMES}")
          width = _parse_int(values.get("width")) or 0
          height = _parse_int(values.get("height")) or 0
          if width <= 0 or height <= 0:
            raise InputValidationError(f"decoded video frame {video_frame_count - 1} has invalid dimensions")
          if width > MAX_WIDTH or height > MAX_HEIGHT or width * height > MAX_PIXELS:
            raise InputValidationError(
              f"decoded video frame {video_frame_count - 1} dimensions {width}x{height} exceed {MAX_WIDTH}x{MAX_HEIGHT}/{MAX_PIXELS} pixels"
            )
          max_frame_width = max(max_frame_width, width)
          max_frame_height = max(max_frame_height, height)
          if duration is not None and duration > 0:
            video_duration_sum += duration * video_time_base
          video_pts_bounds = update_bounds(video_pts_bounds, pts, duration)
        elif media_type == "audio":
          if audio_index is None or audio_time_base is None or index != audio_index:
            raise InputValidationError(f"decoded unexpected audio stream index: {index}")
          audio_frame_count += 1
          if audio_frame_count > MAX_AUDIO_FRAMES:
            raise InputValidationError(f"input audio frame count exceeds the limit of {MAX_AUDIO_FRAMES}")
          channels = _parse_int(values.get("channels"))
          samples = _parse_int(values.get("nb_samples"))
          if channels is None or not 1 <= channels <= MAX_AUDIO_CHANNELS:
            raise InputValidationError(f"decoded audio frame {audio_frame_count - 1} has invalid channel count: {channels}")
          if samples is None or samples <= 0 or duration is None or duration <= 0:
            raise InputValidationError(f"decoded audio frame {audio_frame_count - 1} has invalid sample timing")
          if audio_codec == "opus":
            sample_rate = audio_declared_sample_rate
            if sample_rate is None or not MIN_AUDIO_SAMPLE_RATE <= sample_rate <= MAX_AUDIO_SAMPLE_RATE:
              raise InputValidationError(f"decoded audio frame {audio_frame_count - 1} sample rate is not allowed: {sample_rate}")
          else:
            exact_sample_rate = Fraction(samples, 1) / (duration * audio_time_base)
            sample_rate = round(float(exact_sample_rate))
            if exact_sample_rate < MIN_AUDIO_SAMPLE_RATE or exact_sample_rate > MAX_AUDIO_SAMPLE_RATE:
              raise InputValidationError(f"decoded audio frame {audio_frame_count - 1} sample rate is not allowed: {float(exact_sample_rate):.3f}")
          audio_channels_max = max(audio_channels_max or 0, channels)
          audio_sample_rates.append(sample_rate)
          audio_duration_sum += duration * audio_time_base
          audio_pts_bounds = update_bounds(audio_pts_bounds, pts, duration)
      except MediaWorkerError as exc:
        violation = exc
        self._stop_process(process)
        continue

    try:
      return_code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
      self._stop_process(process, force=True)
      return_code = process.wait(timeout=5)
    for reader in readers:
      reader.join(timeout=1)

    if stopped_for == "cancelled":
      raise CancelledError("input frame scan cancelled")
    if stopped_for == "timeout":
      raise TimeoutError("input frame scan exceeded the hard probe timeout")
    if violation is not None:
      raise violation
    if return_code != 0:
      error = "\n".join(stderr_tail).strip()
      raise ProbeError(f"ffprobe rejected {target} during bounded frame scan: {error[-2000:] or 'unknown error'}")
    if video_frame_count <= 0:
      raise InputValidationError("input contains no decodable video frames")
    if audio_stream is not None and audio_frame_count <= 0:
      raise InputValidationError("declared audio stream contains no decodable audio frames")

    def scanned_duration(total: Fraction, bounds: tuple[int, int] | None, time_base: Fraction) -> float | None:
      candidates = [total]
      if bounds is not None:
        candidates.append((bounds[1] - bounds[0]) * time_base)
      positive = [candidate for candidate in candidates if candidate > 0]
      return float(max(positive)) if positive else None

    return FrameScan(
      video_frame_count=video_frame_count,
      video_duration_seconds=scanned_duration(video_duration_sum, video_pts_bounds, video_time_base),
      audio_duration_seconds=(scanned_duration(audio_duration_sum, audio_pts_bounds, audio_time_base) if audio_time_base is not None else None),
      max_frame_width=max_frame_width,
      max_frame_height=max_frame_height,
      audio_channels_max=audio_channels_max,
      audio_sample_rate_min=min(audio_sample_rates) if audio_sample_rates else None,
      audio_sample_rate_max=max(audio_sample_rates) if audio_sample_rates else None,
      packet_count=packet_count,
    )

  def probe(
    self,
    path: str | Path,
    *,
    input_format: str,
    timeout_seconds: float = 120,
    cancel_event: threading.Event | None = None,
    cancel_file: str | None = None,
  ) -> ProbeInfo:
    if input_format not in DEMUXERS:
      raise ProbeError(f"unsupported forced input format: {input_format}")
    target = Path(path).resolve()
    source_size = _checked_file_size(target)
    deadline = time.monotonic() + min(max(0.001, timeout_seconds), HARD_PROBE_TIMEOUT_SECONDS)
    cancelled = self._cancelled(cancel_event, cancel_file)
    if cancelled():
      raise CancelledError("input probe cancelled")
    command = [
      self.ffprobe,
      "-v",
      "error",
      *_input_guard_args(input_format),
      "-show_entries",
      (
        "format=format_name,duration,size:"
        "stream=index,codec_name,codec_type,width,height,pix_fmt,avg_frame_rate,r_frame_rate,duration,nb_frames,channels,sample_rate,time_base:"
        "program_stream="
      ),
      "-of",
      "json",
      "-i",
      str(target),
    ]
    completed = self._run_capture(command, phase="input header probe", deadline=deadline, cancelled=cancelled)
    if completed.returncode != 0:
      error = completed.stderr.strip()[-2000:]
      raise ProbeError(f"ffprobe rejected {target}: {error or 'unknown error'}")
    try:
      payload = json.loads(completed.stdout)
      streams = payload["streams"]
      if not isinstance(streams, list):
        raise TypeError
      video_streams = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
      audio_streams = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
      other_streams = [item for item in streams if not isinstance(item, dict) or item.get("codec_type") not in {"video", "audio"}]
      stream = video_streams[0]
      audio_stream = audio_streams[0] if audio_streams else None
      format_info = payload.get("format", {})
      if not isinstance(format_info, dict):
        raise TypeError
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
      raise ProbeError(f"ffprobe returned incomplete JSON for {target}") from exc

    format_name = str(format_info.get("format_name", ""))
    self._validate_header_topology(input_format, format_name, video_streams, audio_streams, other_streams)
    scan = self._scan_decoded_frames(
      target,
      input_format=input_format,
      video_stream=stream,
      audio_stream=audio_stream,
      deadline=deadline,
      cancelled=cancelled,
    )

    codec_name = str(stream.get("codec_name", ""))
    format_tokens = {token.strip() for token in format_name.split(",")}
    raw_elementary = input_format == "raw_hevc" and format_tokens == {"hevc"} and codec_name == "hevc"
    header_video_duration = _parse_float(stream.get("duration"))
    header_audio_duration = _parse_float(audio_stream.get("duration")) if audio_stream else None
    container_duration = _parse_float(format_info.get("duration"))
    video_durations = [duration for duration in (header_video_duration, scan.video_duration_seconds) if duration is not None and duration > 0]
    audio_durations = [duration for duration in (header_audio_duration, scan.audio_duration_seconds) if duration is not None and duration > 0]
    video_duration = max(video_durations) if video_durations else None
    audio_duration = max(audio_durations) if audio_durations else None
    all_durations = [duration for duration in (video_duration, audio_duration, container_duration) if duration is not None and duration > 0]
    duration = max(all_durations) if all_durations else None
    return ProbeInfo(
      path=str(target),
      format_name=format_name,
      codec_name=codec_name,
      codec_type=str(stream.get("codec_type", "")),
      width=_parse_int(stream.get("width")) or 0,
      height=_parse_int(stream.get("height")) or 0,
      pixel_format=stream.get("pix_fmt"),
      average_frame_rate=_parse_fraction(stream.get("avg_frame_rate")),
      reported_frame_rate=_parse_fraction(stream.get("r_frame_rate")),
      duration_seconds=duration,
      frame_count=scan.video_frame_count,
      size_bytes=source_size,
      raw_elementary_stream=raw_elementary,
      audio_codec_name=str(audio_stream.get("codec_name")) if audio_stream else None,
      audio_channels=_parse_int(audio_stream.get("channels")) if audio_stream else None,
      audio_sample_rate=_parse_int(audio_stream.get("sample_rate")) if audio_stream else None,
      time_base=str(stream.get("time_base")) if stream.get("time_base") else None,
      video_stream_count=len(video_streams),
      audio_stream_count=len(audio_streams),
      other_stream_count=len(other_streams),
      video_duration_seconds=video_duration,
      audio_duration_seconds=audio_duration,
      container_duration_seconds=container_duration,
      audio_time_base=(str(audio_stream.get("time_base")) if audio_stream and audio_stream.get("time_base") else None),
      max_frame_width=scan.max_frame_width,
      max_frame_height=scan.max_frame_height,
      scanned_audio_channels_max=scan.audio_channels_max,
      scanned_audio_sample_rate_min=scan.audio_sample_rate_min,
      scanned_audio_sample_rate_max=scan.audio_sample_rate_max,
      scan_packet_count=scan.packet_count,
    )

  def bitrate_budget(
    self,
    job: EncodeJob,
    source: ProbeInfo,
    *,
    attempt: int = 1,
    total_ratio: Fraction = BITRATE_TARGET_RATIO,
  ) -> BitrateBudget:
    input_duration_us = _effective_duration_us(source, job.encode.raw_hevc_frame_rate)
    if input_duration_us is None:
      raise InputValidationError("input has no positive effective duration for bitrate budgeting")
    input_total_bitrate_bps = _average_bitrate_bps(source.size_bytes, input_duration_us)
    target_total_bitrate_bps = math.floor(input_total_bitrate_bps * total_ratio)
    reserved_container_bitrate_bps = max(
      MIN_CONTAINER_RESERVE_BPS,
      _ceil_fraction(target_total_bitrate_bps * BITRATE_CONTAINER_RESERVE_RATIO),
    )
    preserves_audio = job.input.input_format == "mpegts" and job.encode.preserve_audio and source.audio_codec_name is not None
    reserved_audio_bitrate_bps = _ceil_fraction(job.encode.audio_bitrate_kbps * 1_000 * BITRATE_AUDIO_RESERVE_RATIO) if preserves_audio else 0
    video_maxrate_bps = target_total_bitrate_bps - reserved_audio_bitrate_bps - reserved_container_bitrate_bps
    if video_maxrate_bps < MIN_VIDEO_MAXRATE_BPS:
      raise InputValidationError(
        "input bitrate is too low for the lower-bitrate AV1 policy: "
        f"{input_total_bitrate_bps} bps input leaves {video_maxrate_bps} bps for video at target ratio "
        f"{total_ratio.numerator}/{total_ratio.denominator}"
      )
    return BitrateBudget(
      attempt=attempt,
      total_ratio=total_ratio,
      input_duration_us=input_duration_us,
      input_total_bitrate_bps=input_total_bitrate_bps,
      target_total_bitrate_bps=target_total_bitrate_bps,
      reserved_audio_bitrate_bps=reserved_audio_bitrate_bps,
      reserved_container_bitrate_bps=reserved_container_bitrate_bps,
      video_maxrate_bps=video_maxrate_bps,
    )

  def bitrate_record(
    self,
    job: EncodeJob,
    source: ProbeInfo,
    output: ProbeInfo,
    budget: BitrateBudget,
  ) -> dict[str, Any]:
    output_duration_us = _effective_duration_us(output, job.encode.raw_hevc_frame_rate)
    if output_duration_us is None:
      raise OutputValidationError("encoded output has no positive effective duration for bitrate validation")
    output_total_bitrate_bps = _average_bitrate_bps(output.size_bytes, output_duration_us)
    exact_ratio = Fraction(output_total_bitrate_bps, budget.input_total_bitrate_bps)
    bitrate_reduced = output_total_bitrate_bps < budget.input_total_bitrate_bps
    size_reduced = output.size_bytes < source.size_bytes
    return {
      "policy_version": BITRATE_POLICY_VERSION,
      "policy": "strictly_lower_total_average_bitrate",
      "initial_target_ratio": {
        "numerator": BITRATE_TARGET_RATIO.numerator,
        "denominator": BITRATE_TARGET_RATIO.denominator,
        "decimal": float(BITRATE_TARGET_RATIO),
      },
      "fallback_target_ratio": {
        "numerator": BITRATE_FALLBACK_RATIO.numerator,
        "denominator": BITRATE_FALLBACK_RATIO.denominator,
        "decimal": float(BITRATE_FALLBACK_RATIO),
      },
      "selected_budget": budget.as_dict(),
      "output_duration_us": output_duration_us,
      "output_total_bitrate_bps": output_total_bitrate_bps,
      "output_to_input_ratio": {
        "numerator": exact_ratio.numerator,
        "denominator": exact_ratio.denominator,
        "decimal": float(exact_ratio),
      },
      "bitrate_reduced": bitrate_reduced,
      "size_reduced": size_reduced,
      "accepted": bitrate_reduced and size_reduced,
    }

  @staticmethod
  def _publish_journal_path(metadata_target: Path) -> Path:
    return metadata_target.with_name(f".{metadata_target.name}.publishing.json")

  @staticmethod
  def _publication_targets(
    video_target: Path,
    metadata_target: Path,
    poster_target: Path,
    frame_index_target: Path,
    thumbnail_targets: list[Path],
  ) -> list[tuple[str, Path]]:
    return [
      ("poster", poster_target),
      *((f"thumbnail:{ordinal:03d}", target) for ordinal, target in enumerate(thumbnail_targets)),
      ("frame_index", frame_index_target),
      ("video", video_target),
      ("metadata", metadata_target),
    ]

  @staticmethod
  def _publish_contract(job: EncodeJob, source_sha256: str) -> dict[str, Any]:
    return {
      "schema_version": job.schema_version,
      "input": {
        "path": str(Path(job.input.path).resolve()),
        "artifact_id": job.input.artifact_id,
        "camera": job.input.camera,
        "kind": job.input.kind,
        "input_format": job.input.input_format,
        "segment_num": job.input.segment_num,
        "sha256": source_sha256,
      },
      "encode": asdict(job.encode),
      "time_mapping": asdict(job.time_mapping) if job.time_mapping is not None else None,
      "retain_raw": job.retain_raw,
    }

  def _publish_transaction_payload(
    self,
    job: EncodeJob,
    source_sha256: str,
    entries: list[dict[str, Any]],
  ) -> dict[str, Any]:
    return {
      "schema_version": PUBLISH_TRANSACTION_SCHEMA_VERSION,
      "transaction": "comma_companion_media_publish",
      "origin_job_id": job.job_id,
      "contract": self._publish_contract(job, source_sha256),
      "entries": entries,
    }

  def _load_publish_transaction(
    self,
    journal_path: Path,
    *,
    job: EncodeJob,
    source_sha256: str,
    expected_targets: list[tuple[str, Path]],
  ) -> tuple[list[dict[str, Any]], _FileIdentity]:
    try:
      initial_stat = journal_path.stat(follow_symlinks=False)
    except OSError as exc:
      raise OutputConflictError(f"publish transaction marker cannot be inspected: {journal_path}") from exc
    if not stat.S_ISREG(initial_stat.st_mode):
      raise OutputConflictError(f"publish transaction marker is not a regular file: {journal_path}")
    journal_size = initial_stat.st_size
    if not 0 < journal_size <= MAX_PUBLISH_TRANSACTION_BYTES:
      raise OutputConflictError(f"publish transaction marker has an invalid size: {journal_path}")
    try:
      flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
      descriptor = os.open(journal_path, flags)
      try:
        opened_stat = os.fstat(descriptor)
        if opened_stat.st_dev != initial_stat.st_dev or opened_stat.st_ino != initial_stat.st_ino or not stat.S_ISREG(opened_stat.st_mode):
          raise OutputConflictError(f"publish transaction marker changed while it was opened: {journal_path}")
        chunks: list[bytes] = []
        total_bytes = 0
        while chunk := os.read(descriptor, min(64 * 1024, MAX_PUBLISH_TRANSACTION_BYTES + 1 - total_bytes)):
          chunks.append(chunk)
          total_bytes += len(chunk)
          if total_bytes > MAX_PUBLISH_TRANSACTION_BYTES:
            raise OutputConflictError(f"publish transaction marker has an invalid size: {journal_path}")
        final_stat = os.fstat(descriptor)
      finally:
        os.close(descriptor)
      marker_identity = _file_identity(final_stat)
      current_stat = journal_path.stat(follow_symlinks=False)
      if not _stat_matches_identity(opened_stat, marker_identity) or not _stat_matches_identity(current_stat, marker_identity):
        raise OutputConflictError(f"publish transaction marker changed while it was read: {journal_path}")
      payload = json.loads(b"".join(chunks).decode("utf-8"))
    except OutputConflictError:
      raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
      raise OutputConflictError(f"publish transaction marker is invalid JSON: {journal_path}") from exc
    if (
      not isinstance(payload, dict)
      or set(payload) != {"schema_version", "transaction", "origin_job_id", "contract", "entries"}
      or isinstance(payload.get("schema_version"), bool)
      or payload.get("schema_version") != PUBLISH_TRANSACTION_SCHEMA_VERSION
      or payload.get("transaction") != "comma_companion_media_publish"
      or not isinstance(payload.get("origin_job_id"), str)
      or not payload["origin_job_id"].strip()
      or payload.get("contract") != self._publish_contract(job, source_sha256)
      or not isinstance(payload.get("entries"), list)
      or len(payload["entries"]) != len(expected_targets)
    ):
      raise OutputConflictError(f"publish transaction marker does not match the requested media generation: {journal_path}")

    normalized: list[dict[str, Any]] = []
    for ordinal, (raw_entry, (expected_role, expected_target)) in enumerate(zip(payload["entries"], expected_targets, strict=True)):
      if not isinstance(raw_entry, dict) or set(raw_entry) != {"role", "target_path", "stage_path", "sha256", "size_bytes"}:
        raise OutputConflictError(f"publish transaction entry {ordinal} is invalid")
      target_path = Path(str(raw_entry.get("target_path", "")))
      stage_path = Path(str(raw_entry.get("stage_path", "")))
      digest = raw_entry.get("sha256")
      size_bytes = raw_entry.get("size_bytes")
      if (
        raw_entry.get("role") != expected_role
        or str(target_path) != str(expected_target)
        or not _is_stage_path_for(stage_path, expected_target)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
      ):
        raise OutputConflictError(f"publish transaction entry {ordinal} does not match its expected target")
      normalized.append(
        {
          "role": expected_role,
          "target": expected_target,
          "stage": stage_path,
          "sha256": digest,
          "size_bytes": size_bytes,
        }
      )
    return normalized, marker_identity

  @staticmethod
  def _verify_transaction_file(
    path: Path,
    entry: dict[str, Any],
    *,
    deadline: float,
    cancelled: Callable[[], bool],
  ) -> _FileIdentity:
    try:
      initial_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
      raise OutputConflictError(f"publish transaction leftover cannot be inspected: {path}") from exc
    if not stat.S_ISREG(initial_stat.st_mode):
      raise OutputConflictError(f"publish transaction contains a non-regular leftover: {path}")
    if initial_stat.st_size != entry["size_bytes"]:
      raise OutputConflictError(f"publish transaction leftover has an unexpected size: {path}")
    try:
      flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
      descriptor = os.open(path, flags)
    except OSError as exc:
      raise OutputConflictError(f"publish transaction leftover cannot be opened safely: {path}") from exc
    digest = hashlib.sha256()
    try:
      opened_stat = os.fstat(descriptor)
      if opened_stat.st_dev != initial_stat.st_dev or opened_stat.st_ino != initial_stat.st_ino or not stat.S_ISREG(opened_stat.st_mode):
        raise OutputConflictError(f"publish transaction leftover changed while it was opened: {path}")
      while chunk := os.read(descriptor, 4 * 1024 * 1024):
        if cancelled():
          raise CancelledError("job cancelled while hashing publish transaction leftovers")
        if time.monotonic() >= deadline:
          raise TimeoutError("job timed out while hashing publish transaction leftovers")
        digest.update(chunk)
      final_stat = os.fstat(descriptor)
    finally:
      os.close(descriptor)
    identity = _file_identity(final_stat)
    try:
      current_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
      raise OutputConflictError(f"publish transaction leftover changed while it was hashed: {path}") from exc
    if not _stat_matches_identity(opened_stat, identity) or not _stat_matches_identity(current_stat, identity):
      raise OutputConflictError(f"publish transaction leftover changed while it was hashed: {path}")
    if digest.hexdigest() != entry["sha256"]:
      raise OutputConflictError(f"publish transaction leftover has an unexpected SHA-256: {path}")
    return identity

  @staticmethod
  def _remove_publish_journal(journal_path: Path, journal_identity: _FileIdentity) -> None:
    try:
      candidates = list(journal_path.parent.iterdir())
    except OSError as exc:
      raise OutputConflictError(f"publish transaction directory cannot be inspected: {journal_path.parent}") from exc
    for candidate in candidates:
      if not _is_stage_path_for(candidate, journal_path):
        continue
      try:
        candidate_stat = candidate.stat(follow_symlinks=False)
      except OSError:
        continue
      if stat.S_ISREG(candidate_stat.st_mode) and candidate_stat.st_dev == journal_identity.device and candidate_stat.st_ino == journal_identity.inode:
        _unlink_verified_identity(
          candidate,
          _file_identity(candidate_stat),
          description="publish transaction marker stage",
        )
    _unlink_verified_identity(
      journal_path,
      journal_identity,
      description="publish transaction marker",
    )

  def _recover_publish_transaction(
    self,
    journal_path: Path,
    *,
    job: EncodeJob,
    source_sha256: str,
    expected_targets: list[tuple[str, Path]],
    deadline: float,
    cancelled: Callable[[], bool],
    progress: ProgressCallback | None,
  ) -> bool:
    if not journal_path.exists() and not journal_path.is_symlink():
      return False
    entries, journal_identity = self._load_publish_transaction(
      journal_path,
      job=job,
      source_sha256=source_sha256,
      expected_targets=expected_targets,
    )
    self._emit(progress, event="phase", phase="publish_recovery", state="started")
    verified_leftovers: list[tuple[Path, _FileIdentity]] = []
    for entry in entries:
      for path in (entry["target"], entry["stage"]):
        if path.exists() or path.is_symlink():
          identity = self._verify_transaction_file(path, entry, deadline=deadline, cancelled=cancelled)
          verified_leftovers.append((path, identity))
    touched_directories = {journal_path.parent}
    for path, identity in verified_leftovers:
      _unlink_verified_identity(
        path,
        identity,
        description="publish transaction leftover",
      )
      touched_directories.add(path.parent)
    self._remove_publish_journal(journal_path, journal_identity)
    for directory in touched_directories:
      _fsync_directory(directory)
    self._emit(progress, event="phase", phase="publish_recovery", state="completed")
    return True

  def _discard_completed_publish_transaction(
    self,
    journal_path: Path,
    *,
    job: EncodeJob,
    source_sha256: str,
    expected_targets: list[tuple[str, Path]],
    deadline: float,
    cancelled: Callable[[], bool],
  ) -> None:
    if not journal_path.exists() and not journal_path.is_symlink():
      return
    entries, journal_identity = self._load_publish_transaction(
      journal_path,
      job=job,
      source_sha256=source_sha256,
      expected_targets=expected_targets,
    )
    for entry in entries:
      stage_path = entry["stage"]
      if stage_path.exists() or stage_path.is_symlink():
        self._verify_transaction_file(stage_path, entry, deadline=deadline, cancelled=cancelled)
        raise OutputConflictError(f"a completed publish transaction still has a staged leftover: {stage_path}")
    self._remove_publish_journal(journal_path, journal_identity)
    _fsync_directory(journal_path.parent)

  @staticmethod
  def _adopt_completed_result(job: EncodeJob, payload: dict[str, Any], metadata_target: Path) -> dict[str, Any]:
    persisted = copy.deepcopy(payload)
    if persisted.get("job_id") != job.job_id:
      persisted["job_id"] = job.job_id
      persisted["status"] = "complete"
      staged_metadata = _stage_path(metadata_target)
      try:
        _write_json_durable(staged_metadata, persisted)
        os.replace(staged_metadata, metadata_target)
        _fsync_directory(metadata_target.parent)
      finally:
        staged_metadata.unlink(missing_ok=True)
    response = copy.deepcopy(persisted)
    response["status"] = "already_complete"
    return response

  def build_encode_command(
    self,
    job: EncodeJob,
    source: ProbeInfo,
    staged_video: Path,
    bitrate_budget: BitrateBudget | None = None,
  ) -> list[str]:
    options = job.encode
    bitrate_budget = bitrate_budget or self.bitrate_budget(job, source)
    source_path = Path(source.path).resolve()
    output_path = Path(staged_video).resolve()
    command = [
      self.ffmpeg,
      "-hide_banner",
      "-nostdin",
      "-y",
      "-loglevel",
      "error",
      "-stats_period",
      "0.5",
      "-progress",
      "pipe:1",
    ]
    if job.input.input_format == "raw_hevc":
      command.extend(["-r", str(options.raw_hevc_frame_rate)])
    command.extend(_input_guard_args(job.input.input_format))
    command.extend(
      [
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-c:v",
        "libsvtav1",
        "-preset",
        str(options.preset),
        "-crf",
        str(options.crf),
        "-b:v",
        "0",
        "-maxrate:v",
        str(bitrate_budget.video_maxrate_bps),
        "-bufsize:v",
        str(bitrate_budget.video_maxrate_bps * 2),
        "-pix_fmt",
        options.pixel_format,
        "-threads",
        str(options.logical_processors),
        "-svtav1-params",
        f"lp={options.logical_processors}",
        "-g",
        str(options.keyframe_interval_seconds * self._effective_frame_rate(source, options.raw_hevc_frame_rate)),
      ]
    )
    if job.input.input_format == "mpegts" and options.preserve_audio and source.audio_codec_name:
      command.extend(["-map", "0:a:0?", "-c:a", "libopus", "-b:a", f"{options.audio_bitrate_kbps}k", "-vbr", "constrained"])
    else:
      command.append("-an")
    command.extend(
      [
        "-cluster_time_limit",
        "2000",
        "-cues_to_front",
        "1",
        "-fs",
        str(MAX_OUTPUT_BYTES),
        "-protocol_whitelist",
        LOCAL_PROTOCOL_WHITELIST,
        "-f",
        "webm",
        str(output_path),
      ]
    )
    return command

  def _validate_source_media(self, job: EncodeJob, source: ProbeInfo) -> None:
    approved_pairs = {
      ("fcamera", "road", "raw_hevc"),
      ("ecamera", "wide", "raw_hevc"),
      ("dcamera", "driver", "raw_hevc"),
      ("qcamera", "qcamera", "raw_hevc"),
      ("qcamera", "qcamera", "mpegts"),
    }
    if (job.input.kind, job.input.camera, job.input.input_format) not in approved_pairs:
      raise InputValidationError(
        f"kind/camera/input_format is not an approved canonical media mapping: {job.input.kind}/{job.input.camera}/{job.input.input_format}"
      )
    format_tokens = {item.strip() for item in source.format_name.split(",")}
    if source.video_stream_count != MAX_VIDEO_STREAMS:
      raise InputValidationError(f"expected exactly one video stream, found {source.video_stream_count}")
    if source.other_stream_count:
      raise InputValidationError(f"unexpected non-audio/video streams: {source.other_stream_count}")
    if source.width <= 0 or source.height <= 0:
      raise InputValidationError("input video dimensions are missing or non-positive")
    if source.width > MAX_WIDTH or source.height > MAX_HEIGHT or source.width * source.height > MAX_PIXELS:
      raise InputValidationError(f"input dimensions {source.width}x{source.height} exceed {MAX_WIDTH}x{MAX_HEIGHT}/{MAX_PIXELS} pixels")
    if source.max_frame_width is not None and source.max_frame_width > MAX_WIDTH:
      raise InputValidationError(f"decoded input frame width {source.max_frame_width} exceeds {MAX_WIDTH}")
    if source.max_frame_height is not None and source.max_frame_height > MAX_HEIGHT:
      raise InputValidationError(f"decoded input frame height {source.max_frame_height} exceeds {MAX_HEIGHT}")
    if not source.frame_count or source.frame_count > MAX_FRAMES:
      raise InputValidationError(f"input frame count {source.frame_count or 0} exceeds the limit of {MAX_FRAMES}")
    if job.input.input_format == "raw_hevc":
      duration = source.frame_count / job.encode.raw_hevc_frame_rate
    else:
      duration = source.video_duration_seconds or source.duration_seconds
    if duration is None or duration <= 0 or duration > MAX_DURATION_SECONDS:
      raise InputValidationError(f"input duration {duration} exceeds the limit of {MAX_DURATION_SECONDS} seconds")
    effective_rate = source.frame_count / duration
    if effective_rate <= 0 or effective_rate > MAX_FRAME_RATE:
      raise InputValidationError(f"input frame rate {effective_rate:.3f} exceeds the limit of {MAX_FRAME_RATE} fps")
    if job.input.input_format != "raw_hevc":
      for reported_rate in (source.average_frame_rate, source.reported_frame_rate):
        if reported_rate is not None and (reported_rate <= 0 or reported_rate > MAX_FRAME_RATE):
          raise InputValidationError(f"reported input frame rate {reported_rate:.3f} exceeds the limit of {MAX_FRAME_RATE} fps")

    if job.input.input_format == "raw_hevc":
      if format_tokens != {"hevc"} or source.codec_name != "hevc" or not source.raw_elementary_stream:
        raise InputValidationError(f"raw_hevc requires an HEVC elementary stream, got {source.format_name}/{source.codec_name}")
      if source.audio_stream_count:
        raise InputValidationError("raw_hevc input must not contain audio")
    else:
      if "mpegts" not in format_tokens or source.codec_name != "h264":
        raise InputValidationError(f"mpegts requires H.264 video, got {source.format_name}/{source.codec_name}")
      if source.audio_stream_count > MAX_AUDIO_STREAMS:
        raise InputValidationError(f"mpegts has {source.audio_stream_count} audio streams; at most one is allowed")
      duration_records = {
        "video": source.video_duration_seconds,
        "audio": source.audio_duration_seconds if source.audio_stream_count else None,
        "container": source.container_duration_seconds,
      }
      for label, stream_duration in duration_records.items():
        if stream_duration is not None and (stream_duration <= 0 or stream_duration > MAX_DURATION_SECONDS):
          raise InputValidationError(f"qcamera {label} duration {stream_duration} exceeds the limit of {MAX_DURATION_SECONDS} seconds")
      for label in ("audio", "container"):
        stream_duration = duration_records[label]
        if stream_duration is not None and abs(stream_duration - duration) > MAX_STREAM_DURATION_SKEW_SECONDS:
          raise InputValidationError(
            f"qcamera {label} duration {stream_duration:.3f}s differs from video duration {duration:.3f}s by more than {MAX_STREAM_DURATION_SKEW_SECONDS:.3f}s"
          )
      if source.audio_stream_count:
        if source.audio_codec_name not in {"aac", "opus"}:
          raise InputValidationError(f"qcamera audio codec is not allowed: {source.audio_codec_name}")
        if source.audio_channels is None or not 1 <= source.audio_channels <= MAX_AUDIO_CHANNELS:
          raise InputValidationError(f"qcamera audio channel count is not allowed: {source.audio_channels}")
        if source.audio_sample_rate is None or source.audio_sample_rate < MIN_AUDIO_SAMPLE_RATE or source.audio_sample_rate > MAX_AUDIO_SAMPLE_RATE:
          raise InputValidationError(f"qcamera audio sample rate is not allowed: {source.audio_sample_rate}")
        if source.scanned_audio_channels_max is not None and source.scanned_audio_channels_max > MAX_AUDIO_CHANNELS:
          raise InputValidationError(f"decoded qcamera audio channel count is not allowed: {source.scanned_audio_channels_max}")
        if (source.scanned_audio_sample_rate_min is not None and source.scanned_audio_sample_rate_min < MIN_AUDIO_SAMPLE_RATE) or (
          source.scanned_audio_sample_rate_max is not None and source.scanned_audio_sample_rate_max > MAX_AUDIO_SAMPLE_RATE
        ):
          raise InputValidationError(
            f"decoded qcamera audio sample rate range is not allowed: {source.scanned_audio_sample_rate_min}..{source.scanned_audio_sample_rate_max}"
          )

  def _effective_frame_rate(self, source: ProbeInfo, raw_hevc_frame_rate: int) -> int:
    if source.raw_elementary_stream and source.codec_name == "hevc":
      return raw_hevc_frame_rate
    rate = source.average_frame_rate or source.reported_frame_rate or raw_hevc_frame_rate
    return max(1, round(rate))

  def validate(
    self,
    path: str | Path,
    *,
    source: ProbeInfo | None = None,
    raw_hevc_frame_rate: int = 20,
    full_decode: bool = True,
    expected_audio: bool = False,
    timeout_seconds: float = 3_600,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    cancel_file: str | None = None,
  ) -> ValidationResult:
    deadline = time.monotonic() + timeout_seconds
    cancelled = self._cancelled(cancel_event, cancel_file)
    target = Path(path).resolve()
    info = self.probe(
      target,
      input_format="webm",
      timeout_seconds=max(0.001, deadline - time.monotonic()),
      cancel_event=cancel_event,
      cancel_file=cancel_file,
    )
    failures: list[str] = []
    if info.video_stream_count != MAX_VIDEO_STREAMS:
      failures.append(f"expected one output video stream, found {info.video_stream_count}")
    if info.other_stream_count:
      failures.append(f"unexpected output streams: {info.other_stream_count}")
    if info.codec_type != "video":
      failures.append("output has no video stream")
    if info.codec_name != "av1":
      failures.append(f"expected AV1, got {info.codec_name or 'unknown codec'}")
    if "webm" not in {item.strip() for item in info.format_name.split(",")}:
      failures.append(f"expected WebM, got {info.format_name or 'unknown format'}")
    if info.width <= 0 or info.height <= 0:
      failures.append("output dimensions are invalid")
    elif info.width > MAX_WIDTH or info.height > MAX_HEIGHT or info.width * info.height > MAX_PIXELS:
      failures.append(f"output dimensions {info.width}x{info.height} exceed worker limits")
    if info.pixel_format != "yuv420p":
      failures.append(f"expected browser-compatible yuv420p, got {info.pixel_format or 'unknown pixel format'}")
    if not info.frame_count or info.frame_count <= 0:
      failures.append("output frame count is missing or zero")
    elif info.frame_count > MAX_FRAMES:
      failures.append(f"output frame count {info.frame_count} exceeds {MAX_FRAMES}")
    if not info.duration_seconds or info.duration_seconds <= 0:
      failures.append("output duration is missing or zero")
    elif info.duration_seconds > MAX_DURATION_SECONDS:
      failures.append(f"output duration {info.duration_seconds} exceeds {MAX_DURATION_SECONDS} seconds")
    output_video_duration = info.video_duration_seconds or info.duration_seconds
    if info.frame_count and output_video_duration and info.frame_count / output_video_duration > MAX_FRAME_RATE:
      failures.append(f"output frame rate exceeds {MAX_FRAME_RATE} fps")
    if expected_audio:
      if info.audio_stream_count != 1 or info.audio_codec_name != "opus":
        failures.append(f"expected one Opus audio stream, got {info.audio_stream_count}/{info.audio_codec_name or 'none'}")
      if info.audio_channels is None or not 1 <= info.audio_channels <= MAX_AUDIO_CHANNELS:
        failures.append(f"output audio channel count is invalid: {info.audio_channels}")
      if info.audio_sample_rate is None or info.audio_sample_rate < MIN_AUDIO_SAMPLE_RATE or info.audio_sample_rate > MAX_AUDIO_SAMPLE_RATE:
        failures.append(f"output audio sample rate is invalid: {info.audio_sample_rate}")
      if (
        output_video_duration is not None
        and info.audio_duration_seconds is not None
        and abs(info.audio_duration_seconds - output_video_duration) > MAX_STREAM_DURATION_SKEW_SECONDS
      ):
        failures.append(f"output audio duration {info.audio_duration_seconds:.3f}s differs from video duration {output_video_duration:.3f}s")
    elif info.audio_stream_count:
      failures.append("unexpected output audio stream")
    cues_front_loaded = _webm_cues_front_loaded(target)
    if not cues_front_loaded:
      failures.append("WebM seek cues are not before the first media cluster")

    if source is not None:
      if source.width and info.width != source.width:
        failures.append(f"output width {info.width} differs from source width {source.width}")
      if source.height and info.height != source.height:
        failures.append(f"output height {info.height} differs from source height {source.height}")
      if source.frame_count and info.frame_count and source.frame_count != info.frame_count:
        failures.append(f"output frame count {info.frame_count} differs from source {source.frame_count}")
      expected_duration = source.effective_duration(raw_hevc_frame_rate)
      if expected_duration and info.duration_seconds:
        duration_tolerance = max(0.25, 2 / raw_hevc_frame_rate)
        if abs(expected_duration - info.duration_seconds) > duration_tolerance:
          failures.append(f"output duration {info.duration_seconds:.3f}s differs from source {expected_duration:.3f}s")

    if failures:
      raise OutputValidationError("; ".join(failures))

    decoded = False
    if full_decode:
      self._emit(progress, event="phase", phase="decode_validation", state="started")
      command = [
        self.ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        *_input_guard_args("webm"),
        "-i",
        str(target),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-f",
        "null",
        "-",
      ]
      self._run_process(
        command,
        phase="decode_validation",
        duration_seconds=info.duration_seconds,
        deadline=deadline,
        cancelled=cancelled,
        progress=progress,
      )
      decoded = True
      self._emit(progress, event="phase", phase="decode_validation", state="completed")
    return ValidationResult(probe=info, cues_front_loaded=cues_front_loaded, decoded=decoded)

  def build_frame_index(
    self,
    path: str | Path,
    *,
    job: EncodeJob,
    video_probe: ProbeInfo,
    video_sha256: str,
    timeout_seconds: float,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    cancel_file: str | None = None,
  ) -> dict[str, Any]:
    target = Path(path).resolve()
    cancelled = self._cancelled(cancel_event, cancel_file)
    if cancelled():
      raise CancelledError("job cancelled before frame index extraction")
    deadline = time.monotonic() + min(max(0.001, timeout_seconds), HARD_PROBE_TIMEOUT_SECONDS)
    self._emit(progress, event="phase", phase="frame_index", state="started")
    command = [
      self.ffprobe,
      "-v",
      "error",
      *_input_guard_args("webm"),
      "-select_streams",
      "v:0",
      "-show_frames",
      "-show_entries",
      "stream=time_base:frame=pts,best_effort_timestamp,duration,pkt_duration,key_frame",
      "-of",
      "json",
      "-i",
      str(target),
    ]
    completed = self._run_capture(command, phase="frame index extraction", deadline=deadline, cancelled=cancelled)
    if completed.returncode != 0:
      error = completed.stderr.strip()[-2000:]
      raise OutputValidationError(f"ffprobe could not enumerate encoded frames: {error or 'unknown error'}")
    try:
      payload = json.loads(completed.stdout)
      raw_frames = payload["frames"]
      stream = payload.get("streams", [{}])[0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
      raise OutputValidationError("ffprobe returned an invalid encoded-frame index") from exc
    if not isinstance(raw_frames, list):
      raise OutputValidationError("ffprobe encoded-frame index is not a list")

    time_base_text = stream.get("time_base") or video_probe.time_base
    exact_time_base = _time_base(time_base_text)
    canonical_time_base = f"{exact_time_base.numerator}/{exact_time_base.denominator}"
    if video_probe.time_base is not None and _time_base(video_probe.time_base) != exact_time_base:
      raise OutputValidationError("frame index time base differs from the validated video stream")
    if not video_probe.frame_count or len(raw_frames) != video_probe.frame_count:
      raise OutputValidationError(f"frame index contains {len(raw_frames)} rows but the decoded video contains {video_probe.frame_count or 0} frames")

    parsed: list[dict[str, Any]] = []
    for ordinal, raw_frame in enumerate(raw_frames):
      if not isinstance(raw_frame, dict):
        raise OutputValidationError(f"frame index row {ordinal} is not an object")
      pts = _parse_int(raw_frame.get("pts"))
      if pts is None:
        pts = _parse_int(raw_frame.get("best_effort_timestamp"))
      duration = _parse_int(raw_frame.get("duration"))
      if duration is None:
        duration = _parse_int(raw_frame.get("pkt_duration"))
      keyframe = _parse_int(raw_frame.get("key_frame"))
      if pts is None:
        raise OutputValidationError(f"frame index row {ordinal} has no presentation timestamp")
      if keyframe not in (0, 1):
        raise OutputValidationError(f"frame index row {ordinal} has no valid keyframe flag")
      parsed.append({"pts": pts, "duration": duration, "keyframe": bool(keyframe)})

    for ordinal, (left, right) in enumerate(itertools.pairwise(parsed)):
      if right["pts"] <= left["pts"]:
        raise OutputValidationError(f"frame index PTS is not strictly increasing at row {ordinal + 1}")

    inferred_durations = 0
    for ordinal, frame in enumerate(parsed):
      duration = frame["duration"]
      if duration is None or duration <= 0:
        if ordinal + 1 < len(parsed):
          duration = parsed[ordinal + 1]["pts"] - frame["pts"]
        elif ordinal > 0:
          duration = parsed[ordinal - 1]["duration"]
        elif video_probe.duration_seconds is not None:
          duration = _round_fraction(Fraction(str(video_probe.duration_seconds)) / exact_time_base)
        inferred_durations += 1
      if not isinstance(duration, int) or duration <= 0:
        raise OutputValidationError(f"frame index row {ordinal} has no positive duration")
      frame["duration"] = duration
      if ordinal + 1 < len(parsed) and frame["pts"] + duration > parsed[ordinal + 1]["pts"]:
        raise OutputValidationError(f"frame index row {ordinal} overlaps the following presentation timestamp")

    if not parsed or parsed[0]["keyframe"] is not True:
      raise OutputValidationError("encoded video does not begin with a keyframe")

    frames = [
      {
        "ordinal": ordinal,
        "segment_frame_id": ordinal,
        "pts": frame["pts"],
        "duration": frame["duration"],
        "pts_us": _ticks_to_microseconds(frame["pts"], exact_time_base),
        "duration_us": _ticks_to_microseconds(frame["duration"], exact_time_base),
        "keyframe": frame["keyframe"],
      }
      for ordinal, frame in enumerate(parsed)
    ]
    if any(frame["duration_us"] <= 0 for frame in frames):
      raise OutputValidationError("frame index contains a duration below one microsecond")

    frame_index = {
      "schema_version": FRAME_INDEX_SCHEMA_VERSION,
      "mapping_type": "encoded_frame_pts",
      "join_key": ["camera", "segment_num", "segment_frame_id"],
      "ordinal_basis": 0,
      "source_frame_key": "segment_frame_id",
      "camera": job.input.camera,
      "segment_num": job.input.segment_num,
      "source_artifact_id": job.input.artifact_id,
      "video": {
        "path": str(Path(job.outputs.video_path)),
        "sha256": video_sha256,
      },
      "time_base": {
        "numerator": exact_time_base.numerator,
        "denominator": exact_time_base.denominator,
        "text": canonical_time_base,
      },
      "frame_count": len(frames),
      "first_pts": frames[0]["pts"],
      "last_end_pts": frames[-1]["pts"] + frames[-1]["duration"],
      "duration_inference_count": inferred_durations,
      "frames": frames,
    }
    self._validate_frame_index_payload(frame_index, video_probe, job=job, video_sha256=video_sha256)
    self._emit(
      progress,
      event="phase",
      phase="frame_index",
      state="completed",
      frames=len(frames),
      time_base=canonical_time_base,
      inferred_durations=inferred_durations,
    )
    return frame_index

  def _validate_frame_index_payload(
    self,
    payload: dict[str, Any],
    video_probe: ProbeInfo,
    *,
    job: EncodeJob | None = None,
    video_sha256: str | None = None,
  ) -> None:
    video_identity = payload.get("video")
    if (
      payload.get("schema_version") != FRAME_INDEX_SCHEMA_VERSION
      or payload.get("mapping_type") != "encoded_frame_pts"
      or payload.get("join_key") != ["camera", "segment_num", "segment_frame_id"]
      or payload.get("ordinal_basis") != 0
      or payload.get("source_frame_key") != "segment_frame_id"
      or isinstance(payload.get("segment_num"), bool)
      or not isinstance(payload.get("segment_num"), int)
      or payload["segment_num"] < 0
      or not isinstance(video_identity, dict)
    ):
      raise OutputValidationError("persisted frame index identity violates the version 1 contract")
    if job is not None and (
      payload.get("camera") != job.input.camera
      or payload.get("segment_num") != job.input.segment_num
      or payload.get("source_artifact_id") != job.input.artifact_id
      or video_identity.get("path") != str(Path(job.outputs.video_path))
    ):
      raise OutputValidationError("persisted frame index identity differs from the media job")
    if video_sha256 is not None and video_identity.get("sha256") != video_sha256:
      raise OutputValidationError("persisted frame index video hash differs from the encoded media")
    frames = payload.get("frames")
    if not isinstance(frames, list) or payload.get("frame_count") != len(frames) or len(frames) != video_probe.frame_count:
      raise OutputValidationError("persisted frame index length does not match decoded frames")
    for ordinal, frame in enumerate(frames):
      if (
        not isinstance(frame, dict)
        or frame.get("ordinal") != ordinal
        or frame.get("segment_frame_id") != ordinal
        or isinstance(frame.get("pts"), bool)
        or not isinstance(frame.get("pts"), int)
        or isinstance(frame.get("duration"), bool)
        or not isinstance(frame.get("duration"), int)
        or frame["duration"] <= 0
        or not isinstance(frame.get("keyframe"), bool)
      ):
        raise OutputValidationError(f"persisted frame index row {ordinal} violates the version 1 contract")
      if ordinal and frame["pts"] <= frames[ordinal - 1]["pts"]:
        raise OutputValidationError(f"persisted frame index PTS is not ordered at row {ordinal}")
      if ordinal and frames[ordinal - 1]["pts"] + frames[ordinal - 1]["duration"] > frame["pts"]:
        raise OutputValidationError(f"persisted frame index rows overlap at row {ordinal}")
    if not frames[0]["keyframe"]:
      raise OutputValidationError("persisted frame index does not begin with a keyframe")
    if payload.get("first_pts") != frames[0]["pts"] or payload.get("last_end_pts") != frames[-1]["pts"] + frames[-1]["duration"]:
      raise OutputValidationError("persisted frame index boundary fields are inconsistent")
    inferred_count = payload.get("duration_inference_count")
    if isinstance(inferred_count, bool) or not isinstance(inferred_count, int) or not 0 <= inferred_count <= len(frames):
      raise OutputValidationError("persisted frame index duration inference count is invalid")
    time_base_payload = payload.get("time_base")
    if not isinstance(time_base_payload, dict):
      raise OutputValidationError("persisted frame index has no exact time base")
    try:
      exact_time_base = Fraction(time_base_payload["numerator"], time_base_payload["denominator"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
      raise OutputValidationError("persisted frame index time base is invalid") from exc
    if exact_time_base <= 0 or time_base_payload.get("text") != f"{exact_time_base.numerator}/{exact_time_base.denominator}":
      raise OutputValidationError("persisted frame index time base is inconsistent")
    for ordinal, frame in enumerate(frames):
      if frame.get("pts_us") != _ticks_to_microseconds(frame["pts"], exact_time_base):
        raise OutputValidationError(f"persisted frame index PTS conversion is invalid at row {ordinal}")
      if frame.get("duration_us") != _ticks_to_microseconds(frame["duration"], exact_time_base):
        raise OutputValidationError(f"persisted frame index duration conversion is invalid at row {ordinal}")
    if video_probe.duration_seconds is not None:
      mapped_end_us = _ticks_to_microseconds(payload["last_end_pts"], exact_time_base)
      expected_end_us = round(video_probe.duration_seconds * 1_000_000)
      duration_tolerance_us = max(100_000, max(frame["duration_us"] for frame in frames) * 2)
      if abs(mapped_end_us - expected_end_us) > duration_tolerance_us:
        raise OutputValidationError(f"frame index ends at {mapped_end_us}us but the validated video duration is {expected_end_us}us")

  def encode(
    self,
    job: EncodeJob,
    *,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
  ) -> dict[str, Any]:
    started_monotonic = time.monotonic()
    started_at = _utc_now()
    deadline = started_monotonic + job.limits.timeout_seconds
    cancelled = self._cancelled(cancel_event, job.limits.cancel_file)
    if job.encode.overwrite:
      raise InputValidationError("encode.overwrite must be false for crash-safe publication")
    source_path = Path(job.input.path).resolve()
    _checked_file_size(source_path)
    self._validate_path_relationships(job)
    versions = self.tool_versions()

    self._emit(progress, event="phase", phase="input_hash", state="started")
    source_hash = _sha256(source_path, deadline=deadline, cancelled=cancelled)
    if job.input.expected_sha256 and source_hash != job.input.expected_sha256:
      raise InputIntegrityError(f"input SHA-256 mismatch: expected {job.input.expected_sha256}, got {source_hash}")
    self._emit(progress, event="phase", phase="input_hash", state="completed", sha256=source_hash)

    source = self.probe(
      source_path,
      input_format=job.input.input_format,
      timeout_seconds=self._remaining(deadline),
      cancel_event=cancel_event,
      cancel_file=job.limits.cancel_file,
    )
    if source.codec_type != "video":
      raise ProbeError(f"input does not contain a video stream: {source_path}")
    self._validate_source_media(job, source)
    self.ensure_encoder(require_opus=(job.input.input_format == "mpegts" and job.encode.preserve_audio and source.audio_codec_name is not None))
    self._emit(
      progress,
      event="phase",
      phase="probe",
      state="completed",
      raw_elementary_stream=source.raw_elementary_stream,
      codec=source.codec_name,
      frames=source.frame_count,
    )

    video_target, metadata_target, poster_target, frame_index_target, thumbnail_targets = self._resolved_outputs(job, source)
    all_targets = [video_target, metadata_target, poster_target, frame_index_target, *thumbnail_targets]
    publication_targets = self._publication_targets(
      video_target,
      metadata_target,
      poster_target,
      frame_index_target,
      thumbnail_targets,
    )
    publish_journal = self._publish_journal_path(metadata_target)
    existing = [path for path in all_targets if path.exists() or path.is_symlink()]
    if existing and not job.encode.overwrite:
      cached = self._load_completed_result(job, source_hash, video_target, metadata_target, source)
      if cached is not None:
        self._discard_completed_publish_transaction(
          publish_journal,
          job=job,
          source_sha256=source_hash,
          expected_targets=publication_targets,
          deadline=deadline,
          cancelled=cancelled,
        )
        return self._adopt_completed_result(job, cached, metadata_target)
    self._recover_publish_transaction(
      publish_journal,
      job=job,
      source_sha256=source_hash,
      expected_targets=publication_targets,
      deadline=deadline,
      cancelled=cancelled,
      progress=progress,
    )
    existing = [path for path in all_targets if path.exists() or path.is_symlink()]
    if existing and not job.encode.overwrite:
      raise OutputConflictError(f"output already exists: {existing[0]}")

    image_budget = min(MAX_TOTAL_IMAGE_BYTES, (len(thumbnail_targets) + 1) * MAX_IMAGE_BYTES)
    maximum_derived_bytes = MAX_OUTPUT_BYTES + image_budget + MAX_AUXILIARY_OUTPUT_BYTES
    self._ensure_disk_space(
      all_targets,
      source.size_bytes,
      job.limits.minimum_free_bytes,
      job.limits.working_space_multiplier,
      maximum_derived_bytes,
    )
    for target in all_targets:
      target.parent.mkdir(parents=True, exist_ok=True)

    staged_video = _stage_path(video_target)
    staged_poster = _stage_path(poster_target)
    staged_frame_index = _stage_path(frame_index_target)
    staged_thumbnails = [_stage_path(path) for path in thumbnail_targets]
    staged_metadata = _stage_path(metadata_target)
    staged = [staged_video, staged_poster, staged_frame_index, *staged_thumbnails, staged_metadata]
    try:
      validation: ValidationResult | None = None
      bitrate_policy: dict[str, Any] | None = None
      failed_bitrate_attempts: list[dict[str, Any]] = []
      for attempt, target_ratio in enumerate((BITRATE_TARGET_RATIO, BITRATE_FALLBACK_RATIO), start=1):
        budget = self.bitrate_budget(job, source, attempt=attempt, total_ratio=target_ratio)
        command = self.build_encode_command(job, source, staged_video, budget)
        self._emit(
          progress,
          event="phase",
          phase="encode",
          state="started",
          attempt=attempt,
          target_ratio=float(target_ratio),
          video_maxrate_bps=budget.video_maxrate_bps,
        )
        self._run_process(
          command,
          phase="encode",
          duration_seconds=source.effective_duration(job.encode.raw_hevc_frame_rate),
          deadline=deadline,
          cancelled=cancelled,
          progress=progress,
          output_path=staged_video,
          maximum_output_bytes=MAX_OUTPUT_BYTES,
        )
        self._emit(progress, event="phase", phase="encode", state="completed", attempt=attempt)

        validation = self.validate(
          staged_video,
          source=source,
          raw_hevc_frame_rate=job.encode.raw_hevc_frame_rate,
          full_decode=True,
          expected_audio=(job.input.input_format == "mpegts" and job.encode.preserve_audio and source.audio_codec_name is not None),
          timeout_seconds=self._remaining(deadline),
          progress=progress,
          cancel_event=cancel_event,
          cancel_file=job.limits.cancel_file,
        )
        bitrate_policy = self.bitrate_record(job, source, validation.probe, budget)
        self._emit(
          progress,
          event="phase",
          phase="bitrate_validation",
          state="completed" if bitrate_policy["accepted"] else "retrying" if attempt == 1 else "rejected",
          attempt=attempt,
          input_total_bitrate_bps=budget.input_total_bitrate_bps,
          output_total_bitrate_bps=bitrate_policy["output_total_bitrate_bps"],
          output_to_input_ratio=bitrate_policy["output_to_input_ratio"]["decimal"],
        )
        if bitrate_policy["accepted"]:
          break
        failed_bitrate_attempts.append(bitrate_policy)
        staged_video.unlink(missing_ok=True)
      else:
        last = failed_bitrate_attempts[-1]
        raise BitrateReductionError(
          "AV1 output was not strictly lower bitrate and smaller than its source after the "
          f"{BITRATE_TARGET_RATIO.numerator}/{BITRATE_TARGET_RATIO.denominator} target and "
          f"{BITRATE_FALLBACK_RATIO.numerator}/{BITRATE_FALLBACK_RATIO.denominator} fallback: "
          f"{last['output_total_bitrate_bps']} bps / {last['selected_budget']['input_total_bitrate_bps']} bps"
        )

      if validation is None or bitrate_policy is None:
        raise BitrateReductionError("AV1 bitrate policy produced no accepted output")
      duration = validation.probe.duration_seconds or 0.0
      self._generate_artwork(
        staged_video,
        staged_poster,
        staged_thumbnails,
        duration=duration,
        width=job.encode.thumbnail_width,
        deadline=deadline,
        cancelled=cancelled,
        progress=progress,
      )

      self._emit(progress, event="phase", phase="output_hash", state="started")
      video_hash = _sha256(staged_video, deadline=deadline, cancelled=cancelled)
      frame_index_payload = self.build_frame_index(
        staged_video,
        job=job,
        video_probe=validation.probe,
        video_sha256=video_hash,
        timeout_seconds=self._remaining(deadline),
        progress=progress,
        cancel_event=cancel_event,
        cancel_file=job.limits.cancel_file,
      )
      staged_frame_index.write_text(json.dumps(frame_index_payload, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
      persisted_frame_index = json.loads(staged_frame_index.read_text(encoding="utf-8"))
      self._validate_frame_index_payload(
        persisted_frame_index,
        validation.probe,
        job=job,
        video_sha256=video_hash,
      )
      frame_index_hash = _sha256(staged_frame_index, deadline=deadline, cancelled=cancelled)
      poster_hash = _sha256(staged_poster, deadline=deadline, cancelled=cancelled)
      thumbnail_records = [
        {
          "path": str(target),
          "mime_type": "image/jpeg",
          "sha256": _sha256(stage, deadline=deadline, cancelled=cancelled),
          "size_bytes": stage.stat().st_size,
          "timestamp_seconds": timestamp,
        }
        for stage, target, timestamp in zip(
          staged_thumbnails,
          thumbnail_targets,
          self._thumbnail_timestamps(duration, len(staged_thumbnails)),
          strict=True,
        )
      ]
      self._emit(progress, event="phase", phase="output_hash", state="completed", sha256=video_hash)

      completed_at = _utc_now()
      validation_record = validation.as_dict()
      validation_record["probe"]["path"] = str(video_target)
      result: dict[str, Any] = {
        "schema_version": 1,
        "job_id": job.job_id,
        "status": "complete",
        "started_at": started_at,
        "completed_at": completed_at,
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
        "raw_retained": True,
        "tools": versions.as_dict(),
        "bitrate_policy": bitrate_policy,
        "input": {
          "path": str(source_path),
          "artifact_id": job.input.artifact_id,
          "camera": job.input.camera,
          "kind": job.input.kind,
          "input_format": job.input.input_format,
          "segment_num": job.input.segment_num,
          "sha256": source_hash,
          "size_bytes": source.size_bytes,
          "probe": source.as_dict(),
          "raw_hevc_frame_rate_applied": (job.encode.raw_hevc_frame_rate if source.raw_elementary_stream and source.codec_name == "hevc" else None),
        },
        "output": {
          "video": {
            "path": str(video_target),
            "mime_type": "video/webm",
            "sha256": video_hash,
            "size_bytes": staged_video.stat().st_size,
            **validation_record,
          },
          "poster": {
            "path": str(poster_target),
            "mime_type": "image/jpeg",
            "sha256": poster_hash,
            "size_bytes": staged_poster.stat().st_size,
            "timestamp_seconds": self._poster_timestamp(duration),
          },
          "frame_index": {
            "path": str(frame_index_target),
            "mime_type": "application/json",
            "sha256": frame_index_hash,
            "size_bytes": staged_frame_index.stat().st_size,
            "schema_version": FRAME_INDEX_SCHEMA_VERSION,
            "frame_count": frame_index_payload["frame_count"],
            "time_base": frame_index_payload["time_base"],
            "join_key": frame_index_payload["join_key"],
            "ordinal_basis": frame_index_payload["ordinal_basis"],
            "source_frame_key": frame_index_payload["source_frame_key"],
            "camera": job.input.camera,
            "segment_num": job.input.segment_num,
            "source_artifact_id": job.input.artifact_id,
          },
          "thumbnails": thumbnail_records,
          "metadata_path": str(metadata_target),
        },
        "encode": asdict(job.encode),
        "time_mapping": asdict(job.time_mapping) if job.time_mapping is not None else None,
      }
      _write_json_durable(staged_metadata, result)
      metadata_hash = _sha256(staged_metadata, deadline=deadline, cancelled=cancelled)
      publication_pairs = [
        (staged_poster, poster_target),
        *zip(staged_thumbnails, thumbnail_targets, strict=True),
        (staged_frame_index, frame_index_target),
        (staged_video, video_target),
        (staged_metadata, metadata_target),
      ]
      publication_hashes = [
        poster_hash,
        *(record["sha256"] for record in thumbnail_records),
        frame_index_hash,
        video_hash,
        metadata_hash,
      ]
      publication_entries: list[dict[str, Any]] = []
      for (role, target), (stage, paired_target), digest in zip(
        publication_targets,
        publication_pairs,
        publication_hashes,
        strict=True,
      ):
        if target != paired_target:
          raise OutputValidationError("publish transaction target order is inconsistent")
        _fsync_file(stage)
        publication_entries.append(
          {
            "role": role,
            "target_path": str(target),
            "stage_path": str(stage),
            "sha256": digest,
            "size_bytes": stage.stat().st_size,
          }
        )
      if cancelled():
        raise CancelledError("job cancelled before output publication")
      if time.monotonic() >= deadline:
        raise TimeoutError("job timed out before output publication")

      staged_journal = _stage_path(publish_journal)
      staged.append(staged_journal)
      _write_json_durable(
        staged_journal,
        self._publish_transaction_payload(job, source_hash, publication_entries),
      )
      try:
        os.link(staged_journal, publish_journal)
      except FileExistsError as exc:
        raise OutputConflictError(f"publish transaction marker already exists: {publish_journal}") from exc
      staged_journal_stat = staged_journal.stat(follow_symlinks=False)
      publish_journal_stat = publish_journal.stat(follow_symlinks=False)
      if (
        not stat.S_ISREG(staged_journal_stat.st_mode)
        or not stat.S_ISREG(publish_journal_stat.st_mode)
        or staged_journal_stat.st_dev != publish_journal_stat.st_dev
        or staged_journal_stat.st_ino != publish_journal_stat.st_ino
      ):
        raise OutputConflictError(f"publish transaction marker does not alias its durable stage: {publish_journal}")
      journal_identity = _file_identity(publish_journal_stat)
      _unlink_verified_identity(
        staged_journal,
        _file_identity(staged_journal_stat),
        description="publish transaction marker stage",
      )
      _fsync_directory(publish_journal.parent)

      for stage, target in publication_pairs:
        os.replace(stage, target)
        _fsync_directory(target.parent)
      _unlink_verified_identity(
        publish_journal,
        journal_identity,
        description="publish transaction marker",
      )
      _fsync_directory(publish_journal.parent)
      self._emit(progress, event="result", state="completed", output_sha256=video_hash)
      return result
    finally:
      for path in staged:
        path.unlink(missing_ok=True)

  def _load_completed_result(
    self,
    job: EncodeJob,
    source_hash: str,
    video_target: Path,
    metadata_target: Path,
    source: ProbeInfo | None = None,
  ) -> dict[str, Any] | None:
    if not video_target.is_file() or not metadata_target.is_file():
      return None
    try:
      payload = json.loads(metadata_target.read_text(encoding="utf-8"))
      if not isinstance(payload, dict):
        return None
      if (
        payload.get("schema_version") != 1 or not isinstance(payload.get("job_id"), str) or not payload["job_id"].strip() or payload.get("status") != "complete"
      ):
        return None
      input_record = payload.get("input", {})
      if not isinstance(input_record, dict) or input_record.get("sha256") != source_hash:
        return None
      if (
        input_record.get("artifact_id") != job.input.artifact_id
        or input_record.get("camera") != job.input.camera
        or input_record.get("kind") != job.input.kind
        or input_record.get("input_format") != job.input.input_format
        or input_record.get("segment_num") != job.input.segment_num
      ):
        return None
      if payload.get("encode") != asdict(job.encode):
        return None
      expected_time_mapping = asdict(job.time_mapping) if job.time_mapping is not None else None
      if payload.get("time_mapping") != expected_time_mapping:
        return None
      persisted_bitrate_policy = payload.get("bitrate_policy")
      if not isinstance(persisted_bitrate_policy, dict) or persisted_bitrate_policy.get("policy_version") != BITRATE_POLICY_VERSION:
        return None

      _, _, poster_target, frame_index_target, thumbnail_targets = self._resolved_outputs(job, None)
      output = payload.get("output", {})
      if not isinstance(output, dict):
        return None
      video_record = output.get("video", {})
      poster_record = output.get("poster", {})
      frame_index_record = output.get("frame_index", {})
      thumbnail_records = output.get("thumbnails", [])
      if video_record.get("path") != str(video_target) or poster_record.get("path") != str(poster_target):
        return None
      if not isinstance(thumbnail_records, list) or len(thumbnail_records) != len(thumbnail_targets):
        return None
      artifact_records = [(video_target, video_record), (poster_target, poster_record), (frame_index_target, frame_index_record)]
      artifact_records.extend(zip(thumbnail_targets, thumbnail_records, strict=True))
      for artifact_path, record in artifact_records:
        if not artifact_path.is_file() or not isinstance(record, dict) or record.get("path") != str(artifact_path):
          return None
        expected_hash = record.get("sha256")
        if not isinstance(expected_hash, str):
          return None
        with artifact_path.open("rb") as stream:
          if hashlib.file_digest(stream, "sha256").hexdigest() != expected_hash:
            return None
      cached_validation = self.validate(
        video_target,
        source=source,
        raw_hevc_frame_rate=job.encode.raw_hevc_frame_rate,
        full_decode=False,
        expected_audio=(job.input.input_format == "mpegts" and job.encode.preserve_audio and source is not None and source.audio_codec_name is not None),
        timeout_seconds=min(300, job.limits.timeout_seconds),
      )
      if source is None:
        return None
      selected_budget = persisted_bitrate_policy.get("selected_budget")
      attempt = selected_budget.get("attempt") if isinstance(selected_budget, dict) else None
      target_ratio = BITRATE_TARGET_RATIO if attempt == 1 else BITRATE_FALLBACK_RATIO if attempt == 2 else None
      if target_ratio is None:
        return None
      expected_bitrate_policy = self.bitrate_record(
        job,
        source,
        cached_validation.probe,
        self.bitrate_budget(job, source, attempt=attempt, total_ratio=target_ratio),
      )
      if not expected_bitrate_policy["accepted"] or persisted_bitrate_policy != expected_bitrate_policy:
        return None
      frame_index_payload = json.loads(frame_index_target.read_text(encoding="utf-8"))
      if not isinstance(frame_index_payload, dict):
        return None
      self._validate_frame_index_payload(
        frame_index_payload,
        self.probe(video_target, input_format="webm", timeout_seconds=min(300, job.limits.timeout_seconds)),
        job=job,
        video_sha256=video_record["sha256"],
      )
      return payload
    except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError, MediaWorkerError):
      return None

  def _validate_path_relationships(self, job: EncodeJob) -> None:
    declared_paths = {
      "input.path": job.input.path,
      "outputs.video_path": job.outputs.video_path,
      "outputs.metadata_path": job.outputs.metadata_path,
      "outputs.poster_path": job.outputs.poster_path,
      "outputs.frame_index_path": job.outputs.frame_index_path,
      "outputs.thumbnails_dir": job.outputs.thumbnails_dir,
    }
    for label, value in declared_paths.items():
      if value is not None and not Path(value).is_absolute():
        raise OutputConflictError(f"{label} must be an absolute local path")
    source = Path(job.input.path).resolve()
    video, metadata, poster, frame_index, thumbnails = self._resolved_outputs(job, None)
    paths = [video, metadata, poster, frame_index, *thumbnails]
    publish_journal = self._publish_journal_path(metadata)
    if source in paths:
      raise OutputConflictError("an output path resolves to the input artifact")
    if len(paths) != len(set(paths)):
      raise OutputConflictError("output paths must be unique")
    if publish_journal == source or publish_journal in paths:
      raise OutputConflictError("the publish transaction marker conflicts with an input or output path")

  def _resolved_outputs(
    self,
    job: EncodeJob,
    source: ProbeInfo | None,
  ) -> tuple[Path, Path, Path, Path, list[Path]]:
    del source
    video = Path(job.outputs.video_path).resolve()
    metadata = Path(job.outputs.metadata_path).resolve()
    poster = Path(job.outputs.poster_path).resolve() if job.outputs.poster_path else video.with_name(f"{video.stem}.poster.jpg")
    frame_index = Path(job.outputs.frame_index_path).resolve() if job.outputs.frame_index_path else video.with_name(f"{video.stem}.frames.json")
    thumbnails_dir = Path(job.outputs.thumbnails_dir).resolve() if job.outputs.thumbnails_dir else video.with_name(f"{video.stem}.thumbnails")
    thumbnails = [thumbnails_dir / f"{index:03d}.jpg" for index in range(job.encode.thumbnail_count)]
    return video, metadata, poster, frame_index, thumbnails

  def _ensure_disk_space(
    self,
    outputs: Iterable[Path],
    input_size: int,
    minimum_free_bytes: int,
    multiplier: float,
    maximum_derived_bytes: int,
  ) -> None:
    required = max(minimum_free_bytes, math.ceil(input_size * multiplier), maximum_derived_bytes)
    roots: dict[Path, int] = {}
    for output in outputs:
      root = _nearest_existing_parent(output.parent)
      roots[root] = shutil.disk_usage(root).free
    for root, free in roots.items():
      if free < required:
        raise DiskSpaceError(f"{root} has {free} bytes free; at least {required} bytes are required")

  def _poster_timestamp(self, duration: float) -> float:
    return round(max(0.0, min(duration * 0.1, max(0.0, duration - 0.05))), 6)

  def _thumbnail_timestamps(self, duration: float, count: int) -> list[float]:
    if count <= 0:
      return []
    return [round(max(0.0, min(duration * (index + 0.5) / count, max(0.0, duration - 0.05))), 6) for index in range(count)]

  def _generate_artwork(
    self,
    video: Path,
    poster: Path,
    thumbnails: list[Path],
    *,
    duration: float,
    width: int,
    deadline: float,
    cancelled: Callable[[], bool],
    progress: ProgressCallback | None,
  ) -> None:
    artwork = [
      ("poster", poster, self._poster_timestamp(duration)),
      *[
        (f"thumbnail_{index + 1}", path, timestamp)
        for index, (path, timestamp) in enumerate(zip(thumbnails, self._thumbnail_timestamps(duration, len(thumbnails)), strict=True))
      ],
    ]
    image_bytes = 0
    for phase, output, timestamp in artwork:
      remaining_image_bytes = MAX_TOTAL_IMAGE_BYTES - image_bytes
      if remaining_image_bytes <= 0:
        raise OutputValidationError(f"poster and thumbnails exceed the aggregate limit of {MAX_TOTAL_IMAGE_BYTES} bytes")
      self._generate_image(
        video,
        output,
        timestamp=timestamp,
        width=width,
        deadline=deadline,
        cancelled=cancelled,
        phase=phase,
        progress=progress,
        maximum_output_bytes=min(MAX_IMAGE_BYTES, remaining_image_bytes),
      )
      image_bytes += output.stat().st_size
      if image_bytes > MAX_TOTAL_IMAGE_BYTES:
        raise OutputValidationError(f"poster and thumbnails exceed the aggregate limit of {MAX_TOTAL_IMAGE_BYTES} bytes")

  def _generate_image(
    self,
    video: Path,
    output: Path,
    *,
    timestamp: float,
    width: int,
    deadline: float,
    cancelled: Callable[[], bool],
    phase: str,
    progress: ProgressCallback | None,
    maximum_output_bytes: int,
  ) -> None:
    self._emit(progress, event="phase", phase=phase, state="started", timestamp_seconds=timestamp)
    video = video.resolve()
    output = output.resolve()
    command = [
      self.ffmpeg,
      "-hide_banner",
      "-nostdin",
      "-v",
      "error",
      "-ss",
      f"{timestamp:.6f}",
      *_input_guard_args("webm"),
      "-i",
      str(video),
      "-frames:v",
      "1",
      "-vf",
      f"scale=w='min({width},iw)':h=-2",
      "-q:v",
      "3",
      "-fs",
      str(maximum_output_bytes),
      "-protocol_whitelist",
      LOCAL_PROTOCOL_WHITELIST,
      "-f",
      "image2",
      str(output),
    ]
    self._run_process(
      command,
      phase=phase,
      duration_seconds=None,
      deadline=deadline,
      cancelled=cancelled,
      progress=progress,
      output_path=output,
      maximum_output_bytes=maximum_output_bytes,
    )
    if not output.is_file() or output.stat().st_size == 0:
      raise OutputValidationError(f"{phase} was not generated")
    self._emit(progress, event="phase", phase=phase, state="completed", timestamp_seconds=timestamp)

  def _cancelled(self, cancel_event: threading.Event | None, cancel_file: str | None) -> Callable[[], bool]:
    def check() -> bool:
      return (cancel_event is not None and cancel_event.is_set()) or (cancel_file is not None and Path(cancel_file).exists())

    return check

  def _remaining(self, deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
      raise TimeoutError("job timed out")
    return remaining

  def _run_process(
    self,
    command: list[str],
    *,
    phase: str,
    duration_seconds: float | None,
    deadline: float,
    cancelled: Callable[[], bool],
    progress: ProgressCallback | None,
    output_path: Path | None = None,
    maximum_output_bytes: int | None = None,
  ) -> None:
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
      process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=os.name != "nt",
        creationflags=creation_flags,
      )
    except FileNotFoundError as exc:
      raise ToolUnavailableError(f"executable is unavailable: {command[0]}") from exc

    lines: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=MAX_CAPTURE_QUEUE_LINES)
    stderr_tail: collections.deque[str] = collections.deque(maxlen=400)

    def drain(name: str, stream: Any) -> None:
      try:
        while line := stream.readline(MAX_CAPTURE_LINE_CHARS + 1):
          if len(line) > MAX_CAPTURE_LINE_CHARS:
            lines.put(("overflow", line[:MAX_CAPTURE_LINE_CHARS]))
          else:
            lines.put((name, line.rstrip("\r\n")))
      finally:
        lines.put((name, None))

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
      threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
      threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
      reader.start()

    progress_block: dict[str, str] = {}
    closed_streams = 0
    stopped_for: str | None = None
    while closed_streams < 2 or process.poll() is None:
      if stopped_for is None and cancelled():
        stopped_for = "cancelled"
        self._stop_process(process)
      if stopped_for is None and time.monotonic() >= deadline:
        stopped_for = "timeout"
        self._stop_process(process)
      if stopped_for is None and output_path is not None and maximum_output_bytes is not None:
        try:
          output_size = output_path.stat().st_size
        except FileNotFoundError:
          output_size = 0
        if output_size > maximum_output_bytes:
          stopped_for = "output_limit"
          self._stop_process(process)
      try:
        stream_name, line = lines.get(timeout=0.2)
      except queue.Empty:
        continue
      if line is None:
        closed_streams += 1
        continue
      if stream_name == "overflow" and stopped_for is None:
        stopped_for = "capture_limit"
        self._stop_process(process)
        continue
      if stopped_for is not None:
        continue
      if stream_name == "stderr":
        stderr_tail.append(line[-4_000:])
        continue
      if "=" not in line:
        continue
      key, value = line.split("=", 1)
      progress_block[key] = value
      if key == "progress":
        self._emit_progress_block(progress, phase, duration_seconds, progress_block)
        progress_block = {}

    try:
      return_code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
      self._stop_process(process, force=True)
      return_code = process.wait(timeout=5)
    for reader in readers:
      reader.join(timeout=1)

    if stopped_for == "cancelled":
      raise CancelledError(f"{phase} cancelled")
    if stopped_for == "timeout":
      raise TimeoutError(f"{phase} exceeded the job timeout")
    if stopped_for == "output_limit":
      raise OutputValidationError(f"{phase} output exceeded the hard limit of {maximum_output_bytes} bytes")
    if stopped_for == "capture_limit":
      raise MediaWorkerError(f"{phase} process output contained a line longer than {MAX_CAPTURE_LINE_CHARS} characters")
    if return_code != 0:
      error = "\n".join(stderr_tail).strip()
      raise MediaWorkerError(f"{phase} command failed with exit code {return_code}: {error[-4000:] or 'no error output'}")

  def _stop_process(self, process: subprocess.Popen[str], force: bool = False) -> None:
    try:
      if os.name != "nt":
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
      elif force:
        process.kill()
      else:
        process.terminate()
      if not force:
        try:
          process.wait(timeout=3)
        except subprocess.TimeoutExpired:
          self._stop_process(process, force=True)
    except ProcessLookupError:
      pass

  def _emit_progress_block(
    self,
    callback: ProgressCallback | None,
    phase: str,
    duration_seconds: float | None,
    values: dict[str, str],
  ) -> None:
    out_time_us = _parse_int(values.get("out_time_us"))
    fraction = None
    if duration_seconds and out_time_us is not None:
      fraction = max(0.0, min(1.0, out_time_us / 1_000_000 / duration_seconds))
    self._emit(
      callback,
      event="progress",
      phase=phase,
      state=values.get("progress", "continue"),
      frame=_parse_int(values.get("frame")),
      fps=_parse_float(values.get("fps")),
      out_time_us=out_time_us,
      total_size=_parse_int(values.get("total_size")),
      speed=values.get("speed"),
      fraction=round(fraction, 6) if fraction is not None else None,
    )

  def _emit(self, callback: ProgressCallback | None, **event: Any) -> None:
    if callback is not None:
      callback({"timestamp": _utc_now(), **event})
