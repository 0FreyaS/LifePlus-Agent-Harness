from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_mode: str = "local"
    lifeplus_root: str | None = None
    showcase_username: str = "interviewer"
    showcase_password: SecretStr | None = None
    showcase_allow_http: bool = False
    public_origin: str = ""
    default_current_location: str = "江苏省无锡市滨湖区东南大学无锡校区"
    default_current_city: str = "无锡市"
    database_path: str = "./data/lifeplus.sqlite3"
    checkpoint_postgres_dsn: str | None = None
    redis_url: str | None = None
    mysql_dsn: str | None = None
    chroma_path: str = "./data/chroma"
    tool_queue_workers: int = 4
    tool_queue_timeout_seconds: float = 8.0
    tool_queue_max_retries: int = 2
    max_active_tasks_per_user: int = Field(default=8, ge=1, le=1000)
    runtime_active_seconds: float = Field(default=120, gt=0, le=1800)
    runtime_planning_seconds: float = Field(default=45, gt=0, le=300)
    input_event_timeout_seconds: float = Field(default=25, ge=1, le=60)
    runtime_model_calls: int = Field(default=8, ge=1, le=100)
    runtime_tool_calls: int = Field(default=24, ge=1, le=200)
    runtime_resumes: int = Field(default=8, ge=1, le=100)
    runtime_refreshes: int = Field(default=5, ge=1, le=50)
    runtime_retries: int = Field(default=3, ge=1, le=20)
    runtime_graph_steps: int = Field(default=64, ge=8, le=500)
    model_max_output_tokens: int = Field(default=2048, ge=128, le=8192)
    model_stream_usage: bool = True
    model_thinking: Literal["enabled", "disabled"] | None = None
    planner_variant: Literal["baseline", "compact"] = "baseline"
    planner_model_name: str | None = None
    intent_model_name: str | None = None
    response_model_name: str | None = None
    planner_max_output_tokens: int = Field(default=1536, ge=512, le=8192)
    intent_max_output_tokens: int | None = Field(default=None, ge=128, le=8192)
    response_max_output_tokens: int | None = Field(default=None, ge=128, le=8192)
    domain_route_provider: Literal["llm", "jev_cascade"] = "llm"
    input_event_provider: Literal["llm", "jev_cascade", "jev_gate", "llm_direct", "jev_resume_filter"] = "llm"
    # `llm` sends every live request to the planner; the planner owns cardinality.
    planning_gate_provider: Literal["llm", "rules", "jev_cardinality", "jev_front"] = "llm"
    jev_api_key: SecretStr | None = None
    jev_base_url: str = "https://api.typesafe.ai/v1"
    jev_model_name: str = "jev-1.14.0"
    jev_timeout_seconds: float = Field(default=1.5, gt=0, le=25)
    jev_min_confidence: float = Field(default=0.8, ge=0, le=1)
    jev_min_probability: float = Field(default=0.9, ge=0, le=1)
    memory_history_messages: int = 12
    memory_history_characters: int = 12000
    freshness_context_seconds: float = Field(default=1800, gt=0, le=86400)
    freshness_selection_seconds: float = Field(default=300, gt=0, le=3600)
    freshness_approval_seconds: float = Field(default=120, gt=0, le=600)
    freshness_clarification_seconds: float = Field(default=1800, gt=0, le=86400)
    freshness_weather_seconds: float = Field(default=300, gt=0, le=3600)
    freshness_places_seconds: float = Field(default=300, gt=0, le=3600)
    freshness_route_seconds: float = Field(default=120, gt=0, le=600)
    freshness_music_seconds: float = Field(default=1800, gt=0, le=86400)
    model_base_url: str | None = None
    model_api_key: str | None = None
    model_name: str | None = None
    amap_maps_api_key: str | None = None
    old_intent_url: str | None = None
    old_reject_url: str | None = None
    demo_user_id: str = "demo-user"
    cors_origins: str = (
        "http://localhost:5173,http://localhost:5175,"
        "http://127.0.0.1:5173,http://127.0.0.1:5175"
    )

    @property
    def database_file(self) -> Path:
        path = Path(self.database_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def origins(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

    @property
    def chroma_directory(self) -> Path:
        path = Path(self.chroma_path).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
