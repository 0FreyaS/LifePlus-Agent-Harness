import json

import pytest

from car_agent import structured_output
from evaluations.planner_binding_ab import load_before_fix, main, summarize


@pytest.mark.asyncio
async def test_baseline_snapshot_uses_original_repair_without_replacing_host_module():
    original = structured_output.invoke_structured
    snapshot = {
        "src/car_agent/structured_output.py": "async def invoke_structured():\n    return 'before-fix'\n",
        "src/car_agent/task_planning.py": "from .structured_output import invoke_structured\nasync def plan_tasks():\n    return await invoke_structured()\n",
    }
    assert await load_before_fix(snapshot)() == "before-fix"
    assert structured_output.invoke_structured is original


def test_pairing_separates_seven_and_long_tail_and_keeps_regressions():
    cases = [{"id": "old", "scope": "failed_seed7"}, {"id": "new", "scope": "long_tail30"}]
    rows = [{"id": item["id"], "scope": item["scope"], "arm": arm,
             "passed": (item["id"] == "old") == (arm == "candidate"),
             "elapsed_ms": 100, "attempts": []} for item in cases for arm in ("baseline", "candidate")]
    value = summarize(rows, cases)
    assert value["complete"]
    assert value["paired"]["failed_seed7"]["rescued"] == ["old"]
    assert value["paired"]["long_tail30"]["regressed"] == ["new"]
    assert not summarize(rows[:-1], cases)["complete"]


def test_paired_cli_cannot_start_paid_calls_without_live_flag(monkeypatch, capsys):
    monkeypatch.setattr("evaluations.planner_binding_ab.run", lambda *_: pytest.fail("Unexpected API call"))
    main([])
    assert json.loads(capsys.readouterr().out)["paid_calls"] == 0
