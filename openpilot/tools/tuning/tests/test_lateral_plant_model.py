from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.tools.tuning import train_lateral_plant_model as plant


class FakeEvent:
  def __init__(self, service: str, log_mono_time: int, payload: object):
    self.logMonoTime = log_mono_time
    self._service = service
    setattr(self, service, payload)

  def which(self) -> str:
    return self._service


def fake_car_params(time_ns: int) -> FakeEvent:
  return FakeEvent("carParams", time_ns, SimpleNamespace(
    brand="hyundai",
    carFingerprint="HYUNDAI_IONIQ_5",
  ))


def fake_car_control(
  time_ns: int,
  torque: float = 0.1,
  curvature: float = 0.01,
) -> FakeEvent:
  return FakeEvent("carControl", time_ns, SimpleNamespace(
    latActive=True,
    actuators=SimpleNamespace(torque=torque, curvature=curvature),
  ))


def fake_car_output(time_ns: int, torque: float) -> FakeEvent:
  return FakeEvent("carOutput", time_ns, SimpleNamespace(
    actuatorsOutput=SimpleNamespace(torque=torque),
  ))


def fake_controls_state(
  time_ns: int,
  actual_lateral_accel: float = 0.3,
  desired_lateral_accel: float = 0.4,
  desired_lateral_jerk: float = 0.2,
) -> FakeEvent:
  torque_state = SimpleNamespace(
    actualLateralAccel=actual_lateral_accel,
    desiredLateralAccel=desired_lateral_accel,
    desiredLateralJerk=desired_lateral_jerk,
    output=0.1,
    i=0.01,
    saturated=False,
  )
  lateral_state = SimpleNamespace(
    which=lambda: "torqueState",
    torqueState=torque_state,
  )
  return FakeEvent("controlsState", time_ns, SimpleNamespace(
    lateralControlState=lateral_state,
  ))


def fake_car_state(
  time_ns: int,
  angle_deg: float,
  steering_torque_eps: float = 1.0,
  steering_pressed: bool = False,
) -> FakeEvent:
  return FakeEvent("carState", time_ns, SimpleNamespace(
    steeringAngleDeg=angle_deg,
    steeringRateDeg=abs(angle_deg),
    steeringTorqueEps=steering_torque_eps,
    vEgo=15.0,
    aEgo=0.0,
    steeringPressed=steering_pressed,
  ))


def fake_sentinel(time_ns: int, sentinel_type: str) -> FakeEvent:
  return FakeEvent(
    "sentinel", time_ns, SimpleNamespace(type=sentinel_type),
  )


def invalid(event: FakeEvent) -> FakeEvent:
  event.valid = False
  return event


def fake_streams(
  monkeypatch: pytest.MonkeyPatch,
  streams: dict[Path, list[FakeEvent]],
) -> None:
  monkeypatch.setattr(
    plant,
    "strict_stream_log_messages",
    lambda path: iter(streams[path]),
  )


def make_trajectory(length: int = 12) -> plant.Trajectory:
  values = {
    name: np.linspace(0.0, 1.0, length, dtype=np.float32)
    for name in (*plant.BASE_FEATURES, *plant.DIAGNOSTIC_FIELDS)
  }
  values["lat_active"][:] = 1.0
  values["driver_overlay"][:] = 0.0
  values["saturated"][:] = 0.0
  values["sample_valid"][:] = 1.0
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
  assert x.shape[0] == 5


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


def test_derived_lateral_jerk_is_prefix_causal():
  times = np.arange(20, dtype=np.float64) * 0.05
  baseline = np.linspace(0.0, 0.95, len(times), dtype=np.float32)
  changed_future = baseline.copy()
  changed_future[11:] += 20.0
  baseline_jerk = plant.derived_lateral_jerk(baseline, times)
  changed_jerk = plant.derived_lateral_jerk(changed_future, times)
  assert np.array_equal(baseline_jerk[:11], changed_jerk[:11])


