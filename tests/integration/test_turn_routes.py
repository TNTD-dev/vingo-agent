"""Luồng HTTP/SSE thật với Postgres và backend giả, không gọi provider."""

import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
import respx
from pydantic_ai import RunContext
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlalchemy.ext.asyncio import create_async_engine

from vingo_agent.agents import factory, orchestrator
from vingo_agent.api.app import create_app
from vingo_agent.persistence import repositories as repo
from vingo_agent.tools.base import TurnDeps

pytest_plugins = ["tests.integration.test_migration"]
pytestmark = pytest.mark.integration


async def test_routes_persistFilteredHistoryAndReplayCommand(database_url, monkeypatch):
    engine = create_async_engine(database_url)

    @asynccontextmanager
    async def transaction():
        async with engine.begin() as conn:
            yield conn

    monkeypatch.setattr(repo, "transaction", transaction)
    event_id = str(uuid4())
    calls = 0

    async def search(ctx: RunContext[TurnDeps]):
        """Tool giả chỉ đăng ký trong test."""
        result = await ctx.deps.backend.read("searchEvents")
        return {
            "ok": True,
            "data": {"items": [{"event_id": result["items"][0]["id"], "title": "Demo"}]},
            "meta": {"operation": "searchEvents"},
        }

    async def respond(messages, info):
        nonlocal calls
        calls += 1
        assert "123456" not in repr(messages)
        if any(getattr(part, "part_kind", "") == "tool-return" for m in messages for part in m.parts):
            yield "Đây là kết quả."
        else:
            yield {0: DeltaToolCall(name="search", json_args="{}", tool_call_id="call1")}

    monkeypatch.setattr(factory, "tools_for", lambda *args: {"search": search})
    monkeypatch.setattr(orchestrator, "build_model", lambda *args: FunctionModel(stream_function=respond))
    headers = {
        "X-Agent-Service-Key": "test-service-key",
        "X-Vingo-User-Id": str(uuid4()),
        "X-Vingo-Role": "BUYER",
        "Authorization": "Bearer session-secret",
    }
    try:
        with respx.mock as backend:
            route = backend.get("http://localhost:8080/api/v1/events").mock(
                return_value=httpx.Response(
                    200, json={"items": [{"id": event_id, "email": "private@example.com", "qr_token": "secret"}]}
                )
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app()), base_url="http://agent"
            ) as client:
                created = await client.post("/internal/v1/conversations", headers=headers)
                assert created.status_code == 201
                cid = created.json()["conversation_id"]
                body = {"command_id": str(uuid4()), "text": "Tìm sự kiện OTP 123456"}
                sent = await client.post(f"/internal/v1/conversations/{cid}/messages", headers=headers, json=body)
                assert sent.status_code == 200
                events = [json.loads(line[6:]) for line in sent.text.splitlines() if line.startswith("data: ")]
                assert events[-1]["type"] == "turn.completed", events
                assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
                tid = events[0]["turn_id"]
                turn = (await client.get(f"/internal/v1/turns/{tid}", headers=headers)).json()
                assert turn["state"] == "COMPLETED"
                assert turn["blocks"][0]["type"] == "EventList"
                history = await client.get(f"/internal/v1/conversations/{cid}", headers=headers)
                assert "123456" not in history.text
                assert "private@example.com" not in history.text
                assert [m["kind"] for m in history.json()["messages"]] == [
                    "USER",
                    "TOOL_CALL",
                    "TOOL_RESULT",
                    "ASSISTANT",
                ]
                again = await client.post(f"/internal/v1/conversations/{cid}/messages", headers=headers, json=body)
                assert '"turn.completed"' in again.text
                assert calls == 2
                assert route.call_count == 1
                conflict = await client.post(
                    f"/internal/v1/conversations/{cid}/messages", headers=headers, json={**body, "text": "khác"}
                )
                assert conflict.status_code == 409
                assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
                stranger = {**headers, "X-Vingo-User-Id": str(uuid4())}
                assert (await client.get(f"/internal/v1/turns/{tid}", headers=stranger)).status_code == 404
                wrong_role = {**headers, "X-Vingo-Role": "ADMIN"}
                assert (
                    await client.post(f"/internal/v1/conversations/{cid}/messages", headers=wrong_role, json=body)
                ).status_code == 403
                assert (await repo.get_conversation(cid))["active_turn_id"] is None
                active = await repo.create_turn(cid, uuid4(), "busy")
                assert await repo.acquire_turn(cid, active["id"], 150)
                busy = await client.post(
                    f"/internal/v1/conversations/{cid}/messages",
                    headers=headers,
                    json={"command_id": str(uuid4()), "text": "xin chào"},
                )
                assert busy.status_code == 409
                assert busy.json()["code"] == "TURN_IN_PROGRESS"
                assert "text/event-stream" not in busy.headers.get("content-type", "")
                await repo.release_turn(cid, active["id"])
                for value, expected in [("", 400), ("a" * 4001, 413), ("x\u0000", 400)]:
                    rejected = await client.post(
                        f"/internal/v1/conversations/{cid}/messages",
                        headers=headers,
                        json={"command_id": str(uuid4()), "text": value},
                    )
                    assert rejected.status_code == expected
                    code = "PAYLOAD_TOO_LARGE" if expected == 413 else "VALIDATION_ERROR"
                    assert rejected.json() == {"status": expected, "code": code, "title": code}
                    assert (await repo.get_conversation(cid))["active_turn_id"] is None
                assert calls == 2
    finally:
        await engine.dispose()


