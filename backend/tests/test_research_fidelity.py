"""Fidelity contract for the research panel: the offline rebuild must equal online.

Every conclusion the research loop produces is a statement about what the live system
saw. That only holds if ``build_panel`` reconstructs the same numbers ``runtime``
computed. Nothing about a divergence here is loud -- the panel returns plausible floats
either way -- so it is pinned rather than left to inspection. The research spec calls
this test mandatory for a narrow reason: if it fails, every downstream number is
measuring a system that does not exist.

The reference below re-derives each cell by hand from the same rows and calls the real
``app.scoring`` / ``app.trade_signal`` functions. What it does NOT copy is the
aggregation -- which rows combine, in what order, under what freshness rule -- because
that aggregation is exactly what the panel has to get right. Where the reference and the
panel disagree, the panel is wrong.

Two aggregation rules are subtle enough to be worth stating, both taken from
``runtime._persist_flow`` and ``runtime._build_detail``:

* The cross-exchange pressures fed to ``activity_score`` are NOT the per-exchange
  ``buy_pressure_5m`` columns. They are recomputed as (summed volume) / (summed 2%
  depth) across exchanges, and a volume enters the numerator only if that same exchange
  also had a usable book. The stored per-exchange columns feed ``cross_exchange_state``
  and ``classify_move`` instead. Using one where the other belongs shifts the score by a
  few points and is invisible in any output you would think to check.
* The cvd ratio uses volume summed over every exchange with a flow row, including those
  with no book. It is a flow-composition measure, not a depth-relative one.

``liquidity_fragility`` is deliberately absent from the rebuild: it needs
``capital_to_move_up[2]``, which ``market_metrics`` does not store. The panel reads it
from ``signal_metrics`` instead, and ``test_fragility_comes_from_the_persisted_column``
pins that it is taken rather than quietly recomputed from the columns that do exist.
"""

from __future__ import annotations

import math
import tracemalloc
import unittest

import numpy as np

from app.flow import pressure
from app.scoring import (
    ClassificationThresholds,
    MarketSignal,
    activity_score,
    aggregate_move_type,
    classify_move,
    cross_exchange_state,
)
from app.trade_signal import TradeBias, calculate_trade_signal
from research.panel import BIAS_CODES, build_panel
from tests.research_helpers import (
    at,
    derivative_row,
    flow_row,
    insert,
    market_row,
    memory_session_factory,
    signal_row,
)

EXCHANGES = ("binance", "okx")
MARKETS = ("spot", "perp")
THRESHOLDS = ClassificationThresholds()
BOOK_TOLERANCE = 30
DERIVATIVE_TOLERANCE = 120


# ---------------------------------------------------------------------------------
# Reference implementation: naive, row by row, no vectorisation and no shortcuts.
# ---------------------------------------------------------------------------------


def _latest(rows, at_time, tolerance, key):
    """The most recent row per key at or before ``at_time``, within ``tolerance``."""
    chosen: dict = {}
    for row in rows:
        age = (at_time - row["timestamp"]).total_seconds()
        if 0 <= age <= tolerance:
            current = chosen.get(key(row))
            if current is None or row["timestamp"] >= current["timestamp"]:
                chosen[key(row)] = row
    return chosen


def _pressure_magnitude(buy, sell):
    values = [value for value in (buy, sell) if value is not None]
    return max(values) if values else None


def _delta_ratio(buy, sell):
    if buy is None or sell is None:
        return None
    total = buy + sell
    return (buy - sell) / total if total else 0.0


def _largest_absolute(values):
    available = [value for value in values if value is not None]
    return max(available, key=abs) if available else None


def _ratio(volume, depth):
    return None if volume is None else pressure(volume, depth)