def test_derived_jerk_is_separate_from_recorded_desired_jerk(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 900_000_000
  events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000),
    fake_car_output(time_ns - 20_000_000, 0.2),
  ]
  for index in range(20):
    events.append(fake_controls_state(
      time_ns + index * 10_000_000 - 1_000_000,
      desired_lateral_accel=index * 0.01,
      desired_lateral_jerk=0.0,
    ))
    events.append(
      fake_car_state(time_ns + index * 10_000_000, float(index)),
    )
  fake_streams(monkeypatch, {path: events})

  trajectory = plant.read_trajectory(path, "hyundai", "IONIQ5", sample_step=1)

  assert trajectory is not None
  assert not trajectory.values["desired_lateral_jerk"].any()
  assert np.allclose(trajectory.values["derived_lateral_jerk"][1:], 1.0)


def test_route_events_order_by_log_mono_time_then_source_ordinal(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 1_000_000_000
  events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000),
    fake_controls_state(time_ns - 20_000_000),
    fake_car_output(time_ns, 0.2),
    fake_car_state(time_ns, 0.0),
    fake_car_output(time_ns, 0.8),
    fake_car_state(time_ns, 0.1),
  ]
  events.extend(
    fake_car_state(time_ns + index * 10_000_000, float(index))
    for index in range(1, 20)
  )
  fake_streams(monkeypatch, {path: events})

  first = plant.read_trajectory(path, "hyundai", "IONIQ5", sample_step=1)
  second = plant.read_trajectory(path, "hyundai", "IONIQ5", sample_step=1)

  assert first is not None
  assert second is not None
  assert first.values["applied_torque"][0] == pytest.approx(0.8)
  assert first.values["steering_angle_deg"][0] == pytest.approx(0.1)
  assert np.array_equal(first.times, second.times)
  for name in first.values:
    assert np.array_equal(first.values[name], second.values[name])


def test_future_service_updates_do_not_change_past_rows(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 2_000_000_000
  future_time_ns = time_ns + 150_000_000
  # Future-dated updates deliberately precede the earlier carState rows in
  # serialized source order.
  events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000),
    fake_car_output(time_ns - 20_000_000, 0.2),
    fake_controls_state(time_ns - 20_000_000, actual_lateral_accel=0.3),
    fake_car_output(future_time_ns, 0.9),
    fake_controls_state(future_time_ns, actual_lateral_accel=9.0),
  ]
  events.extend(
    fake_car_state(time_ns + index * 10_000_000, float(index))
    for index in range(20)
  )
  fake_streams(monkeypatch, {path: events})

  trajectory = plant.read_trajectory(path, "hyundai", "IONIQ5", sample_step=1)

  assert trajectory is not None
  assert np.all(trajectory.values["applied_torque"][:15] == np.float32(0.2))
  assert np.all(trajectory.values["actual_lateral_accel"][:15] == np.float32(0.3))
  assert np.all(trajectory.values["applied_torque"][15:] == np.float32(0.9))
  assert np.all(trajectory.values["actual_lateral_accel"][15:] == np.float32(9.0))


def test_later_ordinal_service_history_is_joined_by_earlier_timestamp(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 2_250_000_000
  events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000),
    fake_car_output(time_ns - 20_000_000, 0.2),
    fake_controls_state(time_ns - 20_000_000),
  ]
  # All anchors are serialized before this output update, even though the
  # update timestamp is before the final anchors.
  events.extend(
    fake_car_state(time_ns + index * 10_000_000, float(index))
    for index in range(20)
  )
  events.append(fake_car_output(time_ns + 95_000_000, 0.9))
  fake_streams(monkeypatch, {path: events})

  trajectory = plant.read_trajectory(path, "hyundai", "IONIQ5", sample_step=1)

  assert trajectory is not None
  assert trajectory.values["applied_torque"][9] == pytest.approx(0.2)
  assert trajectory.values["applied_torque"][10] == pytest.approx(0.9)


