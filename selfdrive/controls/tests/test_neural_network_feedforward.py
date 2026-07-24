import math

from openpilot.starpilot.common.starpilot_variables import NNFF_MODELS_PATH
from openpilot.starpilot.controls.lib.neural_network_feedforward import FluxModel


def test_ioniq5_nnff_model_loads():
  model = FluxModel(NNFF_MODELS_PATH / "HYUNDAI_IONIQ_5.json")

  assert [activation for _, _, activation in model.layers] == ["sigmoid", "sigmoid", "sigmoid", "identity"]
  assert math.isfinite(model.evaluate([0.0] * model.input_size))
  assert model.low_speed_angle_assist_gain == 0.0
  assert model.low_speed_angle_assist_max == 0.0
