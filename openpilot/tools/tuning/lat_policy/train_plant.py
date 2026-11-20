#!/usr/bin/env python3
"""Train the steering plant ensemble on recorded engaged driving.

Members are trained together but each draws its own windows, so they disagree
where the data is thin. Every window is teacher-forced for a short burn-in and
then predicted open loop from the applied torque alone; the loss is taken on
that open-loop part, mostly in lateral-acceleration units.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch

try:
  from . import plant
except ImportError:  # running as a script
  sys.path.insert(0, str(Path(__file__).resolve().parent))
  import plant

FIELDS = ("u", "angle", "offset", "v", "a", "roll", "steer_ratio", "stiffness", "y", "target")


class Table:
  """The training table on the GPU with stratified window sampling."""

  def __init__(self, path: str, device: torch.device, window: int):
    data = np.load(path, allow_pickle=False)
    self.device = device
    self.window = window
    self.columns = {name: torch.from_numpy(data[name]).to(device) for name in FIELDS}
    self.routes = json.loads(str(data["routes_json"]))
    starts = {0: [], 1: [], 2: []}
    y = data["y"]
    u = data["u"]
    activity = np.abs(np.diff(u, prepend=u[:1]))
    for start, end, split in zip(data["run_start"], data["run_end"], data["run_split"], strict=True):
      if end - start > window:
        starts[int(split)].append(np.arange(start, end - window, dtype=np.int64))
    self.starts = {}
    self.weights = {}
    kernel = np.ones(window, dtype=np.float32) / window
    mean_abs_y = np.convolve(np.abs(y), kernel, mode="full")[window - 1:]
    mean_activity = np.convolve(activity, kernel, mode="full")[window - 1:]
    for split, parts in starts.items():
      index = np.concatenate(parts) if parts else np.zeros((0,), dtype=np.int64)
      self.starts[split] = torch.from_numpy(index).to(device)
      # Most driving is straight and quiet; draw turning and busy windows more often.
      weight = 1.0 + 3.0 * np.minimum(mean_abs_y[index] / 1.0, 1.5) + 3.0 * np.minimum(mean_activity[index] / 0.004, 1.5)
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


def loss_terms(prediction: torch.Tensor, batch: dict, burn_in: int):
  truth = batch["angle"][..., 1:]
  free = slice(burn_in, None)
  def accel(angle):
    return plant.lateral_accel(angle, batch["offset"][..., 1:], batch["v"][..., 1:], batch["roll"][..., 1:],
                               batch["steer_ratio"][..., 1:], batch["stiffness"][..., 1:])
  accel_error = (accel(prediction) - accel(truth))[..., free]
  angle_error = (prediction - truth)[..., free]
  return accel_error, angle_error


def evaluate(model, table: Table, split: int, burn_in: int, generator: torch.Generator, batches: int = 8, batch: int = 2048) -> dict:
  horizons = [h for h in (50, 100, 200, 300, 400) if h <= table.window - 1 - burn_in]
  squared = {h: [] for h in horizons}
  squared_persist = {h: [] for h in horizons}
  disagreement = []
  model.eval()
  with torch.no_grad():
    for _ in range(batches):
      data = table.sample(split, 1, batch, generator, weighted=False)
      data = {name: value.expand(model.members, -1, -1) for name, value in data.items()}
      prediction = plant.rollout(model, data, burn_in)
      accel_error, _ = loss_terms(prediction, data, burn_in)
      mean_error, _ = loss_terms(prediction.mean(dim=0, keepdim=True), {k: v[:1] for k, v in data.items()}, burn_in)
      hold = data["angle"][:1, :, burn_in:burn_in + 1].expand(-1, -1, prediction.shape[-1])
      persist_error, _ = loss_terms(hold, {k: v[:1] for k, v in data.items()}, burn_in)
      for h in horizons:
        squared[h].append((mean_error[..., h - 1] ** 2).mean().item())
        squared_persist[h].append((persist_error[..., h - 1] ** 2).mean().item())
      disagreement.append(accel_error.std(dim=0).mean().item())
  model.train()
  report = {f"rmse_{h / 100:g}s": float(np.sqrt(np.mean(squared[h]))) for h in horizons}
  report.update({f"persist_{h / 100:g}s": float(np.sqrt(np.mean(squared_persist[h]))) for h in horizons})
  report["member_disagreement"] = float(np.mean(disagreement))
  return report


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--dataset", required=True)
  parser.add_argument("--output", required=True)
  parser.add_argument("--members", type=int, default=5)
  parser.add_argument("--hidden", type=int, default=128)
  parser.add_argument("--burn-in", type=int, default=50)
  parser.add_argument("--horizon", type=int, default=300, help="open-loop steps predicted after the burn-in")
  parser.add_argument("--batch", type=int, default=1024)
  parser.add_argument("--steps", type=int, default=6000)
  parser.add_argument("--lr", type=float, default=2e-3)
  parser.add_argument("--seed", type=int, default=0)
  args = parser.parse_args()

  device = torch.device("cuda")
  torch.manual_seed(args.seed)
  generator = torch.Generator(device=device).manual_seed(args.seed)
  window = args.burn_in + args.horizon + 1
  table = Table(args.dataset, device, window)
  print(f"windows: train {len(table.starts[0])}, val {len(table.starts[1])}, test {len(table.starts[2])}", flush=True)

  model = plant.EnsemblePlant(args.members, args.hidden).to(device)
  optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-5)
  scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr=args.lr, total_steps=args.steps, pct_start=0.05)
  best = {"score": float("inf"), "state": None, "step": 0}
  started = time.monotonic()
  history = []
  for step in range(1, args.steps + 1):
    batch = table.sample(0, args.members, args.batch, generator)
    # Grow the open-loop horizon so early training is not dominated by drift.
    horizon = int(min(args.horizon, 50 + (args.horizon - 50) * min(1.0, step / (0.3 * args.steps))))
    batch = {name: value[..., :args.burn_in + horizon + 1] for name, value in batch.items()}
    prediction = plant.rollout(model, batch, args.burn_in)
    accel_error, angle_error = loss_terms(prediction, batch, args.burn_in)
    loss = (torch.nn.functional.huber_loss(accel_error / 0.1, torch.zeros_like(accel_error), delta=3.0)
            + 0.25 * torch.nn.functional.huber_loss(angle_error, torch.zeros_like(angle_error), delta=3.0))
    optimizer.zero_grad(set_to_none=True)
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
    optimizer.step()
    scheduler.step()
    if step % 250 == 0 or step == args.steps:
      report = evaluate(model, table, 1 if len(table.starts[1]) else 0, args.burn_in, generator)
      score = report.get("rmse_2s", report.get("rmse_1s", 1e9))
      history.append({"step": step, "loss": loss.item(), **report})
      print(f"step {step} loss {loss.item():.4f} horizon {horizon} {json.dumps({k: round(v, 4) for k, v in report.items()})} " +
            f"{time.monotonic() - started:.0f}s", flush=True)
      if step >= 0.5 * args.steps and score < best["score"]:
        best = {"score": score, "state": {k: v.detach().clone() for k, v in model.state_dict().items()}, "step": step}
  if best["state"] is not None:
    model.load_state_dict(best["state"])
  final = {
    "val": evaluate(model, table, 1 if len(table.starts[1]) else 0, args.burn_in, generator, batches=16),
    "test": evaluate(model, table, 2 if len(table.starts[2]) else 0, args.burn_in, generator, batches=16),
  }
  print("final", json.dumps(final, indent=1), flush=True)
  output = Path(args.output)
  output.parent.mkdir(parents=True, exist_ok=True)
  torch.save({
    "state_dict": model.state_dict(),
    "members": args.members,
    "hidden": args.hidden,
    "burn_in": args.burn_in,
    "horizon": args.horizon,
    "best_step": best["step"],
    "metrics": final,
    "history": history,
    "dataset": str(args.dataset),
    "routes": table.routes,
  }, output)
  np.savez(output.with_suffix(".npz"), **model.export())
  return 0


if __name__ == "__main__":
  sys.exit(main())
