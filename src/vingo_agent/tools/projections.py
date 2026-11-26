"""G7: lọc kết quả backend về đúng field cho phép (V2.3 bảng mục 4.2, 4.3).

Thiếu field bắt buộc hoặc enum lạ → ToolError(INVALID_TOOL_RESULT). Field lạ bị bỏ.
Không bao giờ để lọt: qr_token, payment_url, email, phone, buyer_*.
"""

from typing import Any

FORBIDDEN_FIELDS = frozenset({"qr_token", "payment_url", "email", "phone", "buyer_name", "buyer_email", "buyer_phone"})
SEATMAP_MAX_SEATS = 60


def project(operation: str, raw: dict[str, Any], *, role: str) -> tuple[dict[str, Any], bool]:
    """Trả (data đã lọc, truncated). Mỗi operation một hàm con; khung chưa cài."""
    raise NotImplementedError
