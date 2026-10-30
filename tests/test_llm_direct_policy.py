import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from car_agent.config import Settings
from car_agent.event_routing import build_event_router
from car_agent.freshness import FreshnessPolicy, utcnow
from car_agent.input_events import resolve_input
from car_agent.persistence import Database
from car_agent.runtime_limits import RuntimeLimits
from evaluations.input_event_cases import make_waiting, grade
from evaluations.resume_filter_cases import CASES


def test_configuration_template_selects_direct_text_policy():
    settings = Settings(_env_file=Path(__file__).resolve().parents[1] / ".env.example")
    assert settings.input_event_provider == "llm_direct"
    assert settings.planning_gate_provider == "llm"
    assert settings.domain_route_provider == "jev_cascade"


def test_direct_provider_builds_no_jev_client_without_jev_key():
    settings = Settings(_env_file=None, input_event_provider="llm_direct", jev_api_key=None)
    assert build_event_router(settings) is None


@pytest.mark.parametrize("case_id", ["pure01", "compound01"])
async def test_direct_text_skips_rules_and_stale_filter_but_validates_full_reply(tmp_path, monkeypatch, case_id):
    case = next(value for value in CASES if value["id"] == case_id)
    calls = []

    class Model:
        def with_structured_output(self, *args, **kwargs):
            class Call:
                async def ainvoke(self, messages):
                    calls.append(messages)
                    value = {"route": "resume", "task_id": "place", "answer": "无锡校区吧"}
                    if case_id == "compound01":
                        value["new_query"] = "顺便查下明天无锡天气"
                    return value
            return Call()

    class ForbiddenFilter:
        async def propose(self, *args, **kwargs):
            raise AssertionError("llm_direct must never invoke a stale Jev filter")

    def forbidden_rule(*args, **kwargs):
        raise AssertionError("Live direct text must not run event rules")

    monkeypatch.setattr("car_agent.input_events.rule_decision", forbidden_rule)
    db = Database(str(tmp_path / "direct.sqlite3"))
    await db.open()
    context = SimpleNamespace(input_event_policy="llm_direct", input_event_router=ForbiddenFilter(), db=db,
        freshness=FreshnessPolicy(), limits=RuntimeLimits(), model_for=lambda _: Model())
    app = SimpleNamespace(state=SimpleNamespace(context=context, db=db, input_event_timeout=5))
    try:
        result = await resolve_input(app, case["query"], make_waiting(case, utcnow()), [],
            owner="test", session_id="test", mode="live", request_id="direct")
        assert result.source == "llm" and len(calls) == 1
        assert grade(case, result.decision)["passed"]
        if case_id == "compound01":
            assert result.decision.new_query == "顺便查下明天无锡天气"
    finally:
        await db.close()


async def test_direct_model_failure_does_not_guess_a_rule_resume(tmp_path):
    class Model:
        def with_structured_output(self, *args, **kwargs):
            class Call:
                async def ainvoke(self, messages):
                    raise TimeoutError("controlled timeout")
            return Call()

    db = Database(str(tmp_path / "failed.sqlite3"))
    await db.open()
    context = SimpleNamespace(input_event_policy="llm_direct", input_event_router=None, db=db,
        freshness=FreshnessPolicy(), limits=RuntimeLimits(), model_for=lambda _: Model())
    app = SimpleNamespace(state=SimpleNamespace(context=context, db=db, input_event_timeout=5))
    case = next(value for value in CASES if value["id"] == "pure01")
    try:
        with pytest.raises(HTTPException) as error:
            await resolve_input(app, case["query"], make_waiting(case, utcnow()), [], owner="test",
                session_id="test", mode="live", request_id="failed")
        assert error.value.status_code == 503
        decision = await db.fetchone("SELECT status,decision_json FROM input_event_decisions WHERE request_id=?", ("failed",))
        assert decision["status"] == "failed"
        assert json.loads(decision["decision_json"]) == {}
    finally:
        await db.close()
