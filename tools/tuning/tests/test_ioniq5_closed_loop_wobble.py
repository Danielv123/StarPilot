from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import numpy as np

from openpilot.tools.tuning import optimize_ioniq5_closed_loop as closed_loop
from openpilot.tools.tuning import optimize_ioniq5_neural_closed_loop as neural_closed_loop
from openpilot.tools.tuning import train_lateral_plant_model as plant


class ZeroPlant:
  def predict(self, values: np.ndarray) -> np.ndarray:
    return np.zeros((len(values), len(plant.STATE_FEATURES)))


def make_evaluator() -> closed_loop.ClosedLoopEvaluator:
  steps = 8
  feature_count = len(plant.BASE_FEATURES)
  history = np.zeros((1, 2, feature_count), dtype=np.float32)
  batch = closed_loop.RolloutBatch(
    history=history,
    desired=np.zeros((1, steps)),
    jerk=np.zeros((1, steps)),
    v_ego=np.full((1, steps), 20.0),
    a_ego=np.zeros((1, steps)),
    logged_applied=np.zeros((1, steps)),
    logged_controller_output=np.zeros((1, steps)),
    initial_i=np.zeros(1),
    target_desired=np.zeros((1, steps)),
    target_jerk=np.zeros((1, steps)),
    segments=["route--0"],
  )
  evaluator = closed_loop.ClosedLoopEvaluator.__new__(closed_loop.ClosedLoopEvaluator)
  evaluator.batch = batch
  evaluator.dt = 0.05
  evaluator.model = ZeroPlant()
  evaluator.base_index = {name: index for index, name in enumerate(plant.BASE_FEATURES)}
  evaluator.state_indexes = [evaluator.base_index[name] for name in plant.STATE_FEATURES]
  return evaluator


def make_trace(
  commands: list[float],
  steering_rates: list[float],
  actual_lateral_accels: list[float] | None = None,
) -> closed_loop.RolloutTrace:
  steps = len(commands)
  states = np.zeros((1, steps, len(plant.STATE_FEATURES)))
  states[0, :, plant.STATE_FEATURES.index("signed_steering_rate_deg_s")] = steering_rates
  if actual_lateral_accels is not None:
    states[0, :, plant.STATE_FEATURES.index("actual_lateral_accel")] = actual_lateral_accels
  return closed_loop.RolloutTrace(
    errors=np.zeros((1, steps)),
    command_delta=np.zeros((1, steps)),
    commands=np.asarray([commands], dtype=np.float64),
    states=states,
  )


def test_wobble_score_penalizes_alternating_closed_loop_motion():
  evaluator = make_evaluator()
  smooth = evaluator.wobble_metrics(make_trace([0.1] * 8, [2.0] * 8))
  wobbling = evaluator.wobble_metrics(make_trace([0.1, -0.1] * 4, [20.0, -20.0] * 4))
  assert wobbling["score"] > smooth["score"] * 5.0
  assert wobbling["quiet_command_slew_rms_per_s"] > smooth["quiet_command_slew_rms_per_s"]
  assert wobbling["quiet_steering_accel_rms_deg_s2"] > smooth["quiet_steering_accel_rms_deg_s2"]


def test_wobble_score_penalizes_rate_reversals_while_exiting_a_turn():
  evaluator = make_evaluator()
  evaluator.batch.v_ego[:] = 11.0
  evaluator.batch.target_desired[:] = np.asarray([[0.8, 0.7, 0.55, 0.4, 0.28, 0.18, 0.08, 0.02]])
  evaluator.batch.target_jerk[:] = -0.5
  smooth = evaluator.wobble_metrics(
    make_trace([0.20, 0.18, 0.15, 0.12, 0.09, 0.06, 0.03, 0.0],
               [20.0, 15.0, 10.0, 6.0, 3.0, 1.0, 0.0, 0.0]),
  )
  wobbling = evaluator.wobble_metrics(
    make_trace([0.20, -0.18, 0.16, -0.14, 0.12, -0.10, 0.08, -0.06],
               [20.0, -18.0, 16.0, -14.0, 12.0, -10.0, 8.0, -6.0]),
  )
  assert smooth["turn_exit_samples"] == 8
  assert smooth["turn_exit_left_samples"] == 8
  assert smooth["turn_exit_right_samples"] == 0
  assert wobbling["turn_exit_score"] > smooth["turn_exit_score"] * 2.0
  assert wobbling["turn_exit_rate_reversal_rms_deg_s"] > 0.0
  assert smooth["turn_exit_rate_reversal_rms_deg_s"] == 0.0


