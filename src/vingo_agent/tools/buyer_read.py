"""11 tool đọc của Buyer (V2.3 mục 4.2). Docstring là mô tả tool cho model.

Mỗi tool: kiểm Ref → gọi backend → lọc kết quả (projections) → ghi Ref → thêm block → trả envelope.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictInt
from pydantic_ai import RunContext

from vingo_agent.tools import blocks, projections
from vingo_agent.tools.base import ResourceType, ToolError, TurnDeps, fail, ok, record_refs, require_ref
from vingo_agent.tools.projections import Category, OrderStatus, TicketStatus

PageLimit = Annotated[int, Field(strict=True, ge=1, le=20)]
PriceFilter = Annotated[StrictInt, Field(ge=0)]

_INPUT_REFS: dict[str, ResourceType] = {
    "event_id": "EVENT",
    "showtime_id": "SHOWTIME",
    "zone_id": "ZONE",
    "order_id": "ORDER",
}


async def _read(
    deps: TurnDeps,
    operation: str,
    *,
    ids: dict[str, UUID] | None = None,
    query: dict | None = None,
    row: str | None = None,
    collection: str | None = None,
    records: tuple[ResourceType, ...] = (),
) -> dict:
    ids = ids or {}
    try:
        role = deps.principal.role
        if role != "BUYER":
            raise ToolError("FORBIDDEN")
        for field, value in ids.items():
            await require_ref(deps, _INPUT_REFS[field], value)
        path = {field: value for field, value in ids.items() if field != "zone_id"}
        params = {key: value for key, value in (query or {}).items() if value is not None}
        if "zone_id" in ids:
            params["zone_id"] = str(ids["zone_id"])
        try:
            raw = await deps.backend.read(operation, path=path, query=params)
        except ToolError as err:
            if operation == "getPayment" and err.http_status == 404:
                return ok(
                    operation, {"order_id": str(ids["order_id"]), "status": "NONE"}, observed_at=datetime.now(UTC)
                )
            raise
        observed_at = datetime.now(UTC)
        data, truncated = projections.project(operation, raw, role=role, requested=ids, row=row)
        for kind in records:
            items = data[collection] if collection else [data]
            field = f"{kind.lower()}_id"
            ref_ids = [UUID(item[field]) for item in items if item.get(field) is not None]
            if ref_ids:
                await record_refs(deps, kind, ref_ids)
        block_data = data | ids if operation in ("getSeatmap", "suggestSeats") else data
        block = blocks.build_block(operation, block_data, role=role)
        if block is not None:
            for index, previous in enumerate(deps.blocks):
                if type(previous) is type(block):
                    deps.blocks[index] = block
                    break
            else:
                deps.blocks.append(block)
        return ok(operation, data, truncated=truncated, observed_at=observed_at)
    except ToolError as err:
        return fail(operation, err)


async def search_events(
    ctx: RunContext[TurnDeps],
    q: Annotated[str, Field(min_length=1, max_length=200)] | None = None,
    category: Category | None = None,
    city: Annotated[str, Field(max_length=100)] | None = None,
    from_: Annotated[AwareDatetime | None, Field(alias="from")] = None,
    to: AwareDatetime | None = None,
    price_min: PriceFilter | None = None,
    price_max: PriceFilter | None = None,
    sort: Literal["relevance", "starts_at", "price", "popularity"] | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Tìm sự kiện theo từ khóa, loại, thành phố, khoảng thời gian [from, to) và khoảng giá VND.

    Hiểu ngày theo giờ Việt Nam. Chỉ chọn relevance khi có từ khóa. Giữ nguyên bộ lọc và sort khi dùng cursor.
    → searchEvents"""
    return await _read(
        ctx.deps,
        "searchEvents",
        query={
            "q": q,
            "category": category,
            "city": city,
            "from": from_.isoformat() if from_ else None,
            "to": to.isoformat() if to else None,
            "price_min": price_min,
            "price_max": price_max,
            "sort": sort,
            "cursor": cursor,
            "limit": limit,
        },
        collection="items",
        records=("EVENT",),
    )


async def get_event(ctx: RunContext[TurnDeps], event_id: UUID) -> dict:
    """Xem chi tiết sự kiện đã chọn, gồm sân, hạng vé, giá và số vé tối đa mỗi đơn. → getEvent"""
    return await _read(ctx.deps, "getEvent", ids={"event_id": event_id})


