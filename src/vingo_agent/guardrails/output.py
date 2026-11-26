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
