import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from car_agent.api.app import app, settings
from car_agent.execution_verification import verify_execution
from car_agent.freshness import FreshnessPolicy, expired, recent, stale_receipt
from car_agent.memory import MemoryRepository
from car_agent.models import ChatRequest
from car_agent.orchestration import session_messages
from car_agent.persistence import Database
from car_agent.tool_queue import ToolQueue


NOW = datetime(2026, 10, 24, 8, tzinfo=timezone.utc)


@pytest.mark.parametrize("age,valid", [(0, True), (299, True), (300, False), (301, False), (-6, False)])
def test_recent_has_a_real_age_boundary(age, valid):
    assert recent((NOW - timedelta(seconds=age)).isoformat(), 300, NOW) is valid


@pytest.mark.parametrize("value", [None, "", "not-a-date", "2026-10-24T08:00:00", "2026-10-24T07:59:59+00:00"])
def test_missing_malformed_or_old_expiry_is_not_authorized(value):
    assert expired({"expires_at": value}, NOW)


def test_receipt_and_interrupt_use_the_earliest_expiry(monkeypatch):
    monkeypatch.setattr("car_agent.freshness.utcnow", lambda: NOW)
    policy = FreshnessPolicy(selection_seconds=300)
    receipt = policy.receipt("maps_route", {"distance_m": 1000}, NOW.isoformat())
    assert not stale_receipt(receipt, NOW)
    assert stale_receipt(receipt, NOW + timedelta(seconds=120))
    interrupt = policy.interrupt("one", {"kind": "selection"}, {"tool_result": receipt})
    assert interrupt["expires_at"] == receipt["_freshness"]["expires_at"]


def test_verifier_rejects_stale_evidence_even_when_weather_fields_exist():
    result = FreshnessPolicy().receipt("maps_weather", {"city": "无锡", "date": "2026-10-24", "day_weather": "晴"},
                                       "2000-01-01T00:00:00+00:00")
    report = verify_execution({"domain": "weather", "mode": "live", "tool_result": result})
    assert report["enforce_failure"] and "stale_tool_receipt" in report["problems"]


@pytest.mark.parametrize("text", ["   ", "\x00查询天气", "天气\x01", "x" * 4001])
def test_query_rejects_empty_oversized_and_control_input(text):
    with pytest.raises(ValidationError):
        ChatRequest(message=text)


def test_query_normalizes_without_removing_negation():
    assert ChatRequest(message="  不要播放歌曲\n只查杭州天气  ").message == "不要播放歌曲\n只查杭州天气"
    assert ChatRequest(message="cafe\u0301").message == "café"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "policy.sqlite3"))
    await database.open()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def client(tmp_path):
    old = settings.database_path
    old_input_event_provider = settings.input_event_provider
    settings.database_path = str(tmp_path / "api-policy.sqlite3")
    settings.input_event_provider = "llm"
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://policy",
                                         headers={"X-Demo-User": "policy-user"}) as client:
                yield client
    finally:
        settings.database_path = old
        settings.input_event_provider = old_input_event_provider


async def submit(client, query, session=None, request_id=None):
    session = session or (await client.post("/api/sessions")).json()["id"]
    response = await client.post(f"/api/sessions/{session}/messages", json={
        "message": query, "mode": "demo", "request_id": request_id})
    assert response.status_code == 200, response.text
    return response.json()["task_id"], session


async def wait(client, task_id, status="waiting_user", old_interrupt=None):
    for _ in range(200):
        task = (await client.get(f"/api/tasks/{task_id}")).json()
        if task["status"] == status and (not old_interrupt or task.get("interrupt", {}).get("id") != old_interrupt):
            return task
        await asyncio.sleep(.02)
    raise AssertionError(task)


async def expire_pending(task):
    pending = {**task["interrupt"], "expires_at": "2000-01-01T00:00:00+00:00"}
    await app.state.db.execute("UPDATE tasks SET interrupt_json=? WHERE id=?", (json.dumps(pending), task["id"]))


