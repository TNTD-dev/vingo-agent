"""Chạy migration trên Postgres 18 thật (Testcontainers). Cần Docker."""

import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

TABLES = {"conversation", "turn", "message", "summary", "resource_ref", "pending_action", "execution"}


@pytest.fixture(scope="module")
def database_url():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:18", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        subprocess.run(["alembic", "upgrade", "head"], check=True, env={**os.environ, "DATABASE_URL": url})
        yield url


async def test_upgradeHead_createsSevenAgentTables(database_url):
    engine = create_async_engine(database_url)
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'agent'"))
        names = {row[0] for row in rows}
    await engine.dispose()

    assert TABLES <= names


async def test_onePendingPerConversation_isEnforced(database_url):
    engine = create_async_engine(database_url)
    insert = text(
        "INSERT INTO agent.pending_action (conversation_id, actor_id, role, operation, state, payload, display,"
        " expires_at) VALUES (:c, uuidv7(), 'BUYER', 'HOLD_SEATS', 'PENDING', '{}', '{}', now())"
    )
    async with engine.begin() as conn:
        conversation_id = (
            await conn.execute(
                text("INSERT INTO agent.conversation (owner_id, role) VALUES (uuidv7(), 'BUYER') RETURNING id")
            )
        ).scalar_one()
        await conn.execute(insert, {"c": conversation_id})
    with pytest.raises(Exception, match="uq_pending_action_one_pending"):
        async with engine.begin() as conn:
            await conn.execute(insert, {"c": conversation_id})
    await engine.dispose()
