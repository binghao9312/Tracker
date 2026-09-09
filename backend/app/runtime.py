"""Live public-market pipeline from discovery through dashboard and paper trading."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from app.api import DashboardState
from app.collectors.derivatives import DerivativePollingCollector
from app.collectors.orderbooks import BinanceOrderBookManager, OkxOrderBookManager
from app.collectors.trades import BinanceTradeManager, OkxTradeManager
from app.discovery import MarketDiscoveryService
from app.exchanges.binance import BinanceAdapter
from app.exchanges.derivatives import BinanceDerivativesProvider, OkxDerivativesProvider
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
        cadence_seconds: float = 1.0,
    ) -> None:
        self.state = state
        self.metrics = metrics
        self._session = session
        self._owns_session = session is None
        self._discovery = discovery
        self._thresholds = thresholds or ClassificationThresholds()
        self._cadence_seconds = cadence_seconds
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._books: dict[tuple[Exchange, str, MarketType], OrderBook] = {}
        self._flows: dict[tuple[Exchange, str, MarketType], RollingTradeFlow] = {}
        self._derivatives: dict[tuple[Exchange, str], deque[DerivativeSnapshot]] = defaultdict(
            deque
        )
        self._dirty_symbols: set[str] = set()
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
            groups: defaultdict[tuple[Exchange, MarketType], list[MarketInstrument]] = defaultdict(
                list
            )
            for instrument in result.markets:
                groups[(instrument.exchange, instrument.market)].append(instrument)
            for (exchange, market), instruments in groups.items():
                self._tasks.extend(self._stream_tasks(exchange, market, instruments, adapters))
            perp_instruments = [m for m in result.markets if m.market is MarketType.PERP]
            for index, instrument in enumerate(perp_instruments):
                self._tasks.append(self._derivative_task(instrument, index=index))
            self._tasks.append(asyncio.create_task(self._run_cadence(), name="metric-cadence"))
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

    async def on_trade(self, trade: NormalizedTrade) -> None:
        key = (trade.exchange, trade.symbol, trade.market)
        self._flows.setdefault(key, RollingTradeFlow()).add_trade(trade)
        self._dirty_symbols.add(trade.symbol)

    async def on_order_book(self, book: OrderBook) -> None:
        self._books[(book.exchange, book.symbol, book.market)] = book
        self._dirty_symbols.add(book.symbol)

    async def on_derivative(self, snapshot: DerivativeSnapshot) -> None:
        history = self._derivatives[(snapshot.exchange, snapshot.symbol)]
        history.append(snapshot)
        cutoff = snapshot.timestamp - _OI_RETENTION_MS
        while history and history[0].timestamp < cutoff:
            history.popleft()
        self._dirty_symbols.add(snapshot.symbol)

    async def flush(self) -> None:
        """Build all active raw metrics before normalizing and publishing a cadence."""
        self._dirty_symbols.clear()
        monitored = {
            f"{asset.symbol}USDT"
            for asset in self.state.universe
            if asset.enabled and asset.rank <= 50
        }
        active_symbols = sorted({symbol for _, symbol, _ in self._books if symbol in monitored})
        batch_markets: list[dict[str, Any]] = []
        batch_flows: list[dict[str, Any]] = []
        batch_derivatives: list[dict[str, Any]] = []
        metrics_by_symbol: dict[str, dict[tuple[Exchange, MarketType], LiquidityMetrics]] = {}
        for symbol in active_symbols:
            metrics_by_book = await self._collect_liquidities(symbol, batch=batch_markets)
            if metrics_by_book:
                metrics_by_symbol[symbol] = metrics_by_book
        fragilities = liquidity_fragility_scores(
            {symbol: list(metrics.values()) for symbol, metrics in metrics_by_symbol.items()}
        )
        for symbol, metrics_by_book in metrics_by_symbol.items():
            detail = await self._build_detail(
                symbol,
                metrics_by_book,
                fragilities[symbol],
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
                        await self.prune_historical_metrics(retention_hours=24)
                    except Exception as error:
                        logger.warning("metrics_prune_error: %s", error)

    async def prune_historical_metrics(self, retention_hours: int = 24) -> int:
        """Prune database timeseries metrics older than retention cutoff."""
        if not hasattr(self.metrics, "prune_metrics"):
            return 0
        cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
        return await self.metrics.prune_metrics(cutoff)
    async def _persist_metrics_batch(
        self,
        *,
        markets: list[dict[str, Any]],
        flows: list[dict[str, Any]],
        derivatives: list[dict[str, Any]],
    ) -> None:
        if hasattr(self.metrics, "append_batch"):
            await self.metrics.append_batch(
                markets=markets, flows=flows, derivatives=derivatives
            )
            return
        for m in markets:
            await self.metrics.append_market(m)
        for f in flows:
            await self.metrics.append_flow(f)
        for d in derivatives:
            await self.metrics.append_derivative(d)

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
            trade_manager = BinanceTradeManager(self._session, market, instruments, self.on_trade)
            book_manager = BinanceOrderBookManager(
                self._session, adapter, market, books, self.on_order_book
            )
            return [
                asyncio.create_task(
                    trade_manager.run(self._stop),
                    name=f"trades:{exchange}:{market}",
                ),
                asyncio.create_task(
                    book_manager.run(self._stop),
                    name=f"book:{exchange}:{market}",
                ),
            ]

        if not isinstance(adapter, OkxAdapter):
            raise RuntimeError("OKX stream manager has the wrong adapter")
        trade_manager = OkxTradeManager(self._session, market, instruments, self.on_trade)
        tasks = [
            asyncio.create_task(
                trade_manager.run(self._stop),
                name=f"trades:{exchange}:{market}",
            )
        ]
        for chunk_index, start in enumerate(range(0, len(books), 25)):
            book_manager = OkxOrderBookManager(
                self._session, adapter, market, books[start : start + 25], self.on_order_book
            )
            tasks.append(
                asyncio.create_task(
                    book_manager.run(self._stop),
                    name=f"book:{exchange}:{market}:{chunk_index}",
                )
            )
        return tasks

    def _derivative_task(
        self, instrument: MarketInstrument, index: int = 0
    ) -> asyncio.Task[None]:
        if self._session is None:
            raise RuntimeError("runtime session is not initialized")
        provider = (
            BinanceDerivativesProvider(AiohttpJsonClient(self._session))
            if instrument.exchange is Exchange.BINANCE
            else OkxDerivativesProvider(AiohttpJsonClient(self._session))
        )
        collector = DerivativePollingCollector(
            lambda: provider.snapshot(instrument),
            self.on_derivative,
            initial_delay_seconds=(index % 10) * 1.5,
        )
        return asyncio.create_task(
            collector.run(self._stop),
            name=f"derivatives:{instrument.exchange}:{instrument.symbol}",
        )

    async def _collect_liquidities(
        self, symbol: str, batch: list[dict[str, Any]] | None = None
    ) -> dict[tuple[Exchange, MarketType], LiquidityMetrics]:
        metrics_by_book: dict[tuple[Exchange, MarketType], LiquidityMetrics] = {}
        for (exchange, book_symbol, market), book in self._books.items():
            if book_symbol != symbol:
                continue
            try:
                liquidity = calculate_liquidity(book)
            except ValueError:
                continue
            metrics_by_book[(exchange, market)] = liquidity
            timestamp = datetime.now(UTC)
            metric_row = {
                "timestamp": timestamp,
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
            }
            if batch is not None:
                batch.append(metric_row)
            else:
                await self.metrics.append_market(metric_row)
        return metrics_by_book

    async def _build_detail(
        self,
        symbol: str,
        metrics_by_book: dict[tuple[Exchange, MarketType], LiquidityMetrics],
        fragility: float,
        *,
        batch_flows: list[dict[str, Any]] | None = None,
        batch_derivatives: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        market_flows = {
            market: await self._persist_flow(
                symbol, market, metrics_by_book, batch=batch_flows
            )
            for market in MarketType
        }
        spot = market_flows[MarketType.SPOT]
        perp = market_flows[MarketType.PERP]
        exchange_signals: list[MarketSignal] = []
        oi_change_by_exchange: dict[str, dict[str, float | None]] = {}

        for exchange in Exchange:
            derivative = self._latest_derivative(exchange, symbol)
            has_market_data = any((exchange, market) in metrics_by_book for market in MarketType)
            if not has_market_data and derivative is None:
                continue

            oi_changes = {
                name: self.oi_change(exchange, symbol, seconds)
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
                derivative_row = {
                    "timestamp": datetime.now(UTC),
                    "exchange": exchange.value,
                    "symbol": symbol,
                    "open_interest": derivative.open_interest,
                    "open_interest_usd": derivative.open_interest_usd,
                    "oi_change_5m": oi_changes["5m"],
                    "oi_change_15m": oi_changes["15m"],
                    "oi_change_1h": oi_changes["1h"],
                    "funding_rate": derivative.funding_rate,
                }
                if batch_derivatives is not None:
                    batch_derivatives.append(derivative_row)
                else:
                    await self.metrics.append_derivative(derivative_row)
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
        for exchange in Exchange:
            flow = self._flows.get((exchange, symbol, market))
            liquidity = liquidities.get((exchange, market))
            if flow is None or liquidity is None:
                continue
            now_ms = int(datetime.now(UTC).timestamp() * 1_000)
            windows = flow.windows(now_ms)
            one, five = windows[60], windows[300]
            buy_pressure_1m = pressure(one.buy_volume, liquidity.ask_depth_2)
            sell_pressure_1m = pressure(one.sell_volume, liquidity.bid_depth_2)
            buy_pressure_5m = pressure(five.buy_volume, liquidity.ask_depth_2)
            sell_pressure_5m = pressure(five.sell_volume, liquidity.bid_depth_2)
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
        depth_ask = self._depth_for_market(symbol, market, "ask", liquidities=liquidities)
        depth_bid = self._depth_for_market(symbol, market, "bid", liquidities=liquidities)
        combined["buy_pressure_1m"] = self._ratio(combined["buy_volume_1m"], depth_ask)
        combined["buy_pressure_5m"] = self._ratio(combined["buy_volume_5m"], depth_ask)
        combined["sell_pressure_1m"] = self._ratio(combined["sell_volume_1m"], depth_bid)
        combined["sell_pressure_5m"] = self._ratio(combined["sell_volume_5m"], depth_bid)
        return combined

    def _preferred_price(
        self, liquidities: dict[tuple[Exchange, MarketType], LiquidityMetrics]
    ) -> float:
        for key in ((Exchange.BINANCE, MarketType.PERP), (Exchange.OKX, MarketType.PERP)):
            if key in liquidities:
                return liquidities[key].mid_price
        return next(iter(liquidities.values())).mid_price

    def _public_books(self, symbol: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for (exchange, book_symbol, market), book in self._books.items():
            if book_symbol == symbol:
                result.setdefault(exchange.value, {})[market.value] = {
                    "exchange": exchange.value,
                    "market": market.value,
                    "bids": [[level.price, level.quantity] for level in book.bids],
                    "asks": [[level.price, level.quantity] for level in book.asks],
                }
        return result

    @staticmethod
    def _public_flow(values: dict[str, float | None]) -> dict[str, float | None]:
        return {key: value for key, value in values.items() if ":" not in key}

    def _latest_derivative(self, exchange: Exchange, symbol: str) -> DerivativeSnapshot | None:
        history = self._derivatives.get((exchange, symbol))
        return history[-1] if history else None

    def oi_change(self, exchange: Exchange, symbol: str, seconds: int) -> float | None:
        """Return the signed OI change against the closest retained target sample."""
        if seconds not in _OI_WINDOWS:
            raise ValueError(f"unsupported OI window: {seconds}")
        history = self._derivatives.get((exchange, symbol))
        if not history:
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
        values = [
            snapshot.funding_rate
            for exchange in Exchange
            if (snapshot := self._latest_derivative(exchange, symbol)) is not None
            and snapshot.funding_rate is not None
        ]
        return sum(values) / len(values) if values else None

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
        for (_exchange, book_symbol, book_market), book in self._books.items():
            if book_symbol == symbol and book_market is market:
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
