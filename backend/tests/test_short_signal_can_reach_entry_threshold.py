import unittest
from datetime import UTC, datetime

from app.paper_trading import PaperTradingEngine, PaperTradingSettings
from app.scoring import activity_score
from tests.paper_helpers import MemoryPaperRepository, detail


class ShortSignalEntryThresholdTests(unittest.IsolatedAsyncioTestCase):
    async def test_sell_pressure_anomaly_can_open_a_short_at_entry_threshold(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(
            repository, PaperTradingSettings(entry_activity_score=80, signal_persistence_seconds=0)
        )
        state = detail(score=activity_score(3.0, 3.0, -1.0, 0.0, 0.0, True))
        state["spot"] = {
            "buy_pressure_5m": 1.0,
            "sell_pressure_5m": 3.0,
            "buy_volume_5m": 100.0,
            "sell_volume_5m": 800.0,
        }
        state["perp"] = {
            "buy_pressure_5m": 1.0,
            "sell_pressure_5m": 3.0,
            "buy_volume_5m": 100.0,
            "sell_volume_5m": 800.0,
        }

        events = await engine.process_update("BTCUSDT", state, datetime(2025, 1, 1, tzinfo=UTC))

        self.assertTrue(any(event["type"] == "OPEN" for event in events))
        self.assertEqual(repository.rows[1]["side"], "SHORT")


if __name__ == "__main__":
    unittest.main()
