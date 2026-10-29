import json
from types import SimpleNamespace

import httpx
import pytest

from evaluations.jev_ab import attempts_from_events, compare_e2e, run, summarize, usage_summary
from evaluations.jev_cases import CASES


def attempt(span="a", **values):
    return {"span_id": span, "stage": "domain_route", "usage_known": True,
            "usage": {"input_tokens": 100, "output_tokens": 10}, **values}


def test_forwarded_child_spans_count_once_and_actual_usage_is_preserved():
    events = [{"type": "model.attempt.started", "span_id": "a", "model_name": "base"},
              {"type": "model.attempt.completed", **attempt()},
              {"type": "model.attempt.completed", "child_task_id": "child", **attempt()}]
    spans = attempts_from_events(events)
    assert len(spans) == 1 and spans[0]["model_name"] == "base"
    assert usage_summary(spans)["total_tokens"] == 110


def test_unknown_or_failed_usage_is_not_counted_as_free():
    summary = usage_summary([attempt(), attempt("b", usage_known=False, usage={})])
    assert summary["total_tokens"] is None
    assert summary["observed_tokens"] == {"input_tokens": 100, "output_tokens": 10}
    assert summary["attempts_missing_usage"] == 1


def test_streaming_usage_without_legacy_known_flag_is_still_valid():
    value = attempt()
    value.pop("usage_known")
    assert usage_summary([value])["total_tokens"] == 110


def test_reported_total_includes_separate_reasoning_without_double_counting():
    value = attempt(usage={"input_tokens": 833, "output_tokens": 45, "total_tokens": 1764,
                           "output_token_details": {"reasoning": 886}})
    assert usage_summary([value])["total_tokens"] == 1764
    assert usage_summary([value])["reported_reasoning_tokens"] == 886
    value["usage"].update(output_tokens=931)
    assert usage_summary([value])["total_tokens"] == 1764


def test_cascade_reports_both_calls_and_fast_failure_stays_in_denominator():
    rows = [
        {"id": "case1", "arm": "baseline", "passed": True, "elapsed_ms": 100, "attempts": [attempt()]},
        {"id": "case1", "arm": "candidate", "passed": True, "elapsed_ms": 120, "fallback": True,
         "attempts": [attempt("jev", stage="domain_route_precheck"), attempt("fallback")]},
        {"id": "case2", "arm": "baseline", "passed": True, "elapsed_ms": 100, "attempts": [attempt()]},
        {"id": "case2", "arm": "candidate", "passed": False, "elapsed_ms": 1, "error_type": "TimeoutError", "attempts": []},
    ]
    report = summarize(rows)
    candidate = report["arms"]["candidate"]
    assert candidate["tested"] == 2 and candidate["accuracy_or_completion_rate"] == 0.5
    assert candidate["fallbacks"] == 1 and candidate["total_tokens"] == 220
    assert candidate["tokens_per_successful_case"] == 220
    assert candidate["p95_ms"] == 120 and report["paired_regressions"] == ["case2"]
    assert report["auto_enable"] is False


def test_pilot_splits_are_disjoint_and_cover_every_domain():
    assert len(CASES) == 35 and len({c["id"] for c in CASES}) == 35
    for split in ("development", "holdout"):
        assert {c["expected_domain"] for c in CASES if c["split"] == split} == {
            "weather", "navigation", "music", "planning", "wellbeing", "local_life", "chat"}


def test_classification_confusion_uses_evaluated_label_not_auxiliary_domain():
    report = summarize([{"id": "x", "arm": "baseline", "passed": False, "elapsed_ms": 0,
        "expected_label": "single", "expected_domain": "weather", "actual_label": "multi", "attempts": []}])
    assert report["arms"]["baseline"]["confusion_matrix"] == [{"expected": "single", "actual": "multi", "count": 1}]


