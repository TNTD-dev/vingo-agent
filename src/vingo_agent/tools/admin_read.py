"""Tool đọc của Admin (V2.3 mục 4.3). `get_seatmap` dùng chung tên với Buyer nhưng projection khác.

Chuỗi lấy ID: get_admin_event → layout_id → get_seat_layout → zone_id → get_seatmap → seat_id.
"""

import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic_ai import RunContext

from vingo_agent.guardrails.input import redact
from vingo_agent.guardrails.result import MAX_RESULT_BYTES
from vingo_agent.schemas.chat import Role
from vingo_agent.tools import blocks
from vingo_agent.tools.base import ToolError, TurnDeps, fail, ok, record_refs, require_ref
from vingo_agent.tools.buyer_read import OrderStatus, PageLimit, TicketStatus
from vingo_agent.tools.catalog import ZonePriceInput, as_record, uuid_text, zone_price_body
from vingo_agent.tools.projections import project

Metric = Literal["tickets_sold", "revenue", "fill_rate", "checkins"]
EventStatus = Literal["DRAFT", "PUBLISHED", "CANCELLED"]


def _reporting_validate_window(time_from: datetime, time_to: datetime) -> dict[str, str]:
    """Kiểm theo instant UTC để không lệch khi hai mốc có offset khác nhau."""
    if not all(isinstance(value, datetime) and value.utcoffset() is not None for value in (time_from, time_to)):
        raise ToolError("INVALID_ARGUMENT")
    try:
        start, end = time_from.astimezone(UTC), time_to.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ToolError("INVALID_ARGUMENT") from None
    if not start < end or end - start > timedelta(days=366):
        raise ToolError("INVALID_ARGUMENT")
    return {"from": time_from.isoformat(), "to": time_to.isoformat()}


def _reporting_admin(ctx: RunContext[TurnDeps]) -> None:
    if ctx.deps.principal.role != Role.ADMIN:
        raise ToolError("FORBIDDEN")


async def get_admin_dashboard(
    ctx: RunContext[TurnDeps],
    time_from: datetime,
    time_to: datetime,
    group_by: Literal["day", "month"] = "day",
) -> dict:
    """Tổng quan và chuỗi theo ngày/tháng giờ VN, tối đa 366 ngày; cần đủ khoảng thời gian. → getAdminDashboard"""
    operation = "getAdminDashboard"
    try:
        _reporting_admin(ctx)
        if group_by not in ("day", "month"):
            raise ToolError("INVALID_ARGUMENT")
        query = {**_reporting_validate_window(time_from, time_to), "group_by": group_by}
        raw = await ctx.deps.backend.read(operation, query=query)
        data, truncated = project(operation, raw, role=ctx.deps.principal.role, requested=query)
        return ok(operation, data, truncated=truncated, observed_at=datetime.now(UTC))
    except ToolError as error:
        return fail(operation, error)


async def query_metric(
    ctx: RunContext[TurnDeps],
    metric: Metric,
    time_from: datetime,
    time_to: datetime,
    event_id: UUID | None = None,
    showtime_id: UUID | None = None,
) -> dict:
    """Một số liệu tổng trong [time_from,time_to), tối đa 366 ngày; chuỗi ngày/tháng dùng dashboard. → queryMetric"""
    operation = "queryMetric"
    try:
        _reporting_admin(ctx)
        if metric not in METRICS:
            raise ToolError("INVALID_ARGUMENT")
        query = {**_reporting_validate_window(time_from, time_to), "metric": metric, "group_by": "none"}
        resources = (("EVENT", "event_id", event_id), ("SHOWTIME", "showtime_id", showtime_id))
        for resource_type, key, resource_id in resources:
            if resource_id is not None:
                if not isinstance(resource_id, UUID):
                    raise ToolError("INVALID_ARGUMENT")
                await require_ref(ctx.deps, resource_type, resource_id)
                query[key] = str(resource_id)
        raw = await ctx.deps.backend.read(operation, query=query)
        data, truncated = project(operation, raw, role=ctx.deps.principal.role, requested=query)
        return ok(operation, data, truncated=truncated, observed_at=datetime.now(UTC))
    except ToolError as error:
        return fail(operation, error)


