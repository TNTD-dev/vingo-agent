"""10 tool đọc của Admin (V2.3 mục 4.3). `get_seatmap` dùng chung tên với Buyer nhưng projection khác.

Chuỗi lấy ID: get_admin_event → layout_id → get_seat_layout → zone_id → get_seatmap → seat_id.
"""

import json
import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic_ai import RunContext

from vingo_agent.guardrails.result import MAX_RESULT_BYTES
from vingo_agent.schemas.chat import Role
from vingo_agent.tools.base import ToolError, TurnDeps, fail, ok, record_refs, require_ref
from vingo_agent.tools.buyer_read import OrderStatus, PageLimit, TicketStatus
from vingo_agent.tools.projections import project

Metric = Literal["tickets_sold", "revenue", "fill_rate", "checkins"]
EventStatus = Literal["DRAFT", "PUBLISHED", "CANCELLED"]


def _operations_admin(ctx):
    if ctx.deps.principal.role != Role.ADMIN:
        raise ToolError("FORBIDDEN")


def _operations_page(cursor, limit):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ToolError("INVALID_ARGUMENT")
    query = {"limit": limit}
    if cursor is not None:
        if type(cursor) is not str or not cursor or len(cursor) > 2048:
            raise ToolError("INVALID_ARGUMENT")
        query["cursor"] = cursor
    return query


def _operations_choice(query, name, value, choices):
    if value is not None:
        if type(value) is not str or value not in choices:
            raise ToolError("INVALID_ARGUMENT")
        query[name] = value


async def _operations_ref(ctx, query, key, kind, value):
    if value is not None:
        if not isinstance(value, UUID):
            raise ToolError("INVALID_ARGUMENT")
        await require_ref(ctx.deps, kind, value)
        query[key] = str(value)


async def _operations_read(ctx, operation, *, query=None, path=None, requested=None):
    raw = await ctx.deps.backend.read(operation, query=query, path=path)
    data, truncated = project(
        operation,
        raw,
        role=ctx.deps.principal.role,
        requested=requested if requested is not None else query or path or {},
    )
    if operation in ("adminListOrders", "adminGetOrder", "adminListTickets"):
        kind, key = ("TICKET", "ticket_id") if operation == "adminListTickets" else ("ORDER", "order_id")
        rows = data["items"] if "items" in data and operation != "adminGetOrder" else [data]
        await record_refs(ctx.deps, kind, [UUID(row[key]) for row in rows])
        if operation == "adminListTickets":
            ctx.deps.admin_ticket_codes.update({UUID(row["ticket_id"]): row["code"] for row in rows})
    return ok(operation, data, truncated=truncated, observed_at=datetime.now(UTC))


