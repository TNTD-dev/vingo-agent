"""Kiểm seed/reset bằng Flyway v2 thật trên container riêng, không sửa repo backend."""

from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest

from evals.fixtures import ALIASES, connect, delta, dsn, reset, snapshot

pytestmark = [pytest.mark.integration, pytest.mark.eval]


@pytest.fixture(scope="module")
def eval_database():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:18", driver="asyncpg") as pg:
        yield pg.get_connection_url()


async def test_fixture_matchesLatestBackendSchema_andResetsAgent(eval_database, monkeypatch):
    migrations = Path("../vingo-backend/src/main/resources/db/migration")
    if not migrations.exists():
        pytest.fail("Cần repo vingo-backend cạnh vingo-agent để đọc migration (test eval, CI không chạy)")
    conn = await asyncpg.connect(dsn(eval_database))
    try:
        for filename in [
            "V20261119_1400__shared_schema_v2.sql",
            "V20261125_1200__catalog_ticket_type_and_seat_sku.sql",
            "V20261125_1300__catalog_cleanup_sku_price_and_event_zone_sku.sql",
            "V20261125_1700__auth_session_cookie.sql",
        ]:
            await conn.execute((migrations / filename).read_text().replace("${allowDestructiveReset}", "true"))
        await conn.execute(
            "CREATE SCHEMA agent; CREATE TABLE agent.conversation(id int); INSERT INTO agent.conversation VALUES(1)"
        )
    finally:
        await conn.close()
    monkeypatch.setenv("EVAL_ALLOW_RESET", "1")
    t0 = datetime.now(UTC)
    context = await reset("http://localhost:8080/api/v1", eval_database, eval_database, t0)
    assert context["aliases"] == ALIASES
    conn = await connect(eval_database)
    try:
        assert await conn.fetchval("SELECT count(*) FROM agent.conversation") == 0
        assert await conn.fetchval("SELECT sum(total_amount) FROM ticket_order WHERE status='PAID'") == 5000000
        assert await conn.fetchval("SELECT count(*) FROM ticket") == 20
        assert await conn.fetchval("SELECT count(*) FROM ticket_scan WHERE result='OK'") == 8
        assert (
            await conn.fetchval(
                "SELECT capacity-held-sold FROM standing_inventory WHERE showtime_id=$1", ALIASES["st_01"]
            )
            == 10
        )
        assert await conn.fetchval("SELECT count(*) FROM sku") == 3
        assert await conn.fetchval("SELECT count(*) FROM login_session") == 3
        for column, variable in [
            ("starts_at", "st_01_starts_at"),
            ("sale_opens_at", "sale_opens_at"),
            ("sale_closes_at", "sale_closes_at"),
        ]:
            actual = await conn.fetchval(f"SELECT {column} FROM showtime WHERE id=$1", ALIASES["st_01"])
            assert actual == datetime.fromisoformat(context["time"][variable])
        assert await conn.fetchval("SELECT status FROM ticket WHERE id=$1", ALIASES["tick_b1"]) == "ISSUED"
    finally:
        await conn.close()
    before = await snapshot(eval_database)
    await reset("http://localhost:8080/api/v1", eval_database, eval_database, t0, {"no_pending": True, "stock": 2})
    after = await snapshot(eval_database)
    effects, changes = delta(before, after)
    assert effects > 0 and changes["held"] == -2
