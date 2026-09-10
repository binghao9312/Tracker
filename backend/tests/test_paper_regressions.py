import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from app.paper_trading import ArmState, PaperTradingEngine, PaperTradingSettings
from tests.paper_helpers import MemoryPaperRepository, detail


def short_detail(price: float = 100.0) -> dict:
    values = detail(price)
    values["spot"] = {
        "buy_pressure_5m": 1.0,
        "sell_pressure_5m": 3.0,
        "buy_volume_5m": 100.0,
        "sell_volume_5m": 800.0,
        "cvd_5m": -700.0,
    }
    values["perp"] = {
        "buy_pressure_5m": 1.0,
        "sell_pressure_5m": 3.0,
        "buy_volume_5m": 100.0,
        "sell_volume_5m": 800.0,
        "cvd_5m": -700.0,
    }
    return values


class PaperTradingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_bias_change_restarts_signal_persistence(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(repository, PaperTradingSettings(signal_persistence_seconds=5))
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)
        await engine.process_update("BTCUSDT", short_detail(), now + timedelta(seconds=4))
        events = await engine.process_update("BTCUSDT", short_detail(), now + timedelta(seconds=5))
        self.assertFalse(any(event["type"] == "OPEN" for event in events))
        opened = await engine.process_update("BTCUSDT", short_detail(), now + timedelta(seconds=9))
        self.assertTrue(any(event["type"] == "OPEN" for event in opened))

    async def test_concurrent_updates_open_at_most_one_position_per_symbol(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(repository, PaperTradingSettings(signal_persistence_seconds=0))
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await asyncio.gather(*(engine.process_update("BTCUSDT", detail(), now) for _ in range(8)))
        self.assertEqual(len(repository.rows), 1)

    async def test_unavailable_depth_cannot_open_a_trade(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(repository, PaperTradingSettings(signal_persistence_seconds=0))
        unavailable = detail()
        unavailable["orderbooks"] = {"binance": {"perp": None}}

        events = await engine.process_update(
            "BTCUSDT", unavailable, datetime(2025, 1, 1, tzinfo=UTC)
        )

        self.assertFalse(any(event["type"] == "OPEN" for event in events))
        self.assertEqual(repository.rows, {})

    async def test_recovery_restores_recent_close_cooldown(self) -> None:
        repository = MemoryPaperRepository()
        now = datetime(2025, 1, 1, tzinfo=UTC)
        repository.rows[1] = {
            "id": 1,
            "symbol": "BTCUSDT",
            "status": "CLOSED",
            "closed_at": now - timedelta(minutes=2),
        }
        engine = PaperTradingEngine(repository, PaperTradingSettings(cooldown_minutes=15))
        await engine.recover_open_positions(now)
        self.assertEqual(engine._arm_state["BTCUSDT"], ArmState.COOLDOWN)
        self.assertEqual(engine._cooldowns["BTCUSDT"], now + timedelta(minutes=13))

    async def test_marks_favorable_and_adverse_excursion_before_exit(self) -> None:
        repository = MemoryPaperRepository()
        engine = PaperTradingEngine(
            repository,
            PaperTradingSettings(signal_persistence_seconds=0, take_profit_pct=1, stop_loss_pct=1),
        )
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await engine.process_update("BTCUSDT", detail(), now)
        await engine.process_update("BTCUSDT", detail(price=98), now + timedelta(seconds=1))
        await engine.process_update("BTCUSDT", detail(price=103), now + timedelta(seconds=2))
        trade = repository.rows[1]
        self.assertLess(trade["max_adverse_excursion_pct"], 0)
        self.assertGreater(trade["max_favorable_excursion_pct"], 0)
