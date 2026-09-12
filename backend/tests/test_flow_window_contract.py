"""Behavioural contract for RollingTradeFlow.windows(), plus a scaling guard.

`windows()` is on the per-second cadence path and runs once per exchange-market-
symbol pair, so its cost is multiplied by ~163 before it meets a 1s budget. A
measurement on 2026-09-12 put it at 5.55ms/call at 50 trades/s (0.9x budget) and
10.72ms at 100 trades/s (1.7x budget) -- the backend was pegged at 99.5% CPU and
the cadence loop had stretched to one flush per 61 seconds against a configured 1s.

The obvious fix is to stop rescanning trades on every call and accumulate
incrementally instead. That is a rewrite of the CVD maths, which is exactly the
kind of change that runs cleanly while returning a confident wrong number, so the
answers are pinned here against a deliberately naive reference before anything is
optimised. `ReferenceFlow` is written for obviousness, never for speed: if it and
the real implementation disagree, the real implementation is wrong.
"""

from __future__ import annotations

import random
import time
import unittest

from app.flow import WINDOWS_SECONDS, RollingTradeFlow
from app.models import Exchange, MarketType, NormalizedTrade

RETENTION_MS = 3_600_000


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


class ReferenceFlow:
    """Naive, obviously-correct restatement of the windows() contract."""

    def __init__(self) -> None:
        self._trades: list[NormalizedTrade] = []
        self._high_water = -(10**18)

    def add_trade(self, t: NormalizedTrade) -> None:
        self._trades.append(t)
        self._high_water = max(self._high_water, t.timestamp)
        self._drop_expired(self._high_water)

    def _drop_expired(self, now_ms: int) -> None:
        cutoff = now_ms - RETENTION_MS
        self._trades = [t for t in self._trades if t.timestamp >= cutoff]

    def windows(self, now_ms: int, seconds: tuple[int, ...]) -> dict[int, tuple[float, float]]:
        self._drop_expired(now_ms)
        out: dict[int, tuple[float, float]] = {}
        for w in seconds:
            cutoff = now_ms - w * 1000
            buy = sum(t.quote_value for t in self._trades if t.timestamp >= cutoff and t.side == "BUY")
            sell = sum(
                t.quote_value for t in self._trades if t.timestamp >= cutoff and t.side == "SELL"
            )
            out[w] = (buy, sell)
        return out


