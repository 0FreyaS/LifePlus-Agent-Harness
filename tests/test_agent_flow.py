from datetime import date, timedelta
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from car_agent.adapters.demo import DemoServices
from car_agent.adapters.mcp_client import LocalMCPClient
from car_agent.harness import build_graph, infer, normalize_reminder_time
from car_agent.memory import MemoryRepository
from car_agent.models import ChatRequest
from car_agent.persistence import Database
from car_agent.rag import KnowledgeRetriever
from car_agent.runtime_context import RunContext
from car_agent.skill_registry import LifeSkillRegistry
from car_agent.tool_queue import ToolQueue


@pytest.fixture
async def runtime(tmp_path: Path):
    database_path = tmp_path / "agent.sqlite3"
    db = Database(str(database_path))
    await db.open()
    saver_context = AsyncSqliteSaver.from_conn_string(str(database_path))
    saver = await saver_context.__aenter__()
    await saver.setup()
    memory = MemoryRepository(db)
    await memory.open()
    demo = DemoServices()
    mcp = LocalMCPClient({})

    async def dispatch(tool_name: str, arguments: dict, _task_id: str) -> dict:
        if mcp.session:
            return await mcp.call(tool_name, arguments)
        if tool_name == "save_itinerary":
            return {"status": "saved", "title": arguments["title"], "simulated": True}
        if tool_name == "create_reminder":
            return {"status": "scheduled", "title": arguments["title"],
                    "remind_at": arguments["remind_at"], "simulated": True}
        return demo.query(tool_name, arguments)

    queue = ToolQueue(db, dispatch, db.publish, workers=2, default_timeout=2, default_retries=0)
    await queue.start()
    root = Path(__file__).parents[1]
    retriever = KnowledgeRetriever(tmp_path / "chroma", root / "knowledge")
    retriever.open()
    context = RunContext(
        "test-user", db, memory, demo, mcp, queue,
        LifeSkillRegistry(str(root / "skills")), retriever, skills_dir=str(root / "skills"),
    )
    yield build_graph(saver), context, db, saver_context
    await queue.close()
    await memory.close()
    await mcp.close()
    await saver_context.__aexit__(None, None, None)
    await db.close()


def graph_input(query: str, task_id: str) -> dict:
    return {
        "messages": [HumanMessage(content=query)], "user_id": "test-user",
        "session_id": "test-session", "task_id": task_id, "request_id": task_id,
        "mode": "demo", "original_query": query, "resolved_query": query,
        "status": "running", "memory": {}, "memory_brief": "", "model_history": [],
        "slots": {}, "tool_result": {}, "operation_result": {}, "tool_args": {},
        "tool_name": "", "operation_key": "", "answer": "", "error": "",
        "skill_names": [], "handoff_summary": {}, "rag_context": [],
    }


def test_intent_router_recognizes_supported_domains():
    assert infer("杭州明天的天气怎么样")["domain"] == "weather"
    assert infer("从公司到西湖怎么走")["domain"] == "navigation"
    assert infer("搜索周杰伦的歌")["domain"] == "music"
    assert infer("最近总是睡不好")["domain"] == "wellbeing"
    assert infer("提醒我明天上午9点带伞")["domain"] == "planning"


def test_new_chat_requests_default_to_live_mode():
    assert ChatRequest(message="你好").mode == "live"


@pytest.mark.asyncio
async def test_first_model_delta_is_observable_before_generation_finishes(runtime):
    graph, context, _, _ = runtime
    release = asyncio.Event()
    first_delta = asyncio.Event()
    events = []

    class LLM:
        def with_structured_output(self, schema, **_kwargs):
            class Router:
                async def ainvoke(self, _messages):
                    return schema.model_validate({"domain": "chat", "slots": {}})
            return Router()

        async def astream(self, _messages):
            yield AIMessageChunk(content="第一段")
            await release.wait()
            yield AIMessageChunk(content="第二段")

    context.llm = LLM()
    state = graph_input("你好", "stream-task")
    state["mode"] = "live"

    async def consume():
        async for chunk in graph.astream(state, config={"configurable": {"thread_id": "stream-test"}},
                                         context=context, stream_mode="custom", subgraphs=True):
            event = chunk[1] if isinstance(chunk, tuple) else chunk
            events.append(event)
            if event.get("type") == "answer.delta":
                first_delta.set()

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(first_delta.wait(), timeout=2)
        assert not consumer.done()
        assert not any(event.get("type") == "answer.completed" for event in events)
    finally:
        release.set()
        await consumer
    assert "".join(event["delta"] for event in events if event.get("type") == "answer.delta") == "第一段第二段"