@pytest.mark.asyncio
async def test_expired_selection_is_blocked_then_explicit_refresh_requeries(client):
    calls = []

    async def dispatch(name, arguments, task_id):
        calls.append(name)
        version = len(calls)
        return {"items": [{"id": f"{version}-{index}", "name": f"候选{version}-{index}", "singer": "歌手"}
                          for index in (1, 2)]}

    app.state.context.tool_queue.dispatcher = dispatch
    task_id, _ = await submit(client, "搜索周杰伦歌曲")
    original = await wait(client, task_id)
    assert original["interrupt"]["expires_at"] and not original["interrupt"]["expired"]
    await expire_pending(original)
    payload = {"interrupt_id": original["interrupt"]["id"], "action": "answer", "value": {"index": 2}}
    rejected = await client.post(f"/api/tasks/{task_id}/resume", json=payload)
    assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "INTERRUPT_EXPIRED"
    assert len(calls) == 1
    refreshed = await client.post(f"/api/tasks/{task_id}/refresh", json={"interrupt_id": original["interrupt"]["id"]})
    assert refreshed.status_code == 200, refreshed.text
    current = await wait(client, task_id, old_interrupt=original["interrupt"]["id"])
    assert current["thread_id"] == original["thread_id"]
    assert len(calls) == 2 and current["interrupt"]["candidates"][0]["id"] == "2-1"
    assert (await client.post(f"/api/tasks/{task_id}/resume", json=payload)).status_code == 409
    result = await client.post(f"/api/tasks/{task_id}/resume", json={**payload, "interrupt_id": current["interrupt"]["id"]})
    assert result.status_code == 200
    done = await wait(client, task_id, "completed")
    assert "候选2-2" in done["answer"]


@pytest.mark.asyncio
async def test_expired_approval_cannot_write_and_refresh_requires_new_approval(client):
    task_id, _ = await submit(client, "保存行程无锡出游")
    old = await wait(client, task_id)
    assert old["interrupt"]["kind"] == "approval"
    await expire_pending(old)
    payload = {"interrupt_id": old["interrupt"]["id"], "action": "approve", "value": {}}
    assert (await client.post(f"/api/tasks/{task_id}/resume", json=payload)).status_code == 409
    assert not await app.state.db.fetchall("SELECT * FROM tool_jobs WHERE task_id=?", (task_id,))
    response = await client.post(f"/api/tasks/{task_id}/refresh", json={"interrupt_id": old["interrupt"]["id"]})
    assert response.status_code == 200, response.text
    fresh = await wait(client, task_id, old_interrupt=old["interrupt"]["id"])
    assert fresh["interrupt"]["kind"] == "approval"
    assert not await app.state.db.fetchall("SELECT * FROM tool_jobs WHERE task_id=?", (task_id,))
    assert (await client.post(f"/api/tasks/{task_id}/resume", json={**payload, "interrupt_id": fresh["interrupt"]["id"]})).status_code == 200
    await wait(client, task_id, "completed")
    assert len(await app.state.db.fetchall("SELECT * FROM tool_jobs WHERE task_id=?", (task_id,))) == 1


@pytest.mark.asyncio
async def test_expired_approval_can_be_rejected_without_refresh(client):
    task_id, _ = await submit(client, "保存行程无锡出游")
    task = await wait(client, task_id)
    await expire_pending(task)
    response = await client.post(f"/api/tasks/{task_id}/resume", json={"interrupt_id": task["interrupt"]["id"], "action": "reject"})
    assert response.status_code == 200
    await wait(client, task_id, "cancelled")
    assert not await app.state.db.fetchall("SELECT * FROM tool_jobs WHERE task_id=?", (task_id,))


@pytest.mark.asyncio
async def test_refresh_preserves_previously_clarified_origin(client, monkeypatch):
    from car_agent.config import get_settings
    monkeypatch.setattr(get_settings(), "default_current_location", "")
    task_id, _ = await submit(client, "导航到东南大学")
    first = await wait(client, task_id)
    assert first["interrupt"]["field"] == "origin"
    await client.post(f"/api/tasks/{task_id}/resume", json={"interrupt_id": first["interrupt"]["id"], "action": "answer", "value": {"text": "无锡站"}})
    choices = await wait(client, task_id, old_interrupt=first["interrupt"]["id"])
    assert choices["interrupt"]["kind"] == "selection"
    response = await client.post(f"/api/tasks/{task_id}/refresh", json={"interrupt_id": choices["interrupt"]["id"]})
    assert response.status_code == 200, response.text
    refreshed = await wait(client, task_id, old_interrupt=choices["interrupt"]["id"])
    assert refreshed["interrupt"]["kind"] == "selection"
    state = await app.state.graph.aget_state({"configurable": {"thread_id": refreshed["thread_id"]}})
    assert state.values["slots"]["origin"] == "无锡站"


@pytest.mark.asyncio
async def test_cross_day_relative_query_requires_new_explicit_request(client):
    task_id, _ = await submit(client, "明天天气怎么样")
    task = await wait(client, task_id)
    await app.state.db.execute("UPDATE tasks SET created_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", task_id))
    response = await client.post(f"/api/tasks/{task_id}/refresh", json={"interrupt_id": task["interrupt"]["id"]})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "RELATIVE_DATE_EXPIRED"
    response = await client.post(f"/api/tasks/{task_id}/resume", json={"interrupt_id": task["interrupt"]["id"], "action": "answer", "value": {"text": "无锡"}})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "RELATIVE_DATE_EXPIRED"


