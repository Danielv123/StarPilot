#!/usr/bin/env python3
"""Check that the learned plant responds to torque the way a steering rack does.

The plant is fitted to closed-loop driving, where torque and the road ahead are
correlated, so a good prediction score does not by itself show that the model
learned what torque *causes*. This adds a small torque step to recorded
situations and reports the extra lateral acceleration it produces over time,
per speed band and per ensemble member. A usable plant shows a positive gain of
a few m/s^2 per unit torque, rising over a few tenths of a second, on which the
members agree.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plant
from train_plant import Table

TIMES = (5, 10, 20, 30, 50, 100, 200)


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--dataset", required=True)
  parser.add_argument("--plant", required=True)
  parser.add_argument("--step", type=float, default=0.05)
  parser.add_argument("--rollouts", type=int, default=4096)
  args = parser.parse_args()

  device = torch.device("cuda")
  generator = torch.Generator(device=device).manual_seed(0)
  checkpoint = torch.load(args.plant, map_location=device, weights_only=False)
  model = plant.EnsemblePlant(checkpoint["members"], checkpoint["hidden"]).to(device)
  model.load_state_dict(checkpoint["state_dict"])
  model.eval()
  burn_in = 100
  table = Table(args.dataset, device, burn_in + max(TIMES) + 2)
  batch = table.sample(1, 1, args.rollouts, generator, weighted=False)
  batch = {name: value.expand(model.members, -1, -1).clone() for name, value in batch.items()}

  def accel(data):
    angle = plant.rollout(model, data, burn_in)
    return plant.lateral_accel(angle, data["offset"][..., 1:], data["v"][..., 1:], data["roll"][..., 1:],
                               data["steer_ratio"][..., 1:], data["stiffness"][..., 1:])

  with torch.no_grad():
    base = accel(batch)
    responses = {}
    for sign in (1.0, -1.0):
      stepped = dict(batch)
      stepped["u"] = batch["u"].clone()
      stepped["u"][..., burn_in:] += sign * args.step
      responses[sign] = (accel(stepped) - base) / (sign * args.step)
  speed = batch["v"][0, :, burn_in].cpu().numpy()
  print(f"extra lateral acceleration per unit of added torque, {args.step:+.2f} and {-args.step:+.2f} steps averaged")
  print("speed band   rollouts  " + "  ".join(f"{t * 10:5d}ms" for t in TIMES) + "   member spread at 1 s")
  for low, high in ((4, 8), (8, 15), (15, 22), (22, 40)):
    rows = torch.from_numpy((speed >= low) & (speed < high)).to(device)
    if rows.sum() < 20:
      continue
    gain = 0.5 * (responses[1.0] + responses[-1.0])[:, rows]
    mean = gain.mean(dim=(0, 1)).cpu().numpy()
    members = gain[..., burn_in + 100 - 1].mean(dim=1).cpu().numpy()
    asymmetry = (responses[1.0] - responses[-1.0])[:, rows][..., burn_in + 100 - 1].mean().item()
    print(f"{low:2d}-{high:2d} m/s  {int(rows.sum()):8d}  " + "  ".join(f"{mean[burn_in + t - 1]:7.2f}" for t in TIMES)
          + f"   {members.min():.2f}..{members.max():.2f}  (+/- asymmetry {asymmetry:+.2f})")
  return 0


if __name__ == "__main__":
  sys.exit(main())
