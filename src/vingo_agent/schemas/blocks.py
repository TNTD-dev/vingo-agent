"""Payload của 12 loại block (V2.3 mục 3.3). Block chỉ do code dựng từ kết quả tool đã lọc, model không tạo."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class EventItem(BaseModel):
    event_id: UUID
    title: str
    city_name: str | None = None
    venue_name: str | None = None
    first_starts_at: datetime | None = None
    price_from: int | None = None
    on_sale: bool | None = None


class EventList(BaseModel):
    type: Literal["EventList"] = "EventList"
    items: list[EventItem]


class ShowtimeItem(BaseModel):
    showtime_id: UUID
    starts_at: datetime
    venue_name: str | None = None
    status: str
    sold_out: bool


class ShowtimeList(BaseModel):
    type: Literal["ShowtimeList"] = "ShowtimeList"
    items: list[ShowtimeItem]


class Seating(StrEnum):
    STANDING = "STANDING"
    SEATED = "SEATED"


class ZoneItem(BaseModel):
    zone_id: UUID
    name: str
    seating: Seating
    price: int
    seats_left: int


class ZoneAvailability(BaseModel):
    type: Literal["ZoneAvailability"] = "ZoneAvailability"
    showtime_id: UUID
    zones: list[ZoneItem]


class SeatItem(BaseModel):
    seat_id: UUID
    row: str | None = None
    number: str | None = None
    price: int | None = None
    status: str | None = Field(default=None, exclude_if=lambda value: value is None)  # chỉ Admin thấy


class SeatMap(BaseModel):
    type: Literal["SeatMap"] = "SeatMap"
    showtime_id: UUID
    zone_id: UUID
    available_count: int
    seats: list[SeatItem]


class OrderLine(BaseModel):
    zone: str
    row: str | None = None
    number: str | None = None
    price: int
    quantity: int = 1


class OrderSummary(BaseModel):
    type: Literal["OrderSummary"] = "OrderSummary"
    order_id: UUID
    code: str
    status: str
    event_title: str
    starts_at: datetime
    items: list[OrderLine]
    total_amount: int
    expires_at: datetime | None = None


class PaymentStatus(BaseModel):
    type: Literal["PaymentStatus"] = "PaymentStatus"
    order_id: UUID
    status: str
    amount: int
    expires_at: datetime | None = None


class PaymentLink(BaseModel):
    """Chỉ dựng từ kết quả thực thi createPayment; URL không bao giờ vào model."""

    type: Literal["PaymentLink"] = "PaymentLink"
    order_id: UUID
    payment_url: str
    expires_at: datetime


class TicketItem(BaseModel):
    ticket_id: UUID
    code: str
    status: str
    event_title: str
    starts_at: datetime
    zone: str
    row: str | None = None
    number: str | None = None


class TicketList(BaseModel):
    type: Literal["TicketList"] = "TicketList"
    items: list[TicketItem]


class StatCards(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    type: Literal["StatCards"] = "StatCards"
    metric: str
    unit: str
    from_: datetime = Field(validation_alias=AliasChoices("from", "from_"), serialization_alias="from")
    to: datetime
    timezone: str
    computed_at: datetime
    value: int | float | None = None
    series: list[dict[str, object]] | None = None


class AdminTableKind(StrEnum):
    EVENTS = "EVENTS"
    VENUES = "VENUES"
    LAYOUTS = "LAYOUTS"
    TICKETS = "TICKETS"


class AdminTable(BaseModel):
    type: Literal["AdminTable"] = "AdminTable"
    kind: AdminTableKind
    columns: list[str]
    rows: list[list[object]]


class ConfirmForm(StrEnum):
    BUYER = "BUYER"


class ConfirmAction(BaseModel):
    type: Literal["ConfirmAction"] = "ConfirmAction"
    action_id: UUID
    version: int
    operation: str
    expires_at: datetime
    display: dict[str, object]
    form: ConfirmForm | None = None
    confirm_phrase: str | None = None


class ExecutionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ActionResult(BaseModel):
    type: Literal["ActionResult"] = "ActionResult"
    action_id: UUID
    operation: str
    status: ExecutionStatus
    code: str | None = None
    message: str


Block = (
    EventList
    | ShowtimeList
    | ZoneAvailability
    | SeatMap
    | OrderSummary
    | PaymentStatus
    | PaymentLink
    | TicketList
    | StatCards
    | AdminTable
    | ConfirmAction
    | ActionResult
)
