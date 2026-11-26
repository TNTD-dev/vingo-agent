"""G9 kiểm câu trả lời sau khi model stream xong (V2.3 mục 3.3). Không thu hồi chữ, chỉ phát notice."""

from dataclasses import dataclass

WRITE_CLAIMS = (
    "đã giữ",
    "đã thanh toán",
    "đã hủy",
    "đã huỷ",
    "đã gửi",
    "đã tạo",
    "đã công bố",
    "đã khóa",
    "đã khoá",
)


@dataclass(frozen=True)
class Evidence:
    succeeded_operations: frozenset[str]
    amounts: frozenset[int]


def check_reply(text: str, evidence: Evidence) -> list[str]:
    """Trả danh sách notice WARN: khẳng định thao tác ghi không có bằng chứng; số tiền không có trong evidence."""
    raise NotImplementedError
