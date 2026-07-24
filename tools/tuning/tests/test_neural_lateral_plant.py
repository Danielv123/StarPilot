from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from openpilot.tools.tuning import train_lateral_plant_model as plant_data
from openpilot.tools.tuning import train_ioniq5_nnff_neural_plant as nnff_policy
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


def test_mlp_applies_configured_dropout() -> None:
  config = neural_plant.ModelConfig("fixture", "mlp", 2, 10, (16, 16), dropout=0.25)
  model = neural_plant.build_model(config)
  assert sum(isinstance(module, torch.nn.Dropout) for module in model.modules()) == 2


def test_gru_rejects_multiple_hidden_sizes() -> None:
  with pytest.raises(ValueError, match="exactly one hidden size"):
    neural_plant.ModelConfig("fixture", "gru", 2, 10, (32, 16))


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
  assert all(candidate.history_s == 2.0 for candidate in neural_plant.ARCHITECTURE_CANDIDATES if candidate.sample_step == 1)


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


def test_search_candidates_use_shared_physical_sources() -> None:
  trajectory = synthetic_trajectory()
  dense = neural_plant.ModelConfig("dense", "gru", 1, 20, (8,), dropout=0.0)
  sparse = neural_plant.ModelConfig("sparse", "gru", 2, 10, (8,), dropout=0.0)
  configs = (dense, sparse)
  source_keys = neural_plant.sampled_source_keys(
    neural_plant.common_source_keys(
      [trajectory], {trajectory.route}, configs, rollout_seconds=0.2,
    ),
    cap=30,
    seed=5,
  )
  torque_index = plant_data.BASE_FEATURES.index("applied_torque")
  current_torque = []
  for config in configs:
    rollout_steps = round(0.2 / config.sample_period_s)
    windows = neural_plant.build_windows(
      [trajectory], {trajectory.route}, config, rollout_steps,
      cap=None, seed=5, source_keys=source_keys,
    )
    assert len(windows) == 30
    current_torque.append(windows.history[:, 0, torque_index])
  assert np.array_equal(current_torque[0], current_torque[1])


def test_recursive_training_horizon_is_clamped_to_available_rollout() -> None:
  config = neural_plant.ModelConfig("fixture", "gru", 2, 10, (8,), dropout=0.0)
  assert neural_plant.effective_rollout_train_steps(5.0, config, rollout_steps=100) == 100
  assert neural_plant.effective_rollout_train_steps(0.5, config, rollout_steps=100) == 25


def test_rollout_recomputes_unsigned_rate_from_predicted_signed_rate() -> None:
  class RecordingModel(torch.nn.Module):
    def __init__(self) -> None:
      super().__init__()
      self.inputs: list[torch.Tensor] = []

    def forward(self, values: torch.Tensor) -> torch.Tensor:
      self.inputs.append(values.detach().clone())
      return values.new_zeros((len(values), len(plant_data.STATE_FEATURES)))

  config = neural_plant.ModelConfig("fixture", "gru", 1, 2, (8,))
  model = RecordingModel()
  history = torch.zeros((1, config.history_steps, len(plant_data.BASE_FEATURES)))
  history[:, 0, neural_plant.SIGNED_STEERING_RATE_INDEX] = -3.0
  history[:, 0, neural_plant.STEERING_RATE_INDEX] = 3.0
  future = torch.zeros((1, 2, len(plant_data.BASE_FEATURES)))
  future[:, :, neural_plant.STEERING_RATE_INDEX] = 999.0
  stats = {
    "x_mean": torch.zeros(config.input_size),
    "x_std": torch.ones(config.input_size),
    "y_mean": torch.zeros(len(plant_data.STATE_FEATURES)),
    "y_std": torch.ones(len(plant_data.STATE_FEATURES)),
    "state_std": torch.ones(len(plant_data.STATE_FEATURES)),
  }
  neural_plant.rollout(model, history, future, stats, steps=2)
  assert model.inputs[1][0, neural_plant.STEERING_RATE_INDEX] == 3.0


