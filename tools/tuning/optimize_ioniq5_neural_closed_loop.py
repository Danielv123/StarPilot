#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

try:
  import torch
except ModuleNotFoundError as e:
  raise SystemExit("Run with torch, numpy, joblib, pycapnp, and zstandard installed.") from e

from openpilot.tools.tuning import optimize_ioniq5_closed_loop as legacy
from openpilot.tools.tuning import optimize_ioniq5_response_tune as tune_math
from openpilot.tools.tuning import train_lateral_plant_model as plant_data
from openpilot.tools.tuning import train_neural_lateral_plant as neural_plant


DEFAULT_PLANT = REPO_ROOT / "artifacts/tuning/neural_lateral_plant_20260723/neural_lateral_plant.pt"
DEFAULT_LOG_ROOT = Path(r"D:\comma_driving_logs\10.30.1.75\realdata")
DEFAULT_OUTPUT = REPO_ROOT / "artifacts/tuning/ioniq5_neural_closed_loop_20260724/optimization.json"


class NeuralEnsemblePredictor:
  def __init__(self, model_path: Path, device: torch.device):
    self.models, self.stats, self.payload = neural_plant.load_ensemble_artifact(
      model_path, device, differentiable=False,
    )
    self.device = device
    self.config = neural_plant.ModelConfig(**self.payload["config"])
    self.disagreement_steps: list[np.ndarray] = []

  def clear_disagreement(self) -> None:
    self.disagreement_steps.clear()

  def predict(self, flattened_history: np.ndarray) -> np.ndarray:
    history = torch.as_tensor(
      flattened_history.reshape(
        len(flattened_history), self.config.history_steps, len(plant_data.BASE_FEATURES),
      ),
      dtype=torch.float32,
      device=self.device,
    )
    with torch.no_grad():
      prediction, disagreement = neural_plant.ensemble_predict_delta(
        self.models, history, self.stats,
      )
    self.disagreement_steps.append(disagreement.cpu().numpy())
    return prediction.cpu().numpy()

  def disagreement_summary(self) -> dict[str, Any]:
    if not self.disagreement_steps:
      return {"mean": None, "p95": None, "by_state": {}}
    values = np.stack(self.disagreement_steps, axis=1)
    state_std = self.stats["state_std"].detach().cpu().numpy()
    normalized = values / state_std
    return {
      "mean": float(np.mean(normalized)),
      "p95": float(np.percentile(normalized, 95)),
      "by_state": {
        name: {
          "mean": float(np.mean(normalized[..., index])),
          "p95": float(np.percentile(normalized[..., index], 95)),
        }
        for index, name in enumerate(plant_data.STATE_FEATURES)
      },
    }


