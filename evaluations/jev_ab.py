"""Opt-in paired Jev routing trial and comparison of isolated completion runs."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from .jev_cases import CASES


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def attempts_from_events(events):
    spans = {}
    for event in events:
        if not event.get("span_id") or not event.get("type", "").startswith("model.attempt."):
            continue
        span = spans.setdefault(event["span_id"], {})
        span.update(event)
    return list(spans.values())


def usage_summary(attempts):
    known = [a for a in attempts if a.get("usage_known") is not False
             and all(type(value := a.get("usage", {}).get(k)) is int and value >= 0
                     for k in ("input_tokens", "output_tokens"))
             and ("total_tokens" not in a["usage"] or (
                 type(a["usage"]["total_tokens"]) is int
                 and a["usage"]["total_tokens"] >= a["usage"]["input_tokens"] + a["usage"]["output_tokens"]))]
    observed = {k: sum(a["usage"][k] for a in known) for k in ("input_tokens", "output_tokens")}
    totals = [a["usage"].get("total_tokens", a["usage"]["input_tokens"] + a["usage"]["output_tokens"]) for a in known]
    reasoning = [value for a in known if type(value := a["usage"].get("output_token_details", {}).get("reasoning")) is int]
    return {"model_attempts": len(attempts), "attempts_with_usage": len(known),
            "attempts_missing_usage": len(attempts) - len(known), "observed_tokens": observed,
            "observed_total_tokens": sum(totals), "reported_reasoning_tokens": sum(reasoning),
            "total_tokens": sum(totals) if len(known) == len(attempts) else None}


def summarize(rows):
    arms = {}
    for name in ("baseline", "candidate"):
        selected = [r for r in rows if r["arm"] == name]
        attempts = [attempt for row in selected for attempt in row.get("attempts", [])]
        passed = sum(r["passed"] for r in selected)
        tokens = usage_summary(attempts)
        confusion = Counter((r.get("expected_label", r.get("expected_domain")), r.get("actual_label", r.get("actual_domain", "error")))
                            for r in selected if "expected_domain" in r or "expected_label" in r)
        arms[name] = {"tested": len(selected), "passed": passed,
            "accuracy_or_completion_rate": passed / len(selected) if selected else None,
            "errors": sum(bool(r.get("error_type") or r.get("error")) for r in selected),
            "p50_ms": percentile([r["elapsed_ms"] for r in selected], .5),
            "p95_ms": percentile([r["elapsed_ms"] for r in selected], .95),
            "success_only_p95_ms": percentile([r["elapsed_ms"] for r in selected if r["passed"]], .95),
            "fallbacks": sum(r.get("fallback", False) for r in selected), **tokens,
            "tokens_per_submitted_case": tokens["total_tokens"] / len(selected) if selected and tokens["total_tokens"] is not None else None,
            "tokens_per_successful_case": tokens["total_tokens"] / passed if passed and tokens["total_tokens"] is not None else None,
            "confusion_matrix": [{"expected": expected, "actual": actual, "count": count}
                                 for (expected, actual), count in sorted(confusion.items())],
            "by_group": {group: {"tested": len(subset := [r for r in selected if r.get("group", "unknown") == group]),
                "passed": sum(r["passed"] for r in subset),
                "p95_ms": percentile([r["elapsed_ms"] for r in subset], .95)}
                for group in sorted({r.get("group", "unknown") for r in selected})},
            "by_stage": {stage: usage_summary([a for a in attempts if a.get("stage") == stage])
                         for stage in sorted({a.get("stage", "unknown") for a in attempts})}}
    pairs = {}
    for row in rows:
        pairs.setdefault((row["id"], row.get("repeat", 0)), {})[row["arm"]] = row
    complete = [p for p in pairs.values() if len(p) == 2]
    a, b = arms["baseline"], arms["candidate"]
    return {"arms": arms, "paired_cases": len(complete),
        "paired_regressions": [p["baseline"]["id"] for p in complete if p["baseline"]["passed"] and not p["candidate"]["passed"]],
        "p50_reduction": 1 - b["p50_ms"] / a["p50_ms"] if a["p50_ms"] and b["p50_ms"] is not None else None,
        "p95_reduction": 1 - b["p95_ms"] / a["p95_ms"] if a["p95_ms"] and b["p95_ms"] is not None else None,
        "auto_enable": False, "decision": "evidence_only_not_a_rollout", "prices_applied": False,
        "token_accounting": "provider total_tokens when present; otherwise input+output; reasoning not added twice"}


def write_summary(output, report):
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Jev A/B", "", "| 分支 | 通过/提交 | P50 ms | P95 ms | Token 合计 | 用量未知尝试 |", "|---|---:|---:|---:|---:|---:|"]
    for name, arm in report["arms"].items():
        lines.append(f"| {name} | {arm['passed']}/{arm['tested']} | {arm['p50_ms']} | {arm['p95_ms']} | {arm['total_tokens']} | {arm['attempts_missing_usage']} |")
    lines += ["", "配对退化样本：" + json.dumps(report["paired_regressions"], ensure_ascii=False),
        "", "失败和超时留在分母与耗时中。缺少供应商用量时合计为 null，已知用量只是下界。",
        "局部领域正确率与端到端任务完成率分别报告；Token 数不是账单费用。不会自动切换配置。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args):
    from car_agent.config import Settings
    from car_agent.domain_routing import build_domain_router
    from car_agent.harness import resolve_intent
    from car_agent.model_roles import build_models
    from car_agent.persistence import Database
    from car_agent.runtime_limits import RuntimeLimits, execution_scope

    settings = Settings()
    model, roles = build_models(settings)
    if model is None:
        raise ValueError("Configure the existing real MODEL_* client for the baseline")
    candidate = build_domain_router(settings.model_copy(update={"domain_route_provider": "jev_cascade"}))
    cases = [item for item in CASES if item["split"] == args.split]
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {"scope": "local_domain_route_only_no_tools", "started_at": datetime.now(timezone.utc).isoformat(),
        "cases": cases, "split": args.split, "repeats": args.repeats, "seed": args.seed,
        "baseline_model": getattr(roles.get("intent", model), "model_name", None),
        "candidate_model": settings.jev_model_name, "candidate_url": settings.jev_base_url,
        "confidence_threshold": settings.jev_min_confidence, "probability_threshold": settings.jev_min_probability,
        "jev_timeout_seconds": settings.jev_timeout_seconds, "case_timeout_seconds": args.timeout,
        "dataset_sha256": hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "sources": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in (
            "src/car_agent/harness.py", "src/car_agent/domain_routing.py", "src/car_agent/adapters/jev.py")},
        "auto_enable": False}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    db, rows = Database(str(args.output / "audit.sqlite3")), []
    await db.open()
    try:
        jobs = [(repeat, item) for repeat in range(args.repeats) for item in cases]
        random.Random(args.seed).shuffle(jobs)
        for index, (repeat, case) in enumerate(jobs):
            for arm in (("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")):
                identifier = f"{case['id']}-{repeat}-{arm}"
                await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (identifier, identifier, "jev-ab", case["query"], "running", "live", manifest["started_at"], manifest["started_at"]))
                runtime = SimpleNamespace(context=SimpleNamespace(llm=model, model_roles=roles,
                    domain_router=candidate if arm == "candidate" else None))
                row = {"id": case["id"], "repeat": repeat, "arm": arm, "group": case["group"],
                       "expected_domain": case["expected_domain"], "passed": False}
                started = monotonic()
                try:
                    async with asyncio.timeout(args.timeout), execution_scope(db, identifier, RuntimeLimits.from_settings(settings), "evaluation"):
                        result = await resolve_intent({"original_query": case["query"], "mode": "live",
                            "memory_brief": case["memory_brief"]}, {}, runtime)
                    row.update(actual_domain=result["domain"], passed=result["domain"] == case["expected_domain"])
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                row["elapsed_ms"] = round((monotonic() - started) * 1000)
                events = [{"type": r["type"], **json.loads(r["data_json"])} for r in await db.fetchall(
                    "SELECT type,data_json FROM events WHERE task_id=? ORDER BY seq", (identifier,))]
                row["attempts"] = attempts_from_events(events)
                row["fallback"] = any(e["type"] == "domain.precheck.completed" and not e.get("accepted") for e in events)
                row["prechecks"] = [e for e in events if e["type"] == "domain.precheck.completed"]
                rows.append(row)
                with (args.output / "results.jsonl").open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                write_summary(args.output, {**summarize(rows), "scope": manifest["scope"]})
                print(json.dumps({k: row[k] for k in ("id", "arm", "passed", "elapsed_ms")}), flush=True)
    finally:
        await candidate.close()
        await db.close()


def compare_e2e(paths, output):
    manifests = [json.loads((path / "manifest.json").read_text()) for path in paths]
    for field in ("dataset_sha256", "selected_ids", "model", "role_models", "execution_conditions", "concurrency", "case_budget_seconds", "source_sha256"):
        if manifests[0].get(field) != manifests[1].get(field):
            raise ValueError("Unmatched end-to-end conditions: " + field)
    defaults = {"domain_route_provider": "llm", "input_event_provider": "llm", "planning_gate_provider": "rules"}
    baseline = {field: manifests[0].get(field, default) for field, default in defaults.items()}
    candidate = {field: manifests[1].get(field, default) for field, default in defaults.items()}
    allowed = {"domain_route_provider": {"llm", "jev_cascade"}, "input_event_provider": {"llm", "jev_cascade", "jev_gate", "llm_direct", "jev_resume_filter"},
               "planning_gate_provider": {"rules", "jev_cardinality", "jev_front"}}
    event_filter_pair = (baseline["input_event_provider"] == "llm_direct" and candidate["input_event_provider"] == "jev_resume_filter"
        and baseline["domain_route_provider"] == candidate["domain_route_provider"]
        and baseline["planning_gate_provider"] == candidate["planning_gate_provider"])
    if (baseline != defaults and not event_filter_pair) or any(candidate[k] not in allowed[k] for k in defaults) or baseline == candidate:
        raise ValueError("Expected original baseline and at least one Jev candidate module")
    if manifests[0]["started_at"][:10] != manifests[1]["started_at"][:10]:
        raise ValueError("Relative-date cases must be compared on the same date")
    rows = [row for arm, path in zip(("baseline", "candidate"), paths) for row in read_e2e_rows(path, arm)]
    if any({r["id"] for r in rows if r["arm"] == arm} != set(manifests[0]["selected_ids"]) for arm in ("baseline", "candidate")):
        raise ValueError("End-to-end run did not finish all selected cases")
    output.mkdir(parents=True, exist_ok=False)
    report = {**summarize(rows), "scope": "end_to_end_completion_with_real_model_and_tools", "manifests": manifests}
    write_summary(output, report)


def read_e2e_rows(path, arm):
    rows = []
    with sqlite3.connect((path / "runtime.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as db:
        for line in (path / "results.jsonl").read_text().splitlines():
            row = json.loads(line)
            events = [{"type": kind, **json.loads(data)} for kind, data in db.execute(
                "SELECT e.type,e.data_json FROM events e JOIN tasks t ON e.task_id=t.id WHERE t.user_id=? ORDER BY e.created_at,e.seq",
                (row["owner"],))]
            events += [{"type": kind, **json.loads(data)} for kind, data in db.execute(
                "SELECT s.type,s.data_json FROM input_event_spans s JOIN input_event_decisions d ON s.input_event_id=d.id WHERE d.user_id=? ORDER BY s.seq",
                (row["owner"],))]
            rows.append({**row, "arm": arm, "passed": row["completed"], "attempts": attempts_from_events(events),
                "precheck_counts": {kind: sum(e["type"] == kind for e in events) for kind in ("domain.precheck.completed", "input.precheck.completed", "planning.precheck.completed")},
                "fallback": any(e["type"] in {"domain.precheck.completed", "input.precheck.completed", "planning.precheck.completed"} and not e.get("accepted") for e in events)})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-live", action="store_true", help="Explicitly enable paid real model requests")
    parser.add_argument("--compare-e2e", type=Path, nargs=2, metavar=("BASELINE", "CANDIDATE"))
    parser.add_argument("--split", choices=("development", "holdout"), default="development")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20261029)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if args.repeats < 1 or args.timeout <= 0:
        parser.error("Positive repeats and timeout required")
    if args.compare_e2e:
        if not args.output:
            parser.error("--output is required")
        compare_e2e(args.compare_e2e, args.output)
    elif args.run_live:
        if not args.output:
            parser.error("--output is required")
        asyncio.run(run(args))
    else:
        print(json.dumps({"status": "prepared_not_run", "development_cases": sum(c["split"] == "development" for c in CASES),
            "holdout_cases": sum(c["split"] == "holdout" for c in CASES), "auto_enable": False,
            "next": "Configure JEV_API_KEY, then use --run-live --output <new directory>"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
