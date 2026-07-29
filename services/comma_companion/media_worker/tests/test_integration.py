from __future__ import annotations

import hashlib
import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from comma_companion_media_worker.contract import EncodeJob
from comma_companion_media_worker.worker import InputValidationError, MediaWorker, ProbeError


def _has_encoder(ffmpeg: str, encoder: str) -> bool:
  result = subprocess.run(
    [ffmpeg, "-hide_banner", "-encoders"],
    capture_output=True,
    text=True,
    errors="replace",
    timeout=20,
    check=False,
  )
  return result.returncode == 0 and encoder in result.stdout


def test_raw_hevc_to_validated_av1_webm(tmp_path: Path) -> None:
  ffmpeg = shutil.which("ffmpeg")
  ffprobe = shutil.which("ffprobe")
  if not ffmpeg or not ffprobe:
    pytest.skip("ffmpeg and ffprobe are not installed")
  if not _has_encoder(ffmpeg, "libx265"):
    pytest.skip("ffmpeg does not provide libx265 for the synthetic HEVC fixture")
  if not _has_encoder(ffmpeg, "libsvtav1"):
    pytest.skip("ffmpeg does not provide libsvtav1")

  source = tmp_path / "camera.bytes"
  generated = subprocess.run(
    [
      ffmpeg,
      "-hide_banner",
      "-loglevel",
      "error",
      "-f",
      "lavfi",
      "-i",
      "testsrc2=size=160x96:rate=20:duration=1",
      "-an",
      "-c:v",
      "libx265",
      "-x265-params",
      "log-level=error",
      "-f",
      "hevc",
      str(source),
    ],
    capture_output=True,
    text=True,
    errors="replace",
    timeout=120,
    check=False,
  )
  if generated.returncode != 0:
    pytest.skip(f"could not generate synthetic HEVC fixture: {generated.stderr[-500:]}")

  output = tmp_path / "road.av1.webm"
  metadata = tmp_path / "road.av1.json"
  job = EncodeJob.from_dict(
    {
      "schema_version": 1,
      "job_id": "integration-1",
      "input": {
        "path": str(source),
        "camera": "road",
        "kind": "fcamera",
        "input_format": "raw_hevc",
        "segment_num": 4,
      },
      "outputs": {
        "video_path": str(output),
        "metadata_path": str(metadata),
      },
      "encode": {
        "preset": 10,
        "crf": 40,
        "logical_processors": 2,
        "thumbnail_count": 2,
        "thumbnail_width": 160,
      },
      "limits": {
        "timeout_seconds": 300,
        "minimum_free_bytes": 0,
      },
      "time_mapping": {
        "artifact_id": "map-1",
        "version": "test-v1",
      },
    }
  )

  result = MediaWorker(ffmpeg=ffmpeg, ffprobe=ffprobe).encode(job)

  assert result["status"] == "complete"
  assert result["raw_retained"] is True
  assert result["input"]["input_format"] == "raw_hevc"
  assert result["input"]["raw_hevc_frame_rate_applied"] == 20
  assert result["output"]["video"]["probe"]["codec_name"] == "av1"
  assert result["output"]["video"]["probe"]["frame_count"] == 20
  assert result["output"]["video"]["probe"]["path"] == str(output)
  assert result["output"]["video"]["cues_front_loaded"] is True
  assert result["output"]["video"]["decoded"] is True
  bitrate_policy = result["bitrate_policy"]
  assert bitrate_policy["policy"] == "strictly_lower_total_average_bitrate"
  assert bitrate_policy["accepted"] is True
  assert bitrate_policy["bitrate_reduced"] is True
  assert bitrate_policy["size_reduced"] is True
  assert bitrate_policy["output_total_bitrate_bps"] < bitrate_policy["selected_budget"]["input_total_bitrate_bps"]
  assert result["output"]["video"]["size_bytes"] < result["input"]["size_bytes"]
  assert bitrate_policy["selected_budget"]["target_ratio"]["decimal"] in (0.8, 0.6)
  frame_index_record = result["output"]["frame_index"]
  frame_index_path = Path(frame_index_record["path"])
  frame_index = json.loads(frame_index_path.read_text(encoding="utf-8"))
  assert frame_index_record["mime_type"] == "application/json"
  assert frame_index_record["sha256"] == hashlib.sha256(frame_index_path.read_bytes()).hexdigest()
  assert frame_index_record["frame_count"] == 20
  assert frame_index["join_key"] == ["camera", "segment_num", "segment_frame_id"]
  assert frame_index["camera"] == "road"
  assert frame_index["segment_num"] == 4
  assert frame_index["frame_count"] == 20
  assert [frame["ordinal"] for frame in frame_index["frames"]] == list(range(20))
  assert [frame["segment_frame_id"] for frame in frame_index["frames"]] == list(range(20))
  assert all(left["pts"] < right["pts"] for left, right in itertools.pairwise(frame_index["frames"]))
  assert all(frame["duration"] > 0 and frame["duration_us"] > 0 for frame in frame_index["frames"])
  assert frame_index["frames"][0]["keyframe"] is True
  assert result["time_mapping"]["artifact_id"] == "map-1"
  assert output.is_file()
  assert metadata.is_file()
  assert Path(result["output"]["poster"]["path"]).is_file()
  assert len(result["output"]["thumbnails"]) == 2
  assert source.is_file()


