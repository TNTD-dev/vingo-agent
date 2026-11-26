"""Spec mở rộng theo nhóm nghiệp vụ; khung không biết tên operation cụ thể."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from vingo_agent.clients.backend import OPERATIONS
from vingo_agent.tools.base import ToolError


class LookupMode(StrEnum):
    NONE = "NONE"
    RETRY = "RETRY"
    READ = "READ"


def saved_request(payload: dict, buyer) -> dict:
    """Chỉ dựng request từ snapshot; buyer chỉ tồn tại trong request hiện tại."""
    return {"path": payload.get("path"), "body": payload.get("body")}


def no_result(data: dict) -> dict:
    return {}


@dataclass(frozen=True)
class OperationSpec:
    backend_operation: str
    tool_name: str
    role: str
    build_request: Callable = saved_request
    # Retry khi mở lại hội thoại không có buyer; nhóm nghiệp vụ có thể dựng request
    # tra bằng key mà không cần form (backend trả lại response đã lưu cho key đó).
    build_lookup_request: Callable[[dict], dict] | None = None
    project: Callable[[dict], dict] = no_result
    result_refs: dict[str, str] = field(default_factory=dict)
    safe_retry: bool = False
    lookup_mode: LookupMode = LookupMode.NONE
    # READ trả kết quả đã lọc hoặc None nếu chưa có bằng chứng thành công.
    lookup: Callable[..., Awaitable[dict | None]] | None = None
    check_before: Callable[..., Awaitable[bool]] | None = None
    needs_form: bool = False
    confirm_phrase: Callable[[dict], str | None] | None = None
    result_blocks: Callable[[dict], list] | None = None
    # Chạy sau commit, một lần bởi writer thắng CAS tới SUCCEEDED (kể cả lookup).
    # Hook nhận action_id/conversation_id/result/backend; không nhận buyer/token đã lưu.
    after_success: Callable[..., Awaitable[Any]] | None = None


_specs: dict[str, OperationSpec] = {}


def register(operation: str, spec: OperationSpec) -> None:
    backend = OPERATIONS.get(spec.backend_operation)
    if backend is None or not backend.write:
        raise ValueError("Operation phải có trong OPERATIONS write")
    if spec.lookup_mode == LookupMode.RETRY and not spec.safe_retry:
        raise ValueError("Retry cần safe_retry")
    if spec.lookup_mode == LookupMode.READ and spec.lookup is None:
        raise ValueError("READ cần hàm lookup")
    if operation in _specs:
        raise ValueError("Operation đã đăng ký")
    _specs[operation] = spec


_loaded = False


def load_specs() -> None:
    """Import mọi module trong `actions.specs` một lần; mỗi module tự đăng ký operation của nhóm."""
    global _loaded
    if _loaded:
        return
    import importlib
    import pkgutil

    from vingo_agent.actions import specs

    for module in pkgutil.iter_modules(specs.__path__):
        importlib.import_module(f"{specs.__name__}.{module.name}")
    _loaded = True


def get_spec(operation: str) -> OperationSpec:
    load_specs()
    try:
        return _specs[operation]
    except KeyError:
        raise ToolError("INVALID_ARGUMENT") from None
