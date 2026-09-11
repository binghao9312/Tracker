import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from time import time_ns
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import DashboardState
from app.database import Base
from app.models import (
    Exchange,
    MarketType,
    NormalizedTrade,
    OrderBook,
    PriceLevel,
    UniverseAsset,
)
from app.repository import MetricRepository
from app.runtime import LiveRuntime


class SyncToAsyncSession:
    def __init__(self, sync_session: Any) -> None:
        self.session = sync_session

    async def __aenter__(self) -> "SyncToAsyncSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.session.close()

    async def execute(self, statement: object, params: object = None) -> Any:
        return self.session.execute(statement, params)

    async def scalars(self, statement: object, params: object = None) -> Any:
        return self.session.scalars(statement, params)


class SyncToAsyncSessionFactory:
    def __init__(self, sync_sessionmaker: Any) -> None:
        self._factory = sync_sessionmaker

    def __call__(self) -> SyncToAsyncSession:
        return SyncToAsyncSession(self._factory())

    def begin(self) -> Any:
        class TransactionContext:
            def __init__(self, sync_session: Any) -> None:
                self.session = sync_session

            async def __aenter__(self) -> SyncToAsyncSession:
                return SyncToAsyncSession(self.session)

            async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
                if exc_type is not None:
                    self.session.rollback()
                else:
                    self.session.commit()
                self.session.close()

        return TransactionContext(self._factory())