def test_relative_reminder_time_is_normalized_for_tool_schema():
    decision = infer("提醒我明天上午9点带伞")
    assert decision["slots"]["title"] == "带伞"
    assert decision["slots"]["remind_at"].endswith("T09:00:00+08:00")
    assert normalize_reminder_time("2026-10-24T09:00:00+08:00") == "2026-10-24T09:00:00+08:00"


@pytest.mark.asyncio
async def test_navigation_without_origin_waits_when_default_location_disabled(runtime, monkeypatch):
    from car_agent.config import get_settings
    monkeypatch.setattr(get_settings(), "default_current_location", "")
    graph, context, _, _ = runtime
    config = {"configurable": {"thread_id": "navigation-clarify"}}
    await graph.ainvoke(graph_input("导航去西湖", "navigation-clarify-task"), config=config, context=context)
    snapshot = await graph.aget_state(config)
    value = next(interrupt.value for task in snapshot.tasks for interrupt in task.interrupts)
    assert value["kind"] == "clarification"
    assert value["field"] == "origin"


@pytest.mark.asyncio
async def test_navigation_default_origin_reaches_route_tool_without_origin_prompt(runtime):
    graph, context, db, _ = runtime
    config = {"configurable": {"thread_id": "configured-origin"}}
    await graph.ainvoke(graph_input("导航去西湖", "configured-origin-task"), config=config, context=context)
    snapshot = await graph.aget_state(config)
    pending = next(intr.value for task in snapshot.tasks for intr in task.interrupts)
    assert pending["kind"] == "selection"
    result = await graph.ainvoke(Command(resume={"index": 1}), config=config, context=context)
    assert result["slots"]["origin"] == "江苏省无锡市滨湖区东南大学无锡校区"
    job = await db.fetchone("SELECT args_json FROM tool_jobs WHERE task_id=? AND tool_name='maps_route'",
                            ("configured-origin-task",))
    assert json.loads(job["args_json"])["origin"] == result["slots"]["origin"]


@pytest.mark.asyncio
async def test_local_search_uses_default_reference_point_in_actual_tool_arguments(runtime):
    graph, context, db, _ = runtime
    await graph.ainvoke(graph_input("找附近的电影院", "configured-anchor-task"),
                       config={"configurable": {"thread_id": "configured-anchor"}}, context=context)
    job = await db.fetchone("SELECT args_json FROM tool_jobs WHERE task_id=? AND tool_name='maps_local_search'",
                            ("configured-anchor-task",))
    arguments = json.loads(job["args_json"])
    assert arguments["anchor"] == "江苏省无锡市滨湖区东南大学无锡校区"
    assert arguments["city"] == "无锡市"


@pytest.mark.asyncio
async def test_weather_subgraph_returns_demo_result(runtime):
    graph, context, _, _ = runtime
    result = await graph.ainvoke(graph_input("杭州明天的天气怎么样", "weather-task"),
                                 config={"configurable": {"thread_id": "weather-session"}}, context=context)
    assert result["status"] == "completed"
    assert "杭州" in result["answer"]
    assert "离线演示数据" in result["answer"]


