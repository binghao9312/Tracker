"""Replay persisted metrics through the audited paper-trading engine.

The historical database contains aggregate liquidity rather than order-book levels.
Replay therefore synthesizes exactly one bid and ask at the recorded spread, with
only the recorded 2%-band depth available for fills.  It is an approximation, not
a claim that the historical order book was observed at those levels.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import scoring_path
from app.database import (
    FlowMetricRow,
    MarketMetricRow,
    SignalMetricRow,
    build_engine,
    session_factory,
)
from app.paper_trading import (
    PaperTradingEngine,
    PaperTradingSettings,
    load_paper_trading_settings,
)
from app.repository import PaperTradeRepository
from app.trade_signal import TradeBias

FILL_MODEL = "synthesized single-level book from persisted mid/spread/depth_2"


@dataclass(frozen=True)
class VenueQuote:
    """Persisted top-of-book aggregates for one exchange market."""

    exchange: str
    market: str
    mid: float
    spread_percent: float
    bid_depth_2: float
    ask_depth_2: float


@dataclass(frozen=True)
class Snapshot:
    """One normalized, persisted scanner cadence for a symbol."""

    timestamp: datetime
    symbol: str
    price: float | None
    activity_score: float | None
    liquidity_fragility: float | None
    move_type: str | None
    cross_exchange_state: str | None
    oi_change_5m: float | None
    funding_rate: float | None
    venues: tuple[VenueQuote, ...]
    spot: Mapping[str, float | None]
    perp: Mapping[str, float | None]


@dataclass(frozen=True)
class ReplayResult:
    """Replay output; ``trades`` includes both closed and still-open repository rows."""

    trades: list[dict[str, Any]]
    events: list[dict[str, Any]]
    stats: dict[str, Any]
    fill_model: str
    cadences: int
    gaps: int


class MemoryReplayRepository:
    """Minimal in-memory implementation of the engine's durable repository contract."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self._next_id = 1

    async def open_trade(self, values: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(values) | {"id": self._next_id}
        self.rows[self._next_id] = row
        self._next_id += 1
        return deepcopy(row)

    async def close_trade(self, trade_id: int, values: Mapping[str, Any]) -> dict[str, Any]:
        self.rows[trade_id].update(values)
        return deepcopy(self.rows[trade_id])

    async def get_open_positions(self) -> list[dict[str, Any]]:
        return [deepcopy(row) for row in self.rows.values() if row["status"] == "OPEN"]

    async def get_recent_closed_positions(self, since: datetime) -> list[dict[str, Any]]:
        return [
            deepcopy(row)
            for row in self.rows.values()
            if row["status"] == "CLOSED"
            and row.get("closed_at") is not None
            and row["closed_at"] >= since
        ]

    async def append_event(self, **values: Any) -> dict[str, Any]:
        row = dict(values) | {"id": len(self.events) + 1}
        self.events.append(row)
        return deepcopy(row)


def synthesize_book(quote: VenueQuote) -> dict[str, Any]:
    """Return one executable level per side from persisted quote-notional depth.

    ``spread_percent`` is the full spread relative to mid.  The levels therefore
    sit half that spread from mid, and their base quantities are quote depth divided
    by their individual level prices.
    """
    if not math.isfinite(quote.mid) or quote.mid <= 0:
        raise ValueError("venue mid must be a positive finite number")
    if not math.isfinite(quote.spread_percent) or quote.spread_percent < 0:
        raise ValueError("venue spread_percent must be a finite nonnegative number")
    bid = quote.mid * (1.0 - quote.spread_percent / 200.0)
    ask = quote.mid * (1.0 + quote.spread_percent / 200.0)
    if bid <= 0 or ask <= 0:
        raise ValueError("venue spread produces a nonpositive execution price")
    return {
        "exchange": quote.exchange,
        "market": quote.market,
        "bids": [[bid, max(0.0, quote.bid_depth_2) / bid]],
        "asks": [[ask, max(0.0, quote.ask_depth_2) / ask]],
    }


def _detail(snapshot: Snapshot) -> dict[str, Any]:
    orderbooks: dict[str, dict[str, dict[str, Any]]] = {}
    for quote in snapshot.venues:
        orderbooks.setdefault(quote.exchange, {})[quote.market] = synthesize_book(quote)
    return {
        "price": snapshot.price,
        "activity_score": snapshot.activity_score,
        "liquidity_fragility": snapshot.liquidity_fragility,
        "move_type": snapshot.move_type,
        "cross_exchange_state": snapshot.cross_exchange_state,
        "oi_change_5m": snapshot.oi_change_5m,
        "funding": snapshot.funding_rate,
        "spot": dict(snapshot.spot),
        "perp": dict(snapshot.perp),
        "orderbooks": orderbooks,
    }


def _reset_signal_persistence(engine: PaperTradingEngine, symbol: str) -> None:
    """Forget unobserved arming evidence only.

    A gap means nothing was observed, so accumulated persistence evidence
    (``_high_since``, ``_last_bias``) cannot be trusted across it. A cooldown is a
    different kind of rule: it is a function of elapsed time, not of observation, so
    it must survive the gap -- and so must the COOLDOWN arm state that enforces it.
    Clearing them would re-arm the symbol early and let the replay open trades the
    live engine would never have taken, inflating every statistic downstream. The
    engine re-arms on its own once a surviving cooldown expires.
    """
    engine._high_since.pop((symbol, TradeBias.LONG), None)
    engine._high_since.pop((symbol, TradeBias.SHORT), None)
    engine._last_bias.pop(symbol, None)


def _replay_stats(trades: Sequence[Mapping[str, Any]], fill_model: str) -> dict[str, Any]:
    """Summarize closed trades; a win is a closed trade with positive ``net_pnl``."""
    closed = [trade for trade in trades if trade.get("status") == "CLOSED"]
    wins = [trade for trade in closed if _finite_number(trade.get("net_pnl")) > 0]
    losses = [trade for trade in closed if _finite_number(trade.get("net_pnl")) < 0]
    gross_profit = sum(_finite_number(trade.get("net_pnl")) for trade in wins)
    gross_loss = abs(sum(_finite_number(trade.get("net_pnl")) for trade in losses))
    returns = [
        _finite_number(trade.get("return_pct"))
        for trade in closed
        if trade.get("return_pct") is not None
    ]
    by_exit_reason: dict[str, int] = {}
    for trade in closed:
        reason = trade.get("exit_reason")
        if isinstance(reason, str):
            by_exit_reason[reason] = by_exit_reason.get(reason, 0) + 1
    return {
        "total": len(closed),
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_return": sum(returns) / len(returns) if returns else 0.0,
        "by_exit_reason": by_exit_reason,
        "fill_model": fill_model,
    }


def _finite_number(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return 0.0


async def replay(
    snapshots: Sequence[Snapshot],
    settings: PaperTradingSettings,
    *,
    max_gap_seconds: float = 5.0,
) -> ReplayResult:
    """Replay snapshots in input order, rejecting time reversal and clearing feed gaps."""
    if max_gap_seconds < 0:
        raise ValueError("max_gap_seconds must be nonnegative")

    repository = MemoryReplayRepository()
    engine = PaperTradingEngine(cast(PaperTradeRepository, repository), settings)
    last_timestamp: datetime | None = None
    last_by_symbol: dict[str, datetime] = {}
    gaps = 0

    for snapshot in snapshots:
        timestamp = _utc_timestamp(snapshot.timestamp)
        if last_timestamp is not None and timestamp < last_timestamp:
            raise ValueError("replay snapshots must be in nondecreasing timestamp order")
        last_timestamp = timestamp
        symbol = snapshot.symbol.upper()
        previous = last_by_symbol.get(symbol)
        if previous is not None and (timestamp - previous).total_seconds() > max_gap_seconds:
            _reset_signal_persistence(engine, symbol)
            gaps += 1
        last_by_symbol[symbol] = timestamp
        if snapshot.activity_score is not None:
            await engine.process_update(symbol, _detail(snapshot), timestamp)

    trades = [deepcopy(row) for row in repository.rows.values()]
    events = [deepcopy(event) for event in repository.events]
    return ReplayResult(
        trades=trades,
        events=events,
        stats=_replay_stats(trades, FILL_MODEL),
        fill_model=FILL_MODEL,
        cadences=len(snapshots),
        gaps=gaps,
    )


async def load_snapshots(
    session_factory: async_sessionmaker[AsyncSession],
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    limit: int = 3_600,
) -> list[Snapshot]:
    """Load ascending persisted cadences with a signal row and matching market/flow rows."""
    effective_limit = 3_600 if limit <= 0 else limit
    signal_ids = (
        select(SignalMetricRow.id)
        .where(
            SignalMetricRow.symbol == symbol.upper(),
            SignalMetricRow.timestamp >= start,
            SignalMetricRow.timestamp <= end,
        )
        .order_by(SignalMetricRow.timestamp.asc(), SignalMetricRow.id.asc())
        .limit(effective_limit)
    )
    join_market = and_(
        MarketMetricRow.symbol == SignalMetricRow.symbol,
        MarketMetricRow.timestamp == SignalMetricRow.timestamp,
    )
    join_flow = and_(
        FlowMetricRow.symbol == SignalMetricRow.symbol,
        FlowMetricRow.timestamp == SignalMetricRow.timestamp,
    )
    statement = (
        select(SignalMetricRow, MarketMetricRow, FlowMetricRow)
        .join(MarketMetricRow, join_market)
        .join(FlowMetricRow, join_flow)
        .where(SignalMetricRow.id.in_(signal_ids))
        .order_by(
            SignalMetricRow.timestamp.asc(),
            SignalMetricRow.id.asc(),
            MarketMetricRow.id.asc(),
            FlowMetricRow.id.asc(),
        )
    )
    async with session_factory() as session:
        rows = (await session.execute(statement)).all()

    grouped: dict[tuple[datetime, str], _SnapshotRows] = {}
    for signal, market, flow in rows:
        key = (_utc_timestamp(signal.timestamp), signal.symbol)
        bundle = grouped.setdefault(key, _SnapshotRows(signal=signal))
        bundle.markets[(market.exchange, market.market)] = market
        bundle.flows[(flow.exchange, flow.market)] = flow
    return [
        _snapshot_from_rows(bundle)
        for _, bundle in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1]))
    ]


