"""Client gọi vingo-backend /api/v1 (V2.2 mục 5.1, G6).

- Mỗi request: Authorization: Bearer <session token user> + X-Vingo-Agent-Key.
- Chỉ gọi operation có trong OPERATIONS (path cố định), host lấy từ config, không theo redirect.
- Đọc: thử lại tối đa 2 lần với 429/5xx, tôn trọng Retry-After. Ghi: không tự thử lại (xem actions/lookup.py).
- Không log body, token.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from vingo_agent.config import Settings


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
        raise NotImplementedError

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
