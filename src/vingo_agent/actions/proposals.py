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
    CREATE_VENUE = "CREATE_VENUE"
    UPDATE_VENUE = "UPDATE_VENUE"
    UPDATE_EVENT = "UPDATE_EVENT"
    UPDATE_SHOWTIME = "UPDATE_SHOWTIME"
    UPDATE_PRICE_TIERS = "UPDATE_PRICE_TIERS"
    ADD_TIER_PRICE = "ADD_TIER_PRICE"
    UPDATE_ZONE_PRICES = "UPDATE_ZONE_PRICES"
    CANCEL_EVENT = "CANCEL_EVENT"
    CANCEL_SHOWTIME = "CANCEL_SHOWTIME"
    SEAT_LAYOUT = "SEAT_LAYOUT"
    UPDATE_EVENT_VISIBILITY = "UPDATE_EVENT_VISIBILITY"


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
    Operation.CREATE_VENUE: "createVenue",
    Operation.UPDATE_VENUE: "updateVenue",
    Operation.UPDATE_EVENT: "updateEvent",
    Operation.UPDATE_SHOWTIME: "updateShowtime",
    Operation.UPDATE_PRICE_TIERS: "putPriceTiers",
    Operation.ADD_TIER_PRICE: "addSkuPrice",
    Operation.UPDATE_ZONE_PRICES: "putZonePrices",
    Operation.CANCEL_EVENT: "cancelEvent",
    Operation.CANCEL_SHOWTIME: "cancelShowtime",
    Operation.SEAT_LAYOUT: "completeVenueImport",
    Operation.UPDATE_EVENT_VISIBILITY: "updateEventVisibility",
}

ADMIN_WORKFLOWS = frozenset(
    {Operation.CANCEL_EVENT, Operation.CANCEL_SHOWTIME, Operation.SEAT_LAYOUT, Operation.UPDATE_EVENT_VISIBILITY}
)
NEEDS_BUYER_FORM = frozenset({Operation.CREATE_PAYMENT})


async def create(*, conversation_id, actor_id, role, operation: str, payload, display, expires_at=None, turn_id=None):
    """Trả (action_id, version). turn_id = None khi code tự tạo (đề xuất thanh toán sau hold)."""
    from datetime import UTC, datetime

    from sqlalchemy import insert, select, update

    from vingo_agent.persistence.db import transaction
    from vingo_agent.persistence.models import conversation, pending_action
    from vingo_agent.tools.base import ToolError

    now = datetime.now(UTC)
    ttl = (
        HOLD_TTL
        if operation == Operation.HOLD_SEATS
        else timedelta(minutes=30)
        if operation == Operation.SEAT_LAYOUT
        else DEFAULT_TTL
    )
    if operation in {Operation.CREATE_PAYMENT, Operation.CANCEL_ORDER} and expires_at is None:
        raise ToolError("INVALID_ARGUMENT")
    if operation not in {Operation.CREATE_PAYMENT, Operation.CANCEL_ORDER}:
        expires_at = min(expires_at, now + ttl) if expires_at is not None else now + ttl
    async with transaction() as conn:
        # Khóa cha để cả hai lần tạo đầu tiên cũng tuần tự, trước unique partial index.
        owner = (
            (await conn.execute(select(conversation).where(conversation.c.id == conversation_id).with_for_update()))
            .mappings()
            .one_or_none()
        )
        if owner is None or owner["owner_id"] != actor_id or owner["role"] != role:
            raise ToolError("FORBIDDEN")
        await conn.execute(
            update(pending_action)
            .where(pending_action.c.conversation_id == conversation_id, pending_action.c.state == "PENDING")
            .values(state="SUPERSEDED", decided_at=now)
        )
        row = (
            await conn.execute(
                insert(pending_action)
                .values(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    actor_id=actor_id,
                    role=role,
                    operation=operation,
                    payload=payload,
                    display=display,
                    expires_at=expires_at,
                    version=1,
                    state="PENDING",
                )
                .returning(pending_action.c.id, pending_action.c.version)
            )
        ).one()
        return row.id, row.version
