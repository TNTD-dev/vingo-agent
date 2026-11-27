import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import respx
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from vingo_agent.agents.factory import build_agent
from vingo_agent.agents.registry import tools_for
from vingo_agent.api.auth import Principal
from vingo_agent.clients.backend import BackendClient
from vingo_agent.config import Settings
from vingo_agent.guardrails.tool import guarded_tool
from vingo_agent.schemas.blocks import AdminTableKind
from vingo_agent.schemas.chat import Role
from vingo_agent.tools import base, proposals
from vingo_agent.tools.base import TurnDeps

URL = "http://backend/api/v1"
VENUE, LAYOUT, ZONE, EVENT, SHOWTIME, TIER, OVERVIEW = (str(UUID(int=i)) for i in range(1, 8))
VENUE_PATH = f"{URL}/admin/venues/{VENUE}"
LAYOUT_PATH = f"{URL}/admin/seat-layouts/{LAYOUT}"
EVENT_PATH = f"{URL}/admin/events/{EVENT}"
SEATMAP_PATH = f"{URL}/showtimes/{SHOWTIME}/seatmap"
PRIVATE = {"svg": "raw-svg", "buyer_email": "private@example.invalid", "qr_token": "private-qr"}


@pytest.fixture
async def admin(monkeypatch):
    settings = Settings(_env_file=None, backend_url=URL, agent_to_backend_key="agent-key")
    client = BackendClient(settings, "admin-session")
    deps = TurnDeps(Principal(uuid4(), Role.ADMIN, "admin-session"), uuid4(), uuid4(), client)
    refs = set()

    async def has_ref(conversation_id, kind, resource_id):
        return conversation_id == deps.conversation_id and (kind, str(resource_id)) in refs

    async def add_refs(conversation_id, kind, resource_ids):
        assert conversation_id == deps.conversation_id
        refs.update((kind, str(resource_id)) for resource_id in resource_ids)

    monkeypatch.setattr(base.repositories, "has_ref", has_ref)
    monkeypatch.setattr(base.repositories, "add_refs", add_refs)
    try:
        yield SimpleNamespace(deps=deps, refs=refs)
    finally:
        await client.aclose()


async def call_tool(ctx, name, **args):
    return await guarded_tool(name, tools_for(Role.ADMIN)[name]).function(ctx, **args)


def assert_read(route, **params):
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer admin-session"
    assert request.headers["X-Vingo-Agent-Key"] == "agent-key"
    assert dict(request.url.params) == {key: str(value) for key, value in params.items()}


def venue():
    return {"id": VENUE, "name": "Sân Demo", "city": "HN", "address": "1 phố Demo", **PRIVATE}


def layout():
    return {"id": LAYOUT, "venue_id": VENUE, "name": "Sơ đồ Demo", "version": 2, "layout_type": "CONCERT", **PRIVATE}


def zone(kind="SEATED"):
    return {"id": ZONE, "code": "VIP", "label": "Khu VIP", "capacity": 80, "kind": kind, **PRIVATE}


def seat(number, status="AVAILABLE", row="A"):
    return {"seat_id": str(UUID(int=100 + number)), "row": row, "number": number, "status": status, **PRIVATE}


def event_summary():
    return {
        "id": EVENT,
        "title": "Demo Concert",
        "category": "CONCERT",
        "status": "PUBLISHED",
        "visible": True,
        **PRIVATE,
    }


def event_detail():
    return event_summary() | {
        "seat_layout_id": LAYOUT,
        "showtimes": [
            {
                "id": SHOWTIME,
                "event_id": EVENT,
                "seat_layout_id": LAYOUT,
                "starts_at": "2030-05-10T19:00:00+07:00",
                "status": "ON_SALE",
            }
        ],
        "price_tiers": [{"id": TIER, "name": "VIP", "price": 500_000, "color": "#1a2b3c", **PRIVATE}],
        "zone_prices": [],
    }


def seatmap(seats, *, showtime_id=SHOWTIME, zone_id=ZONE):
    return {
        "showtime_id": showtime_id,
        "status": "ON_SALE",
        "zones": [{"zone_id": zone_id, "kind": "SEATED", "seats": seats}],
    }


