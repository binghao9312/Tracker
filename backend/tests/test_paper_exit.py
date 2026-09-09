import unittest
from datetime import UTC, datetime, timedelta

from app.paper_trading import PaperTradingEngine, PaperTradingSettings
from tests.paper_helpers import MemoryPaperRepository, detail


class PaperExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_take_profit_deducts_entry_and_exit_fees_and_tracks_mfe(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(
            repository,
            PaperTradingSettings(signal_persistence_seconds=0, take_profit_pct=0.01, fee_bps=5),
        )
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)
        await engine.process_update("BTCUSDT", detail(price=103), now + timedelta(seconds=1))
        trade = repository.rows[1]
        self.assertEqual(trade["exit_reason"], "TAKE_PROFIT")
        self.assertGreater(trade["gross_pnl"], trade["net_pnl"])
        self.assertGreater(trade["max_favorable_excursion_pct"], 0)

    async def test_time_stop_closes_position(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(
            repository,
            PaperTradingSettings(
                signal_persistence_seconds=0,
                max_holding_minutes=1,
                take_profit_pct=1,
                stop_loss_pct=1,
            ),
        )
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)
        await engine.process_update("BTCUSDT", detail(), now + timedelta(minutes=1))
        self.assertEqual(repository.rows[1]["exit_reason"], "TIME_STOP")


if __name__ == "__main__":
    unittest.main()
