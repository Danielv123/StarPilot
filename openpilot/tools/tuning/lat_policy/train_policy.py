#!/usr/bin/env python3
"""Train the learned steering torque law by backpropagating through the plant.

Thousands of recorded situations are replayed in parallel. Each starts from the
real car state, hands the wheel to the policy, and lets the learned plant
ensemble respond; the tracking and smoothness cost is differentiated through
the whole closed loop. Every rollout draws its own plant member, torque gain
and bias, actuation lag and planner delay, so the policy has to work across the
spread of what the car might really do rather than one model of it.

The observation and torque limits come from ``learned_torque_core``, the same
code the car runs, so there is one definition of the controller.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import plant
import vehicle

_spec = importlib.util.spec_from_file_location("learned_torque_core", HERE.parents[2] / "starpilot" / "lateral" / "learned_torque_core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

DT = core.DT
WARMUP = 64  # steps of recorded driving before the policy takes over
FIELDS = ("u", "angle", "offset", "v", "a", "roll", "steer_ratio", "stiffness", "target", "driver_torque")
MIN_SPEED = 4.0
ANGLE_QUANTUM = 0.1  # deg, the steering angle sensor resolution


class Policy(nn.Module):
  def __init__(self, hidden: int = 128, max_slew: float = 12.0 / core.STEER_MAX):
    super().__init__()
    self.max_slew = max_slew
    self.net = nn.Sequential(
      nn.Linear(core.OBSERVATION_SIZE, hidden), nn.Tanh(),
      nn.Linear(hidden, hidden), nn.Tanh(),
      nn.Linear(hidden, 1),
    )
    nn.init.uniform_(self.net[-1].weight, -1e-3, 1e-3)
    nn.init.zeros_(self.net[-1].bias)

  def forward(self, observation: torch.Tensor) -> torch.Tensor:
    return self.max_slew * torch.tanh(self.net(observation).squeeze(-1))

  def export(self) -> dict:
    layers = [self.net[0], self.net[2], self.net[4]]
    weights = {"version": np.asarray(core.CORE_VERSION), "max_slew": np.asarray(self.max_slew, dtype=np.float32)}
    for index, layer in enumerate(layers):
      weights[f"w{index}"] = layer.weight.detach().cpu().numpy().T.astype(np.float32)
      weights[f"b{index}"] = layer.bias.detach().cpu().numpy().astype(np.float32)
    return weights


class Table:
  def __init__(self, path: str, device: torch.device, window: int):
    data = np.load(path, allow_pickle=False)
    self.device = device
    self.window = window
    self.columns = {name: torch.from_numpy(data[name]).to(device) for name in FIELDS}
    self.routes = json.loads(str(data["routes_json"]))
    kernel = np.ones(window, dtype=np.float32) / window
    slow = (data["v"] < MIN_SPEED).astype(np.float32)
    any_slow = np.convolve(slow, kernel, mode="full")[window - 1:] > 0
    mean_abs_y = np.convolve(np.abs(data["y"]), kernel, mode="full")[window - 1:]
    change = np.abs(np.diff(data["target"], prepend=data["target"][:1]))
    mean_change = np.convolve(change, kernel, mode="full")[window - 1:]
    self.starts, self.weights = {}, {}
    for split in (0, 1, 2):
      parts = [np.arange(start, end - window, dtype=np.int64)
               for start, end, kind in zip(data["run_start"], data["run_end"], data["run_split"], strict=True)
               if kind == split and end - start > window]
      index = np.concatenate(parts) if parts else np.zeros((0,), dtype=np.int64)
      index = index[~any_slow[index]]
      self.starts[split] = torch.from_numpy(index).to(device)
      weight = 1.0 + 3.0 * np.minimum(mean_abs_y[index], 1.5) + 3.0 * np.minimum(mean_change[index] / 0.01, 1.5)
      self.weights[split] = torch.from_numpy(weight.astype(np.float32)).to(device)
    self.offsets = torch.arange(window, device=device)

  def sample(self, split: int, members: int, batch: int, generator: torch.Generator, *, weighted: bool = True) -> dict:
    count = members * batch
    if weighted:
      picks = torch.multinomial(self.weights[split], count, replacement=True, generator=generator)
    else:
      picks = torch.randint(len(self.starts[split]), (count,), device=self.device, generator=generator)
    index = self.starts[split][picks].view(members, batch, 1) + self.offsets
    return {name: column[index] for name, column in self.columns.items()}


def torque_range(previous, driver_torque, speed):
  """Tensor form of ``core.torque_range``."""
  low_speed = speed < core.LOW_SPEED_RATE_LIMIT_MPS
  up = torch.where(low_speed, core.RATE_UP_LOW, core.RATE_UP_HIGH)
  down = torch.where(low_speed, core.RATE_DOWN_LOW, core.RATE_DOWN_HIGH)
  driver_high = 1.0 + (core.STEER_DRIVER_ALLOWANCE + driver_torque) * core.STEER_DRIVER_MULTIPLIER / core.STEER_MAX
  driver_low = -1.0 + (-core.STEER_DRIVER_ALLOWANCE + driver_torque) * core.STEER_DRIVER_MULTIPLIER / core.STEER_MAX
  high = torch.clamp(torch.clamp(driver_high, max=1.0), min=0.0)
  low = torch.clamp(torch.clamp(driver_low, min=-1.0), max=0.0)
  positive = previous > 0.0
  rate_low = torch.where(positive, torch.maximum(previous - down, -up), previous - up)
  rate_high = torch.where(positive, previous + up, torch.minimum(previous + down, up))
  return torch.minimum(torch.maximum(low, rate_low), rate_high), torch.maximum(torch.minimum(high, rate_high), rate_low)


def sample_randomization(shape, device, generator, *, nominal: bool = False) -> dict:
  def uniform(low, high):
    return torch.empty(shape, device=device).uniform_(low, high, generator=generator)
  if nominal:
    return {
      "gain": torch.ones(shape, device=device),
      "bias": torch.zeros(shape, device=device),
      "lag": torch.full(shape, 2, device=device, dtype=torch.long),
      "delay": torch.full(shape, 33, device=device, dtype=torch.long),
    }
  return {
    # The plant's torque authority and a steady torque bias: tyres, load, road crown, wind.
    "gain": uniform(0.7, 1.4),
    "bias": uniform(-0.05, 0.05),
    # The plant sees the command from ``lag - 1`` frames ago. The controller and
    # car controller run in separate processes, so one frame is the norm.
    "lag": torch.randint(1, 7, shape, device=device, generator=generator),
    # The planner's lateral delay in frames; the car's own estimate is 0.33 s.
    "delay": torch.randint(25, 43, shape, device=device, generator=generator),
  }


def closed_loop(policy, model, batch: dict, randomization: dict, horizon: int):
  """Roll the policy out against the plant. Returns per-step traces ``[members, batch, horizon]``."""
  members, size, _ = batch["angle"].shape
  device = batch["angle"].device
  member_index = torch.arange(members, device=device).view(members, 1).expand(members, size)
  batch_index = torch.arange(size, device=device).view(1, size).expand(members, size)
  delay = randomization["delay"]
  lag = randomization["lag"]
  speed = batch["v"]
  curvature = batch["target"] / torch.clamp(speed * speed, min=1.0)
  offsets = [delay - (index * delay) // core.PREVIEW_POINTS for index in range(core.PREVIEW_POINTS + 1)]

  def geometry(angle, t):
    return vehicle.lateral_accel(angle - batch["offset"][..., t], speed[..., t], batch["roll"][..., t],
                                 batch["steer_ratio"][..., t], batch["stiffness"][..., t])

  def measured(angle, t):
    # The controller sees the angle in sensor steps; gradients pass straight through.
    quantized = angle + (torch.round(angle / ANGLE_QUANTUM) * ANGLE_QUANTUM - angle).detach()
    return quantized, geometry(quantized, t)

  h = model.initial_state(size, device)
  angle = batch["angle"][..., 0]
  previous_angle = angle
  angle_seen, y_seen, error_seen, applied = [], [], [], []
  traces = {name: [] for name in ("y", "setpoint", "u", "angle")}
  torque = batch["u"][..., WARMUP - 1]
  for t in range(WARMUP + horizon):
    recorded = t < WARMUP
    if recorded:
      angle = batch["angle"][..., t]
      previous_angle = batch["angle"][..., t - 1] if t > 0 else angle
    angle_q, y_q = measured(angle, t)
    speed_squared = speed[..., t] * speed[..., t]
    time_index = torch.full_like(delay, t)
    targets = [curvature[member_index, batch_index, torch.clamp(time_index - offset, min=0)] * speed_squared for offset in offsets]
    angle_seen.append(angle_q)
    y_seen.append(y_q)
    error_seen.append(targets[0] - y_q)
    if recorded:
      torque = batch["u"][..., t]
    else:
      error_mean = torch.stack(error_seen[-30:], dim=0).mean(dim=0)
      observation = torch.stack(core.observation_terms(
        targets, y_q, y_seen[-11], y_seen[-31], applied[-1], applied[-6], applied[-16], applied[-31],
        speed[..., t], batch["roll"][..., t], delay * DT, error_mean, angle_q, angle_seen[-11], xp=torch,
      ), dim=-1)
      low, high = torque_range(torque, -batch["driver_torque"][..., t], speed[..., t])
      wanted = torque + policy(observation)
      torque = torch.minimum(torch.maximum(wanted, low), high)
    applied.append(torque)
    # The plant feels the command a few frames later, scaled and biased.
    history = torch.stack(applied[-7:], dim=0)
    delayed = history[torch.clamp(history.shape[0] - lag, min=0), member_index, batch_index] if not recorded else torque
    felt = delayed if recorded else delayed * randomization["gain"] + randomization["bias"]
    y_true = geometry(angle, t)
    rate = (angle - previous_angle) / DT
    x = plant.features(felt, angle - batch["offset"][..., t], rate, y_true, speed[..., t], batch["a"][..., t], batch["roll"][..., t])
    delta, h = model.step(x, h)
    if not recorded:
      traces["y"].append(y_true)
      traces["setpoint"].append(targets[0])
      traces["u"].append(torque)
      traces["angle"].append(angle)
    previous_angle = angle
    angle = angle + delta
  return {name: torch.stack(values, dim=-1) for name, values in traces.items()}


def cost_terms(traces: dict, weights: dict) -> dict:
  error = traces["y"] - traces["setpoint"]
  slew = torch.diff(traces["u"], dim=-1) / DT
  slew_change = torch.diff(slew, dim=-1) / DT
  wheel_rate = torch.diff(traces["angle"], dim=-1) / DT
  wheel_accel = torch.diff(wheel_rate, dim=-1) / DT
  terms = {
    "tracking": (error ** 2).mean(),
    "slew": weights["slew"] * (slew ** 2).mean(),
    "slew_change": weights["slew_change"] * (slew_change ** 2).mean(),
    "wheel_accel": weights["wheel_accel"] * (wheel_accel ** 2).mean(),
  }
  terms["total"] = sum(terms.values())
  return terms


def metrics(traces: dict) -> dict:
  error = traces["y"] - traces["setpoint"]
  slew = torch.diff(traces["u"], dim=-1) / DT
  wheel_rate = torch.diff(traces["angle"], dim=-1) / DT
  wheel_accel = torch.diff(wheel_rate, dim=-1) / DT
  jerk = torch.diff(traces["y"], dim=-1) / DT
  reversals = (slew[..., 1:] * slew[..., :-1] < 0).float().mean() / DT
  return {
    "tracking_rmse": error.pow(2).mean().sqrt().item(),
    "tracking_p95": error.abs().flatten().quantile(0.95).item() if error.numel() < 16_000_000 else float("nan"),
    "lat_jerk_rms": jerk.pow(2).mean().sqrt().item(),
    "torque_slew_rms": slew.pow(2).mean().sqrt().item(),
    "torque_reversals_per_s": reversals.item(),
    "wheel_accel_rms": wheel_accel.pow(2).mean().sqrt().item(),
  }


def recorded_traces(batch: dict, delay: int, horizon: int) -> dict:
  """What the car's own controller did over the same windows, for reference."""
  span = slice(WARMUP, WARMUP + horizon)
  speed = batch["v"]
  y = vehicle.lateral_accel(batch["angle"] - batch["offset"], speed, batch["roll"], batch["steer_ratio"], batch["stiffness"])
  curvature = batch["target"] / torch.clamp(speed * speed, min=1.0)
  setpoint = curvature[..., WARMUP - delay:WARMUP + horizon - delay] * speed[..., span] ** 2
  return {"y": y[..., span], "setpoint": setpoint, "u": batch["u"][..., span], "angle": batch["angle"][..., span]}


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--dataset", required=True)
  parser.add_argument("--plant", required=True)
  parser.add_argument("--output", required=True)
  parser.add_argument("--hidden", type=int, default=128)
  parser.add_argument("--horizon", type=int, default=300)
  parser.add_argument("--batch", type=int, default=2048, help="rollouts per plant member")
  parser.add_argument("--steps", type=int, default=1500)
  parser.add_argument("--lr", type=float, default=1e-3)
  parser.add_argument("--slew-weight", type=float, default=0.02)
  parser.add_argument("--slew-change-weight", type=float, default=2e-4)
  parser.add_argument("--wheel-accel-weight", type=float, default=1e-6)
  parser.add_argument("--seed", type=int, default=0)
  args = parser.parse_args()

  device = torch.device("cuda")
  torch.manual_seed(args.seed)
  generator = torch.Generator(device=device).manual_seed(args.seed)
  checkpoint = torch.load(args.plant, map_location=device, weights_only=False)
  model = plant.EnsemblePlant(checkpoint["members"], checkpoint["hidden"]).to(device)
  model.load_state_dict(checkpoint["state_dict"])
  model.requires_grad_(False)
  model.eval()

  window = WARMUP + args.horizon + 1
  table = Table(args.dataset, device, window)
  print(f"windows: train {len(table.starts[0])}, val {len(table.starts[1])}, test {len(table.starts[2])}", flush=True)
  policy = Policy(args.hidden).to(device)
  optimizer = torch.optim.Adam(policy.parameters(), lr=args.lr)
  scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr=args.lr, total_steps=args.steps, pct_start=0.05)
  weights = {"slew": args.slew_weight, "slew_change": args.slew_change_weight, "wheel_accel": args.wheel_accel_weight}
  shape = (model.members, args.batch)
  validation_split = 1 if len(table.starts[1]) else 0
  history = []
  best = {"score": float("inf"), "state": None, "step": 0}
  started = time.monotonic()

  def validate(split: int, batches: int = 4) -> dict:
    policy.eval()
    totals: dict[str, list[float]] = {}
    with torch.no_grad():
      for _ in range(batches):
        batch = table.sample(split, *shape, generator, weighted=False)
        randomization = sample_randomization(shape, device, generator)
        traces = closed_loop(policy, model, batch, randomization, args.horizon)
        nominal = closed_loop(policy, model, batch, sample_randomization(shape, device, generator, nominal=True), args.horizon)
        rows = {f"policy_{k}": v for k, v in metrics(traces).items()}
        rows.update({f"nominal_{k}": v for k, v in metrics(nominal).items()})
        rows.update({f"recorded_{k}": v for k, v in metrics(recorded_traces(batch, 33, args.horizon)).items()})
        rows["cost"] = cost_terms(traces, weights)["total"].item()
        for key, value in rows.items():
          totals.setdefault(key, []).append(value)
    policy.train()
    return {key: float(np.nanmean(values)) for key, values in totals.items()}

  for step in range(1, args.steps + 1):
    batch = table.sample(0, *shape, generator)
    randomization = sample_randomization(shape, device, generator)
    traces = closed_loop(policy, model, batch, randomization, args.horizon)
    terms = cost_terms(traces, weights)
    optimizer.zero_grad(set_to_none=True)
    terms["total"].backward()
    torch.nn.utils.clip_grad_norm_(policy.parameters(), 1.0)
    optimizer.step()
    scheduler.step()
    if step % 50 == 0 or step == args.steps:
      report = validate(validation_split)
      history.append({"step": step, **{k: v.item() for k, v in terms.items()}, **report})
      print(f"step {step} train {json.dumps({k: round(v.item(), 5) for k, v in terms.items()})} " +
            f"val {json.dumps({k: round(v, 4) for k, v in report.items()})} {time.monotonic() - started:.0f}s", flush=True)
      if step >= 0.4 * args.steps and report["cost"] < best["score"]:
        best = {"score": report["cost"], "state": {k: v.detach().clone() for k, v in policy.state_dict().items()}, "step": step}
  if best["state"] is not None:
    policy.load_state_dict(best["state"])
  final = {"val": validate(validation_split, batches=8), "test": validate(2 if len(table.starts[2]) else 0, batches=8)}
  print("final", json.dumps(final, indent=1), flush=True)
  output = Path(args.output)
  output.parent.mkdir(parents=True, exist_ok=True)
  torch.save({"state_dict": policy.state_dict(), "hidden": args.hidden, "args": vars(args), "best_step": best["step"],
              "metrics": final, "history": history, "plant": str(args.plant)}, output)
  np.savez(output.with_suffix(".npz"), **policy.export())
  return 0


if __name__ == "__main__":
  sys.exit(main())
