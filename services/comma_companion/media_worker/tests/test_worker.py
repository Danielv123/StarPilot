from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from comma_companion_media_worker.contract import ContractError, EncodeJob
from comma_companion_media_worker.worker import (
  ANALYZE_DURATION_US,
  BITRATE_FALLBACK_RATIO,
  BITRATE_TARGET_RATIO,
  LOCAL_PROTOCOL_WHITELIST,
  MAX_CAPTURE_BYTES,
  MAX_CAPTURE_LINE_CHARS,
  MAX_FRAMES,
  MAX_HEIGHT,
  MAX_INPUT_BYTES,
  MAX_OUTPUT_BYTES,
  MAX_PROBE_PACKETS,
  MAX_SINGLE_ALLOCATION_BYTES,
  MAX_WIDTH,
  PROBE_SIZE_BYTES,
  BitrateReductionError,
  CancelledError,
  DiskSpaceError,
  FrameScan,
  InputValidationError,
  MediaWorker,
  OutputConflictError,
  OutputValidationError,
  ProbeError,
  ProbeInfo,
  TimeoutError,
  ToolVersions,
  ValidationResult,
  _published_hardlink_alias,
  _stage_path,
  _webm_cues_front_loaded,
)


def job_for(tmp_path: Path) -> EncodeJob:
  return EncodeJob.from_dict(
    {
      "schema_version": 1,
      "job_id": "job-1",
      "input": {
        "path": str(tmp_path / "camera.data"),
        "camera": "road",
        "kind": "fcamera",
        "input_format": "raw_hevc",
        "segment_num": 4,
      },
      "outputs": {
        "video_path": str(tmp_path / "road.av1.webm"),
        "metadata_path": str(tmp_path / "road.av1.json"),
      },
      "encode": {
        "thumbnail_count": 2,
      },
      "limits": {
        "minimum_free_bytes": 0,
      },
    }
  )


def test_published_hardlink_alias_accepts_cifs_synthetic_inodes(tmp_path: Path) -> None:
  source = tmp_path / "source"
  target = tmp_path / "target"
  source.write_bytes(b"journal")
  os.link(source, target)
  source_stat = source.stat()
  target_stat = target.stat()
  synthetic_target_stat = SimpleNamespace(
    st_mode=target_stat.st_mode,
    st_dev=target_stat.st_dev,
    st_ino=target_stat.st_ino + 1,
    st_nlink=target_stat.st_nlink,
    st_size=target_stat.st_size,
  )

  assert _published_hardlink_alias(source, target, source_stat, synthetic_target_stat)


def raw_hevc_probe(path: str = "camera.data") -> ProbeInfo:
  return ProbeInfo(
    path=path,
    format_name="hevc",
    codec_name="hevc",
    codec_type="video",
    width=1928,
    height=1208,
    pixel_format="yuv420p",
    average_frame_rate=25.0,
    reported_frame_rate=25.0,
    duration_seconds=None,
    frame_count=1200,
    size_bytes=75_000_000,
    raw_elementary_stream=True,
    video_duration_seconds=60.0,
    max_frame_width=1928,
    max_frame_height=1208,
    scan_packet_count=1200,
  )


def av1_probe(path: str = "road.av1.webm", frame_count: int = 3) -> ProbeInfo:
  return ProbeInfo(
    path=path,
    format_name="matroska,webm",
    codec_name="av1",
    codec_type="video",
    width=1928,
    height=1208,
    pixel_format="yuv420p",
    average_frame_rate=20.0,
    reported_frame_rate=20.0,
    duration_seconds=frame_count / 20,
    frame_count=frame_count,
    size_bytes=1_000_000,
    raw_elementary_stream=False,
    time_base="1/1000",
    video_duration_seconds=frame_count / 20,
    max_frame_width=1928,
    max_frame_height=1208,
    scan_packet_count=frame_count,
  )


def mpegts_probe(path: str = "qcamera.ts") -> ProbeInfo:
  return ProbeInfo(
    path=path,
    format_name="mpegts",
    codec_name="h264",
    codec_type="video",
    width=526,
    height=330,
    pixel_format="yuv420p",
    average_frame_rate=20.0,
    reported_frame_rate=20.0,
    duration_seconds=60.0,
    frame_count=1200,
    size_bytes=2_500_000,
    raw_elementary_stream=False,
    audio_codec_name="aac",
    audio_channels=1,
    audio_sample_rate=48_000,
    time_base="1/90000",
    video_stream_count=1,
    audio_stream_count=1,
    video_duration_seconds=60.0,
    audio_duration_seconds=60.0,
    container_duration_seconds=60.0,
    audio_time_base="1/90000",
    max_frame_width=526,
    max_frame_height=330,
    scanned_audio_channels_max=1,
    scanned_audio_sample_rate_min=48_000,
    scanned_audio_sample_rate_max=48_000,
    scan_packet_count=4_020,
  )


def prepare_publish_transaction(
  worker: MediaWorker,
  job: EncodeJob,
  source_sha256: str,
) -> tuple[Path, list[dict[str, object]]]:
  video, metadata, poster, frame_index, thumbnails = worker._resolved_outputs(job, None)
  targets = worker._publication_targets(video, metadata, poster, frame_index, thumbnails)
  entries: list[dict[str, object]] = []
  for ordinal, (role, target) in enumerate(targets):
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = _stage_path(target)
    content = f"{role}-{ordinal}".encode()
    stage.write_bytes(content)
    entries.append(
      {
        "role": role,
        "target_path": str(target),
        "stage_path": str(stage),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
      }
    )
  journal = worker._publish_journal_path(metadata)
  journal.write_text(
    json.dumps(worker._publish_transaction_payload(job, source_sha256, entries)),
    encoding="utf-8",
  )
  return journal, entries


def run_fake_successful_encode(
  worker: MediaWorker,
  job: EncodeJob,
  source: ProbeInfo,
  output_probe: ProbeInfo,
) -> dict[str, object]:
  def fake_process(_command: list[str], **kwargs: object) -> None:
    output_path = kwargs["output_path"]
    assert isinstance(output_path, Path)
    output_path.write_bytes(b"x" * output_probe.size_bytes)

  def fake_artwork(_video: Path, poster: Path, thumbnails: list[Path], **_kwargs: object) -> None:
    poster.write_bytes(b"poster")
    for ordinal, thumbnail in enumerate(thumbnails):
      thumbnail.write_bytes(f"thumbnail-{ordinal}".encode())

  frame_index_payload = {
    "frame_count": output_probe.frame_count,
    "time_base": {"numerator": 1, "denominator": 1000, "text": "1/1000"},
    "join_key": ["camera", "segment_num", "segment_frame_id"],
    "ordinal_basis": 0,
    "source_frame_key": "segment_frame_id",
  }
  with (
    patch.object(worker, "tool_versions", return_value=ToolVersions(ffmpeg="test", ffprobe="test")),
    patch.object(worker, "probe", return_value=source),
    patch.object(worker, "ensure_encoder"),
    patch.object(worker, "_ensure_disk_space"),
    patch.object(worker, "_run_process", side_effect=fake_process),
    patch.object(worker, "validate", return_value=ValidationResult(probe=output_probe, cues_front_loaded=True, decoded=True)),
    patch.object(worker, "_generate_artwork", side_effect=fake_artwork),
    patch.object(worker, "build_frame_index", return_value=frame_index_payload),
    patch.object(worker, "_validate_frame_index_payload"),
  ):
    return worker.encode(job)


