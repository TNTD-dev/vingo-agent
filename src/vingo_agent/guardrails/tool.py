"""G4/G8 dùng chung, được factory gắn tự động vào mọi tool."""

import inspect
import json
import logging
from typing import get_type_hints

from pydantic import ConfigDict, ValidationError, create_model
from pydantic_ai import Tool
from pydantic_ai.exceptions import UsageLimitExceeded

from vingo_agent.config import get_settings
from vingo_agent.observability.logging import denied, log_event
from vingo_agent.tools.base import ToolError, TurnDeps, fail

# Mã do guardrail chặn (đếm vào metric "An toàn"); mã nghiệp vụ từ backend như SEAT_TAKEN thì không.
GUARDRAIL_CODES = frozenset(
    {
        "INVALID_ARGUMENT",
        "INVALID_REFERENCE",
        "INVALID_TOOL_RESULT",
        "ACTION_NOT_PENDING",
        "FORM_REQUIRED",
        "CONFIRM_PHRASE_REQUIRED",
        "PROPOSAL_ALREADY_CREATED",
    }
)


def before_tool_call(deps: TurnDeps, tool_name: str) -> None:
    """Đếm cả lần gọi bị chặn; giới hạn đề xuất và điều kiện quyết định trong turn."""
    deps.tool_calls += 1
    if deps.tool_calls > get_settings().max_tool_calls:
        denied("LIMIT_EXCEEDED")
        raise UsageLimitExceeded("LIMIT_EXCEEDED")
    if tool_name.startswith("propose_") and deps.proposals_created:
        # Không dùng LIMIT_EXCEEDED: mã đó nghĩa là turn bị dừng (mục 5.6), còn ở đây turn vẫn chạy tiếp.
        raise ToolError("PROPOSAL_ALREADY_CREATED")
    if tool_name in {"confirm_pending_action", "reject_pending_action"} and (
        deps.tool_calls != 1 or deps.visible_action is None
    ):
        raise ToolError("ACTION_NOT_PENDING")


def guarded_tool(name, fn):
    """Schema provider strict; validate JSON strict tại chỗ để UUID/thời gian vẫn nhận chuỗi JSON.

    Dùng envelope thay retry prompt để lỗi không chứa tham số thô của ValidationError.
    Tool chạy tuần tự để hai đề xuất cùng response không vượt qua bộ đếm.
    """
    signature = inspect.signature(fn)
    hints = get_type_hints(fn, include_extras=True)
    fields = {
        key: (hints[key], ... if param.default is inspect.Parameter.empty else param.default)
        for key, param in signature.parameters.items()
        if key != "ctx"
    }
    args_type = create_model(f"{name}Args", __config__=ConfigDict(strict=True, extra="forbid"), **fields)

    async def call(ctx, **kwargs):
        try:
            before_tool_call(ctx.deps, name)
            try:
                args = args_type.model_validate_json(json.dumps(kwargs), strict=True)
            except (ValidationError, ValueError, TypeError):
                raise ToolError("INVALID_ARGUMENT") from None
            values = {key: getattr(args, key) for key in fields}
            for key, value in values.items():
                if key.endswith("_ids") and isinstance(value, list) and len(set(value)) != len(value):
                    raise ToolError("INVALID_ARGUMENT")
            result = await fn(ctx, **values) if "ctx" in signature.parameters else await fn(**values)
            if name.startswith("propose_") and isinstance(result, dict) and result.get("ok") is True:
                ctx.deps.proposals_created += 1
            return result
        except ToolError as error:
            if error.code in GUARDRAIL_CODES:
                denied(error.code)
            else:
                log_event(
                    logging.getLogger(__name__), "tool.failed", operation=name, outcome="FAILED", error_code=error.code
                )
            return fail(name, error)

    tool = Tool.from_schema(
        call, name, inspect.getdoc(fn), args_type.model_json_schema(), takes_ctx=True, sequential=True
    )
    tool.strict = True
    return tool
