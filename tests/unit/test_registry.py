from vingo_agent.agents.registry import tools_for
from vingo_agent.config import Settings
from vingo_agent.schemas.chat import Role


def test_buyer_has17Tools_withoutAdminTools():
    tools = tools_for(Role.BUYER)
    assert len(tools) == 17
    assert not any(name.startswith(("get_admin", "admin_", "propose_create_event")) for name in tools)


def test_admin_has18Tools_withSharedSeatmapName():
    tools = tools_for(Role.ADMIN)
    assert len(tools) == 19
    assert tools["get_seatmap"].__name__ == "get_seatmap_admin"


def test_totalDistinctToolNames_is32():
    assert len(set(tools_for(Role.BUYER)) | set(tools_for(Role.ADMIN))) == 33


def test_disabledTools_areNotRegistered(monkeypatch):
    monkeypatch.setenv("AGENT_DISABLED_TOOLS", "propose_hold_seats, confirm_pending_action")
    disabled = Settings().agent_disabled_tools
    tools = tools_for(Role.BUYER, disabled)
    assert "propose_hold_seats" not in tools and "confirm_pending_action" not in tools
    assert len(tools) == 15


def test_proposal_create_venue_isRegistered():
    tools = tools_for(Role.ADMIN)
    assert "propose_create_venue" in tools