def _combined_flow(books, flows, market):
    """Mirror of runtime._persist_flow for one market, written the long way."""
    volume_keys = ("buy_volume_1m", "sell_volume_1m", "buy_volume_5m", "sell_volume_5m")
    combined = {key: None for key in volume_keys}
    with_book = {key: None for key in volume_keys}
    for exchange in EXCHANGES:
        flow = flows.get((exchange, market))
        if flow is None:
            continue
        for key in volume_keys:
            value = flow[key]
            if value is None:
                continue
            combined[key] = (combined[key] or 0.0) + value
            if books.get((exchange, market)) is not None:
                with_book[key] = (with_book[key] or 0.0) + value
    ask_depth = sum(book["ask_depth_2"] for key, book in books.items() if key[1] == market)
    bid_depth = sum(book["bid_depth_2"] for key, book in books.items() if key[1] == market)
    return {
        "buy_pressure_1m": _ratio(with_book["buy_volume_1m"], ask_depth),
        "sell_pressure_1m": _ratio(with_book["sell_volume_1m"], bid_depth),
        "buy_pressure_5m": _ratio(with_book["buy_volume_5m"], ask_depth),
        "sell_pressure_5m": _ratio(with_book["sell_volume_5m"], bid_depth),
        "buy_volume_5m": combined["buy_volume_5m"],
        "sell_volume_5m": combined["sell_volume_5m"],
    }


def reference_point(books, flows, derivatives):
    """Recompute one (time, symbol) cell the way the runtime would have."""
    spot = _combined_flow(books, flows, "spot")
    perp = _combined_flow(books, flows, "perp")

    signals = []
    for exchange in EXCHANGES:
        derivative = derivatives.get(exchange)
        has_book = any(key[0] == exchange for key in books)
        if not has_book and derivative is None:
            continue
        spot_flow = flows.get((exchange, "spot"))
        perp_flow = flows.get((exchange, "perp"))
        signals.append(
            MarketSignal(
                exchange,
                spot_flow["buy_pressure_5m"] if spot_flow else None,
                spot_flow["sell_pressure_5m"] if spot_flow else None,
                perp_flow["buy_pressure_5m"] if perp_flow else None,
                perp_flow["sell_pressure_5m"] if perp_flow else None,
                spot_flow["cvd_5m"] if spot_flow else None,
                perp_flow["cvd_5m"] if perp_flow else None,
                derivative["oi_change_5m"] if derivative else None,
            )
        )

    confirmed = cross_exchange_state(signals, THRESHOLDS)
    move_type = aggregate_move_type(classify_move(signal, THRESHOLDS) for signal in signals)
    oi_change = {
        window: _largest_absolute(
            derivative[f"oi_change_{window}"] for derivative in derivatives.values()
        )
        for window in ("5m", "15m", "1h")
    }
    fundings = [
        derivative["funding_rate"]
        for derivative in derivatives.values()
        if derivative["funding_rate"] is not None
    ]
    funding = sum(fundings) / len(fundings) if fundings else None

    score = activity_score(
        oi_change=oi_change["5m"],
        funding=funding,
        confirmed=confirmed.value == "CONFIRMED",
        spot_pressure_1m=_pressure_magnitude(spot["buy_pressure_1m"], spot["sell_pressure_1m"]),
        spot_pressure_5m=_pressure_magnitude(spot["buy_pressure_5m"], spot["sell_pressure_5m"]),
        spot_cvd_ratio=_delta_ratio(spot["buy_volume_5m"], spot["sell_volume_5m"]),
        perp_pressure_1m=_pressure_magnitude(perp["buy_pressure_1m"], perp["sell_pressure_1m"]),
        perp_pressure_5m=_pressure_magnitude(perp["buy_pressure_5m"], perp["sell_pressure_5m"]),
        perp_cvd_ratio=_delta_ratio(perp["buy_volume_5m"], perp["sell_volume_5m"]),
    )

    # calculate_trade_signal reads the same detail map the dashboard is handed.
    detail = {
        "spot": dict(spot),
        "perp": dict(perp),
        "buy_pressure_5m": perp["buy_pressure_5m"],
        "sell_pressure_5m": perp["sell_pressure_5m"],
        "oi_change_5m": oi_change["5m"],
        "cross_exchange_state": confirmed.value,
    }
    bias = calculate_trade_signal(detail).bias

    price = None
    for key in (("binance", "perp"), ("okx", "perp")):
        if key in books:
            price = books[key]["price"]
            break
    if price is None and books:
        price = next(iter(books.values()))["price"]

    return {
        "activity_score": score,
        "bias": BIAS_CODES[bias.value],
        "price": price,
        "funding": funding,
        "oi_change_5m": oi_change["5m"],
        "oi_change_15m": oi_change["15m"],
        "oi_change_1h": oi_change["1h"],
        "confirmed": 1.0 if confirmed.value == "CONFIRMED" else 0.0,
        "move_type": move_type,
        "tradable": any(key[1] == "perp" for key in books),
    }