async def test_sendMessage_failsStaleTurn_whenLeaseLostBeforeStream(database_url, monkeypatch):
    """Stream chưa từng chạy (client ngắt sớm) để lại turn RUNNING không lease: gửi lại không bị 409 mãi."""
    import hashlib

    from vingo_agent.schemas.chat import SendMessageRequest

    engine = create_async_engine(database_url)

    @asynccontextmanager
    async def transaction():
        async with engine.begin() as conn:
            yield conn

    monkeypatch.setattr(repo, "transaction", transaction)
    headers = {
        "X-Agent-Service-Key": "test-service-key",
        "X-Vingo-User-Id": str(uuid4()),
        "X-Vingo-Role": "BUYER",
        "Authorization": "Bearer session-secret",
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://agent"
        ) as client:
            cid = (await client.post("/internal/v1/conversations", headers=headers)).json()["conversation_id"]
            body = {"command_id": str(uuid4()), "text": "xin chào"}
            command_hash = hashlib.sha256(
                SendMessageRequest.model_validate(body).model_dump_json().encode()
            ).hexdigest()
            stale = await repo.create_turn(cid, body["command_id"], command_hash)
            assert stale["state"] == "RUNNING"
            sent = await client.post(f"/internal/v1/conversations/{cid}/messages", headers=headers, json=body)
            assert sent.status_code == 200
            events = [json.loads(line[6:]) for line in sent.text.splitlines() if line.startswith("data: ")]
            assert events[-1] == {**events[-1], "type": "turn.failed", "payload": {"code": "BACKEND_UNAVAILABLE"}}
            assert (await repo.get_turn(stale["id"]))["state"] == "FAILED"
            assert (await repo.get_conversation(cid))["active_turn_id"] is None
    finally:
        await engine.dispose()


async def test_turn_persistsUsageAndCharsOfLastModelRequest(database_url, monkeypatch):
    from pydantic_ai.messages import ModelRequest

    from vingo_agent.context.tokens import text_chars

    engine = create_async_engine(database_url)

    @asynccontextmanager
    async def transaction():
        async with engine.begin() as conn:
            yield conn

    monkeypatch.setattr(repo, "transaction", transaction)
    request_chars = []

    class MeasuredModel(FunctionModel):
        @asynccontextmanager
        async def request_stream(self, messages, *args, **kwargs):
            # Tính từ messages thật đến provider, gồm instructions đã resolve.
            assert any(isinstance(message, ModelRequest) and message.instructions for message in messages)
            request_chars.append(text_chars(messages))
            async with super().request_stream(messages, *args, **kwargs) as response:
                response._usage.input_tokens = 40 * len(request_chars)
                yield response

    async def lookup():
        """Tool giả để buộc hai lần gọi model trong một turn."""
        return {"ok": True, "data": {"title": "Demo"}, "meta": {"operation": "fake"}}

    async def respond(messages, info):
        if len(request_chars) == 1:
            yield {0: DeltaToolCall(name="lookup", json_args="{}", tool_call_id="call1")}
        else:
            yield "OK"

    monkeypatch.setattr(factory, "tools_for", lambda *args: {"lookup": lookup})
    monkeypatch.setattr(orchestrator, "build_model", lambda *args: MeasuredModel(stream_function=respond))
    headers = {
        "X-Agent-Service-Key": "test-service-key",
        "X-Vingo-User-Id": str(uuid4()),
        "X-Vingo-Role": "BUYER",
        "Authorization": "Bearer session-secret",
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://agent"
        ) as client:
            cid = (await client.post("/internal/v1/conversations", headers=headers)).json()["conversation_id"]
            sent = await client.post(
                f"/internal/v1/conversations/{cid}/messages",
                headers=headers,
                json={"command_id": str(uuid4()), "text": "xin chào"},
            )
            events = [json.loads(line[6:]) for line in sent.text.splitlines() if line.startswith("data: ")]
            assert events[-1]["type"] == "turn.completed", events
            tid = events[0]["turn_id"]
            turn = await repo.get_turn(tid)
            assert len(request_chars) == 2
            assert turn["last_input_tokens"] == 80
            assert turn["last_input_chars"] == request_chars[-1]
            assert turn["last_input_chars"] > request_chars[0]
            assert dict(await repo.get_last_input_usage(cid)) == {
                "last_input_tokens": 80,
                "last_input_chars": request_chars[-1],
            }
    finally:
        await engine.dispose()
