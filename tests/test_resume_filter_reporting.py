import json

import pytest

from car_agent.config import Settings
from car_agent.event_routing import build_event_router
from evaluations.resume_filter_cases import CASES, E2E
from evaluations.resume_filter_report import append_section, archive_run, current_rule_control


def test_direct_llm_policy_does_not_require_or_construct_a_jev_client():
    assert build_event_router(Settings(_env_file=None, input_event_provider="llm_direct", jev_api_key=None)) is None


def test_experiment_is_frozen_into_three_equal_strata_and_composer_e2e():
    assert len(CASES) == len({c["id"] for c in CASES}) == 36
    assert all(sum(c["group"] == group for c in CASES) == 12 for group in ("pure_reply", "compound", "boundary"))
    assert len(E2E) == len({c["id"] for c in E2E}) == 5
    assert sum(c["user_policy"].get("reply_entry") == "composer" for c in E2E) == 4
    assert all(c["expected_filter"] == "needs_llm" for c in CASES if c["group"] != "pure_reply")


def test_zero_model_rule_control_is_not_mistaken_for_full_pipeline_accuracy():
    values = current_rule_control(CASES)
    assert values["pure_reply"]["submitted"] == 12
    assert values["pure_reply"]["handled_correct"] == values["pure_reply"]["handled"]
    assert values["pure_reply"]["handled"] < 12


def test_report_appends_without_overwriting_other_maintained_notes(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("# Existing record\n\nManual notes\n")
    append_section(path, ["first result"])
    append_section(path, ["updated result"])
    assert path.read_text().startswith("# Existing record\n\nManual notes\n")
    assert "first result" not in path.read_text() and "updated result" in path.read_text()


def test_new_archive_preserves_filter_labels_and_rejects_frozen_rewrites(tmp_path):
    source, destination = tmp_path / "run", tmp_path / "archive"
    source.mkdir()
    case = CASES[0]
    (source / "manifest.json").write_text(json.dumps({"cases": [case], "baseline_policy": "llm_direct"}))
    rows = [{"id": case["id"], "arm": arm, "passed": True, "elapsed_ms": 100, "attempts": [],
        "expected_filter": "resume_only", "actual_filter": "resume_only", "unsafe_shortcut": False,
        "filter_label_correct": True, "owner": "private", "task_ids": ["private"]} for arm in ("baseline", "candidate")]
    def write():
        (source / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    write()
    public = archive_run(source, destination, [case])
    assert all("owner" not in row and "task_ids" not in row for row in public)
    assert all(row["expected_filter"] == "resume_only" for row in public)
    rows[0]["passed"] = False
    write()
    with pytest.raises(ValueError, match="Frozen evidence changed"):
        archive_run(source, destination, [case])