def test_rollout_with_derived_rate_backpropagates() -> None:
  config = neural_plant.ModelConfig("fixture", "mlp", 1, 2, (8,), dropout=0.0)
  model = neural_plant.build_model(config)
  history = torch.zeros((2, config.history_steps, len(plant_data.BASE_FEATURES)))
  future = torch.zeros((2, 3, len(plant_data.BASE_FEATURES)))
  stats = {
    "x_mean": torch.zeros(config.input_size),
    "x_std": torch.ones(config.input_size),
    "y_mean": torch.zeros(len(plant_data.STATE_FEATURES)),
    "y_std": torch.ones(len(plant_data.STATE_FEATURES)),
    "state_std": torch.ones(len(plant_data.STATE_FEATURES)),
  }
  neural_plant.rollout(model, history, future, stats, steps=3).sum().backward()
  assert all(parameter.grad is not None for parameter in model.parameters())


def test_early_stopping_subset_is_seeded_and_not_prefix_biased() -> None:
  first = neural_plant.sampled_indexes(10_000, 5_000, 23)
  second = neural_plant.sampled_indexes(10_000, 5_000, 23)
  assert np.array_equal(first, second)
  assert not np.array_equal(first, np.arange(5_000))
  assert first.max() >= 5_000


def test_high_overlay_routes_are_excluded_before_splitting() -> None:
  clean = synthetic_trajectory(100)
  clean.route = "clean"
  clean.segment = "clean--0"
  biased = synthetic_trajectory(100)
  biased.route = "biased"
  biased.segment = "biased--0"
  biased.driver_overlay_rows = 51
  retained, stats, excluded = neural_plant.filter_high_overlay_routes([clean, biased], 0.50)
  assert excluded == ["biased"]
  assert stats["biased"]["driver_overlay_fraction"] == 0.51
  assert {trajectory.route for trajectory in retained} == {"clean"}


def test_trajectory_inventory_discovers_supported_rlog_encodings(tmp_path) -> None:
  for index, filename in enumerate(("rlog", "rlog.zst", "rlog.bz2")):
    segment = tmp_path / f"segment-{index}"
    segment.mkdir()
    (segment / filename).write_bytes(b"fixture")
  paths, inventory = neural_plant.trajectory_inventory(tmp_path)
  assert {path.name for path in paths} == {"rlog", "rlog.zst", "rlog.bz2"}
  assert inventory["rlog_count"] == 3


def test_trajectory_inventory_deduplicates_segment_encodings(tmp_path) -> None:
  segment = tmp_path / "segment"
  segment.mkdir()
  for filename in ("rlog", "rlog.zst", "rlog.bz2"):
    (segment / filename).write_bytes(b"fixture")
  paths, inventory = neural_plant.trajectory_inventory(tmp_path)
  assert [path.name for path in paths] == ["rlog"]
  assert inventory["rlog_count"] == 1


def test_search_profiles_use_distinct_report_paths(tmp_path) -> None:
  temporal = SimpleNamespace(output_dir=tmp_path, candidate_file=None, search_profile="temporal")
  architecture = SimpleNamespace(output_dir=tmp_path, candidate_file=None, search_profile="architecture")
  assert neural_plant.search_report_path(temporal).name == "temporal_search.json"
  assert neural_plant.search_report_path(architecture).name == "architecture_search.json"


def test_split_report_reuses_search_cohorts(tmp_path) -> None:
  report_path = tmp_path / "temporal_search.json"
  report_path.write_text(json.dumps({
    "data": {
      "current": {"rlog_count": 3, "rlog_bytes": 30, "newest_mtime_ns": 7},
      "train_routes": ["train"],
      "validation_routes": ["validation"],
      "holdout_routes": ["holdout"],
    },
  }), encoding="utf-8")
  split = neural_plant.split_from_report(
    report_path,
    {"train", "validation", "holdout", "unused"},
    {"rlog_count": 3, "rlog_bytes": 30, "newest_mtime_ns": 7},
  )
  assert split == ({"train"}, {"validation"}, {"holdout"})


