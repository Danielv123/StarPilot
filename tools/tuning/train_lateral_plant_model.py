#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Iterator
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

try:
  from joblib import dump
  from sklearn.ensemble import HistGradientBoostingRegressor
  from sklearn.metrics import mean_absolute_error, root_mean_squared_error
  from sklearn.multioutput import MultiOutputRegressor
except ModuleNotFoundError as e:
  raise SystemExit(
    "Run with: uv run --no-project --with scikit-learn --with joblib --with pycapnp==2.1.0 --with zstandard python tools/tuning/train_lateral_plant_model.py"
  ) from e

from openpilot.tools.tuning import train_vehicle_response_model as log_data


DEFAULT_LOG_ROOT = Path(r"D:\comma_driving_logs\10.30.1.75\realdata")
DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts/tuning/lateral_plant_20260721"
DERIVED_JERK_WINDOW_S = 0.20
DERIVED_JERK_LIMIT = 2.5
MIN_TRAIN_SPEED_MPS = 0.5
MAX_ROUTE_STATE_CARRY_GAP_S = 1.0
MAX_ASOF_AGE_NS = 35_000_000
TRAJECTORY_EXTRACTION_VERSION = 9

RELEVANT_SERVICES = (
  "carParams",
  "carState",
  "carControl",
  "carOutput",
  "controlsState",
)

BASE_FEATURES = (
  "applied_torque",
  "actual_lateral_accel",
  "steering_angle_deg",
  "steering_rate_deg",
  "signed_steering_rate_deg_s",
  "steering_torque_eps",
  "v_ego",
  "a_ego",
)
STATE_FEATURES = (
  "actual_lateral_accel",
  "steering_angle_deg",
  "signed_steering_rate_deg_s",
  "steering_torque_eps",
)
DIAGNOSTIC_FIELDS = (
  "requested_torque",
  "car_control_age_ms",
  "controls_state_age_ms",
  "car_output_age_ms",
  "source_age_ms",
  "source_time_error_ms",
  "desired_curvature",
  "desired_lateral_accel",
  "desired_lateral_jerk",
  "derived_lateral_jerk",
  "controller_output",
  "controller_i",
  "lat_active",
  "driver_overlay",
  "saturated",
  "sample_valid",
)


@dataclass
class Trajectory:
  segment: str
  route: str
  brand: str
  car_fingerprint: str
  times: np.ndarray
  values: dict[str, np.ndarray]
  lateral_active_rows: int
  driver_overlay_rows: int


class CorruptRouteError(RuntimeError):
  """A deterministic input-decoding failure that rejects the whole route."""


@dataclass(frozen=True)
class RelevantLogEvent:
  log_mono_time_ns: int
  source_ordinal: int
  service: str
  valid: bool
  values: dict[str, Any]


@dataclass(frozen=True)
class SegmentRelevantEvents:
  events: list[RelevantLogEvent]
  next_source_ordinal: int
  start_sentinel: str | None
  end_sentinel: str | None


@dataclass
class RouteExtractionState:
  brand: str
  car_fingerprint: str
  accepted: bool
  latest: dict[str, dict[str, Any]] = field(default_factory=dict)
  latest_event_time_ns: dict[str, int] = field(default_factory=dict)
  source_ordinal: int = 0
  sampled_car_state_rows: int = 0
  last_event_key: tuple[int, int] | None = None
  last_sample_time_s: float | None = None
  last_steering_angle_deg: float | None = None
  jerk_history_times: list[float] = field(default_factory=list)
  jerk_history_accels: list[float] = field(default_factory=list)

  def reset_dynamic_state(self) -> None:
    self.latest.clear()
    self.latest_event_time_ns.clear()
    self.sampled_car_state_rows = 0
    self.last_event_key = None
    self.last_sample_time_s = None
    self.last_steering_angle_deg = None
    self.jerk_history_times.clear()
    self.jerk_history_accels.clear()


def nested(obj: Any, *names: str) -> Any | None:
  cur = obj
  for name in names:
    if cur is None:
      return None
    try:
      cur = getattr(cur, name)
    except AttributeError:
      return None
    except Exception as exc:
      raise CorruptRouteError(
        f"Unreadable field {'.'.join(names)}: {type(exc).__name__}.",
      ) from None
  return cur


def finite(value: Any, default: float = math.nan) -> float:
  try:
    value = float(value)
    return value if math.isfinite(value) else default
  except Exception:
    return default


def route_name(segment: str) -> str:
  return segment.rsplit("--", 1)[0] if "--" in segment else segment


def signed_steering_rate(angles: np.ndarray, times: np.ndarray, max_gap_s: float = 0.09) -> np.ndarray:
  """Reconstruct causal signed wheel motion; carState.steeringRateDeg is magnitude-only on this car."""
  rate = np.zeros_like(angles, dtype=np.float32)
  if len(rate) < 2:
    return rate
  dt = np.diff(times)
  continuous = (dt > 1e-4) & (dt < max_gap_s)
  rate[1:][continuous] = (np.diff(angles)[continuous] / dt[continuous]).astype(np.float32)
  return rate