def test_raw_hevc_command_forces_twenty_fps_and_content_demuxer(tmp_path: Path) -> None:
  job = job_for(tmp_path)
  worker = MediaWorker()

  command = worker.build_encode_command(job, raw_hevc_probe(job.input.path), tmp_path / "stage.webm")

  input_index = command.index("-i")
  assert command[command.index("-r") + 1] == "20"
  assert command[command.index("-f") + 1] == "hevc"
  assert command.index("-r") < command.index("-f") < input_index
  assert command[command.index("-codec_whitelist") + 1] == "hevc"
  assert command[command.index("-preset") + 1] == "10"
  assert command[command.index("-threads") + 1] == "2"
  assert command[command.index("-svtav1-params") + 1] == "lp=2"
  assert command[command.index("-g") + 1] == "80"
  assert command[command.index("-maxrate:v") + 1] == "7600000"
  assert command[command.index("-bufsize:v") + 1] == "15200000"
  assert command[command.index("-cues_to_front") + 1] == "1"
  assert "-reserve_index_space" not in command
  assert command[command.index("-fs") + 1] == str(MAX_OUTPUT_BYTES)
  assert command.count("-protocol_whitelist") == 2
  assert command[-2:] == ["webm", str(tmp_path / "stage.webm")]


def test_declared_raw_hevc_always_forces_demuxer(tmp_path: Path) -> None:
  job = job_for(tmp_path)
  source = raw_hevc_probe(job.input.path)
  source = ProbeInfo(
    **{
      **source.as_dict(),
      "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
      "raw_elementary_stream": False,
      "duration_seconds": 60.0,
    }
  )

  command = MediaWorker().build_encode_command(job, source, tmp_path / "stage.webm")

  input_index = command.index("-i")
  assert command[command.index("-f") + 1] == "hevc"
  assert command.index("-f") < input_index
  assert command[input_index + 1] == job.input.path


def test_qcamera_audio_is_transcoded_to_low_bitrate_opus(tmp_path: Path) -> None:
  payload = job_for(tmp_path).as_dict()
  payload["input"]["camera"] = "qcamera"
  payload["input"]["input_format"] = "mpegts"
  job = EncodeJob.from_dict(payload)
  source = mpegts_probe(job.input.path)

  command = MediaWorker().build_encode_command(job, source, tmp_path / "stage.webm")

  assert command[command.index("-map", command.index("-map") + 1) + 1] == "0:a:0?"
  assert command[command.index("-c:a") + 1] == "libopus"
  assert command[command.index("-b:a") + 1] == "32k"
  assert command[command.index("-vbr") + 1] == "constrained"
  assert command[command.index("-maxrate:v") + 1] == "213333"
  assert "-an" not in command


def test_mpegts_command_forces_demuxer_and_local_protocols(tmp_path: Path) -> None:
  payload = job_for(tmp_path).as_dict()
  payload["input"].update(camera="qcamera", kind="qcamera", input_format="mpegts")
  job = EncodeJob.from_dict(payload)

  command = MediaWorker().build_encode_command(job, mpegts_probe(job.input.path), tmp_path / "stage.webm")
  input_index = command.index("-i")

  assert command[command.index("-f") + 1] == "mpegts"
  assert command.index("-f") < input_index
  whitelist_index = command.index("-protocol_whitelist")
  assert command[whitelist_index + 1] == LOCAL_PROTOCOL_WHITELIST
  assert whitelist_index < input_index
  assert "http" not in LOCAL_PROTOCOL_WHITELIST
  assert "https" not in LOCAL_PROTOCOL_WHITELIST


def test_road_camera_does_not_copy_detected_audio(tmp_path: Path) -> None:
  job = job_for(tmp_path)
  source = raw_hevc_probe(job.input.path)
  source = ProbeInfo(**{**source.as_dict(), "audio_codec_name": "aac", "audio_channels": 1, "audio_sample_rate": 48000})

  command = MediaWorker().build_encode_command(job, source, tmp_path / "stage.webm")

  assert "-an" in command
  assert "-c:a" not in command


def test_bitrate_budget_uses_exact_duration_and_conservative_reserves(tmp_path: Path) -> None:
  worker = MediaWorker()
  raw_job = job_for(tmp_path)
  raw_budget = worker.bitrate_budget(raw_job, raw_hevc_probe(raw_job.input.path))

  assert raw_budget.total_ratio == BITRATE_TARGET_RATIO == Fraction(4, 5)
  assert raw_budget.input_duration_us == 60_000_000
  assert raw_budget.input_total_bitrate_bps == 10_000_000
  assert raw_budget.target_total_bitrate_bps == 8_000_000
  assert raw_budget.reserved_audio_bitrate_bps == 0
  assert raw_budget.reserved_container_bitrate_bps == 400_000
  assert raw_budget.video_maxrate_bps == 7_600_000

  qcamera_payload = raw_job.as_dict()
  qcamera_payload["input"].update(camera="qcamera", kind="qcamera", input_format="mpegts")
  qcamera_job = EncodeJob.from_dict(qcamera_payload)
  qcamera_budget = worker.bitrate_budget(qcamera_job, mpegts_probe(qcamera_job.input.path))

  assert qcamera_budget.input_duration_us == 60_000_000
  assert qcamera_budget.input_total_bitrate_bps == 333_334
  assert qcamera_budget.target_total_bitrate_bps == 266_667
  assert qcamera_budget.reserved_audio_bitrate_bps == 40_000
  assert qcamera_budget.reserved_container_bitrate_bps == 13_334
  assert qcamera_budget.video_maxrate_bps == 213_333


def test_bitrate_budget_rejects_input_too_small_for_mux_reserve(tmp_path: Path) -> None:
  job = job_for(tmp_path)
  source = ProbeInfo(**{**raw_hevc_probe(job.input.path).as_dict(), "frame_count": 20, "size_bytes": 1_000})

  with pytest.raises(InputValidationError, match="too low"):
    MediaWorker().bitrate_budget(job, source)


