import pytest

from car_agent.config import get_settings
from car_agent.location_defaults import with_default_location


@pytest.mark.parametrize("origin", [None, "", "这里", "当前位置"])
def test_navigation_uses_configured_campus_when_origin_unspecified(origin):
    initial = {"origin": origin, "destination": "南京站", "city": "南京市", "route_requested": True}
    result = with_default_location("navigation", "navigation_search", initial)
    assert result["origin"] == "江苏省无锡市滨湖区东南大学无锡校区"
    assert result["destination"] == "南京站"
    assert result["city"] == "南京市"
    assert initial["origin"] == origin


def test_explicit_origin_and_destination_always_win():
    values = {"origin": "上海虹桥站", "destination": "苏州站", "route_requested": True}
    assert with_default_location("navigation", "navigation_search", values) == values


def test_nearby_search_uses_campus_but_not_explicit_different_city():
    result = with_default_location("local_life", "local_search", {"category": "restaurant"})
    assert result["anchor"] == get_settings().default_current_location
    assert result["city"] == "无锡市"
    explicit_city = {"category": "restaurant", "city": "南京市"}
    assert with_default_location("local_life", "local_search", explicit_city) == explicit_city
    explicit_anchor = {"category": "cinema", "anchor": "无锡站"}
    assert with_default_location("local_life", "local_search", explicit_anchor) == explicit_anchor


def test_destination_never_becomes_the_current_location():
    first = with_default_location("navigation", "navigation_search", {"destination": "南京站", "route_requested": True})
    next_request = with_default_location("navigation", "navigation_search", {"destination": "苏州站", "route_requested": True})
    assert first["origin"] == next_request["origin"] == get_settings().default_current_location


def test_default_can_be_disabled_without_hiding_missing_origin(monkeypatch):
    monkeypatch.setattr(get_settings(), "default_current_location", "")
    values = {"destination": "西湖", "route_requested": True}
    assert with_default_location("navigation", "navigation_search", values) == values
