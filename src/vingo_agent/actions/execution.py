"""Thực thi một đề xuất đã CONFIRMED (V2.2 mục 7, bước 3–7).

2xx → SUCCEEDED · 4xx → FAILED + mã backend · timeout/5xx → lookup.resolve() → SUCCEEDED/FAILED/UNKNOWN.
Sau đó: ghi MESSAGE SYSTEM_EVENT, ghi Ref mới, phát ActionResult + block kết quả.
Hold thành công → tự tạo đề xuất CREATE_PAYMENT (thẻ có form); total khác snapshot → notice WARN.
Payment thành công → block PaymentLink (URL không vào model).
"""


async def run(action_id, *, backend, buyer=None):
    raise NotImplementedError
