"""10 tool đọc của Admin (V2.2 mục 4.3). `get_seatmap` dùng chung tên với Buyer nhưng projection khác.

Chuỗi lấy ID: get_admin_event → layout_id → get_seat_layout → zone_id → get_seatmap → seat_id.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic_ai import RunContext

from vingo_agent.tools.base import TurnDeps
from vingo_agent.tools.buyer_read import PageLimit, TicketStatus

Metric = Literal["tickets_sold", "revenue", "fill_rate", "checkins"]
EventStatus = Literal["DRAFT", "PUBLISHED", "CANCELLED"]


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
    list_venues,
    get_admin_venue,
    list_seat_layouts,
    get_seat_layout,
    list_admin_events,
    get_admin_event,
    admin_list_tickets,
)
