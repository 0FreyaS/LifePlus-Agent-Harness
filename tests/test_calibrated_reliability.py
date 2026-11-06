from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from car_agent.conversation import followup_decision, model_references
from car_agent.harness import explicit_route_endpoints, resolve_domain_intent
from car_agent.input_events import InputDecision, exact_selection, grounded_decision
from evaluations.calibration import calibrate_component, calibrate_completion, calibrate_event
from evaluations.completion import EvaluationHTTPError, check_goal, same_coordinate
from evaluations.input_event_cases import MUSIC, PLACE, case, make_waiting
from evaluations.run import model_case, score_decision, score_plan
from car_agent.orchestration import TaskCoordinator
from car_agent.task_planning import GoalPlan


def pending(*tasks):
    return make_waiting(case("test", "", list(tasks), "clarify"), datetime.now(timezone.utc))


@pytest.mark.parametrize("text,index", [("第二个校区", 2), ("选无锡的", 2), ("就南京那个", 1),
    ("去无锡校区", 2), ("不是第二个，是第一个", 1), ("第二个不要，换第一个", 1)])
def test_complete_choice_variants_bind_to_the_actual_snapshot(text, index):
    assert exact_selection(text, [{"name": name} for name in PLACE["candidates"]]) == index
    result = grounded_decision(InputDecision(route="resume", task_id="place", answer=text), text, pending(PLACE))
    assert result.route == "resume"


@pytest.mark.parametrize("text", ["第二个是什么意思", "第二个，别选了", "选无锡，顺便查天气", "不要第二个", "第二个吧，但不要执行"])
def test_choice_normalization_never_consumes_explanation_negation_or_new_demand(text):
    assert exact_selection(text, [{"name": name} for name in PLACE["candidates"]]) is None


@pytest.mark.parametrize("text", ["取消导航任务", "把导航取消掉", "不要这个路线了", "终止当前任务", "别继续了"])
def test_correct_cancel_proposals_survive_host_validation(text):
    result = grounded_decision(InputDecision(route="cancel", task_id="place", answer=text), text, pending(PLACE))
    assert result.route == "cancel"


def test_cancel_cannot_target_an_unrelated_domain_or_guess_between_two_tasks():
    value = InputDecision(route="cancel", task_id="music", answer="取消导航任务")
    assert grounded_decision(value, value.answer, pending(MUSIC)).route == "clarify"
    value = InputDecision(route="cancel", task_id="music", answer="取消任务")
    assert grounded_decision(value, value.answer, pending(MUSIC, PLACE)).route == "clarify"


def test_new_explanation_is_not_forced_into_a_selection():
    text = "第二个是什么意思"
    assert grounded_decision(InputDecision(route="new", new_query=text), text, pending(PLACE)).route == "new"


def test_polite_choice_and_wrong_domain_control_are_not_lost_as_new_tasks():
    assert exact_selection("帮我选晴天", [{"name": "晴天"}, {"name": "七里香"}]) == 1
    assert grounded_decision(InputDecision(route="new", new_query="停止保存行程"), "停止保存行程", pending(MUSIC)).route == "clarify"
    assert exact_selection("选择清吧", [{"name": "清吧"}, {"name": "餐厅"}]) == 1


def test_ambiguous_correct_looking_model_selection_is_still_rejected():
    value = InputDecision(route="resume", task_id="music", answer="第二个")
    assert grounded_decision(value, "第二个", pending(MUSIC, PLACE)).route == "clarify"


def test_cancelling_a_running_task_is_not_enqueued_as_new_business():
    value = InputDecision(route="resume", task_id="place", answer="无锡校区", new_query="取消路线")
    assert grounded_decision(value, "无锡校区，取消路线", pending(PLACE)).route == "clarify"


def test_fixture_really_expires_an_expired_candidate_case():
    value = case("expired", "第二个", [PLACE], "clarify")
    value["expired"] = True
    assert grounded_decision(InputDecision(route="resume", task_id="place", answer="第二个"), "第二个", make_waiting(value, datetime.now(timezone.utc))).route == "clarify"


