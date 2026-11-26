"""Tool quyết định (V2.3 mục 4.5): xác nhận/từ chối bằng tin nhắn. Không nhận tham số.

Code lấy action_id từ `deps.visible_action`, không từ model. Điều kiện confirm:
1. visible_action trùng đề xuất PENDING duy nhất của hội thoại (đúng version);
2. đây là tool call đầu tiên của turn;
3. đề xuất không cần form (thẻ có form → FORM_REQUIRED).
Sai → ACTION_NOT_PENDING / FORM_REQUIRED, không ghi gì. Đúng → actions.confirm.execute(..., via=TEXT).
"""

from pydantic_ai import RunContext

from vingo_agent.tools.base import TurnDeps


async def confirm_pending_action(ctx: RunContext[TurnDeps]) -> dict:
    """Gọi khi người dùng đồng ý rõ ràng với thẻ xác nhận đang hiện ("ok", "chốt", "lấy luôn").
    Phải là tool đầu tiên của lượt. Câu mơ hồ thì hỏi lại, không gọi."""
    raise NotImplementedError


async def reject_pending_action(ctx: RunContext[TurnDeps]) -> dict:
    """Gọi khi người dùng từ chối thẻ xác nhận đang hiện ("thôi", "bỏ đi")."""
    raise NotImplementedError


DECISION_TOOLS = (confirm_pending_action, reject_pending_action)
