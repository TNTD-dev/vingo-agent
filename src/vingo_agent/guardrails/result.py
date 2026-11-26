"""G7: kiểm schema khai báo và giới hạn kích thước kết quả."""

import json
from dataclasses import dataclass
from typing import Any

from vingo_agent.tools.base import ToolError

MAX_RESULT_BYTES = 32_000


@dataclass(frozen=True)
class FieldSpec:
    """Khai báo kiểu, field bắt buộc, enum và tên ID cần khớp."""

    kind: type
    required: bool = True
    fields: dict[str, "FieldSpec"] | None = None
    item: "FieldSpec | None" = None
    enum: tuple[Any, ...] | None = None
    match: str | None = None


def forbidden(name):
    return name.lower() in {
        "qr_token",
        "payment_url",
        "email",
        "phone",
        "svg",
        "svg_content",
        "token",
    } or name.lower().startswith("buyer_")


def validate_object(raw, spec, requested):
    if type(raw) is not dict:
        raise ToolError("INVALID_TOOL_RESULT")
    result = {}
    for key, field in spec.items():
        if forbidden(key):
            continue
        if key not in raw:
            if field.required:
                raise ToolError("INVALID_TOOL_RESULT")
            continue
        result[key] = validate_value(raw[key], field, requested)
    return result


def validate_value(value, spec, requested):
    if type(value) is not spec.kind or (spec.enum is not None and value not in spec.enum):
        raise ToolError("INVALID_TOOL_RESULT")
    if spec.match is not None and (spec.match not in requested or str(value) != str(requested[spec.match])):
        raise ToolError("INVALID_TOOL_RESULT")
    if spec.kind is dict:
        return validate_object(value, spec.fields or {}, requested)
    if spec.kind is list:
        if spec.item is None:
            raise ToolError("INVALID_TOOL_RESULT")
        return [validate_value(item, spec.item, requested) for item in value]
    if spec.kind not in {str, int, float, bool, type(None)}:
        raise ToolError("INVALID_TOOL_RESULT")
    return value


def bound_result(data):
    def size():
        return len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode())

    def lists(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from lists(item)
        elif isinstance(value, list):
            yield value
            for item in value:
                yield from lists(item)

    truncated = False
    try:
        while size() > MAX_RESULT_BYTES:
            candidates = [value for value in lists(data) if value]
            if not candidates:
                raise ToolError("INVALID_TOOL_RESULT")
            max(candidates, key=lambda value: len(json.dumps(value))).pop()
            truncated = True
    except (ValueError, TypeError):
        raise ToolError("INVALID_TOOL_RESULT") from None
    return data, truncated
