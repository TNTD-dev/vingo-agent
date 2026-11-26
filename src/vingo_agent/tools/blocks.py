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


def build_block(operation: str, data: dict[str, Any], *, role: str) -> BaseModel | None:
    if role != "BUYER":
        raise NotImplementedError
    try:
        match BLOCK_FOR_OPERATION.get(operation):
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
                return None if data["status"] == "NONE" else schema.PaymentStatus.model_validate(data)
            case "TicketList":
                return schema.TicketList(items=_string_numbers(data["items"]))
            case None:
                return None
            case _:
                raise NotImplementedError
    except ValidationError:
        raise ToolError("INVALID_TOOL_RESULT") from None
