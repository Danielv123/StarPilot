from __future__ import annotations

import bz2
import hashlib
import io
import json
import math
from pathlib import Path

import pytest
import zstandard as zstd

from cereal import log
from services.comma_companion.adapters.rlog import cli
from services.comma_companion.adapters.rlog import extractor
from services.comma_companion.adapters.rlog.extractor import (
  InputError,
  ResourceLimitError,
  _read_events,
  _read_log,
  discover_route,
  iter_route_records,
)


ROUTE = "00000001--abc123def0"


def event_bytes(
  service: str,
  mono_ns: int,
  configure=None,
  *,
  valid: bool = True,
) -> bytes:
  event = log.Event.new_message()
  event.logMonoTime = mono_ns
  event.valid = valid
  event.init(service)
  if configure is not None:
    configure(getattr(event, service))
  return event.to_bytes()


def car_params(data) -> None:
  data.brand = "hyundai"
  data.carFingerprint = "HYUNDAI_IONIQ_5"
  data.wheelbase = 3.0
  data.steerRatio = 14.2
  data.mass = 2100.0
  data.minSteerSpeed = 0.5
  data.openpilotLongitudinalControl = True
  torque = data.lateralTuning.init("torque")
  torque.latAccelFactor = 2.5
  torque.latAccelOffset = 0.04
  torque.friction = 0.11
  torque.steeringAngleDeadzoneDeg = 0.3


def init_data(data) -> None:
  data.gitCommit = "2747bf037c0f284500457f1befb4f52415e3285a"
  data.gitSrcCommit = "2747bf037c0f284500457f1befb4f52415e3285a"
  params = data.init("params")
  entries = params.init("entries", 9)
  entries[0].key = "NNFF"
  entries[0].value = b"1"
  entries[1].key = "UnrelatedSecret"
  entries[1].value = b"not-exported"
  entries[2].key = "FLMTrialApplied"
  entries[2].value = b"0"
  entries[3].key = "FLMActiveProfileId"
  entries[3].value = b""
  entries[4].key = "FLMActiveOverrides"
  entries[4].value = b"{}"
  entries[5].key = "AdvancedLongitudinalTune"
  entries[5].value = b"1"
  entries[6].key = "TrailerLoad"
  entries[6].value = b"1000"
  entries[7].key = "LateralTune"
  entries[7].value = b"0"
  entries[8].key = "TuningLevelConfirmed"
  entries[8].value = b"0"


def init_data_for_commit(commit: str):
  def configure(data) -> None:
    init_data(data)
    data.gitCommit = commit
    data.gitSrcCommit = commit

  return configure


def refined_init_data(data) -> None:
  data.gitCommit = extractor.REFINED_IONIQ5_CONTROLLER_SOURCE_COMMIT
  data.gitSrcCommit = extractor.REFINED_IONIQ5_CONTROLLER_SOURCE_COMMIT
  params = data.init("params")
  values = (
    ("AdvancedLateralTune", b"1"),
    ("ForceAutoTune", b"0"),
    ("ForceAutoTuneOff", b"1"),
    ("LateralTune", b"1"),
    ("LiveTorqueParameters", b""),
    ("NNFF", b"0"),
    ("NNFFLite", b"0"),
    ("SteerFriction", b"0.2"),
    ("SteerLatAccel", b"3.0"),
    ("TuningLevel", b"3"),
    ("TuningLevelConfirmed", b"1"),
  )
  entries = params.init("entries", len(values))
  for entry, (key, value) in zip(
    entries,
    values,
    strict=True,
  ):
    entry.key = key
    entry.value = value


def resolved_toggles(**overrides):
  values = {
    "force_auto_tune": False,
    "force_auto_tune_off": False,
    "friction": 0.11,
    "latAccelFactor": 2.5,
    "lateral_tune": True,
    "nnff": False,
    "nnff_lite": False,
    "nnff_model_name": None,
    "tuning_level": 3,
    "use_custom_friction": False,
    "use_custom_latAccelFactor": False,
  }
  values.update(overrides)

  def configure(data) -> None:
    data.starpilotToggles = json.dumps(
      values,
      allow_nan=False,
      separators=(",", ":"),
      sort_keys=True,
    )

  return configure


def gps_fix(latitude: float, longitude: float):
  def configure(data) -> None:
    data.hasFix = True
    data.latitude = latitude
    data.longitude = longitude
    data.unixTimestampMillis = 1_800_000_000_000

  return configure


def car_control(data) -> None:
  data.enabled = True
  data.latActive = True
  data.longActive = False
  data.actuators.torque = 0.15
  data.actuators.curvature = 0.002


def car_output(data) -> None:
  data.actuatorsOutput.torque = 0.12


def controls_state(data) -> None:
  data.desiredCurvature = 0.002
  torque = data.lateralControlState.init("torqueState")
  torque.active = True
  torque.actualLateralAccel = 0.18
  torque.desiredLateralAccel = 0.20
  torque.desiredLateralJerk = 0.03
  torque.output = 0.15
  torque.i = 0.02


def car_state(speed: float, angle_deg: float):
  def configure(data) -> None:
    data.vEgo = speed
    data.aEgo = 0.25
    data.steeringAngleDeg = angle_deg
    data.steeringRateDeg = abs(angle_deg)
    data.steeringPressed = True
    data.cruiseState.enabled = True
    data.cruiseState.speed = 20.0

  return configure


def road_index(data) -> None:
  data.frameId = 41
  data.encodeId = 40
  data.segmentNum = 1
  data.segmentId = 0
  data.segmentIdEncode = 3
  data.timestampSof = 2_020_000_000
  data.timestampEof = 2_025_000_000
  data.len = 12345


def road_index_without_encoder_time(data) -> None:
  data.frameId = 1
  data.encodeId = 1
  data.segmentNum = 0
  data.segmentId = 0
  data.len = 100


def device_started(started: bool):
  def configure(data) -> None:
    data.started = started
    data.freeSpacePercent = 75.0

  return configure


def selfdrive_active(active: bool):
  def configure(data) -> None:
    data.active = active
    data.enabled = active
    data.engageable = True

  return configure


def car_control_values(torque: float, curvature: float):
  def configure(data) -> None:
    car_control(data)
    data.actuators.torque = torque
    data.actuators.curvature = curvature

  return configure


def gps_anchor(data) -> None:
  data.hasFix = True
  data.latitude = 59.91
  data.longitude = 10.75
  data.unixTimestampMillis = 1_800_000_000_000


def sentinel(sentinel_type: str, signal: int = 0):
  def configure(data) -> None:
    data.type = sentinel_type
    data.signal = signal

  return configure


def live_torque(data) -> None:
  data.liveValid = True
  data.useParams = True
  data.latAccelFactorFiltered = 2.6
  data.latAccelOffsetFiltered = 0.05
  data.frictionCoefficientFiltered = 0.12


def unused_live_torque(data) -> None:
  live_torque(data)
  data.useParams = False


def live_parameters(data) -> None:
  data.roll = 0.01


