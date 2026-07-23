#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from joblib import load

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

from openpilot.tools.tuning import evaluate_ioniq5_nnff_closed_loop as nnff_eval
from openpilot.tools.tuning import optimize_ioniq5_closed_loop as closed_loop


def main() -> None:
  parser = argparse.ArgumentParser(description="Sweep NNFF output gain on multiple route-level plant holdouts.")
  parser.add_argument("--plant-model", type=Path, required=True)
  parser.add_argument("--model", type=Path, required=True)
  parser.add_argument("--route-prefix", action="append", required=True)
  parser.add_argument("--gain", action="append", type=float, required=True)
  parser.add_argument("--output", type=Path, required=True)
  parser.add_argument("--rollout-steps", type=int, default=30)
  parser.add_argument("--max-windows", type=int, default=1500)
  args = parser.parse_args()

  artifact = load(args.plant_model)
  metadata = artifact["metadata"]
  trajectories = closed_loop.load_validation_trajectories(metadata)
  payload = json.loads(args.model.read_text(encoding="utf-8"))
  result = {}
  for route_prefix in args.route_prefix:
    batch, path_desired = nnff_eval.build_nnff_batch(
      trajectories,
      route_prefix,
      int(metadata["history_steps"]),
      args.rollout_steps,
      args.max_windows,
      31,
    )
    scorer = closed_loop.ClosedLoopEvaluator(
      artifact["plant_model"], batch, float(metadata["sample_period_s"]), wobble_weight=0.04,
    )
    conventional = scorer.evaluate_trace(
      closed_loop.CURRENT_CODE_TUNE,
      scorer.rollout(closed_loop.CURRENT_CODE_TUNE),
    )
    gains = {}
    for gain in args.gain:
      scaled = nnff_eval.scale_flux_output(payload, gain)
      trace = nnff_eval.neural_rollout(
        artifact["plant_model"], batch, path_desired, scaled, scorer.dt,
      )
      metrics = scorer.evaluate_trace(closed_loop.CURRENT_CODE_TUNE, trace)
      gains[str(gain)] = {
        "metrics": metrics,
        "vs_conventional_percent": nnff_eval.relative(metrics, conventional),
      }
      print(
        f"route={route_prefix} gain={gain:.3f} objective={metrics['objective']:.6f} " +
        f"transition={metrics['balanced_transition_rmse']:.6f} " +
        f"overall={metrics['phases']['all']['rmse']:.6f} command={metrics['command_rms']:.6f}",
        flush=True,
      )
    result[route_prefix] = {
      "windows": len(batch.history),
      "conventional": conventional,
      "gains": gains,
    }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(f"report: {args.output}")


if __name__ == "__main__":
  main()
