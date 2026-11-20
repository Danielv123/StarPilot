#!/usr/bin/env python3
"""Closed-loop comparison of the real lateral controllers on the learned plant.

Nothing about the controllers is re-implemented here. Each recorded situation
gets a ``LatControlTorque`` built exactly as controlsd builds it, the car's own
``apply_driver_steer_torque_limits`` stands in for the car controller, and the
learned plant ensemble (evaluated with numpy) stands in for the steering rack.
Run it where the openpilot Python stack imports, for example under WSL.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vehicle

DT = 0.01
WARMUP = 64
# The handover from the recorded controller to the one under test is not a
# situation the car meets, so the first seconds after it are left out of the metrics.
SETTLE = 300
ANGLE_QUANTUM = 0.1
FIELDS = ("u", "angle", "offset", "v", "a", "roll", "steer_ratio", "stiffness", "target", "driver_torque", "y")
# Mirrors plant.py's fixed input scales.
ANGLE_SCALE, RATE_SCALE, LAT_ACCEL_SCALE, SPEED_SCALE, ACCEL_SCALE, DELTA_SCALE = 20.0, 30.0, 2.0, 20.0, 2.0, 0.1


class NumpyPlant:
  def __init__(self, path: str):
    with np.load(path) as weights:
      self.weights = {name: weights[name].astype(np.float64) for name in weights.files}
    self.members = self.weights["w_ih"].shape[0]
    self.hidden = self.weights["w_hh"].shape[1]

  def step(self, member: np.ndarray, u, angle_rel, rate, y, v, a, roll, h):
    x = np.stack((u, angle_rel / ANGLE_SCALE, rate / RATE_SCALE, y / LAT_ACCEL_SCALE, v / SPEED_SCALE,
                  np.minimum(5.0 / np.maximum(v, 0.5), 2.0), a / ACCEL_SCALE, roll * vehicle.GRAVITY), axis=-1)
    delta = np.empty(len(u))
    h_next = np.empty_like(h)
    w = self.weights
    for index in range(self.members):
      rows = member == index
      if not rows.any():
        continue
      gates_x = x[rows] @ w["w_ih"][index] + w["b_ih"][index]
      gates_h = h[rows] @ w["w_hh"][index] + w["b_hh"][index]
      x_r, x_z, x_n = np.split(gates_x, 3, axis=-1)
      h_r, h_z, h_n = np.split(gates_h, 3, axis=-1)
      reset = 1.0 / (1.0 + np.exp(-(x_r + h_r)))
      update = 1.0 / (1.0 + np.exp(-(x_z + h_z)))
      state = (1.0 - update) * np.tanh(x_n + reset * h_n) + update * h[rows]
      hidden = np.concatenate((state, x[rows]), axis=-1) @ w["w_1"][index] + w["b_1"][index]
      hidden = hidden / (1.0 + np.exp(-hidden))
      delta[rows] = (hidden @ w["w_2"][index] + w["b_2"][index])[:, 0] * DELTA_SCALE
      h_next[rows] = state
    return delta, h_next


def pick_windows(data, split: int, window: int, count: int, seed: int, min_speed: float) -> np.ndarray:
  rng = np.random.default_rng(seed)
  starts = []
  for start, end, kind in zip(data["run_start"], data["run_end"], data["run_split"], strict=True):
    if kind == split and end - start > window:
      starts.append(np.arange(start, end - window, window // 2, dtype=np.int64))
  starts = np.concatenate(starts)
  slow = np.array([data["v"][s:s + window].min() < min_speed for s in starts])
  starts = starts[~slow]
  return np.sort(rng.choice(starts, size=min(count, len(starts)), replace=False))


def build_controllers(kind: str, count: int):
  from opendbc.car.car_helpers import interfaces
  from opendbc.car.hyundai.values import CAR, CarControllerParams
  from opendbc.car.lateral import apply_driver_steer_torque_limits
  from opendbc.car.vehicle_model import VehicleModel
  from openpilot.selfdrive.controls.lib.lateral_controller_factory import create_lateral_controller
  from openpilot.starpilot.lateral.controller_selection import ControllerMode

  vehicle_name = CAR.HYUNDAI_IONIQ_5
  cp = interfaces[vehicle_name].get_non_essential_params(vehicle_name)
  reader = cp.as_reader()
  mode = ControllerMode.STANDARD if kind == "standard" else ControllerMode.STARPILOT
  controllers = []
  for _ in range(count):
    controller = create_lateral_controller(reader, interfaces[vehicle_name](cp), DT, mode=mode, turn_assist=True, learned=kind == "learned")
    if kind == "learned":
      name = type(controller.starpilot_extension.policy).__name__
      if name != "LearnedTorquePolicy":
        raise RuntimeError(f"learned controller did not load (got {name})")
    controllers.append(controller)
  limits = {True: CarControllerParams(reader, 5.0), False: CarControllerParams(reader, 20.0)}
  return controllers, VehicleModel(cp), limits, apply_driver_steer_torque_limits


def simulate(kind: str, data, plant: NumpyPlant, starts: np.ndarray, horizon: int, *, gain=1.0, bias=0.0, lag=1,
             delay=0.333, seed=0) -> dict:
  count = len(starts)
  steps = WARMUP + horizon
  index = starts[:, None] + np.arange(steps + 1)
  window = {name: data[name][index].astype(np.float64) for name in FIELDS}
  curvature = window["target"] / np.maximum(window["v"] ** 2, 1.0)
  controllers, vm, limits, apply_limits = build_controllers(kind, count)
  member = np.arange(count) % plant.members
  h = np.zeros((count, plant.hidden))
  angle = window["angle"][:, 0].copy()
  previous_angle = angle.copy()
  applied_last = np.zeros(count, dtype=np.int64)
  limited = np.zeros(count, dtype=bool)
  command_history = [np.zeros(count) for _ in range(lag + 1)]
  traces = {name: np.zeros((count, horizon)) for name in ("y", "setpoint", "u", "angle", "command")}
  delay_steps = int(delay / DT)
  gear = None
  from opendbc.car import structs
  gear = structs.CarState.GearShifter.drive
  for t in range(steps):
    recorded = t < WARMUP
    if recorded:
      angle = window["angle"][:, t].copy()
      previous_angle = window["angle"][:, t - 1].copy() if t > 0 else angle.copy()
    angle_q = np.round(angle / ANGLE_QUANTUM) * ANGLE_QUANTUM
    applied_now = np.zeros(count)
    for i in range(count):
      speed = window["v"][i, t]
      cs = SimpleNamespace(vEgo=speed, vEgoRaw=speed, aEgo=window["a"][i, t], steeringAngleDeg=angle_q[i],
                           steeringPressed=False, steeringTorque=window["driver_torque"][i, t], standstill=False,
                           steerFaultTemporary=False, steerFaultPermanent=False, gearShifter=gear)
      params = SimpleNamespace(angleOffsetDeg=window["offset"][i, t], roll=window["roll"][i, t])
      vm.update_params(window["stiffness"][i, t], window["steer_ratio"][i, t])
      # The controller under test runs from the start so its filters are warm,
      # but the recorded torque steers until the handover.
      steer, _, _log = controllers[i].update(True, cs, vm, params, bool(limited[i]), curvature[i, t], False, delay)
      if recorded:
        applied_last[i] = int(round(-window["u"][i, t] * vehicle.STEER_MAX))
        applied_now[i] = window["u"][i, t]
        continue
      requested = int(round(steer * vehicle.STEER_MAX))
      applied_last[i] = apply_limits(requested, int(applied_last[i]), cs.steeringTorque, limits[speed < vehicle.LOW_SPEED_LIMIT_MPS])
      limited[i] = abs(steer - applied_last[i] / vehicle.STEER_MAX) > 1e-2
      applied_now[i] = -applied_last[i] / vehicle.STEER_MAX
      traces["command"][i, t - WARMUP] = -steer
    command_history.append(applied_now)
    felt = applied_now if recorded else command_history[-1 - lag] * gain + bias
    y_true = vehicle.lateral_accel(angle - window["offset"][:, t], window["v"][:, t], window["roll"][:, t],
                                   window["steer_ratio"][:, t], window["stiffness"][:, t])
    rate = (angle - previous_angle) / DT
    delta, h = plant.step(member, felt, angle - window["offset"][:, t], rate, y_true, window["v"][:, t], window["a"][:, t],
                          window["roll"][:, t], h)
    if not recorded:
      k = t - WARMUP
      traces["y"][:, k] = y_true
      traces["setpoint"][:, k] = curvature[:, max(t - delay_steps, 0)] * window["v"][:, t] ** 2
      traces["u"][:, k] = applied_now
      traces["angle"][:, k] = angle
    previous_angle = angle
    angle = angle + delta
  traces["v"] = window["v"][:, WARMUP:steps]
  return traces


def recorded(data, starts: np.ndarray, horizon: int, delay: float) -> dict:
  steps = WARMUP + horizon
  index = starts[:, None] + np.arange(steps)
  window = {name: data[name][index].astype(np.float64) for name in FIELDS}
  curvature = window["target"] / np.maximum(window["v"] ** 2, 1.0)
  delay_steps = int(delay / DT)
  y = vehicle.lateral_accel(window["angle"] - window["offset"], window["v"], window["roll"], window["steer_ratio"], window["stiffness"])
  return {
    "y": y[:, WARMUP:], "setpoint": curvature[:, WARMUP - delay_steps:steps - delay_steps] * window["v"][:, WARMUP:] ** 2,
    "u": window["u"][:, WARMUP:], "angle": window["angle"][:, WARMUP:], "v": window["v"][:, WARMUP:], "command": window["u"][:, WARMUP:],
  }


def band_rms(signal: np.ndarray, low_hz: float, high_hz: float) -> float:
  """RMS of the part of ``signal`` between two frequencies, averaged over windows."""
  spectrum = np.fft.rfft(signal - signal.mean(axis=-1, keepdims=True), axis=-1)
  frequency = np.fft.rfftfreq(signal.shape[-1], DT)
  keep = (frequency >= low_hz) & (frequency < high_hz)
  power = (np.abs(spectrum[..., keep]) ** 2).sum(axis=-1) * 2.0 / signal.shape[-1] ** 2
  return float(np.sqrt(power.mean()))


def metrics(traces: dict, mask: np.ndarray | None = None) -> dict:
  rows = slice(None) if mask is None else mask
  y, setpoint, u, angle = (traces[name][rows][:, SETTLE:] for name in ("y", "setpoint", "u", "angle"))
  if len(y) == 0:
    return {}
  error = y - setpoint
  slew = np.diff(u, axis=-1) / DT
  wheel_rate = np.diff(angle, axis=-1) / DT
  wheel_accel = np.diff(wheel_rate, axis=-1) / DT
  jerk = np.diff(y, axis=-1) / DT
  moving = np.abs(slew) > 1e-6
  sign = np.sign(slew)
  # Count a reversal when the torque changes direction, ignoring frames where it holds still.
  reversals = 0
  for row_sign, row_moving in zip(sign, moving, strict=True):
    active = row_sign[row_moving]
    reversals += int((active[1:] * active[:-1] < 0).sum())
  return {
    "windows": int(len(y)),
    "tracking_rmse": float(np.sqrt((error ** 2).mean())),
    "tracking_p95": float(np.percentile(np.abs(error), 95)),
    "tracking_bias": float(error.mean()),
    "lat_jerk_rms": float(np.sqrt((jerk ** 2).mean())),
    "torque_slew_rms": float(np.sqrt((slew ** 2).mean())),
    "torque_reversals_per_s": float(reversals / (y.size * DT)),
    "wheel_rate_rms": float(np.sqrt((wheel_rate ** 2).mean())),
    "wheel_accel_rms": float(np.sqrt((wheel_accel ** 2).mean())),
    # Steering-wheel motion a driver feels as weave (slow) and as buzz or notchiness (fast).
    "wheel_weave_0p3_1hz_deg": band_rms(angle, 0.3, 1.0),
    "wheel_wobble_1_3hz_deg": band_rms(angle, 1.0, 3.0),
    "wheel_buzz_3_10hz_deg": band_rms(angle, 3.0, 10.0),
    "max_abs_torque": float(np.abs(u).max()),
  }


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--dataset", required=True)
  parser.add_argument("--plant", required=True, help="plant weights exported as .npz")
  parser.add_argument("--controllers", nargs="+", default=["starpilot", "learned"], choices=["standard", "starpilot", "learned"])
  parser.add_argument("--split", default="test", choices=["train", "val", "test"])
  parser.add_argument("--windows", type=int, default=200)
  parser.add_argument("--horizon", type=int, default=1500)
  parser.add_argument("--min-speed", type=float, default=7.0)
  parser.add_argument("--seed", type=int, default=0)
  parser.add_argument("--stress", action="store_true", help="also run off-nominal plant gain, bias and lag")
  parser.add_argument("--output")
  parser.add_argument("--traces", help="write nominal traces here as .npz")
  parser.add_argument("--weights", help="evaluate these learned weights instead of the installed ones")
  args = parser.parse_args()

  if args.weights:
    from openpilot.starpilot.lateral import learned_torque_policy
    learned_torque_policy.WEIGHTS = Path(args.weights)
  data = np.load(args.dataset, allow_pickle=False)
  plant = NumpyPlant(args.plant)
  split = ("train", "val", "test").index(args.split)
  starts = pick_windows(data, split, WARMUP + args.horizon + 1, args.windows, args.seed, args.min_speed)
  print(f"{len(starts)} windows of {args.horizon * DT:.0f} s from the {args.split} split", flush=True)
  scenarios = [("nominal", {})]
  if args.stress:
    scenarios += [("gain 0.8", {"gain": 0.8}), ("gain 1.2", {"gain": 1.2}), ("bias +0.04", {"bias": 0.04}),
                  ("lag 3 frames", {"lag": 3}), ("delay 0.25 s", {"delay": 0.25}), ("delay 0.42 s", {"delay": 0.42}),
                  # Outside what the policy was trained on, to see how it degrades.
                  ("gain 0.65", {"gain": 0.65}), ("gain 1.4", {"gain": 1.4}), ("gain 1.3 lag 4", {"gain": 1.3, "lag": 4})]
  report: dict = {"recorded": {"nominal": metrics(recorded(data, starts, args.horizon, 0.333))}}
  saved = {}
  for kind in args.controllers:
    report[kind] = {}
    for name, overrides in scenarios:
      started = time.monotonic()
      traces = simulate(kind, data, plant, starts, args.horizon, **overrides)
      report[kind][name] = metrics(traces)
      if name == "nominal":
        speed = traces["v"].mean(axis=-1)
        turning = np.abs(traces["setpoint"]).mean(axis=-1)
        report[kind]["by_speed"] = {
          "7-15 m/s": metrics(traces, speed < 15), "15-22 m/s": metrics(traces, (speed >= 15) & (speed < 22)),
          "22+ m/s": metrics(traces, speed >= 22),
        }
        report[kind]["by_demand"] = {"straight": metrics(traces, turning < 0.15), "turning": metrics(traces, turning >= 0.4)}
        saved[kind] = traces
      print(f"{kind:9s} {name:14s} {json.dumps({k: round(v, 4) for k, v in report[kind][name].items()})} " +
            f"{time.monotonic() - started:.0f}s", flush=True)
  if args.output:
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, indent=1))
  if args.traces:
    flat = {f"{kind}_{name}": value for kind, traces in saved.items() for name, value in traces.items()}
    np.savez_compressed(args.traces, starts=starts, **flat)
  return 0


if __name__ == "__main__":
  sys.exit(main())
