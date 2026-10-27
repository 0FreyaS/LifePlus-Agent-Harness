"""Fault injection at trusted boundaries, not live-provider or load-test results."""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from car_agent.adapters.mcp_client import LocalMCPClient
from car_agent.persistence import Database
from car_agent.task_planning import TaskPlan
from car_agent.tool_queue import ToolQueue


@pytest.fixture
async def database(tmp_path):
    db = Database(str(tmp_path / "industrial.sqlite3"))
    await db.open()
    yield db
    await db.close()


def plan(slots=None, **changes):
    task = {"id": "w", "query": "查无锡天气", "domain": "weather", "action": "weather_lookup",
            "slots": slots or {"city": "无锡"}, **changes}
    return {"tasks": [task]}


@pytest.mark.parametrize("slots", [
    pytest.param({"city": "无锡", "user_id": "another-owner"}, id="forged-owner"),
    pytest.param({"city": "无锡", "tool_name": "create_reminder"}, id="injected-tool"),
    pytest.param({"city": "无锡", "selection": 1}, id="preselected-candidate"),
    pytest.param({"city": ["无锡"]}, id="wrong-city-type"),
    pytest.param({"radius_m": -100}, id="negative-radius"),
    pytest.param({"radius_m": 1000000}, id="oversized-radius"),
    pytest.param({"min_rating": 8}, id="invalid-rating"),
    pytest.param({"max_cost": -1}, id="negative-budget"),
    pytest.param({"category": "payment"}, id="unknown-category"),
])
def test_plan_rejects_unknown_or_invalid_slots(slots):
    with pytest.raises(ValidationError):
        TaskPlan.model_validate(plan(slots))


@pytest.mark.parametrize("target", ["user_id", "selection", "tool_name"])
def test_plan_rejects_unknown_binding_target(target):
    raw = plan()
    raw["tasks"].append({"id": "s", "query": "保存天气", "domain": "planning", "action": "save_itinerary",
                         "slots": {"title": "天气"}, "depends_on": ["w"],
                         "bind_slots": {target: "w.result"}})
    with pytest.raises(ValidationError):
        TaskPlan.model_validate(raw)


def test_plan_rejects_duplicate_dependency_edges():
    raw = plan()
    raw["tasks"].append({"id": "s", "query": "保存天气", "domain": "planning", "action": "save_itinerary",
                         "depends_on": ["w", "w"], "bind_slots": {"details": "w.result"}})
    with pytest.raises(ValidationError):
        TaskPlan.model_validate(raw)


def test_valid_plan_keeps_missing_fields_for_clarification_and_opaque_details():
    raw = {"tasks": [{"id": "r", "query": "提醒我出门", "domain": "planning", "action": "create_reminder",
                      "slots": {"title": "出门"}},
                     {"id": "s", "query": "保存行程", "domain": "planning", "action": "save_itinerary",
                      "slots": {"details": {"source": "user", "note": "原样保存"}}}]}
    result = TaskPlan.model_validate(raw)
    assert "remind_at" not in result.tasks[0].slots
    assert result.tasks[1].slots["details"]["note"] == "原样保存"


@pytest.mark.parametrize("receipt", [
    pytest.param({"success": False}, id="business-false-without-error"),
    pytest.param({"success": False, "message": "quota unavailable"}, id="business-false-message"),
    pytest.param({"ok": "false", "items": []}, id="string-false"),
    pytest.param({"success": "false", "items": []}, id="string-success"),
    pytest.param({"ok": 1, "items": []}, id="integer-success-flag"),
    pytest.param({"ok": True, "success": False}, id="contradictory-flags"),
    pytest.param({"ok": True, "requires_clarification": "false"}, id="string-clarification"),
])
async def test_failed_or_invalid_receipts_never_complete_the_job(database, receipt):
    calls = []

    async def dispatch(*_):
        calls.append(1)
        return receipt

    queue = ToolQueue(database, dispatch, database.publish)
    await queue.start()
    try:
        result = await queue.execute(task_id="failed", tool_name="maps_search", arguments={},
                                     idempotency_key="failed", total_budget_seconds=1)
        assert result.get("queue_status") == "dead_letter"
        assert len(calls) == 1
        row = await database.fetchone("SELECT status FROM tool_jobs WHERE idempotency_key='failed'")
        assert row["status"] != "completed"
        assert len(await database.fetchall("SELECT * FROM tool_dead_letters")) == 1
    finally:
        await queue.close()


