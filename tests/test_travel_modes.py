from types import SimpleNamespace

import pytest

from car_agent.harness import infer, resolve_domain_intent
from car_agent.travel_modes import explicit_travel_mode, mode_choices


@pytest.mark.parametrize("query,expected", [
    ("从杭州东站骑行到西湖", "bicycling"), ("骑车去东南大学", "bicycling"),
    ("骑自行车到学校", "bicycling"), ("从杭州东站步行到西湖", "walking"),
    ("走路去西湖", "walking"), ("开车去西湖", "driving"), ("自驾到西湖", "driving"),
    ("不要开车，给我骑行路线", "bicycling"), ("骑行，不是驾车", "bicycling"),
    ("不想再骑行，步行到西湖", "walking"), ("驾车改成骑行", "bicycling"),
    ("步行改为驾车", "driving"), ("cycling to West Lake", "bicycling"),
    ("walking to West Lake", "walking"), ("导航到西湖", None),
    ("查一下步行街的位置", None), ("不要骑行", None),
    ("骑行和驾车哪个更快", None), ("骑行还是步行比较方便？", None),
    ("骑行现在比驾车快吗", None),
])
def test_only_positive_unambiguous_current_query_modes_become_defaults(query, expected):
    assert explicit_travel_mode(query) == expected


@pytest.mark.parametrize("mode", ["bicycling", "walking", "driving"])
def test_mode_choices_keep_all_supported_modes_and_mark_exactly_one_default(mode):
    choices = mode_choices(mode)
    assert choices[0]["travel_mode"] == mode and choices[0]["is_default"]
    assert sum(choice["is_default"] for choice in choices) == 1
    assert {choice["travel_mode"] for choice in choices} == {"bicycling", "walking", "driving"}


@pytest.mark.asyncio
@pytest.mark.parametrize("domain,action,query,expected", [
    ("navigation", "navigation_search", "从杭州东站骑行到西湖", "bicycling"),
    ("navigation", "navigation_search", "从杭州东站步行到西湖", "walking"),
    ("navigation", "navigation_search", "从杭州东站驾车到西湖", "driving"),
    ("local_life", "local_route", "骑行到这家饭店", "bicycling"),
])
async def test_explicit_transport_overrides_wrong_model_or_planner_slot(domain, action, query, expected):
    state = {"original_query": query, "domain": domain, "mode": "live", "memory_brief": "之前偏好驾车，不代表当前要求。",
             "prepared_decision": {"domain": domain, "action": action, "slots": {"travel_mode": "driving"}}}
    result = await resolve_domain_intent(state, {}, SimpleNamespace(context=SimpleNamespace(llm=None)))
    assert result["slots"]["travel_mode"] == expected


@pytest.mark.asyncio
async def test_a_route_without_an_explicit_mode_does_not_reset_a_valid_selected_mode():
    result = await resolve_domain_intent({"original_query": "导航到西湖", "domain": "navigation", "mode": "live", "prepared_decision": {
        "domain": "navigation", "action": "navigation_search", "slots": {"travel_mode": "bicycling"}}}, {},
        SimpleNamespace(context=SimpleNamespace(llm=None)))
    assert result["slots"]["travel_mode"] == "bicycling"


def test_demo_inference_understands_a_cycling_request_without_the_word_navigation():
    result = infer("骑车去杭州西湖")
    assert result["domain"] == "navigation" and result["slots"]["travel_mode"] == "bicycling"
