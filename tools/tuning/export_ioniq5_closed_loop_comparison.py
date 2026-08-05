#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import torch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

from openpilot.tools.tuning import delay_alignment
from openpilot.tools.tuning import optimize_ioniq5_closed_loop as legacy
from openpilot.tools.tuning import optimize_ioniq5_neural_closed_loop as neural
from openpilot.tools.tuning import optimize_ioniq5_response_tune as tune_math
from openpilot.tools.tuning import train_lateral_plant_model as plant_data


def rounded(values: np.ndarray) -> list[float]:
  return np.round(np.asarray(values, dtype=np.float64), 6).tolist()


def ramp_hold_comparison(
  evaluator: neural.DampedClosedLoopEvaluator,
  old_trace: legacy.RolloutTrace,
  new_trace: legacy.RolloutTrace,
) -> dict[str, Any]:
  events, directions = evaluator._ramp_hold_events()
  actual_index = plant_data.STATE_FEATURES.index("actual_lateral_accel")
  old_actual = old_trace.states[:, :, actual_index]
  new_actual = new_trace.states[:, :, actual_index]
  before_steps = round(0.20 / evaluator.dt)
  after_steps = round(0.50 / evaluator.dt)
  offsets = np.arange(-before_steps, after_steps + 1)
  target_rows: list[np.ndarray] = []
  old_rows: list[np.ndarray] = []
  new_rows: list[np.ndarray] = []
  event_records: list[tuple[float, int, int, np.ndarray, np.ndarray, np.ndarray]] = []
  for row, step in np.argwhere(events):
    start = step - before_steps
    end = step + after_steps + 1
    if start < 0 or end > old_actual.shape[1]:
      continue
    direction = directions[row, step]
    target_row = direction * (
      evaluator.batch.target_desired[row, start:end] - evaluator.batch.target_desired[row, step]
    )
    old_row = direction * (old_actual[row, start:end] - old_actual[row, step])
    new_row = direction * (new_actual[row, start:end] - new_actual[row, step])
    post_end = min(len(old_row), before_steps + round(0.30 / evaluator.dt) + 1)
    old_dip = max(0.0, -float(np.min(old_row[before_steps:post_end])))
    target_rows.append(target_row)
    old_rows.append(old_row)
    new_rows.append(new_row)
    event_records.append((old_dip, row, step, target_row, old_row, new_row))
  if not old_rows:
    raise RuntimeError("No complete ramp-to-hold events were available for comparison.")
  target = np.stack(target_rows)
  old = np.stack(old_rows)
  new = np.stack(new_rows)
  event_records.sort(key=lambda record: record[0])
  representative_index = round(0.90 * (len(event_records) - 1))
  old_dip, row, step, target_row, old_row, new_row = event_records[representative_index]
  return {
    "event_count": len(old_rows),
    "time_s": rounded(offsets * evaluator.dt),
    "delayed_target_mean_mps2": rounded(np.mean(target, axis=0)),
    "old_actual_mean_mps2": rounded(np.mean(old, axis=0)),
    "new_actual_mean_mps2": rounded(np.mean(new, axis=0)),
    "old_actual_p10_mps2": rounded(np.percentile(old, 10, axis=0)),
    "old_actual_p90_mps2": rounded(np.percentile(old, 90, axis=0)),
    "new_actual_p10_mps2": rounded(np.percentile(new, 10, axis=0)),
    "new_actual_p90_mps2": rounded(np.percentile(new, 90, axis=0)),
    "representative": {
      "selection": "90th percentile old-controller post-ramp dip",
      "segment": evaluator.batch.segments[row],
      "event_step": int(step),
      "old_dip_mps2": old_dip,
      "time_s": rounded(offsets * evaluator.dt),
      "delayed_target_mps2": rounded(target_row),
      "old_actual_mps2": rounded(old_row),
      "new_actual_mps2": rounded(new_row),
    },
  }