def test_bitrate_record_requires_lower_average_bitrate_and_smaller_file(tmp_path: Path) -> None:
  worker = MediaWorker()
  job = job_for(tmp_path)
  source = raw_hevc_probe(job.input.path)
  budget = worker.bitrate_budget(job, source, attempt=2, total_ratio=BITRATE_FALLBACK_RATIO)
  smaller = ProbeInfo(**{**av1_probe(frame_count=1200).as_dict(), "duration_seconds": 60.0, "size_bytes": 60_000_000})
  larger = ProbeInfo(**{**smaller.as_dict(), "size_bytes": 80_000_000})
  larger_but_lower_rate = ProbeInfo(**{**larger.as_dict(), "duration_seconds": 100.0})
  smaller_but_higher_rate = ProbeInfo(**{**smaller.as_dict(), "duration_seconds": 40.0})

  accepted = worker.bitrate_record(job, source, smaller, budget)
  rejected = worker.bitrate_record(job, source, larger, budget)
  rejected_size = worker.bitrate_record(job, source, larger_but_lower_rate, budget)
  rejected_rate = worker.bitrate_record(job, source, smaller_but_higher_rate, budget)

  assert accepted["accepted"] is True
  assert accepted["bitrate_reduced"] is True
  assert accepted["size_reduced"] is True
  assert accepted["selected_budget"]["attempt"] == 2
  assert accepted["selected_budget"]["target_ratio"]["decimal"] == 0.6
  assert rejected["accepted"] is False
  assert rejected["bitrate_reduced"] is False
  assert rejected["size_reduced"] is False
  assert rejected_size["accepted"] is False
  assert rejected_size["bitrate_reduced"] is True
  assert rejected_size["size_reduced"] is False
  assert rejected_rate["accepted"] is False
  assert rejected_rate["bitrate_reduced"] is False
  assert rejected_rate["size_reduced"] is True


def test_encode_retries_at_three_fifths_when_first_output_is_not_lower(tmp_path: Path) -> None:
  payload = job_for(tmp_path).as_dict()
  payload["encode"]["thumbnail_count"] = 0
  job = EncodeJob.from_dict(payload)
  Path(job.input.path).write_bytes(b"source")
  worker = MediaWorker()
  source = ProbeInfo(
    **{
      **raw_hevc_probe(job.input.path).as_dict(),
      "frame_count": 20,
      "size_bytes": 100_000,
      "video_duration_seconds": 1.0,
    }
  )
  output_probes = [
    ProbeInfo(**{**av1_probe(frame_count=20).as_dict(), "size_bytes": 110_000}),
    ProbeInfo(**{**av1_probe(frame_count=20).as_dict(), "size_bytes": 70_000}),
  ]
  encode_attempt = 0
  progress_events: list[dict[str, object]] = []

  def fake_process(_command: list[str], **kwargs: object) -> None:
    nonlocal encode_attempt
    output_path = kwargs["output_path"]
    assert isinstance(output_path, Path)
    output_path.write_bytes(b"x" * output_probes[encode_attempt].size_bytes)
    encode_attempt += 1

  def fake_artwork(_video: Path, poster: Path, thumbnails: list[Path], **_kwargs: object) -> None:
    poster.write_bytes(b"poster")
    assert thumbnails == []

  frame_index_payload = {
    "frame_count": 20,
    "time_base": {"numerator": 1, "denominator": 1000, "text": "1/1000"},
    "join_key": ["camera", "segment_num", "segment_frame_id"],
    "ordinal_basis": 0,
    "source_frame_key": "segment_frame_id",
  }
  with (
    patch.object(worker, "tool_versions", return_value=ToolVersions(ffmpeg="test", ffprobe="test")),
    patch.object(worker, "probe", return_value=source),
    patch.object(worker, "ensure_encoder"),
    patch.object(worker, "_ensure_disk_space"),
    patch.object(worker, "_run_process", side_effect=fake_process),
    patch.object(
      worker,
      "validate",
      side_effect=[
        ValidationResult(probe=output_probes[0], cues_front_loaded=True, decoded=True),
        ValidationResult(probe=output_probes[1], cues_front_loaded=True, decoded=True),
      ],
    ),
    patch.object(worker, "_generate_artwork", side_effect=fake_artwork),
    patch.object(worker, "build_frame_index", return_value=frame_index_payload),
    patch.object(worker, "_validate_frame_index_payload"),
  ):
    result = worker.encode(job, progress=progress_events.append)

  assert encode_attempt == 2
  assert result["bitrate_policy"]["accepted"] is True
  assert result["bitrate_policy"]["selected_budget"]["attempt"] == 2
  assert result["bitrate_policy"]["selected_budget"]["target_ratio"]["decimal"] == 0.6
  assert result["output"]["video"]["size_bytes"] == 70_000
  assert any(event.get("phase") == "bitrate_validation" and event.get("state") == "retrying" for event in progress_events)


def test_encode_publishes_nothing_when_both_bitrate_attempts_fail(tmp_path: Path) -> None:
  payload = job_for(tmp_path).as_dict()
  payload["encode"]["thumbnail_count"] = 0
  job = EncodeJob.from_dict(payload)
  Path(job.input.path).write_bytes(b"source")
  worker = MediaWorker()
  source = ProbeInfo(
    **{
      **raw_hevc_probe(job.input.path).as_dict(),
      "frame_count": 20,
      "size_bytes": 100_000,
      "video_duration_seconds": 1.0,
    }
  )
  output_probe = ProbeInfo(**{**av1_probe(frame_count=20).as_dict(), "size_bytes": 110_000})

  def fake_process(_command: list[str], **kwargs: object) -> None:
    output_path = kwargs["output_path"]
    assert isinstance(output_path, Path)
    output_path.write_bytes(b"x" * output_probe.size_bytes)

  with (
    patch.object(worker, "tool_versions", return_value=ToolVersions(ffmpeg="test", ffprobe="test")),
    patch.object(worker, "probe", return_value=source),
    patch.object(worker, "ensure_encoder"),
    patch.object(worker, "_ensure_disk_space"),
    patch.object(worker, "_run_process", side_effect=fake_process),
    patch.object(worker, "validate", return_value=ValidationResult(probe=output_probe, cues_front_loaded=True, decoded=True)),
    pytest.raises(BitrateReductionError, match="not strictly lower"),
  ):
    worker.encode(job)

  assert not Path(job.outputs.video_path).exists()
  assert not Path(job.outputs.metadata_path).exists()


def test_encode_recovers_hash_verified_partial_publish_transaction(tmp_path: Path) -> None:
  payload = job_for(tmp_path).as_dict()
  payload["encode"]["thumbnail_count"] = 0
  job = EncodeJob.from_dict(payload)
  source_path = Path(job.input.path)
  source_path.write_bytes(b"s" * 100_000)
  source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
  worker = MediaWorker()
  source = ProbeInfo(
    **{
      **raw_hevc_probe(job.input.path).as_dict(),
      "frame_count": 20,
      "size_bytes": 100_000,
      "video_duration_seconds": 1.0,
    }
  )
  output_probe = ProbeInfo(**{**av1_probe(frame_count=20).as_dict(), "size_bytes": 70_000})
  journal, entries = prepare_publish_transaction(worker, job, source_sha256)
  first_stage = Path(str(entries[0]["stage_path"]))
  first_target = Path(str(entries[0]["target_path"]))
  os.replace(first_stage, first_target)
  for entry in entries[1:]:
    Path(str(entry["stage_path"])).unlink()
  unrelated = tmp_path / "unrelated.keep"
  unrelated.write_bytes(b"do not remove")

  result = run_fake_successful_encode(worker, job, source, output_probe)

  assert result["status"] == "complete"
  assert not journal.exists()
  assert first_target.is_file()
  assert unrelated.read_bytes() == b"do not remove"
  assert source_path.is_file()