def test_grid_samples_each_service_independently_after_selected_car_state(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 2_400_000_000
  events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000),
    fake_car_output(time_ns - 20_000_000, 0.2),
    fake_controls_state(time_ns - 20_000_000),
  ]
  # At the first absolute tick (t+10 ms), carState is selected from t+1 ms
  # while carOutput must independently select the later t+5 ms update.
  events.extend(
    fake_car_state(time_ns + 1_000_000 + index * 10_000_000, float(index))
    for index in range(21)
  )
  events.append(fake_car_output(time_ns + 5_000_000, 0.9))
  fake_streams(monkeypatch, {path: events})

  trajectory = plant.read_trajectory(path, "hyundai", "IONIQ5", sample_step=1)

  assert trajectory is not None
  assert trajectory.times[0] == pytest.approx(time_ns / 1e9 + 0.01)
  assert trajectory.values["applied_torque"][0] == pytest.approx(0.9)
  assert trajectory.values["source_age_ms"][0] == pytest.approx(9.0)
  assert trajectory.values["car_output_age_ms"][0] == pytest.approx(5.0)


def test_joined_control_sources_must_be_at_most_35ms_old(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 2_500_000_000
  events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000),
    fake_car_output(time_ns - 20_000_000, 0.2),
    fake_controls_state(time_ns - 20_000_000),
  ]
  events.extend(
    fake_car_state(time_ns + index * 10_000_000, float(index))
    for index in range(20)
  )
  fake_streams(monkeypatch, {path: events})

  trajectory = plant.read_trajectory(path, "hyundai", "IONIQ5", sample_step=1)

  assert trajectory is not None
  assert trajectory.values["sample_valid"][:2].tolist() == [1.0, 1.0]
  assert not trajectory.values["sample_valid"][2:].any()


def test_requested_torque_never_substitutes_for_missing_car_output(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 2_750_000_000
  events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 10_000_000, torque=0.95),
    fake_controls_state(time_ns - 10_000_000),
  ]
  events.extend(
    fake_car_state(time_ns + index * 10_000_000, float(index))
    for index in range(20)
  )
  fake_streams(monkeypatch, {path: events})

  trajectory = plant.read_trajectory(path, "hyundai", "IONIQ5", sample_step=1)

  assert trajectory is not None
  assert np.isnan(trajectory.values["applied_torque"]).all()
  assert trajectory.values["requested_torque"][0] == pytest.approx(0.95)
  assert not trajectory.values["sample_valid"].any()


def test_invalid_events_never_supply_anchor_or_joined_state(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 2_900_000_000
  wrong_identity = fake_car_params(time_ns - 40_000_000)
  wrong_identity.carParams.brand = "invalid"
  events = [
    invalid(wrong_identity),
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000),
    fake_car_output(time_ns - 20_000_000, 0.2),
    fake_controls_state(time_ns - 20_000_000),
    invalid(fake_car_output(time_ns + 55_000_000, 8.0)),
    invalid(fake_car_state(time_ns + 70_000_000, 999.0)),
    fake_car_output(time_ns + 125_000_000, 0.7),
  ]
  events.extend(
    fake_car_state(time_ns + index * 10_000_000, float(index))
    for index in range(25) if index != 7
  )
  fake_streams(monkeypatch, {path: events})

  trajectory = plant.read_trajectory(path, "hyundai", "IONIQ5", sample_step=1)

  assert trajectory is not None
  assert trajectory.values["steering_angle_deg"][7] == pytest.approx(6.0)
  assert trajectory.values["source_age_ms"][7] == pytest.approx(10.0)
  assert trajectory.values["applied_torque"][5] == pytest.approx(0.2)
  assert np.isnan(trajectory.values["applied_torque"][6:13]).all()
  assert trajectory.values["applied_torque"][13] == pytest.approx(0.7)


def test_corrupt_event_stream_rejects_route_instead_of_returning_partial_rows(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 2_950_000_000

  def corrupt_stream(_path):
    yield fake_car_params(time_ns - 30_000_000)
    yield fake_car_control(time_ns - 20_000_000)
    raise RuntimeError("corrupt union message")

  monkeypatch.setattr(plant, "strict_stream_log_messages", corrupt_stream)

  with pytest.raises(RuntimeError, match="corrupt union message"):
    plant.read_trajectory(path, "hyundai", "IONIQ5", sample_step=1)


def test_malformed_decoded_event_header_rejects_route(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 2_975_000_000

  class MalformedEvent:
    def which(self) -> str:
      return "carState"

    @property
    def logMonoTime(self) -> int:
      raise ValueError("malformed decoded event")

  events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000),
    fake_car_output(time_ns - 20_000_000, 0.2),
    fake_controls_state(time_ns - 20_000_000),
    MalformedEvent(),
  ]
  events.extend(
    fake_car_state(time_ns + index * 10_000_000, float(index))
    for index in range(20)
  )
  monkeypatch.setattr(
    plant,
    "strict_stream_log_messages",
    lambda _path: iter(events),
  )

  with pytest.raises(RuntimeError, match="Unreadable event"):
    plant.read_trajectory(path, "hyundai", "IONIQ5", sample_step=1)


