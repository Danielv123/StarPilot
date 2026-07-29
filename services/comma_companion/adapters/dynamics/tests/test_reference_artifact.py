from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("torch")

from comma_companion_dynamics.contract import (
  BASE_FEATURES,
  HISTORY_STEPS,
  STATE_FEATURES,
  DynamicsContractError,
  causal_sampling_contract,
)
from comma_companion_dynamics.plant import (
  REFERENCE_MODEL_SHA256,
  REVIEW_QUALITY_THRESHOLDS,
  ReviewedArtifact,
  TorchPlantEnsemble,
  default_model_path,
)


def test_reference_artifact_loads_and_predicts_with_stable_contract() -> None:
  path = default_model_path()
  if not path.is_file():
    pytest.skip("Reference artifact is not present in this checkout.")
  plant = TorchPlantEnsemble(path)
  provenance = plant.provenance()
  assert provenance["sha256"] == REFERENCE_MODEL_SHA256
  assert provenance["training_alignment"] == "legacy_file_order_noncausal"
  assert provenance["causal_training_eligible"] is False
  assert provenance["causal_input_eligible"] is False
  assert provenance["max_asof_age_ms"] == 35.0
  assert provenance["promoted_artifact_verified"] is True
  envelope = plant.fit_envelope(1.0)
  assert envelope is not None
  assert envelope["horizon_s"] == 1.0
  assert set(envelope["limits"]) == set(STATE_FEATURES)
  assert plant.member_count == 3
  assert provenance["reviewed_member_count"] == 3
  assert plant.history_steps == HISTORY_STEPS
  history = np.zeros(
    (plant.member_count, HISTORY_STEPS, len(BASE_FEATURES)),
    dtype=np.float64,
  )
  prediction = plant.predict_member_deltas(history)
  assert prediction.shape == (plant.member_count, len(STATE_FEATURES))
  assert np.isfinite(prediction).all()
  scenarios = plant.predict_scenario_deltas(np.stack((history, history)))
  assert scenarios.shape == (2, plant.member_count, len(STATE_FEATURES))
  assert np.allclose(scenarios[0], prediction, rtol=1e-6, atol=1e-7)
  assert np.array_equal(scenarios[0], scenarios[1])


def _causal_review() -> ReviewedArtifact:
  return ReviewedArtifact(
    relative_path=Path("fixture.pt"),
    sha256="1" * 64,
    training_alignment="timestamp_causal_recorded_history_asof",
    causal_training_eligible=True,
    review_status="causal_reviewed",
    max_asof_age_ms=35.0,
    member_count=3,
    sampling_contract=causal_sampling_contract(),
    training_schema="comma-companion.dynamics-row",
    training_schema_version=1,
    training_extraction_version=9,
    training_extractor_sha256="2" * 64,
    training_contract_version=10,
    recursive_objective_horizon_s=0.5,
    trainer_schema="starpilot.neural-lateral-plant",
    trainer_schema_version=8,
    trainer_sha256="3" * 64,
    compatible_telemetry_extractor_version="1.1.0",
    compatible_telemetry_extractor_sha256="4" * 64,
    member_seeds=(23, 41, 71),
    fit_horizons_s=(1.0, 2.0),
  )


def _causal_payload(review: ReviewedArtifact) -> dict[str, Any]:
  config = {"name": "fixture"}

  def metrics() -> dict[str, dict[str, float]]:
    return {
      state: {
        "rmse": 0.1,
        "p95_abs_error": 0.2,
      }
      for state in STATE_FEATURES
    }

  member_reports = [
    {
      "config": config,
      "fit": {"seed": seed},
      "validation": {
        "horizons": {
          "1.00s": metrics(),
          "2.00s": metrics(),
        }
      },
    }
    for seed in review.member_seeds or ()
  ]
  return {
    "config": config,
    "members": [{}, {}, {}],
    "metadata": {
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
      "compatible_telemetry_extractor_sha256": (review.compatible_telemetry_extractor_sha256),
      "sampling": review.sampling_contract,
      "member_count": review.member_count,
      "member_reports": member_reports,
    },
  }


def _review_validator(review: ReviewedArtifact) -> TorchPlantEnsemble:
  validator = object.__new__(TorchPlantEnsemble)
  validator._review = review
  return validator