def test_qcamera_mpegts_audio_to_validated_av1_opus_webm(tmp_path: Path) -> None:
  ffmpeg = shutil.which("ffmpeg")
  ffprobe = shutil.which("ffprobe")
  if not ffmpeg or not ffprobe:
    pytest.skip("ffmpeg and ffprobe are not installed")
  for encoder in ("libx264", "aac", "libsvtav1", "libopus"):
    if not _has_encoder(ffmpeg, encoder):
      pytest.skip(f"ffmpeg does not provide {encoder}")

  source = tmp_path / "qcamera.ts"
  generated = subprocess.run(
    [
      ffmpeg,
      "-hide_banner",
      "-loglevel",
      "error",
      "-f",
      "lavfi",
      "-i",
      "testsrc2=size=160x96:rate=20:duration=1",
      "-f",
      "lavfi",
      "-i",
      "sine=frequency=1000:sample_rate=48000:duration=1",
      "-map",
      "0:v:0",
      "-map",
      "1:a:0",
      "-c:v",
      "libx264",
      "-pix_fmt",
      "yuv420p",
      "-c:a",
      "aac",
      "-b:a",
      "64k",
      "-f",
      "mpegts",
      str(source),
    ],
    capture_output=True,
    text=True,
    errors="replace",
    timeout=120,
    check=False,
  )
  if generated.returncode != 0:
    pytest.skip(f"could not generate synthetic qcamera fixture: {generated.stderr[-500:]}")

  output = tmp_path / "qcamera.av1.webm"
  metadata = tmp_path / "qcamera.av1.json"
  job = EncodeJob.from_dict(
    {
      "schema_version": 1,
      "job_id": "integration-qcamera",
      "input": {
        "path": str(source),
        "camera": "qcamera",
        "kind": "qcamera",
        "input_format": "mpegts",
        "segment_num": 7,
      },
      "outputs": {
        "video_path": str(output),
        "metadata_path": str(metadata),
      },
      "encode": {
        "preset": 10,
        "crf": 40,
        "logical_processors": 2,
        "thumbnail_count": 0,
        "preserve_audio": True,
      },
      "limits": {
        "timeout_seconds": 300,
        "minimum_free_bytes": 0,
      },
    }
  )

  result = MediaWorker(ffmpeg=ffmpeg, ffprobe=ffprobe).encode(job)

  assert result["status"] == "complete"
  assert result["input"]["input_format"] == "mpegts"
  assert result["input"]["probe"]["codec_name"] == "h264"
  assert result["input"]["probe"]["audio_codec_name"] == "aac"
  assert result["input"]["raw_hevc_frame_rate_applied"] is None
  assert result["output"]["video"]["probe"]["codec_name"] == "av1"
  assert result["output"]["video"]["probe"]["audio_codec_name"] == "opus"
  assert result["output"]["video"]["probe"]["audio_stream_count"] == 1
  assert result["output"]["video"]["decoded"] is True
  bitrate_policy = result["bitrate_policy"]
  assert bitrate_policy["accepted"] is True
  assert bitrate_policy["bitrate_reduced"] is True
  assert bitrate_policy["size_reduced"] is True
  assert bitrate_policy["selected_budget"]["reserved_audio_bitrate_bps"] == 40_000
  assert bitrate_policy["output_total_bitrate_bps"] < bitrate_policy["selected_budget"]["input_total_bitrate_bps"]
  assert result["output"]["video"]["size_bytes"] < result["input"]["size_bytes"]
  assert output.is_file()
  assert metadata.is_file()
  assert source.is_file()


