"""Gọi ghi sau CAS; không giữ transaction qua HTTP, không gọi model."""

import logging
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from vingo_agent.actions.registry import LookupMode, get_spec
from vingo_agent.observability.logging import log_event
from vingo_agent.persistence.db import transaction
from vingo_agent.persistence.models import conversation, execution, message, pending_action, resource_ref, turn
from vingo_agent.schemas.errors import BackendErrorCode
from vingo_agent.tools.base import ToolError


def _json_object(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def execute(operation, *, backend, payload, idempotency_key, buyer=None, lookup_only=False):
    spec = get_spec(operation)
    from vingo_agent.config import get_settings

    if spec.tool_name in get_settings().agent_disabled_tools:
        # Tra lại: lần gửi trước có thể đã ghi → giữ UNKNOWN, không gửi thêm.
        # Lần gửi đầu: chưa gửi gì → FAILED, để lần tra sau không gửi thật một thao tác đã bị tắt.
        return ("UNKNOWN", "EXECUTION_UNKNOWN", {}) if lookup_only else ("FAILED", "FORBIDDEN", {})

    async def send(*, lookup_request=False):
        try:
            request = spec.build_lookup_request(payload) if lookup_request else spec.build_request(payload, buyer)
            return await backend.write(spec.backend_operation, **request, idempotency_key=idempotency_key)
        except httpx.RequestError:
            return None

    def outcome(response):
        if response is not None and response.is_success:
            try:
                return "SUCCEEDED", None, spec.project(_json_object(response))
            except (ValueError, TypeError, KeyError):
                return "UNKNOWN", "EXECUTION_UNKNOWN", {}
        if response is not None and response.is_client_error:
            code = _json_object(response).get("code")
            code = (
                code
                if isinstance(code, str) and code in {item.value for item in BackendErrorCode}
                else "VALIDATION_ERROR"
            )
            return "FAILED", code, {}
        return None

    if not lookup_only:
        if spec.check_before is not None:
            try:
                valid = await spec.check_before(backend=backend, payload=payload)
            except (httpx.RequestError, ToolError):
                # Chưa gửi ghi: không để recovery hiểu nhầm là đã gửi và retry bỏ qua snapshot.
                return "FAILED", "BACKEND_UNAVAILABLE", {}
            if not valid:
                return "FAILED", "STALE_PROPOSAL", {}
        result = outcome(await send())
        if result is not None:
            return result
    if spec.lookup_mode == LookupMode.RETRY:
        if lookup_only and spec.check_before is not None:
            try:
                if not await spec.check_before(backend=backend, payload=payload):
                    return "UNKNOWN", "EXECUTION_UNKNOWN", {}
            except (httpx.RequestError, ToolError):
                return "UNKNOWN", "EXECUTION_UNKNOWN", {}
        # Không có buyer khi mở lại hội thoại: tuyệt đối không tái dựng form từ DB.
        if spec.build_lookup_request is not None or not spec.needs_form or buyer is not None:
            result = outcome(await send(lookup_request=spec.build_lookup_request is not None))
            if result is not None:
                return result
    elif spec.lookup_mode == LookupMode.READ:
        try:
            result = await spec.lookup(backend=backend, payload=payload, idempotency_key=idempotency_key)
        except (httpx.RequestError, ToolError):
            result = None
        if result is not None:
            return "SUCCEEDED", None, result
    return "UNKNOWN", "EXECUTION_UNKNOWN", {}


async def execute_admin_catalog(operation, *, backend, payload, idempotency_key):
    """Giữ entry point KAN-201 cho caller/test cũ; thực thi qua spec."""
    return await execute(operation, backend=backend, payload=payload, idempotency_key=idempotency_key)


async def load(action_id):
    async with transaction() as conn:
        return (
            (
                await conn.execute(
                    select(
                        pending_action,
                        execution.c.idempotency_key,
                        execution.c.state.label("execution_state"),
                        execution.c.error_code,
                        execution.c.result,
                        execution.c.started_at,
                    )
                    .join(execution, pending_action.c.id == execution.c.action_id)
                    .where(pending_action.c.id == action_id)
                )
            )
            .mappings()
            .one_or_none()
        )


async def save(action_id, row, outcome, *, expected, backend=None):
    status, code, result = outcome
    spec = get_spec(row["operation"])
    async with transaction() as conn:
        # Cùng thứ tự khóa cha → con với tạo/confirm để tránh deadlock.
        await conn.execute(
            select(conversation.c.id).where(conversation.c.id == row["conversation_id"]).with_for_update()
        )
        saved = await conn.scalar(
            update(execution)
            .where(execution.c.action_id == action_id, execution.c.state == expected)
            .values(state=status, error_code=code, result=result, finished_at=func.now())
            .returning(execution.c.action_id)
        )
        if saved is None:
            current = (await conn.execute(select(execution).where(execution.c.action_id == action_id))).mappings().one()
            return current["state"], current["error_code"], current["result"] or {}
        ids = {}
        if status == "SUCCEEDED":
            for field, ref_type in spec.result_refs.items():
                if field in result:
                    value = UUID(str(result[field]))
                    ids[field] = str(value)
                    await conn.execute(
                        pg_insert(resource_ref)
                        .values(conversation_id=row["conversation_id"], resource_type=ref_type, resource_id=value)
                        .on_conflict_do_nothing()
                    )
        seq = await conn.scalar(
            select(func.coalesce(func.max(message.c.seq), 0)).where(message.c.conversation_id == row["conversation_id"])
        )
        content = {"operation": row["operation"], "status": status, "ids": ids}
        if code:
            content["code"] = code
        from vingo_agent.actions.confirm import result_blocks

        blocks = [block.model_dump(mode="json") for block in result_blocks(action_id, row["operation"], outcome)]
        # Mỗi transition có bản ghi bất biến để event UNKNOWN cũ giữ đúng block lúc đó.
        result_turn = await conn.scalar(
            insert(turn)
            .values(
                conversation_id=row["conversation_id"],
                command_id=uuid4(),
                command_hash=sha256(str(action_id).encode()).hexdigest(),
                state="COMPLETED",
                final_text_and_blocks={"text": "", "blocks": blocks},
                finished_at=func.now(),
            )
            .returning(turn.c.id)
        )
        await conn.execute(
            insert(message).values(
                conversation_id=row["conversation_id"],
                seq=seq + 1,
                turn_id=result_turn,
                kind="SYSTEM_EVENT",
                content=content,
            )
        )
    if status == "SUCCEEDED" and spec.after_success is not None:
        # Thao tác ghi đã SUCCEEDED và đã commit: lỗi của bước phụ (vd tự tạo đề xuất thanh toán sau hold)
        # không được biến kết quả thành lỗi 500 hay để turn kẹt RUNNING. Ghi log và trả kết quả thật.
        try:
            await spec.after_success(
                action_id=action_id, conversation_id=row["conversation_id"], result=result, backend=backend
            )
        except Exception as exc:
            log_event(
                logging.getLogger(__name__),
                "action.after_success_failed",
                action_id=str(action_id),
                operation=row["operation"],
                outcome="FAILED",
                error_code=type(exc).__name__,
            )
    return outcome


async def run(action_id, *, backend, buyer=None):
    row = await load(action_id)
    if row is None or row["state"] != "CONFIRMED":
        raise ToolError("ACTION_NOT_PENDING")
    if row["execution_state"] != "RUNNING":
        return row["execution_state"], row["error_code"], row["result"] or {}
    outcome = await execute(
        row["operation"], backend=backend, payload=row["payload"], idempotency_key=row["idempotency_key"], buyer=buyer
    )
    return await save(action_id, row, outcome, expected="RUNNING", backend=backend)
