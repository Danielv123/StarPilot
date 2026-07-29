from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Any

import capnp

from . import extractor as e


@dataclass
class _LoadedSegment:
  segment: e.SegmentInput
  payload: bytes
  source_size_bytes: int
  events: list[Any]
  refs: list[e._EventRef]
  digest: str
  compression: str
  corrupt: bool
  unreadable_union_count: int
  invalid_event_counts: Counter[str]
  boundary: dict[str, Any]
  first_mono_ns: int | None
  last_mono_ns: int | None
  message_counts: Counter[str]
  config_car_params: dict[str, Any] | None
  config_route_software: dict[str, Any] | None
  config_resolved_toggles: dict[str, Any] | None
  car_params_snapshots: list[dict[str, Any]]
  route_software_snapshots: list[dict[str, Any]]
  resolved_toggle_snapshots: list[dict[str, Any]]
  continuity_group: int


def _wire_sha256(value: Any) -> str | None:
  return e._struct_wire_sha256(value)


def _safe_event_which(event: Any) -> tuple[str | None, bool]:
  try:
    return event.which(), False
  except Exception:
    return None, True


def _init_snapshot(
  event: Any,
  *,
  segment_num: int,
  source_ordinal: int,
  origin_ns: int,
) -> dict[str, Any]:
  mono_ns = int(event.logMonoTime)
  snapshot = e._init_data_snapshot(
    event,
    segment_num,
    e._t_us(mono_ns, origin_ns),
  )
  snapshot.update(
    {
      "source_ordinal": source_ordinal,
      "log_mono_time_ns": str(mono_ns),
      "event_valid": e._event_valid(event),
      "wire_sha256": _wire_sha256(event.initData),
    }
  )
  return snapshot


def _car_params_snapshot(
  event: Any,
  *,
  segment_num: int,
  source_ordinal: int,
  origin_ns: int,
) -> dict[str, Any]:
  mono_ns = int(event.logMonoTime)
  snapshot = e._car_params_snapshot(
    event,
    segment_num,
    e._t_us(mono_ns, origin_ns),
  )
  wire_hash = _wire_sha256(event.carParams)
  snapshot.update(
    {
      "_wire_sha256": wire_hash,
      "_scope": "route_constant_unique_snapshot",
      "source_ordinal": source_ordinal,
      "log_mono_time_ns": str(mono_ns),
      "event_valid": e._event_valid(event),
    }
  )
  return snapshot


def _resolved_snapshot(
  event: Any,
  *,
  segment_num: int,
  source_ordinal: int,
) -> dict[str, Any] | None:
  snapshot = e._dynamics_service_snapshot(
    event,
    "starpilotPlan",
    segment_num=segment_num,
    source_ordinal=source_ordinal,
  )
  if snapshot is None:
    return None
  return {
    **snapshot,
    "segment_num": segment_num,
    "source_ordinal": source_ordinal,
    "log_mono_time_ns": str(snapshot["_mono_ns"]),
    "source": "starpilotPlan.starpilotToggles",
    "valid": bool(snapshot.get("_event_valid") and snapshot.get("resolved_toggles_valid")),
    "sha256": snapshot.get("resolved_toggles_sha256"),
    "values": snapshot.get("resolved_toggles", {}),
  }


def _unique_snapshot(
  snapshots: Sequence[dict[str, Any]],
  hash_key: str,
) -> dict[str, Any] | None:
  valid = [snapshot for snapshot in snapshots if snapshot.get("event_valid", snapshot.get("valid", False)) and snapshot.get(hash_key)]
  hashes = {str(snapshot[hash_key]) for snapshot in valid}
  return valid[0] if len(hashes) == 1 else None


def _load_segment(
  segment: e.SegmentInput,
  *,
  origin_ns: int | None,
  continuity_group: int,
  remaining_source_bytes: int,
  remaining_decompressed_bytes: int,
) -> tuple[_LoadedSegment, int]:
  payload, digest, compression, source_size_bytes = e._read_log_details(
    segment.path,
    max_source_bytes=min(
      e.MAX_SOURCE_BYTES_PER_SEGMENT,
      remaining_source_bytes,
    ),
    max_decompressed_bytes=min(
      e.MAX_DECOMPRESSED_BYTES_PER_SEGMENT,
      remaining_decompressed_bytes,
    ),
  )
  events, framing_corrupt = e._read_events(payload)
  if origin_ns is None:
    origin_ns = e._timeline_start_ns(events)
    if origin_ns is None:
      raise e.ExtractionError(
        f"first segment has no valid monotonic events: {segment.path}",
      )

  refs: list[e._EventRef] = []
  message_counts: Counter[str] = Counter()
  invalid_counts: Counter[str] = Counter()
  unreadable = 0
  car_params_snapshots: list[dict[str, Any]] = []
  route_software_snapshots: list[dict[str, Any]] = []
  resolved_toggle_snapshots: list[dict[str, Any]] = []
  operational_times: list[int] = []

  for ordinal, event in enumerate(events):
    which, unreadable_union = _safe_event_which(event)
    if unreadable_union:
      unreadable += 1
      continue
    if which is None:
      continue
    message_counts[which] += 1
    mono_ns = e._integer(e._get(event, "logMonoTime"))
    if mono_ns is None or mono_ns <= 0:
      continue
    valid = e._event_valid(event)
    if not valid:
      invalid_counts[which] += 1
    if which == "initData":
      if valid:
        route_software_snapshots.append(
          _init_snapshot(
            event,
            segment_num=segment.number,
            source_ordinal=ordinal,
            origin_ns=origin_ns,
          )
        )
      continue
    operational_times.append(mono_ns)
    if which == "carParams" and valid:
      car_params_snapshots.append(
        _car_params_snapshot(
          event,
          segment_num=segment.number,
          source_ordinal=ordinal,
          origin_ns=origin_ns,
        )
      )
    elif which == "starpilotPlan":
      snapshot = _resolved_snapshot(
        event,
        segment_num=segment.number,
        source_ordinal=ordinal,
      )
      if snapshot is not None:
        resolved_toggle_snapshots.append(snapshot)
    refs.append(
      e._EventRef(
        mono_ns,
        segment.number,
        ordinal,
        continuity_group,
        event,
      )
    )

  refs.sort(
    key=lambda ref: (
      ref.mono_ns,
      ref.segment_num,
      ref.source_ordinal,
    )
  )
  sentinels = e._sentinel_rows(events, segment.number, origin_ns)
  boundary = e._boundary_evidence(segment.number, sentinels)
  selected_car_params = _unique_snapshot(
    car_params_snapshots,
    "_wire_sha256",
  )
  selected_software = _unique_snapshot(
    route_software_snapshots,
    "controller_params_sha256",
  )
  selected_toggles = _unique_snapshot(
    resolved_toggle_snapshots,
    "sha256",
  )
  return (
    _LoadedSegment(
      segment=segment,
      payload=payload,
      source_size_bytes=source_size_bytes,
      events=events,
      refs=refs,
      digest=digest,
      compression=compression,
      corrupt=framing_corrupt or unreadable > 0,
      unreadable_union_count=unreadable,
      invalid_event_counts=invalid_counts,
      boundary=boundary,
      first_mono_ns=(min(operational_times) if operational_times else None),
      last_mono_ns=(max(operational_times) if operational_times else None),
      message_counts=message_counts,
      config_car_params=selected_car_params,
      config_route_software=selected_software,
      config_resolved_toggles=selected_toggles,
      car_params_snapshots=car_params_snapshots,
      route_software_snapshots=route_software_snapshots,
      resolved_toggle_snapshots=resolved_toggle_snapshots,
      continuity_group=continuity_group,
    ),
    origin_ns,
  )


def _carry_state(
  previous: _LoadedSegment | None,
  current: _LoadedSegment,
) -> bool:
  if previous is None:
    return False
  gap_ns = current.first_mono_ns - previous.last_mono_ns if current.first_mono_ns is not None and previous.last_mono_ns is not None else None
  return bool(
    current.segment.number == previous.segment.number + 1
    and previous.boundary["terminal_type"] == "endOfSegment"
    and current.boundary["start"] is not None
    and current.boundary["start"]["type"] == "startOfSegment"
    and not previous.corrupt
    and not current.corrupt
    and gap_ns is not None
    and -250_000_000 < gap_ns < 2_000_000_000
  )


def _event_wire_fingerprint(ref: e._EventRef) -> str:
  try:
    payload = ref.event.as_builder().to_bytes()
  except Exception:
    payload = (f"{ref.mono_ns}:{ref.segment_num}:" + f"{ref.source_ordinal}").encode()
  return hashlib.sha256(payload).hexdigest()


def _deduplicate_refs(
  refs: Sequence[e._EventRef],
) -> tuple[list[e._EventRef], int]:
  result: list[e._EventRef] = []
  dropped = 0
  index = 0
  while index < len(refs):
    mono_ns = refs[index].mono_ns
    end = index + 1
    while end < len(refs) and refs[end].mono_ns == mono_ns:
      end += 1
    seen: dict[tuple[str | None, str], int] = {}
    for ref in refs[index:end]:
      which = e._event_which(ref.event)
      fingerprint = _event_wire_fingerprint(ref)
      key = (which, fingerprint)
      previous_segment = seen.get(key)
      if previous_segment is not None and previous_segment != ref.segment_num:
        dropped += 1
        continue
      seen[key] = ref.segment_num
      result.append(ref)
    index = end
  return result, dropped


def _sorted_merge(
  left: Sequence[e._EventRef],
  right: Sequence[e._EventRef],
) -> list[e._EventRef]:
  return sorted(
    (*left, *right),
    key=lambda ref: (
      ref.mono_ns,
      ref.segment_num,
      ref.source_ordinal,
    ),
  )


def _group_by_timestamp(
  refs: Sequence[e._EventRef],
) -> Iterator[list[e._EventRef]]:
  index = 0
  while index < len(refs):
    end = index + 1
    while end < len(refs) and refs[end].mono_ns == refs[index].mono_ns:
      end += 1
    yield list(refs[index:end])
    index = end