@pytest.mark.parametrize(
  "payload",
  [
    b"#EXTM3U\n#EXTINF:10,\nhttp://127.0.0.1:9/private.ts\n",
    b"#EXTM3U\n#EXTINF:10,\nfile:///etc/passwd\n",
    b"ffconcat version 1.0\nfile 'http://127.0.0.1:9/private.ts'\n",
  ],
)
def test_forced_mpegts_probe_rejects_playlist_protocols(tmp_path: Path, payload: bytes) -> None:
  ffprobe = shutil.which("ffprobe")
  if not ffprobe:
    pytest.skip("ffprobe is not installed")

  source = tmp_path / "qcamera.ts"
  source.write_bytes(payload)

  with pytest.raises(ProbeError, match="ffprobe rejected"):
    MediaWorker(ffprobe=ffprobe).probe(source, input_format="mpegts", timeout_seconds=5)


def test_bounded_probe_rejects_midstream_oversized_hevc_frame(tmp_path: Path) -> None:
  ffmpeg = shutil.which("ffmpeg")
  ffprobe = shutil.which("ffprobe")
  if not ffmpeg or not ffprobe:
    pytest.skip("ffmpeg and ffprobe are not installed")
  if not _has_encoder(ffmpeg, "libx265"):
    pytest.skip("ffmpeg does not provide libx265")

  encoded_frames: list[bytes] = []
  for width in (64, 5000, 64):
    frame_path = tmp_path / f"{width}.hevc"
    generated = subprocess.run(
      [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=size={width}x64:rate=1:duration=1",
        "-frames:v",
        "1",
        "-an",
        "-c:v",
        "libx265",
        "-x265-params",
        "log-level=error",
        "-f",
        "hevc",
        str(frame_path),
      ],
      capture_output=True,
      text=True,
      errors="replace",
      timeout=120,
      check=False,
    )
    if generated.returncode != 0:
      pytest.skip(f"could not generate dynamic-resolution HEVC fixture: {generated.stderr[-500:]}")
    encoded_frames.append(frame_path.read_bytes())
  source = tmp_path / "mixed.hevc"
  source.write_bytes(b"".join(encoded_frames))

  with pytest.raises(InputValidationError, match=r"5000x64|width 5000"):
    MediaWorker(ffmpeg=ffmpeg, ffprobe=ffprobe).probe(source, input_format="raw_hevc", timeout_seconds=30)


def test_qcamera_audio_tail_is_rejected_before_encode(tmp_path: Path) -> None:
  ffmpeg = shutil.which("ffmpeg")
  ffprobe = shutil.which("ffprobe")
  if not ffmpeg or not ffprobe:
    pytest.skip("ffmpeg and ffprobe are not installed")
  if not _has_encoder(ffmpeg, "libx264"):
    pytest.skip("ffmpeg does not provide libx264")

  source = tmp_path / "qcamera.ts"
  generated = subprocess.run(
    [
      ffmpeg,
      "-hide_banner",
      "-loglevel",
      "error",
      "-f",
      "lavfi",
      "-i",
      "testsrc2=size=160x96:rate=20:duration=1",
      "-f",
      "lavfi",
      "-i",
      "sine=frequency=1000:sample_rate=48000:duration=4.8",
      "-map",
      "0:v:0",
      "-map",
      "1:a:0",
      "-c:v",
      "libx264",
      "-pix_fmt",
      "yuv420p",
      "-c:a",
      "aac",
      "-f",
      "mpegts",
      str(source),
    ],
    capture_output=True,
    text=True,
    errors="replace",
    timeout=120,
    check=False,
  )
  if generated.returncode != 0:
    pytest.skip(f"could not generate qcamera duration-skew fixture: {generated.stderr[-500:]}")

  job = EncodeJob.from_dict(
    {
      "schema_version": 1,
      "job_id": "integration-duration-skew",
      "input": {
        "path": str(source),
        "camera": "qcamera",
        "kind": "qcamera",
        "input_format": "mpegts",
        "segment_num": 8,
      },
      "outputs": {
        "video_path": str(tmp_path / "qcamera.av1.webm"),
        "metadata_path": str(tmp_path / "qcamera.av1.json"),
      },
      "encode": {"thumbnail_count": 0},
      "limits": {"timeout_seconds": 120, "minimum_free_bytes": 0},
    }
  )

  with pytest.raises(InputValidationError, match="audio duration"):
    MediaWorker(ffmpeg=ffmpeg, ffprobe=ffprobe).encode(job)