def derived_lateral_jerk(
  accels: np.ndarray,
  times: np.ndarray,
  window_s: float = DERIVED_JERK_WINDOW_S,
  prior_accels: np.ndarray | None = None,
  prior_times: np.ndarray | None = None,
) -> np.ndarray:
  """Estimate planned lateral jerk using only the current and earlier samples."""
  accels = np.asarray(accels, dtype=np.float32)
  times = np.asarray(times, dtype=np.float64)
  jerk = np.zeros_like(accels, dtype=np.float32)
  if len(jerk) == 0:
    return jerk
  if prior_accels is None or prior_times is None:
    prior_accels = np.empty(0, dtype=np.float32)
    prior_times = np.empty(0, dtype=np.float64)
  else:
    prior_accels = np.asarray(prior_accels, dtype=np.float32)
    prior_times = np.asarray(prior_times, dtype=np.float64)
  if len(prior_accels) != len(prior_times):
    raise ValueError("Prior acceleration values and timestamps must have equal lengths.")
  combined_accels = np.concatenate((prior_accels, accels))
  combined_times = np.concatenate((prior_times, times))
  current = len(prior_times) + np.arange(len(times))
  left = np.searchsorted(combined_times, times - window_s, side="left")
  elapsed = combined_times[current] - combined_times[left]
  valid = (current > left) & (elapsed > 1e-4)
  jerk[valid] = (
    (combined_accels[current[valid]] - combined_accels[left[valid]]) /
    elapsed[valid]
  ).astype(np.float32)
  return np.clip(jerk, -DERIVED_JERK_LIMIT, DERIVED_JERK_LIMIT)


def segment_number(path: Path) -> int | None:
  try:
    return int(path.parent.name.rsplit("--", 1)[1])
  except (IndexError, ValueError):
    return None


def segment_path_sort_key(path: Path) -> tuple[int, int | str, str]:
  number = segment_number(path)
  return (
    0 if number is not None else 1,
    number if number is not None else path.parent.name,
    str(path),
  )


def group_route_log_paths(paths: list[Path]) -> list[list[Path]]:
  grouped: dict[str, list[Path]] = {}
  for path in paths:
    grouped.setdefault(route_name(path.parent.name), []).append(path)
  return [
    sorted(grouped[route], key=segment_path_sort_key)
    for route in sorted(grouped)
  ]


def snapshot_relevant_message(msg: Any, service: str) -> dict[str, Any]:
  if service == "carParams":
    return {
      "brand": str(nested(msg.carParams, "brand") or ""),
      "car_fingerprint": str(nested(msg.carParams, "carFingerprint") or ""),
    }
  if service == "carState":
    steering_pressed = nested(msg.carState, "steeringPressed")
    if steering_pressed is None:
      raise CorruptRouteError("Missing required carState.steeringPressed.")
    return {
      "steering_angle_deg": finite(nested(msg.carState, "steeringAngleDeg")),
      "steering_rate_deg": finite(nested(msg.carState, "steeringRateDeg")),
      "steering_torque_eps": finite(nested(msg.carState, "steeringTorqueEps")),
      "v_ego": finite(nested(msg.carState, "vEgo")),
      "a_ego": finite(nested(msg.carState, "aEgo")),
      "steering_pressed": bool(steering_pressed),
      "steering_pressed_known": steering_pressed is not None,
    }
  if service == "carControl":
    lat_active = nested(msg.carControl, "latActive")
    if lat_active is None:
      raise CorruptRouteError("Missing required carControl.latActive.")
    return {
      "lat_active": bool(lat_active),
      "lat_active_known": lat_active is not None,
      "requested_torque": finite(nested(msg.carControl, "actuators", "torque")),
      "desired_curvature": finite(nested(msg.carControl, "actuators", "curvature")),
    }
  if service == "carOutput":
    return {
      "applied_torque": finite(nested(msg.carOutput, "actuatorsOutput", "torque")),
    }
  controls_state = msg.controlsState
  torque_state = None
  lateral_state = nested(controls_state, "lateralControlState")
  if lateral_state is not None:
    try:
      if lateral_state.which() == "torqueState":
        torque_state = lateral_state.torqueState
    except Exception as exc:
      raise CorruptRouteError(
        "Unreadable controlsState.lateralControlState union: " +
        f"{type(exc).__name__}.",
      ) from None
  saturated = nested(torque_state, "saturated")
  if torque_state is not None and saturated is None:
    raise CorruptRouteError(
      "Missing required controlsState torqueState.saturated.",
    )
  return {
    "has_torque_state": torque_state is not None,
    "actual_lateral_accel": finite(nested(torque_state, "actualLateralAccel")),
    "desired_lateral_accel": finite(nested(torque_state, "desiredLateralAccel")),
    "desired_lateral_jerk": finite(nested(torque_state, "desiredLateralJerk")),
    "controller_output": finite(nested(torque_state, "output")),
    "controller_i": finite(nested(torque_state, "i")),
    "saturated": bool(saturated),
    "saturated_known": saturated is not None,
  }


def strict_stream_log_messages(path: Path) -> Iterator[Any]:
  """Yield every event, propagating decompression and Cap'n Proto failures."""
  try:
    data = log_data.decompress_log_bytes(path)
    yield from log_data.capnp_log.Event.read_multiple_bytes(data)
  except Exception as exc:
    raise CorruptRouteError(
      f"Unreadable or corrupt log {path}: {type(exc).__name__}.",
    ) from None