def highway_comparison(
  evaluator: neural.DampedClosedLoopEvaluator,
  old_trace: legacy.RolloutTrace,
  new_trace: legacy.RolloutTrace,
) -> dict[str, Any]:
  actual_index = plant_data.STATE_FEATURES.index("actual_lateral_accel")
  old_actual = old_trace.states[:, :, actual_index]
  initial_actual = evaluator.batch.history[:, 0, evaluator.base_index["actual_lateral_accel"]]
  old_rate = np.diff(np.column_stack((initial_actual, old_actual)), axis=1) / evaluator.dt
  quiet = (
    (evaluator.batch.v_ego >= 22.0)
    & (np.abs(evaluator.batch.target_jerk) < 0.20)
    & (np.abs(evaluator.batch.target_desired) >= 0.05)
  )
  row_scores: list[tuple[float, int]] = []
  for row in range(len(quiet)):
    mask = quiet[row]
    if np.count_nonzero(mask) < 20:
      continue
    error = old_rate[row, mask] - evaluator.batch.target_jerk[row, mask]
    row_scores.append((float(np.sqrt(np.mean(error ** 2))), row))
  if not row_scores:
    raise RuntimeError("No highway window had enough quiet-command samples.")
  row_scores.sort()
  percentile_index = round(0.90 * (len(row_scores) - 1))
  score, row = row_scores[percentile_index]
  new_actual = new_trace.states[row, :, actual_index]
  return {
    "selection": "90th percentile old-controller highway response-rate error",
    "old_rate_error_rms_mps3": score,
    "segment": evaluator.batch.segments[row],
    "mean_speed_mps": float(np.mean(evaluator.batch.v_ego[row])),
    "time_s": rounded(np.arange(old_actual.shape[1]) * evaluator.dt),
    "command_mps2": rounded(evaluator.batch.desired[row]),
    "delayed_target_mps2": rounded(evaluator.batch.target_desired[row]),
    "old_actual_mps2": rounded(old_actual[row]),
    "new_actual_mps2": rounded(new_actual),
    "old_applied_torque": rounded(old_trace.commands[row]),
    "new_applied_torque": rounded(new_trace.commands[row]),
  }


def main() -> None:
  parser = argparse.ArgumentParser(description="Export old/new Ioniq 5 closed-loop comparison traces.")
  parser.add_argument("--plant-model", type=Path, required=True)
  parser.add_argument("--log-root", type=Path, required=True)
  parser.add_argument("--tuning-report", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  parser.add_argument("--route-prefix", action="append", default=[])
  parser.add_argument("--rollout-steps", type=int, default=100)
  parser.add_argument("--max-windows", type=int, default=500)
  parser.add_argument("--response-delay-s", type=float, default=delay_alignment.DEFAULT_RESPONSE_DELAY_S)
  parser.add_argument("--random-state", type=int, default=25)
  parser.add_argument("--device", default="cpu")
  args = parser.parse_args()
  route_prefixes = args.route_prefix or ["00000109", "0000010b"]
  device = torch.device(args.device)
  predictor = neural.NeuralEnsemblePredictor(args.plant_model, device)
  config = predictor.config
  delay_steps, effective_delay_s = delay_alignment.quantize_response_delay(
    args.response_delay_s, config.sample_period_s, config.history_steps - 1,
  )
  trajectories = neural.discover_trajectories(args.log_root, route_prefixes, config.sample_step)
  batch = neural.make_batch(
    trajectories, config.history_steps, args.rollout_steps,
    args.max_windows, args.random_state, delay_steps,
  )
  report = json.loads(args.tuning_report.read_text(encoding="utf-8"))
  old_tune = legacy.current_tune(None)
  new_tune_values = dict(report["recommended_tune"])
  new_tune_values["name"] = "candidate"
  new_tune = tune_math.Tune(**new_tune_values)
  old_evaluator = neural.DampedClosedLoopEvaluator(
    predictor, batch, config.sample_period_s, report["objective"]["wobble_weight"],
    report["objective"]["baseline_damping_gain"],
    report["objective"]["baseline_damping_gain"],
    report["objective"]["baseline_damping_gain"],
    report["objective"]["baseline_reversal_damping_gain"],
    report["objective"]["baseline_reversal_hold_seconds"],
    highway_damping_gain=report["objective"]["baseline_highway_damping_gain"],
  )
  new_evaluator = neural.DampedClosedLoopEvaluator(
    predictor, batch, config.sample_period_s, report["objective"]["wobble_weight"],
    report["recommended_damping_gain"],
    report["recommended_turn_exit_damping_gain"],
    report["recommended_turn_exit_damping_gain_right"],
    report["recommended_reversal_damping_gain"],
    report["recommended_reversal_hold_seconds"],
    report["recommended_steering_rate_feedback_gain"],
    report["recommended_highway_damping_gain"],
    report.get("recommended_damping_reference_gain", 0.0),
  )
  old_trace = old_evaluator.rollout(old_tune)
  new_trace = new_evaluator.rollout(new_tune)
  payload = {
    "effective_response_delay_s": effective_delay_s,
    "route_prefixes": route_prefixes,
    "windows": len(batch.history),
    "ramp_hold": ramp_hold_comparison(old_evaluator, old_trace, new_trace),
    "highway": highway_comparison(old_evaluator, old_trace, new_trace),
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
  print(f"comparison: {args.output}")


if __name__ == "__main__":
  main()
