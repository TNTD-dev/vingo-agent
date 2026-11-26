"""Kiểm đường hợp lệ của đề xuất đã có; không triển khai tool còn thiếu."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from vingo_agent.actions import proposals as pending
from vingo_agent.api.auth import Principal
from vingo_agent.schemas.chat import Role
from vingo_agent.tools import base, proposals
from vingo_agent.tools.base import TurnDeps


@pytest.mark.parametrize(
    "name",
    [
        "propose_create_venue",
        "propose_create_event",
        "propose_create_showtime",
        "propose_publish_event",
        "propose_block_seats",
        "propose_unblock_seats",
    ],
)
async def test_readyAdminProposal_createsPending_withoutBusinessWrite(name, monkeypatch):
    identifier = uuid4()
    now = datetime.now(UTC)
    backend = AsyncMock()
    backend.read.return_value = {"id": str(identifier), "status": "DRAFT", "title": "Demo"}
    if name == "propose_create_showtime":
        backend.read.return_value = {"id": str(identifier), "status": "PUBLISHED"}
    create = AsyncMock(return_value=(uuid4(), 1))
    monkeypatch.setattr(pending, "create", create)
    monkeypatch.setattr(base.repositories, "has_ref", AsyncMock(return_value=True))
    ctx = SimpleNamespace(deps=TurnDeps(Principal(uuid4(), Role.ADMIN, "eval-token"), uuid4(), uuid4(), backend))
    args = {
        "propose_create_venue": {"name": "Eval", "address": "Eval", "city": "HN"},
        "propose_create_event": {"title": "Demo", "category": "CONCERT"},
        "propose_create_showtime": {
            "event_id": identifier,
            "layout_id": identifier,
            "starts_at": now + timedelta(days=10),
            "ends_at": now + timedelta(days=10, hours=3),
            "sale_opens_at": now,
            "sale_closes_at": now + timedelta(days=9),
        },
        "propose_publish_event": {"event_id": identifier},
        "propose_block_seats": {"showtime_id": identifier, "seat_ids": [identifier], "reason": "Bảo trì"},
        "propose_unblock_seats": {"showtime_id": identifier, "seat_ids": [identifier]},
    }
    result = await getattr(proposals, name)(ctx, **args[name])
    assert result["ok"] is True
    assert result["data"]["status"] == "PROPOSED"
    create.assert_awaited_once()
    backend.write.assert_not_called()
    assert ctx.deps.blocks[0].type == "ConfirmAction"


# Test của bộ eval: CI chưa chạy (pytest -m "not eval").
pytestmark = pytest.mark.eval
