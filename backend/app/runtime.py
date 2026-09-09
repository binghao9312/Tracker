"""Live public-market pipeline from discovery through dashboard and paper trading."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

import aiohttp

from app.api import DashboardState
from app.collectors.derivatives import DerivativePollingCollector
from app.collectors.orderbooks import BinanceOrderBookCollector, OkxOrderBookCollector
from app.collectors.trades import BinanceTradeCollector, OkxTradeCollector
from app.discovery import MarketDiscoveryService
from app.exchanges.binance import BinanceAdapter
from app.exchanges.derivatives import BinanceDerivativesProvider, OkxDerivativesProvider
from app.exchanges.okx import OkxAdapter
from app.flow import RollingTradeFlow, pressure
from app.http import AiohttpJsonClient
from app.liquidity import LiquidityMetrics, calculate_liquidity
from app.models import DerivativeSnapshot, Exchange, MarketInstrument, MarketType, NormalizedTrade, OrderBook
from app.orderbook import LocalOrderBook
from app.repository import MetricRepository
from app.scoring import (
    ClassificationThresholds,
    MarketSignal,
    activity_score,
    classify_move,
    cross_exchange_state,
    liquidity_fragility_scores,
)




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
        self._thresholds = thresholds or ClassificationThresholds(pressure=2.0, cvd=0.0, oi_change=0.05)
        self._cadence_seconds = cadence_seconds
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._books: dict[tuple[Exchange, str, MarketType], OrderBook] = {}
        self._flows: dict[tuple[Exchange, str, MarketType], RollingTradeFlow] = {}
        self._derivatives: dict[tuple[Exchange, str], deque[DerivativeSnapshot]] = defaultdict(deque)
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
            for instrument in result.markets:
                self._tasks.extend(self._collector_tasks(instrument, adapters))
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
        cutoff = snapshot.timestamp - 3_600_000
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
        active_symbols = sorted(
            {symbol for _, symbol, _ in self._books if symbol in monitored}
        )
        metrics_by_symbol: dict[str, dict[tuple[Exchange, MarketType], LiquidityMetrics]] = {}
        for symbol in active_symbols:
            metrics_by_book = await self._collect_liquidities(symbol)
            if metrics_by_book:
                metrics_by_symbol[symbol] = metrics_by_book
        fragilities = liquidity_fragility_scores(
            {symbol: list(metrics.values()) for symbol, metrics in metrics_by_symbol.items()}
        )
        for symbol, metrics_by_book in metrics_by_symbol.items():
            detail = await self._build_detail(
                symbol, metrics_by_book, fragilities[symbol]
            )
            await self.state.update_symbol(symbol, detail)

    async def _run_cadence(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._cadence_seconds)
            except TimeoutError:
                self._dirty_symbols.update(symbol for _, symbol, _ in self._books)
                await self.flush()

    def _adapters(self) -> dict[Exchange, BinanceAdapter | OkxAdapter]:
        if self._session is None:
            raise RuntimeError("runtime session is not initialized")
        client = AiohttpJsonClient(self._session)
        return {
            Exchange.BINANCE: BinanceAdapter(client),
            Exchange.OKX: OkxAdapter(client),
        }

    def _collector_tasks(
        self,
        instrument: MarketInstrument,
        adapters: dict[Exchange, BinanceAdapter | OkxAdapter],
    ) -> list[asyncio.Task[None]]:
        if self._session is None:
            raise RuntimeError("runtime session is not initialized")
        adapter = adapters[instrument.exchange]
        book = LocalOrderBook()
        if instrument.exchange is Exchange.BINANCE:
            trade_collector = BinanceTradeCollector(self._session, instrument, self.on_trade)
            book_collector = BinanceOrderBookCollector(self._session, adapter, instrument, book, self.on_order_book)
        else:
            trade_collector = OkxTradeCollector(self._session, instrument, self.on_trade)
            book_collector = OkxOrderBookCollector(self._session, adapter, instrument, book, self.on_order_book)
        tasks = [
            asyncio.create_task(trade_collector.run(self._stop), name=f"trades:{instrument.exchange}:{instrument.symbol}:{instrument.market}"),
            asyncio.create_task(book_collector.run(self._stop), name=f"book:{instrument.exchange}:{instrument.symbol}:{instrument.market}"),
        ]
        if instrument.market is MarketType.PERP:
            provider = BinanceDerivativesProvider(AiohttpJsonClient(self._session)) if instrument.exchange is Exchange.BINANCE else OkxDerivativesProvider(AiohttpJsonClient(self._session))
            collector = DerivativePollingCollector(lambda: provider.snapshot(instrument), self.on_derivative)
            tasks.append(asyncio.create_task(collector.run(self._stop), name=f"derivatives:{instrument.exchange}:{instrument.symbol}"))
        return tasks

    async def _collect_liquidities(
        self, symbol: str
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
            await self.metrics.append_market({
                "timestamp": timestamp, "exchange": exchange.value, "symbol": symbol, "market": market.value,
                "price": liquidity.mid_price, "spread": liquidity.spread_percent,
                "bid_depth_0_5": liquidity.bid_depth_0_5, "ask_depth_0_5": liquidity.ask_depth_0_5,
                "bid_depth_1": liquidity.bid_depth_1, "ask_depth_1": liquidity.ask_depth_1,
                "bid_depth_2": liquidity.bid_depth_2, "ask_depth_2": liquidity.ask_depth_2,
                "bid_depth_5": liquidity.bid_depth_5, "ask_depth_5": liquidity.ask_depth_5,
                "buy_impact_10k": liquidity.buy_impacts[10_000], "sell_impact_10k": liquidity.sell_impacts[10_000],
                "buy_impact_50k": liquidity.buy_impacts[50_000], "sell_impact_50k": liquidity.sell_impacts[50_000],
                "obi": liquidity.order_book_imbalance,
            })
        return metrics_by_book

    async def _build_detail(
        self,
        symbol: str,
        metrics_by_book: dict[tuple[Exchange, MarketType], LiquidityMetrics],
        fragility: float,
    ) -> dict[str, Any]:

        market_flows: dict[MarketType, dict[str, float | None]] = {}
        exchange_signals: list[MarketSignal] = []
        for market in MarketType:
            market_flows[market] = await self._persist_flow(symbol, market, metrics_by_book)
        for exchange in Exchange:
            if not any((exchange, market) in metrics_by_book for market in MarketType):
                continue
            spot = market_flows[MarketType.SPOT]
            perp = market_flows[MarketType.PERP]
            derivative = self._latest_derivative(exchange, symbol)
            exchange_signals.append(MarketSignal(
                exchange.value,
                spot.get(f"{exchange.value}:buy_pressure_5m"),
                spot.get(f"{exchange.value}:sell_pressure_5m"),
                perp.get(f"{exchange.value}:buy_pressure_5m"),
                perp.get(f"{exchange.value}:sell_pressure_5m"),
                spot.get(f"{exchange.value}:cvd_5m"),
                perp.get(f"{exchange.value}:cvd_5m"),
                self._oi_change(exchange, symbol),
            ))
            if derivative is not None:
                await self.metrics.append_derivative({
                    "timestamp": datetime.now(UTC), "exchange": exchange.value, "symbol": symbol,
                    "open_interest": derivative.open_interest, "open_interest_usd": derivative.open_interest_usd,
                    "oi_change_5m": self._oi_change(exchange, symbol), "oi_change_15m": None,
                    "oi_change_1h": None, "funding_rate": derivative.funding_rate,
                })

        confirmed = cross_exchange_state(exchange_signals, self._thresholds)
        movement = [classify_move(signal, self._thresholds) for signal in exchange_signals]
        move_type = next((move.value for move in movement if move.value != "NEUTRAL"), "NEUTRAL")
        perp = market_flows[MarketType.PERP]
        price = self._preferred_price(metrics_by_book)
        detail = {
            "price": price,
            "activity_score": activity_score(
                max(float(perp.get("buy_pressure_1m") or 0), float(perp.get("sell_pressure_1m") or 0)),
                max(float(perp.get("buy_pressure_5m") or 0), float(perp.get("sell_pressure_5m") or 0)),
                self._delta_ratio(perp.get("buy_volume_5m"), perp.get("sell_volume_5m")),
                max((self._oi_change(exchange, symbol) or 0 for exchange in Exchange), default=0),
                self._funding(symbol), confirmed.value == "CONFIRMED",
            ),
            "liquidity_fragility": fragility,
            "move_type": move_type,
            "cross_exchange_state": confirmed.value,
            "oi_change_5m": max((self._oi_change(exchange, symbol) or 0 for exchange in Exchange), default=0),
            "funding": self._funding(symbol),
            "buy_pressure_1m": perp.get("buy_pressure_1m"), "buy_pressure_5m": perp.get("buy_pressure_5m"),
            "sell_pressure_1m": perp.get("sell_pressure_1m"), "sell_pressure_5m": perp.get("sell_pressure_5m"),
            "spot": self._public_flow(market_flows[MarketType.SPOT]),
            "perp": self._public_flow(perp),
            "orderbooks": self._public_books(symbol),
        }
        return detail

    async def _persist_flow(self, symbol: str, market: MarketType, liquidities: dict[tuple[Exchange, MarketType], LiquidityMetrics]) -> dict[str, float | None]:
        combined: dict[str, float | None] = {key: 0.0 for key in ("buy_volume_1m", "sell_volume_1m", "buy_volume_5m", "sell_volume_5m", "cvd_1m", "cvd_5m")}
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
            await self.metrics.append_flow({
                "timestamp": datetime.now(UTC), "exchange": exchange.value, "symbol": symbol, "market": market.value,
                "buy_volume_1m": one.buy_volume, "sell_volume_1m": one.sell_volume,
                "buy_volume_5m": five.buy_volume, "sell_volume_5m": five.sell_volume,
                "cvd_1m": one.cvd, "cvd_5m": five.cvd,
                "buy_pressure_1m": buy_pressure_1m, "buy_pressure_5m": buy_pressure_5m,
                "sell_pressure_1m": sell_pressure_1m, "sell_pressure_5m": sell_pressure_5m,
            })
            values = {
                "buy_volume_1m": one.buy_volume, "sell_volume_1m": one.sell_volume,
                "buy_volume_5m": five.buy_volume, "sell_volume_5m": five.sell_volume,
                "cvd_1m": one.cvd, "cvd_5m": five.cvd,
                "buy_pressure_1m": buy_pressure_1m, "buy_pressure_5m": buy_pressure_5m,
                "sell_pressure_1m": sell_pressure_1m, "sell_pressure_5m": sell_pressure_5m,
            }
            for key, value in values.items():
                if key in combined:
                    combined[key] = float(combined[key] or 0) + float(value or 0)
                combined[f"{exchange.value}:{key}"] = value
        combined["buy_pressure_1m"] = self._ratio(combined["buy_volume_1m"], self._depth_for_market(symbol, market, "ask"))
        combined["buy_pressure_5m"] = self._ratio(combined["buy_volume_5m"], self._depth_for_market(symbol, market, "ask"))
        combined["sell_pressure_1m"] = self._ratio(combined["sell_volume_1m"], self._depth_for_market(symbol, market, "bid"))
        combined["sell_pressure_5m"] = self._ratio(combined["sell_volume_5m"], self._depth_for_market(symbol, market, "bid"))
        return combined

    def _preferred_price(self, liquidities: dict[tuple[Exchange, MarketType], LiquidityMetrics]) -> float:
        for key in ((Exchange.BINANCE, MarketType.PERP), (Exchange.OKX, MarketType.PERP)):
            if key in liquidities:
                return liquidities[key].mid_price
        return next(iter(liquidities.values())).mid_price

    def _public_books(self, symbol: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for (exchange, book_symbol, market), book in self._books.items():
            if book_symbol == symbol:
                result.setdefault(exchange.value, {})[market.value] = {
                    "exchange": exchange.value, "market": market.value,
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

    def _oi_change(self, exchange: Exchange, symbol: str) -> float | None:
        history = self._derivatives.get((exchange, symbol))
        if not history:
            return None
        latest = history[-1]
        prior = next((entry for entry in history if entry.timestamp >= latest.timestamp - 300_000), history[0])
        return None if prior.open_interest == 0 else (latest.open_interest - prior.open_interest) / prior.open_interest

    def _funding(self, symbol: str) -> float:
        values = [snapshot.funding_rate for exchange in Exchange if (snapshot := self._latest_derivative(exchange, symbol)) is not None and snapshot.funding_rate is not None]
        return sum(values) / len(values) if values else 0.0

    def _depth_for_market(self, symbol: str, market: MarketType, side: str) -> float:
        total = 0.0
        for (exchange, book_symbol, book_market), book in self._books.items():
            if book_symbol == symbol and book_market is market:
                try:
                    liquidity = calculate_liquidity(book)
                except ValueError:
                    continue
                total += liquidity.ask_depth_2 if side == "ask" else liquidity.bid_depth_2
        return total

    @staticmethod
    def _ratio(value: float | None, depth: float) -> float | None:
        return pressure(float(value or 0), depth)

    @staticmethod
    def _delta_ratio(buy_volume: float | None, sell_volume: float | None) -> float:
        buy, sell = float(buy_volume or 0), float(sell_volume or 0)
        total = buy + sell
        return (buy - sell) / total if total else 0.0
