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

    def test_requested_windows_include_the_cutoff_and_omit_unrequested_horizons(self) -> None:
        flow = RollingTradeFlow()
        flow.add_trade(trade(10_000, "BUY", 10))
        flow.add_trade(trade(20_000, "SELL", 20))
        flow.add_trade(trade(70_000, "BUY", 30))

        windows = flow.windows(70_000, requested_seconds=(60, 300))

        self.assertEqual(set(windows), {60, 300})
        self.assertEqual(windows[60].buy_volume, 40)
        self.assertEqual(windows[60].sell_volume, 20)
        self.assertEqual(windows[300].cvd, 20)

    def test_out_of_order_trades_do_not_break_reverse_window_scan(self) -> None:
        flow = RollingTradeFlow()
        flow.add_trade(trade(200_000, "BUY", 100))
        flow.add_trade(trade(100_000, "SELL", 40))
        flow.add_trade(trade(150_000, "SELL", 20))

        window = flow.windows(250_000, requested_seconds=(60,))[60]

        self.assertEqual(window.buy_volume, 100)
        self.assertEqual(window.sell_volume, 0)

    def test_pressure_uses_opposing_depth(self) -> None:
        self.assertEqual(pressure(300_000, 50_000), 6)
        self.assertIsNone(pressure(1, 0))


if __name__ == "__main__":
    unittest.main()
