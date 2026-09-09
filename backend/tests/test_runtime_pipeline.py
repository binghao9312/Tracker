import unittest
from time import time_ns

from app.api import DashboardState
from app.discovery import DiscoveryResult
from app.models import Exchange, MarketType, NormalizedTrade, OrderBook, PriceLevel, UniverseAsset
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
