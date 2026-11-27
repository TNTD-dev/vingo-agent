import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import FunctionModel

from tests.unit.test_buyer_read import SHOW, ZONE, availability, event, seat, seatmap, suggestion
from vingo_agent.agents import orchestrator as o
from vingo_agent.agents.factory import build_model
from vingo_agent.api.auth import Principal
from vingo_agent.config import Settings, get_settings
from vingo_agent.guardrails.input import REDACTED, InputRejected, check_input, redact
from vingo_agent.guardrails.limits import usage_limits
from vingo_agent.guardrails.output import Evidence, check_reply
from vingo_agent.schemas.chat import Role, SendMessageRequest


@pytest.mark.parametrize("text,status", [("", 400), (" \t", 400), ("a\x00", 400), ("abcde", 413)])
def test_input_rejectsInvalidText(text, status):
    with pytest.raises(InputRejected) as error:
        check_input(text, max_chars=4)
    assert error.value.status_code == status


@pytest.mark.parametrize(
    "secret",
    [
        "Bearer abc123",
        "eyJhbGciOiJ9.abc.def",
        "OTP: 123456",
        "mã là 654321",
        "4111 1111 1111 1111",
        "https://x.test/a?X-Amz-Signature=secret",
    ],
)
def test_redact_hidesEachSecret(secret):
    result = redact(secret)
    assert REDACTED in result
    assert secret not in result


def test_redact_preservesNonCardAndPublicUrl():
    text = "4111 1111 1111 1112 https://x.test/a?id=123"
    assert redact(text) == text
    assert check_input(" x\n\t ", max_chars=10) == "x"


def test_limits_matchDesign():
    limits = usage_limits(Settings())
    assert (limits.request_limit, limits.tool_calls_limit) == (20, 40)


def test_T07_output_warnsWithoutEvidence():
    assert len(check_reply("Đã giữ vé 100.000 VND", Evidence(frozenset(), frozenset()))) == 2
    assert check_reply("Đã giữ vé 100.000 VND", Evidence(frozenset({"HOLD_SEATS"}), frozenset({100000}))) == []


@pytest.mark.parametrize(
    "budget,echo",
    [
        ("600 nghìn", "600.000"),
        ("600k", "600.000"),
        ("600 ngàn", "600.000"),
        ("1,2 triệu", "1.200.000"),
        ("1.5tr", "1.500.000"),
        ("600.000", "600.000"),
        ("600000đ", "600.000"),
        ("600.000 VND", "600.000"),
    ],
)
def test_B03_reply_acceptsBudgetFromCurrentUserMessage(budget, echo):
    evidence = Evidence(
        frozenset(), frozenset({250_000, 500_000}), user_text=f"Ngân sách tổng {budget} cho 2 vé, nên chọn khu nào?"
    )
    assert check_reply(f"Khu GA 250.000₫/vé, tổng 500.000₫, trong ngân sách {echo}₫.", evidence) == []


@pytest.mark.parametrize(
    "reply,warn",
    [
        ("Tổng 750.000₫", False),
        ("Tổng 5.000.000₫", False),
        ("Tổng 5.250.000₫", True),
        ("Giá 123.000₫", True),
        ("Giá 2₫", True),
        ("Tổng 1.200.000₫", True),
    ],
)
def test_B03_reply_boundsMultiplesToToolPrices(reply, warn):
    evidence = Evidence(frozenset(), frozenset({250_000}), user_text="Ngân sách 600 nghìn cho 2 vé.")
    assert check_reply(reply, evidence) == (["Giá chính xác xem trên thẻ."] if warn else [])


@pytest.mark.parametrize("status,fallback", [(500, True), (429, True), (400, False), (403, False)])
async def test_fallback_onlyRetriesTransientErrors(status, fallback):
    async def fail(messages, info):
        raise ModelHTTPError(status, "primary")
        yield ""

    async def success(messages, info):
        yield "dự phòng"

    agent = Agent(build_model(FunctionModel(stream_function=fail), FunctionModel(stream_function=success)))
    if fallback:
        async with agent.run_stream("hi") as result:
            assert await result.get_output() == "dự phòng"
    else:
        with pytest.raises(ModelHTTPError):
            async with agent.run_stream("hi") as result:
                await result.get_output()


