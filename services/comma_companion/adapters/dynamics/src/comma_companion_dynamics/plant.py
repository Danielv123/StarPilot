from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import numpy as np

from comma_companion_dynamics.contract import (
  BASE_FEATURES,
  HISTORY_STEPS,
  MAX_ASOF_AGE_MS,
  RECURSIVE_OBJECTIVE_HORIZON_S,
  STATE_FEATURES,
  TRAINING_CONTRACT_VERSION,
  DynamicsContractError,
)

REFERENCE_MODEL_RELATIVE_PATH = Path(
  "artifacts/tuning/neural_lateral_plant_20260723/neural_lateral_plant.pt",
)
REFERENCE_MODEL_SHA256 = "fb1b8b951fdff19ff5f9349470415d003b61ee5655fff996365b429d93f6dc45"
MAX_REVIEW_MANIFEST_BYTES = 1024 * 1024
REQUIRED_REVIEW_QUALITY_CHECKS = frozenset(
  {
    "finite_predictions",
    "exact_shared_window_set",
    "member_count_3",
    "seeds_23_41_71",
    "horizons_1s_2s_all_states",
    "causal_extraction9_training_contract10_trainer8",
    "telemetry_compatibility_exact",
    "absolute_causal_quality_reviewed",
    "no_unacceptable_route_state_instability",
    "training_cohort_expanded_without_evaluation_leakage",
  }
)
REVIEW_QUALITY_THRESHOLDS = {
  "all_comparison_predictions_finite": True,
  "every_route_aggregate_score_max": 0.95,
  "horizon_absolute_error_caps": {
    "1.00s": {
      "actual_lateral_accel": {
        "p95_abs_error_max": 0.32,
        "rmse_max": 0.15,
      },
      "applied_eps_torque": {
        "p95_abs_error_max": 2.2,
        "rmse_max": 1.0,
      },
      "steering_angle_deg": {
        "p95_abs_error_max": 6.0,
        "rmse_max": 4.5,
      },
      "steering_rate_deg_s": {
        "p95_abs_error_max": 20.0,
        "rmse_max": 11.0,
      },
    },
    "2.00s": {
      "actual_lateral_accel": {
        "p95_abs_error_max": 0.5,
        "rmse_max": 0.25,
      },
      "applied_eps_torque": {
        "p95_abs_error_max": 2.3,
        "rmse_max": 1.05,
      },
      "steering_angle_deg": {
        "p95_abs_error_max": 9.0,
        "rmse_max": 7.0,
      },
      "steering_rate_deg_s": {
        "p95_abs_error_max": 23.0,
        "rmse_max": 11.5,
      },
    },
  },
  "overall_normalized_recursive_score_max": 0.65,
  "per_route_state_horizon_cap_multiplier": 1.5,
  "p95_normalized_ensemble_disagreement_max": {
    "actual_lateral_accel": 0.25,
    "applied_eps_torque": 0.35,
    "steering_angle_deg": 0.25,
    "steering_rate_deg_s": 0.40,
  },
}


@dataclass(frozen=True)
class ReviewedArtifact:
  relative_path: Path
  sha256: str
  training_alignment: str
  causal_training_eligible: bool
  review_status: str
  max_asof_age_ms: float
  member_count: int
  sampling_contract: dict[str, Any] | None = None
  training_schema: str | None = None
  training_schema_version: int | None = None
  training_extraction_version: int | None = None
  training_extractor_sha256: str | None = None
  training_contract_version: int | None = None
  recursive_objective_horizon_s: float | None = None
  trainer_schema: str | None = None
  trainer_schema_version: int | None = None
  trainer_sha256: str | None = None
  compatible_telemetry_extractor_version: str | None = None
  compatible_telemetry_extractor_sha256: str | None = None
  member_seeds: tuple[int, ...] | None = None
  fit_horizons_s: tuple[float, ...] | None = None
  review_manifest_relative_path: Path | None = None
  review_manifest_sha256: str | None = None


REVIEWED_ARTIFACTS: dict[str, ReviewedArtifact] = {
  REFERENCE_MODEL_SHA256: ReviewedArtifact(
    relative_path=REFERENCE_MODEL_RELATIVE_PATH,
    sha256=REFERENCE_MODEL_SHA256,
    training_alignment="legacy_file_order_noncausal",
    causal_training_eligible=False,
    review_status="legacy_promoted_blocked_for_causal_replay",
    max_asof_age_ms=MAX_ASOF_AGE_MS,
    member_count=3,
  ),
}
PROMOTED_MODEL_SHA256 = REFERENCE_MODEL_SHA256


