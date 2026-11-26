from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import SystemPromptPart, UserPromptPart

from vingo_agent.context import builder, compaction
from vingo_agent.tools.base import ToolError


def test_history_dropsWholeTurn_whenToolResultMissing():
    rows = [
        {"turn_id": 1, "kind": "USER", "content": {"text": "old"}},
        {"turn_id": 1, "kind": "TOOL_CALL", "content": {"tool_call_id": "a", "tool_name": "read"}},
        {"turn_id": 2, "kind": "USER", "content": {"text": "recent"}},
    ]
    history = builder.history_messages(None, rows)
    assert len(history) == 1
    assert history[0].parts[0].content == "recent"


async def test_context_preservesOrder(monkeypatch):
    monkeypatch.setattr(builder, "load_context", AsyncMock(return_value=({"content": "summary"}, [])))
    result = await builder.build_context(
        1, system_prompt="system", now_vn="now", new_message="hello", tool_results=["tool"]
    )
    assert isinstance(result[0].parts[0], SystemPromptPart)
    assert result[0].parts[0].content == "system"
    assert "summary" in result[1].parts[0].content
    assert "now_vn" in result[2].parts[0].content
    assert isinstance(result[3].parts[0], UserPromptPart)
    assert result[4] == "tool"


async def test_compact_keepsThreeRecentTurns_andReestimates(monkeypatch):
    rows = [{"turn_id": i, "seq": i} for i in range(1, 6)]
    monkeypatch.setattr(compaction, "load_context", AsyncMock(return_value=(None, rows)))
    save = AsyncMock()
    monkeypatch.setattr(compaction, "save_summary", save)
    summarize = AsyncMock(return_value="goals")
    assert await compaction.compact_if_needed(1, 80, 100, summarize=summarize, estimate=AsyncMock(return_value=30))
    assert summarize.call_args.args[2] == rows[:2]
    save.assert_awaited_once_with(1, 2, "goals")


async def test_compact_raisesContextLimit_whenStillTooLarge(monkeypatch):
    monkeypatch.setattr(
        compaction, "load_context", AsyncMock(return_value=(None, [{"turn_id": i, "seq": i} for i in range(5)]))
    )
    monkeypatch.setattr(compaction, "save_summary", AsyncMock())
    with pytest.raises(ToolError, match="CONTEXT_LIMIT"):
        await compaction.compact_if_needed(
            1, 80, 100, summarize=AsyncMock(return_value="goals"), estimate=AsyncMock(return_value=80)
        )


async def test_compact_skipsBelowThreshold():
    assert not await compaction.compact_if_needed(1, 79, 100)


def test_history_preservesCompleteToolPair():
    rows = [
        {"turn_id": 1, "kind": "TOOL_CALL", "content": {"tool_call_id": "a", "tool_name": "read", "args": {}}},
        {
            "turn_id": 1,
            "kind": "TOOL_RESULT",
            "content": {"tool_call_id": "a", "tool_name": "read", "result": {"ok": True}},
        },
    ]
    history = builder.history_messages(None, rows)
    assert len(history) == 2
    assert history[0].parts[0].tool_call_id == history[1].parts[0].tool_call_id
    assert not builder.history_messages(None, list(reversed(rows)))
