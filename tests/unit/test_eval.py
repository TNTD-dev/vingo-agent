"""Test bộ eval không cần provider key hay backend/Postgres thật."""

import json
from datetime import datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import respx
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from evals.case_io import load_cases, render, time_variables
from evals.coverage import check_coverage
from evals.evaluators import evaluate, response
from evals.fixtures import ALIASES, BUSINESS_TABLES, check_reset
from evals.judge import judge, safe_payload
from evals.runner import real_case, summarize, validate_nodes
from tests.unit.test_buyer_read import event
from vingo_agent.agents import orchestrator
from vingo_agent.api.app import create_app

pytest_plugins = ["tests.unit.test_turn_loop"]


def test_cases_cover80Original_andFiveTemplates_withValidNodes():
    cases = load_cases()
    assert len([c for c in cases if not c["id"].startswith("X")]) == 80
    assert {c["id"].split("-")[0] for c in cases if c["id"].startswith("X")} == {"X01", "X02", "X03", "X06", "X08"}
    for case in cases:
        if "pytest" in case:
            validate_nodes(case["pytest"])
    assert any("{{month_start}}" in json.dumps(c) for c in cases)


def test_timeVariables_shareT0_andUseVietnamBoundaries():
    variables = time_variables(datetime.fromisoformat("2027-01-31T23:30:00+07:00"))
    assert variables["st_01_starts_at"] == "2027-02-10T19:00:00+07:00"
    assert variables["month_end"] == "2027-02-01T00:00:00+07:00"
    assert variables["weekend_start"] == "2027-01-30T00:00:00+07:00"
    assert render({"a": ["{{month_start}}", "Tháng {{month_label}}"]}, variables)["a"][1] == "Tháng 01/2027"
    with pytest.raises(KeyError):
        render("{{unknown}}", variables)


@pytest.mark.parametrize(
    "backend,db,allow",
    [
        ("https://production.example", "postgresql://a@localhost/eval", True),
        ("https://example.org/test", "postgresql://a@localhost/eval", True),
        ("http://localhost", "postgresql://a@production.example/eval", True),
        ("http://localhost", "postgresql://a@localhost/vingo", True),
        ("http://localhost", "postgresql://a@localhost/vingo_eval", False),
        ("http://localhost", "postgresql://test:eval@example.org/vingo", True),
        ("http://localhost", "postgresql://a@localhost/vingo_eval?database=production", True),
    ],
)
def test_reset_refusesUnsafeOrUnapprovedTarget(backend, db, allow):
    with pytest.raises(ValueError):
        check_reset(backend, db, environ={"EVAL_ALLOW_RESET": "1" if allow else "0"})


def test_reset_acceptsDedicatedTestDatabase_withExplicitFlag():
    check_reset(
        "http://localhost:8080/api/v1",
        "postgresql://a@localhost/vingo_eval",
        "postgresql://a@localhost/agent_test",
        environ={"EVAL_ALLOW_RESET": "1"},
    )


def test_evaluators_failMissingEvidence_andDetectSafety():
    expected = {
        "tools": ["search_events"],
        "business_effects": 0,
        "blocks": [{"type": "EventList"}],
        "canary_absent": True,
        "tool_args": {"search_events": {"q": "Demo"}},
    }
    actual = {"tools": [], "business_effects": 1, "blocks": [], "canary_leaks": {"logs": True}}
    result = evaluate(expected, actual)
    assert result["task"] and result["tool"] and result["response"] and result["safety"]
    assert response({}, {"blocks": [{"status": "OPEN"}], "text": "Đã thanh toán thành công"}) == [
        "response.contradiction"
    ]


def test_response_acceptsVerifiedFacts():
    assert response({"verify_blocks": True}, {"block_facts": [{"actual": 500000, "backend": 500000}]}) == []
    assert response({"verify_blocks": True}, {}) == ["response.missing_backend_evidence"]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"status":"SCORED","scores":{"intent":true,"context":2,"clarity":3},"reason":"x"}',
        '{"status":"SCORED","scores":null,"reason":"x"}',
    ],
)
async def test_judge_retriesInvalidJsonExactlyOnce_thenError(raw):
    invoke = AsyncMock(return_value=raw)
    result = await judge({"request": "Tìm vé"}, invoke=invoke)
    assert result["status"] == "ERROR" and invoke.await_count == 2


async def test_judge_acceptsInconclusive_andRetriesTimeout():
    invoke = AsyncMock(return_value='{"status":"INCONCLUSIVE","scores":null,"reason":"Thiếu ngữ cảnh"}')
    assert (await judge({}, invoke=invoke))["status"] == "INCONCLUSIVE"
    assert invoke.await_count == 1
    invoke = AsyncMock(
        side_effect=[TimeoutError(), '{"status":"SCORED","scores":{"intent":3,"context":2,"clarity":2},"reason":"Đủ"}']
    )
    assert (await judge({}, invoke=invoke))["passed"] is True
    assert invoke.await_count == 2


def test_judge_inputExcludesVerdictFormTraceAndPII():
    text = json.dumps(
        safe_payload(
            {
                "request": "OTP 123456, buyer@example.com +84912345678",
                "verdict": "FAIL",
                "trace": "SECRET",
                "buyer": {"email": "SECRET"},
            }
        )
    )
    assert all(s not in text for s in ("123456", "buyer@example.com", "84912345678", "SECRET", "FAIL"))