@pytest.mark.parametrize("tool", ["save_itinerary", "create_reminder", "cancel_reminder"])
async def test_explicit_business_rejection_of_writes_is_never_retried(database, tool):
    calls = []

    async def dispatch(*_):
        calls.append(1)
        return {"success": False, "error_code": "UPSTREAM_HTTP", "retryable": True}

    queue = ToolQueue(database, dispatch, database.publish)
    await queue.start()
    try:
        result = await queue.execute(task_id="write", tool_name=tool, arguments={}, idempotency_key="write",
                                     total_budget_seconds=1)
        assert result.get("queue_status") == "dead_letter"
        assert len(calls) == 1
    finally:
        await queue.close()


@pytest.mark.parametrize("receipt", [
    {"ok": True, "items": []},
    {"success": True, "items": []},
    {"ok": False, "error": "ambiguous", "requires_clarification": True,
     "field": "origin", "candidates": [{"name": "A"}, {"name": "B"}]},
])
async def test_empty_results_and_real_clarification_are_not_network_retries(database, receipt):
    calls = []

    async def dispatch(*_):
        calls.append(1)
        return receipt

    queue = ToolQueue(database, dispatch, database.publish)
    await queue.start()
    try:
        result = await queue.execute(task_id="valid", tool_name="maps_search", arguments={}, idempotency_key="valid")
        assert result.get("queue_status") is None
        assert len(calls) == 1
        assert bool(result.get("requires_clarification")) == bool(receipt.get("requires_clarification"))
    finally:
        await queue.close()


class Session:
    def __init__(self, payload):
        self.payload = payload

    async def call_tool(self, *_):
        return self.payload


@pytest.mark.parametrize("text", ["not json", "<html>proxy error</html>", "{\"ok\":", "delete_everything()"])
async def test_mcp_malformed_reply_is_not_accepted_as_a_completed_text_result(text):
    payload = SimpleNamespace(isError=False, content=[SimpleNamespace(text=text)])
    with pytest.raises((ValueError, TypeError)):
        await LocalMCPClient._invoke(Session(payload), "maps_search", {})


async def test_mcp_structured_content_is_used_without_requiring_text_content():
    expected = {"ok": True, "items": [{"id": "public-poi"}]}
    payload = SimpleNamespace(isError=False, structuredContent=expected, content=[])
    assert await LocalMCPClient._invoke(Session(payload), "maps_search", {}) == expected


async def test_burst_respects_worker_bound_and_duplicate_jobs_share_one_dispatch(database):
    active, peak = 0, 0
    calls = []

    async def dispatch(_name, args, _task):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(args["n"])
        try:
            await asyncio.sleep(0.01)
            return {"ok": True, "value": args["n"]}
        finally:
            active -= 1

    queue = ToolQueue(database, dispatch, database.publish, workers=3)
    await queue.start()
    try:
        jobs = await asyncio.gather(*(queue.submit(task_id="burst", tool_name="custom", arguments={"n": n % 12},
                                                  idempotency_key=f"burst-{n % 12}") for n in range(36)))
        results = await asyncio.gather(*(queue.wait(job, 3) for job in jobs))
        assert len(set(jobs)) == 12
        assert len(calls) == 12
        assert sorted(calls) == list(range(12))
        assert 1 < peak <= 3
        assert all(result["ok"] for result in results)
        assert not await database.fetchall("SELECT * FROM tool_jobs WHERE status IN ('queued','running')")
    finally:
        await queue.close()
