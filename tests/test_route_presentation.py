import json
import math
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from car_agent.route_geometry import geometry_from_steps, point, sample_points, without_map_geometry
from car_agent.route_presentation import MapPreviewError, RouteMapRenderer, route_record, static_map_parameters
from car_agent.tools import LifeTools


def receipt():
    return {"ok": True, "source": "高德地图", "origin": "120.1482,30.2395", "destination": "120.1551,30.242",
            "distance_m": 1250, "duration_s": 720, "travel_mode": "walking", "origin_name": "西湖", "destination_name": "目的地",
            "steps": ["沿南山路向北步行", "到达终点"], "tolls": "0",
            "geometry": geometry_from_steps([{"polyline": "120.1482,30.2395;120.15,30.241;120.1551,30.242"}])}


def row(result=None):
    return {"id": "job", "task_id": "task", "parent_task_id": None, "session_id": "session", "query": "从西湖步行出发",
            "updated_at": datetime.now(timezone.utc).isoformat(), "result_json": json.dumps(receipt() if result is None else result)}


@pytest.mark.parametrize("value", ["nan,30", "120,inf", [120, 86], [200, 30], "120", None, {}, [1, 2, 3]])
def test_display_coordinates_reject_invalid_or_non_mercator_points(value):
    assert point(value) is None


def test_geometry_comes_from_complete_provider_steps_and_is_bounded():
    steps = [{"polyline": "120.1,30.1;120.2,30.2"}, {"polyline": "120.2,30.2;120.3,30.3"}]
    geometry = geometry_from_steps(steps)
    assert geometry["coordinates"] == [[120.1, 30.1], [120.2, 30.2], [120.3, 30.3]]
    assert geometry["coordinate_system"] == "GCJ-02"
    assert geometry_from_steps([steps[0], {"instruction": "missing geometry"}]) == {}
    assert geometry_from_steps([{"polyline": "120,30;nan,30"}]) == {}
    points = [[120 + i / 10000, 30] for i in range(2000)]
    reduced = sample_points(points, 100)
    assert len(reduced) == 100 and reduced[0] == points[0] and reduced[-1] == points[-1]


def test_model_context_excludes_geometry_but_preserves_execution_evidence():
    original = {"items": [{"name": "place"}], "route_result": {**receipt(), "alternatives": [receipt(), receipt()]}}
    reduced = without_map_geometry(original)
    assert "geometry" not in reduced["route_result"] and "geometry" in original["route_result"]
    assert reduced["route_result"]["steps"] == original["route_result"]["steps"]
    assert "alternatives" not in reduced["route_result"] and len(original["route_result"]["alternatives"]) == 2


@pytest.mark.parametrize("changes", [{"ok": False}, {"simulated": True}, {"origin": "bad"},
    {"duration_s": 0}, {"distance_m": math.inf}, {"source": "model"}, {"travel_mode": "plane"}])
def test_invalid_or_simulated_receipts_are_not_presented_as_amap_routes(changes):
    assert route_record(row({**receipt(), **changes})) is None


def test_legacy_route_draws_markers_not_an_invented_straight_road():
    route = route_record(row({**receipt(), "geometry": {}}))
    assert route["geometry_available"] is False
    assert "paths" not in static_map_parameters(route)
    assert "markers" in static_map_parameters(route)


def test_static_map_contains_real_sampled_path_and_clamped_zoom_without_key():
    route = route_record(row())
    params = static_map_parameters(route)
    assert "120.15,30.241" in params["paths"]
    assert params["size"] == "800*400" and "key" not in params
    assert static_map_parameters(route, 100)["zoom"] == "17"
    assert static_map_parameters(route, -100)["zoom"] == "1"
    assert "callnative=1" in route["navigation_url"]


def test_hangzhou_station_to_lake_view_uses_verified_amap_framing():
    route = route_record(row({**receipt(), "origin": "120.2126,30.290851", "destination": "120.121358,30.222692", "geometry": {}}))
    assert static_map_parameters(route)["zoom"] == "11"
    assert static_map_parameters(route, large=True)["zoom"] == "12"


@pytest.mark.parametrize("result", [[], "invalid", None])
def test_unstructured_receipts_never_crash_the_route_list(result):
    broken = row()
    broken["result_json"] = json.dumps(result)
    assert route_record(broken) is None


