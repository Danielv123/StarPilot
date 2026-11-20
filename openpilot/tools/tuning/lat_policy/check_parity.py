#!/usr/bin/env python3
"""Prove the trainer's batched controller and the on-car numpy controller agree.

The policy is trained through a tensor re-statement of the observation and the
torque limits. This replays training rollouts step by step through
``LearnedTorqueCore``, feeding it exactly what the simulated car reported, and
checks that it asks for the same torque on every frame.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plant
import train_policy as trainer

core = trainer.core


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--dataset", required=True)
  parser.add_argument("--plant", required=True)
  parser.add_argument("--policy", help="trained policy checkpoint; a random policy is used when omitted")
  parser.add_argument("--rollouts", type=int, default=16)
  parser.add_argument("--horizon", type=int, default=300)
  args = parser.parse_args()

  device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
  generator = torch.Generator(device=device).manual_seed(1)
  checkpoint = torch.load(args.plant, map_location=device, weights_only=False)
  model = plant.EnsemblePlant(checkpoint["members"], checkpoint["hidden"]).to(device).double()
  model.load_state_dict(checkpoint["state_dict"])
  if args.policy:
    saved = torch.load(args.policy, map_location=device, weights_only=False)
    policy = trainer.Policy(saved["hidden"]).to(device)
    policy.load_state_dict(saved["state_dict"])
  else:
    torch.manual_seed(1)
    policy = trainer.Policy().to(device)
    # A random policy with a lively output exercises the limits as well as the observation.
    torch.nn.init.normal_(policy.net[-1].weight, std=0.5)
  policy = policy.double()

  table = trainer.Table(args.dataset, device, trainer.WARMUP + args.horizon + 1)
  shape = (model.members, args.rollouts)
  batch = {name: value.double() for name, value in table.sample(0, *shape, generator).items()}
  randomization = trainer.sample_randomization(shape, device, generator)
  with torch.no_grad():
    traces = trainer.closed_loop(policy, model, batch, randomization, args.horizon)

  weights = policy.export()
  speed = batch["v"].cpu().numpy()
  curvature = (batch["target"] / torch.clamp(batch["v"] ** 2, min=1.0)).cpu().numpy()
  recorded_u = batch["u"].cpu().numpy()
  angle_recorded = batch["angle"].cpu().numpy()
  offset = batch["offset"].cpu().numpy()
  roll = batch["roll"].cpu().numpy()
  steer_ratio = batch["steer_ratio"].cpu().numpy()
  stiffness = batch["stiffness"].cpu().numpy()
  driver = batch["driver_torque"].cpu().numpy()
  delay = randomization["delay"].cpu().numpy()
  torque = traces["u"].cpu().numpy()
  angle_policy = traces["angle"].cpu().numpy()

  def measure(angle, m, b, t):
    quantized = np.round(angle / trainer.ANGLE_QUANTUM) * trainer.ANGLE_QUANTUM
    y = trainer.vehicle.lateral_accel(quantized - offset[m, b, t], speed[m, b, t], roll[m, b, t], steer_ratio[m, b, t], stiffness[m, b, t])
    return quantized, y

  worst = 0.0
  for m in range(shape[0]):
    for b in range(shape[1]):
      controller = core.LearnedTorqueCore(weights)
      # The trainer holds the first request for frames before the window starts.
      controller.reset(curvature=curvature[m, b, 0])
      # Warm-up: the recorded controller steers while the learned one only watches.
      for t in range(trainer.WARMUP):
        quantized, y = measure(angle_recorded[m, b, t], m, b, t)
        controller.observe(y, quantized, curvature[m, b, t], speed[m, b, t], roll[m, b, t], delay[m, b] * core.DT)
        controller.commit(recorded_u[m, b, t])
      for k in range(args.horizon):
        t = trainer.WARMUP + k
        quantized, y = measure(angle_policy[m, b, k], m, b, t)
        wanted = controller.propose(y, quantized, curvature[m, b, t], speed[m, b, t], roll[m, b, t], delay[m, b] * core.DT,
                                    -driver[m, b, t], False)
        worst = max(worst, abs(wanted - torque[m, b, k]))
        controller.commit(torque[m, b, k])
  print(f"largest torque disagreement over {shape[0] * shape[1]} rollouts x {args.horizon} frames: {worst:.3e}")
  if worst > 1e-6:
    print("FAILED: the trained and deployed controllers disagree")
    return 1
  print("parity ok")
  return 0


if __name__ == "__main__":
  sys.exit(main())