# ---------------------------------------------------------------------------------
# A synthetic world with enough shape to catch a mixed-up axis.
# ---------------------------------------------------------------------------------


def _seed(factory, *, symbols, steps, step_seconds=10, drop=()):
    """Write aligned rows on a regular cadence.

    Values vary along every axis -- symbol, step, exchange, market -- so that a panel
    which transposed or reused any two of them would produce different numbers rather
    than matching by accident. ``drop`` omits (exchange, market) pairs entirely, which
    is how a symbol listed on only one venue is represented.
    """
    markets, flows, derivatives, signals = [], [], [], []
    for s_index, symbol in enumerate(symbols):
        for step in range(steps):
            seconds = step * step_seconds
            for e_index, exchange in enumerate(EXCHANGES):
                for m_index, market in enumerate(MARKETS):
                    if (exchange, market) in drop:
                        continue
                    salt = 1 + s_index * 7 + step * 3 + e_index * 11 + m_index * 5
                    markets.append(
                        market_row(
                            seconds=seconds,
                            exchange=exchange,
                            symbol=symbol,
                            market=market,
                            price=100.0 + s_index * 10 + step * 0.5 + e_index * 0.01,
                            ask_depth_2=500.0 + salt * 13,
                            bid_depth_2=400.0 + salt * 17,
                            obi=((salt % 21) - 10) / 10,
                        )
                    )
                    flows.append(
                        flow_row(
                            seconds=seconds,
                            exchange=exchange,
                            symbol=symbol,
                            market=market,
                            buy_volume_1m=salt * 3.0,
                            sell_volume_1m=salt * 1.5,
                            buy_volume_5m=salt * 11.0,
                            sell_volume_5m=salt * 6.0,
                            buy_pressure_5m=(salt % 9) * 0.5,
                            sell_pressure_5m=(salt % 5) * 0.5,
                            buy_pressure_1m=(salt % 7) * 0.4,
                            sell_pressure_1m=(salt % 4) * 0.4,
                        )
                    )
                derivatives.append(
                    derivative_row(
                        seconds=seconds,
                        exchange=exchange,
                        symbol=symbol,
                        oi_change_5m=((s_index + step + e_index) % 11 - 5) / 100,
                        oi_change_15m=((s_index + step) % 7 - 3) / 100,
                        oi_change_1h=((step + e_index) % 5 - 2) / 100,
                        funding_rate=(s_index + e_index + 1) * 1e-5,
                    )
                )
            signals.append(
                signal_row(
                    seconds=seconds,
                    symbol=symbol,
                    liquidity_fragility=10.0 + s_index * 5 + step,
                )
            )
    insert(factory, "market_metrics", markets)
    insert(factory, "flow_metrics", flows)
    insert(factory, "derivative_metrics", derivatives)
    insert(factory, "signal_metrics", signals)
    return {"market": markets, "flow": flows, "derivative": derivatives, "signal": signals}


def _for_symbol(rows, symbol):
    return [row for row in rows if row["symbol"] == symbol]


def _venue(row):
    return (row["exchange"], row["market"])


def _exchange(row):
    return row["exchange"]