@pytest.mark.asyncio
async def test_real_tool_retains_geometry_and_steps_without_secret(tmp_path):
    def handler(request):
        assert request.url.path == "/v3/direction/walking"
        return httpx.Response(200, json={"status": "1", "route": {"paths": [{"distance": "1250", "duration": "720", "steps": [
            {"instruction": "沿道路步行", "polyline": "120.1482,30.2395;120.15,30.241;120.1551,30.242"}]}]}})

    result = await LifeTools(str(tmp_path / "test.sqlite3"), "secret", httpx.MockTransport(handler)).call("maps_route", {
        "origin": "120.1482,30.2395", "destination": "120.1551,30.242", "travel_mode": "walking"})
    assert result["ok"] is True and len(result["geometry"]["coordinates"]) == 3
    assert result["steps"] == ["沿道路步行"]
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_renderer_caches_images_but_never_returns_provider_json():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["key"] == "server-only-key"
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nimage", headers={"Content-Type": "image/png"})

    renderer = RouteMapRenderer("server-only-key", httpx.MockTransport(handler))
    route = route_record(row())
    first = await renderer.render(route)
    assert await renderer.render(route) == first
    assert len(calls) == 1
    assert await renderer.render(route, -1) == first
    assert len(calls) == 2
    denied = RouteMapRenderer("secret", httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "0", "info": "secret"})))
    with pytest.raises(MapPreviewError, match="静态地图服务权限") as exc:
        await denied.render(route)
    assert "secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_renderer_network_failure_redacts_credential_url():
    def handler(request):
        raise httpx.ConnectError(str(request.url), request=request)

    renderer = RouteMapRenderer("never-return-this-key", httpx.MockTransport(handler))
    with pytest.raises(MapPreviewError) as exc:
        await renderer.render(route_record(row()))
    assert "never-return-this-key" not in str(exc.value)


@pytest.fixture
async def client(tmp_path):
    from car_agent.api.app import app, settings
    original = settings.database_path
    settings.database_path = str(tmp_path / "route-api.sqlite3")
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-Demo-User": "route-owner"}) as connection:
                yield connection
    finally:
        settings.database_path = original


