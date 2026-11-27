"""Huỷ, ẩn/hiện và nhập sơ đồ qua đề xuất Admin."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_ai import RunContext

from vingo_agent.actions.proposals import Operation
from vingo_agent.actions.specs.admin_workflows import get_showtime, read_event, snapshot
from vingo_agent.persistence.uploads import get_uploads
from vingo_agent.tools.base import ToolError, TurnDeps, require_ref
from vingo_agent.tools.catalog import as_record, rows, timestamp, uuid_text
from vingo_agent.tools.proposals import _admin_proposal

Reason = Annotated[str, Field(min_length=3, max_length=500)]
LayoutName = Annotated[str, Field(min_length=1, max_length=200)]


def _required(value, *, minimum=1):
    if value is None or len(value.strip()) < minimum:
        raise ToolError("INVALID_ARGUMENT")
    return value.strip()


def _row(label, value):
    return {"label": label, "value": str(value)}


@_admin_proposal(Operation.CANCEL_EVENT)
async def propose_cancel_event(ctx: RunContext[TurnDeps], event_id: UUID, reason: Reason) -> dict:
    """Đề xuất huỷ sự kiện chưa mở bán. Admin gõ lại tên sự kiện trên thẻ. → cancelEvent"""
    reason = _required(reason, minimum=3)
    await require_ref(ctx.deps, "EVENT", event_id)
    path = {"event_id": str(event_id)}
    event = await read_event(ctx.deps.backend, path["event_id"])
    before = snapshot(Operation.CANCEL_EVENT, event, path)
    active = [status for status in before["showtimes"].values() if status != "CANCELLED"]
    if before["status"] not in {"DRAFT", "PUBLISHED"} or any(status not in {"DRAFT", "SCHEDULED"} for status in active):
        raise ToolError("EVENT_LOCKED")
    phrase = _required(before["title"])
    display = {
        "confirm_phrase": phrase,
        "rows": [_row("Sự kiện sẽ huỷ", phrase), _row("Số suất ảnh hưởng", len(active)), _row("Lý do", reason)],
    }
    return path, {"reason": reason}, display, {"before": before, "confirm_phrase": phrase}


@_admin_proposal(Operation.CANCEL_SHOWTIME)
async def propose_cancel_showtime(ctx: RunContext[TurnDeps], event_id: UUID, showtime_id: UUID, reason: Reason) -> dict:
    """Đề xuất huỷ suất chưa mở bán. Admin gõ lại giờ suất trên thẻ. → cancelShowtime"""
    reason = _required(reason, minimum=3)
    await require_ref(ctx.deps, "EVENT", event_id)
    await require_ref(ctx.deps, "SHOWTIME", showtime_id)
    path = {"showtime_id": str(showtime_id)}
    event = await read_event(ctx.deps.backend, str(event_id))
    show = get_showtime(event, path["showtime_id"])
    if event.get("status") not in {"DRAFT", "PUBLISHED"} or show.get("status") not in {"DRAFT", "SCHEDULED"}:
        raise ToolError("SHOWTIME_LOCKED")
    if (
        event["status"] == "PUBLISHED"
        and sum(item.get("status") != "CANCELLED" for item in rows(event, "showtimes")) <= 1
    ):
        raise ToolError("SHOWTIME_LOCKED")
    before = snapshot(Operation.CANCEL_SHOWTIME, event, path)
    local_time = datetime.fromisoformat(timestamp(show.get("starts_at"))).astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    phrase = local_time.strftime("%H:%M %d/%m/%Y")
    display = {
        "confirm_phrase": phrase,
        "rows": [_row("Sự kiện", event["title"]), _row("Suất sẽ huỷ", phrase), _row("Lý do", reason)],
    }
    return path, {"reason": reason}, display, {"before": before, "confirm_phrase": phrase, "event_id": str(event_id)}


@_admin_proposal(Operation.UPDATE_EVENT_VISIBILITY)
async def propose_update_event_visibility(
    ctx: RunContext[TurnDeps], event_id: UUID, visible: bool, reason: Reason
) -> dict:
    """Đề xuất ẩn hoặc hiển thị sự kiện đã công bố. → updateEventVisibility"""
    reason = _required(reason, minimum=3)
    await require_ref(ctx.deps, "EVENT", event_id)
    path = {"event_id": str(event_id)}
    event = await read_event(ctx.deps.backend, path["event_id"])
    if event.get("status") != "PUBLISHED":
        raise ToolError("EVENT_LOCKED")
    before = snapshot(Operation.UPDATE_EVENT_VISIBILITY, event, path)
    label = {True: "Công khai", False: "Đã ẩn"}
    display = {
        "rows": [
            _row("Sự kiện", event["title"]),
            _row("Hiển thị", f"{label[before['visible']]} → {label[visible]}"),
            _row("Lý do", reason),
        ]
    }
    return path, {"visible": visible, "reason": reason}, display, {"before": before}


def _revision(session, session_id):
    value = as_record(session, session_id).get("revision")
    if type(value) is not int or value < 0 or session.get("status") != "DRAFT":
        raise ToolError("INVALID_TOOL_RESULT")
    return value


@_admin_proposal(Operation.SEAT_LAYOUT)
async def propose_seat_layout(
    ctx: RunContext[TurnDeps],
    upload_ids: Annotated[list[UUID], Field(min_length=1, max_length=4)],
    layout_name: LayoutName,
    layout_type: Literal["FOOTBALL", "CONCERT"],
    venue_id: UUID | None = None,
    overview_id: UUID | None = None,
    venue_name: LayoutName | None = None,
    address: Annotated[str, Field(max_length=500)] | None = None,
    city: Annotated[str, Field(max_length=100)] | None = None,
) -> dict:
    """Xem trước SVG đính kèm, rồi đề xuất lưu sơ đồ. Sân mới cần tên, địa chỉ và thành phố. → completeVenueImport"""
    deps = ctx.deps
    layout_name = _required(layout_name)
    if len(set(upload_ids)) != len(upload_ids) or (overview_id is not None and venue_id is None):
        raise ToolError("INVALID_ARGUMENT")
    minimum_files = 1 if overview_id is not None else 2
    if len(upload_ids) < minimum_files:
        raise ToolError("INVALID_ARGUMENT")
    draft = {"layout_name": layout_name, "layout_type": layout_type}
    create = {}
    if venue_id is None:
        draft["venue"] = {"name": _required(venue_name), "address": _required(address), "city": _required(city)}
    else:
        await require_ref(deps, "VENUE", venue_id)
        create["target_venue_id"] = str(venue_id)
    if overview_id is not None:
        await require_ref(deps, "OVERVIEW", overview_id)
        create["overview_id"] = str(overview_id)
    for upload_id in upload_ids:
        await require_ref(deps, "UPLOAD", upload_id)
    uploads = await get_uploads(deps.principal.user_id, upload_ids, with_content=True)
    if set(uploads) != set(upload_ids):
        raise ToolError("INVALID_REFERENCE")

    backend = deps.backend
    session = await backend.stage_import("createVenueImport", body=create)
    session_id = uuid_text(session.get("id"))
    path = {"session_id": session_id}
    file_ids = set()
    for index, upload_id in enumerate(upload_ids, 1):
        session = await backend.stage_import(
            "uploadVenueImportFiles",
            path=path,
            form={"revision": str(_revision(session, session_id))},
            files=[("files", (f"layout-{index}.svg", uploads[upload_id], "image/svg+xml"))],
        )
        for file in rows(session, "files"):
            if file.get("role") == "SEAT_LAYOUT":
                file_ids.add(uuid_text(file.get("id")))
    draft["file_layout_types"] = dict.fromkeys(file_ids, layout_type)
    session = await backend.stage_import(
        "updateVenueImport",
        path=path,
        body={"revision": _revision(session, session_id), "draft_data": draft},
    )
    revision = _revision(session, session_id)
    body = {"revision": revision}
    preview = await backend.read("previewVenueImport", path=path, body=body)
    if preview.get("session_id") != session_id or preview.get("revision") != revision:
        raise ToolError("INVALID_TOOL_RESULT")
    if preview.get("valid") is not True:
        raise ToolError("INVALID_SEAT_LAYOUT")
    zones = rows(preview, "zones")
    counts = [zone.get("seat_count") for zone in zones]
    if any(type(count) is not int or count < 0 for count in counts):
        raise ToolError("INVALID_TOOL_RESULT")
    display = {
        "rows": [
            _row("Sơ đồ", layout_name),
            _row("Loại", layout_type),
            _row("Số khu", len(zones)),
            _row("Số ghế", sum(counts)),
        ]
    }
    return path, body, display


ADMIN_WORKFLOW_TOOLS = (
    propose_cancel_event,
    propose_cancel_showtime,
    propose_seat_layout,
    propose_update_event_visibility,
)
