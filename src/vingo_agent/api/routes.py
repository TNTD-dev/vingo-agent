"""Route /internal/v1 (V2.3 mục 7.1). Khung: mọi route đã kiểm auth + validate body, logic trả 501."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from vingo_agent.api.auth import CurrentPrincipal
from vingo_agent.schemas.chat import (
    ConfirmRequest,
    ConversationView,
    CreateConversationResponse,
    RejectRequest,
    RejectResponse,
    SendMessageRequest,
    TurnView,
)

router = APIRouter(prefix="/internal/v1")


def _todo() -> HTTPException:
    return HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "NOT_IMPLEMENTED")


@router.post("/conversations", status_code=201)
async def create_conversation(principal: CurrentPrincipal) -> CreateConversationResponse:
    """Tạo hội thoại; role lấy từ phiên, cố định suốt hội thoại."""
    raise _todo()


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID, principal: CurrentPrincipal) -> ConversationView:
    """Lịch sử đã lọc + đề xuất PENDING. Đồng thời tra lại EXECUTION UNKNOWN (V2.3 mục 7)."""
    raise _todo()


@router.post("/conversations/{conversation_id}/messages")
async def send_message(conversation_id: UUID, body: SendMessageRequest, principal: CurrentPrincipal):
    """Chạy một turn, trả SSE (text/event-stream). Turn đang chạy → 409 TURN_IN_PROGRESS."""
    raise _todo()


@router.post("/actions/{action_id}/confirm")
async def confirm_action(action_id: UUID, body: ConfirmRequest, principal: CurrentPrincipal):
    """Xác nhận bằng nút: thực thi đồng bộ, trả SSE. Không gọi model."""
    raise _todo()


@router.post("/actions/{action_id}/reject")
async def reject_action(action_id: UUID, body: RejectRequest, principal: CurrentPrincipal) -> RejectResponse:
    raise _todo()


@router.get("/turns/{turn_id}")
async def get_turn(turn_id: UUID, principal: CurrentPrincipal) -> TurnView:
    """Dùng khi SSE đứt: trả text cuối, block và trạng thái turn."""
    raise _todo()