def test_encode_removes_only_same_inode_orphaned_journal_stage(tmp_path: Path) -> None:
  payload = job_for(tmp_path).as_dict()
  payload["encode"]["thumbnail_count"] = 0
  job = EncodeJob.from_dict(payload)
  source_path = Path(job.input.path)
  source_path.write_bytes(b"s" * 100_000)
  source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
  worker = MediaWorker()
  source = ProbeInfo(
    **{
      **raw_hevc_probe(job.input.path).as_dict(),
      "frame_count": 20,
      "size_bytes": 100_000,
      "video_duration_seconds": 1.0,
    }
  )
  output_probe = ProbeInfo(**{**av1_probe(frame_count=20).as_dict(), "size_bytes": 70_000})
  journal, _entries = prepare_publish_transaction(worker, job, source_sha256)
  orphaned_journal_stage = _stage_path(journal)
  os.link(journal, orphaned_journal_stage)
  unrelated_stage = _stage_path(journal)
  unrelated_stage.write_bytes(b"unrelated")

  result = run_fake_successful_encode(worker, job, source, output_probe)

  assert result["status"] == "complete"
  assert not journal.exists()
  assert not orphaned_journal_stage.exists()
  assert unrelated_stage.read_bytes() == b"unrelated"


def test_encode_refuses_corrupt_partial_publish_leftover(tmp_path: Path) -> None:
  payload = job_for(tmp_path).as_dict()
  payload["encode"]["thumbnail_count"] = 0
  job = EncodeJob.from_dict(payload)
  source_path = Path(job.input.path)
  source_path.write_bytes(b"s" * 100_000)
  source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
  worker = MediaWorker()
  source = ProbeInfo(
    **{
      **raw_hevc_probe(job.input.path).as_dict(),
      "frame_count": 20,
      "size_bytes": 100_000,
      "video_duration_seconds": 1.0,
    }
  )
  output_probe = ProbeInfo(**{**av1_probe(frame_count=20).as_dict(), "size_bytes": 70_000})
  journal, entries = prepare_publish_transaction(worker, job, source_sha256)
  first_stage = Path(str(entries[0]["stage_path"]))
  first_target = Path(str(entries[0]["target_path"]))
  os.replace(first_stage, first_target)
  first_target.write_bytes(b"z" * int(entries[0]["size_bytes"]))
  unrelated = tmp_path / "unrelated.keep"
  unrelated.write_bytes(b"do not remove")

  with pytest.raises(OutputConflictError, match="unexpected SHA-256"):
    run_fake_successful_encode(worker, job, source, output_probe)

  assert journal.is_file()
  assert first_target.is_file()
  assert unrelated.read_bytes() == b"do not remove"
  assert source_path.is_file()


def test_recovery_refuses_leftover_replaced_after_hash_verification(tmp_path: Path) -> None:
  payload = job_for(tmp_path).as_dict()
  payload["encode"]["thumbnail_count"] = 0
  job = EncodeJob.from_dict(payload)
  source_path = Path(job.input.path)
  source_path.write_bytes(b"s" * 100_000)
  source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
  worker = MediaWorker()
  journal, entries = prepare_publish_transaction(worker, job, source_sha256)
  video, metadata, poster, frame_index, thumbnails = worker._resolved_outputs(job, None)
  targets = worker._publication_targets(video, metadata, poster, frame_index, thumbnails)
  first_stage = Path(str(entries[0]["stage_path"]))
  replacement = first_stage.with_name(f"{first_stage.name}.replacement")
  original_verify = worker._verify_transaction_file
  replaced = False

  def verify_then_replace(path: Path, entry: dict[str, object], **kwargs: object) -> object:
    nonlocal replaced
    identity = original_verify(path, entry, **kwargs)
    if not replaced:
      replacement.write_bytes(path.read_bytes())
      os.replace(replacement, path)
      replaced = True
    return identity

  with (
    patch.object(worker, "_verify_transaction_file", side_effect=verify_then_replace),
    pytest.raises(OutputConflictError, match="changed after verification"),
  ):
    worker._recover_publish_transaction(
      journal,
      job=job,
      source_sha256=source_sha256,
      expected_targets=targets,
      deadline=time.monotonic() + 10,
      cancelled=lambda: False,
      progress=None,
    )

  assert journal.is_file()
  assert first_stage.is_file()
  assert first_stage.read_bytes() == f"{entries[0]['role']}-0".encode()


def test_partial_publish_failure_leaves_journal_owned_target_for_safe_retry(tmp_path: Path) -> None:
  payload = job_for(tmp_path).as_dict()
  payload["encode"]["thumbnail_count"] = 0
  job = EncodeJob.from_dict(payload)
  source_path = Path(job.input.path)
  source_path.write_bytes(b"s" * 100_000)
  worker = MediaWorker()
  source = ProbeInfo(
    **{
      **raw_hevc_probe(job.input.path).as_dict(),
      "frame_count": 20,
      "size_bytes": 100_000,
      "video_duration_seconds": 1.0,
    }
  )
  output_probe = ProbeInfo(**{**av1_probe(frame_count=20).as_dict(), "size_bytes": 70_000})
  video, metadata, poster, _frame_index, _thumbnails = worker._resolved_outputs(job, None)
  journal = worker._publish_journal_path(metadata)
  real_replace = os.replace
  replace_count = 0

  def fail_second_publication(
    source_path_arg: str | bytes | os.PathLike[str] | os.PathLike[bytes], target_path_arg: str | bytes | os.PathLike[str] | os.PathLike[bytes]
  ) -> None:
    nonlocal replace_count
    replace_count += 1
    if replace_count == 2:
      raise OSError("simulated publish interruption")
    real_replace(source_path_arg, target_path_arg)

  with (
    patch("comma_companion_media_worker.worker.os.replace", side_effect=fail_second_publication),
    pytest.raises(OSError, match="simulated publish interruption"),
  ):
    run_fake_successful_encode(worker, job, source, output_probe)

  assert journal.is_file()
  assert poster.is_file()
  assert not video.exists()
  assert source_path.is_file()

  result = run_fake_successful_encode(worker, job, source, output_probe)

  assert result["status"] == "complete"
  assert not journal.exists()
  assert poster.is_file()
  assert video.is_file()
  assert source_path.is_file()


