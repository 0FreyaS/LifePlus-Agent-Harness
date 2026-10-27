"""Shared model-facing slots; ownership and candidate selection stay host-owned."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SlotValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: str | None = Field(default=None, description="用户明确提及的城市，例如无锡")
    forecast_date: str | None = Field(default=None, description="天气查询日期 YYYY-MM-DD")
    origin: str | None = None
    destination: str | None = None
    travel_mode: Literal["driving", "walking", "bicycling"] | None = None
    keyword: str | None = Field(default=None, description="歌曲名或歌手名，去掉命令词")
    title: str | None = None
    remind_at: str | None = None
    reminder_id: str | None = None
    details: dict[str, Any] | None = None
    category: Literal["restaurant", "ktv", "cinema", "cafe"] | None = None
    anchor: str | None = Field(default=None, description="搜索参考地点或坐标，禁止猜测定位")
    poi_id: str | None = None
    radius_m: int | None = Field(default=None, ge=200, le=50000)
    sort_by: Literal["distance", "rating"] | None = None
    min_rating: float | None = Field(default=None, ge=0, le=5)
    max_cost: float | None = Field(default=None, gt=0, le=10000)
    route_requested: bool | None = None
    play: bool | None = None


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: Literal["weather", "navigation", "music", "wellbeing", "planning", "local_life", "chat"]
    action: str | None = None
    intent: str | None = None
    slots: SlotValues = Field(default_factory=SlotValues)

    @field_validator("slots", mode="before")
    @classmethod
    def empty_slots(cls, value):
        return {} if value is None else value
