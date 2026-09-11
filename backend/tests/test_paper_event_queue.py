import asyncio
import unittest
from typing import Any

from app.api import DashboardState
from app.models import UniverseAsset


class PaperEventQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])

    async def test_paper_subscriber_dropped_on_overflow_and_generator_stops(self) -> None:
        gen = self.state.subscribe("paper")
        first_task = asyncio.create_task(anext(gen))
        await asyncio.sleep(0)
        self.assertEqual(len(self.state._subscribers["paper"]), 1)

        with self.assertLogs("app.api", level="WARNING") as captured_logs:
            for i in range(300):
                await self.state._broadcast("paper", {"type": "TRADE_OPENED", "trade_id": i})

        # Slow subscriber must be disconnected (removed from _subscribers)
        self.assertEqual(len(self.state._subscribers["paper"]), 0)

        # Overflow warning was logged including the channel name
        self.assertTrue(any("paper" in log for log in captured_logs.output))
        self.assertTrue(any("overflow" in log.lower() for log in captured_logs.output))

        # Events already queued before overflow must still be delivered in FIFO order
        first_event = await first_task
        self.assertEqual(first_event, {"type": "TRADE_OPENED", "trade_id": 0})

        remaining: list[dict[str, Any]] = []
        async for event in gen:
            remaining.append(event)

        all_events = [first_event] + remaining
        self.assertEqual(len(all_events), 256)
        self.assertEqual([e["trade_id"] for e in all_events], list(range(256)))

        # Generator terminates after draining queued items
        with self.assertRaises(StopAsyncIteration):
            await anext(gen)

    async def test_paper_subscriber_burst_receives_every_event_in_order(self) -> None:
        gen = self.state.subscribe("paper")
        received: list[dict[str, Any]] = []

        async def consumer() -> None:
            async for event in gen:
                received.append(event)
                if len(received) == 100:
                    break

        consumer_task = asyncio.create_task(consumer())
        await asyncio.sleep(0)
        self.assertEqual(len(self.state._subscribers["paper"]), 1)

        for i in range(100):
            await self.state._broadcast("paper", {"type": "TRADE_OPENED", "trade_id": i})

        await asyncio.wait_for(consumer_task, timeout=2.0)
        self.assertEqual(len(received), 100)
        self.assertEqual([e["trade_id"] for e in received], list(range(100)))
        self.assertEqual(len(self.state._subscribers["paper"]), 1)
        await gen.aclose()

    async def test_scanner_subscriber_idle_coalesces_latest_value(self) -> None:
        gen = self.state.subscribe("scanner")
        first_task = asyncio.create_task(anext(gen))
        await asyncio.sleep(0)
        self.assertEqual(len(self.state._subscribers["scanner"]), 1)

        for i in range(5):
            await self.state._broadcast("scanner", {"update_id": i})

        first = await first_task
        # Coalesced to the most recent update while idle; earlier updates (0..3) dropped
        self.assertEqual(first, {"update_id": 4})

        # Broadcast another burst while subscriber stays idle
        for i in range(10, 15):
            await self.state._broadcast("scanner", {"update_id": i})

        # Latest-value channel coalesces to the newest message; old ones dropped
        latest = await anext(gen)
        self.assertEqual(latest, {"update_id": 14})
        self.assertEqual(len(self.state._subscribers["scanner"]), 1)
        await gen.aclose()

    async def test_symbol_subscriber_idle_coalesces_latest_value(self) -> None:
        channel = "symbol:BTCUSDT"
        gen = self.state.subscribe(channel)
        first_task = asyncio.create_task(anext(gen))
        await asyncio.sleep(0)
        self.assertEqual(len(self.state._subscribers[channel]), 1)

        for i in range(5):
            await self.state._broadcast(channel, {"price": 100.0 + i})

        first = await first_task
        # Coalesced to the most recent price while idle; earlier updates dropped
        self.assertEqual(first, {"price": 104.0})

        for i in range(10, 15):
            await self.state._broadcast(channel, {"price": 100.0 + i})

        latest = await anext(gen)
        self.assertEqual(latest, {"price": 114.0})
        self.assertEqual(len(self.state._subscribers[channel]), 1)
        await gen.aclose()

    async def test_broadcast_never_blocks_on_full_queue(self) -> None:
        gen = self.state.subscribe("paper")
        first_task = asyncio.create_task(anext(gen))
        await asyncio.sleep(0)

        # Fill the queue to capacity
        for i in range(256):
            await self.state._broadcast("paper", {"id": i})

        # Broadcasting to a full non-draining queue must return promptly without blocking
        start = asyncio.get_running_loop().time()
        for i in range(256, 300):
            await asyncio.wait_for(self.state._broadcast("paper", {"id": i}), timeout=0.1)
        duration = asyncio.get_running_loop().time() - start
        self.assertLess(duration, 1.0)
        await first_task
        await gen.aclose()

    def test_queue_policy_per_channel(self) -> None:
        paper_policy = self.state._policy_for("paper")
        self.assertEqual(paper_policy.maxsize, 256)
        self.assertFalse(paper_policy.drop_oldest)

        scanner_policy = self.state._policy_for("scanner")
        self.assertEqual(scanner_policy.maxsize, 1)
        self.assertTrue(scanner_policy.drop_oldest)

        symbol_policy = self.state._policy_for("symbol:BTCUSDT")
        self.assertEqual(symbol_policy.maxsize, 1)
        self.assertTrue(symbol_policy.drop_oldest)

    async def test_multiple_subscribers_isolation(self) -> None:
        fast_gen = self.state.subscribe("paper")
        slow_gen = self.state.subscribe("paper")

        fast_received: list[dict[str, Any]] = []

        async def fast_consumer() -> None:
            async for event in fast_gen:
                fast_received.append(event)
                if len(fast_received) == 300:
                    break

        fast_task = asyncio.create_task(fast_consumer())
        slow_first_task = asyncio.create_task(anext(slow_gen))
        await asyncio.sleep(0)
        self.assertEqual(len(self.state._subscribers["paper"]), 2)

        for i in range(300):
            await self.state._broadcast("paper", {"trade_id": i})
            await asyncio.sleep(0)

        await asyncio.wait_for(fast_task, timeout=2.0)
        self.assertEqual(len(fast_received), 300)
        self.assertEqual([e["trade_id"] for e in fast_received], list(range(300)))

        # Slow subscriber was dropped for overflow; only fast subscriber remained
        self.assertEqual(len(self.state._subscribers["paper"]), 1)
        await fast_gen.aclose()
        self.assertEqual(len(self.state._subscribers["paper"]), 0)

        # Slow subscriber drains only its pre-overflow items and terminates
        slow_first = await slow_first_task
        slow_rest: list[dict[str, Any]] = []
        async for event in slow_gen:
            slow_rest.append(event)
        # Slow subscriber received up to its queue capacity and terminated
        self.assertIn(len([slow_first] + slow_rest), (256, 257))
        with self.assertRaises(StopAsyncIteration):
            await anext(slow_gen)


if __name__ == "__main__":
    unittest.main()
