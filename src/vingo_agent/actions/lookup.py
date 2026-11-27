"""Tra UNKNOWN khi mở hội thoại, token hiện tại; không chạy job nền."""

import logging
from datetime import UTC, datetime, timedelta

from vingo_agent.actions import execution as runner
from vingo_agent.config import get_settings
from vingo_agent.observability.logging import log_event
from vingo_agent.observability.tracing import span


async def resolve(action_id, *, backend):
    with span("lookup", action_id=str(action_id)) as trace:
        return await _resolve(action_id, backend=backend, trace=trace)


async def _resolve(action_id, *, backend, trace):
    row = await runner.load(action_id)
    if row is None:
        return None
    trace.set_attribute("operation", row["operation"])
    state = row["execution_state"]
    trace.set_attribute("outcome", state)
    if state == "RUNNING" and row["started_at"] + timedelta(seconds=get_settings().lease_s) < datetime.now(UTC):
        # Lease đã hết: kết quả gửi trước khi process chết có thể đã ghi ở backend.
        await runner.save(action_id, row, ("UNKNOWN", "EXECUTION_UNKNOWN", {}), expected="RUNNING")
        # Cảnh báo EXECUTION UNKNOWN (mục 9) đọc từ log action.executed, kể cả UNKNOWN do hết lease.
        log_event(
            logging.getLogger(__name__),
            "action.executed",
            action_id=str(action_id),
            operation=row["operation"],
            outcome="UNKNOWN",
            error_code="EXECUTION_UNKNOWN",
        )
        state = "UNKNOWN"
    if state != "UNKNOWN":
        return state, row["error_code"], row["result"] or {}
    web_blocks = []
    outcome = await runner.execute(
        row["operation"],
        backend=backend,
        payload=row["payload"],
        idempotency_key=row["idempotency_key"],
        lookup_only=True,
        web_blocks=web_blocks,
    )
    trace.set_attribute("outcome", outcome[0])
    if outcome[0] == "UNKNOWN":
        return outcome
    return await runner.save(action_id, row, outcome, expected="UNKNOWN", backend=backend, web_blocks=web_blocks)
