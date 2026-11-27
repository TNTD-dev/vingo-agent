"""Trước khi lọc AGENT_DISABLED_TOOLS, Buyer có 17 tool (11 đọc), Admin có 19. Hai role có 33 tên riêng biệt."""

from collections.abc import Callable

from vingo_agent.schemas.chat import Role
from vingo_agent.tools.admin_read import ADMIN_READ_TOOLS, get_seatmap_admin
from vingo_agent.tools.buyer_read import BUYER_READ_TOOLS
from vingo_agent.tools.decisions import DECISION_TOOLS
from vingo_agent.tools.proposals import ADMIN_PROPOSAL_TOOLS, BUYER_PROPOSAL_TOOLS


def tools_for(role: Role, disabled: frozenset[str] = frozenset()) -> dict[str, Callable]:
    """Tên tool (như model thấy) → hàm. Admin dùng get_seatmap_admin dưới tên get_seatmap."""
    if role is Role.BUYER:
        tools = {f.__name__: f for f in (*BUYER_READ_TOOLS, *BUYER_PROPOSAL_TOOLS, *DECISION_TOOLS)}
    else:
        tools = {f.__name__: f for f in (*ADMIN_READ_TOOLS, *ADMIN_PROPOSAL_TOOLS, *DECISION_TOOLS)}
        tools["get_seatmap"] = get_seatmap_admin
    return {name: fn for name, fn in tools.items() if name not in disabled}
