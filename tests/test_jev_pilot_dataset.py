from evaluations.jev_e2e_cases import build_pilot


def test_global_pilot_covers_real_execution_and_four_goals_with_one_result_dependency():
    cases = build_pilot()
    assert len(cases) == len({case["id"] for case in cases}) == 12
    assert {case["group"] for case in cases} == {
        "single_completion", "multi_completion", "clarified_completion", "event_completion", "four_goals"}
    four = next(case for case in cases if case["id"] == "jev-four-goals")
    assert len(four["goals"]) == 4
    dependent = [goal for goal in four["goals"] if goal.get("receipt_from")]
    assert len(dependent) == 1 and dependent[0]["receipt_from"] == "maps_weather"
    assert any(case["user_policy"].get("require_compound") for case in cases)