class PanelFidelityTests(unittest.IsolatedAsyncioTestCase):
    """The panel must equal a hand rebuild of the same rows, cell for cell."""

    def _expect(self, rows, symbol, moment):
        books = _latest(_for_symbol(rows["market"], symbol), moment, BOOK_TOLERANCE, _venue)
        if not books:
            return None
        flows = _latest(_for_symbol(rows["flow"], symbol), moment, BOOK_TOLERANCE, _venue)
        derivatives = _latest(
            _for_symbol(rows["derivative"], symbol), moment, DERIVATIVE_TOLERANCE, _exchange
        )
        return reference_point(books, flows, derivatives)

    async def test_every_cell_matches_the_reference_rebuild(self) -> None:
        factory = memory_session_factory()
        symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
        rows = _seed(factory, symbols=symbols, steps=40)

        panel = await build_panel(
            factory,
            start=at(0),
            end=at(390),
            step_seconds=10,
            fresh_tolerance_seconds=BOOK_TOLERANCE,
        )
        self.assertEqual(panel.symbols, symbols)

        compared = 0
        for t_index, epoch in enumerate(panel.grid):
            moment = at(epoch - at(0).timestamp())
            for s_index, symbol in enumerate(symbols):
                want = self._expect(rows, symbol, moment)
                if want is None:
                    continue
                for name in (
                    "activity_score",
                    "bias",
                    "price",
                    "oi_change_5m",
                    "oi_change_15m",
                    "oi_change_1h",
                    "funding",
                ):
                    got = float(panel.feature(name)[t_index, s_index])
                    expected = want[name]
                    with self.subTest(feature=name, t=t_index, symbol=symbol):
                        if expected is None:
                            self.assertTrue(math.isnan(got), f"expected NaN, got {got}")
                        else:
                            self.assertAlmostEqual(got, float(expected), places=5)
                self.assertEqual(bool(panel.tradable[t_index, s_index]), want["tradable"])
                compared += 1
        self.assertGreater(compared, 100, "the comparison itself has to cover the grid")

    async def test_score_matches_when_only_one_venue_lists_the_symbol(self) -> None:
        """A missing exchange must drop out of each average, not enter it as zero."""
        factory = memory_session_factory()
        rows = _seed(
            factory, symbols=("SOLOUSDT",), steps=8, drop={("okx", "spot"), ("okx", "perp")}
        )
        panel = await build_panel(
            factory,
            start=at(0),
            end=at(70),
            step_seconds=10,
            fresh_tolerance_seconds=BOOK_TOLERANCE,
        )
        checked = 0
        for t_index, epoch in enumerate(panel.grid):
            want = self._expect(rows, "SOLOUSDT", at(epoch - at(0).timestamp()))
            if want is None:
                continue
            self.assertAlmostEqual(
                float(panel.feature("activity_score")[t_index, 0]),
                want["activity_score"],
                places=5,
            )
            checked += 1
        self.assertGreater(checked, 0)

    async def test_move_type_and_cross_exchange_state_match(self) -> None:
        factory = memory_session_factory()
        rows = _seed(factory, symbols=("AAAUSDT", "BBBUSDT"), steps=10)
        panel = await build_panel(
            factory,
            start=at(0),
            end=at(90),
            step_seconds=10,
            fresh_tolerance_seconds=BOOK_TOLERANCE,
        )
        for t_index, epoch in enumerate(panel.grid):
            moment = at(epoch - at(0).timestamp())
            for s_index, symbol in enumerate(panel.symbols):
                want = self._expect(rows, symbol, moment)
                if want is None:
                    continue
                self.assertEqual(
                    panel.categorical("move_type")[t_index, s_index], want["move_type"].value
                )
                self.assertAlmostEqual(
                    float(panel.feature("confirmed")[t_index, s_index]), want["confirmed"]
                )