def write_log(path: Path, payload: bytes, compression: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  if compression == "zstd":
    payload = zstd.ZstdCompressor().compress(payload)
  elif compression == "bzip2":
    payload = bz2.compress(payload)
  path.write_bytes(payload)


def values(records: list[dict], signal: str, tier: str = "full") -> tuple[list[int], list]:
  samples = [record for record in records if record["record"] == "series_chunk" and record["signal"] == signal and record["tier"] == tier]
  times = [time for record in samples for time in record["t_us"]]
  data = [value for record in samples for value in record["v"]]
  return times, data


def dynamics_rows(records: list[dict]) -> list[dict]:
  return [row for record in records if record["record"] == "dynamics_chunk" for row in record["rows"]]


def test_route_state_and_identity_carry_across_compression_formats(tmp_path: Path) -> None:
  segment_zero = tmp_path / f"{ROUTE}--0" / "rlog.zst"
  segment_one = tmp_path / f"{ROUTE}--1" / "rlog.bz2"
  write_log(
    segment_zero,
    b"".join(
      [
        event_bytes("initData", 900_000_000, init_data),
        event_bytes("sentinel", 1_000_000_000, sentinel("startOfRoute")),
        event_bytes("carParams", 1_000_000_000, car_params),
        event_bytes("carControl", 1_010_000_000, car_control),
        event_bytes("carOutput", 1_011_000_000, car_output),
        event_bytes("controlsState", 1_012_000_000, controls_state),
        event_bytes("carState", 1_020_000_000, car_state(4.0, 1.0)),
        event_bytes("sentinel", 1_030_000_000, sentinel("endOfSegment")),
      ]
    ),
    "zstd",
  )
  write_log(
    segment_one,
    b"".join(
      [
        event_bytes("sentinel", 2_000_000_000, sentinel("startOfSegment")),
        event_bytes("carControl", 2_010_000_000, car_control),
        event_bytes("carOutput", 2_011_000_000, car_output),
        event_bytes("controlsState", 2_012_000_000, controls_state),
        event_bytes("roadEncodeIdx", 2_020_000_000, road_index),
        event_bytes("carState", 2_021_000_000, car_state(5.0, 2.0)),
        event_bytes("carState", 2_031_000_000, car_state(5.5, 2.5)),
        event_bytes("carState", 2_041_000_000, car_state(6.0, 3.0)),
        event_bytes("sentinel", 2_050_000_000, sentinel("endOfRoute", 2)),
      ]
    ),
    "bzip2",
  )

  route = discover_route([tmp_path], route_id=ROUTE)
  records = list(iter_route_records(route, chunk_size=16))
  manifest = next(record for record in records if record["record"] == "manifest")

  assert manifest["state"] == "complete"
  assert manifest["vehicle"]["brand"] == "hyundai"
  assert manifest["vehicle"]["source_segment"] == 0
  assert len(manifest["provenance"]["car_params_wire_sha256"]) == 64
  assert (
    len(
      manifest["provenance"]["car_params_wire_snapshots"],
    )
    == 1
  )
  assert manifest["provenance"]["baseline_exact_claim_allowed"] is False
  assert "NNFF" in manifest["route_software"]["controller_params"]
  assert "UnrelatedSecret" not in manifest["route_software"]["controller_params"]
  assert manifest["route_software"]["controller_params"]["NNFF"]["text"] == "1"
  controller_provenance = manifest["dynamics"]["controller_provenance"]
  assert controller_provenance["flm_active"] is False
  assert controller_provenance["flm_active_available"] is True
  assert controller_provenance["trailer_load_kg"] == pytest.approx(453.592)
  assert controller_provenance["trailer_load_available"] is True
  second_segment = manifest["completeness"]["segments"][1]
  assert second_segment["start_t_us"] == 1_025_000
  assert second_segment["start_time_source"] == "road.encode_index.timestamp_eof"
  assert second_segment["camera_ranges_us"] == {"road": [1_025_000, 1_025_000]}
  assert [item["compression"] for item in manifest["provenance"]["source_objects"]] == ["zstd", "bzip2"]
  assert values(records, "vehicle.speed")[1] == pytest.approx(
    [4.0, 5.0, 5.5, 6.0],
  )
  signed_rates = values(records, "vehicle.steering_rate_signed")[1]
  assert signed_rates == pytest.approx(
    [
      0.0,
      0.0,
      math.radians(0.5) / 0.01,
      math.radians(0.5) / 0.01,
    ],
  )

  replay_rows = dynamics_rows(records)
  assert [row["nominal_t_us"] for row in replay_rows] == [
    20_000,
    30_000,
    40_000,
    1_030_000,
    1_040_000,
  ]
  assert [row["signed_steering_rate_deg_s"] for row in replay_rows] == pytest.approx([0.0, 0.0, 0.0, 0.0, 50.0])
  assert all(row["controller_i_timing"] == "post_update_asof_source_row" for row in replay_rows)
  assert all(
    age >= 0
    for row in replay_rows
    for age in (
      row["car_control_age_us"],
      row["controls_state_age_us"],
    )
  )
  dynamics_chunks = [record for record in records if record["record"] == "dynamics_chunk"]
  assert len(dynamics_chunks) == 2
  assert all(chunk["rows"][0]["continuous"] is False for chunk in dynamics_chunks)
  assert all(
    right["nominal_t_us"] - left["nominal_t_us"] == 10_000
    for chunk in dynamics_chunks
    for left, right in zip(
      chunk["rows"],
      chunk["rows"][1:],
      strict=False,
    )
  )

  frames = [row for record in records if record["record"] == "frame_chunk" for row in record["rows"]]
  assert frames == [
    {
      "t_us": 1_025_000,
      "event_t_us": 1_020_000,
      "log_mono_time_ns": "2020000000",
      "source_service": "roadEncodeIdx",
      "frame_id": 41,
      "encode_id": 40,
      "segment_num": 1,
      "segment_frame_id": 0,
      "segment_encode_id": None,
      "segment_encode_id_supported": False,
      "timestamp_sof_ns": "2020000000",
      "timestamp_eof_ns": "2025000000",
      "timestamp_source": "timestamp_eof",
      "timestamp_quality": "exact_encoder_timestamp",
      "event_valid": True,
      "flags": 0,
      "bytes": 12345,
      "encode_type": "bigBoxLossless",
      "source_file": "fcamera.hevc",
      "source_ordinal": 4,
    }
  ]
  json.dumps(records, allow_nan=False)


@pytest.mark.parametrize(
  ("name", "compression"),
  (("rlog", "none"), ("rlog.zst", "zstd"), ("rlog.bz2", "bzip2")),
)
def test_reader_accepts_raw_zstd_and_bzip2(tmp_path: Path, name: str, compression: str) -> None:
  payload = event_bytes("carState", 1_000_000_000, car_state(7.0, 0.0))
  path = tmp_path / name
  write_log(path, payload, compression)
  decoded, digest, detected = _read_log(path)
  assert decoded == payload
  assert len(digest) == 64
  assert detected == compression


def test_numeric_downsample_preserves_spikes_in_source_order(tmp_path: Path) -> None:
  path = tmp_path / f"{ROUTE}--0" / "rlog"
  payload = b"".join(
    event_bytes("carState", 1_000_000_000 + index * 10_000_000, car_state(speed, float(index))) for index, speed in enumerate((1.0, 2.0, 9.0, -1.0, 3.0))
  )
  write_log(path, payload, "none")
  records = list(iter_route_records(discover_route([tmp_path], route_id=ROUTE), chunk_size=16))
  times, speeds = values(records, "vehicle.speed", "100ms")
  assert times == [0, 20_000, 30_000, 40_000]
  assert speeds == pytest.approx([1.0, 9.0, -1.0, 3.0])


def test_discovery_requires_route_selection_for_multiple_routes(tmp_path: Path) -> None:
  for route in (ROUTE, "00000002--feedface00"):
    path = tmp_path / f"{route}--0" / "rlog"
    write_log(path, event_bytes("carState", 1_000_000_000, car_state(1.0, 0.0)), "none")
  with pytest.raises(InputError, match="multiple routes"):
    discover_route([tmp_path])


def test_state_markers_and_utc_anchor_are_explicit(tmp_path: Path) -> None:
  path = tmp_path / f"{ROUTE}--0" / "rlog"
  payload = b"".join(
    [
      event_bytes("deviceState", 1_000_000_000, device_started(False)),
      event_bytes("selfdriveState", 1_010_000_000, selfdrive_active(False)),
      event_bytes("deviceState", 1_020_000_000, device_started(True)),
      event_bytes("selfdriveState", 1_030_000_000, selfdrive_active(True)),
      event_bytes("gpsLocation", 1_040_000_000, gps_anchor),
      event_bytes("selfdriveState", 1_050_000_000, selfdrive_active(False)),
    ]
  )
  write_log(path, payload, "none")
  records = list(iter_route_records(discover_route([tmp_path], route_id=ROUTE), chunk_size=16))
  marker_kinds = {record["kind"] for record in records if record["record"] == "marker"}
  assert {"offroad", "onroad", "engagement", "disengagement", "controls_active"} <= marker_kinds
  manifest = next(record for record in records if record["record"] == "manifest")
  assert manifest["timebase"]["utc_anchors"][0] == {
    "t_us": 40_000,
    "utc_us": "1800000000000000",
    "source": "gpsLocation",
    "quality": "validated_gps_fix",
    "log_mono_time_ns": "1040000000",
    "segment_num": 0,
    "source_ordinal": 4,
  }


def test_tail_segment_with_end_of_route_is_still_partial(tmp_path: Path) -> None:
  path = tmp_path / f"{ROUTE}--99" / "rlog"
  payload = b"".join(
    [
      event_bytes(
        "sentinel",
        1_000_000_000,
        sentinel("startOfSegment"),
      ),
      event_bytes(
        "carState",
        1_010_000_000,
        car_state(5.0, 1.0),
      ),
      event_bytes(
        "sentinel",
        1_020_000_000,
        sentinel("endOfRoute", 2),
      ),
    ]
  )
  write_log(path, payload, "none")

  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    )
  )
  manifest = next(record for record in records if record["record"] == "manifest")

  assert manifest["state"] == "partial"
  assert manifest["publication_ready"] is False
  assert manifest["completeness"]["route_start_observed"] is False
  assert manifest["completeness"]["route_end_observed"] is True
  assert manifest["completeness"]["missing_segment_numbers"] == list(range(99))
  assert manifest["timebase"]["origin_stability"] == "provisional_supplied_subset"


