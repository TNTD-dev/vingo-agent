"""10 tool đề xuất (V2.3 mục 4.4): chỉ tạo pending action, không bao giờ ghi nghiệp vụ.

Mỗi tool: kiểm Ref + điều kiện (đọc lại backend nếu cần) → actions.proposals.create(...) → block ConfirmAction
→ trả {"status": "PROPOSED", "action_id", "expires_at"}. Tối đa một đề xuất mỗi turn.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field
from pydantic_ai import RunContext

from vingo_agent.tools.base import TurnDeps

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
async def propose_create_event(
    ctx: RunContext[TurnDeps],
    title: Annotated[str, Field(min_length=1, max_length=200)],
    category: Literal["FOOTBALL", "CONCERT"],
    description: Annotated[str, Field(max_length=2000)] | None = None,
    max_tickets_per_order: Annotated[int, Field(ge=1, le=20)] | None = None,
) -> dict:
    """Đề xuất tạo sự kiện DRAFT. → createEvent"""
    raise NotImplementedError


async def propose_create_showtime(
    ctx: RunContext[TurnDeps],
    event_id: UUID,
    layout_id: UUID,
    starts_at: datetime,
    ends_at: datetime,
    sale_opens_at: datetime,
    sale_closes_at: datetime,
) -> dict:
    """Đề xuất tạo suất; cần sale_opens_at < sale_closes_at ≤ starts_at < ends_at, layout PUBLISHED. → createShowtime"""
    raise NotImplementedError


async def propose_publish_event(ctx: RunContext[TurnDeps], event_id: UUID) -> dict:
    """Đề xuất công bố sự kiện DRAFT. → publishEvent"""
    raise NotImplementedError


async def propose_block_seats(
    ctx: RunContext[TurnDeps],
    showtime_id: UUID,
    seat_ids: SeatIdList,
    reason: Annotated[str, Field(min_length=1, max_length=1000)],
) -> dict:
    """Đề xuất khóa ghế (cần lý do). → blockSeats"""
    raise NotImplementedError


async def propose_unblock_seats(ctx: RunContext[TurnDeps], showtime_id: UUID, seat_ids: SeatIdList) -> dict:
    """Đề xuất mở khóa ghế. → unblockSeats"""
    raise NotImplementedError


async def propose_admin_resend_ticket(ctx: RunContext[TurnDeps], ticket_id: UUID) -> dict:
    """Đề xuất gửi lại vé ISSUED tới email trên đơn. → adminResendTicket"""
    raise NotImplementedError


BUYER_PROPOSAL_TOOLS = (
    propose_hold_seats,
    propose_create_payment,
    propose_cancel_order,
    propose_resend_my_ticket,
)
ADMIN_PROPOSAL_TOOLS = (
    propose_create_event,
    propose_create_showtime,
    propose_publish_event,
    propose_block_seats,
    propose_unblock_seats,
    propose_admin_resend_ticket,
)
