from __future__ import annotations

import numpy as np

from openpilot.tools.tuning import optimize_ioniq5_closed_loop as closed_loop
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


def make_trace(commands: list[float], steering_rates: list[float]) -> closed_loop.RolloutTrace:
  steps = len(commands)
  states = np.zeros((1, steps, len(plant.STATE_FEATURES)))
  states[0, :, plant.STATE_FEATURES.index("signed_steering_rate_deg_s")] = steering_rates
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


def test_route_prefix_selection_is_exact_and_repeatable():
  routes = ["00000102--abc", "00000105--def", "00000106--ghi"]
  assert closed_loop.routes_matching_prefixes(routes, ["00000102", "00000106"]) == {routes[0], routes[2]}


def test_batched_rollout_matches_individual_rollout():
  evaluator = make_evaluator()
  individual = evaluator.rollout(closed_loop.tune_math.UPSTREAM_TUNE)
  batched = evaluator.rollout_many([closed_loop.tune_math.UPSTREAM_TUNE, closed_loop.tune_math.LOGGED_TUNE])[0]
  assert np.allclose(batched.errors, individual.errors)
  assert np.allclose(batched.commands, individual.commands)
  assert np.allclose(batched.states, individual.states)
