"""Planner-only thinking experiment; no deployment switch or tool execution."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path
from time import monotonic

from pydantic import ValidationError

from car_agent.config import get_settings
from car_agent.model_roles import build_models
from car_agent.persistence import Database
from car_agent.runtime_limits import RuntimeLimits, execution_scope
from car_agent.task_planning import plan_tasks
from .deepseek_baseline import ForcePlanner, PLANNER_NOW
from .jev_ab import percentile, usage_summary
from .planner_cases import grade_goals, task, case

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "测试回报/测试数据集/03-规划与依赖/规划与依赖30条.jsonl"
ARMS = ("disabled", "low", "high")
RUN = Path("evaluations/runs/planner-thinking-20261030-01")
PUBLIC = Path("测试数据集/结果记录/2026-10-30-planner-thinking-01")
DOC_URL = "https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/"


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def cases():
    main = [{**item, "scope": "main30"} for item in read_rows(DATASET)]
    diagnostics = [{**case(f"same-domain-{index}", "same_domain_diagnostic",
        f"查{city}今天的天气，同时查看我的提醒和行程清单", [
            task("w", "weather", "weather_lookup"),
            task("r", "planning", "list_reminders", query_terms=["提醒"]),
            task("i", "planning", "list_itineraries", query_terms=["行程"])]),
        "scope": "diagnostic3"} for index, city in enumerate(("北京", "成都", "深圳"), 1)]
    return main + diagnostics


def thinking_client(base, arm, max_tokens=None):
    if arm not in ARMS:
        raise ValueError("Unknown thinking arm")
    update = {"extra_body": {**(base.extra_body or {}),
        "thinking": {"type": "disabled" if arm == "disabled" else "enabled"}},
        "reasoning_effort": None if arm == "disabled" else arm}
    if max_tokens is not None:
        update["max_tokens"] = max_tokens
    return base.model_copy(update=update)


def validation_diagnostics(error):
    found, seen = [], set()
    while error is not None and id(error) not in seen and len(seen) < 8:
        seen.add(id(error))
        if isinstance(error, ValidationError):
            found.extend({"field": ".".join(map(str, item["loc"])), "type": item["type"],
                          "message": item["msg"]} for item in error.errors(include_input=False, include_url=False))
        error = error.__cause__ or error.__context__
    return found[:8]


class PlannerMeter:
    def __init__(self, model):
        self.model, self.model_name, self.attempts = model, model.model_name, []

    def with_structured_output(self, *args, **kwargs):
        runnable, owner = self.model.with_structured_output(*args, **kwargs), self

        class Call:
            async def ainvoke(self, messages):
                started = monotonic()
                record = {"stage": "plan", "model_name": owner.model_name,
                          "attempt": len(owner.attempts) + 1, "usage_known": False}
                try:
                    result = await runnable.ainvoke(messages)
                    raw = result.get("raw") if isinstance(result, dict) else None
                    record["usage"] = getattr(raw, "usage_metadata", None) or {}
                    record["usage_known"] = bool(record["usage"])
                    record["finish_reason"] = (getattr(raw, "response_metadata", None) or {}).get("finish_reason")
                    # Preserve final JSON for diagnosis; never persist hidden reasoning text.
                    content = getattr(raw, "content", None)
                    if isinstance(content, str):
                        record["final_content"] = content[:20000]
                    parsing_error = result.get("parsing_error") if isinstance(result, dict) else None
                    record["parse_failed"] = bool(parsing_error)
                    if parsing_error:
                        record["validation_errors"] = validation_diagnostics(parsing_error)
                    return result
                except BaseException as exc:
                    record["error_type"] = type(exc).__name__
                    record["validation_errors"] = validation_diagnostics(exc)
                    raise
                finally:
                    record["elapsed_ms"] = round((monotonic() - started) * 1000)
                    owner.attempts.append(record)

        return Call()


def summarize(rows, manifest):
    scopes = {}
    for scope in ("main30", "diagnostic3"):
        arms = {}
        for arm in ARMS:
            items = [row for row in rows if row["arm"] == arm and row["scope"] == scope]
            attempts = [attempt for row in items for attempt in row["attempts"]]
            arms[arm] = {"submitted": len(items), "passed": sum(row["passed"] for row in items),
                "p50_ms": percentile([row["elapsed_ms"] for row in items], .5),
                "p95_ms": percentile([row["elapsed_ms"] for row in items], .95),
                "repairs": sum(max(0, len(row["attempts"]) - 1) for row in items),
                "first_attempt_parse_failures": sum(bool(row["attempts"] and row["attempts"][0].get("parse_failed")) for row in items),
                "length_attempts": sum(attempt.get("finish_reason") == "length" or attempt.get("error_type") == "LengthFinishReasonError" for attempt in attempts),
                "runtime_errors": sum(bool(row.get("error_type")) for row in items),
                "failed_ids": [row["id"] for row in items if not row["passed"]],
                "groups": {group: {"submitted": sum(row["group"] == group for row in items),
                    "passed": sum(row["group"] == group and row["passed"] for row in items)}
                    for group in sorted({row["group"] for row in items})}, **usage_summary(attempts)}
        paired = {}
        for arm in ("low", "high"):
            index = {(row["id"], row["arm"]): row for row in rows if row["scope"] == scope}
            ids = sorted({cid for cid, name in index if name == "disabled" and (cid, arm) in index})
            paired[arm] = {"pairs": len(ids),
                "rescued": [cid for cid in ids if not index[cid, "disabled"]["passed"] and index[cid, arm]["passed"]],
                "regressed": [cid for cid in ids if index[cid, "disabled"]["passed"] and not index[cid, arm]["passed"]]}
        scopes[scope] = {"arms": arms, "paired": paired}
    return {"manifest": manifest, "scopes": scopes, "submitted": len(rows),
        "complete": len(rows) == manifest["planned_submissions"],
        "auto_enabled": False,
        "total_usage": usage_summary([attempt for row in rows for attempt in row["attempts"]])}


async def run(args):
    settings = get_settings()
    base, roles = build_models(settings)
    planner = roles.get("planner", base)
    if planner is None or planner.model_name != "deepseek-flash" or settings.model_thinking != "disabled":
        raise ValueError("Expected the unchanged approved DeepSeek baseline")
    if args.output.exists():
        raise ValueError("Use a new experiment version")
    all_cases = cases()
    if args.ids:
        ids = set(args.ids.split(","))
        all_cases = [item for item in all_cases if item["id"] in ids]
        if len(all_cases) != len(ids):
            raise ValueError("Unknown case ID")
    cap = args.max_tokens or planner.max_tokens
    clients = {arm: thinking_client(planner, arm, cap) for arm in ARMS}
    manifest = {"started_at": datetime.now(timezone.utc).isoformat(), "model": planner.model_name,
        "changed_role": "planner_only", "arms": list(ARMS), "seed": args.seed, "repeats": 1,
        "concurrency": 1, "planned_submissions": len(all_cases) * len(ARMS),
        "effective_max_tokens": cap, "original_effective_max_tokens": planner.max_tokens,
        "configured_planner_max_tokens": settings.planner_max_output_tokens,
        "request_timeout_seconds": 25, "planning_deadline_seconds": settings.runtime_planning_seconds,
        "sdk_retries": 0, "structured_attempts_max": 2, "thinking_doc": DOC_URL,
        "frozen_now": PLANNER_NOW.isoformat(), "planner_variant": settings.planner_variant,
        "scope": "forced_global_planner_no_tools_not_end_to_end; diagnostic3_is_posthoc",
        "other_roles_thinking": settings.model_thinking,
        "dataset_sha256": hashlib.sha256(json.dumps(all_cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [Path(__file__), ROOT / "src/car_agent/task_planning.py", ROOT / "src/car_agent/structured_output.py",
                         ROOT / "src/car_agent/model_roles.py", ROOT / "evaluations/planner_cases.py"]},
        "auto_enable": False}
    args.output.mkdir(parents=True)
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (args.output / "cases.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in all_cases))
    db, rows = Database(str(args.output / "audit.sqlite3")), []
    await db.open()
    orders = list(permutations(ARMS))
    rng = random.Random(args.seed)
    main = [item for item in all_cases if item["scope"] == "main30"]
    diagnostic = [item for item in all_cases if item["scope"] == "diagnostic3"]
    rng.shuffle(main)
    rng.shuffle(diagnostic)
    try:
        for index, item in enumerate(main + diagnostic):
            for arm in orders[index % len(orders)]:
                task_id = item["id"] + "-" + arm
                await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (task_id, task_id, "planner-thinking-eval", item["query"], "running", "live", manifest["started_at"], manifest["started_at"]))
                meter, started = PlannerMeter(clients[arm]), monotonic()
                row = {"id": item["id"], "group": item["group"], "scope": item["scope"],
                       "arm": arm, "passed": False, "query": item["query"]}
                try:
                    async with execution_scope(db, task_id, RuntimeLimits.from_settings(settings), "planning"):
                        plan = await plan_tasks(item["query"], "live", meter, item.get("memory_brief", ""),
                            now=PLANNER_NOW, variant=settings.planner_variant, gate=ForcePlanner())
                        grade = grade_goals(item, plan)
                        row.update(passed=grade["passed"], grade=grade, prediction=plan.model_dump() if plan else None)
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                    row["validation_errors"] = validation_diagnostics(exc)
                row.update(elapsed_ms=round((monotonic() - started) * 1000), attempts=meter.attempts)
                rows.append(row)
                with (args.output / "results.jsonl").open("a") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                (args.output / "summary.json").write_text(json.dumps(summarize(rows, manifest), ensure_ascii=False, indent=2))
                print(json.dumps({key: row[key] for key in ("id", "arm", "passed", "elapsed_ms")}), flush=True)
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=RUN)
    parser.add_argument("--seed", type=int, default=31030)
    parser.add_argument("--ids")
    parser.add_argument("--max-tokens", type=int, choices=(2048, 8192))
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
