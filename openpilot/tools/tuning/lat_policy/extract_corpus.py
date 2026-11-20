#!/usr/bin/env python3
"""Build a 100 Hz lateral-dynamics corpus from Comma Companion telemetry.

Each rlog-backed route's ``telemetry.<sha>.ndjson`` is resampled onto a uniform
10 ms grid by taking, for every signal, the latest sample at or before the tick
(the same causal as-of alignment the controller sees at runtime). One
compressed ``.npz`` is written per route; reruns skip routes already extracted
from the same telemetry digest.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import orjson

DEFAULT_ARCHIVE = "//192.168.10.160/media/frog/comma-companion"
DEFAULT_DEVICE_ID = "afa2ee97250fb1e4"
GRID_US = 10_000
# A 100 Hz source older than this at a tick is treated as a dropout.
MAX_AGE_US = 35_000

# name in the corpus -> (telemetry signal id, source group)
SIGNALS = {
  "v_ego": ("vehicle.speed", "car_state"),
  "a_ego": ("vehicle.acceleration", "car_state"),
  "yaw_rate": ("vehicle.yaw_rate", "car_state"),
  "steer_angle": ("vehicle.steering_angle", "car_state"),
  "steer_rate": ("vehicle.steering_rate_signed", "car_state"),
  "steer_torque_driver": ("vehicle.steering_torque_driver", "car_state"),
  "steer_torque_eps": ("vehicle.steering_torque_eps", "car_state"),
  "steering_pressed": ("vehicle.steering_pressed", "car_state"),
  "standstill": ("vehicle.standstill", "car_state"),
  "steer_fault_temporary": ("vehicle.steer_fault_temporary", "car_state"),
  "steer_fault_permanent": ("vehicle.steer_fault_permanent", "car_state"),
  "lat_active": ("control.lateral_active", "car_control"),
  "torque_command": ("control.torque_command", "car_control"),
  "torque_output_can": ("control.torque_output_can", "car_control"),
  "applied_torque": ("control.applied_torque", "car_output"),
  "actual_lat_accel": ("lateral.actual_acceleration", "controls_state"),
  "desired_lat_accel": ("lateral.desired_acceleration", "controls_state"),
  "desired_lat_jerk": ("lateral.desired_jerk", "controls_state"),
  "ctrl_error": ("lateral.error", "controls_state"),
  "ctrl_p": ("lateral.p", "controls_state"),
  "ctrl_i": ("lateral.i", "controls_state"),
  "ctrl_d": ("lateral.d", "controls_state"),
  "ctrl_f": ("lateral.f", "controls_state"),
  "ctrl_output": ("lateral.output", "controls_state"),
  "ctrl_saturated": ("lateral.saturated", "controls_state"),
  "desired_curvature": ("lateral.desired_curvature", "controls_state"),
  "measured_curvature": ("lateral.measured_curvature", "controls_state"),
  "roll": ("live_tune.roll", "live_parameters"),
  "steer_ratio": ("live_tune.steer_ratio", "live_parameters"),
  "stiffness_factor": ("live_tune.stiffness_factor", "live_parameters"),
  "live_lat_accel_factor": ("live_tune.lateral_accel_factor", "live_torque"),
  "live_lat_accel_offset": ("live_tune.lateral_accel_offset", "live_torque"),
  "live_friction": ("live_tune.friction", "live_torque"),
  "model_desired_curvature": ("model.desired_curvature", "model"),
}
# Groups whose staleness invalidates a tick for dynamics learning.
FAST_GROUPS = ("car_state", "car_control", "car_output", "controls_state")
# The extractor writes compact JSON with sorted keys, so these byte markers
# identify a record without parsing megabyte-long lines that are not needed.
SERIES_MARKER = b'"record":"series_chunk","signal":"'
FULL_TIER_MARKER = b'"tier":"full"'
MODEL_MARKER = b'"record":"model_path_chunk"'
HEADER_MARKER = b'"record":"stream_header"'
MANIFEST_MARKER = b'"record":"manifest"'


def _asof(sample_t: np.ndarray, sample_v: np.ndarray, grid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
  index = np.searchsorted(sample_t, grid, side="right") - 1
  valid = index >= 0
  index = np.clip(index, 0, len(sample_t) - 1)
  age = np.where(valid, grid - sample_t[index], np.iinfo(np.int64).max // 2)
  return sample_v[index], age


def extract_route(archive: str, device_id: str, route: str, output_dir: str) -> dict:
  started = time.monotonic()
  route_dir = f"{archive}/telemetry/{device_id}/{route}/v1"
  with open(f"{route_dir}/index.json", "rb") as handle:
    index = orjson.loads(handle.read())
  digest = index["ndjson_sha256"]
  target = Path(output_dir) / f"{route}.npz"
  if target.exists():
    try:
      with np.load(target, allow_pickle=False) as existing:
        if str(existing["telemetry_sha256"]) == digest:
          return {"route": route, "status": "cached", "rows": int(existing["t_us"].shape[0])}
    except Exception:
      pass

  wanted = {signal_id: name for name, (signal_id, _group) in SIGNALS.items()}
  times: dict[str, list[np.ndarray]] = {name: [] for name in SIGNALS}
  values: dict[str, list[np.ndarray]] = {name: [] for name in SIGNALS}
  model_t: list[int] = []
  model_x: list[list[float]] = []
  model_y: list[list[float]] = []
  model_ts: list[float] | None = None
  header: dict = {}
  manifest: dict = {}
  bytes_read = 0
  with open(f"{archive}/{index['ndjson_path']}", "rb", buffering=8 * 1024 * 1024) as handle:
    for line in handle:
      bytes_read += len(line)
      position = line.find(SERIES_MARKER)
      if position >= 0:
        start = position + len(SERIES_MARKER)
        signal_id = line[start:line.index(b'"', start)].decode()
        name = wanted.get(signal_id)
        if name is None or FULL_TIER_MARKER not in line[start:]:
          continue
        record = orjson.loads(line)
        times[name].append(np.asarray(record["t_us"], dtype=np.int64))
        values[name].append(np.asarray(record["v"], dtype=np.float32))
        continue
      if MODEL_MARKER in line[:256]:
        record = orjson.loads(line)
        for row in record["rows"]:
          path = row["path"]
          if model_ts is None:
            model_ts = path["t_s"]
          if len(path["x_m"]) != len(model_ts):
            continue
          model_t.append(row["t_us"])
          model_x.append(path["x_m"])
          model_y.append(path["y_m"])
        continue
      if len(line) < 64 * 1024 and HEADER_MARKER in line:
        header = orjson.loads(line)
        continue
      if line.startswith(b'{"capabilities"') and MANIFEST_MARKER in line:
        manifest = orjson.loads(line)

  if not times["steer_angle"] or not times["torque_command"]:
    return {"route": route, "status": "empty", "rows": 0}
  merged_t: dict[str, np.ndarray] = {}
  merged_v: dict[str, np.ndarray] = {}
  for name in SIGNALS:
    if not times[name]:
      continue
    sample_t = np.concatenate(times[name])
    sample_v = np.concatenate(values[name])
    order = np.argsort(sample_t, kind="stable")
    merged_t[name] = sample_t[order]
    merged_v[name] = sample_v[order]

  car_state_t = merged_t["steer_angle"]
  grid = np.arange(car_state_t[0], car_state_t[-1] + 1, GRID_US, dtype=np.int64)
  arrays: dict[str, np.ndarray] = {"t_us": grid}
  group_age: dict[str, np.ndarray] = {}
  for name, (_signal_id, group) in SIGNALS.items():
    if name not in merged_t:
      arrays[name] = np.full(grid.shape, np.nan, dtype=np.float32)
      continue
    resampled, age = _asof(merged_t[name], merged_v[name], grid)
    arrays[name] = resampled.astype(np.float32)
    group_age[group] = age if group not in group_age else np.maximum(group_age[group], age)
  fresh = np.ones(grid.shape, dtype=bool)
  for group in FAST_GROUPS:
    if group in group_age:
      fresh &= group_age[group] <= MAX_AGE_US
    else:
      fresh &= False
  arrays["fresh"] = fresh
  for group in ("live_parameters", "live_torque", "model"):
    age = group_age.get(group)
    arrays[f"{group}_age_s"] = (
      np.clip(age / 1e6, 0.0, 1e6).astype(np.float32) if age is not None
      else np.full(grid.shape, np.inf, dtype=np.float32)
    )

  if model_t:
    order = np.argsort(np.asarray(model_t, dtype=np.int64), kind="stable")
    arrays["model_t_us"] = np.asarray(model_t, dtype=np.int64)[order]
    arrays["model_path_x"] = np.asarray(model_x, dtype=np.float32)[order]
    arrays["model_path_y"] = np.asarray(model_y, dtype=np.float32)[order]
    arrays["model_path_t_s"] = np.asarray(model_ts, dtype=np.float32)
  else:
    arrays["model_t_us"] = np.zeros((0,), dtype=np.int64)
    arrays["model_path_x"] = np.zeros((0, 33), dtype=np.float32)
    arrays["model_path_y"] = np.zeros((0, 33), dtype=np.float32)
    arrays["model_path_t_s"] = np.zeros((33,), dtype=np.float32)

  software = manifest.get("route_software") or {}
  vehicle = manifest.get("vehicle") or {}
  metadata = {
    "route": route,
    "telemetry_sha256": digest,
    "log_type": header.get("log_type"),
    "extractor_version": header.get("extractor_version"),
    "git_branch": software.get("git_branch"),
    "git_commit": software.get("git_commit"),
    "wall_time_ns": software.get("wall_time_ns"),
    "car_fingerprint": vehicle.get("car_fingerprint"),
    "lateral_torque_tuning": vehicle.get("lateral_torque_tuning"),
    "vehicle": {key: value for key, value in vehicle.items() if isinstance(value, (int, float, str))},
    "controller_params": {
      key: value.get("text") for key, value in (software.get("controller_params") or {}).items()
      if isinstance(value, dict) and "text" in value
    },
  }
  target.parent.mkdir(parents=True, exist_ok=True)
  temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
  with open(temporary, "wb") as handle:
    np.savez_compressed(
      handle,
      telemetry_sha256=np.asarray(digest),
      metadata_json=np.asarray(json.dumps(metadata)),
      **arrays,
    )
  os.replace(temporary, target)
  return {
    "route": route,
    "status": "extracted",
    "rows": int(grid.shape[0]),
    "fresh_fraction": float(fresh.mean()),
    "megabytes": bytes_read / 1e6,
    "seconds": time.monotonic() - started,
  }


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--archive", default=DEFAULT_ARCHIVE)
  parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
  parser.add_argument("--output", required=True, help="directory for per-route .npz files")
  parser.add_argument("--workers", type=int, default=8)
  parser.add_argument("--routes", nargs="*", help="limit to these route names")
  parser.add_argument("--largest-first", action="store_true")
  args = parser.parse_args()

  telemetry_root = f"{args.archive}/telemetry/{args.device_id}"
  routes = sorted(args.routes or os.listdir(telemetry_root))
  selected = []
  for route in routes:
    index_path = f"{telemetry_root}/{route}/v1/index.json"
    if not os.path.exists(index_path):
      continue
    with open(index_path, "rb") as handle:
      index = orjson.loads(handle.read())
    selected.append((index.get("ndjson_size_bytes", 0), route))
  # The archive share reads cold data slowly, so throughput is bounded by bytes.
  # Smallest first makes many distinct routes available early.
  selected.sort(reverse=args.largest_first)
  print(f"{len(selected)} routes, {sum(size for size, _ in selected) / 1e9:.1f} GB of telemetry", flush=True)

  started = time.monotonic()
  done_megabytes = 0.0
  rows = 0
  failures = 0
  with ProcessPoolExecutor(max_workers=args.workers) as pool:
    futures = {
      pool.submit(extract_route, args.archive, args.device_id, route, args.output): route
      for _size, route in selected
    }
    for count, future in enumerate(as_completed(futures), start=1):
      route = futures[future]
      try:
        result = future.result()
      except Exception as error:
        failures += 1
        print(f"FAILED {route}: {error!r}", flush=True)
        continue
      done_megabytes += result.get("megabytes", 0.0)
      rows += result["rows"]
      elapsed = time.monotonic() - started
      print(
        f"[{count}/{len(futures)}] {route} {result['status']} rows={result['rows']} " +
        f"fresh={result.get('fresh_fraction', float('nan')):.3f} " +
        f"total={rows / 360000:.1f}h rate={done_megabytes / max(elapsed, 1e-6):.0f}MB/s",
        flush=True,
      )
  print(f"done: {rows / 360000:.1f} h, {failures} failure(s), {time.monotonic() - started:.0f}s", flush=True)
  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
