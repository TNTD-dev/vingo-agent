"""Kiểm coverage theo role, assertion và pytest node tồn tại; không suy đoán theo câu hỏi."""

import argparse
import inspect
import json
from pathlib import Path

import yaml

from evals.case_io import ROOT, load_cases
from evals.runner import UNRELEASED, validate_nodes
from vingo_agent.agents.registry import tools_for
from vingo_agent.config import get_settings
from vingo_agent.schemas.chat import Role


def check_coverage(path: Path = ROOT / "coverage.yaml"):
    mapping = yaml.safe_load(path.read_text())
    cases = {c["id"]: c for c in load_cases()}
    rows = []
    mapped = set()
    for entry in mapping["tools"]:
        role, name = entry["role"], entry["tool"]
        mapped.add((role, name))
        fn = tools_for(Role(role)).get(name)
        reason = (
            UNRELEASED.get(name)
            or ("DISABLED" if name in get_settings().agent_disabled_tools else None)
            or (
                "NOT_REGISTERED"
                if fn is None
                else "NOT_IMPLEMENTED"
                if "raise NotImplementedError" in inspect.getsource(inspect.unwrap(fn))
                else None
            )
        )
        problems = []
        valid = []
        for ident in entry.get("valid", []):
            if ident.startswith("tests/"):
                try:
                    validate_nodes([ident])
                    valid.append(ident)
                except ValueError:
                    problems.append("INVALID_NODE:" + ident)
                continue
            case = cases.get(ident)
            if (
                not case
                or case["role"] != role
                or not any(
                    name in step["expected"].get("tools", [])
                    or name in step["expected"].get("tool_args", {})
                    or step["expected"].get("first_tool") == name
                    for step in case["steps"]
                )
            ):
                problems.append("NO_EXPLICIT_ASSERTION:" + ident)
            else:
                valid.append(ident)
        if not valid:
            problems.append("MISSING_VALID")
        errors = entry.get("error", [])
        for node in errors:
            try:
                validate_nodes([node])
            except ValueError:
                problems.append("INVALID_ERROR_NODE:" + node)
        if not errors:
            problems.append("MISSING_ERROR")
        rows.append(
            {
                "role": role,
                "tool": name,
                "status": "BLOCKED" if reason else "MISSING" if problems else "COVERED",
                "reason": reason,
                "problems": problems,
            }
        )
    for role in Role:
        for name in tools_for(role):
            if (role.value, name) not in mapped:
                rows.append(
                    {"role": role.value, "tool": name, "status": "MISSING", "problems": ["UNMAPPED_REGISTRY_TOOL"]}
                )
    guards = []
    for name in [f"G{i}" for i in range(1, 12)]:
        entry = mapping.get("guardrails", {}).get(name, {})
        problems = []
        for direction in ("allow", "deny"):
            if not entry.get(direction):
                problems.append("MISSING_" + direction.upper())
            for node in entry.get(direction, []):
                try:
                    validate_nodes([node])
                except ValueError:
                    problems.append("INVALID_NODE:" + node)
        guards.append({"guardrail": name, "status": "MISSING" if problems else "COVERED", "problems": problems})
    eligible = [r for r in rows if r["status"] != "BLOCKED"]
    return {
        "tools": rows,
        "guardrails": guards,
        "numerator": sum(r["status"] == "COVERED" for r in eligible),
        "denominator": len(eligible),
        "blocked": sum(r["status"] == "BLOCKED" for r in rows),
        "complete": all(r["status"] != "MISSING" for r in rows + guards),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = check_coverage()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["complete"] else 1)


if __name__ == "__main__":
    main()
