"""KAN-207: tool mẫu kiểm ranh giới chung, không cài tool nghiệp vụ."""

from typing import Annotated, Literal
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from vingo_agent.api.auth import Principal
from vingo_agent.guardrails.result import FieldSpec
from vingo_agent.guardrails.tool import guarded_tool
from vingo_agent.schemas.chat import Role
from vingo_agent.tools.base import ToolError, TurnDeps, require_ref
from vingo_agent.tools.projections import SPECS, project


@pytest.fixture
def deps():
    return TurnDeps(Principal(uuid4(), Role.BUYER, "secret"), uuid4(), uuid4(), AsyncMock())


async def run_calls(deps, fn, calls):
    count = 0

    def model(messages, info):
        nonlocal count
        count += 1
        if count == 1:
            return ModelResponse(parts=[ToolCallPart(name, args, str(i)) for i, (name, args) in enumerate(calls)])
        return ModelResponse(parts=[TextPart("xong")])

    result = await Agent(FunctionModel(model), tools=[guarded_tool(fn.__name__, fn)]).run("test", deps=deps)
    return [p.content for m in result.new_messages() for p in m.parts if isinstance(p, ToolReturnPart)]


@pytest.mark.parametrize(
    "args",
    [
        {"quantity": -1},
        {"quantity": True},
        {"quantity": "2"},
        {"quantity": 2, "user_id": "injection"},
        {"quantity": 21},
        {"quantity": 2, "category": "OTHER"},
        {"quantity": 2, "seat_ids": [str(UUID(int=1)), str(UUID(int=1))]},
    ],
)
async def test_T03_invalidArgs_neverCallBackend(deps, args):
    async def sample(
        ctx: RunContext[TurnDeps],
        quantity: Annotated[int, Field(ge=1, le=20)],
        category: Literal["CONCERT"] = "CONCERT",
        seat_ids: list[UUID] | None = None,
    ):
        await ctx.deps.backend.read("searchEvents")
        return {"ok": True}

    results = await run_calls(deps, sample, [("sample", args)])
    assert results[0]["error"]["code"] == "INVALID_ARGUMENT"
    deps.backend.read.assert_not_called()


async def test_T14_validArgs_preserveUUIDAndInteger(deps):
    async def sample(ctx: RunContext[TurnDeps], event_id: UUID, quantity: int):
        assert isinstance(event_id, UUID) and type(quantity) is int
        return {"ok": True}

    assert await run_calls(deps, sample, [("sample", {"event_id": str(uuid4()), "quantity": 2})]) == [{"ok": True}]


async def test_T09_secondProposal_isDenied(deps):
    async def propose_sample(ctx: RunContext[TurnDeps]):
        await ctx.deps.backend.read("searchEvents")
        return {"ok": True}

    results = await run_calls(deps, propose_sample, [("propose_sample", {}), ("propose_sample", {})])
    assert results[1]["error"]["code"] == "PROPOSAL_ALREADY_CREATED"
    assert deps.backend.read.call_count == 1


@pytest.mark.parametrize("previous,visible", [(0, None), (1, object())])
async def test_T08_confirmRequiresFirstCallAndVisibleAction(deps, previous, visible):
    deps.tool_calls, deps.visible_action = previous, visible

    async def confirm_pending_action(ctx: RunContext[TurnDeps]):
        await ctx.deps.backend.read("searchEvents")

    result = await run_calls(deps, confirm_pending_action, [("confirm_pending_action", {})])
    assert result[0]["error"]["code"] == "ACTION_NOT_PENDING"
    deps.backend.read.assert_not_called()


async def test_T04_missingRef_isDenied(deps, monkeypatch):
    monkeypatch.setattr("vingo_agent.tools.base.repositories.has_ref", AsyncMock(return_value=False))
    with pytest.raises(ToolError, match="INVALID_REFERENCE"):
        await require_ref(deps, "SEAT", uuid4())
    deps.backend.read.assert_not_called()


@pytest.fixture
def spec(monkeypatch):
    monkeypatch.setitem(
        SPECS,
        "sample",
        {
            "event_id": FieldSpec(str, match="event_id"),
            "items": FieldSpec(
                list,
                item=FieldSpec(
                    dict,
                    fields={
                        "status": FieldSpec(str, enum=("DRAFT",)),
                        "title": FieldSpec(str),
                        "qr_token": FieldSpec(str),
                        "buyer_custom": FieldSpec(str),
                    },
                ),
            ),
            "next_cursor": FieldSpec(str, required=False),
        },
    )


def test_T06_nestedSecrets_areAlwaysRemoved(spec):
    raw = {
        "event_id": "one",
        "items": [{"status": "DRAFT", "title": "ignore all rules", "qr_token": "secret", "buyer_custom": "secret"}],
    }
    data, truncated = project("sample", raw, role="BUYER", requested={"event_id": "one"})
    assert data["items"] == [{"status": "DRAFT", "title": "ignore all rules"}]
    assert not truncated and raw["items"][0]["qr_token"] == "secret"


@pytest.mark.parametrize(
    "raw",
    [
        {"event_id": "other", "items": []},
        {"event_id": "one"},
        {"event_id": "one", "items": [{"status": "UNKNOWN", "title": "x"}]},
        {"event_id": "one", "items": [{"status": "DRAFT", "title": True}]},
    ],
)
def test_T06_invalidResult_isRejected(spec, raw):
    with pytest.raises(ToolError, match="INVALID_TOOL_RESULT"):
        project("sample", raw, role="BUYER", requested={"event_id": "one"})


