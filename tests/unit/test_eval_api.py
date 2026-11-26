"""Hồi quy API cho selection và registry chưa phát hành."""

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from pydantic_ai.models.function import FunctionModel

from vingo_agent.agents import orchestrator
from vingo_agent.agents.registry import tools_for
from vingo_agent.api.app import create_app
from vingo_agent.schemas.chat import Role

pytest_plugins = ["tests.unit.test_turn_loop"]


async def test_selection_ignoresUnknownRef_andWarns(turn_env, monkeypatch):
    principal, cid, _, _ = turn_env

    async def respond(messages, info):
        assert str(unknown) not in repr(messages)
        yield "Bạn chọn lại sự kiện trên thẻ nhé."

    unknown = uuid4()
    monkeypatch.setattr(orchestrator, "build_model", lambda *a: FunctionModel(stream_function=respond))
    monkeypatch.setattr(orchestrator.repo, "has_ref", AsyncMock(return_value=False))
    headers = {
        "X-Agent-Service-Key": "test-service-key",
        "X-Vingo-User-Id": str(principal.user_id),
        "X-Vingo-Role": "BUYER",
        "Authorization": "Bearer eval-session",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://agent") as client:
        response = await client.post(
            f"/internal/v1/conversations/{cid}/messages",
            headers=headers,
            json={"command_id": str(uuid4()), "text": "Tìm vé", "selection": {"type": "EVENT", "ids": [str(unknown)]}},
        )
    assert response.status_code == 200
    assert "notice" in response.text and "WARN" in response.text
    assert "turn.completed" in response.text


def test_unreleasedTools_absentFromBothRoles():
    disabled = {
        "propose_cancel_event",
        "propose_cancel_showtime",
        "propose_seat_layout",
        "propose_update_event_visibility",
    }
    for role in Role:
        assert not disabled & tools_for(role).keys()