def test_dynamics_join_is_timestamp_causal_and_feedforward_is_explicit(
  tmp_path: Path,
) -> None:
  path = tmp_path / f"{ROUTE}--0" / "rlog"
  payload = b"".join(
    [
      event_bytes("initData", 990_000_000, init_data),
      event_bytes(
        "sentinel",
        1_000_000_000,
        sentinel("startOfRoute"),
      ),
      event_bytes("carParams", 1_001_000_000, car_params),
      event_bytes(
        "carControl",
        1_010_000_000,
        car_control_values(0.10, 0.001),
      ),
      event_bytes("carOutput", 1_011_000_000, car_output),
      event_bytes("controlsState", 1_012_000_000, controls_state),
      event_bytes(
        "liveTorqueParameters",
        1_008_000_000,
        unused_live_torque,
      ),
      event_bytes(
        "liveParameters",
        1_009_000_000,
        live_parameters,
      ),
      # This record is physically before the first carState but has a
      # future source timestamp. The first row must keep the older control.
      event_bytes(
        "carControl",
        1_030_000_000,
        car_control_values(0.30, 0.003),
      ),
      event_bytes(
        "carState",
        1_020_000_000,
        car_state(4.0, 1.0),
      ),
      event_bytes(
        "carState",
        1_040_000_000,
        car_state(4.0, 2.0),
      ),
      event_bytes(
        "sentinel",
        1_050_000_000,
        sentinel("endOfRoute"),
      ),
    ]
  )
  write_log(path, payload, "none")

  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    )
  )
  rows = dynamics_rows(records)
  manifest = next(record for record in records if record["record"] == "manifest")
  catalog = next(record for record in records if record["record"] == "dynamics_catalog")

  assert len(rows) == 3
  assert len(catalog["columns"]) == len(
    {column["id"] for column in catalog["columns"]},
  )
  assert {column["id"] for column in catalog["columns"]} == set(rows[0])
  assert [row["desired_curvature"] for row in rows] == pytest.approx([0.001, 0.003, 0.003])
  assert [row["car_control_age_us"] for row in rows] == [
    10_000,
    0,
    10_000,
  ]
  assert all(
    row["car_control_age_us"] >= 0 and row["controls_state_age_us"] >= 0 and row["live_torque_age_us"] >= 0 and row["live_parameters_age_us"] >= 0
    for row in rows
  )
  assert rows[0]["gravity_adjusted_future_lateral_accel"] == pytest.approx(0.002 * 4.0**2 - 0.01 * 9.80665)
  assert rows[0]["future_feedforward_lateral_accel"] == pytest.approx(
    0.002 * 4.0**2 - 0.01 * 9.80665 - 0.04,
  )
  assert rows[0]["future_feedforward_eligible"] is True
  assert rows[0]["future_feedforward_exact"] is False
  assert manifest["dynamics"]["telemetry_provenance"]["alignment"] == "timestamp_causal_recorded_history_asof"
  assert manifest["dynamics"]["telemetry_provenance"]["causal_input_eligible"] is True
  telemetry_provenance = manifest["dynamics"]["telemetry_provenance"]
  assert telemetry_provenance["extractor_version"] == "1.1.0"
  assert telemetry_provenance["route_origin_log_mono_time_ns"] == "1000000000"
  assert telemetry_provenance["invalid_event_policy"] == {
    "carState": "drop_without_invalidating_prior_valid_state",
    "carControl": "invalidate_until_next_valid",
    "controlsState": "invalidate_until_next_valid",
    "carOutput": "invalidate_until_next_valid",
  }
  assert telemetry_provenance["source_selection"] == ("independent_per_source_max_valid_source_with_logMonoTime_at_or_before_tick")
  assert telemetry_provenance["signed_steering_rate"] == ("causal_grid_difference_of_zoh_steering_angle")
  assert telemetry_provenance["applied_torque_source"] == ("carOutput.actuatorsOutput.torque_only_no_fallback")
  assert manifest["dynamics"]["controller_i_timing"] == ("post_update_asof_source_row")
  for proof_name in (
    "source_age_validation",
    "controller_selection_validation",
    "effective_torque_context_validation",
  ):
    proof = manifest["dynamics"][proof_name]
    assert proof["state"] == "verified"
    assert proof["checked_row_count"] == len(rows)
  assert manifest["dynamics"]["effective_torque_context_validation"]["evaluator"] == {
    "name": extractor.TORQUE_CONTEXT_EVALUATOR_NAME,
    "version": extractor.TORQUE_CONTEXT_EVALUATOR_VERSION,
    "source_sha256": (extractor.TORQUE_CONTEXT_EVALUATOR_SOURCE_SHA256),
  }
  controller_proof = manifest["dynamics"]["controller_selection_validation"]
  assert controller_proof["controller_types"] == ["conventional_torque"]
  assert controller_proof["source_types"] == ["versioned_initData_fallback"]
  assert len(controller_proof["snapshot_hashes"]) == 1
  controller_provenance = manifest["dynamics"]["controller_provenance"]
  assert controller_provenance["flm_resolution"]["state"] == "verified"
  assert controller_provenance["flm_resolution"]["source"] == "versioned_source_commit_evaluator"
  assert controller_provenance["init_data_fallback_evaluator"]["source_commit"] == "2747bf037c0f284500457f1befb4f52415e3285a"
  assert controller_provenance["resolved_toggle_snapshots"] == []
  assert all(row["controller_selection_source"] == "versioned_initData_fallback" for row in rows)
  assert all(
    row["controller_selection_stateful"] is True
    and row["effective_torque_params_stateful"] is True
    and row["effective_torque_context_log_mono_time_ns"] == row["controls_state_log_mono_time_ns"]
    for row in rows
  )
  assert all(row["applied_torque_source"] == "carOutput.actuatorsOutput.torque" for row in rows)
  tuning_provenance = controller_provenance["tuning_provenance"]
  baseline_profile = tuning_provenance["baseline_controller_profile"]
  assert controller_provenance["baseline_controller_profile"] == baseline_profile
  assert baseline_profile["profile_id"] == extractor.HISTORICAL_IONIQ5_CONTROLLER_PROFILE_ID
  assert baseline_profile["baseline_controller_params_sha256"] == extractor.HISTORICAL_IONIQ5_CONTROLLER_PARAMS_SHA256
  assert baseline_profile["vehicle_lat_accel_factor_multiplier"] == pytest.approx(1.2101)
  assert manifest["dynamics"]["controller_profile_validation"]["state"] == "verified"
  assert all(
    len(manifest["provenance"][name]) == 64
    for name in (
      "car_params_wire_sha256",
      "car_params_summary_sha256",
      "controller_params_sha256",
    )
  )


