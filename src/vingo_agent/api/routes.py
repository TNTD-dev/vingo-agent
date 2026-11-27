"""Route đọc và stream nội bộ; confirm/reject dành cho KAN-193."""

import json
from hashlib import sha256
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from vingo_agent.actions.confirm import ConfirmedVia, confirm, reject
from vingo_agent.actions.lookup import resolve
from vingo_agent.agents.orchestrator import authorize, run_turn
from vingo_agent.api.auth import CurrentPrincipal
from vingo_agent.api.sse import EventType, StreamEvent
from vingo_agent.api.uploads import svg_upload
from vingo_agent.clients.backend import BackendClient
from vingo_agent.config import get_settings
from vingo_agent.persistence import repositories as repo
from vingo_agent.persistence.uploads import get_uploads, save_upload
from vingo_agent.schemas.blocks import ConfirmAction
from vingo_agent.schemas.chat import (
    ConfirmRequest,
    ConversationView,
    CreateConversationResponse,
    RejectRequest,
    RejectResponse,
    Role,
    SendMessageRequest,
    TurnView,
)
from vingo_agent.tools.base import ToolError

router = APIRouter(prefix="/internal/v1")


@router.post("/uploads")
async def upload_chat_file(request: Request, principal: CurrentPrincipal):
    if principal.role != Role.ADMIN or not get_settings().agent_admin_workflows_enabled:
        raise HTTPException(403, "FORBIDDEN")
    content = await svg_upload(request)
    upload_id = await save_upload(principal.user_id, content)
    if upload_id is None:
        raise HTTPException(429, "RATE_LIMITED")
    return {"upload_id": upload_id, "kind": "SVG", "size": len(content)}


@router.post("/conversations", status_code=201)
async def create_conversation(principal: CurrentPrincipal) -> CreateConversationResponse:
    """Tạo hội thoại; role lấy từ phiên, cố định suốt hội thoại."""
    row = await repo.create_conversation(principal.user_id, principal.role)
    return CreateConversationResponse(conversation_id=row["id"], role=row["role"])


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID, principal: CurrentPrincipal) -> ConversationView:
    """Lịch sử đã lọc và đề xuất PENDING; tra UNKNOWN được bổ sung trong KAN-193."""
    row = await authorize(conversation_id, principal)
    unresolved = await repo.get_unresolved_actions(conversation_id)
    if unresolved:
        backend = BackendClient(get_settings(), principal.session_token)
        try:
            for action_id in unresolved:
                await resolve(action_id, backend=backend)
        finally:
            await backend.aclose()
    messages = []
    for item in await repo.get_messages(conversation_id):
        content = dict(item["content"])
        if item["kind"] in {"ASSISTANT", "SYSTEM_EVENT"} and item["turn_id"]:
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
    if body.upload_ids:
        if principal.role != Role.ADMIN or not get_settings().agent_admin_workflows_enabled:
            raise HTTPException(403, "FORBIDDEN")
        await authorize(conversation_id, principal)
        upload_ids = set(body.upload_ids)
        if (
            len(upload_ids) != len(body.upload_ids)
            or set(await get_uploads(principal.user_id, upload_ids)) != upload_ids
        ):
            raise HTTPException(400, "VALIDATION_ERROR")
        await repo.add_refs(conversation_id, "UPLOAD", body.upload_ids)
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
    action = await repo.get_action(action_id)
    if action is None:
        raise HTTPException(409, "ACTION_NOT_PENDING")
    await authorize(action["conversation_id"], principal)
    # Không lưu form hay hash form; command định danh quyết định trên snapshot bất biến.
    command_hash = sha256(
        f"confirm:{action_id}:{body.version}:{body.buyer is not None}:{body.confirm_phrase}".encode()
    ).hexdigest()
    try:
        result_turn = await repo.create_turn(action["conversation_id"], body.command_id, command_hash)
    except ValueError:
        raise HTTPException(409, "IDEMPOTENCY_CONFLICT") from None
    turn_id = result_turn["id"]
    if not result_turn["_created"]:
        final = result_turn["final_text_and_blocks"] or {}
        if result_turn["state"] in {"COMPLETED", "AWAITING_CONFIRMATION"}:
            return _confirm_stream(turn_id, final.get("blocks", []), final.get("notices", []), result_turn["state"])
        if result_turn["state"] == "FAILED":
            code = final.get("code", "ACTION_NOT_PENDING")
            if code in SSE_REFUSALS:
                return _confirm_failed(turn_id, code)
            raise HTTPException(403 if code == "FORBIDDEN" else 409, code)
        # RUNNING do request khác hoặc process chết: CAS action là khóa thực thi.
        # Cho service confirm chờ/đọc execution, không chặn vĩnh viễn trên turn.
    notices = []
    try:
        _, blocks = await confirm(
            action_id=action_id,
            version=body.version,
            principal=principal,
            via=ConfirmedVia.BUTTON,
            buyer=body.buyer,
            confirm_phrase=body.confirm_phrase,
            notices=notices,
        )
    except ToolError as error:
        await repo.transition_turn(turn_id, "RUNNING", "FAILED", final_text_and_blocks={"code": error.code})
        if error.code in SSE_REFUSALS:
            return _confirm_failed(turn_id, error.code)
        raise HTTPException(403 if error.code == "FORBIDDEN" else 409, error.code) from None
    final = {"text": "", "blocks": [block.model_dump(mode="json") for block in blocks]}
    state = "AWAITING_CONFIRMATION" if any(isinstance(b, ConfirmAction) for b in blocks) else "COMPLETED"
    if notices:
        final["notices"] = notices
    await repo.transition_turn(
        turn_id,
        "RUNNING",
        state,
        final_text_and_blocks=final,
    )

    return _confirm_stream(turn_id, final["blocks"], notices, state)


