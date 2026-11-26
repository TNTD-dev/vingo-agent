"""Đếm nội dung gửi model và ước lượng token bằng số đo usage gần nhất."""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)


def text_chars(messages: Sequence[ModelMessage] | str) -> int:
    """Chỉ đếm nội dung text/JSON, không tính timestamp, ID, metadata hoặc repr của object."""
    if isinstance(messages, str):
        return len(messages)
    total = 0
    for message in messages:
        if isinstance(message, ModelRequest):
            total += len(message.instructions or "")
        for part in message.parts:
            if isinstance(part, (SystemPromptPart, TextPart)):
                total += len(part.content)
            elif isinstance(part, UserPromptPart):
                content = part.content
                total += (
                    len(content)
                    if isinstance(content, str)
                    else sum(len(item) for item in content if isinstance(item, str))
                )
            elif isinstance(part, ToolCallPart):
                total += len(part.tool_name) + len(part.args_as_json_str())
            elif isinstance(part, ToolReturnPart):
                total += len(part.model_response_str())
            elif isinstance(part, RetryPromptPart):
                total += len(part.model_response())
    return total


@dataclass
class TokenEstimator:
    """Anchor chỉ thuộc request trong turn hiện tại; ratio có thể tái sử dụng sau compact/turn mới."""

    default_chars_per_token: float = 2.5
    anchor_tokens: int | None = field(default=None, init=False)
    anchor_chars: int | None = field(default=None, init=False)
    ratio: float = field(init=False)

    def __post_init__(self):
        if not math.isfinite(self.default_chars_per_token) or self.default_chars_per_token <= 0:
            raise ValueError("default_chars_per_token phải lớn hơn 0 và hữu hạn")
        self.ratio = self.default_chars_per_token

    def observe(self, input_tokens: int | None, chars: int | None, schema_chars: int = 0) -> bool:
        """Provider thiếu usage hoặc số đo rỗng: giữ nguyên số đo hợp lệ gần nhất.

        `input_tokens` của provider đã gồm schema tool, nên tỉ lệ tính trên `chars + schema_chars`;
        anchor_chars giữ phần không có schema để khớp với `chars` truyền vào `estimate`.
        """
        if input_tokens is None or input_tokens <= 0 or chars is None or chars <= 0:
            return False
        self.anchor_tokens, self.anchor_chars = input_tokens, chars
        self.ratio = (chars + schema_chars) / input_tokens
        return True

    def reset_anchor(self) -> None:
        """History đã đổi: chỉ giữ ratio, không mang anchor sang request sau compact."""
        self.anchor_tokens = self.anchor_chars = None

    def estimate(self, chars: int, schema_chars: int = 0) -> int:
        if self.anchor_tokens is not None and self.anchor_chars is not None:
            return max(0, math.ceil(self.anchor_tokens + (chars - self.anchor_chars) / self.ratio))
        return max(0, math.ceil((chars + schema_chars) / self.ratio))
