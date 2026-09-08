"""ASGI composition with PostgreSQL lifecycle management."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.api import create_app
from app.database import build_engine, create_schema, session_factory
from app.repository import MetricRepository

_database_url = os.environ.get("DATABASE_URL")
if not _database_url:
    raise RuntimeError("DATABASE_URL must be configured before starting the backend server")

_engine = build_engine(_database_url)
_repository = MetricRepository(session_factory(_engine))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await create_schema(_engine)
    try:
        yield
    finally:
        await _engine.dispose()


app = create_app(history_repository=_repository)
app.router.lifespan_context = lifespan
