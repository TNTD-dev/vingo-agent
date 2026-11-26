"""T08/T10/T11: Postgres thật, backend giả và side effect được đếm."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from vingo_agent.actions import confirm as decisions
from vingo_agent.actions import execution as runner
from vingo_agent.actions import lookup, proposals, registry
from vingo_agent.api.app import create_app
from vingo_agent.api.auth import Principal
from vingo_agent.config import get_settings
from vingo_agent.guardrails.tool import before_tool_call
from vingo_agent.persistence import db
from vingo_agent.persistence import repositories as repo
from vingo_agent.persistence.models import execution, pending_action
from vingo_agent.schemas.chat import Buyer, Role, VisibleAction
from vingo_agent.tools import decisions as tools
from vingo_agent.tools.base import ToolError, TurnDeps

pytest_plugins = ["tests.integration.test_migration"]
pytestmark = pytest.mark.integration


@pytest.fixture
async def store(database_url, monkeypatch):
    engine = create_async_engine(database_url)

    @asynccontextmanager
    async def transaction():
        async with engine.begin() as conn:
            yield conn

    for module in (db, repo, runner, decisions):
        monkeypatch.setattr(module, "transaction", transaction)
    yield engine
    await engine.dispose()


@pytest.fixture
async def setup_action(store):
    principal = Principal(uuid4(), Role.ADMIN, "session-secret")
    conv = await repo.create_conversation(principal.user_id, principal.role)

    async def create(operation="CREATE_EVENT", **kwargs):
        return await proposals.create(
            conversation_id=conv["id"],
            actor_id=principal.user_id,
            role=principal.role,
            operation=operation,
            payload={"body": {"title": "Demo"}, "path": None},
            display={},
            **kwargs,
        )

    return principal, conv["id"], create


def backend(response=None):
    return SimpleNamespace(
        write=AsyncMock(return_value=response or httpx.Response(200, json={"id": str(uuid4()), "email": "private"}))
    )


async def confirm(action, principal, client, **kwargs):
    return await decisions.confirm(
        action_id=action[0],
        version=action[1],
        principal=principal,
        via=decisions.ConfirmedVia.BUTTON,
        backend=client,
        **kwargs,
    )


async def test_T10_concurrentAndRepeatedConfirm_callsBackendOnce(setup_action):
    principal, cid, create = setup_action
    action = await create()
    client = backend()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"id": str(cid), "buyer": "secret"})

    client.write.side_effect = send
    first = asyncio.create_task(confirm(action, principal, client))
    await entered.wait()
    second = asyncio.create_task(confirm(action, principal, client))
    await asyncio.sleep(0.05)
    release.set()
    a, b = await asyncio.gather(first, second)
    assert a == b == await confirm(action, principal, client)
    assert a[0] == ("SUCCEEDED", None, {"event_id": str(cid)})
    assert client.write.await_count == 1
    assert await repo.has_ref(cid, "EVENT", cid)
    messages = await repo.get_messages(cid)
    assert len(messages) == 1
    assert messages[0]["content"] == {"operation": "CREATE_EVENT", "status": "SUCCEEDED", "ids": {"event_id": str(cid)}}


@pytest.mark.parametrize("case", ["version", "expired", "actor", "role", "old_tab"])
async def test_T08_confirmBlocksInvalidAction_withoutWrite(setup_action, store, case):
    principal, _, create = setup_action
    action = await create(expires_at=datetime.now(UTC) - timedelta(seconds=1) if case == "expired" else None)
    if case == "version":
        action = action[0], 2
    if case == "actor":
        principal = replace(principal, user_id=uuid4())
    if case == "role":
        principal = replace(principal, role=Role.BUYER)
    if case == "old_tab":
        await create()
    client = backend()
    with pytest.raises(ToolError, match="FORBIDDEN" if case in {"actor", "role"} else "ACTION_NOT_PENDING"):
        await confirm(action, principal, client)
    client.write.assert_not_awaited()
    if case == "expired":
        async with store.connect() as conn:
            assert (
                await conn.scalar(select(pending_action.c.state).where(pending_action.c.id == action[0])) == "EXPIRED"
            )


@pytest.mark.parametrize("case", ["missing", "not_first", "version", "old_tab", "other_conversation"])
async def test_T08_textChecksVisibleActionBeforeWrite(setup_action, case):
    principal, cid, create = setup_action
    action = await create()
    deps = TurnDeps(principal, cid, uuid4(), backend(), visible_action=VisibleAction(action_id=action[0], version=1))
    if case == "missing":
        deps.visible_action = None
    if case == "not_first":
        deps.tool_calls = 1
    if case == "version":
        deps.visible_action.version = 2
    if case == "old_tab":
        await create()
    if case == "other_conversation":
        deps.conversation_id = (await repo.create_conversation(principal.user_id, principal.role))["id"]
    with pytest.raises(ToolError, match="ACTION_NOT_PENDING"):
        before_tool_call(deps, "confirm_pending_action")
        await tools.confirm_pending_action(SimpleNamespace(deps=deps))
    deps.backend.write.assert_not_awaited()


async def test_textConfirm_usesSharedExecutionAndBlocks(setup_action):
    principal, cid, create = setup_action
    action = await create()
    deps = TurnDeps(principal, cid, uuid4(), backend(), visible_action=VisibleAction(action_id=action[0], version=1))
    before_tool_call(deps, "confirm_pending_action")
    result = await tools.confirm_pending_action(SimpleNamespace(deps=deps))
    assert result["data"] == {"status": "SUCCEEDED"}
    assert deps.blocks[0].status == "SUCCEEDED"
    row = await runner.load(action[0])
    assert row["confirmed_via"] == "TEXT"
    assert deps.backend.write.await_count == 1


async def test_T11_timeoutThenLookup_reusesKeyAndDoesNotDuplicateWrite(setup_action, monkeypatch):
    principal, _, create = setup_action
    # Nhóm mới đăng ký spec ngay trong test, không sửa khung.
    op = "TEST_IDEMPOTENT"
    monkeypatch.setitem(
        registry._specs,
        op,
        registry.OperationSpec(
            "holdSeats",
            "propose_test",
            "ADMIN",
            safe_retry=True,
            lookup_mode=registry.LookupMode.RETRY,
            project=lambda data: {"order_id": data["id"]},
            result_refs={"order_id": "ORDER"},
        ),
    )
    action = await create(operation=op)
    keys = []
    committed = {}

    async def send(*args, idempotency_key, **kwargs):
        keys.append(idempotency_key)
        committed.setdefault(idempotency_key, str(uuid4()))
        if len(keys) <= 2:
            raise httpx.ReadTimeout("secret backend message")
        return httpx.Response(200, json={"id": committed[idempotency_key], "buyer": "secret"})

    client = backend()
    client.write.side_effect = send
    outcome, _ = await confirm(action, principal, client)
    assert outcome == ("UNKNOWN", "EXECUTION_UNKNOWN", {})
    assert (await runner.load(action[0]))["execution_state"] == "UNKNOWN"
    resolved = await lookup.resolve(action[0], backend=client)
    assert resolved[0] == "SUCCEEDED"
    assert len(set(keys)) == len(committed) == 1
    assert len(keys) == 3


async def test_unknownCreate_isNeverRetried(setup_action):
    principal, _, create = setup_action
    action = await create()
    client = backend()
    client.write.side_effect = httpx.ConnectError("private")
    assert (await confirm(action, principal, client))[0][0] == "UNKNOWN"
    assert (await lookup.resolve(action[0], backend=client))[0] == "UNKNOWN"
    assert client.write.await_count == 1


@pytest.mark.parametrize("code", ["FORM_REQUIRED", "CONFIRM_PHRASE_REQUIRED", "STALE_PROPOSAL", "FORBIDDEN"])
async def test_specGuards_preventBackendWrite(setup_action, monkeypatch, code):
    principal, _, create = setup_action
    spec = registry.get_spec("CREATE_EVENT")
    if code == "FORM_REQUIRED":
        spec = replace(spec, needs_form=True)
    if code == "CONFIRM_PHRASE_REQUIRED":
        spec = replace(spec, confirm_phrase=lambda payload: "Demo")
    if code == "STALE_PROPOSAL":
        spec = replace(spec, check_before=AsyncMock(return_value=False))
    if code == "FORBIDDEN":
        monkeypatch.setenv("AGENT_DISABLED_TOOLS", "propose_create_event")
        get_settings.cache_clear()
    monkeypatch.setitem(registry._specs, "CREATE_EVENT", spec)
    action = await create()
    client = backend()
    if code == "STALE_PROPOSAL":
        assert (await confirm(action, principal, client))[0] == ("FAILED", code, {})
    else:
        with pytest.raises(ToolError, match=code):
            await confirm(action, principal, client)
    client.write.assert_not_awaited()


async def test_formIsEphemeral_phraseAndSuccessHookSupported(setup_action, monkeypatch):
    principal, cid, create = setup_action
    spec = registry.get_spec("CREATE_EVENT")
    hook = AsyncMock()

    def request(payload, buyer):
        return {"body": {**payload["body"], "buyer": buyer.model_dump()}}

    monkeypatch.setitem(
        registry._specs,
        "CREATE_EVENT",
        replace(spec, needs_form=True, confirm_phrase=lambda p: "Demo", build_request=request, after_success=hook),
    )
    action = await create()
    client = backend()
    buyer = Buyer(full_name="secret-name", email="secret@example.com", phone="0123456789")
    outcome, _ = await confirm(action, principal, client, buyer=buyer, confirm_phrase="Demo")
    assert outcome[0] == "SUCCEEDED"
    assert client.write.call_args.kwargs["body"]["buyer"]["email"] == buyer.email
    hook.assert_awaited_once()
    assert "secret-name" not in repr(dict(await runner.load(action[0])))
    assert "secret@example.com" not in repr(await repo.get_messages(cid))


async def test_rejectAndSupersedeAreAtomic(setup_action):
    principal, cid, create = setup_action
    first, second = await asyncio.gather(create(), create())
    current = await repo.get_pending_action(cid)
    assert current["action_id"] in {first[0], second[0]}
    assert await decisions.reject(action_id=current["action_id"], version=1, principal=principal) == "REJECTED"
    assert await repo.get_pending_action(cid) is None


async def test_routesConfirmSSEAndRejectAndHistoryLookup(setup_action, monkeypatch):
    principal, cid, create = setup_action
    action = await create()
    client_backend = backend()
    client_backend.aclose = AsyncMock()
    monkeypatch.setattr("vingo_agent.clients.backend.BackendClient", lambda *args: client_backend)
    headers = {
        "X-Agent-Service-Key": "test-service-key",
        "X-Vingo-User-Id": str(principal.user_id),
        "X-Vingo-Role": "ADMIN",
        "Authorization": "Bearer current-token",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://agent") as client:
        response = await client.post(
            f"/internal/v1/actions/{action[0]}/confirm",
            headers=headers,
            json={"command_id": str(uuid4()), "version": 1},
        )
        assert response.status_code == 200, response.text
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert [e["type"] for e in events] == ["turn.started", "block", "turn.completed"]
        assert events[1]["payload"]["payload"]["status"] == "SUCCEEDED"
        recovered = await client.get(f"/internal/v1/turns/{events[0]['turn_id']}", headers=headers)
        assert recovered.status_code == 200
        assert recovered.json()["blocks"][0]["status"] == "SUCCEEDED"
        next_action = await create()
        rejected = await client.post(
            f"/internal/v1/actions/{next_action[0]}/reject",
            headers=headers,
            json={"command_id": str(uuid4()), "version": 1},
        )
        assert rejected.json() == {"state": "REJECTED"}
        history = await client.get(f"/internal/v1/conversations/{cid}", headers=headers)
        assert history.status_code == 200
        assert history.json()["messages"][0]["content"]["status"] == "SUCCEEDED"


async def test_lookupSuccess_callsHookOnce_andPersistsRecoverableBlocks(setup_action, monkeypatch):
    principal, cid, create = setup_action
    hook = AsyncMock()
    spec = replace(registry.get_spec("PUBLISH_EVENT"), after_success=hook)
    monkeypatch.setitem(registry._specs, "PUBLISH_EVENT", spec)
    action = await create(operation="PUBLISH_EVENT")
    client = backend()
    client.write.side_effect = [
        httpx.ReadTimeout("private"),
        httpx.ReadTimeout("private"),
        httpx.Response(200, json={"id": str(cid)}),
    ]
    await confirm(action, principal, client)
    assert hook.await_count == 0
    assert (await lookup.resolve(action[0], backend=client))[0] == "SUCCEEDED"
    assert (await lookup.resolve(action[0], backend=client))[0] == "SUCCEEDED"
    hook.assert_awaited_once()
    messages = await repo.get_messages(cid)
    recovered = await repo.get_turn(messages[-1]["turn_id"])
    assert recovered["final_text_and_blocks"]["blocks"][0]["status"] == "SUCCEEDED"


async def test_lookupKillSwitch_preventsRetry(setup_action, monkeypatch):
    principal, _, create = setup_action
    action = await create(operation="PUBLISH_EVENT")
    client = backend()
    client.write.side_effect = httpx.ReadTimeout("private")
    await confirm(action, principal, client)
    assert client.write.await_count == 2
    monkeypatch.setenv("AGENT_DISABLED_TOOLS", "propose_publish_event")
    get_settings.cache_clear()
    assert (await lookup.resolve(action[0], backend=client))[0] == "UNKNOWN"
    assert client.write.await_count == 2


async def test_beforeReadFailure_neverPermitsRecoveryWrite(setup_action, monkeypatch):
    principal, _, create = setup_action
    spec = replace(
        registry.get_spec("PUBLISH_EVENT"), check_before=AsyncMock(side_effect=ToolError("BACKEND_UNAVAILABLE"))
    )
    monkeypatch.setitem(registry._specs, "PUBLISH_EVENT", spec)
    action = await create(operation="PUBLISH_EVENT")
    client = backend()
    assert (await confirm(action, principal, client))[0] == ("FAILED", "BACKEND_UNAVAILABLE", {})
    assert (await lookup.resolve(action[0], backend=client))[0] == "FAILED"
    client.write.assert_not_awaited()


async def test_readLookup_usesCurrentUserToken_andDoesNotWrite(setup_action, monkeypatch):
    principal, cid, create = setup_action
    spec = registry.OperationSpec(
        "cancelOrder",
        "propose_example_cancel",
        "ADMIN",
        lookup_mode=registry.LookupMode.READ,
        lookup=AsyncMock(return_value={"order_id": str(cid)}),
        result_refs={"order_id": "ORDER"},
    )
    op = "TEST_READ"
    monkeypatch.setitem(registry._specs, op, spec)
    del registry._specs[op]
    registry.register(op, spec)
    action = await create(operation=op)
    initial = backend()
    initial.write.side_effect = httpx.ReadTimeout("private")
    spec.lookup.return_value = None
    await confirm(action, principal, initial)
    spec.lookup.return_value = {"order_id": str(cid)}
    current = backend()
    current.aclose = AsyncMock()
    constructor = Mock()

    # GET dùng BackendClient hiện tại, không giữ token request confirm cũ.
    def make_client(settings, token):
        constructor(token)
        return current

    monkeypatch.setattr("vingo_agent.api.routes.BackendClient", make_client)
    headers = {
        "X-Agent-Service-Key": "test-service-key",
        "X-Vingo-User-Id": str(principal.user_id),
        "X-Vingo-Role": "ADMIN",
        "Authorization": "Bearer new-session",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://agent") as client:
        response = await client.get(f"/internal/v1/conversations/{cid}", headers=headers)
    assert response.status_code == 200
    constructor.assert_called_once_with("new-session")
    current.write.assert_not_awaited()
    assert response.json()["messages"][-1]["content"]["blocks"][0]["status"] == "SUCCEEDED"


async def test_crashedRunningExecution_isResolvedOnlyAfterLease(setup_action, store):
    principal, _, create = setup_action
    action = await create(operation="PUBLISH_EVENT")
    async with store.begin() as conn:
        await conn.execute(update(pending_action).where(pending_action.c.id == action[0]).values(state="CONFIRMED"))
        from sqlalchemy import insert

        await conn.execute(insert(execution).values(action_id=action[0], state="RUNNING", idempotency_key=uuid4()))
    client = backend()
    assert (await lookup.resolve(action[0], backend=client))[0] == "RUNNING"
    client.write.assert_not_awaited()
    async with store.begin() as conn:
        await conn.execute(
            update(execution)
            .where(execution.c.action_id == action[0])
            .values(started_at=datetime.now(UTC) - timedelta(minutes=5))
        )
    assert (await lookup.resolve(action[0], backend=client))[0] == "SUCCEEDED"
    assert client.write.await_count == 1


async def test_T10_httpConcurrentSameCommand_replaysOneExecution(setup_action, monkeypatch):
    principal, _, create = setup_action
    action = await create()
    client_backend = backend()
    client_backend.aclose = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"id": str(uuid4())})

    client_backend.write.side_effect = send
    monkeypatch.setattr("vingo_agent.clients.backend.BackendClient", lambda *args: client_backend)
    headers = {
        "X-Agent-Service-Key": "test-service-key",
        "X-Vingo-User-Id": str(principal.user_id),
        "X-Vingo-Role": "ADMIN",
        "Authorization": "Bearer current-token",
    }
    body = {"command_id": str(uuid4()), "version": 1}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://agent") as client:
        first = asyncio.create_task(
            client.post(f"/internal/v1/actions/{action[0]}/confirm", headers=headers, json=body)
        )
        await entered.wait()
        second = asyncio.create_task(
            client.post(f"/internal/v1/actions/{action[0]}/confirm", headers=headers, json=body)
        )
        await asyncio.sleep(0.05)
        release.set()
        a, b = await asyncio.gather(first, second)
        replay = await client.post(f"/internal/v1/actions/{action[0]}/confirm", headers=headers, json=body)
    assert a.status_code == b.status_code == replay.status_code == 200
    assert a.text == b.text == replay.text
    assert client_backend.write.await_count == 1


async def test_httpRetry_recoversTurnAfterProcessDiesFollowingExecution(setup_action, monkeypatch):
    principal, _, create = setup_action
    action = await create()
    client_backend = backend()
    client_backend.aclose = AsyncMock()
    monkeypatch.setattr("vingo_agent.clients.backend.BackendClient", lambda *args: client_backend)
    original = repo.transition_turn
    calls = 0

    async def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("process interrupted")
        return await original(*args, **kwargs)

    monkeypatch.setattr(repo, "transition_turn", interrupted)
    headers = {
        "X-Agent-Service-Key": "test-service-key",
        "X-Vingo-User-Id": str(principal.user_id),
        "X-Vingo-Role": "ADMIN",
        "Authorization": "Bearer current-token",
    }
    body = {"command_id": str(uuid4()), "version": 1}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False), base_url="http://agent"
    ) as client:
        first = await client.post(f"/internal/v1/actions/{action[0]}/confirm", headers=headers, json=body)
        assert first.status_code == 500
        replay = await client.post(f"/internal/v1/actions/{action[0]}/confirm", headers=headers, json=body)
        assert replay.status_code == 200
    assert client_backend.write.await_count == 1


async def test_textReject_andTextFormPhraseCannotWrite(setup_action, monkeypatch):
    principal, cid, create = setup_action
    for field, code in [("needs_form", "FORM_REQUIRED"), ("confirm_phrase", "CONFIRM_PHRASE_REQUIRED")]:
        original = registry.get_spec("CREATE_EVENT")
        spec = replace(original, **{field: True if field == "needs_form" else lambda p: "Demo"})
        with monkeypatch.context() as patch:
            patch.setitem(registry._specs, "CREATE_EVENT", spec)
            action = await create()
            deps = TurnDeps(
                principal, cid, uuid4(), backend(), visible_action=VisibleAction(action_id=action[0], version=1)
            )
            before_tool_call(deps, "confirm_pending_action")
            with pytest.raises(ToolError, match=code):
                await tools.confirm_pending_action(SimpleNamespace(deps=deps))
            deps.backend.write.assert_not_awaited()
    action = await create()
    deps = TurnDeps(principal, cid, uuid4(), backend(), visible_action=VisibleAction(action_id=action[0], version=1))
    before_tool_call(deps, "reject_pending_action")
    assert (await tools.reject_pending_action(SimpleNamespace(deps=deps)))["data"] == {"status": "REJECTED"}
    deps.backend.write.assert_not_awaited()


async def test_afterSuccessHookFailure_keepsSucceededOutcome(setup_action, monkeypatch):
    # Thao tác ghi đã commit SUCCEEDED; bước phụ lỗi (vd tự tạo đề xuất thanh toán) không được làm hỏng kết quả.
    principal, _, create = setup_action
    hook = AsyncMock(side_effect=RuntimeError("payment proposal failed"))
    monkeypatch.setitem(registry._specs, "CREATE_EVENT", replace(registry.get_spec("CREATE_EVENT"), after_success=hook))
    action = await create()
    outcome, blocks = await confirm(action, principal, backend())
    assert outcome[0] == "SUCCEEDED"
    assert blocks[0].status == "SUCCEEDED"
    hook.assert_awaited_once()


async def test_disabledBetweenClaimAndSend_failsWithoutWrite(setup_action, monkeypatch):
    # Chưa gửi gì thì không được để UNKNOWN: lần tra sau có thể gửi thật một thao tác đã bị tắt.
    principal, _, create = setup_action
    action = await create(operation="PUBLISH_EVENT")
    client = backend()
    real_run = runner.run

    async def disable_then_run(*args, **kwargs):
        monkeypatch.setenv("AGENT_DISABLED_TOOLS", "propose_publish_event")
        get_settings.cache_clear()
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(runner, "run", disable_then_run)
    outcome, _ = await confirm(action, principal, client)
    assert outcome == ("FAILED", "FORBIDDEN", {})
    client.write.assert_not_awaited()
    get_settings.cache_clear()


@pytest.mark.parametrize("field,code", [("needs_form", "FORM_REQUIRED"), ("confirm_phrase", "CONFIRM_PHRASE_REQUIRED")])
async def test_buttonRefusal_isSseTurnFailed_notHttpError(setup_action, monkeypatch, field, code):
    # Backend chỉ map mã có trong ErrorCode của nó; 409 FORM_REQUIRED sẽ thành 503 ở backend.
    principal, _, create = setup_action
    spec = replace(registry.get_spec("CREATE_EVENT"), **{field: True if field == "needs_form" else lambda p: "Demo"})
    monkeypatch.setitem(registry._specs, "CREATE_EVENT", spec)
    action = await create()
    client_backend = backend()
    client_backend.aclose = AsyncMock()
    monkeypatch.setattr("vingo_agent.clients.backend.BackendClient", lambda *args: client_backend)
    headers = {
        "X-Agent-Service-Key": "test-service-key",
        "X-Vingo-User-Id": str(principal.user_id),
        "X-Vingo-Role": "ADMIN",
        "Authorization": "Bearer current-token",
    }
    body = {"command_id": str(uuid4()), "version": 1}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://agent") as client:
        for _ in range(2):  # lần hai là replay cùng command_id
            sent = await client.post(f"/internal/v1/actions/{action[0]}/confirm", headers=headers, json=body)
            assert sent.status_code == 200
            events = [json.loads(line[6:]) for line in sent.text.splitlines() if line.startswith("data: ")]
            assert events[-1]["type"] == "turn.failed" and events[-1]["payload"] == {"code": code}
    client_backend.write.assert_not_awaited()
