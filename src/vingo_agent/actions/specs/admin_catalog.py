"""Spec Admin catalog KAN-201; giữ cách retry và chỉ chiếu ID hợp lệ."""

from uuid import UUID

from vingo_agent.actions.proposals import BACKEND_OPERATION, Operation
from vingo_agent.actions.registry import LookupMode, OperationSpec, register

CATALOG_RETRY = frozenset({Operation.PUBLISH_EVENT, Operation.BLOCK_SEATS, Operation.UNBLOCK_SEATS})
CATALOG_RESULT_ID = {
    Operation.CREATE_VENUE: "venue_id",
    Operation.CREATE_EVENT: "event_id",
    Operation.CREATE_SHOWTIME: "showtime_id",
    Operation.PUBLISH_EVENT: "event_id",
}
CATALOG_OPERATIONS = frozenset(CATALOG_RESULT_ID) | CATALOG_RETRY | {Operation.ADMIN_RESEND_TICKET}


def _project(field):
    def project(data):
        value = data.get("id")
        if field is None or not isinstance(value, str):
            return {}
        try:
            return {field: str(UUID(value))}
        except ValueError:
            return {}

    return project


for operation in CATALOG_OPERATIONS:
    field = CATALOG_RESULT_ID.get(operation)
    register(
        operation,
        OperationSpec(
            backend_operation=BACKEND_OPERATION[operation],
            tool_name="propose_admin_resend_ticket"
            if operation == Operation.ADMIN_RESEND_TICKET
            else "propose_" + operation.lower(),
            role="ADMIN",
            project=_project(field),
            result_refs={field: field.removesuffix("_id").upper()} if field else {},
            safe_retry=operation in CATALOG_RETRY,
            lookup_mode=LookupMode.RETRY if operation in CATALOG_RETRY else LookupMode.NONE,
            messages={
                "SUCCEEDED": "Đã xếp hàng yêu cầu gửi lại vé.",
                "UNKNOWN": "Vé có thể đã gửi. Vui lòng kiểm tra Admin Portal; không tự gửi lại.",
            }
            if operation == Operation.ADMIN_RESEND_TICKET
            else {},
        ),
    )
