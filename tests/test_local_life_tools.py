import httpx
import pytest

from car_agent.tools import LifeTools
from car_agent.tools.service import LOCAL_CATEGORIES, venue_item


def row(identifier="one", **changes):
    return {"id": identifier, "name": "真实格式测试商家", "location": "120.26,31.47", "distance": "400",
            "biz_ext": {"rating": "4.6", "cost": "80"}, **changes}


@pytest.mark.parametrize("category", list(LOCAL_CATEGORIES))
@pytest.mark.asyncio
async def test_category_search_uses_real_around_schema_and_bound_radius(tmp_path, category):
    def handler(request):
        assert request.url.path == "/v3/place/around"
        assert request.url.params["types"] == LOCAL_CATEGORIES[category][1]
        assert request.url.params["radius"] == "3000" and request.url.params["offset"] == "20"
        return httpx.Response(200, json={"status": "1", "pois": [row()]})
    result = await LifeTools(str(tmp_path / "data.sqlite3"), "secret", httpx.MockTransport(handler)).call("maps_local_search", {
        "category": category, "anchor": "120.26,31.47"})
    assert result["ok"] and result["items"][0]["id"] == "one"
    assert result["booking_supported"] is False
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_strict_filters_exclude_unknown_values_closed_places_and_out_of_radius(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"status": "1", "pois": [
            row("unknown", biz_ext=[]), row("valid"), row("expensive", biz_ext={"rating": "4.9", "cost": "200"}),
            row("closed", name="测试影院(暂停营业)"), row("far", distance="8000")]})
    result = await LifeTools(str(tmp_path / "data.sqlite3"), "secret", httpx.MockTransport(handler)).call("maps_local_search", {
        "category": "restaurant", "anchor": "120.26,31.47", "min_rating": 4, "max_cost": 100})
    assert [item["id"] for item in result["items"]] == ["valid"]
    assert result["missing_rating_count"] == 1 and result["missing_cost_count"] == 1
    assert result["excluded_closed_count"] == 1


@pytest.mark.asyncio
async def test_rating_order_is_within_returned_candidates_and_unknown_last(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"status": "1", "pois": [row("near", distance="10"),
            row("high", distance="200", biz_ext={"rating": "4.9"}), row("unknown", biz_ext=[])]})
    result = await LifeTools(str(tmp_path / "data.sqlite3"), "secret", httpx.MockTransport(handler)).call("maps_local_search", {
        "category": "ktv", "anchor": "120.26,31.47", "sort_by": "rating"})
    assert [item["id"] for item in result["items"]] == ["high", "near", "unknown"]
    assert "不代表全城排名" in result["ranking_scope"]


def test_missing_metadata_and_untrusted_photo_schemes_are_not_invented():
    item = venue_item(row(biz_ext=[], address=[], tel=[], opentime=[], photos=[{"url": "javascript:alert(1)"}]))
    assert all(item[key] is None for key in ("cost", "rating", "phone", "opening_hours", "address"))
    assert item["photos"] == [] and item["availability"] == "unknown"


@pytest.mark.asyncio
async def test_ambiguous_anchor_returns_candidates_before_search(tmp_path):
    def handler(request):
        assert request.url.path == "/v3/geocode/geo"
        return httpx.Response(200, json={"status": "1", "geocodes": [
            {"formatted_address": "校区一", "location": "120.26,31.47"},
            {"formatted_address": "校区二", "location": "120.3,31.5"}]})
    result = await LifeTools(str(tmp_path / "data.sqlite3"), "secret", httpx.MockTransport(handler)).call("maps_local_search", {
        "category": "cinema", "anchor": "东南大学"})
    assert result["requires_clarification"] and result["field"] == "anchor"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"radius_m": 100000}, {"category": "everything"}, {"max_cost": 0},
                                   {"min_rating": 6}, {"anchor": " "}, {"limit": 50}])
async def test_invalid_local_filters_never_call_upstream(tmp_path, change):
    def handler(request):
        pytest.fail("Invalid parameters must not reach upstream")
    result = await LifeTools(str(tmp_path / "data.sqlite3"), "secret", httpx.MockTransport(handler)).call("maps_local_search", {
        "category": "restaurant", "anchor": "120.26,31.47", **change})
    assert result["error_code"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
@pytest.mark.parametrize("rows,code", [([], "NO_RESULTS"), ([row("different-id")], "UPSTREAM_SCHEMA")])
async def test_missing_or_mismatched_detail_does_not_use_another_venue(tmp_path, rows, code):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "1", "pois": rows}))
    result = await LifeTools(str(tmp_path / "data.sqlite3"), "secret", transport).call("maps_place_detail", {"poi_id": "requested"})
    assert result["error_code"] == code
