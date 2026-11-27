"""Gọi ghi sau CAS; không giữ transaction qua HTTP, không gọi model."""

import logging
import time
from hashlib import sha256
from inspect import signature
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from vingo_agent.actions.registry import LookupMode, get_spec
from vingo_agent.observability.logging import log_event
from vingo_agent.observability.tracing import span
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


async def execute(operation, *, backend, payload, idempotency_key, buyer=None, lookup_only=False, web_blocks=None):
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
                data = _json_object(response)
                result = spec.project(data)
                if spec.web_blocks is not None:
                    blocks = spec.web_blocks(data)
                    if web_blocks is not None:
                        web_blocks.extend(blocks)
                return "SUCCEEDED", None, result
            except ToolError:
                if spec.web_blocks is None:
                    raise
                return "UNKNOWN", "EXECUTION_UNKNOWN", {}
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
    with span("lookup", operation=operation, outcome="UNKNOWN") as trace:
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
                    trace.set_attribute("outcome", result[0])
                    return result
        elif spec.lookup_mode == LookupMode.READ:
            try:
                result = await spec.lookup(backend=backend, payload=payload, idempotency_key=idempotency_key)
            except (httpx.RequestError, ToolError):
                result = None
            if result is not None:
                if isinstance(result, tuple):
                    result, blocks = result
                    if web_blocks is not None:
                        web_blocks.extend(blocks)
                trace.set_attribute("outcome", "SUCCEEDED")
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


async def save(action_id, row, outcome, *, expected, backend=None, web_blocks=()):
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

        blocks = result_blocks(action_id, row["operation"], outcome)
        final = {"text": "", "blocks": [b.model_dump(mode="json") for b in (*blocks, *web_blocks)]}
        run_hook = status == "SUCCEEDED" and spec.after_success is not None
        # Mỗi transition có bản ghi bất biến để event UNKNOWN cũ giữ đúng block lúc đó.
        result_turn = await conn.scalar(
            insert(turn)
            .values(
                conversation_id=row["conversation_id"],
                command_id=uuid4(),
                command_hash=sha256(str(action_id).encode()).hexdigest(),
                state="RUNNING" if run_hook else "COMPLETED",
                final_text_and_blocks=final,
                finished_at=None if run_hook else func.now(),
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
    if run_hook:
        # Thao tác ghi đã SUCCEEDED và đã commit: lỗi của bước phụ (vd tự tạo đề xuất thanh toán sau hold)
        # không được biến kết quả thành lỗi 500 hay để turn kẹt RUNNING. Ghi log và trả kết quả thật.
        try:
            # Hook cũ giữ nguyên chữ ký. Hook cần snapshot có thể nhận thêm context.
            extra = {"context": (row, web_blocks)} if "context" in signature(spec.after_success).parameters else {}
            additions = await spec.after_success(
                action_id=action_id, conversation_id=row["conversation_id"], result=result, backend=backend, **extra
            )
            if isinstance(additions, tuple):
                more_blocks, notices = additions
                final["blocks"].extend(b.model_dump(mode="json") for b in more_blocks)
                if notices:
                    final["notices"] = notices
        except Exception as exc:
            log_event(
                logging.getLogger(__name__),
                "action.after_success_failed",
                action_id=str(action_id),
                operation=row["operation"],
                outcome="FAILED",
                error_code=type(exc).__name__,
            )
        finally:
            async with transaction() as conn:
                await conn.execute(
                    update(turn)
                    .where(turn.c.id == result_turn)
                    .values(state="COMPLETED", final_text_and_blocks=final, finished_at=func.now())
                )
    return outcome


async def run(action_id, *, backend, buyer=None):
    row = await load(action_id)
    if row is None or row["state"] != "CONFIRMED":
        raise ToolError("ACTION_NOT_PENDING")
    if row["execution_state"] != "RUNNING":
        return row["execution_state"], row["error_code"], row["result"] or {}
    started = time.monotonic()
    web_blocks = []
    outcome = await execute(
        row["operation"],
        backend=backend,
        payload=row["payload"],
        idempotency_key=row["idempotency_key"],
        buyer=buyer,
        web_blocks=web_blocks,
    )
    outcome = await save(action_id, row, outcome, expected="RUNNING", backend=backend, web_blocks=web_blocks)
    log_event(
        logging.getLogger(__name__),
        "action.executed",
        action_id=str(action_id),
        operation=row["operation"],
        outcome=outcome[0],
        duration_ms=(time.monotonic() - started) * 1000,
    )
    return outcome


async def load_web_turn(action_id, conversation_id):
    # command_hash của turn SYSTEM_EVENT định danh action, kể cả khi confirm dùng command_id mới.
    async with transaction() as conn:
        return (
            (
                await conn.execute(
                    select(turn.c.state, turn.c.final_text_and_blocks)
                    .where(
                        turn.c.conversation_id == conversation_id,
                        turn.c.command_hash == sha256(str(action_id).encode()).hexdigest(),
                    )
                    .order_by(turn.c.id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )


async def execute_admin_resend(*, backend, payload: dict) -> tuple[str, str | None, dict]:
    """Handler sau CAS của KAN-193; gửi một lần, timeout/5xx không retry vì backend không idempotent."""
    if type(payload) is not dict or set(payload) != {"path", "body"} or payload["body"] is not None:
        return "FAILED", "INVALID_ARGUMENT", {}
    path = payload["path"]
    if type(path) is not dict or set(path) != {"ticket_id"}:
        return "FAILED", "INVALID_ARGUMENT", {}
    try:
        ticket_id = str(UUID(str(path["ticket_id"])))
    except (ValueError, TypeError, AttributeError):
        return "FAILED", "INVALID_ARGUMENT", {}
    try:
        response = await backend.write("adminResendTicket", path={"ticket_id": ticket_id}, body=None)
    except NotImplementedError:
        return "FAILED", "BACKEND_UNAVAILABLE", {}
    except httpx.TransportError:
        return "UNKNOWN", "EXECUTION_UNKNOWN", {}
    except ToolError as error:
        if error.code in {"BACKEND_UNAVAILABLE", "EXECUTION_UNKNOWN"}:
            return "UNKNOWN", "EXECUTION_UNKNOWN", {}
        allowed = {item.value for item in BackendErrorCode}
        return "FAILED", error.code if error.code in allowed else "VALIDATION_ERROR", {}
    if response.is_success:
        # 202 chỉ xác nhận enqueue, không chứng minh email đã được nhận.
        return "SUCCEEDED", None, {}
    if response.is_client_error:
        candidate = _json_object(response).get("code")
        allowed = {item.value for item in BackendErrorCode}
        code = candidate if isinstance(candidate, str) and candidate in allowed else "VALIDATION_ERROR"
        return "FAILED", code, {}
    return "UNKNOWN", "EXECUTION_UNKNOWN", {}
