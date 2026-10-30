import csv
import io
import json
import pytest

from evaluations.jev_ab import summarize
from evaluations.jev_archive import ARCHIVE, LAYOUT, csv_details, export_archive, metric_changes, validate_pairs
from evaluations.jev_front_report import END, START, comparison, domain_overview, write_maintained_report


def domain_rows():
    return [json.loads(line) for line in (ARCHIVE / "modules/domain/results.jsonl").read_text().splitlines()]


def test_domain_accuracy_latency_and_tokens_reproduce_original_observations():
    rows = domain_rows()
    summary = summarize(rows)
    change = metric_changes(summary)
    assert summary["arms"]["baseline"]["passed"] == summary["arms"]["candidate"]["passed"] == 21
    assert change["accuracy_change_percentage_points"] == 0
    assert change["p50_change_ms"] == -3003
    assert change["p50_change_percent"] == pytest.approx(-84.5677274)
    assert change["p95_change_percent"] == pytest.approx(-73.9444053)
    assert change["total_tokens_change"] == 1414
    assert change["total_tokens_change_percent"] == pytest.approx(9.32656157)
    headline = "\n".join(domain_overview(summary))
    assert "100.00%" in headline and "准确率持平" in headline and "上升9.33%" in headline
    assert "100.00% → 100.00%" in comparison("领域", rows)


def test_manual_notes_survive_generation_and_invalid_markers_fail_closed(tmp_path):
    path = tmp_path / "report.md"
    write_maintained_report(path, ["first"])
    path.write_text(path.read_text() + "\n人工备注：保留尚未完成的全局测试。\n")
    write_maintained_report(path, ["second"])
    assert "second" in path.read_text() and "first" not in path.read_text()
    assert "人工备注：保留" in path.read_text()
    original = END + "\nnotes\n" + START
    path.write_text(original)
    with pytest.raises(ValueError, match="markers"):
        write_maintained_report(path, ["must not overwrite"])
    assert path.read_text() == original


def test_portable_archive_regenerates_without_private_runs(tmp_path, monkeypatch):
    monkeypatch.setattr("evaluations.jev_front_report.ROOT", tmp_path / "missing-private-runs")
    output = tmp_path / "archive"
    index = export_archive(output)
    assert len(index) == 6 and sum(e["cases"] for e in index) == 109
    assert sum(e["paired_observations"] for e in index) == 218
    for relative, _, _ in LAYOUT.values():
        directory = output / relative
        rows = [json.loads(line) for line in (directory / "results.jsonl").read_text().splitlines()]
        assert all(not ({"owner", "task_ids", "trace_files", "audit_id"} & set(row)) for row in rows)
        expected = json.loads((directory / "summary.json").read_text())
        assert summarize(rows) == expected
    before = {str(path.relative_to(output)): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    export_archive(output)
    assert before == {str(path.relative_to(output)): path.read_bytes() for path in output.rglob("*") if path.is_file()}


def test_export_refuses_to_replace_frozen_cases(tmp_path, monkeypatch):
    monkeypatch.setattr("evaluations.jev_front_report.ROOT", tmp_path / "missing-private-runs")
    output = tmp_path / "archive"
    export_archive(output)
    case_path = output / "modules/domain/cases.jsonl"
    case_path.write_text(case_path.read_text().replace("query", "changed_query", 1))
    changed = case_path.read_bytes()
    with pytest.raises(ValueError, match="Frozen cases/results changed"):
        export_archive(output)
    assert case_path.read_bytes() == changed


def test_pair_validation_rejects_duplicates_and_csv_uses_classification_label():
    cases = [{"id": "a", "query": "查无锡天气，谢谢", "expected_label": "single", "expected_domain": "weather"}]
    rows = [{"id": "a", "arm": arm, "actual_label": "multi", "passed": False, "elapsed_ms": 0,
             "attempts": []} for arm in ("baseline", "candidate")]
    validate_pairs(cases, rows, 1)
    table = list(csv.DictReader(io.StringIO(csv_details(cases, rows))))
    assert json.loads(table[0]["expected"]) == "single" and json.loads(table[0]["actual"]) == "multi"
    with pytest.raises(ValueError):
        validate_pairs(cases, [rows[0], rows[0]], 1)


def test_unknown_usage_never_becomes_token_saving_percentage():
    rows = [{"id": "x", "arm": arm, "passed": True, "elapsed_ms": 100,
        "attempts": [{"usage_known": False, "usage": {}, "stage": "test"}]} for arm in ("baseline", "candidate")]
    change = metric_changes(summarize(rows))
    assert change["total_tokens_change"] is None and change["total_tokens_change_percent"] is None