class _RouteRecordBuilder:
  def __init__(
    self,
    *,
    route: e.RouteInput,
    origin_ns: int,
    origin_stable: bool,
    chunk_size: int,
    car_params_by_segment: dict[int, dict[str, Any] | None],
    route_software_by_segment: dict[int, dict[str, Any] | None],
    resolved_toggles_by_segment: dict[int, dict[str, Any] | None],
    process_car_params_snapshots: list[dict[str, Any]],
    process_route_software_snapshots: list[dict[str, Any]],
  ):
    self.route = route
    self.origin_ns = origin_ns
    self.origin_stable = origin_stable
    self.signal_chunks = e._SignalChunker(chunk_size)
    self.downsampler = e._Downsampler()
    self.frame_chunks = e._RecordChunker(
      "frame_chunk",
      "camera",
      chunk_size,
    )
    self.model_chunks = e._RecordChunker(
      "model_path_chunk",
      "source",
      max(16, min(256, chunk_size)),
    )
    self.dynamics_chunks = e._RecordChunker(
      "dynamics_chunk",
      "schema",
      max(16, min(1024, chunk_size)),
    )
    self.dynamics = e._DynamicsGridBuilder(
      origin_ns=origin_ns,
      origin_stable=origin_stable,
      process_car_params_snapshots=(process_car_params_snapshots),
      process_route_software_snapshots=(process_route_software_snapshots),
    )
    self.markers = e._IntervalMarkers()
    self.latest: dict[str, dict[str, Any]] = {}
    self.previous_car_ns: int | None = None
    self.previous_angle_rad: float | None = None
    self.previous_group: int | None = None
    self.previous_frames: dict[str, dict[str, int]] = {}
    self.last_device_onroad: bool | None = None
    self.last_controls_active: bool | None = None
    self.last_alert: tuple[str, str, str, str] | None = None
    self.event_snapshots: dict[
      str,
      dict[str, dict[str, Any]],
    ] = {
      "onroadEvents": {},
      "starpilotOnroadEvents": {},
    }
    self.service_counts: Counter[str] = Counter()
    self.frame_counts: Counter[str] = Counter()
    self.frame_quality_counts: Counter[str] = Counter()
    self.camera_gap_counts: Counter[str] = Counter()
    self.invalid_event_counts: Counter[str] = Counter()
    self.custom_schema_drops: Counter[tuple[int, str]] = Counter()
    self.utc_anchors: list[dict[str, Any]] = []
    self.segment_runtime: dict[int, dict[str, Any]] = {}
    self.marker_count = 0
    self.telemetry_gap_count = 0
    self.last_time_us = 0
    self.first_time_us = 0
    self.route_distance_m = 0.0
    self.route_distance_included_interval_count = 0
    self.route_distance_excluded_interval_count = 0
    self.previous_distance_sample: tuple[int, float, int] | None = None
    self.route_location_start: str | None = None
    self.route_location_end: str | None = None
    self.utc_anchor_dropped_count = 0
    self.dynamics_row_count = 0
    self.dynamics_nonzero_jerk_count = 0
    self.dynamics_feedforward_eligible_count = 0
    self.controller_type_counts: Counter[str] = Counter()
    self.controller_source_counts: Counter[str] = Counter()
    self.controller_snapshot_hashes: set[str] = set()
    self.effective_snapshot_hashes: set[str] = set()
    self.controller_snapshot_identities: set[tuple[int, int, str]] = set()
    self.effective_snapshot_identities: set[tuple[int, int, str]] = set()
    self.source_age_maxima_us: Counter[str] = Counter()
    self.source_age_valid_row_count = 0
    self.source_age_missing_count = 0
    self.source_age_negative_count = 0
    self.source_age_over_maximum_count = 0
    self.source_age_future_car_state_count = 0
    self.source_age_future_required_count = 0
    self.source_age_identity_mismatch_count = 0
    self.source_identity_invalid_count = 0
    self.source_time_error_mismatch_count = 0
    self.controller_stateful_invalid_count = 0
    self.controller_observed_inconsistent_count = 0
    self.controller_context_after_controls_count = 0
    self.controller_identity_invalid_count = 0
    self.controller_validation_valid_row_count = 0
    self.effective_stateful_invalid_count = 0
    self.effective_context_after_controls_count = 0
    self.effective_source_after_controls_count = 0
    self.effective_source_identity_invalid_count = 0
    self.effective_validation_valid_row_count = 0
    self.controller_profile_invalid_count = 0
    self.controller_profile_valid_row_count = 0
    self.effective_exact_count = 0
    self.effective_missing_count = 0
    self.effective_source_counts: dict[
      str,
      Counter[str],
    ] = {
      "factor": Counter(),
      "offset": Counter(),
      "friction": Counter(),
    }

  def register_segment(self, loaded: _LoadedSegment) -> None:
    self.service_counts.update(loaded.message_counts)
    self.invalid_event_counts.update(
      loaded.invalid_event_counts,
    )
    self.segment_runtime[loaded.segment.number] = {
      "range_us": [None, None],
      "camera_ranges_us": {},
      "camera_start_sources": {},
      "frame_quality_issue_count": 0,
    }

  def _track_route_summary(
    self,
    ref: e._EventRef,
    which: str,
    valid: bool,
    fields: dict[str, Any],
  ) -> None:
    if which == "carState":
      speed = e._finite(fields.get("vehicle.speed")) if valid else None
      if speed is None:
        if self.previous_distance_sample is not None:
          self.route_distance_excluded_interval_count += 1
        self.previous_distance_sample = None
      else:
        current = (
          ref.mono_ns,
          abs(speed),
          ref.continuity_group,
        )
        previous = self.previous_distance_sample
        if previous is not None:
          previous_ns, previous_speed, previous_group = previous
          dt_ns = ref.mono_ns - previous_ns
          if previous_group == ref.continuity_group and 0 < dt_ns <= 250_000_000:
            self.route_distance_m += (previous_speed + abs(speed)) * 0.5 * (dt_ns / 1_000_000_000.0)
            self.route_distance_included_interval_count += 1
          else:
            self.route_distance_excluded_interval_count += 1
        self.previous_distance_sample = current

    if not valid or which not in ("gpsLocation", "gpsLocationExternal"):
      return
    data = getattr(ref.event, which)
    if e._boolean(e._get(data, "hasFix")) is not True:
      return
    latitude = e._finite(e._get(data, "latitude"))
    longitude = e._finite(e._get(data, "longitude"))
    if latitude is None or longitude is None or not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
      return
    location = f"{latitude:.7f},{longitude:.7f}"
    if self.route_location_start is None:
      self.route_location_start = location
    self.route_location_end = location

  def route_summary(self) -> dict[str, Any]:
    distance_available = self.route_distance_included_interval_count > 0
    location_available = self.route_location_start is not None
    return {
      "distance_m": (round(self.route_distance_m, 3) if distance_available else None),
      "location_start": self.route_location_start,
      "location_end": self.route_location_end,
      "provenance": {
        "distance_method": ("trapezoidal_absolute_vEgo_monotonic_dt_le_250ms" if distance_available else None),
        "location_method": ("first_last_valid_gps_fix_lat_lon_decimal_degrees_7" if location_available else None),
        "included_interval_count": (self.route_distance_included_interval_count),
        "excluded_interval_count": (self.route_distance_excluded_interval_count),
      },
    }

  def _reset_causal_visual_state(self, group: int) -> list[dict[str, Any]]:
    if self.previous_group is None:
      self.previous_group = group
      return []
    if self.previous_group == group:
      return []
    records = self.markers.close_all(self.last_time_us)
    self.marker_count += len(records)
    self.latest.clear()
    self.previous_car_ns = None
    self.previous_angle_rad = None
    self.previous_frames.clear()
    self.last_device_onroad = None
    self.last_controls_active = None
    self.last_alert = None
    self.event_snapshots = {
      "onroadEvents": {},
      "starpilotOnroadEvents": {},
    }
    self.previous_group = group
    return records

  def _add_dynamics_rows(
    self,
    rows: Sequence[dict[str, Any]],
  ) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
      if row.get("continuous") is False:
        records.extend(
          self.dynamics_chunks.flush_group(
            e.DYNAMICS_SCHEMA,
          ),
        )
      row["live_torque_used"] = bool(
        row.get("live_torque_used"),
      )
      self.dynamics_row_count += 1
      if abs(float(row["desired_lateral_jerk"])) > 1e-6:
        self.dynamics_nonzero_jerk_count += 1
      if row["future_feedforward_eligible"]:
        self.dynamics_feedforward_eligible_count += 1
      controller_type = str(row.get("controller_type", "unresolved"))
      self.controller_type_counts[controller_type] += 1
      controller_row_valid = bool(row.get("controller_selection_valid") is True and controller_type != "unresolved")
      effective_row_valid = bool(row.get("effective_torque_params_exact") is True)
      selection_source = str(
        row.get("controller_selection_source", "unresolved"),
      )
      self.controller_source_counts[selection_source] += 1
      snapshot_hash = row.get("resolved_toggles_sha256")
      if isinstance(snapshot_hash, str) and snapshot_hash:
        self.controller_snapshot_hashes.add(snapshot_hash)
      effective_snapshot_hash = row.get(
        "effective_resolved_toggles_sha256",
      )
      if isinstance(effective_snapshot_hash, str) and effective_snapshot_hash:
        self.effective_snapshot_hashes.add(
          effective_snapshot_hash,
        )
      controller_identity = (
        row.get("controller_resolved_toggles_segment_num"),
        row.get("controller_resolved_toggles_source_ordinal"),
        row.get(
          "controller_resolved_toggles_log_mono_time_ns",
        ),
      )
      if (
        isinstance(controller_identity[0], int)
        and not isinstance(controller_identity[0], bool)
        and isinstance(controller_identity[1], int)
        and not isinstance(controller_identity[1], bool)
        and isinstance(controller_identity[2], str)
      ):
        self.controller_snapshot_identities.add(
          controller_identity,
        )
      else:
        self.controller_identity_invalid_count += 1
        controller_row_valid = False
      effective_identity = (
        row.get("effective_resolved_toggles_segment_num"),
        row.get("effective_resolved_toggles_source_ordinal"),
        row.get(
          "effective_resolved_toggles_log_mono_time_ns",
        ),
      )
      if (
        isinstance(effective_identity[0], int)
        and not isinstance(effective_identity[0], bool)
        and isinstance(effective_identity[1], int)
        and not isinstance(effective_identity[1], bool)
        and isinstance(effective_identity[2], str)
      ):
        self.effective_snapshot_identities.add(
          effective_identity,
        )
      else:
        self.effective_source_identity_invalid_count += 1
        effective_row_valid = False

      source_row_valid = True
      try:
        nominal_ns = int(row["nominal_log_mono_time_ns"])
        nominal_t_us = int(row["nominal_t_us"])
      except (KeyError, TypeError, ValueError):
        nominal_ns = None
        nominal_t_us = None
      for service, prefix, age_field in (
        ("carState", "car_state", "car_state_age_us"),
        ("carControl", "car_control", "car_control_age_us"),
        ("carOutput", "car_output", "car_output_age_us"),
        ("controlsState", "controls_state", "controls_state_age_us"),
      ):
        age = row.get(age_field)
        if not isinstance(age, int) or isinstance(age, bool):
          self.source_age_missing_count += 1
          source_row_valid = False
        else:
          self.source_age_maxima_us[service] = max(
            self.source_age_maxima_us[service],
            age,
          )
          if age < 0:
            self.source_age_negative_count += 1
            source_row_valid = False
          if age > e.DYNAMICS_REQUIRED_SOURCE_MAX_AGE_US:
            self.source_age_over_maximum_count += 1
            source_row_valid = False

        try:
          source_ns = int(
            row[f"{prefix}_log_mono_time_ns"],
          )
        except (KeyError, TypeError, ValueError):
          source_ns = None
        segment_num = row.get(f"{prefix}_segment_num")
        source_ordinal = row.get(
          f"{prefix}_source_ordinal",
        )
        identity_valid = bool(
          source_ns is not None
          and isinstance(segment_num, int)
          and not isinstance(segment_num, bool)
          and segment_num >= 0
          and isinstance(source_ordinal, int)
          and not isinstance(source_ordinal, bool)
          and source_ordinal >= 0
        )
        if not identity_valid:
          self.source_identity_invalid_count += 1
          source_row_valid = False
          continue
        assert source_ns is not None
        if nominal_ns is None or source_ns > nominal_ns:
          self.source_age_future_required_count += 1
          if service == "carState":
            self.source_age_future_car_state_count += 1
          source_row_valid = False
        if nominal_t_us is None or not isinstance(age, int) or isinstance(age, bool) or age != nominal_t_us - e._t_us(source_ns, self.origin_ns):
          self.source_age_identity_mismatch_count += 1
          source_row_valid = False

      car_state_age = row.get("car_state_age_us")
      if not isinstance(car_state_age, int) or isinstance(car_state_age, bool) or row.get("source_time_error_us") != -car_state_age:
        self.source_time_error_mismatch_count += 1
        source_row_valid = False
      if source_row_valid:
        self.source_age_valid_row_count += 1

      if not (
        row.get("controller_selection_stateful") is True
        and row.get(
          "controller_selection_state_machine_version",
        )
        == 1
      ):
        self.controller_stateful_invalid_count += 1
        controller_row_valid = False
      if row.get("controller_selection_observed_consistent") is not True:
        self.controller_observed_inconsistent_count += 1
        controller_row_valid = False
      try:
        controller_context_ns = int(
          row["controller_selection_context_log_mono_time_ns"],
        )
        controls_source_ns = int(
          row["controls_state_log_mono_time_ns"],
        )
        controller_source_ns = int(
          row["controller_resolved_toggles_log_mono_time_ns"],
        )
      except (KeyError, TypeError, ValueError):
        self.controller_identity_invalid_count += 1
        controller_row_valid = False
      else:
        if controller_context_ns > controls_source_ns or controller_source_ns > controller_context_ns:
          self.controller_context_after_controls_count += 1
          controller_row_valid = False

      if not (
        row.get("effective_torque_params_stateful") is True
        and row.get(
          "effective_torque_params_state_machine_version",
        )
        == 1
      ):
        self.effective_stateful_invalid_count += 1
        effective_row_valid = False
      if row.get(
        "effective_torque_context_log_mono_time_ns",
      ) != row.get("controls_state_log_mono_time_ns"):
        self.effective_context_after_controls_count += 1
        effective_row_valid = False
      source_ages = row.get(
        "effective_torque_params_source_age_us",
        {},
      )
      controls_age = row.get("controls_state_age_us")
      sources = row.get("effective_torque_params_source", {})
      if not isinstance(sources, dict):
        sources = {}
      source_identities = row.get(
        "effective_torque_params_source_identity",
        {},
      )
      if not isinstance(source_identities, dict):
        source_identities = {}
      required_parts = ("factor", "offset", "friction")
      if set(sources) != set(required_parts):
        self.effective_source_identity_invalid_count += 1
        effective_row_valid = False
      try:
        controls_source_ns = int(
          row["controls_state_log_mono_time_ns"],
        )
      except (KeyError, TypeError, ValueError):
        controls_source_ns = None
      for part in required_parts:
        owner = sources.get(part)
        age = source_ages.get(part) if isinstance(source_ages, dict) else None
        identity = source_identities.get(part)
        identity_ns = None
        identity_segment = None
        identity_ordinal = None
        if isinstance(identity, dict):
          try:
            identity_ns = int(identity["log_mono_time_ns"])
          except (KeyError, TypeError, ValueError):
            identity_ns = None
          identity_segment = identity.get("segment_num")
          identity_ordinal = identity.get("source_ordinal")
        identity_valid = bool(
          identity_ns is not None
          and isinstance(identity_segment, int)
          and not isinstance(identity_segment, bool)
          and identity_segment >= 0
          and isinstance(identity_ordinal, int)
          and not isinstance(identity_ordinal, bool)
          and identity_ordinal >= 0
          and owner
          in (
            "car_params",
            "live_filtered",
            "resolved_custom",
          )
        )
        if not identity_valid:
          self.effective_source_identity_invalid_count += 1
          effective_row_valid = False
        if owner == "car_params":
          invalid_source_age = age is not None
        else:
          invalid_source_age = (
            not isinstance(age, int) or isinstance(age, bool) or not isinstance(controls_age, int) or isinstance(controls_age, bool) or age < controls_age
          )
        if owner != "car_params" and identity_ns is not None and controls_source_ns is not None and identity_ns > controls_source_ns:
          invalid_source_age = True
        if owner != "car_params" and identity_ns is not None and nominal_t_us is not None and age != nominal_t_us - e._t_us(identity_ns, self.origin_ns):
          invalid_source_age = True
        if invalid_source_age:
          self.effective_source_after_controls_count += 1
          effective_row_valid = False
      profile = e.REVIEWED_IONIQ5_CONTROLLER_PROFILES_BY_ID.get(
        str(row.get("baseline_controller_profile_id")),
      )
      profile_params = profile.get("baseline_controller_params") if profile is not None else None
      profile_row_valid = bool(
        profile is not None
        and isinstance(profile_params, dict)
        and row.get("baseline_controller_params_sha256") == profile.get("baseline_controller_params_sha256")
        and row.get(
          "baseline_controller_source_starpilot_commit",
        )
        == profile.get("source_starpilot_commit")
        and row.get(
          "vehicle_lat_accel_factor_multiplier",
        )
        == profile_params.get("base_lat_accel_factor_mult")
      )
      if not profile_row_valid:
        self.controller_profile_invalid_count += 1
      else:
        self.controller_profile_valid_row_count += 1
      for part in ("factor", "offset", "friction"):
        self.effective_source_counts[part][str(sources.get(part, "unknown"))] += 1
      if row.get("effective_torque_params_exact"):
        self.effective_exact_count += 1
      else:
        self.effective_missing_count += 1
        effective_row_valid = False
      if controller_row_valid:
        self.controller_validation_valid_row_count += 1
      if effective_row_valid:
        self.effective_validation_valid_row_count += 1
      records.extend(
        self.dynamics_chunks.add(e.DYNAMICS_SCHEMA, row),
      )
    return records

  def process_group(
    self,
    refs: Sequence[e._EventRef],
  ) -> list[dict[str, Any]]:
    if not refs:
      return []
    records = self._reset_causal_visual_state(
      refs[0].continuity_group,
    )
    if self.route.log_type == "rlog":
      records.extend(
        self._add_dynamics_rows(
          self.dynamics.add_event_group(refs),
        )
      )
    for ref in refs:
      records.extend(self._process_event(ref))
    return records

  def _process_event(self, ref: e._EventRef) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    event = ref.event
    which = e._event_which(event)
    if which is None:
      return records
    valid = e._event_valid(event)
    time_us = e._t_us(ref.mono_ns, self.origin_ns)
    runtime = self.segment_runtime[ref.segment_num]
    bounds = runtime["range_us"]
    bounds[0] = time_us if bounds[0] is None else min(bounds[0], time_us)
    bounds[1] = time_us if bounds[1] is None else max(bounds[1], time_us)
    self.last_time_us = max(self.last_time_us, time_us)
    self.first_time_us = min(self.first_time_us, time_us)

    fields: dict[str, Any] = {}
    if valid:
      fields = e._event_fields(event, which)
      if fields:
        self.latest[which] = fields
        if which == "carState":
          angle = fields.get("vehicle.steering_angle")
          signed_rate = 0.0
          if isinstance(angle, float) and self.previous_angle_rad is not None and self.previous_car_ns is not None:
            dt_s = (ref.mono_ns - self.previous_car_ns) / 1e9
            if 0.0001 < dt_s < 0.09:
              signed_rate = (angle - self.previous_angle_rad) / dt_s
            elif dt_s >= 0.25:
              records.append(
                self.markers.point(
                  time_us,
                  kind="telemetry_gap",
                  label=f"{dt_s:.3f} s vehicle-state gap",
                  severity="warning",
                  attributes={
                    "gap_us": int(dt_s * 1e6),
                    "segment_num": ref.segment_num,
                  },
                )
              )
              self.marker_count += 1
              self.telemetry_gap_count += 1
          fields["vehicle.steering_rate_signed"] = signed_rate
          self.previous_car_ns = ref.mono_ns
          self.previous_angle_rad = angle if isinstance(angle, float) else None
        for signal, value in fields.items():
          if signal not in e.SIGNAL_BY_ID or value is None:
            continue
          records.extend(
            self.signal_chunks.add(
              signal,
              "full",
              time_us,
              value,
              ref.mono_ns,
              ref.segment_num,
              ref.source_ordinal,
            )
          )
          for (
            tier_signal,
            tier,
            tier_time,
            tier_value,
            tier_mono,
            tier_segment,
            tier_ordinal,
          ) in self.downsampler.add(
            signal,
            e.SIGNAL_BY_ID[signal].value_type,
            time_us,
            value,
            ref.mono_ns,
            ref.segment_num,
            ref.source_ordinal,
          ):
            records.extend(
              self.signal_chunks.add(
                tier_signal,
                tier,
                tier_time,
                tier_value,
                tier_mono,
                tier_segment,
                tier_ordinal,
              )
            )
    elif which == "carState":
      self.previous_car_ns = None
      self.previous_angle_rad = None

    self._track_route_summary(
      ref,
      which,
      valid,
      fields,
    )

    if which in e.CAMERA_SERVICES:
      records.extend(
        self._process_frame(ref, time_us),
      )

    if valid and which == "modelV2":
      row = e._model_path_row(
        event,
        time_us,
        ref.segment_num,
        self.origin_ns,
      )
      row["log_mono_time_ns"] = str(ref.mono_ns)
      row["source_ordinal"] = ref.source_ordinal
      records.extend(
        self.model_chunks.add("modelV2.position", row),
      )

    if valid and which in (
      "gpsLocation",
      "gpsLocationExternal",
      "liveLocationKalman",
    ):
      anchor = e._utc_anchor(event, which, time_us)
      if anchor is not None:
        anchor.update(
          {
            "log_mono_time_ns": str(ref.mono_ns),
            "segment_num": ref.segment_num,
            "source_ordinal": ref.source_ordinal,
          }
        )
        if len(self.utc_anchors) < e.MAX_RETAINED_UTC_ANCHORS:
          self.utc_anchors.append(anchor)
        else:
          self.utc_anchor_dropped_count += 1

    if valid:
      records.extend(
        self._process_markers(ref, which, fields, time_us),
      )
    return records

  def _process_frame(
    self,
    ref: e._EventRef,
    time_us: int,
  ) -> list[dict[str, Any]]:
    event = ref.event
    which = e._event_which(event)
    assert which is not None
    camera, file_name = e.CAMERA_SERVICES[which]
    row = e._frame_row(
      event,
      which,
      time_us,
      self.origin_ns,
    )
    row["source_file"] = file_name
    row["source_ordinal"] = ref.source_ordinal
    self.frame_counts[camera] += 1
    anomalies: list[str] = []
    timestamp_exact = row["timestamp_quality"] in ("exact_encoder_timestamp", "encoder_sof_fallback")
    if not row["event_valid"]:
      anomalies.append("invalid_event")
    if not timestamp_exact:
      anomalies.append("missing_encoder_timestamp")
    if row["segment_num"] is not None and row["segment_num"] != ref.segment_num:
      anomalies.append("encode_segment_num_mismatch")
    frame_time_us = row["t_us"]
    runtime = self.segment_runtime[ref.segment_num]
    if row["event_valid"] and timestamp_exact:
      ranges = runtime["camera_ranges_us"]
      bounds = ranges.setdefault(
        camera,
        [frame_time_us, frame_time_us],
      )
      bounds[0] = min(bounds[0], frame_time_us)
      bounds[1] = max(bounds[1], frame_time_us)
      runtime["camera_start_sources"].setdefault(
        camera,
        row["timestamp_source"],
      )

    previous = self.previous_frames.get(camera)
    if row["event_valid"] and timestamp_exact:
      if previous is not None:
        if isinstance(row["frame_id"], int) and row["frame_id"] <= previous["frame_id"]:
          anomalies.append("frame_id_not_increasing")
        elif isinstance(row["frame_id"], int) and row["frame_id"] > previous["frame_id"] + 1:
          anomalies.append("frame_id_gap")
        if isinstance(row["encode_id"], int) and row["encode_id"] <= previous["encode_id"]:
          anomalies.append("encode_id_not_increasing")
        if frame_time_us <= previous["t_us"]:
          anomalies.append("timestamp_not_increasing")
        elif frame_time_us - previous["t_us"] > 200_000:
          anomalies.append("timestamp_gap")
        if previous["segment_num"] == ref.segment_num and isinstance(row["segment_frame_id"], int):
          if row["segment_frame_id"] <= previous["segment_frame_id"]:
            anomalies.append("segment_frame_id_not_increasing")
          elif row["segment_frame_id"] > previous["segment_frame_id"] + 1:
            anomalies.append("segment_frame_id_gap")
        elif previous["segment_num"] != ref.segment_num and isinstance(row["segment_frame_id"], int) and row["segment_frame_id"] > 0:
          anomalies.append("segment_frame_id_start_gap")
      elif isinstance(row["segment_frame_id"], int) and row["segment_frame_id"] > 0:
        anomalies.append("segment_frame_id_start_gap")
      if all(isinstance(row[name], int) for name in ("frame_id", "encode_id", "segment_frame_id")):
        self.previous_frames[camera] = {
          "frame_id": row["frame_id"],
          "encode_id": row["encode_id"],
          "segment_frame_id": row["segment_frame_id"],
          "t_us": frame_time_us,
          "segment_num": ref.segment_num,
        }

    records: list[dict[str, Any]] = []
    if anomalies:
      anomalies = sorted(set(anomalies))
      for anomaly in anomalies:
        self.frame_quality_counts[f"{camera}:{anomaly}"] += 1
      runtime["frame_quality_issue_count"] += 1
      self.camera_gap_counts[camera] += 1
      records.append(
        self.markers.point(
          frame_time_us,
          kind="camera_gap",
          label=f"{camera} camera index anomaly",
          severity="warning",
          attributes={
            "camera": camera,
            "anomalies": anomalies,
            "segment_num": ref.segment_num,
          },
        )
      )
      self.marker_count += 1
    records.extend(self.frame_chunks.add(camera, row))
    return records

  def _process_markers(
    self,
    ref: e._EventRef,
    which: str,
    fields: dict[str, Any],
    time_us: int,
  ) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if which == "deviceState":
      onroad = fields.get("device.onroad")
      if isinstance(onroad, bool):
        if self.last_device_onroad is None or onroad != self.last_device_onroad:
          records.append(
            self.markers.point(
              time_us,
              kind="onroad" if onroad else "offroad",
              label=("Device went onroad" if onroad else "Device went offroad"),
              attributes={
                "initial_state": self.last_device_onroad is None,
                "truncated_start": not self.origin_stable,
              },
            )
          )
          self.marker_count += 1
        self.last_device_onroad = onroad
        closed = self.markers.transition(
          "onroad_interval",
          onroad,
          time_us,
          kind="onroad_interval",
          label="Device onroad",
        )
        self.marker_count += len(closed)
        records.extend(closed)

    if which in ("carState", "carControl"):
      car = self.latest.get("carState", {})
      control = self.latest.get("carControl", {})
      closed = self.markers.transition(
        "driver_overlay",
        bool(car.get("vehicle.steering_pressed")) and bool(control.get("control.lateral_active")),
        time_us,
        kind="driver_overlay",
        label="Driver steering input while lateral control was active",
        severity="warning",
      )
      self.marker_count += len(closed)
      records.extend(closed)

    if which == "controlsState":
      closed = self.markers.transition(
        "lateral_saturation",
        bool(fields.get("lateral.saturated")),
        time_us,
        kind="lateral_saturation",
        label="Lateral controller saturated",
        severity="warning",
      )
      self.marker_count += len(closed)
      records.extend(closed)

    if which == "selfdriveState":
      active = bool(fields.get("selfdrive.active"))
      if self.last_controls_active is None or active != self.last_controls_active:
        records.append(
          self.markers.point(
            time_us,
            kind=("controls_state_observed" if self.last_controls_active is None else ("engagement" if active else "disengagement")),
            label=("Controls active" if active else "Controls inactive"),
          )
        )
        self.marker_count += 1
      self.last_controls_active = active
      closed = self.markers.transition(
        "controls_active",
        active,
        time_us,
        kind="controls_active",
        label="Controls active",
      )
      self.marker_count += len(closed)
      records.extend(closed)
      alert = e._alert_signature(ref.event)
      if alert != self.last_alert:
        closed = self.markers.transition(
          "alert",
          False,
          time_us,
          kind="alert",
          label="",
        )
        self.marker_count += len(closed)
        records.extend(closed)
        if alert is not None:
          alert_type, text1, text2, status = alert
          self.markers.transition(
            "alert",
            True,
            time_us,
            kind="alert",
            label=text1 or alert_type,
            severity=("critical" if status == "critical" else "warning"),
            attributes={
              "alert_type": alert_type,
              "text_2": text2,
              "status": status,
            },
          )
        self.last_alert = alert

    if which in ("onroadEvents", "starpilotOnroadEvents"):
      try:
        event_rows = e._onroad_event_rows(ref.event, which)
      except capnp.KjException:
        self.custom_schema_drops[(ref.segment_num, which)] += 1
        return records
      current = {row["name"]: row for row in event_rows}
      previous = self.event_snapshots[which]
      for name in sorted(set(previous) | set(current)):
        if name not in current:
          records.append(
            self.markers.point(
              time_us,
              kind="event_cleared",
              label=f"{name} cleared",
              attributes={
                "source": which,
                "name": name,
                "previous": previous[name],
              },
            )
          )
          self.marker_count += 1
        elif name not in previous or current[name] != previous[name]:
          row = current[name]
          records.append(
            self.markers.point(
              time_us,
              kind="event",
              label=row["name"],
              severity=e._event_severity(row),
              attributes={"source": which, **row},
            )
          )
          self.marker_count += 1
      self.event_snapshots[which] = current
    return records

  def flush(self) -> list[dict[str, Any]]:
    self.dynamics.finish()
    records: list[dict[str, Any]] = []
    for (
      signal,
      tier,
      tier_time,
      tier_value,
      tier_mono,
      tier_segment,
      tier_ordinal,
    ) in self.downsampler.flush():
      records.extend(
        self.signal_chunks.add(
          signal,
          tier,
          tier_time,
          tier_value,
          tier_mono,
          tier_segment,
          tier_ordinal,
        )
      )
    records.extend(self.signal_chunks.flush())
    records.extend(self.frame_chunks.flush())
    records.extend(self.model_chunks.flush())
    records.extend(self.dynamics_chunks.flush())
    closing = self.markers.close_all(self.last_time_us)
    self.marker_count += len(closing)
    records.extend(closing)
    return records