def ordered_relevant_events(
  path: Path,
  source_ordinal_start: int = 0,
) -> SegmentRelevantEvents:
  events: list[RelevantLogEvent] = []
  next_ordinal = source_ordinal_start
  start_sentinel: str | None = None
  end_sentinel: str | None = None
  for msg in strict_stream_log_messages(path):
    source_ordinal = next_ordinal
    next_ordinal += 1
    try:
      service = msg.which()
      log_mono_time_ns = int(msg.logMonoTime)
      valid = bool(getattr(msg, "valid", True))
    except Exception as exc:
      raise CorruptRouteError(
        f"Unreadable event at serialized ordinal {source_ordinal} in " +
        f"{path}: {type(exc).__name__}.",
      ) from None
    if service == "sentinel":
      sentinel_type = (
        str(nested(msg.sentinel, "type") or "")
        if valid
        else ""
      )
      if sentinel_type.startswith("start") and start_sentinel is None:
        start_sentinel = sentinel_type
      elif sentinel_type.startswith("end"):
        end_sentinel = sentinel_type
      continue
    if service not in RELEVANT_SERVICES or log_mono_time_ns <= 0:
      continue
    values = snapshot_relevant_message(msg, service) if valid else {}
    events.append(RelevantLogEvent(
      log_mono_time_ns=log_mono_time_ns,
      source_ordinal=source_ordinal,
      service=service,
      valid=valid,
      values=values,
    ))
  events.sort(key=lambda event: (event.log_mono_time_ns, event.source_ordinal))
  return SegmentRelevantEvents(
    events=events,
    next_source_ordinal=next_ordinal,
    start_sentinel=start_sentinel,
    end_sentinel=end_sentinel,
  )


def resolve_route_identity(
  paths: list[Path],
  brand_filter: str,
  fingerprint_filter: str,
) -> tuple[str, str] | None:
  for path in paths:
    for source_ordinal, msg in enumerate(strict_stream_log_messages(path)):
      try:
        if msg.which() != "carParams" or not bool(getattr(msg, "valid", True)):
          continue
        brand = str(nested(msg.carParams, "brand") or "")
        fingerprint = str(nested(msg.carParams, "carFingerprint") or "")
      except Exception as exc:
        raise CorruptRouteError(
          "Unreadable identity event at serialized ordinal " +
          f"{source_ordinal} in {path}: {type(exc).__name__}.",
        ) from None
      if not log_data.identity_matches(
        brand, fingerprint, brand_filter, fingerprint_filter,
      ):
        return None
      return brand, fingerprint
  return None


def update_jerk_history(
  state: RouteExtractionState,
  times: np.ndarray,
  accels: np.ndarray,
) -> None:
  if len(times) == 0:
    return
  combined_times = np.concatenate((
    np.asarray(state.jerk_history_times, dtype=np.float64),
    np.asarray(times, dtype=np.float64),
  ))
  combined_accels = np.concatenate((
    np.asarray(state.jerk_history_accels, dtype=np.float32),
    np.asarray(accels, dtype=np.float32),
  ))
  keep = combined_times >= combined_times[-1] - DERIVED_JERK_WINDOW_S
  state.jerk_history_times = combined_times[keep].tolist()
  state.jerk_history_accels = combined_accels[keep].tolist()


