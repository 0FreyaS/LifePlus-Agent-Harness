import asyncio
import json
import uuid

import httpx
import pytest
from langchain_core.messages import AIMessageChunk
from pydantic import ValidationError

from car_agent.api.app import app, settings
from car_agent.task_planning import GoalPlan, TaskPlan


@pytest.fixture
async def api_client(tmp_path):
    original = settings.database_path
    settings.database_path = str(tmp_path / "multi.sqlite3")
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                         headers={"X-Demo-User": "multi-test"}) as client:
                yield client
    finally:
        settings.database_path = original


async def submit(client, query, mode="demo", session_id=None):
    session = session_id or (await client.post("/api/sessions")).json()["id"]
    response = await client.post(f"/api/sessions/{session}/messages", json={"message": query, "mode": mode})
    assert response.status_code == 200, response.text
    return response.json()["task_id"], session


async def wait_for(client, task_id, predicate):
    for _ in range(150):
        task = (await client.get(f"/api/tasks/{task_id}")).json()
        if predicate(task):
            return task
        await asyncio.sleep(0.03)
    raise AssertionError(task)


@pytest.mark.asyncio
async def test_independent_weather_finishes_while_music_waits_and_resumes_once(api_client):
    client = api_client
    task_id, session = await submit(client, "杭州天气怎么样，同时搜索陶喆的歌曲")
    group = await wait_for(client, task_id, lambda task: task["status"] == "waiting_user")
    assert len(group["children"]) == 2
    weather, music = group["children"]
    assert weather["status"] == "completed" and music["status"] == "waiting_user"
    assert weather["thread_id"] != music["thread_id"]
    tasks = (await client.get(f"/api/tasks?session_id={session}")).json()
    assert tasks[0]["domain"] == "multi"
    trace = (await client.get(f"/api/tasks/{task_id}/trace")).json()
    assert any(event["type"] == "subtask.answer.completed" and event["child_task_id"] == weather["id"] for event in trace)
    payload = {"interrupt_id": music["interrupt"]["id"], "action": "answer", "value": {"index": 2}}
    responses = await asyncio.gather(*(client.post(f"/api/tasks/{music['id']}/resume", json=payload) for _ in range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    done = await wait_for(client, task_id, lambda task: task["status"] == "completed")
    assert "杭州" in done["answer"]
    assert all(child["status"] == "completed" for child in done["children"])
    history = (await client.get(f"/api/sessions/{session}/messages")).json()
    assert len(history) == 2
    trace = (await client.get(f"/api/tasks/{task_id}/trace")).json()
    assert len({event["seq"] for event in trace}) == len(trace)
    assert len([event for event in trace if event["type"] == "task.completed"]) == 1


@pytest.mark.asyncio
async def test_independent_domain_tools_are_actually_in_flight_together(api_client):
    started = set()
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(name, arguments, task_id):
        started.add(name)
        if {"maps_weather", "search_music"} <= started:
            both_started.set()
        await release.wait()
        return app.state.context.demo.query(name, arguments)

    app.state.context.tool_queue.dispatcher = dispatch
    task_id, _ = await submit(api_client, "杭州天气怎么样，同时搜索陶喆的歌曲")
    try:
        await asyncio.wait_for(both_started.wait(), timeout=4)
        group = (await api_client.get(f"/api/tasks/{task_id}")).json()
        assert all(child["status"] == "running" for child in group["children"])
        graph = (await api_client.get(f"/api/tasks/{task_id}/graph")).json()
        child_ids = {child["id"] for child in group["children"]}
        assert {edge["target"] for edge in graph["edges"] if edge["source"] == "plan"} == child_ids
        assert not any(edge["source"] in child_ids and edge["target"] in child_ids for edge in graph["edges"])
    finally:
        release.set()
    await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")


async def test_per_task_context_preserves_optional_routers_through_actual_graph(api_client):
    calls = []

    class Router:
        async def route(self, state):
            calls.append(state["user_id"])
            return "weather"

    class Model:
        def with_structured_output(self, schema, **kwargs):
            assert schema.__name__ == "WeatherExecutionDecision", "Domain LLM must be replaced in the real task context"

            class Call:
                async def ainvoke(self, messages):
                    return {"action": "weather_lookup", "tool_name": "maps_weather", "slots": {"city": "无锡"}}
            return Call()

        async def astream(self, messages):
            yield AIMessageChunk(content="受控天气回复")

    async def dispatch(name, arguments, task_id):
        return {"ok": True, "city": "无锡", "date": "2026-10-29", "day_weather": "晴", "low": 10, "high": 20}

    context = app.state.context
    context.domain_router = Router()
    context.llm = Model()
    context.model_roles = {}
    context.tool_queue.dispatcher = dispatch
    task_id, _ = await submit(api_client, "查无锡天气", mode="live")
    done = await wait_for(api_client, task_id, lambda task: task["status"] in {"completed", "failed"})
    assert done["status"] == "completed", done
    assert calls == ["multi-test"]
    trace = (await api_client.get(f"/api/tasks/{task_id}/trace")).json()
    assert any(event["type"] == "route.completed" and event.get("source") == "jev_cascade" for event in trace)


@pytest.mark.parametrize("variant", ["baseline", "compact"])
async def test_role_models_reach_children_and_domains_extract_their_own_slots(api_client, variant):
    counts = {"planner": 0, "intent": 0, "response": 0}

    class Planner:
        def with_structured_output(self, schema, **kwargs):
            assert schema is GoalPlan

            class Call:
                async def ainvoke(self, messages):
                    counts["planner"] += 1
                    return schema.model_validate({"tasks": [
                        {"id": "w1", "query": "无锡天气", "domain": "weather"},
                        {"id": "w2", "query": "杭州天气", "domain": "weather"},
                    ]})
            return Call()

    class Response:
        async def astream(self, messages):
            counts["response"] += 1
            yield AIMessageChunk(content="天气查询结果已返回。")

    class Forbidden:
        def with_structured_output(self, *args, **kwargs):
            raise AssertionError("A planned child must not classify intent again")

        async def astream(self, *args, **kwargs):
            raise AssertionError("The default model must not replace the response role")
            yield

    class Domain:
        def with_structured_output(self, schema, **kwargs):
            assert schema.__name__ == "WeatherExecutionDecision"

            class Call:
                async def ainvoke(self, messages):
                    counts["intent"] += 1
                    query = json.loads(messages[-1]["content"])["query"]
                    return schema.model_validate({"action": "weather_lookup", "tool_name": "maps_weather",
                                                  "slots": {"city": "无锡" if "无锡" in query else "杭州"}})
            return Call()

    async def dispatch(name, arguments, task_id):
        assert name == "maps_weather"
        return {"ok": True, "city": arguments["city"], "date": "2026-10-27", "day_weather": "晴", "low": 10, "high": 20}

    context = app.state.context
    context.llm = Forbidden()
    context.model_roles = {"planner": Planner(), "intent": Domain(), "response": Response()}
    context.planner_variant = variant
    context.tool_queue.dispatcher = dispatch
    task_id, _ = await submit(api_client, "查询无锡和杭州的天气", mode="live")
    group = await wait_for(api_client, task_id, lambda item: item["status"] in {"completed", "failed", "partial"})
    assert group["status"] == "completed", group
    assert len(group["children"]) == 2
    assert counts == {"planner": 1, "intent": 2, "response": 2}


@pytest.mark.asyncio
async def test_same_session_accepts_new_task_while_another_waits(api_client):
    client = api_client
    music_id, session = await submit(client, "搜索周杰伦的歌曲")
    music = await wait_for(client, music_id, lambda task: task["status"] == "waiting_user")
    weather_id, _ = await submit(client, "杭州天气怎么样", session_id=session)
    weather = await wait_for(client, weather_id, lambda task: task["status"] == "completed")
    assert "杭州" in weather["answer"]
    pending = (await client.get(f"/api/tasks/{music_id}")).json()
    assert pending["interrupt"]["id"] == music["interrupt"]["id"]
    response = await client.post(f"/api/tasks/{music_id}/resume", json={
        "interrupt_id": music["interrupt"]["id"], "action": "answer", "value": {"index": 1}})
    assert response.status_code == 200
    completed = await wait_for(client, music_id, lambda task: task["status"] == "completed")
    assert completed["domain"] == "music"


@pytest.mark.asyncio
async def test_one_reply_can_resume_selection_and_create_a_new_task(api_client):
    music_id, _ = await submit(api_client, "搜索周杰伦的歌曲")
    music = await wait_for(api_client, music_id, lambda task: task["status"] == "waiting_user")
    response = await api_client.post(f"/api/tasks/{music_id}/resume", json={
        "interrupt_id": music["interrupt"]["id"], "action": "answer", "value": {"text": "第二个，同时查杭州天气"}})
    assert response.status_code == 200
    new_task = response.json()["created_task_id"]
    await wait_for(api_client, music_id, lambda task: task["status"] == "completed")
    weather = await wait_for(api_client, new_task, lambda task: task["status"] == "completed")
    assert weather["domain"] == "weather" and "杭州" in weather["answer"]


@pytest.mark.asyncio
async def test_one_failed_branch_produces_partial_success(api_client):
    async def dispatch(name, arguments, task_id):
        if name == "search_music":
            return {"error": "音乐服务不可用", "error_code": "NOT_CONFIGURED", "retryable": False}
        return app.state.context.demo.query(name, arguments)

    app.state.context.tool_queue.dispatcher = dispatch
    task_id, _ = await submit(api_client, "杭州天气怎么样，同时搜索陶喆的歌曲")
    group = await wait_for(api_client, task_id, lambda task: task["status"] == "partial")
    assert [child["status"] for child in group["children"]] == ["completed", "failed"]
    assert "杭州" in group["answer"] and "音乐服务不可用" in group["answer"]


@pytest.mark.asyncio
async def test_dependent_write_receives_upstream_evidence_and_requires_approval(api_client, monkeypatch):
    async def plan(*_args):
        return TaskPlan.model_validate({"tasks": [
            {"id": "save", "query": "保存杭州天气记录到行程", "domain": "planning", "action": "save_itinerary",
             "slots": {"title": "杭州天气记录"}, "depends_on": ["weather"], "bind_slots": {"details": "weather.result"}},
            {"id": "weather", "query": "杭州天气怎么样", "domain": "weather", "action": "weather_lookup", "slots": {"city": "杭州"}},
        ]})

    monkeypatch.setattr("car_agent.orchestration.plan_tasks", plan)
    task_id, _ = await submit(api_client, "查天气并保存")
    group = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
    weather, save = group["children"]
    assert weather["status"] == "completed"
    assert save["interrupt"]["kind"] == "approval"
    assert save["interrupt"]["arguments"]["details"]["city"] == "杭州"
    graph = (await api_client.get(f"/api/tasks/{task_id}/graph")).json()
    assert {"source": weather["id"], "target": save["id"]} in graph["edges"]
    assert {"source": "plan", "target": save["id"]} not in graph["edges"]
    events = (await api_client.get(f"/api/tasks/{task_id}/trace")).json()
    weather_completed = next(event["seq"] for event in events if event["type"] == "subtask.task.completed" and event["child_task_id"] == weather["id"])
    save_started = next(event["seq"] for event in events if event["type"] == "subtask.task.started" and event["child_task_id"] == save["id"])
    assert weather_completed < save_started
    await api_client.post(f"/api/tasks/{save['id']}/resume", json={
        "interrupt_id": save["interrupt"]["id"], "action": "approve", "value": {}})
    await wait_for(api_client, task_id, lambda task: task["status"] == "completed")


async def test_goal_only_plan_binds_actual_receipt_after_domain_extraction(api_client):
    calls = []

    class Model:
        def with_structured_output(self, schema, **kwargs):
            class Call:
                async def ainvoke(self, messages):
                    calls.append(schema.__name__)
                    if schema is GoalPlan:
                        return {"tasks": [
                            {"id": "w", "query": "查杭州天气", "domain": "weather"},
                            {"id": "s", "query": "保存杭州天气结果到行程", "domain": "planning",
                             "depends_on": ["w"], "bind_slots": {"details": "w.result"}},
                        ]}
                    if schema.__name__ == "WeatherExecutionDecision":
                        return {"action": "weather_lookup", "tool_name": "maps_weather", "slots": {"city": "杭州"}}
                    assert schema.__name__ == "PlanningExecutionDecision"
                    data = json.loads(messages[-1]["content"])
                    assert data["bound_slots"]["details"]["city"] == "杭州"
                    return {"action": "save_itinerary", "tool_name": "save_itinerary",
                            "slots": {"title": "杭州天气", "details": {"city": "南京"}}}
            return Call()

        async def astream(self, messages):
            yield AIMessageChunk(content="杭州天气已查询。")

    async def dispatch(name, arguments, task_id):
        assert name == "maps_weather", "The write must wait for explicit approval"
        return {"ok": True, "city": "杭州", "date": arguments["forecast_date"], "day_weather": "雨", "low": 10, "high": 20}

    app.state.context.llm = Model()
    app.state.context.tool_queue.dispatcher = dispatch
    task_id, _ = await submit(api_client, "查杭州天气，然后保存查询结果到行程", mode="live")
    group = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
    assert calls == ["GoalPlan", "WeatherExecutionDecision", "PlanningExecutionDecision"]
    save = group["children"][1]
    assert save["interrupt"]["arguments"]["details"]["city"] == "杭州"
    assert save["interrupt"]["kind"] == "approval"
    plan = json.loads(group["plan_json"])
    assert all(not spec["action"] and not spec["slots"] for spec in plan["tasks"])


async def test_invalid_domain_tool_never_reaches_queue(api_client):
    submitted = []

    class Model:
        def with_structured_output(self, schema, **kwargs):
            class Call:
                async def ainvoke(self, messages):
                    if schema.__name__ == "DomainRoute":
                        return {"domain": "weather"}
                    return {"action": "weather_lookup", "tool_name": "cancel_reminder", "slots": {"city": "杭州"}}
            return Call()

    async def dispatch(*args):
        submitted.append(args)
        raise AssertionError("An invalid model proposal must never execute")

    app.state.context.llm = Model()
    app.state.context.tool_queue.dispatcher = dispatch
    task_id, _ = await submit(api_client, "查杭州天气", mode="live")
    await wait_for(api_client, task_id, lambda task: task["status"] == "failed")
    assert submitted == []
    assert not await app.state.db.fetchall("SELECT * FROM tool_jobs WHERE task_id=?", (task_id,))


@pytest.mark.asyncio
async def test_cancelling_waiting_branch_preserves_finished_sibling(api_client):
    task_id, _ = await submit(api_client, "杭州天气怎么样，同时搜索陶喆的歌曲")
    group = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
    music = group["children"][1]
    response = await api_client.post(f"/api/tasks/{music['id']}/cancel")
    assert response.status_code == 200
    group = await wait_for(api_client, task_id, lambda task: task["status"] == "partial")
    assert group["children"][0]["status"] == "completed"
    assert group["children"][1]["status"] == "cancelled"


class StreamingModel:
    def with_structured_output(self, schema, **_kwargs):
        class Router:
            async def ainvoke(self, _messages):
                return schema.model_validate({"domain": "chat"})
        return Router()

    async def astream(self, _messages):
        yield AIMessageChunk(content="你好")
        await asyncio.sleep(0.01)
        yield AIMessageChunk(content="，欢迎使用LifePlus。")


@pytest.mark.asyncio
async def test_sse_persists_actual_deltas_and_reconnect_honors_last_event_id(api_client):
    app.state.context.llm = StreamingModel()
    task_id, _ = await submit(api_client, "你好", mode="live")
    await wait_for(api_client, task_id, lambda task: task["status"] == "completed")
    trace = (await api_client.get(f"/api/tasks/{task_id}/trace")).json()
    deltas = [event for event in trace if event["type"] == "answer.delta"]
    assert [event["delta"] for event in deltas] == ["你好", "，欢迎使用LifePlus。"]
    assert deltas[-1]["seq"] < next(event["seq"] for event in trace if event["type"] == "answer.completed")
    response = await api_client.get(f"/api/tasks/{task_id}/events", headers={"Last-Event-ID": str(deltas[0]["seq"])})
    replayed = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert replayed and all(event["seq"] > deltas[0]["seq"] for event in replayed)
    assert sum(event["type"] == "answer.delta" for event in replayed) == 1


@pytest.mark.parametrize("domain,action", [("navigation", "navigation_search"), ("local_life", "local_route")])
@pytest.mark.parametrize("mode", ["walk", "car", "plane", ["walking"]])
def test_task_plan_rejects_invalid_travel_mode_before_execution(domain, action, mode):
    with pytest.raises(ValidationError):
        TaskPlan.model_validate({"tasks": [{"id": "route", "query": "步行过去", "domain": domain,
                                          "action": action, "slots": {"travel_mode": mode}}]})


@pytest.mark.parametrize("mode", ["walking", "driving", "bicycling"])
def test_task_plan_accepts_canonical_travel_modes(mode):
    plan = TaskPlan.model_validate({"tasks": [{"id": "route", "query": "到西湖", "domain": "navigation",
                                             "action": "navigation_search", "slots": {"travel_mode": mode}}]})
    assert plan.tasks[0].slots["travel_mode"] == mode


def test_task_plan_rejects_cycles_and_unknown_dependencies():
    with pytest.raises(ValidationError):
        TaskPlan.model_validate({"tasks": [{"id": "one", "query": "查询天气", "domain": "weather",
                                            "action": "weather_lookup", "depends_on": ["missing"]}]})


@pytest.mark.asyncio
async def test_planned_relative_weather_date_is_normalized_before_tool_execution(api_client, monkeypatch):
    from datetime import datetime
    from car_agent.harness import CHINA_TZ

    async def plan(*_args):
        return TaskPlan.model_validate({"tasks": [{"id": "weather", "query": "无锡今天的天气",
            "domain": "weather", "action": "weather_lookup", "slots": {"city": "无锡", "forecast_date": "today"}}]})

    received = []

    async def dispatch(name, arguments, task_id):
        received.append(arguments)
        return app.state.context.demo.query(name, arguments)

    monkeypatch.setattr("car_agent.orchestration.plan_tasks", plan)
    app.state.context.tool_queue.dispatcher = dispatch
    task_id, _ = await submit(api_client, "无锡今天的天气")
    await wait_for(api_client, task_id, lambda task: task["status"] == "completed")
    assert received[0]["forecast_date"] == datetime.now(CHINA_TZ).date().isoformat()


@pytest.mark.asyncio
async def test_concurrent_duplicate_requests_create_one_task_and_one_tool_job(api_client):
    session = (await api_client.post("/api/sessions")).json()["id"]
    payload = {"message": "杭州天气怎么样", "mode": "demo", "request_id": str(uuid.uuid4())}
    responses = await asyncio.gather(*(api_client.post(f"/api/sessions/{session}/messages", json=payload) for _ in range(8)))
    assert all(response.status_code == 200 for response in responses)
    identifiers = {response.json()["task_id"] for response in responses}
    assert len(identifiers) == 1
    task_id = identifiers.pop()
    await wait_for(api_client, task_id, lambda task: task["status"] == "completed")
    jobs = await app.state.db.fetchall("SELECT * FROM tool_jobs WHERE task_id=?", (task_id,))
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_completed_live_request_can_be_deduplicated_when_model_is_unavailable(api_client):
    app.state.context.llm = StreamingModel()
    session = (await api_client.post("/api/sessions")).json()["id"]
    payload = {"message": "你好", "mode": "live", "request_id": str(uuid.uuid4())}
    first = (await api_client.post(f"/api/sessions/{session}/messages", json=payload)).json()
    await wait_for(api_client, first["task_id"], lambda task: task["status"] == "completed")
    app.state.context.llm = None
    replay = await api_client.post(f"/api/sessions/{session}/messages", json=payload)
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True and replay.json()["task_id"] == first["task_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"message": "上海天气怎么样"}, {"mode": "live"}])
async def test_reusing_request_id_with_different_content_is_rejected(api_client, changed):
    session = (await api_client.post("/api/sessions")).json()["id"]
    payload = {"message": "杭州天气怎么样", "mode": "demo", "request_id": str(uuid.uuid4())}
    first = await api_client.post(f"/api/sessions/{session}/messages", json=payload)
    assert first.status_code == 200
    conflict = await api_client.post(f"/api/sessions/{session}/messages", json={**payload, **changed})
    assert conflict.status_code == 409
    await wait_for(api_client, first.json()["task_id"], lambda task: task["status"] == "completed")


@pytest.mark.asyncio
async def test_invalid_selection_waits_again_without_repeating_search(api_client):
    task_id, _ = await submit(api_client, "搜索周杰伦的歌曲")
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
    response = await api_client.post(f"/api/tasks/{task_id}/resume", json={
        "interrupt_id": task["interrupt"]["id"], "action": "answer", "value": {"index": 999}})
    assert response.status_code == 200
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
    assert task["interrupt"]["kind"] == "selection"
    response = await api_client.post(f"/api/tasks/{task_id}/resume", json={
        "interrupt_id": task["interrupt"]["id"], "action": "answer", "value": {"index": 1}})
    assert response.status_code == 200
    await wait_for(api_client, task_id, lambda task: task["status"] == "completed")
    assert len(await app.state.db.fetchall("SELECT id FROM tool_jobs WHERE task_id=?", (task_id,))) == 1


@pytest.mark.asyncio
async def test_stale_interrupt_or_wrong_action_does_not_consume_pending_approval(api_client):
    task_id, _ = await submit(api_client, "保存行程杭州一日游")
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
    assert task["interrupt"]["kind"] == "approval"
    stale = await api_client.post(f"/api/tasks/{task_id}/resume", json={
        "interrupt_id": "expired-interrupt", "action": "approve", "value": {}})
    assert stale.status_code == 409
    wrong = await api_client.post(f"/api/tasks/{task_id}/resume", json={
        "interrupt_id": task["interrupt"]["id"], "action": "answer", "value": {"text": "杭州"}})
    assert wrong.status_code == 422
    current = (await api_client.get(f"/api/tasks/{task_id}")).json()
    assert current["status"] == "waiting_user" and current["interrupt"]["id"] == task["interrupt"]["id"]
    rejected = await api_client.post(f"/api/tasks/{task_id}/resume", json={
        "interrupt_id": task["interrupt"]["id"], "action": "reject", "value": {}})
    assert rejected.status_code == 200
    await wait_for(api_client, task_id, lambda task: task["status"] == "cancelled")
    assert not await app.state.db.fetchall("SELECT id FROM tool_jobs WHERE task_id=?", (task_id,))


@pytest.mark.asyncio
async def test_failed_dependency_does_not_execute_downstream_write(api_client, monkeypatch):
    async def plan(*_args):
        return TaskPlan.model_validate({"tasks": [
            {"id": "weather", "query": "杭州天气", "domain": "weather", "action": "weather_lookup", "slots": {"city": "杭州"}},
            {"id": "save", "query": "保存天气到行程", "domain": "planning", "action": "save_itinerary",
             "slots": {"title": "天气行程"}, "depends_on": ["weather"], "bind_slots": {"details": "weather.result"}}]})

    calls = []

    async def dispatch(name, arguments, task_id):
        calls.append(name)
        return {"error": "故障注入：天气服务不可用", "retryable": False}

    monkeypatch.setattr("car_agent.orchestration.plan_tasks", plan)
    app.state.context.tool_queue.dispatcher = dispatch
    task_id, _ = await submit(api_client, "查询天气，然后保存行程")
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "failed")
    assert calls == ["maps_weather"]
    assert task["children"][1]["status"] == "failed"
    assert "前置任务" in task["children"][1]["answer"]
    events = (await api_client.get(f"/api/tasks/{task_id}/trace")).json()
    assert not any(event["type"] == "subtask.task.started" and event["child_task_id"] == task["children"][1]["id"] for event in events)


@pytest.mark.asyncio
async def test_cancelling_group_prevents_late_resume_and_new_tools(api_client):
    task_id, _ = await submit(api_client, "杭州天气怎么样，同时搜索陶喆的歌曲")
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
    music = task["children"][1]
    cancelled = await api_client.post(f"/api/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    denied = await api_client.post(f"/api/tasks/{music['id']}/resume", json={
        "interrupt_id": music["interrupt"]["id"], "action": "answer", "value": {"index": 1}})
    assert denied.status_code == 409
    parent = (await api_client.get(f"/api/tasks/{task_id}")).json()
    assert [child["status"] for child in parent["children"]] == ["completed", "cancelled"]
    assert parent["status"] == "cancelled"


@pytest.mark.asyncio
async def test_task_trace_graph_resume_and_cancel_are_isolated_by_user(api_client):
    task_id, _ = await submit(api_client, "搜索陶喆的歌曲")
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
    headers = {"X-Demo-User": "different-user"}
    for suffix in ("", "/trace", "/graph", "/events"):
        assert (await api_client.get(f"/api/tasks/{task_id}{suffix}", headers=headers)).status_code == 404
    assert (await api_client.post(f"/api/tasks/{task_id}/cancel", headers=headers)).status_code == 404
    assert (await api_client.post(f"/api/tasks/{task_id}/resume", headers=headers, json={
        "interrupt_id": task["interrupt"]["id"], "action": "answer", "value": {"index": 1}})).status_code == 404
    await api_client.post(f"/api/tasks/{task_id}/cancel")


@pytest.mark.parametrize("tasks", [
    [{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}],
    [{"id": "a"}, {"id": "a"}],
    [{"id": "a", "bind_slots": {"details": "b.result"}}, {"id": "b"}],
    [{"id": f"t{index}"} for index in range(7)],
])
def test_task_plan_rejects_invalid_dag_and_excess_tasks(tasks):
    with pytest.raises(ValidationError):
        TaskPlan.model_validate({"tasks": [{"query": "查天气", "domain": "weather", "action": "weather_lookup", **task} for task in tasks]})


@pytest.mark.asyncio
async def test_sse_replays_more_than_one_database_page_and_uses_highest_cursor(api_client):
    task_id, _ = await submit(api_client, "杭州天气怎么样")
    await wait_for(api_client, task_id, lambda task: task["status"] == "completed")
    before = (await api_client.get(f"/api/tasks/{task_id}")).json()["last_seq"]
    for index in range(205):
        await app.state.db.publish(task_id, "test.event", {"index": index})
    response = await api_client.get(f"/api/tasks/{task_id}/events?after_seq={before}")
    replayed = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert len(replayed) == 205
    assert [event["index"] for event in replayed] == list(range(205))
    response = await api_client.get(f"/api/tasks/{task_id}/events?after_seq={before + 203}",
                                    headers={"Last-Event-ID": str(before + 201)})
    assert [json.loads(line[6:])["index"] for line in response.text.splitlines() if line.startswith("data: ")] == [203, 204]


@pytest.mark.asyncio
async def test_missing_live_model_returns_error_without_creating_demo_task(api_client):
    app.state.context.llm = None
    session = (await api_client.post("/api/sessions")).json()["id"]
    response = await api_client.post(f"/api/sessions/{session}/messages", json={"message": "你好", "mode": "live"})
    assert response.status_code == 503
    assert not (await api_client.get(f"/api/tasks?session_id={session}")).json()


@pytest.mark.asyncio
async def test_interrupted_response_stream_is_replaced_with_tool_evidence(api_client):
    class Model:
        def with_structured_output(self, schema, **kwargs):
            class Router:
                async def ainvoke(self, messages):
                    if schema.__name__ == "DomainRoute":
                        return schema.model_validate({"domain": "weather"})
                    return schema.model_validate({"domain": "weather", "action": "weather_lookup", "tool_name": "maps_weather", "slots": {"city": "杭州"}})
            return Router()

        async def astream(self, messages):
            yield AIMessageChunk(content="尚未生成完的文本")
            raise RuntimeError("injected model stream interruption")

    async def dispatch(name, arguments, task_id):
        return {"ok": True, "city": "杭州", "date": arguments["forecast_date"],
                "day_weather": "小雨", "low": 18, "high": 23, "source": "controlled_test_fixture"}

    app.state.context.llm = Model()
    app.state.context.tool_queue.dispatcher = dispatch
    task_id, _ = await submit(api_client, "杭州天气怎么样", mode="live")
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "completed")
    events = (await api_client.get(f"/api/tasks/{task_id}/trace")).json()
    assert any(event["type"] == "answer.delta" for event in events)
    replacement = next(event for event in events if event["type"] == "answer.replaced")
    assert replacement["answer"] == task["answer"]
    assert "杭州" in task["answer"] and "尚未生成完" not in task["answer"]