async def seed(client, parent=None):
    from car_agent.api.app import app
    session = (await client.post("/api/sessions")).json()["id"]
    # The DB row models an already-completed real tool receipt; no model output is used as evidence.
    now = datetime.now(timezone.utc).isoformat()
    await app.state.db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at,parent_task_id) VALUES(?,?,?,?,?,?,?,?,?)",
        ("route-task", session, "route-owner", "从西湖步行出发", "completed", "live", now, now, parent))
    await app.state.db.execute("INSERT INTO tool_jobs(id,task_id,tool_name,args_json,idempotency_key,status,available_at,created_at,updated_at,result_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("route-job", "route-task", "maps_route", "{}", "route-key", "completed", now, now, now, json.dumps(receipt())))
    return session


@pytest.mark.asyncio
async def test_route_api_persists_history_and_enforces_user_and_job_ownership(client):
    session = await seed(client)
    summary = await client.get(f"/api/sessions/{session}/routes")
    assert summary.status_code == 200
    assert len(summary.json()) == 1 and "geometry" not in summary.json()[0]
    detail = await client.get("/api/tasks/route-task/route?job_id=route-job")
    assert detail.status_code == 200 and detail.json()["geometry_available"] is True
    assert detail.json()["steps"] == receipt()["steps"]
    assert (await client.get("/api/tasks/route-task/route?job_id=another-job")).status_code == 404
    assert (await client.get("/api/tasks/route-task/route", headers={"X-Demo-User": "other"})).status_code == 404
    assert (await client.get(f"/api/sessions/{session}/routes", headers={"X-Demo-User": "other"})).status_code == 404


@pytest.mark.asyncio
async def test_map_proxy_checks_identity_before_cache_and_accepts_only_bounded_zoom(client):
    from car_agent.api.app import app
    await seed(client)
    app.state.route_maps = RouteMapRenderer("private-key", httpx.MockTransport(lambda request: httpx.Response(200, content=b"\x89PNG\r\n\x1a\nimage")))
    response = await client.get("/api/tasks/route-task/route/map?job_id=route-job")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert "private" in response.headers["cache-control"] and "private-key" not in str(response.headers)
    assert (await client.get("/api/tasks/route-task/route/map?job_id=route-job", headers={"X-Demo-User": "other"})).status_code == 404
    assert (await client.get("/api/tasks/route-task/route/map?zoom=100")).status_code == 422


@pytest.mark.asyncio
async def test_cancelled_task_does_not_expose_route_and_stale_snapshot_is_labeled(client):
    from car_agent.api.app import app
    session = await seed(client)
    data = receipt()
    data["_freshness"] = {"issued_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                          "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()}
    await app.state.db.execute("UPDATE tool_jobs SET result_json=?", (json.dumps(data),))
    assert (await client.get("/api/tasks/route-task/route")).json()["is_stale"] is True
    await app.state.db.execute("UPDATE tasks SET status='cancelled' WHERE id='route-task'")
    assert (await client.get(f"/api/sessions/{session}/routes")).json() == []
    assert (await client.get("/api/tasks/route-task/route/map")).status_code == 404


def alternatives():
    first = {key: value for key, value in receipt().items() if key in {"distance_m", "duration_s", "steps", "tolls", "geometry"}}
    return [{**first, "option_id": 0, "strategy": "常规路线"},
            {**first, "option_id": 1, "distance_m": 1600, "duration_s": 800, "strategy": "沿湖道路",
             "steps": ["沿另一条道路步行"], "geometry": geometry_from_steps([{"polyline": "120.1482,30.2395;120.151,30.243;120.1551,30.242"}])}]


def test_alternative_selection_updates_geometry_metrics_and_steps_together():
    data = row({**receipt(), "alternatives": alternatives()})
    route = route_record(data, option=1)
    assert route["option_id"] == 1 and len(route["options"]) == 2
    assert route["distance_m"] == 1600 and route["duration_s"] == 800
    assert route["steps"] == ["沿另一条道路步行"]
    assert "120.151,30.243" in static_map_parameters(route)["paths"]
    assert "120.15,30.241" not in static_map_parameters(route)["paths"]
    assert route_record(data, option=4) is None
    assert route_record(row(), option=1) is None


@pytest.mark.parametrize("mode", ["bicycling", "walking", "driving"])
def test_route_receipt_returns_all_mode_choices_with_the_actual_requested_mode_as_default(mode):
    route = route_record(row({**receipt(), "travel_mode": mode}))
    assert route["default_travel_mode"] == mode
    assert route["mode_choices"][0]["travel_mode"] == mode
    assert len(route["mode_choices"]) == 3
    assert sum(choice["is_default"] for choice in route["mode_choices"]) == 1


def test_bad_alternatives_do_not_inherit_the_recommended_route_geometry_or_metrics():
    data = row({**receipt(), "alternatives": [{"option_id": 0}, {"option_id": 1, "distance_m": 50, "duration_s": 30}]})
    route = route_record(data)
    assert route["option_id"] == 1 and len(route["options"]) == 1
    assert route["geometry_available"] is False and route["steps"] == []
    assert "paths" not in static_map_parameters(route)


@pytest.mark.asyncio
async def test_driving_requests_real_multiple_paths_and_keeps_each_geometry(tmp_path):
    def handler(request):
        assert request.url.path == "/v3/direction/driving"
        assert request.url.params["strategy"] == "10" and request.url.params["extensions"] == "all"
        return httpx.Response(200, json={"status": "1", "route": {"paths": [
            {"distance": "1250", "duration": "720", "strategy": "常规路线", "steps": [{"instruction": "道路一", "polyline": "120.1482,30.2395;120.15,30.241;120.1551,30.242"}]},
            {"distance": "1600", "duration": "800", "strategy": "另一条道路", "steps": [{"instruction": "道路二", "polyline": "120.1482,30.2395;120.151,30.243;120.1551,30.242"}]}]}})

    result = await LifeTools(str(tmp_path / "test.sqlite3"), "secret", httpx.MockTransport(handler)).call("maps_route", {
        "origin": "120.1482,30.2395", "destination": "120.1551,30.242", "travel_mode": "driving"})
    assert result["ok"] and len(result["alternatives"]) == 2
    assert result["alternatives"][0]["geometry"] != result["alternatives"][1]["geometry"]
    assert result["duration_s"] == result["alternatives"][0]["duration_s"]


@pytest.mark.asyncio
async def test_map_api_selects_the_owned_route_option_without_an_extra_provider_planning_call(client):
    from car_agent.api.app import app
    await seed(client)
    await app.state.db.execute("UPDATE tool_jobs SET result_json=?", (json.dumps({**receipt(), "alternatives": alternatives()}),))
    captured = []

    def handler(request):
        captured.append(request.url.params.get("paths"))
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nimage")

    app.state.route_maps = RouteMapRenderer("private-key", httpx.MockTransport(handler))
    assert (await client.get("/api/tasks/route-task/route?option=1")).json()["duration_s"] == 800
    for option in (0, 1):
        assert (await client.get(f"/api/tasks/route-task/route/map?option={option}")).status_code == 200
    assert captured[0] != captured[1]
    assert (await client.get("/api/tasks/route-task/route?option=4")).status_code == 404
    assert (await client.get("/api/tasks/route-task/route?option=-1")).status_code == 422
    assert (await client.get("/api/tasks/route-task/route/map?option=1", headers={"X-Demo-User": "other"})).status_code == 404


async def await_task(client, task_id):
    import asyncio
    for _ in range(400):
        task = (await client.get(f"/api/tasks/{task_id}")).json()
        if task["status"] in {"completed", "failed", "cancelled", "waiting_user"}:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("route replan did not settle")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["driving", "walking", "bicycling"])