def test_qlog_is_visualization_only_for_dynamics(tmp_path: Path) -> None:
  path = tmp_path / f"{ROUTE}--0" / "qlog"
  payload = b"".join(
    [
      event_bytes(
        "sentinel",
        1_000_000_000,
        sentinel("startOfRoute"),
      ),
      event_bytes("carParams", 1_001_000_000, car_params),
      event_bytes("carControl", 1_010_000_000, car_control),
      event_bytes("carOutput", 1_011_000_000, car_output),
      event_bytes("controlsState", 1_012_000_000, controls_state),
      event_bytes(
        "carState",
        1_020_000_000,
        car_state(4.0, 1.0),
      ),
      event_bytes(
        "sentinel",
        1_030_000_000,
        sentinel("endOfRoute"),
      ),
    ]
  )
  write_log(path, payload, "none")

  records = list(
    iter_route_records(
      discover_route(
        [tmp_path],
        route_id=ROUTE,
        log_type="qlog",
      ),
      chunk_size=16,
    )
  )
  manifest = next(record for record in records if record["record"] == "manifest")

  assert dynamics_rows(records) == []
  assert manifest["dynamics"]["state"] == "unavailable_qlog_decimated"
  assert manifest["dynamics"]["causal_input_eligible"] is False
  assert values(records, "vehicle.speed")[1] == pytest.approx([4.0])


def onroad_events_bytes(
  mono_ns: int,
  entries: list[tuple[str, bool]],
) -> bytes:
  event = log.Event.new_message()
  event.logMonoTime = mono_ns
  event.valid = True
  values_list = event.init("onroadEvents", len(entries))
  for value, (name, warning) in zip(values_list, entries, strict=True):
    value.name = name
    value.warning = warning
  return event.to_bytes()


def test_onroad_event_snapshots_emit_diffs_not_full_repeats(
  tmp_path: Path,
) -> None:
  path = tmp_path / f"{ROUTE}--0" / "rlog"
  payload = b"".join(
    [
      event_bytes(
        "sentinel",
        1_000_000_000,
        sentinel("startOfRoute"),
      ),
      onroad_events_bytes(
        1_010_000_000,
        [("doorOpen", True)],
      ),
      onroad_events_bytes(
        1_020_000_000,
        [
          ("doorOpen", True),
          ("seatbeltNotLatched", True),
        ],
      ),
      onroad_events_bytes(
        1_030_000_000,
        [("seatbeltNotLatched", True)],
      ),
      onroad_events_bytes(
        1_040_000_000,
        [("seatbeltNotLatched", True)],
      ),
      event_bytes(
        "sentinel",
        1_050_000_000,
        sentinel("endOfRoute"),
      ),
    ]
  )
  write_log(path, payload, "none")

  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    )
  )
  event_markers = [record for record in records if record["record"] == "marker" and record["kind"] in ("event", "event_cleared")]

  assert [(marker["kind"], marker["attributes"]["name"]) for marker in event_markers] == [
    ("event", "doorOpen"),
    ("event", "seatbeltNotLatched"),
    ("event_cleared", "doorOpen"),
  ]


def test_absolute_grid_age_boundary_and_zoh_signed_rate(
  tmp_path: Path,
) -> None:
  path = tmp_path / f"{ROUTE}--0" / "rlog"
  payload = b"".join(
    [
      event_bytes("initData", 990_000_000, init_data),
      event_bytes(
        "sentinel",
        1_000_000_000,
        sentinel("startOfRoute"),
      ),
      event_bytes("carParams", 1_000_000_000, car_params),
      event_bytes("carControl", 1_005_000_000, car_control),
      event_bytes("carOutput", 1_006_000_000, car_output),
      event_bytes(
        "controlsState",
        1_007_000_000,
        controls_state,
      ),
      event_bytes(
        "carState",
        1_008_000_000,
        car_state(4.0, 1.0),
      ),
      event_bytes(
        "carState",
        1_025_000_000,
        car_state(4.0, 1.5),
      ),
      event_bytes(
        "carState",
        1_035_000_000,
        car_state(4.0, 2.5),
      ),
      event_bytes(
        "carState",
        1_045_000_000,
        car_state(4.0, 3.0),
      ),
      event_bytes(
        "sentinel",
        1_060_000_000,
        sentinel("endOfRoute"),
      ),
    ],
  )
  write_log(path, payload, "none")

  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    ),
  )
  rows = dynamics_rows(records)
  manifest = next(record for record in records if record["record"] == "manifest")

  assert [row["nominal_t_us"] for row in rows] == [
    10_000,
    20_000,
    30_000,
    40_000,
  ]
  assert [row["car_state_age_us"] for row in rows] == [
    2_000,
    12_000,
    5_000,
    5_000,
  ]
  assert [row["car_control_age_us"] for row in rows] == [
    5_000,
    15_000,
    25_000,
    35_000,
  ]
  assert [row["car_output_age_us"] for row in rows] == [
    4_000,
    14_000,
    24_000,
    34_000,
  ]
  assert [row["controls_state_age_us"] for row in rows] == [
    3_000,
    13_000,
    23_000,
    33_000,
  ]
  assert [row["signed_steering_rate_deg_s"] for row in rows] == pytest.approx([0.0, 0.0, 50.0, 100.0])
  assert all(int(row["nominal_log_mono_time_ns"]) % 10_000_000 == 0 for row in rows)
  assert all(row["source_time_error_us"] == -row["car_state_age_us"] for row in rows)
  assert manifest["dynamics"]["drop_counts"]["stale_required_service:carControl"] >= 1