@pytest.mark.asyncio
async def test_health_rag_emits_retrieval_and_safety_handoff_events(api_client):
    task_id, _ = await submit(api_client, "最近睡不好，怎么改善睡眠")
    await wait_for(api_client, task_id, lambda task: task["status"] == "completed")
    trace = (await api_client.get(f"/api/tasks/{task_id}/trace")).json()
    assert any(event["type"] == "rag.retrieved" for event in trace)
    risk_task, _ = await submit(api_client, "胸痛而且呼吸困难")
    await wait_for(api_client, risk_task, lambda task: task["status"] == "completed")
    risk_trace = (await api_client.get(f"/api/tasks/{risk_task}/trace")).json()
    handoff = next(event for event in risk_trace if event["type"] == "human.handoff.required")
    assert handoff["summary"]["risk_signals"]


@pytest.mark.asyncio
async def test_local_search_clarifies_anchor_selects_bound_poi_and_loads_detail(api_client, monkeypatch):
    monkeypatch.setattr(settings, "default_current_location", "")
    task_id, _ = await submit(api_client, "找附近的电影院")
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
    assert task["interrupt"]["field"] == "anchor"
    await api_client.post(f"/api/tasks/{task_id}/resume", json={"interrupt_id": task["interrupt"]["id"],
        "action": "answer", "value": {"text": "东南大学无锡国际校区"}})
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user" and task["interrupt"]["kind"] == "selection")
    await api_client.post(f"/api/tasks/{task_id}/resume", json={"interrupt_id": task["interrupt"]["id"],
        "action": "answer", "value": {"index": 2}})
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "completed")
    jobs = await app.state.db.fetchall("SELECT tool_name,args_json FROM tool_jobs WHERE task_id=?", (task_id,))
    detail = next(job for job in jobs if job["tool_name"] == "maps_place_detail")
    assert json.loads(detail["args_json"])["poi_id"] == "local-2"
    assert "离线演示数据" in task["answer"]


