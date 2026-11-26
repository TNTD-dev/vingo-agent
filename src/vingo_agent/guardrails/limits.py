"""G10 giới hạn turn (V2.2 mục 2): deadline, số model call, số tool call → LIMIT_EXCEEDED, giữ đề xuất đã tạo."""

from vingo_agent.config import Settings


def usage_limits(settings: Settings):
    """Trả pydantic_ai.usage.UsageLimits(request_limit=max_model_calls, tool_calls_limit=max_tool_calls)."""
    raise NotImplementedError