class PanelContractTests(unittest.IsolatedAsyncioTestCase):
    """Grid, freshness and masking semantics, independent of the score arithmetic."""

    async def test_grid_is_evenly_spaced_and_inside_the_requested_window(self) -> None:
        factory = memory_session_factory()
        _seed(factory, symbols=("AAAUSDT",), steps=20)
        panel = await build_panel(
            factory, start=at(0), end=at(100), step_seconds=10, fresh_tolerance_seconds=30
        )
        self.assertTrue(np.all(np.diff(panel.grid) == 10))
        self.assertGreaterEqual(panel.grid[0], at(0).timestamp())
        self.assertLessEqual(panel.grid[-1], at(100).timestamp())
        self.assertEqual(panel.shape, (len(panel.grid), len(panel.symbols)))

    async def test_a_row_older_than_the_tolerance_is_not_used(self) -> None:
        """Staleness is the whole reason online and a naive rebuild can diverge."""
        factory = memory_session_factory()
        insert(
            factory,
            "market_metrics",
            [market_row(seconds=0, exchange="binance", symbol="AAAUSDT", market="perp")],
        )
        insert(
            factory,
            "flow_metrics",
            [
                flow_row(
                    seconds=0,
                    exchange="binance",
                    symbol="AAAUSDT",
                    market="perp",
                    buy_volume_5m=100.0,
                )
            ],
        )
        panel = await build_panel(
            factory, start=at(0), end=at(60), step_seconds=10, fresh_tolerance_seconds=25
        )
        prices = panel.feature("price")[:, 0]
        fresh = panel.grid - at(0).timestamp() <= 25
        self.assertTrue(np.all(np.isfinite(prices[fresh])), "rows inside the tolerance are used")
        self.assertTrue(np.all(np.isnan(prices[~fresh])), "rows past the tolerance are not")

    async def test_tradable_requires_a_fresh_perpetual_book(self) -> None:
        """Spot-only coverage is not tradable: paper positions are only ever perp."""
        factory = memory_session_factory()
        _seed(factory, symbols=("SPOTONLY",), steps=6, drop={("binance", "perp"), ("okx", "perp")})
        panel = await build_panel(
            factory, start=at(0), end=at(50), step_seconds=10, fresh_tolerance_seconds=30
        )
        self.assertFalse(panel.tradable.any())
        self.assertTrue(
            np.all(np.isfinite(panel.feature("price")[:, 0])), "price still resolves from spot"
        )

    async def test_a_symbol_with_no_rows_in_the_window_is_absent(self) -> None:
        factory = memory_session_factory()
        _seed(factory, symbols=("AAAUSDT",), steps=6)
        insert(
            factory,
            "market_metrics",
            [market_row(seconds=100_000, exchange="binance", symbol="LATEUSDT", market="perp")],
        )
        panel = await build_panel(
            factory, start=at(0), end=at(50), step_seconds=10, fresh_tolerance_seconds=30
        )
        self.assertEqual(panel.symbols, ("AAAUSDT",))

    async def test_requested_symbols_restrict_the_panel(self) -> None:
        factory = memory_session_factory()
        _seed(factory, symbols=("AAAUSDT", "BBBUSDT", "CCCUSDT"), steps=6)
        panel = await build_panel(
            factory,
            start=at(0),
            end=at(50),
            step_seconds=10,
            fresh_tolerance_seconds=30,
            symbols=("CCCUSDT", "AAAUSDT"),
        )
        self.assertEqual(panel.symbols, ("AAAUSDT", "CCCUSDT"), "sorted, not caller order")

    async def test_fragility_comes_from_the_persisted_column(self) -> None:
        """market_metrics has no capital_to_move_up, so fragility cannot be rebuilt.

        Taking the online value is the honest option; quietly recomputing a different
        number from the columns that do exist would look identical and be wrong.
        """
        factory = memory_session_factory()
        _seed(factory, symbols=("AAAUSDT",), steps=6)
        panel = await build_panel(
            factory, start=at(0), end=at(50), step_seconds=10, fresh_tolerance_seconds=30
        )
        got = panel.feature("liquidity_fragility")[:, 0]
        finite = np.isfinite(got)
        want = np.array([10.0 + step for step in range(int(finite.sum()))])
        np.testing.assert_allclose(got[finite], want)

    async def test_raw_columns_are_exposed_per_exchange_and_market(self) -> None:
        """Phase C tests components venue by venue, so the raw grid has to survive."""
        factory = memory_session_factory()
        rows = _seed(factory, symbols=("AAAUSDT",), steps=8)
        panel = await build_panel(
            factory,
            start=at(0),
            end=at(70),
            step_seconds=10,
            fresh_tolerance_seconds=30,
            raw_columns=("obi",),
        )
        for exchange in EXCHANGES:
            for market in MARKETS:
                name = f"{exchange}_{market}_obi"
                self.assertIn(name, panel.features)
                expected = next(
                    row["obi"]
                    for row in rows["market"]
                    if row["exchange"] == exchange
                    and row["market"] == market
                    and row["timestamp"] == at(0)
                )
                self.assertAlmostEqual(float(panel.feature(name)[0, 0]), expected, places=5)
        self.assertNotIn("binance_perp_ask_depth_2", panel.features, "only what was asked for")

    async def test_unknown_feature_raises_rather_than_returning_nan(self) -> None:
        factory = memory_session_factory()
        _seed(factory, symbols=("AAAUSDT",), steps=6)
        panel = await build_panel(
            factory, start=at(0), end=at(50), step_seconds=10, fresh_tolerance_seconds=30
        )
        with self.assertRaises(KeyError):
            panel.feature("no_such_feature")

    async def test_an_empty_window_yields_an_empty_panel_rather_than_an_error(self) -> None:
        factory = memory_session_factory()
        panel = await build_panel(
            factory, start=at(0), end=at(50), step_seconds=10, fresh_tolerance_seconds=30
        )
        self.assertEqual(panel.symbols, ())
        self.assertEqual(panel.shape[1], 0)


