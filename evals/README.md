# Eval V2.3 — KAN-196

```sh
uv run python -m evals.runner --mode pytest
uv run python -m evals.coverage
uv run pytest tests/unit
uv run pytest tests/integration
```

Pytest dùng FunctionModel/respx và Postgres Testcontainers khi cần, không dùng key model hay backend đang chạy.
Docker cần sẵn cho integration. Test tương thích SQL fixture đọc migration từ repo backend bên cạnh; CI không có repo đó sẽ skip riêng test này. `--mode pytest` cũng chạy node pytest gắn với case Real.
`Backend` chỉ ghi `LINKED`; runner không chạy test của repo backend.

Real/E2E cần backend **test riêng**, Flyway mới nhất, schema agent đã `alembic upgrade head`,
cổng VNPay sandbox và email sink. Không trỏ backend đang chạy bằng DB dev `vingo` vào eval.
SQL seed được chọn vì cần dữ liệu metric chính xác, alias cố định và snapshot side effect độc lập với model.
`fixtures/__init__.py:ALIASES` là bảng alias → UUID thật (UUID5 ổn định); không đưa UUID fixture trực tiếp vào message.
Agent vẫn lấy Ref qua tool đọc; UUID chỉ render vào expected, selection và hook kiểm thử.

Env cho Real (export trong shell; provider phải thấy key trong process):

- `BACKEND_URL`: backend test `/api/v1`.
- `EVAL_BACKEND_DATABASE_URL`: DSN Postgres của chính backend test đó.
- `DATABASE_URL`: DSN agent test; DB backend/agent phải có tên chứa token `test` hoặc `eval`.
- `EVAL_ALLOW_RESET=1`: cho phép reset dữ liệu; runner vẫn chặn host/DB production, URL không có dấu hiệu test/local.
- `AGENT_SERVICE_KEY`, `AGENT_TO_BACKEND_KEY`, `AGENT_MODEL`, key provider tương ứng (`OPENAI_API_KEY`, …).
- `EVAL_JUDGE_MODEL`: model riêng cho Judge, cùng key provider tương ứng.

Reset xóa dữ liệu public của DB backend test (giữ Flyway history) và toàn bộ hội thoại schema agent.
Runner chạy tuần tự; không chạy hai process trên cùng DB fixture. DB user eval phải chỉ có quyền trên DB test riêng.

```sh
uv run python -m evals.runner --ids B01,B02,X02 --mode real --repeats 3 --report evals/reports/run.json
uv run python -m evals.runner --group buyer --mode all
uv run python -m evals.judge --calibrate evals/judge_calibration.yaml
```

T0 được chụp một lần khi runner bắt đầu. `case_io.time_variables` render `{{weekend_start}}`,
`{{weekend_end}}`, `{{st_01_starts_at}}`, `{{month_start}}`, `{{month_end}}`, `{{month_label}}` cho cả
fixture, message và expected. Agent/backend dùng giờ thật. Hook `expire_action`/`expire_execution` chỉ lùi timestamp DB agent.
Các hook khác: `price_ga`, `event_title`, `expire_session`, `history`, `summary`, `context_ratio`;
`mock_tool_call` dùng FunctionModel cho output/tool call ép buộc.
Riêng bước phiên hết hạn F06 đi qua backend chat proxy bằng cookie thật để kiểm 401 trước khi tới agent. Case fault/concurrency dùng node pytest thật.

Điền 12 mẫu `human_scores` trong calibration bằng người chấm tay (intent/context/clarity 1–3), rồi so Judge.
Không bịa điểm chuẩn. Judge dùng trong Real trả ERROR nếu chưa có 10–15 mẫu chấm tay; calibration CLI vẫn cho so mẫu đã điền. INCONCLUSIVE hoặc Judge lỗi là ERROR, không nâng code FAIL lên PASS.

Report JSON và terminal ghi status theo nhóm, metric với tử/mẫu số, gate, version Git/prompt/schema/Judge/fixture,
T0, latency ở ASGI send và usage provider nếu có. Cost thiếu bảng giá là `unavailable`.
Không lưu text, args, raw response, form, session hay raw trace; trace report chỉ có method/path/status, tool name,
log allowlist và delta. Report nằm trong `evals/reports/` (gitignore).
`ERROR`/`BLOCKED` không tính PASS; tool stub/chưa đăng ký/chưa phát hành ghi rõ lý do.
Exit 1 khi FAIL/ERROR/BLOCKED hoặc không có case; coverage exit 1 khi thiếu ánh xạ/assertion/node hợp lệ.

Coverage hiện kiểm **ánh xạ** (theo role) và node tồn tại, không thay thế kết quả chạy hay gate release.
Tool chưa triển khai có cả trong bảng coverage nhưng được loại khỏi mẫu số và ghi BLOCKED.
Giữ một tập câu thực tế chưa dùng chỉnh prompt/Judge cho lần chạy trước release, không đưa PII vào repo.

Thay đổi thiết kế đã chốt: dùng T0/giờ thật thay đồng hồ giả. Bản thiết kế trong repo backend không sửa theo phạm vi
backend chỉ đọc. Không bổ sung tool nghiệp vụ hay dependency.
