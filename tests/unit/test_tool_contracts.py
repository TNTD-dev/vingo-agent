"""Sinh hợp đồng G4 từ schema mọi tool đăng ký; không cần backend/model."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from vingo_agent.agents.registry import tools_for
from vingo_agent.api.auth import Principal
from vingo_agent.guardrails.tool import guarded_tool
from vingo_agent.schemas.chat import Role
from vingo_agent.tools.base import TurnDeps


def sample(schema, root):
    if "$ref" in schema:
        return sample(root["$defs"][schema["$ref"].split("/")[-1]], root)
    if "anyOf" in schema:
        return sample(next(s for s in schema["anyOf"] if s.get("type") != "null"), root)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    if kind == "string":
        return {"uuid": str(uuid4()), "date-time": "2030-01-01T00:00:00+07:00", "uri": "https://example.invalid/a"}.get(
            schema.get("format"), "x" * max(1, schema.get("minLength", 1))
        )
    if kind in {"integer", "number"}:
        return schema.get("minimum", schema.get("exclusiveMinimum", 0) + 1)
    if kind == "boolean":
        return False
    if kind == "array":
        return [sample(schema["items"], root) for _ in range(max(1, schema.get("minItems", 1)))]
    if kind == "object":
        return {k: sample(v, root) for k, v in schema.get("properties", {}).items() if k in schema.get("required", [])}
    raise ValueError(schema)


def mutations(schema):
    base = sample(schema, schema)
    yield "extra", {**base, "__unknown_field": "injection"}
    for key, field in schema.get("properties", {}).items():
        if key in schema.get("required", []):
            yield key + "-missing", {k: v for k, v in base.items() if k != key}
            yield key + "-null", {**base, key: None}
        target = field
        if "anyOf" in target:
            target = next(s for s in target["anyOf"] if s.get("type") != "null")
        if "$ref" in target:
            target = schema["$defs"][target["$ref"].split("/")[-1]]
        kind = target.get("type")
        yield key + "-type", {**base, key: [] if kind != "array" else {}}
        if kind in {"integer", "number"}:
            yield key + "-bool", {**base, key: True}
        if "enum" in target or "const" in target:
            yield key + "-enum", {**base, key: "__INVALID_ENUM__"}
        for bound, invalid in [
            ("minimum", lambda v: v - 1),
            ("maximum", lambda v: v + 1),
            ("exclusiveMinimum", lambda v: v),
            ("exclusiveMaximum", lambda v: v),
        ]:
            if bound in target:
                yield key + "-" + bound, {**base, key: invalid(target[bound])}
        if "minLength" in target and target["minLength"] > 0:
            yield key + "-short", {**base, key: "x" * (target["minLength"] - 1)}
        if "maxLength" in target:
            yield key + "-long", {**base, key: "x" * (target["maxLength"] + 1)}
        if "minItems" in target and target["minItems"] > 0:
            yield key + "-few", {**base, key: []}
        if "maxItems" in target:
            yield key + "-many", {**base, key: [sample(target["items"], schema) for _ in range(target["maxItems"] + 1)]}


def contracts():
    for role in Role:
        for name, fn in tools_for(role).items():
            tool = guarded_tool(name, fn)
            for variant, args in mutations(tool.tool_def.parameters_json_schema):
                yield pytest.param(role, name, fn, copy.deepcopy(args), id=role.value + "-" + name + "-" + variant)


@pytest.mark.parametrize("role,name,fn,args", list(contracts()))
async def test_registeredTool_rejectsInvalidSchema_beforeHttp(role, name, fn, args):
    backend = AsyncMock()
    deps = TurnDeps(Principal(uuid4(), role, "eval-token"), uuid4(), uuid4(), backend)
    deps.visible_action = SimpleNamespace(action_id=uuid4(), version=1)
    result = await guarded_tool(name, fn).function(SimpleNamespace(deps=deps), **args)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert backend.mock_calls == []


def reference_contracts():
    import inspect

    for role in Role:
        for name, fn in tools_for(role).items():
            if "raise NotImplementedError" in inspect.getsource(inspect.unwrap(fn)):
                continue
            schema = guarded_tool(name, fn).tool_def.parameters_json_schema
            ids = [key for key in schema.get("properties", {}) if key.endswith("_id") or key.endswith("_ids")]
            if not ids:
                continue
            args = sample(schema, schema)
            if name == "list_audit_logs":
                args["entity"] = "order"
            for key in ids:
                args[key] = [str(uuid4())] if key.endswith("_ids") else str(uuid4())
            if "starts_at" in args:
                args.update(
                    starts_at="2030-01-10T19:00:00+07:00",
                    ends_at="2030-01-10T22:00:00+07:00",
                    sale_opens_at="2030-01-01T00:00:00+07:00",
                    sale_closes_at="2030-01-10T18:00:00+07:00",
                )
            yield pytest.param(role, name, fn, args, id=role.value + "-" + name)


@pytest.mark.parametrize("role,name,fn,args", list(reference_contracts()))
async def test_registeredTool_rejectsUnknownRef_beforeHttp(role, name, fn, args, monkeypatch):
    from vingo_agent.tools.base import repositories

    monkeypatch.setattr(repositories, "has_ref", AsyncMock(return_value=False))
    backend = AsyncMock()
    deps = TurnDeps(Principal(uuid4(), role, "eval-token"), uuid4(), uuid4(), backend)
    result = await guarded_tool(name, fn).function(SimpleNamespace(deps=deps), **args)
    assert result["error"]["code"] == "INVALID_REFERENCE"
    assert backend.mock_calls == []


@pytest.mark.parametrize("role", list(Role))
def test_registeredTools_areAbsentFromOtherRole_exceptSharedNames(role):
    other = Role.ADMIN if role == Role.BUYER else Role.BUYER
    shared = {"get_seatmap", "confirm_pending_action", "reject_pending_action"}
    assert not ((tools_for(role).keys() - shared) & tools_for(other).keys())
