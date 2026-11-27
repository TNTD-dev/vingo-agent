from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import AnyHttpUrl

from vingo_agent.actions.execution import execute, execute_admin_catalog
from vingo_agent.actions.proposals import Operation
from vingo_agent.schemas.chat import Role
from vingo_agent.tools import admin_read, proposals
from vingo_agent.tools.catalog import ZonePriceInput


@pytest.fixture
def admin(monkeypatch):
    create = AsyncMock(return_value=(uuid4(), 1))
    monkeypatch.setattr(proposals.pending, "create", create)
    deps = SimpleNamespace(
        principal=SimpleNamespace(role=Role.ADMIN, user_id=uuid4()),
        conversation_id=uuid4(),
        turn_id=uuid4(),
        proposals_created=0,
        blocks=[],
        zone_price_preview=None,
        backend=SimpleNamespace(read=AsyncMock(), write=AsyncMock()),
    )
    return SimpleNamespace(deps=deps), create


async def test_create_event_saves_poster_and_shows_confirmation(admin):
    ctx, create = admin
    url = "https://example.com/poster.jpg"

    result = await proposals.propose_create_event(ctx, "Demo Concert", "CONCERT", poster_mobile_url=AnyHttpUrl(url))

    assert result["data"]["status"] == "PROPOSED"
    create.assert_awaited_once()
    saved = create.await_args.kwargs
    assert saved["operation"] == Operation.CREATE_EVENT
    assert saved["payload"]["body"]["poster_mobile_url"] == url
    assert saved["payload"]["body"]["title"] == "Demo Concert"
    assert len(ctx.deps.blocks) == 1
    assert str(ctx.deps.blocks[0].action_id) == result["data"]["action_id"]
    ctx.deps.backend.write.assert_not_awaited()


async def test_create_showtime_rejects_sale_closing_after_start(admin):
    ctx, create = admin
    starts = datetime(2026, 12, 15, 12, tzinfo=UTC)

    result = await proposals.propose_create_showtime(
        ctx,
        event_id=uuid4(),
        layout_id=uuid4(),
        starts_at=starts,
        ends_at=starts + timedelta(hours=2),
        sale_opens_at=starts - timedelta(days=1),
        sale_closes_at=starts + timedelta(minutes=1),
    )

    assert result["error"]["code"] == "INVALID_ARGUMENT"
    create.assert_not_awaited()
    ctx.deps.backend.read.assert_not_awaited()


async def test_create_showtime_rejects_draft_layout(admin, monkeypatch):
    ctx, create = admin
    monkeypatch.setattr(proposals, "require_ref", AsyncMock())
    layout_id = uuid4()
    ctx.deps.backend.read.return_value = {"id": str(layout_id), "status": "DRAFT"}
    starts = datetime(2026, 12, 15, 12, tzinfo=UTC)

    result = await proposals.propose_create_showtime(
        ctx,
        event_id=uuid4(),
        layout_id=layout_id,
        starts_at=starts,
        ends_at=starts + timedelta(hours=2),
        sale_opens_at=starts - timedelta(days=1),
        sale_closes_at=starts,
    )

    assert result["error"]["code"] == "VALIDATION_ERROR"
    ctx.deps.backend.read.assert_awaited_once_with("getSeatLayout", path={"layout_id": str(layout_id)})
    create.assert_not_awaited()


async def test_block_seats_rejects_blank_reason(admin):
    ctx, create = admin

    result = await proposals.propose_block_seats(ctx, uuid4(), [uuid4()], "   ")

    assert result["error"]["code"] == "INVALID_ARGUMENT"
    create.assert_not_awaited()


