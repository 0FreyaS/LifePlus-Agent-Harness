import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessageChunk

from car_agent.api.app import app, settings
from car_agent.freshness import utcnow
from car_agent.input_events import InputDecision, grounded_decision, rule_decision, event_messages


def waiting(identifier="music", kind="selection", domain="music", field=None):
    return {"id": identifier, "query": "搜索周杰伦歌曲" if domain == "music" else "查询地点", "domain": domain,
            "status": "waiting_user", "interrupt_json": json.dumps({"id": "i-" + identifier, "kind": kind,
                "field": field, "expires_at": (utcnow() + timedelta(minutes=5)).isoformat(),
                "candidates": [{"name": "晴天"}, {"name": "七里香"}]})}


@pytest.mark.parametrize("query,route,answer,new_query", [
    ("第二个", "resume", "第二个", None),
    ("第二首", "resume", "第二首", None),
    ("第二个，同时查杭州天气", "resume", "第二个", "查杭州天气"),
    ("第二个顺便搜索陶喆歌曲", "resume", "第二个", "搜索陶喆歌曲"),
    ("取消任务", "cancel", "取消任务", None),
    ("查杭州天气", "new", None, "查杭州天气"),
])
def test_high_precision_rules(query, route, answer, new_query):
    result = rule_decision(query, [waiting()], ["杭州", "无锡"])
    assert (result.route, result.answer, result.new_query) == (route, answer, new_query)


@pytest.mark.parametrize("query", ["第二个吧，杭州那边的天气也帮我看看", "我想要七里香那首，再看看杭州那边天气", "歌曲选第二首"])
def test_unconsumed_text_falls_through_to_context_model(query):
    assert rule_decision(query, [waiting()], ["杭州"]) is None


def test_two_selection_lists_never_guess_from_recency():
    result = rule_decision("第二个", [waiting(), waiting("place", domain="navigation")], [])
    assert result.route == "clarify"


@pytest.mark.parametrize("query,proposal", [
    ("第二个", {"route": "resume", "task_id": "foreign", "answer": "第二个"}),
    ("第二个", {"route": "resume", "task_id": "music", "answer": "第三个"}),
    ("第二个吧，杭州天气也看看", {"route": "resume", "task_id": "music", "answer": "第二个吧"}),
    ("第二个吧，杭州天气也看看", {"route": "resume", "task_id": "music", "answer": "第二个吧", "new_query": "查杭州天气"}),
    ("第二个，同时不要查天气", {"route": "resume", "task_id": "music", "answer": "第二个", "new_query": "不要查天气"}),
    ("第二个吧，杭州天气也看看", {"route": "new", "new_query": "杭州天气也看看"}),
    ("第二个吧，杭州天气也看看", {"route": "new", "new_query": "第二个吧，杭州天气也看看"}),
    ("我选七里香那首，再查杭州天气", {"route": "resume", "task_id": "music", "answer": "我选七里香那首，再查杭州天气"}),
])
def test_host_rejects_invented_or_dropped_spans_and_wrong_target(query, proposal):
    assert grounded_decision(InputDecision(**proposal), query, [waiting()]).route == "clarify"


def test_model_cannot_turn_approval_text_into_write_authorization():
    task = waiting("write", kind="approval", domain="planning")
    decision = InputDecision(route="resume", task_id="write", answer="好的")
    assert grounded_decision(decision, "好的", [task]).route == "clarify"


def test_domain_qualified_reply_can_bind_one_of_two_lists():
    decision = InputDecision(route="resume", task_id="music", answer="第二首")
    assert grounded_decision(decision, "歌曲选第二首", [waiting(), waiting("place", domain="navigation")]).route == "resume"