class Plant(Protocol):
  member_count: int
  history_steps: int
  feature_names: tuple[str, ...]
  state_feature_names: tuple[str, ...]
  state_scale: np.ndarray

  def predict_member_deltas(self, histories: np.ndarray) -> np.ndarray: ...

  def predict_scenario_deltas(self, histories: np.ndarray) -> np.ndarray: ...

  def ood_metrics(self, histories: np.ndarray) -> dict[str, float]: ...

  def fit_envelope(self, horizon_s: float) -> dict[str, Any] | None: ...

  def provenance(self) -> dict[str, Any]: ...


def _repository_root() -> Path | None:
  relative_path = REVIEWED_ARTIFACTS[PROMOTED_MODEL_SHA256].relative_path
  for candidate in Path(__file__).resolve().parents:
    if (candidate / relative_path).is_file():
      return candidate
  return None


def default_model_path() -> Path:
  relative_path = REVIEWED_ARTIFACTS[PROMOTED_MODEL_SHA256].relative_path
  configured = os.environ.get("COMMA_DYNAMICS_MODEL")
  if configured:
    return Path(configured)
  repository = _repository_root()
  if repository is not None:
    return repository / relative_path
  return Path("/app") / relative_path


def _validation_fit_envelopes(
  payload: dict[str, Any],
) -> dict[float, dict[str, dict[str, float]]]:
  envelopes: dict[float, dict[str, dict[str, float]]] = {}
  metadata = payload.get("metadata", {})
  reports = metadata.get("member_reports", []) if isinstance(metadata, dict) else []
  for report in reports:
    if not isinstance(report, dict):
      continue
    validation = report.get("validation", {})
    horizons = validation.get("horizons", {}) if isinstance(validation, dict) else {}
    for label, state_metrics in horizons.items():
      try:
        horizon_s = float(str(label).removesuffix("s"))
      except ValueError:
        continue
      if not isinstance(state_metrics, dict):
        continue
      limits = envelopes.setdefault(horizon_s, {})
      for state_name, metrics in state_metrics.items():
        if not isinstance(metrics, dict):
          continue
        state_limits = limits.setdefault(state_name, {})
        for metric in ("rmse", "p95_abs_error"):
          value = metrics.get(metric)
          if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and np.isfinite(float(value))
            and float(value) >= 0.0
          ):
            state_limits[metric] = max(
              state_limits.get(metric, 0.0),
              float(value),
            )
  return envelopes


def _sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as source:
    while block := source.read(1024 * 1024):
      digest.update(block)
  return digest.hexdigest()


def _sha256_digest(value: Any) -> bool:
  return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _portable_relative_path(value: Any) -> bool:
  if not isinstance(value, str) or not value or "\\" in value:
    return False
  path = PurePosixPath(value)
  return (
    not path.is_absolute()
    and path.as_posix() == value
    and all(part not in ("", ".", "..") for part in path.parts)
    and all(":" not in part for part in path.parts)
  )


def _strict_json_object(encoded: bytes) -> dict[str, Any]:
  def reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")

  def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
      if key in result:
        raise ValueError(f"duplicate JSON key: {key}")
      result[key] = value
    return result

  value = json.loads(
    encoded.decode("utf-8"),
    parse_constant=reject_constant,
    object_pairs_hook=unique_object,
  )
  if not isinstance(value, dict):
    raise TypeError("top-level JSON value is not an object")
  return value


def _runtime_gru(torch: Any, nn: Any, config: dict[str, Any]) -> Any:
  feature_count = len(BASE_FEATURES)
  history_steps = int(config["history_steps"])
  hidden_size = int(config["hidden_sizes"][0])
  gru_layers = int(config.get("gru_layers", 1))
  dropout = float(config.get("dropout", 0.0))

  class RuntimeGRU(nn.Module):
    def __init__(self) -> None:
      super().__init__()
      self.gru = nn.GRU(
        feature_count,
        hidden_size,
        num_layers=gru_layers,
        batch_first=True,
        dropout=dropout if gru_layers > 1 else 0.0,
      )
      self.head = nn.Sequential(
        nn.Linear(hidden_size, hidden_size),
        nn.SiLU(),
        nn.Linear(hidden_size, len(STATE_FEATURES)),
      )

    def forward(self, values: Any) -> Any:
      sequence = values.reshape((-1, history_steps, feature_count)).flip(1)
      encoded, _ = self.gru(sequence)
      return self.head(encoded[:, -1])

  return RuntimeGRU()