def test_invalid_fast_events_follow_service_specific_policy(
  tmp_path: Path,
) -> None:
  path = tmp_path / f"{ROUTE}--0" / "rlog"
  payload = b"".join(
    [
      event_bytes("initData", 990_000_000, init_data),
      event_bytes(
        "sentinel",
        1_000_000_000,
        sentinel("startOfRoute"),
      ),
      event_bytes("carParams", 1_000_000_000, car_params),
      event_bytes("carControl", 1_005_000_000, car_control),
      event_bytes("carOutput", 1_006_000_000, car_output),
      event_bytes(
        "controlsState",
        1_007_000_000,
        controls_state,
      ),
      event_bytes(
        "carState",
        1_008_000_000,
        car_state(4.0, 1.0),
      ),
      event_bytes(
        "carState",
        1_015_000_000,
        car_state(9.0, 9.0),
        valid=False,
      ),
      event_bytes(
        "carState",
        1_025_000_000,
        car_state(4.0, 1.5),
      ),
      event_bytes(
        "carControl",
        1_025_000_000,
        car_control_values(0.9, 0.009),
        valid=False,
      ),
      event_bytes(
        "carControl",
        1_035_000_000,
        car_control_values(0.2, 0.002),
      ),
      event_bytes(
        "carState",
        1_045_000_000,
        car_state(4.0, 2.0),
      ),
      event_bytes(
        "sentinel",
        1_050_000_000,
        sentinel("endOfRoute"),
      ),
    ],
  )
  write_log(path, payload, "none")

  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    ),
  )
  rows = dynamics_rows(records)
  manifest = next(record for record in records if record["record"] == "manifest")

  assert [row["nominal_t_us"] for row in rows] == [
    10_000,
    20_000,
    40_000,
  ]
  assert rows[1]["log_mono_time_ns"] == "1008000000"
  assert rows[1]["car_state_age_us"] == 12_000
  assert rows[2]["continuous"] is False
  assert manifest["dynamics"]["drop_counts"]["missing_required_service:carControl"] >= 1
  assert manifest["state"] == "complete"
  invalid_warning = next(warning for warning in manifest["warnings"] if warning["code"] == "invalid_events")
  assert invalid_warning["counts"] == {
    "carControl": 1,
    "carState": 1,
  }


def test_adjacent_overlap_is_globally_sorted_and_deduplicated(
  tmp_path: Path,
) -> None:
  duplicate = event_bytes(
    "carState",
    1_990_000_000,
    car_state(2.0, 2.0),
  )
  segment_zero = tmp_path / f"{ROUTE}--0" / "rlog"
  segment_one = tmp_path / f"{ROUTE}--1" / "rlog"
  write_log(
    segment_zero,
    b"".join(
      [
        event_bytes("initData", 900_000_000, init_data),
        event_bytes(
          "sentinel",
          1_000_000_000,
          sentinel("startOfRoute"),
        ),
        event_bytes("carParams", 1_000_000_000, car_params),
        event_bytes(
          "carState",
          1_950_000_000,
          car_state(1.0, 1.0),
        ),
        duplicate,
        event_bytes(
          "sentinel",
          2_000_000_000,
          sentinel("endOfSegment"),
        ),
      ],
    ),
    "none",
  )
  write_log(
    segment_one,
    b"".join(
      [
        event_bytes(
          "sentinel",
          1_980_000_000,
          sentinel("startOfSegment"),
        ),
        duplicate,
        event_bytes(
          "carState",
          2_010_000_000,
          car_state(3.0, 3.0),
        ),
        event_bytes(
          "sentinel",
          2_020_000_000,
          sentinel("endOfRoute"),
        ),
      ],
    ),
    "none",
  )

  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    ),
  )
  speed_chunks = [record for record in records if record["record"] == "series_chunk" and record["signal"] == "vehicle.speed" and record["tier"] == "full"]
  manifest = next(record for record in records if record["record"] == "manifest")

  assert [value for chunk in speed_chunks for value in chunk["v"]] == pytest.approx([1.0, 2.0, 3.0])
  assert [value for chunk in speed_chunks for value in chunk["log_mono_time_ns"]] == [
    "1950000000",
    "1990000000",
    "2010000000",
  ]
  assert [value for chunk in speed_chunks for value in chunk["source_segment_num"]] == [0, 0, 1]
  assert manifest["event_merge"]["deduplicated_event_count"] == 1
  assert manifest["event_merge"]["maximum_observed_overlap_ns"] == "20000000"
  assert manifest["state"] == "complete"


def test_frame_event_validity_and_encoder_timestamp_are_canonical(
  tmp_path: Path,
) -> None:
  def timed_index(data) -> None:
    road_index_without_encoder_time(data)
    data.timestampSof = 1_009_000_000
    data.timestampEof = 1_010_000_000

  path = tmp_path / f"{ROUTE}--0" / "rlog"
  write_log(
    path,
    b"".join(
      [
        event_bytes(
          "sentinel",
          1_000_000_000,
          sentinel("startOfRoute"),
        ),
        event_bytes(
          "roadEncodeIdx",
          1_010_000_000,
          timed_index,
          valid=False,
        ),
        event_bytes(
          "roadEncodeIdx",
          1_020_000_000,
          road_index_without_encoder_time,
        ),
        event_bytes(
          "sentinel",
          1_030_000_000,
          sentinel("endOfRoute"),
        ),
      ],
    ),
    "none",
  )

  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    ),
  )
  frames = [row for record in records if record["record"] == "frame_chunk" for row in record["rows"]]
  manifest = next(record for record in records if record["record"] == "manifest")

  assert frames[0]["event_valid"] is False
  assert frames[0]["timestamp_quality"] == "exact_encoder_timestamp"
  assert frames[1]["event_valid"] is True
  assert frames[1]["timestamp_quality"] == ("invalid_missing_encoder_timestamp")
  assert manifest["state"] == "partial"
  assert manifest["publication_ready"] is False
  assert manifest["completeness"]["segments"][0]["frame_quality_issue_count"] == 2
  assert manifest["completeness"]["segments"][0]["camera_ranges_us"] == {}


def test_reader_fails_closed_on_decompression_and_message_budgets(
  tmp_path: Path,
) -> None:
  payload = event_bytes(
    "carState",
    1_000_000_000,
    car_state(1.0, 0.0),
  )
  path = tmp_path / "rlog.zst"
  write_log(path, payload * 100, "zstd")

  with pytest.raises(
    ResourceLimitError,
    match="segment_decompressed_bytes",
  ):
    _read_log(path, max_decompressed_bytes=len(payload))
  with pytest.raises(
    ResourceLimitError,
    match="segment_message_count",
  ):
    _read_events(payload * 2, max_messages=1)
  with pytest.raises(
    ResourceLimitError,
    match="event_bytes",
  ):
    _read_events(payload, max_event_bytes=len(payload) - 1)


