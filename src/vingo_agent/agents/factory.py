"""Dựng agent theo role và fallback trước chữ đầu tiên."""

import logging
from contextlib import asynccontextmanager
from importlib.resources import files

import httpx
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta
from pydantic_ai.models import infer_model
from pydantic_ai.models.fallback import FallbackModel

from vingo_agent.agents.registry import tools_for
from vingo_agent.guardrails.tool import guarded_tool
from vingo_agent.observability.logging import log_event
from vingo_agent.schemas.chat import Role
from vingo_agent.tools.base import TurnDeps


def load_prompt(role: Role) -> str:
    return files("vingo_agent.prompts").joinpath(f"{role.value.lower()}.md").read_text(encoding="utf-8")


def can_fallback(error: Exception) -> bool:
    if isinstance(error, ModelHTTPError):
        return error.status_code == 429 or error.status_code >= 500
    if isinstance(error.__cause__, Exception):
        return can_fallback(error.__cause__)
    return isinstance(error, (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError))


class TurnFallbackModel(FallbackModel):
    def _record_failed_attempt(self, chain, model, failure, start):
        log_event(logging.getLogger(__name__), "model.fallback", outcome="FALLBACK")
        super()._record_failed_attempt(chain, model, failure, start)

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        # Đệm các sự kiện metadata/tool cho tới chữ đầu tiên; chưa có chữ thì vẫn được fallback.
        for index, model in enumerate(self.models):
            handed_over = False
            try:
                async with model.request_stream(
                    messages, model_settings, model_request_parameters, run_context
                ) as response:
                    iterator = response.__aiter__()
                    buffered = []
                    async for event in iterator:
                        buffered.append(event)
                        if (
                            isinstance(event, PartStartEvent)
                            and isinstance(event.part, TextPart)
                            and event.part.content
                        ) or (
                            isinstance(event, PartDeltaEvent)
                            and isinstance(event.delta, TextPartDelta)
                            and event.delta.content_delta
                        ):
                            break

                    async def replay(events=buffered, rest=iterator):
                        for item in events:
                            yield item
                        async for item in rest:
                            yield item

                    response._event_iterator = replay()
                    handed_over = True
                    yield response
                    return
            except Exception as error:
                if handed_over or not can_fallback(error) or index == len(self.models) - 1:
                    raise
                log_event(logging.getLogger(__name__), "model.fallback", outcome="FALLBACK")


def build_model(primary, fallback=""):
    primary = infer_model(primary) if isinstance(primary, str) else primary
    return TurnFallbackModel(primary, fallback, fallback_on=can_fallback) if fallback else primary


def build_agent(role: Role, *, model, disabled_tools: frozenset[str]):
    return Agent(
        model,
        deps_type=TurnDeps,
        output_type=str,
        instructions=load_prompt(role),
        tools=[guarded_tool(name, fn) for name, fn in tools_for(role, disabled_tools).items()],
    )