@dataclass
class _SnapshotRows:
    signal: SignalMetricRow
    markets: dict[tuple[str, str], MarketMetricRow] = field(default_factory=dict)
    flows: dict[tuple[str, str], FlowMetricRow] = field(default_factory=dict)


def _snapshot_from_rows(rows: _SnapshotRows) -> Snapshot:
    venues = tuple(
        VenueQuote(
            exchange=market.exchange,
            market=market.market,
            mid=market.price,
            spread_percent=market.spread,
            bid_depth_2=market.bid_depth_2,
            ask_depth_2=market.ask_depth_2,
        )
        for _, market in sorted(rows.markets.items())
    )
    return Snapshot(
        timestamp=_utc_timestamp(rows.signal.timestamp),
        symbol=rows.signal.symbol,
        price=rows.signal.price,
        activity_score=rows.signal.activity_score,
        liquidity_fragility=rows.signal.liquidity_fragility,
        move_type=rows.signal.move_type,
        cross_exchange_state=rows.signal.cross_exchange_state,
        oi_change_5m=rows.signal.oi_change_5m,
        funding_rate=rows.signal.funding_rate,
        venues=venues,
        spot=_aggregate_flow(rows.flows, "spot"),
        perp=_aggregate_flow(rows.flows, "perp"),
    )


