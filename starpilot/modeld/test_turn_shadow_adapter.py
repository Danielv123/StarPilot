from pathlib import Path
import unittest

import numpy as np

from starpilot.modeld.turn_shadow_adapter import TurnShadowAdapter


ARTIFACT = Path(__file__).with_name("turn_shadow_adapter.npz")


class TestTurnShadowAdapter(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.adapter = TurnShadowAdapter(ARTIFACT)
    cls.path_x = np.linspace(0.0, 100.0, 33, dtype=np.float32)
    cls.path_z = np.zeros(33, dtype=np.float32)
    cls.path_t = np.linspace(0.0, 10.0, 33, dtype=np.float32)
    cls.desire = np.zeros(8, dtype=np.float32)

  def test_gate_off_is_exact_baseline(self):
    path_y = np.zeros(33, dtype=np.float32)
    result = self.adapter.predict(self.path_x, path_y, self.path_z, self.path_t, 12.0, False, False, self.desire)
    self.assertTrue(result.valid)
    self.assertFalse(result.active)
    np.testing.assert_array_equal(result.path_x, self.path_x)
    np.testing.assert_array_equal(result.path_y, path_y)

  def test_active_prediction_is_lateral_only_and_anchored(self):
    path_y = np.zeros(33, dtype=np.float32)
    result = self.adapter.predict(self.path_x, path_y, self.path_z, self.path_t, 12.0, True, False, self.desire)
    self.assertTrue(result.valid)
    self.assertTrue(result.active)
    np.testing.assert_array_equal(result.path_x, self.path_x)
    np.testing.assert_array_equal(result.path_z, self.path_z)
    np.testing.assert_array_equal(result.path_t, self.path_t)
    self.assertEqual(float(result.path_y[0]), float(path_y[0]))
    self.assertGreater(float(np.max(np.abs(result.path_y - path_y))), 0.0)

  def test_bad_shape_fails_closed(self):
    result = self.adapter.predict(self.path_x[:-1], np.zeros(32), self.path_z[:-1], self.path_t[:-1], 12.0, False, False, self.desire)
    self.assertFalse(result.valid)
    self.assertFalse(result.active)


if __name__ == "__main__":
  unittest.main()
