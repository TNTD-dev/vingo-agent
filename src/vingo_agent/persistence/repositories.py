"""Truy vấn schema agent bằng SQLAlchemy Core (bảng ở persistence/models.py).

Quy tắc: transaction ngắn, không giữ qua lời gọi backend/LLM; CAS bằng UPDATE ... WHERE state/version.
"""


async def acquire_turn(conversation_id, turn_id, lease_s: float) -> bool:
    """UPDATE conversation SET active_turn_id, active_until WHERE active_turn_id IS NULL OR active_until < now()."""
    raise NotImplementedError


async def release_turn(conversation_id, turn_id) -> None:
    raise NotImplementedError


async def append_messages(conversation_id, turn_id, messages: list[dict]) -> None:
    """Cấp seq tăng dần trong conversation."""
    raise NotImplementedError
