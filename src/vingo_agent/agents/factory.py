"""Dựng PydanticAI Agent theo role: prompt từ prompts/<role>.md, tool từ registry, output là text thuần."""

from importlib.resources import files

from vingo_agent.schemas.chat import Role


def load_prompt(role: Role) -> str:
    return files("vingo_agent.prompts").joinpath(f"{role.value.lower()}.md").read_text(encoding="utf-8")


def build_model(primary: str, fallback: str = ""):
    """Model chính, hoặc FallbackModel(primary, fallback) khi có model dự phòng (V2.2 mục 2).

    Chỉ fallback với lỗi timeout/5xx/429/kết nối và trước chữ đầu tiên của lần gọi; 4xx khác không fallback.
    """
    raise NotImplementedError


def build_agent(role: Role, *, model, disabled_tools: frozenset[str]):
    """Agent(model, deps_type=TurnDeps, output_type=str, instructions=load_prompt(role)) + tool từ registry."""
    raise NotImplementedError
