from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import respx

from vingo_agent.clients.backend import BackendClient
from vingo_agent.config import Settings
from vingo_agent.tools import base
from vingo_agent.tools.base import ToolError


@respx.mock
async def test_read_retriesTwice_withAuth(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("vingo_agent.clients.backend.asyncio.sleep", sleep)
    route = respx.get("http://backend/api/v1/events").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(503),
            httpx.Response(200, json={"items": []}),
        ]
    )
    client = BackendClient(Settings(backend_url="http://backend/api/v1", agent_to_backend_key="key"), "session")
    try:
        assert await client.read("searchEvents") == {"items": []}
        assert route.call_count == 3
        assert sleep.call_args_list[0].args == (2.0,)
        assert route.calls[0].request.headers["Authorization"] == "Bearer session"
        assert route.calls[0].request.headers["X-Vingo-Agent-Key"] == "key"
    finally:
        await client.aclose()


@respx.mock
async def test_read_stopsAfterTwoRetries(monkeypatch):
    monkeypatch.setattr("vingo_agent.clients.backend.asyncio.sleep", AsyncMock())
    route = respx.get("http://backend/events").respond(503)
    client = BackendClient(Settings(backend_url="http://backend"), "token")
    try:
        with pytest.raises(ToolError, match="BACKEND_UNAVAILABLE"):
            await client.read("searchEvents")
        assert route.call_count == 3
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await client.read("holdSeats")
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await client.read("getEvent", path={"event_id": "../me"})
    finally:
        await client.aclose()


async def test_requireRef_checksConversationTypeAndId(monkeypatch):
    from types import SimpleNamespace

    deps = SimpleNamespace(conversation_id=uuid4())
    resource_id = uuid4()
    lookup = AsyncMock(return_value=False)
    monkeypatch.setattr(base.repositories, "has_ref", lookup)
    with pytest.raises(ToolError, match="INVALID_REFERENCE"):
        await base.require_ref(deps, "EVENT", resource_id)
    lookup.assert_awaited_once_with(deps.conversation_id, "EVENT", resource_id)
    lookup.return_value = True
    await base.require_ref(deps, "EVENT", resource_id)
    record = AsyncMock()
    monkeypatch.setattr(base.repositories, "add_refs", record)
    await base.record_refs(deps, "EVENT", [resource_id])
    record.assert_awaited_once_with(deps.conversation_id, "EVENT", [resource_id])


@pytest.mark.parametrize(
    "status,body,code",
    [(403, {"code": "FORBIDDEN"}, "FORBIDDEN"), (302, {}, "INVALID_TOOL_RESULT"), (200, [], "INVALID_TOOL_RESULT")],
)
@respx.mock
async def test_read_mapsFailures_withoutFollowingRedirects(status, body, code):
    route = respx.get("http://backend/events").respond(status, json=body, headers={"Location": "http://other"})
    client = BackendClient(Settings(backend_url="http://backend"), "token")
    try:
        with pytest.raises(ToolError, match=code):
            await client.read("searchEvents")
        assert route.call_count == 1
    finally:
        await client.aclose()


@respx.mock
async def test_read_mapsTimeoutToRetryableError():
    respx.get("http://backend/events").mock(side_effect=httpx.ReadTimeout("timeout"))
    client = BackendClient(Settings(backend_url="http://backend"), "token")
    try:
        with pytest.raises(ToolError, match="BACKEND_UNAVAILABLE") as exc:
            await client.read("searchEvents")
        assert exc.value.retryable
    finally:
        await client.aclose()


@respx.mock
async def test_read_unknownErrorCode_doesNotLeakBackendText():
    respx.get("http://backend/events").respond(400, json={"code": "SECRET_CANARY"})
    client = BackendClient(Settings(backend_url="http://backend"), "token")
    try:
        with pytest.raises(ToolError, match="INVALID_TOOL_RESULT"):
            await client.read("searchEvents")
    finally:
        await client.aclose()
