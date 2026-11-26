"""Cấu hình đọc từ biến môi trường (V2.3 mục 2, 5, 9). Secret chỉ đến từ env, không có default thật."""

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://vingo_agent:vingo_agent@localhost:5432/vingo"
    backend_url: str = "http://localhost:8080/api/v1"

    # Backend → agent: header X-Agent-Service-Key. Agent → backend: header X-Vingo-Agent-Key.
    agent_service_key: str = ""
    agent_to_backend_key: str = ""

    # Chuỗi model PydanticAI, ví dụ "openai:<model>". Bắt buộc tool calling + streaming + tiếng Việt.
    # API key đọc theo biến môi trường chuẩn của từng provider (OPENAI_API_KEY, DEEPSEEK_API_KEY...).
    agent_model: str = "openai:gpt-6-luna"
    # Model dự phòng: "deepseek:deepseek-flash" (DeepSeek V4.1 Flash). Rỗng = không fallback.
    agent_fallback_model: str = "deepseek:deepseek-flash"

    turn_deadline_s: float = 120
    max_model_calls: int = 20
    max_tool_calls: int = 40
    max_input_chars: int = 4000
    compact_ratio: float = Field(default=0.8, gt=0, lt=1)
    context_window_tokens: int = Field(default=128000, gt=0)
    output_reserved_tokens: int = Field(default=8192, ge=0)
    lease_s: float = 150  # lease của turn > deadline để không bị turn khác chiếm giữa chừng

    # Kill switch (V2.3 mục 9): tool trong danh sách không được đăng ký với model.
    agent_disabled_tools: Annotated[frozenset[str], NoDecode] = frozenset({"suggest_seats"})
    log_level: str = "INFO"

    @field_validator("agent_disabled_tools", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(v.strip() for v in value.split(",") if v.strip())
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
