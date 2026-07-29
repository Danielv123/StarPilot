#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

try:
  import torch
except ModuleNotFoundError as e:
  raise SystemExit(
    "Run with: uv run --no-project --with torch --with joblib --with scikit-learn " +
    "--with pycapnp==2.1.0 --with zstandard python " +
    "tools/tuning/compare_neural_lateral_plants.py"
  ) from e

from openpilot.tools.tuning import train_lateral_plant_model as plant_data
from openpilot.tools.tuning import train_neural_lateral_plant as neural_plant


FORMAT_VERSION = 1
HORIZONS_S = (0.10, 0.25, 0.50, 1.00, 2.00)


def sha256_file(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
      digest.update(chunk)
  return digest.hexdigest()


def git_value(*args: str) -> str | None:
  try:
    return subprocess.run(
      ("git", *args),
      cwd=REPO_ROOT,
      check=True,
      capture_output=True,
      text=True,
    ).stdout.strip()
  except (OSError, subprocess.CalledProcessError):
    return None


def config_shape(config: neural_plant.ModelConfig) -> dict[str, Any]:
  result = asdict(config)
  result.pop("name")
  return result


def window_hash(windows: neural_plant.WindowBatch, config: neural_plant.ModelConfig) -> str:
  digest = hashlib.sha256()
  header = {
    "format_version": 1,
    "sampling": {
      "sample_step": config.sample_step,
      "history_steps": config.history_steps,
    },
    "feature_names": list(plant_data.BASE_FEATURES),
    "state_feature_names": list(plant_data.STATE_FEATURES),
    "routes": windows.routes,
  }
  digest.update(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
  for values in (windows.history, windows.future_base, windows.target_states):
    little_endian = np.ascontiguousarray(values, dtype="<f4")
    digest.update(np.asarray(little_endian.shape, dtype="<i8").tobytes())
    digest.update(little_endian.tobytes())
  return digest.hexdigest()


def prediction_metrics(
  prediction: np.ndarray,
  disagreement: np.ndarray,
  windows: neural_plant.WindowBatch,
  shared_state_std: np.ndarray,
  sample_period_s: float,
) -> dict[str, Any]:
  target = windows.target_states
  error = prediction - target
  normalized = error / shared_state_std
  normalized_rmse = np.sqrt(np.mean(normalized ** 2, axis=(0, 1)))
  weights = np.asarray(neural_plant.HIGH_SPEED_STATE_WEIGHTS, dtype=np.float64)
  absolute_rmse = np.sqrt(np.mean(error ** 2, axis=(0, 1)))
  absolute_mae = np.mean(np.abs(error), axis=(0, 1))
  absolute_p95 = np.percentile(np.abs(error), 95, axis=(0, 1))
  normalized_disagreement = disagreement / shared_state_std

  result: dict[str, Any] = {
    "windows": len(windows),
    "score": float(np.average(normalized_rmse, weights=weights)),
    "finite": bool(np.isfinite(prediction).all() and np.isfinite(disagreement).all()),
    "finite_prediction_values": int(np.isfinite(prediction).sum()),
    "prediction_values": int(prediction.size),
    "max_abs_prediction": float(np.max(np.abs(prediction))),
    "absolute_rmse": {
      name: float(absolute_rmse[index])
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "absolute_mae": {
      name: float(absolute_mae[index])
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "absolute_p95_error": {
      name: float(absolute_p95[index])
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "normalized_rmse": {
      name: float(normalized_rmse[index])
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "mean_disagreement": {
      name: float(np.mean(disagreement[..., index]))
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "p95_disagreement": {
      name: float(np.percentile(disagreement[..., index], 95))
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "mean_normalized_disagreement": {
      name: float(np.mean(normalized_disagreement[..., index]))
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "p95_normalized_disagreement": {
      name: float(np.percentile(normalized_disagreement[..., index], 95))
      for index, name in enumerate(plant_data.STATE_FEATURES)
    },
    "speed_buckets": neural_plant.normalized_speed_metrics(
      normalized,
      windows.history[:, 0, neural_plant.V_EGO_INDEX],
      shared_state_std,
    ),
    "regimes": neural_plant.normalized_regime_metrics(
      normalized,
      windows.history,
      shared_state_std,
    ),
    "horizons": {},
    "routes": {},
  }
  for horizon_s in HORIZONS_S:
    step = round(horizon_s / sample_period_s) - 1
    if step < 0 or step >= error.shape[1]:
      continue
    horizon_error = error[:, step]
    result["horizons"][f"{horizon_s:.2f}s"] = {
      name: {
        "rmse": float(np.sqrt(np.mean(horizon_error[:, index] ** 2))),
        "mae": float(np.mean(np.abs(horizon_error[:, index]))),
        "p95_abs_error": float(np.percentile(np.abs(horizon_error[:, index]), 95)),
      }
      for index, name in enumerate(plant_data.STATE_FEATURES)
    }

  routes = np.asarray(windows.routes)
  for route in sorted(set(windows.routes)):
    selected = routes == route
    route_normalized_rmse = np.sqrt(
      np.mean(normalized[selected] ** 2, axis=(0, 1)),
    )
    result["routes"][route] = {
      "windows": int(np.sum(selected)),
      "score": float(np.average(route_normalized_rmse, weights=weights)),
      "normalized_rmse": {
        name: float(route_normalized_rmse[index])
        for index, name in enumerate(plant_data.STATE_FEATURES)
      },
    }
  return result


def evaluate_artifact(
  path: Path,
  windows: neural_plant.WindowBatch,
  shared_state_std: np.ndarray,
  device: torch.device,
) -> tuple[dict[str, Any], dict[str, Any]]:
  models, stats, payload = neural_plant.load_ensemble_artifact(path, device=device)
  config = neural_plant.ModelConfig(**payload["config"])
  batch_size = neural_plant.evaluation_batch_size(config)
  predictions: list[np.ndarray] = []
  disagreements: list[np.ndarray] = []
  with torch.no_grad():
    for start in range(0, len(windows), batch_size):
      end = min(start + batch_size, len(windows))
      history = torch.as_tensor(windows.history[start:end], device=device)
      future = torch.as_tensor(windows.future_base[start:end], device=device)
      prediction, disagreement = neural_plant.ensemble_rollout(
        models,
        history,
        future,
        stats,
        windows.target_states.shape[1],
      )
      predictions.append(prediction.cpu().numpy())
      disagreements.append(disagreement.cpu().numpy())
  metrics = prediction_metrics(
    np.concatenate(predictions),
    np.concatenate(disagreements),
    windows,
    shared_state_std,
    config.sample_period_s,
  )
  metadata = payload.get("metadata", {})
  identity = {
    "path": str(path.resolve()),
    "sha256": sha256_file(path),
    "bytes": path.stat().st_size,
    "model_type": payload.get("model_type"),
    "format_version": payload.get("format_version"),
    "config": payload["config"],
    "feature_names": payload.get("feature_names"),
    "state_feature_names": payload.get("state_feature_names"),
    "training_alignment": metadata.get("training_alignment"),
    "causal_training_eligible": metadata.get("causal_training_eligible"),
    "training_schema": metadata.get("training_schema"),
    "training_schema_version": metadata.get("training_schema_version"),
    "training_extraction_version": metadata.get(
      "training_extraction_version",
    ),
    "training_contract_version": metadata.get(
      "training_contract_version",
    ),
    "recursive_objective_horizon_s": metadata.get(
      "recursive_objective_horizon_s",
    ),
    "training_extractor_sha256": metadata.get("training_extractor_sha256"),
    "trainer_schema": metadata.get("trainer_schema"),
    "trainer_schema_version": metadata.get("trainer_schema_version"),
    "trainer_sha256": metadata.get("trainer_sha256"),
    "compatible_telemetry_extractor_sha256": metadata.get(
      "compatible_telemetry_extractor_sha256",
    ),
    "compatible_telemetry_extractor_version": metadata.get(
      "compatible_telemetry_extractor_version",
    ),
    "sampling": metadata.get("sampling"),
    "pretraining_performed": metadata.get("pretraining_performed"),
    "current_data": metadata.get("current_data"),
    "member_count": metadata.get(
      "member_count",
      len(payload.get("members", [])),
    ),
  }
  return metrics, identity


def metric_delta(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
  result: dict[str, Any] = {
    "score": candidate["score"] - baseline["score"],
    "score_percent": (
      100.0 * (candidate["score"] / baseline["score"] - 1.0)
      if baseline["score"] != 0.0
      else None
    ),
    "normalized_rmse": {},
    "absolute_rmse": {},
  }
  for group in ("normalized_rmse", "absolute_rmse"):
    for name in plant_data.STATE_FEATURES:
      old = baseline[group][name]
      new = candidate[group][name]
      result[group][name] = {
        "absolute": new - old,
        "percent": 100.0 * (new / old - 1.0) if old != 0.0 else None,
      }
  return result


def compatible_payload(
  path: Path,
  expected_config: neural_plant.ModelConfig,
) -> dict[str, Any]:
  payload = torch.load(path, map_location="cpu", weights_only=False)
  config = neural_plant.ModelConfig(**payload["config"])
  if config_shape(config) != config_shape(expected_config):
    raise ValueError(f"Model configuration differs for {path}.")
  if payload.get("feature_names") != list(plant_data.BASE_FEATURES):
    raise ValueError(f"Feature schema differs for {path}.")
  if payload.get("state_feature_names") != list(plant_data.STATE_FEATURES):
    raise ValueError(f"State schema differs for {path}.")
  return payload


def validate_candidate_metadata(metadata: dict[str, Any]) -> None:
  expected = {
    "training_alignment": "timestamp_causal_recorded_history_asof",
    "causal_training_eligible": True,
    "training_schema": "comma-companion.dynamics-row",
    "training_schema_version": 1,
    "training_extraction_version": plant_data.TRAJECTORY_EXTRACTION_VERSION,
    "training_contract_version": neural_plant.TRAINING_CONTRACT_VERSION,
    "recursive_objective_horizon_s": (
      neural_plant.RECURSIVE_OBJECTIVE_HORIZON_S
    ),
    "trainer_schema": "starpilot.neural-lateral-plant",
    "trainer_schema_version": neural_plant.TRAINER_SCHEMA_VERSION,
    "member_count": 3,
    "pretraining_performed": False,
  }
  for key, expected_value in expected.items():
    if metadata.get(key) != expected_value:
      raise ValueError(
        f"Candidate metadata {key} differs: " +
        f"{metadata.get(key)!r} != {expected_value!r}.",
      )


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Compare neural lateral plants on one identical causal window set.",
  )
  parser.add_argument("--baseline", type=Path, action="append", required=True)
  parser.add_argument("--candidate", type=Path, required=True)
  parser.add_argument(
    "--current-root",
    type=Path,
    default=plant_data.DEFAULT_LOG_ROOT,
  )
  parser.add_argument("--trajectory-cache", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  parser.add_argument(
    "--cohort",
    choices=("validation", "holdout", "both"),
    default="both",
  )
  parser.add_argument("--max-windows", type=int, default=30000)
  parser.add_argument("--seed", type=int, default=20260729)
  parser.add_argument("--workers", type=int, default=16)
  parser.add_argument(
    "--device",
    default="cuda" if torch.cuda.is_available() else "cpu",
  )
  return parser.parse_args()


def main() -> None:
  args = parse_args()
  candidate_payload = torch.load(
    args.candidate,
    map_location="cpu",
    weights_only=False,
  )
  candidate_config = neural_plant.ModelConfig(**candidate_payload["config"])
  candidate_metadata = candidate_payload.get("metadata", {})
  validate_candidate_metadata(candidate_metadata)
  compatible_hash = candidate_metadata.get(
    "compatible_telemetry_extractor_sha256",
  )
  if (
    not isinstance(compatible_hash, str)
    or len(compatible_hash) != 64
  ):
    raise ValueError(
      "Candidate does not embed a compatible telemetry extractor SHA-256.",
    )
  if not isinstance(
    candidate_metadata.get("compatible_telemetry_extractor_version"),
    str,
  ):
    raise ValueError(
      "Candidate does not embed a compatible telemetry extractor version.",
    )
  for path in args.baseline:
    compatible_payload(path, candidate_config)
  if candidate_payload.get("feature_names") != list(plant_data.BASE_FEATURES):
    raise ValueError("Candidate feature schema does not match the current extractor.")
  if candidate_payload.get("state_feature_names") != list(plant_data.STATE_FEATURES):
    raise ValueError("Candidate state schema does not match the current extractor.")

  trajectories, inventory = neural_plant.load_trajectories(
    args.current_root,
    args.trajectory_cache,
    args.workers,
    "hyundai",
    "IONIQ5",
  )
  trajectories, route_stats, excluded_routes = neural_plant.filter_high_overlay_routes(
    trajectories,
    0.50,
  )
  metadata = candidate_payload["metadata"]
  routes: set[str] = set()
  if args.cohort in ("validation", "both"):
    routes.update(metadata["validation_routes"])
  if args.cohort in ("holdout", "both"):
    routes.update(metadata["holdout_routes"])
  available_routes = {trajectory.route for trajectory in trajectories}
  missing_routes = routes - available_routes
  if missing_routes:
    raise ValueError(
      "Candidate cohort routes are unavailable: " + ", ".join(sorted(missing_routes)),
    )
  rollout_steps = round(
    metadata["anti_exploitation"]["rollout_validation_seconds"] /
    candidate_config.sample_period_s
  )
  windows = neural_plant.build_windows(
    trajectories,
    routes,
    candidate_config,
    rollout_steps,
    args.max_windows,
    args.seed,
    sample_with_replacement=False,
  )

  baseline_scale_payload = torch.load(
    args.baseline[0],
    map_location="cpu",
    weights_only=False,
  )
  shared_state_std = np.asarray(
    baseline_scale_payload["normalization"]["state_std"],
    dtype=np.float32,
  )
  device = torch.device(args.device)
  candidate_metrics, candidate_identity = evaluate_artifact(
    args.candidate,
    windows,
    shared_state_std,
    device,
  )
  if not candidate_metrics["finite"]:
    raise ValueError("Candidate produced non-finite comparison output.")
  baselines: list[dict[str, Any]] = []
  for path in args.baseline:
    baseline_metrics, baseline_identity = evaluate_artifact(
      path,
      windows,
      shared_state_std,
      device,
    )
    if not baseline_metrics["finite"]:
      raise ValueError(f"Baseline produced non-finite output: {path}")
    baselines.append({
      "artifact": baseline_identity,
      "metrics": baseline_metrics,
      "candidate_minus_baseline": metric_delta(
        candidate_metrics,
        baseline_metrics,
      ),
    })

  report = {
    "format_version": FORMAT_VERSION,
    "promotion_status": "candidate_only",
    "evaluator": {
      "source_commit": git_value("rev-parse", "HEAD"),
      "dirty_worktree": bool(git_value("status", "--short")),
      "script_sha256": sha256_file(Path(__file__)),
      "training_extractor_sha256": sha256_file(Path(plant_data.__file__)),
    },
    "data": {
      "inventory": inventory,
      "route_intervention_stats": route_stats,
      "excluded_routes": excluded_routes,
      "cohort": args.cohort,
      "routes": sorted(routes),
      "windows": len(windows),
      "window_set_sha256": window_hash(windows, candidate_config),
      "sampling_seed": args.seed,
      "sample_with_replacement": False,
    },
    "scoring": {
      "state_names": list(plant_data.STATE_FEATURES),
      "state_std": shared_state_std.tolist(),
      "state_std_source": str(args.baseline[0].resolve()),
      "weights": list(neural_plant.HIGH_SPEED_STATE_WEIGHTS),
    },
    "candidate": {
      "artifact": candidate_identity,
      "metrics": candidate_metrics,
    },
    "baselines": baselines,
    "compatibility": {
      "identical_model_shape": True,
      "identical_feature_order": True,
      "identical_state_order": True,
      "identical_window_set": True,
      "causal_extraction_version": plant_data.TRAJECTORY_EXTRACTION_VERSION,
      "training_contract_version": neural_plant.TRAINING_CONTRACT_VERSION,
      "recursive_objective_horizon_s": (
        neural_plant.RECURSIVE_OBJECTIVE_HORIZON_S
      ),
      "trainer_schema_version": neural_plant.TRAINER_SCHEMA_VERSION,
    },
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
  )
  print(json.dumps({
    "candidate": candidate_identity["sha256"],
    "candidate_score": candidate_metrics["score"],
    "baselines": [
      {
        "sha256": item["artifact"]["sha256"],
        "score": item["metrics"]["score"],
        "candidate_minus_baseline": item["candidate_minus_baseline"]["score"],
      }
      for item in baselines
    ],
    "windows": len(windows),
    "window_set_sha256": report["data"]["window_set_sha256"],
    "output": str(args.output),
  }, indent=2))


if __name__ == "__main__":
  main()