class WindowContractTests(unittest.TestCase):
    """Every claim here must stay true through any performance rewrite."""

    def _assert_matches_reference(self, seed: int, out_of_order_rate: float) -> None:
        rnd = random.Random(seed)
        real, ref = RollingTradeFlow(), ReferenceFlow()
        t0 = 1_700_000_000_000
        now = t0

        for step in range(1500):
            ts = t0 + step * 400
            if rnd.random() < out_of_order_rate:
                ts -= rnd.randrange(1_000, 90_000)
            t = trade(ts, rnd.choice(("BUY", "SELL")), rnd.randrange(1, 500))
            real.add_trade(t)
            ref.add_trade(t)
            now = max(now, ts)

            if step % 60 == 0:
                got = real.windows(now, requested_seconds=WINDOWS_SECONDS)
                want = ref.windows(now, WINDOWS_SECONDS)
                for w in WINDOWS_SECONDS:
                    self.assertAlmostEqual(
                        got[w].buy_volume, want[w][0], places=6,
                        msg=f"buy_volume mismatch at step={step} window={w}s",
                    )
                    self.assertAlmostEqual(
                        got[w].sell_volume, want[w][1], places=6,
                        msg=f"sell_volume mismatch at step={step} window={w}s",
                    )
                    self.assertAlmostEqual(
                        got[w].cvd, want[w][0] - want[w][1], places=6,
                        msg=f"cvd mismatch at step={step} window={w}s",
                    )

    def test_matches_reference_on_in_order_stream(self) -> None:
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                self._assert_matches_reference(seed, out_of_order_rate=0.0)

    def test_matches_reference_when_trades_arrive_late(self) -> None:
        # Two venues on one clock: late arrivals are normal, not exceptional.
        for seed in (11, 12, 13):
            with self.subTest(seed=seed):
                self._assert_matches_reference(seed, out_of_order_rate=0.15)

    def test_trade_exactly_on_the_cutoff_is_inside_the_window(self) -> None:
        flow = RollingTradeFlow()
        flow.add_trade(trade(100_000, "BUY", 7))
        # 60s window queried at 160_000 puts the cutoff exactly on the trade.
        self.assertEqual(flow.windows(160_000, requested_seconds=(60,))[60].buy_volume, 7)
        # One millisecond later it has fallen out.
        self.assertEqual(flow.windows(160_001, requested_seconds=(60,))[60].buy_volume, 0)

    def test_empty_flow_reports_zero_volume_and_undefined_ratio(self) -> None:
        window = RollingTradeFlow().windows(500_000, requested_seconds=(60,))[60]
        self.assertEqual(window.buy_volume, 0)
        self.assertEqual(window.sell_volume, 0)
        self.assertEqual(window.delta, 0)
        self.assertEqual(window.cvd, 0)
        self.assertIsNone(window.buy_sell_ratio)

    def test_ratio_is_undefined_rather_than_infinite_with_no_sells(self) -> None:
        flow = RollingTradeFlow()
        flow.add_trade(trade(10_000, "BUY", 50))
        self.assertIsNone(flow.windows(10_000, requested_seconds=(60,))[60].buy_sell_ratio)

    def test_trades_older_than_the_retention_horizon_are_dropped(self) -> None:
        flow = RollingTradeFlow()
        flow.add_trade(trade(0, "BUY", 999))
        flow.add_trade(trade(RETENTION_MS + 1_000, "BUY", 1))
        # The 3600s window at the later timestamp must not resurrect the old trade.
        self.assertEqual(
            flow.windows(RETENTION_MS + 1_000, requested_seconds=(3600,))[3600].buy_volume, 1
        )

    def test_unsupported_window_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RollingTradeFlow().windows(0, requested_seconds=(42,))

    def test_requesting_a_subset_does_not_change_that_subset_s_answers(self) -> None:
        # runtime.py asks for (60, 300) only; that must equal what the full call reports.
        rnd = random.Random(99)
        flow = RollingTradeFlow()
        for step in range(800):
            flow.add_trade(trade(step * 500, rnd.choice(("BUY", "SELL")), rnd.randrange(1, 99)))
        now = 800 * 500
        full = flow.windows(now, requested_seconds=WINDOWS_SECONDS)
        subset = flow.windows(now, requested_seconds=(60, 300))
        for w in (60, 300):
            self.assertAlmostEqual(subset[w].buy_volume, full[w].buy_volume, places=6)
            self.assertAlmostEqual(subset[w].sell_volume, full[w].sell_volume, places=6)


class WindowScalingTests(unittest.TestCase):
    """The cost of a query must not track how many trades are being held.

    This is the actual defect: at a fixed query rate the cadence loop got slower as
    market activity rose, which is the wrong shape for something on a fixed budget.
    A rewrite that keeps running totals passes this with a ratio near 1; the
    rescan-everything version fails it in proportion to the extra trades.
    """

    @staticmethod
    def _build(rate_per_second: int, span_seconds: int) -> tuple[RollingTradeFlow, int]:
        rnd = random.Random(5)
        flow = RollingTradeFlow()
        t0 = 1_700_000_000_000
        for s in range(span_seconds):
            base = t0 + s * 1000
            for i in range(rate_per_second):
                flow.add_trade(
                    trade(base + (i * 1000) // rate_per_second, rnd.choice(("BUY", "SELL")), 10)
                )
        return flow, t0 + span_seconds * 1000

    @staticmethod
    def _best_of(flow: RollingTradeFlow, now: int, rounds: int = 5) -> float:
        best = float("inf")
        for _ in range(rounds):
            start = time.perf_counter()
            for _ in range(10):
                flow.windows(now, requested_seconds=(60, 300))
            best = min(best, (time.perf_counter() - start) / 10)
        return best

    def test_query_cost_does_not_grow_with_trade_volume(self) -> None:
        span = 300
        light, light_now = self._build(8, span)
        heavy, heavy_now = self._build(64, span)  # 8x the trades over the same span

        self._best_of(light, light_now, rounds=2)  # warm up the interpreter
        light_cost = self._best_of(light, light_now)
        heavy_cost = self._best_of(heavy, heavy_now)

        ratio = heavy_cost / light_cost if light_cost > 0 else float("inf")
        self.assertLess(
            ratio,
            3.0,
            msg=(
                f"windows() cost scales with held trades: 8x the trades cost {ratio:.1f}x "
                f"the time ({light_cost * 1e6:.0f}us -> {heavy_cost * 1e6:.0f}us). "
                "Keep running totals instead of rescanning the trade history per call."
            ),
        )


if __name__ == "__main__":
    unittest.main()