def test_route_cumulative_source_and_decompressed_budgets(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  payload_zero = b"".join(
    [
      event_bytes(
        "sentinel",
        1_000_000_000,
        sentinel("startOfRoute"),
      ),
      event_bytes(
        "sentinel",
        1_010_000_000,
        sentinel("endOfSegment"),
      ),
    ],
  )
  payload_one = b"".join(
    [
      event_bytes(
        "sentinel",
        1_020_000_000,
        sentinel("startOfSegment"),
      ),
      event_bytes(
        "sentinel",
        1_030_000_000,
        sentinel("endOfRoute"),
      ),
    ],
  )
  paths = [
    tmp_path / f"{ROUTE}--0" / "rlog.zst",
    tmp_path / f"{ROUTE}--1" / "rlog.zst",
  ]
  write_log(paths[0], payload_zero, "zstd")
  write_log(paths[1], payload_one, "zstd")
  route = discover_route([tmp_path], route_id=ROUTE)

  monkeypatch.setattr(
    extractor,
    "MAX_SOURCE_BYTES_PER_ROUTE",
    sum(path.stat().st_size for path in paths) - 1,
  )
  with pytest.raises(
    ResourceLimitError,
    match="route_source_bytes",
  ):
    list(iter_route_records(route, chunk_size=16))

  monkeypatch.setattr(
    extractor,
    "MAX_SOURCE_BYTES_PER_ROUTE",
    4 * 1024 * 1024 * 1024,
  )
  monkeypatch.setattr(
    extractor,
    "MAX_DECOMPRESSED_BYTES_PER_ROUTE",
    len(payload_zero) + len(payload_one) - 1,
  )
  with pytest.raises(
    ResourceLimitError,
    match="route_decompressed_bytes",
  ):
    list(iter_route_records(route, chunk_size=16))


def test_backward_overlap_owner_chain_is_bounded(
  tmp_path: Path,
) -> None:
  payloads = (
    b"".join(
      [
        event_bytes(
          "sentinel",
          1_000_000_000,
          sentinel("startOfRoute"),
        ),
        event_bytes(
          "carState",
          3_000_000_000,
          car_state(1.0, 1.0),
        ),
        event_bytes(
          "sentinel",
          3_100_000_000,
          sentinel("endOfSegment"),
        ),
      ],
    ),
    b"".join(
      [
        event_bytes(
          "sentinel",
          2_900_000_000,
          sentinel("startOfSegment"),
        ),
        event_bytes(
          "carState",
          3_050_000_000,
          car_state(2.0, 2.0),
        ),
        event_bytes(
          "sentinel",
          3_100_000_000,
          sentinel("endOfSegment"),
        ),
      ],
    ),
    b"".join(
      [
        event_bytes(
          "sentinel",
          2_950_000_000,
          sentinel("startOfSegment"),
        ),
        event_bytes(
          "carState",
          3_075_000_000,
          car_state(3.0, 3.0),
        ),
        event_bytes(
          "sentinel",
          3_120_000_000,
          sentinel("endOfRoute"),
        ),
      ],
    ),
  )
  for number, payload in enumerate(payloads):
    write_log(
      tmp_path / f"{ROUTE}--{number}" / "rlog",
      payload,
      "none",
    )

  with pytest.raises(
    ResourceLimitError,
    match="retained_overlap_segment_count",
  ):
    list(
      iter_route_records(
        discover_route([tmp_path], route_id=ROUTE),
        chunk_size=16,
      ),
    )


@pytest.mark.parametrize(
  "budget",
  ("bytes", "records"),
)
def test_cli_output_budget_is_atomic(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
  budget: str,
) -> None:
  destination = tmp_path / "telemetry.ndjson"
  destination.write_text("previous\n", encoding="utf-8")
  monkeypatch.setattr(
    cli,
    "discover_route",
    lambda *_args, **_kwargs: object(),
  )
  monkeypatch.setattr(
    cli,
    "iter_route_records",
    lambda *_args, **_kwargs: iter(
      [{"record": "one", "payload": "x" * 100}, {"record": "two"}],
    ),
  )
  if budget == "bytes":
    monkeypatch.setattr(cli, "MAX_OUTPUT_BYTES", 32)
  else:
    monkeypatch.setattr(cli, "MAX_OUTPUT_RECORDS", 1)

  result = cli.main(
    [
      str(tmp_path / "ignored"),
      "--output",
      str(destination),
    ],
  )

  assert result == 1
  assert destination.read_text(encoding="utf-8") == "previous\n"
  assert list(tmp_path.glob(".telemetry.ndjson.*.tmp")) == []


def test_cli_cleanup_does_not_mask_primary_write_failure(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
  capsys: pytest.CaptureFixture[str],
) -> None:
  destination = tmp_path / "telemetry.ndjson"
  destination.write_text("previous\n", encoding="utf-8")
  original_open = Path.open

  class FailingStream:
    closed = False

    def write(self, _value: str) -> None:
      raise OSError("primary write failure")

    def close(self) -> None:
      raise OSError("secondary close failure")

  def failing_open(path: Path, *args, **kwargs):
    with original_open(path, *args, **kwargs):
      pass
    return FailingStream()

  monkeypatch.setattr(
    cli,
    "discover_route",
    lambda *_args, **_kwargs: object(),
  )
  monkeypatch.setattr(
    cli,
    "iter_route_records",
    lambda *_args, **_kwargs: iter([{"record": "one"}]),
  )
  monkeypatch.setattr(Path, "open", failing_open)

  result = cli.main(
    [
      str(tmp_path / "ignored"),
      "--output",
      str(destination),
    ],
  )

  assert result == 1
  assert "primary write failure" in capsys.readouterr().err
  with original_open(destination, encoding="utf-8") as stream:
    assert stream.read() == "previous\n"
  assert list(tmp_path.glob(".telemetry.ndjson.*.tmp")) == []


def test_cli_reports_discovery_resource_limit(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  def fail_discovery(*_args, **_kwargs):
    raise ResourceLimitError("route_segment_count", 1, 2)

  monkeypatch.setattr(cli, "discover_route", fail_discovery)
  assert cli.main([str(tmp_path / "ignored")]) == 1


def test_effective_torque_state_is_held_when_runtime_updates_turn_off() -> None:
  car_params_snapshot = {
    "source_segment": 0,
    "source_ordinal": 2,
    "log_mono_time_ns": "1001000000",
    "_wire_sha256": "a" * 64,
    "lateral_tuning_type": "torque",
    "lateral_torque_tuning": {
      "lat_accel_factor": 2.5,
      "lat_accel_offset": 0.04,
      "friction": 0.11,
    },
  }

  def snapshot(mono_ns: int, custom: bool) -> dict:
    return {
      "_mono_ns": mono_ns,
      "_segment_num": 0,
      "_source_ordinal": 3 if custom else 4,
      "_event_valid": True,
      "resolved_toggles_valid": True,
      "resolved_toggles_sha256": ("b" * 64 if custom else "c" * 64),
      "resolved_toggles": {
        "force_auto_tune": False,
        "force_auto_tune_off": False,
        "friction": 0.20,
        "latAccelFactor": 3.0,
        "nnff": False,
        "nnff_lite": False,
        "use_custom_friction": custom,
        "use_custom_latAccelFactor": custom,
      },
    }

  applied = extractor._effective_torque_context(
    car_params=car_params_snapshot,
    route_software=None,
    resolved_snapshot=snapshot(1_010_000_000, True),
    live_snapshot=None,
    context_ns=1_012_000_000,
    previous_state=None,
    initial_state_known=True,
  )
  held = extractor._effective_torque_context(
    car_params=car_params_snapshot,
    route_software=None,
    resolved_snapshot=snapshot(1_020_000_000, False),
    live_snapshot=None,
    context_ns=1_022_000_000,
    previous_state=applied,
    initial_state_known=True,
  )

  assert applied["effective_lat_accel_factor"] == pytest.approx(3.0)
  assert applied["effective_friction"] == pytest.approx(0.20)
  assert held["effective_lat_accel_factor"] == pytest.approx(3.0)
  assert held["effective_friction"] == pytest.approx(0.20)
  assert held["effective_torque_params_state_held"] is True
  assert held["effective_torque_params_exact"] is True
  assert held["effective_torque_params_source_identity"] == applied["effective_torque_params_source_identity"]


def test_config_snapshot_selection_keeps_earliest_process_initialization() -> None:
  first = {
    "event_valid": True,
    "log_mono_time_ns": "1000000000",
    "source_ordinal": 4,
    "source_segment": 0,
  }
  repeated = {
    "event_valid": True,
    "log_mono_time_ns": "2000000000",
    "source_ordinal": 7,
    "source_segment": 1,
  }
  next_group = {
    "event_valid": True,
    "log_mono_time_ns": "3000000000",
    "source_ordinal": 2,
    "source_segment": 2,
  }
  snapshots = [first, repeated, next_group]
  builder = extractor._DynamicsGridBuilder(
    origin_ns=1_000_000_000,
    origin_stable=True,
    process_car_params_snapshots=snapshots,
    process_route_software_snapshots=[],
  )

  assert (
    builder._process_initialization_snapshot(
      snapshots,
    )
    is first
  )


@pytest.mark.parametrize(
  (
    "source_commit",
    "expected_profile_id",
    "expected_hash",
    "expected_multiplier",
    "expected_eligible",
  ),
  [
    (
      extractor.REFINED_IONIQ5_CONTROLLER_SOURCE_COMMIT,
      extractor.REFINED_IONIQ5_CONTROLLER_PROFILE_ID,
      extractor.REFINED_IONIQ5_CONTROLLER_PARAMS_SHA256,
      1.2507,
      True,
    ),
    (
      extractor.CURRENT_IONIQ5_CONTROLLER_SOURCE_COMMIT,
      extractor.CURRENT_IONIQ5_CONTROLLER_PROFILE_ID,
      extractor.CURRENT_IONIQ5_CONTROLLER_PARAMS_SHA256,
      1.36,
      True,
    ),
    (
      "0" * 40,
      None,
      None,
      None,
      False,
    ),
  ],
)
def test_controller_profile_is_selected_only_by_exact_source_commit(
  tmp_path: Path,
  source_commit: str,
  expected_profile_id: str | None,
  expected_hash: str | None,
  expected_multiplier: float | None,
  expected_eligible: bool,
) -> None:
  path = tmp_path / f"{ROUTE}--0" / "rlog"
  write_log(
    path,
    b"".join(
      [
        event_bytes(
          "initData",
          990_000_000,
          init_data_for_commit(source_commit),
        ),
        event_bytes(
          "sentinel",
          1_000_000_000,
          sentinel("startOfRoute"),
        ),
        event_bytes("carParams", 1_001_000_000, car_params),
        event_bytes(
          "starpilotPlan",
          1_005_000_000,
          resolved_toggles(),
        ),
        event_bytes("carControl", 1_010_000_000, car_control),
        event_bytes("carOutput", 1_011_000_000, car_output),
        event_bytes("controlsState", 1_012_000_000, controls_state),
        event_bytes(
          "carState",
          1_020_000_000,
          car_state(4.0, 1.0),
        ),
        event_bytes(
          "sentinel",
          1_030_000_000,
          sentinel("endOfRoute"),
        ),
      ],
    ),
    "none",
  )
  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    ),
  )
  row = dynamics_rows(records)[0]
  manifest = next(record for record in records if record["record"] == "manifest")
  profile = manifest["dynamics"]["controller_provenance"]["tuning_provenance"]["baseline_controller_profile"]

  assert row["baseline_controller_profile_id"] == expected_profile_id
  assert row["baseline_controller_params_sha256"] == expected_hash
  assert row["vehicle_lat_accel_factor_multiplier"] == expected_multiplier
  assert manifest["dynamics"]["causal_input_eligible"] is expected_eligible
  if expected_profile_id is None:
    assert profile is None
    assert manifest["dynamics"]["controller_profile_validation"]["state"] == "failed"
  else:
    assert profile["profile_id"] == expected_profile_id
    assert profile["source_starpilot_commit"] == source_commit


