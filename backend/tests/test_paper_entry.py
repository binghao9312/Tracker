import unittest
from datetime import UTC, datetime, timedelta

from app.paper_trading import PaperTradingEngine, PaperTradingSettings
from tests.paper_helpers import MemoryPaperRepository, detail


class PaperEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_signal_opens_once_and_does_not_duplicate(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(repository, PaperTradingSettings(signal_persistence_seconds=5))
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)
        opened = await engine.process_update("BTCUSDT", detail(), now + timedelta(seconds=5))
        await engine.process_update("BTCUSDT", detail(), now + timedelta(seconds=6))
        self.assertEqual([event["type"] for event in opened], ["SIGNAL_TRIGGERED", "OPEN"])
        self.assertEqual(len(repository.rows), 1)


if __name__ == "__main__":
    unittest.main()