def test_shape_score_penalizes_highway_response_oscillation():
  evaluator = make_evaluator()
  evaluator.batch.v_ego[:] = 27.0
  evaluator.batch.target_desired[:] = 0.5
  evaluator.batch.target_jerk[:] = 0.0
  smooth = evaluator.shape_metrics(
    make_trace([0.1] * 8, [1.0] * 8, [0.02 * step for step in range(8)]),
  )
  oscillating = evaluator.shape_metrics(
    make_trace([0.1, -0.1] * 4, [15.0, -15.0] * 4, [0.10, -0.10] * 4),
  )
  assert smooth["highway_samples"] == 8
  assert oscillating["highway_score"] > smooth["highway_score"] * 5.0
  assert (
    oscillating["highway_shape_rate_error_rms_mps3"]
    > smooth["highway_shape_rate_error_rms_mps3"]
  )


def test_shape_score_penalizes_counter_motion_after_ramp_becomes_hold():
  evaluator = make_evaluator()
  evaluator.batch.v_ego[:] = 12.0
  evaluator.batch.target_desired[:] = np.asarray([[0.20, 0.24, 0.28, 0.32, 0.32, 0.32, 0.32, 0.32]])
  evaluator.batch.target_jerk[:] = np.asarray([[0.5, 0.5, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0]])
  smooth = evaluator.shape_metrics(
    make_trace([0.1] * 8, [1.0] * 8, [0.10, 0.14, 0.18, 0.22, 0.24, 0.25, 0.25, 0.25]),
  )
  dipping = evaluator.shape_metrics(
    make_trace([0.1, -0.1] * 4, [1.0, -1.0] * 4, [0.10, 0.14, 0.18, 0.22, 0.16, 0.13, 0.18, 0.22]),
  )
  assert smooth["ramp_hold_events"] == 1
  assert dipping["ramp_hold_events"] == 1
  assert dipping["ramp_hold_dip_mean_mps2"] > smooth["ramp_hold_dip_mean_mps2"]
  assert dipping["ramp_hold_score"] > smooth["ramp_hold_score"]


def test_route_prefix_selection_is_exact_and_repeatable():
  routes = ["00000102--abc", "00000105--def", "00000106--ghi"]
  assert closed_loop.routes_matching_prefixes(routes, ["00000102", "00000106"]) == {routes[0], routes[2]}


def test_validation_candidates_include_best_and_conservative_search_steps():
  history = []
  for index in range(10):
    values = asdict(closed_loop.CURRENT_CODE_TUNE)
    values["name"] = f"search_{index}"
    values["friction_jerk_gain"] = index * 0.01
    history.append({"objective": float(index), "tune": values})

  candidates = neural_closed_loop.unique_candidates(history, limit=4)
  gains = [candidate.friction_jerk_gain for candidate in candidates]

  assert gains[:2] == [0.0, 0.01]
  assert gains[-1] == 0.09


def test_rollout_batch_aligns_response_with_delayed_command_reference():
  rows = 12
  values = {
    name: np.zeros(rows, dtype=np.float32)
    for name in plant.BASE_FEATURES
  }
  values.update({
    "desired_lateral_accel": np.arange(rows, dtype=np.float32),
    "desired_lateral_jerk": np.arange(rows, dtype=np.float32),
    "controller_output": np.zeros(rows, dtype=np.float32),
    "controller_i": np.zeros(rows, dtype=np.float32),
    "lat_active": np.ones(rows, dtype=np.float32),
    "driver_overlay": np.zeros(rows, dtype=np.float32),
    "saturated": np.zeros(rows, dtype=np.float32),
  })
  values["v_ego"][:] = 10.0
  trajectory = plant.Trajectory(
    segment="route--0",
    route="route",
    brand="hyundai",
    car_fingerprint="HYUNDAI_IONIQ_5",
    times=np.arange(rows, dtype=np.float64) * 0.05,
    values=values,
    lateral_active_rows=rows,
    driver_overlay_rows=0,
  )

  batch = closed_loop.build_batch(
    [trajectory], {trajectory.route}, history_steps=2, rollout_steps=3,
    max_windows=100, seed=1, response_delay_steps=2,
  )

  assert np.array_equal(batch.desired[0], [2.0, 3.0, 4.0])
  assert np.array_equal(batch.target_desired[0], [1.0, 2.0, 3.0])
  assert np.array_equal(batch.jerk[0], [2.0, 3.0, 4.0])
  assert np.array_equal(batch.target_jerk[0], [1.0, 2.0, 3.0])
  assert np.array_equal(batch.response_jerk[0], [0.0, 1.0, 2.0])


