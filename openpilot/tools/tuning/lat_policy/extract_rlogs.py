#!/usr/bin/env python3
"""Build the same per-route corpus as ``extract_corpus.py`` from raw rlogs on disk.

The Comma Companion archive is the source of record, but its share is slow to
read in bulk. Routes that were already staged locally as
``<root>/<route>--<segment>/rlog`` are parsed here instead, producing files the
rest of the pipeline cannot tell apart. Needs the openpilot Python stack for
the log schema, so run it where that imports (for example under WSL).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

GRID_US = 10_000
MAX_AGE_US = 35_000
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
FAST_GROUPS = ("car_state", "car_control", "car_output", "controls_state")


def read_segment(path: Path, columns: dict):
  import zstandard
  from openpilot.cereal import log

  raw = path.read_bytes()
  # Streamed, because a log interrupted mid-write holds more than one zstd frame.
  data = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read() if raw[:4] == ZSTD_MAGIC else raw
  fingerprint = None
  branch = None
  try:
    for event in log.Event.read_multiple_bytes(data):
      kind = event.which()
      t_us = event.logMonoTime // 1000
      if kind == "carState":
        message = event.carState
        columns["car_state"].append((t_us, message.vEgo, message.aEgo, np.radians(message.steeringAngleDeg), message.steeringTorque,
                                     message.steeringPressed, message.steerFaultTemporary, message.steerFaultPermanent))
      elif kind == "carControl":
        message = event.carControl
        columns["car_control"].append((t_us, message.latActive, message.actuators.torque))
      elif kind == "carOutput":
        columns["car_output"].append((t_us, event.carOutput.actuatorsOutput.torque))
      elif kind == "controlsState":
        message = event.controlsState
        columns["controls_state"].append((t_us, message.curvature, message.desiredCurvature))
      elif kind in ("vehicleParameters", "liveParameters"):
        message = getattr(event, kind)
        columns["live_parameters"].append((t_us, message.roll, message.steerRatio, message.stiffnessFactor))
      elif kind == "carParams":
        fingerprint = str(event.carParams.carFingerprint)
      elif kind == "initData":
        branch = str(event.initData.gitBranch)
  except Exception as error:  # a truncated final message; keep what was read
    print(f"{path}: stopped early: {type(error).__name__}", flush=True)
  return fingerprint, branch


def asof(sample_t: np.ndarray, sample_v: np.ndarray, grid: np.ndarray):
  index = np.searchsorted(sample_t, grid, side="right") - 1
  valid = index >= 0
  index = np.clip(index, 0, len(sample_t) - 1)
  age = np.where(valid, grid - sample_t[index], np.iinfo(np.int64).max // 2)
  return sample_v[index], age


def extract_route(root: str, route: str, segments: list[int], output_dir: str) -> dict:
  started = time.monotonic()
  target = Path(output_dir) / f"{route}.npz"
  if target.exists():
    return {"route": route, "status": "cached", "rows": 0}
  columns: dict[str, list] = defaultdict(list)
  fingerprint = branch = None
  for segment in sorted(segments):
    seen_fingerprint, seen_branch = read_segment(Path(root) / f"{route}--{segment}" / "rlog", columns)
    fingerprint = fingerprint or seen_fingerprint
    branch = branch or seen_branch
  if not columns["car_state"] or not columns["car_control"] or not columns["car_output"] or not columns["controls_state"]:
    return {"route": route, "status": "empty", "rows": 0}
  tables = {}
  for group, rows in columns.items():
    table = np.asarray(rows, dtype=np.float64)
    tables[group] = table[np.argsort(table[:, 0], kind="stable")]
  origin = int(tables["car_state"][0, 0])
  times = {group: (table[:, 0] - origin).astype(np.int64) for group, table in tables.items()}
  grid = np.arange(0, times["car_state"][-1] + 1, GRID_US, dtype=np.int64)

  layout = {
    "car_state": ("v_ego", "a_ego", "steer_angle", "steer_torque_driver", "steering_pressed", "steer_fault_temporary", "steer_fault_permanent"),
    "car_control": ("lat_active", "torque_command"),
    "car_output": ("applied_torque",),
    "controls_state": ("measured_curvature", "desired_curvature"),
    "live_parameters": ("roll", "steer_ratio", "stiffness_factor"),
  }
  arrays: dict[str, np.ndarray] = {"t_us": grid}
  ages = {}
  for group, names in layout.items():
    if group not in tables:
      for name in names:
        arrays[name] = np.full(grid.shape, np.nan, dtype=np.float32)
      continue
    for column, name in enumerate(names, start=1):
      values, age = asof(times[group], tables[group][:, column], grid)
      arrays[name] = values.astype(np.float32)
    ages[group] = age
  fresh = np.ones(grid.shape, dtype=bool)
  for group in FAST_GROUPS:
    fresh &= ages[group] <= MAX_AGE_US
  arrays["fresh"] = fresh
  live_age = ages.get("live_parameters")
  arrays["live_parameters_age_s"] = (np.clip(live_age / 1e6, 0.0, 1e6).astype(np.float32) if live_age is not None
                                     else np.full(grid.shape, np.inf, dtype=np.float32))
  metadata = {"route": route, "log_type": "rlog", "source": "local_rlog", "git_branch": branch, "car_fingerprint": fingerprint,
              "segments": sorted(segments)}
  target.parent.mkdir(parents=True, exist_ok=True)
  temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
  with open(temporary, "wb") as handle:
    np.savez_compressed(handle, telemetry_sha256=np.asarray("local_rlog"), metadata_json=np.asarray(json.dumps(metadata)), **arrays)
  os.replace(temporary, target)
  return {"route": route, "status": "extracted", "rows": int(grid.shape[0]), "fresh_fraction": float(fresh.mean()),
          "seconds": time.monotonic() - started}


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--root", required=True, help="directory of <route>--<segment>/rlog folders")
  parser.add_argument("--output", required=True)
  parser.add_argument("--workers", type=int, default=12)
  parser.add_argument("--routes", nargs="*", help="limit to these route names")
  args = parser.parse_args()

  routes: dict[str, list[int]] = defaultdict(list)
  for entry in os.listdir(args.root):
    route, separator, segment = entry.rpartition("--")
    if separator and segment.isdigit() and (Path(args.root) / entry / "rlog").is_file():
      routes[route].append(int(segment))
  if args.routes:
    routes = {route: segments for route, segments in routes.items() if route in set(args.routes)}
  print(f"{len(routes)} routes, {sum(len(v) for v in routes.values())} segments", flush=True)
  rows = failures = 0
  started = time.monotonic()
  with ProcessPoolExecutor(max_workers=args.workers) as pool:
    futures = {pool.submit(extract_route, args.root, route, segments, args.output): route for route, segments in routes.items()}
    for count, future in enumerate(as_completed(futures), start=1):
      try:
        result = future.result()
      except Exception as error:
        failures += 1
        print(f"FAILED {futures[future]}: {error!r}", flush=True)
        continue
      rows += result["rows"]
      print(f"[{count}/{len(futures)}] {result['route']} {result['status']} rows={result['rows']} " +
            f"fresh={result.get('fresh_fraction', float('nan')):.3f} total={rows / 360000:.1f}h {time.monotonic() - started:.0f}s", flush=True)
  print(f"done: {rows / 360000:.1f} h, {failures} failure(s)", flush=True)
  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