def test_entity_coverage_does_not_accept_duplicate_city_queries():
    expected = {"domains": ["weather", "weather"], "dependency": "independent",
                "goals": [{"domain": "weather", "query_contains": [city]} for city in ("上海", "无锡")]}
    tasks = [{"id": str(i), "domain": "weather", "query": "查上海天气"} for i in range(2)]
    assert not score_plan({"expected": expected}, tasks)[0]
    tasks[1]["query"] = "查无锡天气"
    assert score_plan({"expected": expected}, tasks)[0]


def test_none_dependencies_and_annotated_equivalent_plans_are_scored_consistently():
    assert score_plan({"expected": {"domains": ["weather"], "dependency": "none"}}, [{"id": "one", "domain": "weather"}])[0]
    expected = {"domains": ["local_life"], "dependency": "independent", "equivalent_plans": [
        {"domains": ["local_life", "local_life"], "dependency": "data"}]}
    tasks = [{"id": "a", "domain": "local_life"}, {"id": "b", "domain": "local_life", "depends_on": ["a"], "bind_slots": {"poi_id": "a.selected.id"}}]
    assert score_plan({"expected": expected}, tasks)[0]


async def test_array_binding_reads_the_real_second_reminder_and_rejects_missing_index():
    async def snapshot(config):
        return SimpleNamespace(values={"tool_result": {"items": [{"id": "first"}, {"id": "second"}]}})
    coordinator = object.__new__(TaskCoordinator)
    coordinator.app = SimpleNamespace(state=SimpleNamespace(graph=SimpleNamespace(aget_state=snapshot)))
    spec = {"domain": "planning", "bind_slots": {"reminder_id": "source.result.items.1.id"}}
    result = await coordinator._bind(spec, {"source": {"thread_id": "source-thread"}})
    assert result["bound_slots"]["reminder_id"] == "second"
    spec["bind_slots"]["reminder_id"] = "source.result.items.2.id"
    with pytest.raises(ValueError):
        await coordinator._bind(spec, {"source": {"thread_id": "source-thread"}})


def test_nonexistent_selected_poi_field_cannot_be_dispatched():
    with pytest.raises(ValueError, match="POI"):
        GoalPlan.model_validate({"tasks": [{"id": "a", "domain": "local_life", "query": "找饭店"},
            {"id": "b", "domain": "local_life", "query": "查看详情", "depends_on": ["a"], "bind_slots": {"poi_id": "a.selected.poi_id"}}]})


def test_extracted_and_resolved_slots_have_separate_scoring_contracts():
    prediction = {"slots": {"travel_mode": "driving"}, "extracted_slots": {}}
    anchor = datetime.now(timezone.utc)
    gold = {"slots": {"travel_mode": None, "city": ""}, "slot_stage": "extracted"}
    assert score_decision({"expected": gold}, prediction, anchor)[0]
    assert not score_decision({"expected": {"slots": {"travel_mode": None}}}, prediction, anchor)[0]


async def test_component_planner_does_not_bypass_after_then_requests():
    class Model:
        calls = 0

        def with_structured_output(self, schema, **kwargs):
            async def invoke(messages):
                self.calls += 1
                return schema.model_validate({"tasks": [
                    {"id": "route", "query": "规划路线", "domain": "navigation"},
                    {"id": "save", "query": "保存路线", "domain": "planning", "depends_on": ["route"], "bind_slots": {"details": "route.route"}}]})
            return SimpleNamespace(ainvoke=invoke)
    model = Model()
    passed, _, _ = await model_case({"group": "multi_task", "query": "规划路线后保存路线",
        "expected": {"domains": ["navigation", "planning"], "dependency": "data"}}, model, None, datetime.now(timezone.utc))
    assert passed and model.calls == 1


def test_route_origin_boundary_word_is_not_part_of_the_location():
    assert explicit_route_endpoints("从东南大学出发去无锡站") == ("东南大学", "无锡站")


