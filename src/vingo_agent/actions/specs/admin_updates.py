"""Snapshot và tra kết quả cho các thao tác cập nhật catalog Admin."""

from functools import partial
from math import isfinite

from vingo_agent.actions.proposals import BACKEND_OPERATION, Operation
from vingo_agent.actions.registry import LookupMode, OperationSpec, register
from vingo_agent.tools.base import ToolError
from vingo_agent.tools.catalog import as_record, price_tiers, rows, timestamp, uuid_text, zone_prices

SHOWTIME_TIMES = ("sale_opens_at", "sale_closes_at", "starts_at", "ends_at")
FOOTBALL_FIELDS = ("home_team", "away_team", "league", "round")
PATCH_IDS = {
    Operation.UPDATE_VENUE: "venue_id",
    Operation.UPDATE_EVENT: "event_id",
    Operation.UPDATE_SHOWTIME: "showtime_id",
}
_COLLECTION = {Operation.UPDATE_PRICE_TIERS: price_tiers, Operation.UPDATE_ZONE_PRICES: zone_prices}
_CITIES = {
    "Hà Nội": "HN",
    "TP. Hồ Chí Minh": "HCM",
    "Hồ Chí Minh": "HCM",
    "Thành phố Hồ Chí Minh": "HCM",
    "Đà Nẵng": "DN",
}


def venue_city(value: str) -> str:
    return _CITIES.get(value, value)


async def read_target(operation, *, backend, path, event_id=None):
    if operation == Operation.UPDATE_VENUE:
        return as_record(await backend.read("getAdminVenue", path=path), path["venue_id"]), None
    event_id = event_id if operation == Operation.UPDATE_SHOWTIME else path["event_id"]
    event_path = {"event_id": event_id}
    event = as_record(await backend.read("getAdminEvent", path=event_path), event_id)
    if operation == Operation.UPDATE_SHOWTIME:
        target = next((item for item in rows(event, "showtimes") if item.get("id") == path["showtime_id"]), None)
    elif operation == Operation.ADD_TIER_PRICE:
        target = next((item for item in rows(event, "price_tiers") if item.get("id") == path["tier_id"]), None)
    else:
        target = event
    if target is None:
        raise ToolError("INVALID_REFERENCE")
    return target, event


def _patch_value(target, field):
    value = target.get(field)
    if field in SHOWTIME_TIMES:
        return timestamp(value)
    if value is None:
        if field in {"name", "address", "city", "title", "category", "max_tickets_per_order"}:
            raise ToolError("INVALID_TOOL_RESULT")
        return None
    if field == "seat_layout_id":
        return uuid_text(value)
    if field in {"lat", "lng"}:
        if type(value) not in (int, float) or not isfinite(value):
            raise ToolError("INVALID_TOOL_RESULT")
    elif field == "max_tickets_per_order":
        if type(value) is not int:
            raise ToolError("INVALID_TOOL_RESULT")
    elif not isinstance(value, str):
        raise ToolError("INVALID_TOOL_RESULT")
    return venue_city(value) if field == "city" else value


def snapshot(operation, target, body):
    if operation == Operation.ADD_TIER_PRICE:
        price = target.get("price")
        if type(price) is not int or price < 0:
            raise ToolError("INVALID_TOOL_RESULT")
        return {"price": price, "price_ids": sorted(uuid_text(item.get("id")) for item in rows(target, "prices"))}
    if operation in _COLLECTION:
        return _COLLECTION[operation](target)
    fields = dict.fromkeys(body)
    if operation == Operation.UPDATE_SHOWTIME and fields.keys() & set(SHOWTIME_TIMES):
        fields.update(dict.fromkeys(SHOWTIME_TIMES))
    if operation == Operation.UPDATE_EVENT and body.get("category") == "CONCERT":
        fields.update(dict.fromkeys(FOOTBALL_FIELDS))
    return {field: _patch_value(target, field) for field in fields}


async def _current(operation, backend, payload):
    target, event = await read_target(
        operation, backend=backend, path=payload["path"], event_id=payload.get("event_id")
    )
    if "layout_id" in payload and _patch_value(event, "seat_layout_id") != payload["layout_id"]:
        return None
    return snapshot(operation, target, payload["body"])


async def _check_before(operation, *, backend, payload):
    before = payload.get("before")
    if before is None:
        return False
    try:
        return await _current(operation, backend, payload) == before
    except ToolError as error:
        if error.code in {"NOT_FOUND", "INVALID_REFERENCE"}:
            return False
        raise


def _tiers_match(current, desired):
    if len(current) != len(desired):
        return False
    by_name = {tier["name"]: tier for tier in current}
    return all(
        tier["name"] in by_name and all(by_name[tier["name"]][key] == value for key, value in tier.items())
        for tier in desired
    )


async def _reconcile_retry(operation, *, backend, payload):
    if "before" not in payload:
        return None
    current = await _current(operation, backend, payload)
    if current is None:
        return None
    desired = payload["body"]
    if operation == Operation.UPDATE_PRICE_TIERS:
        applied = _tiers_match(current, desired)
    elif operation == Operation.UPDATE_ZONE_PRICES:
        applied = current == zone_prices({"zone_prices": desired})
    else:
        expected = {field: _patch_value(desired, field) for field in desired}
        if operation == Operation.UPDATE_EVENT and desired.get("category") == "CONCERT":
            expected.update(dict.fromkeys(FOOTBALL_FIELDS))
        applied = all(current[field] == value for field, value in expected.items())
    if applied:
        field = PATCH_IDS.get(operation)
        return {field: payload["path"][field]} if field else {}
    return True if current == payload["before"] else None


def _result(field, data):
    if field is None or data.get("id") is None:
        return {}
    try:
        return {field: uuid_text(data["id"])}
    except ToolError:
        return {}


for operation in (*PATCH_IDS, *_COLLECTION, Operation.ADD_TIER_PRICE):
    field = PATCH_IDS.get(operation)
    retry = operation != Operation.ADD_TIER_PRICE
    register(
        operation,
        OperationSpec(
            backend_operation=BACKEND_OPERATION[operation],
            tool_name="propose_" + operation.lower(),
            role="ADMIN",
            project=partial(_result, field),
            result_refs={field: field.removesuffix("_id").upper()} if field else {},
            check_before=partial(_check_before, operation),
            safe_retry=retry,
            lookup_mode=LookupMode.RETRY if retry else LookupMode.NONE,
            reconcile_retry=partial(_reconcile_retry, operation) if retry else None,
            messages={"UNKNOWN": "Chưa rõ giá mới đã được thêm. Hãy kiểm tra lịch sử giá trên trang quản trị."}
            if not retry
            else {},
        ),
    )
