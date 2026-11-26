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


@pytest.mark.parametrize("status", [200, 400, 503])
@respx.mock
async def test_write_sendsOnceWithAuthAndKey(status):
    showtime = uuid4()
    key = uuid4()
    route = respx.post(f"http://backend/showtimes/{showtime}/holds").respond(status, json={"code": "SEAT_TAKEN"})
    client = BackendClient(Settings(backend_url="http://backend", agent_to_backend_key="agent-key"), "session")
    try:
        response = await client.write(
            "holdSeats", path={"showtime_id": showtime}, body={"quantity": 1}, idempotency_key=key
        )
        assert response.status_code == status
        assert route.call_count == 1
        request = route.calls[0].request
        assert request.headers["Idempotency-Key"] == str(key)
        assert request.headers["Authorization"] == "Bearer session"
        assert request.headers["X-Vingo-Agent-Key"] == "agent-key"
        assert request.extensions["timeout"]["read"] == 10
    finally:
        await client.aclose()


@respx.mock
async def test_write_timeoutIsNotRetried_andUnsafeTargetsAreBlocked():
    route = respx.post("http://backend/admin/events").mock(side_effect=httpx.ReadTimeout("secret"))
    client = BackendClient(Settings(backend_url="http://backend"), "session")
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.write("createEvent", body={})
        assert route.call_count == 1
        for operation, path in [
            ("searchEvents", None),
            ("http://evil", None),
            ("publishEvent", {"event_id": "../me"}),
            ("publishEvent", {"extra": uuid4()}),
            ("holdSeats", {"showtime_id": uuid4()}),
        ]:
            with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
                await client.write(operation, path=path)
    finally:
        await client.aclose()


@respx.mock
async def test_write_doesNotFollowRedirect_orSendUnneededKey():
    route = respx.post("http://backend/admin/events").respond(307, headers={"Location": "http://evil"})
    client = BackendClient(Settings(backend_url="http://backend"), "session")
    try:
        assert (await client.write("createEvent", body={}, idempotency_key=uuid4())).status_code == 307
        assert route.call_count == 1
        assert "Idempotency-Key" not in route.calls[0].request.headers
    finally:
        await client.aclose()