FLM_EVALUATOR_NAME = "starpilot-flm-availability-by-source-commit"
FLM_EVALUATOR_VERSION = 1
FLM_ABSENT_SOURCE_COMMITS = {
  "2747bf037c0f284500457f1befb4f52415e3285a",
  "6dd6c0a3d558842b91b903e1cddfaca576a69c25",
}
FLM_EVALUATOR_SOURCE_PAYLOAD = {
  "name": FLM_EVALUATOR_NAME,
  "results_by_source_commit": dict.fromkeys(
    sorted(FLM_ABSENT_SOURCE_COMMITS),
    "absent",
  ),
  "version": FLM_EVALUATOR_VERSION,
}
FLM_EVALUATOR_SOURCE_SHA256 = hashlib.sha256(
  json.dumps(
    FLM_EVALUATOR_SOURCE_PAYLOAD,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
  ).encode(),
).hexdigest()


def _records_for_refs(
  builder: _RouteRecordBuilder,
  refs: Sequence[e._EventRef],
) -> Iterator[dict[str, Any]]:
  for group in _group_by_timestamp(refs):
    yield from builder.process_group(group)


def _set_continuity_group(
  loaded: _LoadedSegment,
  continuity_group: int,
) -> None:
  loaded.continuity_group = continuity_group
  loaded.refs = [
    e._EventRef(
      ref.mono_ns,
      ref.segment_num,
      ref.source_ordinal,
      continuity_group,
      ref.event,
    )
    for ref in loaded.refs
  ]


