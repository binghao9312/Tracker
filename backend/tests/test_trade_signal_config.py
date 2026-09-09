import tempfile
import unittest
from pathlib import Path

from app.trade_signal import (
    TradeBias,
    TradeSignalThresholds,
    calculate_trade_signal,
    load_trade_signal_thresholds,
)


def detail(
    buy_pressure: float, sell_pressure: float, buy_volume: float, sell_volume: float
) -> dict[str, object]:
    return {
        "spot": {
            "buy_pressure_5m": buy_pressure,
            "sell_pressure_5m": sell_pressure,
            "buy_volume_5m": buy_volume,
            "sell_volume_5m": sell_volume,
        },
        "perp": {},
    }


class TradeSignalConfigRegressionTests(unittest.TestCase):
    def _config(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "scoring.yaml"
        path.write_text(
            """
trade_signal:
  pressure: 3.0
  pressure_dominance_ratio: 2.0
  delta_ratio: 0.5
""",
            encoding="utf-8",
        )
        return path

    def test_custom_yaml_thresholds_change_long_short_and_none_decisions(self) -> None:
        thresholds = load_trade_signal_thresholds(self._config())

        self.assertEqual(thresholds, TradeSignalThresholds(3.0, 2.0, 0.5))
        self.assertEqual(calculate_trade_signal(detail(2.5, 1.0, 80, 20)).bias, TradeBias.LONG)
        self.assertEqual(
            calculate_trade_signal(detail(2.5, 1.0, 80, 20), thresholds).bias, TradeBias.NONE
        )
        self.assertEqual(
            calculate_trade_signal(detail(3.0, 1.0, 80, 20), thresholds).bias, TradeBias.LONG
        )
        self.assertEqual(
            calculate_trade_signal(detail(1.0, 3.0, 20, 80), thresholds).bias, TradeBias.SHORT
        )


if __name__ == "__main__":
    unittest.main()
