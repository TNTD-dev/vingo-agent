"""Dựng PydanticAI Agent theo role: prompt từ prompts/<role>.md, tool từ registry, output là text thuần."""

from importlib.resources import files

from vingo_agent.schemas.chat import Role


def load_prompt(role: Role) -> str:
    return files("vingo_agent.prompts").joinpath(f"{role.value.lower()}.md").read_text(encoding="utf-8")


def build_agent(role: Role, *, model: str, disabled_tools: frozenset[str]):
    """Agent(model, deps_type=TurnDeps, output_type=str, instructions=load_prompt(role)) + tool từ registry."""
    raise NotImplementedError
