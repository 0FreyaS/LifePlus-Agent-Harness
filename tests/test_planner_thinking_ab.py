import json

import httpx
import pytest
from langchain_openai import ChatOpenAI

from car_agent.task_planning import GoalPlan
from evaluations.planner_thinking_ab import PlannerMeter, cases, summarize, thinking_client, validation_diagnostics


@pytest.mark.asyncio
async def test_thinking_switch_only_changes_experimental_copy_and_real_payload():
    payloads = []

    def reply(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"tasks":[{"id":"a","query":"查天气","domain":"weather"}]}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18,
                      "completion_tokens_details": {"reasoning_tokens": 3}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
        base = ChatOpenAI(base_url="https://example.invalid/v1", api_key="test-only", model="deepseek-flash",
            http_async_client=transport, max_retries=0, max_tokens=2048,
            extra_body={"thinking": {"type": "disabled"}})
        for arm in ("disabled", "low", "high"):
            meter = PlannerMeter(thinking_client(base, arm))
            await meter.with_structured_output(GoalPlan, method="json_mode", include_raw=True).ainvoke([
                {"role": "user", "content": "Return JSON"}])
            assert meter.attempts[0]["usage"]["total_tokens"] == 18
            assert meter.attempts[0]["usage"]["output_token_details"]["reasoning"] == 3
        assert base.extra_body == {"thinking": {"type": "disabled"}}
        assert base.reasoning_effort is None
        assert payloads[0]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in payloads[0]
        for payload, arm in zip(payloads[1:], ("low", "high")):
            assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == arm
            assert payload.get("max_completion_tokens", payload.get("max_tokens")) == 2048
            assert payload["response_format"] == {"type": "json_object"}


def test_pairing_keeps_failures_tokens_and_diagnostics_separate():
    rows = [{"id": "one", "group": "dependency", "scope": "main30", "arm": arm,
             "passed": arm != "disabled", "elapsed_ms": 1000,
             "attempts": [{"usage_known": False, "finish_reason": "length", "parse_failed": True}]}
            for arm in ("disabled", "low", "high")]
    result = summarize(rows, {"planned_submissions": 3})
    assert result["complete"] and not result["auto_enabled"]
    assert result["scopes"]["main30"]["paired"]["low"]["rescued"] == ["one"]
    assert result["scopes"]["main30"]["arms"]["high"]["total_tokens"] is None
    assert result["scopes"]["main30"]["arms"]["high"]["length_attempts"] == 1
    assert result["scopes"]["diagnostic3"]["arms"]["high"]["submitted"] == 0


def test_frozen_main_and_posthoc_cases_are_not_mixed():
    data = cases()
    assert len(data) == len({item["id"] for item in data}) == 33
    assert sum(item["scope"] == "main30" for item in data) == 30
    assert sum(item["scope"] == "diagnostic3" for item in data) == 3


def test_nested_validation_diagnostics_are_recorded_without_hidden_reasoning():
    try:
        GoalPlan.model_validate({"tasks": [{"id": "a", "query": "查天气", "domain": "weather", "bind_slots": {"result": "a.result"}}]})
    except Exception as cause:
        error = ValueError("wrapped parser failure")
        error.__cause__ = cause
        details = validation_diagnostics(error)
    assert details and details[0]["type"] == "literal_error"
    assert "bind_slots" in details[0]["field"]
