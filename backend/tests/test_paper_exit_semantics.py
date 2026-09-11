"""Exit semantics: trigger on the position's own venue, and never let a breached
stop be reported as a win.

These tests define two contracts that the 1-second snapshot loop must honour:

1. The exit TRIGGER must be evaluated on the order book of the exchange/market the
   position is actually held on, not on the cross-venue aggregate `detail["price"]`.
   Two perps can diverge by tens of bps in fast markets, which is enough to fire a
   1% stop that never happened on the venue holding the position.

2. Once the tracked max adverse excursion has breached the stop, the trade settles as
   STOP_LOSS even if a later snapshot shows a favourable price. Otherwise the
   optimistic bias is one-directional -- losses are missed, wins never are -- and
   win_rate / profit_factor are systematically overstated.
"""

import unittest
from datetime import UTC, datetime, timedelta

from app.paper_trading import PaperTradingEngine, PaperTradingSettings
from tests.paper_helpers import MemoryPaperRepository, book, detail


class ExitTriggerVenueTests(unittest.IsolatedAsyncioTestCase):
    """Contract 1: the trigger follows the position's own book."""

    async def test_aggregate_price_alone_does_not_trigger_take_profit(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(
            repository,
            PaperTradingSettings(
                signal_persistence_seconds=0, take_profit_pct=0.01, stop_loss_pct=0.01
            ),
        )
        now = datetime(2025, 1, 1, tzinfo=UTC)
        okx_only = {"okx": {"perp": book(100.0, spread=0.02)}}
        await engine.process_update("BTCUSDT", detail(books=okx_only), now)
        trade = repository.rows[1]
        self.assertEqual(trade["exchange"], "okx")

        # Aggregate price jumps 3%, but OKX -- where the position lives -- has not moved.
        await engine.process_update(
            "BTCUSDT",
            detail(price=103.0, books={"okx": {"perp": book(100.0, spread=0.02)}}),
            now + timedelta(seconds=1),
        )
        self.assertEqual(repository.rows[1]["status"], "OPEN")
        self.assertIsNone(repository.rows[1]["exit_reason"])

    async def test_position_venue_book_triggers_take_profit(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(
            repository,
            PaperTradingSettings(
                signal_persistence_seconds=0, take_profit_pct=0.01, stop_loss_pct=0.01
            ),
        )
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update(
            "BTCUSDT", detail(books={"okx": {"perp": book(100.0, spread=0.02)}}), now
        )

        # Now OKX itself moves up 3% while the aggregate stays flat.
        await engine.process_update(
            "BTCUSDT",
            detail(price=100.0, books={"okx": {"perp": book(103.0, spread=0.02)}}),
            now + timedelta(seconds=1),
        )
        trade = repository.rows[1]
        self.assertEqual(trade["exit_reason"], "TAKE_PROFIT")
        self.assertEqual(trade["status"], "CLOSED")

    async def test_missing_position_book_makes_no_exit_decision(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(
            repository,
            PaperTradingSettings(
                signal_persistence_seconds=0, take_profit_pct=0.01, stop_loss_pct=0.01
            ),
        )
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update(
            "BTCUSDT", detail(books={"okx": {"perp": book(100.0, spread=0.02)}}), now
        )

        # Only Binance is present now; the OKX position must not be marked from it.
        await engine.process_update(
            "BTCUSDT",
            detail(price=130.0, books={"binance": {"perp": book(130.0)}}),
            now + timedelta(seconds=1),
        )
        trade = repository.rows[1]
        self.assertEqual(trade["status"], "OPEN")
        self.assertIsNone(trade["exit_reason"])
        self.assertEqual(trade["max_favorable_excursion_pct"], 0.0)
        self.assertEqual(trade["max_adverse_excursion_pct"], 0.0)


class BreachedStopTests(unittest.IsolatedAsyncioTestCase):
    """Contract 2: a breached stop is never reported as a win."""

    def _engine(self, repository: MemoryPaperRepository) -> PaperTradingEngine:
        return PaperTradingEngine(
            repository,
            PaperTradingSettings(
                signal_persistence_seconds=0,
                take_profit_pct=0.02,
                stop_loss_pct=0.02,
                fee_bps=0,
            ),
        )

    async def test_stop_breached_while_unfillable_then_rebound_settles_as_stop_loss(self) -> None:
        repository = MemoryPaperRepository()
        engine = self._engine(repository)
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)
        entry = repository.rows[1]["entry_price"]

        # Price is 3% down (stop breached) but the book is too thin to exit on.
        thin = {"binance": {"perp": book(97.0, depth=0.0001)}}
        await engine.process_update(
            "BTCUSDT", detail(price=97.0, books=thin), now + timedelta(seconds=1)
        )
        self.assertEqual(repository.rows[1]["status"], "OPEN")
        self.assertLessEqual(repository.rows[1]["max_adverse_excursion_pct"], -0.02)

        # Price rebounds past take-profit with depth available again.
        await engine.process_update("BTCUSDT", detail(price=105.0), now + timedelta(seconds=2))
        trade = repository.rows[1]
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        # The stop is modelled at the stop level, not at the favourable rebound price.
        self.assertAlmostEqual(trade["exit_price"], entry * 0.98, places=6)
        self.assertLess(trade["return_pct"], 0)
        self.assertLess(trade["net_pnl"], 0)

    async def test_stop_on_the_current_snapshot_still_fills_from_the_book(self) -> None:
        repository = MemoryPaperRepository()
        engine = self._engine(repository)
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)

        # Stop breached on this very snapshot, with depth: unchanged behaviour, the
        # fill comes off the book (bid = 96.0), not from the modelled stop level.
        await engine.process_update("BTCUSDT", detail(price=97.0), now + timedelta(seconds=1))
        trade = repository.rows[1]
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertAlmostEqual(trade["exit_price"], 96.0, places=6)
        self.assertLess(trade["return_pct"], 0)

    async def test_take_profit_without_a_breached_stop_is_still_a_win(self) -> None:
        repository = MemoryPaperRepository()
        engine = self._engine(repository)
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)

        # Dips 1% (inside the 2% stop), then takes profit. This must stay a TAKE_PROFIT.
        await engine.process_update("BTCUSDT", detail(price=100.0), now + timedelta(seconds=1))
        await engine.process_update("BTCUSDT", detail(price=105.0), now + timedelta(seconds=2))
        trade = repository.rows[1]
        self.assertEqual(trade["exit_reason"], "TAKE_PROFIT")
        self.assertGreater(trade["return_pct"], 0)

    async def test_recovered_position_with_breached_stop_settles_as_stop_loss(self) -> None:
        repository = MemoryPaperRepository()
        engine = self._engine(repository)
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)
        entry = repository.rows[1]["entry_price"]
        # A restart loses in-memory state; the breached excursion is durable.
        repository.rows[1]["max_adverse_excursion_pct"] = -0.05
        await engine.recover_open_positions(now + timedelta(seconds=1))

        await engine.process_update("BTCUSDT", detail(price=105.0), now + timedelta(seconds=2))
        trade = repository.rows[1]
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertAlmostEqual(trade["exit_price"], entry * 0.98, places=6)
        self.assertLess(trade["return_pct"], 0)

    async def test_short_position_breached_stop_models_the_stop_above_entry(self) -> None:
        repository = MemoryPaperRepository()
        engine = self._engine(repository)
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)
        repository.rows[1]["side"] = "SHORT"
        repository.rows[1]["entry_trade_bias"] = "SHORT"
        repository.rows[1]["max_adverse_excursion_pct"] = -0.05
        await engine.recover_open_positions(now + timedelta(seconds=1))
        entry = repository.rows[1]["entry_price"]

        # A SHORT is hurt by price going up, so its modelled stop sits above entry.
        await engine.process_update("BTCUSDT", detail(price=97.0), now + timedelta(seconds=2))
        trade = repository.rows[1]
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertAlmostEqual(trade["exit_price"], entry * 1.02, places=6)
        self.assertLess(trade["return_pct"], 0)


if __name__ == "__main__":
    unittest.main()
