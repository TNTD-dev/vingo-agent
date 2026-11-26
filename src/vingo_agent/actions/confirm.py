"""Xác nhận/từ chối (V2.3 mục 7, 4.5). Dùng chung cho nút (BUTTON) và tin nhắn (TEXT). Không gọi model."""

from enum import StrEnum


class ConfirmedVia(StrEnum):
    BUTTON = "BUTTON"
    TEXT = "TEXT"


async def confirm(*, action_id, version: int, principal, via: ConfirmedVia, buyer=None):
    """G8 → CAS PENDING→CONFIRMED + EXECUTION RUNNING (một transaction) → execution.run().
    Bấm hai lần: EXECUTION đã có thì trả lại kết quả của nó. Yield các StreamEvent."""
    raise NotImplementedError


async def reject(*, action_id, version: int, principal) -> str:
    """PENDING→REJECTED; không đụng đơn đã tạo."""
    raise NotImplementedError