def test_invalid_plan_is_ignored_and_live_use_params_binds_versioned_fallback(
  tmp_path: Path,
) -> None:
  path = tmp_path / f"{ROUTE}--0" / "rlog"
  invalid_plan_ns = 1_006_000_000
  live_ns = 1_008_000_000
  write_log(
    path,
    b"".join(
      [
        event_bytes(
          "initData",
          990_000_000,
          refined_init_data,
        ),
        event_bytes(
          "sentinel",
          1_000_000_000,
          sentinel("startOfRoute"),
        ),
        event_bytes(
          "carParams",
          1_001_000_000,
          car_params,
        ),
        event_bytes(
          "starpilotPlan",
          invalid_plan_ns,
          resolved_toggles(
            force_auto_tune_off=False,
            friction=0.11,
            latAccelFactor=2.5,
            use_custom_friction=False,
            use_custom_latAccelFactor=False,
          ),
          valid=False,
        ),
        event_bytes(
          "liveTorqueParameters",
          live_ns,
          live_torque,
        ),
        event_bytes(
          "carControl",
          1_010_000_000,
          car_control,
        ),
        event_bytes(
          "carOutput",
          1_011_000_000,
          car_output,
        ),
        event_bytes(
          "controlsState",
          1_012_000_000,
          controls_state,
        ),
        event_bytes(
          "carState",
          1_020_000_000,
          car_state(4.0, 1.0),
        ),
        event_bytes(
          "sentinel",
          1_030_000_000,
          sentinel("endOfRoute"),
        ),
      ],
    ),
    "none",
  )

  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    ),
  )
  rows = dynamics_rows(records)
  manifest = next(record for record in records if record["record"] == "manifest")

  assert len(rows) == 1
  row = rows[0]
  assert row["controller_type"] == "conventional_torque"
  assert row["controller_selection_source"] == "versioned_initData_fallback"
  assert row["effective_resolved_toggles_source"] == "versioned_initData_fallback"
  assert row["effective_resolved_toggles_evaluator_id"] == "starpilot-torque-context-6dd6c0-v1"
  assert row["force_auto_tune"] is False
  assert row["force_auto_tune_off"] is True
  assert row["use_custom_lat_accel_factor"] is True
  assert row["use_custom_friction"] is True
  assert row["effective_lat_accel_factor"] == pytest.approx(
    3.0,
  )
  assert row["effective_lat_accel_offset"] == pytest.approx(
    0.04,
  )
  assert row["effective_friction"] == pytest.approx(0.2)
  assert row["effective_torque_params_source"] == {
    "factor": "resolved_custom",
    "friction": "resolved_custom",
    "offset": "car_params",
  }
  assert row["live_torque_source_log_mono_time_ns"] == str(live_ns)
  assert row["effective_resolved_toggles_log_mono_time_ns"] == "990000000"
  controller_provenance = manifest["dynamics"]["controller_provenance"]
  assert controller_provenance["resolved_toggle_snapshots"] == []
  assert controller_provenance["effective_resolved_toggle_snapshots"] == []
  fallback = controller_provenance["init_data_fallback_evaluator"]
  assert fallback["state"] == "available"
  assert fallback["evaluator_id"] == "starpilot-torque-context-6dd6c0-v1"
  assert manifest["dynamics"]["controller_profile_validation"]["profile_id"] == extractor.REFINED_IONIQ5_CONTROLLER_PROFILE_ID
  assert manifest["dynamics"]["causal_input_eligible"] is True


