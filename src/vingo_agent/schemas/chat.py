"""Request/response của API nội bộ /internal/v1 (V2.3 mục 7.1). Backend module `chat` proxy 1-1 sang đây."""

from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

WireUUID = Annotated[UUID, BeforeValidator(lambda value: UUID(value) if isinstance(value, str) else value)]


class Role(StrEnum):
    BUYER = "BUYER"
    ADMIN = "ADMIN"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SelectionType(StrEnum):
    EVENT = "EVENT"
    SHOWTIME = "SHOWTIME"
    ZONE = "ZONE"
    SEATS = "SEATS"
    ORDER = "ORDER"
    TICKET = "TICKET"


class Selection(Strict):
    type: Annotated[
        SelectionType, BeforeValidator(lambda value: SelectionType(value) if isinstance(value, str) else value)
    ]
    ids: list[WireUUID] = Field(min_length=1, max_length=200)


class VisibleAction(Strict):
    """Thẻ ConfirmAction đang hiện trên web; điều kiện để confirm/reject bằng tin nhắn (V2.3 mục 4.5)."""

    action_id: WireUUID
    version: int = Field(ge=1)


class SendMessageRequest(Strict):
    command_id: WireUUID
    text: str
    selection: Selection | None = None
    visible_action: VisibleAction | None = None


class Buyer(Strict):
    """Form người mua trên thẻ thanh toán. Không lưu, không đưa vào model."""

    full_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    phone: str = Field(min_length=6, max_length=20)


class ConfirmRequest(Strict):
    command_id: WireUUID
    version: int = Field(ge=1)
    buyer: Buyer | None = None
    confirm_phrase: str | None = None


class RejectRequest(Strict):
    command_id: WireUUID
    version: int = Field(ge=1)


class CreateConversationResponse(BaseModel):
    conversation_id: WireUUID
    role: Role


class TurnState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    FAILED = "FAILED"


class TurnView(BaseModel):
    turn_id: WireUUID
    state: TurnState
    text: str | None = None
    blocks: list[dict[str, Any]] = []


class ConversationView(BaseModel):
    conversation_id: WireUUID
    role: Role
    messages: list[dict[str, Any]]
    pending_action: dict[str, Any] | None = None


class RejectResponse(BaseModel):
    state: str