@pytest.mark.asyncio
async def test_refresh_cannot_cross_user_boundary_or_resurrect_cancelled_task(client):
    task_id, _ = await submit(client, "搜索周杰伦歌曲")
    task = await wait(client, task_id)
    payload = {"interrupt_id": task["interrupt"]["id"]}
    assert (await client.post(f"/api/tasks/{task_id}/refresh", json=payload, headers={"X-Demo-User": "other"})).status_code == 404
    await client.post(f"/api/tasks/{task_id}/cancel")
    assert (await client.post(f"/api/tasks/{task_id}/refresh", json=payload)).status_code == 409


@pytest.mark.asyncio
async def test_concurrent_refresh_accepts_only_one_generation(client):
    task_id, _ = await submit(client, "搜索周杰伦歌曲")
    task = await wait(client, task_id)
    replies = await asyncio.gather(*(client.post(f"/api/tasks/{task_id}/refresh", json={"interrupt_id": task["interrupt"]["id"]}) for _ in range(4)))
    assert sorted(reply.status_code for reply in replies) == [200, 409, 409, 409]
    await wait(client, task_id, old_interrupt=task["interrupt"]["id"])


@pytest.mark.asyncio
async def test_cancel_waits_for_refresh_mutation_then_stops_new_generation(client, monkeypatch):
    task_id, _ = await submit(client, "搜索周杰伦歌曲")
    task = await wait(client, task_id)
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.state.graph.aget_state

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.state.graph, "aget_state", paused)
    refreshing = asyncio.create_task(client.post(f"/api/tasks/{task_id}/refresh", json={"interrupt_id": task["interrupt"]["id"]}))
    await asyncio.wait_for(entered.wait(), 3)
    cancelling = asyncio.create_task(client.post(f"/api/tasks/{task_id}/cancel"))
    await asyncio.sleep(.02)
    assert not cancelling.done()
    release.set()
    assert (await refreshing).status_code == 200
    assert (await cancelling).status_code == 200
    await wait(client, task_id, "cancelled")
    await asyncio.sleep(.03)
    assert (await client.get(f"/api/tasks/{task_id}")).json()["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("late_error", [False, True])
async def test_cancelled_planning_cannot_publish_late_children_or_failure(client, monkeypatch, late_error):
    from car_agent.task_planning import TaskPlan

    entered, release = asyncio.Event(), asyncio.Event()

    async def plan(*_):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        if late_error:
            raise RuntimeError("late planner failure")
        return TaskPlan(tasks=[
            {"id": "one", "query": "天气", "domain": "weather", "action": "weather_lookup", "slots": {"city": "无锡"}},
            {"id": "two", "query": "歌曲", "domain": "music", "action": "music_search", "slots": {"keyword": "周杰伦"}},
        ])

    monkeypatch.setattr("car_agent.orchestration.plan_tasks", plan)
    task_id, _ = await submit(client, "查天气，同时搜索歌曲")
    await asyncio.wait_for(entered.wait(), 3)
    cancelling = asyncio.create_task(client.post(f"/api/tasks/{task_id}/cancel"))
    await wait(client, task_id, "cancelled")
    release.set()
    assert (await cancelling).status_code == 200
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["status"] == "cancelled" and not task.get("children")
    assert not await app.state.db.fetchall("SELECT * FROM tool_jobs WHERE task_id=?", (task_id,))


@pytest.mark.asyncio
async def test_cancel_and_retry_share_the_parent_lifecycle_lock(client, monkeypatch):
    from openai import APIConnectionError
    from car_agent.task_planning import TaskPlan

    class OfflineReply:
        async def astream(self, _messages):
            raise APIConnectionError(request=httpx.Request("POST", "https://model.invalid"))
            yield

    async def plan(*_):
        return TaskPlan(tasks=[
            {"id": "chat", "query": "讲故事", "domain": "chat", "action": "chat_answer"},
            {"id": "music", "query": "搜索歌曲", "domain": "music", "action": "music_search", "slots": {"keyword": "周杰伦"}},
        ])

    monkeypatch.setattr("car_agent.orchestration.plan_tasks", plan)
    app.state.context.llm = OfflineReply()
    session = (await client.post("/api/sessions")).json()["id"]
    response = await client.post(f"/api/sessions/{session}/messages", json={"message": "讲故事，同时搜索歌曲", "mode": "live"})
    task_id = response.json()["task_id"]

    async def dispatch(*_):
        return {"items": [{"id": "one", "name": "一", "url": "https://example.invalid/one"},
                          {"id": "two", "name": "二", "url": "https://example.invalid/two"}]}

    app.state.context.tool_queue.dispatcher = dispatch
    group = await wait(client, task_id)
    child = next(item for item in group["children"] if item["domain"] == "chat")
    assert child["status"] == "failed"
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.state.graph.aget_state

    async def paused(config, **kwargs):
        if config["configurable"]["thread_id"] == child["thread_id"]:
            entered.set()
            await release.wait()
        return await original(config, **kwargs)

    monkeypatch.setattr(app.state.graph, "aget_state", paused)
    retrying = asyncio.create_task(client.post(f"/api/tasks/{child['id']}/retry"))
    await asyncio.wait_for(entered.wait(), 3)
    cancelling = asyncio.create_task(client.post(f"/api/tasks/{task_id}/cancel"))
    await asyncio.sleep(.02)
    assert not cancelling.done()
    release.set()
    assert (await retrying).status_code == 200
    assert (await cancelling).status_code == 200
    done = await wait(client, task_id, "cancelled")
    assert not any(item["status"] in {"queued", "running", "blocked", "waiting_user"} for item in done["children"])


@pytest.mark.asyncio
async def test_recent_history_is_filtered_but_archive_is_not_deleted(client):
    task_id, session = await submit(client, "杭州天气怎么样")
    await wait(client, task_id, "completed")
    await app.state.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", task_id))
    assert not await session_messages(app.state.db, session, completed_only=True, max_age_seconds=1800)
    assert len((await client.get(f"/api/sessions/{session}/messages")).json()) == 2


@pytest.mark.asyncio
async def test_active_user_limit_is_cross_session_and_dedup_still_works(client, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def dispatch(name, args, task_id):
        started.set()
        await release.wait()
        return app.state.context.demo.query(name, args)

    app.state.context.tool_queue.dispatcher = dispatch
    monkeypatch.setattr(settings, "max_active_tasks_per_user", 1)
    task_id, session = await submit(client, "杭州天气怎么样", request_id="quota-dedup")
    await asyncio.wait_for(started.wait(), 3)
    try:
        duplicate = await client.post(f"/api/sessions/{session}/messages", json={"message": "杭州天气怎么样", "mode": "demo", "request_id": "quota-dedup"})
        assert duplicate.status_code == 200 and duplicate.json()["task_id"] == task_id
        other = (await client.post("/api/sessions")).json()["id"]
        rejected = await client.post(f"/api/sessions/{other}/messages", json={"message": "北京天气怎么样", "mode": "demo"})
        assert rejected.status_code == 429 and rejected.headers["retry-after"] == "2"
    finally:
        release.set()
    await wait(client, task_id, "completed")


@pytest.mark.asyncio
async def test_context_expiry_and_temporary_preferences_do_not_erase_audit_data(db):
    memory = MemoryRepository(db, context_seconds=10)
    await memory.save_context("u", "s", [{"role": "user", "content": "旧地点"}], {})
    await db.execute("UPDATE session_contexts SET updated_at=?", ("2000-01-01T00:00:00+00:00",))
    context = await memory.build_context("u", "s", [], {})
    assert context["model_history"] == [] and "旧地点" not in context["memory_brief"]
    await memory.put("u", "当前位置", "过期地址")
    await memory.put("u", "preferred_city", "无锡")
    await db.execute("UPDATE memories SET updated_at=?", ("2000-01-01T00:00:00+00:00",))
    assert await memory.get_map("u") == {"preferred_city": "无锡"}
    assert len(await memory.list("u")) == 2


@pytest.mark.asyncio
async def test_production_identity_does_not_accept_unverified_bearer(client, monkeypatch):
    monkeypatch.setattr(settings, "app_mode", "production")
    response = await client.post("/api/sessions", headers={"Authorization": "Bearer not-a-real-identity"})
    assert response.status_code == 503
    assert (await client.post("/api/sessions")).status_code == 503


@pytest.mark.asyncio
async def test_stale_redis_cache_falls_back_without_extending_durable_age(db):
    class Cache:
        def __init__(self):
            self.payload = {"updated_at": "2000-01-01T00:00:00+00:00", "memory_brief": "失效摘要"}
            self.writes, self.deletes = [], []

        async def get(self, key):
            return json.dumps(self.payload) if self.payload else None

        async def delete(self, key):
            self.deletes.append(key)
            self.payload = None

        async def setex(self, key, ttl, value):
            self.writes.append((ttl, json.loads(value)))

    memory = MemoryRepository(db, context_seconds=100)
    await memory.save_context("u", "s", [{"role": "user", "content": "有效摘要"}], {})
    ago = (datetime.now(timezone.utc) - timedelta(seconds=50)).isoformat()
    await db.execute("UPDATE session_contexts SET updated_at=?", (ago,))
    cache = Cache()
    memory.redis = cache
    context = await memory.build_context("u", "s", [], {})
    assert "失效摘要" not in context["memory_brief"] and "有效摘要" in context["memory_brief"]
    assert cache.deletes and 1 <= cache.writes[0][0] <= 50


@pytest.mark.asyncio
async def test_stale_read_receipt_is_not_reused_as_permanent_idempotent_result(db):
    calls = []

    async def dispatch(name, args, task_id):
        calls.append(name)
        return {"items": [{"id": str(len(calls))}]}

    async def publish(*_):
        return {}

    queue = ToolQueue(db, dispatch, publish)
    await queue.start()
    try:
        spec = {"task_id": "read", "tool_name": "maps_search", "arguments": {"keywords": "学校"}, "idempotency_key": "read-one"}
        first = await queue.execute(**spec)
        assert (await queue.execute(**spec))["items"] == first["items"] and len(calls) == 1
        old = {**first, "_freshness": {**first["_freshness"], "expires_at": "2000-01-01T00:00:00+00:00"}}
        await db.execute("UPDATE tool_jobs SET result_json=? WHERE idempotency_key=?", (json.dumps(old), "read-one"))
        responses = await asyncio.gather(*(queue.execute(**spec) for _ in range(3)))
        assert all(result["items"][0]["id"] == "2" for result in responses) and len(calls) == 2
        assert len(await db.fetchall("SELECT * FROM tool_jobs")) == 2
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_expired_approval_is_checked_when_worker_dispatches_not_only_when_enqueued(db):
    calls = []

    async def dispatch(*args):
        calls.append(args)
        return {"status": "saved"}

    async def publish(*_):
        return {}

    queue = ToolQueue(db, dispatch, publish)
    job = await queue.submit(task_id="write", tool_name="save_itinerary", arguments={}, idempotency_key="expired-write", expires_at="2000-01-01T00:00:00+00:00")
    await queue.start()
    try:
        result = await queue.wait(job)
        assert result["queue_status"] == "dead_letter" and "确认已过期" in result["error"]
        assert calls == []
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_unknown_write_exception_is_not_automatically_retried(db):
    calls = []

    async def dispatch(*args):
        calls.append(args)
        raise TimeoutError("outcome unknown")

    async def publish(*_):
        return {}

    queue = ToolQueue(db, dispatch, publish, default_retries=2)
    await queue.start()
    try:
        result = await queue.execute(task_id="write", tool_name="save_itinerary", arguments={}, idempotency_key="unknown")
        assert result["queue_status"] == "dead_letter" and len(calls) == 1
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_restart_quarantines_unknown_running_write_but_requeues_reads(db):
    async def dispatch(*_):
        return {"items": []}

    async def publish(*_):
        return {}

    queue = ToolQueue(db, dispatch, publish)
    write = await queue.submit(task_id="old-write", tool_name="save_itinerary", arguments={}, idempotency_key="old-write")
    read = await queue.submit(task_id="old-read", tool_name="maps_search", arguments={}, idempotency_key="old-read")
    await db.execute("UPDATE tool_jobs SET status='running'")
    await queue.start()
    try:
        assert (await queue.wait(write))["queue_status"] == "dead_letter"
        assert (await queue.wait(read))["items"] == []
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_legacy_queue_schema_adds_expiry_without_losing_job(tmp_path):
    import aiosqlite
    from car_agent.persistence import SCHEMA

    path = str(tmp_path / "legacy.sqlite3")
    async with aiosqlite.connect(path) as connection:
        await connection.executescript(SCHEMA.replace("  expires_at TEXT,\n", ""))
        await connection.execute(
            "INSERT INTO tool_jobs(id,task_id,tool_name,args_json,idempotency_key,status,available_at,created_at,updated_at) "
            "VALUES('legacy','task','maps_search','{}','legacy-key','completed',?,?,?)", (NOW.isoformat(),) * 3)
        await connection.commit()
    database = Database(path)
    await database.open()
    try:
        row = await database.fetchone("SELECT * FROM tool_jobs WHERE id='legacy'")
        assert row["idempotency_key"] == "legacy-key" and "expires_at" in row and row["expires_at"] is None
    finally:
        await database.close()
