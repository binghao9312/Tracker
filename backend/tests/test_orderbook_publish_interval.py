"""The publish interval is bounded on both sides; neither bound is obvious.

Order-book publishing is the dominant CPU cost in this process: each publish does a
top-N selection and builds ~400 pydantic PriceLevel objects, and it runs once per
exchange-market-symbol pair. Nothing consumes the publish *stream* -- `on_order_book`
overwrites `self._books[key]` and every reader takes the latest stored value -- so the
interval buys freshness only, and raising it is close to free CPU.

It cannot be raised freely, though, and the ceiling is not self-evident from the
publish code: the cadence loop only writes a market_metrics row when the book's
timestamp has moved past its watermark, so an interval at or above the 1s cadence
would intermittently produce no new book between two ticks and silently drop rows.

These tests exist so that someone tuning the interval for CPU has the upper bound in
front of them rather than discovering it as missing data days later.
"""

from __future__ import annotations

import unittest

from app.collectors.orderbooks import ORDERBOOK_PUBLISH_MIN_INTERVAL_MS
from app.runtime import ORDERBOOK_STALE_AFTER_SECONDS

CADENCE_SECONDS = 1.0


class PublishIntervalBoundsTests(unittest.TestCase):
    def test_interval_leaves_several_publishes_per_cadence_tick(self) -> None:
        publishes_per_tick = CADENCE_SECONDS * 1000 / ORDERBOOK_PUBLISH_MIN_INTERVAL_MS
        self.assertGreaterEqual(
            publishes_per_tick,
            2.0,
            msg=(
                f"{ORDERBOOK_PUBLISH_MIN_INTERVAL_MS}ms gives only {publishes_per_tick:.1f} "
                f"publishes per {CADENCE_SECONDS}s cadence tick. The cadence writes a row "
                "only when the book timestamp passes its watermark, so at or below one "
                "publish per tick it starts dropping market_metrics rows."
            ),
        )

    def test_interval_is_far_inside_the_staleness_window(self) -> None:
        stale_ms = ORDERBOOK_STALE_AFTER_SECONDS * 1000
        self.assertLess(
            ORDERBOOK_PUBLISH_MIN_INTERVAL_MS * 4,
            stale_ms,
            msg=(
                f"{ORDERBOOK_PUBLISH_MIN_INTERVAL_MS}ms is too close to the "
                f"{stale_ms}ms staleness cutoff; books would flap in and out of "
                "'available' on ordinary jitter."
            ),
        )

    def test_interval_is_positive_and_actually_throttles(self) -> None:
        # A zero or negative interval would publish on every depth frame, which is the
        # cost profile that saturated the event loop in production.
        self.assertGreater(ORDERBOOK_PUBLISH_MIN_INTERVAL_MS, 0)


if __name__ == "__main__":
    unittest.main()
