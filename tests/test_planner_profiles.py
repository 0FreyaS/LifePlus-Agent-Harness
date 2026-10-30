import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from car_agent.config import Settings
from car_agent.model_roles import build_models, select_model
from car_agent.runtime_context import RunContext
from car_agent.task_planning import GoalPlan, TaskPlan, compact_schema, plan_tasks, planning_prompt
from evaluations.planner_ab import summarize
from evaluations.planner_cases import CASES, grade


def test_baseline_is_default_and_roles_are_opt_in():
    settings = Settings(_env_file=None, model_base_url="http://unused", model_api_key="test", model_name="test")
    assert settings.planner_variant == "baseline"
    base, roles = build_models(settings)
    assert roles == {} and base.max_tokens == settings.model_max_output_tokens
    assert base.max_retries == 0
    assert base.stream_usage is True


def test_compact_has_separate_planner_client_but_same_model():
    settings = Settings(_env_file=None, planner_variant="compact", model_base_url="http://unused", model_api_key="test", model_name="same-model")
    base, roles = build_models(settings)
    assert list(roles) == ["planner"]
    assert roles["planner"].model_name == base.model_name
    assert roles["planner"].max_tokens == 1536
    assert base.max_tokens == 2048


def test_model_roles_can_be_configured_independently():
    settings = Settings(_env_file=None, model_base_url="http://unused", model_api_key="test", model_name="base",
                        intent_model_name="router", response_model_name="writer", response_max_output_tokens=1000)
    base, roles = build_models(settings)
    assert roles["intent"].model_name == "router"
    assert roles["response"].model_name == "writer" and roles["response"].max_tokens == 1000
    assert base.model_name == "base"
    assert build_models(Settings(_env_file=None, model_api_key="", model_name="")) == (None, {})


def test_context_role_fallback_preserves_runtime_model_injection():
    context = SimpleNamespace(model_roles={}, llm=object())
    assert RunContext.model_for(context, "intent") is context.llm
    other = object()
    context.model_roles["planner"] = other
    assert RunContext.model_for(context, "planner") is other
    assert RunContext.model_for(context, "response") is context.llm
    assert select_model(SimpleNamespace(llm=other), "intent") is other


def test_compact_preserves_business_title_and_all_constraints():
    schema = TaskPlan.model_json_schema()
    compact = compact_schema(schema)
    full_slots = schema["$defs"]["PlannedTask"]["properties"]["slots"]["properties"]
    short_slots = compact["$defs"]["PlannedTask"]["properties"]["slots"]["properties"]
    assert short_slots.keys() == full_slots.keys()
    assert "title" in short_slots
    assert short_slots["travel_mode"]["anyOf"][0]["enum"] == ["driving", "walking", "bicycling"]
    assert short_slots["radius_m"]["anyOf"][0]["minimum"] == 200
    assert compact["properties"]["tasks"]["maxItems"] == 6
    assert compact["$defs"]["PlannedTask"]["additionalProperties"] is False
    assert len(json.dumps(compact)) < len(json.dumps(schema)) * .8


def test_prompt_business_rules_and_clock_are_identical():
    now = datetime.fromisoformat("2026-10-27T10:00:00+08:00")
    a = planning_prompt("fixture", now=now)
    b = planning_prompt("fixture", variant="compact", now=now)
    marker = "输出只包含符合JSON Schema的JSON："
    assert a.split(marker)[0] == b.split(marker)[0]
    assert a.split("\n当前北京时间：")[1] == b.split("\n当前北京时间：")[1]
    with pytest.raises(ValueError):
        planning_prompt(variant="unknown")


class Model:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def with_structured_output(self, schema, **kwargs):
        assert schema is GoalPlan and kwargs["method"] == "json_mode"
        parent = self

        class Call:
            async def ainvoke(self, messages):
                parent.calls.append(messages)
                return next(parent.results)
        return Call()


@pytest.mark.parametrize("variant", ["baseline", "compact"])
async def test_invalid_binding_repaired_before_plan_can_execute(variant):
    valid = {"tasks": [{"id": "w", "query": "查无锡天气", "domain": "weather"}]}
    invalid = {"tasks": [{**valid["tasks"][0], "bind_slots": {"city": "missing.result"}}]}
    model = Model([invalid, valid])
    plan = await plan_tasks("查无锡天气，谢谢", "live", model, variant=variant)
    assert plan.tasks[0].id == "w" and len(model.calls) == 2


@pytest.mark.parametrize("variant", ["baseline", "compact"])
async def test_illegal_slots_still_rejected_after_one_repair(variant):
    invalid = {"tasks": [{"id": "r", "query": "导航", "domain": "navigation", "action": "navigation_search",
                          "slots": {"travel_mode": "plane", "authorized": True}}]}
    model = Model([invalid, invalid])
    with pytest.raises(ValidationError):
        await plan_tasks("导航，同时查天气", "live", model, variant=variant)
    assert len(model.calls) == 2


@pytest.mark.parametrize("variant", ["baseline", "compact"])
@pytest.mark.parametrize("query", ["查无锡天气", "返回我第一次输入", "记住我喜欢步行"])
async def test_fast_path_does_not_call_planner(variant, query):
    model = Model([])
    assert await plan_tasks(query, "live", model, variant=variant) is None
    assert model.calls == []


async def test_llm_planning_mode_checks_single_task_without_rule_prefilter():
    from car_agent.planning_gate import LLMPlanningGate

    model = Model([{"tasks": [{"id": "weather", "query": "查无锡天气", "domain": "weather"}]}])
    plan = await plan_tasks("查无锡天气，谢谢", "live", model, gate=LLMPlanningGate())
    assert plan is not None and len(plan.tasks) == 1
    assert len(model.calls) == 1


def test_grading_ignores_task_ids_and_order_but_not_dependencies():
    case = next(case for case in CASES if case["id"] == "dependency01")
    plan = TaskPlan.model_validate({"tasks": [
        {"id": "save", "domain": "planning", "action": "save_itinerary", "query": "保存天气结果到行程",
         "depends_on": ["weather"], "bind_slots": {"details": "weather.result"}},
        {"id": "weather", "domain": "weather", "action": "weather_lookup", "query": "查无锡明天天气",
         "slots": {"city": "无锡市", "forecast_date": "2026-10-28"}},
    ]})
    assert grade(case, plan)["passed"]
    plan.tasks[1].bind_slots = {}
    assert not grade(case, plan)["passed"]


def test_faster_but_wrong_candidate_cannot_pass_gate():
    rows = []
    for variant, ms in [("baseline", 1000), ("compact", 400)]:
        rows.append({"variant": variant, "case_id": "c", "repeat": 0, "group": "g", "elapsed_ms": ms,
                     "grade": {"passed": variant == "baseline", "errors": []},
                     "attempts": [{"usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}}]})
    result = summarize(rows, {"cases": 1, "repeats": 1})
    assert not result["recommend_enable"]
    assert result["new_failures"] == ["c:0"]


def test_fast_connection_failures_are_not_a_performance_win():
    rows = [{"variant": arm, "case_id": "c", "repeat": 0, "group": "g", "elapsed_ms": ms,
             "error": "OpenAIConnectionError", "grade": {"passed": False}, "attempts": []}
            for arm, ms in [("baseline", 10), ("compact", 1)]]
    result = summarize(rows, {"cases": 1, "repeats": 1})
    assert not result["gate_checks"]["transport_valid"] and not result["recommend_enable"]
