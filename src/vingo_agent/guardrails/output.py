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

USER_AMOUNT = re.compile(
    r"(?<!\w)(?<!\d[.,])(?:"
    r"(?P<scaled>\d+(?:[.,]\d+)?)\s*(?P<unit>nghìn|ngàn|k|triệu|tr)\b"
    r"|(?P<dong>\d+(?:[., ]\d{3})*)\s*(?:(?:VND|VNĐ|đồng|đ)\b|₫)"
    r"|(?P<grouped>\d{1,3}(?:[., ]\d{3})+)\b(?![.,]\d))",
    re.IGNORECASE,
)


def _user_amounts(text: str) -> set[int]:
    """Đọc tiền VND trong tin nhắn đã che, giữ nguyên độ chính xác đến một đồng."""
    amounts = set()
    for match in USER_AMOUNT.finditer(text):
        if match["scaled"] is None:
            amounts.add(int(re.sub(r"\D", "", match["dong"] or match["grouped"])))
            continue
        places = 6 if match["unit"].lower() in {"triệu", "tr"} else 3
        whole, _, fraction = match["scaled"].replace(",", ".").partition(".")
        fraction = fraction.rstrip("0")
        if len(fraction) <= places:
            amounts.add(int(whole + fraction.ljust(places, "0")))
    return amounts


@dataclass(frozen=True)
class Evidence:
    succeeded_operations: frozenset[str]
    amounts: frozenset[int]
    user_text: str = ""


def check_reply(text: str, evidence: Evidence) -> list[str]:
    warnings = []
    if any(
        claim in text.lower() and not any(op.startswith(operation) for op in evidence.succeeded_operations)
        for claim, operation in WRITE_CLAIMS.items()
    ):
        warnings.append("Thao tác chưa được thực hiện. Vui lòng xem thẻ bên dưới.")
    amounts = re.findall(r"(?<!\w)(\d+(?:[., ]\d{3})*)\s*(?:VND|VNĐ|đồng|₫|đ\b)", text, re.I)
    if amounts:
        verified = {price * tickets for price in evidence.amounts for tickets in range(1, 21)}
        verified.update(_user_amounts(evidence.user_text))
        if any(int(re.sub(r"\D", "", amount)) not in verified for amount in amounts):
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
