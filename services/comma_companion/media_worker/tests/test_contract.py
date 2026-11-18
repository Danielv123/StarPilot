from __future__ import annotations

from pathlib import Path

import pytest

from comma_companion_media_worker.contract import ContractError, EncodeJob


def minimal_job() -> dict:
  archive = (Path.cwd() / "archive").resolve()
  return {
    "schema_version": 1,
    "job_id": "job-1",
    "input": {
      "path": str(archive / "raw.bin"),
      "camera": "road",
      "kind": "fcamera",
      "input_format": "raw_hevc",
      "segment_num": 4,
    },
    "outputs": {
      "video_path": str(archive / "road.av1.webm"),
      "metadata_path": str(archive / "road.av1.json"),
    },
  }


def test_contract_applies_low_resource_safe_defaults() -> None:
  job = EncodeJob.from_dict(minimal_job())

  assert job.encode.encoder == "libsvtav1"
  assert job.encode.preset == 10
  assert job.encode.crf == 38
  assert job.encode.logical_processors == 2
  assert job.encode.raw_hevc_frame_rate == 20
  assert job.encode.preserve_audio is True
  assert job.encode.audio_bitrate_kbps == 32
  assert job.encode.thumbnail_count == 6
  assert job.retain_raw is True
  assert job.limits.timeout_seconds == 14_400


def test_contract_normalizes_sha256() -> None:
  payload = minimal_job()
  payload["input"]["expected_sha256"] = "AB" * 32

  job = EncodeJob.from_dict(payload)

  assert job.input.expected_sha256 == "ab" * 32


@pytest.mark.parametrize(
  ("change", "message"),
  [
    (lambda job: job.update(schema_version=2), "unsupported schema_version"),
    (lambda job: job.update(extra=True), "unknown fields"),
    (lambda job: job["input"].pop("segment_num"), "input.segment_num"),
    (lambda job: job["input"].update(input_format="hls"), "input.input_format"),
    (lambda job: job["outputs"].update(video_path=str(Path.cwd() / "archive" / "out.mp4")), "must end in .webm"),
    (lambda job: job["input"].update(path="file:/tmp/secret.hevc"), "absolute local path"),
    (lambda job: job["outputs"].update(video_path="http:/127.0.0.1/out.webm"), "absolute local path"),
    (lambda job: job["encode"].update(encoder="libaom-av1"), "must be libsvtav1"),
    (lambda job: job["encode"].update(pixel_format="yuv420p10le"), "browser compatibility"),
    (lambda job: job["encode"].update(overwrite=True), "must be false"),
  ],
)
def test_contract_rejects_unsupported_values(change, message: str) -> None:
  payload = minimal_job()
  payload["encode"] = {}
  change(payload)

  with pytest.raises(ContractError, match=message):
    EncodeJob.from_dict(payload)


def test_empty_time_mapping_is_rejected() -> None:
  payload = minimal_job()
  payload["time_mapping"] = {}

  with pytest.raises(ContractError, match="at least one"):
    EncodeJob.from_dict(payload)
