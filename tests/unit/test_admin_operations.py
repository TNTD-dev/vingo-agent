"""KAN-205: HTTP/Ref, lọc sâu, NEEDS_REVIEW, pending và resend không retry; không dùng mạng thật."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from vingo_agent.actions import proposals as pending
from vingo_agent.actions.execution import execute_admin_resend
from vingo_agent.actions.registry import LookupMode, get_spec
from vingo_agent.agents.registry import tools_for
from vingo_agent.api.auth import Principal
from vingo_agent.clients.backend import BackendClient
from vingo_agent.config import Settings
from vingo_agent.guardrails.tool import guarded_tool
from vingo_agent.persistence import repositories as repo
from vingo_agent.schemas.chat import Role
from vingo_agent.tools import admin_read as ops
from vingo_agent.tools import proposals as proposal_tools
from vingo_agent.tools.base import ToolError, TurnDeps
from vingo_agent.tools.blocks import build_block
from vingo_agent.tools.projections import project

AT = "2026-11-26T09:00:00+00:00"
OID, TID, SID, EID = (str(uuid4()) for _ in range(4))


def order(status="PAID"):
    return {
        "id": OID,
        "code": "ORDER123",
        "status": status,
        "total_amount": 200_000,
        "created_at": AT,
        "event": {"id": EID, "title": "Demo"},
        "showtime": {"id": SID, "starts_at": AT},
        "buyer": {"email": "secret@example.test", "phone": "0901234567"},
        "items": [{"zone": "VIP", "row": "A", "number": 1, "unit_price": 200_000, "quantity": 1}],
        "payments": [
            {"status": "SUCCEEDED", "amount": 200_000, "last_response_code": "00", "payment_url": "https://secret.test"}
        ],
        "history": [{"status": "PAID", "at": AT, "actor": "secret@example.test"}],
    }


def ticket(status="ISSUED"):
    return {
        "id": TID,
        "code": "TICKET123",
        "status": status,
        "order_code": "ORDER123",
        "event": {"id": EID, "title": "Demo"},
        "showtime": {"id": SID, "starts_at": AT},
        "zone": "VIP",
        "row": "A",
        "number": 1,
        "qr_token": "secret-qr",
    }


def page(*rows, cursor=None):
    return {"data": list(rows), "next_cursor": cursor}


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr("vingo_agent.tools.base.repositories.has_ref", AsyncMock(return_value=True))
    monkeypatch.setattr("vingo_agent.tools.base.repositories.add_refs", AsyncMock())
    monkeypatch.setattr(repo, "get_messages", AsyncMock(return_value=[]))
    return SimpleNamespace(deps=TurnDeps(Principal(uuid4(), Role.ADMIN, "session"), uuid4(), uuid4(), AsyncMock()))


@respx.mock
async def test_A15_orders_exactQueryThroughClient_doesNotEchoContact(ctx):
    route = respx.get("http://backend/api/v1/admin/orders").respond(200, json=page(order(), cursor="next123"))
    ctx.deps.backend = BackendClient(
        Settings(backend_url="http://backend/api/v1", agent_to_backend_key="key"), "session"
    )
    try:
        result = await ops.admin_list_orders(ctx, q="  secret@example.test  ")
    finally:
        await ctx.deps.backend.aclose()
    assert result["ok"] is True and result["data"]["next_cursor"] == "next123"
    assert "secret" not in repr(result) and "0901234567" not in repr(result)
    request = route.calls[0].request
    assert dict(request.url.params) == {"q": "secret@example.test", "limit": "10"}
    assert request.headers["Authorization"] == "Bearer session"
    assert request.headers["X-Vingo-Agent-Key"] == "key"
    block = build_block("adminListOrders", result["data"], role="ADMIN")
    assert block.kind == "ORDERS"


async def test_orderDetail_dropsPaymentUrlAndHistoryActor_withoutMutatingInput(ctx):
    raw = order()
    before = deepcopy(raw)
    ctx.deps.backend.read.return_value = raw
    result = await ops.admin_get_order(ctx, UUID(OID))
    assert result["ok"] is True and raw == before
    assert result["data"]["history"] == [{"status": "PAID", "at": AT}]
    assert result["data"]["payments"] == [{"status": "SUCCEEDED", "amount": 200_000, "last_response_code": "00"}]
    assert "secret" not in repr(result)
    assert build_block("adminGetOrder", result["data"], role="ADMIN").total_amount == 200_000


@pytest.mark.parametrize("detail", [True, False])
async def test_needsReview_returnsOnlyIdentityStatusAndPortalMessage(ctx, detail):
    raw = order("NEEDS_REVIEW")
    ctx.deps.backend.read.return_value = raw if detail else page(raw)
    result = await ops.admin_get_order(ctx, UUID(OID)) if detail else await ops.admin_list_orders(ctx)
    data = result["data"] if detail else result["data"]["items"][0]
    assert set(data) == {"order_id", "code", "status", "message"}
    assert "Admin Portal" in data["message"]
    block = build_block("adminGetOrder" if detail else "adminListOrders", result["data"], role="ADMIN")
    assert block.kind == "ORDERS" and "secret" not in repr(block)


async def test_tickets_recordRefsAndTurnLocalCode_onlyAfterProjection(ctx):
    ctx.deps.backend.read.return_value = page(ticket())
    result = await ops.admin_list_tickets(ctx, showtime_id=UUID(SID))
    assert result["ok"] is True and "secret-qr" not in repr(result)
    assert result["data"]["items"][0]["number"] == "1"
    assert ctx.deps.admin_ticket_codes == {UUID(TID): "TICKET123"}
    repo.add_refs.assert_awaited_once_with(ctx.deps.conversation_id, "TICKET", [UUID(TID)])


async def test_wrongTicketId_neverCreatesPendingOrRecordsWrongRef(ctx, monkeypatch):
    ctx.deps.admin_ticket_codes[UUID(TID)] = "TICKET123"
    raw = ticket()
    raw["id"] = str(uuid4())
    ctx.deps.backend.read.return_value = page(raw)
    create = AsyncMock()
    monkeypatch.setattr(pending, "create", create)
    result = await proposal_tools.propose_admin_resend_ticket(ctx, UUID(TID))
    assert result["error"]["code"] == "INVALID_TOOL_RESULT"
    create.assert_not_called()


@pytest.mark.parametrize("q", ["ab", " " * 4, "x" * 255])
async def test_invalidOrderQuery_neverCallsBackend(ctx, q):
    assert (await ops.admin_list_orders(ctx, q=q))["error"]["code"] == "INVALID_ARGUMENT"
    ctx.deps.backend.read.assert_not_called()


@pytest.mark.parametrize("kwargs", [{}, {"limit": 21}, {"limit": True}, {"code": ""}, {"status": "OTHER"}])
async def test_invalidTicketFilter_neverCallsBackend(ctx, kwargs):
    assert (await ops.admin_list_tickets(ctx, **kwargs))["error"]["code"] == "INVALID_ARGUMENT"
    ctx.deps.backend.read.assert_not_called()


async def test_missingRef_blocksHttp(ctx, monkeypatch):
    monkeypatch.setattr("vingo_agent.tools.base.repositories.has_ref", AsyncMock(return_value=False))
    assert (await ops.admin_get_order(ctx, UUID(OID)))["error"]["code"] == "INVALID_REFERENCE"
    ctx.deps.backend.read.assert_not_called()


@pytest.mark.parametrize(
    "name,args",
    [
        ("admin_list_orders", ()),
        ("admin_get_order", (UUID(OID),)),
        ("admin_list_tickets", ()),
        ("list_showtime_checkins", (UUID(SID),)),
        ("list_audit_logs", ()),
        ("propose_admin_resend_ticket", (UUID(TID),)),
    ],
)
async def test_buyerDirectCall_isForbiddenBeforeHttp(ctx, name, args):
    ctx.deps.principal = Principal(uuid4(), Role.BUYER, "session")
    assert (await tools_for(Role.ADMIN)[name](ctx, *args))["error"]["code"] == "FORBIDDEN"
    ctx.deps.backend.read.assert_not_called()


async def test_ticketOrderFilter_rechecksOrderAndRejectsTicketFromAnotherOrder(ctx):
    wrong = ticket()
    wrong["order_code"] = "OTHER_ORDER"
    ctx.deps.backend.read.side_effect = [order(), page(wrong)]
    result = await ops.admin_list_tickets(ctx, order_id=UUID(OID))
    assert result["error"]["code"] == "INVALID_TOOL_RESULT"
    assert ctx.deps.admin_ticket_codes == {}


async def test_backendError_isReturnedAsSafeEnvelope(ctx):
    ctx.deps.backend.read.side_effect = ToolError("BACKEND_UNAVAILABLE", retryable=True)
    assert (await ops.admin_list_orders(ctx))["error"] == {"code": "BACKEND_UNAVAILABLE", "retryable": True}


async def test_checkins_mapScannedAtAndAcceptInvalidQrWithoutTicketCode(ctx):
    ctx.deps.backend.read.return_value = page(
        {"result": "INVALID_QR", "ticket_code": None, "scanned_at": AT, "qr_token": "secret-qr"}
    )
    result = await ops.list_showtime_checkins(ctx, UUID(SID))
    assert result["data"]["items"] == [{"ticket_code": None, "result": "INVALID_QR", "at": AT}]
    assert ctx.deps.backend.read.call_args.kwargs["path"] == {"showtime_id": SID}
    assert build_block("listShowtimeCheckins", result["data"], role="ADMIN").kind == "CHECKINS"


async def test_audit_dropsSnapshotsAndRedactsContactInActorName(ctx):
    ctx.deps.backend.read.return_value = page(
        {
            "action": "RESEND_TICKET",
            "entity": "ticket",
            "entity_id": TID,
            "actor_name": "secret@example.test",
            "via_agent": True,
            "at": AT,
            "before": {"email": "secret@example.test"},
            "after": {"qr_token": "secret-qr"},
        }
    )
    result = await ops.list_audit_logs(ctx, entity="ticket", entity_id=UUID(TID))
    row = result["data"]["items"][0]
    assert row["via_agent"] is True and row["actor_name"] == "[đã ẩn]"
    assert "secret" not in repr(result) and "before" not in row and "after" not in row
    assert build_block("listAuditLogs", result["data"], role="ADMIN").kind == "AUDIT_LOGS"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"entity_id": UUID(TID)},
        {"entity": "user", "entity_id": UUID(TID)},
        {"time_from": datetime(2026, 11, 1)},
        {"time_from": datetime(2026, 11, 2, tzinfo=UTC), "time_to": datetime(2026, 11, 1, tzinfo=UTC)},
    ],
)
async def test_invalidAuditRefOrWindow_neverCallsBackend(ctx, kwargs):
    assert (await ops.list_audit_logs(ctx, **kwargs))["error"]["code"] == "INVALID_ARGUMENT"
    ctx.deps.backend.read.assert_not_called()


@pytest.mark.parametrize("field,value", [("status", "ALIEN"), ("total_amount", True), ("id", "not-a-uuid")])
async def test_invalidOrderSchema_isRejectedBeforeRefAndBlock(ctx, field, value):
    raw = order()
    raw[field] = value
    ctx.deps.backend.read.return_value = raw
    assert (await ops.admin_get_order(ctx, UUID(OID)))["error"]["code"] == "INVALID_TOOL_RESULT"
    assert not ctx.deps.blocks


def test_pages_doNotSilentlyDropRowsOrReturnInvalidCursors():
    for raw in (page(order(), order()), page(order(), cursor="https://secret.test")):
        with pytest.raises(ToolError, match="INVALID_TOOL_RESULT"):
            project("adminListOrders", raw, role="ADMIN", requested={"limit": 1})


def test_emptyTables_haveStableColumns_andBuyerCannotBuildThem():
    for operation in ("adminListOrders", "adminListTickets", "listShowtimeCheckins", "listAuditLogs"):
        block = build_block(operation, {"items": []}, role="ADMIN")
        assert block.columns and block.rows == []
        assert build_block(operation, {"items": []}, role="BUYER") is None


async def _prepare_resend(ctx, monkeypatch, *, ticket_status="ISSUED", order_status="PAID"):
    ctx.deps.admin_ticket_codes[UUID(TID)] = "TICKET123"
    ctx.deps.backend.read.side_effect = [page(ticket(ticket_status)), page(order(order_status))]
    create = AsyncMock(return_value=(uuid4(), 1))
    monkeypatch.setattr(pending, "create", create)
    return create


async def test_A09_resend_onlyCreatesPendingWithFixedPayload_noPost(ctx, monkeypatch):
    create = await _prepare_resend(ctx, monkeypatch)
    result = await proposal_tools.propose_admin_resend_ticket(ctx, UUID(TID))
    assert result["ok"] is True and result["data"]["status"] == "PROPOSED"
    kwargs = create.call_args.kwargs
    assert kwargs["payload"] == {"path": {"ticket_id": TID}, "body": None}
    assert kwargs["operation"] == pending.Operation.ADMIN_RESEND_TICKET
    assert 599 <= (kwargs["expires_at"] - datetime.now(UTC)).total_seconds() <= 600
    assert ctx.deps.blocks[0].type == "ConfirmAction" and "secret" not in repr(ctx.deps.blocks)
    ctx.deps.backend.write.assert_not_called()


@pytest.mark.parametrize("status,code", [("USED", "TICKET_USED"), ("VOID", "TICKET_NOT_VALID")])
async def test_resend_invalidTicket_neverCreatesPending(ctx, monkeypatch, status, code):
    create = await _prepare_resend(ctx, monkeypatch, ticket_status=status)
    assert (await proposal_tools.propose_admin_resend_ticket(ctx, UUID(TID)))["error"]["code"] == code
    create.assert_not_called()
    ctx.deps.backend.write.assert_not_called()


async def test_resend_needsReview_isBlockedBeforePending(ctx, monkeypatch):
    create = await _prepare_resend(ctx, monkeypatch, order_status="NEEDS_REVIEW")
    assert (await proposal_tools.propose_admin_resend_ticket(ctx, UUID(TID)))["error"]["code"] == "ORDER_STATE_INVALID"
    create.assert_not_called()


async def test_resend_platformStub_doesNotPretendProposalSucceeded(ctx, monkeypatch):
    create = await _prepare_resend(ctx, monkeypatch)
    create.side_effect = NotImplementedError
    result = await proposal_tools.propose_admin_resend_ticket(ctx, UUID(TID))
    assert result["error"]["code"] == "BACKEND_UNAVAILABLE" and not ctx.deps.blocks
    ctx.deps.backend.write.assert_not_called()


async def test_resend_canRecoverTicketCodeFromSafeHistory(ctx, monkeypatch):
    await _prepare_resend(ctx, monkeypatch)
    ctx.deps.admin_ticket_codes.clear()
    repo.get_messages.return_value = [
        {
            "kind": "TOOL_RESULT",
            "content": {
                "result": {
                    "ok": True,
                    "meta": {"operation": "adminListTickets"},
                    "data": {"items": [{"ticket_id": TID, "code": "TICKET123"}]},
                }
            },
        }
    ]
    assert (await proposal_tools.propose_admin_resend_ticket(ctx, UUID(TID)))["ok"] is True


@pytest.mark.parametrize(
    "name",
    [
        "admin_list_orders",
        "admin_get_order",
        "admin_list_tickets",
        "list_showtime_checkins",
        "list_audit_logs",
        "propose_admin_resend_ticket",
    ],
)
def test_adminTools_areAbsentFromBuyer_andRespectKillSwitch(name):
    assert name in tools_for(Role.ADMIN) and name not in tools_for(Role.BUYER)
    assert name not in tools_for(Role.ADMIN, frozenset({name}))


async def test_guardedProposal_countsOnceAndRejectsSecondProposal(ctx, monkeypatch):
    create = await _prepare_resend(ctx, monkeypatch)
    ctx.deps.backend.read.side_effect = None
    ctx.deps.backend.read.side_effect = lambda op, **kwargs: (
        page(ticket()) if op == "adminListTickets" else page(order())
    )
    monkeypatch.setattr("vingo_agent.guardrails.tool.denied", lambda *args: None)

    def respond(messages, info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(
                parts=[
                    ToolCallPart("propose_admin_resend_ticket", {"ticket_id": TID}, "first"),
                    ToolCallPart("propose_admin_resend_ticket", {"ticket_id": TID}, "second"),
                ]
            )
        return ModelResponse(parts=[TextPart("Vui lòng xem thẻ xác nhận.")])

    tool = guarded_tool("propose_admin_resend_ticket", proposal_tools.propose_admin_resend_ticket)
    result = await Agent(FunctionModel(respond), deps_type=TurnDeps, tools=[tool]).run("gửi lại vé", deps=ctx.deps)
    returns = [p.content for m in result.new_messages() for p in m.parts if isinstance(p, ToolReturnPart)]
    assert returns[0]["ok"] is True
    assert returns[1]["error"]["code"] == "PROPOSAL_ALREADY_CREATED"
    assert ctx.deps.proposals_created == 1 and create.await_count == 1
    ctx.deps.backend.write.assert_not_called()


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (httpx.Response(202), ("SUCCEEDED", None, {})),
        (httpx.Response(409, json={"code": "TICKET_USED"}), ("FAILED", "TICKET_USED", {})),
        (httpx.Response(409, json={"code": "secret@example.test"}), ("FAILED", "VALIDATION_ERROR", {})),
        (httpx.Response(503), ("UNKNOWN", "EXECUTION_UNKNOWN", {})),
        (httpx.ReadTimeout("secret"), ("UNKNOWN", "EXECUTION_UNKNOWN", {})),
        (NotImplementedError(), ("FAILED", "BACKEND_UNAVAILABLE", {})),
    ],
)
async def test_F03_resendHandler_sendsExactlyOnce_andNeverRetriesUnknown(outcome, expected):
    backend = AsyncMock()
    if isinstance(outcome, Exception):
        backend.write.side_effect = outcome
    else:
        backend.write.return_value = outcome
    result = await execute_admin_resend(backend=backend, payload={"path": {"ticket_id": TID}, "body": None})
    assert result == expected
    backend.write.assert_awaited_once_with("adminResendTicket", path={"ticket_id": TID}, body=None)
    assert get_spec(pending.Operation.ADMIN_RESEND_TICKET).lookup_mode == LookupMode.NONE
    assert not get_spec(pending.Operation.ADMIN_RESEND_TICKET).safe_retry


async def test_resendHandler_rejectsInjectedPayload_withoutCallingBackend():
    backend = AsyncMock()
    result = await execute_admin_resend(
        backend=backend, payload={"path": {"ticket_id": TID}, "body": {"email": "other@example.test"}}
    )
    assert result == ("FAILED", "INVALID_ARGUMENT", {})
    backend.write.assert_not_called()


def test_operations_tools_areDefinedInDesignatedModules():
    for name in (
        "admin_list_orders",
        "admin_get_order",
        "admin_list_tickets",
        "list_showtime_checkins",
        "list_audit_logs",
    ):
        assert tools_for(Role.ADMIN)[name].__module__ == "vingo_agent.tools.admin_read"
    assert tools_for(Role.ADMIN)["propose_admin_resend_ticket"].__module__ == "vingo_agent.tools.proposals"


def test_projection_unrelatedOperation_keepsFieldSpecDispatch(monkeypatch):
    from vingo_agent.guardrails.result import FieldSpec
    from vingo_agent.tools.projections import SPECS, project

    monkeypatch.setitem(SPECS, "catalog_example", {"id": FieldSpec(str, match="id")})
    assert project("catalog_example", {"id": "one", "email": "secret"}, role="ADMIN", requested={"id": "one"}) == (
        {"id": "one"},
        False,
    )
    with pytest.raises(ToolError, match="INVALID_TOOL_RESULT"):
        project("catalog_example", {"id": "other"}, role="ADMIN", requested={"id": "one"})
