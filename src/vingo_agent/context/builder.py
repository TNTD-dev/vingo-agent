"""Dựng context theo thứ tự V2.2 mục 6 (đầu ổn định để trúng prompt cache):
1 system prompt + schema tool · 2 summary + lịch sử gần (giữ trọn cặp tool call/result) · 3 context động
(giờ VN, đề xuất PENDING, kết quả thực thi chưa thấy) · 4 tin nhắn mới · 5 kết quả tool của turn.
"""

from uuid import UUID


async def build_history(conversation_id: UUID) -> list:
    """Trả list ModelMessage của PydanticAI; bỏ cả turn nếu cặp tool call/result hỏng."""
    raise NotImplementedError


def dynamic_context(now_vn: str, pending_action: dict | None, unseen_results: list[dict]) -> str:
    raise NotImplementedError
