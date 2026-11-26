"""Tool quyết định (V2.3 mục 4.5): xác nhận/từ chối bằng tin nhắn. Không nhận tham số.

Code lấy action_id từ `deps.visible_action`, không từ model. Điều kiện confirm:
1. visible_action trùng đề xuất PENDING duy nhất của hội thoại (đúng version);
2. đây là tool call đầu tiên của turn;
3. đề xuất không cần form (thẻ có form → FORM_REQUIRED).
Sai → ACTION_NOT_PENDING / FORM_REQUIRED, không ghi gì. Đúng → actions.confirm.execute(..., via=TEXT).
"""

from datetime import UTC, datetime

from pydantic_ai import RunContext

from vingo_agent.actions.confirm import ConfirmedVia, confirm, reject
from vingo_agent.persistence import repositories as repo
from vingo_agent.tools.base import ToolError, TurnDeps, ok


async def confirm_pending_action(ctx: RunContext[TurnDeps]) -> dict:
    """Gọi khi người dùng đồng ý rõ ràng với thẻ xác nhận đang hiện ("ok", "chốt", "lấy luôn").
    Phải là tool đầu tiên của lượt. Câu mơ hồ thì hỏi lại, không gọi."""
    deps = ctx.deps
    action = await _visible(deps)
    outcome, blocks = await confirm(
        action_id=action.action_id,
        version=action.version,
        principal=deps.principal,
        via=ConfirmedVia.TEXT,
        backend=deps.backend,
        notices=deps.notices,
    )
    deps.blocks.extend(blocks)
    data = {"status": outcome[0]}
    if outcome[1]:
        data["code"] = outcome[1]
    return ok("confirm_pending_action", data, observed_at=datetime.now(UTC))


async def reject_pending_action(ctx: RunContext[TurnDeps]) -> dict:
    """Gọi khi người dùng từ chối thẻ xác nhận đang hiện ("thôi", "bỏ đi")."""
    deps = ctx.deps
    action = await _visible(deps)
    state = await reject(action_id=action.action_id, version=action.version, principal=deps.principal)
    return ok("reject_pending_action", {"status": state}, observed_at=datetime.now(UTC))


async def _visible(deps):
    action = deps.visible_action
    if action is None or deps.tool_calls != 1:
        raise ToolError("ACTION_NOT_PENDING")
    pending = await repo.get_pending_action(deps.conversation_id)
    if not pending or pending["action_id"] != action.action_id or pending["version"] != action.version:
        raise ToolError("ACTION_NOT_PENDING")
    return action


DECISION_TOOLS = (confirm_pending_action, reject_pending_action)
