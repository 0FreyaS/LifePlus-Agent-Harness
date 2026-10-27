import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from langchain_core.messages import AIMessage

from car_agent.api.app import app, settings
from car_agent.conversation import followup_decision, load_references, preference_command, save_reference, asks_first_input
from car_agent.freshness import FreshnessPolicy
from car_agent.memory import MemoryRepository
from car_agent.persistence import Database


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "conversation.sqlite3"))
    monkeypatch.setattr(settings, "chroma_path", str(tmp_path / "chroma"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-Demo-User": "conversation-test"}) as client:
            yield client


async def send(client, message, session=None, **values):
    session = session or (await client.post("/api/sessions")).json()["id"]
    response = await client.post(f"/api/sessions/{session}/messages", json={"message": message, "mode": "demo", **values})
    assert response.status_code == 200, response.text
    return response.json(), session


async def wait(client, identifier, status):
    for _ in range(150):
        task = (await client.get(f"/api/tasks/{identifier}")).json()
        if task["status"] == status:
            return task
        if task["status"] == "failed":
            raise AssertionError(task)
        await asyncio.sleep(0.02)
    raise AssertionError(task)


@pytest.mark.asyncio
async def test_weather_followup_retains_city_but_requeries_date(client):
    first, session = await send(client, "杭州今天天气怎么样")
    await wait(client, first["task_id"], "completed")
    second, _ = await send(client, "那明天呢？", session)
    done = await wait(client, second["task_id"], "completed")
    tomorrow = (datetime.now(timezone(timedelta(hours=8))).date() + timedelta(days=1)).isoformat()
    assert "杭州" in done["answer"] and tomorrow in done["answer"]
    events = (await client.get(f"/api/tasks/{second['task_id']}/trace")).json()
    assert any(event["type"] == "context.followup_resolved" and event["source_task_id"] == first["task_id"] for event in events)
    assert any(event["type"] == "tool.started" and event["arguments"].get("forecast_date") == tomorrow for event in events)


@pytest.mark.asyncio
async def test_city_clarification_can_be_answered_in_the_composer(client):
    first, session = await send(client, "明天天气怎么样")
    await wait(client, first["task_id"], "waiting_user")
    reply, _ = await send(client, "杭州", session)
    assert reply["resumed"] and reply["task_id"] == first["task_id"]
    done = await wait(client, first["task_id"], "completed")
    assert "杭州" in done["answer"]


@pytest.mark.asyncio
async def test_composer_selection_resumes_bound_task_once_and_is_in_history(client):
    first, session = await send(client, "搜索周杰伦的歌曲")
    pending = await wait(client, first["task_id"], "waiting_user")
    selected = pending["interrupt"]["candidates"][1]
    url = f"/api/sessions/{session}/messages"
    payload = {"message": "第二个", "mode": "demo", "request_id": "selection-once"}
    results = await asyncio.gather(client.post(url, json=payload), client.post(url, json=payload))
    assert all(result.status_code == 200 and result.json()["task_id"] == first["task_id"] for result in results)
    assert sum(bool(result.json().get("duplicate")) for result in results) == 1
    done = await wait(client, first["task_id"], "completed")
    assert selected["name"] in done["answer"]
    assert len((await client.get(f"/api/tasks?session_id={session}")).json()) == 1
    history = (await client.get(f"/api/sessions/{session}/messages")).json()
    assert sum(item["content"] == "第二个" for item in history) == 1
    mismatch = await client.post(url, json={**payload, "message": "第一个"})
    assert mismatch.status_code == 409


@pytest.mark.asyncio
async def test_navigation_followup_binds_real_selected_coordinate(client):
    first, session = await send(client, "从公司导航去西湖")
    pending = await wait(client, first["task_id"], "waiting_user")
    candidate = pending["interrupt"]["candidates"][1]
    await send(client, "第二个", session)
    await wait(client, first["task_id"], "completed")
    route, _ = await send(client, "骑行过去", session)
    await wait(client, route["task_id"], "completed")
    trace = (await client.get(f"/api/tasks/{route['task_id']}/trace")).json()
    calls = [event for event in trace if event["type"] == "tool.started"]
    assert len(calls) == 1 and calls[0]["tool_name"] == "maps_route"
    assert calls[0]["arguments"]["destination"] == candidate["location"]
    assert calls[0]["arguments"]["travel_mode"] == "bicycling"
    fresh_session = (await client.post("/api/sessions")).json()["id"]
    other, _ = await send(client, "导航过去", fresh_session)
    task = await wait(client, other["task_id"], "waiting_user")
    assert task["interrupt"]["field"] == "destination"


@pytest.mark.asyncio
async def test_multiple_waiting_lists_do_not_guess_and_expired_candidates_are_rejected(client):
    first, session = await send(client, "搜索周杰伦的歌曲")
    await wait(client, first["task_id"], "waiting_user")
    second, _ = await send(client, "从公司导航去西湖", session)
    await wait(client, second["task_id"], "waiting_user")
    response = await client.post(f"/api/sessions/{session}/messages", json={"message": "第二个", "mode": "demo"})
    assert response.status_code == 409
    for identifier in [first["task_id"], second["task_id"]]:
        task = (await client.get(f"/api/tasks/{identifier}")).json()
        pending = {**task["interrupt"], "expires_at": "2000-01-01T00:00:00+00:00"}
        await app.state.db.execute("UPDATE tasks SET interrupt_json=? WHERE id=?", (json.dumps(pending), identifier))
    assert (await client.post(f"/api/sessions/{session}/messages", json={"message": "第二个", "mode": "demo"})).status_code == 409


@pytest.mark.asyncio
async def test_explicit_preference_persists_across_sessions_and_reaches_live_chat(client):
    saved, session = await send(client, "记住：出行偏好=骑行")
    await wait(client, saved["task_id"], "completed")
    second_session = (await client.post("/api/sessions")).json()["id"]
    preferences = (await client.get(f"/api/sessions/{second_session}/memories")).json()
    assert any(item["key"] == "出行偏好" and item["value"] == "骑行" for item in preferences)
    captured = []

    class Model:
        def with_structured_output(self, schema, **_kwargs):
            class Router:
                async def ainvoke(self, messages):
                    captured.append(messages)
                    return schema.model_validate({"domain": "chat", "action": "chat_answer", "slots": {}})
            return Router()

        async def ainvoke(self, messages):
            captured.append(messages)
            return AIMessage(content="你保存的出行偏好是骑行。")

    app.state.context.llm = Model()
    response = await client.post(f"/api/sessions/{second_session}/messages", json={"message": "我的出行偏好是什么", "mode": "live"})
    await wait(client, response.json()["task_id"], "completed")
    assert len(captured) >= 2
    assert all("出行偏好" in messages[0]["content"] and "骑行" in messages[0]["content"] for messages in captured)
    removed, _ = await send(client, "忘记偏好：出行偏好", session)
    await wait(client, removed["task_id"], "completed")
    assert (await client.get(f"/api/sessions/{second_session}/memories")).json() == []
    assert (await client.get(f"/api/sessions/{session}/memories", headers={"X-Demo-User": "another-user"})).status_code == 404


@pytest.mark.asyncio
async def test_reference_restart_isolation_and_expiry(tmp_path):
    db = Database(str(tmp_path / "references.sqlite3"))
    await db.open()
    await db.execute("INSERT INTO tasks(id,user_id,session_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     ("source", "u", "s", "测试地点", "completed", "demo", "2026-10-26T00:00:00+00:00", "2026-10-26T00:00:00+00:00"))
    state = {"task_id": "source", "user_id": "u", "session_id": "s", "domain": "navigation", "status": "completed",
             "slots": {"selection": 1}, "tool_result": {"items": [{"name": "西湖", "location": "120.1,30.2"}]}}
    await save_reference(db, state, FreshnessPolicy())
    await db.close()
    db = Database(str(tmp_path / "references.sqlite3"))
    await db.open()
    try:
        assert len(await load_references(db, "u", "s")) == 1
        assert await load_references(db, "u", "other") == []
        assert await load_references(db, "other", "s") == []
        await db.execute("UPDATE conversation_references SET expires_at='2000-01-01T00:00:00+00:00'")
        assert await load_references(db, "u", "s") == []
    finally:
        await db.close()


def test_reference_ambiguity_and_explicit_memory_contract():
    refs = [{"source_task_id": "a", "domain": "navigation", "selected": {"location": "120.1,30.2"}},
            {"source_task_id": "b", "domain": "navigation", "selected": {"location": "121.1,31.2"}}]
    assert followup_decision("导航过去", refs)["unresolved_reference"]
    assert preference_command("我喜欢骑行") is None
    assert preference_command("记住：出行偏好=骑行") == ("put", "出行偏好", "骑行")
    assert preference_command("忘记偏好：出行偏好") == ("delete", "出行偏好", "")
    assert not asks_first_input("第一次去苏州旅游，有什么推荐")
    assert not asks_first_input("你好，这是第一轮的独特输入")
    assert asks_first_input("我第一次输入了什么")


@pytest.mark.asyncio
async def test_first_input_is_exact_after_window_trim_and_context_expiry(client):
    original = "你好，这是第一轮的独特输入：我想了解苏州园林。"
    first, session = await send(client, original)
    await wait(client, first["task_id"], "completed")
    await app.state.db.execute("UPDATE tasks SET created_at='2000-01-01T00:00:00+00:00',updated_at='2000-01-01T00:00:01+00:00' WHERE id=?", (first["task_id"],))
    for index in range(8):
        item, _ = await send(client, f"你好，这是后续第{index + 2}轮", session)
        await wait(client, item["task_id"], "completed")
    last, _ = await send(client, "请原样返回我第一次输入的query", session)
    result = await wait(client, last["task_id"], "completed")
    assert result["answer"] == "本次会话的第一条输入是：\n" + original
    trace = (await client.get(f"/api/tasks/{last['task_id']}/trace")).json()
    assert any(event["type"] == "conversation.history_retrieved" and event["source_task_id"] == first["task_id"] for event in trace)
    other, _ = await send(client, "我第一次输入了什么？")
    result = await wait(client, other["task_id"], "completed")
    assert original not in result["answer"]
