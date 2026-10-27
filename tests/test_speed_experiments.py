from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessageChunk

from evaluations.runtime import RuntimeLab
from evaluations.speed_common import summarize, protected_snapshot
from evaluations.speed_io import PooledTools
from evaluations.speed_reply import drain, eligible, grade_reply, response_cases, StreamMeter


def test_fast_reply_scope_does_not_include_open_ended_or_high_risk_domains():
    assert eligible({"domain": "weather", "original_query": "查无锡天气"})
    for domain in ("chat", "wellbeing"):
        assert not eligible({"domain": domain, "original_query": "你好"})
    for query in ("为什么这样走", "比较两条路线", "推荐饭店", "有什么建议"):
        assert not eligible({"domain": "navigation", "original_query": query})
    assert not eligible({"domain": "weather", "handoff_summary": "交接", "original_query": "查天气"})


def test_snapshot_contains_runtime_and_config_but_not_generated_outputs():
    snapshot = protected_snapshot()
    assert "src/car_agent/harness.py" in snapshot
    assert ".env.example" in snapshot
    assert not any(name.startswith(("data/", "evaluations/")) for name in snapshot)


@pytest.mark.parametrize("case", response_cases(), ids=lambda c: c["id"])
async def test_existing_respond_can_use_template_without_executing_tools(case):
    async with RuntimeLab() as lab:
        lab.context.llm = object()
        lab.context.model_roles = {"response": None}
        state = {**deepcopy(case["state"]), "task_id": case["id"], "session_id": "s", "request_id": "r"}
        config = {"configurable": {"thread_id": case["id"]}}
        await lab.graph.aupdate_state(config, state, as_node=state["domain"] + "_agent")
        snapshot, events = await drain(lab, None, config)
        assert grade_reply(case, snapshot.values)["passed"], snapshot.values
        assert not lab.calls
        assert any(e["type"] == "result.verified" for e in events)
        assert not any(e["type"] == "response.generation.started" for e in events)


async def test_baseline_respond_really_calls_the_stream_model():
    class Model:
        model_name = "unit-test"

        async def astream(self, messages):
            yield AIMessageChunk(content="无锡明天晴，15到25度。")

    case = response_cases()[0]
    async with RuntimeLab() as lab:
        meter = StreamMeter(Model())
        lab.context.llm = meter
        state = {**deepcopy(case["state"]), "task_id": "c", "session_id": "s", "request_id": "r"}
        config = {"configurable": {"thread_id": "c"}}
        await lab.graph.aupdate_state(config, state, as_node="weather_agent")
        snapshot, events = await drain(lab, None, config)
        assert snapshot.values["answer"] == "无锡明天晴，15到25度。"
        assert len(meter.attempts) == 1
        assert any(e["type"] == "answer.delta" for e in events)


async def test_pool_wrapper_preserves_http_failures_and_dictionary_contract(tmp_path):
    async def handler(request):
        if request.url.path == "/bad":
            return httpx.Response(503)
        if request.url.path == "/list":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"ok": True})

    from car_agent.tools.service import ToolError
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tools = PooledTools(str(tmp_path / "unused.sqlite"), client=client)
        assert await tools._get("https://fixture/good", {}) == {"ok": True}
        with pytest.raises(httpx.HTTPStatusError):
            await tools._get("https://fixture/bad", {})
        with pytest.raises(ToolError):
            await tools._get("https://fixture/list", {})


def test_model_error_hidden_by_application_fallback_is_counted():
    rows = [{"case_id": "c", "arm": arm, "elapsed_ms": 100,
             "grade": {"passed": True}, "attempts": [{"error_type": "TimeoutError", "usage": None}]}
            for arm in ("baseline", "candidate")]
    result = summarize(rows, 1)
    assert result["arms"]["baseline"]["exceptions"] == 1
    assert not result["large_gain_observed"]
