"""Local-only perpetual paper trading driven by normalized dashboard state."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from app.repository import DuplicateOpenTrade, PaperTradeRepository
from app.trade_signal import TradeBias, TradeSignal, TradeSignalThresholds, calculate_trade_signal


class ArmState(StrEnum):
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    COOLDOWN = "COOLDOWN"


@dataclass(frozen=True)
class PaperTradingSettings:
    enabled: bool = True
    entry_activity_score: float = 80.0
    rearm_activity_score: float = 65.0
    signal_persistence_seconds: float = 5.0
    cooldown_minutes: float = 15.0
    notional_usdt: float = 1000.0
    max_open_positions: int = 5
    take_profit_pct: float = 0.02
    stop_loss_pct: float = 0.01
    max_holding_minutes: float = 60.0
    fee_bps: float = 5.0
    require_cross_exchange_confirmation: bool = False


def load_paper_trading_settings(path: Path) -> PaperTradingSettings:
    """Load the dedicated settings section while preserving safe defaults."""
    with path.open(encoding="utf-8") as file:
        configured = yaml.safe_load(file) or {}
    values = configured.get("paper_trading", {})
    if not isinstance(values, Mapping):
        raise ValueError("paper_trading configuration must be a mapping")
    return PaperTradingSettings(**dict(values))


@dataclass(frozen=True)
class SimulatedFill:
    vwap: float
    quantity: float
    quote_notional: float
    slippage: float


class FillUnavailable(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def simulate_market_fill(
    levels: object,
    *,
    side: str,
    notional_usdt: float | None = None,
    quantity: float | None = None,
) -> SimulatedFill:
    """Consume local order-book levels; never invent a mid-price fill."""
    if (notional_usdt is None) == (quantity is None):
        raise ValueError("exactly one of notional_usdt or quantity is required")
    parsed = _levels(levels)
    if not parsed:
        raise FillUnavailable("INSUFFICIENT_BOOK_DEPTH")
    ordered = sorted(parsed, key=lambda level: level[0], reverse=side.upper() == "SELL")
    remaining_quote = float(notional_usdt or 0)
    remaining_quantity = float(quantity or 0)
    filled_quantity = 0.0
    filled_quote = 0.0
    for price, available in ordered:
        take = (
            min(available, remaining_quantity)
            if quantity is not None
            else min(available, remaining_quote / price)
        )
        if take <= 0:
            continue
        filled_quantity += take
        filled_quote += take * price
        if quantity is not None:
            remaining_quantity -= take
            complete = remaining_quantity <= 1e-10
        else:
            remaining_quote -= take * price
            complete = remaining_quote <= 1e-7
        if complete:
            vwap = filled_quote / filled_quantity
            best_price = ordered[0][0]
            slippage = (vwap - best_price) / best_price * (1 if side.upper() == "BUY" else -1)
            return SimulatedFill(
                vwap=vwap, quantity=filled_quantity, quote_notional=filled_quote, slippage=slippage
            )
    raise FillUnavailable("INSUFFICIENT_BOOK_DEPTH")


class PaperTradingEngine:
    """Stateful, durable simulator. It only consumes already-normalized public state."""

    def __init__(
        self,
        repository: PaperTradeRepository,
        settings: PaperTradingSettings | None = None,
        signal_thresholds: TradeSignalThresholds | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or PaperTradingSettings()
        self._signal_thresholds = signal_thresholds or TradeSignalThresholds()
        self._positions: dict[str, dict[str, Any]] = {}
        self._arm_state: dict[str, ArmState] = {}
        self._high_since: dict[tuple[str, TradeBias], datetime] = {}
        self._last_bias: dict[str, TradeBias] = {}
        self._cooldowns: dict[str, datetime] = {}
        self._symbol_locks: dict[str, asyncio.Lock] = {}

    async def recover_open_positions(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        self._positions = {row["symbol"]: row for row in await self.repository.get_open_positions()}
        self._arm_state.update({symbol: ArmState.TRIGGERED for symbol in self._positions})
        recent_closed = await self.repository.get_recent_closed_positions(
            now - timedelta(minutes=self.settings.cooldown_minutes)
        )
        for position in recent_closed:
            closed_at = _as_datetime(position["closed_at"])
            expires_at = closed_at + timedelta(minutes=self.settings.cooldown_minutes)
            if expires_at > now:
                self._arm_state[position["symbol"]] = ArmState.COOLDOWN
                self._cooldowns[position["symbol"]] = expires_at

    async def process_update(
        self, symbol: str, detail: Mapping[str, Any], now: datetime | None = None
    ) -> list[dict[str, Any]]:
        symbol = symbol.upper()
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        lock = self._symbol_locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            signal = calculate_trade_signal(detail, self._signal_thresholds)
            events: list[dict[str, Any]] = []
            position = self._positions.get(symbol)
            if position is not None:
                event = await self._maintain_position(position, detail, now)
                if event is not None:
                    events.append(event)
            if not self.settings.enabled:
                return events
            events.extend(await self._consider_entry(symbol, detail, signal, now))
            return events

    async def _consider_entry(
        self, symbol: str, detail: Mapping[str, Any], signal: TradeSignal, now: datetime
    ) -> list[dict[str, Any]]:
        score = _number(detail, "activity_score")
        previous_bias = self._last_bias.get(symbol)
        if previous_bias is not signal.bias:
            self._high_since.pop((symbol, TradeBias.LONG), None)
            self._high_since.pop((symbol, TradeBias.SHORT), None)
            self._last_bias[symbol] = signal.bias
        state = self._arm_state.get(symbol, ArmState.ARMED)
        cooldown = self._cooldowns.get(symbol)
        if state is ArmState.COOLDOWN and cooldown is not None and now >= cooldown:
            if score <= self.settings.rearm_activity_score:
                self._arm_state[symbol] = ArmState.ARMED
                return [await self._event(now, symbol, "REARMED", None, None, detail)]
            self._arm_state[symbol] = ArmState.TRIGGERED
            state = ArmState.TRIGGERED
        if score <= self.settings.rearm_activity_score and state is not ArmState.ARMED:
            self._arm_state[symbol] = ArmState.ARMED
            self._high_since.pop((symbol, TradeBias.LONG), None)
            self._high_since.pop((symbol, TradeBias.SHORT), None)
            return [await self._event(now, symbol, "REARMED", None, None, detail)]
        if (
            state is not ArmState.ARMED
            or score < self.settings.entry_activity_score
            or signal.bias is TradeBias.NONE
        ):
            if signal.bias is TradeBias.NONE or score < self.settings.entry_activity_score:
                self._high_since.pop((symbol, TradeBias.LONG), None)
                self._high_since.pop((symbol, TradeBias.SHORT), None)
            return []
        if (
            self.settings.require_cross_exchange_confirmation
            and signal.cross_exchange_state != "CONFIRMED"
        ):
            return []
        if _execution_book(detail) is None:
            self._high_since.pop((symbol, TradeBias.LONG), None)
            self._high_since.pop((symbol, TradeBias.SHORT), None)
            return []
        key = (symbol, signal.bias)
        since = self._high_since.setdefault(key, now)
        if (now - since).total_seconds() < self.settings.signal_persistence_seconds:
            return []
        self._arm_state[symbol] = ArmState.TRIGGERED
        events = [
            await self._event(
                now,
                symbol,
                "SIGNAL_TRIGGERED",
                None,
                None,
                detail | {"trade_signal": signal.snapshot()},
            )
        ]
        if symbol in self._positions:
            events.append(
                await self._event(now, symbol, "TRADE_SKIPPED", "ALREADY_OPEN", None, detail)
            )
            return events
        if len(self._positions) >= self.settings.max_open_positions:
            events.append(
                await self._event(now, symbol, "TRADE_SKIPPED", "MAX_OPEN_POSITIONS", None, detail)
            )
            return events
        execution = _execution_book(detail)
        if execution is None:
            events.append(
                await self._event(now, symbol, "TRADE_SKIPPED", "NO_EXECUTION_MARKET", None, detail)
            )
            return events
        exchange, market, book = execution
        try:
            fill = simulate_market_fill(
                book["asks"] if signal.bias is TradeBias.LONG else book["bids"],
                side="BUY" if signal.bias is TradeBias.LONG else "SELL",
                notional_usdt=self.settings.notional_usdt,
            )
        except FillUnavailable as error:
            events.append(
                await self._event(now, symbol, "TRADE_SKIPPED", error.reason, None, detail)
            )
            return events
        entry_fee = fill.quote_notional * self.settings.fee_bps / 10_000
        values = {
            "symbol": symbol,
            "exchange": exchange,
            "market": market,
            "side": signal.bias.value,
            "status": "OPEN",
            "opened_at": now,
            "closed_at": None,
            "notional_usdt": fill.quote_notional,
            "quantity": fill.quantity,
            "entry_price": fill.vwap,
            "exit_price": None,
            "entry_fee": entry_fee,
            "exit_fee": None,
            "gross_pnl": None,
            "net_pnl": None,
            "return_pct": None,
            "entry_activity_score": score,
            "entry_liquidity_fragility": _optional_number(detail, "liquidity_fragility"),
            "entry_trade_bias": signal.bias.value,
            "entry_move_type": _string(detail, "move_type"),
            "entry_cross_exchange_state": signal.cross_exchange_state,
            "entry_oi_change_5m": signal.oi_change_5m,
            "entry_funding": _optional_number(detail, "funding", "funding_rate"),
            "entry_buy_pressure_1m": _optional_number(detail, "buy_pressure_1m"),
            "entry_buy_pressure_5m": signal.buy_pressure,
            "entry_sell_pressure_1m": _optional_number(detail, "sell_pressure_1m"),
            "entry_sell_pressure_5m": signal.sell_pressure,
            "entry_spot_cvd_5m": _nested_number(detail, "spot", "cvd_5m"),
            "entry_perp_cvd_5m": _nested_number(detail, "perp", "cvd_5m"),
            "max_favorable_excursion_pct": 0.0,
            "max_adverse_excursion_pct": 0.0,
            "exit_reason": None,
            "holding_seconds": None,
            "settings_snapshot": asdict(self.settings),
            "signal_snapshot": _json_safe(
                dict(detail) | {"trade_signal": signal.snapshot(), "entry_fill": asdict(fill)}
            ),
            "exit_snapshot": None,
        }
        try:
            trade = await self.repository.open_trade(values)
        except DuplicateOpenTrade:
            events.append(
                await self._event(now, symbol, "TRADE_SKIPPED", "ALREADY_OPEN", None, detail)
            )
            return events
        self._positions[symbol] = trade
        events.append(
            await self._event(now, symbol, "TRADE_OPENED", None, trade["id"], {"trade": trade})
        )
        return events

    async def _maintain_position(
        self, position: dict[str, Any], detail: Mapping[str, Any], now: datetime
    ) -> dict[str, Any] | None:
        reference_price = _reference_price(detail)
        if reference_price is None:
            return None
        entry = position["entry_price"]
        signed_return = ((reference_price - entry) / entry) * (
            1 if position["side"] == "LONG" else -1
        )
        position["max_favorable_excursion_pct"] = max(
            position["max_favorable_excursion_pct"], signed_return
        )
        position["max_adverse_excursion_pct"] = min(
            position["max_adverse_excursion_pct"], signed_return
        )
        elapsed = (now - _as_datetime(position["opened_at"])).total_seconds()
        reason = (
            "TAKE_PROFIT"
            if signed_return >= self.settings.take_profit_pct
            else "STOP_LOSS"
            if signed_return <= -self.settings.stop_loss_pct
            else "TIME_STOP"
            if elapsed >= self.settings.max_holding_minutes * 60
            else None
        )
        if reason is None:
            await self.repository.close_trade(
                position["id"],
                {
                    "max_favorable_excursion_pct": position["max_favorable_excursion_pct"],
                    "max_adverse_excursion_pct": position["max_adverse_excursion_pct"],
                },
            )
            return None
        execution = _execution_book(
            detail,
            exchange=str(position["exchange"]),
            market=str(position["market"]),
        )
        if execution is None:
            return await self._event(
                now,
                position["symbol"],
                "TRADE_SKIPPED",
                "NO_EXECUTION_MARKET",
                position["id"],
                detail,
            )
        _, _, book = execution
        try:
            fill = simulate_market_fill(
                book["bids"] if position["side"] == "LONG" else book["asks"],
                side="SELL" if position["side"] == "LONG" else "BUY",
                quantity=position["quantity"],
            )
        except FillUnavailable as error:
            return await self._event(
                now, position["symbol"], "TRADE_SKIPPED", error.reason, position["id"], detail
            )
        gross = (
            (fill.vwap - entry) * position["quantity"] * (1 if position["side"] == "LONG" else -1)
        )
        exit_fee = fill.quote_notional * self.settings.fee_bps / 10_000
        net = gross - position["entry_fee"] - exit_fee
        closed = await self.repository.close_trade(
            position["id"],
            {
                "status": "CLOSED",
                "closed_at": now,
                "exit_price": fill.vwap,
                "exit_fee": exit_fee,
                "gross_pnl": gross,
                "net_pnl": net,
                "return_pct": net / position["notional_usdt"],
                "max_favorable_excursion_pct": position["max_favorable_excursion_pct"],
                "max_adverse_excursion_pct": position["max_adverse_excursion_pct"],
                "exit_reason": reason,
                "holding_seconds": elapsed,
                "exit_snapshot": _json_safe(dict(detail) | {"exit_fill": asdict(fill)}),
            },
        )
        self._positions.pop(position["symbol"], None)
        self._arm_state[position["symbol"]] = ArmState.COOLDOWN
        self._cooldowns[position["symbol"]] = now + timedelta(
            minutes=self.settings.cooldown_minutes
        )
        return await self._event(
            now, position["symbol"], "TRADE_CLOSED", reason, closed["id"], {"trade": closed}
        )

    async def _event(
        self,
        timestamp: datetime,
        symbol: str,
        event_type: str,
        reason: str | None,
        trade_id: int | None,
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        row = await self.repository.append_event(
            timestamp=timestamp,
            symbol=symbol,
            event_type=event_type,
            reason=reason,
            trade_id=trade_id,
            snapshot=_json_safe(snapshot),
        )
        return {
            "type": {"TRADE_OPENED": "OPEN", "TRADE_CLOSED": "CLOSE", "TRADE_SKIPPED": "SKIP"}.get(
                event_type, event_type
            ),
            "data": row,
        }


def _execution_book(
    detail: Mapping[str, Any],
    *,
    exchange: str | None = None,
    market: str | None = None,
) -> tuple[str, str, Mapping[str, Any]] | None:
    books = detail.get("orderbooks")
    candidates: list[tuple[str, str, Mapping[str, Any]]] = []

    def visit(
        value: object, current_exchange: str | None = None, current_market: str | None = None
    ) -> None:
        if isinstance(value, Mapping):
            found_exchange = str(value.get("exchange", current_exchange or "")).lower()
            found_market = str(value.get("market", current_market or "")).lower()
            normalized_market = (
                "perp" if found_market in ("perp", "perpetual", "swap") else found_market
            )
            if "bids" in value and "asks" in value and normalized_market == "perp":
                candidates.append((found_exchange, normalized_market, value))
            for key, nested in value.items():
                if key not in {"bids", "asks"}:
                    visit(
                        nested,
                        key if key.lower() in {"binance", "okx"} else found_exchange,
                        key if key.lower() in {"perp", "perpetual", "swap"} else found_market,
                    )
        elif isinstance(value, list):
            for nested in value:
                visit(nested, current_exchange, current_market)

    visit(books)
    target_exchange = exchange.lower() if exchange else None
    target_market = (
        ("perp" if market in {"perp", "perpetual", "swap"} else market) if market else None
    )
    for candidate_exchange, candidate_market, book in candidates:
        if (target_exchange is None or candidate_exchange == target_exchange) and (
            target_market is None or candidate_market == target_market
        ):
            return candidate_exchange, candidate_market, book
    if target_exchange is not None:
        return None
    for preferred in ("binance", "okx"):
        for candidate_exchange, candidate_market, book in candidates:
            if candidate_exchange == preferred:
                return candidate_exchange, candidate_market, book
    return None


def _levels(raw: object) -> list[tuple[float, float]]:
    if not isinstance(raw, list):
        return []
    result = []
    for level in raw:
        if isinstance(level, Mapping):
            price, quantity = level.get("price"), level.get("quantity", level.get("qty"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, quantity = level[0], level[1]
        else:
            continue
        if (
            isinstance(price, (int, float))
            and isinstance(quantity, (int, float))
            and price > 0
            and quantity > 0
        ):
            result.append((float(price), float(quantity)))
    return result


def _number(values: Mapping[str, Any], key: str) -> float:
    value = values.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _optional_number(values: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = values.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _nested_number(values: Mapping[str, Any], parent: str, key: str) -> float | None:
    nested = values.get(parent)
    return _optional_number(nested, key) if isinstance(nested, Mapping) else None


def _string(values: Mapping[str, Any], key: str) -> str | None:
    value = values.get(key)
    return value if isinstance(value, str) else None


def _reference_price(detail: Mapping[str, Any]) -> float | None:
    value = detail.get("price")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    execution = _execution_book(detail)
    if execution:
        _, _, book = execution
        bids, asks = _levels(book["bids"]), _levels(book["asks"])
        if bids and asks:
            return (bids[0][0] + asks[0][0]) / 2
    return None


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return value
