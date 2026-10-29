import json
from types import SimpleNamespace

import httpx
import pytest

from car_agent.adapters.jev import JevClient
from car_agent.event_routing import JevEventRouter, proposals
from car_agent.freshness import FreshnessPolicy, utcnow
from car_agent.input_events import grounded_decision, resolve_input
from car_agent.persistence import Database
from car_agent.runtime_limits import RuntimeLimits
from evaluations.input_event_cases import CASES, MUSIC, PLACE, APPROVAL, make_waiting


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_all_proposals_are_grounded_and_never_authorize_writes(case):
    waiting = make_waiting(case, utcnow())
    for decision in proposals(case["query"], waiting, case["focus"]).values():
        if decision.route == "resume":
            assert decision.answer in case["query"]
            assert decision.new_query is None or decision.new_query in case["query"]
            assert grounded_decision(decision, case["query"], waiting, case["focus"]).route == "resume"
            assert next(t for t in waiting if t["id"] == decision.task_id)["domain"] != "planning"


def test_compound_proposals_keep_both_demands_and_focus_and_do_not_offer_approval():
    case = {"waiting": [MUSIC, PLACE]}
    waiting = make_waiting(case, utcnow())
    choices = proposals("歌曲选第二首，杭州那边的天气也帮我看看", waiting, "music")
    assert any(c.route == "resume" and c.task_id == "music" and c.new_query == "杭州那边的天气也帮我看看" for c in choices.values())
    assert all(c.task_id in {None, "music"} for c in choices.values())
    approvals = make_waiting({"waiting": [APPROVAL]}, utcnow())
    assert all(c.route != "resume" for c in proposals("可以直接保存", approvals).values())


@pytest.mark.parametrize("fallback", [False, True])
async def test_event_router_uses_typed_proposals_and_keeps_llm_repair_budget(tmp_path, fallback):
    def handler(request):
        body = json.loads(request.content)
        criteria = body["questions"]["event"]["criteria"]
        choice = next(k for k, description in criteria.items() if k.startswith("proposal_") and json.loads(description)["task_id"] == "music")
        return httpx.Response(200, json={"model": "test-jev", "answers": {"event": {
            "type": "choice", "choice": choice, "confidence": 0.1 if fallback else 1,
            "probabilities": {key: int(key == choice) for key in criteria}}},
            "usage": {"input_tokens": 80, "output_tokens": 10}})

    router = JevEventRouter(JevClient("test-only", transport=httpx.MockTransport(handler)))
    waiting = make_waiting({"waiting": [MUSIC, PLACE]}, utcnow())
    db = Database(str(tmp_path / "events.sqlite3"))
    await db.open()

    class Model:
        calls = 0

        def with_structured_output(self, *args, **kwargs):
            model = self

            class Call:
                async def ainvoke(self, messages):
                    model.calls += 1
                    if model.calls == 1:
                        return {"route": "unknown"}
                    return {"route": "resume", "task_id": "music", "answer": "第二首"}
            return Call()

    model = Model()
    context = SimpleNamespace(db=db, freshness=FreshnessPolicy(), limits=RuntimeLimits(),
                              model_for=lambda _: model, input_event_router=router)
    app = SimpleNamespace(state=SimpleNamespace(context=context, db=db, input_event_timeout=5))
    try:
        result = await resolve_input(app, "歌曲选第二首", waiting, [], owner="test", session_id="test", request_id="r", mode="live")
        assert result.decision.route == "resume" and result.decision.task_id == "music"
        assert result.source == ("llm" if fallback else "jev")
        usage = await db.fetchone("SELECT model_calls FROM runtime_usage WHERE task_id=?", (result.audit_id,))
        assert usage["model_calls"] == (3 if fallback else 1)
        assert model.calls == (2 if fallback else 0)
    finally:
        await router.close()
        await db.close()
