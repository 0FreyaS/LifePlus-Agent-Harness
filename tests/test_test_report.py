import json
import re
import shutil
from pathlib import Path
from urllib.parse import unquote

import pytest

from evaluations import test_report
from evaluations.jev_front_report import write_maintained_report


@pytest.fixture
def report(tmp_path, monkeypatch):
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("Organizing the report must not make API requests")
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    output = tmp_path / "测试回报"
    evidence = test_report.build(output)
    return output, evidence


def snapshot(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*") if path.is_file()}


def test_current_baseline_metrics_and_unmeasured_slots_are_separated(report):
    output, value = report
    assert len(value["datasets"]) == 18 and len(value["metrics"]) == 28
    assert value["paid_calls_during_organization"] == 0
    slots = next(dataset for dataset in value["datasets"] if dataset["id"] == "D03")
    assert (slots["cases"], slots["annotated_fields"], slots["must_not_infer_fields"]) == (30, 44, 3)
    if value.get("current_baseline"):
        assert "实测" in slots["measurement_status"]
    else:
        assert "待" in slots["measurement_status"]
    assert value["observations"]["domain"]["baseline"]["passed"] == 21
    assert value["observations"]["domain"]["candidate"]["total_tokens"] is None
    assert value["observations"]["domain_e2e"]["candidate"]["goals_completed"] == 14
    assert value["observations"]["domain_e2e"]["candidate"]["goals_total"] == 20
    known = value["complete_usage_pairs"]["domain_e2e"]
    assert len(known["ids"]) == 11
    assert known["arms"]["baseline"]["total_tokens"] == 28516
    assert known["arms"]["candidate"]["total_tokens"] == 32852
    assert value["engineering"]["counts"] == {"passed": 790, "skipped": 2}
    if value.get("current_baseline"):
        assert "## 0. 修复前实测基线（保留）" in (output / "测试总文档.md").read_text()
    else:
        assert "槽位提取准确率 | **待测**" in (output / "测试总文档.md").read_text()
    assert all("grok" not in path.read_text().lower() for path in output.rglob("*") if path.is_file())
    assert "planner_thinking_experiment" not in value
    assert "规划器单独开启思考的真实对比" not in (output / "测试总文档.md").read_text()
    assert value["planner_binding_preparation"]["scopes"] == {"failed_seed7": 7, "long_tail30": 30}
    if value.get("planner_binding_fix", {}).get("engineering"):
        current = value["planner_binding_fix"]["engineering"]
        assert current["failed"] == current["errors"] == 0
        assert "受控回归不计入真实模型样本" in (output / "测试总文档.md").read_text()


def test_each_dataset_has_a_note_and_valid_unique_jsonl_ids(report):
    output, value = report
    for dataset in value["datasets"]:
        path = output / dataset["file"]
        assert path.with_suffix(".说明.md").exists()
        rows = test_report.read_rows(path)
        assert len(rows) == len({row["id"] for row in rows}) == dataset["cases"]


def test_rebuild_is_idempotent_and_portable_without_private_runs(report, monkeypatch, tmp_path):
    output, expected = report
    before = snapshot(output)
    test_report.build(output)
    assert snapshot(output) == before
    monkeypatch.setattr(test_report, "ROOT", tmp_path / "missing-original-files")
    actual = test_report.build(output)
    assert actual == expected
    assert snapshot(output) == before


def test_changed_frozen_cases_are_not_silently_replaced(report):
    output, value = report
    path = output / value["datasets"][0]["file"]
    changed = path.read_text().replace("query", "changed_query", 1)
    path.write_text(changed)
    with pytest.raises(ValueError, match="Frozen evidence differs"):
        test_report.build(output)
    assert path.read_text() == changed


def test_only_portable_report_is_sufficient_for_a_fresh_output(report, monkeypatch, tmp_path):
    output, expected = report
    root = tmp_path / "portable-repo"
    shutil.copytree(output, root / "测试回报")
    monkeypatch.setattr(test_report, "ROOT", root)
    regenerated = root / "fresh-report"
    assert test_report.build(regenerated) == expected
    assert snapshot(output) == snapshot(regenerated)


def test_other_model_is_rejected_before_export(tmp_path, monkeypatch):
    monkeypatch.setattr(test_report, "ROOT", tmp_path)
    source = tmp_path / "incoming.jsonl"
    source.write_text(json.dumps({"id": "bad", "model": "not-the-current-baseline", "attempts": []}) + "\n")
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="Non-baseline"):
        test_report.export_results(output, "bad.jsonl", Path("incoming.jsonl"))
    assert not output.exists()


def test_legacy_report_cannot_overwrite_migrated_entry(tmp_path):
    path = tmp_path / "old.md"
    original = test_report.REDIRECT + "\nKeep this redirect and its current-model policy.\n"
    path.write_text(original)
    with pytest.raises(ValueError, match="migrated"):
        write_maintained_report(path, ["obsolete model report"])
    assert path.read_text() == original


def test_bad_case_review_is_preserved_by_offline_rebuild(report):
    output, _ = report
    source = Path(test_report.__file__).with_name("bad-case-review.md").read_text()
    rendered = (output / "测试总文档.md").read_text()
    assert source.strip() in rendered
    assert {f"BC{i:02d}" for i in range(1, 11)} <= set(re.findall(r"BC\d{2}", source))
    assert "报告整理本身不请求模型" in source
    assert "前置计划失败引发后续missing_task，只记为传播症状" in source
    assert "原3条天气＋提醒＋行程会话复测" in source


def test_bad_case_evidence_links_resolve_in_portable_report(report):
    output, _ = report
    source = Path(test_report.__file__).with_name("bad-case-review.md").read_text()
    for link in re.findall(r"\]\(([^)]+)\)", source):
        if link.startswith("../evaluations/"):
            target = Path(test_report.__file__).resolve().parents[1] / link.removeprefix("../")
        else:
            target = output / unquote(link.split("#", 1)[0])
        assert target.exists(), link


def test_bad_case_semantic_failures_and_successful_repairs_have_distinct_statuses(report):
    output, _ = report
    events = test_report.read_rows(output / "测试数据集/结果记录/events.jsonl")
    for cid in ("boundary04", "boundary06"):
        observed = [row for row in events if row["id"] == cid]
        assert len(observed) == 2 and all(not row["passed"] for row in observed)
    pairs = test_report.read_rows(output / "测试数据集/结果记录/2026-10-30-planner-binding-fix-01/results.jsonl")
    for cid in ("binding-search_save-02", "binding-route_save-01"):
        observed = next(row for row in pairs if row["id"] == cid and row["arm"] == "candidate")
        assert observed["passed"] and len(observed["attempts"]) == 2
        assert observed["attempts"][0]["parse_failed"] and not observed["attempts"][1]["parse_failed"]
