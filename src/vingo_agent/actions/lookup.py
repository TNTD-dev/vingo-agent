"""Tra UNKNOWN khi mở hội thoại, token hiện tại; không chạy job nền."""

from datetime import UTC, datetime, timedelta

from vingo_agent.actions import execution as runner
from vingo_agent.config import get_settings


async def resolve(action_id, *, backend):
    row = await runner.load(action_id)
    if row is None:
        return None
    state = row["execution_state"]
    if state == "RUNNING" and row["started_at"] + timedelta(seconds=get_settings().lease_s) < datetime.now(UTC):
        # Lease đã hết: kết quả gửi trước khi process chết có thể đã ghi ở backend.
        await runner.save(action_id, row, ("UNKNOWN", "EXECUTION_UNKNOWN", {}), expected="RUNNING")
        state = "UNKNOWN"
    if state != "UNKNOWN":
        return state, row["error_code"], row["result"] or {}
    outcome = await runner.execute(
        row["operation"],
        backend=backend,
        payload=row["payload"],
        idempotency_key=row["idempotency_key"],
        lookup_only=True,
    )
    if outcome[0] == "UNKNOWN":
        return outcome
    return await runner.save(action_id, row, outcome, expected="UNKNOWN", backend=backend)
