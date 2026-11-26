"""Log JSON (V2.2 mục 9, G11).

Chỉ ghi các field trong ALLOWED_FIELDS. Không log nội dung tin nhắn, tham số tool, body, token, form,
payment URL, QR.
"""

import json
import logging
import sys

from vingo_agent.config import get_settings

ALLOWED_FIELDS = frozenset(
    {
        "event",
        "request_id",
        "turn_id",
        "action_id",
        "role",
        "operation",
        "outcome",
        "duration_ms",
        "error_code",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", {}) or {}
        entry.update({k: v for k, v in fields.items() if k in ALLOWED_FIELDS})
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(get_settings().log_level)


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """Ghi một sự kiện; field ngoài ALLOWED_FIELDS bị bỏ."""
    logger.info(event, extra={"fields": {"event": event, **fields}})