async def list_metrics(ctx: RunContext[TurnDeps], time_from: datetime, time_to: datetime) -> dict:
    """Danh mục cho khoảng ≤366 ngày; computed_at là lúc dựng danh mục, không phải lúc đo. → list_metrics (local)"""
    operation = "list_metrics"
    try:
        _reporting_admin(ctx)
        window = _reporting_validate_window(time_from, time_to)
        generated_at = datetime.now(UTC)
        data = {
            **window,
            "timezone": TIMEZONE,
            "computed_at": generated_at.isoformat(),
            "computed_at_basis": "catalog_generated_at",
            "interval": "[from,to)",
            "metrics": [{"metric": metric, **definition} for metric, definition in METRICS.items()],
        }
        return ok(operation, data, observed_at=generated_at)
    except ToolError as error:
        return fail(operation, error)


async def list_venues(
    ctx: RunContext[TurnDeps], q: str | None = None, cursor: str | None = None, limit: PageLimit = 10
) -> dict:
    """Tìm sân. → listVenues"""
    return await _catalog_list(ctx, "listVenues", "VENUE", "venue_id", cursor, limit, q=q)


async def get_admin_venue(ctx: RunContext[TurnDeps], venue_id: UUID) -> dict:
    """Chi tiết sân đã chọn. → getAdminVenue"""
    return await _catalog_detail(ctx, "getAdminVenue", "VENUE", "venue_id", venue_id)


async def list_seat_layouts(
    ctx: RunContext[TurnDeps], venue_id: UUID, cursor: str | None = None, limit: PageLimit = 10
) -> dict:
    """Các sơ đồ ghế (layout) của một sân. → listSeatLayouts"""
    path = {"venue_id": str(venue_id)}
    return await _catalog_list(
        ctx, "listSeatLayouts", "LAYOUT", "layout_id", cursor, limit, path=path, parent=("VENUE", venue_id)
    )


async def get_seat_layout(ctx: RunContext[TurnDeps], layout_id: UUID) -> dict:
    """Chi tiết layout và các khu (zone_id, sức chứa), không gồm SVG. → getSeatLayout"""
    return await _catalog_detail(
        ctx, "getSeatLayout", "LAYOUT", "layout_id", layout_id, refs=(("zones", "ZONE", "zone_id"),)
    )


async def get_seat_layout_zone(ctx: RunContext[TurnDeps], layout_id: UUID, zone_id: UUID) -> dict:
    """Xem hàng và số ghế của khu ngồi, hoặc sức chứa khu đứng, trên đúng phiên bản layout. → getSeatLayoutZone"""
    operation = "getSeatLayoutZone"
    try:
        _reporting_admin(ctx)
        deps = ctx.deps
        await require_ref(deps, "LAYOUT", layout_id)
        await require_ref(deps, "ZONE", zone_id)
        path = {"layout_id": str(layout_id), "zone_id": str(zone_id)}
        raw = await deps.backend.read(operation, path=path)
        data, truncated = project(operation, raw, role=Role.ADMIN, requested={"zone_id": zone_id})
        await record_refs(deps, "SEAT", [UUID(seat["seat_id"]) for row in data["rows"] or [] for seat in row["seats"]])
        return ok(operation, data | {"layout_id": str(layout_id)}, truncated=truncated, observed_at=datetime.now(UTC))
    except ToolError as error:
        return fail(operation, error)