def test_probe_forces_declared_format_and_uses_bounded_decoded_scan(tmp_path: Path) -> None:
  artifact = tmp_path / "not-video.txt"
  artifact.write_bytes(b"synthetic")
  response = {
    "streams": [
      {
        "index": 0,
        "codec_name": "hevc",
        "codec_type": "video",
        "width": 1928,
        "height": 1208,
        "pix_fmt": "yuv420p",
        "avg_frame_rate": "25/1",
        "r_frame_rate": "25/1",
        "time_base": "1/1200000",
      },
    ],
    "format": {"format_name": "hevc", "size": "9"},
  }
  completed = subprocess.CompletedProcess(["ffprobe"], 0, json.dumps(response), "")
  scan = FrameScan(
    video_frame_count=1200,
    video_duration_seconds=60.0,
    audio_duration_seconds=None,
    max_frame_width=1928,
    max_frame_height=1208,
    audio_channels_max=None,
    audio_sample_rate_min=None,
    audio_sample_rate_max=None,
    packet_count=1200,
  )
  worker = MediaWorker()

  with (
    patch.object(worker, "_run_capture", return_value=completed) as capture,
    patch.object(worker, "_scan_decoded_frames", return_value=scan),
  ):
    result = worker.probe(artifact, input_format="raw_hevc")

  command = capture.call_args.args[0]
  assert command[command.index("-protocol_whitelist") + 1] == LOCAL_PROTOCOL_WHITELIST
  assert command[command.index("-f") + 1] == "hevc"
  assert command[command.index("-probesize") + 1] == str(PROBE_SIZE_BYTES)
  assert command[command.index("-analyzeduration") + 1] == str(ANALYZE_DURATION_US)
  assert command[command.index("-max_probe_packets") + 1] == str(MAX_PROBE_PACKETS)
  assert command[command.index("-max_pixels") + 1] == str(MAX_WIDTH * MAX_HEIGHT)
  assert command[command.index("-max_streams") + 1] == "1"
  assert command[command.index("-max_alloc") + 1] == str(MAX_SINGLE_ALLOCATION_BYTES)
  assert command[command.index("-codec_whitelist") + 1] == "hevc"
  assert "-count_frames" not in command
  assert result.codec_name == "hevc"
  assert result.raw_elementary_stream is True
  assert result.effective_duration(20) == 60
  assert result.audio_codec_name is None
  assert result.max_frame_width == 1928
  assert result.scan_packet_count == 1200


@pytest.mark.parametrize(
  "malicious_path",
  [
    "http://127.0.0.1:9/video.ts",
    "https://example.invalid/video.ts",
    "file:///etc/passwd",
    "concat:file1.ts|file2.ts",
  ],
)
def test_protocol_input_paths_are_rejected_before_ffprobe(malicious_path: str) -> None:
  with (
    patch("subprocess.Popen") as popen,
    pytest.raises(ProbeError, match="not a regular file"),
  ):
    MediaWorker().probe(malicious_path, input_format="mpegts")

  popen.assert_not_called()


def test_oversized_input_is_rejected_before_hash_or_subprocess(tmp_path: Path) -> None:
  job = job_for(tmp_path)
  worker = MediaWorker()

  with (
    patch.object(Path, "is_file", return_value=True),
    patch.object(Path, "stat", return_value=SimpleNamespace(st_size=MAX_INPUT_BYTES + 1)),
    patch.object(worker, "tool_versions") as versions,
    patch("comma_companion_media_worker.worker._sha256") as sha256,
    pytest.raises(ProbeError, match="worker limit"),
  ):
    worker.encode(job)

  versions.assert_not_called()
  sha256.assert_not_called()


def test_encode_job_rejects_relative_or_url_shaped_worker_paths(tmp_path: Path) -> None:
  relative_input = job_for(tmp_path).as_dict()
  relative_input["input"]["path"] = "file:/tmp/secret.hevc"
  with pytest.raises(ContractError, match=r"input\.path.*absolute local"):
    EncodeJob.from_dict(relative_input)

  relative_output = job_for(tmp_path).as_dict()
  relative_output["outputs"]["video_path"] = "http:/127.0.0.1:9/out.webm"
  with pytest.raises(ContractError, match=r"outputs\.video_path.*absolute local"):
    EncodeJob.from_dict(relative_output)


@pytest.mark.parametrize(
  "playlist",
  [
    "#EXTM3U\n#EXTINF:10,\nhttp://127.0.0.1:9/private\n",
    "file:///etc/passwd\n",
    "concat:file1.ts|file2.ts\n",
  ],
)
def test_playlist_bytes_are_forced_through_mpegts_without_nested_protocols(tmp_path: Path, playlist: str) -> None:
  source = tmp_path / "qcamera.ts"
  source.write_text(playlist, encoding="utf-8")
  rejected = subprocess.CompletedProcess(["ffprobe"], 1, "", "Invalid data found when processing input")
  worker = MediaWorker()

  with (
    patch.object(worker, "_run_capture", return_value=rejected) as capture,
    pytest.raises(ProbeError, match="ffprobe rejected"),
  ):
    worker.probe(source, input_format="mpegts")

  command = capture.call_args.args[0]
  assert command[command.index("-f") + 1] == "mpegts"
  assert command[command.index("-protocol_whitelist") + 1] == LOCAL_PROTOCOL_WHITELIST


def test_source_media_caps_and_canonical_mapping_are_enforced(tmp_path: Path) -> None:
  worker = MediaWorker()
  raw_job = job_for(tmp_path)
  raw = raw_hevc_probe(raw_job.input.path)

  worker._validate_source_media(raw_job, raw)
  for camera in ("road", "wide", "driver", "qcamera"):
    generic_raw_payload = raw_job.as_dict()
    generic_raw_payload["input"].update(kind="video", camera=camera)
    worker._validate_source_media(EncodeJob.from_dict(generic_raw_payload), raw)

  invalid_raw_sources = [
    ProbeInfo(**{**raw.as_dict(), "width": MAX_WIDTH + 1}),
    ProbeInfo(**{**raw.as_dict(), "height": MAX_HEIGHT + 1}),
    ProbeInfo(**{**raw.as_dict(), "max_frame_width": MAX_WIDTH + 1}),
    ProbeInfo(**{**raw.as_dict(), "max_frame_height": MAX_HEIGHT + 1}),
    ProbeInfo(**{**raw.as_dict(), "frame_count": MAX_FRAMES + 1}),
    ProbeInfo(**{**raw.as_dict(), "frame_count": 4000}),
    ProbeInfo(**{**raw.as_dict(), "video_stream_count": 2}),
    ProbeInfo(**{**raw.as_dict(), "other_stream_count": 1}),
    ProbeInfo(**{**raw.as_dict(), "audio_stream_count": 1, "audio_codec_name": "aac"}),
    ProbeInfo(**{**raw.as_dict(), "codec_name": "h264"}),
  ]
  for invalid in invalid_raw_sources:
    with pytest.raises(InputValidationError):
      worker._validate_source_media(raw_job, invalid)

  mpeg_job_payload = raw_job.as_dict()
  mpeg_job_payload["input"].update(camera="qcamera", kind="qcamera", input_format="mpegts")
  mpeg_job = EncodeJob.from_dict(mpeg_job_payload)
  mpeg = mpegts_probe(mpeg_job.input.path)
  worker._validate_source_media(mpeg_job, mpeg)
  generic_mpeg_payload = mpeg_job.as_dict()
  generic_mpeg_payload["input"]["kind"] = "video"
  worker._validate_source_media(EncodeJob.from_dict(generic_mpeg_payload), mpeg)

  invalid_mpeg_sources = [
    ProbeInfo(**{**mpeg.as_dict(), "audio_stream_count": 2}),
    ProbeInfo(**{**mpeg.as_dict(), "audio_channels": 3}),
    ProbeInfo(**{**mpeg.as_dict(), "audio_sample_rate": 96_000}),
    ProbeInfo(**{**mpeg.as_dict(), "audio_codec_name": "mp3"}),
    ProbeInfo(**{**mpeg.as_dict(), "video_duration_seconds": 181.0}),
    ProbeInfo(**{**mpeg.as_dict(), "audio_duration_seconds": 61.01}),
    ProbeInfo(**{**mpeg.as_dict(), "container_duration_seconds": 61.01}),
    ProbeInfo(**{**mpeg.as_dict(), "scanned_audio_channels_max": 3}),
    ProbeInfo(**{**mpeg.as_dict(), "scanned_audio_sample_rate_min": 4_000}),
    ProbeInfo(**{**mpeg.as_dict(), "scanned_audio_sample_rate_max": 96_000}),
    ProbeInfo(**{**mpeg.as_dict(), "other_stream_count": 1}),
    ProbeInfo(**{**mpeg.as_dict(), "codec_name": "hevc"}),
  ]
  for invalid in invalid_mpeg_sources:
    with pytest.raises(InputValidationError):
      worker._validate_source_media(mpeg_job, invalid)

  wrong_mapping = mpeg_job.as_dict()
  wrong_mapping["input"].update(camera="road", kind="fcamera")
  with pytest.raises(InputValidationError, match="canonical media mapping"):
    worker._validate_source_media(EncodeJob.from_dict(wrong_mapping), mpeg)
  generic_wrong_mapping = mpeg_job.as_dict()
  generic_wrong_mapping["input"].update(camera="road", kind="video")
  with pytest.raises(InputValidationError, match="canonical media mapping"):
    worker._validate_source_media(EncodeJob.from_dict(generic_wrong_mapping), mpeg)


