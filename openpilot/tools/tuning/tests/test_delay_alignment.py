from __future__ import annotations

import numpy as np
import pytest

from openpilot.tools.tuning import delay_alignment


def test_nominal_ioniq5_delay_quantizes_to_two_50ms_samples() -> None:
  steps, effective = delay_alignment.quantize_response_delay(0.10, 0.05, 6)
  assert steps == 2
  assert effective == pytest.approx(0.10)


def test_delay_quantization_rounds_half_samples_up() -> None:
  steps, effective = delay_alignment.quantize_response_delay(0.075, 0.05, 6)
  assert steps == 2
  assert effective == pytest.approx(0.10)


def test_delayed_reference_tracks_past_command_shape() -> None:
  indexes = delay_alignment.delayed_reference_indexes(
    current_index=6,
    rollout_steps=4,
    delay_steps=2,
  )
  assert np.array_equal(indexes, [5, 6, 7, 8])


@pytest.mark.parametrize("delay", (-0.01, float("nan"), float("inf")))
def test_invalid_delay_is_rejected(delay: float) -> None:
  with pytest.raises(ValueError, match="finite, non-negative"):
    delay_alignment.quantize_response_delay(delay, 0.05, 6)


def test_delay_cannot_exceed_available_reference_history() -> None:
  with pytest.raises(ValueError, match="only 1 historical"):
    delay_alignment.quantize_response_delay(0.10, 0.05, 1)


def test_continuous_delay_range_uses_ceiling_history() -> None:
  assert delay_alignment.validate_response_delay_range(
    0.10, 0.0, 0.30, 0.05, 6,
  ) == 6


def test_initial_delay_must_be_inside_learned_range() -> None:
  with pytest.raises(ValueError, match="inside the learned range"):
    delay_alignment.validate_response_delay_range(0.31, 0.0, 0.30, 0.05, 6)
