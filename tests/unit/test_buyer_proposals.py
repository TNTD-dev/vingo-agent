from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from tests.unit.test_buyer_read import (
    EVENT,
    ORDER,
    SEAT,
    SHOW,
    START,
    TICKET,
    URL,
    ZONE,
    availability,
    event_detail,
    order,
    payment,
    seat,
    seatmap,
    showtime,
    ticket,
)
from vingo_agent.actions import execution, registry
from vingo_agent.actions.confirm import result_blocks
from vingo_agent.api.auth import Principal
from vingo_agent.clients.backend import BackendClient
from vingo_agent.config import Settings
from vingo_agent.schemas.chat import Buyer, Role
from vingo_agent.tools import base
from vingo_agent.tools import proposals as p
from vingo_agent.tools.base import TurnDeps

S2 = UUID(int=8)
PAY_URL = "https://gateway.test/pay?signed=mock"


def pending_order(**changes):
    return (
        order() | {"status": "PENDING", "expires_at": (datetime.now(UTC) + timedelta(minutes=9)).isoformat()} | changes
    )


@pytest.fixture
async def ctx(monkeypatch, respx_mock):
    deps = TurnDeps(
        Principal(uuid4(), Role.BUYER, "session"), uuid4(), uuid4(), BackendClient(Settings(backend_url=URL), "session")
    )
    refs = {("SHOWTIME", SHOW), ("ZONE", ZONE), ("SEAT", SEAT), ("SEAT", S2), ("ORDER", ORDER), ("TICKET", TICKET)}
    monkeypatch.setattr(
        base.repositories,
        "has_ref",
        AsyncMock(side_effect=lambda cid, k, v: cid == deps.conversation_id and (k, v) in refs),
    )
    create = AsyncMock(return_value=(uuid4(), 1))
    monkeypatch.setattr(p.pending, "create", create)
    try:
        yield SimpleNamespace(deps=deps, messages=[], create=create, refs=refs)
    finally:
        await deps.backend.aclose()
    assert all(call.request.method == "GET" for call in respx_mock.calls)


def rejected(ctx, result, code):
    assert result["error"] == {"code": code, "retryable": False}
    assert not result["ok"] and not ctx.deps.blocks
    ctx.create.assert_not_awaited()


def stored(ctx, result, operation, payload, display, *, form=None):
    ctx.create.assert_awaited_once()
    (card,) = ctx.deps.blocks
    assert card.form == form and card.display == display
    assert result["data"] == {
        "status": "PROPOSED",
        "action_id": str(card.action_id),
        "expires_at": card.expires_at.isoformat(),
    }
    assert ctx.create.await_args.kwargs == dict(
        conversation_id=ctx.deps.conversation_id,
        actor_id=ctx.deps.principal.user_id,
        turn_id=ctx.deps.turn_id,
        role="BUYER",
        operation=operation,
        payload=payload,
        display=display | ({"form": form} if form else {}),
        expires_at=card.expires_at,
    )
    assert "private" not in repr(card)
    return card


def available(respx_mock, *, kind="STANDING", price=250000, status="ON_SALE"):
    raw = availability()
    zone = raw["zones"][0]
    zone.update(
        kind=kind,
        name="GA" if kind == "STANDING" else "VIP",
        tier={"name": "tier", "price": price} if price is not None else None,
    )
    respx_mock.get(f"{URL}/showtimes/{SHOW}/availability").respond(json=raw | {"status": status})


def showtime_history(items):
    return [
        ModelResponse(parts=[ToolCallPart("list_event_showtimes", {"event_id": str(EVENT)}, "x")]),
        ModelRequest(parts=[ToolReturnPart("list_event_showtimes", {"ok": True, "data": {"items": items}}, "x")]),
    ]


