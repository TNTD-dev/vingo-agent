"""Compact turn cũ bằng callback LLM, không giữ transaction qua mạng."""

from vingo_agent.config import get_settings
from vingo_agent.persistence.repositories import load_context, save_summary
from vingo_agent.tools.base import ToolError

KEEP_RECENT_TURNS = 3
SUMMARY_INSTRUCTIONS = (
    "Chỉ tóm tắt mục tiêu, lựa chọn, câu hỏi còn mở. Không giữ giá, trạng thái hay bí mật làm bằng chứng."
)


async def compact_if_needed(
    conversation_id, estimated_tokens: int, budget_tokens: int | None = None, *, summarize=None, estimate=None
) -> bool:
    """Caller truyền bộ ước lượng toàn bộ input và hàm tóm tắt async."""
    settings = get_settings()
    budget = (
        budget_tokens if budget_tokens is not None else settings.context_window_tokens - settings.output_reserved_tokens
    )
    if budget <= 0:
        raise ToolError("CONTEXT_LIMIT")
    if estimated_tokens < settings.compact_ratio * budget:
        return False
    summary, rows = await load_context(conversation_id)
    turn_ids = list(dict.fromkeys(row["turn_id"] for row in rows))
    recent = set(turn_ids[-KEEP_RECENT_TURNS:])
    cutoff = next((row["seq"] for row in rows if row["turn_id"] in recent), 0)
    old = [row for row in rows if row["seq"] < cutoff]
    if not old or summarize is None or estimate is None:
        raise ToolError("CONTEXT_LIMIT")
    content = await summarize(SUMMARY_INSTRUCTIONS, summary["content"] if summary else None, old)
    if not isinstance(content, str) or not content.strip():
        raise ToolError("INVALID_TOOL_RESULT")
    await save_summary(conversation_id, old[-1]["seq"], content)
    if await estimate() >= settings.compact_ratio * budget:
        raise ToolError("CONTEXT_LIMIT")
    return True
