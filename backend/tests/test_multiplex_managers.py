import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp

from app.api import DashboardState
from app.collectors.orderbooks import BinanceOrderBookManager, OkxOrderBookManager
from app.collectors.trades import BinanceTradeManager, OkxTradeManager
from app.discovery import DiscoveryResult
from app.exchanges.okx import OkxAdapter
from app.models import Exchange, MarketInstrument, MarketType, NormalizedTrade, UniverseAsset
from app.orderbook import LocalOrderBook, OrderBookSequenceGap, SequencedOrderBookSnapshot
from app.runtime import LiveRuntime


def instrument(
    symbol: str,
    exchange_symbol: str,
    *,
    exchange: Exchange = Exchange.OKX,
    market: MarketType = MarketType.PERP,
) -> MarketInstrument:
    return MarketInstrument(
        exchange=exchange,
        symbol=symbol,
        market=market,
        exchange_symbol=exchange_symbol,
        base_quantity_multiplier=0.1,
    )


def message(data: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(data))


class FakeWebSocket:
    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = messages
        self.subscriptions: list[dict[str, object]] = []

    async def __aenter__(self) -> "FakeWebSocket":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send_json(self, payload: dict[str, object]) -> None:
        self.subscriptions.append(payload)

    async def __aiter__(self):
        for item in self.messages:
            yield item


class FakeSession:
    def __init__(self, sockets: list[FakeWebSocket]) -> None:
        self.sockets = sockets
        self.calls = 0
        self.urls: list[str] = []

    def ws_connect(self, url: str, **__: object) -> FakeWebSocket:
        self.urls.append(url)
        socket = self.sockets[self.calls]
        self.calls += 1
        return socket


class SnapshotAdapter:
    def __init__(self, sequences: list[int]) -> None:
        self.sequences = sequences
        self.calls = 0

    async def fetch_order_book_snapshot(
        self, market: MarketInstrument
    ) -> SequencedOrderBookSnapshot:
        sequence = self.sequences[self.calls]
        self.calls += 1
        return SequencedOrderBookSnapshot(
            exchange=Exchange.OKX,
            symbol=market.symbol,
            market=market.market,
            sequence=sequence,
            timestamp=sequence,
            bids=[(99, 0.1)],
            asks=[(100, 0.1)],
        )


class MultiplexManagerRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_binance_combined_stream_urls_depend_on_market(self) -> None:
        cases = (
            (
                MarketType.SPOT,
                "wss://stream.binance.com:9443/stream?streams=btcusdt@depth@100ms",
                "wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade",
            ),
            (
                MarketType.PERP,
                "wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms",
                "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade",
            ),
        )

        async def on_book(_: object) -> None:
            return None

        async def on_trade(_: NormalizedTrade) -> None:
            return None

        for market, depth_url, trade_url in cases:
            with self.subTest(market=market):
                binance = instrument(
                    "BTCUSDT",
                    "BTCUSDT",
                    exchange=Exchange.BINANCE,
                    market=market,
                )
                stop = asyncio.Event()
                stop.set()
                book_session = FakeSession([FakeWebSocket([])])
                await BinanceOrderBookManager(
                    book_session,
                    SnapshotAdapter([100]),
                    market,
                    [(binance, LocalOrderBook())],
                    on_book,
                )._synchronize_and_stream(stop)
                trade_session = FakeSession([FakeWebSocket([])])
                await BinanceTradeManager(
                    trade_session, market, [binance], on_trade
                )._stream(stop)

                self.assertEqual(book_session.urls, [depth_url])
                self.assertEqual(trade_session.urls, [trade_url])

    async def test_one_trade_and_book_connection_routes_multiple_instruments(self) -> None:
        btc, eth = instrument("BTCUSDT", "BTC-USDT-SWAP"), instrument("ETHUSDT", "ETH-USDT-SWAP")
        received_trades: list[NormalizedTrade] = []
        trade_stop = asyncio.Event()

        async def on_trade(trade: NormalizedTrade) -> None:
            received_trades.append(trade)
            if len(received_trades) == 2:
                trade_stop.set()

        trade_socket = FakeWebSocket(
            [
                message(
                    {
                        "arg": {"channel": "trades", "instId": "ETH-USDT-SWAP"},
                        "data": [{"px": "100", "sz": "25", "ts": "1", "side": "buy"}],
                    }
                ),
                message(
                    {
                        "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
                        "data": [{"px": "200", "sz": "25", "ts": "2", "side": "sell"}],
                    }
                ),
            ]
        )
        trade_session = FakeSession([trade_socket])
        await OkxTradeManager(trade_session, MarketType.PERP, [btc, eth], on_trade)._stream(
            trade_stop
        )

        self.assertEqual(trade_session.calls, 1)
        self.assertEqual({trade.symbol for trade in received_trades}, {"BTCUSDT", "ETHUSDT"})
        self.assertEqual({trade.quantity for trade in received_trades}, {2.5})
        self.assertEqual(len(trade_socket.subscriptions[0]["args"]), 2)

        received_books = []
        book_stop = asyncio.Event()

        async def on_book(book: object) -> None:
            received_books.append(book)
            if len(received_books) == 2:
                book_stop.set()

        book_socket = FakeWebSocket(
            [
                message(
                    {
                        "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"},
                        "action": "snapshot",
                        "data": [
                            {
                                "seqId": "2",
                                "ts": "2",
                                "bids": [["99", "25"]],
                                "asks": [["100", "25"]],
                            }
                        ],
                    }
                ),
                message(
                    {
                        "arg": {"channel": "books", "instId": "ETH-USDT-SWAP"},
                        "action": "update",
                        "data": [
                            {"seqId": "2", "prevSeqId": "1", "bids": [["99", "25"]], "asks": []}
                        ],
                    }
                ),
            ]
        )
        book_session = FakeSession([book_socket])
        manager = OkxOrderBookManager(
            book_session,
            SnapshotAdapter([1, 1]),
            MarketType.PERP,
            [(btc, LocalOrderBook()), (eth, LocalOrderBook())],
            on_book,
        )
        await manager._synchronize_and_stream(book_stop)

        self.assertEqual(book_session.calls, 1)
        self.assertEqual({book.symbol for book in received_books}, {"BTCUSDT", "ETHUSDT"})
        self.assertEqual({book.bids[0].quantity for book in received_books}, {2.5})
        self.assertEqual(len(book_socket.subscriptions[0]["args"]), 2)

    async def test_sequence_gap_resnapshots_before_later_increment_is_published(self) -> None:
        btc = instrument("BTCUSDT", "BTC-USDT-SWAP")
        stop = asyncio.Event()
        published = []

        async def on_book(book: object) -> None:
            published.append(book)
            stop.set()

        first = FakeWebSocket(
            [
                message(
                    {
                        "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"},
                        "action": "update",
                        "data": [
                            {"seqId": "3", "prevSeqId": "2", "bids": [["99", "25"]], "asks": []}
                        ],
                    }
                )
            ]
        )
        second = FakeWebSocket(
            [
                message(
                    {
                        "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"},
                        "action": "update",
                        "data": [
                            {"seqId": "101", "prevSeqId": "100", "bids": [["99", "25"]], "asks": []}
                        ],
                    }
                )
            ]
        )
        session = FakeSession([first, second])
        adapter = SnapshotAdapter([1, 100])
        manager = OkxOrderBookManager(
            session, adapter, MarketType.PERP, [(btc, LocalOrderBook())], on_book
        )

        with self.assertRaises(OrderBookSequenceGap):
            await manager._synchronize_and_stream(stop)
        await manager._synchronize_and_stream(stop)

        self.assertEqual(adapter.calls, 2)
        self.assertEqual(published[0].bids[0].quantity, 2.5)

    async def test_okx_sequence_reset_with_matching_prev_seq_id_is_applied(self) -> None:
        btc = instrument("BTCUSDT", "BTC-USDT-SWAP")
        stop = asyncio.Event()
        published = []

        async def on_book(book: object) -> None:
            published.append(book)
            stop.set()

        socket = FakeWebSocket(
            [
                message(
                    {
                        "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"},
                        "action": "update",
                        "data": [
                            {"seqId": "90", "prevSeqId": "89", "bids": [["98", "25"]], "asks": []}
                        ],
                    }
                ),
                message(
                    {
                        "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"},
                        "action": "update",
                        "data": [
                            {"seqId": "1", "prevSeqId": "100", "bids": [["99", "25"]], "asks": []}
                        ],
                    }
                ),
            ]
        )
        session = FakeSession([socket])
        adapter = SnapshotAdapter([100])
        manager = OkxOrderBookManager(
            session, adapter, MarketType.PERP, [(btc, LocalOrderBook())], on_book
        )

        await manager._synchronize_and_stream(stop)

        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].bids[0].price, 99.0)
        self.assertEqual(published[0].bids[0].quantity, 2.5)


