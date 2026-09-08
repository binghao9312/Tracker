import unittest

from app.flow import RollingTradeFlow, pressure
from app.models import Exchange, MarketType, NormalizedTrade


def trade(timestamp: int, side: str, quote_value: float) -> NormalizedTrade:
    return NormalizedTrade(
        exchange=Exchange.BINANCE,
        symbol="BTCUSDT",
        market=MarketType.SPOT,
        timestamp=timestamp,
        price=100,
        quantity=quote_value / 100,
        quote_value=quote_value,
        side=side,
    )


class RollingTradeFlowTests(unittest.TestCase):
    def test_separates_buy_sell_delta_ratio_and_cvd(self) -> None:
        flow = RollingTradeFlow()
        flow.add_trade(trade(0, "BUY", 100))
        flow.add_trade(trade(30_000, "SELL", 40))
        flow.add_trade(trade(70_000, "BUY", 60))

        windows = flow.windows(70_000)

        self.assertEqual(windows[60].buy_volume, 60)
        self.assertEqual(windows[60].sell_volume, 40)
        self.assertEqual(windows[60].delta, 20)
        self.assertEqual(windows[60].buy_sell_ratio, 1.5)
        self.assertEqual(windows[300].cvd, 120)

    def test_pressure_uses_opposing_depth(self) -> None:
        self.assertEqual(pressure(300_000, 50_000), 6)
        self.assertIsNone(pressure(1, 0))


if __name__ == "__main__":
    unittest.main()
