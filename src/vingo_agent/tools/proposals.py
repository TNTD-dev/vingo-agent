"""Tool đề xuất (V2.3 mục 4.4): chỉ tạo pending action, không bao giờ ghi nghiệp vụ.

Mỗi tool: kiểm Ref + điều kiện (đọc lại backend nếu cần) → actions.proposals.create(...) → block ConfirmAction
→ trả {"status": "PROPOSED", "action_id", "expires_at"}. Tối đa một đề xuất mỗi turn.
"""

from datetime import UTC, datetime
from functools import wraps
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, AwareDatetime, Field
from pydantic_ai import RunContext

from vingo_agent.actions import proposals as pending
from vingo_agent.actions.proposals import Operation
from vingo_agent.persistence import repositories as repo
from vingo_agent.schemas.blocks import ConfirmAction
from vingo_agent.schemas.chat import Role
from vingo_agent.tools.admin_read import _operations_admin, _operations_read
from vingo_agent.tools.base import ToolError, TurnDeps, fail, ok, require_ref

SeatIds = list[UUID]
SeatIdList = Annotated[list[UUID], Field(min_length=1, max_length=200)]


# ---- Buyer
async def propose_hold_seats(
    ctx: RunContext[TurnDeps],
    showtime_id: UUID,
    seat_ids: SeatIds | None = None,
    zone_id: UUID | None = None,
    quantity: Annotated[int, Field(ge=1, le=20)] | None = None,
) -> dict:
    """Đề xuất giữ vé. Khu ngồi: chỉ seat_ids. Khu đứng: zone_id + quantity. Hạn 5 phút. → holdSeats"""
    raise NotImplementedError


async def propose_create_payment(ctx: RunContext[TurnDeps], order_id: UUID) -> dict:
    """Đề xuất thanh toán đơn PENDING; thẻ có form người mua. Hạn = thời gian giữ ghế còn lại. → createPayment"""
    raise NotImplementedError


async def propose_cancel_order(ctx: RunContext[TurnDeps], order_id: UUID) -> dict:
    """Đề xuất hủy đơn PENDING và nhả ghế. → cancelOrder"""
    raise NotImplementedError


async def propose_resend_my_ticket(ctx: RunContext[TurnDeps], ticket_id: UUID) -> dict:
    """Đề xuất gửi lại vé ISSUED tới email trên đơn. → resendMyTicket"""
    raise NotImplementedError


# ---- Admin
def _admin_proposal(operation: Operation):
    def decorate(tool):
        @wraps(tool)
        async def propose(ctx: RunContext[TurnDeps], *args, **kwargs):
            deps = ctx.deps
            backend_operation = pending.BACKEND_OPERATION[operation]
            try:
                if deps.principal.role != Role.ADMIN:
                    raise ToolError("FORBIDDEN")
                if deps.proposals_created:
                    raise ToolError("LIMIT_EXCEEDED")
                path, body, display = await tool(ctx, *args, **kwargs)
                now = datetime.now(UTC)
                expires_at = now + pending.DEFAULT_TTL
                action_id, version = await pending.create(
                    conversation_id=deps.conversation_id,
                    turn_id=deps.turn_id,
                    actor_id=deps.principal.user_id,
                    role=deps.principal.role,
                    operation=operation,
                    payload={"path": path, "body": body},
                    display=display,
                    expires_at=expires_at,
                )
            except ToolError as error:
                return fail(backend_operation, error)
            deps.proposals_created += 1
            deps.blocks.append(
                ConfirmAction(
                    action_id=action_id, version=version, operation=operation, expires_at=expires_at, display=display
                )
            )
            return ok(
                backend_operation,
                {"status": "PROPOSED", "action_id": str(action_id), "expires_at": expires_at.isoformat()},
                observed_at=now,
            )

        return propose

    return decorate


@_admin_proposal(Operation.CREATE_VENUE)
async def propose_create_venue(
    ctx: RunContext[TurnDeps], name: str, address: str, city: Annotated[str, Field(max_length=100)]
) -> dict:
    """Đề xuất tạo sân với tên, địa chỉ và thành phố. → createVenue"""
    body = {"name": name.strip(), "address": address.strip(), "city": city.strip()}
    if not all(body.values()):
        raise ToolError("INVALID_ARGUMENT")
    return None, body, body


