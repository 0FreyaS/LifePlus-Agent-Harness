from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .structured_output import invoke_structured
from .intent_schema import SlotValues


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    query: str = Field(min_length=1, max_length=4000)
    domain: Literal["weather", "navigation", "music", "planning", "wellbeing", "local_life", "chat"]
    action: str
    slots: dict[str, Any] = Field(default_factory=dict, json_schema_extra=SlotValues.model_json_schema())
    depends_on: list[str] = Field(default_factory=list)
    bind_slots: dict[str, str] = Field(default_factory=dict)

    @field_validator("slots")
    @classmethod
    def validate_slots(cls, values):
        return SlotValues.model_validate(values).model_dump(exclude_none=True)

    @model_validator(mode="after")
    def validate_action(self):
        allowed = {
            "planning": {"create_reminder", "save_itinerary", "list_reminders", "list_itineraries", "cancel_reminder"},
            "local_life": {"local_search", "local_detail", "local_route"},
        }
        if self.domain in allowed and self.action not in allowed[self.domain]:
            raise ValueError("子任务动作不属于该领域支持的动作集合")
        if self.domain in {"navigation", "local_life"} and self.slots.get("travel_mode") is not None:
            if not isinstance(self.slots["travel_mode"], str) or self.slots["travel_mode"] not in {"driving", "walking", "bicycling"}:
                raise ValueError("travel_mode 必须为 driving/walking/bicycling，步行用 walking，不能用 walk")
        return self


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[PlannedTask] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_dependencies(self):
        ids = {item.id for item in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("子任务ID必须唯一")
        if any(len(item.depends_on) != len(set(item.depends_on)) for item in self.tasks):
            raise ValueError("任务依赖不能重复")
        resolved: set[str] = set()
        ordered: list[PlannedTask] = []
        while len(resolved) < len(ids):
            ready = {item.id for item in self.tasks if item.id not in resolved
                     and set(item.depends_on) <= resolved}
            if not ready:
                raise ValueError("任务依赖有环或引用了不存在的任务")
            ordered.extend(item for item in self.tasks if item.id in ready)
            resolved.update(ready)
        self.tasks = ordered
        for item in self.tasks:
            for target, path in item.bind_slots.items():
                if target not in SlotValues.model_fields:
                    raise ValueError("槽位绑定目标不属于允许的业务字段")
                parts = path.split(".")
                if parts[0] not in item.depends_on:
                    raise ValueError("槽位绑定只能引用直接依赖的任务")
                if len(parts) < 2 or parts[1] not in {"selected", "route", "result", "slots"}:
                    raise ValueError("绑定路径必须引用 selected/route/result/slots 中的执行证据")
        return self


def may_have_multiple_tasks(query: str) -> bool:
    return bool(re.search(r"同时|并且|并(?=查|搜|播|保|提|导)|顺便|然后|以及|还要|另外|接着|再帮|和|[，,；;]", query))


def compact_schema(value: Any) -> Any:
    """Remove descriptive duplication, not validation constraints or enum choices."""
    if isinstance(value, list):
        return [compact_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: ({name: compact_schema(spec) for name, spec in item.items()}
                  if key == "properties" else compact_schema(item)) for key, item in value.items()
            if key not in {"title", "description", "default"}}


def planning_prompt(memory_brief: str = "", *, variant: str = "baseline", now: datetime | None = None) -> str:
    if variant not in {"baseline", "compact"}:
        raise ValueError("Unknown planning variant")
    schema = TaskPlan.model_json_schema()
    contract = (json.dumps(compact_schema(schema), ensure_ascii=False, separators=(",", ":"))
                if variant == "compact" else json.dumps(schema, ensure_ascii=False))
    return (
        "将用户的请求拆为可执行子任务。只保留用户明确要求的事情，最多6个，不增加任务。"
        "单一任务仍返回只有一项的tasks，坐标逗号、地点名称和普通修饰语不构成独立任务。"
        "独立任务depends_on=[]，不能因为用户说同时或顺便就添加依赖。"
        "只有需要前一结果的任务才设depends_on。不要为未知地点或时间猜值，缺失slots留空交给澄清。"
        "action按领域取值：weather_lookup/navigation_search/music_search/knowledge_search/chat_answer；"
        "planning取create_reminder/save_itinerary/list_reminders/list_itineraries/cancel_reminder。"
        "本地商家检索用local_life/local_search，slots含category(restaurant/ktv/cinema/cafe)、anchor、city、keyword、radius_m、sort_by、min_rating、max_cost。"
        "附近没有参考地点时anchor留空；不猜定位。已指定商家ID查详情用local_detail，slots.poi_id；商家ID规划路线用local_route，slots.poi_id/anchor/travel_mode。"
        "slots字段：天气city/forecast_date（必须转为YYYY-MM-DD，禁止返回today等相对文本）；导航origin/destination/city/travel_mode（仅driving/walking/bicycling，步行必须为walking）；音乐keyword；"
        "提醒title/remind_at；行程title/details；取消提醒reminder_id。"
        "query必须是该项完整、独立的用户请求，明确引用的城市和时间应保留。"
        "路线子任务也必须保留用户明确说的出行方式；骑行/骑车对应bicycling，步行/走路对应walking，驾车/开车对应driving，不得擅自改为驾车。"
        "依赖输出绑定用bind_slots，例如destination='t1.selected.location'，details='t1.route'。"
        "可引用selected(用户选中的地点/歌曲)、route(路线回执)、result(工具结果)、slots。"
        "用户说先找地点再导航过去：可保留为一个navigation_search子任务，不必分开。"
        "输出只包含符合JSON Schema的JSON：" + contract
        + "\n当前北京时间：" + (now or datetime.now(timezone(timedelta(hours=8)))).isoformat()
        + "\n会话摘要：" + memory_brief
    )


async def plan_tasks(query: str, mode: str, llm: Any, memory_brief: str = "", *,
                     variant: str = "baseline", now: datetime | None = None) -> TaskPlan | None:
    from .conversation import preference_command, asks_first_input
    if preference_command(query) or asks_first_input(query):
        return None
    if not may_have_multiple_tasks(query):
        return None
    if mode != "live":
        from .harness import infer

        parts = [part.strip() for part in re.split(r"(?:同时|并且|顺便|然后|以及|还要|另外|接着|[，；;])", query) if part.strip()]
        if len(parts) < 2:
            return None
        return TaskPlan(tasks=[PlannedTask(id=f"t{index}", query=part, **infer(part))
                               for index, part in enumerate(parts[:6], 1)])
    if llm is None:
        raise RuntimeError("多任务规划需要配置真实模型")
    prompt = planning_prompt(memory_brief, variant=variant, now=now)
    from .harness import emit

    result = await invoke_structured(llm, TaskPlan, [
        {"role": "system", "content": prompt}, {"role": "user", "content": query},
    ], stage="plan", report=emit)
    return TaskPlan.model_validate(result.model_dump())
