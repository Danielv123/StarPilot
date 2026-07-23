from __future__ import annotations

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from openpilot.tools.tuning import train_lateral_plant_model as plant_data
from openpilot.tools.tuning import train_neural_lateral_plant as neural_plant


def synthetic_trajectory(rows: int = 500) -> plant_data.Trajectory:
  times = np.arange(rows, dtype=np.float64) * 0.01
  values = {
    name: np.zeros(rows, dtype=np.float32)
    for name in (*plant_data.BASE_FEATURES, *plant_data.DIAGNOSTIC_FIELDS)
  }
  values["lat_active"][:] = 1.0
  values["v_ego"][:] = 20.0
  values["applied_torque"] = np.linspace(-0.5, 0.5, rows, dtype=np.float32)
  values["actual_lateral_accel"] = 0.8 * values["applied_torque"]
  values["steering_angle_deg"] = 10.0 * values["applied_torque"]
  values["signed_steering_rate_deg_s"][1:] = np.diff(values["steering_angle_deg"]) / 0.01
  values["steering_rate_deg"] = np.abs(values["signed_steering_rate_deg_s"])
  values["steering_torque_eps"] = 100.0 * values["applied_torque"]
  return plant_data.Trajectory(
    segment="00000100--fixture--0",
    route="00000100--fixture",
    brand="hyundai",
    car_fingerprint="HYUNDAI_IONIQ_5",
    times=times,
    values=values,
    lateral_active_rows=rows,
    driver_overlay_rows=0,
  )


def test_baseline_parameter_count_matches_previous_surrogate() -> None:
  config = neural_plant.INITIAL_CANDIDATES[0]
  assert neural_plant.parameter_count(neural_plant.build_model(config)) == 37_444


def test_dense_candidates_expand_window_and_capacity() -> None:
  baseline = neural_plant.INITIAL_CANDIDATES[0]
  dense = next(candidate for candidate in neural_plant.INITIAL_CANDIDATES if candidate.name == "dense_2s_large_mlp")
  assert dense.sample_period_s < baseline.sample_period_s
  assert dense.history_s > baseline.history_s
  assert neural_plant.parameter_count(neural_plant.build_model(dense)) > neural_plant.parameter_count(
    neural_plant.build_model(baseline)
  )


def test_temporal_sweep_varies_interval_and_history_at_fixed_capacity() -> None:
  settings = {
    (candidate.sample_step, round(candidate.history_s, 1))
    for candidate in neural_plant.TEMPORAL_CANDIDATES
  }
  assert settings == {
    (sample_step, history_seconds)
    for sample_step in (1, 2, 5)
    for history_seconds in (0.5, 1.0, 1.5, 2.0, 3.0)
  }
  parameter_counts = {
    neural_plant.parameter_count(neural_plant.build_model(candidate))
    for candidate in neural_plant.TEMPORAL_CANDIDATES
  }
  assert len(parameter_counts) == 1


def test_architecture_sweep_includes_controls_and_sequence_families() -> None:
  families = {candidate.family for candidate in neural_plant.ARCHITECTURE_CANDIDATES}
  assert families == {"mlp", "gru", "tcn", "transformer"}
  assert any(candidate.sample_step == 2 for candidate in neural_plant.ARCHITECTURE_CANDIDATES)
  assert all(candidate.history_s == 3.0 for candidate in neural_plant.ARCHITECTURE_CANDIDATES if candidate.sample_step == 1)


def test_sequence_architectures_use_memory_safe_evaluation_batches() -> None:
  transformer = next(
    candidate for candidate in neural_plant.ARCHITECTURE_CANDIDATES
    if candidate.family == "transformer"
  )
  gru = next(candidate for candidate in neural_plant.ARCHITECTURE_CANDIDATES if candidate.family == "gru")
  assert neural_plant.evaluation_batch_size(transformer) < neural_plant.evaluation_batch_size(gru)


@pytest.mark.parametrize("family", ("tcn", "transformer"))
def test_sequence_architectures_return_state_delta(family: str) -> None:
  config = neural_plant.ModelConfig(
    "fixture",
    family,
    sample_step=2,
    history_steps=20,
    hidden_sizes=(32,),
    temporal_layers=2,
    attention_heads=4,
    feedforward_size=64,
  )
  model = neural_plant.build_model(config)
  inputs = torch.zeros((3, config.input_size))
  assert model(inputs).shape == (3, len(plant_data.STATE_FEATURES))