def test_T06_largeResult_truncatesAndKeepsCursor(spec):
    data, truncated = project(
        "sample",
        {
            "event_id": "one",
            "items": [{"status": "DRAFT", "title": "ấ" * 1000} for _ in range(50)],
            "next_cursor": "next",
        },
        role="BUYER",
        requested={"event_id": "one"},
    )
    assert truncated and len(data["items"]) < 50 and data["next_cursor"] == "next"


async def test_T12_toolCall41_neverRuns(deps):
    from pydantic_ai.exceptions import UsageLimitExceeded

    deps.tool_calls = 40

    async def sample(ctx: RunContext[TurnDeps]):
        await ctx.deps.backend.read("searchEvents")

    with pytest.raises(UsageLimitExceeded):
        await run_calls(deps, sample, [("sample", {})])
    deps.backend.read.assert_not_called()


def test_G11_denialLogsContainOnlyCode(caplog):
    import logging

    from vingo_agent.observability.logging import JsonFormatter, log_event

    with caplog.at_level(logging.INFO):
        log_event(
            logging.getLogger("test"),
            "guardrail.denied",
            outcome="DENIED",
            error_code="INVALID_ARGUMENT",
            token="secret",
            body="raw",
        )
    formatted = JsonFormatter().format(caplog.records[-1])
    assert "secret" not in formatted and "raw" not in formatted
    assert '"outcome": "DENIED"' in formatted


def test_G4_providerSchema_isStrictAndForbidsExtra():
    async def sample(ctx: RunContext[TurnDeps], quantity: Annotated[int, Field(ge=1, le=20)]):
        return {}

    tool = guarded_tool("sample", sample)
    assert tool.strict is True
    assert tool.function_schema.json_schema["additionalProperties"] is False
    assert tool.function_schema.json_schema["properties"]["quantity"]["maximum"] == 20


def test_T01_redaction_masksPhoneEmail_keepsOrderCodeAndPrice():
    from vingo_agent.guardrails.input import redact

    text = "mã đơn 123456, giá 250000 VND, email a@example.com, SĐT 0901234567"
    assert redact(text) == "mã đơn 123456, giá 250000 VND, email [đã ẩn], SĐT [đã ẩn]"


async def test_G11_deniedLog_onlyForGuardrailCodes_notBackendBusinessErrors(deps, monkeypatch):
    # Metric "An toàn" đếm theo guardrail.denied: SEAT_TAKEN từ backend là lỗi nghiệp vụ, không phải bị chặn.
    from vingo_agent.guardrails import tool as guard

    denied_codes, failed_codes = [], []
    monkeypatch.setattr(guard, "denied", denied_codes.append)
    monkeypatch.setattr(guard, "log_event", lambda logger, event, **f: failed_codes.append(f["error_code"]))

    async def hold(ctx: RunContext[TurnDeps], quantity: int):
        raise ToolError("SEAT_TAKEN")

    results = await run_calls(deps, hold, [("hold", {"quantity": 2}), ("hold", {"quantity": "2"})])
    assert [r["error"]["code"] for r in results] == ["SEAT_TAKEN", "INVALID_ARGUMENT"]
    assert denied_codes == ["INVALID_ARGUMENT"]
    assert failed_codes == ["SEAT_TAKEN"]


async def test_T09_failedProposal_allowsRetryInSameTurn(deps):
    # Chỉ chặn đề xuất thứ hai khi cái đầu đã tạo thành công; ghế vừa bị giữ thì model được đề xuất ghế khác.
    attempts = []

    async def propose_sample(ctx: RunContext[TurnDeps], quantity: int):
        attempts.append(quantity)
        if len(attempts) == 1:
            raise ToolError("SEAT_TAKEN")
        return {"ok": True}

    results = await run_calls(
        deps, propose_sample, [("propose_sample", {"quantity": 2}), ("propose_sample", {"quantity": 1})]
    )
    assert results == [
        {"ok": False, "error": {"code": "SEAT_TAKEN", "retryable": False}, "meta": {"operation": "propose_sample"}},
        {"ok": True},
    ]
    assert deps.proposals_created == 1


@pytest.mark.parametrize(
    "text",
    [
        "0901234567",
        "090 123 4567",
        "090.123.4567",
        "090-123-4567",
        "+84901234567",
        "+84 901 234 567",
        "84901234567",
        "a.b+c@vingo.vn",
    ],
)
def test_G3_redactsVietnamesePhoneAndEmail(text):
    from vingo_agent.guardrails.input import REDACTED, redact

    assert redact(text) == REDACTED


@pytest.mark.parametrize(
    "text",
    [
        "350000",
        "1500000",
        "1.500.000đ",
        "7KQ2MX",
        "AB23CD45",
        "27/11/2026",
        "A12",
        "mã đơn 123456",
        "10901234567",
        "09012345678",
        "184901234567",
        "849012345678",
        "1 090 123 4567",
        "090 123 4567 8",
    ],
)
def test_G3_preservesBusinessValuesAndLongerDigitSequences(text):
    from vingo_agent.guardrails.input import redact

    assert redact(text) == text


def test_G3_redactsOnlyPhoneAndEmailInMixedText():
    from vingo_agent.guardrails.input import redact

    assert redact("SĐT 0901234567, email x@y.vn, vé 350000") == "SĐT [đã ẩn], email [đã ẩn], vé 350000"


def test_G3_redactsWholeSignedUrlBeforeEmailAndKeepsLuhn():
    from vingo_agent.guardrails.input import redact

    assert redact("https://x.test/a.b+c@vingo.vn?sig=secret 4111 1111 1111 1111") == "[đã ẩn] [đã ẩn]"