class DampedClosedLoopEvaluator(legacy.ClosedLoopEvaluator):
  def __init__(self, model: Any, batch: legacy.RolloutBatch, sample_period_s: float,
               wobble_weight: float, damping_gain: float):
    super().__init__(model, batch, sample_period_s, wobble_weight)
    self.damping_gain = damping_gain

  def rollout_many(self, tunes: list[tune_math.Tune]) -> list[legacy.RolloutTrace]:
    candidate_count = len(tunes)
    window_count = len(self.batch.history)
    history = np.tile(self.batch.history, (candidate_count, 1, 1))
    integral = np.tile(self.batch.initial_i.astype(np.float64), candidate_count)
    errors = []
    command_deltas = []
    commands = []
    states = []
    for step in range(self.batch.desired.shape[1]):
      desired = np.tile(self.batch.desired[:, step].astype(np.float64), candidate_count)
      jerk = np.tile(self.batch.jerk[:, step].astype(np.float64), candidate_count)
      speed = np.tile(self.batch.v_ego[:, step].astype(np.float64), candidate_count)
      actual = history[:, 0, self.base_index["actual_lateral_accel"]].astype(np.float64)
      previous_actual = history[:, 1, self.base_index["actual_lateral_accel"]].astype(np.float64)
      measurement_rate = np.clip((actual - previous_actual) / self.dt, -2.5, 2.5)
      kp = np.interp(speed, legacy.KP_SPEEDS, legacy.KP_VALUES)
      low_speed_factor = (
        np.interp(speed, legacy.LOW_SPEED_X, legacy.LOW_SPEED_Y) / np.maximum(speed, 0.3)
      ) ** 2
      error = desired - actual
      error_lsf = error * (1.0 + low_speed_factor / np.maximum(kp, 1e-3))
      p = kp * error_lsf
      integral += legacy.KI * self.dt * error_lsf
      d = -self.damping_gain * measurement_rate
      controller_output = np.empty_like(desired)
      for candidate_index, tune in enumerate(tunes):
        candidate = slice(candidate_index * window_count, (candidate_index + 1) * window_count)
        feedforward, factor = tune_math.tune_terms(
          tune, desired[candidate], jerk[candidate], speed[candidate], error_lsf[candidate],
        )
        controller_output[candidate] = np.clip(
          -(p[candidate] + integral[candidate] + d[candidate] + feedforward) / factor,
          -1.0, 1.0,
        )
      limiter_gap = np.tile(
        self.batch.logged_applied[:, step] - self.batch.logged_controller_output[:, step],
        candidate_count,
      )
      applied = np.clip(controller_output + limiter_gap, -1.0, 1.0)
      history[:, 0, self.base_index["applied_torque"]] = applied
      prediction_delta = self.model.predict(history.reshape((len(history), -1)))
      next_state = history[:, 0, self.state_indexes] + prediction_delta
      errors.append(
        next_state[:, 0] - np.tile(self.batch.target_desired[:, step], candidate_count),
      )
      command_deltas.append(
        applied - np.tile(self.batch.logged_applied[:, step], candidate_count),
      )
      commands.append(applied)
      states.append(next_state)

      next_base = history[:, 0].copy()
      next_base[:, self.base_index["v_ego"]] = np.tile(
        self.batch.v_ego[:, min(step + 1, self.batch.v_ego.shape[1] - 1)],
        candidate_count,
      )
      next_base[:, self.base_index["a_ego"]] = np.tile(
        self.batch.a_ego[:, min(step + 1, self.batch.a_ego.shape[1] - 1)],
        candidate_count,
      )
      next_base[:, self.state_indexes] = next_state
      history[:, 1:] = history[:, :-1]
      history[:, 0] = next_base
    combined = legacy.RolloutTrace(
      errors=np.stack(errors, axis=1).reshape((candidate_count, window_count, -1)),
      command_delta=np.stack(command_deltas, axis=1).reshape((candidate_count, window_count, -1)),
      commands=np.stack(commands, axis=1).reshape((candidate_count, window_count, -1)),
      states=np.stack(states, axis=1).reshape(
        (candidate_count, window_count, -1, len(self.state_indexes)),
      ),
    )
    return [
      legacy.RolloutTrace(
        combined.errors[index],
        combined.command_delta[index],
        combined.commands[index],
        combined.states[index],
      )
      for index in range(candidate_count)
    ]


