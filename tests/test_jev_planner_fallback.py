import asyncio

import pytest
from pydantic import ValidationError

from car_agent.task_planning import GoalPlan, plan_tasks


class Gate:
    def __init__(self, result):
        self.result = result

    async def classify(self, *args):
        return self.result


class Planner:
    calls = 0

    def __init__(self, domains, invalid=False):
        self.domains, self.invalid = domains, invalid
        self.calls = 0

    def with_structured_output(self, schema, **kwargs):
        assert schema is GoalPlan
        model = self

        class Call:
            async def ainvoke(self, messages):
                model.calls += 1
                if model.invalid:
                    return {"tasks": []}
                return {"tasks": [{"id": f"t{i}", "query": messages[-1]["content"], "domain": domain}
                                  for i, domain in enumerate(model.domains)]}
        return Call()


@pytest.mark.parametrize("query,domains", [
    ("无锡天气周杰伦歌曲都要", ["weather", "music"]),
    ("提醒我明早八点带雨伞和钥匙", ["planning"]),
    ("查一查明天下午无锡下不下雨以及气温", ["weather"]),
    ("明天呢", ["weather"]),
])
async def test_abstention_always_uses_planner_even_without_heuristic_trigger(query, domains, monkeypatch):
    def forbidden(*args):
        raise AssertionError("An active Jev gate must not fall back to regex")
    monkeypatch.setattr("car_agent.task_planning.may_have_multiple_tasks", forbidden)
    model = Planner(domains)
    result = await plan_tasks(query, "live", model, gate=Gate((None, None)))
    assert [t.domain for t in result.tasks] == domains
    assert model.calls == 1
    assert all(not t.action and not t.slots for t in result.tasks)


async def test_accepted_multi_uses_planner_without_rechecking_regex(monkeypatch):
    monkeypatch.setattr("car_agent.task_planning.may_have_multiple_tasks", lambda _: pytest.fail("regex called"))
    model = Planner(["weather", "music"])
    assert len((await plan_tasks("无锡天气和歌曲", "live", model, gate=Gate(("multi", None)))).tasks) == 2


async def test_accepted_single_still_skips_planner():
    model = Planner(["weather"])
    result = await plan_tasks("查天气，谢谢", "live", model, gate=Gate(("single", "weather")))
    assert len(result.tasks) == 1 and model.calls == 0


async def test_failed_planner_does_not_silently_return_heuristic_single():
    model = Planner([], invalid=True)
    with pytest.raises(ValidationError):
        await plan_tasks("无锡天气周杰伦歌曲都要", "live", model, gate=Gate((None, None)))
    assert model.calls == 2


async def test_no_gate_keeps_original_rules_and_missing_planner_fails_explicitly():
    model = Planner(["weather"])
    assert await plan_tasks("明天呢", "live", model) is None
    assert model.calls == 0
    with pytest.raises(RuntimeError, match="真实模型"):
        await plan_tasks("明天呢", "live", None, gate=Gate((None, None)))


async def test_cancellation_is_not_converted_into_fallback():
    class Cancelled:
        async def classify(self, *args):
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await plan_tasks("查天气", "live", Planner(["weather"]), gate=Cancelled())


def test_fallback_evidence_is_complete_and_report_preserves_other_notes(tmp_path):
    from pathlib import Path
    from evaluations.jev_planner_fallback_ab import DEFAULT_OUTPUT, NOTE_START, NOTE_END, render_report

    document = tmp_path / "notes.md"
    document.write_text("# 已有测试\n\n不可覆盖的主线程备注。\n")
    original = Path(DEFAULT_OUTPUT) / "cases.jsonl"
    results = Path(DEFAULT_OUTPUT) / "results.jsonl"
    if not original.exists() or not results.exists():
        pytest.skip("Real run evidence is not available in this checkout")
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "cases.jsonl").write_bytes(original.read_bytes())
    (archive / "results.jsonl").write_bytes(results.read_bytes())
    before = (archive / "results.jsonl").read_bytes()
    render_report(archive, document)
    first = document.read_text()
    render_report(archive, document)
    assert document.read_text() == first
    assert "不可覆盖的主线程备注。" in first
    assert first.count(NOTE_START) == first.count(NOTE_END) == 1
    assert "27/30 → 30/30" in first and "10/12 → 12/12" in first
    assert (archive / "results.jsonl").read_bytes() == before


def test_duplicate_evidence_cannot_be_rendered_as_completed_trial(tmp_path):
    from evaluations.jev_planner_fallback_ab import render_report

    (tmp_path / "cases.jsonl").write_text('{"id":"x"}\n')
    (tmp_path / "results.jsonl").write_text('{"id":"x","arm":"baseline"}\n' * 2)
    with pytest.raises(ValueError, match="paired evidence"):
        render_report(tmp_path)
