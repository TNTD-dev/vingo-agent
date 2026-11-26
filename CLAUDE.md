# CLAUDE.md — vingo-agent

Thiết kế `docs/VINGO_AGENT_DESIGN_V2.2.md` là nguồn sự thật. Đọc đúng mục trước khi sửa phần tương ứng:

| Sửa | Đọc mục |
|---|---|
| Tool, tham số, output cho model | 4 |
| Xác nhận, thực thi, tra khi không rõ kết quả | 7, 4.5 |
| Guardrail, quyền, auth | 5 |
| Context, compact, bảng `agent` | 6 |
| SSE, block, kiểm câu trả lời | 3.3 |
| Model, fallback, giới hạn turn | 2 |
| Eval, test case | 8 |

Code lệch thiết kế: dừng lại và hỏi, rồi cập nhật thiết kế cùng lúc với code (cả bản ở `../vingo-backend/docs/agent/`).
API backend: `../vingo-backend/contracts/vingo-openapi.yaml`. Contract đổi thì chạy `scripts/sync_contract.py`.

## Xong việc khi

```bash
uv run ruff format . && uv run ruff check .
uv run pytest tests/unit
uv run pytest tests/integration   # khi đụng persistence/ hoặc migrations/
```

Cả ba xanh mới báo xong. Test đỏ thì sửa code, giữ nguyên test.

## Bất biến an toàn

Mỗi điều dưới đây có test trong `evals/cases/safety.yaml`. Giữ đúng từng điều; điều nào không giữ được thì hỏi trước khi code.

- **Model chỉ đề xuất.** Ghi nghiệp vụ chỉ xảy ra trong `actions/execution.py`, sau khi user xác nhận (nút, hoặc `confirm_pending_action()` đủ 3 điều kiện mục 4.5), chạy đúng payload đã lưu, không gọi model.
- **Backend qua một cửa.** Mọi lời gọi đi qua `clients/backend.py` với operation trong `OPERATIONS`. Thêm endpoint = thêm vào `OPERATIONS` + chạy `sync_contract.py`.
- **ID phải có nguồn.** Mọi ID trong tham số tool qua `require_ref`. Tham số tool chỉ chứa ID và lựa chọn của user; `user_id`, `role`, token, URL, giá do code thêm.
- **Kết quả đã lọc.** Dữ liệu backend tới model chỉ qua `tools/projections.py`. `qr_token`, `payment_url`, email, SĐT chỉ đi tới web qua block.
- **Block do code dựng** trong `tools/blocks.py`; model chỉ viết text.
- **Log theo allowlist.** Ghi log bằng `log_event`; nội dung tin nhắn, tham số tool, body, token, form người mua nằm ngoài log và trace.
- **Không giữ bí mật.** Session token chỉ sống trong request; form người mua đi thẳng lên backend.
- **Transaction ngắn.** Mở và đóng transaction DB trước mỗi lời gọi backend/LLM. Chuyển trạng thái bằng CAS: `UPDATE ... WHERE state = :expected AND version = :v`.

## Quy ước

- **Thêm hoặc sửa tool:** cập nhật thiết kế mục 4 → hàm trong `tools/` (docstring là mô tả cho model, kết thúc `→ operationId`) → `agents/registry.py` → projection → block → số lượng trong `tests/unit/test_registry.py`.
- **Đổi schema DB:** tạo revision Alembic mới với DDL viết cứng; revision đã commit giữ nguyên; `persistence/models.py` khớp DDL.
- **Prompt** ở `src/vingo_agent/prompts/`, là bản sao mục 3.1, 3.2: sửa ở cả hai nơi.
- **Thư viện mới:** hỏi trước khi thêm vào `pyproject.toml`.
- Tên code tiếng Anh; comment, docstring, commit message tiếng Việt.
- Tên test mô tả hành vi: `test_confirm_returnsActionNotPending_whenVersionStale`.
- Thời gian dùng `datetime` có timezone; tiền là `int` VND.
