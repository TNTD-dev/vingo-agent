import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from vingo_agent.context.tokens import TokenEstimator, text_chars


def test_textChars_countsContentAndInstructions_withoutObjectMetadata():
    request = ModelRequest(parts=[SystemPromptPart("hệ thống"), UserPromptPart("xin chào")], instructions="quy tắc")
    response = ModelResponse(parts=[TextPart("trả lời")], model_name="ignored-model-name")
    expected = sum(map(len, ["hệ thống", "xin chào", "quy tắc", "trả lời"]))
    assert text_chars([request, response]) == expected
    assert (
        text_chars(
            [
                replace(
                    request,
                    timestamp=datetime(2020, 1, 1, tzinfo=UTC),
                    run_id="ignored-run-id",
                    metadata={"long": "x" * 1000},
                ),
                response,
            ]
        )
        == expected
    )
    assert text_chars([request, response]) < len(repr([request, response]))


def test_textChars_countsToolNameAndJson_withoutCallId():
    args, content = {"q": "vé"}, {"items": [{"title": "Sự kiện", "price": 100000}]}
    call = ModelResponse(parts=[ToolCallPart("search", args, "call-id-not-counted")])
    result = ModelRequest(parts=[ToolReturnPart("search", content, "call-id-not-counted")])
    expected = len("search") + len(json.dumps(args, ensure_ascii=False, separators=(",", ":")))
    expected += len(json.dumps(content, ensure_ascii=False, separators=(",", ":")))
    assert text_chars([call, result]) == expected
    call.parts[0].args = '{ "q": "vé" }'
    assert text_chars([call, result]) == expected


def test_textChars_countsTextOnlyInUserPartsAndRawSchemaJson():
    assert text_chars([ModelRequest(parts=[UserPromptPart(["một", "hai"])])]) == 6
    schema = '{"type":"object"}'
    assert text_chars(schema) == len(schema)
    assert text_chars([]) == 0


def test_estimator_usesDefaultRatioAndSchema_withoutAnchor():
    estimator = TokenEstimator()
    assert estimator.ratio == 2.5
    assert estimator.estimate(100, 25) == 50
    assert TokenEstimator(default_chars_per_token=4).estimate(100, 25) == 32


def test_estimator_anchorsOnLatestRequestUsage():
    estimator = TokenEstimator()
    assert estimator.observe(100, 400)
    assert estimator.ratio == 4
    assert estimator.estimate(480, 1000) == 120
    assert estimator.estimate(360) == 90
    assert estimator.observe(80, 400)
    assert estimator.ratio == 5
    assert estimator.estimate(450) == 90


def test_estimator_keepsRatioAndDropsAnchor_afterCompact():
    estimator = TokenEstimator()
    estimator.observe(100, 400)
    estimator.reset_anchor()
    assert estimator.anchor_tokens is None and estimator.anchor_chars is None
    assert estimator.ratio == 4
    assert estimator.estimate(80, 40) == 30


@pytest.mark.parametrize("tokens,chars", [(0, 100), (None, 100), (-1, 100), (20, 0), (20, None)])
def test_estimator_ignoresMissingOrInvalidUsage(tokens, chars):
    estimator = TokenEstimator()
    assert not estimator.observe(tokens, chars)
    assert estimator.estimate(25) == 10
    estimator.observe(10, 40)
    assert not estimator.observe(tokens, chars)
    assert (estimator.anchor_tokens, estimator.anchor_chars, estimator.ratio) == (10, 40, 4)


@pytest.mark.parametrize("ratio", [0, -1, float("inf"), float("nan")])
def test_estimator_rejectsInvalidDefaultRatio(ratio):
    with pytest.raises(ValueError):
        TokenEstimator(ratio)


def test_estimator_ratioIncludesSchema_becauseInputTokensIncludeSchema():
    # Provider tính cả schema tool vào input_tokens: 300 ký tự nội dung + 100 ký tự schema = 100 token → 4 ký tự/token.
    estimator = TokenEstimator(2.5)
    assert estimator.observe(100, 300, schema_chars=100)
    assert estimator.ratio == 4
    estimator.reset_anchor()
    assert estimator.estimate(300, schema_chars=100) == 100
