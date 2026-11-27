"""Kiểm tra trace thật qua generator, model giả và backend HTTP giả."""

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from tests.unit import test_turn_loop
from vingo_agent.actions import confirm, execution
from vingo_agent.agents import orchestrator as o
from vingo_agent.clients.backend import BackendClient
from vingo_agent.config import Settings
from vingo_agent.observability import tracing
from vingo_agent.observability.logging import ALLOWED_FIELDS
from vingo_agent.schemas.chat import SendMessageRequest
from vingo_agent.tools.base import ToolError, TurnDeps

turn_env = test_turn_loop.turn_env

SECRET = "SECRET-0901234567"
TOKEN = "Bearer tok-xyz"


@pytest.fixture
def traces(monkeypatch):
    monkeypatch.setattr(tracing, "get_settings", lambda: Settings(otel_exporter_endpoint=""))
    previous = tracing._provider
    provider = tracing.configure_tracing()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(tracing.FilteringExporter(exporter)))
    yield exporter
    Agent.instrument_all(False)
    provider.shutdown()
    monkeypatch.setattr(tracing, "_provider", previous)


async def traced_turn(turn_env, monkeypatch, respx_mock):
    principal, cid, tid, mocks = turn_env
    principal = replace(principal, session_token="tok-xyz")
    monkeypatch.setattr(o, "build_context", AsyncMock(return_value=[ModelRequest(parts=[UserPromptPart(SECRET)])]))
    respx_mock.get(url__regex=r".*/events.*").respond(200, json={"private": SECRET})

    async def search(ctx: RunContext[TurnDeps], query: str):
        return await ctx.deps.backend.read("searchEvents", query={"query": query})

    async def respond(messages, info):
        if any(p.part_kind == "tool-return" for m in messages for p in m.parts):
            yield "OK"
        else:
            yield {0: DeltaToolCall(name="search", json_args=json.dumps({"query": SECRET}), tool_call_id="call1")}

    monkeypatch.setattr(o, "build_model", lambda *a: FunctionModel(stream_function=respond))
    monkeypatch.setattr(o, "build_agent", lambda role, model, **kw: Agent(model, deps_type=TurnDeps, tools=[search]))
    request = SendMessageRequest(command_id=uuid4(), text=SECRET)
    events = [e async for e in await o.run_turn(conversation_id=cid, request=request, principal=principal)]
    assert events[-1].type == "turn.completed"
    return cid


async def test_turn_trace_hasSpanTree(traces, turn_env, monkeypatch, respx_mock):
    cid = await traced_turn(turn_env, monkeypatch, respx_mock)
    spans = traces.get_finished_spans()
    root = next(s for s in spans if s.name == "chat.turn")
    backend = next(s for s in spans if s.name == "backend.http")
    by_id = {s.context.span_id: s for s in spans}
    ancestors = []
    current = backend
    while current.parent:
        current = by_id[current.parent.span_id]
        ancestors.append(current)
    assert ancestors[-1] == root
    assert any("tool" in s.name.lower() for s in ancestors)
    agent = next(s for s in ancestors if s.name.startswith("invoke_agent"))
    model_spans = [s for s in spans if s.name.startswith("chat ")]
    assert model_spans
    assert all(s.parent.span_id == agent.context.span_id for s in model_spans)
    assert len({s.context.trace_id for s in spans}) == 1
    assert root.attributes["session.id"] == str(cid)
    assert root.attributes["outcome"] == "COMPLETED"
    assert {"context.build", "context.compact", "response.check"} <= {s.name for s in spans}


async def test_trace_excludesSensitiveContent(traces, turn_env, monkeypatch, respx_mock):
    await traced_turn(turn_env, monkeypatch, respx_mock)
    for s in traces.get_finished_spans():
        serialized = repr(
            (s.name, dict(s.attributes), [(e.name, dict(e.attributes)) for e in s.events], s.status.description)
        )
        assert SECRET not in serialized
        assert TOKEN not in serialized


