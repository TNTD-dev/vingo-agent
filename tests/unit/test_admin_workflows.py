from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from vingo_agent.actions.execution import execute
from vingo_agent.actions.proposals import Operation
from vingo_agent.agents.registry import tools_for
from vingo_agent.api import routes
from vingo_agent.api.app import create_app
from vingo_agent.config import get_settings
from vingo_agent.schemas.chat import Role
from vingo_agent.tools import admin_workflows, proposals


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("AGENT_ADMIN_WORKFLOWS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def admin(enabled, monkeypatch):
    create = AsyncMock(return_value=(uuid4(), 1))
    monkeypatch.setattr(proposals.pending, "create", create)
    monkeypatch.setattr(admin_workflows, "require_ref", AsyncMock())
    backend = SimpleNamespace(read=AsyncMock(), stage_import=AsyncMock(), write=AsyncMock())
    deps = SimpleNamespace(
        principal=SimpleNamespace(role=Role.ADMIN, user_id=uuid4()),
        conversation_id=uuid4(),
        turn_id=uuid4(),
        blocks=[],
        backend=backend,
    )
    return SimpleNamespace(deps=deps), create


def test_admin_workflows_areOptIn(enabled):
    assert {fn.__name__ for fn in admin_workflows.ADMIN_WORKFLOW_TOOLS} <= tools_for(Role.ADMIN).keys()
    assert not ({fn.__name__ for fn in admin_workflows.ADMIN_WORKFLOW_TOOLS} & tools_for(Role.BUYER).keys())


@pytest.mark.parametrize("is_showtime", [False, True])
async def test_propose_cancel_requiresExactPhraseAndSavesBefore(admin, is_showtime):
    ctx, create = admin
    event, show = uuid4(), uuid4()
    raw = {
        "id": str(event),
        "status": "DRAFT",
        "title": "Demo",
        "showtimes": [
            {"id": str(show), "status": "DRAFT", "starts_at": "2030-05-10T12:00:00Z"},
        ],
    }
    ctx.deps.backend.read.return_value = raw
    if is_showtime:
        operation = Operation.CANCEL_SHOWTIME
        result = await admin_workflows.propose_cancel_showtime(ctx, event, show, "Đổi lịch")
        phrase = "19:00 10/05/2030"
    else:
        operation = Operation.CANCEL_EVENT
        result = await admin_workflows.propose_cancel_event(ctx, event, "Đổi lịch")
        phrase = "Demo"
    assert result["data"]["status"] == "PROPOSED"
    assert ctx.deps.blocks[0].confirm_phrase == phrase
    data = create.await_args.kwargs
    assert data["operation"] == operation
    assert data["payload"]["confirm_phrase"] == phrase
    assert data["payload"]["body"] == {"reason": "Đổi lịch"}
    assert data["payload"]["before"]["status"] == "DRAFT"
    ctx.deps.backend.write.assert_not_awaited()


async def test_cancel_recoversAfterTimeoutByRead(enabled):
    event = str(uuid4())
    backend = SimpleNamespace(
        read=AsyncMock(
            side_effect=[
                {"id": event, "title": "Demo", "status": "DRAFT", "showtimes": []},
                {"id": event, "title": "Demo", "status": "CANCELLED", "showtimes": []},
            ]
        ),
        write=AsyncMock(side_effect=httpx.ReadTimeout("timeout")),
    )
    payload = {
        "path": {"event_id": event},
        "body": {"reason": "Đổi lịch"},
        "before": {"title": "Demo", "status": "DRAFT", "showtimes": {}},
        "confirm_phrase": "Demo",
    }
    assert await execute(Operation.CANCEL_EVENT, backend=backend, payload=payload, idempotency_key=uuid4()) == (
        "SUCCEEDED",
        None,
        {},
    )
    backend.write.assert_awaited_once()


async def test_propose_visibility_snapshotsCurrentPublicState(admin):
    ctx, create = admin
    event = uuid4()
    ctx.deps.backend.read.return_value = {"id": str(event), "status": "PUBLISHED", "title": "Demo", "visible": True}
    result = await admin_workflows.propose_update_event_visibility(ctx, event, False, "Đổi lịch")
    assert result["data"]["status"] == "PROPOSED"
    data = create.await_args.kwargs["payload"]
    assert data["before"] == {"status": "PUBLISHED", "visible": True}
    assert data["body"]["visible"] is False
    ctx.deps.backend.write.assert_not_awaited()


async def test_layoutStagesFilesAndPreviewBeforeConfirm(admin, monkeypatch):
    ctx, create = admin
    venue, overview, upload, file_id, session_id = [uuid4() for _ in range(5)]
    get_uploads = AsyncMock(return_value={upload: b"<svg/>"})
    monkeypatch.setattr(admin_workflows, "get_uploads", get_uploads)
    session = {"id": str(session_id), "status": "DRAFT", "revision": 0}
    ctx.deps.backend.stage_import.side_effect = [
        session,
        session
        | {
            "revision": 1,
            "files": [{"id": str(file_id), "role": "SEAT_LAYOUT"}],
        },
        session | {"revision": 2},
    ]
    ctx.deps.backend.read.return_value = {
        "session_id": str(session_id),
        "revision": 2,
        "valid": True,
        "zones": [{"seat_count": 5}],
        "overview_svg": "raw svg",
    }

    result = await admin_workflows.propose_seat_layout(ctx, [upload], "Sơ đồ Demo", "CONCERT", venue, overview)

    assert result["data"]["status"] == "PROPOSED"
    assert ctx.deps.backend.stage_import.await_args_list[2].kwargs["body"]["draft_data"]["file_layout_types"] == {
        str(file_id): "CONCERT"
    }
    assert create.await_args.kwargs["payload"] == {"path": {"session_id": str(session_id)}, "body": {"revision": 2}}
    assert "raw svg" not in repr(ctx.deps.blocks)
    ctx.deps.backend.write.assert_not_awaited()


def test_uploadEndpointStoresExactBytesOnlyForAdmin(enabled, monkeypatch):
    user, upload = uuid4(), uuid4()
    headers = {
        "X-Agent-Service-Key": "test-service-key",
        "X-Vingo-User-Id": str(user),
        "X-Vingo-Role": "ADMIN",
        "Authorization": "Bearer session",
    }
    save = AsyncMock(return_value=upload)
    monkeypatch.setattr(routes, "save_upload", save)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg">\n</svg>'
    client = TestClient(create_app())
    response = client.post(
        "/internal/v1/uploads", headers=headers, files={"file": ("layout.svg", svg, "image/svg+xml")}
    )
    assert response.json() == {"upload_id": str(upload), "kind": "SVG", "size": len(svg)}
    save.assert_awaited_once_with(user, svg)
    response = client.post(
        "/internal/v1/uploads",
        headers=headers | {"X-Vingo-Role": "BUYER"},
        files={"file": ("layout.svg", svg, "image/svg+xml")},
    )
    assert response.status_code == 403
