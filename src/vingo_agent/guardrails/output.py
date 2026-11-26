"""G9 đính chính câu trả lời bằng notice sau khi stream."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

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


LINK_WARNING = "Liên kết trong câu trả lời không phải của Vingo. Đừng bấm hoặc nhập thông tin thanh toán."
# URL đầy đủ, www hoặc tên miền trần có path; không coi dấu chấm trong văn xuôi là link.
# Tên miền trần không có path chỉ bắt khi TLD nằm trong danh sách hay dùng để lừa ("vingo-pay.xyz"),
# tránh báo nhầm "v2.3", "Node.js", giá "500.000đ".
COMMON_TLDS = (
    "com|vn|net|org|info|xyz|top|site|online|shop|store|app|link|click|io|co|me|biz|cc|tk|live|pro|asia|website"
)
LINK_PATTERN = re.compile(
    r"https?://[^\s<>\"']+|www\.[^\s<>\"']+|"
    r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}(?::[0-9]{1,5})?/[^\s<>\"']*|"
    rf"(?<![\w.@-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:{COMMON_TLDS})(?![\w.-]*[\w-])",
    re.I,
)


def has_unverified_link(text: str, allowed_hosts: frozenset[str]) -> bool:
    """So host chính xác, không chấp nhận hậu tố hay userinfo giả hostname."""
    if re.search(r"!\[[^\]\r\n]*\]", text):
        return True
    allowed = {host.lower().rstrip(".").encode("idna").decode("ascii") for host in allowed_hosts}
    for match in LINK_PATTERN.finditer(text):
        url = match.group().rstrip(".,;:!?)]}")
        if "\\" in url:
            return True
        try:
            host = urlsplit(url if re.match(r"https?://", url, re.I) else "https://" + url).hostname
            if not host or host.lower().rstrip(".").encode("idna").decode("ascii") not in allowed:
                return True
        except (ValueError, UnicodeError):
            return True
    return False


def check_reply(text: str, evidence: Evidence, *, allowed_link_hosts: frozenset[str] = frozenset()) -> list[str]:
    warnings = []
    if any(
        claim in text.lower() and not any(op.startswith(operation) for op in evidence.succeeded_operations)
        for claim, operation in WRITE_CLAIMS.items()
    ):
        warnings.append("Thao tác chưa được thực hiện. Vui lòng xem thẻ bên dưới.")
    amounts = re.findall(r"(?<!\w)(\d+(?:[., ]\d{3})*)\s*(?:VND|VNĐ|đồng|₫|đ\b)", text, re.I)
    if any(int(re.sub(r"\D", "", amount)) not in evidence.amounts for amount in amounts):
        warnings.append("Giá chính xác xem trên thẻ.")
    if has_unverified_link(text, allowed_link_hosts):
        warnings.append(LINK_WARNING)
    return warnings