class TorchPlantEnsemble:
  def __init__(
    self,
    path: Path,
    device: str = "cpu",
    require_reference_hash: bool = True,
  ):
    if not path.is_file():
      raise DynamicsContractError(
        "model_not_found",
        "The configured dynamics artifact does not exist.",
        {"path": str(path)},
      )
    try:
      import torch
      from torch import nn
    except ModuleNotFoundError as exc:
      raise DynamicsContractError(
        "model_runtime_unavailable",
        "PyTorch is required to load the dynamics artifact.",
      ) from exc
    self._torch = torch
    self._device = torch.device(device)
    configured_threads = os.environ.get("COMMA_DYNAMICS_TORCH_THREADS")
    try:
      inference_threads = int(configured_threads) if configured_threads else 1
    except ValueError as exc:
      raise DynamicsContractError(
        "invalid_runtime_config",
        "COMMA_DYNAMICS_TORCH_THREADS must be an integer.",
      ) from exc
    if inference_threads < 1:
      raise DynamicsContractError(
        "invalid_runtime_config",
        "COMMA_DYNAMICS_TORCH_THREADS must be at least one.",
      )
    torch.set_num_threads(inference_threads)
    self._inference_threads = inference_threads
    self._path = path.resolve()
    self._sha256 = _sha256(path)
    self._review = REVIEWED_ARTIFACTS.get(self._sha256)
    self._review_manifest_path: Path | None = None
    self._review_manifest: dict[str, Any] | None = None
    if self._review is not None and self._review.sha256 != self._sha256:
      raise DynamicsContractError(
        "model_review_registry_mismatch",
        "The reviewed model registry key and embedded review hash disagree.",
        {
          "registry_key": self._sha256,
          "review_sha256": self._review.sha256,
        },
      )
    if require_reference_hash and self._sha256 != PROMOTED_MODEL_SHA256:
      raise DynamicsContractError(
        "model_hash_mismatch",
        "The dynamics artifact does not match the explicitly promoted model.",
        {
          "expected_sha256": PROMOTED_MODEL_SHA256,
          "actual_sha256": self._sha256,
        },
      )
    try:
      safe_numpy_types = [
        np._core.multiarray._reconstruct,
        np.ndarray,
        np.dtype,
        np.dtypes.Float32DType,
        np.dtypes.Float64DType,
      ]
      with torch.serialization.safe_globals(
        safe_numpy_types,
      ):
        payload = torch.load(
          path,
          map_location=self._device,
          weights_only=True,
        )
    except Exception as exc:
      raise DynamicsContractError(
        "model_load_failed",
        "The dynamics artifact could not be loaded.",
        {"error_type": type(exc).__name__},
      ) from exc
    self._validate_payload(payload)
    self._validate_reviewed_metadata(payload)
    self._validate_review_manifest()
    self._payload = payload
    self._config = dict(payload["config"])
    self.feature_names = tuple(payload["feature_names"])
    self.state_feature_names = tuple(payload["state_feature_names"])
    self.history_steps = int(self._config["history_steps"])
    self.sample_period_s = int(self._config["sample_step"]) * 0.01
    self._models = []
    for state in payload["members"]:
      model = _runtime_gru(torch, nn, self._config).to(self._device)
      model.load_state_dict(state)
      model.eval()
      self._models.append(model)
    self.member_count = len(self._models)
    self._x_mean = torch.as_tensor(
      payload["normalization"]["x_mean"],
      dtype=torch.float32,
      device=self._device,
    )
    self._x_std = torch.as_tensor(
      payload["normalization"]["x_std"],
      dtype=torch.float32,
      device=self._device,
    )
    self._y_mean = torch.as_tensor(
      payload["normalization"]["y_mean"],
      dtype=torch.float32,
      device=self._device,
    )
    self._y_std = torch.as_tensor(
      payload["normalization"]["y_std"],
      dtype=torch.float32,
      device=self._device,
    )
    self.state_scale = np.asarray(
      payload["normalization"]["state_std"],
      dtype=np.float64,
    )
    self._fit_envelopes = _validation_fit_envelopes(payload)

  @staticmethod
  def _validate_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
      raise DynamicsContractError(
        "unsupported_model",
        "Only neural plant artifact format version 1 is supported.",
      )
    config = payload.get("config", {})
    problems = {}
    expected = {
      "model_type": "neural_controller_independent_lateral_plant_ensemble",
      "family": "gru",
      "sample_step": 1,
      "history_steps": HISTORY_STEPS,
      "feature_names": list(BASE_FEATURES),
      "state_feature_names": list(STATE_FEATURES),
    }
    actual = {
      "model_type": payload.get("model_type"),
      "family": config.get("family"),
      "sample_step": config.get("sample_step"),
      "history_steps": config.get("history_steps"),
      "feature_names": payload.get("feature_names"),
      "state_feature_names": payload.get("state_feature_names"),
    }
    for name, expected_value in expected.items():
      if actual[name] != expected_value:
        problems[name] = {"expected": expected_value, "actual": actual[name]}
    members = payload.get("members")
    if not isinstance(members, list) or len(members) < 2:
      problems["members"] = {
        "expected": "ensemble with at least two members",
        "actual": len(members) if isinstance(members, list) else 0,
      }
    normalization = payload.get("normalization")
    expected_normalization_shapes = {
      "x_mean": (HISTORY_STEPS * len(BASE_FEATURES),),
      "x_std": (HISTORY_STEPS * len(BASE_FEATURES),),
      "y_mean": (len(STATE_FEATURES),),
      "y_std": (len(STATE_FEATURES),),
      "state_std": (len(STATE_FEATURES),),
    }
    if not isinstance(normalization, dict):
      problems["normalization"] = {
        "expected": "object",
        "actual": type(normalization).__name__,
      }
    else:
      for name, expected_shape in expected_normalization_shapes.items():
        try:
          values = np.asarray(
            normalization[name],
            dtype=np.float64,
          )
        except (KeyError, TypeError, ValueError):
          problems[f"normalization.{name}"] = {
            "expected_shape": list(expected_shape),
            "actual": "missing_or_invalid",
          }
          continue
        require_positive = name in {
          "x_std",
          "y_std",
          "state_std",
        }
        if (
          values.shape != expected_shape
          or not np.isfinite(values).all()
          or (require_positive and np.any(values <= 0.0))
        ):
          problems[f"normalization.{name}"] = {
            "expected_shape": list(expected_shape),
            "actual_shape": list(values.shape),
            "finite": bool(np.isfinite(values).all()),
            "positive": (bool(np.all(values > 0.0)) if require_positive else None),
          }
    if problems:
      raise DynamicsContractError(
        "unsupported_model",
        "The dynamics artifact does not match the replay runtime contract.",
        problems,
      )

  def _validate_reviewed_metadata(self, payload: dict[str, Any]) -> None:
    review = self._review
    if review is None:
      return
    if len(payload["members"]) != review.member_count:
      raise DynamicsContractError(
        "model_review_metadata_mismatch",
        "The causal artifact ensemble size does not match its reviewed registry entry.",
        {
          "member_count": {
            "expected": review.member_count,
            "actual": len(payload["members"]),
          }
        },
      )
    if not review.causal_training_eligible:
      return
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
      raise DynamicsContractError(
        "model_review_metadata_mismatch",
        "The reviewed causal artifact has no metadata object.",
      )
    expected = {
      "causal_training_eligible": True,
      "training_alignment": review.training_alignment,
      "training_schema": review.training_schema,
      "training_schema_version": review.training_schema_version,
      "training_extraction_version": review.training_extraction_version,
      "training_extractor_sha256": review.training_extractor_sha256,
      "training_contract_version": review.training_contract_version,
      "recursive_objective_horizon_s": (review.recursive_objective_horizon_s),
      "trainer_schema": review.trainer_schema,
      "trainer_schema_version": review.trainer_schema_version,
      "trainer_sha256": review.trainer_sha256,
      "compatible_telemetry_extractor_version": (review.compatible_telemetry_extractor_version),
      "compatible_telemetry_extractor_sha256": review.compatible_telemetry_extractor_sha256,
      "sampling": {
        **(review.sampling_contract or {}),
      },
    }
    mismatches = {
      name: {
        "expected": value,
        "actual": metadata.get(name),
      }
      for name, value in expected.items()
      if metadata.get(name) != value
    }
    if review.training_contract_version != TRAINING_CONTRACT_VERSION:
      mismatches["reviewed_training_contract_version"] = {
        "expected": TRAINING_CONTRACT_VERSION,
        "actual": review.training_contract_version,
      }
    if review.recursive_objective_horizon_s != RECURSIVE_OBJECTIVE_HORIZON_S:
      mismatches["reviewed_recursive_objective_horizon_s"] = {
        "expected": RECURSIVE_OBJECTIVE_HORIZON_S,
        "actual": review.recursive_objective_horizon_s,
      }
    for name in (
      "training_schema_version",
      "training_extraction_version",
      "training_contract_version",
      "trainer_schema_version",
    ):
      value = metadata.get(name)
      if not isinstance(value, int) or isinstance(value, bool):
        mismatches[name] = {
          "expected": expected[name],
          "actual": value,
        }
    objective_horizon = metadata.get(
      "recursive_objective_horizon_s",
    )
    if (
      not isinstance(objective_horizon, (int, float))
      or isinstance(objective_horizon, bool)
      or not math.isfinite(float(objective_horizon))
      or float(objective_horizon) != RECURSIVE_OBJECTIVE_HORIZON_S
    ):
      mismatches["recursive_objective_horizon_s"] = {
        "expected": RECURSIVE_OBJECTIVE_HORIZON_S,
        "actual": objective_horizon,
      }
    member_count = metadata.get("member_count")
    if not isinstance(member_count, int) or isinstance(member_count, bool) or member_count != review.member_count:
      mismatches["member_count"] = {
        "expected": review.member_count,
        "actual": member_count,
      }
    member_reports = metadata.get("member_reports")
    if not isinstance(member_reports, list) or len(member_reports) != review.member_count:
      mismatches["member_reports"] = {
        "expected_count": review.member_count,
        "actual_count": len(member_reports) if isinstance(member_reports, list) else None,
      }
    elif review.member_seeds is None or len(review.member_seeds) != review.member_count:
      mismatches["reviewed_member_seeds"] = {
        "expected": f"{review.member_count} pinned seeds",
        "actual": review.member_seeds,
      }
    elif review.fit_horizons_s is None or not review.fit_horizons_s:
      mismatches["reviewed_fit_horizons_s"] = {
        "expected": "one or more pinned validation horizons",
        "actual": review.fit_horizons_s,
      }
    else:
      actual_seeds: list[Any] = []
      incomplete_metrics: list[dict[str, Any]] = []
      for member_index, report in enumerate(member_reports):
        fit = report.get("fit") if isinstance(report, dict) else None
        seed = fit.get("seed") if isinstance(fit, dict) else None
        actual_seeds.append(seed)
        if not isinstance(seed, int) or isinstance(seed, bool):
          continue
        if not isinstance(report, dict) or report.get("config") != payload.get("config"):
          incomplete_metrics.append(
            {
              "member_index": member_index,
              "field": "config",
            }
          )
          continue
        validation = report.get("validation")
        horizons = validation.get("horizons") if isinstance(validation, dict) else None
        if not isinstance(horizons, dict):
          incomplete_metrics.append(
            {
              "member_index": member_index,
              "field": "validation.horizons",
            }
          )
          continue
        parsed_horizons: dict[float, list[Any]] = {}
        for label, state_metrics in horizons.items():
          try:
            horizon_s = float(str(label).removesuffix("s"))
          except ValueError:
            continue
          if not math.isfinite(horizon_s):
            continue
          parsed_horizons.setdefault(horizon_s, []).append(state_metrics)
        for horizon_s in review.fit_horizons_s:
          matching = parsed_horizons.get(float(horizon_s), [])
          if len(matching) != 1 or not isinstance(matching[0], dict):
            incomplete_metrics.append(
              {
                "member_index": member_index,
                "horizon_s": horizon_s,
                "field": "validation.horizons",
              }
            )
            continue
          state_metrics = matching[0]
          for state_name in STATE_FEATURES:
            metrics = state_metrics.get(state_name)
            for metric_name in ("rmse", "p95_abs_error"):
              value = metrics.get(metric_name) if isinstance(metrics, dict) else None
              if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0.0
              ):
                incomplete_metrics.append(
                  {
                    "member_index": member_index,
                    "horizon_s": horizon_s,
                    "state": state_name,
                    "metric": metric_name,
                  }
                )
      if tuple(actual_seeds) != review.member_seeds or any(
        not isinstance(seed, int) or isinstance(seed, bool) for seed in actual_seeds
      ):
        mismatches["member_seeds"] = {
          "expected": list(review.member_seeds),
          "actual": actual_seeds,
        }
      if incomplete_metrics:
        mismatches["member_validation_metrics"] = {
          "expected": (
            "complete finite nonnegative rmse and p95_abs_error for every reviewed member, state, and pinned horizon"
          ),
          "actual": incomplete_metrics,
        }
    if mismatches:
      raise DynamicsContractError(
        "model_review_metadata_mismatch",
        "The causal artifact metadata does not match its reviewed registry entry.",
        mismatches,
      )

  def _validate_review_manifest(self) -> None:
    review = self._review
    if review is None or not review.causal_training_eligible:
      return

    def fail(
      field: str,
      expected: Any,
      actual: Any,
    ) -> None:
      raise DynamicsContractError(
        "model_review_manifest_mismatch",
        "The causal artifact review manifest does not match its reviewed registry entry.",
        {
          field: {
            "expected": expected,
            "actual": actual,
          }
        },
      )

    relative_path = review.review_manifest_relative_path
    if (
      relative_path is None
      or relative_path.is_absolute()
      or ".." in relative_path.parts
      or relative_path.suffix.lower() != ".json"
    ):
      fail(
        "review_manifest_relative_path",
        "safe repository-relative JSON path",
        str(relative_path) if relative_path is not None else None,
      )
    if not _sha256_digest(review.review_manifest_sha256):
      fail(
        "review_manifest_sha256",
        "lowercase 64-character SHA-256",
        review.review_manifest_sha256,
      )

    candidates = [parent / relative_path for parent in Path(__file__).resolve().parents]
    candidates.append(self._path.parent / relative_path.name)
    manifest_path = next(
      (candidate for candidate in candidates if candidate.is_file()),
      None,
    )
    if manifest_path is None:
      fail(
        "review_manifest",
        relative_path.as_posix(),
        "missing",
      )
    try:
      size = manifest_path.stat().st_size
      if size < 1 or size > MAX_REVIEW_MANIFEST_BYTES:
        fail(
          "review_manifest_bytes",
          f"1..{MAX_REVIEW_MANIFEST_BYTES}",
          size,
        )
      encoded = manifest_path.read_bytes()
    except OSError as exc:
      fail(
        "review_manifest_read",
        "readable regular file",
        type(exc).__name__,
      )
    actual_sha256 = hashlib.sha256(encoded).hexdigest()
    if actual_sha256 != review.review_manifest_sha256:
      fail(
        "review_manifest_sha256",
        review.review_manifest_sha256,
        actual_sha256,
      )
    try:
      manifest = _strict_json_object(encoded)
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError) as exc:
      fail(
        "review_manifest_json",
        "strict UTF-8 JSON object with unique keys",
        type(exc).__name__,
      )

    format_version = manifest.get("format_version")
    if not isinstance(format_version, int) or isinstance(format_version, bool) or format_version != 1:
      fail("format_version", 1, format_version)
    for field, expected in (
      ("promotion_status", "candidate_only"),
      ("selection_status", "selected_after_explicit_review"),
    ):
      if manifest.get(field) != expected:
        fail(field, expected, manifest.get(field))

    quality_gate = manifest.get("quality_gate")
    quality_checks = quality_gate.get("checks") if isinstance(quality_gate, dict) else None
    if not (
      isinstance(quality_gate, dict)
      and quality_gate.get("status") == "pass"
      and isinstance(quality_checks, dict)
      and set(quality_checks) == REQUIRED_REVIEW_QUALITY_CHECKS
      and all(value is True for value in quality_checks.values())
      and quality_gate.get("thresholds") == REVIEW_QUALITY_THRESHOLDS
    ):
      fail(
        "quality_gate",
        {
          "status": "pass",
          "checks": sorted(REQUIRED_REVIEW_QUALITY_CHECKS),
          "thresholds": REVIEW_QUALITY_THRESHOLDS,
        },
        quality_gate,
      )

    artifact = manifest.get("artifact")
    artifact_bytes = artifact.get("bytes") if isinstance(artifact, dict) else None
    if not (
      isinstance(artifact, dict)
      and artifact.get("path") == review.relative_path.as_posix()
      and artifact.get("sha256") == self._sha256
      and isinstance(artifact_bytes, int)
      and not isinstance(artifact_bytes, bool)
      and artifact_bytes == self._path.stat().st_size
    ):
      fail(
        "artifact",
        {
          "path": review.relative_path.as_posix(),
          "sha256": self._sha256,
          "bytes": self._path.stat().st_size,
        },
        artifact,
      )

    source = manifest.get("source")
    if not (
      isinstance(source, dict)
      and source.get("training_extractor_sha256") == review.training_extractor_sha256
      and source.get("trainer_sha256") == review.trainer_sha256
      and _sha256_digest(source.get("comparison_script_sha256"))
    ):
      fail(
        "source",
        {
          "training_extractor_sha256": review.training_extractor_sha256,
          "trainer_sha256": review.trainer_sha256,
          "comparison_script_sha256": "lowercase SHA-256",
        },
        source,
      )

    compatibility = manifest.get("compatibility")
    if not (
      isinstance(compatibility, dict)
      and compatibility.get("telemetry_extractor_version") == review.compatible_telemetry_extractor_version
      and compatibility.get("telemetry_extractor_sha256") == review.compatible_telemetry_extractor_sha256
    ):
      fail(
        "compatibility",
        {
          "telemetry_extractor_version": (review.compatible_telemetry_extractor_version),
          "telemetry_extractor_sha256": (review.compatible_telemetry_extractor_sha256),
        },
        compatibility,
      )

    def evidence_entry(
      field: str,
      *,
      require_bytes: bool = False,
      require_entries: bool = False,
    ) -> None:
      value = manifest.get(field)
      byte_count = value.get("bytes") if isinstance(value, dict) else None
      entry_count = value.get("entries") if isinstance(value, dict) else None
      valid = (
        isinstance(value, dict) and _portable_relative_path(value.get("path")) and _sha256_digest(value.get("sha256"))
      )
      if require_bytes:
        valid = valid and isinstance(byte_count, int) and not isinstance(byte_count, bool) and byte_count > 0
      if require_entries:
        valid = valid and isinstance(entry_count, int) and not isinstance(entry_count, bool) and entry_count > 0
      if not valid:
        fail(
          field,
          "portable relative path, lowercase SHA-256, and required positive counts",
          value,
        )

    evidence_entry("training_report", require_bytes=True)
    evidence_entry("quality_contract", require_bytes=True)
    evidence_entry("comparison_report", require_bytes=True)
    evidence_entry("split_report")
    evidence_entry("strict_extraction_audit")
    evidence_entry("grid_oracle_audit")
    evidence_entry("corpus_content_manifest", require_entries=True)

    data = manifest.get("data")
    if not (
      isinstance(data, dict)
      and _sha256_digest(data.get("inventory_manifest_sha256"))
      and _sha256_digest(data.get("window_set_sha256"))
      and "route_rejections" in data
      and "cohorts" in data
    ):
      fail(
        "data",
        "inventory/window SHA-256 values plus route_rejections and cohorts",
        data,
      )

    self._review_manifest_path = manifest_path.resolve()
    self._review_manifest = manifest

  def predict_member_deltas(self, histories: np.ndarray) -> np.ndarray:
    if histories.shape != (self.member_count, self.history_steps, len(self.feature_names)):
      raise DynamicsContractError(
        "invalid_model_input",
        "Plant histories have the wrong shape.",
        {
          "expected": [self.member_count, self.history_steps, len(self.feature_names)],
          "actual": list(histories.shape),
        },
      )
    return self.predict_scenario_deltas(histories[None, ...])[0]

  def predict_scenario_deltas(self, histories: np.ndarray) -> np.ndarray:
    if (
      histories.ndim != 4
      or histories.shape[0] < 1
      or histories.shape[1:] != (self.member_count, self.history_steps, len(self.feature_names))
    ):
      raise DynamicsContractError(
        "invalid_model_input",
        "Plant scenario histories have the wrong shape.",
        {
          "expected_suffix": [self.member_count, self.history_steps, len(self.feature_names)],
          "actual": list(histories.shape),
        },
      )
    # Use a fixed scenario batch of two. PyTorch's CPU GRU kernels can differ by
    # roughly 1e-6 when the same row is evaluated at batch size one versus two.
    # Padding odd chunks keeps model results invariant when a caller asks for a
    # baseline alone or for the normal baseline/candidate pair.
    chunks = []
    for start in range(0, len(histories), 2):
      chunk = histories[start : start + 2]
      valid_rows = len(chunk)
      if valid_rows == 1:
        chunk = np.concatenate((chunk, chunk), axis=0)
      chunks.append(self._predict_scenario_pair(chunk)[:valid_rows])
    result = np.concatenate(chunks, axis=0)
    if not np.isfinite(result).all():
      raise DynamicsContractError(
        "nonfinite_model_output",
        "The plant returned non-finite state deltas.",
      )
    return result

  def _predict_scenario_pair(self, histories: np.ndarray) -> np.ndarray:
    torch = self._torch
    values = torch.as_tensor(histories, dtype=torch.float32, device=self._device)
    predictions = []
    with torch.inference_mode():
      for member, model in enumerate(self._models):
        flat = values[:, member].flatten(1)
        normalized = (flat - self._x_mean) / self._x_std
        delta = model(normalized) * self._y_std + self._y_mean
        predictions.append(delta)
    return torch.stack(predictions, dim=1).cpu().numpy().astype(np.float64, copy=False)

  def ood_metrics(self, histories: np.ndarray) -> dict[str, float]:
    torch = self._torch
    values = torch.as_tensor(histories, dtype=torch.float32, device=self._device)
    normalized = ((values.flatten(1) - self._x_mean) / self._x_std).abs()
    result = {
      "max_abs_z": float(normalized.max().item()),
      "p99_abs_z": float(torch.quantile(normalized, 0.99).item()),
      "fraction_over_6": float((normalized > 6.0).float().mean().item()),
    }
    if not all(math.isfinite(value) and value >= 0.0 for value in result.values()):
      raise DynamicsContractError(
        "nonfinite_model_output",
        "The plant returned invalid OOD metrics.",
      )
    return result

  def fit_envelope(self, horizon_s: float) -> dict[str, Any] | None:
    if not self._fit_envelopes:
      return None
    available = sorted(self._fit_envelopes)
    selected = next(
      (candidate for candidate in available if candidate >= horizon_s - 1e-9),
      None,
    )
    if selected is None:
      return None
    selected_metrics = self._fit_envelopes[selected]
    if any(
      state not in selected_metrics
      or "rmse" not in selected_metrics[state]
      or "p95_abs_error" not in selected_metrics[state]
      for state in STATE_FEATURES
    ):
      return None
    return {
      "horizon_s": selected,
      "metric": "maximum_member_validation_rmse_and_p95_abs_error",
      "warning_limits": {
        state: metrics["rmse"] for state, metrics in self._fit_envelopes[selected].items() if "rmse" in metrics
      },
      "blocker_limits": {
        state: metrics["p95_abs_error"]
        for state, metrics in self._fit_envelopes[selected].items()
        if "p95_abs_error" in metrics
      },
      # Compatibility alias for callers that only understand the hard gate.
      "limits": {
        state: metrics["p95_abs_error"]
        for state, metrics in self._fit_envelopes[selected].items()
        if "p95_abs_error" in metrics
      },
    }

  def provenance(self) -> dict[str, Any]:
    metadata = self._payload.get("metadata", {})
    review = self._review
    return {
      "artifact": self._path.name,
      "sha256": self._sha256,
      "promoted_sha256": PROMOTED_MODEL_SHA256,
      "promoted_artifact_verified": self._sha256 == PROMOTED_MODEL_SHA256,
      "review_registry_match": review is not None,
      "review_status": review.review_status if review else "unreviewed",
      "review_manifest": (
        review.review_manifest_relative_path.as_posix()
        if review and review.review_manifest_relative_path is not None
        else None
      ),
      "review_manifest_sha256": (review.review_manifest_sha256 if review else None),
      "review_manifest_verified": (
        self._review_manifest is not None if review and review.causal_training_eligible else False
      ),
      "training_alignment": review.training_alignment if review else "unknown",
      "causal_training_eligible": (review.causal_training_eligible if review else False),
      # Compatibility alias. New callers should use causal_training_eligible
      # so telemetry eligibility is never confused with artifact eligibility.
      "causal_input_eligible": (review.causal_training_eligible if review else False),
      "training_schema": review.training_schema if review else None,
      "training_schema_version": (review.training_schema_version if review else None),
      "training_extraction_version": (review.training_extraction_version if review else None),
      "training_extractor_sha256": (review.training_extractor_sha256 if review else None),
      "training_contract_version": (review.training_contract_version if review else None),
      "recursive_objective_horizon_s": (review.recursive_objective_horizon_s if review else None),
      "trainer_schema": review.trainer_schema if review else None,
      "trainer_schema_version": (review.trainer_schema_version if review else None),
      "trainer_sha256": review.trainer_sha256 if review else None,
      "compatible_telemetry_extractor_version": (review.compatible_telemetry_extractor_version if review else None),
      "compatible_telemetry_extractor_sha256": (review.compatible_telemetry_extractor_sha256 if review else None),
      "max_asof_age_ms": review.max_asof_age_ms if review else None,
      "sampling": (dict(review.sampling_contract) if review and review.sampling_contract is not None else None),
      "format_version": self._payload["format_version"],
      "model_type": self._payload["model_type"],
      "config": self._config,
      "member_count": self.member_count,
      "reviewed_member_count": (review.member_count if review else None),
      "parameters_per_member": metadata.get("parameters_per_member"),
      "ensemble_parameters": metadata.get("ensemble_parameters"),
      "inference_threads": self._inference_threads,
      "feature_names": list(self.feature_names),
      "state_feature_names": list(self.state_feature_names),
      "reviewed_fit_envelopes": {
        f"{horizon_s:.2f}s": values for horizon_s, values in sorted(self._fit_envelopes.items())
      },
    }