class BiasEncodingTests(unittest.TestCase):
    def test_every_bias_has_a_distinct_numeric_code(self) -> None:
        """Features are float arrays, so bias travels as a number; pin the mapping."""
        self.assertEqual(BIAS_CODES[TradeBias.LONG.value], 1.0)
        self.assertEqual(BIAS_CODES[TradeBias.SHORT.value], -1.0)
        self.assertEqual(BIAS_CODES[TradeBias.NONE.value], 0.0)


class PanelScalingTests(unittest.IsolatedAsyncioTestCase):
    """Memory must track the grid, not the number of rows behind it.

    Measured on 2026-09-14 against the first implementation: 566 MB of peak traced
    allocation for 338,400 rows, which extrapolates to about 7.9 GB for one real day
    (46 symbols at a ~1.7s cadence produce roughly 4.7M rows across the four tables).
    That is not a slow path, it is an unusable one, and nothing about it shows up in a
    test built on a few hundred synthetic rows.

    The fix is to stop materialising the window. Both the rows and the grid are ordered
    by time, so one forward pass can keep only the latest row per (symbol, venue) and
    drop the rest. Then memory depends on the panel being built rather than on how long
    the collector happened to be running.

    This test holds the grid fixed and makes the rows behind it eight times denser. A
    panel that buffers the window pays eight times the memory; one that streams pays
    almost nothing extra.
    """

    @staticmethod
    async def _peak_bytes(factory, *, end_seconds: float) -> int:
        tracemalloc.start()
        try:
            await build_panel(
                factory,
                start=at(0),
                end=at(end_seconds),
                step_seconds=10,
                fresh_tolerance_seconds=30,
            )
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    async def test_memory_does_not_track_row_density(self) -> None:
        window = 600.0
        symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT")

        sparse = memory_session_factory()
        _seed(sparse, symbols=symbols, steps=int(window / 10), step_seconds=10)
        dense = memory_session_factory()
        _seed(dense, symbols=symbols, steps=int(window / 1.25), step_seconds=1.25)

        await self._peak_bytes(sparse, end_seconds=window)  # warm up allocators
        sparse_peak = await self._peak_bytes(sparse, end_seconds=window)
        dense_peak = await self._peak_bytes(dense, end_seconds=window)

        ratio = dense_peak / sparse_peak
        self.assertLess(
            ratio,
            3.0,
            msg=(
                f"eight times the rows behind the same grid cost {ratio:.1f}x the peak "
                f"memory ({sparse_peak / 1e6:.1f} MB -> {dense_peak / 1e6:.1f} MB). "
                "The panel is buffering the window instead of streaming it, which does "
                "not fit in memory for a real day of data."
            ),
        )


if __name__ == "__main__":
    unittest.main()
