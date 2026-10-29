import json
from types import SimpleNamespace

import httpx
import pytest

from car_agent.adapters.jev import JevClient
from car_agent.event_resume_filter import JevResumeFilter, pure_resume_proposal
from car_agent.freshness import FreshnessPolicy, utcnow
from car_agent.input_events import InputDecision, resolve_input
from car_agent.persistence import Database
from car_agent.runtime_limits import RuntimeLimits
from evaluations.input_event_cases import grade, make_waiting
from evaluations.resume_filter_cases import CASES


def pending(case):
    rows = make_waiting(case, utcnow())
    if case.get("expired"):
        for row in rows:
            value = json.loads(row["interrupt_json"])
            value["expires_at"] = "2020-01-01T00:00:00+00:00"
            row["interrupt_json"] = json.dumps(value)
    return rows


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_only_unique_whole_pure_reply_has_host_executable_proposal(case):
    decision = pure_resume_proposal(case["query"], pending(case), case["focus"])
    if case["group"] == "pure_reply":
        assert decision is not None and not decision.new_query
        assert grade(case, decision)["passed"]
    else:
        assert decision is None


@pytest.mark.parametrize("case", [c for c in CASES if c["group"] != "pure_reply"], ids=lambda c: c["id"])
async def test_even_forced_high_score_never_executes_compound_ambiguous_or_approval(case):
    def handler(request):
        return httpx.Response(200, json={"model": "controlled-test", "answers": {"resume_filter": {
            "type": "choice", "choice": "resume_only", "confidence": 1,
            "probabilities": {"resume_only": 1, "needs_llm": 0}}}, "usage": {"input_tokens": 100, "output_tokens": 10}})
    router = JevResumeFilter(JevClient("test-only", transport=httpx.MockTransport(handler)))
    try:
        assert await router.propose(case["query"], pending(case), [], case["focus"]) is None
    finally:
        await router.close()


@pytest.mark.parametrize("policy,model_calls", [("llm_direct", 1), ("jev_resume_filter", 0)])
async def test_natural_pure_reply_bypasses_old_rules_but_only_filter_can_skip_llm(tmp_path, policy, model_calls):
    case = CASES[0]
    calls = []
    class Model:
        def with_structured_output(self, *args, **kwargs):
            class Call:
                async def ainvoke(self, messages):
                    calls.append(messages)
                    return {"route": "resume", "task_id": "place", "answer": "无锡校区吧"}
            return Call()
    def handler(request):
        return httpx.Response(200, json={"model": "controlled-test", "answers": {"resume_filter": {
            "type": "choice", "choice": "resume_only", "confidence": 1,
            "probabilities": {"resume_only": 1, "needs_llm": 0}}}, "usage": {"input_tokens": 100, "output_tokens": 10}})
    router = JevResumeFilter(JevClient("test-only", transport=httpx.MockTransport(handler)))
    db = Database(str(tmp_path / "events.sqlite3"))
    await db.open()
    context = SimpleNamespace(input_event_policy=policy, input_event_router=router if policy == "jev_resume_filter" else None,
        db=db, freshness=FreshnessPolicy(), limits=RuntimeLimits(), model_for=lambda _: Model())
    app = SimpleNamespace(state=SimpleNamespace(context=context, db=db, input_event_timeout=5))
    try:
        result = await resolve_input(app, case["query"], pending(case), [], owner="test", session_id="test", mode="live", request_id="one")
        assert grade(case, result.decision)["passed"]
        assert len(calls) == model_calls
        assert result.source == ("llm" if model_calls else "jev")
    finally:
        await router.close()
        await db.close()