@_admin_proposal(Operation.CREATE_EVENT)
async def propose_create_event(
    ctx: RunContext[TurnDeps],
    title: Annotated[str, Field(min_length=1, max_length=200)],
    category: Literal["FOOTBALL", "CONCERT"],
    description: Annotated[str, Field(max_length=2000)] | None = None,
    max_tickets_per_order: Annotated[int, Field(strict=True, ge=1, le=20)] | None = None,
    seat_layout_id: UUID | None = None,
    home_team: str | None = None,
    away_team: str | None = None,
    league: str | None = None,
    round: str | None = None,
    poster_mobile_url: AnyHttpUrl | None = None,
    poster_desktop_url: AnyHttpUrl | None = None,
) -> dict:
    """Đề xuất tạo sự kiện DRAFT. Poster dùng URL HTTP(S) do Admin cung cấp. → createEvent"""
    if not title.strip():
        raise ToolError("INVALID_ARGUMENT")
    if seat_layout_id is not None:
        await require_ref(ctx.deps, "LAYOUT", seat_layout_id)
    fields = {
        "title": title.strip(),
        "category": category,
        "description": description,
        "max_tickets_per_order": max_tickets_per_order,
        "seat_layout_id": str(seat_layout_id) if seat_layout_id is not None else None,
        "home_team": home_team,
        "away_team": away_team,
        "league": league,
        "round": round,
        "poster_mobile_url": str(poster_mobile_url) if poster_mobile_url is not None else None,
        "poster_desktop_url": str(poster_desktop_url) if poster_desktop_url is not None else None,
    }
    body = {key: value for key, value in fields.items() if value is not None}
    return None, body, body


@_admin_proposal(Operation.CREATE_SHOWTIME)
async def propose_create_showtime(
    ctx: RunContext[TurnDeps],
    event_id: UUID,
    layout_id: UUID,
    starts_at: AwareDatetime,
    ends_at: AwareDatetime,
    sale_opens_at: AwareDatetime,
    sale_closes_at: AwareDatetime,
) -> dict:
    """Đề xuất tạo suất; cần sale_opens_at < sale_closes_at ≤ starts_at < ends_at, layout PUBLISHED. → createShowtime"""
    times = (sale_opens_at, sale_closes_at, starts_at, ends_at)
    if any(value.utcoffset() is None for value in times):
        raise ToolError("INVALID_ARGUMENT")
    opens, closes, starts, ends = (value.astimezone(UTC) for value in times)
    if not opens < closes <= starts < ends:
        raise ToolError("INVALID_ARGUMENT")
    await require_ref(ctx.deps, "EVENT", event_id)
    await require_ref(ctx.deps, "LAYOUT", layout_id)
    layout = await ctx.deps.backend.read("getSeatLayout", path={"layout_id": str(layout_id)})
    if layout.get("id") != str(layout_id):
        raise ToolError("INVALID_TOOL_RESULT")
    # Backend hiện chỉ trả phiên bản đã công bố; nháp nằm trong phiên import.
    if layout.get("status", "PUBLISHED") != "PUBLISHED":
        raise ToolError("VALIDATION_ERROR")
    path = {"event_id": str(event_id)}
    body = {
        "seat_layout_id": str(layout_id),
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "sale_opens_at": sale_opens_at.isoformat(),
        "sale_closes_at": sale_closes_at.isoformat(),
    }
    return path, body, {**path, **body}


@_admin_proposal(Operation.PUBLISH_EVENT)
async def propose_publish_event(ctx: RunContext[TurnDeps], event_id: UUID) -> dict:
    """Đề xuất công bố sự kiện DRAFT. → publishEvent"""
    await require_ref(ctx.deps, "EVENT", event_id)
    path = {"event_id": str(event_id)}
    event = await ctx.deps.backend.read("getAdminEvent", path=path)
    status = event.get("status")
    if status not in ("DRAFT", "PUBLISHED", "CANCELLED"):
        raise ToolError("INVALID_TOOL_RESULT")
    if status != "DRAFT":
        raise ToolError("EVENT_LOCKED")
    title = event.get("title")
    display = {**path, "title": title} if isinstance(title, str) else path
    return path, None, display


async def _seat_payload(deps: TurnDeps, showtime_id: UUID, seat_ids: SeatIdList) -> tuple[dict, dict]:
    if not 1 <= len(seat_ids) <= 200 or len(set(seat_ids)) != len(seat_ids):
        raise ToolError("INVALID_ARGUMENT")
    await require_ref(deps, "SHOWTIME", showtime_id)
    for seat_id in seat_ids:
        await require_ref(deps, "SEAT", seat_id)
    return {"showtime_id": str(showtime_id)}, {"seat_ids": [str(seat_id) for seat_id in seat_ids]}


@_admin_proposal(Operation.BLOCK_SEATS)
async def propose_block_seats(
    ctx: RunContext[TurnDeps],
    showtime_id: UUID,
    seat_ids: SeatIdList,
    reason: Annotated[str, Field(min_length=1, max_length=1000)],
) -> dict:
    """Đề xuất khóa ghế (cần lý do). → blockSeats"""
    if not reason.strip():
        raise ToolError("INVALID_ARGUMENT")
    path, body = await _seat_payload(ctx.deps, showtime_id, seat_ids)
    body["reason"] = reason.strip()
    return path, body, {**path, **body}


