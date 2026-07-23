from __future__ import annotations

import numpy as np

from openpilot.tools.tuning import train_lateral_plant_model as plant


def make_trajectory(length: int = 12) -> plant.Trajectory:
  values = {
    name: np.linspace(0.0, 1.0, length, dtype=np.float32)
    for name in (*plant.BASE_FEATURES, *plant.DIAGNOSTIC_FIELDS)
  }
  values["lat_active"][:] = 1.0
  values["driver_overlay"][:] = 0.0
  values["saturated"][:] = 0.0
  values["v_ego"][:] = 15.0
  return plant.Trajectory(
    segment="route--0",
    route="route",
    brand="hyundai",
    car_fingerprint="HYUNDAI_IONIQ_5",
    times=np.arange(length, dtype=np.float64) * 0.05,
    values=values,
    lateral_active_rows=100,
    driver_overlay_rows=20,
  )


def test_predictor_features_exclude_controller_and_path_inputs():
  forbidden = {"desired_lateral_accel", "desired_lateral_jerk", "controller_i", "controller_output", "controller_error", "p", "i", "d", "f"}
  assert not forbidden.intersection(plant.BASE_FEATURES)
  assert set(plant.BASE_FEATURES) == {
    "applied_torque", "actual_lateral_accel", "steering_angle_deg", "steering_rate_deg", "signed_steering_rate_deg_s",
    "steering_torque_eps", "v_ego", "a_ego",
  }


def test_route_overlay_fraction_aggregates_complete_drive():
  first = make_trajectory()
  second = make_trajectory()
  second.segment = "route--1"
  second.lateral_active_rows = 100
  second.driver_overlay_rows = 90
  stats = plant.route_intervention_stats([first, second])
  assert stats["route"]["driver_overlay_fraction"] == 0.55


def test_training_target_is_next_timestamp_state_delta():
  trajectory = make_trajectory()
  x, y = plant.trajectory_samples(trajectory, history_steps=3)
  assert x.shape == (9, 3 * len(plant.BASE_FEATURES))
  assert y.shape == (9, len(plant.STATE_FEATURES))
  expected = trajectory.values["actual_lateral_accel"][3] - trajectory.values["actual_lateral_accel"][2]
  assert np.isclose(y[0, 0], expected)


def test_overlay_rows_are_removed_from_plant_samples():
  trajectory = make_trajectory()
  trajectory.values["driver_overlay"][5] = 1.0
  x, _ = plant.trajectory_samples(trajectory, history_steps=3)
  assert x.shape[0] == 7


def test_signed_steering_rate_preserves_direction_and_resets_across_gaps():
  angles = np.asarray([0.0, 1.0, 0.5, 3.0], dtype=np.float32)
  times = np.asarray([0.0, 0.05, 0.10, 0.30], dtype=np.float64)
  rate = plant.signed_steering_rate(angles, times)
  assert np.allclose(rate, [0.0, 20.0, -10.0, 0.0])


def test_derived_lateral_jerk_recovers_path_slope_and_clamps_spikes():
  times = np.arange(7, dtype=np.float64) * 0.05
  accels = np.asarray([0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 3.0], dtype=np.float32)
  jerk = plant.derived_lateral_jerk(accels, times)
  assert np.allclose(jerk[1:4], 1.0, atol=1e-5)
  assert jerk[-1] == plant.DERIVED_JERK_LIMIT


def test_forced_holdout_routes_are_never_used_for_training():
  trajectories = [make_trajectory() for _ in range(4)]
  for index, trajectory in enumerate(trajectories):
    trajectory.route = f"route-{index}"
    trajectory.segment = f"route-{index}--0"
  train, validation = plant.split_routes(trajectories, 0.25, 7, ("route-2",))
  assert "route-2" in validation
  assert "route-2" not in train