class PopulatedDiscovery:
    async def discover(self, _: object) -> DiscoveryResult:
        markets = []
        for exchange in Exchange:
            for market in MarketType:
                for symbol in ("BTCUSDT", "ETHUSDT"):
                    suffix = "SWAP" if market is MarketType.PERP else "SPOT"
                    markets.append(
                        MarketInstrument(
                            exchange=exchange,
                            symbol=symbol,
                            market=market,
                            exchange_symbol=f"{symbol}-{exchange}-{suffix}",
                        )
                    )
        return DiscoveryResult(markets, {})


class RuntimeMultiplexTaskRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_manager_task_count_depends_on_exchange_market_groups_not_instruments(
        self,
    ) -> None:
        runtime = LiveRuntime(
            DashboardState(
                [
                    UniverseAsset(rank=1, symbol="BTC", name="Bitcoin"),
                    UniverseAsset(rank=2, symbol="ETH", name="Ethereum"),
                ]
            ),
            object(),
            session=object(),
            discovery=PopulatedDiscovery(),
        )
        await runtime.start()
        try:
            websocket_tasks = [
                task for task in runtime._tasks if task.get_name().startswith(("trades:", "book:"))
            ]
            self.assertEqual(len(websocket_tasks), 8)
        finally:
            await runtime.stop()


    async def test_okx_book_managers_chunk_fifty_instruments_at_twenty_five(self) -> None:
        instruments = [
            instrument(
                f"ASSET{index}USDT",
                f"ASSET{index}-USDT",
                market=MarketType.SPOT,
            )
            for index in range(50)
        ]
        chunks: list[list[tuple[MarketInstrument, LocalOrderBook]]] = []

        class RecordingOkxOrderBookManager:
            def __init__(
                self,
                _: object,
                __: object,
                ___: MarketType,
                books: list[tuple[MarketInstrument, LocalOrderBook]],
                ____: object,
            ) -> None:
                chunks.append(books)

            async def run(self, stop: asyncio.Event) -> None:
                await stop.wait()

        runtime = LiveRuntime(DashboardState([]), object(), session=object())
        with patch("app.runtime.OkxOrderBookManager", RecordingOkxOrderBookManager):
            tasks = runtime._stream_tasks(
                Exchange.OKX,
                MarketType.SPOT,
                instruments,
                {Exchange.OKX: OkxAdapter(object())},
            )
        try:
            self.assertEqual(
                [task.get_name() for task in tasks],
                ["trades:okx:spot", "book:okx:spot:0", "book:okx:spot:1"],
            )
            self.assertEqual([len(chunk) for chunk in chunks], [25, 25])
            self.assertEqual(
                [[entry[0].exchange_symbol for entry in chunk] for chunk in chunks],
                [
                    [f"ASSET{index}-USDT" for index in range(25)],
                    [f"ASSET{index}-USDT" for index in range(25, 50)],
                ],
            )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    unittest.main()