def test_frame_index_uses_exact_pts_and_unambiguous_rlog_join(tmp_path: Path) -> None:
  job = job_for(tmp_path)
  video = av1_probe(frame_count=3)
  ffprobe_payload = {
    "streams": [{"time_base": "1/1000"}],
    "frames": [
      {"pts": 0, "duration": 50, "key_frame": 1},
      {"pts": 50, "duration": 50, "key_frame": 0},
      {"pts": 100, "duration": 50, "key_frame": 0},
    ],
  }
  completed = subprocess.CompletedProcess(["ffprobe"], 0, json.dumps(ffprobe_payload), "")
  worker = MediaWorker()

  with patch.object(worker, "_run_capture", return_value=completed):
    result = worker.build_frame_index(
      tmp_path / "stage.webm",
      job=job,
      video_probe=video,
      video_sha256="b" * 64,
      timeout_seconds=10,
    )

  assert result["schema_version"] == 1
  assert result["join_key"] == ["camera", "segment_num", "segment_frame_id"]
  assert result["source_frame_key"] == "segment_frame_id"
  assert result["ordinal_basis"] == 0
  assert result["camera"] == "road"
  assert result["segment_num"] == 4
  assert result["time_base"] == {"numerator": 1, "denominator": 1000, "text": "1/1000"}
  assert result["frame_count"] == 3
  assert result["duration_inference_count"] == 0
  assert result["frames"] == [
    {"ordinal": 0, "segment_frame_id": 0, "pts": 0, "duration": 50, "pts_us": 0, "duration_us": 50_000, "keyframe": True},
    {"ordinal": 1, "segment_frame_id": 1, "pts": 50, "duration": 50, "pts_us": 50_000, "duration_us": 50_000, "keyframe": False},
    {"ordinal": 2, "segment_frame_id": 2, "pts": 100, "duration": 50, "pts_us": 100_000, "duration_us": 50_000, "keyframe": False},
  ]

  wrong_segment = json.loads(json.dumps(result))
  wrong_segment["segment_num"] = job.input.segment_num + 1
  with pytest.raises(OutputValidationError, match="identity differs from the media job"):
    worker._validate_frame_index_payload(
      wrong_segment,
      video,
      job=job,
      video_sha256="b" * 64,
    )


def test_frame_index_deterministically_fills_missing_frame_durations(tmp_path: Path) -> None:
  job = job_for(tmp_path)
  ffprobe_payload = {
    "streams": [{"time_base": "1/1000"}],
    "frames": [
      {"pts": 0, "key_frame": 1},
      {"pts": 50, "key_frame": 0},
      {"pts": 100, "key_frame": 0},
    ],
  }
  completed = subprocess.CompletedProcess(["ffprobe"], 0, json.dumps(ffprobe_payload), "")
  worker = MediaWorker()

  with patch.object(worker, "_run_capture", return_value=completed):
    result = worker.build_frame_index(
      tmp_path / "stage.webm",
      job=job,
      video_probe=av1_probe(frame_count=3),
      video_sha256="b" * 64,
      timeout_seconds=10,
    )

  assert result["duration_inference_count"] == 3
  assert [frame["duration"] for frame in result["frames"]] == [50, 50, 50]


def test_frame_index_rejects_missing_or_reordered_decoded_frames(tmp_path: Path) -> None:
  job = job_for(tmp_path)
  wrong_count = subprocess.CompletedProcess(
    ["ffprobe"],
    0,
    json.dumps(
      {
        "streams": [{"time_base": "1/1000"}],
        "frames": [
          {"pts": 0, "duration": 50, "key_frame": 1},
          {"pts": 50, "duration": 50, "key_frame": 0},
        ],
      }
    ),
    "",
  )
  worker = MediaWorker()
  with (
    patch.object(worker, "_run_capture", return_value=wrong_count),
    pytest.raises(OutputValidationError, match="2 rows.*3 frames"),
  ):
    worker.build_frame_index(
      tmp_path / "stage.webm",
      job=job,
      video_probe=av1_probe(frame_count=3),
      video_sha256="b" * 64,
      timeout_seconds=10,
    )

  reordered = subprocess.CompletedProcess(
    ["ffprobe"],
    0,
    json.dumps(
      {
        "streams": [{"time_base": "1/1000"}],
        "frames": [
          {"pts": 0, "duration": 50, "key_frame": 1},
          {"pts": 100, "duration": 50, "key_frame": 0},
          {"pts": 50, "duration": 50, "key_frame": 0},
        ],
      }
    ),
    "",
  )
  with (
    patch.object(worker, "_run_capture", return_value=reordered),
    pytest.raises(OutputValidationError, match="not strictly increasing"),
  ):
    worker.build_frame_index(
      tmp_path / "stage.webm",
      job=job,
      video_probe=av1_probe(frame_count=3),
      video_sha256="b" * 64,
      timeout_seconds=10,
    )


def test_webm_cues_must_precede_first_cluster(tmp_path: Path) -> None:
  front_loaded = tmp_path / "front.webm"
  front_loaded.write_bytes(b"header\x1c\x53\xbb\x6bcues\x1f\x43\xb6\x75cluster")
  trailing = tmp_path / "trailing.webm"
  trailing.write_bytes(b"header\x1f\x43\xb6\x75cluster\x1c\x53\xbb\x6bcues")

  assert _webm_cues_front_loaded(front_loaded) is True
  assert _webm_cues_front_loaded(trailing) is False