def test_domain_label_inside_answer_is_normalized_only_for_the_bound_domain():
    proposal = InputDecision(route="resume", task_id="music", answer="歌曲选第二首")
    result = grounded_decision(proposal, "歌曲选第二首", [waiting(), waiting("place", domain="navigation")])
    assert result.route == "resume" and result.answer == "选第二首"
    wrong = proposal.model_copy(update={"task_id": "place"})
    assert grounded_decision(wrong, "歌曲选第二首", [waiting(), waiting("place", domain="navigation")]).route == "clarify"


def test_new_demand_before_old_answer_does_not_bypass_event_interpretation():
    text = "查杭州天气，歌曲选第二首"
    assert rule_decision(text, [waiting()], []) is None
    proposal = InputDecision(route="resume", task_id="music", answer="歌曲选第二首", new_query="查杭州天气")
    assert grounded_decision(proposal, text, [waiting()]).route == "resume"


def test_polite_choice_wrappers_do_not_hide_a_second_business_command():
    proposal = InputDecision(route="resume", task_id="music", answer="七里香", new_query="再看看杭州天气")
    result = grounded_decision(proposal, "我想要七里香那首，再看看杭州天气", [waiting()])
    assert result.route == "resume"


def test_focus_snapshot_does_not_offer_other_tasks_to_the_model():
    data = json.loads(event_messages("第二个", [waiting(), waiting("place", domain="navigation")], [], "music")[-1]["content"])
    assert [task["task_id"] for task in data["waiting"]] == ["music"]


class Model:
    def __init__(self, proposal=None, error=None, pause=False):
        self.proposal, self.error, self.pause = proposal, error, pause
        self.event_calls = 0
        self.started, self.release = asyncio.Event(), asyncio.Event()

    def with_structured_output(self, schema, **kwargs):
        parent = self

        class Call:
            async def ainvoke(self, messages):
                if schema is InputDecision:
                    parent.event_calls += 1
                    parent.started.set()
                    if parent.pause:
                        await parent.release.wait()
                    if parent.error:
                        raise parent.error
                    return parent.proposal
                music = "搜索周杰伦" in messages[-1]["content"]
                if schema.__name__ == "DomainRoute":
                    return schema.model_validate({"domain": "music" if music else "weather"})
                if music:
                    return schema.model_validate({"domain": "music", "action": "music_search", "tool_name": "search_music", "slots": {"keyword": "周杰伦"}})
                return schema.model_validate({"domain": "weather", "action": "weather_lookup", "tool_name": "maps_weather", "slots": {"city": "杭州"}})
        return Call()

    async def astream(self, messages):
        yield AIMessageChunk(content="受控回复")


@pytest.fixture
async def client(tmp_path, monkeypatch):
    for field, value in {"database_path": str(tmp_path / "input.sqlite3"), "chroma_path": str(tmp_path / "chroma"),
                          "model_api_key": None, "redis_url": None, "mysql_dsn": None,
                          "checkpoint_postgres_dsn": None, "app_mode": "local"}.items():
        monkeypatch.setattr(settings, field, value)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers={"X-Demo-User": "event-user"}) as client:
            yield client


async def until(client, task_id, status):
    for _ in range(200):
        task = (await client.get(f"/api/tasks/{task_id}")).json()
        if task["status"] == status:
            return task
        await asyncio.sleep(.01)
    raise AssertionError(task)


async def pending_music(client, live=False):
    if live:
        install_model("pending")
    session = (await client.post("/api/sessions")).json()["id"]
    reply = await client.post(f"/api/sessions/{session}/messages", json={"message": "搜索周杰伦歌曲", "mode": "live" if live else "demo"})
    task = await until(client, reply.json()["task_id"], "waiting_user")
    return session, task


def install_model(task_id, **kwargs):
    model = Model({"route": "resume", "task_id": task_id, "answer": "第二个吧", "new_query": "杭州那边的天气也帮我看看"}, **kwargs)
    app.state.context.llm = model
    app.state.context.model_roles = {}

    async def controlled_tool(name, args, task_id):
        if name == "search_music":
            return {"ok": True, "items": [{"id": "song1", "name": "晴天", "url": "https://y.qq.com/n/ryqq/songDetail/one"},
                                          {"id": "song2", "name": "七里香", "url": "https://y.qq.com/n/ryqq/songDetail/two"}], "source": "controlled-test"}
        return {"ok": True, "city": args["city"], "date": args["forecast_date"], "day_weather": "晴", "low": 10, "high": 20, "source": "controlled-test"}
    app.state.context.tool_queue.dispatcher = controlled_tool
    return model