@pytest.mark.asyncio
async def test_local_route_uses_selected_id_without_researching_venues(api_client, monkeypatch):
    async def plan(*args):
        return TaskPlan.model_validate({"tasks": [{"id": "route", "query": "规划所选商家的路线", "domain": "local_life", "action": "local_route",
            "slots": {"poi_id": "local-2", "anchor": "120.26,31.47", "travel_mode": "walking"}}]})
    monkeypatch.setattr("car_agent.orchestration.plan_tasks", plan)
    task_id, _ = await submit(api_client, "规划商家路线")
    await wait_for(api_client, task_id, lambda task: task["status"] == "completed")
    jobs = await app.state.db.fetchall("SELECT tool_name,args_json FROM tool_jobs WHERE task_id=? ORDER BY created_at", (task_id,))
    assert [job["tool_name"] for job in jobs] == ["maps_place_detail", "maps_route"]
    assert json.loads(jobs[1]["args_json"])["destination"] == "120.21,31.47"
    assert json.loads(jobs[1]["args_json"])["travel_mode"] == "walking"


@pytest.mark.asyncio
async def test_weather_and_local_life_run_independently(api_client):
    task_id, _ = await submit(api_client, "杭州天气怎么样，同时找东南大学附近的电影院")
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
    assert [(child["domain"], child["status"]) for child in task["children"]] == [("weather", "completed"), ("local_life", "waiting_user")]
    await api_client.post(f"/api/tasks/{task_id}/cancel")


