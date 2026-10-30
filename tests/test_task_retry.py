import asyncio

import httpx
import pytest
from langchain_core.messages import AIMessageChunk
from openai import APIConnectionError

from car_agent.api.app import app, settings
from car_agent.task_planning import GoalPlan, TaskPlan


@pytest.fixture
async def client(tmp_path):
    old = settings.database_path
    old_input_event_provider = settings.input_event_provider
    settings.database_path = str(tmp_path / "retry.sqlite3")
    settings.input_event_provider = "llm"
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://retry-test", headers={"X-Demo-User": "retry-user"}) as client:
                yield client
    finally:
        settings.database_path = old
        settings.input_event_provider = old_input_event_provider


class Provider:
    def __init__(self, fail_route=True):
        self.online = False
        self.fail_route = fail_route

    def with_structured_output(self, schema, **_):
        parent = self

        class Model:
            async def ainvoke(self, _):
                if parent.fail_route and not parent.online:
                    raise APIConnectionError(request=httpx.Request("POST", "https://model.invalid"))
                if schema is GoalPlan:
                    return {"tasks": [{"id": "chat", "query": "你好", "domain": "chat"}]}
                return schema.model_validate({"domain": "chat"})
        return Model()

    async def astream(self, messages):
        if not self.online and "回答自然、简洁" in messages[0]["content"]:
            raise APIConnectionError(request=httpx.Request("POST", "https://model.invalid"))
        yield AIMessageChunk(content="受控模型已恢复")


async def submit(client, query):
    session = (await client.post("/api/sessions")).json()["id"]
    response = await client.post(f"/api/sessions/{session}/messages", json={"message": query, "mode": "live"})
    assert response.status_code == 200
    return response.json()["task_id"]


async def wait(client, task_id, status):
    for _ in range(200):
        task = (await client.get(f"/api/tasks/{task_id}")).json()
        if task["status"] == status:
            return task
        await asyncio.sleep(.02)
    raise AssertionError(task)


@pytest.mark.asyncio
async def test_failed_route_resumes_checkpoint_once_and_preserves_identity(client):
    provider = Provider()
    app.state.context.llm = provider
    task_id = await submit(client, "你好")
    failed = await wait(client, task_id, "failed")
    assert failed["can_retry"]
    provider.online = True
    responses = await asyncio.gather(*(client.post(f"/api/tasks/{task_id}/retry") for _ in range(5)))
    assert sorted(response.status_code for response in responses) == [200, 409, 409, 409, 409]
    done = await wait(client, task_id, "completed")
    assert done["thread_id"] == failed["thread_id"] and done["mode"] == "live"
    assert not done["can_retry"]
    assert (await client.post(f"/api/tasks/{task_id}/retry")).status_code == 409
    assert (await client.post(f"/api/tasks/{task_id}/retry", headers={"X-Demo-User": "other"})).status_code == 404
    events = (await client.get(f"/api/tasks/{task_id}/trace")).json()
    assert sum(event["type"] == "task.retry_requested" for event in events) == 1
    assert sum(event["type"] == "memory.loaded" for event in events) == 1


