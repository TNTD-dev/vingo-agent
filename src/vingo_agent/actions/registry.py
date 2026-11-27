"""Spec mở rộng theo nhóm nghiệp vụ; khung không biết tên operation cụ thể."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

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
    # READ trả kết quả đã lọc (có thể kèm block web), hoặc None khi chưa có bằng chứng.
    lookup: Callable[..., Awaitable[dict | tuple[dict, list] | None]] | None = None
    check_before: Callable[..., Awaitable[bool]] | None = None
    # Sau lỗi mạng: dict = đã áp dụng, True = còn snapshot cũ và được gửi lại, None = chưa rõ.
    reconcile_retry: Callable[..., Awaitable[dict | bool | None]] | None = None
    needs_form: bool = False
    confirm_phrase: Callable[[dict], str | None] | None = None
    result_blocks: Callable[[dict], list] | None = None
    # Chạy sau commit, một lần bởi writer thắng CAS tới SUCCEEDED (kể cả lookup).
    # Có thể nhận thêm context=(action, web_blocks) và trả (blocks, notices) cho turn.
    after_success: Callable[..., Awaitable[tuple[list, list[dict[str, str]]] | None]] | None = None
    # Chỉ chiếu response gốc thành block cho web. Không đưa các block này vào result.
    web_blocks: Callable[[dict], list] | None = None
    messages: dict[str, str] = field(default_factory=dict)


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