async def test_fallback_doesNotRetryAfterText():
    called = False

    async def fail(messages, info):
        yield "chữ đầu"
        raise ModelHTTPError(500, "primary")

    async def success(messages, info):
        nonlocal called
        called = True
        yield "khác"

    agent = Agent(build_model(FunctionModel(stream_function=fail), FunctionModel(stream_function=success)))
    with pytest.raises(ModelHTTPError):
        async with agent.run_stream("hi") as result:
            await result.get_output()
    assert not called


@pytest.fixture
def turn_env(monkeypatch):
    principal = Principal(uuid4(), Role.BUYER, "session-secret")
    cid, tid = uuid4(), uuid4()
    mocks = {}
    values = {
        "get_conversation": {"owner_id": principal.user_id, "role": "BUYER"},
        "create_turn": {"id": tid, "state": "RUNNING"},
        "acquire_turn": True,
        "get_pending_action": None,
        "get_messages": [],
        "load_context": (None, []),
        "append_messages": None,
        "transition_turn": True,
        "release_turn": None,
        "set_model_version": None,
        "get_last_input_usage": None,
        "set_input_usage": None,
        "discard_unstarted_turn": None,
    }
    for name, value in values.items():
        mocks[name] = AsyncMock(return_value=value)
        monkeypatch.setattr(o.repo, name, mocks[name])
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    monkeypatch.setattr(
        o, "build_context", AsyncMock(return_value=[ModelRequest(parts=[UserPromptPart("OTP [đã ẩn]")])])
    )
    monkeypatch.setattr(o, "compact_if_needed", AsyncMock())
    return principal, cid, tid, mocks


def request():
    return SendMessageRequest(command_id=uuid4(), text="OTP 123456")


async def test_T01_turn_streamsInOrderAndSavesRedactedText(turn_env, monkeypatch):
    principal, cid, tid, mocks = turn_env

    from pydantic_ai.messages import ModelRequest, UserPromptPart

    secrets = ("123456", "0901234567", "a.b+c@vingo.vn")
    seen_requests = []

    async def context(conversation_id, *, new_message, **kwargs):
        return [ModelRequest(parts=[UserPromptPart(new_message)])]

    monkeypatch.setattr(o, "build_context", context)

    async def text(messages, info):
        seen_requests.append(messages)
        assert all(secret not in repr(messages) for secret in secrets)
        assert "OTP [đã ẩn], SĐT [đã ẩn], email [đã ẩn]" in repr(messages)
        yield "Xin "
        yield "chào"

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=text))
    monkeypatch.setattr(o, "build_agent", lambda role, model, **kwargs: Agent(model))
    stream = await o.run_turn(
        conversation_id=cid,
        request=request().model_copy(update={"text": "OTP 123456, SĐT 0901234567, email a.b+c@vingo.vn"}),
        principal=principal,
    )
    events = [event async for event in stream]
    assert events[0].type == "turn.started"
    assert events[-1].type == "turn.completed"
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert sum(e.type in {"turn.completed", "turn.failed"} for e in events) == 1
    assert "123456" not in repr(mocks["append_messages"].call_args)
    assert seen_requests
    assert all(secret not in repr(mocks["append_messages"].call_args) for secret in secrets)
    assert REDACTED in repr(mocks["append_messages"].call_args)
    mocks["release_turn"].assert_awaited_once_with(cid, tid)
    assert mocks["set_model_version"].await_count == 1


