"""KAN-204: đối chiếu HTTP, projection, thẻ, Ref và hồi quy ranh giới các role."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import respx
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from vingo_agent.agents import orchestrator as o
from vingo_agent.agents.orchestrator import amounts_in
from vingo_agent.agents.registry import tools_for
from vingo_agent.api.auth import Principal
from vingo_agent.clients.backend import BackendClient
from vingo_agent.config import Settings
from vingo_agent.guardrails.output import Evidence, check_reply
from vingo_agent.guardrails.tool import guarded_tool
from vingo_agent.schemas.blocks import StatCards
from vingo_agent.schemas.chat import Role
from vingo_agent.tools import admin_read
from vingo_agent.tools.admin_read import get_admin_dashboard, list_metrics, query_metric
from vingo_agent.tools.base import ToolError, TurnDeps
from vingo_agent.tools.blocks import build_block
from vingo_agent.tools.projections import SPECS, project

START = datetime.fromisoformat("2026-11-01T00:00:00+07:00")
END = datetime.fromisoformat("2026-12-01T00:00:00+07:00")
COMPUTED = "2026-11-26T09:00:00+00:00"
WINDOW = {"from": START.isoformat(), "to": END.isoformat()}


def metric_result(metric="revenue", value=5_000_000, *, group_by="none", **filters):
    unit = {"revenue": "VND", "tickets_sold": "tickets", "fill_rate": "percent", "checkins": "checkins"}[metric]
    return {
        "metric": metric,
        "unit": unit,
        "computed_at": COMPUTED,
        "filters": {**WINDOW, "group_by": group_by, **filters},
        "value": value,
    }


def dashboard_result(group_by="day"):
    key = "2026-11-01" if group_by == "day" else "2026-11"
    revenue = metric_result(group_by=group_by)
    tickets = metric_result("tickets_sold", 20, group_by=group_by)
    revenue.pop("value")
    tickets.pop("value")
    revenue["series"] = [{"key": key, "value": 300_000}]
    tickets["series"] = [{"key": key, "value": 3}]
    return {
        **WINDOW,
        "timezone": "Asia/Ho_Chi_Minh",
        "computed_at": COMPUTED,
        "current": {"revenue": 5_000_000, "tickets_sold": 20, "new_users": 1},
        "previous": {"revenue": 4_000_000, "tickets_sold": 18, "new_users": 0},
        "on_sale_showtimes": 2,
        "revenue": revenue,
        "tickets": tickets,
    }


@pytest.fixture
def ctx():
    deps = TurnDeps(Principal(uuid4(), Role.ADMIN, "session-secret"), uuid4(), uuid4(), AsyncMock())
    return SimpleNamespace(deps=deps)


@pytest.mark.parametrize(
    "metric,value",
    [("revenue", 5_000_000), ("tickets_sold", 20), ("fill_rate", 42.86), ("checkins", 8)],
)
@respx.mock
async def test_metric_usesBackendValueAndMetadata_withExactQueryAndAuth(ctx, metric, value):
    route = respx.get("http://backend/api/v1/admin/reports/metrics").respond(200, json=metric_result(metric, value))
    client = BackendClient(Settings(backend_url="http://backend/api/v1", agent_to_backend_key="agent-key"), "session")
    ctx.deps.backend = client
    try:
        result = await query_metric(ctx, metric, START, END)
    finally:
        await client.aclose()
    assert result["ok"] is True and result["data"]["value"] == value
    assert result["data"] == {
        "metric": metric,
        "unit": metric_result(metric, value)["unit"],
        **WINDOW,
        "timezone": "Asia/Ho_Chi_Minh",
        "computed_at": COMPUTED,
        "value": value,
    }
    assert route.call_count == 1
    request = route.calls[0].request
    assert dict(request.url.params) == {**WINDOW, "metric": metric, "group_by": "none"}
    assert request.headers["Authorization"] == "Bearer session"
    assert request.headers["X-Vingo-Agent-Key"] == "agent-key"
    ctx.deps.backend = AsyncMock()


@pytest.mark.parametrize("group_by", ["day", "month"])
@respx.mock
async def test_dashboard_keepsBackendTotalsAndBothSeries_withoutSummingPoints(ctx, group_by):
    raw = dashboard_result(group_by)
    route = respx.get("http://backend/api/v1/admin/dashboard").respond(200, json=raw)
    client = BackendClient(Settings(backend_url="http://backend/api/v1"), "session")
    ctx.deps.backend = client
    try:
        result = await get_admin_dashboard(ctx, START, END, group_by)
    finally:
        await client.aclose()
    assert result["ok"] is True
    assert result["data"]["current"] == raw["current"]
    assert result["data"]["previous"] == raw["previous"]
    assert result["data"]["on_sale_showtimes"] == 2
    assert result["data"]["revenue"]["series"][0]["value"] == 300_000
    assert result["data"]["tickets"]["series"][0]["value"] == 3
    assert dict(route.calls[0].request.url.params) == {**WINDOW, "group_by": group_by}
    block = build_block("getAdminDashboard", result["data"], role="ADMIN").model_dump(mode="json")
    assert block["value"] == 5_000_000 and block["series"][0]["value"] == 300_000
    assert block["metric"] == "revenue" and block["unit"] == "VND"
    assert block["from"] == WINDOW["from"] and "from_" not in block
    assert block["computed_at"] == datetime.fromisoformat(COMPUTED).isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize(
    "start,end",
    [
        (START.replace(tzinfo=None), END),
        (START, END.replace(tzinfo=None)),
        (START, START),
        (END, START),
        (START, START + timedelta(days=366, microseconds=1)),
    ],
)
async def test_invalidWindow_neverCallsBackend(ctx, start, end):
    for tool, args in (
        (get_admin_dashboard, (start, end)),
        (query_metric, ("revenue", start, end)),
        (list_metrics, (start, end)),
    ):
        result = await tool(ctx, *args)
        assert result["error"]["code"] == "INVALID_ARGUMENT"
    ctx.deps.backend.read.assert_not_called()


async def test_window_acceptsExactly366Days_byInstant(ctx):
    end = START + timedelta(days=366)
    raw = metric_result()
    raw["filters"]["to"] = end.astimezone(UTC).isoformat()
    ctx.deps.backend.read.return_value = raw
    result = await query_metric(ctx, "revenue", START, end)
    assert result["ok"] is True and result["data"]["to"] == end.isoformat()


@pytest.mark.parametrize("tool", [get_admin_dashboard, query_metric, list_metrics])
async def test_buyer_cannotCallReportingDirectly(ctx, tool):
    ctx.deps.principal = Principal(uuid4(), Role.BUYER, "session-secret")
    args = ("revenue", START, END) if tool is query_metric else (START, END)
    assert (await tool(ctx, *args))["error"]["code"] == "FORBIDDEN"
    ctx.deps.backend.read.assert_not_called()


async def test_metric_checksBothRefs_beforeHttp(ctx, monkeypatch):
    event_id, showtime_id = uuid4(), uuid4()
    refs = AsyncMock(return_value=True)
    monkeypatch.setattr("vingo_agent.tools.base.repositories.has_ref", refs)
    ctx.deps.backend.read.return_value = metric_result(event_id=str(event_id), showtime_id=str(showtime_id))
    result = await query_metric(ctx, "revenue", START, END, event_id, showtime_id)
    assert result["ok"] is True
    assert [call.args for call in refs.await_args_list] == [
        (ctx.deps.conversation_id, "EVENT", event_id),
        (ctx.deps.conversation_id, "SHOWTIME", showtime_id),
    ]
    query = ctx.deps.backend.read.call_args.kwargs["query"]
    assert query["event_id"] == str(event_id) and query["showtime_id"] == str(showtime_id)


async def test_metric_missingRef_neverCallsBackend(ctx, monkeypatch):
    monkeypatch.setattr("vingo_agent.tools.base.repositories.has_ref", AsyncMock(return_value=False))
    result = await query_metric(ctx, "revenue", START, END, event_id=uuid4())
    assert result["error"]["code"] == "INVALID_REFERENCE"
    ctx.deps.backend.read.assert_not_called()


@pytest.mark.parametrize("value", [True, "5000000", None, -1, 0.5, float("nan"), float("inf")])
async def test_metric_invalidRevenue_isRejectedWithoutBlock(ctx, value):
    ctx.deps.backend.read.return_value = metric_result(value=value)
    result = await query_metric(ctx, "revenue", START, END)
    assert result["error"]["code"] == "INVALID_TOOL_RESULT"
    assert ctx.deps.blocks == [] and amounts_in(result) == set()


@pytest.mark.parametrize(
    "field,value",
    [("metric", "conversion"), ("unit", "percent"), ("computed_at", None), ("computed_at", "2026-11-26T09:00:00")],
)
async def test_metric_invalidMetadata_isRejected(ctx, field, value):
    raw = metric_result()
    raw[field] = value
    ctx.deps.backend.read.return_value = raw
    assert (await query_metric(ctx, "revenue", START, END))["error"]["code"] == "INVALID_TOOL_RESULT"


@pytest.mark.parametrize(
    "field,value",
    [("from", "2026-11-02T00:00:00+07:00"), ("group_by", "day"), ("event_id", str(uuid4()))],
)
async def test_metric_unexpectedBackendScope_isRejected(ctx, field, value):
    raw = metric_result()
    raw["filters"][field] = value
    ctx.deps.backend.read.return_value = raw
    assert (await query_metric(ctx, "revenue", START, END))["error"]["code"] == "INVALID_TOOL_RESULT"


@pytest.mark.parametrize("metric,value", [("revenue", 0), ("tickets_sold", 0), ("fill_rate", 0.0), ("checkins", 0)])
async def test_metric_zero_isReturnedAsMeasuredValue(ctx, metric, value):
    ctx.deps.backend.read.return_value = metric_result(metric, value)
    result = await query_metric(ctx, metric, START, END)
    assert result["ok"] is True and result["data"]["value"] == 0


async def test_projection_removesUnknownFieldsAtEveryLevel_withoutMutatingBackend(ctx):
    raw = dashboard_result()
    raw["email"] = raw["upcoming"] = "secret@example.test"
    raw["current"]["buyer_phone"] = "0901234567"
    raw["revenue"]["payment_url"] = "https://secret.test"
    raw["revenue"]["series"][0].update(label="secret@example.test", qr_token="secret-qr")
    original = deepcopy(raw)
    ctx.deps.backend.read.return_value = raw
    result = await get_admin_dashboard(ctx, START, END)
    assert result["ok"] is True and raw == original
    secrets = ("secret@example.test", "0901234567", "secret.test", "secret-qr")
    assert not any(secret in repr(result) for secret in secrets)
    assert "upcoming" not in result["data"]


@pytest.mark.parametrize("key", ["2026-13-01", "2026-11-31", "secret@example.test", None])
async def test_dashboard_invalidBucket_isRejected(ctx, key):
    raw = dashboard_result()
    raw["revenue"]["series"][0]["key"] = key
    ctx.deps.backend.read.return_value = raw
    assert (await get_admin_dashboard(ctx, START, END))["error"]["code"] == "INVALID_TOOL_RESULT"


@pytest.mark.parametrize("field,value", [("timezone", "UTC"), ("from", "2026-11-02T00:00:00+07:00")])
async def test_dashboard_mismatchedMetadata_isRejected(ctx, field, value):
    raw = dashboard_result()
    raw[field] = value
    ctx.deps.backend.read.return_value = raw
    assert (await get_admin_dashboard(ctx, START, END))["error"]["code"] == "INVALID_TOOL_RESULT"


async def test_dashboard_emptySeries_isNotReplacedWithInventedValues(ctx):
    raw = dashboard_result()
    raw["revenue"]["series"] = raw["tickets"]["series"] = []
    ctx.deps.backend.read.return_value = raw
    result = await get_admin_dashboard(ctx, START, END)
    assert result["ok"] is True
    assert build_block("getAdminDashboard", result["data"], role="ADMIN").series == []


async def test_listMetrics_isLocalFreshWithWindowAndExplicitCatalogTimestamp(ctx):
    before = datetime.now(UTC)
    result = await list_metrics(ctx, START, END)
    after = datetime.now(UTC)
    metrics = result["data"]["metrics"]
    assert {item["metric"] for item in metrics} == {"revenue", "tickets_sold", "fill_rate", "checkins"}
    assert all(item["unit"] and item["definition"] and item["time_basis"] for item in metrics)
    assert result["data"]["from"] == WINDOW["from"] and result["data"]["to"] == WINDOW["to"]
    assert result["data"]["timezone"] == "Asia/Ho_Chi_Minh"
    assert before <= datetime.fromisoformat(result["data"]["computed_at"]) <= after
    assert result["data"]["computed_at_basis"] == "catalog_generated_at"
    assert result["meta"]["observed_at"] == result["data"]["computed_at"]
    assert "value" not in result["data"] and "series" not in result["data"]
    assert build_block("list_metrics", result["data"], role="ADMIN") is None
    metrics[0]["definition"] = "mutated"
    assert (await list_metrics(ctx, START, END))["data"]["metrics"][0]["definition"] != "mutated"
    ctx.deps.backend.read.assert_not_called()


async def test_reporting_preservesBackendErrorCode_withoutRawMessage(ctx):
    ctx.deps.backend.read.side_effect = ToolError("BACKEND_UNAVAILABLE", retryable=True)
    result = await query_metric(ctx, "revenue", START, END)
    assert result["error"] == {"code": "BACKEND_UNAVAILABLE", "retryable": True}


async def test_evidence_acceptsOnlyReportingRevenue_andPreservesExistingBuyerAmounts(ctx):
    ctx.deps.backend.read.return_value = dashboard_result()
    result = await get_admin_dashboard(ctx, START, END)
    amounts = amounts_in(result)
    assert amounts == {5_000_000, 4_000_000, 300_000}
    assert check_reply("Doanh thu 5.000.000 VND", Evidence(frozenset(), frozenset(amounts))) == []
    assert check_reply("Doanh thu 20 VND", Evidence(frozenset(), frozenset(amounts)))
    assert amounts_in({"total_amount": 50_000, "items": [{"price": 25_000}]}) == {50_000, 25_000}
    for metric, value in (("tickets_sold", 20), ("checkins", 8), ("fill_rate", 40)):
        ctx.deps.backend.read.return_value = metric_result(metric, value)
        assert amounts_in(await query_metric(ctx, metric, START, END)) == set()
    assert amounts_in({"meta": None, "amount": 123}) == {123}


async def test_metric_card_preservesIntegerVndAndWireFieldNames(ctx):
    amount = 2**53 + 1
    ctx.deps.backend.read.return_value = metric_result(value=amount)
    result = await query_metric(ctx, "revenue", START, END)
    card = build_block("queryMetric", result["data"], role="ADMIN")
    dumped = card.model_dump(mode="json")
    assert type(dumped["value"]) is int and dumped["value"] == amount
    assert dumped["from"] == WINDOW["from"] and "from_" not in dumped
    # Giữ tương thích caller cũ đang truyền from_ trong Python.
    legacy = {**result["data"], "from_": START}
    legacy.pop("from")
    assert StatCards(**legacy).from_ == START


def test_reporting_registration_doesNotChangeBuyerCatalogOrKillSwitch():
    admin, buyer = tools_for(Role.ADMIN), tools_for(Role.BUYER)
    names = {"get_admin_dashboard", "query_metric", "list_metrics"}
    assert names <= admin.keys() and not names & buyer.keys()
    assert not names & tools_for(Role.ADMIN, frozenset(names)).keys()
    assert admin_read.get_admin_dashboard is get_admin_dashboard
    assert admin["list_venues"] is admin_read.list_venues
    assert admin["get_seatmap"] is admin_read.get_seatmap_admin
    assert len(buyer) == 17


def test_projection_otherOperations_keepExistingDispatch(monkeypatch):
    from vingo_agent.guardrails.result import FieldSpec

    monkeypatch.setitem(SPECS, "sample", {"total": FieldSpec(int)})
    assert project("sample", {"total": 25, "email": "secret"}, role="BUYER") == ({"total": 25}, False)
    with pytest.raises(ToolError, match="INVALID_TOOL_RESULT"):
        project("queryMetric", metric_result(), role="ADMIN")
    with pytest.raises(ToolError, match="FORBIDDEN"):
        project("queryMetric", metric_result(), role="BUYER", requested={**WINDOW, "metric": "revenue"})


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_admin_dashboard", {"group_by": "hour"}),
        ("query_metric", {"metric": "conversion"}),
        ("query_metric", {"metric": "revenue", "user_id": str(uuid4())}),
        ("query_metric", {"metric": "revenue", "group_by": "day"}),
        ("query_metric", {"metric": "revenue", "time_from": None}),
        ("list_metrics", {"time_from": None}),
        ("list_metrics", {"group_by": "day"}),
    ],
)
async def test_providerSchema_invalidArguments_neverReachHttp(ctx, name, args):
    def respond(messages, info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            arguments = {"time_from": WINDOW["from"], "time_to": WINDOW["to"], **args}
            return ModelResponse(parts=[ToolCallPart(name, arguments)])
        return ModelResponse(parts=[TextPart("Tham số chưa hợp lệ.")])

    tool = guarded_tool(name, tools_for(Role.ADMIN)[name])
    result = await Agent(FunctionModel(respond), deps_type=TurnDeps, tools=[tool]).run("test", deps=ctx.deps)
    returns = [
        part.content for message in result.new_messages() for part in message.parts if isinstance(part, ToolReturnPart)
    ]
    assert returns[0]["error"]["code"] == "INVALID_ARGUMENT"
    ctx.deps.backend.read.assert_not_called()


async def test_listMetrics_registeredToolRequiresWindow_andReturnsMetadataWithoutBackend(ctx):
    calls = 0

    def respond(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("list_metrics", {})])
        if calls == 2:
            return ModelResponse(
                parts=[ToolCallPart("list_metrics", {"time_from": WINDOW["from"], "time_to": WINDOW["to"]})]
            )
        return ModelResponse(parts=[TextPart("Danh mục cho khoảng đã chọn.")])

    tool = guarded_tool("list_metrics", tools_for(Role.ADMIN)["list_metrics"])
    result = await Agent(FunctionModel(respond), deps_type=TurnDeps, tools=[tool]).run("Danh mục metric", deps=ctx.deps)
    returned = [p.content for m in result.new_messages() for p in m.parts if isinstance(p, ToolReturnPart)]
    assert returned[0]["error"]["code"] == "INVALID_ARGUMENT"
    data = returned[1]["data"]
    assert data["from"] == WINDOW["from"] and data["to"] == WINDOW["to"]
    assert data["timezone"] == "Asia/Ho_Chi_Minh"
    assert datetime.fromisoformat(data["computed_at"]).utcoffset() is not None
    assert data["computed_at_basis"] == "catalog_generated_at"
    ctx.deps.backend.read.assert_not_called()


@pytest.mark.parametrize("name", ["get_admin_dashboard", "query_metric"])
@pytest.mark.parametrize("valid", [True, False])
@respx.mock
async def test_reporting_turn_usesRealRegistryProjectionAndSse_withoutDatabaseOrModelApi(monkeypatch, name, valid):
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models.function import DeltaToolCall

    from vingo_agent.schemas.chat import SendMessageRequest

    principal = Principal(uuid4(), Role.ADMIN, "session-secret")
    cid, tid = uuid4(), uuid4()
    values = {
        "get_conversation": {"owner_id": principal.user_id, "role": "ADMIN"},
        "create_turn": {"id": tid, "state": "RUNNING"},
        "acquire_turn": True,
        "get_pending_action": None,
        "load_context": (None, []),
        "append_messages": None,
        "transition_turn": True,
        "release_turn": None,
        "set_model_version": None,
        "get_last_input_usage": None,
        "set_input_usage": None,
    }
    mocks = {}
    for key, value in values.items():
        mocks[key] = AsyncMock(return_value=value)
        monkeypatch.setattr(o.repo, key, mocks[key])
    monkeypatch.setattr(o, "build_context", AsyncMock(return_value=[ModelRequest(parts=[UserPromptPart("Báo cáo")])]))
    monkeypatch.setattr(o, "compact_if_needed", AsyncMock())
    raw = dashboard_result() if name == "get_admin_dashboard" else metric_result()
    if not valid:
        raw.pop("computed_at")
    path = "/admin/dashboard" if name == "get_admin_dashboard" else "/admin/reports/metrics"
    route = respx.get("http://backend/api/v1" + path).respond(200, json=raw)
    client = BackendClient(Settings(backend_url="http://backend/api/v1"), principal.session_token)
    monkeypatch.setattr(o, "BackendClient", lambda *args: client)

    async def respond(messages, info):
        if not any(getattr(part, "part_kind", "") == "tool-return" for message in messages for part in message.parts):
            arguments = {"time_from": WINDOW["from"], "time_to": WINDOW["to"]}
            if name == "query_metric":
                arguments["metric"] = "revenue"
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id="report1")}
        else:
            yield "Doanh thu 5.000.000 VND" if valid else "Dữ liệu chưa hợp lệ."

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=respond))
    request = SendMessageRequest(command_id=uuid4(), text="Báo cáo tháng 11/2026")
    stream = await o.run_turn(conversation_id=cid, request=request, principal=principal)
    events = [event async for event in stream]
    assert route.call_count == 1
    assert events[0].type == "turn.started" and events[-1].type == "turn.completed"
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    blocks = [event.payload for event in events if event.type == "block"]
    if valid:
        assert len(blocks) == 1 and blocks[0]["type"] == "StatCards"
        assert blocks[0]["payload"]["from"] == WINDOW["from"]
        assert blocks[0]["payload"]["value"] == 5_000_000
        assert not any(event.type == "notice" for event in events)
    else:
        assert not blocks
        saved = mocks["append_messages"].call_args.args[2]
        assert "INVALID_TOOL_RESULT" in repr(saved)
    mocks["release_turn"].assert_awaited_once_with(cid, tid)


def test_reporting_tools_areDefinedInAdminRead():
    for name in ("get_admin_dashboard", "query_metric", "list_metrics"):
        assert tools_for(Role.ADMIN)[name].__module__ == "vingo_agent.tools.admin_read"


def test_projection_unrelatedOperation_keepsFieldSpecDispatch(monkeypatch):
    from vingo_agent.guardrails.result import FieldSpec
    from vingo_agent.tools.projections import SPECS, project

    monkeypatch.setitem(SPECS, "catalog_example", {"id": FieldSpec(str, match="id")})
    assert project("catalog_example", {"id": "one", "email": "secret"}, role="ADMIN", requested={"id": "one"}) == (
        {"id": "one"},
        False,
    )
    with pytest.raises(ToolError, match="INVALID_TOOL_RESULT"):
        project("catalog_example", {"id": "other"}, role="ADMIN", requested={"id": "one"})