async def test_trace_stripsExceptionMessage_whenBackendFails(traces, respx_mock):
    url = "http://backend/events"
    respx_mock.get(url).mock(side_effect=httpx.ConnectError(f"{url}?id={SECRET}"))
    client = BackendClient(Settings(backend_url="http://backend"), TOKEN)
    try:
        with pytest.raises(ToolError):
            await client.read("searchEvents")
        # Mô phỏng exception tự ghi bởi thư viện model/tool.
        with tracing.span("library") as s:
            s.record_exception(httpx.ConnectError(SECRET))
            s.set_status(Status(StatusCode.ERROR, SECRET))
    finally:
        await client.aclose()
    spans = traces.get_finished_spans()
    backend = next(s for s in spans if s.name == "backend.http")
    assert backend.status.status_code == StatusCode.ERROR
    assert backend.attributes["outcome"] == "NETWORK_ERROR"
    assert backend.attributes["error_code"] == "ConnectError"
    for s in spans:
        assert not s.events
        assert s.status.description is None
        assert SECRET not in repr(dict(s.attributes))


def test_tracing_disabled_whenEndpointEmpty(monkeypatch):
    monkeypatch.setattr(tracing, "get_settings", lambda: Settings(otel_exporter_endpoint=""))
    exporter = Mock()
    monkeypatch.setattr(tracing, "OTLPSpanExporter", exporter)
    previous = tracing._provider
    provider = tracing.configure_tracing()
    try:
        exporter.assert_not_called()
    finally:
        Agent.instrument_all(False)
        provider.shutdown()
        monkeypatch.setattr(tracing, "_provider", previous)


async def test_turn_completedLog_hasAllowedFields(traces, turn_env, monkeypatch, respx_mock, caplog):
    with caplog.at_level(logging.INFO):
        await traced_turn(turn_env, monkeypatch, respx_mock)
    fields = next(r.fields for r in caplog.records if r.getMessage() == "turn.completed")
    assert {"turn_id", "role", "outcome", "duration_ms"} <= fields.keys()
    assert fields.keys() <= ALLOWED_FIELDS
    assert fields["duration_ms"] >= 0


@pytest.mark.parametrize("status,outcome", [(200, "SUCCEEDED"), (422, "FAILED"), (503, "UNKNOWN")])
async def test_confirm_trace_hasActionSpan(traces, monkeypatch, respx_mock, turn_env, caplog, status, outcome):
    principal, cid, tid, mocks = turn_env
    action_id = uuid4()
    row = {
        "conversation_id": cid,
        "version": 1,
        "state": "PENDING",
        "operation": "RESEND_MY_TICKET",
        "expires_at": datetime.now(UTC) + timedelta(minutes=1),
        "payload": {"path": {"ticket_id": str(uuid4())}},
        "display": {},
    }
    conn = AsyncMock()
    result = Mock()
    result.mappings.return_value.one_or_none.return_value = None
    conn.execute.return_value = result
    conn.scalar.return_value = action_id

    @asynccontextmanager
    async def transaction():
        yield conn

    monkeypatch.setattr(confirm, "transaction", transaction)
    monkeypatch.setattr(confirm, "_lock", AsyncMock(return_value=row))
    monkeypatch.setattr(
        execution,
        "load",
        AsyncMock(return_value={**row, "state": "CONFIRMED", "execution_state": "RUNNING", "idempotency_key": uuid4()}),
    )

    async def save(action_id, row, outcome, **kwargs):
        return outcome

    monkeypatch.setattr(execution, "save", save)
    respx_mock.post(url__regex=r".*/tickets/.*/resend").respond(status, json={})
    client = BackendClient(Settings(backend_url="http://backend"), TOKEN)
    try:
        with caplog.at_level(logging.INFO):
            actual, blocks = await confirm.confirm(
                action_id=action_id, version=1, principal=principal, via=confirm.ConfirmedVia.BUTTON, backend=client
            )
    finally:
        await client.aclose()
    assert actual[0] == outcome
    spans = traces.get_finished_spans()
    root = next(s for s in spans if s.name == "action.confirm")
    backend = next(s for s in spans if s.name == "backend.http")
    assert backend.parent.span_id == root.context.span_id
    assert backend.context.trace_id == root.context.trace_id
    assert root.attributes["outcome"] == outcome
    fields = next(r.fields for r in caplog.records if r.getMessage() == "action.executed")
    assert {"action_id", "operation", "outcome", "duration_ms"} <= fields.keys()
    assert fields.keys() <= ALLOWED_FIELDS
    assert fields["outcome"] == outcome


