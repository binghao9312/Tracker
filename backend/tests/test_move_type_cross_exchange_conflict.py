import unittest

from app.scoring import (
    ClassificationThresholds,
    MarketSignal,
    MoveType,
    aggregate_move_type,
    classify_move,
    exchange_directions,
)


class CrossExchangeMoveConflictTests(unittest.TestCase):
    def test_spot_and_leverage_exchange_moves_are_mixed_in_any_order_with_evidence(self) -> None:
        thresholds = ClassificationThresholds(pressure=2.0, cvd=0.0, oi_change=0.05)
        spot = MarketSignal("binance", 3.0, 1.0, None, None, 1.0, None, None)
        leverage = MarketSignal("okx", None, None, 3.0, 1.0, None, 1.0, 0.1)
        signals = [spot, leverage]

        moves = {signal.exchange: classify_move(signal, thresholds) for signal in signals}
        reverse = [classify_move(signal, thresholds) for signal in reversed(signals)]

        self.assertEqual(moves, {"binance": MoveType.SPOT_DRIVEN, "okx": MoveType.LEVERAGE_DRIVEN})
        self.assertEqual(aggregate_move_type(moves.values()), MoveType.MIXED)
        self.assertEqual(aggregate_move_type(reverse), MoveType.MIXED)
        self.assertEqual(exchange_directions(signals, thresholds), {"binance": "BUY", "okx": "BUY"})


if __name__ == "__main__":
    unittest.main()
