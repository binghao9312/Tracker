import asyncio
import unittest
from time import monotonic
from types import SimpleNamespace

from app.collectors.derivatives import _status_retry_delay
from app.exchanges.derivatives import BinanceDerivativeScheduler


class BinanceDerivativeSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_many_symbols_are_bounded_and_evenly_paced(self) -> None:
        scheduler = BinanceDerivativeScheduler(6, cadence_seconds=0.06)
        active = 0
        peak = 0
        started: list[float] = []

        async def fetch() -> None:
            nonlocal active, peak
            async with scheduler.slot():
                started.append(monotonic())
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.002)
                active -= 1

        await asyncio.gather(*(fetch() for _ in range(6)))

        self.assertLessEqual(peak, 3)
        self.assertTrue(all(b > a for a, b in zip(started, started[1:], strict=False)))

    async def test_waiters_observe_cooldown_set_while_paced(self) -> None:
        scheduler = BinanceDerivativeScheduler(3, cadence_seconds=0.03)
        scheduler._backoff = 0.03
        started: list[float] = []

        async def fetch(index: int) -> None:
            async with scheduler.slot():
                started.append(monotonic())
                if index == 0:
                    await scheduler.report_error(
                        SimpleNamespace(status=429, headers={"Retry-After": "0.03"})
                    )

        await asyncio.gather(*(fetch(index) for index in range(3)))

        self.assertGreaterEqual(started[1] - started[0], 0.025)

    async def test_rate_limit_and_ban_set_global_cooldowns(self) -> None:
        scheduler = BinanceDerivativeScheduler(1)
        await scheduler.report_error(SimpleNamespace(status=429, headers={}))
        first = scheduler.retry_delay
        await scheduler.report_error(SimpleNamespace(status=429, headers={}))
        second = scheduler.retry_delay
        await scheduler.report_error(SimpleNamespace(status=418, headers={"Retry-After": "1"}))

        self.assertGreaterEqual(first, 1.9)
        self.assertGreaterEqual(second, 3.9)
        self.assertGreaterEqual(scheduler.retry_delay, 59.9)

    def test_collector_honors_retry_after_and_minimum_ban_cooldown(self) -> None:
        self.assertEqual(
            _status_retry_delay(SimpleNamespace(status=429, headers={"Retry-After": "7"}), 2),
            7,
        )
        self.assertEqual(
            _status_retry_delay(SimpleNamespace(status=418, headers={"Retry-After": "1"}), 2),
            60,
        )


if __name__ == "__main__":
    unittest.main()
