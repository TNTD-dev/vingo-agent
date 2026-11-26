"""Route đọc và stream nội bộ; confirm/reject dành cho KAN-193."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from vingo_agent.agents.orchestrator import authorize, run_turn
from vingo_agent.api.auth import CurrentPrincipal
from vingo_agent.persistence import repositories as repo
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
    row = await repo.create_conversation(principal.user_id, principal.role)
    return CreateConversationResponse(conversation_id=row["id"], role=row["role"])


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID, principal: CurrentPrincipal) -> ConversationView:
    """Lịch sử đã lọc và đề xuất PENDING; tra UNKNOWN được bổ sung trong KAN-193."""
    row = await authorize(conversation_id, principal)
    messages = []
    for item in await repo.get_messages(conversation_id):
        content = dict(item["content"])
        if item["kind"] == "ASSISTANT" and item["turn_id"]:
            turn = await repo.get_turn(item["turn_id"])
            final = turn["final_text_and_blocks"] or {}
            content["blocks"] = final.get("blocks", [])
            content["notices"] = final.get("notices", [])
        messages.append({"kind": item["kind"], "content": content, "seq": item["seq"]})
    return ConversationView(
        conversation_id=conversation_id,
        role=row["role"],
        messages=messages,
        pending_action=await repo.get_pending_action(conversation_id),
    )


@router.post("/conversations/{conversation_id}/messages")
async def send_message(conversation_id: UUID, body: SendMessageRequest, principal: CurrentPrincipal):
    """Chạy một turn, trả SSE (text/event-stream). Turn đang chạy → 409 TURN_IN_PROGRESS."""
    stream = await run_turn(conversation_id=conversation_id, request=body, principal=principal)

    async def encoded():
        try:
            async for item in stream:
                yield item.encode()
        finally:
            await stream.aclose()

    return StreamingResponse(
        encoded(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


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
    turn = await repo.get_turn(turn_id)
    if not turn:
        raise HTTPException(404, "NOT_FOUND")
    await authorize(turn["conversation_id"], principal)
    final = turn["final_text_and_blocks"] or {}
    return TurnView(turn_id=turn_id, state=turn["state"], text=final.get("text"), blocks=final.get("blocks", []))
