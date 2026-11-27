"""Đọc case, render alias/T0; không đưa UUID fixture thẳng vào lời nhắn model."""

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).parent
VN = ZoneInfo("Asia/Ho_Chi_Minh")


def time_variables(t0: datetime) -> dict:
    local = t0.astimezone(VN)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    weekend = day + timedelta(days=(5 - day.weekday()) % 7)
    if day.weekday() == 6:
        weekend = day - timedelta(days=1)
    month = day.replace(day=1)
    following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    starts = (day + timedelta(days=10)).replace(hour=19)
    return {
        "T0": local.isoformat(),
        "st_01_starts_at": starts.isoformat(),
        "st_01_ends_at": (starts + timedelta(hours=3)).isoformat(),
        "sale_opens_at": (local - timedelta(days=1)).isoformat(),
        "sale_closes_at": (starts - timedelta(hours=1)).isoformat(),
        "weekend_start": weekend.isoformat(),
        "weekend_end": (weekend + timedelta(days=2)).isoformat(),
        "month_start": month.isoformat(),
        "month_end": following.isoformat(),
        "month_label": local.strftime("%m/%Y"),
    }


def render(value, variables: dict):
    if isinstance(value, dict):
        return {k: render(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, variables) for v in value]
    if isinstance(value, str):
        match = re.fullmatch(r"\{\{\s*(\w+)\s*\}\}", value)
        if match:
            return variables[match[1]]
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(variables[m[1]]), value)
    return value


def load_cases(directory: Path = ROOT / "cases") -> list[dict]:
    cases = []
    for path in sorted(directory.glob("*.yaml")):
        for entry in yaml.safe_load(path.read_text()) or []:
            if "variants" in entry:
                for variant in entry["variants"]:
                    case = {k: v for k, v in entry.items() if k != "variants"}
                    case.update(variant)
                    case["id"] = f"{entry['id']}-{variant['name']}"
                    case.pop("name")
                    cases.append(case)
            else:
                cases.append(entry)
    seen = set()
    for case in cases:
        required = {"id", "role", "actor", "fixture", "mode", "steps"}
        if required - case.keys() or case["id"] in seen:
            raise ValueError(f"Case không hợp lệ: {case.get('id')}")
        seen.add(case["id"])
        if case["mode"] not in {"Real", "pytest", "E2E", "Backend"}:
            raise ValueError(f"Mode không hợp lệ: {case['id']}")
        if case["mode"] == "pytest" and not case.get("pytest"):
            raise ValueError(f"Thiếu pytest node: {case['id']}")
        if case["mode"] in {"Real", "E2E"} and not case["steps"]:
            raise ValueError(f"Thiếu bước: {case['id']}")
        for step in case["steps"]:
            if sum(k in step for k in ("message", "confirm", "reject")) != 1 or not step.get("expected"):
                raise ValueError(f"Bước không hợp lệ: {case['id']}")
    return cases
