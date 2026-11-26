"""Tạo đề xuất (V2.3 mục 4.4): một transaction supersede PENDING cũ + insert PENDING mới."""

from datetime import timedelta
from enum import StrEnum

HOLD_TTL = timedelta(minutes=5)
DEFAULT_TTL = timedelta(minutes=10)


class Operation(StrEnum):
    HOLD_SEATS = "HOLD_SEATS"
    CREATE_PAYMENT = "CREATE_PAYMENT"
    CANCEL_ORDER = "CANCEL_ORDER"
    RESEND_MY_TICKET = "RESEND_MY_TICKET"
    CREATE_EVENT = "CREATE_EVENT"
    CREATE_SHOWTIME = "CREATE_SHOWTIME"
    PUBLISH_EVENT = "PUBLISH_EVENT"
    BLOCK_SEATS = "BLOCK_SEATS"
    UNBLOCK_SEATS = "UNBLOCK_SEATS"
    ADMIN_RESEND_TICKET = "ADMIN_RESEND_TICKET"


# Operation → operationId backend thực thi.
BACKEND_OPERATION: dict[Operation, str] = {
    Operation.HOLD_SEATS: "holdSeats",
    Operation.CREATE_PAYMENT: "createPayment",
    Operation.CANCEL_ORDER: "cancelOrder",
    Operation.RESEND_MY_TICKET: "resendMyTicket",
    Operation.CREATE_EVENT: "createEvent",
    Operation.CREATE_SHOWTIME: "createShowtime",
    Operation.PUBLISH_EVENT: "publishEvent",
    Operation.BLOCK_SEATS: "blockSeats",
    Operation.UNBLOCK_SEATS: "unblockSeats",
    Operation.ADMIN_RESEND_TICKET: "adminResendTicket",
}

NEEDS_BUYER_FORM = frozenset({Operation.CREATE_PAYMENT})


async def create(*, conversation_id, turn_id, actor_id, role, operation: Operation, payload, display, expires_at):
    """Trả (action_id, version). turn_id = None khi code tự tạo (đề xuất thanh toán sau hold)."""
    raise NotImplementedError
