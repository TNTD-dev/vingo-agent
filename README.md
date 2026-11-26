# vingo-agent

Trợ lý chat mua vé (Buyer) và quản trị (Admin) của Vingo. Thiết kế: [docs/VINGO_AGENT_DESIGN_V2.2.md](docs/VINGO_AGENT_DESIGN_V2.2.md).

Dịch vụ nội bộ: chỉ module `chat` của vingo-backend gọi vào (`/internal/v1/*`, SSE). Agent gọi backend qua REST `/api/v1`
bằng session token của user, lưu hội thoại và đề xuất ở schema Postgres `agent`. Model không bao giờ ghi nghiệp vụ:
tool ghi chỉ tạo đề xuất, user xác nhận (nút hoặc nhắn đồng ý) thì code thực thi.

**Trạng thái: khung.** Route trả 501, tool/guardrail/actions còn `NotImplementedError`. Đã có thật: config, schema
mã lỗi / SSE / block / API, migration 7 bảng, registry tool theo role, kiểm auth G1.

## Chạy local

```bash
uv sync
cp .env.example .env          # điền key dịch vụ, AGENT_MODEL, AGENT_FALLBACK_MODEL và key provider
uv run alembic upgrade head   # cần Postgres 18 (uuidv7) của backend, role vingo_agent
uv run uvicorn vingo_agent.api.app:app --host 127.0.0.1 --port 8000
```

## Kiểm tra

```bash
uv run ruff check .
uv run pytest tests/unit
uv run pytest tests/integration                 # cần Docker
uv run python scripts/sync_contract.py          # operationId khớp ../vingo-backend/contracts/vingo-openapi.yaml
```

## Cấu trúc

| Thư mục | Trách nhiệm |
|---|---|
| `api/` | Route `/internal/v1`, auth từ backend, sự kiện SSE |
| `agents/` | Registry tool theo role, dựng PydanticAI Agent (model chính + dự phòng), chạy turn |
| `prompts/` | System prompt Buyer/Admin (copy từ thiết kế mục 3) |
| `tools/` | 32 tool: đọc, đề xuất, quyết định; lọc kết quả; dựng block |
| `guardrails/` | G2–G10: input, tool, kết quả, câu trả lời, giới hạn |
| `context/` | Dựng context, compact |
| `actions/` | Đề xuất, xác nhận, thực thi, tra khi không rõ kết quả |
| `clients/` | Gọi backend (danh sách operation cố định) |
| `persistence/` | Bảng schema `agent`, truy vấn |
| `schemas/` | Mã lỗi, block, request/response API |
| `observability/` | Log JSON (allowlist field), trace |
| `evals/` | Seed + 73 case từ thiết kế mục 8 |