def read_trajectory_events(
  segment: str,
  route: str,
  events: list[RelevantLogEvent],
  sample_step: int,
  state: RouteExtractionState,
  carry_dynamic_state: bool,
) -> tuple[Trajectory | None, bool]:
  if not events:
    state.reset_dynamic_state()
    return None, False
  if not carry_dynamic_state:
    state.reset_dynamic_state()
  elif (
    state.last_event_key is not None
    and (
      events[0].log_mono_time_ns - state.last_event_key[0]
    ) / 1e9 > MAX_ROUTE_STATE_CARRY_GAP_S
  ):
    state.reset_dynamic_state()

  previous_key = state.last_event_key
  for event in events:
    event_key = (event.log_mono_time_ns, event.source_ordinal)
    if previous_key is not None and event_key < previous_key:
      raise AssertionError("Relevant events were not processed in causal order.")
    previous_key = event_key
    if event.service == "carParams" and event.valid:
      if (
        event.values["brand"] != state.brand
        or event.values["car_fingerprint"] != state.car_fingerprint
      ):
        state.accepted = False
        return None, True

  valid_car_state_times = [
    event.log_mono_time_ns
    for event in events
    if event.service == "carState" and event.valid
  ]
  if not valid_car_state_times:
    state.reset_dynamic_state()
    return None, False
  period_ns = sample_step * 10_000_000
  first_grid_ns = (
    (min(valid_car_state_times) + period_ns - 1) // period_ns
  ) * period_ns
  last_grid_ns = (
    max(valid_car_state_times) // period_ns
  ) * period_ns
  if last_grid_ns < first_grid_ns:
    state.reset_dynamic_state()
    return None, False

  latest = dict(state.latest)
  latest_time_ns = dict(state.latest_event_time_ns)
  times: list[float] = []
  rows: dict[str, list[float]] = {
    name: [] for name in (*BASE_FEATURES, *DIAGNOSTIC_FIELDS)
  }
  lateral_active_rows = 0
  driver_overlay_rows = 0
  event_index = 0
  previous_angle: float | None = None
  previous_car_state_current = False

  def source_age_ns(service: str, grid_time_ns: int) -> int | None:
    source_time_ns = latest_time_ns.get(service)
    return (
      grid_time_ns - source_time_ns
      if source_time_ns is not None
      else None
    )

  for grid_ns in range(first_grid_ns, last_grid_ns + period_ns, period_ns):
    while (
      event_index < len(events)
      and events[event_index].log_mono_time_ns <= grid_ns
    ):
      event = events[event_index]
      event_index += 1
      service = event.service
      if service == "carParams":
        continue
      if not event.valid:
        if service != "carState":
          latest.pop(service, None)
          latest_time_ns.pop(service, None)
        continue
      latest[service] = event.values
      latest_time_ns[service] = event.log_mono_time_ns

    car_state = latest.get("carState")
    car_control = latest.get("carControl")
    controls_state = latest.get("controlsState")
    car_output = latest.get("carOutput")
    car_state_age_ns = source_age_ns("carState", grid_ns)
    car_control_age_ns = source_age_ns("carControl", grid_ns)
    controls_state_age_ns = source_age_ns("controlsState", grid_ns)
    car_output_age_ns = source_age_ns("carOutput", grid_ns)
    source_ages = (
      car_state_age_ns,
      car_control_age_ns,
      controls_state_age_ns,
      car_output_age_ns,
    )
    sources_current = all(
      age_ns is not None and 0 <= age_ns <= MAX_ASOF_AGE_NS
      for age_ns in source_ages
    )
    has_torque_state = bool(
      controls_state is not None
      and controls_state["has_torque_state"]
    )
    eligibility_booleans_known = bool(
      car_state is not None
      and car_state["steering_pressed_known"]
      and car_control is not None
      and car_control["lat_active_known"]
      and controls_state is not None
      and controls_state["saturated_known"]
    )
    lat_active = bool(car_control and car_control["lat_active"])
    steering_pressed = bool(
      car_state and car_state["steering_pressed"]
    )
    driver_overlay = lat_active and steering_pressed

    steering_angle_deg = (
      car_state["steering_angle_deg"]
      if car_state is not None
      else math.nan
    )
    car_state_current = bool(
      car_state_age_ns is not None
      and 0 <= car_state_age_ns <= MAX_ASOF_AGE_NS
    )
    signed_rate = 0.0
    if (
      car_state_current
      and previous_car_state_current
      and previous_angle is not None
      and math.isfinite(steering_angle_deg)
    ):
      signed_rate = (
        steering_angle_deg - previous_angle
      ) / (period_ns / 1e9)
    previous_angle = steering_angle_deg
    previous_car_state_current = car_state_current

    def snapshot_value(
      snapshot: dict[str, Any] | None,
      name: str,
      default: float = math.nan,
    ) -> float:
      return (
        finite(snapshot.get(name), default)
        if snapshot is not None
        else default
      )

    desired_lateral_accel = snapshot_value(
      controls_state, "desired_lateral_accel",
    )
    v_ego = snapshot_value(car_state, "v_ego")
    desired_curvature = snapshot_value(
      car_control, "desired_curvature",
    )
    if (
      not math.isfinite(desired_curvature)
      and math.isfinite(desired_lateral_accel)
      and math.isfinite(v_ego)
    ):
      desired_curvature = desired_lateral_accel / max(
        v_ego ** 2, MIN_TRAIN_SPEED_MPS ** 2,
      )
    row = {
      "applied_torque": snapshot_value(car_output, "applied_torque"),
      "actual_lateral_accel": snapshot_value(
        controls_state, "actual_lateral_accel",
      ),
      "steering_angle_deg": steering_angle_deg,
      "steering_rate_deg": snapshot_value(
        car_state, "steering_rate_deg",
      ),
      "signed_steering_rate_deg_s": signed_rate,
      "steering_torque_eps": snapshot_value(
        car_state, "steering_torque_eps",
      ),
      "v_ego": v_ego,
      "a_ego": snapshot_value(car_state, "a_ego"),
      "requested_torque": snapshot_value(
        car_control, "requested_torque",
      ),
      "car_control_age_ms": (
        car_control_age_ns / 1e6
        if car_control_age_ns is not None
        else math.nan
      ),
      "controls_state_age_ms": (
        controls_state_age_ns / 1e6
        if controls_state_age_ns is not None
        else math.nan
      ),
      "car_output_age_ms": (
        car_output_age_ns / 1e6
        if car_output_age_ns is not None
        else math.nan
      ),
      "source_age_ms": (
        car_state_age_ns / 1e6
        if car_state_age_ns is not None
        else math.nan
      ),
      "source_time_error_ms": (
        -car_state_age_ns / 1e6
        if car_state_age_ns is not None
        else math.nan
      ),
      "desired_curvature": desired_curvature,
      "desired_lateral_accel": desired_lateral_accel,
      "desired_lateral_jerk": snapshot_value(
        controls_state, "desired_lateral_jerk",
      ),
      "derived_lateral_jerk": 0.0,
      "controller_output": snapshot_value(
        controls_state, "controller_output",
      ),
      "controller_i": snapshot_value(controls_state, "controller_i"),
      "lat_active": float(lat_active),
      "driver_overlay": float(driver_overlay),
      "saturated": float(
        bool(controls_state and controls_state["saturated"]),
      ),
      "sample_valid": 0.0,
    }
    required_finite = (
      *BASE_FEATURES,
      "desired_curvature",
      "desired_lateral_accel",
      "desired_lateral_jerk",
      "controller_output",
      "controller_i",
    )
    row["sample_valid"] = float(
      sources_current
      and has_torque_state
      and eligibility_booleans_known
      and all(math.isfinite(row[name]) for name in required_finite)
    )
    lateral_active_rows += int(
      lat_active and row["sample_valid"] > 0.5
    )
    driver_overlay_rows += int(
      driver_overlay and row["sample_valid"] > 0.5
    )
    times.append(grid_ns / 1e9)
    for name, value in row.items():
      rows[name].append(value)

  sampled_times = np.asarray(times, dtype=np.float64)
  values = {name: np.asarray(value, dtype=np.float32) for name, value in rows.items()}
  if len(sampled_times):
    values["derived_lateral_jerk"] = derived_lateral_jerk(
      values["desired_lateral_accel"],
      sampled_times,
      prior_accels=np.asarray(state.jerk_history_accels, dtype=np.float32),
      prior_times=np.asarray(state.jerk_history_times, dtype=np.float64),
    )
    update_jerk_history(
      state, sampled_times, values["desired_lateral_accel"],
    )
  state.latest = latest
  state.latest_event_time_ns = latest_time_ns
  state.last_event_key = (
    events[-1].log_mono_time_ns,
    events[-1].source_ordinal,
  )
  state.last_sample_time_s = (
    float(sampled_times[-1]) if len(sampled_times) else None
  )
  state.last_steering_angle_deg = previous_angle
  if len(sampled_times) < 20:
    return None, True
  return Trajectory(
    segment=segment,
    route=route,
    brand=state.brand,
    car_fingerprint=state.car_fingerprint,
    times=sampled_times,
    values=values,
    lateral_active_rows=lateral_active_rows,
    driver_overlay_rows=driver_overlay_rows,
  ), True


