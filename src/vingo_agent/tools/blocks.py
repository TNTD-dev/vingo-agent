"""Dựng block từ kết quả đã lọc (V2.3 mục 3.3). Cùng loại block trong một turn thì giữ cái mới nhất."""

from typing import Any

from pydantic import BaseModel

# operation → loại block. Tool không có trong bảng thì không sinh block.
BLOCK_FOR_OPERATION: dict[str, str] = {
    "searchEvents": "EventList",
    "listEventShowtimes": "ShowtimeList",
    "getAvailability": "ZoneAvailability",
    "getSeatmap": "SeatMap",
    "getOrder": "OrderSummary",
    "getPayment": "PaymentStatus",
    "listMyTickets": "TicketList",
    "getTicket": "TicketList",
    "getAdminDashboard": "StatCards",
    "queryMetric": "StatCards",
    "listVenues": "AdminTable",
    "listSeatLayouts": "AdminTable",
    "listAdminEvents": "AdminTable",
    "adminListTickets": "AdminTable",
}


def build_block(operation: str, data: dict[str, Any], *, role: str) -> BaseModel | None:
    from vingo_agent.schemas import blocks

    if operation in (*_OPERATIONS_TABLES, "adminGetOrder"):
        return _build_operations_block(operation, data, role)
    kind = BLOCK_FOR_OPERATION.get(operation)
    if kind is None:
        return None
    payload = dict(data)
    if kind == "SeatMap" and role == "BUYER":
        payload["seats"] = [{k: v for k, v in seat.items() if k != "status"} for seat in payload.get("seats", [])]
    if kind == "TicketList" and operation == "getTicket":
        payload = {"items": [data]}
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
    return getattr(blocks, kind).model_validate(payload)


_OPERATIONS_TABLES = {
    "adminListOrders": (
        "ORDERS",
        ("order_id", "code", "status", "event_title", "starts_at", "total_amount", "created_at", "message"),
    ),
    "adminListTickets": (
        "TICKETS",
        ("ticket_id", "code", "status", "event_title", "starts_at", "zone", "row", "number"),
    ),
    "listShowtimeCheckins": ("CHECKINS", ("ticket_code", "result", "at")),
    "listAuditLogs": ("AUDIT_LOGS", ("action", "entity", "entity_id", "actor_name", "via_agent", "at")),
}


def _build_operations_block(operation: str, data: dict, role: str) -> BaseModel | None:
    from vingo_agent.schemas import blocks

    if role != "ADMIN":
        return None
    if operation == "adminGetOrder" and data["status"] != "NEEDS_REVIEW":
        return blocks.OrderSummary.model_validate(data)
    if operation == "adminGetOrder":
        kind, columns, items = "ORDERS", ("code", "status", "message"), [data]
    else:
        kind, columns = _OPERATIONS_TABLES[operation]
        items = data["items"]
    return blocks.AdminTable(
        kind=kind, columns=list(columns), rows=[[row.get(key) for key in columns] for row in items]
    )
