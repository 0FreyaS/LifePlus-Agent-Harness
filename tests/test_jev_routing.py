import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from car_agent.adapters.jev import JevClient, JevUnavailable
from car_agent.config import Settings
from car_agent.domain_routing import DOMAIN_CRITERIA, JevDomainRouter, build_domain_router
from car_agent.harness import resolve_intent
from car_agent.persistence import Database
from car_agent.runtime_limits import RuntimeLimitExceeded, RuntimeLimits, execution_scope


def payload(choice="weather", confidence=0.99, probability=0.99, usage=True):
    probabilities = {key: (probability if key == choice else (1 - probability) / 7) for key in DOMAIN_CRITERIA}
    return {"model": "jev-1.14.0", "answers": {"domain": {"type": "choice", "choice": choice,
        "confidence": confidence, "probabilities": probabilities}},
        **({"usage": {"input_tokens": 137, "output_tokens": 12}} if usage else {})}


def router(handler, **kwargs):
    return JevDomainRouter(JevClient("test-secret", transport=httpx.MockTransport(handler), **kwargs))


def test_default_never_constructs_a_jev_client_and_key_is_redacted():
    settings = Settings(_env_file=None, jev_api_key="test-secret")
    assert settings.domain_route_provider == "llm" and build_domain_router(settings) is None
    assert "test-secret" not in repr(settings)
    with pytest.raises(ValueError, match="JEV_API_KEY"):
        build_domain_router(Settings(_env_file=None, domain_route_provider="jev_cascade", jev_api_key=""))


async def test_real_wire_contract_and_domain_only_result():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["Authorization"] == "Bearer test-secret"
        return httpx.Response(200, json=payload())

    candidate = router(handler)
    try:
        assert await candidate.route({"original_query": "查杭州天气"}) == "weather"
        assert seen[0]["questions"]["domain"]["type"] == "choice"
        assert set(seen[0]["questions"]["domain"]["criteria"]) == set(DOMAIN_CRITERIA)
        assert "slots" not in seen[0]["state"]
    finally:
        await candidate.close()


@pytest.mark.parametrize("changes", [{"choice": "defer"}, {"confidence": 0.5}, {"probability": 0.7}])
async def test_uncertain_answers_abstain(changes):
    candidate = router(lambda _: httpx.Response(200, json=payload(**changes)))
    try:
        assert await candidate.route({"original_query": "那个呢"}) is None
    finally:
        await candidate.close()


@pytest.mark.parametrize("failure", ["unauthorized", "limited", "server", "non_json", "unknown_choice", "missing_option", "nan", "negative", "missing_answer"])
async def test_provider_and_schema_errors_never_become_routing_decisions(failure):
    value = payload()
    if failure == "unknown_choice":
        value["answers"]["domain"]["choice"] = "execute_anything"
    if failure == "missing_option":
        value["answers"]["domain"]["probabilities"].pop("defer")
    if failure == "nan":
        value["answers"]["domain"]["probabilities"]["weather"] = "NaN"
    if failure == "negative":
        value["answers"]["domain"]["probabilities"]["weather"] = -1
    if failure == "missing_answer":
        value["answers"] = {}

    def handler(_):
        if failure in {"unauthorized", "limited", "server"}:
            return httpx.Response({"unauthorized": 401, "limited": 429, "server": 503}[failure], text="test-secret")
        if failure == "non_json":
            return httpx.Response(200, text="not JSON")
        return httpx.Response(200, json=value)

    candidate = router(handler)
    try:
        assert await candidate.route({"original_query": "查杭州天气"}) is None
    finally:
        await candidate.close()


async def test_short_timeout_and_external_cancellation_are_distinct():
    async def handler(_):
        await asyncio.sleep(10)
        return httpx.Response(200, json=payload())

    candidate = router(handler, timeout=0.01)
    try:
        assert await candidate.route({"original_query": "查天气"}) is None
        candidate.client.timeout = 10
        task = asyncio.create_task(candidate.route({"original_query": "查天气"}))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await candidate.close()


@pytest.mark.parametrize("status", [200, 429])
async def test_failed_attempt_retains_reported_usage_or_status_without_response_body(status, monkeypatch):
    events = []

    async def record(kind, **data):
        events.append({"type": kind, **data})

    monkeypatch.setattr("car_agent.adapters.jev.record", record)
    invalid = payload()
    invalid["answers"]["domain"]["choice"] = "not-an-option"
    candidate = router(lambda _: httpx.Response(status, json=invalid if status == 200 else {"error": "test-secret"}))
    try:
        assert await candidate.route({"original_query": "查天气"}) is None
        failure = next(e for e in events if e["type"] == "model.attempt.failed")
        if status == 200:
            assert failure["usage"] == {"input_tokens": 137, "output_tokens": 12}
            assert failure["usage_known"] is True
        else:
            assert failure["http_status"] == 429 and failure["usage_known"] is False
        assert "test-secret" not in json.dumps(events)
    finally:
        await candidate.close()


class DomainModel:
    def __init__(self):
        self.calls = 0

    def with_structured_output(self, *args, **kwargs):
        model = self

        class Call:
            async def ainvoke(self, messages):
                model.calls += 1
                return {"domain": "weather"}
        return Call()


@pytest.mark.parametrize("accepted,expected_llm", [(True, 0), (False, 1)])
async def test_only_accepted_decisions_skip_existing_domain_llm(accepted, expected_llm):
    model = DomainModel()
    candidate = router(lambda _: httpx.Response(200, json=payload(confidence=0.99 if accepted else 0.2)))
    context = SimpleNamespace(llm=model, model_roles={}, domain_router=candidate)
    try:
        result = await resolve_intent({"original_query": "查杭州天气", "mode": "live"}, {}, SimpleNamespace(context=context))
        assert result["domain"] == "weather" and model.calls == expected_llm
        assert "action" not in result and "slots" not in result
    finally:
        await candidate.close()


@pytest.mark.parametrize("state", [
    {"mode": "live", "prepared_decision": {"domain": "weather"}},
    {"mode": "demo"},
    {"mode": "live", "original_query": "请原样返回我第一次输入的query"},
])
async def test_prepared_tasks_demo_and_history_commands_bypass_jev(state):
    class Never:
        async def route(self, state):
            raise AssertionError("Jev should not run here")

    result = await resolve_intent({"original_query": "查杭州天气", **state}, {},
        SimpleNamespace(context=SimpleNamespace(llm=None, model_roles={}, domain_router=Never())))
    assert result["domain"] in {"weather", "chat"}


async def test_jev_and_llm_share_model_budget_and_preserve_unknown_usage(tmp_path):
    db = Database(str(tmp_path / "audit.sqlite3"))
    await db.open()
    await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     ("task", "session", "user", "查杭州天气", "running", "live", "now", "now"))
    candidate = router(lambda _: httpx.Response(200, json=payload(confidence=0.2, usage=False)))
    model = DomainModel()
    try:
        async with execution_scope(db, "task", RuntimeLimits(model_calls=1), "test"):
            with pytest.raises(RuntimeLimitExceeded):
                await resolve_intent({"original_query": "查杭州天气", "mode": "live"}, {},
                    SimpleNamespace(context=SimpleNamespace(llm=model, model_roles={}, domain_router=candidate)))
        assert model.calls == 0
        rows = await db.fetchall("SELECT data_json FROM events WHERE type='model.attempt.completed'")
        assert len(rows) == 1
        event = json.loads(rows[0]["data_json"])
        assert event["usage_known"] is False and event["usage"] == {}
    finally:
        await candidate.close()
        await db.close()
