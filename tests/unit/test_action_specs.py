"""Registry và hồi quy catalog KAN-201, không sửa test hiện có."""

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from vingo_agent.actions.execution import execute_admin_catalog
from vingo_agent.actions.registry import LookupMode, OperationSpec, get_spec, register


@pytest.mark.parametrize(
    "operation,field",
    [
        ("CREATE_VENUE", "venue_id"),
        ("CREATE_EVENT", "event_id"),
        ("CREATE_SHOWTIME", "showtime_id"),
        ("PUBLISH_EVENT", "event_id"),
    ],
)
async def test_catalog_projectsOnlyValidId(operation, field):
    resource_id = str(uuid4())
    backend = AsyncMock()
    backend.write.return_value = httpx.Response(
        200, json={"id": resource_id, "email": "secret", "buyer": {"phone": "secret"}}
    )
    assert await execute_admin_catalog(
        operation, backend=backend, payload={"path": None, "body": {}}, idempotency_key=uuid4()
    ) == ("SUCCEEDED", None, {field: resource_id})
    backend.write.return_value = httpx.Response(200, json={"id": "invalid"})
    assert (await execute_admin_catalog(operation, backend=backend, payload={}, idempotency_key=uuid4())) == (
        "SUCCEEDED",
        None,
        {},
    )


@pytest.mark.parametrize("operation", ["PUBLISH_EVENT", "BLOCK_SEATS", "UNBLOCK_SEATS"])
async def test_catalog_retriesSafeOperationOnce_withSameKey(operation):
    backend = AsyncMock()
    backend.write.side_effect = [httpx.ReadTimeout("private"), httpx.Response(204)]
    key = uuid4()
    assert (await execute_admin_catalog(operation, backend=backend, payload={}, idempotency_key=key))[0] == "SUCCEEDED"
    assert backend.write.await_count == 2
    assert all(call.kwargs["idempotency_key"] == key for call in backend.write.call_args_list)


@pytest.mark.parametrize("operation", ["CREATE_EVENT", "CREATE_SHOWTIME", "CREATE_VENUE", "ADMIN_RESEND_TICKET"])
async def test_catalog_nonRetryableWrite_staysUnknown(operation):
    backend = AsyncMock()
    backend.write.side_effect = httpx.ConnectError("private")
    assert (await execute_admin_catalog(operation, backend=backend, payload={}, idempotency_key=uuid4())) == (
        "UNKNOWN",
        "EXECUTION_UNKNOWN",
        {},
    )
    backend.write.assert_awaited_once()


@pytest.mark.parametrize(
    "body,code",
    [
        ({"code": "EVENT_INCOMPLETE"}, "EVENT_INCOMPLETE"),
        ({"code": "SECRET"}, "VALIDATION_ERROR"),
        ({"code": {}}, "VALIDATION_ERROR"),
    ],
)
async def test_catalog_clientError_returnsSafeCodeWithoutRetry(body, code):
    backend = AsyncMock()
    backend.write.return_value = httpx.Response(422, json=body)
    assert (await execute_admin_catalog("PUBLISH_EVENT", backend=backend, payload={}, idempotency_key=uuid4())) == (
        "FAILED",
        code,
        {},
    )
    backend.write.assert_awaited_once()


def test_registry_acceptsGroupSpec_withoutChangingFramework(monkeypatch):
    from vingo_agent.actions import registry

    operation = "TEST_REGISTER"
    monkeypatch.setitem(registry._specs, operation, None)
    del registry._specs[operation]
    spec = OperationSpec("createEvent", "propose_test", "ADMIN")
    register(operation, spec)
    assert get_spec(operation) is spec
    with pytest.raises(ValueError):
        register(operation, spec)
    with pytest.raises(ValueError):
        register("TEST_READ", OperationSpec("getEvent", "propose_test", "ADMIN"))
    with pytest.raises(ValueError):
        register("TEST_UNSAFE", OperationSpec("createEvent", "propose_test", "ADMIN", lookup_mode=LookupMode.RETRY))


async def test_formOperation_canLookupWithKeyWithoutPersistedBuyer(monkeypatch):
    from vingo_agent.actions import registry
    from vingo_agent.actions.execution import execute

    operation = "TEST_FORM_LOOKUP"
    key = uuid4()
    builder = __import__("unittest.mock", fromlist=["Mock"]).Mock(return_value={"body": None})
    spec = OperationSpec(
        "createPayment",
        "propose_example_payment",
        "BUYER",
        needs_form=True,
        safe_retry=True,
        lookup_mode=LookupMode.RETRY,
        build_lookup_request=builder,
    )
    monkeypatch.setitem(registry._specs, operation, spec)
    backend = AsyncMock()
    backend.write.return_value = httpx.Response(200, json={})
    assert (
        await execute(operation, backend=backend, payload={"order_id": "saved"}, idempotency_key=key, lookup_only=True)
    )[0] == "SUCCEEDED"
    builder.assert_called_once_with({"order_id": "saved"})
    assert backend.write.call_args.kwargs["idempotency_key"] == key
    assert backend.write.call_args.kwargs["body"] is None


def test_registry_loadsEverySpecModule_withoutFrameworkImport():
    # Nhóm mới chỉ thêm file vào actions/specs; không import tay ở file khung.
    import subprocess
    import sys

    code = (
        "from vingo_agent.actions import registry; registry.load_specs(); "
        "import sys; print(sorted(m for m in sys.modules if m.startswith('vingo_agent.actions.specs.')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert "vingo_agent.actions.specs.admin_catalog" in out
