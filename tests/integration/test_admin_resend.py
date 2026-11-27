"""KAN-205: tra vé → pending thật → confirm/CAS → executor; Postgres thật, HTTP giả."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from tests.integration.test_actions import store as store
from vingo_agent.actions import confirm, lookup
from vingo_agent.api.auth import Principal
from vingo_agent.persistence import repositories as repo
from vingo_agent.schemas.chat import Role
from vingo_agent.tools import admin_read, proposals
from vingo_agent.tools.base import TurnDeps

pytest_plugins = ["tests.integration.test_migration"]
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("outcome", [202, 409, 503, "timeout"])
async def test_resend_proposalConfirmReplay_useFrozenPayload_andNeverSendTwice(store, outcome):
    principal = Principal(uuid4(), Role.ADMIN, "test-session")
    conv = await repo.create_conversation(principal.user_id, principal.role)
    tid, oid, sid, eid = (str(uuid4()) for _ in range(4))
    at = "2030-01-01T00:00:00+07:00"
    event = {"id": eid, "title": "Demo"}
    showtime = {"id": sid, "starts_at": at}
    ticket = {
        "id": tid,
        "code": "TICKET123",
        "status": "ISSUED",
        "order_code": "ORDER123",
        "event": event,
        "showtime": showtime,
        "zone": "VIP",
        "row": "A",
        "number": 1,
    }
    order = {
        "id": oid,
        "code": "ORDER123",
        "status": "PAID",
        "event": event,
        "showtime": showtime,
        "total_amount": 200000,
        "created_at": at,
    }
    backend = SimpleNamespace(
        read=AsyncMock(side_effect=[{"data": [ticket]}, {"data": [ticket]}, {"data": [order]}]), write=AsyncMock()
    )
    if outcome == "timeout":
        backend.write.side_effect = httpx.ReadTimeout("timeout")
    else:
        backend.write.return_value = httpx.Response(outcome, json={"code": "TICKET_USED"} if outcome == 409 else {})
    ctx = SimpleNamespace(deps=TurnDeps(principal, conv["id"], None, backend))
    assert (await admin_read.admin_list_tickets(ctx, status="ISSUED"))["ok"]
    from uuid import UUID

    proposed = await proposals.propose_admin_resend_ticket(ctx, UUID(tid))
    assert proposed["ok"]
    backend.write.assert_not_awaited()
    card = ctx.deps.blocks[-1]
    args = dict(
        action_id=card.action_id,
        version=card.version,
        principal=principal,
        via=confirm.ConfirmedVia.BUTTON,
        backend=backend,
    )
    first = await confirm.confirm(**args)
    expected = "SUCCEEDED" if outcome == 202 else "FAILED" if outcome == 409 else "UNKNOWN"
    assert first[0][0] == expected
    assert (await confirm.confirm(**args))[0][0] == expected
    if expected == "UNKNOWN":
        assert (await lookup.resolve(card.action_id, backend=backend))[0] == "UNKNOWN"
    backend.write.assert_awaited_once()
    call = backend.write.call_args
    assert call.args == ("adminResendTicket",)
    assert call.kwargs["path"] == {"ticket_id": tid} and call.kwargs["body"] is None
    assert "test-session" not in repr(first) and "email" not in repr(first)
    if outcome == 202:
        assert "xếp hàng" in first[1][0].message