def _aggregate_flow(
    flows: Mapping[tuple[str, str], FlowMetricRow], market: str
) -> dict[str, float | None]:
    """Aggregate recorded flow fields without deriving an unrecorded pressure value."""
    selected = [flow for (_, flow_market), flow in flows.items() if flow_market == market]
    values = {
        key: _sum_non_null(selected, key)
        for key in (
            "buy_volume_1m",
            "sell_volume_1m",
            "buy_volume_5m",
            "sell_volume_5m",
            "cvd_1m",
            "cvd_5m",
        )
    }
    for key in (
        "buy_pressure_1m",
        "buy_pressure_5m",
        "sell_pressure_1m",
        "sell_pressure_5m",
    ):
        values[key] = _max_non_null(selected, key)
    return values


def _sum_non_null(rows: Sequence[FlowMetricRow], key: str) -> float | None:
    values = [getattr(row, key) for row in rows]
    observed = [float(value) for value in values if value is not None]
    return sum(observed) if observed else None


def _max_non_null(rows: Sequence[FlowMetricRow], key: str) -> float | None:
    values = [getattr(row, key) for row in rows]
    observed = [float(value) for value in values if value is not None]
    return max(observed) if observed else None


def _utc_timestamp(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _run_cli(symbol: str, hours: float) -> ReplayResult:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL must be configured for replay")
    engine = build_engine(database_url)
    try:
        now = datetime.now(UTC)
        snapshots = await load_snapshots(
            session_factory(engine), symbol, now - timedelta(hours=hours), now
        )
        return await replay(
            snapshots,
            load_paper_trading_settings(scoring_path()),
        )
    finally:
        await engine.dispose()


def _print_stats(result: ReplayResult) -> None:
    print(f"fill_model: {result.fill_model}")
    print("metric | value")
    for key in ("total", "win_rate", "profit_factor", "average_return", "by_exit_reason"):
        print(f"{key} | {result.stats[key]}")
    print(f"cadences | {result.cadences}")
    print(f"gaps | {result.gaps}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay persisted metrics through paper trading.")
    parser.add_argument("--symbol", required=True, help="Symbol to replay, e.g. BTCUSDT")
    parser.add_argument("--hours", type=float, default=6.0, help="Look back N hours (default: 6)")
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("--hours must be positive")
    result = asyncio.run(_run_cli(args.symbol.strip().upper(), args.hours))
    _print_stats(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
