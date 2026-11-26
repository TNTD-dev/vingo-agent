"""G2 kiểm input và G3 che bí mật trước mọi lưu trữ hoặc lời gọi model."""

import re
import unicodedata
from urllib.parse import parse_qs, urlsplit

REDACTED = "[đã ẩn]"


class InputRejected(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def check_input(text: str, *, max_chars: int) -> str:
    if len(text) > max_chars:
        raise InputRejected(413, "PAYLOAD_TOO_LARGE")
    if not text.strip() or any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        raise InputRejected(400, "VALIDATION_ERROR")
    return text.strip()


def _card(match):
    digits = re.sub(r"\D", "", match.group())
    values = [int(c) for c in digits[::-1]]
    total = sum((v * 2 - 9 if v * 2 > 9 else v * 2) if i % 2 else v for i, v in enumerate(values))
    return REDACTED if 13 <= len(digits) <= 19 and total % 10 == 0 else match.group()


def _url(match):
    keys = {k.lower() for k in parse_qs(urlsplit(match.group()).query)}
    signed = {"signature", "sig", "token", "x-amz-signature", "x-goog-signature"}
    return REDACTED if keys & signed else match.group()


def redact(text: str) -> str:
    text = re.sub(r"https?://[^\s<>]+", _url, text, flags=re.I)
    text = re.sub(r"\bBearer\s+[^\s,;]+", REDACTED, text, flags=re.I)
    text = re.sub(r"\beyJ[\w-]*\.[\w-]+\.[\w-]+", REDACTED, text)
    text = re.sub(r"((?:\bOTP\b|\bmã\b)[^\d\n]{0,30})\d{6}(?!\d)", lambda m: m[1] + REDACTED, text, flags=re.I)
    return re.sub(r"(?<!\w)(?:\d[ -]?){12,18}\d(?!\w)", _card, text)