def test_span_dropsAttributesOutsideAllowlist(traces):
    with tracing.span("safe", turn_id="turn", body=SECRET, token=TOKEN):
        pass
    assert dict(traces.get_finished_spans()[0].attributes) == {"turn_id": "turn"}


@pytest.mark.parametrize("key,headers", [("", {}), ("phoenix-secret", {"Authorization": "Bearer phoenix-secret"})])
def test_tracing_configuresPhoenixEndpointAndAuth(monkeypatch, key, headers):
    monkeypatch.setattr(
        tracing,
        "get_settings",
        lambda: Settings(
            otel_exporter_endpoint="http://localhost:6006/", phoenix_api_key=key, otel_project_name="test-project"
        ),
    )
    exporter = Mock()
    monkeypatch.setattr(tracing, "OTLPSpanExporter", exporter)
    previous = tracing._provider
    provider = tracing.configure_tracing()
    try:
        exporter.assert_called_once_with(endpoint="http://localhost:6006/v1/traces", headers=headers)
        assert provider.resource.attributes["service.name"] == "vingo-agent"
        assert provider.resource.attributes["openinference.project.name"] == "test-project"
    finally:
        Agent.instrument_all(False)
        provider.shutdown()
        monkeypatch.setattr(tracing, "_provider", previous)


async def test_turn_completedLog_onStreamClose(traces, turn_env, caplog):
    principal, cid, tid, mocks = turn_env
    with caplog.at_level(logging.INFO):
        stream = await o.run_turn(
            conversation_id=cid, request=SendMessageRequest(command_id=uuid4(), text="Xin chào"), principal=principal
        )
        await anext(stream)
        await stream.aclose()
    fields = next(r.fields for r in caplog.records if r.getMessage() == "turn.completed")
    assert fields["outcome"] == "FAILED"
    assert traces.get_finished_spans()[0].attributes["outcome"] == "FAILED"


@pytest.mark.parametrize(
    "endpoint,flag,expected",
    [
        ("http://localhost:6006", True, True),
        ("http://127.0.0.1:6006", True, True),
        ("http://localhost:6006", False, False),
        ("https://phoenix.staging.vingo.vn", True, False),
        ("", True, False),
    ],
)
def test_includeContent_onlyWhenFlagAndLocalEndpoint(endpoint, flag, expected):
    settings = Settings(otel_exporter_endpoint=endpoint, otel_include_content=flag)
    assert tracing.include_content(settings) is expected


def test_tracing_keepsContentOff_whenFlagSetOnRemoteEndpoint(monkeypatch):
    monkeypatch.setattr(
        tracing,
        "get_settings",
        lambda: Settings(otel_exporter_endpoint="https://phoenix.staging.vingo.vn", otel_include_content=True),
    )
    monkeypatch.setattr(tracing, "OTLPSpanExporter", Mock())
    previous = tracing._provider
    provider = tracing.configure_tracing()
    try:
        assert Agent._instrument_default.include_content is False
    finally:
        Agent.instrument_all(False)
        provider.shutdown()
        monkeypatch.setattr(tracing, "_provider", previous)
