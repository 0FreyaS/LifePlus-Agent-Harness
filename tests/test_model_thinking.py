import json

import httpx
import pytest
from langchain_core.messages import HumanMessage

from car_agent.config import Settings
from car_agent.model_roles import build_models


def test_default_does_not_change_existing_provider_request():
    settings = Settings(_env_file=None, model_base_url="https://test.invalid/v1", model_api_key="test-only", model_name="base")
    model, _ = build_models(settings)
    assert model.extra_body is None


def test_thinking_is_explicit_and_shared_by_all_model_roles():
    settings = Settings(_env_file=None, model_base_url="https://test.invalid/v1", model_api_key="test-only",
        model_name="deepseek-flash", model_thinking="disabled", planner_model_name="deepseek-flash",
        intent_model_name="deepseek-flash", response_model_name="deepseek-flash")
    base, roles = build_models(settings)
    for model in (base, *roles.values()):
        assert model.extra_body == {"thinking": {"type": "disabled"}}
        assert model.max_retries == 0


async def test_disabled_thinking_reaches_actual_openai_sdk_wire(monkeypatch):
    import langchain_openai

    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    original = langchain_openai.ChatOpenAI
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(langchain_openai, "ChatOpenAI", lambda **kw: original(**kw, http_async_client=client))
        settings = Settings(_env_file=None, model_base_url="https://test.invalid/v1", model_api_key="test-only",
            model_name="deepseek-flash", model_thinking="disabled")
        model, _ = build_models(settings)
        assert (await model.ainvoke([HumanMessage(content="Reply OK")])).content == "OK"
    assert captured[0]["thinking"] == {"type": "disabled"}
    assert captured[0]["model"] == "deepseek-flash"
