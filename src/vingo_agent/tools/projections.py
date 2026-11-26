"""G7: lọc kết quả backend về đúng field cho phép (V2.3 bảng mục 4.2, 4.3).

Thiếu field bắt buộc hoặc enum lạ → ToolError(INVALID_TOOL_RESULT). Field lạ bị bỏ.
Không bao giờ để lọt: qr_token, payment_url, email, phone, buyer_*.
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

from vingo_agent.tools.base import ToolError

FORBIDDEN_FIELDS = frozenset({"qr_token", "payment_url", "email", "phone", "buyer_name", "buyer_email", "buyer_phone"})
SEATMAP_MAX_SEATS = 60
MAX_TEXT_LENGTH = 2000

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


def _seatmap(raw: dict, scope: dict[str, UUID], row: str | None) -> tuple[dict, bool]:
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
    context = {"truncated": False}
    seats = []
    count = 0
    for seat in zone.seats or []:
        if seat.status != "AVAILABLE" or (selected_row is not None and seat.row.casefold() != selected_row):
            continue
        count += 1
        if len(seats) < SEATMAP_MAX_SEATS:
            seats.append(seat.model_dump(mode="json", context=context) | {"price": price})
    return {"available_count": count, "seats": seats}, count > SEATMAP_MAX_SEATS or context["truncated"]


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


def project(
    operation: str, raw: dict[str, Any], *, role: str, scope: dict[str, UUID] | None = None, row: str | None = None
) -> tuple[dict[str, Any], bool]:
    """Lọc theo allowlist; scope là các Ref đã kiểm để đối chiếu kết quả theo ID."""
    if role != "BUYER":
        raise NotImplementedError
    scope = scope or {}
    try:
        if operation == "getSeatmap":
            return _seatmap(raw, scope, row)
        if operation == "getMe":
            profile = _Profile.model_validate(raw)
            return {"has_full_name": bool((profile.full_name or "").strip()), "has_phone": bool(profile.phone)}, False
        model = _BUYER_MODELS.get(operation)
        if model is None:
            raise NotImplementedError
        view = model.model_validate(raw)
        for field, value in scope.items():
            if field in type(view).model_fields and getattr(view, field) != value:
                raise ToolError("INVALID_TOOL_RESULT")
        context = {"truncated": False}
        data = view.model_dump(mode="json", exclude_unset=True, context=context)
        return data, context["truncated"]
    except ValidationError:
        raise ToolError("INVALID_TOOL_RESULT") from None
