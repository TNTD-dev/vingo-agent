"""Vòng turn chỉ đọc: lease trước SSE, transaction ngắn, dọn dẹp kể cả khi ngắt."""

import asyncio
import hashlib
import json
import logging
import time
from contextlib import aclosing
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.usage import RunUsage

from vingo_agent.agents.factory import build_agent, build_model, load_prompt
from vingo_agent.api.sse import StreamEvent
from vingo_agent.clients.backend import BackendClient
from vingo_agent.config import get_settings
from vingo_agent.context.builder import build_context
from vingo_agent.context.compaction import compact_if_needed
from vingo_agent.context.tokens import TokenEstimator, text_chars
from vingo_agent.guardrails.input import InputRejected, check_input, redact
from vingo_agent.guardrails.limits import usage_limits
from vingo_agent.guardrails.output import LINK_WARNING, Evidence, check_reply
from vingo_agent.observability.logging import denied, log_event
from vingo_agent.persistence import repositories as repo
from vingo_agent.tools.base import ToolError, TurnDeps
from vingo_agent.tools.blocks import build_block

logger = logging.getLogger(__name__)


async def authorize(conversation_id, principal):
    conversation = await repo.get_conversation(conversation_id)
    if not conversation or conversation["owner_id"] != principal.user_id:
        denied("NOT_FOUND")
        raise HTTPException(404, "NOT_FOUND")
    if conversation["role"] != principal.role:
        denied("FORBIDDEN")
        raise HTTPException(403, "FORBIDDEN")
    return conversation


def filtered(value):
    # Kết quả tool đã qua projection; thêm lớp che bí mật trước khi lưu lịch sử.
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            k: filtered(v)
            for k, v in value.items()
            if k.lower() not in {"qr_token", "payment_url", "email", "phone", "token"}
        }
    if isinstance(value, list):
        return [filtered(v) for v in value]
    return value


def amounts_in(value):
    amounts = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"price", "price_from", "amount", "total", "total_amount", "unit_price"} and type(item) is int:
                amounts.add(item)
            amounts.update(amounts_in(item))
    elif isinstance(value, list):
        for item in value:
            amounts.update(amounts_in(item))
    return amounts