@pytest.mark.parametrize(
    "reply,warn",
    [
        ("Giá 250.000 đồng.", False),
        ("Tổng 750.000 đồng.", False),
        ("Giá 123.000 đồng.", True),
    ],
    ids=["context-price", "three-tickets", "summary-only"],
)
async def test_B03_turn_checksRetainedToolPriceWithoutNewToolCall(turn_env, monkeypatch, reply, warn):
    from vingo_agent.context import builder

    principal, cid, _, mocks = turn_env
    old_turn = uuid4()
    old_call = {"tool_name": "get_availability", "tool_call_id": "old"}
    mocks["load_context"].return_value = (
        {"content": "Giá 123.000 đồng."},
        [
            {"turn_id": old_turn, "kind": "TOOL_CALL", "content": old_call},
            {
                "turn_id": old_turn,
                "kind": "TOOL_RESULT",
                "content": old_call | {"result": {"ok": True, "data": {"price": 250_000}}},
            },
        ],
    )
    monkeypatch.setattr(builder, "load_context", mocks["load_context"])
    monkeypatch.setattr(o, "build_context", builder.build_context)

    async def respond(messages, info):
        yield reply

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=respond))
    req = SendMessageRequest(command_id=uuid4(), text="Nhắc lại giá giúp tôi.")
    events = [e async for e in await o.run_turn(conversation_id=cid, request=req, principal=principal)]
    assert events[-1].type == "turn.completed"
    assert "".join(e.payload["text"] for e in events if e.type == "text.delta") == reply
    assert not any(e.type == "progress" for e in events)
    assert [e.payload for e in events if e.type == "notice"] == (
        [{"level": "WARN", "text": "Giá chính xác xem trên thẻ."}] if warn else []
    )


@pytest.mark.parametrize("valid_result", [True, False], ids=["current-price", "invalid-result"])
async def test_B03_turn_checksBudgetAndCurrentToolPrice(turn_env, monkeypatch, respx_mock, valid_result):
    from pydantic_ai.models.function import DeltaToolCall

    principal, cid, _, _ = turn_env
    monkeypatch.setattr(o.repo, "has_ref", AsyncMock(return_value=True))
    monkeypatch.setattr(o.repo, "add_refs", AsyncMock())
    raw = availability()
    raw["zones"][0]["tier"]["price"] = 250_000
    if not valid_result:
        raw["status"] = "INVALID"
    route = respx_mock.get(f"{get_settings().backend_url}/showtimes/{SHOW}/availability").respond(json=raw)

    async def respond(messages, info):
        if any(
            getattr(p, "part_kind", "") == "tool-return" and p.tool_call_id == "current"
            for m in messages
            for p in m.parts
        ):
            yield "Giá 250.000₫/vé, tổng 500.000₫, trong ngân sách 600.000₫."
        else:
            yield {
                0: DeltaToolCall(
                    name="get_availability", json_args=json.dumps({"showtime_id": str(SHOW)}), tool_call_id="current"
                )
            }

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=respond))
    req = SendMessageRequest(command_id=uuid4(), text="Ngân sách tổng 600 nghìn cho 2 vé, nên chọn khu nào?")
    events = [e async for e in await o.run_turn(conversation_id=cid, request=req, principal=principal)]
    assert route.call_count == 1 and events[-1].type == "turn.completed"
    assert [e.payload["type"] for e in events if e.type == "block"] == (["ZoneAvailability"] if valid_result else [])
    assert [e.payload for e in events if e.type == "notice"] == (
        [] if valid_result else [{"level": "WARN", "text": "Giá chính xác xem trên thẻ."}]
    )


async def test_T13_turn_rejectsBusyBeforeStream(turn_env):
    principal, cid, tid, mocks = turn_env
    mocks["acquire_turn"].return_value = False
    with pytest.raises(HTTPException) as error:
        await o.run_turn(conversation_id=cid, request=request(), principal=principal)
    assert error.value.status_code == 409
    mocks["append_messages"].assert_not_awaited()


async def test_turn_releasesWhenClientCloses(turn_env, monkeypatch):
    principal, cid, tid, mocks = turn_env
    stream = await o.run_turn(conversation_id=cid, request=request(), principal=principal)
    assert (await anext(stream)).type == "turn.started"
    await stream.aclose()
    mocks["release_turn"].assert_awaited_once_with(cid, tid)
    assert mocks["transition_turn"].call_args.args[2] == "FAILED"


async def test_turn_failsAndReleasesOnModelError(turn_env, monkeypatch):
    principal, cid, tid, mocks = turn_env

    async def fail(messages, info):
        raise ModelHTTPError(500, "fake")
        yield ""

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=fail))
    monkeypatch.setattr(o, "build_agent", lambda role, model, **kwargs: Agent(model))
    stream = await o.run_turn(conversation_id=cid, request=request(), principal=principal)
    events = [e async for e in stream]
    assert events[-1].payload == {"code": "BACKEND_UNAVAILABLE"}
    mocks["release_turn"].assert_awaited_once()