async def get_admin_layout_tree(
    ctx: RunContext[TurnDeps],
    venue_id: UUID,
    layout_type: Literal["FOOTBALL", "CONCERT"],
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Xem các phiên bản layout của sân theo loại, mới trước, kèm thông tin nền và các khu. → getAdminLayoutTree"""
    return await _catalog_list(
        ctx,
        "getAdminLayoutTree",
        "LAYOUT",
        "layout_id",
        cursor,
        limit,
        path={"venue_id": str(venue_id)},
        parent=("VENUE", venue_id),
        layout_type=layout_type,
    )


async def list_admin_events(
    ctx: RunContext[TurnDeps],
    q: str | None = None,
    status: EventStatus | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Sự kiện mọi trạng thái, kể cả DRAFT. → listAdminEvents"""
    return await _catalog_list(ctx, "listAdminEvents", "EVENT", "event_id", cursor, limit, q=q, status=status)


async def get_admin_event(ctx: RunContext[TurnDeps], event_id: UUID) -> dict:
    """Chi tiết sự kiện và các suất (kèm layout_id). → getAdminEvent"""
    return await _catalog_detail(
        ctx,
        "getAdminEvent",
        "EVENT",
        "event_id",
        event_id,
        refs=(
            ("showtimes", "SHOWTIME", "showtime_id"),
            ("showtimes", "LAYOUT", "layout_id"),
            ("price_tiers", "PRICE_TIER", "price_tier_id"),
            ("zone_prices", "ZONE", "zone_id"),
        ),
    )


async def _catalog_list(ctx, operation, resource_type, key, cursor, limit, *, path=None, parent=None, **filters):
    try:
        _reporting_admin(ctx)
        if parent is not None:
            await require_ref(ctx.deps, *parent)
        query = _operations_page(cursor, limit) | {key: value for key, value in filters.items() if value is not None}
        raw = await ctx.deps.backend.read(operation, path=path, query=query)
        requested = dict(path or {})
        if operation == "getAdminLayoutTree":
            requested["layout_type"] = filters["layout_type"]
        data, truncated = project(operation, raw, role=Role.ADMIN, requested=requested)
        block = blocks.build_block(operation, data, role=Role.ADMIN)
        await record_refs(ctx.deps, resource_type, [UUID(item[key]) for item in data["items"]])
        if operation == "getAdminLayoutTree":
            await record_refs(
                ctx.deps, "ZONE", [UUID(zone["zone_id"]) for item in data["items"] for zone in item["zones"]]
            )
        if block is not None:
            ctx.deps.blocks.append(block)
        return ok(operation, data, truncated=truncated, observed_at=datetime.now(UTC))
    except ToolError as error:
        return fail(operation, error)


async def _catalog_detail(ctx, operation, resource_type, key, resource_id, *, refs=()):
    try:
        _reporting_admin(ctx)
        await require_ref(ctx.deps, resource_type, resource_id)
        path = {key: str(resource_id)}
        raw = await ctx.deps.backend.read(operation, path=path)
        data, truncated = project(operation, raw, role=Role.ADMIN, requested=path)
        if operation == "getAdminEvent" and data.get("layout_id"):
            await record_refs(ctx.deps, "LAYOUT", [UUID(data["layout_id"])])
        for collection, kind, item_key in refs:
            await record_refs(ctx.deps, kind, [UUID(item[item_key]) for item in data[collection]])
        return ok(operation, data, truncated=truncated, observed_at=datetime.now(UTC))
    except ToolError as error:
        return fail(operation, error)


async def get_zone_prices_preview(ctx: RunContext[TurnDeps], event_id: UUID, assignments: list[ZonePriceInput]) -> dict:
    """Xem trước tổng SKU và SKU mỗi khu cho toàn bộ danh sách gán hạng vé. → previewZonePrices"""
    operation = "previewZonePrices"
    try:
        _reporting_admin(ctx)
        deps = ctx.deps
        deps.zone_price_preview = None
        await require_ref(deps, "EVENT", event_id)
        body = zone_price_body(assignments)
        for item in assignments:
            await require_ref(deps, "ZONE", item.zone_id)
            await require_ref(deps, "PRICE_TIER", item.price_tier_id)
        path = {"event_id": str(event_id)}
        event = as_record(await deps.backend.read("getAdminEvent", path=path), str(event_id))
        layout_id = uuid_text(event["seat_layout_id"]) if event.get("seat_layout_id") else None
        raw = await deps.backend.read(operation, path=path, body=body)
        data, truncated = project(operation, raw, role=Role.ADMIN, requested=path)
        zones = data["zones"]
        expected_zones = {item["zone_id"] for item in body}
        if (
            len({item["zone_id"] for item in zones}) != len(zones)
            or not {item["zone_id"] for item in zones} <= expected_zones
        ):
            raise ToolError("INVALID_TOOL_RESULT")
        if not truncated and (
            len(zones) != len(body) or sum(item["sku_count"] for item in zones) != data["total_sku_count"]
        ):
            raise ToolError("INVALID_TOOL_RESULT")
        deps.zone_price_preview = str(event_id), body, layout_id, data
        return ok(operation, data, truncated=truncated, observed_at=datetime.now(UTC))
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


async def get_seatmap_admin(
    ctx: RunContext[TurnDeps], showtime_id: UUID, zone_id: UUID, row: str | None = None
) -> dict:
    """Xem tối đa 60 ghế với mọi trạng thái trong một khu của suất công khai. Nếu bị cắt, chọn hàng. → getSeatmap"""
    operation = "getSeatmap"
    try:
        _reporting_admin(ctx)
        deps = ctx.deps
        if row is not None:
            row = row.strip()
            if not row:
                raise ToolError("INVALID_ARGUMENT")
        await require_ref(deps, "SHOWTIME", showtime_id)
        await require_ref(deps, "ZONE", zone_id)
        requested = {"showtime_id": showtime_id, "zone_id": zone_id}
        raw = await deps.backend.read(
            operation, path={"showtime_id": str(showtime_id)}, query={"zone_id": str(zone_id)}
        )
        data, truncated = project(operation, raw, role=Role.ADMIN, requested=requested, row=row)
        block = blocks.build_block(operation, data, role=Role.ADMIN)
        await record_refs(deps, "SEAT", [UUID(seat["seat_id"]) for seat in data["seats"]])
        if block is not None:
            deps.blocks.append(block)
        return ok(operation, data, truncated=truncated, observed_at=datetime.now(UTC))
    except ToolError as error:
        return fail(operation, error)


REPORTING_OPERATIONS = frozenset({"getAdminDashboard", "queryMetric"})


TIMEZONE = "Asia/Ho_Chi_Minh"


METRICS = {
    "tickets_sold": {
        "unit": "tickets",
        "definition": "Tổng quantity của đơn PAID.",
        "time_basis": "paid_at",
    },
    "revenue": {
        "unit": "VND",
        "definition": "Tổng quantity × unit_price của đơn PAID; không giảm theo vé VOID.",
        "time_basis": "paid_at",
    },
    "fill_rate": {
        "unit": "percent",
        "definition": "100 × tổng sold / tổng capacity của suất SCHEDULED; mẫu bằng 0 trả 0.",
        "time_basis": "starts_at",
    },
    "checkins": {
        "unit": "checkins",
        "definition": "Số vé USED khác nhau thuộc đơn PAID, theo lần quét OK đầu tiên.",
        "time_basis": "first_ok_scan",
    },
}


def _reporting_object(value) -> dict:
    if type(value) is not dict:
        raise ToolError("INVALID_TOOL_RESULT")
    return value


def _reporting_timestamp(value) -> datetime:
    if type(value) is not str:
        raise ToolError("INVALID_TOOL_RESULT")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except ValueError:
        raise ToolError("INVALID_TOOL_RESULT") from None


def _reporting_number(value, *, integer: bool = False) -> int | float:
    if type(value) not in (int, float) or value < 0 or (type(value) is float and not math.isfinite(value)):
        raise ToolError("INVALID_TOOL_RESULT")
    if integer:
        if type(value) is float and not value.is_integer():
            raise ToolError("INVALID_TOOL_RESULT")
        return int(value)
    return value


def _reporting_metadata(raw: dict, requested: dict, *, dashboard: bool = False) -> dict:
    if dashboard:
        if raw.get("timezone") != TIMEZONE:
            raise ToolError("INVALID_TOOL_RESULT")
        for key in ("from", "to"):
            if _reporting_timestamp(raw.get(key)) != _reporting_timestamp(requested[key]):
                raise ToolError("INVALID_TOOL_RESULT")
    return {
        "from": requested["from"],
        "to": requested["to"],
        "timezone": TIMEZONE,
        "computed_at": _reporting_timestamp(raw.get("computed_at")).isoformat(),
    }


def _reporting_filters(raw: dict, requested: dict, *, required: bool) -> None:
    if not required and raw.get("filters") is None:
        return
    filters = _reporting_object(raw.get("filters"))
    for key in ("from", "to"):
        if (required or key in filters) and _reporting_timestamp(filters.get(key)) != _reporting_timestamp(
            requested[key]
        ):
            raise ToolError("INVALID_TOOL_RESULT")
    if (required or "group_by" in filters) and filters.get("group_by") != requested["group_by"]:
        raise ToolError("INVALID_TOOL_RESULT")
    # Không chấp nhận backend âm thầm thêm/bỏ bộ lọc rồi gắn nhãn kết quả khác phạm vi.
    for key in ("event_id", "showtime_id", "price_tier_id"):
        if filters.get(key) != requested.get(key):
            raise ToolError("INVALID_TOOL_RESULT")


def _reporting_series(raw, *, metric: str, group_by: str) -> list[dict]:
    if type(raw) is not list:
        raise ToolError("INVALID_TOOL_RESULT")
    result, seen = [], set()
    pattern = r"\d{4}-\d{2}-\d{2}" if group_by == "day" else r"\d{4}-\d{2}"
    for item in raw:
        item = _reporting_object(item)
        key = item.get("key")
        if type(key) is not str or re.fullmatch(pattern, key) is None or key in seen:
            raise ToolError("INVALID_TOOL_RESULT")
        try:
            datetime.strptime(key, "%Y-%m-%d" if group_by == "day" else "%Y-%m")
        except ValueError:
            raise ToolError("INVALID_TOOL_RESULT") from None
        seen.add(key)
        # Label được dựng từ bucket đã kiểm, không chép text tùy ý từ backend.
        result.append(
            {"key": key, "label": key, "value": _reporting_number(item.get("value"), integer=metric != "fill_rate")}
        )
    return result


def _reporting_metric(raw, requested: dict, *, grouped: bool = False) -> dict:
    raw = _reporting_object(raw)
    metric = requested["metric"]
    if raw.get("metric") != metric or raw.get("unit") != METRICS[metric]["unit"]:
        raise ToolError("INVALID_TOOL_RESULT")
    _reporting_filters(raw, requested, required=not grouped)
    result = {"metric": metric, "unit": METRICS[metric]["unit"], **_reporting_metadata(raw, requested)}
    if grouped:
        result["series"] = _reporting_series(raw.get("series"), metric=metric, group_by=requested["group_by"])
    else:
        result["value"] = _reporting_number(raw.get("value"), integer=metric != "fill_rate")
    return result


def _reporting_kpis(raw) -> dict:
    raw = _reporting_object(raw)
    result = {key: _reporting_number(raw.get(key), integer=True) for key in ("revenue", "tickets_sold")}
    if "new_users" in raw:
        result["new_users"] = _reporting_number(raw["new_users"], integer=True)
    return result


def project_reporting(operation: str, raw: dict, requested: dict) -> dict:
    """Allowlist riêng cho reporting; caller đi qua tools.projections.project."""
    raw = _reporting_object(raw)
    requested = _reporting_object(requested)
    start, end = (_reporting_timestamp(requested.get(key)) for key in ("from", "to"))
    try:
        duration = end.astimezone(UTC) - start.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ToolError("INVALID_TOOL_RESULT") from None
    if not timedelta(0) < duration <= timedelta(days=366):
        raise ToolError("INVALID_TOOL_RESULT")
    if operation == "queryMetric":
        if requested.get("metric") not in METRICS or requested.get("group_by") != "none":
            raise ToolError("INVALID_TOOL_RESULT")
        return _reporting_metric(raw, requested)
    if operation != "getAdminDashboard" or requested.get("group_by") not in ("day", "month"):
        raise ToolError("INVALID_TOOL_RESULT")
    return {
        **_reporting_metadata(raw, requested, dashboard=True),
        "current": _reporting_kpis(raw.get("current")),
        "previous": _reporting_kpis(raw.get("previous")),
        "on_sale_showtimes": _reporting_number(raw.get("on_sale_showtimes"), integer=True),
        "revenue": _reporting_metric(raw.get("revenue"), {**requested, "metric": "revenue"}, grouped=True),
        "tickets": _reporting_metric(raw.get("tickets"), {**requested, "metric": "tickets_sold"}, grouped=True),
    }


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
    """Tra đơn bằng mã; email/SĐT và NEEDS_REVIEW dùng Admin Portal theo KAN-210. → adminListOrders"""
    operation = "adminListOrders"
    try:
        _operations_admin(ctx)
        query = _operations_page(cursor, limit)
        _operations_choice(query, "status", status, ORDER_STATUSES)
        await _operations_ref(ctx, query, "event_id", "EVENT", event_id)
        await _operations_ref(ctx, query, "showtime_id", "SHOWTIME", showtime_id)
        if q is not None:
            if type(q) is not str:
                raise ToolError("INVALID_ARGUMENT")
            code = q.strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{3,64}", code) is None or redact(code) != code:
                raise ToolError("INVALID_ARGUMENT")
            query["q"] = code
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


ADMIN_READ_TOOLS = (
    get_admin_dashboard,
    query_metric,
    list_metrics,
    list_venues,
    get_admin_venue,
    list_seat_layouts,
    get_seat_layout,
    get_seat_layout_zone,
    get_admin_layout_tree,
    list_admin_events,
    get_admin_event,
    get_zone_prices_preview,
    admin_list_tickets,
    admin_list_orders,
    admin_get_order,
    list_showtime_checkins,
    list_audit_logs,
)
