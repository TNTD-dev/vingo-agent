"""Hai điểm mở rộng của khung, từ response gốc đến turn và SSE của hai đường xác nhận."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from vingo_agent.actions import confirm as service
from vingo_agent.actions import execution, registry
from vingo_agent.agents import orchestrator
from vingo_agent.api import routes
from vingo_agent.persistence import repositories as repo
from vingo_agent.schemas.blocks import ConfirmAction, PaymentLink
from vingo_agent.schemas.chat import ConfirmRequest, SendMessageRequest, VisibleAction

pytest_plugins = ["tests.unit.test_turn_loop"]


def transaction_with(conn):
    @asynccontextmanager
    async def transaction():
        yield conn

    return transaction


def params(call):
    return call.args[0].compile().params


async def test_webBlocksAndHookStayOnTurn_whenSavingExecution(monkeypatch):
    action_id, cid, tid, order_id = (uuid4() for _ in range(4))
    expires = datetime.now(UTC)
    url = "https://gateway.test/pay?signed=test"
    card = ConfirmAction(
        action_id=uuid4(), version=1, operation="CREATE_PAYMENT", display={}, expires_at=expires, form="BUYER"
    )
    notice = {"level": "WARN", "text": "Giá đã đổi"}
    hook = AsyncMock(return_value=([card], [notice]))
    spec = registry.OperationSpec(
        "holdSeats",
        "test_proposal",
        "BUYER",
        project=lambda data: {"order_id": data["order_id"]},
        result_refs={"order_id": "ORDER"},
        web_blocks=lambda raw: [PaymentLink.model_validate(raw)],
        after_success=hook,
    )
    monkeypatch.setitem(registry._specs, "TEST_WEB", spec)
    backend = AsyncMock()
    backend.write.return_value = httpx.Response(
        201,
        json={
            "order_id": str(order_id),
            "expires_at": expires.isoformat(),
            "payment_url": url,
            "buyer": {"email": "test@example.test"},
            "qr_token": "private",
        },
    )
    action = {
        "operation": "TEST_WEB",
        "conversation_id": cid,
        "idempotency_key": uuid4(),
        "state": "CONFIRMED",
        "execution_state": "RUNNING",
        "payload": {"body": {}},
    }
    monkeypatch.setattr(execution, "load", AsyncMock(return_value=action))
    conn = AsyncMock()
    conn.scalar.side_effect = [action_id, 0, tid]
    monkeypatch.setattr(execution, "transaction", transaction_with(conn))
    outcome = await execution.run(action_id, backend=backend)
    assert outcome == ("SUCCEEDED", None, {"order_id": str(order_id)})
    assert params(conn.scalar.await_args_list[0])["result"] == outcome[2]
    statements = conn.execute.await_args_list
    ref, event, final = (params(call) for call in statements[1:])
    assert (ref["resource_type"], ref["resource_id"]) == ("ORDER", order_id)
    assert event["kind"] == "SYSTEM_EVENT"
    assert event["content"] == {"operation": "TEST_WEB", "status": "SUCCEEDED", "ids": outcome[2]}
    hook.assert_awaited_once_with(action_id=action_id, conversation_id=cid, result=outcome[2], backend=backend)
    saved = final["final_text_and_blocks"]
    assert saved["notices"] == [notice]
    assert [b["type"] for b in saved["blocks"]] == ["ActionResult", "PaymentLink", "ConfirmAction"]
    assert saved["blocks"][1]["payment_url"] == url and repr(saved).count(url) == 1
    assert "qr_token" not in repr(saved) and "test@example.test" not in repr(saved)


async def test_legacySuccessHookReturningNone_keepsOriginalProjectionAndBlocks(monkeypatch):
    hook = AsyncMock(return_value=None)
    monkeypatch.setitem(
        registry._specs, "LEGACY", registry.OperationSpec("createEvent", "old", "ADMIN", after_success=hook)
    )
    conn = AsyncMock()
    conn.scalar.side_effect = [uuid4(), 0, uuid4()]
    monkeypatch.setattr(execution, "transaction", transaction_with(conn))
    result = ("SUCCEEDED", None, {})
    assert (
        await execution.save(uuid4(), {"conversation_id": uuid4(), "operation": "LEGACY"}, result, expected="RUNNING")
        == result
    )
    final = params(conn.execute.await_args_list[-1])["final_text_and_blocks"]
    assert set(final) == {"text", "blocks"} and [b["type"] for b in final["blocks"]] == ["ActionResult"]
    hook.assert_awaited_once()


async def test_buttonAndTextEmitSameHookEvents_andReplayWithoutExecution(turn_env, monkeypatch):
    principal, cid, tid, mocks = turn_env
    action_id = uuid4()
    request = ConfirmRequest(command_id=uuid4(), version=1)
    action = {"conversation_id": cid, "version": 1, "state": "CONFIRMED", "operation": "HOLD_SEATS"}
    monkeypatch.setattr(repo, "get_action", AsyncMock(return_value=action))
    monkeypatch.setattr(service, "_lock", AsyncMock(return_value=action))
    existing = Mock()
    existing.mappings.return_value.one_or_none.return_value = {"state": "SUCCEEDED", "error_code": None, "result": {}}
    monkeypatch.setattr(service, "transaction", transaction_with(AsyncMock(execute=AsyncMock(return_value=existing))))
    card = ConfirmAction(
        action_id=uuid4(),
        version=1,
        operation="CREATE_PAYMENT",
        form="BUYER",
        display={"total_amount": 600000},
        expires_at=datetime.now(UTC),
    )
    notice = {"level": "WARN", "text": "Tổng tiền đã đổi thành 600.000 VND."}
    final = {
        "blocks": [
            b.model_dump(mode="json")
            for b in [*service.result_blocks(action_id, "HOLD_SEATS", ("SUCCEEDED", None, {})), card]
        ],
        "notices": [notice],
    }
    recovered = AsyncMock(return_value={"state": "COMPLETED", "final_text_and_blocks": final})
    monkeypatch.setattr(execution, "load_web_turn", recovered)
    run = AsyncMock(side_effect=AssertionError("Không được ghi khi đã có kết quả"))
    monkeypatch.setattr(execution, "run", run)

    async def button():
        response = await routes.confirm_action(action_id, request, principal)
        return "".join([chunk async for chunk in response.body_iterator])

    mocks["create_turn"].return_value = {"id": tid, "_created": True}
    original = await button()
    saved = mocks["transition_turn"].await_args.kwargs["final_text_and_blocks"]
    assert saved == {"text": "", **final}
    state = mocks["transition_turn"].await_args.args[2]
    assert state == "AWAITING_CONFIRMATION"
    mocks["create_turn"].return_value = {
        "id": tid,
        "_created": False,
        "state": state,
        "final_text_and_blocks": saved,
    }
    assert await button() == original
    button_events = [json.loads(line[6:]) for line in original.splitlines() if line.startswith("data: ")]
    assert [e["seq"] for e in button_events] == list(range(1, len(button_events) + 1))
    assert [e["type"] for e in button_events] == ["turn.started", "block", "block", "notice", "turn.completed"]
    expected = [(e["type"], e["payload"]) for e in button_events if e["type"] in ("block", "notice")]
    assert expected[-1] == ("notice", notice)

    async def model(messages, info):
        returns = [p for m in messages for p in m.parts if p.part_kind == "tool-return"]
        if not returns:
            yield {0: DeltaToolCall(name="confirm_pending_action", json_args="{}", tool_call_id="decision")}
        else:
            assert returns[-1].content["data"] == {"status": "SUCCEEDED"}
            yield "Đã giữ vé. Tổng tiền là 600.000 VND."

    monkeypatch.setattr(orchestrator, "build_model", lambda *_: FunctionModel(stream_function=model))
    monkeypatch.setattr(orchestrator, "ZoneInfo", lambda _: UTC)
    mocks["create_turn"].return_value = {"id": tid, "state": "RUNNING", "_created": True}
    mocks["get_pending_action"].return_value = {"action_id": action_id, "version": 1}
    msg = SendMessageRequest(
        command_id=uuid4(), text="Đồng ý", visible_action=VisibleAction(action_id=action_id, version=1)
    )

    async def text_events():
        stream = await orchestrator.run_turn(conversation_id=cid, request=msg, principal=principal)
        events = [e async for e in stream]
        assert events[-1].type == "turn.completed"
        return [(e.type, e.payload) for e in events if e.type in ("block", "notice")]

    assert await text_events() == expected
    saved = mocks["transition_turn"].await_args.kwargs["final_text_and_blocks"]
    assert saved["notices"] == [notice]
    mocks["create_turn"].return_value = {"id": tid, "state": "AWAITING_CONFIRMATION", "final_text_and_blocks": saved}
    assert await text_events() == expected
    assert recovered.await_count == 2
    run.assert_not_awaited()