def test_controller_selection_never_relabels_after_initialization(
  tmp_path: Path,
) -> None:
  path = tmp_path / f"{ROUTE}--0" / "rlog"
  write_log(
    path,
    b"".join(
      [
        event_bytes("initData", 990_000_000, init_data),
        event_bytes(
          "sentinel",
          1_000_000_000,
          sentinel("startOfRoute"),
        ),
        event_bytes("carParams", 1_001_000_000, car_params),
        event_bytes(
          "starpilotPlan",
          1_005_000_000,
          resolved_toggles(nnff=True),
        ),
        event_bytes("carControl", 1_010_000_000, car_control),
        event_bytes("carOutput", 1_011_000_000, car_output),
        event_bytes("controlsState", 1_012_000_000, controls_state),
        event_bytes(
          "carState",
          1_020_000_000,
          car_state(4.0, 1.0),
        ),
        event_bytes(
          "starpilotPlan",
          1_025_000_000,
          resolved_toggles(nnff=False),
        ),
        event_bytes("controlsState", 1_032_000_000, controls_state),
        event_bytes(
          "carState",
          1_040_000_000,
          car_state(4.0, 2.0),
        ),
        event_bytes(
          "sentinel",
          1_050_000_000,
          sentinel("endOfRoute"),
        ),
      ],
    ),
    "none",
  )
  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    ),
  )
  rows = dynamics_rows(records)
  manifest = next(record for record in records if record["record"] == "manifest")

  assert rows
  assert all(row["controller_type"] == "nnff" for row in rows)
  assert rows[-1]["controller_selection_observed_consistent"] is False
  assert manifest["dynamics"]["controller_selection_validation"]["state"] == "failed"
  assert manifest["dynamics"]["causal_input_eligible"] is False


def test_subset_without_controller_initialization_proof_is_ineligible(
  tmp_path: Path,
) -> None:
  path = tmp_path / f"{ROUTE}--98" / "rlog"
  write_log(
    path,
    b"".join(
      [
        event_bytes("initData", 990_000_000, init_data),
        event_bytes(
          "sentinel",
          1_000_000_000,
          sentinel("startOfSegment"),
        ),
        event_bytes("carParams", 1_001_000_000, car_params),
        event_bytes(
          "starpilotPlan",
          1_005_000_000,
          resolved_toggles(
            latAccelFactor=3.0,
            use_custom_latAccelFactor=True,
          ),
        ),
        event_bytes("carControl", 1_010_000_000, car_control),
        event_bytes("carOutput", 1_011_000_000, car_output),
        event_bytes("controlsState", 1_012_000_000, controls_state),
        event_bytes(
          "carState",
          1_020_000_000,
          car_state(4.0, 1.0),
        ),
        event_bytes(
          "sentinel",
          1_030_000_000,
          sentinel("endOfSegment"),
        ),
      ],
    ),
    "none",
  )
  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    ),
  )
  row = dynamics_rows(records)[0]
  manifest = next(record for record in records if record["record"] == "manifest")

  assert row["controller_selection_stateful"] is False
  assert manifest["dynamics"]["controller_selection_validation"]["state"] == "failed"
  assert manifest["dynamics"]["causal_input_eligible"] is False


def test_route_summary_uses_only_bounded_monotonic_speed_intervals_and_valid_gps(
  tmp_path: Path,
) -> None:
  path = tmp_path / f"{ROUTE}--0" / "rlog"
  write_log(
    path,
    b"".join(
      [
        event_bytes(
          "sentinel",
          1_000_000_000,
          sentinel("startOfRoute"),
        ),
        event_bytes(
          "gpsLocation",
          1_010_000_000,
          gps_fix(59.91012344, 10.75098766),
        ),
        event_bytes(
          "carState",
          1_020_000_000,
          car_state(2.0, 0.0),
        ),
        event_bytes(
          "carState",
          1_120_000_000,
          car_state(4.0, 0.0),
        ),
        event_bytes(
          "carState",
          1_520_000_000,
          car_state(4.0, 0.0),
        ),
        event_bytes(
          "gpsLocationExternal",
          1_530_000_000,
          gps_fix(59.92000004, 10.76000006),
        ),
        event_bytes(
          "sentinel",
          1_540_000_000,
          sentinel("endOfRoute"),
        ),
      ],
    ),
    "none",
  )
  records = list(
    iter_route_records(
      discover_route([tmp_path], route_id=ROUTE),
      chunk_size=16,
    ),
  )
  summary = next(record for record in records if record["record"] == "manifest")["route_summary"]

  assert summary == {
    "distance_m": pytest.approx(0.3),
    "location_start": "59.9101234,10.7509877",
    "location_end": "59.9200000,10.7600001",
    "provenance": {
      "distance_method": ("trapezoidal_absolute_vEgo_monotonic_dt_le_250ms"),
      "location_method": ("first_last_valid_gps_fix_lat_lon_decimal_degrees_7"),
      "included_interval_count": 1,
      "excluded_interval_count": 1,
    },
  }


def test_reader_hashes_counts_and_decompresses_one_open_file_description(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  payload = event_bytes(
    "carState",
    1_000_000_000,
    car_state(1.0, 0.0),
  )
  encoded = bz2.compress(payload)
  opens = 0

  def one_open(_path: Path, *_args, **_kwargs):
    nonlocal opens
    opens += 1
    return io.BytesIO(encoded)

  monkeypatch.setattr(Path, "open", one_open)
  decoded, digest, compression, source_size = extractor._read_log_details(
    tmp_path / "rlog.bz2",
    max_source_bytes=len(encoded),
  )

  assert opens == 1
  assert decoded == payload
  assert digest == hashlib.sha256(encoded).hexdigest()
  assert compression == "bzip2"
  assert source_size == len(encoded)


def test_chunk_size_is_bounded_in_public_api_and_cli() -> None:
  empty_route = extractor.RouteInput("route", "rlog", ())
  for chunk_size in (
    extractor.MIN_CHUNK_SIZE - 1,
    extractor.MAX_CHUNK_SIZE + 1,
  ):
    with pytest.raises(InputError, match="chunk_size"):
      list(iter_route_records(empty_route, chunk_size))
    with pytest.raises(SystemExit, match="chunk-size"):
      cli.main(
        [
          "ignored",
          "--chunk-size",
          str(chunk_size),
        ],
      )
