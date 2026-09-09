import unittest

from app.flow import RollingTradeFlow
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


class FlowWindowCvdRegressionTests(unittest.TestCase):
    def test_one_and_five_minute_cvd_use_their_own_rolling_windows(self) -> None:
        flow = RollingTradeFlow()
        flow.add_trade(trade(0, "BUY", 100))
        flow.add_trade(trade(30_000, "SELL", 40))
        flow.add_trade(trade(70_000, "BUY", 60))

        windows = flow.windows(70_000)

        self.assertEqual(windows[60].cvd, 20)
        self.assertEqual(windows[300].cvd, 120)

    def test_trades_older_than_one_hour_do_not_reenter_a_window(self) -> None:
        flow = RollingTradeFlow()
        flow.add_trade(trade(0, "BUY", 500))
        flow.add_trade(trade(3_600_001, "SELL", 20))

        self.assertEqual(flow.windows(3_600_001)[3600].cvd, -20)


if __name__ == "__main__":
    unittest.main()