def test_required_boolean_decode_failures_reject_route(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 2_980_000_000

  class BrokenSteeringPressed:
    steeringAngleDeg = 0.0
    steeringRateDeg = 0.0
    steeringTorqueEps = 0.0
    vEgo = 15.0
    aEgo = 0.0

    @property
    def steeringPressed(self) -> bool:
      raise ValueError("bad steeringPressed")

  broken_car_state = FakeEvent(
    "carState",
    time_ns,
    BrokenSteeringPressed(),
  )
  fake_streams(monkeypatch, {path: [broken_car_state]})

  with pytest.raises(plant.CorruptRouteError, match="steeringPressed"):
    plant.ordered_relevant_events(path)

  class BrokenSaturated:
    actualLateralAccel = 0.0
    desiredLateralAccel = 0.0
    desiredLateralJerk = 0.0
    output = 0.0
    i = 0.0

    @property
    def saturated(self) -> bool:
      raise ValueError("bad saturated")

  lateral_state = SimpleNamespace(
    which=lambda: "torqueState",
    torqueState=BrokenSaturated(),
  )
  broken_controls_state = FakeEvent(
    "controlsState",
    time_ns,
    SimpleNamespace(lateralControlState=lateral_state),
  )
  fake_streams(monkeypatch, {path: [broken_controls_state]})

  with pytest.raises(plant.CorruptRouteError, match="saturated"):
    plant.ordered_relevant_events(path)


def test_lateral_control_union_decode_failure_rejects_route(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"

  class BrokenUnion:
    def which(self) -> str:
      raise ValueError("bad lateral control union")

  event = FakeEvent(
    "controlsState",
    2_985_000_000,
    SimpleNamespace(lateralControlState=BrokenUnion()),
  )
  fake_streams(monkeypatch, {path: [event]})

  with pytest.raises(plant.CorruptRouteError, match="lateralControlState"):
    plant.ordered_relevant_events(path)


def test_stale_joined_sources_do_not_inflate_route_overlay_stats(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "route--0" / "rlog"
  time_ns = 2_990_000_000
  events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000),
    fake_car_output(time_ns - 20_000_000, 0.2),
    fake_controls_state(time_ns - 20_000_000),
  ]
  events.extend(
    fake_car_state(
      time_ns + index * 10_000_000,
      float(index),
      steering_pressed=index >= 2,
    )
    for index in range(20)
  )
  fake_streams(monkeypatch, {path: events})

  trajectory = plant.read_trajectory(
    path,
    "hyundai",
    "IONIQ5",
    sample_step=1,
  )

  assert trajectory is not None
  assert int(np.count_nonzero(trajectory.values["sample_valid"])) == 2
  assert trajectory.lateral_active_rows == 2
  assert trajectory.driver_overlay_rows == 0


def test_adjacent_segments_carry_route_identity_and_causal_state(
  tmp_path,
  monkeypatch,
):
  first_path = tmp_path / "route--0" / "rlog"
  second_path = tmp_path / "route--1" / "rlog"
  time_ns = 3_000_000_000
  first_events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000, torque=0.15),
    fake_car_output(time_ns - 20_000_000, 0.25),
    fake_controls_state(time_ns - 20_000_000, actual_lateral_accel=0.5),
  ]
  first_events.extend(
    fake_car_state(time_ns + index * 10_000_000, float(index))
    for index in range(20)
  )
  first_events.append(
    fake_sentinel(time_ns + 195_000_000, "endOfSegment"),
  )
  second_events = [
    fake_sentinel(time_ns + 195_000_000, "startOfSegment"),
  ]
  second_events.extend(
    fake_car_state(time_ns + (20 + index) * 10_000_000, float(20 + index))
    for index in range(20)
  )
  fake_streams(monkeypatch, {
    first_path: first_events,
    second_path: second_events,
  })

  trajectories = plant.read_route_trajectories(
    [second_path, first_path],
    "hyundai",
    "IONIQ5",
    sample_step=1,
  )

  assert [trajectory.segment for trajectory in trajectories] == ["route--0-1"]
  merged = trajectories[0]
  assert merged.brand == "hyundai"
  assert merged.car_fingerprint == "HYUNDAI_IONIQ_5"
  assert merged.values["applied_torque"][20] == pytest.approx(0.25)
  assert merged.values["actual_lateral_accel"][20] == pytest.approx(0.5)
  assert merged.values["signed_steering_rate_deg_s"][20] == pytest.approx(100.0)


