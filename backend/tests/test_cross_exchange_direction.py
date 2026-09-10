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

    def test_same_buy_direction_is_confirmed(self) -> None:
        state = cross_exchange_state(
            [self._signal("binance", "BUY"), self._signal("okx", "BUY")], self.thresholds
        )
        self.assertEqual(state, CrossExchangeState.CONFIRMED)

    def test_same_sell_direction_is_confirmed(self) -> None:
        state = cross_exchange_state(
            [self._signal("binance", "SELL"), self._signal("okx", "SELL")], self.thresholds
        )
        self.assertEqual(state, CrossExchangeState.CONFIRMED)

    def test_opposite_directions_are_divergent(self) -> None:
        state = cross_exchange_state(
            [self._signal("binance", "BUY"), self._signal("okx", "SELL")], self.thresholds
        )
        self.assertEqual(state, CrossExchangeState.DIVERGENT)

    def test_inactive_exchange_is_single_exchange(self) -> None:
        state = cross_exchange_state(
            [self._signal("binance", "BUY"), self._signal("okx", "NONE")], self.thresholds
        )
        self.assertEqual(state, CrossExchangeState.SINGLE_EXCHANGE)

    def test_inactive_then_sell_is_single_exchange(self) -> None:
        state = cross_exchange_state(
            [self._signal("binance", "NONE"), self._signal("okx", "SELL")], self.thresholds
        )
        self.assertEqual(state, CrossExchangeState.SINGLE_EXCHANGE)

    def test_inactive_exchanges_are_neutral(self) -> None:
        state = cross_exchange_state(
            [self._signal("binance", "NONE"), self._signal("okx", "NONE")], self.thresholds
        )
        self.assertEqual(state, CrossExchangeState.NEUTRAL)


if __name__ == "__main__":
    unittest.main()
