from uuid import UUID

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from vingo_agent.actions.proposals import BACKEND_OPERATION, Operation
from vingo_agent.persistence.db import transaction
from vingo_agent.persistence.models import execution, pending_action, resource_ref
from vingo_agent.tools.base import ToolError

CATALOG_RETRY = frozenset(
    {
        Operation.PUBLISH_EVENT,
        Operation.BLOCK_SEATS,
        Operation.UNBLOCK_SEATS,
    }
)

CATALOG_RESULT_ID = {
    Operation.CREATE_VENUE: "venue_id",
    Operation.CREATE_EVENT: "event_id",
    Operation.CREATE_SHOWTIME: "showtime_id",
    Operation.PUBLISH_EVENT: "event_id",
}

CATALOG_OPERATIONS = frozenset(CATALOG_RESULT_ID) | CATALOG_RETRY


async def run(action_id, *, backend, buyer=None) -> tuple[str, str | None, dict]:
    """Thực thi sau khi luồng xác nhận đã giành quyền tạo execution RUNNING."""
    async with transaction() as connection:
        row = (
            await connection.execute(
                select(
                    pending_action.c.conversation_id,
                    pending_action.c.operation,
                    pending_action.c.payload,
                    execution.c.idempotency_key,
                    execution.c.state,
                    execution.c.error_code,
                    execution.c.result,
                )
                .select_from(pending_action.join(execution, pending_action.c.id == execution.c.action_id))
                .where(
                    pending_action.c.id == action_id,
                    pending_action.c.state == "CONFIRMED",
                    pending_action.c.role == "ADMIN",
                )
            )
        ).one_or_none()

    if row is None:
        raise ToolError("ACTION_NOT_PENDING")
    operation = Operation(row.operation)
    if operation not in CATALOG_OPERATIONS:
        raise NotImplementedError
    if row.state != "RUNNING":
        return row.state, row.error_code, row.result or {}

    status, code, ids = await execute_admin_catalog(
        operation, backend=backend, payload=row.payload, idempotency_key=row.idempotency_key
    )

    async with transaction() as connection:
        saved = await connection.scalar(
            update(execution)
            .where(execution.c.action_id == action_id, execution.c.state == "RUNNING")
            .values(state=status, error_code=code, result=ids, finished_at=func.now())
            .returning(execution.c.action_id)
        )
        if saved is None:
            current = (
                await connection.execute(
                    select(execution.c.state, execution.c.error_code, execution.c.result).where(
                        execution.c.action_id == action_id
                    )
                )
            ).one()
            return current.state, current.error_code, current.result or {}
        for field, value in ids.items():
            await connection.execute(
                insert(resource_ref)
                .values(
                    conversation_id=row.conversation_id,
                    resource_type=field.removesuffix("_id").upper(),
                    resource_id=UUID(value),
                )
                .on_conflict_do_nothing()
            )
    return status, code, ids


def _json_object(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def execute_admin_catalog(
    operation: Operation,
    *,
    backend,
    payload: dict,
    idempotency_key: UUID,
) -> tuple[str, str | None, dict]:
    operation_id = BACKEND_OPERATION[operation]

    async def send() -> httpx.Response | None:
        try:
            return await backend.write(
                operation_id,
                path=payload["path"],
                body=payload["body"],
                idempotency_key=idempotency_key,
            )
        except httpx.TransportError:
            return None

    response = await send()

    if response is not None and response.is_client_error:
        code = _json_object(response).get("code")
        return "FAILED", code if isinstance(code, str) else "VALIDATION_ERROR", {}

    if response is None or response.is_server_error:
        if operation in CATALOG_RETRY:
            response = await send()

    if response is None or not response.is_success:
        return "UNKNOWN", "EXECUTION_UNKNOWN", {}

    id_field = CATALOG_RESULT_ID.get(operation)
    if id_field is None:
        return "SUCCEEDED", None, {}

    raw_id = _json_object(response).get("id")
    if not isinstance(raw_id, str):
        return "SUCCEEDED", None, {}
    try:
        resource_id = UUID(raw_id)
    except ValueError:
        return "SUCCEEDED", None, {}
    return "SUCCEEDED", None, {id_field: str(resource_id)}
