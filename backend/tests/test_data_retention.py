import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.api import DashboardState
from app.database import (
    DerivativeMetricRow,
    FlowMetricRow,
    MarketMetricRow,
    PaperTradeEventRow,
    PaperTradeRow,
)
from app.repository import MetricRepository
from app.runtime import LiveRuntime
from app.scoring import DataRetentionSettings, load_data_retention_settings


class DataRetentionConfigurationTests(unittest.TestCase):
    def _config(self, metric_history_days: object) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "scoring.yaml"
        path.write_text(
            f"data_retention:\n  metric_history_days: {metric_history_days}\n",
            encoding="utf-8",
        )
        return path

    def test_default_thirty_day_retention_loads(self) -> None:
        self.assertEqual(DataRetentionSettings(), DataRetentionSettings(metric_history_days=30))
        self.assertEqual(
            load_data_retention_settings(self._config(30)),
            DataRetentionSettings(metric_history_days=30),
        )

    def test_custom_seven_day_retention_loads(self) -> None:
        self.assertEqual(
            load_data_retention_settings(self._config(7)),
            DataRetentionSettings(metric_history_days=7),
        )

    def test_invalid_retention_days_are_rejected_by_the_loader_and_settings(self) -> None:
        for value in (0, -1, True, 7.0, '"7"'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "metric_history_days must be an integer greater than or equal to 1",
                ):
                    DataRetentionSettings(metric_history_days=value)
                with self.assertRaisesRegex(
                    ValueError,
                    "metric_history_days must be an integer greater than or equal to 1",
                ):
                    load_data_retention_settings(self._config(value))


class RetentionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pruning_uses_configured_days_as_hours(self) -> None:
        class PruningMetrics:
            def __init__(self) -> None:
                self.pruned_before: datetime | None = None

            async def prune_metrics(self, before: datetime) -> int:
                self.pruned_before = before
                return 42

        metrics = PruningMetrics()
        runtime = LiveRuntime(DashboardState([]), metrics, metric_history_days=7)
        now = datetime(2025, 1, 8, tzinfo=UTC)

        with patch("app.runtime.datetime") as runtime_datetime:
            runtime_datetime.now.return_value = now
            self.assertEqual(await runtime.prune_historical_metrics(), 42)

        self.assertEqual(metrics.pruned_before, now - timedelta(days=7))


class RecordingTransaction:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def __aenter__(self) -> "RecordingTransaction":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, statement: object) -> SimpleNamespace:
        self.statements.append(statement)
        return SimpleNamespace(rowcount=1)


class RecordingSessions:
    def __init__(self) -> None:
        self.transaction = RecordingTransaction()

    def begin(self) -> RecordingTransaction:
        return self.transaction


class MetricPruningTests(unittest.IsolatedAsyncioTestCase):
    async def test_pruning_issues_deletes_only_for_metric_tables(self) -> None:
        sessions = RecordingSessions()
        repository = MetricRepository(sessions)

        await repository.prune_metrics(datetime(2025, 1, 8, tzinfo=UTC))

        deleted_tables = {statement.table.name for statement in sessions.transaction.statements}
        self.assertSetEqual(
            deleted_tables,
            {
                MarketMetricRow.__tablename__,
                FlowMetricRow.__tablename__,
                DerivativeMetricRow.__tablename__,
            },
        )
        self.assertNotIn(PaperTradeRow.__tablename__, deleted_tables)
        self.assertNotIn(PaperTradeEventRow.__tablename__, deleted_tables)
