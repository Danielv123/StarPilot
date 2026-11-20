#!/usr/bin/env python3
"""Turn the per-route corpus into one training table of clean engaged driving.

Everything is expressed in the torque controller's own sign convention, so a
positive torque produces a positive lateral acceleration:

  u      applied steering torque, ``-carOutput.actuatorsOutput.torque``
  y      measured lateral acceleration, ``controlsState.curvature * v**2``
  angle  steering wheel angle in degrees, de-quantised: the sensor reports
         0.1 degree steps, which a short zero-phase filter removes so the
         plant learns the motion rather than the staircase
  offset the angle offset the controller was using, recovered so that
         ``vehicle.lateral_accel(angle - offset, v, roll, steer_ratio, stiffness) == y``
  target ``controlsState.desiredCurvature * v**2``; the planner issues it for
         one lateral delay into the future

A row is usable for plant learning when the controller was steering, the
driver was not, and all sources were fresh. Rows are grouped into contiguous
runs so windows never straddle a gap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import savgol_filter

try:
  from . import vehicle
except ImportError:  # running as a script
  sys.path.insert(0, str(Path(__file__).resolve().parent))
  import vehicle

MIN_SPEED = 0.5
MIN_RUN_ROWS = 300
# liveParameters' angle offset has a fast component, so it is recovered per
# sample; the short median only removes as-of alignment jitter.
OFFSET_FILTER_ROWS = 51
OFFSET_MIN_SPEED = 6.0
ANGLE_SMOOTH_ROWS = 15
ANGLE_SMOOTH_ORDER = 3
COLUMNS = ("u", "u_cmd", "y", "angle", "offset", "v", "a", "roll", "target", "steer_ratio", "stiffness", "driver_torque")


def split_of(route: str) -> str:
  """Deterministic route-level split: about 80 % train, 10 % validation, 10 % test."""
  bucket = int(hashlib.sha256(route.encode()).hexdigest()[:8], 16) % 10
  return "val" if bucket == 0 else "test" if bucket == 1 else "train"


def recover_offset(angle: np.ndarray, implied: np.ndarray, valid: np.ndarray) -> np.ndarray:
  """Per-sample angle offset where the geometry is well conditioned, held elsewhere."""
  if valid.sum() < OFFSET_FILTER_ROWS:
    return np.zeros_like(angle)
  index = np.arange(len(angle))
  raw = np.interp(index, index[valid], (angle - implied)[valid])
  return median_filter(raw, size=OFFSET_FILTER_ROWS, mode="nearest").astype(np.float32)


def load_route(path: Path) -> dict | None:
  with np.load(path, allow_pickle=False) as data:
    arrays = {key: data[key] for key in data.files}
  metadata = json.loads(str(arrays["metadata_json"]))
  if metadata.get("log_type") != "rlog" or metadata.get("car_fingerprint") != "HYUNDAI_IONIQ_5":
    return None
  v = arrays["v_ego"]
  y = arrays["measured_curvature"] * v * v
  steer_ratio = np.where(np.isfinite(arrays["steer_ratio"]) & (arrays["steer_ratio"] > 1.0), arrays["steer_ratio"], vehicle.BASE_STEER_RATIO)
  stiffness = np.where(np.isfinite(arrays["stiffness_factor"]), np.maximum(arrays["stiffness_factor"], 0.1), 1.0)
  roll = np.nan_to_num(arrays["roll"])
  raw_angle = np.degrees(arrays["steer_angle"])
  angle = savgol_filter(np.nan_to_num(raw_angle), ANGLE_SMOOTH_ROWS, ANGLE_SMOOTH_ORDER).astype(np.float32)
  # Recover the learned angle offset from the controller's own measurement.
  moving = v > OFFSET_MIN_SPEED
  implied = vehicle.steering_angle_deg(y, np.maximum(v, OFFSET_MIN_SPEED), roll, steer_ratio, stiffness)
  offset = recover_offset(raw_angle, implied, moving & arrays["fresh"] & np.isfinite(implied) & np.isfinite(raw_angle))

  finite = np.isfinite(v) & np.isfinite(y) & np.isfinite(raw_angle) & np.isfinite(arrays["applied_torque"]) & np.isfinite(arrays["desired_curvature"])
  usable = (
    arrays["fresh"] & finite
    & (arrays["lat_active"] > 0.5)
    & (arrays["steering_pressed"] < 0.5)
    & (arrays["steer_fault_temporary"] < 0.5)
    & (arrays["steer_fault_permanent"] < 0.5)
    & (arrays["live_parameters_age_s"] < 1.0)
    & (v >= MIN_SPEED)
  )
  table = {
    "u": -arrays["applied_torque"],
    "u_cmd": -arrays["torque_command"],
    "y": y,
    "angle": angle,
    "offset": offset,
    "v": v,
    "a": np.nan_to_num(arrays["a_ego"]),
    "roll": roll,
    "target": arrays["desired_curvature"] * v * v,
    "steer_ratio": steer_ratio,
    "stiffness": stiffness,
    "driver_torque": np.nan_to_num(arrays["steer_torque_driver"]),
  }
  return {
    "route": metadata["route"],
    "branch": metadata.get("git_branch"),
    "wall_time_ns": int(metadata.get("wall_time_ns") or 0),
    "usable": usable,
    "table": {key: value.astype(np.float32) for key, value in table.items()},
  }


def runs_of(mask: np.ndarray, minimum: int) -> list[tuple[int, int]]:
  padded = np.concatenate(([False], mask, [False]))
  edges = np.flatnonzero(padded[1:] != padded[:-1])
  return [(int(start), int(end)) for start, end in zip(edges[::2], edges[1::2], strict=True) if end - start >= minimum]


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--corpus", required=True)
  parser.add_argument("--output", required=True)
  args = parser.parse_args()

  columns: dict[str, list[np.ndarray]] = {name: [] for name in COLUMNS}
  run_start, run_end, run_route, run_split = [], [], [], []
  routes = []
  cursor = 0
  total_rows = 0
  for path in sorted(Path(args.corpus).glob("*.npz")):
    route = load_route(path)
    if route is None:
      continue
    total_rows += len(route["usable"])
    runs = runs_of(route["usable"], MIN_RUN_ROWS)
    if not runs:
      continue
    split = split_of(route["route"])
    routes.append({"route": route["route"], "branch": route["branch"], "wall_time_ns": route["wall_time_ns"], "split": split,
                   "rows": int(sum(end - start for start, end in runs))})
    for start, end in runs:
      for name in COLUMNS:
        columns[name].append(route["table"][name][start:end])
      run_start.append(cursor)
      cursor += end - start
      run_end.append(cursor)
      run_route.append(len(routes) - 1)
      run_split.append(("train", "val", "test").index(split))

  table = {name: np.concatenate(parts) for name, parts in columns.items()}
  output = Path(args.output)
  output.parent.mkdir(parents=True, exist_ok=True)
  np.savez(
    output,
    run_start=np.asarray(run_start, dtype=np.int64),
    run_end=np.asarray(run_end, dtype=np.int64),
    run_route=np.asarray(run_route, dtype=np.int32),
    run_split=np.asarray(run_split, dtype=np.int8),
    routes_json=np.asarray(json.dumps(routes)),
    **table,
  )
  hours = {name: sum(route["rows"] for route in routes if route["split"] == name) / 360000 for name in ("train", "val", "test")}
  print(f"{len(routes)} routes with engaged driving out of {total_rows / 360000:.1f} h recorded")
  print(f"usable hours: train {hours['train']:.2f}, val {hours['val']:.2f}, test {hours['test']:.2f}; {len(run_start)} runs")
  for name in ("u", "y", "angle", "offset", "v", "target"):
    values = table[name]
    print(f"  {name:7s} mean {values.mean():8.4f} std {values.std():7.4f} p1 {np.percentile(values, 1):8.3f} p99 {np.percentile(values, 99):8.3f}")
  residual = vehicle.lateral_accel(table["angle"] - table["offset"], table["v"], table["roll"], table["steer_ratio"], table["stiffness"]) - table["y"]
  print(f"  geometry residual rms {np.sqrt(np.mean(residual**2)):.4f} m/s^2")
  return 0


if __name__ == "__main__":
  sys.exit(main())
