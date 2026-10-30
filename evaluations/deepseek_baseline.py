"""Current-client component benchmark; real understanding, controlled graph receipts.

This runner never changes the deployment, production prompts, or frozen gold labels.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from car_agent.config import get_settings
from car_agent.domain_routing import build_domain_router
from car_agent.harness import resolve_intent, resolve_domain_intent
from car_agent.model_roles import build_models, select_model
from car_agent.persistence import Database
from car_agent.runtime_limits import RuntimeLimits, execution_scope
from car_agent.task_planning import plan_tasks

from .jev_ab import attempts_from_events, usage_summary, percentile
from .planner_cases import grade_goals
from .run import score_decision, error_metrics
from .runtime import RuntimeLab

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "测试回报/测试数据集"
SOURCES = {
    "single_task": DATA / "01-领域划分/单任务域与动作30条.jsonl",
    "slot_extraction": DATA / "02-槽位与参数/槽位提取30条.jsonl",
    "clarification": DATA / "04-信息澄清/缺参与完整信息30条.jsonl",
    "planner": DATA / "03-规划与依赖/规划与依赖30条.jsonl",
}
TZ = timezone(timedelta(hours=8))
PLANNER_NOW = datetime.fromisoformat("2026-10-27T10:00:00+08:00")


class ForcePlanner:
    """Isolate planner quality from the cardinality heuristic, including singletons."""
    async def classify(self, query, memory_brief):
        return "multi", None


def clarification_score(expected, actual, *, runtime_error=False):
    necessary = expected is not None
    metrics = {"clarification_field_accuracy": [int(not runtime_error and actual == expected), 1],
               "clarification_runtime_error": [int(runtime_error), 1]}
    metrics["necessary_clarification_recall" if necessary else "unnecessary_clarification_rate"] = [
        int(not runtime_error and actual is not None), 1]
    if necessary:
        metrics["necessary_clarification_correct_field_recall"] = [int(not runtime_error and actual == expected), 1]
    else:
        metrics["complete_input_runtime_error"] = [int(runtime_error), 1]
    return not runtime_error and actual == expected, metrics


def summary(rows, manifest):
    groups = {}
    for group in manifest["groups"]:
        items = [row for row in rows if row["group"] == group]
        counters = defaultdict(lambda: [0, 0])
        for row in items:
            for name, (num, den) in row.get("metrics", {}).items():
                counters[name][0] += num
                counters[name][1] += den
        confusion = Counter((row["expected_domain"], row.get("prediction", {}).get("domain", "ERROR"))
            for row in items if "expected_domain" in row)
        groups[group] = {"expected": manifest["case_counts"][group], "submitted": len(items),
            "passed": sum(row["passed"] for row in items),
            "p50_ms": percentile([row["elapsed_ms"] for row in items], .5),
            "p95_ms": percentile([row["elapsed_ms"] for row in items], .95),
            "metrics": {name: {"numerator": num, "denominator": den, "value": num / den if den else None}
                for name, (num, den) in counters.items()},
            "confusion": [{"expected": expected, "actual": actual, "count": count}
                for (expected, actual), count in sorted(confusion.items())],
            "failed_ids": [row["id"] for row in items if not row["passed"]],
            **usage_summary([attempt for row in items for attempt in row["attempts"]])}
    return {"manifest": manifest, "groups": groups,
            "submitted": len(rows), "passed": sum(row["passed"] for row in rows)}


async def run(args):
    settings = get_settings()
    if settings.model_name != "deepseek-flash" or settings.model_thinking != "disabled":
        raise ValueError("Expected the approved deepseek-flash client with thinking disabled")
    if args.output.exists():
        raise ValueError("Use a new output directory; frozen evidence must not be overwritten")
    groups = args.groups.split(",")
    if set(groups) - SOURCES.keys():
        raise ValueError("Unknown component group")
    cases = []
    for group in groups:
        cases.extend({**json.loads(line), "group": group} for line in SOURCES[group].read_text().splitlines() if line.strip())
    anchor = datetime.now(TZ)
    manifest = {"started_at": anchor.isoformat(), "model": settings.model_name,
        "model_thinking": settings.model_thinking, "domain_route_provider": settings.domain_route_provider,
        "input_event_provider": settings.input_event_provider, "sdk_retries": 0, "concurrency": 1,
        "groups": groups, "case_counts": dict(Counter(case["group"] for case in cases)),
        "role_models": {role: getattr(settings, role + "_model_name") or settings.model_name
                        for role in ("intent", "planner", "response")},
        "case_timeout_seconds": args.timeout, "planner_anchor": PLANNER_NOW.isoformat(),
        "clarification_default_location": "disabled_in_this_process_to_match_frozen_gold",
        "scope": "configured_jev_then_deepseek_understanding_no_gold_domain; controlled_graph_receipts; forced_planner_not_gate",
        "datasets": {group: {"path": str(SOURCES[group].relative_to(ROOT)),
            "sha256": hashlib.sha256(SOURCES[group].read_bytes()).hexdigest()} for group in groups},
        "sources": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [Path(__file__), *sorted((ROOT / "src/car_agent").rglob("*.py"))]}}
    args.output.mkdir(parents=True)
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    model, roles = build_models(settings)
    if model is None:
        raise ValueError("Real model credentials required")
    router = build_domain_router(settings)
    runtime = SimpleNamespace(context=SimpleNamespace(llm=model, model_roles=roles, domain_router=router))
    db = Database(str(args.output / "audit.sqlite3"))
    await db.open()
    original_location = settings.default_current_location
    settings.default_current_location = ""
    rows = []
    try:
        async with RuntimeLab() as lab:
            for case in cases:
                row = {"id": case["id"], "group": case["group"], "query": case["query"], "passed": False,
                       "metrics": {}, "attempts": []}
                if case.get("expected", {}).get("domain"):
                    row["expected_domain"] = case["expected"]["domain"]
                await db.execute(
                    "INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (case["id"], case["id"], "component-eval", case["query"], "running", "live",
                     anchor.isoformat(), anchor.isoformat()))
                started = monotonic()
                try:
                    async with asyncio.timeout(args.timeout), execution_scope(
                        db, case["id"], RuntimeLimits.from_settings(settings),
                        "planning" if case["group"] == "planner" else "evaluation"):
                        if case["group"] == "planner":
                            plan = await plan_tasks(case["query"], "live", select_model(runtime.context, "planner"),
                                case.get("memory_brief", ""), variant=settings.planner_variant,
                                now=PLANNER_NOW, gate=ForcePlanner())
                            grade = grade_goals(case, plan)
                            row.update(passed=grade["passed"], grade=grade,
                                       prediction=plan.model_dump() if plan else {})
                            row["metrics"] = {"plan_exact": [int(grade["passed"]), 1]}
                        else:
                            state = {"original_query": case["query"], "mode": "live"}
                            state.update(await resolve_intent(state, {}, runtime))
                            prediction = await resolve_domain_intent(state, {}, runtime)
                            row["prediction"] = prediction
                            if case["group"] == "clarification":
                                result = await lab.clarification(case, prediction)
                                row["graph"] = result
                                row["passed"], row["metrics"] = clarification_score(
                                    case["expected"]["missing_field"], result["missing_field"])
                            else:
                                row["passed"], row["metrics"] = score_decision(case, prediction, anchor)
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                    row["metrics"] = ({"plan_exact": [0, 1]} if case["group"] == "planner" else error_metrics(case))
                row["elapsed_ms"] = round((monotonic() - started) * 1000)
                events = [{"type": item["type"], **json.loads(item["data_json"])}
                    for item in await db.fetchall("SELECT type,data_json FROM events WHERE task_id=? ORDER BY seq", (case["id"],))]
                row["attempts"] = attempts_from_events(events)
                row["domain_prechecks"] = [item for item in events if item["type"] == "domain.precheck.completed"]
                rows.append(row)
                with (args.output / "results.jsonl").open("a") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                (args.output / "summary.json").write_text(json.dumps(summary(rows, manifest), ensure_ascii=False, indent=2))
                print(json.dumps({key: row[key] for key in ("id", "group", "passed", "elapsed_ms")}), flush=True)
    finally:
        settings.default_current_location = original_location
        if router:
            await router.close()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groups", default=",".join(SOURCES))
    parser.add_argument("--timeout", type=float, default=75)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
