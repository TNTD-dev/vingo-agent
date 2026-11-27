"""Kiểu input và dữ liệu catalog được phép dùng trong đề xuất Admin."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from vingo_agent.tools.base import ToolError

TicketPrice = Annotated[int, Field(strict=True, ge=0, le=4_611_686_018_427_387)]


class PriceTierInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    name: Annotated[str, Field(min_length=1, max_length=200)]
    price: TicketPrice
    color: Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")] | None = None


class ZonePriceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone_id: UUID
    price_tier_id: UUID
    display_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class _ZonePrice(ZonePriceInput):
    model_config = ConfigDict(extra="ignore")

    price_tier_id: UUID = Field(validation_alias=AliasChoices("price_tier_id", "ticket_type_id"))


class _Tier(PriceTierInput):
    model_config = ConfigDict(extra="ignore")

    id: UUID = Field(validation_alias=AliasChoices("id", "ticket_type_id"))


def uuid_text(value) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        raise ToolError("INVALID_TOOL_RESULT") from None


def timestamp(value: datetime | str, error="INVALID_TOOL_RESULT") -> str:
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.utcoffset() is not None:
            return value.astimezone(UTC).isoformat()
    except (AttributeError, ValueError, OverflowError):
        pass
    raise ToolError(error)


def rows(data: dict, field: str) -> list[dict]:
    items = data.get(field)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ToolError("INVALID_TOOL_RESULT")
    return items


def as_record(data, expected_id: str) -> dict:
    if not isinstance(data, dict) or uuid_text(data.get("id")) != expected_id:
        raise ToolError("INVALID_TOOL_RESULT")
    return data


def _collection(data: dict, field: str, model, key: str) -> list[dict]:
    try:
        values = [model.model_validate(item).model_dump(mode="json") for item in rows(data, field)]
    except ValidationError:
        raise ToolError("INVALID_TOOL_RESULT") from None
    if len({item[key] for item in values}) != len(values):
        raise ToolError("INVALID_TOOL_RESULT")
    return sorted(values, key=lambda item: item[key])


def price_tiers(event: dict) -> list[dict]:
    return _collection(event, "price_tiers", _Tier, "id")


def zone_prices(event: dict) -> list[dict]:
    return _collection(event, "zone_prices", _ZonePrice, "zone_id")


def zone_price_body(assignments: list[ZonePriceInput]) -> list[dict]:
    if len({item.zone_id for item in assignments}) != len(assignments):
        raise ToolError("INVALID_ARGUMENT")
    return sorted(
        (item.model_dump(mode="json", exclude_none=True) for item in assignments), key=lambda item: item["zone_id"]
    )
