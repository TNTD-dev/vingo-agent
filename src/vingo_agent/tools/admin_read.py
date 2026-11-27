"""10 tool đọc của Admin (V2.3 mục 4.3). `get_seatmap` dùng chung tên với Buyer nhưng projection khác.

Chuỗi lấy ID: get_admin_event → layout_id → get_seat_layout → zone_id → get_seatmap → seat_id.
"""

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic_ai import RunContext

from vingo_agent.schemas.chat import Role
from vingo_agent.tools.base import ToolError, TurnDeps, fail, ok, require_ref
from vingo_agent.tools.buyer_read import PageLimit, TicketStatus
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


async def admin_list_tickets(
    ctx: RunContext[TurnDeps],
    showtime_id: UUID | None = None,
    order_id: UUID | None = None,
    code: str | None = None,
    status: TicketStatus | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Tra cứu vé (cần ít nhất một bộ lọc); không trả email, SĐT, QR. → adminListTickets"""
    raise NotImplementedError


async def get_seatmap_admin(
    ctx: RunContext[TurnDeps], showtime_id: UUID, zone_id: UUID, row: str | None = None
) -> dict:
    """Ghế mọi trạng thái trong một khu (tối đa 60). Đăng ký với model dưới tên `get_seatmap`. → getSeatmap"""
    raise NotImplementedError


ADMIN_READ_TOOLS = (
    get_admin_dashboard,
    query_metric,
    list_metrics,
    list_venues,
    get_admin_venue,
    list_seat_layouts,
    get_seat_layout,
    list_admin_events,
    get_admin_event,
    admin_list_tickets,
)


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
