from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from evaluations.dataset import build_cases
from evaluations.run import error_metrics, expected_slots, percentile, score_decision, score_plan, summarize, wilson
from evaluations.runtime import RuntimeLab
from car_agent.harness import explicit_route_endpoints, resolve_intent, wants_route


def test_dataset_has_30_per_scenario_and_unique_queries():
    cases = build_cases()
    assert len(cases) == 240
    assert set(Counter(case["group"] for case in cases).values()) == {30}
    assert len({case["id"] for case in cases}) == 240
    for group in {case["group"] for case in cases}:
        queries = [case["query"] for case in cases if case["group"] == group]
        assert len(set(queries)) == 30


def test_slot_scoring_does_not_let_missing_or_wrong_units_pass():
    case = {"expected": {"domain": "local_life", "slots": {"radius_m": 2000, "anchor": None}}}
    anchor = datetime.now(timezone.utc)
    assert not score_decision(case, {"domain": "local_life", "slots": {"radius_m": 2}}, anchor)[0]
    assert not score_decision(case, {"domain": "local_life", "slots": {"radius_m": 2000, "anchor": "猜测定位"}}, anchor)[0]
    assert score_decision(case, {"domain": "local_life", "slots": {"radius_m": 2000}}, anchor)[0]


def test_multi_task_scoring_checks_duplicate_domains_and_dependency_bindings():
    case = {"expected": {"domains": ["weather", "planning"], "dependency": "required"}}
    tasks = [{"id": "t1", "domain": "weather"}, {"id": "t2", "domain": "planning", "depends_on": ["t1"]}]
    assert not score_plan(case, tasks)[0]
    tasks[1]["bind_slots"] = {"details": "t1.result"}
    assert score_plan(case, tasks)[0]
    tasks.append({"id": "t3", "domain": "weather"})
    assert not score_plan(case, tasks)[0]


def test_model_errors_stay_in_metric_denominators():
    case = {"id": "s1", "group": "single_task", "expected": {"domain": "weather", "action": "weather_lookup"}}
    rows = [{"id": "s1", "group": "single_task", "passed": False, "elapsed_ms": 25000, "metrics": error_metrics(case)}]
    report = summarize(rows, [case], {})
    assert report["groups"]["single_task"]["metrics"]["intent_exact"] == {"numerator": 0, "denominator": 1, "value": 0}
    assert percentile(list(range(1, 31)), .95) == 29
    assert 0.88 < wilson(30, 30)[0] < .89


def test_relative_dates_have_explicit_reference_time():
    anchor = datetime.fromisoformat("2026-10-23T14:00:00+08:00")
    assert expected_slots({"forecast_date": "$tomorrow", "remind_at": "$day_after15"}, anchor) == {
        "forecast_date": "2026-10-24", "remind_at": "2026-10-25T15:00:00+08:00"}


@pytest.mark.asyncio
@pytest.mark.parametrize("domain,alias,canonical", [("weather", "get_weather", "weather_lookup"), ("chat", "self_introduction", "chat_answer")])
async def test_readonly_domain_uses_canonical_action(domain, alias, canonical):
    class LLM:
        def with_structured_output(self, schema, **_):
            class Router:
                async def ainvoke(self, _):
                    return schema.model_validate({"domain": domain})
            return Router()
    from car_agent.harness import resolve_domain_intent
    decision = await resolve_domain_intent({"original_query": "无锡天气", "domain": domain, "mode": "live",
        "prepared_decision": {"domain": domain, "action": alias, "slots": {"city": "无锡"}}}, {},
        SimpleNamespace(context=SimpleNamespace(llm=LLM())))
    assert decision["action"] == canonical


@pytest.mark.asyncio
async def test_null_slots_for_chat_are_treated_as_no_parameters():
    class LLM:
        def with_structured_output(self, schema, **_):
            class Router:
                async def ainvoke(self, _):
                    return schema.model_validate({"domain": "chat"})
            return Router()
    result = await resolve_intent({"original_query": "讲故事", "mode": "live"}, {}, SimpleNamespace(context=SimpleNamespace(llm=LLM())))
    from car_agent.harness import resolve_domain_intent
    result = await resolve_domain_intent({"original_query": "讲故事", "mode": "live", **result}, {}, SimpleNamespace(context=SimpleNamespace(llm=LLM())))
    assert result["slots"] == {}
    assert result["action"] == "chat_answer"


@pytest.mark.parametrize("query,origin,destination", [
    ("从杭州东站步行到西湖", "杭州东站", "西湖"),
    ("从苏州站骑行去拙政园", "苏州站", "拙政园"),
    ("从上海虹桥站开车到外滩", "上海虹桥站", "外滩"),
    ("从南京站驾车到南京南站", "南京站", "南京南站"),
    ("从无锡步行街到无锡站", "无锡步行街", "无锡站"),
])
def test_explicit_route_does_not_include_transport_verb_in_origin(query, origin, destination):
    assert explicit_route_endpoints(query) == (origin, destination)
    assert wants_route(query)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["save_itinerary", "create_reminder", "cancel_reminder"])
async def test_wrong_confirmation_key_reinterrupts_then_executes_once(action):
    async with RuntimeLab() as lab:
        result = await lab.recovery({"id": "wrong-key-" + action, "action": action, "expected": {"scenario": "bad_key"}})
        assert result == {"reconfirmation": True, "writes": 1}
