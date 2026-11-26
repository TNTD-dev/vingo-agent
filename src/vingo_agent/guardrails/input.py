"""G2 kiểm input + G3 che dữ liệu nhạy cảm (V2.3 mục 5.3). Chạy trước khi lưu và trước khi gửi model."""

REDACTED = "[đã ẩn]"


class InputRejected(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def check_input(text: str, *, max_chars: int) -> str:
    """Rỗng → 400, quá dài → 413, ký tự điều khiển (trừ \\n, \\t) → 400. Trả text đã chuẩn hóa."""
    raise NotImplementedError


def redact(text: str) -> str:
    """Thay JWT/bearer token, OTP (6 số đi kèm "OTP"/"mã"), số thẻ (Luhn), URL có chữ ký bằng REDACTED."""
    raise NotImplementedError
