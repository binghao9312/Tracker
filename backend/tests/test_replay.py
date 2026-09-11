"""Replay/backtest semantics.

A backtest is the one kind of code where a bug does not crash -- it returns a
confident wrong number that you then trade on. These tests pin the decisions that
would otherwise be silently wrong:

* Fills are synthesized from persisted aggregates (mid + spread + depth), because the
  order book itself was never stored. The harness must say so, and must cap a fill at
  the depth that actually existed rather than inventing liquidity.
* Time must move forward only, and a feed outage must not be mistaken for a signal
  that held steady across it -- that is the difference between "score stayed above 80
  for 5 seconds" and "we have two rows 10 minutes apart".
* Exits must come from the audited PaperTradingEngine, not a second implementation,
  so the breached-stop rule holds in replay exactly as it does live.
"""

import unittest
from datetime import UTC, datetime, timedelta

from app.paper_trading import PaperTradingSettings
from research.replay import Snapshot, VenueQuote, replay, synthesize_book

FLOW_LONG = {
    "buy_pressure_1m": 4.0,
    "buy_pressure_5m": 4.0,
    "sell_pressure_1m": 0.5,
    "sell_pressure_5m": 0.5,
    "buy_volume_5m": 900.0,
    "sell_volume_5m": 100.0,
    "cvd_5m": 800.0,
}


def snapshot(
    seconds: int,
    mid: float,
    *,
    score: float = 90.0,
    depth: float = 5_000_000.0,
    symbol: str = "BTCUSDT",
) -> Snapshot:
    return Snapshot(
        timestamp=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(seconds=seconds),
        symbol=symbol,
        price=mid,
        activity_score=score,
        liquidity_fragility=40.0,
        move_type="MIXED",
        cross_exchange_state="CONFIRMED",
        oi_change_5m=0.05,
        funding_rate=0.0001,
        venues=(
            VenueQuote(
                exchange="binance",
                market="perp",
                mid=mid,
                spread_percent=1.0,
                bid_depth_2=depth,
                ask_depth_2=depth,
            ),
        ),
        spot=FLOW_LONG,
        perp=FLOW_LONG,
    )


def settings(**overrides: object) -> PaperTradingSettings:
    base = {
        "signal_persistence_seconds": 5,
        "entry_activity_score": 80.0,
        "rearm_activity_score": 65.0,
        "take_profit_pct": 0.02,
        "stop_loss_pct": 0.02,
        "notional_usdt": 1000.0,
        "fee_bps": 0,
        "cooldown_minutes": 0,
    }
    return PaperTradingSettings(**(base | overrides))  # type: ignore[arg-type]


class BookSynthesisTests(unittest.TestCase):
    def test_book_is_derived_from_mid_and_half_spread(self) -> None:
        quote = VenueQuote(
            exchange="binance",
            market="perp",
            mid=100.0,
            spread_percent=1.0,
            bid_depth_2=10_000.0,
            ask_depth_2=20_000.0,
        )
        book = synthesize_book(quote)
        # spread_percent is (ask - bid) / mid * 100, so each side sits half a spread out.
        self.assertAlmostEqual(book["bids"][0][0], 99.5, places=9)
        self.assertAlmostEqual(book["asks"][0][0], 100.5, places=9)

    def test_depth_is_converted_from_quote_notional_to_base_quantity(self) -> None:
        quote = VenueQuote(
            exchange="binance",
            market="perp",
            mid=100.0,
            spread_percent=1.0,
            bid_depth_2=10_000.0,
            ask_depth_2=20_000.0,
        )
        book = synthesize_book(quote)
        # Persisted depth is quote notional (USDT), the book needs base quantity.
        self.assertAlmostEqual(book["bids"][0][1], 10_000.0 / 99.5, places=9)
        self.assertAlmostEqual(book["asks"][0][1], 20_000.0 / 100.5, places=9)


class ReplayGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_monotonic_input_is_rejected(self) -> None:
        rows = [snapshot(0, 100.0), snapshot(2, 100.0), snapshot(1, 100.0)]
        with self.assertRaises(ValueError):
            await replay(rows, settings())

    async def test_result_declares_its_fill_model(self) -> None:
        result = await replay([snapshot(0, 100.0)], settings())
        self.assertTrue(result.fill_model)
        self.assertIn("fill_model", result.stats)
        self.assertEqual(result.stats["fill_model"], result.fill_model)

    async def test_signal_persistence_does_not_survive_a_feed_gap(self) -> None:
        # Two rows 10 minutes apart. The score is above the entry threshold in both,
        # but nothing was observed in between, so the 5s persistence requirement was
        # never actually met and no trade may be opened.
        rows = [snapshot(0, 100.0), snapshot(600, 100.0)]
        result = await replay(rows, settings(), max_gap_seconds=5.0)
        self.assertEqual(result.trades, [])
        self.assertGreaterEqual(result.gaps, 1)

    async def test_contiguous_persistence_does_open_a_trade(self) -> None:
        rows = [snapshot(second, 100.0) for second in range(0, 8)]
        result = await replay(rows, settings(), max_gap_seconds=5.0)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.gaps, 0)
        self.assertEqual(result.trades[0]["side"], "LONG")

    async def test_fill_is_capped_by_the_depth_that_existed(self) -> None:
        # Only 10 USDT of depth was ever recorded; a 1000 USDT order cannot fill.
        rows = [snapshot(second, 100.0, depth=10.0) for second in range(0, 8)]
        result = await replay(rows, settings(), max_gap_seconds=5.0)
        self.assertEqual(result.trades, [])
        self.assertTrue(
            any(event.get("reason") == "INSUFFICIENT_BOOK_DEPTH" for event in result.events)
        )


class ReplayOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rising_series_closes_as_take_profit_with_positive_return(self) -> None:
        rows = [snapshot(second, 100.0) for second in range(0, 8)]
        rows += [snapshot(8, 105.0), snapshot(9, 106.0)]
        result = await replay(rows, settings(), max_gap_seconds=5.0)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade["exit_reason"], "TAKE_PROFIT")
        self.assertGreater(trade["return_pct"], 0)
        self.assertEqual(result.stats["total"], 1)
        self.assertAlmostEqual(result.stats["win_rate"], 1.0, places=9)

    async def test_breached_stop_then_rebound_is_a_loss_not_a_win(self) -> None:
        # This is the T4 rule, and it must hold in replay because replay drives the
        # same engine. A dip through the stop followed by a rebound past take-profit
        # settles as STOP_LOSS, so the backtest cannot report it as a win.
        rows = [snapshot(second, 100.0) for second in range(0, 8)]
        rows += [snapshot(8, 96.0, depth=10.0), snapshot(9, 106.0)]
        result = await replay(rows, settings(), max_gap_seconds=5.0)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertLess(trade["return_pct"], 0)
        self.assertAlmostEqual(result.stats["win_rate"], 0.0, places=9)

    async def test_stats_report_exit_reason_breakdown_and_cadence_count(self) -> None:
        rows = [snapshot(second, 100.0) for second in range(0, 8)]
        rows += [snapshot(8, 105.0)]
        result = await replay(rows, settings(), max_gap_seconds=5.0)
        self.assertEqual(result.cadences, 9)
        self.assertEqual(result.stats["by_exit_reason"], {"TAKE_PROFIT": 1})

    async def test_a_feed_gap_does_not_erase_an_active_cooldown(self) -> None:
        # A cooldown is a time rule: "do not re-enter this symbol for N minutes after
        # closing". A feed outage is not observation of anything, so it cannot satisfy
        # or cancel that rule -- if a gap re-arms the symbol early, the backtest invents
        # trades that the live engine would never have taken, and every one of them
        # lands in win_rate and profit_factor.
        rows = [snapshot(second, 100.0) for second in range(0, 8)]
        rows.append(snapshot(8, 105.0))          # closes on take-profit, starts cooldown
        rows += [snapshot(600 + second, 100.0) for second in range(0, 8)]  # after a gap
        result = await replay(
            rows, settings(cooldown_minutes=60), max_gap_seconds=5.0
        )
        self.assertGreaterEqual(result.gaps, 1)
        # The cooldown still had ~50 minutes to run, so no second trade may open.
        self.assertEqual(len(result.trades), 1)

    async def test_symbols_are_replayed_independently(self) -> None:
        rows: list[Snapshot] = []
        for second in range(0, 8):
            rows.append(snapshot(second, 100.0, symbol="BTCUSDT"))
            rows.append(snapshot(second, 50.0, symbol="ETHUSDT"))
        result = await replay(rows, settings(), max_gap_seconds=5.0)
        self.assertEqual({trade["symbol"] for trade in result.trades}, {"BTCUSDT", "ETHUSDT"})


if __name__ == "__main__":
    unittest.main()