@pytest.mark.parametrize("seated", [False, True], ids=["B04-GA", "B08-VIP"])
async def test_hold_createsOnlyTheSelectedPayload(ctx, respx_mock, seated):
    price = 500000 if seated else 250000
    available(respx_mock, kind="SEATED" if seated else "STANDING", price=price)
    if seated:
        # Hai ghế đã được Ref bởi suggestSeats, ở sau giới hạn 60 ghế của tool đọc.
        seats = [seat(n, seat_id=UUID(int=100 + n)) for n in range(65)] + [seat(11), seat(12, seat_id=S2)]
        respx_mock.get(f"{URL}/showtimes/{SHOW}/seatmap", params={"zone_id": str(ZONE)}).respond(json=seatmap(seats))
    ctx.messages = showtime_history([{"showtime_id": str(SHOW), "starts_at": START}])
    respx_mock.get(f"{URL}/events/{EVENT}").respond(json=event_detail())
    respx_mock.get(URL + "/orders", params={"status": "PENDING"}).respond(json={"data": []})
    result = await p.propose_hold_seats(ctx, SHOW, ZONE, 2, [S2, SEAT] if seated else None)
    body = {"seat_ids": [str(S2), str(SEAT)]} if seated else {"zone_id": str(ZONE), "quantity": 2}
    display = {
        "event": "Demo",
        "showtime": START,
        "zone": "VIP" if seated else "GA",
        "quantity": 2,
        "unit_price": price,
        "total": 2 * price,
        "currency": "VND",
    }
    if seated:
        display["seats"] = ["R1-12", "R1-11"]
    stored(ctx, result, "HOLD_SEATS", {"path": {"showtime_id": str(SHOW)}, "body": body}, display)


@pytest.mark.parametrize(
    "messages,expect_event_read",
    [
        pytest.param([], False, id="empty-history"),
        pytest.param(None, False, id="missing-history"),
        pytest.param([{}], False, id="malformed-message"),
        pytest.param(showtime_history(None), False, id="malformed-items"),
        pytest.param(showtime_history([{"showtime_id": str(SHOW)}]), False, id="missing-start"),
        pytest.param(showtime_history([{"showtime_id": str(SHOW), "starts_at": START}]), True, id="get-event-fails"),
    ],
)
async def test_hold_withoutLabels_preservesSeatAndPriceWithoutCatalogScan(ctx, respx_mock, messages, expect_event_read):
    if messages is None:
        del ctx.messages
    else:
        ctx.messages = messages
    available(respx_mock, kind="SEATED")
    respx_mock.get(f"{URL}/showtimes/{SHOW}/seatmap").respond(json=seatmap([seat(11)]))
    respx_mock.get(URL + "/orders").respond(json={"data": []})
    detail_url = httpx.URL(f"{URL}/events/{EVENT}")
    if expect_event_read:
        respx_mock.get(detail_url).respond(404)

    result = await p.propose_hold_seats(ctx, SHOW, ZONE, 1, [SEAT])
    stored(
        ctx,
        result,
        "HOLD_SEATS",
        {"path": {"showtime_id": str(SHOW)}, "body": {"seat_ids": [str(SEAT)]}},
        {"zone": "VIP", "quantity": 1, "seats": ["R1-11"], "unit_price": 250000, "total": 250000, "currency": "VND"},
    )
    event_requests = [call.request.url for call in respx_mock.calls if "/events" in call.request.url.path]
    assert event_requests == ([detail_url] if expect_event_read else [])


@pytest.mark.parametrize(
    "case,code",
    [
        ("no_ref", "INVALID_REFERENCE"),
        ("foreign_seat", "INVALID_REFERENCE"),
        ("held", "INVALID_REFERENCE"),
        ("foreign_zone", "INVALID_REFERENCE"),
        ("off_sale", "SHOWTIME_NOT_ON_SALE"),
        ("no_price", "INVALID_REFERENCE"),
        ("standing_with_seats", "INVALID_ARGUMENT"),
        ("empty_on_standing", "INVALID_ARGUMENT"),
        ("missing_seats", "INVALID_ARGUMENT"),
        ("extra_seats", "INVALID_ARGUMENT"),
        ("duplicate", "INVALID_ARGUMENT"),
        ("stock", "NOT_ENOUGH_STOCK"),
        ("bool", "INVALID_ARGUMENT"),
        ("zero", "INVALID_ARGUMENT"),
    ],
)
async def test_hold_rejectsInvalidSelection(ctx, respx_mock, case, code):
    seated = case in {"no_ref", "foreign_seat", "held", "missing_seats", "extra_seats", "duplicate"}
    ids = [SEAT] if seated or case == "standing_with_seats" else None
    qty = 1
    zone = ZONE
    if case == "no_ref":
        ctx.refs.remove(("SEAT", SEAT))
    elif case == "foreign_zone":
        zone = uuid4()
        ctx.refs.add(("ZONE", zone))
    elif case == "empty_on_standing":
        ids = []
    elif case == "missing_seats":
        ids = None
    elif case in {"extra_seats", "duplicate"}:
        ids = [SEAT, SEAT if case == "duplicate" else S2]
    elif case in {"stock", "bool", "zero"}:
        qty = {"stock": 4, "bool": True, "zero": 0}[case]
    if case not in {"duplicate", "bool", "zero", "no_ref"}:
        available(
            respx_mock,
            kind="SEATED" if seated else "STANDING",
            price=None if case == "no_price" else 250000,
            status="SCHEDULED" if case == "off_sale" else "ON_SALE",
        )
    if case in {"foreign_seat", "held"}:
        seats = [seat(seat_id=S2)] if case == "foreign_seat" else [seat(status="HELD")]
        respx_mock.get(f"{URL}/showtimes/{SHOW}/seatmap").respond(json=seatmap(seats))
    rejected(ctx, await p.propose_hold_seats(ctx, SHOW, zone, qty, ids), code)