def test_pretraining_note_is_generic_unless_explicitly_supplied() -> None:
  assert neural_plant.pretraining_note(SimpleNamespace(pretraining_note=None)) == (
    "Pretraining was not performed by this command."
  )
  assert neural_plant.pretraining_note(SimpleNamespace(pretraining_note="Inspected archive.")) == "Inspected archive."


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

  differentiable_models, _, _ = neural_plant.load_ensemble_artifact(
    artifact_path, differentiable=True,
  )
  assert all(model.training for model in differentiable_models)
  assert not any(parameter.requires_grad for model in differentiable_models for parameter in model.parameters())


def test_nnff_policy_offsets_preserve_runtime_horizons() -> None:
  assert nnff_policy.path_offsets(0.05) == (-6, -4, -2, 8, 14, 22, 32)
  assert nnff_policy.path_offsets(0.01) == (-30, -20, -10, 40, 70, 110, 160)


def test_nnff_policy_export_matches_runtime_schema() -> None:
  report = {
    "validation": {"optimized": {"rmse": 0.1}},
    "data": {
      "train_windows": 100,
      "validation_windows": 20,
      "holdout_windows": 30,
    },
  }
  policy = nnff_policy.legacy.FluxPolicy((4,), len(nnff_policy.INPUT_VARS))
  payload = nnff_policy.export_policy(
    policy,
    np.zeros(len(nnff_policy.INPUT_VARS)),
    np.ones(len(nnff_policy.INPUT_VARS)),
    report,
  )
  assert payload["input_size"] == len(nnff_policy.INPUT_VARS)
  assert payload["output_size"] == 1
  assert payload["input_vars"] == list(nnff_policy.INPUT_VARS)
  assert payload["input_vars"][-1] == "desired_curvature"
  assert payload["low_speed_angle_assist_gain"] == 0.0


def test_nnff_policy_regime_sampler_balances_rare_turns() -> None:
  desired = np.asarray([0.0] * 100 + [0.5] * 5 + [0.4] * 10 + [0.4] * 10 + [0.4] * 10)
  jerk = np.asarray([0.0] * 100 + [0.8] * 5 + [0.2] * 10 + [-0.2] * 10 + [0.0] * 10)
  speed = np.asarray([20.0] * 100 + [8.0] * 5 + [20.0] * 30)
  regimes = nnff_policy.classify_regime(desired, jerk, speed)
  selected = nnff_policy.balanced_indexes(regimes, 100, np.random.default_rng(7))
  counts = {
    name: int(np.count_nonzero(regimes[selected] == name))
    for name in nnff_policy.REGIMES
  }
  assert counts == dict.fromkeys(nnff_policy.REGIMES, 20)


def test_low_speed_windows_are_retained() -> None:
  trajectory = synthetic_trajectory()
  trajectory.values["v_ego"][:] = 1.0
  config = neural_plant.ModelConfig("fixture", "gru", 1, 20, (8,), dropout=0.0)
  sources, _, _ = neural_plant.eligible_sources(trajectory, config, rollout_steps=10)
  assert len(sources) > 0


def test_plant_state_weights_change_with_speed() -> None:
  weights = neural_plant.speed_conditioned_state_weights(torch.tensor([1.0, 20.0]))
  accel_index = plant_data.STATE_FEATURES.index("actual_lateral_accel")
  angle_index = plant_data.STATE_FEATURES.index("steering_angle_deg")
  assert weights[0, angle_index] > weights[1, angle_index]
  assert weights[0, accel_index] < weights[1, accel_index]


def test_speed_sampler_balances_available_buckets() -> None:
  speeds = np.concatenate([
    np.full(10, 1.0),
    np.full(10, 4.0),
    np.full(10, 6.0),
    np.full(10, 10.0),
    np.full(10, 20.0),
  ])
  selected = neural_plant.speed_stratified_indexes(speeds, 100, np.random.default_rng(3))
  counts = np.bincount(neural_plant.speed_bucket_indexes(speeds[selected]), minlength=5)
  assert counts.tolist() == [20, 20, 20, 20, 20]


def test_low_speed_curvature_identifies_intersection_turn_in() -> None:
  regimes = nnff_policy.classify_regime(
    desired=np.asarray([0.02]),
    jerk=np.asarray([0.01]),
    speed=np.asarray([2.0]),
    curvature=np.asarray([0.02]),
    curvature_rate=np.asarray([0.08]),
  )
  assert regimes.tolist() == ["sharp_turn_in"]
