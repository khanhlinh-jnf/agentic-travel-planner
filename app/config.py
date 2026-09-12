"""Central configuration. This is the only module that reads environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="Agentic Travel Planner", alias="APP_NAME")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    api_base_url: str = Field(default="http://127.0.0.1:8000", alias="API_BASE_URL")

    openai_api_keys: str = Field(default="", alias="OPENAI_API_KEYS")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    llm_model: str = Field(default="gpt-5.4-mini", alias="LLM_MODEL")
    llm_max_completion_tokens: int = Field(
        default=3200, alias="LLM_MAX_COMPLETION_TOKENS"
    )
    use_mock_llm: bool = Field(default=False, alias="USE_MOCK_LLM")

    use_mock_travel_data: bool = Field(default=False, alias="USE_MOCK_TRAVEL_DATA")
    travel_search_max_results: int = Field(default=5, alias="TRAVEL_SEARCH_MAX_RESULTS")
    serpapi_api_key: str = Field(default="", alias="SERPAPI_API_KEY")
    serpapi_mcp_url: str = Field(
        default="https://mcp.serpapi.com/mcp", alias="SERPAPI_MCP_URL"
    )
    booking_mcp_url: str = Field(
        default="https://hotels.flightpowers.com/mcp", alias="BOOKING_MCP_URL"
    )
    rapidapi_key: str = Field(default="", alias="RAPIDAPI_KEY")

    max_revisions: int = Field(default=3, alias="MAX_REVISIONS")
    max_external_search_calls_per_plan: int = Field(
        default=6, alias="MAX_EXTERNAL_SEARCH_CALLS_PER_PLAN"
    )
    user_memory_path: str = Field(
        default="./data/user_memory.json", alias="USER_MEMORY_PATH"
    )

    @property
    def api_keys(self) -> list[str]:
        combined = self.openai_api_keys or self.openai_api_key
        return [value.strip() for value in combined.split(",") if value.strip()]

    @property
    def primary_api_key(self) -> str:
        return self.api_keys[0] if self.api_keys else ""

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def memory_file(self) -> Path:
        path = Path(self.user_memory_path)
        return path if path.is_absolute() else self.project_root / path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
