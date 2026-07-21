#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean, median
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

from tools.tuning_viewer import analyze_logs, split_segment_name  # noqa: E402


DEFAULT_LOG_ROOT = Path(r"D:\comma_driving_logs\10.30.1.75\realdata")
DEFAULT_OUTPUT = REPO_ROOT / "artifacts" / "tuning" / "latest_data_evaluation" / "planned_path_metrics.json"


def percentile(values: list[float], pct: float) -> float | None:
  if not values:
    return None
  values = sorted(values)
  idx = (len(values) - 1) * pct / 100.0
  lo = int(math.floor(idx))
  hi = int(math.ceil(idx))
  if lo == hi:
    return values[lo]
  return values[lo] + (values[hi] - values[lo]) * (idx - lo)


def signed_planned_path_error(sample: dict[str, Any]) -> float | None:
  desired = sample.get("desired")
  actual = sample.get("actual")
  if desired is None or actual is None or abs(float(desired)) < 1e-6:
    return None
  return math.copysign(1.0, float(desired)) * (float(actual) - float(desired))


def phase_samples(samples: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
  active = [sample for sample in samples if sample.get("active") and signed_planned_path_error(sample) is not None]
  if phase == "all_active":
    return active
  if phase == "near_center":
    return [sample for sample in active if abs(float(sample.get("desired") or 0.0)) < 0.08]

  def jerk(sample: dict[str, Any]) -> float | None:
    value = sample.get("desired_jerk")
    return float(value) if value is not None and math.isfinite(float(value)) else None

  if phase == "turn_in":
    return [
      sample for sample in active
      if abs(float(sample.get("desired") or 0.0)) >= 0.08
      and jerk(sample) is not None
      and float(sample["desired"]) * float(sample["desired_jerk"]) > 0.0
    ]
  if phase == "turn_in_left":
    return [sample for sample in phase_samples(samples, "turn_in") if float(sample["desired"]) > 0.0]
  if phase == "turn_in_right":
    return [sample for sample in phase_samples(samples, "turn_in") if float(sample["desired"]) < 0.0]
  if phase == "unwind":
    return [
      sample for sample in active
      if abs(float(sample.get("desired") or 0.0)) >= 0.08
      and jerk(sample) is not None
      and float(sample["desired"]) * float(sample["desired_jerk"]) < 0.0
    ]
  if phase == "higher_lat_steady":
    return [
      sample for sample in active
      if abs(float(sample.get("desired") or 0.0)) >= 0.08
      and jerk(sample) is not None
      and abs(float(sample["desired_jerk"])) < 0.08
    ]
  raise ValueError(f"Unknown phase: {phase}")


def phase_metrics(samples: list[dict[str, Any]], phase: str) -> dict[str, Any]:
  rows = phase_samples(samples, phase)
  errors = [signed_planned_path_error(sample) for sample in rows]
  errors = [error for error in errors if error is not None]
  if not errors:
    return {"samples": 0, "rmse": None, "mae": None, "p95_abs_error": None, "median_signed_error": None, "mean_signed_error": None}
  abs_errors = [abs(error) for error in errors]
  return {
    "samples": len(errors),
    "rmse": math.sqrt(mean(error * error for error in errors)),
    "mae": mean(abs_errors),
    "p95_abs_error": percentile(abs_errors, 95),
    "median_signed_error": median(errors),
    "mean_signed_error": mean(errors),
  }


def summarize(samples: list[dict[str, Any]], last_live_torque: dict[str, Any] | None = None) -> dict[str, Any]:
  active = [sample for sample in samples if sample.get("active")]
  pressed = [sample for sample in active if sample.get("steering_pressed")]
  phases = (
    "all_active",
    "near_center",
    "turn_in",
    "turn_in_left",
    "turn_in_right",
    "unwind",
    "higher_lat_steady",
  )
  return {
    "active_samples": len(active),
    "steering_pressed_active_samples": len(pressed),
    "steering_pressed_active_fraction": len(pressed) / len(active) if active else None,
    "phases": {phase: phase_metrics(samples, phase) for phase in phases},
    "last_live_torque": last_live_torque or {},
  }


def discover_routes(root: Path, log_type: str, route_prefixes: list[str]) -> dict[str, list[Path]]:
  routes: dict[str, list[tuple[int, Path]]] = {}
  for log_path in root.glob(f"*/{log_type}.zst"):
    trip, segment = split_segment_name(log_path.parent.name)
    if route_prefixes and not any(trip.startswith(prefix) for prefix in route_prefixes):
      continue
    routes.setdefault(trip, []).append((segment, log_path))
  return {
    trip: [path for _, path in sorted(paths)]
    for trip, paths in sorted(routes.items())
  }


def main() -> None:
  parser = argparse.ArgumentParser(description="Batch planned-path metrics for copied comma routes.")
  parser.add_argument("--root", type=Path, default=DEFAULT_LOG_ROOT)
  parser.add_argument("--log-type", choices=("qlog", "rlog"), default="qlog")
  parser.add_argument("--route-prefix", action="append", default=[], help="Include trips starting with this prefix; repeat as needed.")
  parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
  args = parser.parse_args()

  routes = discover_routes(args.root, args.log_type, args.route_prefix)
  if not routes:
    raise SystemExit("No matching route logs found")

  route_results: dict[str, Any] = {}
  all_samples: list[dict[str, Any]] = []
  live_snapshots: list[dict[str, Any]] = []
  for index, (trip, log_paths) in enumerate(routes.items(), 1):
    print(f"analyzing {index}/{len(routes)} {trip} ({len(log_paths)} segments)", flush=True)
    analysis = analyze_logs(log_paths, log_type=args.log_type, label=trip)
    samples = analysis["samples"]
    last_live_torque = analysis.get("last_live_torque") or {}
    route_results[trip] = {
      "segment_count": len(log_paths),
      **summarize(samples, last_live_torque),
    }
    all_samples.extend(samples)
    if last_live_torque:
      live_snapshots.append(last_live_torque)

  output = {
    "log_root": str(args.root),
    "log_type": args.log_type,
    "route_prefixes": args.route_prefix,
    "route_count": len(routes),
    "segment_count": sum(len(paths) for paths in routes.values()),
    "error_convention": "sign(desiredLateralAccel) * (actualLateralAccel - desiredLateralAccel); positive=tighter/inside, negative=wider/outside",
    "combined": summarize(all_samples, live_snapshots[-1] if live_snapshots else None),
    "routes": route_results,
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(f"metrics: {args.output}")


if __name__ == "__main__":
  main()
