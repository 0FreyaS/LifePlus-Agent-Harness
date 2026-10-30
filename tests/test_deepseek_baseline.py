import json

from evaluations.deepseek_baseline import clarification_score, summary, SOURCES
from evaluations.run import score_decision, TZ
from datetime import datetime


def test_clarification_errors_never_count_as_correct_negative():
    passed, metrics = clarification_score(None, None, runtime_error=True)
    assert not passed
    assert metrics["clarification_field_accuracy"] == [0, 1]
    assert metrics["complete_input_runtime_error"] == [1, 1]


def test_clarification_positive_and_negative_have_separate_denominators():
    assert clarification_score("city", "city")[1]["necessary_clarification_recall"] == [1, 1]
    assert clarification_score(None, "city")[1]["unnecessary_clarification_rate"] == [1, 1]
    assert clarification_score("city", "origin")[1]["necessary_clarification_correct_field_recall"] == [0, 1]


def test_unknown_usage_and_failed_rows_stay_visible():
    rows = [{"id": "one", "group": "slots", "passed": False, "elapsed_ms": 75000,
             "metrics": {"field": [0, 2]}, "attempts": [{"usage_known": False}]}]
    value = summary(rows, {"groups": ["slots"], "case_counts": {"slots": 30}})["groups"]["slots"]
    assert value["submitted"] == 1 and value["expected"] == 30
    assert value["metrics"]["field"]["denominator"] == 2
    assert value["total_tokens"] is None and value["attempts_missing_usage"] == 1
    assert value["p95_ms"] == 75000


def test_none_slot_is_an_explicit_no_inference_constraint():
    case = {"expected": {"domain": "weather", "slots": {"city": None}}}
    passed, metrics = score_decision(case, {"domain": "weather", "slots": {"city": "无锡"}}, datetime.now(TZ))
    assert not passed and metrics["annotated_slot_accuracy"] == [0, 1]


def test_frozen_module_denominators_and_clarification_balance():
    datasets = {group: [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
                for group, path in SOURCES.items()}
    assert all(len(cases) == len({case["id"] for case in cases}) == 30 for cases in datasets.values())
    fields = [value for case in datasets["slot_extraction"] for value in case["expected"]["slots"].values()]
    assert len(fields) == 44 and sum(value is None for value in fields) == 3
    assert sum(case["expected"]["missing_field"] is not None for case in datasets["clarification"]) == 15
