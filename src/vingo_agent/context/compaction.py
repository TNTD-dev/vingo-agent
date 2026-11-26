"""Compact khi ước lượng input ≥ compact_ratio × B (B = context window − phần dành cho output).

Tóm tắt các turn cũ, giữ 3 turn gần nhất, lưu bảng summary, ước lượng lại; vẫn quá → CONTEXT_LIMIT.
Summary chỉ ghi mục tiêu, lựa chọn, câu hỏi mở; không làm bằng chứng giá/trạng thái.
"""

KEEP_RECENT_TURNS = 3


async def compact_if_needed(conversation_id, estimated_tokens: int, budget_tokens: int) -> bool:
    raise NotImplementedError