@pytest.mark.parametrize("entry", ["composer", "card"])
async def test_complex_reply_resumes_old_and_creates_new_with_audit(client, entry):
    session, task = await pending_music(client, live=True)
    model = install_model(task["id"])
    query = "第二个吧，杭州那边的天气也帮我看看"
    if entry == "composer":
        response = await client.post(f"/api/sessions/{session}/messages", json={"message": query, "mode": "live", "request_id": "complex-once"})
    else:
        response = await client.post(f"/api/tasks/{task['id']}/resume", json={"interrupt_id": task["interrupt"]["id"], "action": "answer", "value": {"text": query}})
    assert response.status_code == 200, response.text
    assert response.json()["event_source"] == "llm"
    await until(client, task["id"], "completed")
    await until(client, response.json()["created_task_id"], "completed")
    from car_agent.interactions import effective_values
    snapshot = await app.state.graph.aget_state({"configurable": {"thread_id": task["thread_id"]}}, subgraphs=True)
    assert effective_values(snapshot)["slots"]["selection"] == 2
    audit = (await client.get(f"/api/sessions/{session}/input-events")).json()
    assert audit[0]["decision"]["new_query"] == "杭州那边的天气也帮我看看"
    assert any(e["type"] == "model.attempt.completed" for e in audit[0]["attempts"])
    assert model.event_calls == 1
    assert (await client.get(f"/api/sessions/{session}/input-events", headers={"X-Demo-User": "other"})).status_code == 404
    if entry == "composer":
        repeat = await client.post(f"/api/sessions/{session}/messages", json={"message": query, "mode": "live", "request_id": "complex-once"})
        assert repeat.json()["duplicate"]
        assert repeat.json()["created_task_id"] == response.json()["created_task_id"]
        assert model.event_calls == 1


async def test_rule_compound_is_supported_in_composer_without_model(client):
    session, task = await pending_music(client)
    response = await client.post(f"/api/sessions/{session}/messages", json={"message": "第二个，同时查杭州天气", "mode": "demo"})
    assert response.status_code == 200
    assert response.json()["event_source"] == "rules"
    await until(client, task["id"], "completed")
    await until(client, response.json()["created_task_id"], "completed")


@pytest.mark.parametrize("failure", ["format", "provider", "timeout", "foreign", "dropped"])
async def test_uncertain_or_failed_interpreter_never_mutates_tasks(client, failure):
    session, task = await pending_music(client)
    model = install_model(task["id"], error=RuntimeError("controlled network error") if failure == "provider" else None,
                          pause=failure == "timeout")
    if failure == "format":
        model.proposal = {"route": "approve", "tool_name": "save_itinerary"}
    if failure == "foreign":
        model.proposal["task_id"] = "another-users-task"
    if failure == "dropped":
        model.proposal["new_query"] = None
    if failure == "timeout":
        app.state.input_event_timeout = .02
    response = await client.post(f"/api/sessions/{session}/messages", json={"message": "第二个吧，杭州那边的天气也帮我看看", "mode": "live"})
    assert response.status_code == (503 if failure in {"format", "provider", "timeout"} else 409), response.text
    current = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert current["status"] == "waiting_user"
    assert current["interrupt"]["id"] == task["interrupt"]["id"]
    assert len((await client.get(f"/api/tasks?session_id={session}")).json()) == 1
    assert model.event_calls == (2 if failure == "format" else 1)
    audit = (await client.get(f"/api/sessions/{session}/input-events")).json()[0]
    assert audit["status"] == ("failed" if response.status_code == 503 else "completed")


