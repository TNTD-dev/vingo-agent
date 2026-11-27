"""G7: lọc kết quả backend về đúng field cho phép (V2.3 bảng mục 4.2, 4.3), trước Ref/block/envelope.

Hai cách khai báo, cùng chạy qua bound_result (giới hạn byte):
- Buyer: model pydantic trong file này (_BUYER_MODELS, getSeatmap, getMe) vì cần đổi tên field, đọc field
  lồng nhau, tính field mới và lọc ghế; FieldSpec chưa làm được các việc này.
- Operation khác: khai báo SPECS[operation] = {"items": FieldSpec(list, item=FieldSpec(dict,
  fields={"event_id": FieldSpec(str), "status": FieldSpec(str, enum=("DRAFT",))}))}.
  Dùng match="event_id" và project(..., requested={"event_id": id}) để kiểm ID đã hỏi. Field tùy chọn dùng
  required=False.

Thiếu field bắt buộc hoặc enum lạ → ToolError(INVALID_TOOL_RESULT). Field lạ bị bỏ.
Không bao giờ để lọt: qr_token, payment_url, email, phone, buyer_*, svg.
"""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AliasChoices,
    AliasPath,
    AwareDatetime,
    BaseModel,
    Field,
    PlainSerializer,
    SerializationInfo,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
    computed_field,
)

from vingo_agent.guardrails.result import FieldSpec, bound_result, validate_object
from vingo_agent.tools.base import ToolError

FORBIDDEN_FIELDS = frozenset(
    {"qr_token", "payment_url", "email", "phone", "buyer_name", "buyer_email", "buyer_phone", "svg"}
)
SEATMAP_MAX_SEATS = 60
MAX_TEXT_LENGTH = 2000
SPECS: dict[str, dict[str, FieldSpec]] = {}

_AWARE_TIME = TypeAdapter(AwareDatetime)


def _timestamp(value: str) -> str:
    if "T" not in value.upper() and " " not in value:
        raise ValueError("Cần ngày và giờ ISO 8601")
    _AWARE_TIME.validate_python(value)
    return value


def _bounded_text(value: str, info: SerializationInfo) -> str:
    if len(value) > MAX_TEXT_LENGTH:
        info.context["truncated"] = True
        return value[:MAX_TEXT_LENGTH]
    return value


Text = Annotated[str, PlainSerializer(_bounded_text)]
Timestamp = Annotated[str, AfterValidator(_timestamp)]
Price = Annotated[StrictInt, Field(ge=0)]
Category = Literal["FOOTBALL", "CONCERT"]
OrderStatus = Literal["PENDING", "PAID", "EXPIRED", "CANCELLED", "NEEDS_REVIEW", "REJECTED"]
TicketStatus = Literal["ISSUED", "USED", "VOID"]
ShowtimeStatus = Literal["DRAFT", "SCHEDULED", "ON_SALE", "SALES_CLOSED", "FINISHED", "CANCELLED"]
Seating = Literal["STANDING", "SEATED"]


class _Page[T](BaseModel):
    items: list[T] = Field(validation_alias="data")
    next_cursor: str | None = None


class _Event(BaseModel):
    event_id: UUID = Field(validation_alias="id")
    title: Text
    category: Category
    city_name: Text | None = Field(default=None, validation_alias=AliasChoices("city_name", "city"))
    venue_name: Text | None = None
    first_starts_at: Timestamp | None = None
    price_from: Price | None = None
    on_sale: StrictBool


class _Tier(BaseModel):
    name: Text
    price: Price


class _EventDetail(BaseModel):
    event_id: UUID = Field(validation_alias="id")
    title: Text
    category: Category
    status: Literal["DRAFT", "PUBLISHED", "CANCELLED"]
    description: Text | None = None
    venue_name: Text = Field(validation_alias=AliasPath("venue", "name"))
    max_tickets_per_order: Annotated[StrictInt, Field(ge=1)]
    tiers: list[_Tier] = Field(validation_alias="price_tiers")


class _Venue(BaseModel):
    name: Text | None = None


class _Showtime(BaseModel):
    showtime_id: UUID = Field(validation_alias="id")
    starts_at: Timestamp
    ends_at: Timestamp | None = None
    venue: _Venue | None = Field(default=None, exclude=True)
    status: ShowtimeStatus
    sold_out: StrictBool
    sale_opens_at: Timestamp | None = None

    @computed_field
    @property
    def venue_name(self) -> Text | None:
        return self.venue.name if self.venue is not None else None


class _Zone(BaseModel):
    zone_id: UUID
    name: Text
    seating: Seating = Field(validation_alias="kind")
    tier: _Tier | None = Field(default=None, exclude=True)
    seats_left: StrictInt

    @computed_field
    @property
    def tier_name(self) -> Text | None:
        return self.tier.name if self.tier is not None else None

    @computed_field
    @property
    def price(self) -> Price | None:
        return self.tier.price if self.tier is not None else None


class _Availability(BaseModel):
    showtime_id: UUID
    status: ShowtimeStatus
    sold_out: StrictBool
    zones: list[_Zone]


