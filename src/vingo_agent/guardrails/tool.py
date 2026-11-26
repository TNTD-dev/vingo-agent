"""G4 schema, G5 quyền + Ref, G6 outbound (V2.3 mục 5.4, 5.5).

G4 do PydanticAI validate theo chữ ký tool (extra=forbid, strict). G5: registry theo role (agents/registry.py)
+ tools.base.require_ref. G6: clients.backend.OPERATIONS là danh sách path duy nhất được gọi.
"""

from vingo_agent.tools.base import TurnDeps


def before_tool_call(deps: TurnDeps, tool_name: str) -> None:
    """Đếm tool call (G10), chặn đề xuất thứ hai trong turn, chặn confirm không phải tool đầu."""
    raise NotImplementedError