def read_trajectory_segment(
  path: Path,
  sample_step: int,
  state: RouteExtractionState,
  carry_dynamic_state: bool,
) -> tuple[Trajectory | None, bool]:
  segment_events = ordered_relevant_events(
    path, state.source_ordinal,
  )
  state.source_ordinal = segment_events.next_source_ordinal
  return read_trajectory_events(
    path.parent.name,
    route_name(path.parent.name),
    segment_events.events,
    sample_step,
    state,
    carry_dynamic_state,
  )


def read_route_trajectories(
  paths: list[Path],
  brand_filter: str,
  fingerprint_filter: str,
  sample_step: int,
) -> list[Trajectory]:
  if sample_step < 1:
    raise ValueError("sample_step must be positive.")
  ordered_paths = sorted(paths, key=segment_path_sort_key)
  routes = {route_name(path.parent.name) for path in ordered_paths}
  if len(routes) > 1:
    raise ValueError("All trajectory paths must belong to the same route.")
  identity = resolve_route_identity(
    ordered_paths, brand_filter, fingerprint_filter,
  )
  if identity is None:
    return []
  state = RouteExtractionState(
    brand=identity[0],
    car_fingerprint=identity[1],
    accepted=True,
  )
  trajectories: list[Trajectory] = []
  route = next(iter(routes))
  segment_events: list[tuple[Path, SegmentRelevantEvents]] = []
  for path in ordered_paths:
    batch = ordered_relevant_events(path, state.source_ordinal)
    state.source_ordinal = batch.next_source_ordinal
    segment_events.append((path, batch))

  contiguous_groups: list[list[tuple[Path, SegmentRelevantEvents]]] = []
  for path, batch in segment_events:
    previous_number = (
      segment_number(contiguous_groups[-1][-1][0])
      if contiguous_groups
      else None
    )
    previous_batch = contiguous_groups[-1][-1][1] if contiguous_groups else None
    number = segment_number(path)
    within_carry_gap = bool(
      previous_batch is not None
      and previous_batch.events
      and batch.events
      and (
        batch.events[0].log_mono_time_ns
        - previous_batch.events[-1].log_mono_time_ns
      ) / 1e9 <= MAX_ROUTE_STATE_CARRY_GAP_S
    )
    if (
      not contiguous_groups
      or number is None
      or previous_number is None
      or number != previous_number + 1
      or previous_batch is None
      or previous_batch.end_sentinel != "endOfSegment"
      or batch.start_sentinel != "startOfSegment"
      or not within_carry_gap
    ):
      contiguous_groups.append([(path, batch)])
    else:
      contiguous_groups[-1].append((path, batch))

  for group in contiguous_groups:
    grouped_paths = [path for path, _ in group]
    events: list[RelevantLogEvent] = []
    for _, batch in group:
      events.extend(batch.events)
    events.sort(
      key=lambda event: (event.log_mono_time_ns, event.source_ordinal),
    )
    first_number = segment_number(grouped_paths[0])
    last_number = segment_number(grouped_paths[-1])
    segment = (
      grouped_paths[0].parent.name
      if len(grouped_paths) == 1
      else f"{route}--{first_number}-{last_number}"
    )
    trajectory, _ = read_trajectory_events(
      segment, route, events, sample_step, state, carry_dynamic_state=False,
    )
    if not state.accepted:
      return []
    if trajectory is not None:
      trajectories.append(trajectory)
  return trajectories


def read_trajectory(
  path: Path,
  brand_filter: str,
  fingerprint_filter: str,
  sample_step: int,
) -> Trajectory | None:
  trajectories = read_route_trajectories(
    [path], brand_filter, fingerprint_filter, sample_step,
  )
  return trajectories[0] if trajectories else None


def expanded_feature_names(history_steps: int) -> list[str]:
  return [f"{name}_t_minus_{lag}" for lag in range(history_steps) for name in BASE_FEATURES]