def test_build_windows_preserves_current_first_history() -> None:
  trajectory = synthetic_trajectory()
  config = neural_plant.ModelConfig("fixture", "mlp", 2, 20, (32,))
  windows = neural_plant.build_windows(
    [trajectory], {trajectory.route}, config, rollout_steps=10, cap=50, seed=1,
  )
  assert windows.history.shape == (50, 20, len(plant_data.BASE_FEATURES))
  assert windows.future_base.shape == (50, 10, len(plant_data.BASE_FEATURES))
  torque_index = plant_data.BASE_FEATURES.index("applied_torque")
  assert np.all(windows.history[:, 0, torque_index] >= windows.history[:, 1, torque_index])


def test_holdout_routes_are_never_selected_for_training() -> None:
  trajectories = [
    synthetic_trajectory()
    for _ in range(5)
  ]
  routes = [
    "000000f0--a",
    "000000f1--b",
    "000000f2--c",
    "00000109--holdout-a",
    "0000010b--holdout-b",
  ]
  for trajectory, route in zip(trajectories, routes, strict=True):
    trajectory.route = route
    trajectory.segment = route + "--0"
  training, validation, holdout = neural_plant.split_routes(
    trajectories, 0.25, ("00000109", "0000010b"), 7,
  )
  assert holdout == {"00000109--holdout-a", "0000010b--holdout-b"}
  assert not training & holdout
  assert not validation & holdout


def test_ensemble_prediction_exposes_disagreement() -> None:
  config = neural_plant.ModelConfig("fixture", "mlp", 2, 10, (16,))
  first = neural_plant.build_model(config)
  second = neural_plant.build_model(config)
  with torch.no_grad():
    for parameter in first.parameters():
      parameter.zero_()
    for parameter in second.parameters():
      parameter.fill_(0.01)
  history = torch.zeros((4, config.history_steps, len(plant_data.BASE_FEATURES)))
  stats = {
    "x_mean": torch.zeros(config.input_size),
    "x_std": torch.ones(config.input_size),
    "y_mean": torch.zeros(len(plant_data.STATE_FEATURES)),
    "y_std": torch.ones(len(plant_data.STATE_FEATURES)),
    "state_std": torch.ones(len(plant_data.STATE_FEATURES)),
  }
  mean, disagreement = neural_plant.ensemble_predict_delta([first, second], history, stats)
  assert mean.shape == (4, len(plant_data.STATE_FEATURES))
  assert torch.all(disagreement > 0)


def test_ensemble_rollout_exposes_temporal_disagreement() -> None:
  config = neural_plant.ModelConfig("fixture", "mlp", 2, 10, (16,))
  first = neural_plant.build_model(config)
  second = neural_plant.build_model(config)
  with torch.no_grad():
    for parameter in first.parameters():
      parameter.zero_()
    for parameter in second.parameters():
      parameter.fill_(0.01)
  batch_size = 4
  feature_count = len(plant_data.BASE_FEATURES)
  state_count = len(plant_data.STATE_FEATURES)
  history = torch.zeros((batch_size, config.history_steps, feature_count))
  future = torch.zeros((batch_size, 5, feature_count))
  stats = {
    "x_mean": torch.zeros(config.input_size),
    "x_std": torch.ones(config.input_size),
    "y_mean": torch.zeros(state_count),
    "y_std": torch.ones(state_count),
    "state_std": torch.ones(state_count),
  }
  mean, disagreement = neural_plant.ensemble_rollout(
    [first, second], history, future, stats, steps=5,
  )
  assert mean.shape == (batch_size, 5, state_count)
  assert disagreement.shape == mean.shape
  assert torch.all(disagreement > 0)


def test_ensemble_artifact_round_trip(tmp_path) -> None:
  config = neural_plant.ModelConfig("fixture", "gru", 2, 10, (16,), gru_layers=2)
  model = neural_plant.build_model(config)
  feature_count = len(plant_data.BASE_FEATURES)
  state_count = len(plant_data.STATE_FEATURES)
  artifact = {
    "format_version": neural_plant.FORMAT_VERSION,
    "config": {
      "name": config.name,
      "family": config.family,
      "sample_step": config.sample_step,
      "history_steps": config.history_steps,
      "hidden_sizes": config.hidden_sizes,
      "gru_layers": config.gru_layers,
    },
    "normalization": {
      "x_mean": np.zeros(config.history_steps * feature_count, dtype=np.float32),
      "x_std": np.ones(config.history_steps * feature_count, dtype=np.float32),
      "y_mean": np.zeros(state_count, dtype=np.float32),
      "y_std": np.ones(state_count, dtype=np.float32),
      "state_std": np.ones(state_count, dtype=np.float32),
    },
    "members": [neural_plant.state_dict_cpu(model)],
  }
  artifact_path = tmp_path / "plant.pt"
  torch.save(artifact, artifact_path)
  models, stats, payload = neural_plant.load_ensemble_artifact(artifact_path)
  assert payload["format_version"] == neural_plant.FORMAT_VERSION
  assert len(models) == 1
  assert stats["x_mean"].shape == (config.input_size,)