def test_delay_aligned_damping_references_the_expected_response_rate():
  batch = make_evaluator().batch
  batch.response_jerk = np.ones_like(batch.jerk)
  baseline = neural_closed_loop.DampedClosedLoopEvaluator(
    ZeroPlant(), batch, 0.05, 0.35, 0.02,
  ).rollout(closed_loop.CURRENT_CODE_TUNE)
  aligned = neural_closed_loop.DampedClosedLoopEvaluator(
    ZeroPlant(), batch, 0.05, 0.35, 0.02, damping_reference_gain=1.0,
  ).rollout(closed_loop.CURRENT_CODE_TUNE)

  assert np.allclose(baseline.commands, 0.0)
  assert np.all(aligned.commands < 0.0)


def test_delay_aligned_damping_preserves_the_turn_exit_schedule():
  batch = make_evaluator().batch
  batch.v_ego[:] = 11.0
  batch.desired[:] = 0.5
  batch.jerk[:] = -0.5
  batch.response_jerk = np.ones_like(batch.jerk)
  baseline = neural_closed_loop.DampedClosedLoopEvaluator(
    ZeroPlant(), batch, 0.05, 0.35, 0.02,
  ).rollout(closed_loop.CURRENT_CODE_TUNE)
  aligned = neural_closed_loop.DampedClosedLoopEvaluator(
    ZeroPlant(), batch, 0.05, 0.35, 0.02, damping_reference_gain=1.0,
  ).rollout(closed_loop.CURRENT_CODE_TUNE)

  assert np.allclose(aligned.commands, baseline.commands)


def test_batched_rollout_matches_individual_rollout():
  evaluator = make_evaluator()
  individual = evaluator.rollout(closed_loop.tune_math.UPSTREAM_TUNE)
  batched = evaluator.rollout_many([closed_loop.tune_math.UPSTREAM_TUNE, closed_loop.tune_math.LOGGED_TUNE])[0]
  assert np.allclose(batched.errors, individual.errors)
  assert np.allclose(batched.commands, individual.commands)
  assert np.allclose(batched.states, individual.states)


def test_turn_exit_gate_allows_the_baseline_at_zero_regression():
  baseline = {
    "wobble": {
      "turn_exit_score": 1.0,
      "turn_exit_rate_reversal_rms_deg_s": 1.0,
      "turn_exit_steering_rate_rms_deg_s": 1.0,
    },
  }

  assert neural_closed_loop.turn_exit_safe(baseline, baseline, 0.0, 0.0)
  assert not neural_closed_loop.turn_exit_safe(baseline, baseline, 0.0, -0.0005)


def test_current_code_evaluator_uses_the_runtime_reversal_schedule():
  batch = make_evaluator().batch
  args = SimpleNamespace(
    baseline_damping_gain=0.02,
    baseline_reversal_damping_gain=0.0175,
    baseline_reversal_hold_seconds=0.60,
  )

  evaluator = neural_closed_loop.current_code_evaluator(
    ZeroPlant(), batch, 0.05, 0.35, args,
  )

  assert evaluator.damping_gain == 0.02
  assert evaluator.turn_exit_damping_gain == 0.02
  assert evaluator.turn_exit_damping_gain_right == 0.02
  assert evaluator.reversal_damping_gain == 0.0175
  assert evaluator.reversal_hold_seconds == 0.60
  assert evaluator.highway_damping_gain == 0.02
  assert evaluator.damping_reference_gain == 0.0


def test_promotion_improvement_distinguishes_noise_from_a_useful_gain():
  baseline = {
    "objective": 1.0,
    "wobble": {"turn_exit_rate_reversal_rms_deg_s": 1.0},
    "shape": {"highway_score": 1.0, "ramp_hold_score": 1.0},
  }
  noise = {
    "objective": 1.000001,
    "wobble": {"turn_exit_rate_reversal_rms_deg_s": 0.99986},
    "shape": {"highway_score": 0.9999, "ramp_hold_score": 0.9999},
  }
  useful = {
    "objective": 0.995,
    "wobble": {"turn_exit_rate_reversal_rms_deg_s": 0.995},
    "shape": {"highway_score": 0.995, "ramp_hold_score": 0.995},
  }

  noise_improvements = neural_closed_loop.promotion_improvements(
    noise, baseline, noise, baseline,
  )
  useful_improvements = neural_closed_loop.promotion_improvements(
    useful, baseline, useful, baseline,
  )

  assert max(noise_improvements.values()) < 0.004
  assert max(useful_improvements.values()) >= 0.004