def test_coverage_mapsEveryRegisteredTool_andG1ThroughG11():
    result = check_coverage()
    assert result["complete"], result
    assert len(result["guardrails"]) == 11
    assert len({r["tool"] for r in result["tools"]}) == 51
    assert result["blocked"] > 0


def test_report_doesNotCountErrorsOrBlockedAsPass():
    report = summarize([{"status": s, "group": "buyer"} for s in ["PASS", "FAIL", "ERROR", "BLOCKED"]])
    assert report["metrics"]["task_success"] == {"numerator": 1, "denominator": 4, "rate": 0.25}
    assert report["cost"] == "unavailable"


@pytest.mark.parametrize("wanted", ["PASS", "FAIL", "ERROR", "BLOCKED"])
async def test_runner_callsAsgi_withFunctionModelAndFakeBackend(turn_env, monkeypatch, wanted):
    principal, cid, _, mocks = turn_env
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://eval@localhost/agent_eval")
    monkeypatch.setenv("EVAL_BACKEND_DATABASE_URL", "postgresql://eval@localhost/backend_eval")
    monkeypatch.setenv("EVAL_ALLOW_RESET", "1")
    from vingo_agent.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(orchestrator.repo, "create_conversation", AsyncMock(return_value={"id": cid, "role": "BUYER"}))
    monkeypatch.setattr(orchestrator.repo, "add_refs", AsyncMock())
    monkeypatch.setattr(orchestrator.repo, "get_messages", AsyncMock(return_value=[]))
    import evals.runner as runner

    monkeypatch.setattr(runner, "action_rows", AsyncMock(return_value=[]))

    async def respond(messages, info):
        if wanted == "ERROR":
            raise ConnectionError("DO-NOT-REPORT")
        if any(getattr(p, "part_kind", "") == "tool-return" for m in messages for p in m.parts):
            yield "Đây là kết quả."
        else:
            yield {0: DeltaToolCall(name="search_events", json_args='{"q":"Demo"}', tool_call_id="eval-call")}

    monkeypatch.setattr(orchestrator, "build_model", lambda *a, **kw: FunctionModel(stream_function=respond))
    case = {
        "id": "sample",
        "role": "BUYER",
        "actor": "B1",
        "mode": "Real",
        "fixture": "seed",
        "steps": [
            {
                "message": "Tìm Demo",
                "expected": {
                    "tools": ["propose_hold_seats" if wanted == "BLOCKED" else "search_events"],
                    "business_effects": 0,
                    "blocks": [{"type": "IMPOSSIBLE" if wanted == "FAIL" else "EventList"}],
                },
            }
        ],
    }

    async def seed(*args):
        return {
            "aliases": {**ALIASES, "B1": str(principal.user_id)},
            "sessions": {"B1": "eval-token"},
            "time": time_variables(datetime.now().astimezone()),
        }

    async def snap(*args):
        return {t: [] for t in BUSINESS_TABLES}

    with respx.mock as backend:
        backend.get("http://localhost:8080/api/v1/me").respond(200, json={"id": str(principal.user_id)})
        route = backend.get("http://localhost:8080/api/v1/events").respond(
            200, json={"data": [event() | {"id": str(uuid4())}]}
        )
        result = await real_case(
            case, datetime.now().astimezone(), app=create_app(), fixture_reset=seed, backend_snapshot=snap
        )
    assert result["status"] == wanted, json.dumps(result)
    assert "DO-NOT-REPORT" not in json.dumps(result)
    if wanted in {"PASS", "FAIL"}:
        assert route.call_count == 1
        assert result["steps"][0]["http"][0]["method"] == "GET"
        assert result["steps"][0]["tool_names"] == ["search_events"]
        assert isinstance(result["steps"][0]["first_text_s"], float)


def test_coverage_detectsMissingMapping_andInvalidAssertion(tmp_path):
    import yaml

    from evals.case_io import ROOT

    mapping = yaml.safe_load((ROOT / "coverage.yaml").read_text())
    mapping["tools"] = [e for e in mapping["tools"] if e["tool"] != "search_events"]
    mapping["guardrails"]["G1"]["deny"] = ["tests/unit/test_api.py::test_doesNotExist"]
    path = tmp_path / "coverage.yaml"
    path.write_text(yaml.safe_dump(mapping))
    result = check_coverage(path)
    assert not result["complete"]
    assert any(r["tool"] == "search_events" and r["status"] == "MISSING" for r in result["tools"])
    assert result["guardrails"][0]["status"] == "MISSING"


async def test_judge_neverPassesKnownCanaryToModel_orReport():
    secret = "EVAL_OPAQUE_SECRET_X97"
    invoke = AsyncMock(
        return_value=json.dumps(
            {"status": "SCORED", "scores": {"intent": 3, "context": 3, "clarity": 3}, "reason": secret}
        )
    )
    result = await judge({"request": "Mã " + secret}, invoke=invoke, secrets=[secret])
    assert secret not in invoke.await_args.args[0]
    assert secret not in json.dumps(result)