def _normalized_resolved_snapshot(
  snapshot: dict[str, Any],
) -> dict[str, Any]:
  values = dict(snapshot.get("values", {}))
  return {
    "sha256": snapshot.get("sha256"),
    "source": "starpilotPlan.starpilotToggles",
    "valid": bool(snapshot.get("valid")),
    "values": dict(sorted(values.items())),
    "segment_num": snapshot["segment_num"],
    "source_ordinal": snapshot["source_ordinal"],
    "log_mono_time_ns": snapshot["log_mono_time_ns"],
  }


def _flm_resolution(
  route_software: dict[str, Any] | None,
  car_params: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
  source_commit = route_software.get("git_source_commit") or route_software.get("git_commit") if route_software is not None else None
  runtime = e._controller_runtime_context(
    route_software,
    car_params,
  )
  if source_commit in FLM_ABSENT_SOURCE_COMMITS:
    runtime.update(
      {
        "flm_active": False,
        "flm_active_available": True,
      }
    )
    return (
      runtime,
      {
        "state": "verified",
        "flm_active": False,
        "source": "versioned_source_commit_evaluator",
        "evaluator": {
          "name": FLM_EVALUATOR_NAME,
          "version": FLM_EVALUATOR_VERSION,
          "source_commit": source_commit,
          "source_sha256": FLM_EVALUATOR_SOURCE_SHA256,
        },
      },
    )
  if runtime["flm_active_available"]:
    return (
      runtime,
      {
        "state": "verified",
        "flm_active": runtime["flm_active"],
        "source": "initData_controller_params",
        "evaluator": None,
      },
    )
  return (
    runtime,
    {
      "state": "unavailable",
      "flm_active": None,
      "source": "unresolved",
      "evaluator": None,
    },
  )


def iter_route_records(
  route: e.RouteInput,
  chunk_size: int = 4096,
) -> Iterator[dict[str, Any]]:
  if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < e.MIN_CHUNK_SIZE or chunk_size > e.MAX_CHUNK_SIZE:
    raise e.InputError(
      "chunk_size must be an integer from " + f"{e.MIN_CHUNK_SIZE} through {e.MAX_CHUNK_SIZE}",
    )
  if not route.segments:
    raise e.ExtractionError("route has no segments")
  if len(route.segments) > e.MAX_ROUTE_SEGMENTS:
    raise e.ResourceLimitError(
      "route_segment_count",
      e.MAX_ROUTE_SEGMENTS,
      len(route.segments),
    )

  total_source_bytes = 0
  total_decompressed_bytes = 0
  first = route.segments[0]
  try:
    loaded, origin_ns = _load_segment(
      first,
      origin_ns=None,
      continuity_group=0,
      remaining_source_bytes=(e.MAX_SOURCE_BYTES_PER_ROUTE),
      remaining_decompressed_bytes=(e.MAX_DECOMPRESSED_BYTES_PER_ROUTE),
    )
  except e.ResourceLimitError as exc:
    if exc.code == "segment_source_bytes" and e.MAX_SOURCE_BYTES_PER_ROUTE < e.MAX_SOURCE_BYTES_PER_SEGMENT:
      raise e.ResourceLimitError(
        "route_source_bytes",
        e.MAX_SOURCE_BYTES_PER_ROUTE,
        exc.observed,
      ) from exc
    if exc.code == "segment_decompressed_bytes" and e.MAX_DECOMPRESSED_BYTES_PER_ROUTE < e.MAX_DECOMPRESSED_BYTES_PER_SEGMENT:
      raise e.ResourceLimitError(
        "route_decompressed_bytes",
        e.MAX_DECOMPRESSED_BYTES_PER_ROUTE,
        exc.observed,
      ) from exc
    raise
  total_source_bytes += loaded.source_size_bytes
  total_decompressed_bytes += len(loaded.payload)
  first_boundary = loaded.boundary
  origin_stable = first.number == 0 and first_boundary["start_valid"]
  origin_id = hashlib.sha256(
    f"{route.route_id}\0{origin_ns}".encode(),
  ).hexdigest()

  missing_segments = e._missing_segment_numbers(route.segments)
  warnings: list[dict[str, Any]] = []
  if missing_segments:
    warnings.append(
      e._warning(
        "missing_segments",
        "One or more segment numbers are absent from the supplied route.",
        missing_segment_numbers=missing_segments,
      )
    )

  car_params_by_segment: dict[
    int,
    dict[str, Any] | None,
  ] = {}
  route_software_by_segment: dict[
    int,
    dict[str, Any] | None,
  ] = {}
  resolved_toggles_by_segment: dict[
    int,
    dict[str, Any] | None,
  ] = {}
  all_car_params_snapshots: list[dict[str, Any]] = []
  all_route_software_snapshots: list[dict[str, Any]] = []
  process_car_params_snapshots: list[dict[str, Any]] = []
  process_route_software_snapshots: list[dict[str, Any]] = []
  all_resolved_toggle_snapshots: list[dict[str, Any]] = []
  source_objects: list[dict[str, Any]] = []
  segment_metadata: dict[int, dict[str, Any]] = {}
  failed_segments: list[int] = []
  ambiguous_car_params_segments: set[int] = set()
  ambiguous_software_segments: set[int] = set()
  deduplicated_event_count = 0
  maximum_observed_overlap_ns = 0

  builder = _RouteRecordBuilder(
    route=route,
    origin_ns=origin_ns,
    origin_stable=origin_stable,
    chunk_size=chunk_size,
    car_params_by_segment=car_params_by_segment,
    route_software_by_segment=route_software_by_segment,
    resolved_toggles_by_segment=resolved_toggles_by_segment,
    process_car_params_snapshots=(process_car_params_snapshots),
    process_route_software_snapshots=(process_route_software_snapshots),
  )

  previous_loaded: _LoadedSegment | None = None
  previous_car_params: dict[str, Any] | None = None
  previous_software: dict[str, Any] | None = None
  previous_toggles: dict[str, Any] | None = None

  def register(
    item: _LoadedSegment,
    *,
    carry: bool,
  ) -> None:
    nonlocal previous_car_params
    nonlocal previous_software
    nonlocal previous_toggles
    segment_num = item.segment.number
    if len({snapshot.get("_wire_sha256") for snapshot in item.car_params_snapshots} - {None}) > 1:
      ambiguous_car_params_segments.add(segment_num)
    if len({snapshot.get("controller_params_sha256") for snapshot in item.route_software_snapshots} - {None}) > 1:
      ambiguous_software_segments.add(segment_num)
    selected_car_params = item.config_car_params if item.config_car_params is not None else (previous_car_params if carry else None)
    selected_software = item.config_route_software if item.config_route_software is not None else (previous_software if carry else None)
    selected_toggles = item.config_resolved_toggles if item.config_resolved_toggles is not None else (previous_toggles if carry else None)
    car_params_by_segment[segment_num] = selected_car_params
    route_software_by_segment[segment_num] = selected_software
    resolved_toggles_by_segment[segment_num] = selected_toggles
    previous_car_params = selected_car_params
    previous_software = selected_software
    previous_toggles = selected_toggles
    all_car_params_snapshots.extend(item.car_params_snapshots)
    process_car_params_snapshots.extend(dict(snapshot) for snapshot in item.car_params_snapshots)
    all_route_software_snapshots.extend(
      item.route_software_snapshots,
    )
    process_route_software_snapshots.extend(dict(snapshot) for snapshot in item.route_software_snapshots)
    all_resolved_toggle_snapshots.extend(_normalized_resolved_snapshot(snapshot) for snapshot in item.resolved_toggle_snapshots)
    source_objects.append(
      {
        "segment_num": segment_num,
        "sha256": item.digest,
        "log_type": route.log_type,
        "size_bytes": item.source_size_bytes,
        "decompressed_size_bytes": len(item.payload),
        "compression": item.compression,
        "file_name": item.segment.path.name,
      }
    )
    segment_metadata[segment_num] = {
      "segment_num": segment_num,
      "directory_name": item.segment.directory_name,
      "log_sha256": item.digest,
      "log_type": route.log_type,
      "source_size_bytes": item.source_size_bytes,
      "decompressed_size_bytes": len(item.payload),
      "compression": item.compression,
      "event_count": sum(item.message_counts.values()),
      "message_counts": dict(sorted(item.message_counts.items())),
      "boundary": item.boundary,
      "corrupt": item.corrupt,
      "unreadable_union_count": item.unreadable_union_count,
      "invalid_event_counts": dict(
        sorted(item.invalid_event_counts.items()),
      ),
    }
    builder.register_segment(item)

  register(loaded, carry=False)

  yield {
    "record": "stream_header",
    "schema": e.CONTRACT_NAME,
    "schema_version": e.CONTRACT_VERSION,
    "extractor_version": e.EXTRACTOR_VERSION,
    "route_id": route.route_id,
    "log_type": route.log_type,
    "timebase": {
      "unit": "us",
      "origin_log_mono_time_ns": str(origin_ns),
      "origin_id": origin_id,
      "origin_stability": ("segment_zero_stable" if origin_stable else "provisional_supplied_subset"),
      "conversion": "floor((logMonoTime-origin)/1000)",
      "stable_sample_identity": [
        "route_id",
        "source_segment_num",
        "source_ordinal",
        "log_mono_time_ns",
      ],
    },
    "tiers": [{"id": "full", "width_us": None}] + [{"id": e._tier_name(width), "width_us": width} for width in e.TIER_WIDTHS_US],
  }
  yield {
    "record": "signal_catalog",
    "signals": [spec.as_dict() for spec in e.SIGNAL_SPECS],
  }
  yield {
    "record": "dynamics_catalog",
    "schema": e.DYNAMICS_SCHEMA,
    "schema_version": e.DYNAMICS_SCHEMA_VERSION,
    "sample_period_us": e.DYNAMICS_SAMPLE_PERIOD_US,
    "required_source_max_age_us": (e.DYNAMICS_REQUIRED_SOURCE_MAX_AGE_US),
    "alignment": "timestamp_causal_recorded_history_asof",
    "source_selection": ("independent_per_source_max_valid_source_with_" + "logMonoTime_at_or_before_tick"),
    "event_order": [
      "logMonoTime",
      "segment_num",
      "source_ordinal",
    ],
    "grid": "absolute_logMonoTime_10ms",
    "invalid_event_policy": {
      "carState": "drop_without_invalidating_prior_valid_state",
      "carControl": "invalidate_until_next_valid",
      "controlsState": "invalidate_until_next_valid",
      "carOutput": "invalidate_until_next_valid",
    },
    "controller_i_timing": "post_update_asof_source_row",
    "effective_torque_context_binding": ("selected_controlsState_logMonoTime"),
    "controller_selection_binding": ("controlsd_initialization_once_per_route_process"),
    "signed_steering_rate": ("causal_grid_difference_of_zoh_steering_angle"),
    "applied_torque_source": ("carOutput.actuatorsOutput.torque_only_no_fallback"),
    "availability": ("available" if route.log_type == "rlog" else "unavailable_qlog_decimated"),
    "columns": list(e.DYNAMICS_COLUMNS),
  }

  pending_refs = loaded.refs
  pending_payload_owners: dict[int, bytes] = {
    loaded.segment.number: loaded.payload,
  }
  previous_loaded = loaded
  current_group = 0
  last_emitted_mono_ns: int | None = None

  for segment in route.segments[1:]:
    remaining_source = e.MAX_SOURCE_BYTES_PER_ROUTE - total_source_bytes
    if remaining_source <= 0:
      raise e.ResourceLimitError(
        "route_source_bytes",
        e.MAX_SOURCE_BYTES_PER_ROUTE,
        total_source_bytes + 1,
      )
    remaining = e.MAX_DECOMPRESSED_BYTES_PER_ROUTE - total_decompressed_bytes
    if remaining <= 0:
      raise e.ResourceLimitError(
        "route_decompressed_bytes",
        e.MAX_DECOMPRESSED_BYTES_PER_ROUTE,
        total_decompressed_bytes + 1,
      )
    try:
      current, _ = _load_segment(
        segment,
        origin_ns=origin_ns,
        continuity_group=current_group,
        remaining_source_bytes=remaining_source,
        remaining_decompressed_bytes=remaining,
      )
    except e.ResourceLimitError as exc:
      if exc.code == "segment_source_bytes" and remaining_source < e.MAX_SOURCE_BYTES_PER_SEGMENT:
        raise e.ResourceLimitError(
          "route_source_bytes",
          e.MAX_SOURCE_BYTES_PER_ROUTE,
          total_source_bytes + exc.observed,
        ) from exc
      if exc.code == "segment_decompressed_bytes" and remaining < e.MAX_DECOMPRESSED_BYTES_PER_SEGMENT:
        raise e.ResourceLimitError(
          "route_decompressed_bytes",
          e.MAX_DECOMPRESSED_BYTES_PER_ROUTE,
          total_decompressed_bytes + exc.observed,
        ) from exc
      raise
    except Exception as exc:
      for record in _records_for_refs(builder, pending_refs):
        yield record
      pending_refs = []
      pending_payload_owners.clear()
      failed_segments.append(segment.number)
      segment_metadata[segment.number] = {
        "segment_num": segment.number,
        "directory_name": segment.directory_name,
        "state": "failed",
        "error_type": type(exc).__name__,
      }
      warnings.append(
        e._warning(
          "segment_read_failed",
          f"Could not read segment: {type(exc).__name__}.",
          severity="error",
          segment_num=segment.number,
        )
      )
      current_group += 1
      previous_loaded = None
      previous_car_params = None
      previous_software = None
      previous_toggles = None
      continue

    total_source_bytes += current.source_size_bytes
    total_decompressed_bytes += len(current.payload)
    if total_decompressed_bytes > e.MAX_DECOMPRESSED_BYTES_PER_ROUTE:
      raise e.ResourceLimitError(
        "route_decompressed_bytes",
        e.MAX_DECOMPRESSED_BYTES_PER_ROUTE,
        total_decompressed_bytes,
      )
    carry = _carry_state(previous_loaded, current)
    if not carry:
      current_group += 1
      _set_continuity_group(current, current_group)
    register(current, carry=carry)

    if not carry:
      for record in _records_for_refs(builder, pending_refs):
        yield record
      pending_refs = current.refs
      pending_payload_owners = {
        current.segment.number: current.payload,
      }
      previous_loaded = current
      continue

    assert previous_loaded is not None
    if current.first_mono_ns is not None and previous_loaded.last_mono_ns is not None:
      overlap_ns = max(
        0,
        previous_loaded.last_mono_ns - current.first_mono_ns,
      )
      maximum_observed_overlap_ns = max(
        maximum_observed_overlap_ns,
        overlap_ns,
      )
      if overlap_ns > e.EVENT_MERGE_MAX_OVERLAP_NS:
        raise e.ExtractionError(
          "adjacent segment overlap exceeds bounded merge window: " + f"{overlap_ns} ns",
        )
    merged = _sorted_merge(pending_refs, current.refs)
    merged, dropped = _deduplicate_refs(merged)
    deduplicated_event_count += dropped
    cutoff = current.first_mono_ns
    if cutoff is None:
      safe_refs = pending_refs
      held_refs = current.refs
    else:
      safe_refs = [ref for ref in merged if ref.mono_ns < cutoff]
      held_refs = [ref for ref in merged if ref.mono_ns >= cutoff]
    if safe_refs and last_emitted_mono_ns is not None and safe_refs[0].mono_ns < last_emitted_mono_ns:
      raise e.ExtractionError(
        "non-adjacent segment overlap would violate global order",
      )
    for record in _records_for_refs(builder, safe_refs):
      yield record
    if safe_refs:
      last_emitted_mono_ns = safe_refs[-1].mono_ns
    pending_refs = held_refs
    pending_payload_owners[current.segment.number] = current.payload
    retained_segments = {ref.segment_num for ref in pending_refs}
    pending_payload_owners = {number: payload for number, payload in pending_payload_owners.items() if number in retained_segments}
    retained_bytes = sum(len(payload) for payload in pending_payload_owners.values())
    retained_count = len(pending_payload_owners)
    if retained_count > e.MAX_RETAINED_SEGMENT_PAYLOADS:
      raise e.ResourceLimitError(
        "retained_overlap_segment_count",
        e.MAX_RETAINED_SEGMENT_PAYLOADS,
        retained_count,
      )
    retained_span = current.segment.number - min(pending_payload_owners) if pending_payload_owners else 0
    if retained_span > 1:
      raise e.ResourceLimitError(
        "retained_overlap_segment_span",
        1,
        retained_span,
      )
    if retained_bytes > e.MAX_RETAINED_DECOMPRESSED_BYTES:
      raise e.ResourceLimitError(
        "retained_overlap_payload_bytes",
        e.MAX_RETAINED_DECOMPRESSED_BYTES,
        retained_bytes,
      )
    previous_loaded = current

  pending_refs, dropped = _deduplicate_refs(pending_refs)
  deduplicated_event_count += dropped
  if pending_refs and last_emitted_mono_ns is not None and pending_refs[0].mono_ns < last_emitted_mono_ns:
    raise e.ExtractionError(
      "final pending event order regressed",
    )
  for record in _records_for_refs(builder, pending_refs):
    yield record
  yield from builder.flush()

  parsed_reports: list[dict[str, Any]] = []
  for segment in route.segments:
    metadata = segment_metadata[segment.number]
    if metadata.get("state") == "failed":
      parsed_reports.append(metadata)
      continue
    runtime = builder.segment_runtime[segment.number]
    camera_ranges = runtime["camera_ranges_us"]
    preferred_camera = (
      "road"
      if "road" in camera_ranges
      else (
        min(
          camera_ranges,
          key=lambda camera: camera_ranges[camera][0],
        )
        if camera_ranges
        else None
      )
    )
    start_source = runtime["camera_start_sources"].get(preferred_camera) if preferred_camera is not None else None
    segment_state = (
      "partial"
      if (metadata["corrupt"] or not metadata["boundary"]["start_valid"] or not metadata["boundary"]["end_valid"] or runtime["frame_quality_issue_count"])
      else "complete"
    )
    parsed_reports.append(
      {
        **metadata,
        "state": segment_state,
        "range_us": runtime["range_us"],
        "start_t_us": (camera_ranges[preferred_camera][0] if preferred_camera is not None else None),
        "start_time_source": (f"{preferred_camera}.encode_index.{start_source}" if preferred_camera is not None else None),
        "camera_ranges_us": dict(sorted(camera_ranges.items())),
        "frame_quality_issue_count": runtime["frame_quality_issue_count"],
      }
    )

  successful_reports = [report for report in parsed_reports if report["state"] != "failed"]
  parsed_numbers = [report["segment_num"] for report in successful_reports]
  route_start_observed = bool(
    successful_reports
    and successful_reports[0]["segment_num"] == 0
    and successful_reports[0]["boundary"]["start_valid"]
    and successful_reports[0]["boundary"]["start"]["type"] == "startOfRoute"
  )
  route_end_observed = bool(successful_reports and successful_reports[-1]["boundary"]["terminal_type"] == "endOfRoute")
  contiguous_from_zero = parsed_numbers == list(range(len(parsed_numbers))) and not failed_segments
  boundary_chain_valid = bool(successful_reports)
  for index, report in enumerate(successful_reports):
    expected = "endOfRoute" if index == len(successful_reports) - 1 else "endOfSegment"
    if report["boundary"]["terminal_type"] != expected:
      boundary_chain_valid = False
      report["state"] = "partial"
      warnings.append(
        e._warning(
          "segment_terminal_mismatch",
          "The segment terminal sentinel does not match its position.",
          segment_num=report["segment_num"],
          expected=expected,
          actual=report["boundary"]["terminal_type"],
        )
      )
  complete_route = bool(
    contiguous_from_zero
    and route_start_observed
    and route_end_observed
    and boundary_chain_valid
    and not missing_segments
    and not failed_segments
    and all(report["state"] == "complete" for report in successful_reports)
  )
  open_route = bool(
    contiguous_from_zero
    and route_start_observed
    and not route_end_observed
    and not missing_segments
    and not failed_segments
    and successful_reports
    and successful_reports[-1]["boundary"]["terminal_type"] is None
    and all(report["state"] == "complete" for report in successful_reports[:-1])
  )
  route_state = "complete" if complete_route else ("open" if open_route else "partial")

  if not route_start_observed:
    warnings.append(
      e._warning(
        "route_start_not_observed",
        "Segment zero with a valid startOfRoute sentinel is required.",
      )
    )
  if not route_end_observed:
    warnings.append(
      e._warning(
        "route_end_not_observed",
        "A valid endOfRoute sentinel was not observed.",
        severity="info",
      )
    )
  if builder.invalid_event_counts:
    warnings.append(
      e._warning(
        "invalid_events",
        "Event.valid was false; affected semantic data was rejected.",
        counts=dict(sorted(builder.invalid_event_counts.items())),
      )
    )
  unreadable_total = sum(report.get("unreadable_union_count", 0) for report in successful_reports)
  if unreadable_total:
    warnings.append(
      e._warning(
        "unreadable_event_union",
        "One or more Cap'n Proto event union discriminants were unreadable.",
        severity="error",
        count=unreadable_total,
      )
    )
  if builder.dynamics.drop_reasons:
    warnings.append(
      e._warning(
        "dynamics_grid_rows_rejected",
        "Grid ticks that lacked exact causal plant/controller inputs were omitted.",
        severity="info",
        counts=dict(
          sorted(builder.dynamics.drop_reasons.items()),
        ),
      )
    )
  if route.log_type != "rlog":
    warnings.append(
      e._warning(
        "dynamics_unavailable_qlog",
        "qlog is visualization-only; no 100 Hz dynamics rows were synthesized.",
        severity="info",
      )
    )
  if not builder.utc_anchors:
    warnings.append(
      e._warning(
        "missing_utc_anchor",
        "No trustworthy valid GPS/localization UTC anchor was present.",
        severity="info",
      )
    )
  if builder.utc_anchor_dropped_count:
    warnings.append(
      e._warning(
        "utc_anchor_retention_limit",
        "Additional valid UTC anchors were omitted after the bounded retention limit.",
        severity="info",
        retained_count=len(builder.utc_anchors),
        dropped_count=builder.utc_anchor_dropped_count,
        limit=e.MAX_RETAINED_UTC_ANCHORS,
      )
    )

  car_params_hashes = {snapshot["_wire_sha256"] for snapshot in all_car_params_snapshots if snapshot.get("_wire_sha256")}
  car_params = all_car_params_snapshots[0] if len(car_params_hashes) == 1 and all_car_params_snapshots else None
  controller_hashes = {snapshot["controller_params_sha256"] for snapshot in all_route_software_snapshots if snapshot.get("controller_params_sha256")}
  source_commits = {
    str(commit)
    for snapshot in all_route_software_snapshots
    if (
      commit := snapshot.get(
        "git_source_commit",
      )
      or snapshot.get("git_commit")
    )
  }
  route_software = all_route_software_snapshots[-1] if all_route_software_snapshots else None
  car_params_wire_sha256 = next(iter(car_params_hashes)) if len(car_params_hashes) == 1 else None
  car_params_summary_sha256 = (
    hashlib.sha256(
      json.dumps(
        car_params,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
      ).encode(),
    ).hexdigest()
    if car_params is not None
    else None
  )
  controller_params_sha256 = next(iter(controller_hashes)) if len(controller_hashes) == 1 else None
  source_starpilot_commit = next(iter(source_commits)) if len(source_commits) == 1 else None
  if len(car_params_hashes) > 1:
    warnings.append(
      e._warning(
        "car_params_changed",
        "The full wire carParams changed within the route.",
        hashes=sorted(car_params_hashes),
      )
    )
  if len(controller_hashes) > 1:
    warnings.append(
      e._warning(
        "controller_params_changed",
        "Allowlisted controller Params changed within the route.",
        hashes=sorted(controller_hashes),
      )
    )
  if len(source_commits) > 1:
    warnings.append(
      e._warning(
        "source_starpilot_commit_changed",
        "The StarPilot source commit changed within the route.",
        commits=sorted(source_commits),
      )
    )

  signals: list[dict[str, Any]] = []
  for spec in e.SIGNAL_SPECS:
    count = builder.signal_chunks.sample_counts[spec.signal_id]
    if not count:
      continue
    signal = spec.as_dict()
    signal.update(
      {
        "sample_count": count,
        "coverage": [builder.signal_chunks.ranges[spec.signal_id]],
      }
    )
    signals.append(signal)

  utc_start_us = None
  utc_solution = None
  if builder.utc_anchors:
    offsets = [int(anchor["utc_us"]) - int(anchor["t_us"]) for anchor in builder.utc_anchors]
    offsets.sort()
    median_offset = offsets[len(offsets) // 2]
    residuals = [abs(offset - median_offset) for offset in offsets]
    utc_start_us = str(median_offset)
    utc_solution = {
      "method": "median_validated_gps_offset",
      "anchor_count": len(offsets),
      "max_residual_us": max(residuals),
    }

  timeline_hasher = hashlib.sha256()
  timeline_hasher.update(route.route_id.encode())
  timeline_hasher.update(b"\0")
  timeline_hasher.update(str(origin_ns).encode())
  for source in source_objects:
    timeline_hasher.update(b"\0")
    timeline_hasher.update(str(source["segment_num"]).encode())
    timeline_hasher.update(b":")
    timeline_hasher.update(source["sha256"].encode())
  timeline_version = timeline_hasher.hexdigest()
  extractor_hash = e._extractor_source_sha256()
  extractor_dirty = e._extractor_dirty()

  row_count = builder.dynamics_row_count
  conventional_count = builder.controller_type_counts["conventional_torque"]
  controller_unresolved_count = builder.controller_type_counts["unresolved"]
  source_age_validation = {
    "schema": "comma-companion.source-age-proof",
    "schema_version": 1,
    "state": (
      "verified"
      if (
        builder.source_age_valid_row_count == row_count
        and builder.source_age_missing_count == 0
        and builder.source_age_negative_count == 0
        and builder.source_age_over_maximum_count == 0
        and builder.source_age_future_required_count == 0
        and builder.source_age_identity_mismatch_count == 0
        and builder.source_identity_invalid_count == 0
        and builder.source_time_error_mismatch_count == 0
      )
      else "failed"
    ),
    "alignment": ("latest_at_or_before_grid_time_zero_order_hold"),
    "maximum_age_us": e.DYNAMICS_REQUIRED_SOURCE_MAX_AGE_US,
    "comparison": "0 <= age_us <= maximum_age_us",
    "checked_row_count": row_count,
    "valid_row_count": builder.source_age_valid_row_count,
    "missing_required_age_count": (builder.source_age_missing_count),
    "negative_required_age_count": (builder.source_age_negative_count),
    "over_maximum_age_count": (builder.source_age_over_maximum_count),
    "future_car_state_count": (builder.source_age_future_car_state_count),
    "future_required_source_count": (builder.source_age_future_required_count),
    "source_identity_invalid_count": (builder.source_identity_invalid_count),
    "source_age_identity_mismatch_count": (builder.source_age_identity_mismatch_count),
    "source_time_error_mismatch_count": (builder.source_time_error_mismatch_count),
    "car_state_time_relation": ("source_time_error_us == -car_state_age_us"),
    "required_sources": {
      "car_state": {
        "field": "car_state_age_us",
        "source": "carState",
      },
      "car_control": {
        "field": "car_control_age_us",
        "source": "carControl",
      },
      "car_output": {
        "field": "car_output_age_us",
        "source": "carOutput.actuatorsOutput.torque",
      },
      "controls_state": {
        "field": "controls_state_age_us",
        "source": "controlsState",
      },
    },
    "maximum_observed_age_us": {
      key: builder.source_age_maxima_us.get(key, 0)
      for key in (
        "carState",
        "carControl",
        "carOutput",
        "controlsState",
      )
    },
  }
  controller_selection_validation = {
    "schema": "comma-companion.controller-selection-proof",
    "schema_version": 1,
    "state": (
      "verified"
      if (
        builder.controller_validation_valid_row_count == row_count
        and controller_unresolved_count == 0
        and builder.controller_stateful_invalid_count == 0
        and builder.controller_observed_inconsistent_count == 0
        and builder.controller_context_after_controls_count == 0
        and builder.controller_identity_invalid_count == 0
      )
      else "failed"
    ),
    "evaluator": {
      "name": e.CONTROLLER_SELECTION_EVALUATOR_NAME,
      "version": e.CONTROLLER_SELECTION_EVALUATOR_VERSION,
      "source_sha256": (e.CONTROLLER_SELECTION_EVALUATOR_SOURCE_SHA256),
    },
    "checked_row_count": row_count,
    "resolved_row_count": (builder.controller_validation_valid_row_count),
    "missing_row_count": 0,
    "invalid_row_count": (row_count - builder.controller_validation_valid_row_count),
    "stateful_invalid_row_count": (builder.controller_stateful_invalid_count),
    "observed_inconsistent_row_count": (builder.controller_observed_inconsistent_count),
    "context_after_controls_row_count": (builder.controller_context_after_controls_count),
    "source_identity_invalid_count": (builder.controller_identity_invalid_count),
    "controller_types": sorted(builder.controller_type_counts),
    "conventional_torque_row_count": conventional_count,
    "nnff_row_count": builder.controller_type_counts["nnff"],
    "nnff_lite_row_count": builder.controller_type_counts["nnff_lite"],
    "unsupported_row_count": (builder.controller_type_counts["unsupported"]),
    "source_types": sorted(builder.controller_source_counts),
    "snapshot_hashes": sorted(
      builder.controller_snapshot_hashes,
    ),
  }
  selected_resolved_toggle_snapshots = [
    snapshot
    for snapshot in all_resolved_toggle_snapshots
    if (
      snapshot.get("valid") is True
      and (
        snapshot.get("segment_num"),
        snapshot.get("source_ordinal"),
        snapshot.get("log_mono_time_ns"),
      )
      in builder.controller_snapshot_identities
    )
  ]
  effective_resolved_toggle_snapshots = [
    snapshot
    for snapshot in all_resolved_toggle_snapshots
    if (
      snapshot.get("valid") is True
      and (
        snapshot.get("segment_num"),
        snapshot.get("source_ordinal"),
        snapshot.get("log_mono_time_ns"),
      )
      in builder.effective_snapshot_identities
    )
  ]
  effective_validation = {
    "schema": "comma-companion.effective-torque-context-proof",
    "schema_version": 1,
    "evaluator": {
      "name": e.TORQUE_CONTEXT_EVALUATOR_NAME,
      "version": e.TORQUE_CONTEXT_EVALUATOR_VERSION,
      "source_sha256": (e.TORQUE_CONTEXT_EVALUATOR_SOURCE_SHA256),
    },
    "state": (
      "verified"
      if (
        builder.effective_validation_valid_row_count == row_count
        and builder.effective_exact_count == row_count
        and builder.effective_missing_count == 0
        and builder.effective_stateful_invalid_count == 0
        and builder.effective_context_after_controls_count == 0
        and builder.effective_source_after_controls_count == 0
        and builder.effective_source_identity_invalid_count == 0
      )
      else "failed"
    ),
    "checked_row_count": row_count,
    "exact_row_count": builder.effective_exact_count,
    "inexact_row_count": builder.effective_missing_count,
    "missing_field_row_count": builder.effective_missing_count,
    "valid_row_count": (builder.effective_validation_valid_row_count),
    "invalid_row_count": (row_count - builder.effective_validation_valid_row_count),
    "stateful_invalid_row_count": (builder.effective_stateful_invalid_count),
    "context_not_bound_to_controls_row_count": (builder.effective_context_after_controls_count),
    "source_after_controls_row_count": (builder.effective_source_after_controls_count),
    "source_identity_invalid_count": (builder.effective_source_identity_invalid_count),
    "snapshot_hashes": sorted(
      builder.effective_snapshot_hashes,
    ),
    "factor_source_counts": dict(
      sorted(builder.effective_source_counts["factor"].items()),
    ),
    "offset_source_counts": dict(
      sorted(builder.effective_source_counts["offset"].items()),
    ),
    "friction_source_counts": dict(
      sorted(
        builder.effective_source_counts["friction"].items(),
      )
    ),
  }
  baseline_profile = (
    e._baseline_controller_profile(
      route_software,
      car_params,
    )
    if (len(controller_hashes) == 1 and len(source_commits) == 1)
    else None
  )
  controller_profile_validation = {
    "schema": "comma-companion.controller-profile-proof",
    "schema_version": 1,
    "state": (
      "verified"
      if (
        row_count > 0
        and baseline_profile is not None
        and builder.controller_profile_valid_row_count == row_count
        and builder.controller_profile_invalid_count == 0
      )
      else "failed"
    ),
    "checked_row_count": row_count,
    "valid_row_count": (builder.controller_profile_valid_row_count),
    "invalid_row_count": (builder.controller_profile_invalid_count),
    "profile_id": (baseline_profile.get("profile_id") if baseline_profile is not None else None),
    "baseline_controller_params_sha256": (
      baseline_profile.get(
        "baseline_controller_params_sha256",
      )
      if baseline_profile is not None
      else None
    ),
    "source_starpilot_commit": source_starpilot_commit,
  }
  causal_input_eligible = bool(
    route.log_type == "rlog"
    and row_count > 0
    and source_age_validation["state"] == "verified"
    and controller_selection_validation["state"] == "verified"
    and effective_validation["state"] == "verified"
    and controller_profile_validation["state"] == "verified"
    and len(car_params_hashes) == 1
    and len(controller_hashes) == 1
    and len(source_commits) == 1
    and not ambiguous_car_params_segments
    and not ambiguous_software_segments
  )

  runtime_context, flm_resolution = _flm_resolution(
    route_software,
    car_params,
  )
  params = route_software.get("controller_params", {}) if route_software is not None else {}
  lateral_tune = e._param_bool(params, "LateralTune")
  nnff = e._param_bool(params, "NNFF")
  nnff_lite = e._param_bool(params, "NNFFLite")
  nnff_model_name = e._param_text(params, "NNFFModelName")
  baseline_limitations = [
    "controller_internal_state_is_not_fully_logged",
  ]
  if not runtime_context["flm_active_available"]:
    baseline_limitations.append("flm_runtime_state_unavailable")
  if not runtime_context["trailer_load_available"]:
    baseline_limitations.append("trailer_load_runtime_state_unavailable")

  manifest = {
    "record": "manifest",
    "schema": "comma-companion.telemetry-manifest",
    "schema_version": e.CONTRACT_VERSION,
    "route_id": route.route_id,
    "source_route": route.route_id,
    "state": route_state,
    "publication_ready": complete_route,
    "timeline_version": timeline_version,
    "timebase": {
      "unit": "us",
      "origin_log_mono_time_ns": str(origin_ns),
      "origin_id": origin_id,
      "origin_stability": ("segment_zero_stable" if route_start_observed else "provisional_supplied_subset"),
      "conversion": "floor((logMonoTime-origin)/1000)",
      "utc_start_us": utc_start_us,
      "utc_anchors": builder.utc_anchors,
      "utc_anchor_limit": e.MAX_RETAINED_UTC_ANCHORS,
      "utc_anchor_dropped_count": (builder.utc_anchor_dropped_count),
      "utc_solution": utc_solution,
      "stable_sample_identity": [
        "route_id",
        "source_segment_num",
        "source_ordinal",
        "log_mono_time_ns",
      ],
      "dependent_index_policy": ("publish atomically under timeline_version"),
    },
    "range": {
      "start_us": builder.first_time_us,
      "end_us": builder.last_time_us,
    },
    "route_summary": builder.route_summary(),
    "tiers": [{"id": "full", "width_us": None}] + [{"id": e._tier_name(width), "width_us": width} for width in e.TIER_WIDTHS_US],
    "signals": signals,
    "vehicle": car_params,
    "route_software": route_software,
    "frame_counts": dict(sorted(builder.frame_counts.items())),
    "frame_quality": {
      "issues": dict(
        sorted(builder.frame_quality_counts.items()),
      ),
    },
    "event_merge": {
      "ordering": ("logMonoTime_segment_num_source_ordinal"),
      "deduplication": "exact_wire_duplicate_across_segments",
      "deduplicated_event_count": deduplicated_event_count,
      "maximum_observed_overlap_ns": str(
        maximum_observed_overlap_ns,
      ),
      "bounded_overlap_window_ns": str(
        e.EVENT_MERGE_MAX_OVERLAP_NS,
      ),
    },
    "resource_usage": {
      "source_bytes": total_source_bytes,
      "source_bytes_limit": e.MAX_SOURCE_BYTES_PER_ROUTE,
      "decompressed_bytes": total_decompressed_bytes,
      "decompressed_bytes_limit": (e.MAX_DECOMPRESSED_BYTES_PER_ROUTE),
      "segment_count": len(route.segments),
      "segment_count_limit": e.MAX_ROUTE_SEGMENTS,
    },
    "completeness": {
      "supplied_segment_count": len(route.segments),
      "supplied_segment_range": [
        min(segment.number for segment in route.segments),
        max(segment.number for segment in route.segments),
      ],
      "parsed_segment_numbers": parsed_numbers,
      "missing_segment_numbers": missing_segments,
      "failed_segment_numbers": failed_segments,
      "contiguous_from_segment_zero": contiguous_from_zero,
      "route_start_observed": route_start_observed,
      "route_end_observed": route_end_observed,
      "boundary_chain_valid": boundary_chain_valid,
      "segments": parsed_reports,
    },
    "dynamics": {
      "schema": e.DYNAMICS_SCHEMA,
      "schema_version": e.DYNAMICS_SCHEMA_VERSION,
      "state": ("available" if route.log_type == "rlog" else "unavailable_qlog_decimated"),
      "alignment": "timestamp_causal_recorded_history_asof",
      "sample_period_us": e.DYNAMICS_SAMPLE_PERIOD_US,
      "controller_i_timing": "post_update_asof_source_row",
      "signed_steering_rate": ("causal_grid_difference_of_zoh_steering_angle"),
      "applied_torque_source": ("carOutput.actuatorsOutput.torque_only_no_fallback"),
      "row_count": row_count,
      "nonzero_logged_jerk_row_count": (builder.dynamics_nonzero_jerk_count),
      "feedforward_eligible_row_count": (builder.dynamics_feedforward_eligible_count),
      "drop_counts": dict(
        sorted(builder.dynamics.drop_reasons.items()),
      ),
      "quality_counts": dict(
        sorted(builder.dynamics.quality_counts.items()),
      ),
      "causal_input_eligible": causal_input_eligible,
      "source_age_validation": source_age_validation,
      "controller_selection_validation": (controller_selection_validation),
      "effective_torque_context_validation": (effective_validation),
      "controller_profile_validation": (controller_profile_validation),
      "telemetry_provenance": {
        "schema": e.DYNAMICS_SCHEMA,
        "schema_version": e.DYNAMICS_SCHEMA_VERSION,
        "extractor_version": e.EXTRACTOR_VERSION,
        "alignment": "timestamp_causal_recorded_history_asof",
        "source_selection": ("independent_per_source_max_valid_source_with_" + "logMonoTime_at_or_before_tick"),
        "event_order": [
          "logMonoTime",
          "segment_num",
          "source_ordinal",
        ],
        "invalid_event_policy": {
          "carState": "drop_without_invalidating_prior_valid_state",
          "carControl": "invalidate_until_next_valid",
          "controlsState": "invalidate_until_next_valid",
          "carOutput": "invalidate_until_next_valid",
        },
        "grid": "absolute_logMonoTime_10ms",
        "signed_steering_rate": ("causal_grid_difference_of_zoh_steering_angle"),
        "applied_torque_source": ("carOutput.actuatorsOutput.torque_only_no_fallback"),
        "max_asof_age_ms": 35,
        "route_origin_log_mono_time_ns": str(origin_ns),
        "causal_input_eligible": causal_input_eligible,
        "extractor_source_sha256": extractor_hash,
      },
      "controller_provenance": {
        "lateral_tuning_type": (car_params.get("lateral_tuning_type") if car_params is not None else None),
        "steer_control_type": (car_params.get("steer_control_type") if car_params is not None else None),
        "car_params_wire_sha256": (car_params_wire_sha256),
        "controller_params_sha256": (controller_params_sha256),
        "lateral_tune": lateral_tune,
        "lateral_tune_available": lateral_tune is not None,
        "nnff_capable": (
          bool(lateral_tune) and (bool(nnff) or bool(nnff_lite)) if lateral_tune is not None and nnff is not None and nnff_lite is not None else None
        ),
        "nnff_capable_available": (lateral_tune is not None and nnff is not None and nnff_lite is not None),
        "nnff_model_name": nnff_model_name,
        "nnff_model_name_available": (nnff_model_name is not None),
        **runtime_context,
        "flm_resolution": flm_resolution,
        "resolved_toggle_snapshots": (selected_resolved_toggle_snapshots),
        "effective_resolved_toggle_snapshots": (effective_resolved_toggle_snapshots),
        "baseline_controller_profile": baseline_profile,
        "tuning_provenance": {
          "controller_params_sha256": (controller_params_sha256),
          "source_starpilot_commit": (source_starpilot_commit),
          "baseline_controller_profile": baseline_profile,
          "controller_profile_validation": (controller_profile_validation),
          "controller_selection_validation": (controller_selection_validation),
          "effective_torque_context_validation": (effective_validation),
        },
        "init_data_fallback_evaluator": {
          "state": (
            "available"
            if e.KNOWN_TORQUE_CONTEXT_EVALUATORS.get(
              str(source_starpilot_commit),
            )
            is not None
            else "unavailable"
          ),
          "name": e.TORQUE_CONTEXT_EVALUATOR_NAME,
          "version": e.TORQUE_CONTEXT_EVALUATOR_VERSION,
          "evaluator_id": (
            e.KNOWN_TORQUE_CONTEXT_EVALUATORS.get(
              str(source_starpilot_commit),
            )
          ),
          "source_commit": source_starpilot_commit,
          "source_sha256": (e.TORQUE_CONTEXT_EVALUATOR_SOURCE_SHA256),
        },
        "baseline_exact_claim_allowed": False,
        "limitations": baseline_limitations,
      },
    },
    "capabilities": {
      "historical_custom_events": {
        "state": ("partial_schema_mismatch" if builder.custom_schema_drops else "available"),
        "dropped_messages": [
          {
            "segment_num": segment_num,
            "service": service,
            "count": count,
          }
          for (segment_num, service), count in sorted(builder.custom_schema_drops.items())
        ],
      },
      "segment_encode_id": {
        "state": "unsupported_not_populated_by_loggerd",
      },
      "exact_controller_baseline": {
        "state": ("available" if controller_profile_validation["state"] == "verified" else "unavailable"),
        "profile_id": (controller_profile_validation["profile_id"]),
        "baseline_controller_params_sha256": (controller_profile_validation["baseline_controller_params_sha256"]),
        "limitations": baseline_limitations,
      },
    },
    "provenance": {
      "extractor": "comma-companion-rlog",
      "extractor_version": e.EXTRACTOR_VERSION,
      "extractor_source_sha256": extractor_hash,
      "extractor_dirty": extractor_dirty,
      "build_id": e.os.getenv("COMMA_COMPANION_BUILD_ID"),
      "starpilot_commit": e._starpilot_commit(),
      "source_starpilot_commit": source_starpilot_commit,
      "cereal_schema_sha256": e._schema_sha256(),
      "car_params_wire_sha256": car_params_wire_sha256,
      "car_params_summary_sha256": (car_params_summary_sha256),
      "controller_params_sha256": (controller_params_sha256),
      "car_params_wire_snapshots": all_car_params_snapshots,
      "controller_params_snapshots": (all_route_software_snapshots),
      "resolved_toggle_snapshots": (selected_resolved_toggle_snapshots),
      "effective_resolved_toggle_snapshots": (effective_resolved_toggle_snapshots),
      "baseline_controller_profile": baseline_profile,
      "source_objects": source_objects,
      "baseline_exact_claim_allowed": False,
      "baseline_limitations": baseline_limitations,
    },
    "warnings": warnings,
  }
  yield manifest
  yield {
    "record": "stream_end",
    "status": route_state,
    "counts": {
      "signals": len(signals),
      "frames": sum(builder.frame_counts.values()),
      "markers": builder.marker_count,
      "dynamics_rows": row_count,
      "segments": len(route.segments),
    },
  }