@pytest.mark.asyncio
async def test_live_chat_uses_model_and_keeps_recent_conversation(runtime):
    graph, context, _, _ = runtime

    class Decision:
        def model_dump(self):
            return {"domain": "chat", "intent": "chat_answer", "slots": {}}

    class Router:
        async def ainvoke(self, _messages):
            return Decision()

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def with_structured_output(self, *_args, **_kwargs):
            return Router()

        async def ainvoke(self, messages):
            self.calls.append(messages)
            return AIMessage(content="你好！很高兴和你聊天。")

    llm = FakeLLM()
    context.llm = llm
    config = {"configurable": {"thread_id": "chat-session"}}
    first = graph_input("你好", "chat-task-1")
    first["mode"] = "live"
    await graph.ainvoke(first, config=config, context=context)
    second = graph_input("我刚才说了什么？", "chat-task-2")
    second["mode"] = "live"
    result = await graph.ainvoke(second, config=config, context=context)
    assert result["answer"] == "你好！很高兴和你聊天。"
    assert len(llm.calls) == 2
    assert any(message["role"] == "user" and message["content"] == "你好" for message in llm.calls[-1])


@pytest.mark.asyncio
async def test_live_tool_result_is_summarized_by_model(runtime):
    graph, context, _, _ = runtime

    class Decision:
        def model_dump(self):
            return {"domain": "weather", "action": "weather_lookup", "slots": {
                "forecast_date": (date.today() + timedelta(days=1)).isoformat(),
            }}

    class Router:
        async def ainvoke(self, _messages):
            return Decision()

    class FakeLLM:
        def with_structured_output(self, *_args, **_kwargs):
            return Router()

        async def ainvoke(self, _messages):
            return AIMessage(content="杭州明天有小雨，气温 18 到 23 度。")

    context.llm = FakeLLM()
    context.mcp.session = object()
    context.mcp.call = AsyncMock(return_value={
        "city": "杭州", "date": "2026-10-24", "day_weather": "小雨", "low": 18, "high": 23,
    })
    state = graph_input("杭州明天天气怎么样", "live-weather-task")
    state["mode"] = "live"
    result = await graph.ainvoke(state,
        config={"configurable": {"thread_id": "live-weather-session"}}, context=context)
    assert result["answer"] == "杭州明天有小雨，气温 18 到 23 度。"
    assert context.mcp.call.await_args.args[0] == "maps_weather"
    assert context.mcp.call.await_args.args[1]["city"] == "杭州"


@pytest.mark.asyncio
async def test_live_music_reply_is_bound_to_selected_song_and_real_link(runtime):
    graph, context, _, _ = runtime

    class LLM:
        def with_structured_output(self, schema, **_kwargs):
            class Router:
                async def ainvoke(self, messages):
                    assert "JSON Schema" in messages[0]["content"]
                    return schema.model_validate({"domain": "music", "action": "music_search",
                                                  "slots": {"keyword": "陶喆"}})
            return Router()

        async def ainvoke(self, messages):
            evidence = json.loads(messages[-1]["content"])
            assert len(evidence["工具结果"]["items"]) == 1
            assert evidence["工具结果"]["items"][0]["name"] == "普通朋友"
            return AIMessage(content="你选择了陶喆的《普通朋友》。")

    context.llm = LLM()
    context.mcp.session = object()
    url = "https://y.qq.com/n/ryqq/songDetail/real-song-id"
    context.mcp.call = AsyncMock(return_value={"items": [
        {"id": "1", "name": "爱我还是他", "singer": "陶喆", "url": "https://y.qq.com/n/ryqq/songDetail/first"},
        {"id": "2", "name": "普通朋友", "singer": "陶喆", "url": url},
    ], "source": "QQ音乐"})
    state = graph_input("搜索陶喆的歌曲", "music-selected")
    state["mode"] = "live"
    config = {"configurable": {"thread_id": "music-selected"}}
    await graph.ainvoke(state, config=config, context=context)
    result = await graph.ainvoke(Command(resume={"index": 2}), config=config, context=context)
    assert result["status"] == "completed"
    assert "普通朋友" in result["answer"] and url in result["answer"]


@pytest.mark.asyncio
async def test_navigation_selection_resumes_and_plans_route(runtime):
    graph, context, _, _ = runtime
    config = {"configurable": {"thread_id": "navigation-session"}}
    await graph.ainvoke(graph_input("从公司导航去湖滨附近的咖啡店", "navigation-task"),
                        config=config, context=context)
    snapshot = await graph.aget_state(config)
    selection = next(interrupt.value for task in snapshot.tasks for interrupt in task.interrupts)
    assert selection["kind"] == "selection"
    result = await graph.ainvoke(Command(resume={"index": 2}), config=config, context=context)
    assert result["status"] == "completed"
    assert "市民中心店" in result["answer"]
    assert "8.2 公里" in result["answer"]