async def test_local_ab_runs_real_resolver_and_records_both_branches_without_network(tmp_path, monkeypatch):
    from car_agent.adapters.jev import JevClient
    from car_agent.domain_routing import DOMAIN_CRITERIA, JevDomainRouter

    class Model:
        model_name = "fixture-not-real-model"

        def with_structured_output(self, *args, **kwargs):
            class Call:
                async def ainvoke(self, messages):
                    return {"raw": SimpleNamespace(usage_metadata={"input_tokens": 100, "output_tokens": 10}),
                            "parsed": {"domain": "weather"}, "parsing_error": None}
            return Call()

    def handler(_):
        return httpx.Response(200, json={"model": "fixture-not-real-jev", "answers": {"domain": {
            "type": "choice", "choice": "weather", "confidence": 1,
            "probabilities": {key: int(key == "weather") for key in DOMAIN_CRITERIA}}},
            "usage": {"input_tokens": 50, "output_tokens": 8}})

    candidate = JevDomainRouter(JevClient("test-only", transport=httpx.MockTransport(handler)))
    monkeypatch.setattr("car_agent.model_roles.build_models", lambda _: (Model(), {}))
    monkeypatch.setattr("car_agent.domain_routing.build_domain_router", lambda _: candidate)
    monkeypatch.setattr("evaluations.jev_ab.CASES", [CASES[0]])
    output = tmp_path / "local"
    await run(SimpleNamespace(output=output, split="development", repeats=1, seed=1, timeout=2))
    report = json.loads((output / "summary.json").read_text())
    assert report["paired_cases"] == 1
    assert report["arms"]["baseline"]["total_tokens"] == 110
    assert report["arms"]["candidate"]["total_tokens"] == 58
    assert report["arms"]["candidate"]["passed"] == 1
    assert report["auto_enable"] is False


async def test_e2e_comparator_includes_event_model_usage_and_rejects_source_mismatch(tmp_path):
    from car_agent.persistence import Database

    directories = [tmp_path / "a", tmp_path / "b"]
    manifest = {"dataset_sha256": "same", "selected_ids": ["x"], "model": "fixture-model", "role_models": {},
                "execution_conditions": {}, "concurrency": 1, "case_budget_seconds": 120,
                "source_sha256": {"harness.py": "same"}, "started_at": "2026-10-29T10:00:00+08:00"}
    for directory, provider in zip(directories, ("llm", "jev_cascade")):
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({**manifest, "domain_route_provider": provider}))
        (directory / "results.jsonl").write_text(json.dumps({"id": "x", "owner": "test", "completed": True,
                                                            "elapsed_ms": 200, "group": "single_task"}) + "\n")
        db = Database(str(directory / "runtime.sqlite3"))
        await db.open()
        try:
            await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                             ("t", "s", "test", "query", "completed", "live", "now", "now"))
            await db.publish("t", "model.attempt.completed", attempt("domain"))
            await db.execute("INSERT INTO input_event_decisions VALUES(?,?,?,?,?,?,?,?)",
                             ("i", "test", "s", "r", "completed", "{}", "now", None))
            await db.execute("INSERT INTO input_event_spans(input_event_id,type,data_json,created_at) VALUES(?,?,?,?)",
                             ("i", "model.attempt.completed", json.dumps(attempt("event", stage="input_events")), "now"))
        finally:
            await db.close()
    output = tmp_path / "comparison"
    compare_e2e(directories, output)
    report = json.loads((output / "summary.json").read_text())
    assert report["arms"]["candidate"]["total_tokens"] == 220
    assert report["arms"]["candidate"]["by_stage"]["input_events"]["total_tokens"] == 110
    (directories[1] / "manifest.json").write_text(json.dumps({**manifest, "source_sha256": {"harness.py": "changed"},
                                                           "domain_route_provider": "jev_cascade"}))
    with pytest.raises(ValueError, match="source_sha256"):
        compare_e2e(directories, tmp_path / "rejected")
