"""Dựng block từ kết quả đã lọc (V2.3 mục 3.3). Cùng loại block trong một turn thì giữ cái mới nhất."""

from typing import Any

from pydantic import BaseModel, ValidationError

from vingo_agent.schemas import blocks as schema
from vingo_agent.tools.base import ToolError

_CATALOG_CHANGE_LABELS = {
    "name": "Tên sân",
    "address": "Địa chỉ",
    "city": "Thành phố",
    "district": "Quận/huyện",
    "lat": "Vĩ độ",
    "lng": "Kinh độ",
    "rules": "Nội quy",
    "title": "Tên sự kiện",
    "category": "Loại sự kiện",
    "description": "Mô tả",
    "max_tickets_per_order": "Vé tối đa mỗi đơn",
    "seat_layout_id": "Mã sơ đồ",
    "home_team": "Đội nhà",
    "away_team": "Đội khách",
    "league": "Giải đấu",
    "round": "Vòng đấu",
    "poster_mobile_url": "Poster di động",
    "poster_desktop_url": "Poster desktop",
    "sale_opens_at": "Mở bán",
    "sale_closes_at": "Đóng bán",
    "starts_at": "Bắt đầu",
    "ends_at": "Kết thúc",
    "price": "Giá vé (đ)",
    "effective_from": "Hiệu lực từ",
    "reason": "Lý do",
}


def catalog_change_rows(display: dict) -> list[schema.ConfirmRow]:
    def row(label, before, after):
        return schema.ConfirmRow(
            label=label, value=f"{'—' if before is None else before} → {'—' if after is None else after}"
        )

    def keyed(items, key):
        return {item.get(key) or item["name"]: item for item in items}

    def tier(item):
        if item is None:
            return None
        suffix = f" · {item['color']}" if item.get("color") else ""
        return f"{item['name']} · {item['price']:,} đ{suffix}"

    def zone(item):
        if item is None:
            return None
        names = display["tier_names"]
        tier_id = item["price_tier_id"]
        name = names.get(tier_id, tier_id)
        return f"{item['display_name']} · {name}" if item.get("display_name") else name

    rows = []
    if "tier_name" in display:
        rows.append(schema.ConfirmRow(label="Hạng vé", value=display["tier_name"]))
    for change in display["changes"]:
        field, before, after = change["field"], change["before"], change["after"]
        if field in {"price_tiers", "zone_prices"}:
            is_tier = field == "price_tiers"
            key = "id" if is_tier else "zone_id"
            render = tier if is_tier else zone
            old, new = keyed(before, key), keyed(after, key)
            zones = {item["zone_id"]: item["zone_name"] for item in display.get("preview", {}).get("zones", [])}
            for identifier in dict.fromkeys((*old, *new)):
                original, proposed = render(old.get(identifier)), render(new.get(identifier))
                if original != proposed:
                    index = len(rows) + 1
                    label = f"Hạng vé {index}" if is_tier else f"Khu {index} · {zones.get(identifier, identifier)}"
                    rows.append(row(label, original, proposed))
        else:
            rows.append(row(_CATALOG_CHANGE_LABELS[field], before, after))
    if "preview" in display:
        rows.append(schema.ConfirmRow(label="Tổng SKU sau cập nhật", value=str(display["preview"]["total_sku_count"])))
    return rows or [schema.ConfirmRow(label="Trước và sau", value="Cấu hình giữ nguyên")]


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
    "getAdminLayoutTree": "AdminTable",
    "listAdminEvents": "AdminTable",
    "adminListTickets": "AdminTable",
    "adminListOrders": "AdminTable",
    "adminGetOrder": "OrderSummary",
    "listShowtimeCheckins": "AdminTable",
    "listAuditLogs": "AdminTable",
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
    if kind == "SeatMap":
        payload["seats"] = _string_numbers(payload["seats"])
    if kind == "AdminTable":
        table_kind = {
            "listVenues": "VENUES",
            "listSeatLayouts": "LAYOUTS",
            "getAdminLayoutTree": "LAYOUTS",
            "listAdminEvents": "EVENTS",
            "adminListTickets": "TICKETS",
        }[operation]
        items = payload.get("items", [])
        columns = {
            "listVenues": ["venue_id", "name", "city"],
            "listSeatLayouts": ["layout_id", "name", "layout_type", "version"],
            "getAdminLayoutTree": ["layout_id", "name", "version", "status", "overview_name"],
            "listAdminEvents": ["event_id", "title", "category", "status"],
        }.get(operation, list(items[0]) if items else [])
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
        if operation in (*_OPERATIONS_TABLES, "adminGetOrder"):
            return _build_operations_block(operation, data, role)
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
