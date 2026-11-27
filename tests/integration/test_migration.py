"""Chạy migration trên Postgres 18 thật (Testcontainers). Cần Docker."""

import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from vingo_agent.persistence import uploads

pytestmark = pytest.mark.integration

TABLES = {"conversation", "turn", "message", "summary", "resource_ref", "pending_action", "execution", "chat_upload"}


@pytest.fixture(scope="module")
def database_url():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:18", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        subprocess.run(["alembic", "upgrade", "head"], check=True, env={**os.environ, "DATABASE_URL": url})
        yield url


async def test_upgradeHead_createsAgentTables(database_url):
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


async def test_chat_upload_preserves_bytes_and_enforces_owner_and_expiry(database_url, monkeypatch):
    engine = create_async_engine(database_url)
    monkeypatch.setattr(uploads, "transaction", engine.begin)
    owner, other_owner = uuid4(), uuid4()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" />'
    try:
        upload_id = await uploads.save_upload(owner, svg)
        assert upload_id is not None
        assert await uploads.get_uploads(owner, [upload_id], with_content=True) == {upload_id: svg}
        assert await uploads.get_uploads(other_owner, [upload_id], with_content=True) == {}
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE agent.chat_upload SET expires_at = now() - interval '1 second' WHERE id = :id"),
                {"id": upload_id},
            )
        assert await uploads.get_uploads(owner, [upload_id], with_content=True) == {}
    finally:
        await engine.dispose()


async def test_inputUsageMigration_roundTripsWithoutChangingExistingTurns(database_url):
    engine = create_async_engine(database_url)
    env = {**os.environ, "DATABASE_URL": database_url}
    columns_sql = text(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'agent' AND table_name = 'turn' AND column_name LIKE 'last_input_%'"
    )
    try:
        async with engine.begin() as conn:
            cid = (
                await conn.execute(
                    text("INSERT INTO agent.conversation (owner_id, role) VALUES (uuidv7(), 'BUYER') RETURNING id")
                )
            ).scalar_one()
            tid = (
                await conn.execute(
                    text(
                        "INSERT INTO agent.turn (conversation_id, command_id, command_hash, state) "
                        "VALUES (:cid, uuidv7(), 'migration-test', 'COMPLETED') RETURNING id"
                    ),
                    {"cid": cid},
                )
            ).scalar_one()
            row = (
                await conn.execute(
                    text("SELECT last_input_tokens, last_input_chars FROM agent.turn WHERE id = :tid"), {"tid": tid}
                )
            ).one()
            assert row == (None, None)
            columns = (await conn.execute(columns_sql)).all()
            assert sorted(columns) == [("last_input_chars", "integer", "YES"), ("last_input_tokens", "integer", "YES")]
        subprocess.run(["alembic", "downgrade", "0001"], check=True, env=env)
        async with engine.connect() as conn:
            assert (await conn.execute(columns_sql)).all() == []
            assert (
                await conn.execute(text("SELECT state FROM agent.turn WHERE id = :tid"), {"tid": tid})
            ).scalar_one() == "COMPLETED"
        subprocess.run(["alembic", "upgrade", "head"], check=True, env=env)
        async with engine.connect() as conn:
            assert len((await conn.execute(columns_sql)).all()) == 2
            assert (
                await conn.execute(
                    text("SELECT last_input_tokens, last_input_chars FROM agent.turn WHERE id = :tid"), {"tid": tid}
                )
            ).one() == (None, None)
    finally:
        # Không để các test sau ở revision cũ nếu assertion trong nhánh downgrade thất bại.
        subprocess.run(["alembic", "upgrade", "head"], check=True, env=env)
        await engine.dispose()
