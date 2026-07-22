import math

import numpy as np


IONIQ_5_UNWIND_PREVIEW_TIME = 1.0
IONIQ_5_UNWIND_PREVIEW_BLEND_LEFT = 0.25
IONIQ_5_UNWIND_PREVIEW_BLEND_RIGHT = 0.25
IONIQ_5_UNWIND_PREVIEW_MAX_REDUCTION = 0.15
IONIQ_5_UNWIND_PREVIEW_MIN_LAT_ACCEL = 0.35
IONIQ_5_UNWIND_PREVIEW_MIN_DROP = 0.12
IONIQ_5_UNWIND_PREVIEW_DROP_WIDTH = 0.25


def get_ioniq_5_early_unwind_lateral_accel(desired_lateral_accel, preview_lateral_accel):
  desired_abs = abs(desired_lateral_accel)
  if desired_abs < IONIQ_5_UNWIND_PREVIEW_MIN_LAT_ACCEL:
    return desired_lateral_accel

  # A sign change means the path has passed through straight. Stop the preview
  # at zero so it can never command an anticipatory opposite turn.
  preview_abs = abs(preview_lateral_accel) if desired_lateral_accel * preview_lateral_accel > 0.0 else 0.0
  drop = desired_abs - preview_abs
  if drop <= IONIQ_5_UNWIND_PREVIEW_MIN_DROP:
    return desired_lateral_accel

  gate = np.clip((drop - IONIQ_5_UNWIND_PREVIEW_MIN_DROP) / IONIQ_5_UNWIND_PREVIEW_DROP_WIDTH, 0.0, 1.0)
  blend = IONIQ_5_UNWIND_PREVIEW_BLEND_LEFT if desired_lateral_accel >= 0.0 else IONIQ_5_UNWIND_PREVIEW_BLEND_RIGHT
  reduction = min(blend * drop * gate, IONIQ_5_UNWIND_PREVIEW_MAX_REDUCTION * desired_abs)
  return math.copysign(desired_abs - reduction, desired_lateral_accel)