class _Seat(BaseModel):
    seat_id: UUID
    row: Text
    number: StrictInt
    status: Literal["AVAILABLE", "HELD", "SOLD", "BLOCKED"] = Field(exclude=True)


class _SeatZone(BaseModel):
    zone_id: UUID
    kind: Seating
    tier: _Tier | None = None
    seats: list[_Seat] | None = None


class _SeatMap(BaseModel):
    showtime_id: UUID
    status: ShowtimeStatus
    zones: list[_SeatZone]


def _seatmap(raw: dict, scope: dict, row: str | None) -> tuple[dict, bool]:
    seatmap = _SeatMap.model_validate(raw)
    if "showtime_id" in scope and seatmap.showtime_id != scope["showtime_id"]:
        raise ToolError("INVALID_TOOL_RESULT")
    zones = [zone for zone in seatmap.zones if "zone_id" not in scope or zone.zone_id == scope["zone_id"]]
    if len(zones) != 1:
        raise ToolError("INVALID_TOOL_RESULT")
    zone = zones[0]
    if zone.kind == "SEATED" and zone.seats is None:
        raise ToolError("INVALID_TOOL_RESULT")
    price = zone.tier.price if zone.tier is not None else None
    selected_row = row.casefold() if row is not None else None
    selected_ids = scope.get("seat_ids")
    context = {"truncated": False}
    seats = []
    count = 0
    for seat in zone.seats or []:
        if seat.status != "AVAILABLE" or (selected_row is not None and seat.row.casefold() != selected_row):
            continue
        if selected_ids is not None and seat.seat_id not in selected_ids:
            continue
        count += 1
        if selected_ids is not None or len(seats) < SEATMAP_MAX_SEATS:
            seats.append(seat.model_dump(mode="json", context=context) | {"price": price})
    return {"available_count": count, "seats": seats}, count > len(seats) or context["truncated"]


class _SuggestedSeat(BaseModel):
    seat_id: UUID
    row: Text
    number: StrictInt
    row_rank: Annotated[StrictInt, Field(ge=1)]


class _Suggestion(BaseModel):
    showtime_id: UUID = Field(exclude=True)
    zone_id: UUID = Field(exclude=True)
    quantity: Annotated[StrictInt, Field(ge=1, le=10, exclude=True)]
    currency: Literal["VND"] = Field(exclude=True)
    observed_at: Timestamp = Field(exclude=True)
    seats: list[_SuggestedSeat]
    together: StrictBool
    seats_left: Annotated[StrictInt, Field(ge=0)]
    unit_price: Price | None
    total: Price | None


class _Profile(BaseModel):
    full_name: str | None = None
    phone: str | None = None


class _OrderLine(BaseModel):
    zone: Text
    row: Text | None = None
    number: StrictInt | None = None
    price: Price = Field(validation_alias="unit_price")
    quantity: Annotated[StrictInt, Field(ge=1)]


class _Order(BaseModel):
    order_id: UUID = Field(validation_alias="id")
    code: Text
    status: OrderStatus
    showtime_id: UUID = Field(validation_alias=AliasPath("showtime", "id"))
    event_title: Text = Field(validation_alias=AliasPath("event", "title"))
    starts_at: Timestamp = Field(validation_alias=AliasPath("showtime", "starts_at"))
    items: list[_OrderLine]
    total_amount: Price
    expires_at: Timestamp | None = None
    paid_at: Timestamp | None = None


class _Payment(BaseModel):
    order_id: UUID
    status: Literal["OPEN", "SUCCEEDED", "FAILED", "EXPIRED"]
    amount: Price
    expires_at: Timestamp


class _Ticket(BaseModel):
    ticket_id: UUID = Field(validation_alias="id")
    code: Text
    status: TicketStatus
    order_code: Text
    event_title: Text = Field(validation_alias=AliasPath("event", "title"))
    starts_at: Timestamp = Field(validation_alias=AliasPath("showtime", "starts_at"))
    zone: Text
    row: Text | None = None
    number: StrictInt | None = None


_BUYER_MODELS = {
    "searchEvents": _Page[_Event],
    "getEvent": _EventDetail,
    "listEventShowtimes": _Page[_Showtime],
    "getAvailability": _Availability,
    "suggestSeats": _Suggestion,
    "listMyOrders": _Page[_Order],
    "getOrder": _Order,
    "getPayment": _Payment,
    "listMyTickets": _Page[_Ticket],
}


class _AdminVenue(BaseModel):
    venue_id: UUID = Field(validation_alias="id")
    name: Text
    city: Text
    address: Text | None = None


class _AdminLayout(BaseModel):
    layout_id: UUID = Field(validation_alias="id")
    venue_id: UUID
    name: Text


class _AdminZone(BaseModel):
    zone_id: UUID = Field(validation_alias="id")
    name: Text = Field(validation_alias=AliasChoices("label", "name"))
    capacity: StrictInt | None = None


class _AdminLayoutDetail(_AdminLayout):
    zones: list[_AdminZone]


