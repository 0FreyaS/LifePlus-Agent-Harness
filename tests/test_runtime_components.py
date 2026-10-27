import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from car_agent.adapters.mcp_client import LocalMCPClient
from car_agent.memory import MemoryRepository
from car_agent.persistence import Database
from car_agent.skill_registry import LifeSkillRegistry
from car_agent.tool_queue import ToolQueue


@pytest.fixture
async def database(tmp_path: Path):
    db = Database(str(tmp_path / "components.sqlite3"))
    await db.open()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_tool_queue_runs_independent_jobs_concurrently(database):
    started = 0
    both_started = asyncio.Event()

    async def dispatch(tool_name: str, arguments: dict, _task_id: str) -> dict:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
        return {"tool": tool_name, "value": arguments["value"]}

    queue = ToolQueue(database, dispatch, database.publish, workers=2,
                      default_timeout=1, default_retries=0)
    await queue.start()
    try:
        results = await queue.execute_many([
            {"task_id": "task-a", "tool_name": "weather", "arguments": {"value": 1},
             "idempotency_key": "parallel-a"},
            {"task_id": "task-b", "tool_name": "music", "arguments": {"value": 2},
             "idempotency_key": "parallel-b"},
        ], total_budget_seconds=1)
    finally:
        await queue.close()

    assert results == [
        {"tool": "weather", "value": 1},
        {"tool": "music", "value": 2},
    ]


@pytest.mark.asyncio
async def test_tool_queue_moves_exhausted_job_to_dead_letter(database):
    attempts = 0

    async def dispatch(_tool_name: str, _arguments: dict, _task_id: str) -> dict:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("upstream unavailable")

    queue = ToolQueue(database, dispatch, database.publish, workers=1,
                      default_timeout=0.2, default_retries=1)
    await queue.start()
    try:
        result = await queue.execute(
            task_id="task-dlq", tool_name="maps_search", arguments={"keywords": "西湖"},
            idempotency_key="dead-letter-case", total_budget_seconds=3,
        )
    finally:
        await queue.close()

    dead_letter = await database.fetchone(
        "SELECT attempts,error FROM tool_dead_letters WHERE task_id=?", ("task-dlq",)
    )
    assert result["queue_status"] == "dead_letter"
    assert attempts == 2
    assert dead_letter and dead_letter["attempts"] == 2
    assert "upstream unavailable" in dead_letter["error"]


@pytest.mark.asyncio
async def test_permanent_tool_error_keeps_clarification_payload_without_retry(database):
    attempts = 0

    async def dispatch(_name, _arguments, _task_id):
        nonlocal attempts
        attempts += 1
        return {"ok": False, "error": "起点存在多个候选", "error_code": "AMBIGUOUS_LOCATION",
                "retryable": False, "requires_clarification": True, "field": "origin"}

    queue = ToolQueue(database, dispatch, database.publish)
    await queue.start()
    try:
        result = await queue.execute(task_id="clarify", tool_name="maps_route", arguments={}, idempotency_key="clarify")
    finally:
        await queue.close()
    assert attempts == 1
    assert result["requires_clarification"] is True
    assert result["field"] == "origin"
    assert result["error_code"] == "AMBIGUOUS_LOCATION"