@_admin_proposal(Operation.UNBLOCK_SEATS)
async def propose_unblock_seats(ctx: RunContext[TurnDeps], showtime_id: UUID, seat_ids: SeatIdList) -> dict:
    """Đề xuất mở khóa ghế. → unblockSeats"""
    path, body = await _seat_payload(ctx.deps, showtime_id, seat_ids)
    return path, body, {**path, **body}


async def _known_ticket_code(ctx, ticket_id):
    # Chỉ lấy mã từ kết quả adminListTickets đã lọc của chính hội thoại, không từ lời user/model.
    if ticket_id in ctx.deps.admin_ticket_codes:
        return ctx.deps.admin_ticket_codes[ticket_id]
    for message in reversed(await repo.get_messages(ctx.deps.conversation_id)):
        if message["kind"] != "TOOL_RESULT":
            continue
        result = message["content"].get("result", {})
        if result.get("ok") is not True or result.get("meta", {}).get("operation") != "adminListTickets":
            continue
        for item in result.get("data", {}).get("items", []):
            if item.get("ticket_id") == str(ticket_id):
                return item["code"]
    raise ToolError("INVALID_REFERENCE")


async def propose_admin_resend_ticket(ctx: RunContext[TurnDeps], ticket_id: UUID) -> dict:
    """Đề xuất gửi lại vé ISSUED tới email đã lưu, TTL 10 phút; chỉ gửi sau xác nhận. → adminResendTicket"""
    operation = "adminResendTicket"
    try:
        _operations_admin(ctx)
        if ctx.deps.proposals_created:
            raise ToolError("PROPOSAL_ALREADY_CREATED")
        if not isinstance(ticket_id, UUID):
            raise ToolError("INVALID_ARGUMENT")
        await require_ref(ctx.deps, "TICKET", ticket_id)
        code = await _known_ticket_code(ctx, ticket_id)
        query = {"code": code, "limit": 1}
        ticket_result = await _operations_read(
            ctx, "adminListTickets", query=query, requested={**query, "ticket_id": str(ticket_id)}
        )
        ticket = ticket_result["data"]["items"][0]
        if ticket["status"] != "ISSUED":
            raise ToolError("TICKET_USED" if ticket["status"] == "USED" else "TICKET_NOT_VALID")
        orders = (await _operations_read(ctx, "adminListOrders", query={"q": ticket["order_code"], "limit": 1}))[
            "data"
        ]["items"]
        if len(orders) != 1 or orders[0]["code"].upper() != ticket["order_code"].upper():
            raise ToolError("INVALID_TOOL_RESULT")
        if orders[0]["status"] != "PAID":
            raise ToolError("ORDER_STATE_INVALID")
        now = datetime.now(UTC)
        expires_at = now + pending.DEFAULT_TTL
        display = {"ticket_code": ticket["code"], "message": "Yêu cầu gửi lại vé tới email trên đơn."}
        try:
            action_id, version = await pending.create(
                conversation_id=ctx.deps.conversation_id,
                turn_id=ctx.deps.turn_id,
                actor_id=ctx.deps.principal.user_id,
                role=ctx.deps.principal.role,
                operation=pending.Operation.ADMIN_RESEND_TICKET,
                payload={"path": {"ticket_id": str(ticket_id)}, "body": None},
                display=display,
                expires_at=expires_at,
            )
        except NotImplementedError:
            # KAN-193 chưa sẵn sàng: không trả đề xuất giả và không gửi email.
            raise ToolError("BACKEND_UNAVAILABLE", retryable=True) from None
        ctx.deps.blocks.append(
            ConfirmAction(
                action_id=action_id,
                version=version,
                operation=pending.Operation.ADMIN_RESEND_TICKET,
                expires_at=expires_at,
                display=display,
            )
        )
        # guarded_tool là nơi duy nhất tăng proposals_created sau khi tool thành công.
        return ok(
            operation,
            {"status": "PROPOSED", "action_id": str(action_id), "expires_at": expires_at.isoformat()},
            observed_at=now,
        )
    except ToolError as error:
        return fail(operation, error)


BUYER_PROPOSAL_TOOLS = (
    propose_hold_seats,
    propose_create_payment,
    propose_cancel_order,
    propose_resend_my_ticket,
)
ADMIN_PROPOSAL_TOOLS = (
    propose_create_venue,
    propose_create_event,
    propose_create_showtime,
    propose_publish_event,
    propose_block_seats,
    propose_unblock_seats,
    propose_admin_resend_ticket,
)
