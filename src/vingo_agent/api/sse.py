"""Sự kiện SSE (V2.2 mục 3.3). Mỗi turn có đúng một sự kiện cuối: turn.completed hoặc turn.failed."""

import json
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class EventType(StrEnum):
    TURN_STARTED = "turn.started"
    PROGRESS = "progress"
    TEXT_DELTA = "text.delta"
    BLOCK = "block"
    NOTICE = "notice"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"


class ProgressStep(StrEnum):
    SEARCHING = "SEARCHING"
    READING = "READING"
    PROPOSING = "PROPOSING"
    EXECUTING = "EXECUTING"


class NoticeLevel(StrEnum):
    INFO = "INFO"
    WARN = "WARN"


TERMINAL_EVENTS = frozenset({EventType.TURN_COMPLETED, EventType.TURN_FAILED})


class StreamEvent(BaseModel):
    turn_id: UUID
    seq: int
    type: EventType
    payload: dict[str, Any] = {}

    def encode(self) -> str:
        """Định dạng một sự kiện SSE: `event:` + `data:` JSON, kết thúc bằng dòng trống."""
        data = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
        return f"event: {self.type.value}\ndata: {data}\n\n"
