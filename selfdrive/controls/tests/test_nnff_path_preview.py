from openpilot.starpilot.controls.lib.nnff_path_preview import get_ioniq_5_early_unwind_lateral_accel


def test_ioniq_5_preview_only_reduces_an_upcoming_unwind():
  assert get_ioniq_5_early_unwind_lateral_accel(1.0, 1.2) == 1.0
  assert get_ioniq_5_early_unwind_lateral_accel(-1.0, -1.2) == -1.0
  assert get_ioniq_5_early_unwind_lateral_accel(0.30, 0.0) == 0.30


def test_ioniq_5_preview_is_direction_symmetric_and_capped():
  assert get_ioniq_5_early_unwind_lateral_accel(1.0, 0.0) == 0.85
  assert get_ioniq_5_early_unwind_lateral_accel(-1.0, 0.0) == -0.85


def test_ioniq_5_preview_never_commands_the_opposite_turn():
  left = get_ioniq_5_early_unwind_lateral_accel(0.8, -0.5)
  right = get_ioniq_5_early_unwind_lateral_accel(-0.8, 0.5)
  assert 0.0 < left <= 0.8
  assert -0.8 <= right < 0.0