def trajectory_samples(trajectory: Trajectory, history_steps: int) -> tuple[np.ndarray, np.ndarray]:
  values = trajectory.values
  base = np.column_stack([values[name] for name in BASE_FEATURES])
  state = np.column_stack([values[name] for name in STATE_FEATURES])
  source = np.arange(history_steps - 1, len(trajectory.times) - 1)
  if len(source) == 0:
    return (
      np.empty((0, history_steps * len(BASE_FEATURES)), dtype=np.float32),
      np.empty((0, len(STATE_FEATURES)), dtype=np.float32),
    )
  sample_valid = values.get(
    "sample_valid", np.ones(len(trajectory.times), dtype=np.float32),
  )
  row_clean = (
    (values["lat_active"] > 0.5)
    & (values["driver_overlay"] < 0.5)
    & (values["saturated"] < 0.5)
    & (sample_valid > 0.5)
    & np.isfinite(base).all(axis=1)
    & np.isfinite(state).all(axis=1)
  )
  bad_row_prefix = np.concatenate(([0], np.cumsum(~row_clean)))
  gaps = np.diff(trajectory.times)
  bad_gap = (gaps <= 1e-4) | (gaps >= 0.09)
  bad_gap_prefix = np.concatenate(([0], np.cumsum(bad_gap)))
  starts = source - history_steps + 1
  ends = source + 1
  clean = (
    (values["v_ego"][source] >= MIN_TRAIN_SPEED_MPS)
    & ((bad_row_prefix[ends + 1] - bad_row_prefix[starts]) == 0)
    & ((bad_gap_prefix[ends] - bad_gap_prefix[starts]) == 0)
  )
  source = source[clean]
  if len(source) == 0:
    return np.empty((0, history_steps * len(BASE_FEATURES)), dtype=np.float32), np.empty((0, len(STATE_FEATURES)), dtype=np.float32)
  history = np.stack([base[source - lag] for lag in range(history_steps)], axis=1)
  target_delta = state[source + 1] - state[source]
  return history.reshape((-1, history_steps * len(BASE_FEATURES))), target_delta


def route_intervention_stats(trajectories: list[Trajectory]) -> dict[str, dict[str, float]]:
  stats: dict[str, dict[str, float]] = {}
  for trajectory in trajectories:
    item = stats.setdefault(trajectory.route, {"lateral_active_rows": 0, "driver_overlay_rows": 0})
    item["lateral_active_rows"] += trajectory.lateral_active_rows
    item["driver_overlay_rows"] += trajectory.driver_overlay_rows
  for item in stats.values():
    item["driver_overlay_fraction"] = item["driver_overlay_rows"] / max(item["lateral_active_rows"], 1)
  return stats


def load_trajectories(args: argparse.Namespace) -> tuple[list[Trajectory], dict[str, dict[str, float]], list[str]]:
  paths = log_data.discover_log_files(Path(args.root), "rlog", args.max_segments, [], None)
  route_paths = group_route_log_paths(paths)
  trajectories: list[Trajectory] = []
  started = perf_counter()
  parsed_segments = 0
  for index, grouped_paths in enumerate(route_paths, 1):
    try:
      route_trajectories = read_route_trajectories(
        grouped_paths,
        args.brand,
        args.car_fingerprint_contains,
        args.sample_step,
      )
    except Exception as e:
      print(f"skip route {route_name(grouped_paths[0].parent.name)}: {e}", file=sys.stderr)
      continue
    parsed_segments += len(grouped_paths)
    trajectories.extend(route_trajectories)
    if index % 5 == 0 or index == len(route_paths):
      print(
        f"loaded {parsed_segments}/{len(paths)} segments across " +
        f"{index}/{len(route_paths)} routes; usable={len(trajectories)}",
        flush=True,
      )
  stats = route_intervention_stats(trajectories)
  excluded = sorted(route for route, item in stats.items() if item["driver_overlay_fraction"] > args.max_route_driver_overlay)
  trajectories = [trajectory for trajectory in trajectories if trajectory.route not in excluded]
  print(f"excluded {len(excluded)}/{len(stats)} routes above {args.max_route_driver_overlay:.0%} driver overlay")
  for route in excluded:
    print(f"  exclude {route}: {stats[route]['driver_overlay_fraction']:.1%} overlay")
  print(f"retained {len(trajectories)} segments from {len({t.route for t in trajectories})} routes in {perf_counter() - started:.1f}s")
  return trajectories, stats, excluded


def split_routes(trajectories: list[Trajectory], validation_fraction: float, seed: int,
                 holdout_route_prefixes: tuple[str, ...] = ()) -> tuple[set[str], set[str]]:
  routes = sorted({trajectory.route for trajectory in trajectories})
  if len(routes) < 2:
    raise SystemExit(f"Need at least two retained routes for a route-level split; found {len(routes)}.")
  forced = {route for route in routes if any(route.startswith(prefix) for prefix in holdout_route_prefixes)}
  missing = [prefix for prefix in holdout_route_prefixes if not any(route.startswith(prefix) for route in routes)]
  if missing:
    raise SystemExit(f"No retained route matches forced holdout prefix(es): {', '.join(missing)}")
  if len(forced) >= len(routes):
    raise SystemExit("Forced holdout routes leave no routes for plant training.")
  rng = np.random.default_rng(seed)
  remaining = [route for route in routes if route not in forced]
  shuffled = list(np.asarray(remaining)[rng.permutation(len(remaining))])
  validation_count = max(1, min(len(routes) - 1, round(len(routes) * validation_fraction)))
  validation = forced | set(shuffled[:max(0, validation_count - len(forced))])
  return set(routes) - validation, validation


def stack_route_samples(trajectories: list[Trajectory], routes: set[str], history_steps: int, cap: int | None, seed: int) -> tuple[np.ndarray, np.ndarray]:
  samples = [trajectory_samples(t, history_steps) for t in trajectories if t.route in routes]
  samples = [(x, y) for x, y in samples if len(x)]
  if not samples:
    raise SystemExit(f"No clean plant samples for {len(routes)} selected routes.")
  x = np.vstack([item[0] for item in samples])
  y = np.vstack([item[1] for item in samples])
  if cap is not None and len(x) > cap:
    rng = np.random.default_rng(seed)
    selected = np.sort(rng.choice(len(x), cap, replace=False))
    x, y = x[selected], y[selected]
  return x, y