async def list_event_showtimes(ctx: RunContext[TurnDeps], event_id: UUID) -> dict:
    """Xem các suất của sự kiện đã chọn, giờ mở bán và suất nào đã hết vé. → listEventShowtimes"""
    return await _read(
        ctx.deps, "listEventShowtimes", ids={"event_id": event_id}, collection="items", records=("SHOWTIME",)
    )


async def get_availability(ctx: RunContext[TurnDeps], showtime_id: UUID) -> dict:
    """Xem số chỗ còn, giá vé và loại khu đứng (STANDING) hoặc khu ngồi (SEATED) của suất. → getAvailability"""
    return await _read(
        ctx.deps, "getAvailability", ids={"showtime_id": showtime_id}, collection="zones", records=("ZONE",)
    )


async def get_seatmap(ctx: RunContext[TurnDeps], showtime_id: UUID, zone_id: UUID, row: str | None = None) -> dict:
    """Tra ghế cụ thể trong khu ngồi. Trả tối đa 60 ghế trống theo hàng. Nếu bị cắt, hỏi người dùng chọn hàng.
    → getSeatmap"""
    return await _read(
        ctx.deps,
        "getSeatmap",
        ids={"showtime_id": showtime_id, "zone_id": zone_id},
        row=row,
        collection="seats",
        records=("SEAT",),
    )


async def suggest_seats(
    ctx: RunContext[TurnDeps],
    showtime_id: UUID,
    zone_id: UUID,
    quantity: Annotated[int, Field(strict=True, ge=1, le=10)],
    position: Literal["FRONT", "MIDDLE", "BACK"] | None = None,
) -> dict:
    """Gợi ý 1–10 ghế chỉ cho khu ngồi, ưu tiên ghế có số liền nhau trong cùng hàng.

    position chỉ xếp thứ tự ưu tiên các hàng trong khu đã chọn. → suggestSeats"""
    return await _read(
        ctx.deps,
        "suggestSeats",
        ids={"showtime_id": showtime_id, "zone_id": zone_id},
        query={"quantity": quantity, "position": position},
        collection="seats",
        records=("SEAT",),
    )


async def get_me(ctx: RunContext[TurnDeps]) -> dict:
    """Trả has_full_name và has_phone để nhắc người dùng bổ sung thông tin còn thiếu trước khi hiện thẻ thanh toán.
    → getMe"""
    return await _read(ctx.deps, "getMe")


async def list_my_orders(
    ctx: RunContext[TurnDeps],
    status: OrderStatus | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Tìm đơn của người dùng hiện tại theo trạng thái, dùng cursor để xem trang tiếp. → listMyOrders"""
    return await _read(
        ctx.deps,
        "listMyOrders",
        query={"status": status, "cursor": cursor, "limit": limit},
        collection="items",
        records=("ORDER", "SHOWTIME"),
    )


async def get_order(ctx: RunContext[TurnDeps], order_id: UUID) -> dict:
    """Xem trạng thái mới nhất, chi tiết vé, tổng tiền và hạn giữ chỗ của đơn đã chọn. → getOrder"""
    return await _read(ctx.deps, "getOrder", ids={"order_id": order_id}, records=("SHOWTIME",))


async def get_payment(ctx: RunContext[TurnDeps], order_id: UUID) -> dict:
    """Tra cùng get_order khi người dùng hỏi đã thanh toán chưa. Trả trạng thái, số tiền và hạn thanh toán.
    → getPayment"""
    return await _read(ctx.deps, "getPayment", ids={"order_id": order_id})


async def list_my_tickets(
    ctx: RunContext[TurnDeps],
    when: Literal["upcoming", "past"] | None = None,
    status: TicketStatus | None = None,
    cursor: str | None = None,
    limit: PageLimit = 10,
) -> dict:
    """Tìm vé của người dùng hiện tại để xem vé, mở QR trên thẻ vé hoặc chọn vé gửi lại.
    Bỏ when tương đương when=upcoming (vé sắp tới). Dùng when=past để xem vé đã qua.
    Nếu người dùng không nêu thời gian, tra vé sắp tới trước. Nếu danh sách đó rỗng, gọi lại với when=past
    trước khi báo không có vé. → listMyTickets"""
    return await _read(
        ctx.deps,
        "listMyTickets",
        query={"when": when, "status": status, "cursor": cursor, "limit": limit},
        collection="items",
        records=("TICKET",),
    )


BUYER_READ_TOOLS = (
    search_events,
    get_event,
    list_event_showtimes,
    get_availability,
    get_seatmap,
    suggest_seats,
    get_me,
    list_my_orders,
    get_order,
    get_payment,
    list_my_tickets,
)
