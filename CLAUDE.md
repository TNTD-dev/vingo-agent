# CLAUDE.md — vingo-agent

Nguồn sự thật: `docs/VINGO_AGENT_DESIGN_V2.2.md`. Code và tài liệu mâu thuẫn thì hỏi lại, không tự đoán.
API backend: `../vingo-backend/contracts/vingo-openapi.yaml` (chạy `scripts/sync_contract.py` khi contract đổi).

## Lệnh

```bash
uv sync
uv run ruff check . && uv run ruff format .
uv run pytest tests/unit
uv run pytest tests/integration      # cần Docker
uv run alembic upgrade head
```

Chạy `ruff check` và `pytest` trước khi báo xong.

## Quy tắc bắt buộc

- **Model không bao giờ ghi nghiệp vụ.** Tool ghi chỉ tạo pending action. Thực thi chỉ ở `actions/` sau khi user xác nhận
  (nút hoặc `confirm_pending_action()` đủ điều kiện mục 4.5). Không gọi model trong lúc thực thi.
- Chỉ gọi backend qua `clients/backend.py` với operation trong `OPERATIONS`. Không thêm HTTP tùy ý, không SQL vào bảng nghiệp vụ.
- Mọi ID trong tham số tool phải qua `require_ref`. Schema tool không có `user_id`, `role`, token, URL, giá.
- Kết quả backend luôn qua `tools/projections.py` trước khi tới model. Không để lọt `qr_token`, `payment_url`, email, SĐT.
- Block do code dựng (`tools/blocks.py`), model không tạo block.
- Không log nội dung tin nhắn, tham số tool, body, token, form người mua, payment URL. Dùng `log_event` (allowlist field).
- Không lưu session token, không lưu form người mua.
- Transaction DB ngắn, không giữ qua lời gọi backend/LLM. CAS bằng `UPDATE ... WHERE state/version`.
- Migration đã merge không sửa; đổi schema thì tạo revision mới, DDL viết cứng trong migration.
- Thêm tool: cập nhật thiết kế mục 4, registry, projection, block, test số lượng tool trong `tests/unit/test_registry.py`.

## Code style

- Python 3.12, type hint đầy đủ, Pydantic v2 cho dữ liệu vào/ra. Input tool cấm field lạ.
- Tên code tiếng Anh; comment, docstring, commit message tiếng Việt. Docstring tool là mô tả cho model: ngắn, rõ, kết thúc bằng `→ operationId`.
- Tên test mô tả hành vi: `test_confirm_returnsActionNotPending_whenVersionStale`.
- Không thêm thư viện mới khi chưa hỏi.