@pytest.mark.asyncio
async def test_invalid_navigation_selection_does_not_default_to_first_candidate(runtime):
    graph, context, _, _ = runtime
    config = {"configurable": {"thread_id": "navigation-invalid-selection"}}
    await graph.ainvoke(graph_input("从公司导航去湖滨附近的咖啡店", "invalid-selection"), config=config, context=context)
    await graph.ainvoke(Command(resume={"index": 99}), config=config, context=context)
    snapshot = await graph.aget_state(config)
    pending = [item.value for task in snapshot.tasks for item in task.interrupts]
    assert pending and pending[-1]["kind"] == "selection"
    result = await graph.ainvoke(Command(resume={"index": 2}), config=config, context=context)
    assert result["status"] == "completed"
    assert result["slots"]["selection"] == 2


@pytest.mark.asyncio
async def test_invalid_reminder_time_keeps_task_in_clarification(runtime):
    graph, context, db, _ = runtime
    config = {"configurable": {"thread_id": "reminder-invalid-time"}}
    await graph.ainvoke(graph_input("提醒我带伞", "invalid-time"), config=config, context=context)
    await graph.ainvoke(Command(resume={"text": "随便什么时候"}), config=config, context=context)
    snapshot = await graph.aget_state(config)
    pending = [item.value for task in snapshot.tasks for item in task.interrupts]
    assert pending and pending[-1]["field"] == "remind_at"
    assert await db.fetchall("SELECT id FROM tool_jobs WHERE tool_name='create_reminder'") == []
    await graph.ainvoke(Command(resume={"text": "明天上午9点"}), config=config, context=context)
    snapshot = await graph.aget_state(config)
    pending = [item.value for task in snapshot.tasks for item in task.interrupts]
    assert pending and pending[-1]["kind"] == "approval"


@pytest.mark.asyncio
async def test_listing_multiple_itineraries_does_not_require_a_selection(runtime):
    graph, context, _, _ = runtime

    async def dispatch(name, arguments, task_id):
        assert name == "list_itineraries"
        return {"items": [{"title": "西湖"}, {"title": "灵山"}], "source": "LifePlus"}

    context.tool_queue.dispatcher = dispatch
    result = await graph.ainvoke(graph_input("查看行程清单", "list-itineraries"),
                                 config={"configurable": {"thread_id": "list-itineraries"}}, context=context)
    assert result["status"] == "completed"
    assert "西湖" in result["answer"] and "灵山" in result["answer"]


@pytest.mark.asyncio
async def test_planning_write_waits_for_approval_and_uses_idempotency(runtime):
    graph, context, db, _ = runtime
    config = {"configurable": {"thread_id": "planning-session"}}
    await graph.ainvoke(graph_input("提醒我明天上午9点带伞", "planning-task"), config=config, context=context)
    snapshot = await graph.aget_state(config)
    approval = next(interrupt.value for task in snapshot.tasks for interrupt in task.interrupts)
    assert approval["kind"] == "approval"
    result = await graph.ainvoke(Command(resume={"action": "approve", "operation_key": approval["operation_key"]}),
                                 config=config, context=context)
    assert result["status"] == "completed"
    assert "已创建提醒" in result["answer"]
    jobs = await db.fetchall("SELECT idempotency_key,status FROM tool_jobs WHERE tool_name='create_reminder'")
    assert len(jobs) == 1
    assert jobs[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_high_risk_wellbeing_adds_safety_skill_and_handoff(runtime):
    graph, context, _, _ = runtime
    result = await graph.ainvoke(graph_input("运动时胸痛而且呼吸困难", "risk-task"),
        config={"configurable": {"thread_id": "risk-session"}}, context=context)
    assert result["status"] == "completed"
    assert "safety" in result["skill_names"]
    assert result["handoff_summary"]["risk_signals"]
    assert "专业人员" in result["answer"]
