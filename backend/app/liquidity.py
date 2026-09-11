"""Depth, market-impact, and capital-to-move calculations."""

from __future__ import annotations

from dataclasses import dataclass

from app.models import OrderBook, PriceLevel

IMPACT_NOTIONALS = (1_000, 5_000, 10_000, 25_000, 50_000, 100_000)
DEPTH_BANDS = (0.005, 0.01, 0.02, 0.05)


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


def calculate_liquidity(
    order_book: OrderBook,
    *,
    impact_notionals: tuple[int, ...] | None = None,
    capital_percentages: tuple[int, ...] | None = None,
) -> LiquidityMetrics:
    if not order_book.bids or not order_book.asks:
        raise ValueError("both sides of the order book are required")
    bids = _sorted_levels(order_book.bids, reverse=True)
    asks = _sorted_levels(order_book.asks)
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    bid_depths, ask_depths = _depths_at_bands(bids, asks, mid)
    bid_depth_2, ask_depth_2 = bid_depths[2], ask_depths[2]
    denominator = bid_depth_2 + ask_depth_2
    imbalance = 0.0 if denominator == 0 else (bid_depth_2 - ask_depth_2) / denominator
    notionals = IMPACT_NOTIONALS if impact_notionals is None else impact_notionals
    percentages = (1, 2, 5) if capital_percentages is None else capital_percentages
    return LiquidityMetrics(
        mid_price=mid,
        spread_percent=(best_ask - best_bid) / mid * 100,
        bid_depth_0_5=bid_depths[0],
        ask_depth_0_5=ask_depths[0],
        bid_depth_1=bid_depths[1],
        ask_depth_1=ask_depths[1],
        bid_depth_2=bid_depth_2,
        ask_depth_2=ask_depth_2,
        bid_depth_5=bid_depths[3],
        ask_depth_5=ask_depths[3],
        buy_impacts={
            notional: _market_impact(asks, mid, float(notional)) for notional in notionals
        },
        sell_impacts={
            notional: _market_impact(bids, mid, float(notional)) for notional in notionals
        },
        capital_to_move_up=_capital_to_reach_many(asks, mid, percentages, is_buy=True),
        capital_to_move_down=_capital_to_reach_many(bids, mid, percentages, is_buy=False),
        order_book_imbalance=imbalance,
    )


def _sorted_levels(levels: list[PriceLevel], reverse: bool = False) -> list[tuple[float, float]]:
    return sorted(
        ((level.price, level.quantity) for level in levels),
        reverse=reverse,
    )


def _depths_at_bands(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    mid: float,
) -> tuple[list[float], list[float]]:
    bid_depths = [0.0 for _ in DEPTH_BANDS]
    ask_depths = [0.0 for _ in DEPTH_BANDS]
    bid_floors = [mid * (1 - band) for band in DEPTH_BANDS]
    ask_ceilings = [mid * (1 + band) for band in DEPTH_BANDS]
    for price, quantity in bids:
        notional = price * quantity
        for index, floor in enumerate(bid_floors):
            if price >= floor:
                bid_depths[index] += notional
    for price, quantity in asks:
        notional = price * quantity
        for index, ceiling in enumerate(ask_ceilings):
            if price <= ceiling:
                ask_depths[index] += notional
    return bid_depths, ask_depths


def _market_impact(
    levels: list[tuple[float, float]], mid: float, target_notional: float
) -> float | None:
    remaining, acquired = target_notional, 0.0
    for price, quantity in levels:
        available_notional = price * quantity
        take_notional = min(remaining, available_notional)
        acquired += take_notional / price
        remaining -= take_notional
        if remaining <= target_notional * 1e-12:
            vwap = target_notional / acquired
            return (vwap - mid) / mid
    return None


def _capital_to_reach_many(
    levels: list[tuple[float, float]],
    mid: float,
    percentages: tuple[int, ...],
    *,
    is_buy: bool,
) -> dict[int, float | None]:
    results: dict[int, float | None] = {percent: None for percent in percentages}
    targets = sorted(
        (
            (percent, mid * (1 + percent / 100)) if is_buy else (percent, mid * (1 - percent / 100))
            for percent in results
        ),
        key=lambda item: item[1],
        reverse=not is_buy,
    )
    target_index = 0
    capital = 0.0
    for price, quantity in levels:
        capital += price * quantity
        while target_index < len(targets):
            percent, target = targets[target_index]
            if (is_buy and price < target) or (not is_buy and price > target):
                break
            results[percent] = capital
            target_index += 1
    return results
