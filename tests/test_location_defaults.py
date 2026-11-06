import json
from pathlib import Path

import pytest

from car_agent.config import get_settings
from car_agent.location_defaults import DEFAULT_CURRENT_LOCATION, with_default_location


LOCATION_POLICY_CASES = json.loads((Path(__file__).parent / "fixtures/location-policy-20261106.json").read_text())


@pytest.mark.parametrize("origin", [None, "", "这里", "当前位置"])
def test_navigation_uses_configured_campus_when_origin_unspecified(origin):
    initial = {"origin": origin, "destination": "南京站", "city": "南京市", "route_requested": True}
    result = with_default_location("navigation", "navigation_search", initial)
    assert result["origin"] == DEFAULT_CURRENT_LOCATION
    assert result["destination"] == "南京站"
    assert result["city"] == "南京市"
    assert initial["origin"] == origin


def test_explicit_origin_and_destination_always_win():
    values = {"origin": "上海虹桥站", "destination": "苏州站", "route_requested": True}
    assert with_default_location("navigation", "navigation_search", values) == values


def test_nearby_search_uses_unified_default_or_explicit_city_center():
    result = with_default_location("local_life", "local_search", {"category": "restaurant"})
    assert result["anchor"] == get_settings().default_current_location
    assert result["city"] == "无锡市"
    explicit_city = {"category": "restaurant", "city": "南京市"}
    resolved_city = with_default_location("local_life", "local_search", explicit_city)
    assert resolved_city["anchor"] == "南京市中心"
    assert resolved_city["city"] == "南京市"
    explicit_anchor = {"category": "cinema", "anchor": "无锡站"}
    assert with_default_location("local_life", "local_search", explicit_anchor) == explicit_anchor


def test_destination_never_becomes_the_current_location():
    first = with_default_location("navigation", "navigation_search", {"destination": "南京站", "route_requested": True})
    next_request = with_default_location("navigation", "navigation_search", {"destination": "苏州站", "route_requested": True})
    assert first["origin"] == next_request["origin"] == get_settings().default_current_location


def test_empty_location_setting_falls_back_to_product_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "default_current_location", "")
    monkeypatch.setattr(get_settings(), "default_current_city", "")
    values = {"destination": "西湖", "route_requested": True}
    resolved = with_default_location("navigation", "navigation_search", values)
    assert resolved["origin"] == DEFAULT_CURRENT_LOCATION
    assert resolved["destination"] == "西湖"
    nearby = with_default_location("local_life", "local_search", {"category": "cinema"})
    assert nearby["anchor"] == DEFAULT_CURRENT_LOCATION
    assert nearby["city"] == "无锡市"


@pytest.mark.parametrize("case", LOCATION_POLICY_CASES, ids=lambda case: case["id"])
def test_frozen_location_policy_cases(case):
    resolved = with_default_location(case["domain"], case["action"], case["slots"])
    for field, expected in case.get("expected", {}).items():
        assert resolved[field] == expected
    for field in case.get("expected_absent", []):
        assert field not in resolved