def discover_trajectories(root: Path, prefixes: list[str],
                          sample_step: int) -> list[plant_data.Trajectory]:
  paths = sorted({
    path.resolve()
    for prefix in prefixes
    for path in root.glob(f"{prefix}--*/rlog.zst")
  })
  if not paths:
    raise SystemExit(f"No rlogs matched route prefixes: {', '.join(prefixes)}")
  trajectories: list[plant_data.Trajectory] = []
  for index, path in enumerate(paths, 1):
    trajectory = plant_data.read_trajectory(path, "hyundai", "IONIQ5", sample_step=sample_step)
    if trajectory is not None:
      desired = trajectory.values["desired_lateral_accel"]
      jerk = trajectory.values["desired_lateral_jerk"]
      if np.count_nonzero(np.abs(jerk) > 1e-6) < max(10, len(jerk) // 100):
        dt = np.maximum(np.gradient(trajectory.times), 1e-3)
        trajectory.values["desired_lateral_jerk"] = np.clip(
          np.gradient(desired) / dt, -2.5, 2.5,
        )
      trajectories.append(trajectory)
    if index % 10 == 0 or index == len(paths):
      print(f"loaded {index}/{len(paths)} rlogs; usable={len(trajectories)}", flush=True)
  return trajectories


def make_batch(trajectories: list[plant_data.Trajectory], history_steps: int,
               rollout_steps: int, max_windows: int, seed: int) -> legacy.RolloutBatch:
  routes = {trajectory.route for trajectory in trajectories}
  return legacy.build_batch(
    trajectories, routes, history_steps, rollout_steps, max_windows, seed,
  )


def tune_from_report(report: dict[str, Any], name: str) -> tune_math.Tune:
  values = dict(report["tune"])
  values["name"] = name
  return tune_math.Tune(**values)


def unique_candidates(history: list[dict[str, Any]], limit: int) -> list[tune_math.Tune]:
  ordered = sorted(history, key=lambda item: float(item["objective"]))
  seen: set[tuple[tuple[str, Any], ...]] = set()
  result: list[tune_math.Tune] = []
  for item in ordered:
    values = dict(item["tune"])
    values.pop("name", None)
    key = tuple(sorted(values.items()))
    if key in seen:
      continue
    seen.add(key)
    values["name"] = f"candidate_{len(result):02d}"
    result.append(tune_math.Tune(**values))
    if len(result) >= limit:
      break
  return result


def path_safe(candidate: dict[str, Any], baseline: dict[str, Any],
              max_regression: float) -> bool:
  phase_safe = all(
    candidate["phases"][name]["rmse"]
    <= baseline["phases"][name]["rmse"] * (1.0 + max_regression)
    for name in ("turn_in_left", "turn_in_right", "unwind_left", "unwind_right")
  )
  return (
    candidate["balanced_transition_rmse"]
    <= baseline["balanced_transition_rmse"] * (1.0 + max_regression)
    and candidate["phases"]["center"]["rmse"]
    <= baseline["phases"]["center"]["rmse"] * (1.0 + max_regression)
    and phase_safe
  )


def evaluate_with_uncertainty(evaluator: legacy.ClosedLoopEvaluator,
                              predictor: NeuralEnsemblePredictor,
                              tune: tune_math.Tune) -> dict[str, Any]:
  predictor.clear_disagreement()
  result = evaluator.evaluate(tune)
  result["plant_disagreement"] = predictor.disagreement_summary()
  return result


def evaluate_many_chunked(evaluator: legacy.ClosedLoopEvaluator,
                          tunes: list[tune_math.Tune],
                          chunk_size: int = 20) -> list[dict[str, Any]]:
  results: list[dict[str, Any]] = []
  for start in range(0, len(tunes), chunk_size):
    results.extend(evaluator.evaluate_many(tunes[start:start + chunk_size]))
  return results


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Tune the Ioniq 5 conventional controller through the neural plant ensemble.",
  )
  parser.add_argument("--plant-model", type=Path, default=DEFAULT_PLANT)
  parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
  parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
  parser.add_argument("--search-route-prefix", action="append", default=[])
  parser.add_argument("--validation-route-prefix", action="append", default=[])
  parser.add_argument("--acceptance-route-prefix", action="append", default=[])
  parser.add_argument("--rollout-steps", type=int, default=100)
  parser.add_argument("--max-search-windows", type=int, default=300)
  parser.add_argument("--max-validation-windows", type=int, default=400)
  parser.add_argument("--max-acceptance-windows", type=int, default=500)
  parser.add_argument("--validation-candidates", type=int, default=20)
  parser.add_argument("--wobble-weight", type=float, default=0.35)
  parser.add_argument("--max-path-regression", type=float, default=0.01)
  parser.add_argument("--max-acceptance-regression", type=float, default=0.02)
  parser.add_argument("--max-disagreement-regression", type=float, default=0.10)
  parser.add_argument("--damping-gain", type=float, action="append", default=[])
  parser.add_argument("--device", default="cuda")
  parser.add_argument("--random-state", type=int, default=23)
  args = parser.parse_args()
  if not args.search_route_prefix:
    args.search_route_prefix = ["00000112"]
  if not args.validation_route_prefix:
    args.validation_route_prefix = ["00000113"]
  if not args.acceptance_route_prefix:
    args.acceptance_route_prefix = ["00000109", "0000010b"]
  if not args.damping_gain:
    args.damping_gain = [0.0, 0.01, 0.02, 0.04, 0.08]
  if args.device.startswith("cuda") and not torch.cuda.is_available():
    raise SystemExit("CUDA was requested but is unavailable.")

  device = torch.device(args.device)
  predictor = NeuralEnsemblePredictor(args.plant_model, device)
  config = predictor.config
  search_trajectories = discover_trajectories(
    args.log_root, args.search_route_prefix, config.sample_step,
  )
  validation_trajectories = discover_trajectories(
    args.log_root, args.validation_route_prefix, config.sample_step,
  )
  acceptance_trajectories = discover_trajectories(
    args.log_root, args.acceptance_route_prefix, config.sample_step,
  )
  search_batch = make_batch(
    search_trajectories, config.history_steps, args.rollout_steps,
    args.max_search_windows, args.random_state,
  )
  validation_batch = make_batch(
    validation_trajectories, config.history_steps, args.rollout_steps,
    args.max_validation_windows, args.random_state + 1,
  )
  acceptance_batch = make_batch(
    acceptance_trajectories, config.history_steps, args.rollout_steps,
    args.max_acceptance_windows, args.random_state + 2,
  )
  window_summary = " ".join([
    f"device={device}",
    f"plant={config.name}",
    f"windows search={len(search_batch.history)}",
    f"validation={len(validation_batch.history)}",
    f"acceptance={len(acceptance_batch.history)}",
  ])
  print(window_summary, flush=True)

  baseline_tune = legacy.current_tune(None)
  search_evaluator = legacy.ClosedLoopEvaluator(
    predictor, search_batch, config.sample_period_s, args.wobble_weight,
  )
  _, history = legacy.optimize(
    search_evaluator, baseline_tune, args.max_path_regression,
  )
  search_baseline = search_evaluator.evaluate(baseline_tune)
  validation_evaluator = legacy.ClosedLoopEvaluator(
    predictor, validation_batch, config.sample_period_s, args.wobble_weight,
  )
  validation_baseline = validation_evaluator.evaluate(baseline_tune)
  candidates = unique_candidates(history, args.validation_candidates)
  candidate_search_results = evaluate_many_chunked(search_evaluator, candidates)
  validation_results = evaluate_many_chunked(validation_evaluator, candidates)
  eligible = [
    (candidate, search_result, validation_result)
    for candidate, search_result, validation_result
    in zip(candidates, candidate_search_results, validation_results, strict=True)
    if (
      path_safe(search_result, search_baseline, args.max_path_regression)
      and path_safe(validation_result, validation_baseline, args.max_path_regression)
    )
  ]
  if not eligible:
    raise RuntimeError("No candidate preserved transition and center tracking on the validation route.")
  selected, selected_search, selected_validation = min(
    eligible, key=lambda item: item[2]["objective"],
  )
  selected = replace(selected, name="neural_plant_optimized")
  selected_validation = validation_evaluator.evaluate(selected)
  selected_search = search_evaluator.evaluate(selected)

  acceptance_evaluator = legacy.ClosedLoopEvaluator(
    predictor, acceptance_batch, config.sample_period_s, args.wobble_weight,
  )
  acceptance_baseline = evaluate_with_uncertainty(
    acceptance_evaluator, predictor, baseline_tune,
  )
  selected_acceptance_undamped = acceptance_evaluator.evaluate(selected)
  if not path_safe(
    selected_acceptance_undamped, acceptance_baseline, args.max_acceptance_regression,
  ):
    selected = replace(baseline_tune, name="neural_plant_damping_base")
  damping_results: list[dict[str, Any]] = []
  damping_candidates: list[tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
  for gain in sorted(set(args.damping_gain)):
    damped_search_evaluator = DampedClosedLoopEvaluator(
      predictor, search_batch, config.sample_period_s, args.wobble_weight, gain,
    )
    damped_validation_evaluator = DampedClosedLoopEvaluator(
      predictor, validation_batch, config.sample_period_s, args.wobble_weight, gain,
    )
    damped_acceptance_evaluator = DampedClosedLoopEvaluator(
      predictor, acceptance_batch, config.sample_period_s, args.wobble_weight, gain,
    )
    search_result = damped_search_evaluator.evaluate(selected)
    validation_result = damped_validation_evaluator.evaluate(selected)
    acceptance_result = damped_acceptance_evaluator.evaluate(selected)
    safe = (
      path_safe(search_result, search_baseline, args.max_path_regression)
      and path_safe(validation_result, validation_baseline, args.max_path_regression)
      and path_safe(acceptance_result, acceptance_baseline, args.max_acceptance_regression)
    )
    damping_results.append({
      "gain": gain,
      "safe": safe,
      "search": search_result,
      "validation": validation_result,
      "acceptance": acceptance_result,
    })
    if safe:
      damping_candidates.append((gain, search_result, validation_result, acceptance_result))
  if not damping_candidates:
    raise RuntimeError("No damping gain preserved all phase-level path gates.")
  damping_gain, selected_search, selected_validation, _ = min(
    damping_candidates, key=lambda item: item[2]["objective"],
  )
  final_acceptance_evaluator = DampedClosedLoopEvaluator(
    predictor, acceptance_batch, config.sample_period_s, args.wobble_weight, damping_gain,
  )
  acceptance_selected = evaluate_with_uncertainty(
    final_acceptance_evaluator, predictor, selected,
  )
  accepted = (
    selected_validation["objective"] < validation_baseline["objective"]
    and path_safe(selected_validation, validation_baseline, args.max_path_regression)
    and path_safe(acceptance_selected, acceptance_baseline, args.max_acceptance_regression)
    and acceptance_selected["objective"]
    <= acceptance_baseline["objective"] * (1.0 + args.max_acceptance_regression)
    and acceptance_selected["plant_disagreement"]["p95"]
    <= acceptance_baseline["plant_disagreement"]["p95"] * (1.0 + args.max_disagreement_regression)
  )
  report = {
    "plant_model": str(args.plant_model),
    "plant_config": predictor.payload["config"],
    "sample_period_s": config.sample_period_s,
    "rollout_steps": args.rollout_steps,
    "rollout_seconds": args.rollout_steps * config.sample_period_s,
    "search_route_prefixes": args.search_route_prefix,
    "validation_route_prefixes": args.validation_route_prefix,
    "acceptance_route_prefixes": args.acceptance_route_prefix,
    "windows": {
      "search": len(search_batch.history),
      "validation": len(validation_batch.history),
      "acceptance": len(acceptance_batch.history),
    },
    "objective": {
      "wobble_weight": args.wobble_weight,
      "max_path_regression": args.max_path_regression,
      "max_acceptance_regression": args.max_acceptance_regression,
      "max_disagreement_regression": args.max_disagreement_regression,
    },
    "accepted": accepted,
    "recommended_damping_gain": damping_gain if accepted else 0.0,
    "damping_search": damping_results,
    "search": {
      "current": search_baseline,
      "optimized": selected_search,
    },
    "validation": {
      "current": validation_baseline,
      "optimized": selected_validation,
    },
    "acceptance": {
      "current": acceptance_baseline,
      "optimized": acceptance_selected,
    },
    "recommended_tune": asdict(selected) if accepted else asdict(baseline_tune),
    "search_evaluations": len(history),
    "validation_candidates": len(candidates),
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  print(json.dumps({
    "accepted": accepted,
    "validation": report["validation"],
    "acceptance": report["acceptance"],
    "recommended_tune": report["recommended_tune"],
    "recommended_damping_gain": report["recommended_damping_gain"],
  }, indent=2), flush=True)
  print(f"report: {args.output}", flush=True)
  if not accepted:
    raise SystemExit("No conventional tune passed all neural-plant gates.")


if __name__ == "__main__":
  main()
