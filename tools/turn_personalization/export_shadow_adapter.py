#!/usr/bin/env python3
"""Export a trained PyTorch turn adapter to a pickle-free NumPy runtime bundle."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile

import numpy as np
import torch


LAYER_KEYS = (
  "network.0.weight", "network.0.bias",
  "network.2.weight", "network.2.bias",
  "network.4.weight", "network.4.bias",
)


def sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
      digest.update(chunk)
  return digest.hexdigest()


def write_deterministic_npz(path: Path, arrays: dict[str, np.ndarray]) -> None:
  with path.open("wb") as stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for name in sorted(arrays):
      payload = io.BytesIO()
      np.lib.format.write_array(payload, np.asanyarray(arrays[name]), allow_pickle=False)
      info = zipfile.ZipInfo(f"{name}.npy", date_time=(1980, 1, 1, 0, 0, 0))
      info.compress_type = zipfile.ZIP_DEFLATED
      info.external_attr = 0o600 << 16
      archive.writestr(info, payload.getvalue(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def main() -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("checkpoint", type=Path)
  parser.add_argument("output", type=Path)
  parser.add_argument("--residual-scale", type=float, required=True)
  args = parser.parse_args()

  checkpoint = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
  if checkpoint.get("parameterization") != "lateral_only_degree5_bezier":
    raise RuntimeError(f"unsupported parameterization: {checkpoint.get('parameterization')}")
  model = checkpoint["model"]
  missing = [key for key in (*LAYER_KEYS, "bezier_basis") if key not in model]
  if missing:
    raise RuntimeError(f"checkpoint is missing tensors: {missing}")

  arrays = {
    "feature_mean": np.asarray(checkpoint["feature_mean"], dtype=np.float32),
    "feature_std": np.asarray(checkpoint["feature_std"], dtype=np.float32),
    "layer0_weight": model["network.0.weight"].detach().cpu().numpy().astype(np.float32),
    "layer0_bias": model["network.0.bias"].detach().cpu().numpy().astype(np.float32),
    "layer1_weight": model["network.2.weight"].detach().cpu().numpy().astype(np.float32),
    "layer1_bias": model["network.2.bias"].detach().cpu().numpy().astype(np.float32),
    "layer2_weight": model["network.4.weight"].detach().cpu().numpy().astype(np.float32),
    "layer2_bias": model["network.4.bias"].detach().cpu().numpy().astype(np.float32),
    "bezier_basis": model["bezier_basis"].detach().cpu().numpy().astype(np.float32),
    "path_indices": np.arange(0, 33, 2, dtype=np.int32),
    "residual_scale": np.asarray(args.residual_scale, dtype=np.float32),
    "checkpoint_sha256": np.asarray(sha256(args.checkpoint)),
    "dataset_sha256": np.asarray(checkpoint["dataset_sha256"]),
    "schema": np.asarray("starpilot.turn-shadow-adapter"),
    "schema_version": np.asarray(1, dtype=np.int32),
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  write_deterministic_npz(args.output, arrays)

  print(json.dumps({
    "output": str(args.output),
    "output_sha256": sha256(args.output),
    "checkpoint_sha256": arrays["checkpoint_sha256"].item(),
    "residual_scale": args.residual_scale,
  }, sort_keys=True))


if __name__ == "__main__":
  main()
