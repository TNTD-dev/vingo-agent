"""Tra khi không rõ kết quả ghi (V2.2 mục 7, bảng tra).

Retry an toàn: HOLD_SEATS, CREATE_PAYMENT (cùng Idempotency-Key), PUBLISH_EVENT, BLOCK/UNBLOCK_SEATS.
Đọc để tra: CANCEL_ORDER (getOrder: CANCELLED → SUCCEEDED).
Không tra được: RESEND_*, CREATE_EVENT, CREATE_SHOWTIME → UNKNOWN.
EXECUTION UNKNOWN được tra lại khi user mở hội thoại (GET /conversations/{id}); không có job nền.
"""

from vingo_agent.actions.proposals import Operation

SAFE_RETRY = frozenset(
    {
        Operation.HOLD_SEATS,
        Operation.CREATE_PAYMENT,
        Operation.PUBLISH_EVENT,
        Operation.BLOCK_SEATS,
        Operation.UNBLOCK_SEATS,
    }
)
READ_LOOKUP = frozenset({Operation.CANCEL_ORDER})


async def resolve(action_id, *, backend):
    raise NotImplementedError
