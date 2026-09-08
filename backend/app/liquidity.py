"""Depth, market-impact, and capital-to-move calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models import OrderBook, PriceLevel

IMPACT_NOTIONALS = (1_000, 5_000, 10_000, 25_000, 50_000, 100_000)
DEPTH_BANDS = (Decimal("0.005"), Decimal("0.01"), Decimal("0.02"), Decimal("0.05"))


@dataclass(frozen=True)
class LiquidityMetrics:
    mid_price: float
    spread_percent: float
    bid_depth_0_5: float
    ask_depth_0_5: float
    bid_depth_1: float
    ask_depth_1: float
    bid_depth_2: float
    ask_depth_2: float
    bid_depth_5: float
    ask_depth_5: float
    buy_impacts: dict[int, float | None]
    sell_impacts: dict[int, float | None]
    capital_to_move_up: dict[int, float | None]
    capital_to_move_down: dict[int, float | None]
    order_book_imbalance: float


def calculate_liquidity(order_book: OrderBook) -> LiquidityMetrics:
    if not order_book.bids or not order_book.asks:
        raise ValueError("both sides of the order book are required")
    bids = _sorted_levels(order_book.bids, reverse=True)
    asks = _sorted_levels(order_book.asks)
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / Decimal(2)
    depths = [_depth_at_band(bids, asks, mid, band) for band in DEPTH_BANDS]
    bid_depth_2, ask_depth_2 = depths[2]
    denominator = bid_depth_2 + ask_depth_2
    imbalance = Decimal(0) if denominator == 0 else (bid_depth_2 - ask_depth_2) / denominator
    return LiquidityMetrics(
        mid_price=float(mid),
        spread_percent=float((best_ask - best_bid) / mid * 100),
        bid_depth_0_5=float(depths[0][0]),
        ask_depth_0_5=float(depths[0][1]),
        bid_depth_1=float(depths[1][0]),
        ask_depth_1=float(depths[1][1]),
        bid_depth_2=float(bid_depth_2),
        ask_depth_2=float(ask_depth_2),
        bid_depth_5=float(depths[3][0]),
        ask_depth_5=float(depths[3][1]),
        buy_impacts={notional: _market_impact(asks, mid, Decimal(notional), is_buy=True) for notional in IMPACT_NOTIONALS},
        sell_impacts={notional: _market_impact(bids, mid, Decimal(notional), is_buy=False) for notional in IMPACT_NOTIONALS},
        capital_to_move_up={percent: _capital_to_reach(asks, mid * (1 + Decimal(percent) / 100), is_buy=True) for percent in (1, 2, 5)},
        capital_to_move_down={percent: _capital_to_reach(bids, mid * (1 - Decimal(percent) / 100), is_buy=False) for percent in (1, 2, 5)},
        order_book_imbalance=float(imbalance),
    )


def _sorted_levels(levels: list[PriceLevel], reverse: bool = False) -> list[tuple[Decimal, Decimal]]:
    return sorted(((Decimal(str(level.price)), Decimal(str(level.quantity))) for level in levels), reverse=reverse)


def _depth_at_band(
    bids: list[tuple[Decimal, Decimal]], asks: list[tuple[Decimal, Decimal]], mid: Decimal, band: Decimal
) -> tuple[Decimal, Decimal]:
    bid_floor, ask_ceiling = mid * (1 - band), mid * (1 + band)
    bid_depth = sum((price * quantity for price, quantity in bids if price >= bid_floor), Decimal(0))
    ask_depth = sum((price * quantity for price, quantity in asks if price <= ask_ceiling), Decimal(0))
    return bid_depth, ask_depth


def _market_impact(
    levels: list[tuple[Decimal, Decimal]], mid: Decimal, target_notional: Decimal, *, is_buy: bool
) -> float | None:
    remaining, acquired = target_notional, Decimal(0)
    for price, quantity in levels:
        available_notional = price * quantity
        take_notional = min(remaining, available_notional)
        acquired += take_notional / price
        remaining -= take_notional
        if remaining == 0:
            vwap = target_notional / acquired
            impact = (vwap - mid) / mid
            return float(impact)
    return None


def _capital_to_reach(
    levels: list[tuple[Decimal, Decimal]], target_price: Decimal, *, is_buy: bool
) -> float | None:
    capital = Decimal(0)
    for price, quantity in levels:
        capital += price * quantity
        if (is_buy and price >= target_price) or (not is_buy and price <= target_price):
            return float(capital)
    return None
