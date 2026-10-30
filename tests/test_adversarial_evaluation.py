import json
from collections import Counter

from evaluations.adversarial_cases import dataset
from evaluations.adversarial_eval import main, summarize
from evaluations.planner_cases import grade_goals
from car_agent.task_planning import TaskPlan


def test_thirty_scenarios_separate_real_models_from_controlled_contracts():
    cases = dataset()
    assert len(cases) == len({row["id"] for row in cases}) == 30
    assert sorted(Counter(row["group"] for row in cases).values()) == [5] * 6
    assert Counter(row["layer"] for row in cases) == {
        "event_live": 5, "planner_live": 11, "domain_live": 1, "controlled_contract": 13}
    assert all(row["oracle"] and row["coverage_limit"] for row in cases)
    assert all(bool(row["checks"]) == (row["layer"] == "controlled_contract") for row in cases)


def test_runtime_oracles_keep_binding_sources_and_negated_write_scope():
    cases = {row["id"]: row for row in dataset()}
    assert cases["tail-C02"]["plan"]["tasks"][-1]["binds"] == {"details": ["n", "result"]}
    assert len(cases["tail-B02"]["plan"]["tasks"]) == 1
    assert len(cases["tail-B04"]["plan"]["tasks"]) == 1
    assert cases["tail-A05"]["event"]["expected"]["route"] == "new"


def test_equivalent_plans_do_not_require_literal_model_task_ids():
    case = next(row for row in dataset() if row["id"] == "tail-C03")["plan"]
    alternative = case["alternatives"][0]
    plan = TaskPlan.model_validate({"tasks": [
        {"id": "search-and-route", "domain": "local_life", "query": "找饭店并规划路线"},
        {"id": "save-real-route", "domain": "planning", "query": "保存完整路线", "depends_on": ["search-and-route"],
         "bind_slots": {"details": "search-and-route.route"}},
    ]})
    assert grade_goals({**case, "tasks": alternative}, plan)["passed"]


def test_summary_has_no_combined_end_to_end_rate_for_mixed_layers():
    cases = dataset()
    rows = [{"id": row["id"], "layer": row["layer"], "passed": row["layer"] != "event_live", "elapsed_ms": 100,
             "attempts": [], "errors": []} for row in cases]
    result = summarize(rows, cases)
    assert result["complete"] and result["submitted"] == 30
    assert "passed" not in result and "completion_rate" not in result
    assert len(result["failures"]) == 5
    assert not summarize(rows[:-1], cases)["complete"]
    assert not summarize([*rows[:-1], rows[0]], cases)["complete"]


def test_live_runner_does_not_spend_without_explicit_flag(monkeypatch, capsys):
    monkeypatch.setattr("evaluations.adversarial_eval.run", lambda *_: (_ for _ in ()).throw(AssertionError("paid call")))
    main([])
    assert json.loads(capsys.readouterr().out)["paid_calls"] == 0