@pytest.mark.parametrize("kind", ["SEATED", "STANDING"])
@respx.mock
async def test_admin_catalog_readsVenueLayoutsAndZone_withScopedRefs(admin, kind):
    venue_list = respx.get(f"{URL}/admin/venues").respond(200, json={"data": [venue()], "next_cursor": "sân-next"})
    venue_detail = respx.get(VENUE_PATH).respond(200, json=venue())
    layouts = respx.get(f"{VENUE_PATH}/seat-layouts").respond(
        200, json={"data": [layout()], "next_cursor": "layout-next"}
    )
    overview = respx.get(LAYOUT_PATH).respond(200, json=layout() | {"zones": [zone(kind)]})
    zone_response = zone(kind)
    seat_id = seat(1)["seat_id"]
    if kind == "SEATED":
        zone_response["rows"] = [{"row_label": "A", "seats": [{"id": seat_id, "number": 1, "x": 12, "y": 25}]}]
    zone_detail = respx.get(f"{LAYOUT_PATH}/zones/{ZONE}").respond(200, json=zone_response)

    listing = await call_tool(admin, "list_venues", q="Sân", cursor="sân-page", limit=3)
    assert listing["data"]["next_cursor"] == "sân-next"
    assert listing["data"]["items"][0]["venue_id"] == VENUE
    assert_read(venue_list, q="Sân", cursor="sân-page", limit=3)
    assert admin.deps.blocks[-1].kind is AdminTableKind.VENUES

    found_venue = await call_tool(admin, "get_admin_venue", venue_id=VENUE)
    assert found_venue["data"] == {"venue_id": VENUE, "name": "Sân Demo", "city": "HN", "address": "1 phố Demo"}
    assert_read(venue_detail)

    versions = await call_tool(admin, "list_seat_layouts", venue_id=VENUE, limit=2)
    assert versions["data"]["items"][0]["layout_id"] == LAYOUT
    assert versions["data"]["next_cursor"] == "layout-next"
    assert_read(layouts, limit=2)
    assert admin.deps.blocks[-1].kind is AdminTableKind.LAYOUTS

    found_layout = await call_tool(admin, "get_seat_layout", layout_id=LAYOUT)
    assert found_layout["data"]["zones"] == [
        {"zone_id": ZONE, "name": "Khu VIP", "code": "VIP", "kind": kind, "capacity": 80}
    ]
    assert_read(overview)

    found_zone = await call_tool(admin, "get_seat_layout_zone", layout_id=LAYOUT, zone_id=ZONE)
    assert found_zone["data"] == found_layout["data"]["zones"][0] | {
        "layout_id": LAYOUT,
        "rows": [{"row_label": "A", "seats": [{"seat_id": seat_id, "number": 1}]}] if kind == "SEATED" else None,
    }
    assert_read(zone_detail)
    expected_refs = {("VENUE", VENUE), ("LAYOUT", LAYOUT), ("ZONE", ZONE)}
    if kind == "SEATED":
        expected_refs.add(("SEAT", seat_id))
    assert admin.refs == expected_refs


@respx.mock
async def test_admin_layout_tree_preserves_version_order_and_records_refs(admin):
    admin.refs.add(("VENUE", VENUE))
    old_id = str(uuid4())
    zone_data = zone() | {"name": "Khu VIP"}
    version = {
        "id": LAYOUT,
        "name": "Sơ đồ Demo",
        "version": 2,
        "status": "PUBLISHED",
        "overview_id": OVERVIEW,
        "overview_name": "Nền v2",
        "overview_status": "PUBLISHED",
        "zones": [zone_data],
        **PRIVATE,
    }
    route = respx.get(f"{VENUE_PATH}/layout-tree").respond(
        200,
        json={
            "venue_id": VENUE,
            "layout_type": "CONCERT",
            "data": [version, version | {"id": old_id, "version": 1}],
            "next_cursor": "next",
        },
    )

    result = await call_tool(
        admin, "get_admin_layout_tree", venue_id=VENUE, layout_type="CONCERT", cursor="page", limit=2
    )

    assert_read(route, layout_type="CONCERT", cursor="page", limit=2)
    data = result["data"]
    assert data["next_cursor"] == "next"
    assert [item["version"] for item in data["items"]] == [2, 1]
    assert data["items"][0]["overview_id"] == OVERVIEW
    assert data["items"][0]["zones"][0]["zone_id"] == ZONE
    assert not PRIVATE.keys() & data["items"][0].keys()
    assert admin.refs == {
        ("VENUE", VENUE),
        ("LAYOUT", LAYOUT),
        ("LAYOUT", old_id),
        ("ZONE", ZONE),
        ("OVERVIEW", OVERVIEW),
    }
    block = admin.deps.blocks[-1]
    assert block.kind is AdminTableKind.LAYOUTS
    assert block.columns == ["layout_id", "name", "version", "status", "overview_name"]
    assert block.rows[0] == [LAYOUT, "Sơ đồ Demo", 2, "PUBLISHED", "Nền v2"]


