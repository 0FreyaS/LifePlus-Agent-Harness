import json

import pytest

from evaluations.current_measurements import aggregate, frozen, load, public_attempt, PUBLIC


def test_attempt_export_is_allowlisted():
    assert public_attempt({"model_name": "deepseek-flash", "stage": "plan", "api_key": "secret",
                           "user_id": "private", "raw_message": "private"}) == {
                               "model_name": "deepseek-flash", "stage": "plan"}
    with pytest.raises(ValueError, match="Non-baseline"):
        public_attempt({"model_name": "other-model", "usage": {}})


def test_failed_consumption_is_included_and_unknown_is_not_zero():
    rows = [{"completed": False, "elapsed_ms": 9000, "goals_completed": 1, "goals_total": 2,
             "attempts": [{"stage": "plan", "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 9}},
                          {"stage": "plan", "usage_known": False}]}]
    result = aggregate(rows)
    assert result["submitted"] == 1 and result["passed"] == 0
    assert result["total_tokens"] is None and result["observed_total_tokens"] == 9
    assert result["by_stage"]["plan"]["model_attempts"] == 2


def test_frozen_result_changes_require_a_new_version(tmp_path):
    path = tmp_path / "result.json"
    frozen(path, {"passed": False})
    frozen(path, {"passed": False})
    with pytest.raises(ValueError, match="Frozen"):
        frozen(path, {"passed": True})
    assert json.loads(path.read_text()) == {"passed": False}


def test_portable_results_load_without_original_run_databases(tmp_path):
    root, output = tmp_path / "root", tmp_path / "output"
    source = root / "测试回报" / PUBLIC
    frozen(source / "实测汇总.json", {"version": "test"})
    frozen(source / "组件逐条结果.json", [{"passed": False}])
    assert load(output, root) == {"version": "test"}
    assert json.loads((output / PUBLIC / "组件逐条结果.json").read_text()) == [{"passed": False}]
