"""G10 giới hạn lời gọi trong một turn."""

from pydantic_ai.usage import UsageLimits

from vingo_agent.config import Settings


def usage_limits(settings: Settings):
    return UsageLimits(request_limit=settings.max_model_calls, tool_calls_limit=settings.max_tool_calls)