async def admin_list_orders(
    ctx: RunContext[TurnDeps],
    status: OrderStatus | None = None,
    event_id: UUID | None = None,
    showtime_id: UUID | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Tra đơn bằng mã/email/SĐT khớp chính xác; không trả contact. NEEDS_REVIEW hướng Portal. → adminListOrders"""
    operation = "adminListOrders"
    try:
        _operations_admin(ctx)
        query = _operations_page(cursor, limit)
        _operations_choice(query, "status", status, ORDER_STATUSES)
        await _operations_ref(ctx, query, "event_id", "EVENT", event_id)
        await _operations_ref(ctx, query, "showtime_id", "SHOWTIME", showtime_id)
        if q is not None:
            if type(q) is not str or not 3 <= len(q.strip()) <= 254:
                raise ToolError("INVALID_ARGUMENT")
            query["q"] = q.strip()
        return await _operations_read(ctx, operation, query=query)
    except ToolError as error:
        return fail(operation, error)


async def admin_get_order(ctx: RunContext[TurnDeps], order_id: UUID) -> dict:
    """Đơn đã chọn, payments và lịch sử an toàn; NEEDS_REVIEW chỉ báo trạng thái/Portal. → adminGetOrder"""
    operation = "adminGetOrder"
    try:
        _operations_admin(ctx)
        path = {}
        await _operations_ref(ctx, path, "order_id", "ORDER", order_id)
        return await _operations_read(ctx, operation, path=path)
    except ToolError as error:
        return fail(operation, error)


async def admin_list_tickets(
    ctx: RunContext[TurnDeps],
    showtime_id: UUID | None = None,
    order_id: UUID | None = None,
    code: str | None = None,
    status: TicketStatus | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Tra vé với ít nhất một bộ lọc nghiệp vụ, không trả QR/contact. → adminListTickets"""
    operation = "adminListTickets"
    try:
        _operations_admin(ctx)
        query = _operations_page(cursor, limit)
        _operations_choice(query, "status", status, TICKET_STATUSES)
        await _operations_ref(ctx, query, "showtime_id", "SHOWTIME", showtime_id)
        await _operations_ref(ctx, query, "order_id", "ORDER", order_id)
        if code is not None:
            if type(code) is not str or not code.strip() or len(code.strip()) > 64:
                raise ToolError("INVALID_ARGUMENT")
            query["code"] = code.strip()
        if not any(key in query for key in ("showtime_id", "order_id", "code", "status")):
            raise ToolError("INVALID_ARGUMENT")
        requested = dict(query)
        if order_id is not None:
            order = (await _operations_read(ctx, "adminGetOrder", path={"order_id": str(order_id)}))["data"]
            if order["status"] == "NEEDS_REVIEW":
                raise ToolError("ORDER_STATE_INVALID")
            requested["order_code"] = order["code"]
        return await _operations_read(ctx, operation, query=query, requested=requested)
    except ToolError as error:
        return fail(operation, error)


async def list_showtime_checkins(
    ctx: RunContext[TurnDeps],
    showtime_id: UUID,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Các lần quét của suất đã chọn, gồm kết quả và thời gian; không trả QR. → listShowtimeCheckins"""
    operation = "listShowtimeCheckins"
    try:
        _operations_admin(ctx)
        path = {}
        await _operations_ref(ctx, path, "showtime_id", "SHOWTIME", showtime_id)
        return await _operations_read(ctx, operation, path=path, query=_operations_page(cursor, limit))
    except ToolError as error:
        return fail(operation, error)


async def list_audit_logs(
    ctx: RunContext[TurnDeps],
    entity: str | None = None,
    entity_id: UUID | None = None,
    action: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Nhật ký thao tác an toàn, không before/after; entity_id cần entity và Ref đúng loại. → listAuditLogs"""
    operation = "listAuditLogs"
    try:
        _operations_admin(ctx)
        query = _operations_page(cursor, limit)
        for key, value in (("entity", entity), ("action", action)):
            if value is not None:
                if type(value) is not str or not value.strip() or len(value) > 100:
                    raise ToolError("INVALID_ARGUMENT")
                query[key] = value.strip()
        if entity_id is not None:
            kind = ENTITY_TYPES.get(query.get("entity"))
            if kind is None:
                raise ToolError("INVALID_ARGUMENT")
            await _operations_ref(ctx, query, "entity_id", kind, entity_id)
        for key, value in (("from", time_from), ("to", time_to)):
            if value is not None:
                if not isinstance(value, datetime) or value.utcoffset() is None:
                    raise ToolError("INVALID_ARGUMENT")
                query[key] = value.isoformat()
        if time_from is not None and time_to is not None and time_from >= time_to:
            raise ToolError("INVALID_ARGUMENT")
        return await _operations_read(ctx, operation, query=query)
    except ToolError as error:
        return fail(operation, error)


async def get_admin_dashboard(
    ctx: RunContext[TurnDeps],
    time_from: datetime,
    time_to: datetime,
    group_by: Literal["day", "month"] = "day",
) -> dict:
    """Tổng quan doanh thu, vé bán theo ngày/tháng giờ VN; khoảng tối đa 366 ngày. → getAdminDashboard"""
    raise NotImplementedError


async def query_metric(
    ctx: RunContext[TurnDeps],
    metric: Metric,
    time_from: datetime,
    time_to: datetime,
    event_id: UUID | None = None,
    showtime_id: UUID | None = None,
) -> dict:
    """Một số liệu tổng (group_by=none) trong [time_from, time_to). Chuỗi theo ngày dùng dashboard. → queryMetric"""
    raise NotImplementedError


async def list_venues(
    ctx: RunContext[TurnDeps], q: str | None = None, cursor: str | None = None, limit: PageLimit = 10
) -> dict:
    """Tìm sân. → listVenues"""
    raise NotImplementedError


async def get_admin_venue(ctx: RunContext[TurnDeps], venue_id: UUID) -> dict:
    """Chi tiết sân đã chọn. → getAdminVenue"""
    raise NotImplementedError


async def list_seat_layouts(
    ctx: RunContext[TurnDeps], venue_id: UUID, cursor: str | None = None, limit: PageLimit = 10
) -> dict:
    """Các sơ đồ ghế (layout) của một sân. → listSeatLayouts"""
    raise NotImplementedError


async def get_seat_layout(ctx: RunContext[TurnDeps], layout_id: UUID) -> dict:
    """Chi tiết layout và các khu (zone_id, sức chứa), không gồm SVG. → getSeatLayout"""
    raise NotImplementedError


async def list_admin_events(
    ctx: RunContext[TurnDeps],
    q: str | None = None,
    status: EventStatus | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Sự kiện mọi trạng thái, kể cả DRAFT. → listAdminEvents"""
    raise NotImplementedError


async def get_admin_event(ctx: RunContext[TurnDeps], event_id: UUID) -> dict:
    """Chi tiết sự kiện và các suất (kèm layout_id). → getAdminEvent"""
    raise NotImplementedError


async def get_seatmap_admin(
    ctx: RunContext[TurnDeps], showtime_id: UUID, zone_id: UUID, row: str | None = None
) -> dict:
    """Ghế mọi trạng thái trong một khu (tối đa 60). Đăng ký với model dưới tên `get_seatmap`. → getSeatmap"""
    raise NotImplementedError


ADMIN_READ_TOOLS = (
    get_admin_dashboard,
    query_metric,
    list_venues,
    get_admin_venue,
    list_seat_layouts,
    get_seat_layout,
    list_admin_events,
    get_admin_event,
    admin_list_tickets,
    admin_list_orders,
    admin_get_order,
    list_showtime_checkins,
    list_audit_logs,
)


ADMIN_OPERATIONS = frozenset(
    {"adminListOrders", "adminGetOrder", "adminListTickets", "listShowtimeCheckins", "listAuditLogs"}
)


ORDER_STATUSES = ("PENDING", "PAID", "EXPIRED", "CANCELLED", "NEEDS_REVIEW", "REJECTED")


TICKET_STATUSES = ("ISSUED", "USED", "VOID")


ENTITY_TYPES = {
    "order": "ORDER",
    "ticket_order": "ORDER",
    "ticket": "TICKET",
    "event": "EVENT",
    "showtime": "SHOWTIME",
    "venue": "VENUE",
    "seat_layout": "LAYOUT",
    "seat": "SEAT",
    "zone": "ZONE",
}


PORTAL_MESSAGE = "Đơn cần kiểm tra thủ công. Vui lòng xử lý trên Admin Portal."


def _operations_invalid():
    raise ToolError("INVALID_TOOL_RESULT")


def _operations_obj(value):
    if type(value) is not dict:
        _operations_invalid()
    return value


def _operations_text(value, *, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not str or not value.strip() or len(value) > 2000:
        _operations_invalid()
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[đã ẩn]", value)
    return re.sub(r"(?<!\w)\+?\d[\d .-]{7,}\d(?!\w)", "[đã ẩn]", value)


def _operations_code(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None:
        _operations_invalid()
    return value


def _operations_uid(value):
    if type(value) is not str:
        _operations_invalid()
    try:
        return str(UUID(value))
    except ValueError:
        _operations_invalid()


def _operations_stamp(value, *, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not str:
        _operations_invalid()
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            _operations_invalid()
        return parsed.isoformat()
    except ValueError:
        _operations_invalid()


def _operations_enum(value, choices):
    if type(value) is not str or value not in choices:
        _operations_invalid()
    return value


def _operations_money(value):
    if type(value) is not int or value < 0:
        _operations_invalid()
    return value


def _operations_order(raw, *, detail=False):
    raw = _operations_obj(raw)
    result = {
        "order_id": _operations_uid(raw.get("id")),
        "code": _operations_code(raw.get("code")),
        "status": _operations_enum(raw.get("status"), ORDER_STATUSES),
    }
    if result["status"] == "NEEDS_REVIEW":
        return {**result, "message": PORTAL_MESSAGE}
    event, showtime = _operations_obj(raw.get("event")), _operations_obj(raw.get("showtime"))
    result.update(
        event_title=_operations_text(event.get("title")),
        starts_at=_operations_stamp(showtime.get("starts_at")),
        total_amount=_operations_money(raw.get("total_amount")),
        created_at=_operations_stamp(raw.get("created_at")),
    )
    if detail:
        if type(raw.get("items")) is not list:
            _operations_invalid()
        result["items"] = []
        for line in raw["items"]:
            line = _operations_obj(line)
            quantity = line.get("quantity")
            if type(quantity) is not int or quantity < 1:
                _operations_invalid()
            result["items"].append(
                {
                    "zone": _operations_text(line.get("zone")),
                    "row": _operations_text(line.get("row"), nullable=True),
                    "number": _operations_seat_number(line.get("number")),
                    "quantity": quantity,
                    "price": _operations_money(line.get("unit_price")),
                }
            )
        result["expires_at"] = _operations_stamp(raw.get("expires_at"), nullable=True)
        if type(raw.get("payments")) is not list or type(raw.get("history")) is not list:
            _operations_invalid()
        result["payments"] = [
            {
                "status": _operations_enum(
                    _operations_obj(payment).get("status"), ("OPEN", "SUCCEEDED", "FAILED", "EXPIRED")
                ),
                "amount": _operations_money(payment.get("amount")),
                "last_response_code": _operations_response_code(payment.get("last_response_code")),
            }
            for payment in raw["payments"]
        ]
        result["history"] = [
            {
                "status": _operations_enum(_operations_obj(item).get("status"), ORDER_STATUSES),
                "at": _operations_stamp(item.get("at")),
            }
            for item in raw["history"]
        ]
    return result


def _operations_response_code(value):
    if value is None:
        return None
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) is None:
        _operations_invalid()
    return value


def _operations_seat_number(value):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        _operations_invalid()
    return str(value)


def _operations_ticket(raw):
    raw = _operations_obj(raw)
    return {
        "ticket_id": _operations_uid(raw.get("id")),
        "code": _operations_code(raw.get("code")),
        "status": _operations_enum(raw.get("status"), TICKET_STATUSES),
        "order_code": _operations_code(raw.get("order_code")),
        "event_title": _operations_text(_operations_obj(raw.get("event")).get("title")),
        "starts_at": _operations_stamp(_operations_obj(raw.get("showtime")).get("starts_at")),
        "zone": _operations_text(raw.get("zone")),
        "row": _operations_text(raw.get("row"), nullable=True),
        "number": _operations_seat_number(raw.get("number")),
    }


def _operations_checkin(raw):
    raw = _operations_obj(raw)
    ticket_code = raw.get("ticket_code")
    return {
        "ticket_code": _operations_code(ticket_code) if ticket_code is not None else None,
        "result": _operations_enum(
            raw.get("result"), ("OK", "ALREADY_USED", "TICKET_VOID", "WRONG_SHOWTIME", "INVALID_QR")
        ),
        "at": _operations_stamp(raw.get("scanned_at")),
    }


def _operations_audit(raw):
    raw = _operations_obj(raw)
    if type(raw.get("via_agent")) is not bool:
        _operations_invalid()
    return {
        "action": _operations_code(raw.get("action")),
        "entity": _operations_code(raw.get("entity")),
        "entity_id": _operations_uid(raw["entity_id"]) if raw.get("entity_id") is not None else None,
        "actor_name": _operations_text(raw.get("actor_name"), nullable=True),
        "via_agent": raw["via_agent"],
        "at": _operations_stamp(raw.get("at")),
    }


def project_operations(operation, raw, requested):
    raw = _operations_obj(raw)
    if operation == "adminGetOrder":
        result = _operations_order(raw, detail=True)
        if result["order_id"] != requested.get("order_id"):
            _operations_invalid()
    else:
        rows = raw.get("data")
        if type(rows) is not list or len(rows) > requested.get("limit", 20):
            _operations_invalid()
        transform = {
            "adminListOrders": _operations_order,
            "adminListTickets": _operations_ticket,
            "listShowtimeCheckins": _operations_checkin,
            "listAuditLogs": _operations_audit,
        }[operation]
        items = [transform(row) for row in rows]
        cursor = raw.get("next_cursor")
        if cursor is not None and (
            type(cursor) is not str or len(cursor) > 2048 or re.fullmatch(r"[A-Za-z0-9_+/=-]+", cursor) is None
        ):
            _operations_invalid()
        result = {"items": items, "next_cursor": cursor}
        for raw_row, item in zip(rows, items, strict=True):
            if operation in ("adminListOrders", "adminListTickets"):
                for key, nested in (("showtime_id", "showtime"), ("event_id", "event")):
                    if (
                        key in requested
                        and _operations_uid(_operations_obj(raw_row.get(nested)).get("id")) != requested[key]
                    ):
                        _operations_invalid()
                if "status" in requested and item["status"] != requested["status"]:
                    _operations_invalid()
                if "code" in requested and item["code"].upper() != requested["code"].upper():
                    _operations_invalid()
                if operation == "adminListTickets" and "order_code" in requested:
                    if item["order_code"].upper() != requested["order_code"].upper():
                        _operations_invalid()
            if operation == "listAuditLogs":
                for key in ("entity", "entity_id", "action"):
                    if key in requested and item[key] != requested[key]:
                        _operations_invalid()
                at = datetime.fromisoformat(item["at"])
                if "from" in requested and at < datetime.fromisoformat(requested["from"]):
                    _operations_invalid()
                if "to" in requested and at >= datetime.fromisoformat(requested["to"]):
                    _operations_invalid()
        if requested.get("ticket_id") is not None:
            if len(items) != 1 or items[0].get("ticket_id") != requested["ticket_id"]:
                _operations_invalid()
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESULT_BYTES:
        _operations_invalid()
    return result