class _AdminEvent(BaseModel):
    event_id: UUID = Field(validation_alias="id")
    title: Text
    category: Category
    status: Literal["DRAFT", "PUBLISHED", "CANCELLED"]


class _AdminShowtime(BaseModel):
    showtime_id: UUID = Field(validation_alias="id")
    layout_id: UUID = Field(validation_alias="seat_layout_id")
    starts_at: Timestamp
    status: ShowtimeStatus


class _AdminTier(_Tier):
    price_tier_id: UUID = Field(validation_alias=AliasChoices("id", "ticket_type_id"))
    color: str | None = None


class _AdminAssignment(BaseModel):
    zone_id: UUID
    price_tier_id: UUID = Field(validation_alias=AliasChoices("price_tier_id", "ticket_type_id"))
    display_name: Text | None = None


class _AdminEventDetail(_AdminEvent):
    layout_id: UUID | None = Field(default=None, validation_alias="seat_layout_id")
    showtimes: list[_AdminShowtime]
    price_tiers: list[_AdminTier]
    zone_prices: list[_AdminAssignment]


class _AdminZonePreview(BaseModel):
    zone_id: UUID
    zone_name: Text
    sku_count: Annotated[StrictInt, Field(ge=0)]


class _AdminPricePreview(BaseModel):
    event_id: UUID
    total_sku_count: Annotated[StrictInt, Field(ge=0)]
    zones: list[_AdminZonePreview]


_ADMIN_CATALOG = {
    "listVenues": _Page[_AdminVenue],
    "getAdminVenue": _AdminVenue,
    "listSeatLayouts": _Page[_AdminLayout],
    "getSeatLayout": _AdminLayoutDetail,
    "listAdminEvents": _Page[_AdminEvent],
    "getAdminEvent": _AdminEventDetail,
    "previewZonePrices": _AdminPricePreview,
}


def project(
    operation: str,
    raw: dict[str, Any],
    *,
    role: str,
    requested: dict[str, Any] | None = None,
    row: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Dựng object mới, kiểm schema và ID đã hỏi (requested) rồi cắt list theo giới hạn byte.

    Operation có khai báo trong SPECS luôn dùng SPECS; chưa khai báo thì Buyer dùng model pydantic.
    """
    requested = requested or {}
    if operation in ("adminListOrders", "adminGetOrder", "adminListTickets", "listShowtimeCheckins", "listAuditLogs"):
        from vingo_agent.tools.admin_read import project_operations

        if role != "ADMIN":
            raise ToolError("FORBIDDEN")
        return project_operations(operation, raw, requested), False
    if operation in ("getAdminDashboard", "queryMetric"):
        from vingo_agent.tools.admin_read import project_reporting

        if role != "ADMIN":
            raise ToolError("FORBIDDEN")
        return bound_result(project_reporting(operation, raw, requested))
    if operation in _ADMIN_CATALOG and role == "ADMIN":
        try:
            view = _ADMIN_CATALOG[operation].model_validate(raw)
            records = view.items if isinstance(view, _Page) else [view]
            if any(str(getattr(item, key, None)) != str(value) for item in records for key, value in requested.items()):
                raise ToolError("INVALID_TOOL_RESULT")
            context = {"truncated": False}
            data, bounded = bound_result(view.model_dump(mode="json", context=context))
            return data, bounded or context["truncated"]
        except ValidationError:
            raise ToolError("INVALID_TOOL_RESULT") from None
    if operation in SPECS:
        return bound_result(validate_object(raw, SPECS[operation], requested))
    if role == "BUYER" and (operation in _BUYER_MODELS or operation in ("getSeatmap", "getMe")):
        data, truncated = _project_buyer(operation, raw, requested, row)
        data, bounded = bound_result(data)
        return data, truncated or bounded
    raise ToolError("INVALID_TOOL_RESULT")


def _project_buyer(
    operation: str, raw: dict[str, Any], scope: dict[str, Any], row: str | None
) -> tuple[dict[str, Any], bool]:
    try:
        if operation == "getSeatmap":
            return _seatmap(raw, scope, row)
        if operation == "getMe":
            profile = _Profile.model_validate(raw)
            return {"has_full_name": bool((profile.full_name or "").strip()), "has_phone": bool(profile.phone)}, False
        view = _BUYER_MODELS[operation].model_validate(raw)
        for field, value in scope.items():
            if field in type(view).model_fields and getattr(view, field) != value:
                raise ToolError("INVALID_TOOL_RESULT")
        context = {"truncated": False}
        data = view.model_dump(mode="json", exclude_unset=True, context=context)
        # Kiểm đề xuất trên đúng đối tượng trước khi bound_result cắt danh sách dài cho model.
        for op, collection, key in (
            ("getAvailability", "zones", "zone_id"),
            ("listMyOrders", "items", "showtime_id"),
            ("listMyTickets", "items", "ticket_id"),
            ("listEventShowtimes", "items", "showtime_id"),
        ):
            if operation == op and key in scope:
                data[collection] = [item for item in data[collection] if item[key] == str(scope[key])]
        return data, context["truncated"]
    except ValidationError:
        raise ToolError("INVALID_TOOL_RESULT") from None
