"""Bốn thao tác Buyer sau xác nhận (V2.3 mục 7)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field, TypeAdapter

from vingo_agent.actions.proposals import Operation
from vingo_agent.actions.registry import LookupMode, OperationSpec, register
from vingo_agent.schemas.blocks import PaymentLink
from vingo_agent.schemas.chat import Buyer
from vingo_agent.tools import projections
from vingo_agent.tools.blocks import build_block
from vingo_agent.tools.projections import OrderStatus, Price, Timestamp
from vingo_agent.tools.proposals import create_buyer_card, payment_display


class _OrderResult(BaseModel):
    order_id: UUID = Field(validation_alias="id")
    status: OrderStatus


class _HeldOrder(_OrderResult):
    total_amount: Price
    expires_at: Timestamp


class _PaymentResult(BaseModel):
    order_id: UUID
    status: Literal["OPEN", "SUCCEEDED", "FAILED", "EXPIRED"]
    expires_at: Timestamp


def _order_blocks(raw):
    data, _ = projections.project("getOrder", raw, role="BUYER")
    return [build_block("getOrder", data, role="BUYER")]


def _payment_blocks(raw):
    # Kiểm scheme nhưng giữ nguyên chuỗi URL đã ký của backend.
    TypeAdapter(AnyHttpUrl).validate_python(raw["payment_url"])
    return [PaymentLink.model_validate(raw)]


def _payment_request(payload, buyer):
    return {"path": payload["path"], "body": {**payload["body"], "buyer": Buyer.model_validate(buyer).model_dump()}}


async def _after_hold(*, conversation_id, result, context, **kwargs):
    action, (order,) = context
    total = result["total_amount"]
    expires = result["expires_at"]
    display = payment_display(order.code, order.event_title, total, expires)
    notices = []
    if total != action["display"]["total"]:
        amount = f"{total:,}".replace(",", ".")
        notices.append({"level": "WARN", "text": f"Tổng tiền đã đổi thành {amount} VND."})
    try:
        card = await create_buyer_card(
            conversation_id=conversation_id,
            turn_id=None,
            actor_id=action["actor_id"],
            operation=Operation.CREATE_PAYMENT,
            payload={"path": {"order_id": result["order_id"]}, "body": {}},
            display=display,
            expires_at=datetime.fromisoformat(expires),
        )
    except Exception:
        if not notices:
            raise
        # Vẫn báo tổng thật khi chỉ bước tạo thẻ sau commit gặp lỗi.
        notices.append(
            {"level": "WARN", "text": "Chưa tạo được thẻ thanh toán. Bạn có thể yêu cầu thanh toán đơn này."}
        )
        return [], notices
    return [card], notices


async def _lookup_cancel(*, backend, payload, idempotency_key):
    raw = await backend.read("getOrder", path=payload["path"])
    data, _ = projections.project(
        "getOrder", raw, role="BUYER", requested={"order_id": UUID(payload["path"]["order_id"])}
    )
    if data["status"] == "CANCELLED":
        return {k: data[k] for k in ("order_id", "status")}, [build_block("getOrder", data, role="BUYER")]
    return None


register(
    Operation.HOLD_SEATS,
    OperationSpec(
        "holdSeats",
        "propose_hold_seats",
        "BUYER",
        safe_retry=True,
        lookup_mode=LookupMode.RETRY,
        project=lambda raw: _HeldOrder.model_validate(raw).model_dump(mode="json"),
        result_refs={"order_id": "ORDER"},
        web_blocks=_order_blocks,
        after_success=_after_hold,
    ),
)
register(
    Operation.CREATE_PAYMENT,
    OperationSpec(
        "createPayment",
        "propose_create_payment",
        "BUYER",
        needs_form=True,
        build_request=_payment_request,
        safe_retry=True,
        lookup_mode=LookupMode.RETRY,
        project=lambda raw: _PaymentResult.model_validate(raw).model_dump(mode="json"),
        web_blocks=_payment_blocks,
    ),
)
register(
    Operation.CANCEL_ORDER,
    OperationSpec(
        "cancelOrder",
        "propose_cancel_order",
        "BUYER",
        lookup_mode=LookupMode.READ,
        lookup=_lookup_cancel,
        project=lambda raw: _OrderResult.model_validate(raw).model_dump(mode="json"),
        web_blocks=lambda raw: _order_blocks(raw) if "items" in raw else [],
    ),
)
register(
    Operation.RESEND_MY_TICKET,
    OperationSpec(
        "resendMyTicket",
        "propose_resend_my_ticket",
        "BUYER",
        messages={"SUCCEEDED": "Đã xếp hàng gửi lại vé.", "UNKNOWN": "Vé có thể đã gửi. Vui lòng kiểm tra email."},
    ),
)