@pytest.mark.asyncio
async def test_local_form_uses_validated_parameters_without_llm_routing(api_client, monkeypatch):
    async def forbidden(*args):
        raise AssertionError("A structured form must not ask the LLM to reinterpret its parameters")
    monkeypatch.setattr("car_agent.orchestration.plan_tasks", forbidden)
    session = (await api_client.post("/api/sessions")).json()["id"]
    body = {"session_id": session, "action": "local_search", "mode": "demo", "request_id": str(uuid.uuid4()),
            "parameters": {"category": "ktv", "anchor": "东南大学无锡国际校区", "radius_m": 5000, "min_rating": 4.2, "max_cost": 100}}
    response = await api_client.post("/api/local-life/tasks", json=body)
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    task = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
    job = await app.state.db.fetchone("SELECT args_json FROM tool_jobs WHERE task_id=?", (task_id,))
    args = json.loads(job["args_json"])
    assert args["radius_m"] == 5000 and args["min_rating"] == 4.2 and args["max_cost"] == 100
    duplicate = await api_client.post("/api/local-life/tasks", json=body)
    assert duplicate.json()["duplicate"] is True
    conflict = await api_client.post("/api/local-life/tasks", json={**body, "parameters": {**body["parameters"], "max_cost": 80}})
    assert conflict.status_code == 409
    events = (await api_client.get(f"/api/tasks/{task_id}/trace")).json()
    assert any(event.get("source") == "validated_form" for event in events)
    await api_client.post(f"/api/tasks/{task_id}/cancel")


