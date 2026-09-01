#!/usr/bin/env python3
"""Verify the portable shadow runtime against the selected PyTorch checkpoint."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import torch
from torch import nn

from starpilot.modeld.turn_shadow_adapter import TurnShadowAdapter


class ReferenceAdapter(nn.Module):
  def __init__(self, input_dim: int, hidden_dim: int, basis: np.ndarray):
    super().__init__()
    self.network = nn.Sequential(
      nn.Linear(input_dim, hidden_dim), nn.SiLU(),
      nn.Linear(hidden_dim, hidden_dim), nn.SiLU(),
      nn.Linear(hidden_dim, 6),
    )
    self.register_buffer("bezier_basis", torch.as_tensor(basis, dtype=torch.float32))

  def forward(self, features: torch.Tensor) -> torch.Tensor:
    raw = self.network(features)
    controls = raw - raw[:, :1]
    lateral = torch.einsum("pc,bc->bp", self.bezier_basis, controls)
    return torch.stack((torch.zeros_like(lateral), lateral), dim=1).reshape(-1, 66)


def main() -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("dataset", type=Path)
  parser.add_argument("checkpoint", type=Path)
  parser.add_argument("artifact", type=Path)
  args = parser.parse_args()

  with np.load(args.dataset, allow_pickle=False) as stored:
    data = {key: stored[key] for key in stored.files}
  checkpoint = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
  reference = ReferenceAdapter(checkpoint["input_dim"], checkpoint["hidden_dim"], checkpoint["model"]["bezier_basis"])
  reference.load_state_dict(checkpoint["model"])
  reference.eval()
  adapter = TurnShadowAdapter(args.artifact)

  features = ((data["features"] - checkpoint["feature_mean"]) / checkpoint["feature_std"]).astype(np.float32)
  with torch.no_grad():
    reference_residual = reference(torch.from_numpy(features)).numpy().reshape(-1, 2, 33)[:, 1] * adapter.residual_scale

  portable_residual = np.zeros_like(reference_residual)
  runtime_gate = np.zeros(len(features), dtype=np.bool_)
  rejected = 0
  for index in range(len(features)):
    source = data["features"][index]
    controls = adapter._network(source.astype(np.float32))
    controls -= controls[0]
    portable_residual[index] = adapter.residual_scale * (adapter.arrays["bezier_basis"] @ controls)
    result = adapter.predict(
      data["baseline_x"][index], data["baseline_y"][index], np.zeros(33, dtype=np.float32), data["path_t"][index],
      float(source[51]), bool(source[52] > 0.5), bool(source[53] > 0.5), source[54:62],
    )
    rejected += int(not result.valid)
    runtime_gate[index] = result.active

  if not np.array_equal(runtime_gate, data["gate"]):
    raise RuntimeError(f"runtime gate mismatch on {int(np.sum(runtime_gate != data['gate']))} samples")
  maximum_error = float(np.max(np.abs(portable_residual - reference_residual)))
  applied_outside_gate = 0.0
  if maximum_error > 5e-5:
    raise RuntimeError(f"parity failure: max={maximum_error}")
  print({
    "samples": len(features), "active": int(np.sum(runtime_gate)), "display_rejected": rejected,
    "maximum_lateral_residual_error_m": maximum_error,
    "outside_gate_applied_residual_m": applied_outside_gate,
    "artifact_sha256": adapter.artifact_sha256,
  })


if __name__ == "__main__":
  main()
