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
    action: str = ""
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
        if self.action and self.domain in allowed and self.action not in allowed[self.domain]:
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


class TaskGoal(BaseModel):
    """Global goals preserve constraints without exposing tool parameter schemas."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    query: str = Field(min_length=1, max_length=4000)
    domain: Literal["weather", "navigation", "music", "planning", "wellbeing", "local_life", "chat"]
    depends_on: list[str] = Field(default_factory=list)
    bind_slots: dict[str, str] = Field(default_factory=dict)


class GoalPlan(TaskPlan):
    tasks: list[TaskGoal] = Field(min_length=1, max_length=6)


def may_have_multiple_tasks(query: str) -> bool:
    return bool(re.search(r"同时|并且|并(?=查|搜|播|保|提|导)|顺便|然后|以及|还要|另外|接着|再帮|和|[，,；;。\n、]", query)
                or len(re.findall(r"查(?:询)?|搜索|播放|导航|提醒我|保存|列出|找", query)) >= 2)


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
    schema = GoalPlan.model_json_schema()
    contract = (json.dumps(compact_schema(schema), ensure_ascii=False, separators=(",", ":"))
                if variant == "compact" else json.dumps(schema, ensure_ascii=False))
    return (
        "将用户的请求拆为可执行子任务。只保留用户明确要求的事情，最多6个，不增加任务。"
        "单一任务仍返回只有一项的tasks，坐标逗号、地点名称和普通修饰语不构成独立任务。"
        "只负责目标拆分、领域分派和依赖关系，不输出action、slots或工具参数。"
        "独立任务depends_on=[]。然后只是话语顺序，不自动构成依赖；同时或顺便不构成依赖。"
        "只有需要前置结果，或用户明确要求等待前置成功时才设depends_on。数据依赖还需bind_slots，纯控制依赖可以没有绑定。"
        "domain取weather/navigation/music/planning/wellbeing/local_life/chat。"
        "weather查天气；music歌曲检索；planning负责创建取消查询提醒、保存查询行程；wellbeing仅健康知识检索。"
        "local_life负责商家检索、详情及商家路线；navigation负责一般地点路线；chat负责闲聊与歌手介绍等知识问答。"
        "query必须保留本项目标、城市、时间、起终点、出行方式、否定及筛选限制。共享城市时间要传递到相关子任务，不增加用户未表达的信息。"
        "每个明确目标恰好覆盖一次；未知地点、时间和ID交给领域澄清，不猜值。"
        "依赖输出绑定用bind_slots，例如destination='t1.selected.location'，details='t1.route'。"
        "可引用selected(用户选中的地点/歌曲)、route(路线回执)、result(工具结果)、slots。"
        "用户说先找地点再导航过去：可保留为一个navigation领域目标，由领域子图完成检索选择和路线。"
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
        return TaskPlan(tasks=[PlannedTask(id=f"t{index}", query=part, domain=infer(part)["domain"])
                               for index, part in enumerate(parts[:6], 1)])
    if llm is None:
        raise RuntimeError("多任务规划需要配置真实模型")
    prompt = planning_prompt(memory_brief, variant=variant, now=now)
    from .harness import emit

    result = await invoke_structured(llm, GoalPlan, [
        {"role": "system", "content": prompt}, {"role": "user", "content": query},
    ], stage="plan", report=emit)
    return TaskPlan.model_validate(result.model_dump())
