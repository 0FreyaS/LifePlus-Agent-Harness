import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from car_agent.domain_understanding import candidates, decision_schema, messages
from car_agent.harness import resolve_domain_intent, resolve_intent
from car_agent.task_planning import GoalPlan, may_have_multiple_tasks, planning_prompt


def test_global_plan_has_no_tool_or_slot_schema_but_keeps_dependency_contract():
    schema = GoalPlan.model_json_schema()["$defs"]["TaskGoal"]["properties"]
    assert set(schema) == {"id", "query", "domain", "depends_on", "bind_slots"}
    assert "forecast_date" not in planning_prompt()
    with pytest.raises(ValidationError):
        GoalPlan.model_validate({"tasks": [{"id": "a", "domain": "weather", "query": "查天气", "slots": {"city": "杭州"}}]})
    with pytest.raises(ValidationError):
        GoalPlan.model_validate({"tasks": [{"id": "a", "domain": "weather", "query": "查天气", "depends_on": ["a"]}]})


@pytest.mark.parametrize("separator", [" ", "\n", "。", "、"])
def test_implicit_multiple_commands_enter_global_planning(separator):
    assert may_have_multiple_tasks("查杭州天气" + separator + "搜索周杰伦歌曲")


def test_domain_candidates_hide_host_fields_and_unrelated_tools():
    tools = candidates("planning")
    assert {t["tool_name"] for t in tools} == {"create_reminder", "save_itinerary", "cancel_reminder", "list_reminders", "list_itineraries"}
    assert all("user_id" not in t["argument_schema"]["properties"] and "idempotency_key" not in t["argument_schema"]["properties"] for t in tools)
    schema = decision_schema("weather")
    with pytest.raises(ValidationError):
        schema.model_validate({"action": "weather_lookup", "tool_name": "search_music", "slots": {"city": "杭州"}})
    with pytest.raises(ValidationError):
        schema.model_validate({"action": "weather_lookup", "tool_name": "maps_weather", "slots": {"keyword": "周杰伦"}})
    schema = decision_schema("planning")
    with pytest.raises(ValidationError):
        schema.model_validate({"action": "list_reminders", "tool_name": "cancel_reminder", "slots": {}})


def test_planning_prompt_preserves_cancel_action_when_reminder_id_is_missing():
    schema = decision_schema("planning")
    prompt = messages("planning", {"original_query": "取消一个提醒，但我还不知道提醒ID"}, schema,
                      "2026-10-31T10:00:00+08:00")[0]["content"]
    assert "缺少参数不能改变用户动作" in prompt
    assert "不能改选list_reminders" in prompt
    assert "reminder_id留空" in prompt


class Model:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def with_structured_output(self, schema, **kwargs):
        outer = self

        class Call:
            async def ainvoke(self, messages):
                outer.calls.append((schema.__name__, messages))
                return outer.payload
        return Call()


def runtime(model):
    return SimpleNamespace(context=SimpleNamespace(llm=model, model_roles={}))


async def test_prepared_domain_routes_without_model_then_domain_extracts():
    model = Model({"action": "weather_lookup", "tool_name": "maps_weather", "slots": {"city": "杭州"}})
    state = {"original_query": "查杭州天气", "mode": "live", "prepared_decision": {"domain": "weather"}}
    state.update(await resolve_intent(state, {}, runtime(model)))
    assert not model.calls and "slots" not in state
    result = await resolve_domain_intent(state, {}, runtime(model))
    assert result["slots"]["city"] == "杭州"
    assert [name for name, _ in model.calls] == ["WeatherExecutionDecision"]
    assert "search_music" not in model.calls[0][1][0]["content"]


async def test_bound_result_overrides_model_generated_substitute():
    evidence = {"ok": True, "city": "杭州", "day_weather": "雨"}
    model = Model({"action": "save_itinerary", "tool_name": "save_itinerary", "slots": {"title": "天气记录", "details": {"city": "南京"}}})
    state = {"original_query": "保存天气结果", "domain": "planning", "mode": "live",
             "prepared_decision": {"domain": "planning", "bound_slots": {"details": evidence}}}
    result = await resolve_domain_intent(state, {}, runtime(model))
    assert result["slots"]["details"] == evidence
    assert json.loads(model.calls[0][1][-1]["content"])["bound_slots"]["details"] == evidence


async def test_binding_cannot_add_another_domains_fields():
    model = Model({})
    with pytest.raises(ValueError, match="不属于当前领域"):
        await resolve_domain_intent({"original_query": "查杭州天气", "domain": "weather", "mode": "live",
                                    "prepared_decision": {"domain": "weather", "bound_slots": {"details": {"city": "杭州"}}}}, {}, runtime(model))
    assert not model.calls


def test_goal_grader_checks_dependencies_without_requiring_early_slots():
    from evaluations.planner_cases import CASES, grade_goals

    item = next(item for item in CASES if item["id"] == "dependency01")
    plan = GoalPlan.model_validate({"tasks": [
        {"id": "weather", "domain": "weather", "query": "查无锡明天天气"},
        {"id": "save", "domain": "planning", "query": "保存天气结果到行程", "depends_on": ["weather"], "bind_slots": {"details": "weather.result"}},
    ]})
    assert grade_goals(item, plan)["passed"]
    plan.tasks[1].bind_slots = {}
    assert not grade_goals(item, plan)["passed"]


async def test_cross_domain_tool_proposal_exhausts_one_repair_without_execution():
    model = Model({"action": "weather_lookup", "tool_name": "search_music", "slots": {"city": "杭州"}})
    with pytest.raises(ValidationError):
        await resolve_domain_intent({"original_query": "查杭州天气", "domain": "weather", "mode": "live"}, {}, runtime(model))
    assert len(model.calls) == 2


@pytest.mark.parametrize("kind,expected", [("OpenAIRateLimitError", "RateLimitError"), ("OpenAITimeoutError", "APITimeoutError"), ("ValueError", None)])
async def test_provider_wrapped_failures_preserve_explicit_retry_classification(kind, expected):
    from car_agent.retry import failure_kind

    class DB:
        async def fetchone(self, *args):
            return {"data_json": json.dumps({"error": kind + ": provider failure"})}

    assert await failure_kind(DB(), "task") == expected