def test_default_artwork_paths_are_derived_beside_video(tmp_path: Path) -> None:
  job = job_for(tmp_path)

  video, metadata, poster, frame_index, thumbnails = MediaWorker()._resolved_outputs(job, None)

  assert video == tmp_path / "road.av1.webm"
  assert metadata == tmp_path / "road.av1.json"
  assert poster == tmp_path / "road.av1.poster.jpg"
  assert frame_index == tmp_path / "road.av1.frames.json"
  assert thumbnails == [
    tmp_path / "road.av1.thumbnails" / "000.jpg",
    tmp_path / "road.av1.thumbnails" / "001.jpg",
  ]


def test_completed_result_requires_every_artifact_and_matching_contract(tmp_path: Path) -> None:
  job = job_for(tmp_path)
  worker = MediaWorker()
  source_probe = raw_hevc_probe(job.input.path)
  video, metadata, poster, frame_index, thumbnails = worker._resolved_outputs(job, None)
  poster.parent.mkdir(parents=True, exist_ok=True)
  thumbnails[0].parent.mkdir(parents=True, exist_ok=True)
  records = []
  for path, content in [
    (video, b"video"),
    (poster, b"poster"),
    (frame_index, b'{"frames":[]}'),
    (thumbnails[0], b"thumb-0"),
    (thumbnails[1], b"thumb-1"),
  ]:
    path.write_bytes(content)
    records.append({"path": str(path), "sha256": hashlib.sha256(content).hexdigest()})
  payload = {
    "schema_version": 1,
    "job_id": job.job_id,
    "status": "complete",
    "input": {
      "sha256": "a" * 64,
      "artifact_id": job.input.artifact_id,
      "camera": job.input.camera,
      "kind": job.input.kind,
      "input_format": job.input.input_format,
      "segment_num": job.input.segment_num,
    },
    "output": {
      "video": records[0],
      "poster": records[1],
      "frame_index": records[2],
      "thumbnails": records[3:],
    },
    "encode": asdict(job.encode),
    "time_mapping": None,
  }
  output_probe = ProbeInfo(
    **{
      **av1_probe(str(video), frame_count=1200).as_dict(),
      "duration_seconds": 60.0,
      "video_duration_seconds": 60.0,
      "size_bytes": video.stat().st_size,
    }
  )
  payload["bitrate_policy"] = worker.bitrate_record(
    job,
    source_probe,
    output_probe,
    worker.bitrate_budget(job, source_probe),
  )
  metadata.write_text(json.dumps(payload), encoding="utf-8")

  with (
    patch.object(worker, "validate", return_value=ValidationResult(probe=output_probe, cues_front_loaded=True, decoded=False)),
    patch.object(worker, "probe", return_value=output_probe),
    patch.object(worker, "_validate_frame_index_payload"),
  ):
    assert worker._load_completed_result(job, "a" * 64, video, metadata, source_probe) == payload

  payload["job_id"] = "prior-job-id"
  metadata.write_text(json.dumps(payload), encoding="utf-8")
  with (
    patch.object(worker, "validate", return_value=ValidationResult(probe=output_probe, cues_front_loaded=True, decoded=False)),
    patch.object(worker, "probe", return_value=output_probe),
    patch.object(worker, "_validate_frame_index_payload"),
  ):
    assert worker._load_completed_result(job, "a" * 64, video, metadata, source_probe) == payload
  adopted = worker._adopt_completed_result(job, payload, metadata)
  persisted_adoption = json.loads(metadata.read_text(encoding="utf-8"))
  assert adopted["job_id"] == job.job_id
  assert adopted["status"] == "already_complete"
  assert persisted_adoption["job_id"] == job.job_id
  assert persisted_adoption["status"] == "complete"
  payload = persisted_adoption

  del payload["bitrate_policy"]
  metadata.write_text(json.dumps(payload), encoding="utf-8")
  with (
    patch.object(worker, "validate", return_value=ValidationResult(probe=output_probe, cues_front_loaded=True, decoded=False)),
    patch.object(worker, "probe", return_value=output_probe),
    patch.object(worker, "_validate_frame_index_payload"),
  ):
    assert worker._load_completed_result(job, "a" * 64, video, metadata, source_probe) is None
  payload["bitrate_policy"] = worker.bitrate_record(
    job,
    source_probe,
    output_probe,
    worker.bitrate_budget(job, source_probe),
  )
  metadata.write_text(json.dumps(payload), encoding="utf-8")

  thumbnails[1].unlink()
  with (
    patch.object(worker, "validate", return_value=ValidationResult(probe=output_probe, cues_front_loaded=True, decoded=False)),
    patch.object(worker, "probe", return_value=output_probe),
    patch.object(worker, "_validate_frame_index_payload"),
  ):
    assert worker._load_completed_result(job, "a" * 64, video, metadata, source_probe) is None


def test_subprocess_honors_total_deadline() -> None:
  worker = MediaWorker()

  with pytest.raises(TimeoutError, match="exceeded"):
    worker._run_process(
      [sys.executable, "-c", "import time; time.sleep(30)"],
      phase="test",
      duration_seconds=None,
      deadline=time.monotonic() + 0.1,
      cancelled=lambda: False,
      progress=None,
    )


def test_subprocess_honors_cancellation() -> None:
  worker = MediaWorker()

  with pytest.raises(CancelledError, match="cancelled"):
    worker._run_process(
      [sys.executable, "-c", "import time; time.sleep(30)"],
      phase="test",
      duration_seconds=None,
      deadline=time.monotonic() + 30,
      cancelled=lambda: True,
      progress=None,
    )


def test_capture_probe_process_honors_in_flight_cancellation() -> None:
  worker = MediaWorker()
  cancel_event = threading.Event()
  timer = threading.Timer(0.1, cancel_event.set)
  timer.start()
  try:
    with pytest.raises(CancelledError, match="probe scan cancelled"):
      worker._run_capture(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        phase="probe scan",
        deadline=time.monotonic() + 30,
        cancelled=cancel_event.is_set,
      )
  finally:
    timer.cancel()


def test_capture_probe_process_has_independent_wall_timeout() -> None:
  worker = MediaWorker()

  with pytest.raises(TimeoutError, match="hard probe timeout"):
    worker._run_capture(
      [sys.executable, "-c", "import time; time.sleep(30)"],
      phase="probe scan",
      deadline=time.monotonic() + 0.1,
      cancelled=lambda: False,
    )


def test_probe_capture_has_hard_stdout_and_stderr_budget() -> None:
  worker = MediaWorker()

  with pytest.raises(ProbeError, match="capture limit"):
    worker._run_capture(
      [sys.executable, "-c", f"import sys; sys.stdout.write('x' * {MAX_CAPTURE_BYTES + 1})"],
      phase="probe scan",
      deadline=time.monotonic() + 10,
      cancelled=lambda: False,
    )


