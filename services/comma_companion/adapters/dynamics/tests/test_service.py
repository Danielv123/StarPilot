from __future__ import annotations

import io
import json
from typing import Any

import numpy as np
import pytest

from comma_companion_dynamics import cli
from comma_companion_dynamics.cli import serve
from comma_companion_dynamics.contract import (
  BASE_FEATURES,
  HISTORY_STEPS,
  STATE_FEATURES,
  causal_sampling_contract,
)
from comma_companion_dynamics.service import DynamicsService


class InfoPlant:
  member_count = 1
  history_steps = HISTORY_STEPS
  feature_names = BASE_FEATURES
  state_feature_names = STATE_FEATURES
  state_scale = np.ones(len(STATE_FEATURES))

  def provenance(self) -> dict:
    return {
      "artifact": "fixture",
      "member_count": 1,
      "training_alignment": "legacy_file_order_noncausal",
      "causal_training_eligible": False,
      "causal_input_eligible": False,
    }


class CausalInfoPlant(InfoPlant):
  def provenance(self) -> dict:
    return {
      **super().provenance(),
      "member_count": 3,
      "reviewed_member_count": 3,
      "promoted_artifact_verified": True,
      "review_registry_match": True,
      "review_manifest_verified": True,
      "review_manifest_sha256": "d" * 64,
      "training_alignment": "timestamp_causal_recorded_history_asof",
      "causal_training_eligible": True,
      "causal_input_eligible": True,
      "training_schema": "comma-companion.dynamics-row",
      "training_schema_version": 1,
      "training_extraction_version": 9,
      "training_extractor_sha256": "a" * 64,
      "training_contract_version": 10,
      "recursive_objective_horizon_s": 0.5,
      "trainer_schema": "starpilot.neural-lateral-plant",
      "trainer_schema_version": 8,
      "trainer_sha256": "c" * 64,
      "compatible_telemetry_extractor_version": "1.1.0",
      "compatible_telemetry_extractor_sha256": "b" * 64,
      "max_asof_age_ms": 35.0,
      "sampling": causal_sampling_contract(),
    }


class IncompleteCausalInfoPlant(CausalInfoPlant):
  def provenance(self) -> dict:
    result = super().provenance()
    result.pop("compatible_telemetry_extractor_sha256")
    return result


def test_json_line_cli_keeps_stdout_machine_readable() -> None:
  source = io.StringIO(
    json.dumps({"id": "a", "method": "ping"})
    + "\n"
    + "{bad json}\n"
    + json.dumps({"id": "b", "method": "model_info"})
    + "\n",
  )
  target = io.StringIO()
  serve(source, target, DynamicsService(plant=InfoPlant()))
  responses = [json.loads(line) for line in target.getvalue().splitlines()]
  assert responses[0] == {
    "id": "a",
    "ok": True,
    "result": {"protocol_version": 1, "status": "ok"},
  }
  assert responses[1]["error"]["code"] == "invalid_json"
  assert responses[2]["result"]["capabilities"]["apply_to_car"] is False
  assert responses[2]["result"]["capabilities"]["exact_baseline"] is False
  assert responses[2]["result"]["capabilities"]["causal_replay_eligible"] is False
  assert responses[2]["result"]["input_contract"]["max_asof_age_ms"] == 35.0
  assert responses[2]["result"]["input_contract"]["controller_i_timing"] == "post_update_asof_source_row"


def test_causal_capability_requires_the_complete_reviewed_provenance_contract() -> None:
  info = DynamicsService(plant=CausalInfoPlant()).model_info()
  assert info["capabilities"]["causal_replay_eligible"] is True
  assert (
    DynamicsService(plant=IncompleteCausalInfoPlant()).model_info()["capabilities"]["causal_replay_eligible"] is False
  )


def test_cli_bounds_and_drains_overlong_lines(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(cli, "MAX_REQUEST_BYTES", 64)
  source = io.StringIO(("x" * 200) + "\n" + json.dumps({"id": "next", "method": "ping"}) + "\n")
  target = io.StringIO()
  serve(source, target, DynamicsService(plant=InfoPlant()))
  responses = [json.loads(line) for line in target.getvalue().splitlines()]
  assert responses[0]["error"]["code"] == "request_too_large"
  assert responses[1]["id"] == "next"
  assert responses[1]["ok"] is True


def test_cli_recovers_from_excessively_nested_json() -> None:
  nested = ("[" * 10_000) + "0" + ("]" * 10_000)
  source = io.StringIO(nested + "\n" + json.dumps({"id": "next", "method": "ping"}) + "\n")
  target = io.StringIO()
  serve(source, target, DynamicsService(plant=InfoPlant()))
  responses = [json.loads(line) for line in target.getvalue().splitlines()]
  assert responses[0]["error"]["code"] == "invalid_json"
  assert responses[1]["id"] == "next"
  assert responses[1]["ok"] is True


def test_cli_rejects_nonfinite_json_and_invalid_responses() -> None:
  source = io.StringIO('{"id":NaN,"method":"ping"}\n{"id":Infinity,"method":"ping"}\n')
  target = io.StringIO()
  serve(source, target, DynamicsService(plant=InfoPlant()))
  responses = [json.loads(line) for line in target.getvalue().splitlines()]
  assert [response["error"]["code"] for response in responses] == ["invalid_json", "invalid_json"]

  class NonfiniteService:
    def handle(self, payload: Any) -> dict[str, float]:
      return {"value": float("nan")}

  nonfinite_target = io.StringIO()
  serve(
    io.StringIO('{"id":"bad","method":"ping"}\n'),
    nonfinite_target,
    NonfiniteService(),
  )
  response = json.loads(nonfinite_target.getvalue())
  assert response["error"]["code"] == "internal_error"
