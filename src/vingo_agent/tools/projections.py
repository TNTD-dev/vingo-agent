"""G7: lọc theo khai báo operation, trước Ref/block/envelope.

Khai báo SPECS[operation] = {"items": FieldSpec(list, item=FieldSpec(dict,
fields={"event_id": FieldSpec(str), "status": FieldSpec(str, enum=("DRAFT",))}))}.
Dùng match="event_id" và project(..., requested={"event_id": id}) để kiểm
ID đã hỏi. Field tùy chọn dùng required=False. Không khai báo spec nghiệp vụ ở đây.
"""

from typing import Any

from vingo_agent.guardrails.result import FieldSpec, bound_result, validate_object
from vingo_agent.tools.base import ToolError

FORBIDDEN_FIELDS = frozenset(
    {"qr_token", "payment_url", "email", "phone", "buyer_name", "buyer_email", "buyer_phone", "svg"}
)
SEATMAP_MAX_SEATS = 60
SPECS: dict[str, dict[str, FieldSpec]] = {}


def project(operation: str, raw: dict[str, Any], *, role: str, requested=None) -> tuple[dict[str, Any], bool]:
    """Dựng object mới, kiểm schema/ID rồi cắt list theo giới hạn byte."""
    if operation not in SPECS:
        raise ToolError("INVALID_TOOL_RESULT")
    return bound_result(validate_object(raw, SPECS[operation], requested or {}))
