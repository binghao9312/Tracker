"""Live public-market pipeline from discovery through dashboard and paper trading."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from time import time_ns
from typing import Any

import aiohttp

from app.api import DashboardState
from app.collectors.derivatives import DerivativePollingCollector
from app.collectors.orderbooks import (
    BINANCE_BOOK_CHUNK_SIZE,
    BinanceOrderBookManager,
    BinanceSnapshotScheduler,
    OkxOrderBookManager,
)
from app.collectors.trades import BinanceTradeManager, OkxTradeManager
from app.discovery import MarketDiscoveryService
from app.exchanges.binance import BinanceAdapter
from app.exchanges.derivatives import (
    BinanceDerivativeScheduler,
    BinanceDerivativesProvider,
    OkxDerivativeScheduler,
    OkxDerivativesProvider,
)
from app.exchanges.okx import OkxAdapter
from app.flow import RollingTradeFlow, pressure
from app.http import AiohttpJsonClient
from app.liquidity import LiquidityMetrics, calculate_liquidity
from app.models import (
    DerivativeSnapshot,
    Exchange,
    MarketInstrument,
    MarketType,
    NormalizedTrade,
    OrderBook,
)
from app.orderbook import LocalOrderBook
from app.repository import MetricRepository
from app.scoring import (
    ClassificationThresholds,
    MarketSignal,
    MoveType,
    activity_score,
    aggregate_move_type,
    classify_move,
    cross_exchange_state,
    exchange_directions,
    liquidity_fragility_scores,
)

logger = logging.getLogger(__name__)

_OI_WINDOWS = {
    60: "1m",
    300: "5m",
    900: "15m",
    3_600: "1h",
}
_OI_MAX_TARGET_DISTANCE_MS = 60_000
_OI_RETENTION_MS = 3_600_000 + _OI_MAX_TARGET_DISTANCE_MS
_OI_MIN_WINDOW_FRACTION = 0.75

ORDERBOOK_STALE_AFTER_SECONDS = 10
DERIVATIVE_STALE_AFTER_SECONDS = 60
BINANCE_DERIVATIVE_CADENCE_SECONDS = 12
_RUNTIME_WORKER_RESTART_SECONDS = 1.0


class LiveRuntime:
    """Owns public collectors, canonical aggregation, persistence, and clean shutdown."""

    def __init__(
        self,
        state: DashboardState,
        metrics: MetricRepository,
        *,
        session: aiohttp.ClientSession | None = None,
        discovery: MarketDiscoveryService | None = None,
        thresholds: ClassificationThresholds | None = None,
        metric_history_days: int = 30,
        cadence_seconds: float = 1.0,
    ) -> None:
        self.state = state
        self.metrics = metrics
        self._session = session
        self._owns_session = session is None
        self._discovery = discovery
        self._thresholds = thresholds or ClassificationThresholds()
        self._cadence_seconds = cadence_seconds
        self._retention_hours = metric_history_days * 24
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._books: dict[tuple[Exchange, str, MarketType], OrderBook] = {}
        self._flows: dict[tuple[Exchange, str, MarketType], RollingTradeFlow] = {}
        self._derivatives: dict[tuple[Exchange, str], deque[DerivativeSnapshot]] = defaultdict(
            deque
        )
        self._dirty_symbols: set[str] = set()
        self._known_book_keys: set[tuple[Exchange, str, MarketType]] = set()
        self._book_states: dict[tuple[Exchange, str, MarketType], str] = {}
        self._market_watermarks: dict[tuple[Exchange, str, MarketType], int] = {}
        self._derivative_states: dict[tuple[Exchange, str], str] = {}
        self._derivative_watermarks: dict[tuple[Exchange, str], int] = {}
        self._binance_derivative_scheduler: BinanceDerivativeScheduler | None = None
        self._okx_derivative_scheduler: OkxDerivativeScheduler | None = None
        self._binance_snapshot_scheduler = BinanceSnapshotScheduler()
        self.discovered_markets: list[MarketInstrument] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            adapters = self._adapters()
            discovery = self._discovery or MarketDiscoveryService(list(adapters.values()))
            result = await discovery.discover(self.state.universe)
            self.discovered_markets = result.markets
            self._known_book_keys = {
                (instrument.exchange, instrument.symbol, instrument.market)
                for instrument in result.markets
            }
            groups: defaultdict[tuple[Exchange, MarketType], list[MarketInstrument]] = defaultdict(
                list
            )
            for instrument in result.markets:
                groups[(instrument.exchange, instrument.market)].append(instrument)
            for (exchange, market), instruments in groups.items():
                self._tasks.extend(self._stream_tasks(exchange, market, instruments, adapters))
            perp_instruments = [m for m in result.markets if m.market is MarketType.PERP]
            binance_perps = [m for m in perp_instruments if m.exchange is Exchange.BINANCE]
            okx_perps = [m for m in perp_instruments if m.exchange is Exchange.OKX]
            self._binance_derivative_scheduler = BinanceDerivativeScheduler(
                len(binance_perps), cadence_seconds=BINANCE_DERIVATIVE_CADENCE_SECONDS
            )
            self._okx_derivative_scheduler = OkxDerivativeScheduler(len(okx_perps))
            for index, instrument in enumerate(perp_instruments):
                self._tasks.append(self._derivative_task(instrument, index=index))
            self._tasks.append(self._supervised_task(self._run_cadence, name="metric-cadence"))
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None

    def _supervised_task(
        self, worker: Callable[[], Awaitable[None]], *, name: str
    ) -> asyncio.Task[None]:
        return asyncio.create_task(self._supervise_worker(worker, name), name=name)

    async def _supervise_worker(self, worker: Callable[[], Awaitable[None]], name: str) -> None:
        while not self._stop.is_set():
            try:
                await worker()
                if self._stop.is_set():
                    return
                logger.error("runtime_worker_returned: %s", name)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("runtime_worker_failed: %s", name)
            if await self._wait_for_stop_or_timeout(_RUNTIME_WORKER_RESTART_SECONDS):
                return

    async def _wait_for_stop_or_timeout(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def on_trade(self, trade: NormalizedTrade) -> None:
        key = (trade.exchange, trade.symbol, trade.market)
        self._flows.setdefault(key, RollingTradeFlow()).add_trade(trade)
        self._dirty_symbols.add(trade.symbol)

    async def on_order_book(self, book: OrderBook) -> None:
        key = (book.exchange, book.symbol, book.market)
        self._known_book_keys.add(key)
        self._books[key] = book
        next_state = "connected" if book.available else "resync"
        previous_state = self._book_states.get(key)
        if next_state != previous_state:
            event = "recovered" if next_state == "connected" and previous_state else next_state
            logger.info(
                "orderbook_%s: %s:%s:%s",
                event,
                book.exchange.value,
                book.symbol,
                book.market.value,
            )
            self._book_states[key] = next_state
        self._dirty_symbols.add(book.symbol)

    async def on_derivative(self, snapshot: DerivativeSnapshot) -> None:
        key = (snapshot.exchange, snapshot.symbol)
        history = self._derivatives[key]
        if history and snapshot.timestamp < history[-1].timestamp:
            return
        if history and history[-1].timestamp == snapshot.timestamp:
            history[-1] = snapshot
        else:
            history.append(snapshot)
        cutoff = history[-1].timestamp - _OI_RETENTION_MS
        while history and history[0].timestamp < cutoff:
            history.popleft()
        if self._derivative_states.get(key) == "stale":
            logger.info("derivative_recovered: %s:%s", snapshot.exchange.value, snapshot.symbol)
        self._derivative_states[key] = "connected"
        self._dirty_symbols.add(snapshot.symbol)

    async def flush(self) -> None:
        """Build all active raw metrics before normalizing and publishing a cadence."""
        self._dirty_symbols.clear()
        monitored = {
            f"{asset.symbol}USDT"
            for asset in self.state.universe
            if asset.enabled and asset.rank <= 50
        }
        active_symbols = sorted(
            {symbol for _, symbol, _ in self._known_book_keys if symbol in monitored}
            | {symbol for _, symbol, _ in self._books if symbol in monitored}
        )
        batch_markets: list[dict[str, Any]] = []
        batch_flows: list[dict[str, Any]] = []
        batch_derivatives: list[dict[str, Any]] = []
        metrics_by_symbol = await self._collect_liquidities(
            set(active_symbols), batch=batch_markets
        )
        fragilities = liquidity_fragility_scores(
            {symbol: list(metrics.values()) for symbol, metrics in metrics_by_symbol.items()}
        )
        for symbol in active_symbols:
            detail = await self._build_detail(
                symbol,
                metrics_by_symbol.get(symbol, {}),
                fragilities.get(symbol),
                batch_flows=batch_flows,
                batch_derivatives=batch_derivatives,
            )
            await self.state.update_symbol(symbol, detail)
        await self._persist_metrics_batch(
            markets=batch_markets,
            flows=batch_flows,
            derivatives=batch_derivatives,
        )

    async def _run_cadence(self) -> None:
        cycles = 0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._cadence_seconds)
            except TimeoutError:
                self._dirty_symbols.update(symbol for _, symbol, _ in self._books)
                await self.flush()
                cycles += 1
                if cycles % 3600 == 0:
                    try:
                        await self.prune_historical_metrics()
                    except Exception as error:
                        logger.warning("metrics_prune_error: %s", error)

    async def prune_historical_metrics(self) -> int:
        """Prune database timeseries metrics older than the configured retention cutoff."""
        if not hasattr(self.metrics, "prune_metrics"):
            return 0
        cutoff = datetime.now(UTC) - timedelta(hours=self._retention_hours)
        return await self.metrics.prune_metrics(cutoff)

    async def _persist_metrics_batch(
        self,
        *,
        markets: list[dict[str, Any]],
        flows: list[dict[str, Any]],
        derivatives: list[dict[str, Any]],
    ) -> None:
        market_rows = [self._without_persistence_metadata(row) for row in markets]
        derivative_rows = [self._without_persistence_metadata(row) for row in derivatives]
        if hasattr(self.metrics, "append_batch"):
            await self.metrics.append_batch(
                markets=market_rows, flows=flows, derivatives=derivative_rows
            )
            self._advance_watermarks(markets, self._market_watermarks)
            self._advance_watermarks(derivatives, self._derivative_watermarks)
            return
        for row, values in zip(markets, market_rows, strict=True):
            await self.metrics.append_market(values)
            self._advance_watermarks([row], self._market_watermarks)
        for flow in flows:
            await self.metrics.append_flow(flow)
        for row, values in zip(derivatives, derivative_rows, strict=True):
            await self.metrics.append_derivative(values)
            self._advance_watermarks([row], self._derivative_watermarks)

    @staticmethod
    def _without_persistence_metadata(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if not key.startswith("_")}

    @staticmethod
    def _advance_watermarks(rows: list[dict[str, Any]], watermarks: dict[Any, int]) -> None:
        for row in rows:
            key = row.get("_watermark_key")
            timestamp = row.get("_source_timestamp")
            if key is not None and isinstance(timestamp, int):
                watermarks[key] = timestamp

    def _adapters(self) -> dict[Exchange, BinanceAdapter | OkxAdapter]:
        if self._session is None:
            raise RuntimeError("runtime session is not initialized")
        client = AiohttpJsonClient(self._session)
        return {
            Exchange.BINANCE: BinanceAdapter(client),
            Exchange.OKX: OkxAdapter(client),
        }

    def _stream_tasks(
        self,
        exchange: Exchange,
        market: MarketType,
        instruments: list[MarketInstrument],
        adapters: dict[Exchange, BinanceAdapter | OkxAdapter],
    ) -> list[asyncio.Task[None]]:
        if self._session is None:
            raise RuntimeError("runtime session is not initialized")
        books = [(instrument, LocalOrderBook()) for instrument in instruments]
        adapter = adapters[exchange]
        if exchange is Exchange.BINANCE:
            if not isinstance(adapter, BinanceAdapter):
                raise RuntimeError("Binance stream manager has the wrong adapter")
            binance_trade_manager = BinanceTradeManager(
                self._session, market, instruments, self.on_trade
            )
            tasks = [
                self._supervised_task(
                    partial(binance_trade_manager.run, self._stop),
                    name=f"trades:{exchange}:{market}",
                )
            ]
            for chunk_index, start in enumerate(range(0, len(books), BINANCE_BOOK_CHUNK_SIZE)):
                binance_book_manager = BinanceOrderBookManager(
                    self._session,
                    adapter,
                    market,
                    books[start : start + BINANCE_BOOK_CHUNK_SIZE],
                    self.on_order_book,
                    snapshot_scheduler=self._binance_snapshot_scheduler,
                )
                tasks.append(
                    self._supervised_task(
                        partial(binance_book_manager.run, self._stop),
                        name=f"book:{exchange}:{market}:{chunk_index}",
                    )
                )
            return tasks

        if not isinstance(adapter, OkxAdapter):
            raise RuntimeError("OKX stream manager has the wrong adapter")
        okx_trade_manager = OkxTradeManager(self._session, market, instruments, self.on_trade)
        tasks = [
            self._supervised_task(
                partial(okx_trade_manager.run, self._stop),
                name=f"trades:{exchange}:{market}",
            )
        ]
        for chunk_index, start in enumerate(range(0, len(books), 25)):
            okx_book_manager = OkxOrderBookManager(
                self._session, adapter, market, books[start : start + 25], self.on_order_book
            )
            tasks.append(
                self._supervised_task(
                    partial(okx_book_manager.run, self._stop),
                    name=f"book:{exchange}:{market}:{chunk_index}",
                )
            )
        return tasks

    def _derivative_task(self, instrument: MarketInstrument, index: int = 0) -> asyncio.Task[None]:
        if self._session is None:
            raise RuntimeError("runtime session is not initialized")
        fetch: Callable[[], Awaitable[DerivativeSnapshot]]
        interval: float
        initial_delay: float
        if instrument.exchange is Exchange.BINANCE:
            binance_provider = BinanceDerivativesProvider(
                AiohttpJsonClient(self._session),
                self._binance_derivative_scheduler,
            )
            fetch = partial(binance_provider.snapshot, instrument)
            interval = float(BINANCE_DERIVATIVE_CADENCE_SECONDS)
            initial_delay = 0.0
        else:
            okx_provider = OkxDerivativesProvider(
                AiohttpJsonClient(self._session), self._okx_derivative_scheduler
            )
            fetch = partial(okx_provider.snapshot, instrument)
            interval = 30.0
            initial_delay = (index % 10) * 1.5
        collector = DerivativePollingCollector(
            fetch,
            self.on_derivative,
            interval_seconds=interval,
            initial_delay_seconds=initial_delay,
        )
        return self._supervised_task(
            partial(collector.run, self._stop),
            name=f"derivatives:{instrument.exchange}:{instrument.symbol}",
        )

    async def _collect_liquidities(
        self, symbols: set[str], batch: list[dict[str, Any]] | None = None
    ) -> dict[str, dict[tuple[Exchange, MarketType], LiquidityMetrics]]:
        metrics_by_symbol: dict[str, dict[tuple[Exchange, MarketType], LiquidityMetrics]] = {}
        for key, book in self._books.items():
            exchange, symbol, market = key
            if symbol not in symbols or not self._available_book(exchange, symbol, market, book):
                continue
            liquidity = self._liquidity_for_book(book)
            if liquidity is None:
                continue
            metrics_by_symbol.setdefault(symbol, {})[(exchange, market)] = liquidity
            if book.timestamp <= self._market_watermarks.get(key, -1):
                continue
            metric_row = {
                "timestamp": datetime.fromtimestamp(book.timestamp / 1_000, UTC),
                "exchange": exchange.value,
                "symbol": symbol,
                "market": market.value,
                "price": liquidity.mid_price,
                "spread": liquidity.spread_percent,
                "bid_depth_0_5": liquidity.bid_depth_0_5,
                "ask_depth_0_5": liquidity.ask_depth_0_5,
                "bid_depth_1": liquidity.bid_depth_1,
                "ask_depth_1": liquidity.ask_depth_1,
                "bid_depth_2": liquidity.bid_depth_2,
                "ask_depth_2": liquidity.ask_depth_2,
                "bid_depth_5": liquidity.bid_depth_5,
                "ask_depth_5": liquidity.ask_depth_5,
                "buy_impact_10k": liquidity.buy_impacts[10_000],
                "sell_impact_10k": liquidity.sell_impacts[10_000],
                "buy_impact_50k": liquidity.buy_impacts[50_000],
                "sell_impact_50k": liquidity.sell_impacts[50_000],
                "obi": liquidity.order_book_imbalance,
                "_watermark_key": key,
                "_source_timestamp": book.timestamp,
            }
            if batch is not None:
                batch.append(metric_row)
            else:
                await self._persist_metrics_batch(markets=[metric_row], flows=[], derivatives=[])
        return metrics_by_symbol

    def _liquidity_for_book(self, book: OrderBook) -> LiquidityMetrics | None:
        try:
            metrics = calculate_liquidity(
                book,
                impact_notionals=(10_000, 50_000),
                capital_percentages=(1, 2),
            )
        except ValueError:
            metrics = None
        return metrics

    async def _build_detail(
        self,
        symbol: str,
        metrics_by_book: dict[tuple[Exchange, MarketType], LiquidityMetrics],
        fragility: float | None,
        *,
        batch_flows: list[dict[str, Any]] | None = None,
        batch_derivatives: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        market_flows = {
            market: await self._persist_flow(symbol, market, metrics_by_book, batch=batch_flows)
            for market in MarketType
        }
        spot = market_flows[MarketType.SPOT]
        perp = market_flows[MarketType.PERP]
        exchange_signals: list[MarketSignal] = []
        oi_change_by_exchange: dict[str, dict[str, float | None]] = {}
        derivative_by_exchange: dict[str, dict[str, float | int | None]] = {}

        for exchange in Exchange:
            latest_derivative = self._latest_derivative(exchange, symbol)
            derivative = (
                latest_derivative
                if latest_derivative is not None and self._derivative_is_fresh(latest_derivative)
                else None
            )
            known_derivative = latest_derivative is not None or any(
                known_exchange is exchange
                and known_symbol == symbol
                and known_market is MarketType.PERP
                for known_exchange, known_symbol, known_market in self._known_book_keys
            )
            if known_derivative:
                derivative_by_exchange[exchange.value] = {
                    "open_interest": derivative.open_interest if derivative else None,
                    "open_interest_usd": derivative.open_interest_usd if derivative else None,
                    "funding_rate": derivative.funding_rate if derivative else None,
                    "source_timestamp": (
                        latest_derivative.timestamp if latest_derivative is not None else None
                    ),
                    "received_at": (
                        latest_derivative.received_at if latest_derivative is not None else None
                    ),
                }
            has_market_data = any((exchange, market) in metrics_by_book for market in MarketType)
            if not has_market_data and derivative is None:
                continue

            oi_changes = {
                name: self.oi_change(exchange, symbol, seconds) if derivative else None
                for seconds, name in _OI_WINDOWS.items()
            }
            oi_change_by_exchange[exchange.value] = oi_changes
            exchange_signals.append(
                MarketSignal(
                    exchange.value,
                    spot.get(f"{exchange.value}:buy_pressure_5m"),
                    spot.get(f"{exchange.value}:sell_pressure_5m"),
                    perp.get(f"{exchange.value}:buy_pressure_5m"),
                    perp.get(f"{exchange.value}:sell_pressure_5m"),
                    spot.get(f"{exchange.value}:cvd_5m"),
                    perp.get(f"{exchange.value}:cvd_5m"),
                    oi_changes["5m"],
                )
            )
            if derivative is not None:
                key = (exchange, symbol)
                if derivative.timestamp <= self._derivative_watermarks.get(key, -1):
                    continue
                derivative_row = {
                    "timestamp": datetime.fromtimestamp(derivative.timestamp / 1_000, UTC),
                    "exchange": exchange.value,
                    "symbol": symbol,
                    "open_interest": derivative.open_interest,
                    "open_interest_usd": derivative.open_interest_usd,
                    "oi_change_5m": oi_changes["5m"],
                    "oi_change_15m": oi_changes["15m"],
                    "oi_change_1h": oi_changes["1h"],
                    "funding_rate": derivative.funding_rate,
                    "_watermark_key": key,
                    "_source_timestamp": derivative.timestamp,
                }
                if batch_derivatives is not None:
                    batch_derivatives.append(derivative_row)
                else:
                    await self._persist_metrics_batch(
                        markets=[], flows=[], derivatives=[derivative_row]
                    )
        confirmed = cross_exchange_state(exchange_signals, self._thresholds)
        move_type_by_exchange = {
            signal.exchange: classify_move(signal, self._thresholds).value
            for signal in exchange_signals
        }
        move_type = aggregate_move_type(MoveType(move) for move in move_type_by_exchange.values())
        global_oi_changes = {
            name: self._largest_absolute(
                changes[name] for changes in oi_change_by_exchange.values()
            )
            for name in ("5m", "15m", "1h")
        }
        funding = self._funding(symbol)
        primary_liq = next(
            (m for (_, m_type), m in metrics_by_book.items() if m_type is MarketType.PERP),
            next(iter(metrics_by_book.values()), None),
        )
        liquidity_summary = (
            {
                "spread_percent": primary_liq.spread_percent,
                "bid_depth_0_5": primary_liq.bid_depth_0_5,
                "ask_depth_0_5": primary_liq.ask_depth_0_5,
                "bid_depth_1": primary_liq.bid_depth_1,
                "ask_depth_1": primary_liq.ask_depth_1,
                "bid_depth_2": primary_liq.bid_depth_2,
                "ask_depth_2": primary_liq.ask_depth_2,
                "bid_depth_5": primary_liq.bid_depth_5,
                "ask_depth_5": primary_liq.ask_depth_5,
                "buy_impact_10k": primary_liq.buy_impacts.get(10_000),
                "sell_impact_10k": primary_liq.sell_impacts.get(10_000),
                "buy_impact_50k": primary_liq.buy_impacts.get(50_000),
                "sell_impact_50k": primary_liq.sell_impacts.get(50_000),
                "capital_to_move_up_1pct": primary_liq.capital_to_move_up.get(1),
                "capital_to_move_up_2pct": primary_liq.capital_to_move_up.get(2),
                "capital_to_move_down_1pct": primary_liq.capital_to_move_down.get(1),
                "capital_to_move_down_2pct": primary_liq.capital_to_move_down.get(2),
                "order_book_imbalance": primary_liq.order_book_imbalance,
            }
            if primary_liq is not None
            else {}
        )
        detail = {
            "price": self._preferred_price(metrics_by_book),
            "activity_score": activity_score(
                oi_change=global_oi_changes["5m"],
                funding=funding,
                confirmed=confirmed.value == "CONFIRMED",
                spot_pressure_1m=self._pressure_magnitude(
                    spot.get("buy_pressure_1m"), spot.get("sell_pressure_1m")
                ),
                spot_pressure_5m=self._pressure_magnitude(
                    spot.get("buy_pressure_5m"), spot.get("sell_pressure_5m")
                ),
                spot_cvd_ratio=self._delta_ratio(
                    spot.get("buy_volume_5m"), spot.get("sell_volume_5m")
                ),
                perp_pressure_1m=self._pressure_magnitude(
                    perp.get("buy_pressure_1m"), perp.get("sell_pressure_1m")
                ),
                perp_pressure_5m=self._pressure_magnitude(
                    perp.get("buy_pressure_5m"), perp.get("sell_pressure_5m")
                ),
                perp_cvd_ratio=self._delta_ratio(
                    perp.get("buy_volume_5m"), perp.get("sell_volume_5m")
                ),
            ),
            "liquidity_fragility": fragility,
            "move_type": move_type.value,
            "move_type_by_exchange": move_type_by_exchange,
            "cross_exchange_state": confirmed.value,
            "exchange_directions": exchange_directions(exchange_signals, self._thresholds),
            "oi_change_5m": global_oi_changes["5m"],
            "oi_change_15m": global_oi_changes["15m"],
            "oi_change_1h": global_oi_changes["1h"],
            "oi_change_by_exchange": oi_change_by_exchange,
            "derivatives": derivative_by_exchange,
            "funding": funding,
            "buy_pressure_1m": perp.get("buy_pressure_1m"),
            "buy_pressure_5m": perp.get("buy_pressure_5m"),
            "sell_pressure_1m": perp.get("sell_pressure_1m"),
            "sell_pressure_5m": perp.get("sell_pressure_5m"),
            "spot": self._public_flow(spot),
            "perp": self._public_flow(perp),
            "orderbooks": self._public_books(symbol),
            "liquidity": liquidity_summary,
        }
        return detail

    async def _persist_flow(
        self,
        symbol: str,
        market: MarketType,
        liquidities: dict[tuple[Exchange, MarketType], LiquidityMetrics],
        *,
        batch: list[dict[str, Any]] | None = None,
    ) -> dict[str, float | None]:
        aggregate_keys = (
            "buy_volume_1m",
            "sell_volume_1m",
            "buy_volume_5m",
            "sell_volume_5m",
            "cvd_1m",
            "cvd_5m",
        )
        combined: dict[str, float | None] = {key: None for key in aggregate_keys}
        pressure_volumes: dict[str, float | None] = {key: None for key in aggregate_keys[:4]}
        for exchange in Exchange:
            flow = self._flows.get((exchange, symbol, market))
            liquidity = liquidities.get((exchange, market))
            if flow is None:
                continue
            now_ms = int(datetime.now(UTC).timestamp() * 1_000)
            windows = flow.windows(now_ms, requested_seconds=(60, 300))
            one, five = windows[60], windows[300]
            buy_pressure_1m = pressure(one.buy_volume, liquidity.ask_depth_2) if liquidity else None
            sell_pressure_1m = (
                pressure(one.sell_volume, liquidity.bid_depth_2) if liquidity else None
            )
            buy_pressure_5m = (
                pressure(five.buy_volume, liquidity.ask_depth_2) if liquidity else None
            )
            sell_pressure_5m = (
                pressure(five.sell_volume, liquidity.bid_depth_2) if liquidity else None
            )
            flow_row = {
                "timestamp": datetime.now(UTC),
                "exchange": exchange.value,
                "symbol": symbol,
                "market": market.value,
                "buy_volume_1m": one.buy_volume,
                "sell_volume_1m": one.sell_volume,
                "buy_volume_5m": five.buy_volume,
                "sell_volume_5m": five.sell_volume,
                "cvd_1m": one.cvd,
                "cvd_5m": five.cvd,
                "buy_pressure_1m": buy_pressure_1m,
                "buy_pressure_5m": buy_pressure_5m,
                "sell_pressure_1m": sell_pressure_1m,
                "sell_pressure_5m": sell_pressure_5m,
            }
            if batch is not None:
                batch.append(flow_row)
            else:
                await self.metrics.append_flow(flow_row)
            values = {
                "buy_volume_1m": one.buy_volume,
                "sell_volume_1m": one.sell_volume,
                "buy_volume_5m": five.buy_volume,
                "sell_volume_5m": five.sell_volume,
                "cvd_1m": one.cvd,
                "cvd_5m": five.cvd,
                "buy_pressure_1m": buy_pressure_1m,
                "buy_pressure_5m": buy_pressure_5m,
                "sell_pressure_1m": sell_pressure_1m,
                "sell_pressure_5m": sell_pressure_5m,
            }
            for key, value in values.items():
                if key in combined and value is not None:
                    combined[key] = float(combined[key] or 0) + float(value)
                combined[f"{exchange.value}:{key}"] = value
            if liquidity is not None:
                for key in pressure_volumes:
                    volume = values[key]
                    if volume is not None:
                        pressure_volumes[key] = float(pressure_volumes[key] or 0) + volume
        depth_ask = self._depth_for_market(symbol, market, "ask", liquidities=liquidities)
        depth_bid = self._depth_for_market(symbol, market, "bid", liquidities=liquidities)
        combined["buy_pressure_1m"] = self._ratio(pressure_volumes["buy_volume_1m"], depth_ask)
        combined["buy_pressure_5m"] = self._ratio(pressure_volumes["buy_volume_5m"], depth_ask)
        combined["sell_pressure_1m"] = self._ratio(pressure_volumes["sell_volume_1m"], depth_bid)
        combined["sell_pressure_5m"] = self._ratio(pressure_volumes["sell_volume_5m"], depth_bid)
        return combined

    def _preferred_price(
        self, liquidities: dict[tuple[Exchange, MarketType], LiquidityMetrics]
    ) -> float | None:
        for key in ((Exchange.BINANCE, MarketType.PERP), (Exchange.OKX, MarketType.PERP)):
            if key in liquidities:
                return liquidities[key].mid_price
        return next(iter(liquidities.values())).mid_price if liquidities else None

    def _public_books(self, symbol: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for exchange, book_symbol, market in self._known_book_keys:
            if book_symbol != symbol:
                continue
            book = self._books.get((exchange, book_symbol, market))
            value: dict[str, Any] | None = None
            if book is not None and self._available_book(exchange, book_symbol, market, book):
                value = {
                    "exchange": exchange.value,
                    "market": market.value,
                    "source_timestamp": book.timestamp,
                    "received_at": book.received_at,
                    "bids": [[level.price, level.quantity] for level in book.bids],
                    "asks": [[level.price, level.quantity] for level in book.asks],
                }
            result.setdefault(exchange.value, {})[market.value] = value
        return result

    @staticmethod
    def _public_flow(values: dict[str, float | None]) -> dict[str, float | None]:
        return {key: value for key, value in values.items() if ":" not in key}

    def _available_book(
        self, exchange: Exchange, symbol: str, market: MarketType, book: OrderBook
    ) -> bool:
        key = (exchange, symbol, market)
        if not book.available:
            return False
        observed_at = book.received_at if book.received_at is not None else book.timestamp
        if time_ns() // 1_000_000 - observed_at <= ORDERBOOK_STALE_AFTER_SECONDS * 1_000:
            return True
        if self._book_states.get(key) != "stale":
            logger.info("orderbook_stale: %s:%s:%s", exchange.value, symbol, market.value)
            self._book_states[key] = "stale"
        return False

    def _latest_derivative(self, exchange: Exchange, symbol: str) -> DerivativeSnapshot | None:
        history = self._derivatives.get((exchange, symbol))
        return history[-1] if history else None

    def _derivative_is_fresh(self, snapshot: DerivativeSnapshot) -> bool:
        received_at = snapshot.received_at
        freshness_timestamp = snapshot.timestamp if received_at is None else received_at
        if time_ns() // 1_000_000 - freshness_timestamp <= DERIVATIVE_STALE_AFTER_SECONDS * 1_000:
            return True
        key = (snapshot.exchange, snapshot.symbol)
        if self._derivative_states.get(key) != "stale":
            logger.info("derivative_stale: %s:%s", snapshot.exchange.value, snapshot.symbol)
            self._derivative_states[key] = "stale"
        return False

    def oi_change(self, exchange: Exchange, symbol: str, seconds: int) -> float | None:
        """Return the signed OI change against the closest retained target sample."""
        if seconds not in _OI_WINDOWS:
            raise ValueError(f"unsupported OI window: {seconds}")
        history = self._derivatives.get((exchange, symbol))
        if not history or not self._derivative_is_fresh(history[-1]):
            return None
        latest = history[-1]
        target = latest.timestamp - seconds * 1_000
        if len(history) < 2:
            return None
        prior = min(
            tuple(history)[:-1],
            key=lambda entry: (abs(entry.timestamp - target), entry.timestamp > target),
        )
        if abs(prior.timestamp - target) > _OI_MAX_TARGET_DISTANCE_MS:
            return None
        elapsed = latest.timestamp - prior.timestamp
        if elapsed < seconds * 1_000 * _OI_MIN_WINDOW_FRACTION:
            return None
        if prior.open_interest == 0:
            return None
        return (latest.open_interest - prior.open_interest) / prior.open_interest

    def _funding(self, symbol: str) -> float | None:
        funding_rates: list[float] = []
        for exchange in Exchange:
            snapshot = self._latest_derivative(exchange, symbol)
            if (
                snapshot is not None
                and self._derivative_is_fresh(snapshot)
                and snapshot.funding_rate is not None
            ):
                funding_rates.append(snapshot.funding_rate)
        return sum(funding_rates) / len(funding_rates) if funding_rates else None

    def _depth_for_market(
        self,
        symbol: str,
        market: MarketType,
        side: str,
        *,
        liquidities: dict[tuple[Exchange, MarketType], LiquidityMetrics] | None = None,
    ) -> float:
        if liquidities is not None:
            return sum(
                liq.ask_depth_2 if side == "ask" else liq.bid_depth_2
                for (_, book_market), liq in liquidities.items()
                if book_market is market
            )
        total = 0.0
        for (exchange, book_symbol, book_market), book in self._books.items():
            if (
                book_symbol == symbol
                and book_market is market
                and self._available_book(exchange, book_symbol, book_market, book)
            ):
                try:
                    liquidity = calculate_liquidity(book)
                except ValueError:
                    continue
                total += liquidity.ask_depth_2 if side == "ask" else liquidity.bid_depth_2
        return total

    @staticmethod
    def _ratio(value: float | None, depth: float) -> float | None:
        return None if value is None else pressure(value, depth)

    @staticmethod
    def _delta_ratio(buy_volume: float | None, sell_volume: float | None) -> float | None:
        if buy_volume is None or sell_volume is None:
            return None
        total = buy_volume + sell_volume
        return (buy_volume - sell_volume) / total if total else 0.0

    @staticmethod
    def _pressure_magnitude(
        buy_pressure: float | None, sell_pressure: float | None
    ) -> float | None:
        values = [value for value in (buy_pressure, sell_pressure) if value is not None]
        return max(values) if values else None

    @staticmethod
    def _largest_absolute(values: Any) -> float | None:
        available = [value for value in values if value is not None]
        return max(available, key=abs) if available else None