def _review_manifest(
  review: ReviewedArtifact,
  artifact_bytes: int,
) -> dict[str, Any]:
  evidence_sha256 = "5" * 64
  return {
    "format_version": 1,
    "promotion_status": "candidate_only",
    "selection_status": "selected_after_explicit_review",
    "quality_gate": {
      "status": "pass",
      "checks": {
        "finite_predictions": True,
        "exact_shared_window_set": True,
        "member_count_3": True,
        "seeds_23_41_71": True,
        "horizons_1s_2s_all_states": True,
        "causal_extraction9_training_contract10_trainer8": True,
        "telemetry_compatibility_exact": True,
        "absolute_causal_quality_reviewed": True,
        "no_unacceptable_route_state_instability": True,
        "training_cohort_expanded_without_evaluation_leakage": True,
      },
      "thresholds": REVIEW_QUALITY_THRESHOLDS,
    },
    "artifact": {
      "path": review.relative_path.as_posix(),
      "sha256": review.sha256,
      "bytes": artifact_bytes,
    },
    "training_report": {
      "path": "artifacts/review/training.json",
      "sha256": evidence_sha256,
      "bytes": 100,
    },
    "quality_contract": {
      "path": "artifacts/review/quality-contract.json",
      "sha256": evidence_sha256,
      "bytes": 100,
    },
    "comparison_report": {
      "path": "artifacts/review/comparison.json",
      "sha256": evidence_sha256,
      "bytes": 100,
    },
    "split_report": {
      "path": "artifacts/review/split.json",
      "sha256": evidence_sha256,
    },
    "strict_extraction_audit": {
      "path": "artifacts/review/strict-extraction.json",
      "sha256": evidence_sha256,
    },
    "grid_oracle_audit": {
      "path": "artifacts/review/grid-oracle.json",
      "sha256": evidence_sha256,
    },
    "corpus_content_manifest": {
      "path": "artifacts/review/corpus.json",
      "sha256": evidence_sha256,
      "entries": 64,
    },
    "source": {
      "training_extractor_sha256": review.training_extractor_sha256,
      "trainer_sha256": review.trainer_sha256,
      "comparison_script_sha256": evidence_sha256,
    },
    "compatibility": {
      "telemetry_extractor_version": (review.compatible_telemetry_extractor_version),
      "telemetry_extractor_sha256": (review.compatible_telemetry_extractor_sha256),
    },
    "data": {
      "inventory_manifest_sha256": evidence_sha256,
      "route_rejections": [],
      "cohorts": {},
      "window_set_sha256": evidence_sha256,
    },
  }


def test_causal_review_manifest_binds_artifact_and_quality_gate(
  tmp_path: Path,
) -> None:
  model_path = tmp_path / "fixture.pt"
  model_path.write_bytes(b"fixture model")
  artifact_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
  review = replace(
    _causal_review(),
    sha256=artifact_sha256,
    review_manifest_relative_path=Path("candidate_manifest.json"),
  )
  manifest = _review_manifest(review, model_path.stat().st_size)
  manifest_path = tmp_path / "candidate_manifest.json"
  encoded = (
    json.dumps(
      manifest,
      indent=2,
      sort_keys=True,
      ensure_ascii=False,
      allow_nan=False,
    )
    + "\n"
  ).encode()
  manifest_path.write_bytes(encoded)
  review = replace(
    review,
    review_manifest_sha256=hashlib.sha256(encoded).hexdigest(),
  )
  validator = _review_validator(review)
  validator._path = model_path.resolve()
  validator._sha256 = artifact_sha256
  validator._review_manifest_path = None
  validator._review_manifest = None
  validator._validate_review_manifest()
  assert validator._review_manifest_path == manifest_path.resolve()
  assert validator._review_manifest == manifest

  manifest["quality_gate"]["checks"]["absolute_causal_quality_reviewed"] = False
  rejected = (
    json.dumps(
      manifest,
      indent=2,
      sort_keys=True,
      ensure_ascii=False,
      allow_nan=False,
    )
    + "\n"
  ).encode()
  manifest_path.write_bytes(rejected)
  validator._review = replace(
    review,
    review_manifest_sha256=hashlib.sha256(rejected).hexdigest(),
  )
  with pytest.raises(DynamicsContractError) as error:
    validator._validate_review_manifest()
  assert error.value.code == "model_review_manifest_mismatch"


def test_causal_review_requires_complete_pinned_member_reports() -> None:
  review = _causal_review()
  payload = _causal_payload(review)
  _review_validator(review)._validate_reviewed_metadata(payload)

  for mutation in ("missing_member", "wrong_seed", "nonfinite_metric"):
    invalid = _causal_payload(review)
    if mutation == "missing_member":
      invalid["metadata"]["member_reports"].pop()
    elif mutation == "wrong_seed":
      invalid["metadata"]["member_reports"][1]["fit"]["seed"] = 99
    else:
      invalid["metadata"]["member_reports"][2]["validation"]["horizons"]["2.00s"]["actual_lateral_accel"]["rmse"] = (
        float("nan")
      )
    with pytest.raises(DynamicsContractError) as error:
      _review_validator(review)._validate_reviewed_metadata(invalid)
    assert error.value.code == "model_review_metadata_mismatch"


@pytest.mark.parametrize(
  "field",
  (
    "training_schema_version",
    "training_extraction_version",
    "training_contract_version",
    "trainer_schema_version",
    "member_count",
  ),
)
def test_causal_review_integer_metadata_rejects_boolean(field: str) -> None:
  review = _causal_review()
  payload = _causal_payload(review)
  payload["metadata"][field] = True
  with pytest.raises(DynamicsContractError) as error:
    _review_validator(review)._validate_reviewed_metadata(payload)
  assert error.value.code == "model_review_metadata_mismatch"


def test_causal_review_objective_horizon_rejects_boolean() -> None:
  review = _causal_review()
  payload = _causal_payload(review)
  payload["metadata"]["recursive_objective_horizon_s"] = True
  with pytest.raises(DynamicsContractError) as error:
    _review_validator(review)._validate_reviewed_metadata(
      payload,
    )
  assert error.value.code == "model_review_metadata_mismatch"
