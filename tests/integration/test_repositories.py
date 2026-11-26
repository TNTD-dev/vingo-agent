import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from vingo_agent.persistence import repositories as repo

pytest_plugins = ["tests.integration.test_migration"]

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine(database_url, monkeypatch):
    engine = create_async_engine(database_url)

    @asynccontextmanager
    async def transaction():
        async with engine.begin() as conn:
            yield conn

    monkeypatch.setattr(repo, "transaction", transaction)
    yield engine
    await engine.dispose()


async def test_repositories_serializeSeq_andCompareState(engine):
    c = await repo.create_conversation(uuid4(), "BUYER")
    command = uuid4()
    turn = await repo.create_turn(c["id"], command, "hash")
    assert (await repo.create_turn(c["id"], command, "hash"))["id"] == turn["id"]
    with pytest.raises(ValueError, match="COMMAND_CONFLICT"):
        await repo.create_turn(c["id"], command, "different")
    assert await repo.acquire_turn(c["id"], turn["id"], 150)
    assert not await repo.acquire_turn(c["id"], uuid4(), 150)
    await repo.release_turn(c["id"], uuid4())
    assert (await repo.get_conversation(c["id"]))["active_turn_id"] == turn["id"]
    await asyncio.gather(
        *(repo.append_messages(c["id"], turn["id"], [{"kind": "USER", "content": {"text": "x"}}]) for _ in range(5))
    )
    _, rows = await repo.load_context(c["id"])
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5]
    assert await repo.transition_turn(turn["id"], "RUNNING", "COMPLETED")
    assert not await repo.transition_turn(turn["id"], "RUNNING", "FAILED")
    await repo.save_summary(c["id"], 2, "goals")
    await repo.save_summary(c["id"], 1, "stale")
    summary, rows = await repo.load_context(c["id"])
    assert summary["content"] == "goals"
    assert len(rows) == 3
    resource_id = uuid4()
    await repo.add_refs(c["id"], "EVENT", [resource_id, resource_id])
    assert await repo.has_ref(c["id"], "EVENT", resource_id)
    assert not await repo.has_ref(c["id"], "ORDER", resource_id)
    assert not await repo.has_ref(uuid4(), "EVENT", resource_id)
    async with engine.begin() as conn:
        action = (
            await conn.execute(
                text(
                    "INSERT INTO agent.pending_action (conversation_id, actor_id, role, operation, state, "
                    "payload, display, expires_at) VALUES (:c, uuidv7(), 'BUYER', 'HOLD_SEATS', "
                    "'PENDING', '{}', '{}', now()) RETURNING id"
                ),
                {"c": c["id"]},
            )
        ).scalar_one()
    assert not await repo.transition_action(action, "PENDING", 2, "CONFIRMED")
    results = await asyncio.gather(*(repo.transition_action(action, "PENDING", 1, "CONFIRMED") for _ in range(2)))
    assert sorted(results) == [False, True]


async def test_role_canOnlyAccessAgentSchema(engine):
    sql = Path("scripts/local_agent_role.sql").read_text()
    async with engine.begin() as conn:
        # DO chứa dấu chấm phẩy, gửi nguyên khối riêng.
        end = sql.index("END $$;") + len("END $$;")
        await conn.execute(text(sql[:end]))
        for statement in sql[end:].split(";"):
            if statement.strip():
                await conn.execute(text(statement))
        await conn.execute(text("CREATE TABLE IF NOT EXISTS public.business_secret (id int)"))
        await conn.execute(text("SET LOCAL ROLE vingo_agent"))
        await conn.execute(text("INSERT INTO agent.conversation (owner_id, role) VALUES (uuidv7(), 'BUYER')"))
    with pytest.raises(Exception, match="permission denied"):
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE vingo_agent"))
            await conn.execute(text("SELECT * FROM public.business_secret"))
