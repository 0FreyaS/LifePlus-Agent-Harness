"""Compare the frozen pre-fix planner with current code in an isolated process."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from types import ModuleType

from .deepseek_baseline import ForcePlanner, PLANNER_NOW
from .planner_binding_cases import ROOT, DATASET
from .planner_binding_eval import summarize as single_summary
from .planner_cases import grade_goals
from .planner_thinking_ab import PlannerMeter

SOURCE_PATHS = ("src/car_agent/task_planning.py", "src/car_agent/structured_output.py")
BASELINE_REF = "ffb084a5ecbd8a295c7b727c5b54cca666236aa7"


def before_fix_sources():
    ref = BASELINE_REF
    expected = json.loads((ROOT / "测试回报/测试数据集/结果记录/2026-10-30-01/组件运行条件.json").read_text())["sources"]
    sources = {path: subprocess.check_output(["git", "show", ref + ":" + path], cwd=ROOT, text=True) for path in SOURCE_PATHS}
    if any(hashlib.sha256(text.encode()).hexdigest() != expected[path] for path, text in sources.items()):
        raise ValueError("Frozen ref no longer matches the measured pre-fix source; do not silently change the baseline")
    return ref, sources


def load_before_fix(sources):
    modules = {}
    for filename in ("structured_output", "task_planning"):
        name = "car_agent._binding_ab_before_" + filename
        module = ModuleType(name)
        module.__package__ = "car_agent"
        sys.modules[name] = module
        source = sources["src/car_agent/" + filename + ".py"]
        exec(compile(source, "<frozen-before-fix:" + filename + ">", "exec"), module.__dict__)
        modules[filename] = module
    # Both versions use the same host budgets and clients; only these two sources differ.
    modules["task_planning"].invoke_structured = modules["structured_output"].invoke_structured
    return modules["task_planning"].plan_tasks


def summarize(rows, cases):
    paired = {}
    for scope in sorted({item["scope"] for item in cases}):
        index = {(row["id"], row["arm"]): row for row in rows if row["scope"] == scope}
        ids = sorted({cid for cid, arm in index if arm == "baseline" and (cid, "candidate") in index})
        paired[scope] = {"pairs": len(ids),
            "rescued": [cid for cid in ids if not index[cid, "baseline"]["passed"] and index[cid, "candidate"]["passed"]],
            "regressed": [cid for cid in ids if index[cid, "baseline"]["passed"] and not index[cid, "candidate"]["passed"]]}
    return {"arms": {arm: single_summary([row for row in rows if row["arm"] == arm], cases)
                     for arm in ("baseline", "candidate")}, "paired": paired,
            "submitted": len(rows), "complete": len(rows) == len(cases) * 2,
            "scope": "planner_only_not_end_to_end; thinking_disabled_both_arms"}


async def run(args):
    from car_agent.config import get_settings
    from car_agent.model_roles import build_models
    from car_agent.persistence import Database
    from car_agent.runtime_limits import RuntimeLimits, execution_scope
    from car_agent.task_planning import plan_tasks

    if args.output is None or args.output.exists():
        raise ValueError("Provide a new output directory")
    settings = get_settings()
    base, roles = build_models(settings)
    model = roles.get("planner", base)
    if model is None or model.model_name != "deepseek-flash" or settings.model_thinking != "disabled":
        raise ValueError("Keep the approved DeepSeek model and thinking disabled")
    ref, sources = before_fix_sources()
    old_plan = load_before_fix(sources)
    raw = args.dataset.read_bytes()
    cases = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    if len(cases) != 37 or len({item["id"] for item in cases}) != 37:
        raise ValueError("Expected the frozen 7+30 dataset")
    args.output.mkdir(parents=True)
    started_at = datetime.now(timezone.utc).isoformat()
    manifest = {"started_at": started_at, "model": model.model_name, "thinking": "disabled",
        "baseline_ref": ref, "concurrency": 1, "seed": args.seed, "repeats": 1, "planned_submissions": 74,
        "max_tokens": model.max_tokens, "request_timeout_seconds": 25, "planning_deadline_seconds": settings.runtime_planning_seconds,
        "sdk_retries": 0, "structured_attempts_max": 2, "frozen_now": PLANNER_NOW.isoformat(),
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "baseline_sources": {path: hashlib.sha256(text.encode()).hexdigest() for path, text in sources.items()},
        "candidate_sources": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SOURCE_PATHS},
        "changes": "visible_binding_contract, domain_validation, precise_repair_feedback, atomic_goal_prompt",
        "scope": "forced_planner_contract_no_tools_or_production_deployment"}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (args.output / "cases.jsonl").write_bytes(raw)
    for path, text in sources.items():
        target = args.output / "baseline_source" / Path(path).name
        target.parent.mkdir(exist_ok=True)
        target.write_text(text)
    db, rows = Database(str(args.output / "audit.sqlite3")), []
    await db.open()
    jobs = list(cases)
    random.Random(args.seed).shuffle(jobs)
    try:
        for index, item in enumerate(jobs):
            for arm in (("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")):
                cid = item["id"] + "-" + arm
                await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (cid, cid, "binding-fix-ab", item["query"], "running", "live", started_at, started_at))
                meter, started = PlannerMeter(model), monotonic()
                row = {"id": item["id"], "arm": arm, "scope": item["scope"], "group": item["group"], "passed": False}
                try:
                    async with execution_scope(db, cid, RuntimeLimits.from_settings(settings), "planning"):
                        plan = await (old_plan if arm == "baseline" else plan_tasks)(item["query"], "live", meter,
                            item.get("memory_brief", ""), variant=settings.planner_variant, now=PLANNER_NOW, gate=ForcePlanner())
                        grade = grade_goals(item, plan)
                        row.update(passed=grade["passed"], grade=grade, prediction=plan.model_dump() if plan else None)
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                row.update(elapsed_ms=round((monotonic() - started) * 1000), attempts=meter.attempts)
                rows.append(row)
                with (args.output / "results.jsonl").open("a") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                (args.output / "summary.json").write_text(json.dumps(summarize(rows, cases), ensure_ascii=False, indent=2))
                print(json.dumps({key: row[key] for key in ("id", "arm", "passed", "elapsed_ms")}), flush=True)
    finally:
        await db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / DATASET)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=31031)
    parser.add_argument("--run-live", action="store_true")
    args = parser.parse_args(argv)
    if not args.run_live:
        print(json.dumps({"status": "not_started", "paid_calls": 0, "require": "--run-live --output <new-directory>"}))
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