@pytest.mark.parametrize(
    ("operation", "expected_status", "attempts"),
    [
        (Operation.CREATE_VENUE, "UNKNOWN", 1),
        (Operation.CREATE_EVENT, "UNKNOWN", 1),
        (Operation.CREATE_SHOWTIME, "UNKNOWN", 1),
        (Operation.PUBLISH_EVENT, "SUCCEEDED", 2),
        (Operation.BLOCK_SEATS, "SUCCEEDED", 2),
        (Operation.UNBLOCK_SEATS, "SUCCEEDED", 2),
    ],
)
async def test_timeout_retries_only_safe_operations(operation, expected_status, attempts):
    write = AsyncMock(side_effect=[httpx.ReadTimeout("test timeout"), httpx.Response(200)])
    backend = SimpleNamespace(write=write)

    status, _, _ = await execute_admin_catalog(
        operation,
        backend=backend,
        payload={"path": None, "body": None},
        idempotency_key=uuid4(),
    )

    assert status == expected_status
    assert write.await_count == attempts
    if attempts == 2:
        assert write.await_args_list[0] == write.await_args_list[1]


async def test_publish_preserves_event_incomplete_error():
    write = AsyncMock(return_value=httpx.Response(422, json={"code": "EVENT_INCOMPLETE"}))

    result = await execute_admin_catalog(
        Operation.PUBLISH_EVENT,
        backend=SimpleNamespace(write=write),
        payload={"path": {"event_id": str(uuid4())}, "body": None},
        idempotency_key=uuid4(),
    )

    assert result == ("FAILED", "EVENT_INCOMPLETE", {})
    write.assert_awaited_once()


async def test_update_venue_captures_before_and_changes(admin, monkeypatch):
    ctx, create = admin
    venue_id = uuid4()
    require_ref = AsyncMock()
    monkeypatch.setattr(proposals, "require_ref", require_ref)
    ctx.deps.backend.read.return_value = {
        "id": str(venue_id),
        "name": "Sân A",
        "city": "Hà Nội",
    }

    result = await proposals.propose_update_venue(ctx, venue_id, name="  Sân B  ")

    assert result["data"]["status"] == "PROPOSED"
    require_ref.assert_awaited_once_with(ctx.deps, "VENUE", venue_id)
    ctx.deps.backend.read.assert_awaited_once_with("getAdminVenue", path={"venue_id": str(venue_id)})
    create.assert_awaited_once()
    saved = create.await_args.kwargs
    assert saved["operation"] == Operation.UPDATE_VENUE
    assert saved["payload"] == {
        "path": {"venue_id": str(venue_id)},
        "body": {"name": "Sân B"},
        "before": {"name": "Sân A"},
    }
    assert saved["display"] == {"changes": [{"field": "name", "before": "Sân A", "after": "Sân B"}]}
    assert len(ctx.deps.blocks) == 1
    assert ctx.deps.blocks[0].display == saved["display"]
    assert ctx.deps.blocks[0].rows[0].value == "Sân A → Sân B"
    ctx.deps.backend.write.assert_not_awaited()


