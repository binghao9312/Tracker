import unittest
from datetime import UTC, datetime, timedelta

from app.paper_trading import ArmState, PaperTradingEngine, PaperTradingSettings
from tests.paper_helpers import MemoryPaperRepository, detail


class PaperRearmTests(unittest.IsolatedAsyncioTestCase):
    async def test_trade_rearms_only_after_score_drops_below_threshold(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(repository, PaperTradingSettings(signal_persistence_seconds=0))
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)
        await engine.process_update("BTCUSDT", detail(score=65), now + timedelta(seconds=1))
        self.assertEqual(engine._arm_state["BTCUSDT"], ArmState.ARMED)

    async def test_closed_trade_honors_cooldown(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(repository, PaperTradingSettings(signal_persistence_seconds=0, take_profit_pct=0.01, cooldown_minutes=15))
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)
        await engine.process_update("BTCUSDT", detail(price=103), now + timedelta(seconds=1))
        events = await engine.process_update("BTCUSDT", detail(), now + timedelta(minutes=1))
        self.assertFalse(any(event["type"] == "OPEN" for event in events))


if __name__ == "__main__":
    unittest.main()
