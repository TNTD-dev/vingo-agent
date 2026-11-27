"""G9 đính chính câu trả lời bằng notice sau khi stream."""

import re
from dataclasses import dataclass

WRITE_CLAIMS = {
    "đã giữ": "HOLD_SEATS",
    "đã thanh toán": "CREATE_PAYMENT",
    "đã hủy": "CANCEL_ORDER",
    "đã huỷ": "CANCEL_ORDER",
    "đã gửi": "RESEND",
    "đã tạo": "CREATE",
    "đã công bố": "PUBLISH_EVENT",
    "đã khóa": "BLOCK_SEATS",
    "đã khoá": "BLOCK_SEATS",
}


@dataclass(frozen=True)
class Evidence:
    succeeded_operations: frozenset[str]
    amounts: frozenset[int]


def check_reply(text: str, evidence: Evidence) -> list[str]:
    warnings = []
    if any(
        claim in text.lower() and not any(op.startswith(operation) for op in evidence.succeeded_operations)
        for claim, operation in WRITE_CLAIMS.items()
    ):
        warnings.append("Thao tác chưa được thực hiện. Vui lòng xem thẻ bên dưới.")
    amounts = re.findall(r"(?<!\w)(\d+(?:[., ]\d{3})*)\s*(?:VND|VNĐ|đồng|₫|đ\b)", text, re.I)
    if any(int(re.sub(r"\D", "", amount)) not in evidence.amounts for amount in amounts):
        warnings.append("Giá chính xác xem trên thẻ.")
    return warnings


def revenue_amounts(value) -> set[int]:
    """Bằng chứng G9 chỉ từ doanh thu đã lọc; count/percent không phải số tiền."""
    if type(value) is not dict:
        return set()
    meta = value.get("meta")
    if type(meta) is not dict:
        return set()
    operation = meta.get("operation")
    data = value.get("data")
    if value.get("ok") is not True or operation not in {"getAdminDashboard", "queryMetric"} or type(data) is not dict:
        return set()
    values = []
    if operation == "queryMetric" and data.get("metric") == "revenue" and data.get("unit") == "VND":
        values.append(data.get("value"))
    elif operation == "getAdminDashboard":
        values.extend(data.get(period, {}).get("revenue") for period in ("current", "previous"))
        values.extend(point.get("value") for point in data.get("revenue", {}).get("series", []))
    return {amount for amount in values if type(amount) is int and amount >= 0}