@pytest.mark.asyncio
@pytest.mark.parametrize("action,parameters", [("local_search", {"category": "cinema", "anchor": "某校区", "radius_m": 100001}),
                                               ("local_route", {"poi_id": "actual", "anchor": "某校区", "travel_mode": "plane"}),
                                               ("local_detail", {"poi_id": "actual", "idempotency_key": "injected"})])
async def test_local_form_rejects_bad_arguments_before_creating_task(api_client, action, parameters):
    session = (await api_client.post("/api/sessions")).json()["id"]
    response = await api_client.post("/api/local-life/tasks", json={"session_id": session, "mode": "demo", "action": action, "parameters": parameters})
    assert response.status_code == 422
    assert not (await api_client.get(f"/api/tasks?session_id={session}")).json()


@pytest.mark.asyncio
async def test_invalid_anchor_selection_keeps_the_task_resumable(api_client):
    calls = []
    async def dispatch(name, arguments, task_id):
        if name == "maps_local_search":
            calls.append(dict(arguments))
            if arguments["anchor"] == "东南大学":
                return {"error": "参考地点存在歧义", "retryable": False, "requires_clarification": True,
                        "candidates": [{"name": "校区一", "location": "120.26,31.47"}, {"name": "校区二", "location": "120.3,31.5"}]}
            result = app.state.context.demo.query(name, arguments)
            result["items"] = result["items"][:1]
            return result
        return app.state.context.demo.query(name, arguments)
    app.state.context.tool_queue.dispatcher = dispatch
    session = (await api_client.post("/api/sessions")).json()["id"]
    response = await api_client.post("/api/local-life/tasks", json={"session_id": session, "action": "local_search", "mode": "demo",
        "parameters": {"category": "cinema", "anchor": "东南大学"}})
    task_id = response.json()["task_id"]
    for index in (999, 2):
        task = await wait_for(api_client, task_id, lambda task: task["status"] == "waiting_user")
        assert task["interrupt"]["field"] == "anchor"
        await api_client.post(f"/api/tasks/{task_id}/resume", json={"interrupt_id": task["interrupt"]["id"], "action": "answer", "value": {"index": index}})
    await wait_for(api_client, task_id, lambda task: task["status"] == "completed")
    assert len(calls) == 2 and calls[1]["anchor"] == "120.3,31.5"


@pytest.mark.asyncio
async def test_waiting_group_restores_independent_interrupt_after_restart(tmp_path):
    original = settings.database_path
    settings.database_path = str(tmp_path / "restart.sqlite3")
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                task_id, _ = await submit(client, "杭州天气怎么样，同时搜索陶喆的歌曲")
                group = await wait_for(client, task_id, lambda task: task["status"] == "waiting_user")
                music_id = group["children"][1]["id"]
                interrupt_id = group["children"][1]["interrupt"]["id"]
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                restored = (await client.get(f"/api/tasks/{music_id}")).json()
                assert restored["interrupt"]["id"] == interrupt_id
                response = await client.post(f"/api/tasks/{music_id}/resume", json={
                    "interrupt_id": interrupt_id, "action": "answer", "value": {"index": 1}})
                assert response.status_code == 200
                group = await wait_for(client, task_id, lambda task: task["status"] == "completed")
                assert all(child["status"] == "completed" for child in group["children"])
                assert len((await app.state.db.fetchall("SELECT * FROM tool_jobs WHERE tool_name='maps_weather'"))) == 1
    finally:
        settings.database_path = original
