"""Luồng xác nhận, snapshot và tra cứu cho KAN-203."""

from functools import partial
from uuid import UUID

from vingo_agent.actions.proposals import BACKEND_OPERATION, Operation
from vingo_agent.actions.registry import LookupMode, OperationSpec, register
from vingo_agent.tools.base import ToolError
from vingo_agent.tools.catalog import as_record, rows, timestamp, uuid_text


async def read_event(backend, event_id):
    return as_record(await backend.read("getAdminEvent", path={"event_id": event_id}), event_id)


def get_showtime(event, showtime_id):
    for item in rows(event, "showtimes"):
        if item.get("id") == showtime_id:
            return item
    raise ToolError("INVALID_REFERENCE")


def snapshot(operation, event, path):
    if operation == Operation.CANCEL_SHOWTIME:
        show = get_showtime(event, path["showtime_id"])
        return {
            "event_status": event["status"],
            "status": show["status"],
            "starts_at": timestamp(show.get("starts_at")),
        }
    if operation == Operation.CANCEL_EVENT:
        return {
            "status": event["status"],
            "title": event["title"],
            "showtimes": {uuid_text(item["id"]): item["status"] for item in rows(event, "showtimes")},
        }
    if type(event.get("visible")) is not bool:
        raise ToolError("INVALID_TOOL_RESULT")
    return {"status": event["status"], "visible": event["visible"]}


async def current(operation, backend, payload):
    path = payload["path"]
    event = await read_event(
        backend, payload["event_id"] if operation == Operation.CANCEL_SHOWTIME else path["event_id"]
    )
    try:
        return snapshot(operation, event, path)
    except KeyError:
        raise ToolError("INVALID_TOOL_RESULT") from None


async def check_before(operation, *, backend, payload):
    try:
        return await current(operation, backend, payload) == payload.get("before")
    except ToolError as error:
        if error.code in ("NOT_FOUND", "INVALID_REFERENCE"):
            return False
        raise


async def lookup_cancel(operation, *, backend, payload, idempotency_key):
    if (await current(operation, backend, payload))["status"] == "CANCELLED":
        return {}
    return None


async def reconcile_visibility(*, backend, payload):
    value = await current(Operation.UPDATE_EVENT_VISIBILITY, backend, payload)
    if value["status"] == "PUBLISHED" and value["visible"] == payload["body"]["visible"]:
        return {}
    return True if value == payload.get("before") else None


def imported_ids(data):
    try:
        return {key: str(UUID(str(data[key]))) for key in ("venue_id", "layout_id")}
    except (ValueError, TypeError):
        raise ValueError("Venue Import thiếu ID hợp lệ") from None


for operation in (Operation.CANCEL_EVENT, Operation.CANCEL_SHOWTIME):
    register(
        operation,
        OperationSpec(
            backend_operation=BACKEND_OPERATION[operation],
            tool_name="propose_" + operation.lower(),
            role="ADMIN",
            check_before=partial(check_before, operation),
            confirm_phrase=lambda payload: payload["confirm_phrase"],
            lookup_mode=LookupMode.READ,
            lookup=partial(lookup_cancel, operation),
        ),
    )

register(
    Operation.UPDATE_EVENT_VISIBILITY,
    OperationSpec(
        backend_operation="updateEventVisibility",
        tool_name="propose_update_event_visibility",
        role="ADMIN",
        check_before=partial(check_before, Operation.UPDATE_EVENT_VISIBILITY),
        lookup_mode=LookupMode.RETRY,
        safe_retry=True,
        reconcile_retry=reconcile_visibility,
    ),
)

register(
    Operation.SEAT_LAYOUT,
    OperationSpec(
        backend_operation="completeVenueImport",
        tool_name="propose_seat_layout",
        role="ADMIN",
        lookup_mode=LookupMode.RETRY,
        safe_retry=True,
        project=imported_ids,
        result_refs={"venue_id": "VENUE", "layout_id": "LAYOUT"},
    ),
)