def _utc(value: datetime) -> datetime:
    """SQLite does not persist tzinfo; normalize before an exact comparison."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _comparable(value: object) -> object:
    """Normalize only datetimes; every other field is compared exactly as stored."""
    return _utc(value) if isinstance(value, datetime) else value


def _build_test_repository() -> MetricRepository:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sync_factory = sessionmaker(engine, expire_on_commit=False)
    return MetricRepository(SyncToAsyncSessionFactory(sync_factory))  # type: ignore[arg-type]


class RecordingMetrics:
    def __init__(self) -> None:
        self.markets: list[dict[str, Any]] = []
        self.flows: list[dict[str, Any]] = []
        self.derivatives: list[dict[str, Any]] = []
        self.signals: list[dict[str, Any]] = []

    async def append_market(self, values: Mapping[str, Any]) -> None:
        self.markets.append(dict(values))

    async def append_flow(self, values: Mapping[str, Any]) -> None:
        self.flows.append(dict(values))

    async def append_derivative(self, values: Mapping[str, Any]) -> None:
        self.derivatives.append(dict(values))

    async def append_signal(self, values: Mapping[str, Any]) -> None:
        self.signals.append(dict(values))

    async def persist_signals(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.signals.extend(dict(r) for r in rows)

    async def append_batch(
        self,
        *,
        markets: Sequence[Mapping[str, Any]] | None = None,
        flows: Sequence[Mapping[str, Any]] | None = None,
        derivatives: Sequence[Mapping[str, Any]] | None = None,
        signals: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        if markets:
            self.markets.extend(dict(v) for v in markets)
        if flows:
            self.flows.extend(dict(v) for v in flows)
        if derivatives:
            self.derivatives.extend(dict(v) for v in derivatives)
        if signals:
            self.signals.extend(dict(v) for v in signals)


class SignalPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_row_round_trips_with_every_field_intact(self) -> None:
        repo = _build_test_repository()
        base_time = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
        rows = [
            {
                "timestamp": base_time + timedelta(seconds=1),
                "symbol": "BTCUSDT",
                "price": 60500.5,
                "activity_score": 85.25,
                "liquidity_fragility": 33.1,
                "move_type": "BREAKOUT",
                "cross_exchange_state": "CONFIRMED",
                "oi_change_5m": 0.012,
                "oi_change_15m": 0.024,
                "oi_change_1h": -0.005,
                "funding_rate": 0.0001,
            },
            {
                "timestamp": base_time,
                "symbol": "BTCUSDT",
                "price": 60400.0,
                "activity_score": 80.0,
                "liquidity_fragility": 30.0,
                "move_type": "ABSORPTION",
                "cross_exchange_state": "NEUTRAL",
                "oi_change_5m": 0.01,
                "oi_change_15m": 0.02,
                "oi_change_1h": 0.03,
                "funding_rate": 0.00012,
            },
        ]

        await repo.persist_signals(rows)

        history = await repo.signal_history(
            "BTCUSDT",
            start_time=base_time - timedelta(minutes=1),
            end_time=base_time + timedelta(minutes=1),
        )

        self.assertEqual(len(history), 2)
        # Verify strictly ascending order by timestamp.
        self.assertEqual(_utc(history[0]["timestamp"]), base_time)
        self.assertEqual(_utc(history[1]["timestamp"]), base_time + timedelta(seconds=1))

        # Check every field intact on the first row.
        earlier_row = rows[1]
        for key, expected in earlier_row.items():
            self.assertEqual(_comparable(history[0][key]), _comparable(expected))

        # Check every field intact on the second row.
        later_row = rows[0]
        for key, expected in later_row.items():
            self.assertEqual(_comparable(history[1][key]), _comparable(expected))

    async def test_null_activity_score_comes_back_as_none_not_zero(self) -> None:
        repo = _build_test_repository()
        now = datetime(2026, 9, 11, 14, 0, 0, tzinfo=UTC)
        row = {
            "timestamp": now,
            "symbol": "SOLUSDT",
            "price": 145.0,
            "activity_score": None,
            "liquidity_fragility": None,
            "move_type": None,
            "cross_exchange_state": None,
            "oi_change_5m": None,
            "oi_change_15m": None,
            "oi_change_1h": None,
            "funding_rate": None,
        }

        await repo.persist_signals([row])

        history = await repo.signal_history(
            "SOLUSDT",
            start_time=now - timedelta(seconds=10),
            end_time=now + timedelta(seconds=10),
        )

        self.assertEqual(len(history), 1)
        # NULL activity_score must be None, not coerced to 0.0.
        self.assertIsNone(history[0]["activity_score"])
        self.assertIsNot(history[0]["activity_score"], 0.0)
        self.assertIsNone(history[0]["liquidity_fragility"])
        self.assertIsNone(history[0]["move_type"])
        self.assertIsNone(history[0]["cross_exchange_state"])
        self.assertIsNone(history[0]["funding_rate"])

    async def test_signal_history_honours_limit_and_symbol_filter(self) -> None:
        repo = _build_test_repository()
        base_time = datetime(2026, 9, 11, 15, 0, 0, tzinfo=UTC)
        btc_rows = [
            {
                "timestamp": base_time + timedelta(seconds=i),
                "symbol": "BTCUSDT",
                "price": 60000.0 + i,
                "activity_score": 75.0 + i,
                "liquidity_fragility": 20.0,
                "move_type": "NEUTRAL",
                "cross_exchange_state": "NEUTRAL",
                "oi_change_5m": 0.0,
                "oi_change_15m": 0.0,
                "oi_change_1h": 0.0,
                "funding_rate": 0.0001,
            }
            for i in range(5)
        ]
        eth_rows = [
            {
                "timestamp": base_time + timedelta(seconds=i),
                "symbol": "ETHUSDT",
                "price": 3000.0 + i,
                "activity_score": 80.0 + i,
                "liquidity_fragility": 25.0,
                "move_type": "NEUTRAL",
                "cross_exchange_state": "NEUTRAL",
                "oi_change_5m": 0.0,
                "oi_change_15m": 0.0,
                "oi_change_1h": 0.0,
                "funding_rate": 0.0001,
            }
            for i in range(3)
        ]

        await repo.persist_signals(btc_rows + eth_rows)

        # Query BTCUSDT with limit=3.
        history = await repo.signal_history(
            "BTCUSDT",
            start_time=base_time,
            end_time=base_time + timedelta(seconds=10),
            limit=3,
        )

        self.assertEqual(len(history), 3)
        # Must never return ETHUSDT rows.
        for item in history:
            self.assertEqual(item["symbol"], "BTCUSDT")

        # Must be ascending by timestamp.
        self.assertEqual(_utc(history[0]["timestamp"]), base_time)
        self.assertEqual(_utc(history[1]["timestamp"]), base_time + timedelta(seconds=1))
        self.assertEqual(_utc(history[2]["timestamp"]), base_time + timedelta(seconds=2))

        # Query ETHUSDT.
        eth_history = await repo.signal_history(
            "ETHUSDT",
            start_time=base_time,
            end_time=base_time + timedelta(seconds=10),
        )
        self.assertEqual(len(eth_history), 3)
        for item in eth_history:
            self.assertEqual(item["symbol"], "ETHUSDT")

    async def test_flush_emits_one_signal_row_per_symbol_matching_the_published_score(
        self,
    ) -> None:
        state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])
        metrics = RecordingMetrics()
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
        published_score = detail["activity_score"]
        self.assertIsNotNone(published_score)

        # flush() must emit exactly one signal row for BTCUSDT.
        self.assertEqual(len(metrics.signals), 1)
        signal = metrics.signals[0]
        self.assertEqual(signal["symbol"], "BTCUSDT")
        # Carrying the same activity_score that the published detail carries.
        self.assertEqual(signal["activity_score"], published_score)
        self.assertEqual(signal["price"], detail["price"])
        self.assertEqual(signal["liquidity_fragility"], detail["liquidity_fragility"])
        self.assertEqual(signal["move_type"], detail["move_type"])
        self.assertEqual(signal["cross_exchange_state"], detail["cross_exchange_state"])
        self.assertIsInstance(signal["timestamp"], datetime)

    async def test_runtime_flush_end_to_end_with_metric_repository(self) -> None:
        state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])
        repo = _build_test_repository()
        runtime = LiveRuntime(state, repo)
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
        now = datetime.now(UTC)
        history = await repo.signal_history(
            "BTCUSDT",
            start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(minutes=5),
        )

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["symbol"], "BTCUSDT")
        self.assertEqual(history[0]["activity_score"], detail["activity_score"])