async def test_state_change_during_model_call_is_revalidated(client):
    session, task = await pending_music(client)
    model = install_model(task["id"], pause=True)
    pending = asyncio.create_task(client.post(f"/api/sessions/{session}/messages", json={"message": "第二个吧，杭州那边的天气也帮我看看", "mode": "live"}))
    await asyncio.wait_for(model.started.wait(), 2)
    assert (await client.post(f"/api/tasks/{task['id']}/cancel")).status_code == 200
    model.release.set()
    response = await pending
    assert response.status_code == 409
    assert (await client.get(f"/api/tasks/{task['id']}")).json()["status"] == "cancelled"
    assert len((await client.get(f"/api/tasks?session_id={session}")).json()) == 1


async def test_partial_creation_failure_is_visible_and_duplicate_does_not_resume_again(client, monkeypatch):
    session, task = await pending_music(client)
    async def unavailable(*args, **kwargs):
        raise HTTPException(429, "controlled admission limit")
    monkeypatch.setattr("car_agent.api.app.enqueue_message", unavailable)
    payload = {"message": "第二个，同时查杭州天气", "mode": "demo", "request_id": "partial"}
    first = await client.post(f"/api/sessions/{session}/messages", json=payload)
    assert first.status_code == 200
    assert first.json()["additional_task_error"]["query"] == "查杭州天气"
    repeated = await client.post(f"/api/sessions/{session}/messages", json=payload)
    assert repeated.json()["duplicate"]
    events = (await client.get(f"/api/tasks/{task['id']}/trace")).json()
    assert sum(e["type"] == "user.resumed" for e in events) == 1


async def test_natural_approval_uses_protocol_gate_without_calling_model(client):
    session = (await client.post("/api/sessions")).json()["id"]
    submitted = await client.post(f"/api/sessions/{session}/messages", json={"message": "保存行程标题是测试行程", "mode": "demo"})
    task = await until(client, submitted.json()["task_id"], "waiting_user")
    assert task["interrupt"]["kind"] == "approval"
    model = Model({"route": "resume", "task_id": task["id"], "answer": "好的"})
    app.state.context.llm, app.state.context.model_roles = model, {}
    response = await client.post(f"/api/sessions/{session}/messages", json={"message": "好的", "mode": "live"})
    assert response.status_code == 409
    assert model.event_calls == 0
    assert (await client.get(f"/api/tasks/{task['id']}")).json()["status"] == "waiting_user"
    assert not await app.state.db.fetchall("SELECT * FROM tool_jobs WHERE tool_name='save_itinerary'")


async def test_expired_card_never_calls_model_or_consumes_interrupt(client):
    session, task = await pending_music(client)
    model = install_model(task["id"])
    pending = {**task["interrupt"], "expires_at": "2000-01-01T00:00:00+00:00"}
    await app.state.db.execute("UPDATE tasks SET mode='live',interrupt_json=? WHERE id=?", (json.dumps(pending), task["id"]))
    response = await client.post(f"/api/tasks/{task['id']}/resume", json={"interrupt_id": pending["id"], "action": "answer",
                                  "value": {"text": "第二个吧，杭州那边的天气也帮我看看"}})
    assert response.status_code == 409 and model.event_calls == 0
    assert (await client.get(f"/api/tasks/{task['id']}")).json()["interrupt"]["id"] == pending["id"]


@pytest.mark.parametrize("text", ["a" * 4001, {"index": 2}, "bad\x00text"])
async def test_invalid_text_is_rejected_before_event_model(client, text):
    _, task = await pending_music(client)
    model = install_model(task["id"])
    response = await client.post(f"/api/tasks/{task['id']}/resume", json={"interrupt_id": task["interrupt"]["id"], "action": "answer", "value": {"text": text}})
    assert response.status_code == 422 and model.event_calls == 0
    assert (await client.get(f"/api/tasks/{task['id']}")).json()["status"] == "waiting_user"