async def test_B21_pendingOrderOnSecondPage_preventsHold(ctx, respx_mock):
    available(respx_mock)
    other = pending_order(showtime=showtime() | {"id": str(uuid4())})
    pages = respx_mock.get(URL + "/orders").mock(
        side_effect=[
            httpx.Response(200, json={"data": [other], "next_cursor": "next"}),
            httpx.Response(200, json={"data": [pending_order()]}),
        ]
    )
    rejected(ctx, await p.propose_hold_seats(ctx, SHOW, ZONE, 2), "ORDER_STATE_INVALID")
    assert [dict(c.request.url.params) for c in pages.calls] == [
        {"status": "PENDING"},
        {"status": "PENDING", "cursor": "next"},
    ]


async def test_hold_allowsExpiredPendingOrderForSameShowtime(ctx, respx_mock):
    available(respx_mock)
    expired = pending_order(expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    other = pending_order(showtime=showtime() | {"id": str(uuid4())})
    respx_mock.get(URL + "/orders").respond(json={"data": [expired, other]})

    result = await p.propose_hold_seats(ctx, SHOW, ZONE, 2)
    assert result["data"]["status"] == "PROPOSED"
    ctx.create.assert_awaited_once()


@pytest.mark.parametrize("operation", ["CREATE_PAYMENT", "CANCEL_ORDER"])
async def test_B10_B13_orderCardsMatchBackendExpiry(ctx, respx_mock, operation):
    raw = pending_order()
    respx_mock.get(f"{URL}/orders/{ORDER}").respond(json=raw)
    payload = {"path": {"order_id": str(ORDER)}}
    display = {"order_code": "O1", "event": "Demo", "total_amount": raw["total_amount"]}
    pay = operation == "CREATE_PAYMENT"
    if pay:
        payload["body"] = {}
        display.update(currency="VND", expires_at=raw["expires_at"])
    tool = p.propose_create_payment if pay else p.propose_cancel_order
    card = stored(ctx, await tool(ctx, ORDER), operation, payload, display, form="BUYER" if pay else None)
    assert card.expires_at == datetime.fromisoformat(raw["expires_at"])


@pytest.mark.parametrize(
    "tool,status,code",
    [
        (p.propose_create_payment, "PENDING", "HOLD_EXPIRED"),
        (p.propose_create_payment, "PAID", "ORDER_STATE_INVALID"),
        (p.propose_cancel_order, "CANCELLED", "ORDER_STATE_INVALID"),
        (p.propose_cancel_order, "PENDING", None),
    ],
)
async def test_orderStateAndExpiredCancelGrace(ctx, respx_mock, tool, status, code):
    now = datetime.now(UTC)
    respx_mock.get(f"{URL}/orders/{ORDER}").respond(
        json=pending_order(status=status, expires_at=(now - timedelta(minutes=1)).isoformat())
    )
    result = await tool(ctx, ORDER)
    if code:
        rejected(ctx, result, code)
    else:
        assert result["ok"]
        assert now + timedelta(minutes=1) <= ctx.deps.blocks[0].expires_at <= datetime.now(UTC) + timedelta(minutes=1)


@pytest.mark.parametrize(
    "status,code",
    [("ISSUED", None), ("USED", "TICKET_USED"), ("VOID", "TICKET_NOT_VALID"), ("missing", "INVALID_REFERENCE")],
)
async def test_B14_resendSearchesBothPeriodsAndCursors(ctx, respx_mock, status, code):
    pages = respx_mock.get(URL + "/tickets").mock(
        side_effect=[
            httpx.Response(200, json={"data": []}),
            httpx.Response(200, json={"data": [], "next_cursor": "next"}),
            httpx.Response(200, json={"data": [] if status == "missing" else [ticket() | {"status": status}]}),
        ]
    )
    result = await p.propose_resend_my_ticket(ctx, TICKET)
    assert [dict(c.request.url.params) for c in pages.calls] == [
        {"when": "upcoming"},
        {"when": "past"},
        {"when": "past", "cursor": "next"},
    ]
    if code:
        rejected(ctx, result, code)
    else:
        stored(
            ctx,
            result,
            "RESEND_MY_TICKET",
            {"path": {"ticket_id": str(TICKET)}},
            {"ticket_code": "T1", "event": "Demo", "zone": "A", "row": "R1", "number": 5},
        )


def test_loadSpecs_registersAllFourBuyerTools():
    registry.load_specs()
    for tool in p.BUYER_PROPOSAL_TOOLS:
        spec = registry.get_spec(tool.__name__.removeprefix("propose_").upper())
        assert (spec.tool_name, spec.role) == (tool.__name__, "BUYER")


@pytest.mark.parametrize(
    "tool,args",
    [
        (p.propose_hold_seats, (SHOW, ZONE, 2)),
        (p.propose_create_payment, (ORDER,)),
        (p.propose_cancel_order, (ORDER,)),
        (p.propose_resend_my_ticket, (TICKET,)),
    ],
)
async def test_buyerRoleAndRefsAreRequiredBeforeReading(ctx, respx_mock, tool, args):
    old = ctx.deps.principal
    ctx.deps.principal = Principal(old.user_id, Role.ADMIN, "session")
    rejected(ctx, await tool(ctx, *args), "FORBIDDEN")
    ctx.deps.principal = old
    ctx.refs.clear()
    rejected(ctx, await tool(ctx, *args), "INVALID_REFERENCE")
    assert not respx_mock.calls


async def test_holdWarnsAboutPriceEvenIfPaymentProposalCannotBeSaved(ctx):
    ctx.create.side_effect = RuntimeError("unavailable")
    spec = registry.get_spec("HOLD_SEATS")
    raw = pending_order(total_amount=600000)
    blocks, notices = await spec.after_success(
        conversation_id=ctx.deps.conversation_id,
        result=spec.project(raw),
        context=({"actor_id": ctx.deps.principal.user_id, "display": {"total": 500000}}, spec.web_blocks(raw)),
    )
    assert blocks == []
    assert notices[0] == {"level": "WARN", "text": "Tổng tiền đã đổi thành 600.000 VND."}


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("total", [500000, 600000])
async def test_B06_B17_F02_holdAndFollowup(ctx, retry, total):
    raw = pending_order(total_amount=total)
    backend = AsyncMock()
    backend.write.side_effect = ([httpx.ReadTimeout("timeout")] if retry else []) + [httpx.Response(201, json=raw)]
    key = uuid4()
    blocks = []
    result = await execution.execute(
        "HOLD_SEATS",
        backend=backend,
        payload={"path": {"showtime_id": str(SHOW)}, "body": {"zone_id": str(ZONE), "quantity": 2}},
        idempotency_key=key,
        web_blocks=blocks,
    )
    assert result == (
        "SUCCEEDED",
        None,
        {"order_id": str(ORDER), "status": "PENDING", "total_amount": total, "expires_at": raw["expires_at"]},
    )
    assert [c.kwargs["idempotency_key"] for c in backend.write.await_args_list] == [key] * (1 + retry)
    spec = registry.get_spec("HOLD_SEATS")
    assert spec.result_refs == {"order_id": "ORDER"}
    (summary,) = blocks
    assert summary.type == "OrderSummary" and summary.total_amount == total and "private" not in repr(summary)
    cards, notices = await spec.after_success(
        conversation_id=ctx.deps.conversation_id,
        result=result[2],
        context=({"actor_id": ctx.deps.principal.user_id, "display": {"total": 500000}}, blocks),
    )
    (card,) = cards
    assert card.operation == "CREATE_PAYMENT" and card.form == "BUYER"
    assert card.display == {
        "order_code": "O1",
        "event": "Demo",
        "total_amount": total,
        "currency": "VND",
        "expires_at": raw["expires_at"],
    }
    assert ctx.create.await_args.kwargs["turn_id"] is None
    assert ctx.create.await_args.kwargs["payload"] == {"path": {"order_id": str(ORDER)}, "body": {}}
    assert card.expires_at == datetime.fromisoformat(raw["expires_at"])
    assert notices == ([] if total == 500000 else [{"level": "WARN", "text": "Tổng tiền đã đổi thành 600.000 VND."}])


@pytest.mark.parametrize("field,value", [("id", "bad"), ("expires_at", None), ("status", 42), ("total_amount", True)])
async def test_holdInvalidSuccessIsUnknown(field, value):
    backend = AsyncMock()
    backend.write.return_value = httpx.Response(201, json=pending_order(**{field: value}))
    assert await execution.execute("HOLD_SEATS", backend=backend, payload={}, idempotency_key=uuid4()) == (
        "UNKNOWN",
        "EXECUTION_UNKNOWN",
        {},
    )


async def test_createPaymentRetriesSameFormAndKeyAndKeepsUrlOnlyInWebBlock():
    raw = payment() | {"payment_url": PAY_URL}
    key = uuid4()
    backend = AsyncMock()
    backend.write.side_effect = [httpx.ReadTimeout("timeout"), httpx.Response(201, json=raw)]
    buyer = Buyer(full_name="Test", email="test@example.test", phone="+84900000000")
    payload = {"path": {"order_id": str(ORDER)}, "body": {}}
    blocks = []
    outcome = await execution.execute(
        "CREATE_PAYMENT", backend=backend, payload=payload, idempotency_key=key, buyer=buyer, web_blocks=blocks
    )
    assert outcome == ("SUCCEEDED", None, {k: raw[k] for k in ("order_id", "status", "expires_at")})
    assert payload == {"path": {"order_id": str(ORDER)}, "body": {}}
    assert [c.kwargs for c in backend.write.await_args_list] == [
        {"path": payload["path"], "body": {"buyer": buyer.model_dump()}, "idempotency_key": key}
    ] * 2
    (link,) = blocks
    assert link.type == "PaymentLink" and link.payment_url == PAY_URL
    assert buyer.email not in repr(blocks)
    assert (
        await execution.execute(
            "CREATE_PAYMENT", backend=backend, payload=payload, idempotency_key=key, lookup_only=True
        )
    )[0] == "UNKNOWN"
    assert backend.write.await_count == 2


@pytest.mark.parametrize("status,success", [("CANCELLED", True), ("PENDING", False)])
async def test_cancelTimeoutReadsOrderOnce(status, success):
    backend = AsyncMock()
    backend.write.side_effect = httpx.ReadTimeout("timeout")
    backend.read.return_value = pending_order(status=status)
    path = {"order_id": str(ORDER)}
    blocks = []
    outcome = await execution.execute(
        "CANCEL_ORDER", backend=backend, payload={"path": path}, idempotency_key=uuid4(), web_blocks=blocks
    )
    assert outcome == (
        ("SUCCEEDED", None, {"order_id": str(ORDER), "status": status})
        if success
        else ("UNKNOWN", "EXECUTION_UNKNOWN", {})
    )
    assert [b.type for b in blocks] == (["OrderSummary"] if success else [])
    backend.read.assert_awaited_once_with("getOrder", path=path)
    backend.write.assert_awaited_once()


@pytest.mark.parametrize("timeout", [False, True])
async def test_F03_resendNeverRetries(timeout):
    backend = AsyncMock()
    backend.write.side_effect = [httpx.ReadTimeout("timeout") if timeout else httpx.Response(202)]
    outcome = await execution.execute(
        "RESEND_MY_TICKET", backend=backend, payload={"path": {"ticket_id": str(TICKET)}}, idempotency_key=uuid4()
    )
    assert outcome == (("UNKNOWN", "EXECUTION_UNKNOWN", {}) if timeout else ("SUCCEEDED", None, {}))
    message = result_blocks(uuid4(), "RESEND_MY_TICKET", outcome)[0].message
    assert ("có thể đã gửi" if timeout else "xếp hàng") in message
    backend.write.assert_awaited_once()
    backend.read.assert_not_awaited()
