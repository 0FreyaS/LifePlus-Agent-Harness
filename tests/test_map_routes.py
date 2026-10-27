import httpx
import pytest

from car_agent.tools import LifeTools
from car_agent.tools.service import coordinate, location_query


@pytest.mark.parametrize("value,city,expected", [
    ("从无锡市人民医院出发", "南京市", ("无锡市人民医院", "")),
    ("江苏省无锡市人民医院", "南京", ("江苏省无锡市人民医院", "")),
    ("从无锡市人民医院出发", "无锡", ("无锡市人民医院", "无锡")),
    ("人民医院", "南京市", ("人民医院", "南京市")),
    ("从化市人民医院", "广州市", ("从化市人民医院", "")),
])
def test_origin_normalization_does_not_force_destination_city(value, city, expected):
    assert location_query(value, city, "origin") == expected


@pytest.mark.parametrize("value,expected", [
    ("120.324003,31.542815", "120.324003,31.542815"),
    ("120.32400348, 31.54281523", "120.324003,31.542815"),
    ("0.0000001,0", "0,0"),
])
def test_coordinates_match_amap_six_decimal_contract(value, expected):
    assert coordinate(value, "origin") == expected


@pytest.mark.asyncio
async def test_legacy_cross_city_failure_is_corrected_before_geocoding(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v3/geocode/geo":
            assert request.url.params["address"] == "无锡市人民医院"
            assert "city" not in request.url.params
            return httpx.Response(200, json={"status": "1", "infocode": "10000", "geocodes": [{"location": "120.324003,31.542815"}]})
        assert request.url.path == "/v3/direction/driving"
        assert request.url.params["origin"] == "120.324003,31.542815"
        return httpx.Response(200, json={"status": "1", "infocode": "10000", "route": {"paths": [
            {"distance": "190000", "duration": "9000", "steps": [{"instruction": "沿道路行驶"}]}]}})

    result = await LifeTools(str(tmp_path / "maps.sqlite3"), "test-key", httpx.MockTransport(handler)).call("maps_route", {
        "origin": "从无锡市人民医院出发", "destination": "118.795193,32.054213", "city": "南京市", "travel_mode": "driving"})
    assert result["ok"] is True and result["distance_m"] == 190000
    assert len(requests) == 2
    assert "test-key" not in str(result)


@pytest.mark.asyncio
async def test_each_address_uses_its_own_applicable_city_hint(tmp_path):
    geocodes = {}

    def handler(request):
        if request.url.path == "/v3/geocode/geo":
            address = request.url.params["address"]
            geocodes[address] = request.url.params.get("city")
            location = "120.324003,31.542815" if "无锡" in address else "118.795193,32.054213"
            return httpx.Response(200, json={"status": "1", "geocodes": [{"location": location}]})
        return httpx.Response(200, json={"status": "1", "route": {"paths": [{"distance": "190000", "duration": "9000"}]}})

    result = await LifeTools(str(tmp_path / "maps.sqlite3"), "test-key", httpx.MockTransport(handler)).call("maps_route", {
        "origin": "无锡市人民医院", "destination": "南京市东南大学", "city": "南京市"})
    assert result["ok"] is True
    assert geocodes == {"无锡市人民医院": None, "南京市东南大学": "南京市"}


@pytest.mark.asyncio
async def test_remaining_engine_error_keeps_stage_and_never_fabricates_a_route(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"status": "0", "infocode": "30001", "info": "ENGINE_RESPONSE_DATA_ERROR"})

    result = await LifeTools(str(tmp_path / "maps.sqlite3"), "test-key", httpx.MockTransport(handler)).call("maps_route", {
        "origin": "无锡市人民医院", "destination": "118.795193,32.054213"})
    assert result["ok"] is False and result["error_code"] == "AMAP_30001"
    assert result["upstream_path"] == "v3/geocode/geo" and result["retryable"] is True
    assert "地址解析" in result["error"] and "navigation_url" not in result
    assert "test-key" not in str(result)


@pytest.mark.asyncio
async def test_invalid_coordinates_never_reach_the_upstream(tmp_path):
    def handler(request):
        pytest.fail("Invalid input must not call AMap")

    result = await LifeTools(str(tmp_path / "maps.sqlite3"), "test-key", httpx.MockTransport(handler)).call("maps_route", {
        "origin": "200,95", "destination": "118.795193,32.054213"})
    assert result["error_code"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_ambiguous_origin_remains_a_clarification_instead_of_first_candidate(tmp_path):
    def handler(request):
        assert request.url.path == "/v3/geocode/geo"
        return httpx.Response(200, json={"status": "1", "geocodes": [
            {"formatted_address": "第一家人民医院", "location": "120.1,31.5"},
            {"formatted_address": "第二家人民医院", "location": "120.2,31.5"}]})

    result = await LifeTools(str(tmp_path / "maps.sqlite3"), "test-key", httpx.MockTransport(handler)).call("maps_route", {
        "origin": "人民医院", "destination": "118.795193,32.054213"})
    assert result["requires_clarification"] is True and result["field"] == "origin"
    assert len(result["candidates"]) == 2