@respx.mock
async def test_admin_chat_resolves_event_layout_zone_and_seat_refs(admin):
    listing = respx.get(f"{URL}/admin/events").respond(200, json={"data": [event_summary()]})
    event = respx.get(EVENT_PATH).respond(200, json=event_detail())
    layout_route = respx.get(LAYOUT_PATH).respond(200, json=layout() | {"zones": [zone()]})
    statuses = ["AVAILABLE", "HELD", "SOLD", "BLOCKED"]
    seats = [seat(i, status) for i, status in enumerate(statuses, 1)]
    seatmap_route = respx.get(SEATMAP_PATH).respond(200, json=seatmap([*seats, seat(5, row="B")]))

    async def model_response(messages, _info):
        results = [part.content for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        assert all(result["ok"] for result in results)
        assert "private-qr" not in json.dumps(results)
        match len(results):
            case 0:
                part = ToolCallPart("list_admin_events", {"q": "Demo Concert", "status": "PUBLISHED"})
            case 1:
                event_id = results[0]["data"]["items"][0]["event_id"]
                part = ToolCallPart("get_admin_event", {"event_id": event_id})
            case 2:
                showtime = results[1]["data"]["showtimes"][0]
                part = ToolCallPart("get_seat_layout", {"layout_id": showtime["layout_id"]})
            case 3:
                zone_id = results[2]["data"]["zones"][0]["zone_id"]
                showtime_id = results[1]["data"]["showtimes"][0]["showtime_id"]
                part = ToolCallPart("get_seatmap", {"showtime_id": showtime_id, "zone_id": zone_id, "row": " a "})
            case 4:
                data = results[3]["data"]
                assert [seat["status"] for seat in data["seats"]] == statuses
                assert data["available_count"] == 1
                part = TextPart("Đã tra hàng A của khu VIP.")
            case _:
                pytest.fail("Luồng tra catalog gọi tool thừa")
        return ModelResponse(parts=[part])

    agent = build_agent(Role.ADMIN, model=FunctionModel(model_response), disabled_tools=frozenset())
    result = await agent.run("Tra ghế hàng A khu VIP của Demo Concert", deps=admin.deps)

    assert result.output == "Đã tra hàng A của khu VIP."
    assert_read(listing, q="Demo Concert", status="PUBLISHED", limit=10)
    assert_read(event)
    assert_read(layout_route)
    assert_read(seatmap_route, zone_id=ZONE)
    assert admin.deps.blocks[0].kind is AdminTableKind.EVENTS
    block = admin.deps.blocks[-1]
    assert block.type == "SeatMap"
    assert [item.number for item in block.seats] == ["1", "2", "3", "4"]
    assert [item.status for item in block.seats] == statuses
    assert admin.refs == {
        ("EVENT", EVENT),
        ("LAYOUT", LAYOUT),
        ("SHOWTIME", SHOWTIME),
        ("ZONE", ZONE),
        ("PRICE_TIER", TIER),
    } | {("SEAT", item["seat_id"]) for item in seats}


@respx.mock
async def test_admin_zone_price_preview_posts_only_the_assignment_array(admin):
    admin.refs.update({("EVENT", EVENT), ("ZONE", ZONE), ("PRICE_TIER", TIER)})
    event = respx.get(EVENT_PATH).respond(200, json=event_detail())
    route = respx.post(f"{EVENT_PATH}/zone-prices/preview").respond(
        200,
        json={
            "event_id": EVENT,
            "total_sku_count": 80,
            "zones": [{"zone_id": ZONE, "zone_name": "VIP", "sku_count": 80, "ticket_type_id": TIER, **PRIVATE}],
        },
    )
    assignments = [{"zone_id": ZONE, "price_tier_id": TIER, "display_name": "VIP"}]

    result = await call_tool(admin, "get_zone_prices_preview", event_id=EVENT, assignments=assignments)

    assert_read(event)
    assert_read(route)
    assert json.loads(route.calls.last.request.content) == assignments
    data = {"event_id": EVENT, "total_sku_count": 80, "zones": [{"zone_id": ZONE, "zone_name": "VIP", "sku_count": 80}]}
    assert result["data"] == data
    assert admin.deps.zone_price_preview == (EVENT, assignments, LAYOUT, data)


@respx.mock
async def test_admin_seatmap_caps_rows_and_grants_refs_for_returned_seats(admin):
    admin.refs.update({("SHOWTIME", SHOWTIME), ("ZONE", ZONE)})
    seats = [seat(i, "SOLD" if i <= 60 else "AVAILABLE") for i in range(1, 64)]
    respx.get(SEATMAP_PATH).respond(200, json=seatmap(seats))

    result = await call_tool(admin, "get_seatmap", showtime_id=SHOWTIME, zone_id=ZONE)

    assert result["meta"]["truncated"] is True
    assert result["data"]["available_count"] == 3
    assert len(result["data"]["seats"]) == 60
    assert {ref_id for kind, ref_id in admin.refs if kind == "SEAT"} == {item["seat_id"] for item in seats[:60]}


@respx.mock
async def test_admin_seatmap_reads_standing_stock_without_seat_refs(admin):
    admin.refs.update({("SHOWTIME", SHOWTIME), ("ZONE", ZONE)})
    raw = seatmap([]) | {"zones": [{"zone_id": ZONE, "kind": "STANDING", "seats_left": 13}]}
    respx.get(SEATMAP_PATH).respond(200, json=raw)

    data = (await call_tool(admin, "get_seatmap", showtime_id=SHOWTIME, zone_id=ZONE))["data"]

    assert data == {"showtime_id": SHOWTIME, "zone_id": ZONE, "kind": "STANDING", "available_count": 13, "seats": []}
    assert admin.deps.blocks[-1].available_count == 13
    assert ("SEAT", seat(1)["seat_id"]) not in admin.refs


@respx.mock
async def test_admin_catalog_ref_guards_reject_unknown_zone_and_mismatched_responses(admin):
    admin.refs.update({("VENUE", VENUE), ("LAYOUT", LAYOUT), ("SHOWTIME", SHOWTIME)})
    extra_id = str(uuid4())
    route = respx.get(f"{LAYOUT_PATH}/zones/{ZONE}").respond(200, json=zone() | {"id": extra_id, "rows": []})
    respx.get(SEATMAP_PATH).respond(200, json=seatmap([seat(1)], showtime_id=extra_id))
    respx.get(f"{VENUE_PATH}/layout-tree").respond(200, json={"venue_id": VENUE, "layout_type": "FOOTBALL", "data": []})

    args = {"layout_id": LAYOUT, "zone_id": ZONE}
    assert (await call_tool(admin, "get_seat_layout_zone", **args))["error"]["code"] == "INVALID_REFERENCE"
    assert not route.called
    admin.refs.add(("ZONE", ZONE))
    original_refs = admin.refs.copy()
    assert (await call_tool(admin, "get_seat_layout_zone", **args))["error"]["code"] == "INVALID_TOOL_RESULT"
    assert (await call_tool(admin, "get_seatmap", showtime_id=SHOWTIME, zone_id=ZONE))["error"][
        "code"
    ] == "INVALID_TOOL_RESULT"
    assert (await call_tool(admin, "get_admin_layout_tree", venue_id=VENUE, layout_type="CONCERT"))["error"][
        "code"
    ] == "INVALID_TOOL_RESULT"
    assert admin.refs == original_refs
    assert admin.deps.blocks == []


@pytest.mark.parametrize(
    ("tool", "args", "operation", "body"),
    [
        pytest.param(
            "propose_update_showtime",
            {"showtime_id": SHOWTIME, "ends_at": "2030-05-10T22:00:00+07:00"},
            "UPDATE_SHOWTIME",
            {"ends_at": "2030-05-10T15:00:00+00:00"},
            id="showtime",
        ),
        pytest.param(
            "propose_update_price_tiers",
            {"tiers": [{"id": TIER, "name": "VIP Plus", "price": 600_000}]},
            "UPDATE_PRICE_TIERS",
            [{"id": TIER, "name": "VIP Plus", "price": 600_000, "color": "#1a2b3c"}],
            id="tiers",
        ),
        pytest.param(
            "propose_add_tier_price",
            {"tier_id": TIER, "price": 600_000},
            "ADD_TIER_PRICE",
            {"price": 600_000},
            id="price",
        ),
    ],
)
@respx.mock
async def test_admin_event_refs_enable_proposal_without_business_write(admin, monkeypatch, tool, args, operation, body):
    admin.refs.add(("EVENT", EVENT))
    before = event_detail() | {"status": "DRAFT", "visible": False}
    before["showtimes"][0].update(
        status="DRAFT",
        sale_opens_at="2030-05-01T10:00:00+07:00",
        sale_closes_at="2030-05-10T18:30:00+07:00",
        ends_at="2030-05-10T21:00:00+07:00",
    )
    before["price_tiers"][0]["prices"] = []
    route = respx.get(EVENT_PATH).respond(200, json=before)
    create = AsyncMock(return_value=(uuid4(), 1))
    monkeypatch.setattr(proposals.pending, "create", create)

    assert (await call_tool(admin, "get_admin_event", event_id=EVENT))["ok"]
    result = await call_tool(admin, tool, event_id=EVENT, **args)

    assert result["data"]["status"] == "PROPOSED"
    assert create.await_args.kwargs["operation"] == operation
    payload = create.await_args.kwargs["payload"]
    assert payload["body"] == body
    assert payload["before"]
    assert route.call_count == 2
    assert len(respx.calls) == 2