def test_coordinate_identity_ignores_decimal_padding_but_rejects_missing_evidence():
    assert same_coordinate("120.305450,31.586899", "120.30545,31.586899")
    assert not same_coordinate(None, None)
    assert not same_coordinate("120.30545,31.586899", "120.3,31.5")


async def test_origin_only_request_cannot_become_a_destination_via_heuristics():
    result = await resolve_domain_intent({"original_query": "从杭州东站出发怎么走", "domain": "navigation", "mode": "demo",
        "prepared_decision": {"domain": "navigation", "action": "navigation_search", "slots": {"origin": "杭州东站"}}}, {}, SimpleNamespace(context=SimpleNamespace(llm=None)))
    assert not result["slots"]["destination"] and result["slots"]["origin"] == "杭州东站"


def test_cross_turn_route_saving_requires_a_unique_live_receipt():
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    receipt = {"ok": True, "origin": "120.2,31.5", "destination": "120.3,31.5", "distance_m": 123,
               "duration_s": 45, "_freshness": {"expires_at": future}}
    source = {"source_task_id": "actual-route", "selected": {"name": "无锡站"}, "route": receipt}
    result = followup_decision("把刚才的路线存起来", [source])
    assert result["slots"]["details"] == receipt and result["context_source_task_id"] == "actual-route"
    assert followup_decision("把刚才的路线存起来", [])["requires_route_reference"]
    assert followup_decision("把刚才的路线存起来", [source, source])["requires_route_reference"]
    receipt["_freshness"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    assert followup_decision("把刚才的路线存起来", [source])["requires_route_reference"]


def test_model_context_contains_reference_handles_without_large_receipts():
    source = {"source_task_id": "route-task", "route": {"steps": ["large"] * 500}, "slots": {"destination": "无锡站"}}
    brief = model_references([source])[0]
    assert brief["route_available"] and "route" not in brief and brief["source_task_id"] == "route-task"
    assert len(source["route"]["steps"]) == 500


def test_409_business_detail_is_preserved_instead_of_assuming_a_race():
    error = EvaluationHTTPError(httpx.Response(409, json={"detail": {"code": "INPUT_EVENT_CLARIFICATION_REQUIRED", "message": "选择有歧义"}}))
    assert error.status_code == 409 and error.code == "INPUT_EVENT_CLARIFICATION_REQUIRED"
    assert error.detail["message"] == "选择有歧义"


def test_calibration_keeps_frozen_inputs_unchanged_and_excludes_invalid_goals():
    original = {"id": "event-051", "expected": {"index": 2}}
    assert calibrate_event(original)["expected"]["index"] == 1 and original["expected"]["index"] == 2
    assert calibrate_completion({"id": "clarified_completion-99"})[0] is None
    case1 = {"id": "any", "group": "slot_extraction", "query": "找便宜的咖啡店", "expected": {"slots": {"max_cost": 80}}}
    assert "max_cost" not in calibrate_component(case1)["expected"]["slots"]


def test_strict_route_evidence_cannot_accept_unrelated_origin_coordinates():
    goal = {"domain": "navigation", "tool": "maps_route", "evidence_contract": "v2",
            "arguments": {"origin": "无锡站", "destination": "西湖"}}
    trace = [{"type": "tool.started", "job_id": "r", "arguments": {"origin": "120.1,31.1", "destination": "120.2,31.2"}},
             {"type": "tool.completed", "job_id": "r", "tool_name": "maps_route", "result": {
                 "ok": True, "destination": "120.2,31.2", "distance_m": 12, "duration_s": 5, "navigation_url": "https://example.org"}}]
    state = {"tool_result": {"items": [{"name": "西湖", "location": "120.2,31.2"}]}}
    failures = check_goal(goal, {"domain": "navigation", "status": "completed", "answer": "路线已规划"}, trace, state, [trace], datetime.now(timezone.utc))
    assert "route_origin_identity_unproven" in failures
