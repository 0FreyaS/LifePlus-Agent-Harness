"""Explicitly gated, planner-only regression runner; defaults to no API calls."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from .deepseek_baseline import ForcePlanner, PLANNER_NOW
from .jev_ab import percentile, usage_summary
from .planner_binding_cases import ROOT, DATASET
from .planner_cases import grade_goals
from .planner_thinking_ab import PlannerMeter


def summarize(rows, expected):
    scopes = {}
    for scope in sorted({item["scope"] for item in expected}):
        subset = [row for row in rows if row["scope"] == scope]
        scopes[scope] = {"expected": sum(item["scope"] == scope for item in expected),
            "submitted": len(subset), "passed": sum(row["passed"] for row in subset),
            "p50_ms": percentile([row["elapsed_ms"] for row in subset], .5),
            "p95_ms": percentile([row["elapsed_ms"] for row in subset], .95),
            "failed_ids": [row["id"] for row in subset if not row["passed"]],
            **usage_summary([attempt for row in subset for attempt in row["attempts"]])}
    return {"scopes": scopes, "submitted": len(rows), "expected": len(expected), "complete": len(rows) == len(expected),
            "scope": "planner_contract_not_tool_execution_or_end_to_end_completion"}


async def run(args):
    from car_agent.config import get_settings
    from car_agent.model_roles import build_models
    from car_agent.persistence import Database
    from car_agent.runtime_limits import RuntimeLimits, execution_scope
    from car_agent.task_planning import plan_tasks

    settings = get_settings()
    base, roles = build_models(settings)
    planner = roles.get("planner", base)
    if settings.model_thinking != "disabled" or planner is None or planner.model_name != "deepseek-flash":
        raise ValueError("This regression requires the unchanged DeepSeek baseline with thinking disabled")
    if args.output is None or args.output.exists():
        raise ValueError("Provide a new output directory")
    raw = args.dataset.read_bytes()
    cases = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    if len(cases) != len({item["id"] for item in cases}):
        raise ValueError("Duplicate case IDs")
    args.output.mkdir(parents=True)
    started_at = datetime.now(timezone.utc).isoformat()
    manifest = {"started_at": started_at, "model": planner.model_name, "thinking": "disabled",
        "effective_max_tokens": planner.max_tokens, "request_timeout_seconds": 25,
        "planning_deadline_seconds": settings.runtime_planning_seconds, "structured_attempts_max": 2,
        "sdk_retries": 0, "concurrency": 1, "frozen_now": PLANNER_NOW.isoformat(),
        "dataset_sha256": hashlib.sha256(raw).hexdigest(), "planned": len(cases),
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path(__file__), ROOT / "src/car_agent/task_planning.py", ROOT / "src/car_agent/structured_output.py")}}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (args.output / "cases.jsonl").write_bytes(raw)
    db, rows = Database(str(args.output / "audit.sqlite3")), []
    await db.open()
    try:
        for item in cases:
            cid = item["id"]
            await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (cid, cid, "binding-regression", item["query"], "running", "live", started_at, started_at))
            meter, started = PlannerMeter(planner), monotonic()
            row = {"id": cid, "scope": item["scope"], "group": item["group"], "passed": False}
            try:
                async with execution_scope(db, cid, RuntimeLimits.from_settings(settings), "planning"):
                    plan = await plan_tasks(item["query"], "live", meter, item.get("memory_brief", ""),
                        variant=settings.planner_variant, now=PLANNER_NOW, gate=ForcePlanner())
                    grade = grade_goals(item, plan)
                    row.update(passed=grade["passed"], grade=grade, prediction=plan.model_dump() if plan else None)
            except Exception as exc:
                row["error_type"] = type(exc).__name__
            row.update(elapsed_ms=round((monotonic() - started) * 1000), attempts=meter.attempts)
            rows.append(row)
            with (args.output / "results.jsonl").open("a") as file:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
            (args.output / "summary.json").write_text(json.dumps(summarize(rows, cases), ensure_ascii=False, indent=2))
            print(json.dumps({key: row[key] for key in ("id", "scope", "passed", "elapsed_ms")}), flush=True)
    finally:
        await db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / DATASET)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-live", action="store_true")
    args = parser.parse_args(argv)
    if not args.run_live:
        print(json.dumps({"status": "not_started", "paid_calls": 0, "require": "--run-live --output <new-directory>"}))
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
