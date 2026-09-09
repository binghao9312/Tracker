import tempfile
import unittest
from pathlib import Path

from app.scoring import (
    ClassificationThresholds,
    MarketSignal,
    MoveType,
    classify_move,
    load_classification_thresholds,
)


class ClassificationConfigRegressionTests(unittest.TestCase):
    def _config(self, content: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "scoring.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_custom_yaml_thresholds_load_and_change_classification(self) -> None:
        thresholds = load_classification_thresholds(
            self._config("""
classification:
  pressure: 1.0
  cvd: 0.5
  oi_change: 0.2
  allow_missing_cvd: false
""")
        )
        signal = MarketSignal("okx", 1.5, 0.0, None, None, 1.0, None, None)

        self.assertEqual(thresholds, ClassificationThresholds(1.0, 0.5, 0.2, False))
        self.assertEqual(classify_move(signal, thresholds), MoveType.SPOT_DRIVEN)
        self.assertEqual(classify_move(signal, ClassificationThresholds()), MoveType.NEUTRAL)

    def test_missing_or_zero_cvd_does_not_confirm_without_explicit_opt_in(self) -> None:
        signal_missing = MarketSignal("okx", 3.0, 0.0, None, None, None, None, None)
        signal_zero = MarketSignal("okx", 3.0, 0.0, None, None, 0.0, None, None)
        default = ClassificationThresholds()

        self.assertEqual(classify_move(signal_missing, default), MoveType.NEUTRAL)
        self.assertEqual(classify_move(signal_zero, default), MoveType.NEUTRAL)
        self.assertEqual(
            classify_move(signal_missing, ClassificationThresholds(allow_missing_cvd=True)),
            MoveType.SPOT_DRIVEN,
        )


if __name__ == "__main__":
    unittest.main()