def test_adjacent_sentinels_do_not_carry_state_across_large_event_gap(
  tmp_path,
  monkeypatch,
):
  first_path = tmp_path / "route--0" / "rlog"
  second_path = tmp_path / "route--1" / "rlog"
  time_ns = 3_500_000_000
  first_events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000),
    fake_car_output(time_ns - 20_000_000, 0.25),
    fake_controls_state(time_ns - 20_000_000),
  ]
  first_events.extend(
    fake_car_state(time_ns + index * 10_000_000, float(index))
    for index in range(20)
  )
  first_events.append(
    fake_sentinel(time_ns + 195_000_000, "endOfSegment"),
  )
  second_start_ns = time_ns + 2_000_000_000
  second_events = [
    fake_sentinel(second_start_ns, "startOfSegment"),
  ]
  second_events.extend(
    fake_car_state(second_start_ns + index * 10_000_000, float(index))
    for index in range(20)
  )
  fake_streams(monkeypatch, {
    first_path: first_events,
    second_path: second_events,
  })

  trajectories = plant.read_route_trajectories(
    [first_path, second_path],
    "hyundai",
    "IONIQ5",
    sample_step=1,
  )

  assert [trajectory.segment for trajectory in trajectories] == [
    "route--0",
    "route--1",
  ]
  assert np.isnan(trajectories[1].values["applied_torque"]).all()


def test_overlapping_adjacent_segments_are_folded_in_global_causal_order(
  tmp_path,
  monkeypatch,
):
  first_path = tmp_path / "route--0" / "rlog"
  second_path = tmp_path / "route--1" / "rlog"
  time_ns = 4_000_000_000
  first_events = [
    fake_car_params(time_ns - 30_000_000),
    fake_car_control(time_ns - 20_000_000),
    fake_car_output(time_ns - 20_000_000, 0.2),
    fake_controls_state(time_ns - 20_000_000),
  ]
  first_events.extend(
    fake_car_state(time_ns + index * 10_000_000, float(index))
    for index in range(30)
  )
  first_events.append(
    fake_sentinel(time_ns + 295_000_000, "endOfSegment"),
  )
  # The next segment contains a causally earlier service update than the final
  # rows of the previous segment. Discovery order must not delay that update.
  second_events = [
    fake_sentinel(time_ns + 295_000_000, "startOfSegment"),
    fake_car_output(time_ns + 245_000_000, 0.9),
  ]
  second_events.extend(
    fake_car_state(time_ns + (30 + index) * 10_000_000, float(30 + index))
    for index in range(20)
  )
  fake_streams(monkeypatch, {
    first_path: first_events,
    second_path: second_events,
  })

  trajectories = plant.read_route_trajectories(
    [second_path, first_path],
    "hyundai",
    "IONIQ5",
    sample_step=1,
  )

  assert len(trajectories) == 1
  trajectory = trajectories[0]
  assert trajectory.values["applied_torque"][24] == pytest.approx(0.2)
  assert trajectory.values["applied_torque"][25] == pytest.approx(0.9)


def test_forced_holdout_routes_are_never_used_for_training():
  trajectories = [make_trajectory() for _ in range(4)]
  for index, trajectory in enumerate(trajectories):
    trajectory.route = f"route-{index}"
    trajectory.segment = f"route-{index}--0"
  train, validation = plant.split_routes(trajectories, 0.25, 7, ("route-2",))
  assert "route-2" in validation
  assert "route-2" not in train
