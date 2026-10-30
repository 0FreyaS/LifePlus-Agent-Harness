from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from car_agent.freshness import FreshnessPolicy
from car_agent.input_events import event_messages
from car_agent.orchestration import TaskCoordinator
from car_agent.rag import KnowledgeRetriever
from car_agent.task_planning import GoalPlan


def shape(target, source):
    return {"tasks": [{"id": "search", "domain": "local_life", "query": "查饭店"},
        {"id": "route", "domain": "navigation", "query": "从无锡站到选中的饭店", "depends_on": ["search"], "bind_slots": {target: source}}]}


@pytest.mark.parametrize("target,source,kind", [
    ("destination", "search.selected", "binding_source_shape"),
    ("origin", "search.slots.origin", "binding_source_field"),
    ("city", "search.result", "binding_source_shape"),
])
def test_impossible_binding_is_rejected_before_tools(target, source, kind):
    with pytest.raises(ValidationError) as error:
        GoalPlan.model_validate(shape(target, source))
    assert error.value.errors()[0]["type"] == kind


def test_real_coordinate_path_is_allowed_without_extracting_global_slots():
    plan = GoalPlan.model_validate(shape("destination", "search.selected.location"))
    assert plan.tasks[1].bind_slots == {"destination": "search.selected.location"}
    assert not hasattr(plan.tasks[1], "slots")


async def bind(receipt, path="s.result", field="details"):
    class Graph:
        async def aget_state(self, config):
            return SimpleNamespace(values={"tool_result": receipt, "slots": {"selection": 1}})
    coordinator = TaskCoordinator(SimpleNamespace(state=SimpleNamespace(db=None, graph=Graph())), None)
    return await coordinator._bind({"domain": "planning" if field == "details" else "navigation", "bind_slots": {field: path}},
                                   {"s": {"thread_id": "source"}})


@pytest.mark.asyncio
async def test_expired_receipt_and_nested_route_are_rejected_before_binding():
    old = FreshnessPolicy().receipt("maps_weather", {"city": "无锡"}, "2000-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="过期"):
        await bind(old)
    route = FreshnessPolicy().receipt("maps_route", {"distance_m": 200}, "2000-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="过期"):
        await bind({"items": [{"name": "西湖"}], "route_result": route}, "s.route")


@pytest.mark.asyncio
async def test_fresh_receipt_preserves_actual_data_and_freshness():
    real = FreshnessPolicy().receipt("maps_weather", {"city": "无锡", "day_weather": "晴"})
    result = await bind(real)
    assert result["bound_slots"]["details"] == real


@pytest.mark.asyncio
async def test_runtime_checks_selected_shape_existence_and_receiver_type():
    receipt = {"items": [{"id": "poi-1", "location": "120.1,31.2"}]}
    assert (await bind(receipt, "s.selected.location", "destination"))["bound_slots"]["destination"] == "120.1,31.2"
    with pytest.raises(ValueError, match="类型不兼容"):
        await bind(receipt, "s.selected", "destination")
    with pytest.raises(ValueError, match="未提供绑定字段"):
        await bind(receipt, "s.selected.unknown", "destination")
    with pytest.raises(ValueError, match="有效绑定证据"):
        await bind({"items": []}, "s.selected", "details")


def test_event_prompt_distinguishes_unique_cancel_and_quoted_language():
    prompt = event_messages("测试", [], [])[0]["content"]
    assert "应cancel该唯一task_id" in prompt
    assert "不能把词义提问当选第二项" in prompt
    assert "approval回复应clarify" in prompt


def test_missing_knowledge_mount_never_clears_existing_collection(tmp_path):
    reader = KnowledgeRetriever(tmp_path / "index", tmp_path / "missing")
    with pytest.raises(FileNotFoundError, match="refusing to clear"):
        reader.open()
