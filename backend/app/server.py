"""ASGI composition with PostgreSQL lifecycle management."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.api import create_app
from app.config import scoring_path
from app.database import build_engine, create_schema, session_factory
from app.paper_trading import PaperTradingEngine, load_paper_trading_settings
from app.repository import MetricRepository, PaperTradeRepository
from app.runtime import LiveRuntime

_database_url = os.environ.get("DATABASE_URL")
if not _database_url:
    raise RuntimeError("DATABASE_URL must be configured before starting the backend server")

_engine = build_engine(_database_url)
_sessions = session_factory(_engine)
_repository = MetricRepository(_sessions)
_paper_repository = PaperTradeRepository(_sessions)
_paper_engine = PaperTradingEngine(_paper_repository, load_paper_trading_settings(scoring_path()))
_runtime: LiveRuntime | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _runtime
    await create_schema(_engine)
    await _paper_engine.recover_open_positions()
    _runtime = LiveRuntime(app.state.dashboard, _repository)
    await _runtime.start()
    try:
        yield
    finally:
        if _runtime is not None:
            await _runtime.stop()
            _runtime = None
        await _engine.dispose()

app = create_app(
    history_repository=_repository,
    paper_repository=_paper_repository,
    paper_engine=_paper_engine,
)
app.router.lifespan_context = lifespan