@pytest.mark.asyncio
async def test_plan_connection_failure_retries_without_a_checkpoint(client, monkeypatch):
    calls = 0

    async def plan(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise APIConnectionError(request=httpx.Request("POST", "https://model.invalid"))
        return TaskPlan(tasks=[{"id": "chat", "query": "你好", "domain": "chat", "action": "chat_answer"}])

    monkeypatch.setattr("car_agent.orchestration.plan_tasks", plan)
    provider = Provider(); provider.online = True
    app.state.context.llm = provider
    task_id = await submit(client, "你好，同时介绍自己")
    await wait(client, task_id, "failed")
    response = await client.post(f"/api/tasks/{task_id}/retry")
    assert response.status_code == 200
    await wait(client, task_id, "completed")
    assert calls == 2


@pytest.mark.asyncio
async def test_retry_failed_branch_does_not_reexecute_successful_weather(client, monkeypatch):
    async def plan(*_):
        return TaskPlan(tasks=[
            {"id": "weather", "query": "查杭州天气", "domain": "weather", "action": "weather_lookup", "slots": {"city": "杭州"}},
            {"id": "chat", "query": "讲笑话", "domain": "chat", "action": "chat_answer"}])

    monkeypatch.setattr("car_agent.orchestration.plan_tasks", plan)
    provider = Provider(fail_route=False)
    app.state.context.llm = provider
    calls = []

    async def dispatch(name, args, task_id):
        calls.append(name)
        return {"ok": True, "city": "杭州", "date": args["forecast_date"], "day_weather": "晴", "low": 10, "high": 20}

    app.state.context.tool_queue.dispatcher = dispatch
    task_id = await submit(client, "查杭州天气，同时讲笑话")
    partial = await wait(client, task_id, "partial")
    assert partial["can_retry"] and calls == ["maps_weather"]
    completed_id = partial["children"][0]["id"]
    provider.online = True
    response = await client.post(f"/api/tasks/{task_id}/retry")
    assert response.status_code == 200 and response.json()["retried_task_ids"] == [partial["children"][1]["id"]]
    done = await wait(client, task_id, "completed")
    assert done["children"][0]["id"] == completed_id
    assert calls == ["maps_weather"]


@pytest.mark.asyncio
async def test_retry_unblocks_unstarted_dependents_and_still_requires_write_confirmation(client, monkeypatch):
    async def plan(*_):
        return TaskPlan(tasks=[
            {"id": "chat", "query": "生成行程说明", "domain": "chat", "action": "chat_answer"},
            {"id": "save", "query": "保存行程", "domain": "planning", "action": "save_itinerary", "slots": {"title": "出游"},
             "depends_on": ["chat"], "bind_slots": {"details": "chat.result"}}])

    monkeypatch.setattr("car_agent.orchestration.plan_tasks", plan)
    provider = Provider(fail_route=False)
    app.state.context.llm = provider
    writes = []

    async def dispatch(name, args, task_id):
        writes.append(name)
        return {"ok": True, "status": "saved", "item_id": args["idempotency_key"], "title": args["title"]}

    app.state.context.tool_queue.dispatcher = dispatch
    task_id = await submit(client, "先生成说明，然后保存行程")
    failed = await wait(client, task_id, "failed")
    assert all(child["status"] == "failed" for child in failed["children"])
    provider.online = True
    response = await client.post(f"/api/tasks/{task_id}/retry")
    assert response.status_code == 200 and len(response.json()["dependent_reset_ids"]) == 1
    pending = await wait(client, task_id, "waiting_user")
    assert writes == []
    save = pending["children"][1]
    assert save["interrupt"]["kind"] == "approval"
    await client.post(f"/api/tasks/{save['id']}/resume", json={"interrupt_id": save["interrupt"]["id"], "action": "approve"})
    await wait(client, task_id, "completed")
    assert writes == ["save_itinerary"]


@pytest.mark.asyncio
async def test_tool_failure_is_not_blindly_retried_even_if_message_mentions_connection(client):
    provider = Provider(); provider.online = True
    app.state.context.llm = provider
    # Simulate a failed external write receipt, not a recoverable LLM exception.
    task_id = await submit(client, "你好")
    await wait(client, task_id, "completed")
    await app.state.db.execute("UPDATE tasks SET status='failed' WHERE id=?", (task_id,))
    await app.state.db.publish(task_id, "task.failed", {"error": "ToolFailure: APIConnectionError: write outcome unknown"})
    assert (await client.get(f"/api/tasks/{task_id}")).json()["can_retry"] is False
    assert (await client.post(f"/api/tasks/{task_id}/retry")).status_code == 409


@pytest.mark.asyncio
async def test_cancelled_task_is_never_restarted(client):
    provider = Provider(); provider.online = True
    app.state.context.llm = provider
    task_id = await submit(client, "你好")
    await wait(client, task_id, "completed")
    await app.state.db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,))
    await app.state.db.publish(task_id, "task.failed", {"error": "APIConnectionError: old failure"})
    assert (await client.post(f"/api/tasks/{task_id}/retry")).status_code == 409
