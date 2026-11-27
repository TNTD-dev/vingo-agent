"""Phần chung cho mọi tool (V2.3 mục 4.1): deps của turn, envelope, lỗi, kiểm Ref."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from vingo_agent.api.auth import Principal
from vingo_agent.clients.backend import BackendClient
from vingo_agent.persistence import repositories
from vingo_agent.schemas.chat import VisibleAction

ResourceType = Literal[
    "EVENT",
    "SHOWTIME",
    "ZONE",
    "SEAT",
    "ORDER",
    "TICKET",
    "VENUE",
    "LAYOUT",
    "ACTION",
    "PRICE_TIER",
    "OVERVIEW",
    "UPLOAD",
]


class ToolError(Exception):
    """Lỗi trả về cho model dưới dạng envelope `ok=false`, không phải exception lộ ra ngoài."""

    def __init__(self, code: str, retryable: bool = False, *, http_status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status


@dataclass
class TurnDeps:
    """Deps PydanticAI truyền vào mọi tool trong một turn."""

    principal: Principal
    conversation_id: UUID
    turn_id: UUID
    backend: BackendClient
    visible_action: VisibleAction | None = None
    tool_calls: int = 0
    proposals_created: int = 0
    blocks: list[BaseModel] = field(default_factory=list)
    notices: list[dict[str, str]] = field(default_factory=list)
    admin_ticket_codes: dict[UUID, str] = field(default_factory=dict)
    zone_price_preview: tuple[str, list[dict], str | None, dict] | None = None


class ToolArgs(BaseModel):
    """Base cho input tool: cấm field lạ, kiểu chặt (bool không thay int)."""

    model_config = ConfigDict(extra="forbid", strict=True)


def ok(operation: str, data: dict[str, Any], *, truncated: bool = False, observed_at: datetime) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "meta": {"operation": operation, "observed_at": observed_at.isoformat(), "truncated": truncated},
    }


def fail(operation: str, error: ToolError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": error.code, "retryable": error.retryable},
        "meta": {"operation": operation},
    }


async def require_ref(deps: TurnDeps, resource_type: ResourceType, resource_id: UUID) -> None:
    """G5: ID phải đã xuất hiện trong hội thoại (resource_ref), đúng loại. Sai → INVALID_REFERENCE."""
    if not await repositories.has_ref(deps.conversation_id, resource_type, resource_id):
        raise ToolError("INVALID_REFERENCE")


async def record_refs(deps: TurnDeps, resource_type: ResourceType, ids: list[UUID]) -> None:
    """Ghi Ref từ kết quả tool đã lọc, UI selection hoặc kết quả thực thi."""
    await repositories.add_refs(deps.conversation_id, resource_type, ids)
