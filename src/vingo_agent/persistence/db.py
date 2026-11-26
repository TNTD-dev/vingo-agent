"""Engine và session async. Không giữ transaction qua lời gọi mạng (backend, LLM)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from vingo_agent.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncConnection]:
    """Một transaction ngắn; commit khi thoát khối, rollback khi có lỗi."""
    async with get_engine().begin() as connection:
        yield connection
