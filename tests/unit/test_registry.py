from pydantic_ai import Tool

from vingo_agent.agents.registry import tools_for
from vingo_agent.config import Settings
from vingo_agent.schemas.chat import Role
from vingo_agent.tools.buyer_read import BUYER_READ_TOOLS


def test_buyer_has17Tools_withoutAdminTools():
    tools = tools_for(Role.BUYER)
    assert len(tools) == 17
    assert len(BUYER_READ_TOOLS) == 11
    assert "suggest_seats" in tools and "get_ticket" not in tools
    assert not any(name.startswith(("get_admin", "admin_", "propose_create_event")) for name in tools)


def test_admin_has31Tools_withSharedSeatmapName():
    tools = tools_for(Role.ADMIN)
    assert len(tools) == 31
    assert tools["get_seatmap"].__name__ == "get_seatmap_admin"


def test_totalDistinctToolNames_is45():
    buyer, admin = tools_for(Role.BUYER).keys(), tools_for(Role.ADMIN).keys()
    assert buyer & admin == {"get_seatmap", "confirm_pending_action", "reject_pending_action"}
    assert len(buyer | admin) == 45


def test_suggestSeats_isEnabledByDefault(monkeypatch):
    monkeypatch.delenv("AGENT_DISABLED_TOOLS", raising=False)
    tools = tools_for(Role.BUYER, Settings(_env_file=None).agent_disabled_tools)
    assert "suggest_seats" in tools
    assert len(tools) == 17


def test_buyerSearchAndSeatSchemas_matchModelInputs():
    tools = tools_for(Role.BUYER)
    search = Tool(tools["search_events"]).tool_def.parameters_json_schema["properties"]
    assert set(search) == {"q", "category", "city", "from", "to", "price_min", "price_max", "sort", "cursor", "limit"}
    seats = Tool(tools["suggest_seats"]).tool_def.parameters_json_schema
    assert set(seats["required"]) == {"showtime_id", "zone_id", "quantity"}
    quantity = seats["properties"]["quantity"]
    assert (quantity["minimum"], quantity["maximum"]) == (1, 10)


def test_disabledTools_areNotRegistered(monkeypatch):
    monkeypatch.setenv("AGENT_DISABLED_TOOLS", "propose_hold_seats, confirm_pending_action")
    disabled = Settings().agent_disabled_tools
    tools = tools_for(Role.BUYER, disabled)
    assert "propose_hold_seats" not in tools and "confirm_pending_action" not in tools
    assert len(tools) == 15


def test_proposal_create_venue_isRegistered():
    tools = tools_for(Role.ADMIN)
    assert "propose_create_venue" in tools


def test_proposal_update_venue_isRegistered():
    tools = tools_for(Role.ADMIN)
    assert "propose_update_venue" in tools