def make_model(args: argparse.Namespace) -> MultiOutputRegressor:
  return MultiOutputRegressor(HistGradientBoostingRegressor(
    max_iter=args.max_iter,
    learning_rate=args.learning_rate,
    max_leaf_nodes=args.max_leaf_nodes,
    l2_regularization=args.l2_regularization,
    random_state=args.random_state,
  ))


def one_step_metrics(model: Any, x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
  prediction = model.predict(x)
  targets = {}
  for index, name in enumerate(STATE_FEATURES):
    targets[f"delta_{name}"] = {
      "mae": float(mean_absolute_error(y[:, index], prediction[:, index])),
      "rmse": float(root_mean_squared_error(y[:, index], prediction[:, index])),
      "p95_abs_error": float(np.percentile(np.abs(prediction[:, index] - y[:, index]), 95)),
    }
  return {"samples": int(len(x)), "targets": targets}


def rollout_metrics(model: Any, trajectories: list[Trajectory], routes: set[str], history_steps: int, rollout_steps: int,
                    sample_period_s: float, cap: int, seed: int) -> dict[str, Any]:
  windows: list[tuple[Trajectory, int]] = []
  for trajectory in trajectories:
    if trajectory.route not in routes:
      continue
    values = trajectory.values
    sample_valid = values.get(
      "sample_valid", np.ones(len(trajectory.times), dtype=np.float32),
    )
    for source in range(history_steps - 1, len(trajectory.times) - rollout_steps):
      window = slice(source - history_steps + 1, source + rollout_steps + 1)
      if (
        values["lat_active"][window].min() < 0.5
        or values["driver_overlay"][window].max() > 0.5
        or values["saturated"][window].max() > 0.5
        or sample_valid[window].min() < 0.5
        or values["v_ego"][source] < MIN_TRAIN_SPEED_MPS
      ):
        continue
      gaps = np.diff(trajectory.times[window])
      if np.any((gaps <= 1e-4) | (gaps >= 0.09)):
        continue
      windows.append((trajectory, source))
  if len(windows) > cap:
    rng = np.random.default_rng(seed)
    windows = [windows[index] for index in np.sort(rng.choice(len(windows), cap, replace=False))]

  history = np.stack([
    np.stack([
      np.asarray([trajectory.values[name][source - lag] for name in BASE_FEATURES], dtype=np.float32)
      for lag in range(history_steps)
    ])
    for trajectory, source in windows
  ])
  errors: list[np.ndarray] = []
  state_indices = [BASE_FEATURES.index(name) for name in STATE_FEATURES]
  steering_rate_index = BASE_FEATURES.index("steering_rate_deg")
  signed_rate_state_index = STATE_FEATURES.index("signed_steering_rate_deg_s")
  for step in range(rollout_steps):
    prediction_delta = model.predict(history.reshape((len(history), -1)))
    next_state = history[:, 0, state_indices] + prediction_delta
    actual_next = np.stack([
      np.asarray([trajectory.values[name][source + step + 1] for name in STATE_FEATURES])
      for trajectory, source in windows
    ])
    errors.append(next_state - actual_next)
    next_base = np.stack([
      np.asarray([trajectory.values[name][source + step + 1] for name in BASE_FEATURES])
      for trajectory, source in windows
    ])
    next_base[:, state_indices] = next_state
    next_base[:, steering_rate_index] = np.abs(
      next_state[:, signed_rate_state_index],
    )
    history[:, 1:] = history[:, :-1]
    history[:, 0] = next_base

  result: dict[str, Any] = {"windows": len(windows), "horizons": {}}
  for step, error in enumerate(errors, 1):
    result["horizons"][f"{step * sample_period_s:.2f}s"] = {
      name: {
        "mae": float(np.mean(np.abs(error[:, index]))),
        "rmse": float(np.sqrt(np.mean(error[:, index] ** 2))),
        "p95_abs_error": float(np.percentile(np.abs(error[:, index]), 95)),
      }
      for index, name in enumerate(STATE_FEATURES)
    }
  return result


def train(args: argparse.Namespace) -> None:
  trajectories, route_stats, excluded_routes = load_trajectories(args)
  holdout_prefixes = tuple(args.holdout_route_prefix)
  train_routes, validation_routes = split_routes(trajectories, args.validation_fraction, args.random_state, holdout_prefixes)
  x_train, y_train = stack_route_samples(trajectories, train_routes, args.history_steps, args.max_train_samples, args.random_state)
  x_validation, y_validation = stack_route_samples(trajectories, validation_routes, args.history_steps, args.max_validation_samples, args.random_state + 1)
  print(f"plant samples: train={len(x_train)} validation={len(x_validation)}")
  print(f"routes: train={len(train_routes)} validation={len(validation_routes)}")

  model = make_model(args)
  started = perf_counter()
  model.fit(x_train, y_train)
  print(f"plant model fit in {perf_counter() - started:.1f}s")
  train_metrics = one_step_metrics(model, x_train, y_train)
  validation_metrics = one_step_metrics(model, x_validation, y_validation)
  rollout = rollout_metrics(model, trajectories, validation_routes, args.history_steps, args.rollout_steps,
                            args.sample_period_s, args.max_rollout_windows, args.random_state + 2)
  for name, target in validation_metrics["targets"].items():
    print(f"validation {name:32s} rmse={target['rmse']:.6f} p95={target['p95_abs_error']:.6f}")
  for horizon, target in rollout["horizons"].items():
    lat = target["actual_lateral_accel"]
    print(f"rollout {horizon}: lateral rmse={lat['rmse']:.6f} p95={lat['p95_abs_error']:.6f}")

  metadata = {
    "model_type": "controller_independent_lateral_plant_delta",
    "log_root": str(Path(args.root)),
    "sample_step": args.sample_step,
    "sample_period_s": args.sample_period_s,
    "history_steps": args.history_steps,
    "base_feature_names": list(BASE_FEATURES),
    "feature_names": expanded_feature_names(args.history_steps),
    "state_feature_names": list(STATE_FEATURES),
    "trajectory_extraction": {
      "version": TRAJECTORY_EXTRACTION_VERSION,
      "event_order": ["logMonoTime", "source_ordinal"],
      "adjacent_segment_overlap": "route_global_merge",
      "contiguous_run_proof": (
        "adjacent_segment_numbers_and_valid_endOfSegment_startOfSegment_sentinels"
      ),
      "route_state_carry": True,
      "max_route_state_carry_gap_s": MAX_ROUTE_STATE_CARRY_GAP_S,
      "max_asof_age_ms": MAX_ASOF_AGE_NS / 1e6,
      "source_selection": "independent_per_source_max_valid_source_with_logMonoTime_at_or_before_tick",
      "invalid_event_policy": {
        "carState": "drop_without_invalidating_prior_valid_state",
        "carControl": "invalidate_until_next_valid",
        "controlsState": "invalidate_until_next_valid",
        "carOutput": "invalidate_until_next_valid",
      },
      "event_valid_policy": (
        "drop_invalid_carState_and_invalidate_invalid_joined_service"
      ),
      "corrupt_input_policy": "reject_route",
      "required_asof_sources": [
        "carState",
        "carControl",
        "controlsState",
        "carOutput",
      ],
      "applied_torque_source": "carOutput.actuatorsOutput.torque_only_no_fallback",
      "desired_lateral_jerk": "recorded_only",
      "derived_lateral_jerk": "separate_causal_trailing_window_diagnostic",
      "signed_steering_rate": "causal_grid_difference_of_zoh_steering_angle",
    },
    "predictor_excludes": [
      "requested controller torque",
      "desired path and lateral-acceleration request",
      "controller error and P/I/D/F terms",
      "controller saturation and active state",
      "driver intervention labels",
    ],
    "route_driver_overlay_threshold": args.max_route_driver_overlay,
    "route_intervention_stats": route_stats,
    "excluded_routes": excluded_routes,
    "train_routes": sorted(train_routes),
    "validation_routes": sorted(validation_routes),
    "forced_holdout_route_prefixes": list(holdout_prefixes),
    "train_segments": [t.segment for t in trajectories if t.route in train_routes],
    "validation_segments": [t.segment for t in trajectories if t.route in validation_routes],
    "one_step": {"train": train_metrics, "validation": validation_metrics},
    "open_loop_rollout": rollout,
    "artifact_fit_samples": int(len(x_train)),
  }
  output = Path(args.output_dir)
  output.mkdir(parents=True, exist_ok=True)
  dump({"plant_model": model, "metadata": metadata}, output / "lateral_plant_model.joblib")
  (output / "metrics.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(f"model: {output / 'lateral_plant_model.joblib'}")
  print(f"metrics: {output / 'metrics.json'}")


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Train a controller-independent autoregressive lateral plant model.")
  parser.add_argument("--root", type=Path, default=DEFAULT_LOG_ROOT)
  parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
  parser.add_argument("--brand", default="hyundai")
  parser.add_argument("--car-fingerprint-contains", default="IONIQ5")
  parser.add_argument("--max-segments", type=int, default=0)
  parser.add_argument("--sample-step", type=int, default=5, help="100 Hz carState rows per retained plant step.")
  parser.add_argument("--history-steps", type=int, default=12, help="Retained 50 ms states supplied to the plant (12 = 0.6 s).")
  parser.add_argument("--rollout-steps", type=int, default=40, help="Open-loop validation horizon (40 = 2.0 s at the default sample step).")
  parser.add_argument("--holdout-route-prefix", action="append", default=[],
                      help="Force matching routes into validation; repeat for measured-regression routes.")
  parser.add_argument("--max-route-driver-overlay", type=float, default=0.50)
  parser.add_argument("--validation-fraction", type=float, default=0.20)
  parser.add_argument("--max-train-samples", type=int, default=350000)
  parser.add_argument("--max-validation-samples", type=int, default=90000)
  parser.add_argument("--max-rollout-windows", type=int, default=5000)
  parser.add_argument("--max-iter", type=int, default=180)
  parser.add_argument("--learning-rate", type=float, default=0.06)
  parser.add_argument("--max-leaf-nodes", type=int, default=31)
  parser.add_argument("--l2-regularization", type=float, default=0.03)
  parser.add_argument("--random-state", type=int, default=7)
  parsed = parser.parse_args()
  parsed.max_segments = None if parsed.max_segments == 0 else parsed.max_segments
  parsed.max_train_samples = None if parsed.max_train_samples == 0 else parsed.max_train_samples
  parsed.max_validation_samples = None if parsed.max_validation_samples == 0 else parsed.max_validation_samples
  parsed.sample_period_s = parsed.sample_step * 0.01
  return parsed


if __name__ == "__main__":
  args = parse_args()
  train(args)