async def test_turn_deadlineEmitsLimitExceeded(turn_env, monkeypatch):
    principal, cid, tid, mocks = turn_env
    monkeypatch.setattr(o, "get_settings", lambda: get_settings().model_copy(update={"turn_deadline_s": 0.01}))

    async def slow(messages, info):
        await asyncio.sleep(1)
        yield "late"

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=slow))
    monkeypatch.setattr(o, "build_agent", lambda role, model, **kwargs: Agent(model))
    events = [e async for e in await o.run_turn(conversation_id=cid, request=request(), principal=principal)]
    assert events[-1].payload == {"code": "LIMIT_EXCEEDED"}


@pytest.mark.parametrize(
    "tool,args,path,raw,block_type",
    [
        ("search_events", {"q": "Demo"}, "/events", {"data": [event() | {"id": str(uuid4())}]}, "EventList"),
        (
            "get_seatmap",
            {"showtime_id": str(SHOW), "zone_id": str(ZONE)},
            f"/showtimes/{SHOW}/seatmap",
            seatmap([seat()]),
            "SeatMap",
        ),
        (
            "suggest_seats",
            {"showtime_id": str(SHOW), "zone_id": str(ZONE), "quantity": 2},
            f"/showtimes/{SHOW}/seat-suggestions",
            suggestion(),
            "SeatMap",
        ),
    ],
    ids=["search_events", "get_seatmap", "suggest_seats"],
)
async def test_turn_progressPrecedesToolAndBuildsBlock(
    turn_env, monkeypatch, respx_mock, tool, args, path, raw, block_type
):
    from pydantic_ai.models.function import DeltaToolCall

    principal, cid, _, _ = turn_env
    monkeypatch.setattr(o.repo, "has_ref", AsyncMock(return_value=True))
    monkeypatch.setattr(o.repo, "add_refs", AsyncMock())
    route = respx_mock.get(get_settings().backend_url + path).respond(json=raw)

    async def respond_stream(messages, info):
        if not any(getattr(part, "part_kind", "") == "tool-return" for m in messages for part in m.parts):
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args), tool_call_id="call1")}
        else:
            yield "Kết quả"

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=respond_stream))
    events = [e async for e in await o.run_turn(conversation_id=cid, request=request(), principal=principal)]
    assert route.call_count == 1
    assert events[-1].type == "turn.completed" and events[-1].payload["status"] == "COMPLETED"
    blocks = [e.payload for e in events if e.type == "block"]
    assert [block["type"] for block in blocks] == [block_type]
    if block_type == "SeatMap":
        assert blocks[0]["payload"]["showtime_id"] == str(SHOW)
        assert blocks[0]["payload"]["zone_id"] == str(ZONE)
    kinds = [e.type for e in events]
    assert kinds.index("progress") < kinds.index("block") < kinds.index("text.delta")


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionError()])
async def test_fallback_handlesTransportErrors(error):
    async def fail(messages, info):
        raise error
        yield ""

    async def success(messages, info):
        yield "OK"

    agent = Agent(build_model(FunctionModel(stream_function=fail), FunctionModel(stream_function=success)))
    async with agent.run_stream("hi") as result:
        assert await result.get_output() == "OK"


@pytest.mark.parametrize("field", ["max_model_calls", "max_tool_calls"])
async def test_T12_turn_enforcesUsageLimits(turn_env, monkeypatch, field):
    from pydantic_ai.models.function import DeltaToolCall

    principal, cid, tid, mocks = turn_env
    monkeypatch.setattr(o, "get_settings", lambda: get_settings().model_copy(update={field: 1}))
    tool_count = 0

    async def lookup():
        nonlocal tool_count
        tool_count += 1
        return {"ok": True, "data": {}, "meta": {"operation": "fake"}}

    async def loop(messages, info):
        yield {0: DeltaToolCall(name="lookup", json_args="{}", tool_call_id=f"call{tool_count}")}

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=loop))
    monkeypatch.setattr(o, "build_agent", lambda role, model, **kwargs: Agent(model, tools=[lookup]))
    events = [e async for e in await o.run_turn(conversation_id=cid, request=request(), principal=principal)]
    assert events[-1].payload == {"code": "LIMIT_EXCEEDED"}
    assert tool_count == 1
    mocks["release_turn"].assert_awaited_once()


