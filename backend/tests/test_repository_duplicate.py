import unittest
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from app.repository import DuplicateOpenTrade, PaperTradeRepository


class FailingTransaction:
    async def __aenter__(self) -> "FailingTransaction":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def add(self, _: object) -> None:
        return None

    async def flush(self) -> None:
        raise IntegrityError("insert", {}, RuntimeError("duplicate OPEN symbol"))


class FailingSessions:
    def begin(self) -> FailingTransaction:
        return FailingTransaction()


class DuplicateOpenRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_trade_rejects_durable_duplicate_open_position(self) -> None:
        repository = PaperTradeRepository(FailingSessions())
        with self.assertRaises(DuplicateOpenTrade):
            await repository.open_trade({"symbol": "BTCUSDT", "opened_at": datetime(2025, 1, 1, tzinfo=UTC)})
