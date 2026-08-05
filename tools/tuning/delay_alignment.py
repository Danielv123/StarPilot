from __future__ import annotations

import math

import numpy as np


DEFAULT_RESPONSE_DELAY_S = 0.10
DEFAULT_RESPONSE_DELAY_MIN_S = 0.0
DEFAULT_RESPONSE_DELAY_MAX_S = 0.30


def quantize_response_delay(
  response_delay_s: float,
  sample_period_s: float,
  max_history_steps: int,
) -> tuple[int, float]:
  """Return the nearest causal sample delay and its effective duration."""
  if not math.isfinite(response_delay_s) or response_delay_s < 0.0:
    raise ValueError("Response delay must be a finite, non-negative duration.")
  if not math.isfinite(sample_period_s) or sample_period_s <= 0.0:
    raise ValueError("Sample period must be finite and positive.")
  if max_history_steps < 0:
    raise ValueError("Available history steps must be non-negative.")

  # The epsilon keeps an exact half-sample expressed in decimal seconds from
  # rounding down because of its binary floating-point representation.
  delay_steps = int(math.floor(response_delay_s / sample_period_s + 0.5 + 1e-12))
  if delay_steps > max_history_steps:
    raise ValueError(
      f"Response delay requires {delay_steps} samples, but only " +
      f"{max_history_steps} historical reference samples are available."
    )
  return delay_steps, delay_steps * sample_period_s


def validate_response_delay_range(
  initial_delay_s: float,
  minimum_delay_s: float,
  maximum_delay_s: float,
  sample_period_s: float,
  max_history_steps: int,
) -> int:
  """Validate a continuous learned-delay range and return required history."""
  values = (initial_delay_s, minimum_delay_s, maximum_delay_s)
  if not all(math.isfinite(value) for value in values):
    raise ValueError("Response delay bounds and initialization must be finite.")
  if minimum_delay_s < 0.0 or maximum_delay_s <= minimum_delay_s:
    raise ValueError("Response delay bounds must satisfy 0 <= minimum < maximum.")
  if not minimum_delay_s <= initial_delay_s <= maximum_delay_s:
    raise ValueError("Initial response delay must be inside the learned range.")
  if not math.isfinite(sample_period_s) or sample_period_s <= 0.0:
    raise ValueError("Sample period must be finite and positive.")
  required_history_steps = int(math.ceil(maximum_delay_s / sample_period_s - 1e-12))
  if required_history_steps > max_history_steps:
    raise ValueError(
      f"Maximum response delay requires {required_history_steps} samples, but only " +
      f"{max_history_steps} historical reference samples are available."
    )
  return required_history_steps


def delayed_reference_indexes(
  current_index: int,
  rollout_steps: int,
  delay_steps: int,
) -> np.ndarray:
  """Indexes for response timestamps 1..N aligned to command(t - delay)."""
  if current_index < 0 or rollout_steps < 0 or delay_steps < 0:
    raise ValueError("Reference indexes, rollout steps, and delay must be non-negative.")
  indexes = current_index + np.arange(1, rollout_steps + 1) - delay_steps
  if len(indexes) and indexes[0] < 0:
    raise ValueError("Response delay reaches before the available reference history.")
  return indexes