async def run_turn(*, conversation_id, request, principal):
    """Chuẩn bị trước stream rồi trả async iterator; lỗi HTTP vẫn trả JSON được."""
    settings = get_settings()
    deadline = time.monotonic() + settings.turn_deadline_s
    await authorize(conversation_id, principal)
    # Hash không lưu bản thô, nhưng vẫn phân biệt hai command có bí mật khác nhau.
    command_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    try:
        turn = await repo.create_turn(conversation_id, request.command_id, command_hash)
    except ValueError as exc:
        # Backend chỉ map mã có trong ErrorCode của nó; trùng command_id khác nội dung = IDEMPOTENCY_CONFLICT.
        denied("IDEMPOTENCY_CONFLICT")
        raise HTTPException(409, "IDEMPOTENCY_CONFLICT") from exc
    turn_id = turn["id"]
    if turn["state"] != "RUNNING":

        async def replay():
            final = turn["final_text_and_blocks"] or {}
            seq = 0
            for event_type, payload in [
                ("turn.started", {"turn_id": str(turn_id)}),
                ("text.delta", {"text": final.get("text", "")}),
                *[("block", block) for block in final.get("blocks", [])],
                *[("notice", notice) for notice in final.get("notices", [])],
                ("turn.failed", {"code": final.get("code", "BACKEND_UNAVAILABLE")})
                if turn["state"] == "FAILED"
                else ("turn.completed", {"status": turn["state"]}),
            ]:
                seq += 1
                yield StreamEvent(turn_id=turn_id, seq=seq, type=event_type, payload=payload)

        return replay()
    if not turn.get("_created", True):
        # Turn cùng command_id đang chạy thật (còn giữ lease) → 409. Mất lease (stream chưa từng chạy,
        # process chết) → đóng FAILED để lần gửi lại nhận kết quả thay vì 409 mãi.
        conversation = await repo.get_conversation(conversation_id)
        alive = (
            conversation["active_turn_id"] == turn_id
            and conversation["active_until"] is not None
            and conversation["active_until"] > datetime.now(UTC)
        )
        if alive:
            denied("TURN_IN_PROGRESS")
            raise HTTPException(409, "TURN_IN_PROGRESS")
        await repo.transition_turn(turn_id, "RUNNING", "FAILED", final_text_and_blocks={"code": "BACKEND_UNAVAILABLE"})
        await repo.release_turn(conversation_id, turn_id)
        return await run_turn(conversation_id=conversation_id, request=request, principal=principal)
    if not await repo.acquire_turn(conversation_id, turn_id, settings.lease_s):
        await repo.discard_unstarted_turn(turn_id)
        denied("TURN_IN_PROGRESS")
        raise HTTPException(409, "TURN_IN_PROGRESS")
    try:
        text = redact(check_input(request.text, max_chars=settings.max_input_chars))
    except InputRejected as exc:
        try:
            await repo.transition_turn(turn_id, "RUNNING", "FAILED", final_text_and_blocks={"code": exc.code})
        finally:
            await repo.release_turn(conversation_id, turn_id)
        raise HTTPException(exc.status_code, exc.code) from exc
    # Xóa tham chiếu tới input thô trước khi tạo iterator sống qua toàn bộ stream.
    selection, visible_action = request.selection, request.visible_action
    del request

    async def stream():
        seq, final_text, state, code = 0, "", "FAILED", "BACKEND_UNAVAILABLE"
        blocks, messages, amounts = {}, [{"kind": "USER", "content": {"text": text}}], set()
        notices = []
        model_version = None
        last_input_tokens = last_input_chars = None

        def event(kind, payload):
            nonlocal seq
            seq += 1
            return StreamEvent(turn_id=turn_id, seq=seq, type=kind, payload=payload)

        async def persist():
            await repo.append_messages(conversation_id, turn_id, messages)
            if model_version:
                await repo.set_model_version(turn_id, model_version)
            if last_input_tokens is not None:
                await repo.set_input_usage(turn_id, last_input_tokens, last_input_chars)
            await repo.transition_turn(
                turn_id,
                "RUNNING",
                state,
                final_text_and_blocks={
                    "text": final_text,
                    "blocks": list(blocks.values()),
                    "notices": notices,
                    **({"code": code} if state == "FAILED" else {}),
                },
            )

        try:
            yield event("turn.started", {"turn_id": str(turn_id)})
            timeout = asyncio.timeout_at(deadline)
            async with timeout:
                model = build_model(settings.agent_model, settings.agent_fallback_model)
                agent = build_agent(principal.role, model=model, disabled_tools=settings.agent_disabled_tools)
                usage = RunUsage()
                limits = usage_limits(settings)
                pending = await repo.get_pending_action(conversation_id)
                prompt = load_prompt(principal.role)
                now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat()

                async def context():
                    return await build_context(conversation_id, now_vn=now, pending_action=pending, new_message=text)

                tool_schema_size = text_chars(
                    json.dumps(
                        [
                            {
                                "name": name,
                                "description": tool.description,
                                "parameters": tool.function_schema.json_schema,
                            }
                            for name, tool in agent._function_toolset.tools.items()
                        ],
                        ensure_ascii=False,
                    )
                )

                estimator = TokenEstimator(settings.default_chars_per_token)
                previous_usage = await repo.get_last_input_usage(conversation_id)
                if previous_usage:
                    estimator.observe(
                        previous_usage["last_input_tokens"], previous_usage["last_input_chars"], tool_schema_size
                    )
                    estimator.reset_anchor()

                async def estimate():
                    return estimator.estimate(text_chars(await context()) + len(prompt), tool_schema_size)

                async def estimate_after_compact():
                    estimator.reset_anchor()
                    return await estimate()

                async def summarize(instructions, summary, rows):
                    summarizer = Agent(model, instructions=instructions)
                    result = await summarizer.run(
                        json.dumps(
                            {"summary": summary, "messages": [dict(row) for row in rows]},
                            default=str,
                            ensure_ascii=False,
                        ),
                        usage=usage,
                        usage_limits=limits,
                    )
                    return redact(result.output)

                await compact_if_needed(
                    conversation_id, await estimate(), summarize=summarize, estimate=estimate_after_compact
                )
                history = await context()
                _, context_rows = await repo.load_context(conversation_id)
                for row in context_rows:
                    if row["kind"] == "TOOL_RESULT":
                        if row["content"].get("result", {}).get("ok") is True:
                            amounts.update(amounts_in(row["content"]))
                if selection:
                    kind = "SEAT" if selection.type.value == "SEATS" else selection.type.value
                    valid = [str(value) for value in selection.ids if await repo.has_ref(conversation_id, kind, value)]
                    if len(valid) != len(selection.ids):
                        yield event("notice", {"level": "WARN", "text": "Lựa chọn không hợp lệ đã được bỏ qua."})
                    if valid:
                        from pydantic_ai.messages import UserPromptPart

                        history.append(
                            ModelRequest(parts=[UserPromptPart(json.dumps({"selection": valid, "type": kind}))])
                        )
                async with aclosing(BackendClient(settings, principal.session_token)) as backend:
                    deps = TurnDeps(principal, conversation_id, turn_id, backend, visible_action=visible_action)
                    history_count = len(history)
                    async with agent.iter(message_history=history, deps=deps, usage=usage, usage_limits=limits) as run:
                        async for node in run:
                            if Agent.is_model_request_node(node):
                                request_messages = [
                                    *run.ctx.state.message_history,
                                    replace(node.request, instructions=prompt),
                                ]
                                if estimator.estimate(text_chars(request_messages), tool_schema_size) >= (
                                    settings.compact_ratio
                                    * (settings.context_window_tokens - settings.output_reserved_tokens)
                                ):
                                    current = run.ctx.state.message_history[history_count:]

                                    async def estimate_current(current=current, node=node):
                                        # Callback chỉ chạy sau khi summary đã thay history trong DB.
                                        estimator.reset_anchor()
                                        return estimator.estimate(
                                            text_chars(
                                                [*await context(), *current, replace(node.request, instructions=prompt)]
                                            ),
                                            tool_schema_size,
                                        )

                                    await compact_if_needed(
                                        conversation_id,
                                        estimator.estimate(text_chars(request_messages), tool_schema_size),
                                        summarize=summarize,
                                        estimate=estimate_current,
                                    )
                                    compacted_history = await context()
                                    history_count = len(compacted_history)
                                    run.ctx.state.message_history[:] = [*compacted_history, *current]
                                async with node.stream(run.ctx) as response:
                                    # node.stream đã thêm request và resolve instructions; history chưa có response.
                                    request_chars = text_chars(run.ctx.state.message_history)
                                    async for item in response:
                                        delta = None
                                        if isinstance(item, PartStartEvent) and isinstance(item.part, TextPart):
                                            delta = item.part.content
                                        elif isinstance(item, PartDeltaEvent) and isinstance(item.delta, TextPartDelta):
                                            delta = item.delta.content_delta
                                        if delta:
                                            model_version = response.response.model_name or model_version
                                            final_text += delta
                                            yield event("text.delta", {"text": delta})
                                input_tokens = response.response.usage.input_tokens
                                if estimator.observe(input_tokens, request_chars, tool_schema_size):
                                    last_input_tokens, last_input_chars = input_tokens, request_chars
                            elif Agent.is_call_tools_node(node):
                                async with node.stream(run.ctx) as tool_stream:
                                    async for item in tool_stream:
                                        if isinstance(item, FunctionToolCallEvent):
                                            name = item.part.tool_name
                                            step = (
                                                "PROPOSING"
                                                if name.startswith("propose_")
                                                else "EXECUTING"
                                                if name in {"confirm_pending_action", "reject_pending_action"}
                                                else "SEARCHING"
                                                if name.startswith("search")
                                                else "READING"
                                            )
                                            yield event("progress", {"step": step})
                                            messages.append(
                                                {
                                                    "kind": "TOOL_CALL",
                                                    "content": filtered(
                                                        {
                                                            "tool_name": name,
                                                            "tool_call_id": item.part.tool_call_id,
                                                            "args": item.part.args_as_dict(),
                                                        }
                                                    ),
                                                }
                                            )
                                        elif isinstance(item, FunctionToolResultEvent):
                                            result = filtered(item.part.content)
                                            item.part.content = result
                                            messages.append(
                                                {
                                                    "kind": "TOOL_RESULT",
                                                    "content": {
                                                        "tool_name": item.part.tool_name,
                                                        "tool_call_id": item.part.tool_call_id,
                                                        "result": result,
                                                    },
                                                }
                                            )
                                            if isinstance(result, dict) and result.get("ok") is True:
                                                amounts.update(amounts_in(result))
                                            if isinstance(result, dict) and result.get("ok"):
                                                block = build_block(
                                                    result.get("meta", {}).get("operation"),
                                                    result["data"],
                                                    role=principal.role,
                                                )
                                                if block:
                                                    deps.blocks.append(block)
                                            if not isinstance(result, dict) or result.get("ok") is not True:
                                                deps.blocks.clear()
                                            for block in deps.blocks:
                                                data = block.model_dump(mode="json")
                                                payload = {"type": data.pop("type"), "payload": data}
                                                blocks[payload["type"]] = payload
                                                yield event("block", payload)
                                            deps.blocks.clear()
                        if run.result:
                            responses = [m for m in run.result.new_messages() if isinstance(m, ModelResponse)]
                            if responses:
                                model_version = responses[-1].model_name
                for warning in check_reply(
                    final_text,
                    Evidence(frozenset(), frozenset(amounts)),
                    allowed_link_hosts=settings.allowed_link_hosts,
                ):
                    denied("UNVERIFIED_LINK" if warning == LINK_WARNING else "UNVERIFIED_REPLY")
                    notice = {"level": "WARN", "text": warning}
                    notices.append(notice)
                    yield event("notice", notice)
                state = "AWAITING_CONFIRMATION" if await repo.get_pending_action(conversation_id) else "COMPLETED"
        except UsageLimitExceeded:
            denied("LIMIT_EXCEEDED")
            code = "LIMIT_EXCEEDED"
        except TimeoutError:
            code = "LIMIT_EXCEEDED" if timeout.expired() else "BACKEND_UNAVAILABLE"
            denied(code)
        except ToolError as exc:
            code = exc.code
        except Exception as exc:
            code = "BACKEND_UNAVAILABLE"
            # Không nuốt lỗi lạ: ghi loại lỗi (không ghi nội dung) để còn điều tra.
            log_event(logger, "turn.error", turn_id=str(turn_id), outcome="FAILED", error_code=type(exc).__name__)
        finally:
            final_text = redact(final_text)
            if final_text:
                messages.append({"kind": "ASSISTANT", "content": {"text": redact(final_text)}})

            async def finish():
                try:
                    await persist()
                finally:
                    await repo.release_turn(conversation_id, turn_id)

            task = asyncio.create_task(finish())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            except Exception:
                state, code = "FAILED", "BACKEND_UNAVAILABLE"
        yield event("turn.completed", {"status": state}) if state != "FAILED" else event("turn.failed", {"code": code})

    return stream()