async def test_turn_providerTimeoutIsNotTurnDeadline(turn_env, monkeypatch):
    principal, cid, tid, mocks = turn_env

    async def fail(messages, info):
        raise TimeoutError()
        yield ""

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=fail))
    monkeypatch.setattr(o, "build_agent", lambda role, model, **kwargs: Agent(model))
    events = [e async for e in await o.run_turn(conversation_id=cid, request=request(), principal=principal)]
    assert events[-1].payload == {"code": "BACKEND_UNAVAILABLE"}


async def test_turn_releasesLeaseDuringTaskCancellation(turn_env, monkeypatch):
    principal, cid, tid, mocks = turn_env
    started = asyncio.Event()

    async def slow(messages, info):
        started.set()
        await asyncio.Event().wait()
        yield "never"

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=slow))
    monkeypatch.setattr(o, "build_agent", lambda role, model, **kwargs: Agent(model))
    stream = await o.run_turn(conversation_id=cid, request=request(), principal=principal)

    async def consume():
        return [e async for e in stream]

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    mocks["release_turn"].assert_awaited_once_with(cid, tid)


async def test_T02_turn_rejectsWrongOwnerBeforeCreatingTurn(turn_env):
    principal, cid, tid, mocks = turn_env
    mocks["get_conversation"].return_value = {"owner_id": uuid4(), "role": "BUYER"}
    with pytest.raises(HTTPException) as error:
        await o.run_turn(conversation_id=cid, request=request(), principal=principal)
    assert error.value.status_code == 404
    mocks["create_turn"].assert_not_awaited()


async def test_fallback_doesNotStopOnEmptyTextBeforeFailure():
    async def fail(messages, info):
        yield ""
        raise ModelHTTPError(500, "primary")

    async def success(messages, info):
        yield "OK"

    agent = Agent(build_model(FunctionModel(stream_function=fail), FunctionModel(stream_function=success)))
    async with agent.run_stream("hi") as result:
        assert await result.get_output() == "OK"


@pytest.mark.parametrize("input_tokens,should_compact", [(40, False), (900, True)])
async def test_T12_turn_compactsByMeasuredTokens_insteadOfReprSize(turn_env, monkeypatch, input_tokens, should_compact):
    from contextlib import asynccontextmanager

    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models.function import DeltaToolCall

    from vingo_agent.context import compaction
    from vingo_agent.context.tokens import text_chars

    principal, cid, tid, mocks = turn_env
    # Ngưỡng 800 token; context thật 1.000 ký tự nhưng repr > 800. Request đầu dùng tỉ lệ mặc định.
    monkeypatch.setattr(
        o,
        "get_settings",
        lambda: get_settings().model_copy(
            update={
                "context_window_tokens": 1000,
                "output_reserved_tokens": 0,
            }
        ),
    )
    monkeypatch.setattr(compaction, "get_settings", o.get_settings)
    monkeypatch.setattr(o, "load_prompt", lambda role: "")
    monkeypatch.setattr(o, "build_context", AsyncMock(return_value=[ModelRequest(parts=[UserPromptPart("x" * 1000)])]))
    monkeypatch.setattr(o, "compact_if_needed", compaction.compact_if_needed)
    rows = [{"turn_id": i, "seq": i, "kind": "USER", "content": {"text": "old"}} for i in range(1, 6)]
    load = AsyncMock(return_value=(None, rows))
    monkeypatch.setattr(compaction, "load_context", load)
    monkeypatch.setattr(compaction, "save_summary", AsyncMock())
    requests = []

    class MeasuredModel(FunctionModel):
        @asynccontextmanager
        async def request_stream(self, messages, *args, **kwargs):
            requests.append(text_chars(messages))
            assert len(repr(messages)) > 800
            async with super().request_stream(messages, *args, **kwargs) as response:
                response._usage.input_tokens = input_tokens
                yield response

    async def lookup():
        return {"ok": True, "data": {}, "meta": {"operation": "fake"}}

    async def respond(messages, info):
        if any(getattr(part, "part_kind", "") == "tool-return" for m in messages for part in m.parts):
            yield "OK"
        else:
            yield {0: DeltaToolCall(name="lookup", json_args="{}", tool_call_id="call1")}

    # Compact dùng callback LLM không-stream; chỉ nhánh vượt ngưỡng mới gọi callback này.
    async def summarize(messages, info):
        from pydantic_ai.messages import ModelResponse, TextPart
        from pydantic_ai.usage import RequestUsage

        return ModelResponse(parts=[TextPart("mục tiêu")], usage=RequestUsage(input_tokens=20))

    monkeypatch.setattr(o, "build_model", lambda *args: MeasuredModel(function=summarize, stream_function=respond))
    monkeypatch.setattr(o, "build_agent", lambda role, model, **kwargs: Agent(model, tools=[lookup]))
    events = [e async for e in await o.run_turn(conversation_id=cid, request=request(), principal=principal)]
    if should_compact:
        load.assert_awaited_once()
        compaction.save_summary.assert_awaited_once()
        # Summary không giảm context giả lập: giữ nguyên CONTEXT_LIMIT sau khi ước lượng lại.
        assert events[-1].payload == {"code": "CONTEXT_LIMIT"}
    else:
        load.assert_not_awaited()
        compaction.save_summary.assert_not_awaited()
        assert events[-1].type == "turn.completed"
    mocks["set_input_usage"].assert_awaited_once_with(tid, input_tokens, requests[-1])


