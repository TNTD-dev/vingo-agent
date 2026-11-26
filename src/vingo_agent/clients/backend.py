"""Client gọi vingo-backend /api/v1 (V2.3 mục 5.1, G6).

- Mỗi request: Authorization: Bearer <session token user> + X-Vingo-Agent-Key.
- Chỉ gọi operation có trong OPERATIONS (path cố định), host lấy từ config, không theo redirect.
- Đọc: thử lại tối đa 2 lần với 429/5xx, tôn trọng Retry-After. Ghi: không tự thử lại (xem actions/lookup.py).
- Không log body, token.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from string import Formatter
from typing import Any
from uuid import UUID

import httpx

from vingo_agent.config import Settings
from vingo_agent.schemas.errors import BackendErrorCode


@dataclass(frozen=True)
class Operation:
    method: str
    path: str  # template, ví dụ "/showtimes/{showtime_id}/holds"
    write: bool = False
    idempotent_key: bool = False  # gửi Idempotency-Key


OPERATIONS: dict[str, Operation] = {
    # Buyer đọc
    "searchEvents": Operation("GET", "/events"),
    "getEvent": Operation("GET", "/events/{event_id}"),
    "listEventShowtimes": Operation("GET", "/events/{event_id}/showtimes"),
    "getAvailability": Operation("GET", "/showtimes/{showtime_id}/availability"),
    "getSeatmap": Operation("GET", "/showtimes/{showtime_id}/seatmap"),
    "getMe": Operation("GET", "/me"),
    "listMyOrders": Operation("GET", "/orders"),
    "getOrder": Operation("GET", "/orders/{order_id}"),
    "getPayment": Operation("GET", "/orders/{order_id}/payment"),
    "listMyTickets": Operation("GET", "/tickets"),
    "getTicket": Operation("GET", "/tickets/{ticket_id}"),
    # Admin đọc
    "getAdminDashboard": Operation("GET", "/admin/dashboard"),
    "queryMetric": Operation("GET", "/admin/reports/metrics"),
    "listVenues": Operation("GET", "/admin/venues"),
    "getAdminVenue": Operation("GET", "/admin/venues/{venue_id}"),
    "listSeatLayouts": Operation("GET", "/admin/venues/{venue_id}/seat-layouts"),
    "getSeatLayout": Operation("GET", "/admin/seat-layouts/{layout_id}"),
    "listAdminEvents": Operation("GET", "/admin/events"),
    "getAdminEvent": Operation("GET", "/admin/events/{event_id}"),
    "adminListTickets": Operation("GET", "/admin/tickets"),
    "adminListOrders": Operation("GET", "/admin/orders"),
    "adminGetOrder": Operation("GET", "/admin/orders/{order_id}"),
    "listShowtimeCheckins": Operation("GET", "/admin/showtimes/{showtime_id}/checkins"),
    "listAuditLogs": Operation("GET", "/admin/audit-logs"),
    # Ghi (chỉ actions/execution.py gọi)
    "holdSeats": Operation("POST", "/showtimes/{showtime_id}/holds", write=True, idempotent_key=True),
    "createPayment": Operation("POST", "/orders/{order_id}/payments", write=True, idempotent_key=True),
    "cancelOrder": Operation("POST", "/orders/{order_id}/cancel", write=True),
    "resendMyTicket": Operation("POST", "/tickets/{ticket_id}/resend", write=True),
    "createEvent": Operation("POST", "/admin/events", write=True),
    "createShowtime": Operation("POST", "/admin/events/{event_id}/showtimes", write=True),
    "publishEvent": Operation("POST", "/admin/events/{event_id}/publish", write=True),
    "blockSeats": Operation("POST", "/admin/showtimes/{showtime_id}/seats/block", write=True),
    "unblockSeats": Operation("POST", "/admin/showtimes/{showtime_id}/seats/unblock", write=True),
    "adminResendTicket": Operation("POST", "/admin/tickets/{ticket_id}/resend", write=True),
    # Admin
    "createVenue": Operation("POST", "/admin/venues", write=True),
}


class BackendClient:
    def __init__(self, settings: Settings, session_token: str, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._token = session_token
        self._http = http or httpx.AsyncClient(base_url=settings.backend_url, follow_redirects=False, timeout=10)

    async def read(
        self, operation: str, *, path: dict[str, Any] | None = None, query: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Gọi operation đọc; lỗi chuyển thành ToolError theo mã backend (ProblemDetail.code)."""
        from vingo_agent.tools.base import ToolError

        spec = OPERATIONS.get(operation)
        if spec is None or spec.write:
            raise ToolError("INVALID_ARGUMENT")
        fields = {name for _, name, _, _ in Formatter().parse(spec.path) if name}
        if set(path or {}) != fields:
            raise ToolError("INVALID_ARGUMENT")
        try:
            safe_path = {key: str(UUID(str(value))) for key, value in (path or {}).items()}
        except (ValueError, TypeError, AttributeError):
            raise ToolError("INVALID_ARGUMENT") from None
        url = self._settings.backend_url.rstrip("/") + spec.path.format(**safe_path)
        for attempt in range(3):
            try:
                response = await self._http.request(
                    spec.method,
                    url,
                    params=query,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "X-Vingo-Agent-Key": self._settings.agent_to_backend_key,
                    },
                    follow_redirects=False,
                )
            except httpx.RequestError:
                raise ToolError("BACKEND_UNAVAILABLE", retryable=True) from None
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < 2:
                delay = 0.25 * (2**attempt)
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(delay, (parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            pass
                await asyncio.sleep(delay)
                continue
            if not response.is_success:
                code = (
                    "BACKEND_UNAVAILABLE"
                    if response.status_code >= 500
                    else "RATE_LIMITED"
                    if response.status_code == 429
                    else "INVALID_TOOL_RESULT"
                )
                if not retryable:
                    try:
                        candidate = response.json().get("code")
                        if isinstance(candidate, str) and candidate in {item.value for item in BackendErrorCode}:
                            code = candidate
                    except (ValueError, AttributeError):
                        pass
                raise ToolError(code, retryable=retryable)
            try:
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError
                return data
            except ValueError:
                raise ToolError("INVALID_TOOL_RESULT") from None
        raise ToolError("BACKEND_UNAVAILABLE", retryable=True)

    async def write(
        self,
        operation: str,
        *,
        path: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: UUID | None = None,
    ) -> httpx.Response:
        """Gọi operation ghi đúng một lần; timeout/5xx để caller quyết định UNKNOWN."""
        raise NotImplementedError

    async def aclose(self) -> None:
        await self._http.aclose()
