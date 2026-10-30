"""Explicit audit tests; run by adversarial_eval, not silently added to model scores."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from datetime import datetime, timezone

import httpx
import pytest

from car_agent.api.app import app, settings
from car_agent.freshness import FreshnessPolicy, stale_receipt
from car_agent.orchestration import TaskCoordinator
from car_agent.persistence import Database
from car_agent.tool_queue import ToolQueue
from car_agent.tools import LifeTools


@pytest.fixture
async def db(tmp_path):
    value = Database(str(tmp_path / "audit.sqlite3"))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
async def client(tmp_path, monkeypatch):
    for key, value in {"database_path": str(tmp_path / "api.sqlite3"), "chroma_path": str(tmp_path / "chroma"),
                       "app_mode": "local", "redis_url": None, "mysql_dsn": None, "checkpoint_postgres_dsn": None}.items():
        monkeypatch.setattr(settings, key, value)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://audit",
                                     headers={"X-Demo-User": "audit-user"}) as value:
            yield value


async def pending(client, task_id, *, previous=None, terminal=None):
    for _ in range(200):
        result = (await client.get(f"/api/tasks/{task_id}")).json()
        if terminal and result["status"] == terminal:
            return result
        if not terminal and result["status"] == "waiting_user" and result.get("interrupt", {}).get("id") != previous:
            return result
        await asyncio.sleep(.01)
    raise AssertionError(result)


async def save_task(client):
    session = (await client.post("/api/sessions")).json()["id"]
    result = await client.post(f"/api/sessions/{session}/messages", json={"message": "保存行程周末出行", "mode": "demo"})
    assert result.status_code == 200
    return await pending(client, result.json()["task_id"])


@pytest.mark.asyncio
async def test_changed_arguments_reject_old_approval(monkeypatch):
    from car_agent.harness import build_domain_graph

    before = {"title": "周末出行", "details": {"travel_mode": "driving"}}
    after = {**before, "details": {"travel_mode": "bicycling"}}
    old_key = hashlib.sha256(json.dumps(["write", "planning", "save_itinerary", before], sort_keys=True).encode()).hexdigest()
    prompts, events = [], []

    class WaitingAgain(Exception):
        pass

    def interrupt(value):
        prompts.append(value)
        if len(prompts) == 1:
            return {"action": "approve", "operation_key": old_key}
        raise WaitingAgain()

    monkeypatch.setattr("car_agent.harness.interrupt", interrupt)
    monkeypatch.setattr("car_agent.harness.emit", lambda kind, **kw: events.append(kind))
    authorize = build_domain_graph("planning").nodes["authorize"].bound.afunc
    with pytest.raises(WaitingAgain):
        await authorize({"task_id": "write", "action": "save_itinerary", "tool_name": "save_itinerary", "tool_args": after}, {}, None)
    assert len(prompts) == 2 and prompts[-1]["operation_key"] != old_key
    assert prompts[-1]["arguments"] == after and "approval.invalid" in events


@pytest.mark.asyncio
async def test_stale_dependency_is_not_silently_forwarded():
    receipt = FreshnessPolicy().receipt("maps_weather", {"city": "无锡", "day_weather": "晴"},
                                       "2000-01-01T00:00:00+00:00")
    assert stale_receipt(receipt)

    class Graph:
        async def aget_state(self, config):
            return SimpleNamespace(values={"tool_result": receipt, "slots": {}})

    coordinator = TaskCoordinator(SimpleNamespace(state=SimpleNamespace(db=None, graph=Graph())), None)
    with pytest.raises(ValueError, match="过期|stale|expired"):
        await coordinator._bind({"domain": "planning", "bind_slots": {"details": "w.result"}},
                                {"w": {"thread_id": "weather-thread"}})


@pytest.mark.asyncio
async def test_real_write_lost_receipt_is_not_duplicated(db, tmp_path):
    tools = LifeTools(str(tmp_path / "audit.sqlite3"))
    arguments = {"user_id": "audit-user", "title": "丢失回执行程", "idempotency_key": "remote-key", "details": {"city": "无锡"}}
    calls = []

    async def dispatch(name, args, task_id):
        calls.append(name)
        result = await tools.call(name, args)
        assert result["ok"]
        raise ConnectionError("injected lost receipt after commit")

    queue = ToolQueue(db, dispatch, db.publish, default_retries=2)
    await queue.start()
    try:
        spec = dict(task_id="write", tool_name="save_itinerary", arguments=arguments, idempotency_key="job-key")
        first = await queue.execute(**spec)
        again = await queue.execute(**spec)
        assert first["queue_status"] == again["queue_status"] == "dead_letter"
        assert calls == ["save_itinerary"]
        records = await tools.call("list_itineraries", {"user_id": "audit-user"})
        assert len(records["items"]) == 1
        reconciled = await tools.call("save_itinerary", arguments)
        assert reconciled["item_id"] == records["items"][0]["id"]
        assert len((await tools.call("list_itineraries", {"user_id": "audit-user"}))["items"]) == 1
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_transient_rate_limit_retries_then_succeeds(db):
    calls = []
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES(?,?,?,?,?)", ("s", "audit-user", "audit", now, now))
    await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     ("read", "s", "audit-user", "搜索西湖", "running", "demo", now, now))

    async def dispatch(name, args, task_id):
        calls.append(name)
        if len(calls) == 1:
            return {"ok": False, "error": "injected 429", "error_code": "UPSTREAM_HTTP", "retryable": True,
                    "retry_after_seconds": .01}
        return {"ok": True, "items": [{"id": "real-read-fixture", "name": "西湖"}]}

    queue = ToolQueue(db, dispatch, db.publish, default_retries=1)
    await queue.start()
    try:
        result = await queue.execute(task_id="read", tool_name="maps_search", arguments={"keywords": "西湖"},
                                     idempotency_key="read-key", total_budget_seconds=3)
        assert result["ok"] and len(calls) == 2
        assert (await db.fetchone("SELECT attempt,status FROM tool_jobs"))["attempt"] == 2
        assert not await db.fetchall("SELECT * FROM tool_dead_letters")
        events = await db.fetchall("SELECT * FROM events WHERE task_id='read'")
        assert any(row["type"] == "tool.retry_scheduled" for row in events)
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_two_approvals_create_one_real_write(client):
    original = await save_task(client)
    calls = []
    tools = LifeTools(settings.database_path)

    async def dispatch(name, args, task_id):
        calls.append(name)
        return await tools.call(name, args)

    app.state.context.tool_queue.dispatcher = dispatch
    payload = {"interrupt_id": original["interrupt"]["id"], "action": "approve", "value": {}}
    responses = await asyncio.gather(*(client.post(f"/api/tasks/{original['id']}/resume", json=payload) for _ in range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    await pending(client, original["id"], terminal="completed")
    assert calls == ["save_itinerary"]
    assert len(await app.state.db.fetchall("SELECT * FROM itinerary_items WHERE user_id='audit-user'")) == 1


@pytest.mark.parametrize("operation,expected", [("delete", 0), ("shrink", 1)])
def test_rag_reopen_removes_deleted_or_shortened_chunks(tmp_path, operation, expected):
    from car_agent.rag import KnowledgeRetriever

    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    source = knowledge / "policy.md"
    source.write_text("# 当前服务规则\n新规则文本\n# 已废弃条款\n旧条款特有暗号旧版餐饮优惠")
    store = tmp_path / "index"
    before = KnowledgeRetriever(store, knowledge)
    before.open()
    if before.backend != "chroma":
        pytest.skip("Chroma unavailable in host interpreter; separate container audit required")
    assert before.collection.count() == 2
    if operation == "delete":
        source.unlink()
    else:
        source.write_text("# 当前服务规则\n新规则文本")
    after = KnowledgeRetriever(store, knowledge)
    after.open()
    actual = after.collection.count()
    stale = any("旧条款特有暗号" in item["text"] for item in after.search("旧条款特有暗号", limit=3))
    assert actual == expected and not stale, {"before": 2, "expected": expected, "after": actual, "stale_retrievable": stale}