async def test_update_venue_rejects_empty_or_invalid_arguments(admin):
    ctx, create = admin
    venue_id = uuid4()

    empty_result = await proposals.propose_update_venue(ctx, venue_id)
    blank_result = await proposals.propose_update_venue(ctx, venue_id, name="   ")
    bool_result = await proposals.propose_update_venue(ctx, venue_id, lat=True)

    assert empty_result["error"]["code"] == "INVALID_ARGUMENT"
    assert blank_result["error"]["code"] == "INVALID_ARGUMENT"
    assert bool_result["error"]["code"] == "INVALID_ARGUMENT"
    ctx.deps.backend.read.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.parametrize(
    ("first_title", "title_after_timeout", "lookup_only", "expected", "writes"),
    [
        ("Đổi trên web", None, False, ("FAILED", "STALE_PROPOSAL"), 0),
        ("Tên cũ", "Tên mới", False, ("SUCCEEDED", None), 1),
        ("Tên cũ", "Tên cũ", False, ("SUCCEEDED", None), 2),
        ("Tên cũ", "Đổi trên web", False, ("UNKNOWN", "EXECUTION_UNKNOWN"), 1),
        ("Tên mới", None, True, ("SUCCEEDED", None), 0),
        ("Đổi trên web", None, True, ("UNKNOWN", "EXECUTION_UNKNOWN"), 0),
    ],
)
async def test_update_event_reconciles_against_before_and_target(
    first_title, title_after_timeout, lookup_only, expected, writes
):
    event_id = str(uuid4())
    path = {"event_id": event_id}
    body = {"title": "Tên mới"}
    key = uuid4()
    reads = [{"id": event_id, "title": first_title}]
    if title_after_timeout is not None:
        reads.append({"id": event_id, "title": title_after_timeout})
    backend = SimpleNamespace(
        read=AsyncMock(side_effect=reads),
        write=AsyncMock(side_effect=[httpx.ReadTimeout("timeout"), httpx.Response(200, json={"id": event_id})]),
    )

    result = await execute(
        Operation.UPDATE_EVENT,
        backend=backend,
        payload={"path": path, "body": body, "before": {"title": "Tên cũ"}},
        idempotency_key=key,
        lookup_only=lookup_only,
    )

    assert result[:2] == expected
    assert backend.write.await_count == writes
    assert all(
        call.kwargs == {"path": path, "body": body, "idempotency_key": key} for call in backend.write.await_args_list
    )


async def test_add_tier_price_timeout_does_not_insert_a_second_version():
    event_id, tier_id = str(uuid4()), str(uuid4())
    backend = SimpleNamespace(
        read=AsyncMock(return_value={"id": event_id, "price_tiers": [{"id": tier_id, "price": 100, "prices": []}]}),
        write=AsyncMock(side_effect=httpx.ReadTimeout("timeout")),
    )
    payload = {
        "path": {"event_id": event_id, "tier_id": tier_id},
        "body": {"price": 200},
        "before": {"price": 100, "price_ids": []},
    }

    for lookup_only in (False, True):
        assert await execute(
            Operation.ADD_TIER_PRICE, backend=backend, payload=payload, idempotency_key=uuid4(), lookup_only=lookup_only
        ) == ("UNKNOWN", "EXECUTION_UNKNOWN", {})

    backend.write.assert_awaited_once()


async def test_zone_update_requires_the_exact_preview_and_saves_an_array(admin, monkeypatch):
    ctx, create = admin
    ref = AsyncMock()
    monkeypatch.setattr(admin_read, "require_ref", ref)
    monkeypatch.setattr(proposals, "require_ref", ref)
    event_id, zone_id, tier_id, layout_id = uuid4(), uuid4(), uuid4(), uuid4()
    assignment = ZonePriceInput(zone_id=zone_id, price_tier_id=tier_id)
    event = {
        "id": str(event_id),
        "status": "DRAFT",
        "seat_layout_id": str(layout_id),
        "zone_prices": [],
        "price_tiers": [{"id": str(tier_id), "name": "VIP", "price": 200}],
    }
    preview = {
        "event_id": str(event_id),
        "total_sku_count": 1,
        "zones": [{"zone_id": str(zone_id), "zone_name": "GA", "sku_count": 1}],
    }
    ctx.deps.backend.read.side_effect = [event, preview, event]

    assert (await proposals.propose_update_zone_prices(ctx, event_id, [assignment]))["ok"] is False
    assert (await admin_read.get_zone_prices_preview(ctx, event_id, [assignment]))["ok"] is True
    assert (await proposals.propose_update_zone_prices(ctx, event_id, []))["ok"] is False
    assert (await proposals.propose_update_zone_prices(ctx, event_id, [assignment]))["ok"] is True

    payload = create.await_args.kwargs["payload"]
    assert payload["before"] == []
    assert payload["body"] == [{"zone_id": str(zone_id), "price_tier_id": str(tier_id)}]
    assert ctx.deps.backend.read.await_args_list[1].kwargs["body"] == payload["body"]
    ctx.deps.backend.write.assert_not_awaited()
