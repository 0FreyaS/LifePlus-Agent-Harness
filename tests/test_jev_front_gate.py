import json

import httpx
import pytest

from car_agent.adapters.jev import JevClient
from car_agent.config import Settings
from car_agent.event_routing import JevEventGate, unique_proposal
from car_agent.freshness import utcnow
from car_agent.planning_gate import JevPlanningGate
from car_agent.task_planning import plan_tasks
from evaluations.input_event_cases import MUSIC, PLACE, APPROVAL, make_waiting


def client_for(answers, captured=None):
    def handler(request):
        body = json.loads(request.content)
        if captured is not None:
            captured.append(body)
        return httpx.Response(200, json={"model": "test", "answers": {
            key: {"type": "choice", "choice": answers[key], "confidence": 1,
                  "probabilities": {c: int(c == answers[key]) for c in spec["criteria"]}}
            for key, spec in body["questions"].items()}, "usage": {"input_tokens": 100, "output_tokens": 10}})
    return JevClient("test-only", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("label", ["single", "multi", "defer"])
async def test_combined_questions_one_http_request_and_domain_only_for_single(label):
    calls = []
    gate = JevPlanningGate(client_for({"cardinality": label, "domain": "weather"}, calls), include_domain=True)
    try:
        result = await gate.classify("查无锡天气")
        expected = (None, None) if label == "defer" else (label, "weather" if label == "single" else None)
        assert result == expected
        assert len(calls) == 1 and set(calls[0]["questions"]) == {"cardinality", "domain"}
    finally:
        await gate.close()


async def test_single_gate_skips_global_planner_and_passes_only_domain():
    gate = JevPlanningGate(client_for({"cardinality": "single", "domain": "weather"}), include_domain=True)
    try:
        plan = await plan_tasks("查无锡天气，谢谢", "live", None, gate=gate)
        assert len(plan.tasks) == 1
        assert plan.tasks[0].domain == "weather"
        assert plan.tasks[0].action == "" and plan.tasks[0].slots == {}
    finally:
        await gate.close()


async def test_multi_gate_overrides_heuristic_false_negative_without_inventing_plan():
    gate = JevPlanningGate(client_for({"cardinality": "multi"}))
    try:
        with pytest.raises(RuntimeError, match="真实模型"):
            await plan_tasks("无锡天气周杰伦歌曲都要", "live", None, gate=gate)
    finally:
        await gate.close()


@pytest.mark.parametrize("waiting,focus", [([MUSIC, PLACE], None), ([APPROVAL], None), ([MUSIC], "place")])
def test_gate_cannot_construct_ambiguous_expired_or_authorization_event(waiting, focus):
    rows = make_waiting({"waiting": waiting}, utcnow())
    assert unique_proposal("第二个", rows, focus) is None
    for row in rows:
        pending = json.loads(row["interrupt_json"])
        pending["expires_at"] = "2020-01-01T00:00:00+00:00"
        row["interrupt_json"] = json.dumps(pending)
    assert unique_proposal("第二个", rows, focus) is None


@pytest.mark.parametrize("choice", ["clear", "unclear"])
async def test_binary_event_gate_preserves_selection_and_full_new_task(choice):
    router = JevEventGate(client_for({"clarity": choice}))
    rows = make_waiting({"waiting": [MUSIC]}, utcnow())
    try:
        result = await router.propose("第二个吧，帮我介绍南京天气", rows, [])
        if choice == "clear":
            assert result.task_id == "music" and result.answer == "第二个吧"
            assert result.new_query == "帮我介绍南京天气"
        else:
            assert result is None
    finally:
        await router.close()


def test_defaults_keep_original_system():
    settings = Settings(_env_file=None)
    assert settings.planning_gate_provider == "rules"
    assert settings.domain_route_provider == settings.input_event_provider == "llm"


async def test_gate_service_failure_abstains_and_wrong_multi_contract_is_rejected():
    def handler(request):
        return httpx.Response(503)
    gate = JevPlanningGate(JevClient("test-only", transport=httpx.MockTransport(handler)), include_domain=True)
    try:
        assert await gate.classify("无锡天气") == (None, None)
    finally:
        await gate.close()


@pytest.mark.parametrize("query", ["查无锡天气", "记住我喜欢步行", "返回我的第一次输入"])
async def test_gate_never_calls_paid_provider_for_demo_or_persistent_history(query):
    class Forbidden:
        async def classify(self, *args):
            raise AssertionError("No paid classifier expected")
    assert await plan_tasks(query, "demo", None, gate=Forbidden()) is None