def test_frame_scan_rejects_oversized_process_line(tmp_path: Path) -> None:
  class FakeProcess:
    def __init__(self) -> None:
      self.stdout = io.StringIO("x" * (MAX_CAPTURE_LINE_CHARS + 1) + "\n" + "noise\n" * 1_000)
      self.stderr = io.StringIO("")
      self.returncode = 0

    def poll(self) -> int:
      return self.returncode

    def wait(self, timeout: float | None = None) -> int:
      del timeout
      return self.returncode

  fake = FakeProcess()
  worker = MediaWorker()
  prior_threads = set(threading.enumerate())
  with (
    patch("subprocess.Popen", return_value=fake),
    patch.object(worker, "_stop_process"),
    pytest.raises(ProbeError, match="line longer"),
  ):
    worker._scan_decoded_frames(
      tmp_path / "input.hevc",
      input_format="raw_hevc",
      video_stream={"index": 0, "time_base": "1/20", "codec_name": "hevc"},
      audio_stream=None,
      deadline=time.monotonic() + 10,
      cancelled=lambda: False,
    )
  time.sleep(0.05)
  assert not [thread for thread in threading.enumerate() if thread not in prior_threads and thread.is_alive()]


def test_frame_scan_stops_at_video_frame_limit(tmp_path: Path) -> None:
  class FakeProcess:
    def __init__(self) -> None:
      self.stdout = io.StringIO(
        "".join(f"frame|media_type=video|stream_index=0|pts={index}|duration=1|width=64|height=64\n" for index in range(MAX_FRAMES + 1))
      )
      self.stderr = io.StringIO("")
      self.returncode = 0

    def poll(self) -> int:
      return self.returncode

    def wait(self, timeout: float | None = None) -> int:
      del timeout
      return self.returncode

  worker = MediaWorker()
  with (
    patch("subprocess.Popen", return_value=FakeProcess()),
    patch.object(worker, "_stop_process"),
    pytest.raises(InputValidationError, match=f"frame count exceeds the limit of {MAX_FRAMES}"),
  ):
    worker._scan_decoded_frames(
      tmp_path / "input.hevc",
      input_format="raw_hevc",
      video_stream={"index": 0, "time_base": "1/20", "codec_name": "hevc"},
      audio_stream=None,
      deadline=time.monotonic() + 10,
      cancelled=lambda: False,
    )


def test_frame_scan_accepts_ffprobe_pkt_duration_compatibility_field(tmp_path: Path) -> None:
  class FakeProcess:
    def __init__(self) -> None:
      self.stdout = io.StringIO(
        "frame|media_type=video|stream_index=0|pts=0|pkt_duration=4500|width=64|height=64\n"
        "frame|media_type=audio|stream_index=1|pts=0|pkt_duration=1920|nb_samples=1024|channels=1\n"
      )
      self.stderr = io.StringIO("")
      self.returncode = 0

    def poll(self) -> int:
      return self.returncode

    def wait(self, timeout: float | None = None) -> int:
      del timeout
      return self.returncode

  worker = MediaWorker()
  with patch("subprocess.Popen", return_value=FakeProcess()):
    result = worker._scan_decoded_frames(
      tmp_path / "qcamera.ts",
      input_format="mpegts",
      video_stream={"index": 0, "time_base": "1/90000", "codec_name": "h264"},
      audio_stream={"index": 1, "time_base": "1/90000", "codec_name": "aac", "sample_rate": "48000"},
      deadline=time.monotonic() + 10,
      cancelled=lambda: False,
    )

  assert result.video_frame_count == 1
  assert result.video_duration_seconds == pytest.approx(0.05)
  assert result.audio_duration_seconds == pytest.approx(1024 / 48_000)
  assert result.audio_sample_rate_min == 48_000
  assert result.audio_sample_rate_max == 48_000


def test_encode_output_byte_cap_cleans_staged_artifact(tmp_path: Path) -> None:
  job = job_for(tmp_path)
  Path(job.input.path).write_bytes(b"source")
  worker = MediaWorker()
  staged_paths: list[Path] = []

  def oversized_command(_job: EncodeJob, _source: ProbeInfo, staged_video: Path, _budget: object) -> list[str]:
    staged_paths.append(staged_video)
    return [
      sys.executable,
      "-c",
      "from pathlib import Path; import sys, time; Path(sys.argv[1]).write_bytes(b'x' * 2048); time.sleep(30)",
      str(staged_video),
    ]

  with (
    patch("comma_companion_media_worker.worker.MAX_OUTPUT_BYTES", 1_024),
    patch.object(worker, "tool_versions", return_value=ToolVersions(ffmpeg="test", ffprobe="test")),
    patch.object(worker, "probe", return_value=raw_hevc_probe(job.input.path)),
    patch.object(worker, "ensure_encoder"),
    patch.object(worker, "_ensure_disk_space"),
    patch.object(worker, "build_encode_command", side_effect=oversized_command),
    pytest.raises(OutputValidationError, match="hard limit"),
  ):
    worker.encode(job)

  assert len(staged_paths) == 1
  assert not staged_paths[0].exists()
  assert not Path(job.outputs.video_path).exists()
  assert not Path(job.outputs.metadata_path).exists()


def test_artwork_has_aggregate_byte_cap(tmp_path: Path) -> None:
  worker = MediaWorker()
  poster = tmp_path / "poster.jpg"
  thumbnails = [tmp_path / "one.jpg", tmp_path / "two.jpg"]

  def oversized_artwork(
    _video: Path,
    output: Path,
    **_kwargs,
  ) -> None:
    output.write_bytes(b"x" * 6)

  with (
    patch("comma_companion_media_worker.worker.MAX_IMAGE_BYTES", 8),
    patch("comma_companion_media_worker.worker.MAX_TOTAL_IMAGE_BYTES", 10),
    patch.object(worker, "_generate_image", side_effect=oversized_artwork) as generate,
    pytest.raises(OutputValidationError, match="aggregate limit"),
  ):
    worker._generate_artwork(
      tmp_path / "video.webm",
      poster,
      thumbnails,
      duration=1.0,
      width=160,
      deadline=time.monotonic() + 10,
      cancelled=lambda: False,
      progress=None,
    )

  assert [call.kwargs["maximum_output_bytes"] for call in generate.call_args_list] == [8, 4]
  assert not thumbnails[1].exists()


def test_disk_preflight_reserves_full_derived_budget(tmp_path: Path) -> None:
  worker = MediaWorker()

  with (
    patch("shutil.disk_usage", return_value=SimpleNamespace(free=100)),
    pytest.raises(DiskSpaceError, match="at least 123 bytes"),
  ):
    worker._ensure_disk_space(
      [tmp_path / "video.webm"],
      input_size=1,
      minimum_free_bytes=0,
      multiplier=1.0,
      maximum_derived_bytes=123,
    )


def test_ffmpeg_progress_is_structured_and_bounded() -> None:
  events = []

  MediaWorker()._emit_progress_block(
    events.append,
    "encode",
    10.0,
    {
      "frame": "100",
      "fps": "7.5",
      "out_time_us": "5000000",
      "total_size": "12345",
      "speed": "0.5x",
      "progress": "continue",
    },
  )

  assert events[0]["event"] == "progress"
  assert events[0]["phase"] == "encode"
  assert events[0]["frame"] == 100
  assert events[0]["fraction"] == 0.5