@pytest.mark.asyncio
async def test_cancelling_running_tool_stops_it_and_keeps_worker_available(database):
    started = asyncio.Event()
    blocker = asyncio.Event()
    finished = []

    async def dispatch(name, arguments, task_id):
        if task_id == "cancel-this":
            started.set()
            await blocker.wait()
        finished.append(task_id)
        return {"value": task_id}

    queue = ToolQueue(database, dispatch, database.publish, workers=1)
    await queue.start()
    try:
        job = await queue.submit(task_id="cancel-this", tool_name="search_music", arguments={}, idempotency_key="cancel-this")
        await asyncio.wait_for(started.wait(), timeout=1)
        await queue.cancel_task("cancel-this")
        assert (await queue.wait(job))["queue_status"] == "cancelled"
        other = await queue.execute(task_id="keep-working", tool_name="search_music", arguments={}, idempotency_key="keep-working")
        assert other["value"] == "keep-working"
        assert finished == ["keep-working"]
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_late_tool_response_cannot_overwrite_cancellation(database):
    started = asyncio.Event()
    returned = asyncio.Event()

    async def dispatch(name, arguments, task_id):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            returned.set()
            return {"ok": True, "remote_result": "already accepted before cancellation"}

    queue = ToolQueue(database, dispatch, database.publish, workers=1)
    await queue.start()
    try:
        job_id = await queue.submit(task_id="late-result", tool_name="search_music", arguments={}, idempotency_key="late-result")
        await asyncio.wait_for(started.wait(), timeout=1)
        await queue.cancel_task("late-result")
        await asyncio.wait_for(returned.wait(), timeout=1)
        for _ in range(50):
            if not queue._active:
                break
            await asyncio.sleep(0.01)
        row = await database.fetchone("SELECT status,result_json FROM tool_jobs WHERE id=?", (job_id,))
        assert row["status"] == "cancelled"
        assert row["result_json"] and "already accepted" in row["result_json"]
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_queue_failed_dependency_never_dispatches_dependent_job(database):
    calls = []

    async def dispatch(name, arguments, task_id):
        calls.append(name)
        return {"error": "injected permanent failure", "retryable": False}

    queue = ToolQueue(database, dispatch, database.publish, workers=2, default_retries=0)
    await queue.start()
    try:
        first = await queue.submit(task_id="dependencies", tool_name="weather", arguments={}, idempotency_key="upstream")
        second = await queue.submit(task_id="dependencies", tool_name="save", arguments={}, idempotency_key="downstream", dependencies=[first])
        result = await queue.wait(second, total_budget_seconds=2)
        assert result["queue_status"] == "dead_letter"
        assert "依赖任务失败" in result["error"]
        assert calls == ["weather"]
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_tool_timeout_does_not_block_unrelated_branch(database):
    async def dispatch(name, arguments, task_id):
        if name == "slow":
            await asyncio.Event().wait()
        return {"value": "fast-result"}

    queue = ToolQueue(database, dispatch, database.publish, workers=2, default_timeout=0.04, default_retries=0)
    await queue.start()
    try:
        slow = await queue.submit(task_id="timeout-case", tool_name="slow", arguments={}, idempotency_key="slow")
        fast = await queue.submit(task_id="fast-case", tool_name="fast", arguments={}, idempotency_key="fast")
        assert (await queue.wait(fast, total_budget_seconds=1))["value"] == "fast-result"
        failure = await queue.wait(slow, total_budget_seconds=1)
        assert failure["queue_status"] == "dead_letter" and "TimeoutError" in failure["error"]
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_memory_context_is_trimmed_and_restored_from_durable_store(database):
    memory = MemoryRepository(database, history_messages=3, history_characters=80)
    await memory.open()
    await memory.put("user-1", "travel_preference", "优先步行")
    messages = [
        HumanMessage(content="第一条用户消息"),
        AIMessage(content="第一条助手回复"),
        HumanMessage(content="第二条用户消息"),
        AIMessage(content="第二条助手回复"),
    ]
    await memory.save_context("user-1", "session-1", messages, {"slots": {"city": "杭州"}})
    restored = await memory.build_context("user-1", "session-1", [], {})
    await memory.close()

    assert restored["preferences"] == {"travel_preference": "优先步行"}
    assert len(restored["model_history"]) == 3
    assert restored["model_history"][-1]["content"] == "第二条助手回复"
    assert "当前约束" in restored["memory_brief"]


@pytest.mark.asyncio
async def test_mcp_stdio_connection_opens_and_closes_in_owner_task():
    client = LocalMCPClient({})
    await client.open()
    try:
        assert {"maps_weather", "maps_search", "maps_route", "search_music",
                "save_itinerary", "create_reminder"} <= client.tool_names
        result = await client.call("maps_weather", {"city": "杭州"})
        assert result["error"] == "缺少 AMAP_MAPS_API_KEY"
    finally:
        await client.close()
    assert client.session is None


def test_skill_registry_adds_safety_overlay_and_stable_hashes(tmp_path: Path):
    for name, content in (("wellbeing", "普通健康建议"), ("safety", "高风险转人工")):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "SKILL.md").write_text(content, encoding="utf-8")

    registry = LifeSkillRegistry(str(tmp_path))
    bundle = registry.resolve("wellbeing", "我胸痛而且呼吸困难")
    first = registry.describe()
    second = LifeSkillRegistry(str(tmp_path)).describe()

    assert bundle.names == ["wellbeing", "safety"]
    assert bundle.risk_level == "high"
    assert bundle.handoff_summary and bundle.handoff_summary["risk_signals"]
    assert [item["content_hash"] for item in first] == [item["content_hash"] for item in second]
