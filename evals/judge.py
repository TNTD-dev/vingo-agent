"""Judge độc lập, không tool; validate JSON và retry tối đa một lần."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from pydantic_ai import Agent

from evals.case_io import ROOT
from vingo_agent.guardrails.input import redact


class Scores(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: StrictInt = Field(ge=1, le=3)
    context: StrictInt = Field(ge=1, le=3)
    clarity: StrictInt = Field(ge=1, le=3)


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["SCORED", "INCONCLUSIVE"]
    scores: Scores | None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def check_scores(self):
        if (self.status == "SCORED") != (self.scores is not None):
            raise ValueError("scores không khớp status")
        return self


def safe_payload(value):
    """Allowlist trường; không nhận trace, code verdict, form hay block chứa link/QR."""
    allowed = {
        "request",
        "history",
        "goal",
        "facts",
        "response",
        "text",
        "type",
        "title",
        "status",
        "blocks",
        "items",
        "zones",
        "seats",
        "cards",
        "name",
        "price",
        "price_from",
        "amount",
        "total",
        "total_amount",
        "quantity",
        "starts_at",
        "ends_at",
        "value",
        "metric",
        "unit",
        "seats_left",
        "on_sale",
        "event_title",
    }
    if isinstance(value, dict):
        return {k: safe_payload(v) for k, v in value.items() if k in allowed}
    if isinstance(value, list):
        return [safe_payload(v) for v in value]
    if isinstance(value, str):
        import re

        text = redact(value)
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
        text = re.sub(r"(?<!\w)\+?\d[\d .-]{8,}\d", "[phone]", text)
        return re.sub(r"https?://\S+", "[url]", text)
    return value


async def judge(payload: dict, *, invoke=None, secrets=(), calibration=False) -> dict:
    model = os.environ.get("EVAL_JUDGE_MODEL")
    if invoke is None and not model:
        return {"status": "ERROR", "reason": "EVAL_JUDGE_MODEL_MISSING"}
    if invoke is None and not calibration:
        rows = yaml.safe_load((ROOT / "judge_calibration.yaml").read_text())
        if not 10 <= sum(r.get("human_scores") is not None for r in rows) <= 15:
            return {"status": "ERROR", "reason": "JUDGE_CALIBRATION_REQUIRED"}
    encoded = json.dumps(safe_payload(payload), ensure_ascii=False)
    for secret in secrets:
        encoded = encoded.replace(secret, "[đã ẩn]")
    if invoke is None:
        agent = Agent(model, instructions=(ROOT / "judge.md").read_text(), tools=[], retries=0)

        async def invoke(data):
            result = await agent.run(data)
            return result.output

    for attempt in range(2):
        try:
            raw = await asyncio.wait_for(invoke(encoded), timeout=60)
            result = Verdict.model_validate_json(raw)
            verdict = result.model_dump()
            verdict["passed"] = result.scores is not None and min(result.scores.model_dump().values()) >= 2
            # Không lưu reason thô do model trả (có thể lặp PII).
            verdict["reason"] = safe_payload(result.reason)
            for secret in secrets:
                verdict["reason"] = verdict["reason"].replace(secret, "[đã ẩn]")
            verdict["attempts"] = attempt + 1
            return verdict
        except Exception:
            pass
    return {"status": "ERROR", "reason": "JUDGE_INVALID_OR_UNAVAILABLE", "attempts": 2}


async def calibrate(path: Path) -> dict:
    rows = yaml.safe_load(path.read_text())
    eligible = [r for r in rows if r.get("human_scores") is not None]
    compared = []
    for row in eligible:
        human = Scores.model_validate(row["human_scores"]).model_dump()
        verdict = await judge(row["input"], calibration=True)
        compared.append({"id": row["id"], "human": human, "judge": verdict, "exact": verdict.get("scores") == human})
    return {
        "filled": len(eligible),
        "required": "10–15",
        "ready": 10 <= len(eligible) <= 15,
        "exact": sum(r["exact"] for r in compared),
        "denominator": len(compared),
        "samples": compared,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--calibrate", type=Path, default=ROOT / "judge_calibration.yaml")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(calibrate(args.calibrate)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
