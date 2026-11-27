"""Dựng block từ kết quả đã lọc (V2.3 mục 3.3). Cùng loại block trong một turn thì giữ cái mới nhất."""

from typing import Any

from pydantic import BaseModel, ValidationError

from vingo_agent.schemas import blocks as schema
from vingo_agent.tools.base import ToolError

# operation → loại block. Tool không có trong bảng thì không sinh block.
BLOCK_FOR_OPERATION: dict[str, str] = {
    "searchEvents": "EventList",
    "listEventShowtimes": "ShowtimeList",
    "getAvailability": "ZoneAvailability",
    "getSeatmap": "SeatMap",
    "suggestSeats": "SeatMap",
    "getOrder": "OrderSummary",
    "getPayment": "PaymentStatus",
    "listMyTickets": "TicketList",
    "getAdminDashboard": "StatCards",
    "queryMetric": "StatCards",
    "listVenues": "AdminTable",
    "listSeatLayouts": "AdminTable",
    "listAdminEvents": "AdminTable",
    "adminListTickets": "AdminTable",
}


def _string_numbers(items: list[dict]) -> list[dict]:
    return [item | {"number": str(item["number"])} if item.get("number") is not None else item for item in items]


def _buyer_block(operation: str, kind: str, data: dict[str, Any]) -> BaseModel:
    """Block Buyer dựng từ data đã lọc ở projections (_BUYER_MODELS)."""
    match kind:
        case "EventList":
            return schema.EventList(items=data["items"])
        case "ShowtimeList":
            return schema.ShowtimeList(items=data["items"])
        case "ZoneAvailability":
            priced_zones = [zone for zone in data["zones"] if zone["price"] is not None]
            return schema.ZoneAvailability(showtime_id=data["showtime_id"], zones=priced_zones)
        case "SeatMap":
            seats = _string_numbers(data["seats"])
            if operation == "suggestSeats":
                seats = [seat | {"price": data["unit_price"]} for seat in seats]
            count = data["seats_left"] if operation == "suggestSeats" else data["available_count"]
            return schema.SeatMap(
                showtime_id=data["showtime_id"], zone_id=data["zone_id"], available_count=count, seats=seats
            )
        case "OrderSummary":
            return schema.OrderSummary.model_validate(data | {"items": _string_numbers(data["items"])})
        case "PaymentStatus":
            return schema.PaymentStatus.model_validate(data)
        case "TicketList":
            return schema.TicketList(items=_string_numbers(data["items"]))
        case _:
            raise NotImplementedError


def _generic_block(operation: str, kind: str, data: dict[str, Any], role: str) -> BaseModel:
    payload = dict(data)
    if kind == "SeatMap" and role == "BUYER":
        payload["seats"] = [{k: v for k, v in seat.items() if k != "status"} for seat in payload.get("seats", [])]
    if kind == "AdminTable":
        table_kind = {
            "listVenues": "VENUES",
            "listSeatLayouts": "LAYOUTS",
            "listAdminEvents": "EVENTS",
            "adminListTickets": "TICKETS",
        }[operation]
        items = payload.get("items", [])
        columns = list(items[0]) if items else []
        payload = {"kind": table_kind, "columns": columns, "rows": [[row.get(key) for key in columns] for row in items]}
    if kind == "StatCards" and "from" in payload:
        payload["from_"] = payload.pop("from")
    return getattr(schema, kind).model_validate(payload)


def build_block(operation: str, data: dict[str, Any], *, role: str) -> BaseModel | None:
    kind = BLOCK_FOR_OPERATION.get(operation)
    if kind is None:
        return None
    if kind == "PaymentStatus" and data.get("status") == "NONE":
        return None
    try:
        if operation in ("getAdminDashboard", "queryMetric"):
            return _build_reporting_block(operation, data, role)
        if role == "BUYER":
            return _buyer_block(operation, kind, data)
        return _generic_block(operation, kind, data, role)
    except ValidationError:
        raise ToolError("INVALID_TOOL_RESULT") from None


def _reporting_card(operation: str, data: dict) -> dict:
    """Một StatCards theo schema hiện có; dashboard hiện doanh thu, số vé giữ trong tool result."""
    metadata = {key: data[key] for key in ("from", "to", "timezone", "computed_at")}
    if operation == "getAdminDashboard":
        return {
            **metadata,
            "metric": "revenue",
            "unit": "VND",
            "value": data["current"]["revenue"],
            "series": data["revenue"]["series"],
        }
    return {**metadata, "metric": data["metric"], "unit": data["unit"], "value": data["value"]}


def _build_reporting_block(operation: str, data: dict, role: str) -> BaseModel | None:
    from vingo_agent.schemas import blocks

    if role != "ADMIN":
        return None
    return blocks.StatCards.model_validate(_reporting_card(operation, data))
