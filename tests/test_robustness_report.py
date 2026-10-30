import json
import shutil
from pathlib import Path

import pytest

from evaluations.robustness_report import load, PUBLIC, section

ROOT = Path(__file__).resolve().parents[1]


def test_robustness_report_is_portable_without_private_runs(tmp_path):
    root = tmp_path / "portable"
    shutil.copytree(ROOT / "测试回报" / PUBLIC, root / "测试回报" / PUBLIC)
    result = load(tmp_path / "new", root)
    assert result["full90"]["submitted"] == 90 and result["full90"]["passed"] == 90
    assert result["old8"]["submitted"] == 8
    assert result["full90"]["total_tokens"] is None
    assert result["tail30"]["by_layer"]["event_live"]["passed"] == 3
    assert result["tail30"]["by_layer"]["controlled_contract"]["submitted"] == 13
    assert result["rag"]["current_source_matches"]
    assert all(not row["passed"] for row in result["rag"]["cases"])
    assert "中位耗时没有改善" in section(result)


def test_robustness_evidence_never_overwrites_changed_results(tmp_path):
    output = tmp_path / "out"
    load(output, ROOT)
    path = output / PUBLIC / "长尾30条逐条结果.json"
    path.write_text("[]\n")
    with pytest.raises(ValueError, match="Frozen current evidence differs"):
        load(output, ROOT)
    assert path.read_text() == "[]\n"


def test_test_fixture_failures_remain_distinct_from_final_contract_results():
    directory = ROOT / "测试回报" / PUBLIC
    before = json.loads((directory / "夹具诊断01.json").read_text())
    after = json.loads((directory / "长尾30条逐条结果.json").read_text())
    assert not next(row for row in before["results"] if row["id"] == "tail-E02")["passed"]
    assert next(row for row in after if row["id"] == "tail-E02")["passed"]
    assert not next(row for row in after if row["id"] == "tail-D05")["passed"]
    source = json.loads((directory / "长尾30条运行条件.json").read_text())
    assert source["paid_calls_in_this_run"] == 0
    assert source["live_observations_reused_from"] == "robustness30-20261030-01"
