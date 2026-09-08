import asyncio
import unittest

from app.collectors.orderbooks import _ResyncingCollector
from app.orderbook import LocalOrderBook


class FlakyCollector(_ResyncingCollector):
    def __init__(self) -> None:
        super().__init__(LocalOrderBook(), self._publish_unused)
        self.attempts = 0
        self.stop: asyncio.Event | None = None

    async def _publish_unused(self, _) -> None:
        return None

    async def _synchronize_and_stream(self, stop: asyncio.Event) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise OSError("temporary disconnect")
        stop.set()


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_collector_retries_after_transient_disconnect(self) -> None:
        collector = FlakyCollector()
        stop = asyncio.Event()

        await collector.run(stop)

        self.assertEqual(collector.attempts, 2)


if __name__ == "__main__":
    unittest.main()
