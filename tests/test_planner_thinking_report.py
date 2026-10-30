import json

import pytest

from evaluations.planner_thinking_report import PUBLIC, RUN, frozen, load


def test_incomplete_thinking_run_is_not_published(tmp_path):
    path = tmp_path / RUN
    path.mkdir(parents=True)
    (path / "summary.json").write_text(json.dumps({"complete": False, "submitted": 7}))
    output = tmp_path / "report"
    assert load(output, tmp_path) is None
    assert not output.exists()


def test_thinking_evidence_is_frozen(tmp_path):
    path = tmp_path / "result.json"
    frozen(path, b'{"passed":false}')
    with pytest.raises(ValueError, match="Frozen"):
        frozen(path, b'{"passed":true}')
    assert path.read_bytes() == b'{"passed":false}'


def test_thinking_archive_is_portable_without_private_runs(tmp_path):
    source = tmp_path / "root/测试回报" / PUBLIC
    source.mkdir(parents=True)
    for name in ("运行条件.json", "实测汇总.json"):
        (source / name).write_text(json.dumps({"marker": "portable-test"}))
    for name in ("逐条结果.jsonl", "规划思考模式33条.jsonl"):
        (source / name).write_text('{"id":"test-only"}\n')
    output = tmp_path / "output"
    assert load(output, tmp_path / "root") == {"marker": "portable-test"}
    assert (output / PUBLIC / "规划思考模式33条.说明.md").exists()
    assert (output / PUBLIC / "逐条结果.jsonl").read_bytes() == (source / "逐条结果.jsonl").read_bytes()
