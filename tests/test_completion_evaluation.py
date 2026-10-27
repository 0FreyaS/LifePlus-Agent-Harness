from collections import Counter
from datetime import datetime

from evaluations.completion import check_goal, report_rows
from evaluations.completion_dataset import build_cases


ANCHOR = datetime.fromisoformat("2026-10-23T10:00:00+08:00")


def weather_trace(city="无锡", forecast_date="2026-10-23"):
    return [
        {"type": "route.completed", "domain": "weather"},
        {"type": "tool.started", "seq": 1, "job_id": "job", "arguments": {"city": city, "forecast_date": forecast_date}},
        {"type": "tool.completed", "seq": 2, "job_id": "job", "tool_name": "maps_weather",
         "result": {"ok": True, "city": city, "date": forecast_date, "day_weather": "晴", "source": "test"}},
    ]


def test_completion_dataset_has_90_conversations_including_user_policy():
    cases = build_cases()
    assert len(cases) == len({case["id"] for case in cases}) == 90
    assert set(Counter(case["group"] for case in cases).values()) == {30}
    signatures = {(case["group"], case["query"], str(case["user_policy"])) for case in cases}
    assert len(signatures) == 90


def test_completed_status_without_business_receipt_does_not_pass():
    goal = {"domain": "weather", "tool": "maps_weather", "arguments": {"city": "无锡", "forecast_date": "$today"}}
    leaf = {"status": "completed", "domain": "weather", "answer": "完成了"}
    assert "missing_successful_tool_receipt:maps_weather" in check_goal(goal, leaf, [], {}, [], ANCHOR)
    trace = weather_trace()
    assert not check_goal(goal, leaf, trace, {}, [trace], ANCHOR)
    assert "argument_mismatch:city" in check_goal(goal, leaf, weather_trace("杭州"), {}, [], ANCHOR)
    assert "wrong_weather_date" in check_goal(goal, leaf, weather_trace(forecast_date="2026-10-24"), {}, [], ANCHOR)


def test_route_requires_selected_poi_binding_and_real_receipt():
    goal = {"domain": "navigation", "tool": "maps_route", "arguments": {"travel_mode": "walking"}}
    leaf = {"status": "completed", "domain": "navigation", "answer": "路线已生成"}
    trace = [{"type": "tool.started", "job_id": "job", "arguments": {"travel_mode": "walking", "destination": "120.3,31.5"}},
        {"type": "tool.completed", "job_id": "job", "tool_name": "maps_route", "result": {"ok": True, "distance_m": 10, "duration_s": 30, "navigation_url": "https://example.org/route"}}]
    state = {"slots": {"selection": 1}, "tool_result": {"items": [{"location": "120.4,31.5"}]}}
    assert "route_not_bound_to_selected_poi" in check_goal(goal, leaf, trace, state, [trace], ANCHOR)
    state["tool_result"]["items"][0]["location"] = "120.3,31.5"
    assert not check_goal(goal, leaf, trace, state, [trace], ANCHOR)
    trace[-1]["result"]["simulated"] = True
    assert "invalid_or_simulated_receipt" in check_goal(goal, leaf, trace, state, [trace], ANCHOR)


def test_save_weather_requires_approval_and_actual_upstream_fields():
    goal = {"domain": "planning", "tool": "save_itinerary", "arguments": {}, "receipt_from": "maps_weather"}
    leaf = {"status": "completed", "domain": "planning", "answer": "已保存"}
    trace = [{"type": "tool.completed", "seq": 5, "tool_name": "save_itinerary", "result": {"ok": True, "status": "saved", "item_id": "item", "details": {}}}]
    failures = check_goal(goal, leaf, trace, {}, [trace, weather_trace()], ANCHOR)
    assert "write_without_prior_approval" in failures
    assert "upstream_evidence_not_saved" in failures
    trace.insert(0, {"type": "user.resumed", "action": "approve", "seq": 4})
    trace[-1]["result"]["details"] = weather_trace()[-1]["result"]
    assert not check_goal(goal, leaf, trace, {}, [trace, weather_trace()], ANCHOR)


def test_partial_failure_and_timeout_stay_in_completion_denominator():
    cases = [{"id": "a", "group": "multi_completion"}, {"id": "b", "group": "multi_completion"}]
    rows = [{"id": "a", "group": "multi_completion", "completed": False, "all_roots_completed": True,
             "goals_completed": 1, "goals_total": 2, "elapsed_ms": 1000},
            {"id": "b", "group": "multi_completion", "completed": False, "goals_total": 2, "elapsed_ms": 210000}]
    report = report_rows(rows, cases, {})["groups"]["multi_completion"]
    assert report["tested"] == 2 and report["completed"] == 0
    assert report["goals_total"] == 4 and report["goals_completed"] == 1
    assert report["structured_false_completion"] == {"numerator": 1, "denominator": 1}
    assert report["p95_ms"] == 210000