# Mã từ chối không có trong ErrorCode của backend: trả HTTP 409 thì backend chuyển thành 503.
# Trả trong SSE `turn.failed` để web nhắc điền form / gõ lại tên.
SSE_REFUSALS = frozenset({"FORM_REQUIRED", "CONFIRM_PHRASE_REQUIRED"})


def _confirm_failed(turn_id, code):
    async def encoded():
        yield StreamEvent(
            turn_id=turn_id, seq=1, type=EventType.TURN_STARTED, payload={"turn_id": str(turn_id)}
        ).encode()
        yield StreamEvent(turn_id=turn_id, seq=2, type=EventType.TURN_FAILED, payload={"code": code}).encode()

    return StreamingResponse(
        encoded(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


def _confirm_stream(turn_id, blocks, notices=(), state="COMPLETED"):
    async def encoded():
        yield StreamEvent(
            turn_id=turn_id, seq=1, type=EventType.TURN_STARTED, payload={"turn_id": str(turn_id)}
        ).encode()
        for seq, block in enumerate(blocks, 2):
            # JSONB đổi thứ tự key; chuẩn hóa để replay SSE giống response ban đầu.
            data = json.loads(json.dumps(block, sort_keys=True))
            yield StreamEvent(
                turn_id=turn_id, seq=seq, type=EventType.BLOCK, payload={"type": data.pop("type"), "payload": data}
            ).encode()
        for seq, notice in enumerate(notices, len(blocks) + 2):
            yield StreamEvent(
                turn_id=turn_id,
                seq=seq,
                type=EventType.NOTICE,
                payload={"level": notice["level"], "text": notice["text"]},
            ).encode()
        yield StreamEvent(
            turn_id=turn_id,
            seq=len(blocks) + len(notices) + 2,
            type=EventType.TURN_COMPLETED,
            payload={"status": state},
        ).encode()

    return StreamingResponse(
        encoded(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.post("/actions/{action_id}/reject")
async def reject_action(action_id: UUID, body: RejectRequest, principal: CurrentPrincipal) -> RejectResponse:
    try:
        return RejectResponse(state=await reject(action_id=action_id, version=body.version, principal=principal))
    except ToolError as error:
        raise HTTPException(403 if error.code == "FORBIDDEN" else 409, error.code) from None


@router.get("/turns/{turn_id}")
async def get_turn(turn_id: UUID, principal: CurrentPrincipal) -> TurnView:
    """Dùng khi SSE đứt: trả text cuối, block và trạng thái turn."""
    turn = await repo.get_turn(turn_id)
    if not turn:
        raise HTTPException(404, "NOT_FOUND")
    await authorize(turn["conversation_id"], principal)
    final = turn["final_text_and_blocks"] or {}
    return TurnView(turn_id=turn_id, state=turn["state"], text=final.get("text"), blocks=final.get("blocks", []))
