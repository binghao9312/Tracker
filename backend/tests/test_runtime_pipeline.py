import unittest
from time import time_ns

from app.api import DashboardState
from app.discovery import DiscoveryResult
from app.models import (
    DerivativeSnapshot,
    Exchange,
    MarketType,
    NormalizedTrade,
    OrderBook,
    PriceLevel,
    UniverseAsset,
)
from app.paper_trading import PaperTradingEngine, PaperTradingSettings
from app.runtime import LiveRuntime
from tests.paper_helpers import MemoryPaperRepository


class MemoryMetrics:
    def __init__(self) -> None:
        self.market: list[dict] = []
        self.flow: list[dict] = []
        self.derivative: list[dict] = []

    async def append_market(self, values: dict) -> None:
        self.market.append(dict(values))

    async def append_flow(self, values: dict) -> None:
        self.flow.append(dict(values))

    async def append_derivative(self, values: dict) -> None:
        self.derivative.append(dict(values))


class EmptyDiscovery:
    async def discover(self, _: object) -> DiscoveryResult:
        return DiscoveryResult(markets=[], failures={})


class RuntimePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_flush_persists_canonical_state_and_drives_paper_entry(self) -> None:
        repository = MemoryPaperRepository()
        state = DashboardState(
            [UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")],
            PaperTradingEngine(repository, PaperTradingSettings(signal_persistence_seconds=0)),
        )
        metrics = MemoryMetrics()
        runtime = LiveRuntime(state, metrics)
        now_ms = time_ns() // 1_000_000
        await runtime.on_order_book(
            OrderBook(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                market=MarketType.PERP,
                timestamp=now_ms,
                bids=[PriceLevel(price=99, quantity=100)],
                asks=[PriceLevel(price=101, quantity=100)],
            )
        )
        await runtime.on_trade(
            NormalizedTrade(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                market=MarketType.PERP,
                timestamp=now_ms,
                price=101,
                quantity=500,
                quote_value=50_500,
                side="BUY",
            )
        )

        await runtime.flush()

        detail = state.detail("BTCUSDT")
        self.assertIsNotNone(detail)
        self.assertGreaterEqual(detail["activity_score"], 80)
        self.assertEqual(len(metrics.market), 1)
        self.assertEqual(len(repository.rows), 1)

    async def test_market_persistence_uses_source_watermarks_and_stale_books(self) -> None:
        state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])
        metrics = MemoryMetrics()
        runtime = LiveRuntime(state, metrics)
        now_ms = time_ns() // 1_000_000

        book = OrderBook(
            exchange=Exchange.BINANCE,
            symbol="BTCUSDT",
            market=MarketType.PERP,
            timestamp=now_ms,
            received_at=now_ms,
            bids=[PriceLevel(price=99, quantity=100)],
            asks=[PriceLevel(price=101, quantity=100)],
        )
        await runtime.on_order_book(book)
        for _ in range(10):
            await runtime.flush()
        self.assertEqual(len(metrics.market), 1)

        updated = book.model_copy(
            update={"timestamp": now_ms + 1, "received_at": now_ms + 1}
        )
        await runtime.on_order_book(updated)
        await runtime.flush()
        self.assertEqual(len(metrics.market), 2)

        await runtime.on_order_book(
            updated.model_copy(
                update={
                    "timestamp": now_ms + 2,
                    "received_at": now_ms - 11_000,
                }
            )
        )
        await runtime.flush()
        self.assertEqual(len(metrics.market), 2)
        self.assertIsNone(state.detail("BTCUSDT")["orderbooks"]["binance"]["perp"])
        self.assertIsNone(state.detail("BTCUSDT")["liquidity_fragility"])

    async def test_stale_depth_keeps_trade_cvd_live_without_pressure(self) -> None:
        state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])
        metrics = MemoryMetrics()
        runtime = LiveRuntime(state, metrics)
        now_ms = time_ns() // 1_000_000
        await runtime.on_order_book(
            OrderBook(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                market=MarketType.PERP,
                timestamp=now_ms - 11_000,
                received_at=now_ms - 11_000,
                bids=[PriceLevel(price=99, quantity=100)],
                asks=[PriceLevel(price=101, quantity=100)],
            )
        )
        await runtime.on_trade(
            NormalizedTrade(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                market=MarketType.PERP,
                timestamp=now_ms,
                price=101,
                quantity=1,
                quote_value=101,
                side="BUY",
            )
        )

        await runtime.flush()

        detail = state.detail("BTCUSDT")
        self.assertEqual(len(metrics.market), 0)
        self.assertEqual(len(metrics.flow), 1)
        self.assertEqual(detail["perp"]["cvd_5m"], 101)
        self.assertIsNone(detail["perp"]["buy_pressure_5m"])
        self.assertIsNone(metrics.flow[0]["buy_pressure_5m"])

    async def test_derivative_persistence_uses_source_timestamp_watermark(self) -> None:
        state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])
        metrics = MemoryMetrics()
        runtime = LiveRuntime(state, metrics)
        now_ms = time_ns() // 1_000_000
        await runtime.on_order_book(
            OrderBook(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                market=MarketType.PERP,
                timestamp=now_ms,
                received_at=now_ms,
                bids=[PriceLevel(price=99, quantity=100)],
                asks=[PriceLevel(price=101, quantity=100)],
            )
        )
        first = DerivativeSnapshot(
            exchange=Exchange.BINANCE,
            symbol="BTCUSDT",
            timestamp=now_ms,
            received_at=now_ms,
            open_interest=10,
            open_interest_usd=1_000,
            funding_rate=0.01,
            mark_price=100,
        )
        await runtime.on_derivative(first)
        await runtime.flush()
        await runtime.flush()
        self.assertEqual(len(metrics.derivative), 1)

        await runtime.on_derivative(
            first.model_copy(update={"timestamp": now_ms + 1, "received_at": now_ms + 1})
        )
        await runtime.flush()
        self.assertEqual(len(metrics.derivative), 2)

    async def test_older_derivative_response_does_not_regress_latest_state(self) -> None:
        runtime = LiveRuntime(DashboardState([]), MemoryMetrics())
        now_ms = time_ns() // 1_000_000
        latest = DerivativeSnapshot(
            exchange=Exchange.BINANCE,
            symbol="BTCUSDT",
            timestamp=now_ms,
            received_at=now_ms,
            open_interest=10,
            open_interest_usd=1_000,
            funding_rate=0.01,
            mark_price=100,
        )
        await runtime.on_derivative(latest)
        await runtime.on_derivative(
            latest.model_copy(
                update={
                    "timestamp": now_ms - 1,
                    "received_at": now_ms + 1,
                    "open_interest": 9,
                }
            )
        )

        self.assertIs(runtime._latest_derivative(Exchange.BINANCE, "BTCUSDT"), latest)

    async def test_stop_cancels_runtime_cadence_task(self) -> None:
        state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])
        runtime = LiveRuntime(state, MemoryMetrics(), session=object(), discovery=EmptyDiscovery())
        await runtime.start()
        await runtime.stop()
        self.assertEqual(runtime._tasks, [])

    async def test_prune_historical_metrics_delegates_cutoff_to_metrics(self) -> None:
        state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])

        class PruningMetrics(MemoryMetrics):
            def __init__(self) -> None:
                super().__init__()
                self.pruned_before = None

            async def prune_metrics(self, before: object) -> int:
                self.pruned_before = before
                return 42

        metrics = PruningMetrics()
        runtime = LiveRuntime(state, metrics)
        count = await runtime.prune_historical_metrics()
        self.assertEqual(count, 42)
        self.assertIsNotNone(metrics.pruned_before)
