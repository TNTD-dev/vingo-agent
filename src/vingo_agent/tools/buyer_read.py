"""11 tool đọc của Buyer (V2.3 mục 4.2). Docstring là mô tả tool cho model.

Mỗi tool: kiểm Ref → gọi backend → lọc kết quả (projections) → ghi Ref → thêm block → trả envelope.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field
from pydantic_ai import RunContext

from vingo_agent.tools.base import TurnDeps

Category = Literal["FOOTBALL", "CONCERT"]
OrderStatus = Literal["PENDING", "PAID", "EXPIRED", "CANCELLED", "NEEDS_REVIEW", "REJECTED"]
TicketStatus = Literal["ISSUED", "USED", "VOID"]
PageLimit = Annotated[int, Field(ge=1, le=20)]


async def search_events(
    ctx: RunContext[TurnDeps],
    q: str | None = None,
    category: Category | None = None,
    city: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    price_max: int | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Tìm sự kiện đang bán theo từ khóa, loại, thành phố, khoảng [time_from, time_to) +07:00. → searchEvents"""
    raise NotImplementedError


async def get_event(ctx: RunContext[TurnDeps], event_id: UUID) -> dict:
    """Chi tiết sự kiện đã tìm được: mô tả, sân, các hạng vé và giá. → getEvent"""
    raise NotImplementedError


async def list_event_showtimes(ctx: RunContext[TurnDeps], event_id: UUID) -> dict:
    """Các suất của một sự kiện kèm trạng thái bán. → listEventShowtimes"""
    raise NotImplementedError


async def get_availability(ctx: RunContext[TurnDeps], showtime_id: UUID) -> dict:
    """Số chỗ còn và giá theo khu của một suất; seating STANDING/SEATED. → getAvailability"""
    raise NotImplementedError


async def get_seatmap(ctx: RunContext[TurnDeps], showtime_id: UUID, zone_id: UUID, row: str | None = None) -> dict:
    """Ghế còn trống trong một khu ngồi, tối đa 60 ghế; nhiều hơn thì hỏi user chọn hàng. → getSeatmap"""
    raise NotImplementedError


async def get_me(ctx: RunContext[TurnDeps]) -> dict:
    """Tài khoản đã xác minh email chưa (điều kiện giữ vé). Không trả email, SĐT. → getMe"""
    raise NotImplementedError


async def list_my_orders(
    ctx: RunContext[TurnDeps],
    status: OrderStatus | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Đơn của chính người dùng. → listMyOrders"""
    raise NotImplementedError


async def get_order(ctx: RunContext[TurnDeps], order_id: UUID) -> dict:
    """Chi tiết đơn đã tìm được. → getOrder"""
    raise NotImplementedError


async def get_payment(ctx: RunContext[TurnDeps], order_id: UUID) -> dict:
    """Trạng thái thanh toán gần nhất của đơn (OPEN/SUCCEEDED/FAILED/EXPIRED). Không trả link. → getPayment"""
    raise NotImplementedError


async def list_my_tickets(
    ctx: RunContext[TurnDeps],
    when: Literal["upcoming", "past"] | None = None,
    status: TicketStatus | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Vé của chính người dùng. → listMyTickets"""
    raise NotImplementedError


async def get_ticket(ctx: RunContext[TurnDeps], ticket_id: UUID) -> dict:
    """Chi tiết vé; không trả QR (web tự tải ảnh QR). → getTicket"""
    raise NotImplementedError


BUYER_READ_TOOLS = (
    search_events,
    get_event,
    list_event_showtimes,
    get_availability,
    get_seatmap,
    get_me,
    list_my_orders,
    get_order,
    get_payment,
    list_my_tickets,
    get_ticket,
)
