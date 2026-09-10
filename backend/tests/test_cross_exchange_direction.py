import unittest

from app.scoring import (
    ClassificationThresholds,
    CrossExchangeState,
    MarketSignal,
    cross_exchange_state,
)


class CrossExchangeDirectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.thresholds = ClassificationThresholds(pressure=2.0, cvd=0.0, oi_change=0.05)

    def _signal(self, exchange: str, direction: str) -> MarketSignal:
        if direction == "BUY":
            return MarketSignal(exchange, 3.0, 1.0, None, None, 1.0, None, None)
        if direction == "SELL":
            return MarketSignal(exchange, 1.0, 3.0, None, None, -1.0, None, None)
        return MarketSignal(exchange, 0.0, 0.0, None, None, 0.0, None, None)

    def test_direction_table(self) -> None:
        cases = (
            ("BUY", "BUY", CrossExchangeState.CONFIRMED),
            ("SELL", "SELL", CrossExchangeState.CONFIRMED),
            ("BUY", "SELL", CrossExchangeState.DIVERGENT),
            ("SELL", "BUY", CrossExchangeState.DIVERGENT),
            ("NONE", "NONE", CrossExchangeState.NEUTRAL),
            ("NONE", "BUY", CrossExchangeState.SINGLE_EXCHANGE),
            ("NONE", "SELL", CrossExchangeState.SINGLE_EXCHANGE),
            ("BUY", "NONE", CrossExchangeState.SINGLE_EXCHANGE),
            ("SELL", "NONE", CrossExchangeState.SINGLE_EXCHANGE),
        )

        for binance, okx, expected in cases:
            with self.subTest(binance=binance, okx=okx):
                state = cross_exchange_state(
                    [self._signal("binance", binance), self._signal("okx", okx)],
                    self.thresholds,
                )
                self.assertEqual(state, expected)

    def test_only_one_available_exchange_is_single_exchange(self) -> None:
        state = cross_exchange_state([self._signal("binance", "BUY")], self.thresholds)
        self.assertEqual(state, CrossExchangeState.SINGLE_EXCHANGE)


if __name__ == "__main__":
    unittest.main()
