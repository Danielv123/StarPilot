#!/usr/bin/env python3
"""Publish a diagnostic-only personalized path; no control process consumes it."""

from __future__ import annotations

import os

# Keep this low-priority diagnostic from creating a BLAS thread pool.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import time
from pathlib import Path

import numpy as np

from cereal import messaging
from openpilot.common.swaglog import cloudlog
from openpilot.starpilot.modeld.turn_shadow_adapter import TurnShadowAdapter


ARTIFACT_PATH = Path(__file__).with_name("turn_shadow_adapter.npz")
EXPECTED_ARTIFACT_SHA256 = "e7892cc562169798e4ca945d4724cc5ab647dcc1570880d3525c921ca148fec4"
EXPECTED_CHECKPOINT_SHA256 = "403f63c77c4e5524c10f779489f9e1e478464a6c6783d6420ec0fb58c42953dc"


def _desire_state(model) -> np.ndarray:
  values = np.asarray(model.meta.desireState, dtype=np.float32)
  output = np.zeros(8, dtype=np.float32)
  output[:min(len(values), len(output))] = values[:len(output)]
  return output


def build_message(adapter: TurnShadowAdapter, model, car_state):
  started = time.perf_counter()
  prediction = adapter.predict(
    np.asarray(model.position.x, dtype=np.float32),
    np.asarray(model.position.y, dtype=np.float32),
    np.asarray(model.position.z, dtype=np.float32),
    np.asarray(model.position.t, dtype=np.float32),
    float(car_state.vEgo), bool(car_state.leftBlinker), bool(car_state.rightBlinker), _desire_state(model),
  )
  message = messaging.new_message("starpilotTurnShadow")
  shadow = message.starpilotTurnShadow
  shadow.frameId = model.frameId
  shadow.timestampEof = model.timestampEof
  shadow.valid = prediction.valid
  shadow.active = prediction.active
  shadow.inferenceTimeMs = (time.perf_counter() - started) * 1000.0
  shadow.maxAbsResidual = prediction.max_abs_residual
  shadow.residualScale = adapter.residual_scale
  shadow.modelSha256 = adapter.checkpoint_sha256
  shadow.artifactSha256 = adapter.artifact_sha256
  shadow.status = prediction.status
  shadow.pathX = prediction.path_x.tolist()
  shadow.pathY = prediction.path_y.tolist()
  shadow.pathZ = prediction.path_z.tolist()
  shadow.pathT = prediction.path_t.tolist()
  return message


def main() -> None:
  adapter = TurnShadowAdapter(ARTIFACT_PATH)
  if adapter.artifact_sha256 != EXPECTED_ARTIFACT_SHA256 or adapter.checkpoint_sha256 != EXPECTED_CHECKPOINT_SHA256:
    raise RuntimeError("turn-shadow artifact provenance mismatch")
  cloudlog.warning(
    "turnshadowd loaded diagnostic adapter artifact=%s checkpoint=%s",
    adapter.artifact_sha256, adapter.checkpoint_sha256,
  )
  sm = messaging.SubMaster(["modelV2", "carState"], poll="modelV2")
  pm = messaging.PubMaster(["starpilotTurnShadow"])
  while True:
    sm.update()
    if not sm.updated["modelV2"] or not sm.valid["modelV2"] or not sm.valid["carState"]:
      continue
    try:
      pm.send("starpilotTurnShadow", build_message(adapter, sm["modelV2"], sm["carState"]))
    except Exception:
      cloudlog.exception("turnshadowd inference failed")


if __name__ == "__main__":
  main()
