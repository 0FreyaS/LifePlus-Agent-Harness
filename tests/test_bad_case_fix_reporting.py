import json
import shutil
from pathlib import Path

import pytest

from evaluations.bad_case_fix_report import PUBLIC, load, section

ROOT = Path(__file__).resolve().parents[1]


def test_fix_report_retains_pre_fix_failures_and_distinguishes_scope(tmp_path):
    value = load(tmp_path / "report", ROOT)
    assert value["full90"]["passed"] == 90
    assert value["events36"]["by_layer"]["event_live"]["passed"] == 36
    assert value["tail30"]["by_layer"]["controlled_contract"]["passed"] == 13
    assert all(row["passed"] for row in value["rag"]["cases"])
    assert value["rag"]["source_origin"] == "supplied_working_tree_isolated_module"
    before = json.loads((ROOT / "测试回报/测试数据集/结果记录/2026-10-30-robustness-01/汇总.json").read_text())
    assert before["tail30"]["by_layer"]["event_live"]["passed"] == 3
    assert not any(row["passed"] for row in before["rag"]["cases"])
    text = section(value, before)
    assert "3/5 → 5/5" in text and "12/13 → 13/13" in text
    assert "未部署或重启正常服务" in text and "绑定后长时间等待" in text


def test_fix_evidence_is_portable_without_runs_or_container(tmp_path):
    root = tmp_path / "portable"
    shutil.copytree(ROOT / "测试回报" / PUBLIC, root / "测试回报" / PUBLIC)
    value = load(tmp_path / "out", root)
    assert value["full90"]["goals_completed"] == 130
    assert value["model"] == "deepseek-flash" and value["thinking"] == "disabled"


def test_fix_report_refuses_to_replace_corrupted_evidence(tmp_path):
    root = tmp_path / "portable"
    shutil.copytree(ROOT / "测试回报" / PUBLIC, root / "测试回报" / PUBLIC)
    output = tmp_path / "out"
    load(output, root)
    target = output / PUBLIC / "事件36条逐条结果.json"
    target.write_text("[]\n")
    with pytest.raises(ValueError, match="Frozen current evidence differs"):
        load(output, root)
