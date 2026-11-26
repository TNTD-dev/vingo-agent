"""Chạy một turn (V2.2 mục 2): acquire lease → kiểm input → dựng context → agent.iter() stream SSE
→ kiểm câu trả lời → lưu lịch sử → release. Một sự kiện cuối duy nhất mỗi turn."""


async def run_turn(*, conversation_id, request, principal):
    """Async generator các StreamEvent."""
    raise NotImplementedError
