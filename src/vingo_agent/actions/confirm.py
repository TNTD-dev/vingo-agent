"""G8 dùng chung BUTTON/TEXT; CAS và execution trong một transaction ngắn."""

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import insert, select, update

from vingo_agent.actions import execution as runner
from vingo_agent.actions.registry import get_spec
from vingo_agent.config import get_settings
from vingo_agent.persistence.db import transaction
from vingo_agent.persistence.models import conversation, execution, pending_action
from vingo_agent.schemas.blocks import ActionResult
from vingo_agent.tools.base import ToolError


class ConfirmedVia(StrEnum):
    BUTTON = "BUTTON"
    TEXT = "TEXT"


async def _lock(conn, action_id, principal):
    cid = await conn.scalar(select(pending_action.c.conversation_id).where(pending_action.c.id == action_id))
    if cid is None:
        raise ToolError("ACTION_NOT_PENDING")
    owner = (
        (await conn.execute(select(conversation).where(conversation.c.id == cid).with_for_update())).mappings().one()
    )
    row = (
        (await conn.execute(select(pending_action).where(pending_action.c.id == action_id).with_for_update()))
        .mappings()
        .one()
    )
    if (
        owner["owner_id"] != principal.user_id
        or row["actor_id"] != principal.user_id
        or owner["role"] != principal.role
        or row["role"] != principal.role
    ):
        raise ToolError("FORBIDDEN")
    return row


def result_blocks(action_id, operation, outcome):
    status, code, result = outcome
    # RUNNING do request khác đang xử lý không phải bằng chứng thất bại/thành công.
    status = "UNKNOWN" if status == "RUNNING" else status
    code = "EXECUTION_UNKNOWN" if status == "UNKNOWN" else code
    blocks = [
        ActionResult(
            action_id=action_id,
            operation=operation,
            status=status,
            code=code,
            message={
                "SUCCEEDED": "Thao tác thành công.",
                "FAILED": "Thao tác không thành công.",
                "UNKNOWN": "Chưa rõ kết quả; vui lòng kiểm tra lại hội thoại.",
            }[status],
        )
    ]
    spec = get_spec(operation)
    if status == "SUCCEEDED" and spec.result_blocks:
        blocks.extend(spec.result_blocks(result))
    return blocks


async def confirm(
    *, action_id, version: int, principal, via: ConfirmedVia, buyer=None, confirm_phrase=None, backend=None
):
    expired = False
    claimed = False
    async with transaction() as conn:
        row = await _lock(conn, action_id, principal)
        operation = row["operation"]
        if row["version"] != version:
            raise ToolError("ACTION_NOT_PENDING")
        spec = get_spec(operation)
        if spec.role != principal.role:
            raise ToolError("FORBIDDEN")
        if spec.tool_name in get_settings().agent_disabled_tools:
            raise ToolError("FORBIDDEN")
        existing = (
            (await conn.execute(select(execution).where(execution.c.action_id == action_id))).mappings().one_or_none()
        )
        if existing is not None and row["state"] == "CONFIRMED":
            outcome = existing["state"], existing["error_code"], existing["result"] or {}
        else:
            if row["state"] != "PENDING":
                raise ToolError("ACTION_NOT_PENDING")
            if row["expires_at"] <= datetime.now(UTC):
                await conn.execute(
                    update(pending_action)
                    .where(
                        pending_action.c.id == action_id,
                        pending_action.c.state == "PENDING",
                        pending_action.c.version == version,
                    )
                    .values(state="EXPIRED", decided_at=datetime.now(UTC))
                )
                expired = True
            else:
                phrase = (
                    spec.confirm_phrase(row["payload"]) if spec.confirm_phrase else row["display"].get("confirm_phrase")
                )
                if phrase is not None and (via == ConfirmedVia.TEXT or confirm_phrase != phrase):
                    raise ToolError("CONFIRM_PHRASE_REQUIRED")
                if (spec.needs_form or row["display"].get("form")) and (via == ConfirmedVia.TEXT or buyer is None):
                    raise ToolError("FORM_REQUIRED")
                changed = await conn.scalar(
                    update(pending_action)
                    .where(
                        pending_action.c.id == action_id,
                        pending_action.c.state == "PENDING",
                        pending_action.c.version == version,
                    )
                    .values(state="CONFIRMED", confirmed_via=via, decided_at=datetime.now(UTC))
                    .returning(pending_action.c.id)
                )
                if changed is None:
                    raise ToolError("ACTION_NOT_PENDING")
                await conn.execute(
                    insert(execution).values(action_id=action_id, state="RUNNING", idempotency_key=uuid4())
                )
                claimed = True
    # Không raise trong transaction khi EXPIRED: phải commit trạng thái trước.
    if expired:
        raise ToolError("ACTION_NOT_PENDING")
    if claimed:
        from vingo_agent.clients.backend import BackendClient

        owned = backend is None
        backend = backend or BackendClient(get_settings(), principal.session_token)
        try:
            outcome = await runner.run(action_id, backend=backend, buyer=buyer)
        finally:
            if owned:
                await backend.aclose()
    elif outcome[0] == "RUNNING":
        # Request thua CAS chỉ chờ đọc DB, tuyệt đối không giành quyền gửi lại.
        try:
            async with asyncio.timeout(25):
                while outcome[0] == "RUNNING":
                    await asyncio.sleep(0.025)
                    current = await runner.load(action_id)
                    outcome = current["execution_state"], current["error_code"], current["result"] or {}
        except TimeoutError:
            outcome = "UNKNOWN", "EXECUTION_UNKNOWN", {}
    return outcome, result_blocks(action_id, operation, outcome)


async def reject(*, action_id, version: int, principal) -> str:
    expired = False
    async with transaction() as conn:
        row = await _lock(conn, action_id, principal)
        if row["version"] != version or row["state"] != "PENDING":
            raise ToolError("ACTION_NOT_PENDING")
        expired = row["expires_at"] <= datetime.now(UTC)
        await conn.execute(
            update(pending_action)
            .where(
                pending_action.c.id == action_id,
                pending_action.c.state == "PENDING",
                pending_action.c.version == version,
            )
            .values(state="EXPIRED" if expired else "REJECTED", decided_at=datetime.now(UTC))
        )
    if expired:
        raise ToolError("ACTION_NOT_PENDING")
    return "REJECTED"