async def test_replan_uses_fixed_coordinates_new_task_queue_and_no_llm_or_poi_search(client, mode):
    from car_agent.api.app import app
    await seed(client)
    app.state.context.llm = object()  # Any attempted model method would fail this deterministic UI action.
    calls = []

    async def dispatch(name, arguments, task_id):
        calls.append((name, arguments, task_id))
        return {**receipt(), **arguments, "origin_name": "", "destination_name": "", "alternatives": alternatives(),
                "navigation_url": "https://uri.amap.com/navigation?mode=" + {"driving": "car", "walking": "walk", "bicycling": "ride"}[arguments["travel_mode"]]}

    app.state.context.tool_queue.dispatcher = dispatch
    payload = {"job_id": "route-job", "travel_mode": mode, "request_id": "mode-switch"}
    response = await client.post("/api/tasks/route-task/route/replan", json=payload)
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    duplicate = await client.post("/api/tasks/route-task/route/replan", json=payload)
    assert duplicate.json()["task_id"] == task_id and duplicate.json()["duplicate"] is True
    task = await await_task(client, task_id)
    assert task["status"] == "completed", task
    route = (await client.get(f"/api/tasks/{task_id}/route")).json()
    assert route["travel_mode"] == mode and route["origin_name"] == "西湖"
    assert route["origin"] == receipt()["origin"] and route["destination"] == receipt()["destination"]
    assert task_id != "route-task" and len(calls) == 1 and calls[0][0] == "maps_route"
    assert calls[0][1]["travel_mode"] == mode and calls[0][2] == task_id
    assert (await client.get("/api/tasks/route-task/route")).json()["travel_mode"] == "walking"
    assert (await client.post("/api/tasks/route-task/route/replan", json={**payload, "travel_mode": "walking" if mode != "walking" else "driving"})).status_code == 409
    trace = (await client.get(f"/api/tasks/{task_id}/trace")).json()
    assert any(event["type"] == "tool.completed" for event in trace)
    assert not any(event["type"].startswith("model.") for event in trace)


@pytest.mark.asyncio
async def test_replan_rejects_cross_user_job_invalid_mode_and_endpoint_injection(client):
    await seed(client)
    payload = {"job_id": "route-job", "travel_mode": "driving", "request_id": "new-request"}
    assert (await client.post("/api/tasks/route-task/route/replan", json=payload, headers={"X-Demo-User": "other"})).status_code == 404
    assert (await client.post("/api/tasks/route-task/route/replan", json={**payload, "job_id": "other-job"})).status_code == 404
    assert (await client.post("/api/tasks/route-task/route/replan", json={**payload, "travel_mode": "walk"})).status_code == 422
    assert (await client.post("/api/tasks/route-task/route/replan", json={**payload, "origin": "120,30"})).status_code == 422


@pytest.mark.asyncio
async def test_failed_replan_does_not_change_old_receipt_or_invent_a_new_map(client):
    from car_agent.api.app import app
    await seed(client)
    app.state.context.llm = object()

    async def dispatch(*_args):
        return {"ok": False, "error": "该方式暂无路线", "error_code": "NO_ROUTE", "retryable": False}

    app.state.context.tool_queue.dispatcher = dispatch
    response = await client.post("/api/tasks/route-task/route/replan", json={"job_id": "route-job", "travel_mode": "bicycling", "request_id": "failed-switch"})
    task_id = response.json()["task_id"]
    assert (await await_task(client, task_id))["status"] == "failed"
    assert (await client.get(f"/api/tasks/{task_id}/route")).status_code == 404
    assert (await client.get("/api/tasks/route-task/route")).json()["duration_s"] == receipt()["duration_s"]


def test_replan_receipt_cannot_silently_change_the_mode_or_endpoints():
    from car_agent.execution_verification import verify_execution
    result = {**receipt(), "navigation_url": "https://uri.amap.com/navigation"}
    slots = {key: result[key] for key in ("origin", "destination", "travel_mode")}
    state = {"domain": "navigation", "action": "navigation_route", "mode": "live", "status": "running",
             "slots": {**slots, "route_requested": True}, "tool_result": {"route_result": result, "items": [{"name": "地点"}]}}
    assert not verify_execution(state)["enforce_failure"]
    for key, value in [("origin", "120,30"), ("destination", "121,31"), ("travel_mode", "driving")]:
        bad = {**state, "tool_result": {"route_result": {**result, key: value}, "items": [{"name": "地点"}]}}
        assert "replanned_route_mismatch" in verify_execution(bad)["problems"]