async def test_turn_reusesPreviousRatio_withoutReusingAnchor(turn_env, monkeypatch):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    principal, cid, tid, mocks = turn_env
    mocks["get_last_input_usage"].return_value = {"last_input_tokens": 100, "last_input_chars": 1000}
    monkeypatch.setattr(o, "load_prompt", lambda role: "")
    monkeypatch.setattr(o, "build_context", AsyncMock(return_value=[ModelRequest(parts=[UserPromptPart("x" * 2000)])]))

    async def respond(messages, info):
        yield "OK"

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=respond))
    monkeypatch.setattr(o, "build_agent", lambda role, model, **kwargs: Agent(model))
    events = [e async for e in await o.run_turn(conversation_id=cid, request=request(), principal=principal)]
    assert events[-1].type == "turn.completed"
    # Ratio = (1.000 + 2 ký tự schema "[]") / 100 token = 10,02; ước lượng (2.000 + 2) / 10,02 → 200.
    # Anchor của turn cũ đã bỏ.
    assert o.compact_if_needed.call_args.args[1] == 200


async def test_T07_replyNotices_followStreamAndPersist(turn_env, monkeypatch, caplog):
    import logging

    principal, cid, tid, mocks = turn_env

    async def respond(messages, info):
        yield "Đã thanh toán 999.999 VND."

    monkeypatch.setattr(o, "build_model", lambda *args: FunctionModel(stream_function=respond))
    with caplog.at_level(logging.INFO):
        events = [e async for e in await o.run_turn(conversation_id=cid, request=request(), principal=principal)]
    warnings = [e.payload for e in events if e.type == "notice"]
    assert warnings == [
        {"level": "WARN", "text": "Thao tác chưa được thực hiện. Vui lòng xem thẻ bên dưới."},
        {"level": "WARN", "text": "Giá chính xác xem trên thẻ."},
    ]
    assert events[-1].type == "turn.completed"
    assert max(e.seq for e in events if e.type == "text.delta") < min(e.seq for e in events if e.type == "notice")
    persisted = mocks["transition_turn"].call_args.kwargs["final_text_and_blocks"]
    assert persisted["notices"] == warnings
    denied = [getattr(record, "fields", {}) for record in caplog.records if record.message == "guardrail.denied"]
    assert [f["error_code"] for f in denied] == ["UNVERIFIED_REPLY", "UNVERIFIED_REPLY"]
    assert all(f["outcome"] == "DENIED" for f in denied)
    assert "999.999" not in repr(denied)
